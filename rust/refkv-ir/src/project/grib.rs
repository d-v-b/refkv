//! The GRIB projection (spec/zarrify/grib.md §3
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#3-the-hierarchy>–§4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#4-georeferencing>, and the
//! chunks of the profile's §8.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#85-chunks>)
//! from a valid store IR and its facts: a group per grid, holding its coordinates and an array per
//! variable, whose chunks are the file's messages, one field each.

use super::vsi::object_url;
use super::{arr, n, with_counts};
use crate::grib::meta::gaussian_latitude;
use crate::grib::names;
use crate::ir::Ir;
use crate::out::{array_json, convention, gzip_array, group_doc, root_property, Out, Part, CONVENTION_KEY};
use crate::safe::product::{proj_cmo, spatial_cmo};
use serde_json::{json, Map, Value as J};
use std::collections::{BTreeMap, BTreeSet};

const PROFILE: &str = "grib";
/// The most values of a coordinate's chunk (conventions §9
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#9-dataset-layouts>).
const COORD_CHUNK: u64 = 1 << 17;
const DEGREE: &str = "ANGLEUNIT[\"degree\",0.0174532925199433]";

/// A finite f64 of the facts.
fn f(v: &J) -> f64 {
    v.as_f64().unwrap_or(f64::NAN)
}

/// A level value as an ordered key (its bits, NaN-free values compare as numbers).
fn level_key(v: f64) -> i128 {
    let b = v.to_bits() as i64;
    (if b < 0 { b ^ i64::MAX } else { b }) as i128
}

/// A duration's text: hours, minutes or seconds, whichever divides it.
fn duration(s: i64) -> String {
    if s % 3600 == 0 {
        format!("{}h", s / 3600)
    } else if s % 60 == 0 {
        format!("{}min", s / 60)
    } else {
        format!("{s}s")
    }
}

/// The name of a field's fixed surfaces (its level dimension's, when it has values), and its
/// units (spec/zarrify/grib.md §3.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#32-names>).
fn level_name(centre: u64, first: u64, second: u64) -> (String, Option<&'static str>) {
    let one = |k: u64| names::surface(centre, k as u8).map_or_else(|| (format!("level{k}"), None), |(n, u)| (n.to_string(), u));
    let (a, u) = one(first);
    if second == 255 {
        return (a, u);
    }
    if second == first {
        return (format!("{a}Layer"), u);
    }
    let (b, _) = one(second);
    let mut c = b.chars();
    let b = c.next().map(|x| x.to_ascii_uppercase().to_string() + c.as_str()).unwrap_or_default();
    (format!("{a}To{b}"), u)
}

/// A variable: the fields of one identity (spec/zarrify/grib.md §3.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#31-variables>).
struct Variable<'a> {
    fields: Vec<&'a J>,
    name: String,
    level: Option<String>,
    member: bool,
}

