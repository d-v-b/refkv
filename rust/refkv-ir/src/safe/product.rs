//! A SAFE product's metadata (spec/virtualize/safe.md §2.2
//! <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/safe.md#22-the-product-metadata>–§2.5
//! <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/safe.md#25-band-files>): the product
//! metadata's image files, the tile geocoding, band names and resolutions; and the source
//! metadata the convention copies from the XML (§6.1
//! <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/safe.md#61-values>–§6.3
//! <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/safe.md#63-the-source-metadata-node>)
//! and its GeoZarr grids (§5 <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/safe.md#5-georeferencing-geozarr>).

use super::xmltree::{self, Element};
use crate::rules::{self, Rules, flag, num, text};
use serde_json::{Map, Value as J, json};

pub const MAX_XML: u64 = 1 << 24;
const MAX_SAFE: u64 = (1 << 53) - 1;
const MAX_RECORDS: usize = 64;
const WS: &[char] = &[' ', '\t', '\r', '\n'];
const GRANULE: &str = "General_Info/Product_Info/Product_Organisation/Granule_List/Granule";

/// (level, product root element, tile root element) of a product metadata key.
pub const LEVELS: [(&str, &str, &str, &str); 2] = [
    ("MTD_MSIL1C.xml", "L1C", "Level-1C_User_Product", "Level-1C_Tile_ID"),
    ("MTD_MSIL2A.xml", "L2A", "Level-2A_User_Product", "Level-2A_Tile_ID"),
];

/// The names of PSD 14.2 that stand for later ones (§2.1
/// <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/safe.md#21-objects>).
fn alias(name: &str) -> &str {
    match name {
        "Product_Info" => "L2A_Product_Info",
        "Product_Organisation" => "L2A_Product_Organisation",
        "IMAGE_FILE" => "IMAGE_FILE_2A",
        "PRODUCT_URI" => "PRODUCT_URI_2A",
        "Product_Image_Characteristics" => "L2A_Product_Image_Characteristics",
        "QUANTIFICATION_VALUES_LIST" => "L1C_L2A_Quantification_Values_List",
        "BOA_QUANTIFICATION_VALUE" => "L2A_BOA_QUANTIFICATION_VALUE",
        "AOT_QUANTIFICATION_VALUE" => "L2A_AOT_QUANTIFICATION_VALUE",
        "WVP_QUANTIFICATION_VALUE" => "L2A_WVP_QUANTIFICATION_VALUE",
        "Scene_Classification_List" => "L2A_Scene_Classification_List",
        "Scene_Classification_ID" => "L2A_Scene_Classification_ID",
        "SCENE_CLASSIFICATION_TEXT" => "L2A_SCENE_CLASSIFICATION_TEXT",
        "SCENE_CLASSIFICATION_INDEX" => "L2A_SCENE_CLASSIFICATION_INDEX",
        "TILE_ID" => "TILE_ID_2A",
        n => n,
    }
}

/// An element's local name: its name after the last `:`.
fn local(name: &str) -> &str {
    name.rsplit(':').next().unwrap_or(name)
}

/// The children of `el` whose local name is `name`, or its PSD 14.2 alias.
pub fn kids<'a>(el: &'a Element, name: &str) -> Vec<&'a Element> {
    let a = alias(name);
    el.children.iter().filter(|c| {
        let l = local(&c.name);
        l == name || l == a
    }).collect()
}

/// The elements at the path `p` (local names separated by `/`) under `el`.
pub fn path<'a>(el: &'a Element, p: &str) -> Vec<&'a Element> {
    let mut found = vec![el];
    for name in p.split('/') {
        found = found.into_iter().flat_map(|e| kids(e, name)).collect();
    }
    found
}

/// An element's text, without leading and trailing whitespace; None when it has a child.
pub fn el_text(el: &Element) -> Option<String> {
    if !el.children.is_empty() {
        return None;
    }
    Some(el.text.concat().trim_matches(WS).to_string())
}

