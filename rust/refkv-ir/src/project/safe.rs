//! The SAFE projection (spec/virtualize/safe.md §3
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/safe.md#3-the-hierarchy>–§6.2
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/safe.md#62-band-arrays>, the profile's
//! §12.6 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/safe.md#126-chunks>–§12.8
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/safe.md#128-the-source-table>) of
//! either form: the GeoZarr root and resolution groups, one array per band file whose chunks
//! are its tiles as standalone codestreams, and, in the directory form, the other objects at
//! `vzip_source/objects/<k>`. The facts hold what the parser read; the projection checks only
//! the archive's own limits (reference payloads, output keys).

use super::{arr, n};
use crate::ir::Ir;
use crate::out::{array_json, convention, declare, group_doc, payload_size, root_property, Out, Part, CONVENTION_KEY, MAX_PAYLOAD};
use crate::safe::jp2::{self, Codestream};
use crate::safe::product::{multiscales_cmo, proj_cmo, spatial_cmo, wkt2};
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

fn grid(facts: &J, r: u64) -> Grid {
    let size = arr(&facts["geo"]["sizes"]).iter().find(|s| n(&s[0]) == r).cloned().unwrap_or(J::Null);
    let (rows, cols) = (n(&size[1]), n(&size[2]));
    let pos = arr(&facts["geo"]["positions"]).iter().find(|p| n(&p[0]) == r).cloned().unwrap_or(J::Null);
    let v: Vec<f64> = (0..4).map(|k| pos[1][k].as_f64().unwrap_or(f64::NAN)).collect();
    let (ulx, uly, xdim, ydim) = (v[0], v[1], v[2], v[3]);
    let (x1, y1) = (ulx + xdim * cols as f64, uly + ydim * rows as f64);
    Grid {
        transform: json!([xdim, 0.0, ulx, 0.0, ydim, uly]),
        shape: json!([rows, cols]),
        bbox: json!([ulx.min(x1), uly.min(y1), ulx.max(x1), uly.max(y1)]),
    }
}

fn crs(facts: &J, m: &mut Map<String, J>) {
    let code = facts["geo"]["code"].as_str().unwrap_or("");
    m.insert("proj:code".into(), json!(code));
    if let Some(w) = wkt2(code) {
        m.insert("proj:wkt2".into(), json!(w));
    }
}

