//! The HDF4 projection (spec/zarrify/hdf4.md §2
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#2-the-hierarchy>) from a valid
//! IR: a group per presented Vgroup and an array per presented dataset, at the paths the parser
//! decided (the facts). A dataset's chunks are, by its storage (spec/zarrify/hdf4.md §3.2
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#32-chunks>): its contiguous
//! bytes cut as conventions §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays> cuts
//! contiguous values; its one compressed stream; or its chunks. Bytes in linked blocks are the
//! ranges of their blocks, and a block never written a range of the zero data source.

use super::{contiguous, key, Copied};
use crate::ir::Ir;
use crate::out::{declare, declared_group, grid_chunks, Chunk, Out, Part};
use crate::refs::Refs;
use serde_json::{json, Map, Value as J};

const PROFILE: &str = "hdf4";
/// The chunk limit of contiguous data (spec/zarrify/hdf4.md §3.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#32-chunks>).
const CHUNK_BYTES: u64 = 1 << 20;
/// The most bytes of the zero data source: a longer run of zeros is several ranges of it.
const ZEROS_MOST: u64 = 1 << 16;

fn n(v: &J) -> u64 {
    v.as_u64().unwrap_or(0)
}

fn list(v: &J) -> &[J] {
    v.as_array().map_or(&[], |a| a.as_slice())
}

fn shape_of(v: &J) -> Vec<u64> {
    list(v).iter().map(n).collect()
}

/// A storage's segments: (offset, or None for zeros, length).
fn segments(v: &J) -> Vec<(Option<u64>, u64)> {
    list(v).iter().map(|s| (s[0].as_u64(), n(&s[1]))).collect()
}

/// The parts of the bytes `[at, at + len)` of the segments `segs`, zeros as ranges of the zero
/// data source.
fn parts_of(segs: &[(Option<u64>, u64)], mut at: u64, mut len: u64, zeros: &mut contiguous::Zeros, out: &mut Out) -> Result<Vec<Part>, String> {
    let mut parts = Vec::new();
    for &(o, m) in segs {
        if len == 0 {
            break;
        }
        if at >= m {
            at -= m;
            continue;
        }
        let k = len.min(m - at);
        match o {
            Some(o) => match parts.last_mut() {
                Some(Part::Src(p, q)) if *p + *q == o + at => *q += k,
                _ => parts.push(Part::Src(o + at, k)),
            },
            None => zero_parts(&mut parts, k, zeros, out)?,
        }
        len -= k;
        at = 0;
    }
    if len > 0 {
        return Err("internal: a chunk lies past its dataset's bytes".into());
    }
    Ok(parts)
}

fn zero_parts(parts: &mut Vec<Part>, mut k: u64, zeros: &mut contiguous::Zeros, out: &mut Out) -> Result<(), String> {
    while k > 0 {
        let m = k.min(ZEROS_MOST);
        parts.push(zeros.range(out, m)?);
        k -= m;
    }
    Ok(())
}

/// The chunk shape and chunks of a contiguous array (or of one with no data, whose chunks are not
/// written).
/// A chunk shape, and each chunk with its grid coordinates.
type Cut = (Vec<u64>, Vec<(Vec<u64>, Chunk)>);

fn cut(a: &J) -> Result<Cut, String> {
    let shape = shape_of(&a["shape"]);
    grid_chunks(0, &shape, n(&a["item"]).max(1), CHUNK_BYTES, usize::MAX).ok_or_else(|| "internal: the data cannot be cut into chunks".to_string())
}

fn chunk_key(path: &str, coords: &[u64]) -> String {
    key(&if path.is_empty() { "c/".to_string() } else { format!("{path}/c/") }, coords.iter().copied())
}