/// The CRS's WKT2 (spec/zarrify/grib.md §4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#4-georeferencing>), from a
/// grid's facts: its Earth and projection.
fn wkt2(g: &J) -> Option<String> {
    let e = &g["earth"];
    let ellipsoid = if let Some(r) = e["radius"].as_f64() {
        format!("ELLIPSOID[\"sphere\",{r},0,LENGTHUNIT[\"metre\",1]]")
    } else {
        format!("ELLIPSOID[\"ellipsoid\",{},{},LENGTHUNIT[\"metre\",1]]", e["a"].as_f64()?, e["inverse_flattening"].as_f64()?)
    };
    let geog = format!("DATUM[\"GRIB\",{ellipsoid}],PRIMEM[\"Greenwich\",0,{DEGREE}]");
    let p = &g["projection"];
    let par = |name: &str, v: f64, id: u32| format!("PARAMETER[\"{name}\",{v},{DEGREE},ID[\"EPSG\",{id}]]");
    let meters = |name: &str, id: u32| format!("PARAMETER[\"{name}\",0,LENGTHUNIT[\"metre\",1],ID[\"EPSG\",{id}]]");
    let projected = |method: &str, id: u32, parameters: String| {
        format!(
            "PROJCRS[\"GRIB\",BASEGEOGCRS[\"GRIB\",{geog}],CONVERSION[\"GRIB\",METHOD[\"{method}\",ID[\"EPSG\",{id}]],{parameters}],\
             CS[Cartesian,2],AXIS[\"easting\",east,ORDER[1],LENGTHUNIT[\"metre\",1]],AXIS[\"northing\",north,ORDER[2],LENGTHUNIT[\"metre\",1]]]"
        )
    };
    let ellipsoidal = format!("CS[ellipsoidal,2],AXIS[\"latitude\",north,ORDER[1],{DEGREE}],AXIS[\"longitude\",east,ORDER[2],{DEGREE}]");
    Some(match p["kind"].as_str()? {
        "latlon" | "gaussian" | "reduced" => format!("GEOGCRS[\"GRIB\",{geog},{ellipsoidal}]"),
        "rotated" => {
            let par = |name: &str, v: f64| format!("PARAMETER[\"{name} (GRIB convention)\",{v},{DEGREE}]");
            format!(
                "GEOGCRS[\"GRIB rotated\",BASEGEOGCRS[\"GRIB\",{geog}],DERIVINGCONVERSION[\"Pole rotation (GRIB convention)\",\
                 METHOD[\"Pole rotation (GRIB convention)\"],{},{},{}],{ellipsoidal}]",
                par("Latitude of the southern pole", f(&p["pole_lat"])),
                par("Longitude of the southern pole", lon180(f(&p["pole_lon"]))),
                par("Axis rotation", f(&p["angle"])),
            )
        }
        "lambert" => projected(
            "Lambert Conic Conformal (2SP)",
            9802,
            format!(
                "{},{},{},{},{},{}",
                par("Latitude of false origin", f(&p["latin1"]), 8821),
                par("Longitude of false origin", lon180(f(&p["lov"])), 8822),
                par("Latitude of 1st standard parallel", f(&p["latin1"]), 8823),
                par("Latitude of 2nd standard parallel", f(&p["latin2"]), 8824),
                meters("Easting at false origin", 8826),
                meters("Northing at false origin", 8827),
            ),
        ),
        "polar" => projected(
            "Polar Stereographic (variant B)",
            9829,
            format!(
                "{},{},{},{}",
                par("Latitude of standard parallel", f(&p["lad"]), 8832),
                // a south pole's `y` runs towards it along LoV (`polar`), so PROJ's along the meridian
                // opposite
                par("Longitude of origin", lon180(f(&p["lov"]) + if p["south"].as_bool().unwrap_or(false) { 180.0 } else { 0.0 }), 8833),
                meters("False easting", 8806),
                meters("False northing", 8807),
            ),
        ),
        "mercator" => projected(
            "Mercator (variant B)",
            9805,
            format!(
                "{},{},{},{}",
                par("Latitude of 1st standard parallel", f(&p["lad"]), 8823),
                par("Longitude of natural origin", 0.0, 8802),
                meters("False easting", 8806),
                meters("False northing", 8807),
            ),
        ),
        _ => return None,
    })
}

/// A longitude in [−180, 180).
fn lon180(l: f64) -> f64 {
    (l + 180.0).rem_euclid(360.0) - 180.0
}

/// `r(d)` of spec/zarrify/grib.md §4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#4-georeferencing>: degrees as radians.
fn radians(d: f64) -> f64 {
    d * (std::f64::consts::PI / 180.0)
}

/// An Earth's semi-major axis and eccentricity.
fn axis_and_eccentricity(earth: &J) -> (f64, f64) {
    match earth["radius"].as_f64() {
        Some(radius) => (radius, 0.0),
        None => {
            let a = f(&earth["a"]);
            let fl = 1.0 / f(&earth["inverse_flattening"]);
            (a, libm::sqrt(fl * (2.0 - fl)))
        }
    }
}

/// `m(x)` of spec/zarrify/grib.md §4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#4-georeferencing> (Snyder's 14-15).
fn big_m(x: f64, e: f64) -> f64 {
    libm::cos(x) / libm::sqrt(1.0 - e * e * libm::sin(x) * libm::sin(x))
}

/// `t(x)` of spec/zarrify/grib.md §4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#4-georeferencing> (Snyder's 15-9).
fn big_t(x: f64, e: f64) -> f64 {
    let s = libm::sin(x);
    libm::tan(std::f64::consts::FRAC_PI_4 - x / 2.0) / libm::pow((1.0 - e * s) / (1.0 + e * s), e / 2.0)
}

