//! The netCDF3 projection (spec/zarrify/netcdf3.md §2
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#2-the-hierarchy>) from a
//! valid IR: the root and an array per presented variable, as the parser decided them (the
//! facts). A fixed-size variable's values are cut as conventions §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays> cuts
//! contiguous values; a record variable is one chunk per record (spec/zarrify/netcdf3.md §3.2
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#32-chunks>).

use super::{contiguous, key, Copied};
use crate::ir::Ir;
use crate::out::{declare, declared_group, grid_chunks, Chunk, Out, Part};
use crate::refs::Refs;
use serde_json::{json, Map, Value as J};

const PROFILE: &str = "netcdf3";
/// The chunk limit of a fixed-size variable (spec/zarrify/netcdf3.md §3.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#32-chunks>).
const CHUNK_BYTES: u64 = 1 << 20;

fn n(v: &J) -> u64 {
    v.as_u64().unwrap_or(0)
}

fn list(v: &J) -> &[J] {
    v.as_array().map_or(&[], |a| a.as_slice())
}

fn shape_of(v: &J) -> Vec<u64> {
    list(v).iter().map(n).collect()
}

/// A chunk shape, and each chunk with its grid coordinates.
type Cut = (Vec<u64>, Vec<(Vec<u64>, Chunk)>);

/// The contiguous cut of a fixed-size variable's array; None for a record variable's.
fn cut(a: &J, threshold: usize) -> Result<Option<Cut>, String> {
    let store = &a["store"];
    if store["kind"] != "fixed" {
        return Ok(None);
    }
    grid_chunks(n(&store["offset"]), &shape_of(&a["shape"]), n(&store["item"]), CHUNK_BYTES, threshold)
        .map(Some)
        .ok_or_else(|| "internal: the data cannot be cut into chunks".to_string())
}

/// The prefix of the chunk keys of the array at `path` of rank `rank`: `<path>/c` alone for a
/// scalar.
fn prefix(path: &str, rank: usize) -> String {
    if rank == 0 { format!("{path}/c") } else { format!("{path}/c/") }
}

/// The source's ranges of the edge chunks the projection copies.
pub fn reads(_ir: &Ir, facts: &J) -> Result<Vec<(u64, u64)>, String> {
    let threshold = contiguous::threshold(facts);
    let mut out = Vec::new();
    for a in list(&facts["arrays"]) {
        if let Some((_, chunks)) = cut(a, threshold)? {
            out.extend(contiguous::copy_reads(&chunks));
        }
    }
    Ok(out)
}

pub fn project(ir: &Ir, facts: &J, url: &str, copied: &Copied) -> Result<Out, String> {
    let threshold = contiguous::threshold(facts);
    let arrays = list(&facts["arrays"]);
    let mut out = Out::new(url);
    let root = &facts["root"];
    let attributes: Map<String, J> = root["attributes"].as_object().cloned().unwrap_or_default();
    out.json("zarr.json", &declared_group(attributes, PROFILE, Some(url), Some(root["own"].clone())));
    let mut cuts = Vec::with_capacity(arrays.len());
    for a in arrays {
        cuts.push(cut(a, threshold)?);
    }
    for (a, c) in arrays.iter().zip(&cuts) {
        let path = a["path"].as_str().unwrap_or("");
        let shape = shape_of(&a["shape"]);
        let chunk_shape: Vec<u64> = match c {
            Some((grid, _)) => grid.clone(),
            // one record a chunk, every other axis whole
            None => shape.iter().enumerate().map(|(k, &v)| if k == 0 { 1 } else { v.max(1) }).collect(),
        };
        let attributes: Map<String, J> = a["attributes"].as_object().cloned().unwrap_or_default();
        let doc = json!({
            "zarr_format": 3,
            "node_type": "array",
            "shape": shape,
            "data_type": a["data_type"],
            "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": chunk_shape}},
            "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
            "fill_value": a["fill_value"],
            "codecs": a["codecs"],
            "attributes": declare(attributes, PROFILE, None, Some(a["own"].clone())),
            "dimension_names": a["dimension_names"],
        });
        out.json(&format!("{path}/zarr.json"), &doc);
    }
    let mut zeros = contiguous::Zeros::of(cuts.iter().flatten().flat_map(|c| &c.1));
    let mut refs = Refs::new(ir, out);
    let mut count = 0u64;
    for (a, c) in arrays.iter().zip(cuts) {
        let path = a["path"].as_str().unwrap_or("");
        let shape = shape_of(&a["shape"]);
        let p = prefix(path, shape.len());
        match c {
            Some((_, chunks)) => {
                count += chunks.len() as u64;
                contiguous::emit(&mut refs, chunks, (0, 0, 0), copied, &[&p], |c| c, &mut zeros)?;
            }
            None => {
                let store = &a["store"];
                let (offset, stride, length) = (n(&store["offset"]), n(&store["stride"]), n(&store["length"]));
                for r in 0..n(&store["records"]) {
                    let at = r.checked_mul(stride).and_then(|o| o.checked_add(offset)).ok_or("internal: a record past 2^64 bytes")?;
                    let coords = std::iter::once(r).chain(shape.iter().skip(1).map(|_| 0));
                    refs.chunk(key(&p, coords), 0, 0, 0, Some(vec![Part::Src(at, length)]))?;
                    count += 1;
                }
            }
        }
    }
    let mut out = refs.out;
    let mut summary = facts["summary"].clone();
    summary["chunk_entries"] = json!(count);
    out.summary = summary;
    Ok(out)
}
