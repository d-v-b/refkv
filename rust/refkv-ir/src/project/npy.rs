//! The NumPy projection (spec/zarrify/npy.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#4-the-hierarchy>) from a valid
//! IR: a root group with the source metadata the parser decided, and an array per presented NPY
//! array (a `.npy` file's `array`, a `.npz` archive's members), its chunks cut from its `data`
//! element as conventions §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays> cuts
//! contiguous values of its stored shape; a deflated member's one chunk, its deflate stream as
//! a gzip member (spec/zarrify/npy.md §8.4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#84-chunks>).

use super::{arr, contiguous, Copied};
use crate::ir::Ir;
use crate::out::{array_json, declared_group, grid_chunks, Chunk, Out, Part, GZIP_HEADER, GZIP_LEVEL};
use crate::refs::Refs;
use serde_json::{json, Map, Value as J};
use std::collections::BTreeMap;

/// The most bytes of a chunk (spec/zarrify/npy.md §8.4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#84-chunks>).
const CHUNK_BYTES: u64 = 1 << 20;

fn n(v: &J) -> u64 {
    v.as_u64().unwrap_or(0)
}

/// An array's shape, chunk shape and chunks (their coordinates in the array's axis order), and
/// whether its stored shape is its shape reversed (spec/zarrify/npy.md §4.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#43-the-arrays>).
struct Cut {
    shape: Vec<u64>,
    chunk_shape: Vec<u64>,
    chunks: Vec<(Vec<u64>, Chunk)>,
    reversed: bool,
    trailing: bool,
}

fn cut(a: &J, threshold: usize) -> Result<Cut, String> {
    let mut shape: Vec<u64> = arr(&a["shape"]).iter().map(n).collect();
    let r = shape.len();
    let reversed = a["fortran_order"].as_bool().unwrap_or(false) && r >= 2;
    let trailing = a["trailing"].as_u64();
    let mut stored: Vec<u64> = if reversed { shape.iter().rev().copied().collect() } else { shape.clone() };
    let item = match trailing {
        Some(t) => {
            stored.push(t);
            shape.push(t);
            1
        }
        None => n(&a["item"]),
    };
    let d = &a["deflate"];
    if d.is_object() {
        // one chunk, the whole array: the deflate stream as a gzip member, the header and data
        // it inflates to (spec/zarrify/npy.md §8.4
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#84-chunks>)
        let chunk_shape = shape.iter().map(|&s| s.max(1)).collect();
        let mut chunks = Vec::new();
        if n(&a["length"]) > 0 {
            let mut trailer = (n(&d["crc"]) as u32).to_le_bytes().to_vec();
            trailer.extend_from_slice(&(n(&d["size"]) as u32).to_le_bytes());
            let parts = vec![Part::Lit(GZIP_HEADER.to_vec()), Part::Src(n(&d["start"]), n(&d["length"])), Part::Lit(trailer)];
            chunks.push((vec![0; shape.len()], Chunk::Ranges(parts)));
        }
        return Ok(Cut { shape, chunk_shape, chunks, reversed, trailing: trailing.is_some() });
    }
    let (grid, chunks) = grid_chunks(n(&a["offset"]), &stored, item, CHUNK_BYTES, threshold).ok_or("internal: the data cannot be cut into chunks")?;
    // the stored order's coordinates in the array's: the first r reversed, a trailing axis last
    let order = |c: Vec<u64>| -> Vec<u64> {
        if !reversed {
            return c;
        }
        let mut v: Vec<u64> = c[..r].iter().rev().copied().collect();
        v.extend_from_slice(&c[r..]);
        v
    };
    Ok(Cut { shape, chunk_shape: order(grid), chunks: chunks.into_iter().map(|(c, k)| (order(c), k)).collect(), reversed, trailing: trailing.is_some() })
}

/// The source's ranges of the edge chunks the projection copies (§8.4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#84-chunks>).
pub fn reads(_ir: &Ir, facts: &J) -> Result<Vec<(u64, u64)>, String> {
    let threshold = contiguous::threshold(facts);
    let mut out = Vec::new();
    for a in arr(&facts["arrays"]) {
        out.extend(contiguous::copy_reads(&cut(a, threshold)?.chunks));
    }
    Ok(out)
}

