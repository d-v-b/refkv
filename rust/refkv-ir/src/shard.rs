//! Virtual shards (spec/zarrify.md §2.1
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#21-virtual-shards>): a pass over a
//! projection's output that makes an array of chunk references a `sharding_indexed` array, each
//! shard one reference to its inner chunks' ranges and a literal shard index. A projection opts
//! in with `Out::shard(path, shape)`, the inner chunks a shard holds along each axis, after it
//! wrote the array's document and chunks as an unsharded array; the run applies it
//! (`run::output`).

use crate::out::{payload_size, to_text, Out, Part, MAX_PAYLOAD};
use serde_json::{json, Value as J};
use std::collections::HashMap;

/// The index entry of an inner chunk a shard does not hold (the sharding codec's).
pub const EMPTY: u64 = u64::MAX;

/// The CRC-32C (Castagnoli, reflected; RFC 3720 §B.4) of `b`, as the `crc32c` codec appends it:
/// bitwise, over an index of at most a few KiB a shard, so that the core keeps to its two
/// dependencies (the CLI's `crc32c` crate checksums the sources' ranges, which are large).
pub fn crc32c(b: &[u8]) -> u32 {
    !b.iter().fold(!0u32, |mut c, &x| {
        c ^= x as u32;
        for _ in 0..8 {
            c = if c & 1 != 0 { (c >> 1) ^ 0x82F6_3B78 } else { c >> 1 };
        }
        c
    })
}

/// The bytes a range describes.
fn size(p: &Part) -> u64 {
    match p {
        Part::Src(_, n) | Part::Data(_, _, n) | Part::Obj(_, _, n) => *n,
        Part::Lit(b) => b.len() as u64,
    }
}

/// `b` appended to `a` when they are adjacent ranges of one source: a's end is b's start.
fn join(a: &mut Part, b: &Part) -> bool {
    match (a, b) {
        (Part::Src(o, n), Part::Src(p, m)) if *o + *n == *p => *n += m,
        (Part::Data(i, o, n), Part::Data(j, p, m)) if i == j && *o + *n == *p => *n += m,
        (Part::Obj(k, o, n), Part::Obj(l, p, m)) if k == l && *o + *n == *p => *n += m,
        _ => return false,
    }
    true
}

/// The shard of inner chunks `chunks` (each its parts, None when absent), in row-major order
/// over the shard's inner grid: their parts one after the other, the first range of each
/// joined to the range before it when `merge` and they are adjacent, then the index (each
/// chunk's offset in the shard and its length, 8-byte little-endian, or (EMPTY, EMPTY)) and its
/// CRC-32C, 4-byte little-endian, as one literal range.
pub fn shard_parts(chunks: &[Option<Vec<Part>>], merge: bool) -> Vec<Part> {
    let mut parts: Vec<Part> = Vec::new();
    let mut index = Vec::with_capacity(16 * chunks.len() + 4);
    let mut at = 0u64;
    for c in chunks {
        let Some(c) = c else {
            index.extend_from_slice(&EMPTY.to_le_bytes());
            index.extend_from_slice(&EMPTY.to_le_bytes());
            continue;
        };
        let n: u64 = c.iter().map(size).sum();
        index.extend_from_slice(&at.to_le_bytes());
        index.extend_from_slice(&n.to_le_bytes());
        at += n;
        for (k, p) in c.iter().enumerate() {
            let joined = merge && k == 0 && parts.last_mut().is_some_and(|last| join(last, p));
            if !joined {
                parts.push(p.clone());
            }
        }
    }
    let crc = crc32c(&index);
    index.extend_from_slice(&crc.to_le_bytes());
    parts.push(Part::Lit(index));
    parts
}

/// The coordinates of `i` in a row-major grid of `shape`.
fn unravel(mut i: u64, shape: &[u64]) -> Vec<u64> {
    let mut c = vec![0; shape.len()];
    for k in (0..shape.len()).rev() {
        c[k] = i % shape[k];
        i /= shape[k];
    }
    c
}

fn key(path: &str, c: &[u64]) -> String {
    let mut k = format!("{path}/c");
    for x in c {
        k.push('/');
        k.push_str(&x.to_string());
    }
    k
}

