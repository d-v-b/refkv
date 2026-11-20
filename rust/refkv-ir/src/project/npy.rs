//! The NumPy projection (spec/zarrify/npy.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#4-the-hierarchy>) from a valid
//! IR and the parser's description of the file: a root group with the source metadata, and an
//! array per presented NPY array (a `.npy` file's `array`, a `.npz` archive's members).
//!
//! What an array is comes from what the IR describes, not from decisions the parser made for
//! this output: its shape, its order and its data are its stored array's
//! (spec/stored-array.md §7 <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>),
//! one unit in the chunk map; its data type, byte order and fill value are read here from its
//! header's `descr`, the array's declaration
//! (§3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#3-data-types>). Its chunks
//! are cut from its `data`
//! element as conventions §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays> cuts
//! contiguous values of its stored shape; a deflated member's one chunk, its deflate stream as
//! a gzip member (spec/zarrify/npy.md §8.4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#84-chunks>).

use super::{arr, contiguous, Copied};
use crate::ir::Ir;
use crate::npy::header::{self, Dtype, Header};
use crate::out::{array_json, copy_threshold, declared_group, grid_chunks, Chunk, Out, Part, GZIP_HEADER, GZIP_LEVEL};
use crate::refs::Refs;
use crate::rules;
use serde_json::{json, Map, Value as J};
use std::collections::{BTreeMap, HashMap};

/// The most bytes of a chunk (spec/zarrify/npy.md §8.4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#84-chunks>).
const CHUNK_BYTES: u64 = 1 << 20;

fn n(v: &J) -> u64 {
    v.as_u64().unwrap_or(0)
}

/// A presented array as its declaration and its description give it: its header, parsed, and
/// the data type its `descr` is presented as; its shape in storage order, and whether that is
/// its header's reversed; and its data, the range of the source that holds it (of what a
/// deflated member's stream inflates to) and its element's form.
struct Presented<'a> {
    path: &'a str,
    header: Header,
    dtype: Dtype,
    stored: Vec<u64>,
    reversed: bool,
    data: (u64, u64),
    form: u32,
    deflate: Option<Deflated>,
}

/// A deflated member's stream: its range of the file, the extent of the derived element whose
/// space holds the member's header and data; and its CRC-32 and the size of what it inflates to,
/// as the central directory gives them.
struct Deflated {
    stream: (u64, u64),
    crc: u32,
    size: u64,
}

/// The IR's stored arrays as the projection looks them up, built once a call: the position of
/// each description by the element that keys it, and each array's one unit's element.
struct Described {
    position: HashMap<u32, usize>,
    unit: Vec<Option<u32>>,
}

impl Described {
    fn of(ir: &Ir) -> Described {
        let stored = &ir.stored;
        let position = stored.arrays.iter().enumerate().filter(|(_, s)| s.index.is_none()).map(|(k, s)| (s.element, k)).collect();
        let mut unit = vec![None; stored.arrays.len()];
        for row in 0..stored.map.len() {
            unit[stored.map.array[row] as usize] = stored.map.element[row];
        }
        Described { position, unit }
    }
}

/// The array the description's entry `a` presents. Its shape and order are those of its stored
/// array, which `a` keys, and its data is that array's one unit: a range of the file, or, for a
/// deflated member, of what its stream inflates to, the space of the derived element its header
/// and data are in.
///
/// A description may have no grid, and so no unit, for an array the profile presents all the
/// same: one whose `descr` NumPy's own layout gives no size (`a<n>`, which the profile reads as
/// `S<n>`). The profile then reads the declaring structure for the rest (spec/stored-array.md
/// §2.3 <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#23-the-grid>): the data
/// is the `data` element beside the header, or, for an array of no elements, nothing at the
/// header's end.
fn presented<'a>(ir: &Ir, described: &Described, a: &'a J) -> Result<Presented<'a>, String> {
    let rules = rules::of("npy");
    let path = a["path"].as_str().unwrap_or("array");
    let text = a["header"].as_str().ok_or("internal: a presented array without its header's text")?;
    let header = header::parse(text, rules.limit("max_rank") as usize)?;
    let dtype = header::dtype(rules, &header.descr)?;
    let key = a["stored"].as_u64().ok_or("internal: a presented array without its description's key")? as u32;
    let &k = described.position.get(&key).ok_or("internal: a presented array without a description")?;
    let array = &ir.stored.arrays[k];
    let stored = array.shape.clone().ok_or("internal: a presented array of no extent")?;
    // its one unit; else, where the description has no grid, the `data` element beside the header
    // (the parser emits it straight after the header, under the same parent)
    let beside = || {
        let next = key as usize + 1;
        (next < ir.len() && ir.parent[next] == ir.parent[key as usize] && ir.names.get(ir.name[next]) == "data").then_some(next as u32)
    };
    let (data, form) = match described.unit[k].or_else(beside) {
        Some(e) => ((ir.start[e as usize], ir.len[e as usize]), ir.form_id(e).unwrap_or(0)),
        None => ((ir.start[key as usize] + ir.len[key as usize], 0), 0),
    };
    // the bytes the header's shape and the declared type give, as the parser counts them (none
    // for an array of no elements, whatever its other axes): a unit of another length is not
    // this array's
    let length = if header.shape.contains(&0) {
        Some(0)
    } else {
        header.shape.iter().try_fold(dtype.item as u128, |n, &s| n.checked_mul(s as u128))
    };
    if length != Some(data.1 as u128) {
        return Err(format!("internal: the array at {path} holds {} bytes, not what its header declares", data.1));
    }
    // a deflated member: its header is in the space of the derived element that is its stream
    let space = ir.space[key as usize];
    let deflate = match (space, &a["deflate"]) {
        (0, _) => None,
        (d, J::Object(_)) => Some(Deflated {
            stream: (ir.start[d as usize], ir.len[d as usize]),
            crc: n(&a["deflate"]["crc"]) as u32,
            size: n(&a["deflate"]["size"]),
        }),
        _ => return Err(format!("internal: the array at {path} is in a derived space, and its description names no stream")),
    };
    // a deflated member's data is its stream's, not a recipe of the file's bytes
    let form = if deflate.is_some() { 0 } else { form };
    Ok(Presented { path, header, dtype, stored, reversed: array.axes.is_some(), data, form, deflate })
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