fn one<'a>(els: Vec<&'a Element>, what: &str) -> Result<&'a Element, String> {
    if els.len() != 1 {
        return Err(format!("{what}: {} elements where there must be one", els.len()));
    }
    Ok(els[0])
}

fn layout_text(el: &Element, what: &str) -> Result<String, String> {
    el_text(el).ok_or(format!("{what} has a child element"))
}

/// A decimal number (§6.1 <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/safe.md#61-values>):
/// an optional sign, digits, optionally `.` and digits, optionally an exponent.
pub fn is_decimal(t: &str) -> bool {
    let b = t.as_bytes();
    let mut i = 0;
    let digits = |i: &mut usize| {
        let s = *i;
        while *i < b.len() && b[*i].is_ascii_digit() {
            *i += 1;
        }
        *i > s
    };
    if i < b.len() && (b[i] == b'+' || b[i] == b'-') {
        i += 1;
    }
    if !digits(&mut i) {
        return false;
    }
    if i < b.len() && b[i] == b'.' {
        i += 1;
        if !digits(&mut i) {
            return false;
        }
    }
    if i < b.len() && (b[i] == b'e' || b[i] == b'E') {
        i += 1;
        if i < b.len() && (b[i] == b'+' || b[i] == b'-') {
            i += 1;
        }
        if !digits(&mut i) {
            return false;
        }
    }
    i == b.len()
}

/// A decimal number's binary64 value, or None if `t` is not one.
fn decimal(t: &str) -> Option<f64> {
    if !is_decimal(t) {
        return None;
    }
    t.parse::<f64>().ok()
}

fn is_positive(t: &str) -> bool {
    let b = t.as_bytes();
    !b.is_empty() && (b'1'..=b'9').contains(&b[0]) && b.iter().all(|c| c.is_ascii_digit())
}

/// A product's metadata document: UTF-8 and in the XML subset (§2.1
/// <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/safe.md#21-objects>).
pub fn parse_document(data: &[u8], what: &str) -> Result<Element, String> {
    if data.starts_with(b"\xef\xbb\xbf") {
        return Err(format!("{what} starts with a byte order mark"));
    }
    let s = std::str::from_utf8(data).map_err(|_| format!("{what} is not UTF-8"))?;
    xmltree::parse(s, what)
}

/// The key of a product's metadata and the level (§2.2
/// <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/safe.md#22-the-product-metadata>):
/// index into [`LEVELS`].
pub fn product_level(rules: &Rules, has: &dyn Fn(&str) -> bool) -> Result<usize, String> {
    let found: Vec<usize> = (0..2).filter(|&k| has(LEVELS[k].0)).collect();
    rules.check("product", &mut rules::env([("metadata", num(found.len() as f64)), ("manifest", flag(has("manifest.safe")))]))?;
    Ok(found[0])
}

pub struct Image {
    /// the image file F
    pub text: String,
    /// its band file, F.jp2
    pub key: String,
    pub basename: String,
}