/// The array at `path` of `out` as a sharded array of `shard` inner chunks along each axis: its
/// chunk references and copied chunks become the shards' references, and its document's chunk
/// shape the shard's, its codecs the sharding codec's inner codecs. `merge`: whether ranges
/// adjacent across inner chunks are joined (not when the ranges carry checksums).
pub fn apply(out: &mut Out, path: &str, shard: &[u64], merge: bool) -> Result<(), String> {
    let doc_key = format!("{path}/zarr.json");
    let mut doc: J = out
        .entries
        .iter()
        .find(|e| e.0 == doc_key)
        .and_then(|e| serde_json::from_slice(&e.1).ok())
        .ok_or_else(|| format!("internal: no array document {doc_key} to shard"))?;
    let nums = |v: &J| -> Option<Vec<u64>> { v.as_array()?.iter().map(|x| x.as_u64()).collect() };
    let shape = nums(&doc["shape"]).ok_or("internal: an array without a shape")?;
    let inner = nums(&doc["chunk_grid"]["configuration"]["chunk_shape"]).ok_or("internal: an array without a regular grid")?;
    if doc["chunk_grid"]["name"] != "regular" || doc["chunk_key_encoding"] != json!({"name": "default", "configuration": {"separator": "/"}})
        || inner.len() != shape.len() || shard.len() != shape.len() || inner.contains(&0) || shard.contains(&0)
    {
        return Err(format!("internal: {path} cannot be sharded"));
    }
    let grid: Vec<u64> = shape.iter().zip(&inner).map(|(s, c)| s.div_ceil(*c)).collect();
    let shards: Vec<u64> = grid.iter().zip(shard).map(|(g, s)| g.div_ceil(*s)).collect();
    // the inner chunks, by their coordinates
    let prefix = format!("{path}/c/");
    let mut chunks: HashMap<Vec<u64>, Vec<Part>> = HashMap::new();
    for (k, v) in out.take_entries(&prefix) {
        chunks.insert(coords(&k[prefix.len()..])?, vec![Part::Lit(v)]);
    }
    for (k, v) in out.take_refs(&prefix) {
        chunks.insert(coords(&k[prefix.len()..])?, v);
    }
    let per: u64 = shard.iter().product();
    let count: u64 = shards.iter().product();
    for s in 0..count {
        let sc = unravel(s, &shards);
        let held: Vec<Option<Vec<Part>>> = (0..per)
            .map(|i| {
                let c: Vec<u64> = unravel(i, shard).iter().zip(&sc).zip(shard).map(|((i, s), n)| s * n + i).collect();
                chunks.remove(&c)
            })
            .collect();
        if held.iter().all(Option::is_none) {
            continue;
        }
        let copied: usize = held.iter().flatten().filter_map(|c| match c.as_slice() {
            [Part::Lit(b)] => Some(b.len()),
            _ => None,
        }).sum();
        let parts = shard_parts(&held, merge);
        let k = key(path, &sc);
        if payload_size(&parts) > MAX_PAYLOAD {
            return Err(format!("{k}: a shard's reference payload of more than {MAX_PAYLOAD} bytes (of which {copied} \
                                bytes of copied inner chunks)"));
        }
        out.refs(&k, parts);
    }
    if let Some(c) = chunks.keys().next() {
        return Err(format!("internal: {} is outside {path}'s grid", key(path, c)));
    }
    let outer: Vec<u64> = inner.iter().zip(shard).map(|(c, s)| c * s).collect();
    doc["codecs"] = json!([{"name": "sharding_indexed", "configuration": {
        "chunk_shape": inner,
        "codecs": doc["codecs"].clone(),
        "index_codecs": [{"name": "bytes", "configuration": {"endian": "little"}}, {"name": "crc32c"}],
        "index_location": "end",
    }}]);
    doc["chunk_grid"]["configuration"]["chunk_shape"] = json!(outer);
    out.bytes(&doc_key, to_text(&doc).into_bytes());
    Ok(())
}

/// A chunk key's coordinates (`3/0`).
fn coords(s: &str) -> Result<Vec<u64>, String> {
    s.split('/').map(|x| x.parse::<u64>().map_err(|_| format!("internal: {s} is not a chunk key"))).collect()
}