fn cut(a: &Presented, threshold: usize) -> Result<Cut, String> {
    let r = a.stored.len();
    let reversed = a.reversed;
    let trailing = a.dtype.trailing;
    let mut stored = a.stored.clone();
    let mut shape: Vec<u64> = if reversed { stored.iter().rev().copied().collect() } else { stored.clone() };
    let item = match trailing {
        Some(t) => {
            stored.push(t);
            shape.push(t);
            1
        }
        None => a.dtype.item,
    };
    if let Some(d) = &a.deflate {
        // one chunk, the whole array: the deflate stream as a gzip member, the header and data
        // it inflates to (spec/zarrify/npy.md §8.4
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#84-chunks>)
        let chunk_shape = shape.iter().map(|&s| s.max(1)).collect();
        let mut chunks = Vec::new();
        if a.data.1 > 0 {
            let mut trailer = d.crc.to_le_bytes().to_vec();
            trailer.extend_from_slice(&(d.size as u32).to_le_bytes());
            let parts = vec![Part::Lit(GZIP_HEADER.to_vec()), Part::Src(d.stream.0, d.stream.1), Part::Lit(trailer)];
            chunks.push((vec![0; shape.len()], Chunk::Ranges(parts)));
        }
        return Ok(Cut { shape, chunk_shape, chunks, reversed, trailing: trailing.is_some() });
    }
    let (grid, chunks) = grid_chunks(a.data.0, &stored, item, CHUNK_BYTES, threshold).ok_or("internal: the data cannot be cut into chunks")?;
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
pub fn reads(ir: &Ir, description: &J) -> Result<Vec<(u64, u64)>, String> {
    let described = Described::of(ir);
    let mut out = Vec::new();
    for a in arr(&description["arrays"]) {
        out.extend(contiguous::copy_reads(&cut(&presented(ir, &described, a)?, copy_threshold())?.chunks));
    }
    Ok(out)
}

/// An array's `zarr.json` (§4.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#43-the-arrays>).
fn array_doc(a: &Presented, c: &Cut) -> J {
    let r = a.stored.len();
    let mut codecs = Vec::new();
    if c.reversed {
        let mut order: Vec<usize> = (0..r).rev().collect();
        if c.trailing {
            order.push(r);
        }
        codecs.push(json!({"name": "transpose", "configuration": {"order": order}}));
    }
    codecs.push(match a.dtype.little {
        Some(l) => json!({"name": "bytes", "configuration": {"endian": if l { "little" } else { "big" }}}),
        None => json!({"name": "bytes"}),
    });
    if a.deflate.is_some() {
        // the data's offset in what the stream inflates to
        codecs.push(json!({"name": "refkv.skip", "configuration": {"length": a.data.0}}));
        codecs.push(json!({"name": "gzip", "configuration": {"level": GZIP_LEVEL}}));
    }
    let mut dims: Vec<String> = (0..r).map(|k| format!("dim_{k}")).collect();
    if c.trailing {
        dims.push("byte".into());
    }
    let dims: Vec<&str> = dims.iter().map(String::as_str).collect();
    let mut doc = array_json(&c.shape, "", json!(c.chunk_shape), J::Array(codecs), &dims);
    doc["data_type"] = a.dtype.zarr.clone();
    doc["fill_value"] = a.dtype.fill.clone();
    doc
}

pub fn project(ir: &Ir, description: &J, url: &str, copied: &Copied) -> Result<Out, String> {
    let facts = description;
    let threshold = copy_threshold();
    let described = Described::of(ir);
    let arrays = arr(&facts["arrays"]).iter().map(|a| presented(ir, &described, a)).collect::<Result<Vec<Presented>, String>>()?;
    let cuts = arrays.iter().map(|a| cut(a, threshold)).collect::<Result<Vec<Cut>, String>>()?;
    let mut out = Out::new(url);
    out.json("zarr.json", &declared_group(Map::new(), "npy", Some(url), Some(facts["own"].clone())));
    let mut summary = Vec::new();
    for (a, c) in arrays.iter().zip(&cuts) {
        let path = a.path;
        out.json(&format!("{path}/zarr.json"), &array_doc(a, c));
        summary.push(json!({
            "path": path, "dataType": a.dtype.zarr, "shape": c.shape,
            "order": if a.header.fortran_order { "F" } else { "C" },
            "chunkShape": c.chunk_shape, "chunks": c.chunks.len(),
        }));
    }
    let mut zeros = contiguous::Zeros::of(cuts.iter().flat_map(|c| c.chunks.iter()));
    let mut refs = Refs::new(ir, out);
    for (a, c) in arrays.iter().zip(cuts) {
        let path = a.path;
        // a chunk of rank 0 is `<path>/c` (spec/zarrify/npy.md §8.4
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#84-chunks>)
        let prefix = if c.shape.is_empty() { format!("{path}/c") } else { format!("{path}/c/") };
        // a deflated member's chunk is its stream, which its derived element is
        let element = a.deflate.as_ref().map_or(a.data, |d| d.stream);
        contiguous::emit(&mut refs, c.chunks, (element.0, element.1, a.form), copied, &[&prefix], |k| k, &mut zeros)?;
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
