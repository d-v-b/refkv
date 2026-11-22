//! The SAFE projection (spec/zarrify/safe.md §3
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#3-the-hierarchy>–§6.2
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#62-band-arrays>, the profile's
//! §8.6 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#86-chunks>–§8.8
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#88-the-source-table>) of
//! either form: the GeoZarr root and resolution groups, one array per band file whose chunks
//! are its tiles as standalone codestreams, and, in the directory form, the other objects at
//! `refkv_source/objects/<k>`. refkv's **plan** ([`plan`]) makes the convention's decisions from
//! the parser's description of the product (spec/zarrify/safe.md §8.11
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#811-the-description>): the root's
//! and each band's source metadata, the objects kept whole, and the counts. The projection checks
//! only the archive's own limits (reference payloads, output keys).

use super::{arr, n, with_counts};
use crate::ir::Ir;
use crate::out::{array_json, convention, declare_array, group_doc, payload_size, root_property, transpose_codec, Out, Part, CONVENTION_KEY, MAX_PAYLOAD};
use crate::safe::jp2::{self, Codestream};
use crate::safe::product::{band_metadata, multiscales_cmo, proj_cmo, root_metadata, spatial_cmo, wkt2, Geocoding};
use crate::safe::xmltree::Element;
use std::collections::HashSet;
use serde_json::{json, Map, Value as J};

const PROFILE: &str = "safe";

/// A chunk's range before the data sources are numbered.
enum Pre {
    Part(Part),
    Shared(Vec<u8>),
}

struct Grid {
    transform: J,
    shape: J,
    bbox: J,
}

fn grid(d: &J, r: u64) -> Grid {
    let size = arr(&d["geocoding"]["sizes"]).iter().find(|s| n(&s[0]) == r).cloned().unwrap_or(J::Null);
    let (rows, cols) = (n(&size[1]), n(&size[2]));
    let pos = arr(&d["geocoding"]["positions"]).iter().find(|p| n(&p[0]) == r).cloned().unwrap_or(J::Null);
    let v: Vec<f64> = (0..4).map(|k| pos[1][k].as_f64().unwrap_or(f64::NAN)).collect();
    let (ulx, uly, xdim, ydim) = (v[0], v[1], v[2], v[3]);
    let (x1, y1) = (ulx + xdim * cols as f64, uly + ydim * rows as f64);
    Grid {
        transform: json!([xdim, 0.0, ulx, 0.0, ydim, uly]),
        shape: json!([rows, cols]),
        bbox: json!([ulx.min(x1), uly.min(y1), ulx.max(x1), uly.max(y1)]),
    }
}

fn crs(d: &J, m: &mut Map<String, J>) {
    let code = d["geocoding"]["code"].as_str().unwrap_or("");
    m.insert("proj:code".into(), json!(code));
    if let Some(w) = wkt2(code) {
        m.insert("proj:wkt2".into(), json!(w));
    }
}

/// The tile geocoding the description gives.
fn geocoding(g: &J) -> Geocoding {
    let text = |v: &J| v.as_str().map(str::to_string);
    Geocoding {
        code: g["code"].as_str().unwrap_or("").to_string(),
        sizes: arr(&g["sizes"]).iter().map(|s| (n(&s[0]), n(&s[1]), n(&s[2]))).collect(),
        positions: arr(&g["positions"]).iter().map(|p| (n(&p[0]), [0, 1, 2, 3].map(|k| p[1][k].as_f64().unwrap_or(f64::NAN)))).collect(),
        cs_name: text(&g["cs_name"]),
        cs_code: text(&g["cs_code"]),
    }
}

