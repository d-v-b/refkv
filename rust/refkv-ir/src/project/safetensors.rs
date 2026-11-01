//! The safetensors projection (spec/zarrify/safetensors.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#4-the-hierarchy>) from a
//! valid IR: a root group with the source metadata the parser decided, a group for each prefix
//! of a tensor's path, and an array per presented tensor, its chunks cut from its `data` element
//! as conventions §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays> cuts
//! contiguous values; a packed tensor's (4 or 6 bits an element) cut by elements and mapped to
//! its bytes (spec/zarrify/safetensors.md §8.3
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#83-chunks>).

use super::{arr, contiguous, Copied};
use crate::ir::Ir;
use crate::out::{array_json, declared_group, grid_chunks, metadata_group, payload_size, Chunk, Out, Part};
use crate::refs::Refs;
use serde_json::{json, Map, Value as J};
use std::collections::BTreeMap;

/// The most bytes of a chunk (spec/zarrify/safetensors.md §8.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#83-chunks>).
const CHUNK_BYTES: u64 = 1 << 20;

fn n(v: &J) -> u64 {
    v.as_u64().unwrap_or(0)
}

/// A tensor's chunk shape and chunks (their coordinates and references).
type Cut = (Vec<u64>, Vec<(Vec<u64>, Chunk)>);

fn cut(a: &J, threshold: usize) -> Result<Cut, String> {
    let shape: Vec<u64> = arr(&a["shape"]).iter().map(n).collect();
    let (offset, bits) = (n(&a["offset"]), n(&a["bits"]));
    if bits >= 8 {
        return grid_chunks(offset, &shape, bits / 8, CHUNK_BYTES, threshold).ok_or_else(|| "internal: the data cannot be cut into chunks".into());
    }
    // a packed tensor: cut by elements, each counted as a byte, then each run of elements
    // [s, s + m) the bytes [s k / 8, (s + m) k / 8) when every run starts on a byte
    let (grid, chunks) = grid_chunks(0, &shape, 1, CHUNK_BYTES, usize::MAX).ok_or("internal: the data cannot be cut into chunks")?;
    let per: u64 = grid.iter().product();
    let full = (per * bits).div_ceil(8);
    let runs: Option<Vec<(Vec<u64>, u64, u64)>> = chunks.into_iter().map(|(c, k)| match k {
        Chunk::Ranges(parts) => match parts.first() {
            Some(&Part::Src(s, m)) if (s * bits).is_multiple_of(8) => Some((c, s, m)),
            _ => None,
        },
        _ => None,
    }).collect();
    let Some(runs) = runs else {
        // one chunk, the whole data
        let length = n(&a["length"]);
        let one = if length > 0 { vec![(vec![0; shape.len()], Chunk::Ranges(vec![Part::Src(offset, length)]))] } else { Vec::new() };
        return Ok((shape.iter().map(|&s| s.max(1)).collect(), one));
    };
    let chunks = runs.into_iter().map(|(c, s, m)| {
        let (start, length) = (offset + s * bits / 8, (m * bits).div_ceil(8));
        let pad = full - length;
        let chunk = if pad == 0 {
            Chunk::Ranges(vec![Part::Src(start, length)])
        } else {
            let parts = vec![Part::Src(start, length), Part::Lit(vec![0; pad as usize])];
            if payload_size(&parts) > threshold { Chunk::Copy(start, length, pad) } else { Chunk::Ranges(parts) }
        };
        (c, chunk)
    }).collect();
    Ok((grid, chunks))
}

/// The source's ranges of the edge chunks the projection copies (§8.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#83-chunks>).
pub fn reads(_ir: &Ir, facts: &J) -> Result<Vec<(u64, u64)>, String> {
    let threshold = contiguous::threshold(facts);
    let mut out = Vec::new();
    for a in arr(&facts["arrays"]) {
        out.extend(contiguous::copy_reads(&cut(a, threshold)?.1));
    }
    Ok(out)
}

/// A tensor's `zarr.json` (§4.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#42-the-arrays>).
fn array_doc(a: &J, chunk_shape: &[u64]) -> J {
    let shape: Vec<u64> = arr(&a["shape"]).iter().map(n).collect();
    let codec = match n(&a["bits"]) {
        b if b < 8 => json!({"name": "packbits", "configuration": {"padding_encoding": "none"}}),
        8 => json!({"name": "bytes"}),
        _ => json!({"name": "bytes", "configuration": {"endian": "little"}}),
    };
    let dims: Vec<String> = (0..shape.len()).map(|k| format!("dim_{k}")).collect();
    let dims: Vec<&str> = dims.iter().map(String::as_str).collect();
    let mut doc = array_json(&shape, "", json!(chunk_shape), json!([codec]), &dims);
    doc["data_type"] = a["data_type"].clone();
    doc["fill_value"] = a["fill"].clone();
    doc
}