/// An array's `zarr.json` (§4.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#43-the-arrays>).
fn array_doc(a: &J, c: &Cut) -> J {
    let r = arr(&a["shape"]).len();
    let mut codecs = Vec::new();
    if c.reversed {
        let mut order: Vec<usize> = (0..r).rev().collect();
        if c.trailing {
            order.push(r);
        }
        codecs.push(json!({"name": "transpose", "configuration": {"order": order}}));
    }
    codecs.push(match a["little"].as_bool() {
        Some(l) => json!({"name": "bytes", "configuration": {"endian": if l { "little" } else { "big" }}}),
        None => json!({"name": "bytes"}),
    });
    if a["deflate"].is_object() {
        codecs.push(json!({"name": "refkv.skip", "configuration": {"length": a["deflate"]["skip"]}}));
        codecs.push(json!({"name": "gzip", "configuration": {"level": GZIP_LEVEL}}));
    }
    let mut dims: Vec<String> = (0..r).map(|k| format!("dim_{k}")).collect();
    if c.trailing {
        dims.push("byte".into());
    }
    let dims: Vec<&str> = dims.iter().map(String::as_str).collect();
    let mut doc = array_json(&c.shape, "", json!(c.chunk_shape), J::Array(codecs), &dims);
    doc["data_type"] = a["data_type"].clone();
    doc["fill_value"] = a["fill"].clone();
    doc
}

pub fn project(ir: &Ir, facts: &J, url: &str, copied: &Copied) -> Result<Out, String> {
    let threshold = contiguous::threshold(facts);
    let arrays = arr(&facts["arrays"]);
    let cuts = arrays.iter().map(|a| cut(a, threshold)).collect::<Result<Vec<Cut>, String>>()?;
    let mut out = Out::new(url);
    out.json("zarr.json", &declared_group(Map::new(), "npy", Some(url), Some(facts["own"].clone())));
    let mut summary = Vec::new();
    for (a, c) in arrays.iter().zip(&cuts) {
        let path = a["path"].as_str().unwrap_or("array");
        out.json(&format!("{path}/zarr.json"), &array_doc(a, c));
        summary.push(json!({
            "path": path, "dataType": a["data_type"], "shape": c.shape,
            "order": if a["fortran_order"].as_bool().unwrap_or(false) { "F" } else { "C" },
            "chunkShape": c.chunk_shape, "chunks": c.chunks.len(),
        }));
    }
    let mut zeros = contiguous::Zeros::of(cuts.iter().flat_map(|c| c.chunks.iter()));
    let mut refs = Refs::new(ir, out);
    for (a, c) in arrays.iter().zip(cuts) {
        let path = a["path"].as_str().unwrap_or("array");
        // a chunk of rank 0 is `<path>/c` (spec/zarrify/npy.md §8.4
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#84-chunks>)
        let prefix = if c.shape.is_empty() { format!("{path}/c") } else { format!("{path}/c/") };
        let form = a["data"].as_u64().and_then(|d| ir.form_id(d as u32)).unwrap_or(0);
        // a deflated member's chunk is its stream, which its derived element is
        let d = &a["deflate"];
        let element = if d.is_object() { (n(&d["start"]), n(&d["length"])) } else { (n(&a["offset"]), n(&a["length"])) };
        contiguous::emit(&mut refs, c.chunks, (element.0, element.1, form), copied, &[&prefix], |k| k, &mut zeros)?;
    }
    let mut out = refs.out;
    let mut unpresented: BTreeMap<String, u64> = BTreeMap::new();
    for m in arr(&facts["own"]["members"]) {
        if let Some(r) = m["reason"].as_str() {
            *unpresented.entry(r.to_string()).or_default() += 1;
        }
    }
    out.summary = json!({"kind": facts["own"]["kind"], "arrays": summary, "unpresented": unpresented});
    Ok(out)
}