/// refkv's plan of the product the description `d` describes: `root`, the root's source metadata
/// (§6.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#63-the-source-metadata-node>);
/// `meta`, each band's (§6.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#62-band-arrays>),
/// in the bands' order; `objects`, the other objects a directory's mirror keeps whole, each `[key,
/// size]` (§8.7 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#87-other-objects>);
/// and `counts`, the summary's (§8.9 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#89-summary>).
pub fn plan(d: &J) -> J {
    let metadata = Element::from_json(&d["product_metadata"]["document"]);
    let tile = Element::from_json(&d["tile_metadata"]["document"]);
    let l2a = d["level"] == "L2A";
    let store = d["form"] == "store";
    let bands = arr(&d["bands"]);
    let meta: Vec<J> = bands.iter().map(|b| {
        let siz = crate::hdf5::file::unhex(b["codestream"]["siz"].as_str().unwrap_or(""));
        band_metadata(&metadata, l2a, b["image_file"].as_str().unwrap_or(""), b["name"].as_str().unwrap_or(""), &crate::mirror::base64(&siz))
    }).collect();
    let band_keys: HashSet<&str> = bands.iter().filter_map(|b| b["key"].as_str()).collect();
    let objects = arr(&d["objects"]);
    let (mut referenced, mut empty) = (Vec::new(), 0u64);
    for o in objects {
        let key = o["key"].as_str().unwrap_or("");
        if o["marker"] == true || o["held"] == true || band_keys.contains(key) {
            continue;
        }
        if n(&o["size"]) == 0 {
            empty += 1;
        } else {
            referenced.push(json!([key, o["size"]]));
        }
    }
    let counts = json!({
        "objects": objects.len(), "folderMarkers": objects.iter().filter(|o| o["marker"] == true).count(),
        "emptyDirs": arr(&d["empty_dirs"]).len(), "heldXml": objects.iter().filter(|o| o["held"] == true).count(),
        "otherObjects": referenced.len(), "emptyObjects": empty,
        "tileParts": bands.iter().map(|b| arr(&b["codestream"]["tiles"]).len()).sum::<usize>(),
        "bandReads": d["reads"]["band_reads"], "batches": d["reads"]["batches"],
    });
    json!({
        "root": root_metadata(&metadata, &tile, &geocoding(&d["geocoding"])),
        "meta": meta,
        "objects": if store { referenced } else { Vec::new() },
        "counts": counts,
    })
}