pub fn project(ir: &Ir, facts: &J, url: &str, copied: &Copied) -> Result<Out, String> {
    let threshold = contiguous::threshold(facts);
    let arrays = arr(&facts["arrays"]);
    let cuts = arrays.iter().map(|a| cut(a, threshold)).collect::<Result<Vec<Cut>, String>>()?;
    let mut out = Out::new(url);
    out.json("zarr.json", &declared_group(Map::new(), "safetensors", Some(url), Some(facts["own"].clone())));
    for g in arr(&facts["groups"]) {
        metadata_group(&mut out, g.as_str().unwrap_or_default(), None);
    }
    let mut summary = Vec::new();
    for (a, (grid, chunks)) in arrays.iter().zip(&cuts) {
        let path = a["path"].as_str().unwrap_or_default();
        out.json(&format!("{path}/zarr.json"), &array_doc(a, grid));
        summary.push(json!({
            "path": path, "dtype": a["dtype"], "dataType": a["data_type"], "shape": a["shape"],
            "chunkShape": grid, "chunks": chunks.len(),
        }));
    }
    let mut zeros = contiguous::Zeros::of(cuts.iter().flat_map(|c| c.1.iter()));
    let mut refs = Refs::new(ir, out);
    for (a, (_, chunks)) in arrays.iter().zip(cuts) {
        let path = a["path"].as_str().unwrap_or_default();
        // a chunk of rank 0 is `<path>/c` (§8.3
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#83-chunks>)
        let prefix = if arr(&a["shape"]).is_empty() { format!("{path}/c") } else { format!("{path}/c/") };
        let form = a["data"].as_u64().and_then(|d| ir.form_id(d as u32)).unwrap_or(0);
        contiguous::emit(&mut refs, chunks, (n(&a["offset"]), n(&a["length"]), form), copied, &[&prefix], |k| k, &mut zeros)?;
    }
    let mut out = refs.out;
    let mut unpresented: BTreeMap<String, u64> = BTreeMap::new();
    for t in arr(&facts["own"]["tensors"]) {
        if let Some(r) = t["reason"].as_str() {
            *unpresented.entry(r.to_string()).or_default() += 1;
        }
    }
    out.summary = json!({"tensors": summary, "unpresented": unpresented});
    Ok(out)
}

/// The store form's projection (spec/zarrify/safetensors.md §9
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#9-the-store-form>): the
/// root group, declaring the convention with the store's source metadata; a group per directory
/// above a shard; a group per presented shard at its key, declaring the convention with the
/// shard's own source metadata, holding its tensors as the file form lays them out, each chunk a
/// range of the shard's object (padded from the zero data source); and every other object,
/// referenced whole under `refkv_source/objects/`.
pub fn project_store(ir: &Ir, facts: &J, url: &str) -> Result<Out, String> {
    use super::store::{check_key, object_key};
    let shards = arr(&facts["shards"]);
    let mut out = Out::new(url);
    out.json("zarr.json", &declared_group(Map::new(), "safetensors", Some(url), Some(facts["own"].clone())));
    for d in arr(&facts["dirs"]) {
        metadata_group(&mut out, d.as_str().unwrap_or_default(), None);
    }
    // every shard's tensors, cut as the file form cuts them (no chunk is copied: a padding is a
    // range of the zero data source)
    let mut cuts = Vec::new();
    for s in shards {
        let f = &s["facts"];
        cuts.push(arr(&f["arrays"]).iter().map(|a| cut(a, usize::MAX)).collect::<Result<Vec<Cut>, String>>()?);
    }
    let mut summary = Vec::new();
    for (s, shard_cuts) in shards.iter().zip(&cuts) {
        let (path, f) = (s["path"].as_str().unwrap_or_default(), &s["facts"]);
        out.json(&format!("{path}/zarr.json"), &declared_group(Map::new(), "safetensors", None, Some(f["own"].clone())));
        for g in arr(&f["groups"]) {
            metadata_group(&mut out, &format!("{path}/{}", g.as_str().unwrap_or_default()), None);
        }
        let mut tensors = 0;
        for (a, (grid, _)) in arr(&f["arrays"]).iter().zip(shard_cuts) {
            let k = format!("{path}/{}/zarr.json", a["path"].as_str().unwrap_or_default());
            check_key(&k)?;
            out.json(&k, &array_doc(a, grid));
            tensors += 1;
        }
        summary.push(json!({"key": s["key"], "tensors": tensors}));
    }
    let mut zeros = contiguous::Zeros::of(cuts.iter().flatten().flat_map(|c| c.1.iter()));
    let mut refs = Refs::new(ir, out);
    let mut chunks_total = 0u64;
    for (s, shard_cuts) in shards.iter().zip(cuts) {
        let (path, key, f) = (s["path"].as_str().unwrap_or_default(), s["key"].as_str().unwrap_or_default(), &s["facts"]);
        for (a, (_, chunks)) in arr(&f["arrays"]).iter().zip(shard_cuts) {
            let tensor = a["path"].as_str().unwrap_or_default();
            let prefix = if arr(&a["shape"]).is_empty() { format!("{path}/{tensor}/c") } else { format!("{path}/{tensor}/c/") };
            for (piece, (c, chunk)) in chunks.into_iter().enumerate() {
                let Chunk::Ranges(parts) = chunk else { return Err("internal: a store chunk to copy".into()) };
                let parts = contiguous::padded(parts, &mut zeros, &mut refs.out)?;
                let parts: Vec<Part> = parts.into_iter().map(|p| match p {
                    Part::Src(o, n) => Part::Obj(key.to_string(), o, n),
                    p => p,
                }).collect();
                let k = super::key(&prefix, c.into_iter());
                check_key(&k)?;
                refs.chunk(k, n(&a["offset"]), n(&a["length"]), 0, Some(parts), piece as u64)?;
                chunks_total += 1;
            }
        }
    }
    let mut out = refs.out;
    for o in arr(&facts["others"]) {
        let (key, size) = (o[0].as_str().unwrap_or_default(), n(&o[1]));
        let k = object_key(key);
        check_key(&k)?;
        out.refs(&k, vec![Part::Obj(key.to_string(), 0, size)]);
    }
    let unpresented = arr(&facts["own"]["files"]).iter().filter(|f| f.get("reason").is_some()).count();
    out.summary = json!({"shards": summary, "unpresented": unpresented, "chunks": chunks_total, "others": arr(&facts["others"]).len()});
    Ok(out)
}