/// The image files of the product metadata whose root is `root` (§2.2
/// <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/safe.md#22-the-product-metadata>, §2.3
/// <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/safe.md#23-image-files>): the bands,
/// and the granule directory `<g>`.
pub fn images(rules: &Rules, root: &Element, level: usize, has: &dyn Fn(&str) -> bool) -> Result<(Vec<Image>, String), String> {
    let (key, _, root_name, _) = LEVELS[level];
    rules.check("root_element", &mut rules::env([("what", text(key)), ("found", text(local(&root.name))), ("expected", text(root_name))]))?;
    let files: Vec<&Element> = path(root, GRANULE).into_iter().flat_map(|g| kids(g, "IMAGE_FILE")).collect();
    if files.is_empty() {
        return Err(format!("{key}: no granule has an IMAGE_FILE"));
    }
    let mut images = Vec::new();
    let mut seen = std::collections::HashSet::new();
    let mut g: Option<String> = None;
    for el in files {
        let f = layout_text(el, &format!("{key}: an IMAGE_FILE"))?;
        let parts: Vec<&str> = f.split('/').collect();
        let short: String = f.chars().take(200).collect();
        let form = parts.len() >= 4 && parts[0] == "GRANULE" && !parts.contains(&"");
        let other = form && g.as_deref().is_some_and(|x| x != parts[1]);
        if form && g.is_none() {
            g = Some(parts[1].to_string());
        }
        let twice = !seen.insert(f.clone());
        let band = form && parts[2] == "IMG_DATA";
        let present = has(&format!("{f}.jp2"));
        rules.check("image_file", &mut rules::env([("what", text(key)), ("file", text(&short)), ("form", flag(form)),
            ("other_granule", flag(other)), ("twice", flag(twice)), ("band", flag(band)), ("present", flag(present))]))?;
        if band {
            images.push(Image { key: format!("{f}.jp2"), basename: parts[parts.len() - 1].to_string(), text: f });
        }
    }
    rules.check("bands", &mut rules::env([("what", text(key)), ("bands", num(images.len() as f64))]))?;
    Ok((images, g.unwrap_or_default()))
}

pub struct Geocoding {
    pub code: String,
    /// (r, NROWS, NCOLS) in document order
    pub sizes: Vec<(u64, u64, u64)>,
    /// r -> (ULX, ULY, XDIM, YDIM)
    pub positions: Vec<(u64, [f64; 4])>,
    pub cs_name: Option<String>,
    pub cs_code: Option<String>,
}

fn u32_text(rules: &Rules, t: &str, what: &str) -> Result<u64, String> {
    let ok = is_positive(t) && t.len() <= 10 && t.parse::<u64>().is_ok_and(|v| v <= 0xFFFF_FFFF);
    let short: String = t.chars().take(40).collect();
    rules.check("u32_text", &mut rules::env([("what", text(what)), ("t", text(&format!("{short:?}"))), ("ok", flag(ok))]))?;
    Ok(t.parse().unwrap())
}

/// The tile geocoding (§2.4 <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/safe.md#24-the-tile-geocoding>).
pub fn geocoding(rules: &Rules, tile: &Element) -> Result<Geocoding, String> {
    let tg = one(path(tile, "Geometric_Info/Tile_Geocoding"), "MTD_TL.xml: Geometric_Info/Tile_Geocoding")?;
    let code = layout_text(one(kids(tg, "HORIZONTAL_CS_CODE"), "Tile_Geocoding/HORIZONTAL_CS_CODE")?, "HORIZONTAL_CS_CODE")?;
    let epsg = code.strip_prefix("EPSG:").is_some_and(|d| !d.is_empty() && d.bytes().all(|c| c.is_ascii_digit()));
    let short: String = code.chars().take(40).collect();
    rules.check("cs_code", &mut rules::env([("code", text(&format!("{short:?}"))), ("epsg", flag(epsg))]))?;
    let size_els = kids(tg, "Size");
    if size_els.is_empty() {
        return Err("Tile_Geocoding has no Size".into());
    }
    let mut sizes: Vec<(u64, u64, u64)> = Vec::new();
    let mut texts: Vec<(u64, String)> = Vec::new();
    for el in size_els {
        let rt = el.attr("resolution").ok_or("a Size has no resolution")?;
        let r = u32_text(rules, rt, "a Size's resolution")?;
        if sizes.iter().any(|s| s.0 == r) {
            return Err(format!("two Sizes have the resolution {r}"));
        }
        let rows = u32_text(rules, &layout_text(one(kids(el, "NROWS"), "Size/NROWS")?, "NROWS")?, "NROWS")?;
        let cols = u32_text(rules, &layout_text(one(kids(el, "NCOLS"), "Size/NCOLS")?, "NCOLS")?, "NCOLS")?;
        sizes.push((r, rows, cols));
        texts.push((r, rt.to_string()));
    }
    let geos = kids(tg, "Geoposition");
    let mut positions = Vec::new();
    for (r, rt) in &texts {
        let el = one(geos.iter().copied().filter(|g| g.attr("resolution") == Some(rt.as_str())).collect(), &format!("Geoposition of resolution {rt}"))?;
        let mut vals = [0f64; 4];
        for (k, name) in ["ULX", "ULY", "XDIM", "YDIM"].iter().enumerate() {
            let t = layout_text(one(kids(el, name), &format!("Geoposition/{name}"))?, name)?;
            let v = decimal(&t);
            let short: String = t.chars().take(40).collect();
            rules.check("geoposition", &mut rules::env([("name", text(name)), ("t", text(&format!("{short:?}"))),
                ("finite", flag(v.is_some_and(f64::is_finite)))]))?;
            vals[k] = v.unwrap();
        }
        rules.check("dims", &mut rules::env([("xdim", num(vals[2])), ("ydim", num(vals[3]))]))?;
        positions.push((*r, vals));
    }
    let string = |n: &str| -> Option<String> {
        let k = kids(tg, n);
        if k.len() == 1 { el_text(k[0]) } else { None }
    };
    Ok(Geocoding { code, sizes, positions, cs_name: string("HORIZONTAL_CS_NAME"), cs_code: string("HORIZONTAL_CS_CODE") })
}

