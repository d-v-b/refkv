//! The HDF5 projection (spec/zarrify/hdf5.md §2
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#2-the-hierarchy>) from a valid
//! IR: a group per HDF5 group and an array per presented dataset, at their canonical paths,
//! with the attributes and storage the parser decided (the facts). The chunks are, by the
//! dataset's storage: the IR's chunk elements (chunked), the contiguous data cut as conventions
//! §7 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays>
//! cuts contiguous values (contiguous), one range in the object header (compact), a `vlen-utf8`
//! chunk of literal lengths and global heap ranges (strings), or the `vlen-utf8` bytes of
//! canonical paths (references).

use super::{arr, contiguous, key, Copied};
use crate::ir::Ir;
use crate::out::{declare, declared_group, grid_chunks, Chunk, Out, Part};
use crate::refs::Refs;
use serde_json::{json, Map, Value as J};

const PROFILE: &str = "hdf5";
/// The chunk limit of contiguous data (spec/zarrify/hdf5.md §3.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#32-chunks>).
const CHUNK_BYTES: u64 = 1 << 20;
/// A string's share of a chunk's payload, and a chunk's (2048 strings).
const STRING_ITEM: u64 = 32;
const STRING_LIMIT: u64 = 1 << 16;
/// A reference's share of a chunk (its stored pointer pair), and a chunk's.
const REFERENCE_ITEM: u64 = 16;

fn n(v: &J) -> u64 {
    v.as_u64().unwrap_or(0)
}

fn shape_of(v: &J) -> Vec<u64> {
    arr(v).iter().map(n).collect()
}

/// The chunk key of `coords` of the array at `path`: `c` alone for an array of rank 0.
fn chunk_key(path: &str, coords: &[u64]) -> String {
    let base = if path.is_empty() { "c".to_string() } else { format!("{path}/c") };
    if coords.is_empty() {
        base
    } else {
        key(&format!("{base}/"), coords.iter().copied())
    }
}

/// The contiguous data's chunks of an array (its chunk shape and chunks), or None for another
/// storage.
fn contiguous_chunks(a: &J, threshold: usize) -> Result<Option<(Vec<u64>, Vec<(Vec<u64>, Chunk)>)>, String> {
    let store = &a["store"];
    if store["kind"] != "contiguous" {
        return Ok(None);
    }
    let shape = shape_of(&a["shape"]);
    grid_chunks(n(&store["offset"]), &shape, n(&store["item"]), CHUNK_BYTES, threshold)
        .map(Some)
        .ok_or_else(|| "internal: the data cannot be cut into chunks".to_string())
}

/// The source's ranges of the contiguous edge chunks the projection copies.
pub fn reads(_ir: &Ir, facts: &J) -> Result<Vec<(u64, u64)>, String> {
    let threshold = contiguous::threshold(facts);
    let mut out = Vec::new();
    for node in arr(&facts["nodes"]) {
        if let Some((_, chunks)) = contiguous_chunks(&node["array"], threshold)? {
            out.extend(contiguous::copy_reads(&chunks));
        }
    }
    Ok(out)
}

/// A `vlen-utf8` chunk of strings, each (offset, length) in the file or (0, 0) for an empty
/// one, then `pad` empty strings: the count and each length a literal, each string a range.
fn string_parts(spans: &[(u64, u64)], pad: u64) -> Vec<Part> {
    let count = spans.len() as u64 + pad;
    let mut parts: Vec<Part> = vec![Part::Lit((count as u32).to_le_bytes().to_vec())];
    let lit = |parts: &mut Vec<Part>, b: [u8; 4]| match parts.last_mut() {
        Some(Part::Lit(l)) => l.extend_from_slice(&b),
        _ => parts.push(Part::Lit(b.to_vec())),
    };
    for &(o, len) in spans {
        lit(&mut parts, (len as u32).to_le_bytes());
        if len > 0 {
            parts.push(Part::Src(o, len));
        }
    }
    for _ in 0..pad {
        lit(&mut parts, [0; 4]);
    }
    parts
}

/// The `vlen-utf8` bytes of `texts`, then `pad` empty strings.
fn vlen_utf8(texts: &[&str], pad: u64) -> Vec<u8> {
    let mut b = ((texts.len() as u64 + pad) as u32).to_le_bytes().to_vec();
    for t in texts {
        b.extend_from_slice(&(t.len() as u32).to_le_bytes());
        b.extend_from_slice(t.as_bytes());
    }
    for _ in 0..pad {
        b.extend_from_slice(&[0; 4]);
    }
    b
}