/// The Lambert conformal conic projection's forward map (Snyder, *Map Projections: A Working
/// Manual*, 1987, eqs. 15-1 to 15-10, 14-15): the easting and northing in meters of a latitude
/// and longitude in degrees, the origin at (`latin1`, `lov`), computed in the order written with
/// musl's libm functions (spec/zarrify/grib.md §4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#4-georeferencing>), so that every
/// host gets the same bits.
pub fn lambert(lat: f64, lon: f64, latin1: f64, latin2: f64, lov: f64, earth: &J) -> (f64, f64) {
    use libm::{log, pow, sin};
    let (p, p1, p2) = (radians(lat), radians(latin1), radians(latin2));
    let (a, e) = axis_and_eccentricity(earth);
    let m = |x: f64| big_m(x, e);
    let t = |x: f64| big_t(x, e);
    let n = if p1 == p2 { sin(p1) } else { (log(m(p1)) - log(m(p2))) / (log(t(p1)) - log(t(p2))) };
    let ff = m(p1) / (n * pow(t(p1), n));
    let rho = a * ff * pow(t(p), n);
    let rho0 = a * ff * pow(t(p1), n);
    let theta = n * radians(lon180(lon - lov));
    (rho * sin(theta), rho0 - rho * libm::cos(theta))
}

/// The polar stereographic projection's forward map (Snyder, eqs. 21-33 to 21-36, with 14-15 and
/// 15-9, for its polar aspect): the easting and northing in meters of a latitude and longitude in
/// degrees, true to scale at `lad`. Whichever the pole, `y` runs towards it along `lov` and `x` from
/// it towards the meridian 90° east of `lov` as seen from above that pole, as NCEP's south-polar
/// grids have it: Snyder's south pole with its central meridian at `lov + 180`.
pub fn polar(lat: f64, lon: f64, lad: f64, lov: f64, south: bool, earth: &J) -> (f64, f64) {
    use libm::{pow, sqrt};
    let sign = if south { -1.0 } else { 1.0 };
    let (a, e) = axis_and_eccentricity(earth);
    let t = big_t(radians(sign * lat), e);
    let rho = if lad.abs() == 90.0 {
        2.0 * a * t / sqrt(pow(1.0 + e, 1.0 + e) * pow(1.0 - e, 1.0 - e))
    } else {
        let c = radians(sign * lad);
        a * big_m(c, e) * t / big_t(c, e)
    };
    let theta = radians(lon180(lon - lov));
    (sign * rho * libm::sin(theta), -rho * libm::cos(theta))
}

/// The Mercator projection's forward map (Snyder, eqs. 7-6 and 7-7, with 14-15 and 15-9): the
/// easting and northing in meters of a latitude and longitude in degrees, true to scale at `lad`,
/// the central meridian Greenwich.
pub fn mercator(lat: f64, lon: f64, lad: f64, earth: &J) -> (f64, f64) {
    let (a, e) = axis_and_eccentricity(earth);
    let k = a * big_m(radians(lad), e);
    (k * radians(lon180(lon)), -k * libm::log(big_t(radians(lat), e)))
}

/// A dimension's coordinate.
enum Axis {
    /// the index of regular steps: its first value and step
    Regular(f64, f64),
    /// values listed, as the rows of a Gaussian grid
    Listed(Vec<f64>),
}

/// A dimension of a grid, and its coordinate, when it has one: its name, its length, its axis, its
/// units and its standard name.
struct Dimension {
    name: &'static str,
    len: u64,
    axis: Option<(Axis, &'static str, &'static str)>,
}