/// A band file's band name (§2.5 <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/safe.md#25-band-files>),
/// and whether it is ASCII letters and digits.
pub fn band_name(basename: &str, r: u64) -> (String, bool) {
    let suffix = format!("_{r}m");
    let b = basename.strip_suffix(&suffix).unwrap_or(basename);
    let name = b.rsplit('_').next().unwrap_or(b);
    (name.to_string(), !name.is_empty() && name.bytes().all(|c| c.is_ascii_alphanumeric()))
}

// ---- the GeoZarr attributes (§5 <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/safe.md#5-georeferencing-geozarr>)

pub fn proj_cmo() -> J {
    json!({"uuid": "f17cb550-5864-4468-aeb7-f3180cfb622f",
           "schema_url": "https://raw.githubusercontent.com/zarr-conventions/proj/refs/tags/v0.1/schema.json",
           "spec_url": "https://github.com/zarr-conventions/proj/blob/v0.1/README.md",
           "name": "proj", "description": "Coordinate reference system information for geospatial data"})
}

pub fn spatial_cmo() -> J {
    json!({"uuid": "689b58e2-cf7b-45e0-9fff-9cfc0883d6b4",
           "schema_url": "https://raw.githubusercontent.com/zarr-conventions/spatial/refs/tags/v0.1/schema.json",
           "spec_url": "https://github.com/zarr-conventions/spatial/blob/v0.1/README.md",
           "name": "spatial", "description": "Spatial coordinate information"})
}

pub fn multiscales_cmo() -> J {
    json!({"uuid": "d35379db-88df-4056-af3a-620245f8e347",
           "schema_url": "https://raw.githubusercontent.com/zarr-conventions/multiscales/refs/tags/v0.1/schema.json",
           "spec_url": "https://github.com/zarr-conventions/multiscales/blob/v0.1/README.md",
           "name": "multiscales", "description": "Multiscale layout of zarr datasets"})
}