/// The elements a chunk of a run cut by `grid_chunks` (at offset 0, `item` bytes an element)
/// holds: (first, count, padding).
fn run_of(chunk: &Chunk, item: u64) -> (u64, u64, u64) {
    match chunk {
        Chunk::Ranges(parts) => {
            let (o, len) = match parts.first() {
                Some(Part::Src(o, len)) => (*o, *len),
                _ => (0, 0),
            };
            let pad = match parts.get(1) {
                Some(Part::Lit(z)) => z.len() as u64,
                _ => 0,
            };
            (o / item, len / item, pad / item)
        }
        Chunk::Copy(o, len, pad) => (o / item, len / item, pad / item),
        Chunk::Bytes(_) => (0, 0, 0),
    }
}

pub fn project(ir: &Ir, facts: &J, url: &str, copied: &Copied) -> Result<Out, String> {
    let threshold = contiguous::threshold(facts);
    let nodes = arr(&facts["nodes"]);
    let mut out = Out::new(url);
    // every array's chunk shape and contiguous chunks first: one zero data source for every
    // contiguous array's padding
    let mut cut: Vec<Option<(Vec<u64>, Vec<(Vec<u64>, Chunk)>)>> = Vec::with_capacity(nodes.len());
    for node in nodes {
        cut.push(contiguous_chunks(&node["array"], threshold)?);
    }
    let mut zeros = contiguous::Zeros::of(cut.iter().flatten().flat_map(|c| &c.1));
    let mut docs = Vec::with_capacity(nodes.len());
    for (node, cut) in nodes.iter().zip(&cut) {
        let path = node["path"].as_str().unwrap_or("");
        let doc_key = if path.is_empty() { "zarr.json".to_string() } else { format!("{path}/zarr.json") };
        let attributes: Map<String, J> = node["attributes"].as_object().cloned().unwrap_or_default();
        let own = node["own"].clone();
        let a = &node["array"];
        if a.is_null() {
            docs.push((doc_key, declared_group(attributes, PROFILE, path.is_empty().then_some(url), Some(own))));
            continue;
        }
        let shape = shape_of(&a["shape"]);
        let store = &a["store"];
        let whole: Vec<u64> = shape.iter().map(|&v| v.max(1)).collect();
        let chunk_shape: Vec<u64> = match store["kind"].as_str() {
            Some("chunked") => shape_of(&store["chunk"]),
            Some("contiguous") => cut.as_ref().map(|c| c.0.clone()).unwrap_or(whole),
            Some("strings") => grid_chunks(0, &shape, STRING_ITEM, STRING_LIMIT, usize::MAX).map_or(whole, |c| c.0),
            Some("references") => grid_chunks(0, &shape, REFERENCE_ITEM, CHUNK_BYTES, usize::MAX).map_or(whole, |c| c.0),
            Some("records") => grid_chunks(0, &shape, n(&store["item"]).max(1), CHUNK_BYTES, usize::MAX).map_or(whole, |c| c.0),
            Some("none") if store.get("chunk").is_some() => shape_of(&store["chunk"]),
            _ => whole,
        };
        let doc = json!({
            "zarr_format": 3,
            "node_type": "array",
            "shape": shape,
            "data_type": a["data_type"],
            "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": chunk_shape}},
            "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
            "fill_value": a["fill_value"],
            "codecs": a["codecs"],
            "attributes": declare(attributes, PROFILE, None, Some(own)),
        });
        docs.push((doc_key, doc));
    }
    for (k, d) in docs {
        out.json(&k, &d);
    }
    let mut refs = Refs::new(ir, out);
    let mut count = 0u64;
    for (node, cut) in nodes.iter().zip(cut) {
        let a = &node["array"];
        if a.is_null() {
            continue;
        }
        let path = node["path"].as_str().unwrap_or("");
        let store = &a["store"];
        let shape = shape_of(&a["shape"]);
        match store["kind"].as_str() {
            Some("chunked") => {
                let el = n(&store["element"]) as u32;
                let grid = shape_of(&store["grid"]);
                let trailing = store["trailing"].as_bool().unwrap_or(false);
                let mut members = ir.members(el);
                members.sort_unstable();
                for (idx, start, length, form) in members {
                    let mut coords = crate::hdf5::unravel(idx as u128, &grid);
                    if trailing {
                        coords.push(0);
                    }
                    refs.chunk(chunk_key(path, &coords), start, length, form, None, 0)?;
                    count += 1;
                }
            }
            Some("contiguous") => {
                let Some((_, chunks)) = cut else { continue };
                let el = n(&store["element"]) as u32;
                let (start, length) = (ir.start[el as usize], ir.len[el as usize]);
                let form = ir.form_id(el).ok_or("internal: the data has no form")?;
                count += chunks.len() as u64;
                if shape.is_empty() {
                    for (c, chunk) in chunks {
                        let Chunk::Ranges(parts) = chunk else { return Err("internal: a scalar's chunk is not a range".into()) };
                        refs.chunk(chunk_key(path, &c), start, length, form, Some(parts), 0)?;
                    }
                } else {
                    let prefix = if path.is_empty() { "c/".to_string() } else { format!("{path}/c/") };
                    contiguous::emit(&mut refs, chunks, (start, length, form), copied, &[&prefix], |c| c, &mut zeros)?;
                }
            }
            Some("compact") => {
                let (o, len) = (n(&store["offset"]), n(&store["length"]));
                if shape.contains(&0) || len == 0 {
                    continue;
                }
                let coords = vec![0u64; shape.len()];
                refs.chunk(chunk_key(path, &coords), o, len, 0, Some(vec![Part::Src(o, len)]), 0)?;
                count += 1;
            }
            Some("strings") => {
                let spans: Vec<(u64, u64)> = arr(&store["spans"]).chunks(2).map(|p| (n(&p[0]), n(&p[1]))).collect();
                let Some((_, chunks)) = grid_chunks(0, &shape, STRING_ITEM, STRING_LIMIT, usize::MAX) else { continue };
                let first = spans.iter().find(|s| s.1 > 0).map_or((0, 0), |s| *s);
                for (piece, (c, chunk)) in chunks.into_iter().enumerate() {
                    let (k, m, pad) = run_of(&chunk, STRING_ITEM);
                    let parts = string_parts(&spans[k as usize..(k + m) as usize], pad);
                    refs.chunk(chunk_key(path, &c), first.0, first.1, 0, Some(parts), piece as u64)?;
                    count += 1;
                }
            }
            Some("references") => {
                let paths: Vec<&str> = arr(&store["paths"]).iter().map(|p| p.as_str().unwrap_or("")).collect();
                let Some((_, chunks)) = grid_chunks(0, &shape, REFERENCE_ITEM, CHUNK_BYTES, usize::MAX) else { continue };
                for (c, chunk) in chunks {
                    let (k, m, pad) = run_of(&chunk, REFERENCE_ITEM);
                    refs.out.bytes(&chunk_key(path, &c), vlen_utf8(&paths[k as usize..(k + m) as usize], pad));
                    count += 1;
                }
            }
            Some("records") => {
                let item = n(&store["item"]).max(1);
                let bytes = crate::hdf5::unhex(store["hex"].as_str().unwrap_or(""));
                let Some((_, chunks)) = grid_chunks(0, &shape, item, CHUNK_BYTES, usize::MAX) else { continue };
                for (c, chunk) in chunks {
                    let (k, m, pad) = run_of(&chunk, item);
                    let mut b = bytes[(k * item) as usize..((k + m) * item) as usize].to_vec();
                    b.resize(b.len() + (pad * item) as usize, 0);
                    refs.out.bytes(&chunk_key(path, &c), b);
                    count += 1;
                }
            }
            _ => {}
        }
    }
    let mut out = refs.out;
    let mut summary = facts["summary"].clone();
    summary["chunk_entries"] = json!(count);
    out.summary = summary;
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A chunk of strings: the count and the first length one literal, each string a range,
    /// lengths between them literals, an empty string and the padding only their lengths.
    #[test]
    fn strings_are_literal_lengths_and_ranges() {
        let parts = string_parts(&[(100, 3), (0, 0), (200, 2)], 2);
        assert_eq!(parts, vec![
            Part::Lit([5u32.to_le_bytes(), 3u32.to_le_bytes()].concat()),
            Part::Src(100, 3),
            Part::Lit([0u32.to_le_bytes(), 2u32.to_le_bytes()].concat()),
            Part::Src(200, 2),
            Part::Lit([0u8; 8].to_vec()),
        ]);
        assert_eq!(vlen_utf8(&["ab", ""], 1), [&3u32.to_le_bytes()[..], &2u32.to_le_bytes(), b"ab", &[0; 8]].concat());
        // 2048 strings of every offset and length a chunk can hold fit in a payload
        let worst: Vec<(u64, u64)> = (0..2048).map(|k| ((1u64 << 52) + k, u32::MAX as u64)).collect();
        assert!(crate::out::payload_size(&string_parts(&worst, 0)) <= crate::out::MAX_PAYLOAD);
    }

    #[test]
    fn scalar_chunks_are_keyed_c() {
        assert_eq!(chunk_key("a/b", &[]), "a/b/c");
        assert_eq!(chunk_key("a", &[1, 2]), "a/c/1/2");
    }
}