/// Every shard request of `out` (`Out::shard`), in order; and, when there is one, the summary's
/// `joinRanges`: whether ranges adjacent across inner chunks were joined (`merge`: not when the
/// host records checksums).
pub fn apply_all(out: &mut Out, merge: bool) -> Result<(), String> {
    let requests = std::mem::take(&mut out.shards);
    if requests.is_empty() {
        return Ok(());
    }
    for (path, shard) in requests {
        apply(out, &path, &shard, merge)?;
    }
    if let Some(m) = out.summary.as_object_mut() {
        m.insert("joinRanges".into(), merge.into());
    }
    Ok(())
}

/// Fails when `out` asks for shards: a store run's hosts do not pass whether they record
/// checksums, so a store profile cannot shard until they do.
pub fn refuse(out: &Out) -> Result<(), String> {
    match out.shards.first() {
        Some((path, _)) => Err(format!("internal: {path} asks to be sharded, which a store run does not do")),
        None => Ok(()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::out::array_json;

    #[test]
    fn crc32c_is_castagnolis() {
        assert_eq!(crc32c(b"123456789"), 0xE306_9283);
        assert_eq!(crc32c(b""), 0);
    }

    fn index(entries: &[(u64, u64)]) -> Part {
        let mut b = Vec::new();
        for (o, n) in entries {
            b.extend_from_slice(&o.to_le_bytes());
            b.extend_from_slice(&n.to_le_bytes());
        }
        let crc = crc32c(&b);
        b.extend_from_slice(&crc.to_le_bytes());
        Part::Lit(b)
    }

    /// A 5 x 3 array of 2 x 2 chunks (a 3 x 2 grid) in shards of 2 x 2 chunks: the chunks laid
    /// out row-major in each shard, those adjacent in the source joined when merging and kept
    /// apart when not, absent chunks given the empty entry, a copied chunk as a literal, and the
    /// document's grid and codecs the sharding codec's.
    #[test]
    fn shards_lay_out_inner_chunks_row_major_with_an_index() {
        for merge in [true, false] {
            let mut out = Out::new("u");
            out.json("a/zarr.json", &array_json(&[5, 3], "uint8", json!([2, 2]), json!([{"name": "bytes"}]), &["y", "x"]));
            out.refs("a/c/0/0", vec![Part::Src(0, 4)]);
            out.refs("a/c/0/1", vec![Part::Src(4, 4)]);
            out.refs("a/c/1/1", vec![Part::Lit(vec![9]), Part::Src(20, 4)]);
            out.refs("a/c/1/0", vec![Part::Src(8, 4)]);
            out.bytes("a/c/2/1", vec![7, 7, 7, 7]);
            out.shard("a", &[2, 2]);
            apply_all(&mut out, merge).unwrap();
            let first: Vec<Part> = if merge {
                vec![Part::Src(0, 12), Part::Lit(vec![9]), Part::Src(20, 4)]
            } else {
                vec![Part::Src(0, 4), Part::Src(4, 4), Part::Src(8, 4), Part::Lit(vec![9]), Part::Src(20, 4)]
            };
            let mut want = first;
            want.push(index(&[(0, 4), (4, 4), (8, 4), (12, 5)]));
            assert_eq!(out.reference("a/c/0/0").unwrap(), want.as_slice());
            assert_eq!(out.reference("a/c/1/0").unwrap(), [Part::Lit(vec![7; 4]), index(&[(EMPTY, EMPTY), (0, 4), (EMPTY, EMPTY), (EMPTY, EMPTY)])]);
            assert!(out.reference("a/c/0/1").is_none() && out.reference("a/c/1/1").is_none());
            assert!(!out.has("a/c/2/1"));
            let doc: J = serde_json::from_slice(&out.entries.iter().find(|e| e.0 == "a/zarr.json").unwrap().1).unwrap();
            assert_eq!(doc["chunk_grid"]["configuration"]["chunk_shape"], json!([4, 4]));
            assert_eq!(doc["codecs"][0]["configuration"]["chunk_shape"], json!([2, 2]));
            assert_eq!(doc["codecs"][0]["configuration"]["codecs"], json!([{"name": "bytes"}]));
        }
    }

    /// An output with the array `a` of `doc`'s grid and the chunk references `keys`, its shards
    /// of `shard` inner chunks applied.
    fn sharded(doc: J, keys: &[&str], shard: &[u64]) -> Result<(), String> {
        let mut out = Out::new("u");
        out.json("a/zarr.json", &doc);
        for k in keys {
            out.refs(k, vec![Part::Src(0, 1)]);
        }
        out.shard("a", shard);
        apply_all(&mut out, true)
    }

    fn doc(shape: &[u64], chunks: &[u64]) -> J {
        array_json(shape, "uint8", json!(chunks), json!([{"name": "bytes"}]), &["y", "x"][2 - shape.len()..])
    }

    #[test]
    fn the_summary_says_whether_ranges_were_joined() {
        for merge in [true, false] {
            let mut out = Out::new("u");
            out.json("a/zarr.json", &doc(&[2], &[1]));
            out.refs("a/c/0", vec![Part::Src(0, 1)]);
            out.shard("a", &[2]);
            apply_all(&mut out, merge).unwrap();
            assert_eq!(out.summary["joinRanges"], json!(merge));
        }
        let mut out = Out::new("u");
        apply_all(&mut out, true).unwrap();
        assert!(out.summary.get("joinRanges").is_none());
    }

    #[test]
    fn rejects_an_array_without_a_document() {
        let mut out = Out::new("u");
        out.shard("a", &[2]);
        assert_eq!(apply_all(&mut out, true), Err("internal: no array document a/zarr.json to shard".into()));
    }

    #[test]
    fn rejects_an_irregular_grid() {
        let mut d = doc(&[4], &[2]);
        d["chunk_grid"]["name"] = json!("rectilinear");
        assert_eq!(sharded(d, &[], &[2]), Err("internal: a cannot be sharded".into()));
    }

    #[test]
    fn rejects_another_chunk_key_encoding() {
        let mut d = doc(&[4], &[2]);
        d["chunk_key_encoding"]["configuration"]["separator"] = json!(".");
        assert_eq!(sharded(d, &[], &[2]), Err("internal: a cannot be sharded".into()));
    }

    #[test]
    fn rejects_a_shard_shape_of_another_rank() {
        assert_eq!(sharded(doc(&[4, 4], &[2, 2]), &[], &[2]), Err("internal: a cannot be sharded".into()));
    }

    #[test]
    fn rejects_an_empty_shard_shape() {
        assert_eq!(sharded(doc(&[4], &[2]), &[], &[0]), Err("internal: a cannot be sharded".into()));
    }

    #[test]
    fn rejects_a_chunk_outside_the_grid() {
        assert_eq!(sharded(doc(&[4], &[2]), &["a/c/0", "a/c/2"], &[2]), Err("internal: a/c/2 is outside a's grid".into()));
    }

    #[test]
    fn rejects_a_chunk_key_that_is_not_coordinates() {
        assert_eq!(sharded(doc(&[4], &[2]), &["a/c/x"], &[2]), Err("internal: x is not a chunk key".into()));
    }

    #[test]
    fn a_store_run_refuses_a_shard_request() {
        let mut out = Out::new("u");
        assert_eq!(refuse(&out), Ok(()));
        out.shard("a", &[2]);
        assert_eq!(refuse(&out), Err("internal: a asks to be sharded, which a store run does not do".into()));
    }

    #[test]
    fn rejects_a_shard_past_the_payload_limit() {
        let mut out = Out::new("u");
        out.json("a/zarr.json", &array_json(&[4096], "uint8", json!([1]), json!([{"name": "bytes"}]), &["x"]));
        for i in 0..4096u64 {
            out.refs(&format!("a/c/{i}"), vec![Part::Src((1 << 40) | (2 * i), 1)]);
        }
        out.shard("a", &[4096]);
        assert_eq!(apply_all(&mut out, false),
                   Err("a/c/0: a shard's reference payload of more than 65519 bytes (of which 0 bytes of copied inner chunks)".into()));
    }
}