pub fn project(ir: &Ir, facts: &J, url: &str, _copied: &Copied) -> Result<Out, String> {
    let nodes = list(&facts["nodes"]);
    let mut out = Out::new(url);
    // each array's chunk shape, and the zero data source its padding and unwritten blocks need
    let mut cuts = Vec::with_capacity(nodes.len());
    let mut most = 0u64;
    for node in nodes {
        let a = &node["array"];
        let store = &a["store"];
        let c = match store["kind"].as_str() {
            Some("contiguous" | "none") => Some(cut(a)?),
            _ => None,
        };
        if let Some((_, chunks)) = &c {
            for (_, ch) in chunks {
                if let Chunk::Ranges(parts) = ch
                    && let Some(Part::Lit(z)) = parts.last()
                {
                    most = most.max(z.len() as u64);
                }
            }
        }
        let mut segs: Vec<&J> = vec![&store["segments"]];
        segs.extend(list(&store["chunks"]).iter().map(|c| &c["segments"]));
        for s in segs {
            for (o, m) in segments(s) {
                if o.is_none() {
                    most = most.max(m.min(ZEROS_MOST));
                }
            }
        }
        cuts.push(c);
    }
    let mut zeros = contiguous::Zeros::sized(most);
    for (node, c) in nodes.iter().zip(&cuts) {
        let path = node["path"].as_str().unwrap_or("");
        let doc_key = if path.is_empty() { "zarr.json".to_string() } else { format!("{path}/zarr.json") };
        let attributes: Map<String, J> = node["attributes"].as_object().cloned().unwrap_or_default();
        let own = node["own"].clone();
        let a = &node["array"];
        if a.is_null() {
            out.json(&doc_key, &declared_group(attributes, PROFILE, path.is_empty().then_some(url), Some(own)));
            continue;
        }
        let shape = shape_of(&a["shape"]);
        let store = &a["store"];
        let whole: Vec<u64> = shape.iter().map(|&v| v.max(1)).collect();
        let chunk_shape = match (store["kind"].as_str(), c) {
            (Some("chunked"), _) => shape_of(&store["chunk"]),
            (_, Some((cs, _))) => cs.clone(),
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
            "dimension_names": a["dimension_names"],
        });
        out.json(&doc_key, &doc);
    }
    let mut refs = Refs::new(ir, out);
    let mut count = 0u64;
    for (node, c) in nodes.iter().zip(cuts) {
        let a = &node["array"];
        if a.is_null() {
            continue;
        }
        let path = node["path"].as_str().unwrap_or("");
        let store = &a["store"];
        match store["kind"].as_str() {
            Some("contiguous") => {
                let segs = segments(&store["segments"]);
                let Some((_, chunks)) = c else { continue };
                for (coords, chunk) in chunks {
                    let Chunk::Ranges(cp) = chunk else { return Err("internal: a contiguous chunk is not a range".into()) };
                    let mut parts = Vec::new();
                    for p in cp {
                        match p {
                            Part::Src(o, m) => parts.extend(parts_of(&segs, o, m, &mut zeros, &mut refs.out)?),
                            Part::Lit(z) => zero_parts(&mut parts, z.len() as u64, &mut zeros, &mut refs.out)?,
                            p => parts.push(p),
                        }
                    }
                    refs.chunk(chunk_key(path, &coords), 0, 0, 0, Some(parts))?;
                    count += 1;
                }
            }
            Some("whole") => {
                let segs = segments(&store["segments"]);
                let total: u64 = segs.iter().map(|s| s.1).sum();
                let parts = parts_of(&segs, 0, total, &mut zeros, &mut refs.out)?;
                refs.chunk(chunk_key(path, &vec![0; shape_of(&a["shape"]).len()]), 0, 0, 0, Some(parts))?;
                count += 1;
            }
            Some("chunked") => {
                for ch in list(&store["chunks"]) {
                    let segs = segments(&ch["segments"]);
                    let total: u64 = segs.iter().map(|s| s.1).sum();
                    let parts = parts_of(&segs, 0, total, &mut zeros, &mut refs.out)?;
                    refs.chunk(chunk_key(path, &shape_of(&ch["at"])), 0, 0, 0, Some(parts))?;
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

    /// A range across segments is their ranges, adjacent ones merged, and zeros the zero data
    /// source's.
    #[test]
    fn ranges_follow_the_blocks() {
        let mut out = Out::new("u");
        let mut zeros = contiguous::Zeros::sized(8);
        let segs = [(Some(100), 4), (Some(104), 4), (None, 8), (Some(10), 4)];
        let parts = parts_of(&segs, 2, 14, &mut zeros, &mut out).unwrap();
        assert_eq!(parts, vec![Part::Src(102, 6), Part::Data(1, 0, 8)]);
        let parts = parts_of(&segs, 15, 4, &mut zeros, &mut out).unwrap();
        assert_eq!(parts, vec![Part::Data(1, 0, 1), Part::Src(10, 3)]);
        assert!(parts_of(&segs, 18, 4, &mut zeros, &mut out).is_err());
    }
}