/// `proj:wkt2` for a WGS 84 / UTM zone's CRS code (§5.2
/// <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/safe.md#52-resolution-groups>), else None.
pub fn wkt2(code: &str) -> Option<String> {
    let d = code.strip_prefix("EPSG:")?;
    if d.len() != 5 || !d.bytes().all(|c| c.is_ascii_digit()) || d.starts_with('0') {
        return None;
    }
    let c: i64 = d.parse().ok()?;
    let (z, h) = if (32601..=32660).contains(&c) {
        (c - 32600, "N")
    } else if (32701..=32760).contains(&c) {
        (c - 32700, "S")
    } else {
        return None;
    };
    let deg = r#"ANGLEUNIT["degree",0.0174532925199433]"#;
    let m = r#"LENGTHUNIT["metre",1]"#;
    let lon = 6 * z - 183;
    let fn_ = if h == "N" { 0 } else { 10000000 };
    Some(format!(
        concat!(
            r#"PROJCRS["WGS 84 / UTM zone {z}{h}",BASEGEOGCRS["WGS 84",ENSEMBLE["World Geodetic System 1984 ensemble","#,
            r#"MEMBER["World Geodetic System 1984 (Transit)"],MEMBER["World Geodetic System 1984 (G730)"],"#,
            r#"MEMBER["World Geodetic System 1984 (G873)"],MEMBER["World Geodetic System 1984 (G1150)"],"#,
            r#"MEMBER["World Geodetic System 1984 (G1674)"],MEMBER["World Geodetic System 1984 (G1762)"],"#,
            r#"MEMBER["World Geodetic System 1984 (G2139)"],MEMBER["World Geodetic System 1984 (G2296)"],"#,
            r#"ELLIPSOID["WGS 84",6378137,298.257223563,{m}],ENSEMBLEACCURACY[2.0]],"#,
            r#"PRIMEM["Greenwich",0,{deg}],ID["EPSG",4326]],"#,
            r#"CONVERSION["UTM zone {z}{h}",METHOD["Transverse Mercator",ID["EPSG",9807]],"#,
            r#"PARAMETER["Latitude of natural origin",0,{deg},ID["EPSG",8801]],"#,
            r#"PARAMETER["Longitude of natural origin",{lon},{deg},ID["EPSG",8802]],"#,
            r#"PARAMETER["Scale factor at natural origin",0.9996,SCALEUNIT["unity",1],ID["EPSG",8805]],"#,
            r#"PARAMETER["False easting",500000,{m},ID["EPSG",8806]],"#,
            r#"PARAMETER["False northing",{fn_},{m},ID["EPSG",8807]]],"#,
            r#"CS[Cartesian,2],AXIS["(E)",east,ORDER[1],{m}],"#,
            r#"AXIS["(N)",north,ORDER[2],{m}],ID["EPSG",{c}]]"#
        ),
        z = z, h = h, m = m, deg = deg, lon = lon, fn_ = fn_, c = c
    ))
}

// ---- values (§6.1 <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/safe.md#61-values>)

fn float_json(v: f64) -> J {
    if v.is_nan() {
        return json!("NaN");
    }
    if v.is_infinite() {
        return json!(if v > 0.0 { "Infinity" } else { "-Infinity" });
    }
    json!(v)
}

/// A text as a value: a decimal number as a JSON number, else the string.
pub fn typed(t: &str) -> J {
    if !is_decimal(t) {
        return json!(t);
    }
    if t.contains(['.', 'e', 'E']) {
        return float_json(t.parse::<f64>().unwrap());
    }
    let sign = if t.starts_with('-') { "-" } else { "" };
    let d = t.trim_start_matches(['+', '-']).trim_start_matches('0');
    let digits = if d.is_empty() { "0" } else { d };
    if digits.len() > 16 || digits.parse::<u64>().is_ok_and(|v| v > MAX_SAFE) {
        return json!(format!("{sign}{digits}"));
    }
    let v: i64 = digits.parse().unwrap();
    json!(if sign == "-" { -v } else { v })
}

/// The value of the one element of `els`, or None (absent).
fn value(els: &[&Element]) -> Option<J> {
    if els.len() != 1 {
        return None;
    }
    el_text(els[0]).map(|t| typed(&t))
}

fn string(els: &[&Element]) -> Option<J> {
    if els.len() != 1 {
        return None;
    }
    el_text(els[0]).map(J::String)
}