/// The projection of the product the description `d` describes, through refkv's plan ([`plan`]).
pub fn project(_ir: &Ir, d: &J, url: &str) -> Result<Out, String> {
    let planned = plan(d);
    let mut out = Out::new(url);
    let store = d["form"] == "store";
    let l2a = d["level"] == "L2A";
    let bands = arr(&d["bands"]);
    let mut resolutions: Vec<u64> = bands.iter().map(|b| n(&b["resolution"])).collect();
    resolutions.sort_unstable();
    resolutions.dedup();
    // the root (§5.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#53-the-root>)
    let mut geo = Map::new();
    crs(d, &mut geo);
    geo.insert("spatial:bbox".into(), grid(d, resolutions[0]).bbox);
    let mut cmos = vec![convention(PROFILE)];
    if l2a {
        cmos.push(multiscales_cmo());
        geo.insert("spatial:dimensions".into(), json!(["y", "x"]));
        geo.insert("spatial:registration".into(), json!("pixel"));
        let layout: Vec<J> = resolutions.iter().map(|&r| {
            let g = grid(d, r);
            json!({"asset": format!("r{r}m"), "spatial:shape": g.shape, "spatial:transform": g.transform})
        }).collect();
        geo.insert("multiscales".into(), json!({"layout": layout}));
    }
    cmos.extend([proj_cmo(), spatial_cmo()]);
    let mut prop = root_property(PROFILE, url);
    prop.insert(PROFILE.into(), planned["root"].clone());
    let mut root = Map::new();
    root.insert("zarr_conventions".into(), J::Array(cmos));
    root.insert(CONVENTION_KEY.into(), J::Object(prop));
    root.extend(geo);
    out.json("zarr.json", &group_doc(root));
    // the resolution groups (§5.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#52-resolution-groups>)
    for &r in &resolutions {
        let g = grid(d, r);
        let mut a = Map::new();
        a.insert("zarr_conventions".into(), json!([proj_cmo(), spatial_cmo()]));
        crs(d, &mut a);
        a.insert("spatial:dimensions".into(), json!(["y", "x"]));
        a.insert("spatial:transform".into(), g.transform);
        a.insert("spatial:shape".into(), g.shape);
        a.insert("spatial:bbox".into(), g.bbox);
        a.insert("spatial:registration".into(), json!("pixel"));
        out.json(&format!("r{r}m/zarr.json"), &group_doc(a));
    }
    // the band arrays and their chunks (§4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#4-arrays>)
    let mut refs: Vec<(String, Vec<Pre>)> = Vec::new();
    let (mut chunks, mut edges) = (0u64, 0u64);
    for (b, meta) in bands.iter().zip(arr(&planned["meta"])) {
        let cs = Codestream::from_facts(&b["codestream"]);
        let path = format!("r{}m/{}", n(&b["resolution"]), b["name"].as_str().unwrap_or(""));
        let three = cs.components == 3;
        let (mut shape, mut chunk) = (vec![cs.height, cs.width], vec![cs.tile_h, cs.tile_w]);
        let dims: &[&str] = if three { &["c", "y", "x"] } else { &["y", "x"] };
        let mut codecs = vec![json!({"name": "imagecodecs_jpeg2k"})];
        if three {
            shape.insert(0, 3);
            chunk.insert(0, 3);
            codecs.insert(0, transpose_codec(dims));
        }
        let mut doc = array_json(&shape, if cs.precision <= 8 { "uint8" } else { "uint16" }, json!(chunk), J::Array(codecs), dims);
        declare_array(&mut doc, PROFILE, meta.clone());
        out.json(&format!("{path}/zarr.json"), &doc);
        let key = b["key"].as_str().unwrap_or("").to_string();
        let base = n(&b["base"]);
        let (nx, _) = cs.grid();
        for t in 0..cs.tiles.len() as u64 {
            let (head, sot, (o, len), tail, padded) = jp2::chunk_parts(&cs, t)?;
            let body = if store { Part::Obj(key.clone(), o, len) } else { Part::Src(base + o, len) };
            let tail = if padded { Pre::Shared(tail) } else { Pre::Part(Part::Lit(tail)) };
            let parts = vec![Pre::Part(Part::Lit(head)), Pre::Shared(cs.rest.clone()), Pre::Part(Part::Lit(sot)), Pre::Part(body), tail];
            refs.push((super::key(&format!("{path}/c/"), three.then_some(0).into_iter().chain([t / nx, t % nx])), parts));
            chunks += 1;
            edges += padded as u64;
        }
    }
    // the other objects, whole (§6.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#63-the-source-metadata-node>)
    for o in arr(&planned["objects"]) {
        let (k, size) = (o[0].as_str().unwrap_or("").to_string(), n(&o[1]));
        refs.push((super::store::object_key(&k), vec![Pre::Part(Part::Obj(k, 0, size))]));
    }
    // the data sources, numbered by first use in the order of the entries' keys (§8.8
    // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#88-the-source-table>)
    refs.sort_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
    for (key, pre) in refs {
        let parts: Vec<Part> = pre.into_iter().map(|p| match p {
            Pre::Part(p) => p,
            Pre::Shared(b) => out.shared(&b),
        }).collect();
        if payload_size(&parts) > MAX_PAYLOAD {
            let short: String = key.chars().take(200).collect();
            return Err(format!("the reference payload of {short} is over {MAX_PAYLOAD} bytes"));
        }
        out.refs(&key, parts);
    }
    for k in out.entries.iter().map(|e| &e.0).chain(out.refs.iter().map(|r| &r.0)) {
        if k.len() > 65535 {
            return Err("an output key is longer than 65535 bytes".into());
        }
        if k.starts_with("__vz__/") {
            let short: String = k.chars().take(200).collect();
            return Err(format!("output key {short:?} is in the reserved __vz__/ space"));
        }
    }
    let summary = json!({"level": d["level"], "form": d["form"], "groups": resolutions.len(), "bands": bands.len(),
                         "chunks": chunks, "edgeChunks": edges, "dataSources": out.data.len()});
    out.summary = with_counts(summary, &planned);
    Ok(out)
}

/// The projection of the profile's store run (`StoreFormat`; crate::store).
pub fn store_project(ir: &Ir, description: &J, url: &str) -> Result<Out, String> {
    project(ir, description, url)
}