pub fn project(_ir: &Ir, facts: &J, url: &str) -> Result<Out, String> {
    let mut out = Out::new(url);
    let store = facts["form"] == "store";
    let l2a = facts["level"] == "L2A";
    let bands = arr(&facts["bands"]);
    let mut resolutions: Vec<u64> = bands.iter().map(|b| n(&b["resolution"])).collect();
    resolutions.sort_unstable();
    resolutions.dedup();
    // the root (§5.3 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/safe.md#53-the-root>)
    let mut geo = Map::new();
    crs(facts, &mut geo);
    geo.insert("spatial:bbox".into(), grid(facts, resolutions[0]).bbox);
    let mut cmos = vec![convention(PROFILE)];
    if l2a {
        cmos.push(multiscales_cmo());
        geo.insert("spatial:dimensions".into(), json!(["y", "x"]));
        geo.insert("spatial:registration".into(), json!("pixel"));
        let layout: Vec<J> = resolutions.iter().map(|&r| {
            let g = grid(facts, r);
            json!({"asset": format!("r{r}m"), "spatial:shape": g.shape, "spatial:transform": g.transform})
        }).collect();
        geo.insert("multiscales".into(), json!({"layout": layout}));
    }
    cmos.extend([proj_cmo(), spatial_cmo()]);
    let mut prop = root_property(PROFILE, url);
    prop.insert(PROFILE.into(), facts["root"].clone());
    let mut root = Map::new();
    root.insert("zarr_conventions".into(), J::Array(cmos));
    root.insert(CONVENTION_KEY.into(), J::Object(prop));
    root.extend(geo);
    out.json("zarr.json", &group_doc(root));
    // the resolution groups (§5.2 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/safe.md#52-resolution-groups>)
    for &r in &resolutions {
        let g = grid(facts, r);
        let mut a = Map::new();
        a.insert("zarr_conventions".into(), json!([proj_cmo(), spatial_cmo()]));
        crs(facts, &mut a);
        a.insert("spatial:dimensions".into(), json!(["y", "x"]));
        a.insert("spatial:transform".into(), g.transform);
        a.insert("spatial:shape".into(), g.shape);
        a.insert("spatial:bbox".into(), g.bbox);
        a.insert("spatial:registration".into(), json!("pixel"));
        out.json(&format!("r{r}m/zarr.json"), &group_doc(a));
    }
    // the band arrays and their chunks (§4 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/safe.md#4-arrays>)
    let mut refs: Vec<(String, Vec<Pre>)> = Vec::new();
    let (mut chunks, mut edges) = (0u64, 0u64);
    for b in bands {
        let cs = Codestream::from_facts(&b["codestream"]);
        let path = format!("r{}m/{}", n(&b["resolution"]), b["name"].as_str().unwrap_or(""));
        let three = cs.components == 3;
        let (mut shape, mut chunk) = (vec![cs.height, cs.width], vec![cs.tile_h, cs.tile_w]);
        let mut codecs = vec![json!({"name": "imagecodecs_jpeg2k"})];
        if three {
            shape.insert(0, 3);
            chunk.insert(0, 3);
            codecs.insert(0, json!({"name": "transpose", "configuration": {"order": [1, 2, 0]}}));
        }
        let dims: &[&str] = if three { &["c", "y", "x"] } else { &["y", "x"] };
        let mut doc = array_json(&shape, if cs.precision <= 8 { "uint8" } else { "uint16" }, json!(chunk), J::Array(codecs), dims);
        doc["attributes"] = J::Object(declare(Map::new(), PROFILE, None, Some(b["meta"].clone())));
        out.json(&format!("{path}/zarr.json"), &doc);
        let key = b["key"].as_str().unwrap_or("").to_string();
        let base = n(&b["base"]);
        let (nx, _) = cs.grid();
        for t in 0..cs.tiles.len() as u64 {
            let (head, sot, (o, len), tail, padded) = jp2::chunk_parts(&cs, t)?;
            let body = if store { Part::Obj(key.clone(), o, len) } else { Part::Src(base + o, len) };
            let tail = if padded { Pre::Shared(tail) } else { Pre::Part(Part::Lit(tail)) };
            let parts = vec![Pre::Part(Part::Lit(head)), Pre::Shared(cs.rest.clone()), Pre::Part(Part::Lit(sot)), Pre::Part(body), tail];
            refs.push((format!("{path}/c/{}{}/{}", if three { "0/" } else { "" }, t / nx, t % nx), parts));
            chunks += 1;
            edges += padded as u64;
        }
    }
    // the other objects, whole (§6.3 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/safe.md#63-the-source-metadata-node>)
    for o in arr(&facts["objects"]) {
        let (k, size) = (o[0].as_str().unwrap_or("").to_string(), n(&o[1]));
        refs.push((super::store::object_key(&k), vec![Pre::Part(Part::Obj(k, 0, size))]));
    }
    // the data sources, numbered by first use in the order of the entries' keys (§12.8
    // <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/safe.md#128-the-source-table>)
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
    let mut summary = json!({"level": facts["level"], "form": facts["form"], "groups": resolutions.len(), "bands": bands.len(),
                             "chunks": chunks, "edgeChunks": edges, "dataSources": out.data.len()});
    if let (Some(m), Some(c)) = (summary.as_object_mut(), facts["counts"].as_object()) {
        m.extend(c.clone());
    }
    out.summary = summary;
    Ok(out)
}