/// A measure: {"value", "unit"} when the element has a unit, else its value.
fn measure(els: &[&Element]) -> Option<J> {
    let v = value(els)?;
    Some(match els[0].attr("unit") {
        Some(u) => json!({"value": v, "unit": u}),
        None => v,
    })
}

/// An index attribute read as an integer: one to nine digits.
fn index(t: Option<&str>) -> Option<u64> {
    let t = t?;
    (!t.is_empty() && t.len() <= 9 && t.bytes().all(|c| c.is_ascii_digit())).then(|| t.parse().unwrap())
}

fn by_index<'a>(els: Vec<&'a Element>, name: &str, i: u64) -> Vec<&'a Element> {
    els.into_iter().filter(|e| index(e.attr(name)) == Some(i)).collect()
}

fn put(out: &mut Map<String, J>, key: &str, v: Option<J>) {
    match v {
        Some(J::Object(m)) if m.is_empty() => {}
        Some(J::Array(a)) if a.is_empty() => {}
        Some(v) => {
            out.insert(key.to_string(), v);
        }
        None => {}
    }
}

/// One record per element, or None (absent) past 64.
fn records(els: &[&Element], names: &[&str]) -> Option<J> {
    if els.len() > MAX_RECORDS {
        return None;
    }
    Some(J::Array(els.iter().map(|el| {
        let mut rec = Map::new();
        for n in names {
            put(&mut rec, n, value(&kids(el, n)));
        }
        J::Object(rec)
    }).collect()))
}

fn characteristics(metadata: &Element) -> Option<&Element> {
    let found = path(metadata, "General_Info/Product_Image_Characteristics");
    (found.len() == 1).then(|| found[0])
}

/// A band array's source metadata (§6.2
/// <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/safe.md#62-band-arrays>).
pub fn band_metadata(metadata: &Element, l2a: bool, image_text: &str, name: &str, siz_b64: &str) -> J {
    let mut s = Map::new();
    s.insert("IMAGE_FILE".into(), json!(image_text));
    let x = characteristics(metadata);
    let nb = name.as_bytes();
    let pb = if nb.len() == 3 && nb[0] == b'B' && nb[1].is_ascii_digit() && nb[2].is_ascii_digit() {
        Some(if nb[1] == b'0' { format!("B{}", nb[2] as char) } else { name.to_string() })
    } else if name == "B8A" {
        Some("B8A".to_string())
    } else {
        None
    };
    if let (Some(x), Some(pb)) = (x, pb.as_deref()) {
        let infos: Vec<&Element> = path(x, "Spectral_Information_List/Spectral_Information").into_iter()
            .filter(|e| e.attr("physicalBand") == Some(pb) && index(e.attr("bandId")).is_some()).collect();
        if infos.len() == 1 {
            let info = infos[0];
            let i = index(info.attr("bandId")).unwrap();
            s.insert("bandId".into(), json!(i));
            s.insert("physicalBand".into(), json!(pb));
            put(&mut s, "RESOLUTION", value(&kids(info, "RESOLUTION")));
            let wl = kids(info, "Wavelength");
            if wl.len() == 1 {
                let mut w = Map::new();
                for n in ["MIN", "MAX", "CENTRAL"] {
                    put(&mut w, n, measure(&kids(wl[0], n)));
                }
                put(&mut s, "Wavelength", Some(J::Object(w)));
            }
            put(&mut s, "PHYSICAL_GAINS", value(&by_index(kids(x, "PHYSICAL_GAINS"), "bandId", i)));
            put(&mut s, "SOLAR_IRRADIANCE", measure(&by_index(path(x, "Reflectance_Conversion/Solar_Irradiance_List/SOLAR_IRRADIANCE"), "bandId", i)));
            if l2a {
                put(&mut s, "BOA_QUANTIFICATION_VALUE", measure(&path(x, "QUANTIFICATION_VALUES_LIST/BOA_QUANTIFICATION_VALUE")));
                put(&mut s, "BOA_ADD_OFFSET", value(&by_index(path(x, "BOA_ADD_OFFSET_VALUES_LIST/BOA_ADD_OFFSET"), "band_id", i)));
            } else {
                put(&mut s, "QUANTIFICATION_VALUE", measure(&kids(x, "QUANTIFICATION_VALUE")));
                put(&mut s, "RADIO_ADD_OFFSET", value(&by_index(path(x, "Radiometric_Offset_List/RADIO_ADD_OFFSET"), "band_id", i)));
            }
        }
    }
    if let (Some(x), true) = (x, l2a) {
        if name == "AOT" || name == "WVP" {
            put(&mut s, &format!("{name}_QUANTIFICATION_VALUE"), measure(&path(x, &format!("QUANTIFICATION_VALUES_LIST/{name}_QUANTIFICATION_VALUE"))));
        } else if name == "SCL" {
            put(&mut s, "Scene_Classification_List", records(&path(x, "Scene_Classification_List/Scene_Classification_ID"),
                &["SCENE_CLASSIFICATION_TEXT", "SCENE_CLASSIFICATION_INDEX"]));
        }
    }
    s.insert("siz".into(), json!(siz_b64));
    J::Object(s)
}