/// A grid's dimensions, each with its coordinate (y, then x; or one of points), and its affine
/// transform, when it has one.
fn axes(g: &J) -> (Vec<Dimension>, Option<J>) {
    let p = &g["projection"];
    let (ny, nx) = (n(&g["ny"]), n(&g["nx"]));
    let regular = |y: (&'static str, f64, f64, &'static str, &'static str), x: (&'static str, f64, f64, &'static str, &'static str)| {
        (
            vec![
                Dimension { name: y.0, len: ny, axis: Some((Axis::Regular(y.1, y.2), y.3, y.4)) },
                Dimension { name: x.0, len: nx, axis: Some((Axis::Regular(x.1, x.2), x.3, x.4)) },
            ],
            Some(json!([x.2, 0.0, x.1, 0.0, y.2, y.1])),
        )
    };
    let projected = |(x0, y0): (f64, f64)| {
        let (dx, dy) = (f(&p["dx"]), f(&p["dy"]));
        regular(("y", y0, dy, "m", "projection_y_coordinate"), ("x", x0, dx, "m", "projection_x_coordinate"))
    };
    let (lat0, lon0) = (f(&p["lat0"]), f(&p["lon0"]));
    match p["kind"].as_str().unwrap_or("") {
        "latlon" => regular(
            ("latitude", lat0, f(&p["dlat"]), "degrees_north", "latitude"),
            ("longitude", lon0, f(&p["dlon"]), "degrees_east", "longitude"),
        ),
        "rotated" => regular(
            ("rlat", lat0, f(&p["dlat"]), "degrees", "grid_latitude"),
            ("rlon", lon0, f(&p["dlon"]), "degrees", "grid_longitude"),
        ),
        "gaussian" => (
            vec![
                Dimension { name: "latitude", len: ny, axis: Some((Axis::Listed(gaussian_rows(&p["rows"], ny)), "degrees_north", "latitude")) },
                Dimension { name: "longitude", len: nx, axis: Some((Axis::Regular(lon0, f(&p["dlon"])), "degrees_east", "longitude")) },
            ],
            None,
        ),
        "lambert" => projected(lambert(lat0, lon0, f(&p["latin1"]), f(&p["latin2"]), f(&p["lov"]), &g["earth"])),
        "polar" => projected(polar(lat0, lon0, f(&p["lad"]), f(&p["lov"]), p["south"].as_bool().unwrap_or(false), &g["earth"])),
        "mercator" => projected(mercator(lat0, lon0, f(&p["lad"]), &g["earth"])),
        _ => (vec![Dimension { name: "point", len: n(&g["points"]), axis: None }], None),
    }
}

/// The latitudes of `count` rows of a Gaussian grid.
fn gaussian_rows(rows: &J, count: u64) -> Vec<f64> {
    let (parallels, first, step) = (n(&rows["parallels"]), n(&rows["first"]) as i64, rows["step"].as_i64().unwrap_or(1));
    (0..count as i64).map(|j| gaussian_latitude(parallels, (first + j * step) as u64)).collect()
}

/// The latitude and longitude of each point of a reduced grid, in the file's order.
fn reduced_positions(p: &J) -> (Vec<f64>, Vec<f64>) {
    let counts: Vec<u64> = arr(&p["counts"]).iter().map(n).collect();
    let rows = &p["rows"];
    let lat: Vec<f64> = match rows.get("parallels") {
        Some(_) => gaussian_rows(rows, counts.len() as u64),
        None => (0..counts.len()).map(|j| f(&rows["lat0"]) + j as f64 * f(&rows["dlat"])).collect(),
    };
    let lon0 = f(&p["lon0"]);
    let total = counts.iter().sum::<u64>() as usize;
    let (mut lats, mut lons) = (Vec::with_capacity(total), Vec::with_capacity(total));
    for (&c, &l) in counts.iter().zip(&lat) {
        let step = 360.0 / c as f64;
        for i in 0..c {
            lats.push(l);
            lons.push(lon0 + i as f64 * step);
        }
    }
    (lats, lons)
}

/// An array of float64 values the archive holds whole, little-endian and deflated.
fn values_array(out: &mut Out, path: &str, dims: &[&str], shape: &[u64], values: &[f64], attributes: J) {
    let codecs = json!([{"name": "bytes", "configuration": {"endian": "little"}}, {"name": "gzip", "configuration": {"level": crate::out::GZIP_LEVEL}}]);
    let mut doc = array_json(shape, "float64", json!(shape), codecs, dims);
    doc["fill_value"] = json!("NaN");
    doc["attributes"] = attributes;
    out.json(&format!("{path}/zarr.json"), &doc);
    let raw: Vec<u8> = values.iter().flat_map(|v| v.to_le_bytes()).collect();
    out.bytes(&format!("{path}/c/{}", vec!["0"; shape.len()].join("/")), crate::out::gzip(&raw));
}

/// A coordinate of a reduced grid: float64 values over `point`, little-endian and deflated, in
/// chunks of at most `COORD_CHUNK`.
fn point_array(out: &mut Out, path: &str, values: &[f64], units: &str, standard: &str) {
    let c = values.len().min(COORD_CHUNK as usize);
    let codecs = json!([{"name": "bytes", "configuration": {"endian": "little"}}, {"name": "gzip", "configuration": {"level": crate::out::GZIP_LEVEL}}]);
    let mut doc = array_json(&[values.len() as u64], "float64", json!([c]), codecs, &["point"]);
    doc["fill_value"] = json!("NaN");
    doc["attributes"] = json!({"units": units, "standard_name": standard});
    out.json(&format!("{path}/zarr.json"), &doc);
    for (q, chunk) in values.chunks(c).enumerate() {
        let raw: Vec<u8> = chunk.iter().flat_map(|v| v.to_le_bytes()).collect();
        out.bytes(&format!("{path}/c/{q}"), crate::out::gzip(&raw));
    }
}

