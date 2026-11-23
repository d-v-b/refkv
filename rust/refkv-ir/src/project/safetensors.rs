//! The safetensors projection (spec/zarrify/safetensors.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#4-the-hierarchy>) from a
//! valid IR and the parser's description of the file (§8.5
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#85-the-description>),
//! through refkv's plan of it (`safetensors::plan`): a root group with the source metadata, a group
//! for each prefix of a tensor's path, and an array per presented tensor, its chunks cut from its `data` element
//! as conventions §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays> cuts
//! contiguous values; a packed tensor's (4 or 6 bits an element) cut by elements and mapped to
//! its bytes (spec/zarrify/safetensors.md §8.3
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#83-chunks>).

use crate::error::Error;

use super::{arr, contiguous, Copied};
use crate::ir::Ir;
use crate::out::{array_json, declared_group, grid_chunks, metadata_group, Chunk, Chunking, Out, Part};
use crate::safetensors::plan::{plan, plan_store};
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

fn cut(a: &J, threshold: usize) -> crate::error::Res<Cut> {
    let shape: Vec<u64> = arr(&a["shape"]).iter().map(n).collect();
    let (offset, bits) = (n(&a["offset"]), n(&a["bits"]));
    if bits >= 8 {
        return grid_chunks(offset, &shape, bits / 8, CHUNK_BYTES, threshold).ok_or_else(|| Error::internal("the data cannot be cut into chunks"));
    }
    match contiguous::packed_chunks(offset, &shape, bits, CHUNK_BYTES, threshold) {
        Some(cut) => Ok(cut),
        // one chunk, the whole data
        None => {
            let length = n(&a["length"]);
            let one = if length > 0 { vec![(vec![0; shape.len()], Chunk::Ranges(vec![Part::Src(offset, length)]))] } else { Vec::new() };
            Ok((shape.iter().map(|&s| s.max(1)).collect(), one))
        }
    }
}

/// The source's ranges of the edge chunks the projection copies (§8.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#83-chunks>).
pub fn reads(_ir: &Ir, description: &J, chunking: &Chunking) -> crate::error::Res<Vec<(u64, u64)>> {
    let planned = &plan(description)?;
    let threshold = chunking.copy_threshold;
    let mut out = Vec::new();
    for a in arr(&planned["arrays"]) {
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

/// The hierarchy of a file whose description is `description`, laid out as refkv's plan of it
/// says (`safetensors::plan::plan`).
pub fn project(ir: &Ir, description: &J, url: &str, copied: &Copied, chunking: &Chunking) -> crate::error::Res<Out> {
    let planned = &plan(description)?;
    let threshold = chunking.copy_threshold;
    let arrays = arr(&planned["arrays"]);
    let cuts = arrays.iter().map(|a| cut(a, threshold)).collect::<crate::error::Res<Vec<Cut>>>()?;
    let mut out = Out::new(url);
    out.json("zarr.json", &declared_group(Map::new(), "safetensors", Some(url), Some(planned["own"].clone())));
    for g in arr(&planned["groups"]) {
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
    for t in arr(&planned["own"]["tensors"]) {
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
/// referenced whole under `refkv_source/objects/`; laid out as refkv's plan of the store's
/// description says (`safetensors::plan::plan_store`).
pub fn project_store(ir: &Ir, description: &J, url: &str) -> crate::error::Res<Out> {
    use super::store::{check_key, object_key};
    let src = ir.sources();
    let listing: Vec<(String, u64)> = src.objects.iter().map(|&o| (ir.key(o), ir.len[o as usize])).collect();
    let planned = &plan_store(description, &listing)?;
    let shards = arr(&planned["shards"]);
    let mut out = Out::new(url);
    out.json("zarr.json", &declared_group(Map::new(), "safetensors", Some(url), Some(planned["own"].clone())));
    for d in arr(&planned["dirs"]) {
        metadata_group(&mut out, d.as_str().unwrap_or_default(), None);
    }
    // every shard's tensors, cut as the file form cuts them (no chunk is copied: a padding is a
    // range of the zero data source)
    let mut cuts = Vec::new();
    for s in shards {
        let f = &s["planned"];
        cuts.push(arr(&f["arrays"]).iter().map(|a| cut(a, usize::MAX)).collect::<crate::error::Res<Vec<Cut>>>()?);
    }
    let mut summary = Vec::new();
    for (s, shard_cuts) in shards.iter().zip(&cuts) {
        let (path, f) = (s["path"].as_str().unwrap_or_default(), &s["planned"]);
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
        let (path, key, f) = (s["path"].as_str().unwrap_or_default(), s["key"].as_str().unwrap_or_default(), &s["planned"]);
        for (a, (_, chunks)) in arr(&f["arrays"]).iter().zip(shard_cuts) {
            let tensor = a["path"].as_str().unwrap_or_default();
            let prefix = if arr(&a["shape"]).is_empty() { format!("{path}/{tensor}/c") } else { format!("{path}/{tensor}/c/") };
            for (c, chunk) in chunks {
                let Chunk::Ranges(parts) = chunk else { return Err(Error::internal("a store chunk to copy")) };
                let parts = contiguous::padded(parts, &mut zeros, &mut refs.out)?;
                let parts: Vec<Part> = parts.into_iter().map(|p| match p {
                    Part::Src(o, n) => Part::Obj(key.to_string(), o, n),
                    p => p,
                }).collect();
                let k = super::key(&prefix, c.into_iter());
                check_key(&k)?;
                refs.chunk(k, n(&a["offset"]), n(&a["length"]), 0, Some(parts))?;
                chunks_total += 1;
            }
        }
    }
    let mut out = refs.out;
    for o in arr(&planned["others"]) {
        let (key, size) = (o[0].as_str().unwrap_or_default(), n(&o[1]));
        let k = object_key(key);
        check_key(&k)?;
        out.refs(&k, vec![Part::Obj(key.to_string(), 0, size)]);
    }
    let unpresented = arr(&planned["own"]["files"]).iter().filter(|f| f.get("reason").is_some()).count();
    out.summary = json!({"shards": summary, "unpresented": unpresented, "chunks": chunks_total, "others": arr(&planned["others"]).len()});
    Ok(out)
}

/// The projection of the profile's store run (`StoreFormat`; crate::store).
pub fn store_project(ir: &Ir, description: &J, url: &str, _copied: &super::Copied, _chunking: &crate::out::Chunking) -> crate::error::Res<Out> {
    project_store(ir, description, url)
}
