//! The HDF5 projection (spec/zarrify/hdf5.md §2
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#2-the-hierarchy>) from a valid
//! IR: a group per HDF5 group and an array per presented dataset, at their canonical paths,
//! with the attributes and storage the parser decided (the facts). The chunks are, by the
//! dataset's storage: the IR's chunk elements (chunked), the contiguous data cut as conventions
//! §7 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays>
//! cuts contiguous values (contiguous), one range in the object header (compact), a `vlen-utf8`
//! chunk of literal lengths and global heap ranges (strings), or the `vlen-utf8` bytes of
//! canonical paths (references).

use super::vsi::object_url;
use super::{arr, contiguous, key, Copied};
use crate::hdf5;
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
/// The bytes of an LZF literal run, and the most a chunk's runs hold within a reference payload.
const LZF_RUN: u64 = 32;
const LZF_MOST: u64 = 1 << 16;

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
    if store["kind"] != "contiguous" && store["kind"] != "external" {
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

/// Whether a chunk of `n` bytes the filter skipped has a literal form within a reference payload
/// (its LZF form takes a part per 32 bytes).
pub fn framed(filter: u64, n: u64) -> bool {
    match filter {
        hdf5::LZF => n <= LZF_MOST,
        hdf5::DEFLATE | hdf5::BLOSC | hdf5::LZ4 | hdf5::ZSTD => true,
        _ => false,
    }
}

/// The `length` bytes of the source at `offset`, of a chunk `filter` skipped, in that filter's literal
/// form: what its codec decodes to those bytes (spec/zarrify/hdf5.md §3.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#32-chunks>). `adler` is their
/// Adler-32, for deflate; `item` the elements' bytes, for Blosc.
fn framed_parts(filter: u64, offset: u64, length: u64, adler: u32, item: u64) -> Vec<Part> {
    let runs = |most: u64, head: &dyn Fn(u64, bool) -> Vec<u8>, first: Vec<u8>, last: Vec<u8>| {
        let mut lit = first;
        let mut parts = Vec::new();
        let mut at = 0;
        while at < length {
            let n = most.min(length - at);
            lit.extend_from_slice(&head(n, at + n == length));
            parts.push(Part::Lit(std::mem::take(&mut lit)));
            parts.push(Part::Src(offset + at, n));
            at += n;
        }
        if !last.is_empty() {
            parts.push(Part::Lit(last));
        }
        parts
    };
    match filter {
        // RFC 1950's header and Adler-32, around RFC 1951's stored blocks
        hdf5::DEFLATE => runs(
            65535,
            &|n, end| {
                let mut h = vec![end as u8];
                h.extend_from_slice(&(n as u16).to_le_bytes());
                h.extend_from_slice(&(!(n as u16)).to_le_bytes());
                h
            },
            vec![0x78, 0x01],
            adler.to_be_bytes().to_vec(),
        ),
        // LZF's literal runs: a control byte below 32, one less than the bytes that follow
        hdf5::LZF => runs(LZF_RUN, &|n, _| vec![(n - 1) as u8], Vec::new(), Vec::new()),
        // RFC 8878's frame, of one segment with its content size, and raw blocks
        hdf5::ZSTD => {
            let mut first = vec![0x28, 0xB5, 0x2F, 0xFD, 0xE0];
            first.extend_from_slice(&length.to_le_bytes());
            runs(1 << 17, &|n, end| ((n as u32) << 3 | end as u32).to_le_bytes()[..3].to_vec(), first, Vec::new())
        }
        // the filter's framing, a block as long as the chunk, stored because its length is the block's
        hdf5::LZ4 => {
            let mut head = length.to_be_bytes().to_vec();
            head.extend_from_slice(&(length as u32).to_be_bytes());
            head.extend_from_slice(&(length as u32).to_be_bytes());
            vec![Part::Lit(head), Part::Src(offset, length)]
        }
        // c-blosc's header of a chunk it copied (flag 2): format 2, 16 bytes before the data
        _ => {
            let mut head = vec![2, 1, 2, item.min(255) as u8];
            head.extend_from_slice(&(length as u32).to_le_bytes());
            head.extend_from_slice(&(length as u32).to_le_bytes());
            head.extend_from_slice(&((length + 16) as u32).to_le_bytes());
            vec![Part::Lit(head), Part::Src(offset, length)]
        }
    }
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
    build(ir, facts, url, url, copied)
}

/// The output of a file read as a store (spec/zarrify/hdf5.md §8.6
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#86-external-files>), its store
/// URL `url`: the root's `source.url` is the named file's object URL.
pub fn project_store(ir: &Ir, facts: &J, url: &str) -> Result<Out, String> {
    let named = object_url(url, facts["named_key"].as_str().unwrap_or(""));
    build(ir, facts, url, &named, &Copied::new())
}

/// A range of the external data `ranges` (key, offset, length) lay out, as ranges of their files.
fn external_parts(ranges: &[J], mut at: u64, mut left: u64) -> Result<Vec<Part>, String> {
    let mut parts = Vec::new();
    for r in ranges {
        let (offset, length) = (n(&r[1]), n(&r[2]));
        if at >= length {
            at -= length;
            continue;
        }
        let k = left.min(length - at);
        parts.push(Part::Obj(r[0].as_str().unwrap_or("").to_string(), offset + at, k));
        left -= k;
        at = 0;
        if left == 0 {
            return Ok(parts);
        }
    }
    Err("internal: a chunk lies past its external files".into())
}

fn build(ir: &Ir, facts: &J, url: &str, source: &str, copied: &Copied) -> Result<Out, String> {
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
            docs.push((doc_key, declared_group(attributes, PROFILE, path.is_empty().then_some(source), Some(own))));
            continue;
        }
        let shape = shape_of(&a["shape"]);
        let store = &a["store"];
        let whole: Vec<u64> = shape.iter().map(|&v| v.max(1)).collect();
        let chunk_shape: Vec<u64> = match store["kind"].as_str() {
            Some("chunked") => shape_of(&store["chunk"]),
            Some("contiguous" | "external") => cut.as_ref().map(|c| c.0.clone()).unwrap_or(whole),
            Some("strings") => grid_chunks(0, &shape, STRING_ITEM, STRING_LIMIT, usize::MAX).map_or(whole, |c| c.0),
            Some("references") => grid_chunks(0, &shape, REFERENCE_ITEM, CHUNK_BYTES, usize::MAX).map_or(whole, |c| c.0),
            Some("records") => grid_chunks(0, &shape, n(&store["item"]).max(1), CHUNK_BYTES, usize::MAX).map_or(whole, |c| c.0),
            Some("none") if store.get("chunk").is_some() => shape_of(&store["chunk"]),
            _ => whole,
        };
        let mut doc = json!({
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
        if !a["dimension_names"].is_null() {
            doc["dimension_names"] = a["dimension_names"].clone();
        }
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
        if let Some(object) = store["object"].as_u64() {
            refs.in_object_of(object as u32);
        }
        match store["kind"].as_str() {
            Some("external") => {
                let Some((_, chunks)) = cut else { continue };
                let ranges = arr(&store["ranges"]);
                let total: u64 = ranges.iter().map(|r| n(&r[2])).sum();
                count += chunks.len() as u64;
                for (c, chunk) in chunks {
                    let Chunk::Ranges(parts) = chunk else { return Err("internal: an external chunk is not a range".into()) };
                    let mut placed = Vec::new();
                    for part in contiguous::padded(parts, &mut zeros, &mut refs.out)? {
                        match part {
                            Part::Src(o, len) => placed.extend(external_parts(ranges, o, len)?),
                            part => placed.push(part),
                        }
                    }
                    refs.chunk(chunk_key(path, &c), n(&store["address"]), total, 0, Some(placed))?;
                }
            }
            Some("chunked") => {
                let el = n(&store["element"]) as u32;
                let grid = shape_of(&store["grid"]);
                let trailing = store["trailing"].as_bool().unwrap_or(false);
                let trim = n(&store["trim"]);
                let masked: std::collections::HashMap<u64, &J> = arr(&store["masked"]).iter().map(|m| (n(&m["index"]), m)).collect();
                let mut members = ir.members(el);
                members.sort_unstable();
                for (idx, start, length, form) in members {
                    let mut coords = crate::hdf5::unravel(idx as u128, &grid);
                    if trailing {
                        coords.push(0);
                    }
                    // a checksum left out, and a chunk a filter skipped given the form its codec reads
                    let parts = match masked.get(&idx) {
                        Some(m) => Some(match m["wrap"].as_u64() {
                            Some(f) => framed_parts(f, start, n(&m["stored"]), n(&m["adler"]) as u32, n(&m["item"])),
                            None => vec![Part::Src(start, n(&m["stored"]))],
                        }),
                        None if trim > 0 => Some(vec![Part::Src(start, length - trim)]),
                        None => None,
                    };
                    refs.chunk(chunk_key(path, &coords), start, length, form, parts)?;
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
                        refs.chunk(chunk_key(path, &c), start, length, form, Some(parts))?;
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
                refs.chunk(chunk_key(path, &coords), o, len, 0, Some(vec![Part::Src(o, len)]))?;
                count += 1;
            }
            Some("strings") => {
                let spans: Vec<(u64, u64)> = arr(&store["spans"]).chunks(2).map(|p| (n(&p[0]), n(&p[1]))).collect();
                let Some((_, chunks)) = grid_chunks(0, &shape, STRING_ITEM, STRING_LIMIT, usize::MAX) else { continue };
                let first = spans.iter().find(|s| s.1 > 0).map_or((0, 0), |s| *s);
                for (c, chunk) in chunks {
                    let (k, m, pad) = run_of(&chunk, STRING_ITEM);
                    let parts = string_parts(&spans[k as usize..(k + m) as usize], pad);
                    refs.chunk(chunk_key(path, &c), first.0, first.1, 0, Some(parts))?;
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

/// The projection of the profile's store run (`StoreFormat`; crate::store).
pub fn store_project(ir: &Ir, facts: &J, url: &str) -> Result<Out, String> {
    project_store(ir, facts, url)
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

    /// Stored blocks inflate to the bytes they name: one block, and two past its limit; and the
    /// longest LZF form fits in a payload.
    #[test]
    fn literal_forms_read_as_their_filters_write() {
        for len in [1u64, 65535, 65536, 200_000] {
            let src: Vec<u8> = (0..100 + len).map(|k| (k % 251) as u8).collect();
            let want = &src[100..];
            let adler = {
                let (mut a, mut c) = (1u32, 0u32);
                for &x in want {
                    a = (a + x as u32) % 65521;
                    c = (c + a) % 65521;
                }
                c << 16 | a
            };
            let mut z = Vec::new();
            for p in framed_parts(hdf5::DEFLATE, 100, len, adler, 1) {
                match p {
                    Part::Lit(b) => z.extend_from_slice(&b),
                    Part::Src(o, m) => z.extend_from_slice(&src[o as usize..(o + m) as usize]),
                    _ => panic!("a part neither literal nor source"),
                }
            }
            assert_eq!(miniz_oxide::inflate::decompress_to_vec_zlib(&z).unwrap(), want);
        }
        assert!(framed(hdf5::LZF, LZF_MOST) && !framed(hdf5::LZF, LZF_MOST + 1) && !framed(hdf5::BITSHUFFLE, 8));
        assert!(crate::out::payload_size(&framed_parts(hdf5::LZF, (1 << 52) + 1, LZF_MOST, 0, 1)) <= crate::out::MAX_PAYLOAD);
    }

    #[test]
    fn scalar_chunks_are_keyed_c() {
        assert_eq!(chunk_key("a/b", &[]), "a/b/c");
        assert_eq!(chunk_key("a", &[1, 2]), "a/c/1/2");
    }
}