/// A field's chunk (spec/zarrify/grib.md §8.5
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#85-chunks>): its message, when
/// the message holds it alone; else a message of its own, made of its sections between a new
/// section 0 and a `7777`.
fn chunk_parts(field: &J, messages: &[J], key: &str) -> Vec<Part> {
    let m = &messages[n(&field["message"]) as usize];
    let (start, length) = (n(&m[0]), n(&m[1]));
    if field["single"].as_bool().unwrap_or(true) {
        return vec![Part::Obj(key.to_string(), start, length)];
    }
    let parts: Vec<(u64, u64)> = arr(&field["parts"]).iter().map(|p| (n(&p[0]), n(&p[1]))).collect();
    let total = 16 + parts.iter().map(|p| p.1).sum::<u64>() + 4;
    let mut s0 = b"GRIB\0\0".to_vec();
    s0.push(n(&field["discipline"]) as u8);
    s0.push(2);
    s0.extend_from_slice(&total.to_be_bytes());
    let mut out = vec![Part::Lit(s0)];
    out.extend(parts.into_iter().map(|(o, len)| Part::Obj(key.to_string(), o, len)));
    out.push(Part::Lit(b"7777".to_vec()));
    out
}

pub fn project(ir: &Ir, facts: &J, url: &str) -> Result<Out, String> {
    let file_key = facts["file_key"].as_str().unwrap_or("").to_string();
    let file_url = object_url(url, &file_key);
    let messages = arr(&facts["messages"]);
    let fields: Vec<&J> = arr(&facts["fields"]).iter().filter(|x| x.get("reason").is_none()).collect();
    let mut out = Out::new(url);
    let mut prop = root_property(PROFILE, &file_url);
    prop.insert(PROFILE.into(), facts["own"].clone());
    let mut root = Map::new();
    root.insert("zarr_conventions".into(), json!([convention(PROFILE)]));
    root.insert(CONVENTION_KEY.into(), J::Object(prop));
    out.json("zarr.json", &group_doc(root));

    // the grids, in the order of their first fields
    let mut grids: Vec<&str> = Vec::new();
    for x in &fields {
        let g = x["grid"].as_str().unwrap_or("");
        if !grids.contains(&g) {
            grids.push(g);
        }
    }
    let mut summary = Vec::new();
    let mut refs: Vec<(String, Vec<Part>)> = Vec::new();
    for (gi, gkey) in grids.iter().enumerate() {
        let group = format!("grid{gi}");
        let in_grid: Vec<&J> = fields.iter().copied().filter(|x| x["grid"].as_str() == Some(gkey)).collect();
        let g = &facts["grids"][*gkey];
        let (dimensions, transform) = axes(g);
        let kind = g["projection"]["kind"].as_str().unwrap_or("");
        let positions = (kind == "reduced").then(|| reduced_positions(&g["projection"]));

        // the variables, by identity, in the order of their first fields
        let mut order: Vec<String> = Vec::new();
        let mut by: BTreeMap<String, Vec<&J>> = BTreeMap::new();
        for x in &in_grid {
            let has = |k: usize| !x["surfaces"][k][1].is_null();
            let id = format!("{}:{}{}", x["identity"].as_str().unwrap_or(""), has(0) as u8, has(1) as u8);
            if !by.contains_key(&id) {
                order.push(id.clone());
            }
            by.entry(id).or_default().push(x);
        }
        let mut taken: BTreeSet<String> = ["time", "step", "valid_time", "number"].into_iter().map(String::from).collect();
        taken.extend(dimensions.iter().map(|d| d.name.to_string()));
        if positions.is_some() {
            taken.extend(["latitude".to_string(), "longitude".to_string()]);
        }
        // a field whose cell an earlier one of its identity holds goes to the next copy, an array
        // of its own (spec/zarrify/grib.md §3.1
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#31-variables>)
        let mut copies: Vec<Vec<&J>> = Vec::new();
        for id in &order {
            let mut cells: BTreeMap<String, usize> = BTreeMap::new();
            let first = copies.len();
            for x in by.remove(id).unwrap_or_default() {
                let cell = format!("{}|{}|{}|{}|{}", x["reference"], x["step"], x["surfaces"][0][1], x["surfaces"][1][1], x["member"]);
                let c = cells.entry(cell).or_insert(0);
                if first + *c >= copies.len() {
                    copies.push(Vec::new());
                }
                copies[first + *c].push(x);
                *c += 1;
            }
        }
        let mut variables: Vec<Variable> = Vec::new();
        for fs in copies {
            let x = fs[0];
            let centre = n(&x["centre"]);
            let (d, c, num) = (n(&x["discipline"]) as u8, n(&x["category"]) as u8, n(&x["number"]) as u8);
            let short = names::parameter(centre, d, c, num).map_or_else(|| format!("d{d}_c{c}_p{num}"), |p| p.0.to_string());
            let (s1, s2) = (n(&x["surfaces"][0][0]), n(&x["surfaces"][1][0]));
            let (lname, _) = level_name(centre, s1, s2);
            let mut base = if s1 == 255 { short } else { format!("{short}_{lname}") };
            if let Some(q) = x["qualifier"].as_str() {
                base.push('_');
                base.push_str(q);
            }
            if let Some(s) = x["statistic"].as_array() {
                base.push('_');
                base.push_str(&names::statistic(n(&s[0]) as u8));
                if let Some(l) = s[1].as_i64() {
                    base.push_str(&duration(l));
                }
            }
            let mut name = base.clone();
            let mut k = 2;
            while taken.contains(&name) {
                name = format!("{base}_{k}");
                k += 1;
            }
            taken.insert(name.clone());
            let level = (!x["surfaces"][0][1].is_null()).then_some(lname);
            let member = !x["member"].is_null();
            variables.push(Variable { fields: fs, name, level, member });
        }

        // the group's dimension coordinates: the union of its fields' values
        let times: Vec<i64> = in_grid.iter().filter_map(|x| x["reference"].as_i64()).collect::<BTreeSet<_>>().into_iter().collect();
        let steps: Vec<i64> = in_grid.iter().filter_map(|x| x["step"].as_i64()).collect::<BTreeSet<_>>().into_iter().collect();
        let members: Vec<u64> = in_grid.iter().filter_map(|x| x["member"].as_u64()).collect::<BTreeSet<_>>().into_iter().collect();
        let mut levels: BTreeMap<String, BTreeSet<(i128, i128)>> = BTreeMap::new();
        let mut level_values: BTreeMap<(i128, i128), (f64, f64)> = BTreeMap::new();
        let mut level_units: BTreeMap<String, Option<&str>> = BTreeMap::new();
        let mut layered: BTreeSet<String> = BTreeSet::new();
        for v in &variables {
            let Some(l) = &v.level else { continue };
            for x in &v.fields {
                let (a, b) = (f(&x["surfaces"][0][1]), x["surfaces"][1][1].as_f64().unwrap_or(f64::NAN));
                let k = (level_key(a), level_key(b));
                level_values.insert(k, (a, b));
                levels.entry(l.clone()).or_default().insert(k);
                if !b.is_nan() {
                    layered.insert(l.clone());
                }
            }
            let x = v.fields[0];
            level_units.insert(l.clone(), level_name(n(&x["centre"]), n(&x["surfaces"][0][0]), n(&x["surfaces"][1][0])).1);
        }
        // a level dimension sorts by the first surface's value, then the second's
        let levels: BTreeMap<String, Vec<(i128, i128)>> = levels.into_iter().map(|(k, s)| {
            let mut v: Vec<(i128, i128)> = s.into_iter().collect();
            v.sort_by(|p, q| level_values[p].0.total_cmp(&level_values[q].0).then(level_values[p].1.total_cmp(&level_values[q].1)));
            (k, v)
        }).collect();

        // the group: its georeferencing (§4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#4-georeferencing>)
        let mut a = Map::new();
        let names: Vec<&str> = dimensions.iter().map(|d| d.name).collect();
        let lengths: Vec<u64> = dimensions.iter().map(|d| d.len).collect();
        match kind {
            "unstructured" => {
                let p = &g["projection"];
                a.insert("GRIB_numberOfGridUsed".into(), p["grid"].clone());
                a.insert("GRIB_numberOfGridInReference".into(), p["reference"].clone());
                a.insert("GRIB_uuidOfHGrid".into(), p["uuid"].clone());
            }
            "reduced" => {
                if let Some(w) = wkt2(g) {
                    a.insert("zarr_conventions".into(), json!([proj_cmo()]));
                    a.insert("proj:wkt2".into(), json!(w));
                }
            }
            _ => {
                a.insert("zarr_conventions".into(), json!([proj_cmo(), spatial_cmo()]));
                if let Some(w) = wkt2(g) {
                    a.insert("proj:wkt2".into(), json!(w));
                }
                a.insert("spatial:dimensions".into(), json!(names));
                if let Some(t) = transform {
                    a.insert("spatial:transform".into(), t);
                }
                a.insert("spatial:shape".into(), json!(lengths));
                a.insert("spatial:registration".into(), json!("node"));
            }
        }
        out.json(&format!("{group}/zarr.json"), &group_doc(a));

        // the coordinates (§3.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#33-coordinates>)
        let tv: Vec<f64> = times.iter().map(|&t| t as f64).collect();
        values_array(&mut out, &format!("{group}/time"), &["time"], &[tv.len() as u64], &tv,
                     json!({"units": "seconds since 1970-01-01T00:00:00Z", "calendar": "proleptic_gregorian", "standard_name": "forecast_reference_time", "long_name": "initial time of forecast"}));
        let sv: Vec<f64> = steps.iter().map(|&s| s as f64).collect();
        values_array(&mut out, &format!("{group}/step"), &["step"], &[sv.len() as u64], &sv,
                     json!({"units": "seconds", "standard_name": "forecast_period", "long_name": "time since forecast_reference_time"}));
        let valid: Vec<f64> = times.iter().flat_map(|&t| steps.iter().map(move |&s| (t + s) as f64)).collect();
        values_array(&mut out, &format!("{group}/valid_time"), &["time", "step"], &[tv.len() as u64, sv.len() as u64], &valid,
                     json!({"units": "seconds since 1970-01-01T00:00:00Z", "calendar": "proleptic_gregorian", "standard_name": "time", "long_name": "time"}));
        if !members.is_empty() {
            let mv: Vec<f64> = members.iter().map(|&m| m as f64).collect();
            values_array(&mut out, &format!("{group}/number"), &["number"], &[mv.len() as u64], &mv,
                         json!({"units": "1", "standard_name": "realization", "long_name": "ensemble member numerical id"}));
        }
        for (l, ks) in &levels {
            let firsts: Vec<f64> = ks.iter().map(|k| level_values[k].0).collect();
            let mut attrs = Map::new();
            if let Some(u) = level_units.get(l).copied().flatten() {
                attrs.insert("units".into(), json!(u));
            }
            attrs.insert("long_name".into(), json!(l));
            if layered.contains(l) {
                attrs.insert("bounds".into(), json!(format!("{l}_bounds")));
                let b: Vec<f64> = ks.iter().flat_map(|k| [level_values[k].0, level_values[k].1]).collect();
                values_array(&mut out, &format!("{group}/{l}_bounds"), &[l, "bounds"], &[ks.len() as u64, 2], &b, json!({}));
            }
            values_array(&mut out, &format!("{group}/{l}"), &[l], &[firsts.len() as u64], &firsts, J::Object(attrs));
        }
        for d in &dimensions {
            let (dim, len) = (d.name, d.len);
            match &d.axis {
                Some((Axis::Regular(first, step), units, standard)) => {
                    let filters = vec![json!({"name": "numcodecs.delta", "configuration": {"dtype": "<u4"}})];
                    let attributes = json!({"units": units, "standard_name": standard, "scale_factor": step, "add_offset": first});
                    gzip_array(&mut out, &format!("{group}/{dim}"), "uint32", len, COORD_CHUNK, dim, filters, attributes, |q, c| {
                        let mut raw = Vec::with_capacity((c * 4) as usize);
                        raw.extend_from_slice(&((q * c) as u32).to_le_bytes());
                        for _ in 1..c {
                            raw.extend_from_slice(&1u32.to_le_bytes());
                        }
                        raw
                    });
                }
                Some((Axis::Listed(values), units, standard)) => {
                    values_array(&mut out, &format!("{group}/{dim}"), &[dim], &[len], values, json!({"units": units, "standard_name": standard}));
                }
                None => {}
            }
        }
        if let Some((lats, lons)) = &positions {
            point_array(&mut out, &format!("{group}/latitude"), lats, "degrees_north", "latitude");
            point_array(&mut out, &format!("{group}/longitude"), lons, "degrees_east", "longitude");
        }

        // the variables' arrays and chunks (§3.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#31-variables>)
        for v in &variables {
            let x = v.fields[0];
            let mut dims: Vec<String> = vec!["time".into(), "step".into()];
            let mut shape = vec![times.len() as u64, steps.len() as u64];
            if let Some(l) = &v.level {
                dims.push(l.clone());
                shape.push(levels[l].len() as u64);
            }
            if v.member {
                dims.push("number".into());
                shape.push(members.len() as u64);
            }
            dims.extend(names.iter().map(|d| d.to_string()));
            shape.extend(lengths.iter().copied());
            let chunk_shape: Vec<u64> = shape.iter().enumerate().map(|(k, &s)| if k + lengths.len() >= shape.len() { s } else { 1 }).collect();
            let dim_refs: Vec<&str> = dims.iter().map(String::as_str).collect();
            let mut doc = array_json(&shape, "float64", json!(chunk_shape), json!([{"name": "refkv.grib"}]), &dim_refs);
            doc["fill_value"] = json!("NaN");
            let centre = n(&x["centre"]);
            let (d, c, num) = (n(&x["discipline"]) as u8, n(&x["category"]) as u8, n(&x["number"]) as u8);
            let mut attrs = Map::new();
            if let Some((_, long, units)) = names::parameter(centre, d, c, num) {
                attrs.insert("long_name".into(), json!(long));
                attrs.insert("units".into(), json!(units));
            }
            attrs.insert("coordinates".into(), json!(if positions.is_some() { "valid_time latitude longitude" } else { "valid_time" }));
            attrs.insert("GRIB_centre".into(), json!(centre));
            attrs.insert("GRIB_discipline".into(), json!(d));
            attrs.insert("GRIB_parameterCategory".into(), json!(c));
            attrs.insert("GRIB_parameterNumber".into(), json!(num));
            attrs.insert("GRIB_productDefinitionTemplateNumber".into(), x["product"].clone());
            attrs.insert("GRIB_typeOfFirstFixedSurface".into(), x["surfaces"][0][0].clone());
            attrs.insert("GRIB_typeOfSecondFixedSurface".into(), x["surfaces"][1][0].clone());
            if let Some(s) = x["statistic"].as_array() {
                attrs.insert("GRIB_typeOfStatisticalProcessing".into(), s[0].clone());
                attrs.insert("GRIB_lengthOfTimeRange".into(), s[1].clone());
            }
            doc["attributes"] = J::Object(attrs);
            out.json(&format!("{group}/{}/zarr.json", v.name), &doc);
            for x in &v.fields {
                let t = times.binary_search(&x["reference"].as_i64().unwrap_or(0)).unwrap_or(0);
                let s = steps.binary_search(&x["step"].as_i64().unwrap_or(0)).unwrap_or(0);
                let mut coords = vec![t, s];
                if let Some(l) = &v.level {
                    let k = (level_key(f(&x["surfaces"][0][1])), level_key(x["surfaces"][1][1].as_f64().unwrap_or(f64::NAN)));
                    coords.push(levels[l].iter().position(|q| *q == k).unwrap_or(0));
                }
                if v.member {
                    coords.push(members.binary_search(&x["member"].as_u64().unwrap_or(0)).unwrap_or(0));
                }
                coords.extend(lengths.iter().map(|_| 0));
                let key = format!("{group}/{}/c/{}", v.name, coords.iter().map(|c| c.to_string()).collect::<Vec<_>>().join("/"));
                refs.push((key, chunk_parts(x, messages, &file_key)));
            }
            summary.push(json!({"path": format!("{group}/{}", v.name), "shape": shape, "fields": v.fields.len()}));
        }
    }
    let count = refs.len();
    let _ = ir;
    for (k, p) in refs {
        if crate::out::payload_size(&p) > crate::out::MAX_PAYLOAD {
            return Err(format!("{k}: a reference payload of more than {} bytes", crate::out::MAX_PAYLOAD));
        }
        out.refs(&k, p);
    }
    for k in out.entries.iter().map(|e| &e.0).chain(out.refs.iter().map(|r| &r.0)) {
        super::store::check_key(k)?;
    }
    let mut unpresented: BTreeMap<String, u64> = BTreeMap::new();
    for x in arr(&facts["fields"]) {
        if let Some(r) = x["reason"].as_str() {
            *unpresented.entry(r.to_string()).or_default() += 1;
        }
    }
    out.summary = with_counts(json!({
        "messages": messages.len(), "fields": arr(&facts["fields"]).len(), "grids": grids.len(), "arrays": summary,
        "chunks": count, "unpresented": unpresented, "index": facts["index_key"],
    }), facts);
    Ok(out)
}