/// The root's source metadata (§6.3
/// <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/safe.md#63-the-source-metadata-node>).
pub fn root_metadata(metadata: &Element, tile: &Element, geo: &Geocoding) -> J {
    let mut s = Map::new();
    let info = path(metadata, "General_Info/Product_Info");
    for n in ["PRODUCT_URI", "PROCESSING_LEVEL", "PRODUCT_TYPE", "PROCESSING_BASELINE"] {
        let els: Vec<&Element> = info.iter().flat_map(|e| kids(e, n)).collect();
        put(&mut s, n, string(&els));
    }
    if let Some(x) = characteristics(metadata) {
        put(&mut s, "Special_Values", records(&kids(x, "Special_Values"), &["SPECIAL_VALUE_TEXT", "SPECIAL_VALUE_INDEX"]));
        put(&mut s, "U", value(&path(x, "Reflectance_Conversion/U")));
    }
    for n in ["TILE_ID", "SENSING_TIME"] {
        put(&mut s, n, string(&path(tile, &format!("General_Info/{n}"))));
    }
    put(&mut s, "HORIZONTAL_CS_NAME", geo.cs_name.clone().map(J::String));
    put(&mut s, "HORIZONTAL_CS_CODE", geo.cs_code.clone().map(J::String));
    J::Object(s)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn values() {
        for (t, want) in [("12", json!(12)), ("-0", json!(0)), ("+007", json!(7)), ("1.5", json!(1.5)), ("2E3", json!(2000.0)),
                          ("9007199254740992", json!("9007199254740992")), ("-12345678901234567", json!("-12345678901234567")),
                          ("1e999", json!("Infinity")), ("x", json!("x")), ("1.", json!("1.")), ("05.10", json!(5.1))] {
            assert_eq!(typed(t), want, "{t}");
        }
    }

    #[test]
    fn wkt2_of_utm_zones() {
        assert!(wkt2("EPSG:32632").unwrap().starts_with(r#"PROJCRS["WGS 84 / UTM zone 32N""#));
        assert!(wkt2("EPSG:32756").unwrap().contains(r#"PARAMETER["False northing",10000000,"#));
        assert!(wkt2("EPSG:32601").unwrap().contains(r#"PARAMETER["Longitude of natural origin",-177,"#));
        assert_eq!(wkt2("EPSG:4326"), None);
        assert_eq!(wkt2("EPSG:032632"), None);
    }

    #[test]
    fn band_names() {
        assert_eq!(band_name("T32TNS_20240103T101329_B02_20m", 20), ("B02".into(), true));
        assert_eq!(band_name("T32TNS_20240103T101329_B02", 10), ("B02".into(), true));
        assert_eq!(band_name("T32TNS_B-2", 10).1, false);
    }
}
