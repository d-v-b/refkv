//! The stored arrays of a Zarr store, v2 or v3 (spec/stored-array.md §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): what an
//! array's metadata document says of how the store holds it, read for the description and for
//! nothing else. Each array is its document's `shape`; its storage order, where the document
//! declares another than its own (a Zarr v2 `order` of `F`, a Zarr v3 array's leading
//! `transpose` codecs); the bits of one element its data type gives; its chunk grid, under
//! `full` (a Zarr chunk past an axis's end is stored whole); its codecs as the IR's chain,
//! a codec the IR's list has no name for kept as `source` with the document's own id and
//! configuration; and the bytes of one element its fill value gives. The parsers (`zarr2.rs`,
//! `zarr3.rs`) find the chunks, by these chunk keys, and add the descriptions.
//!
//! Nothing here decides an output: no data type is named, no fill value written, and a document
//! the Zarr profiles refuse is described as far as it reads.

use crate::array::{AxisUnits, Chain, ChunkMap, Codec, Edge, Fill, Grid, Param, Place, Row, Run, StoredArray};
use crate::json::Jv;
use crate::npy::header::{self, Lit};
use crate::project::store::canonical_index;
use serde_json::{json, Value as J};

const MAX_SAFE: i64 = 9007199254740991;
/// The most axes a document's shape is read with.
const MAX_DIMS: usize = 32;

/// How a store names an array's chunks: the `v2` chunk key encoding (Zarr v2's own keys) or the
/// `default` one (`c`, then each index after the separator), the separator, and the chunks along
/// each of the document's axes.
#[derive(Clone, Debug, PartialEq)]
pub struct Keys {
    pub v2: bool,
    pub sep: String,
    pub counts: Vec<u64>,
}

impl Keys {
    /// The chunk indexes `rest` names (a key below the array, after its path), in the document's
    /// order of the axes, or None for a key that is not one of the array's chunk keys
    /// (spec/zarrify/zarr2.md §3.2
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#32-chunks>, spec/zarrify/zarr3.md
    /// §3.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#31-chunk-grids-and-chunk-keys>).
    pub fn coords(&self, rest: &str) -> Option<Vec<u64>> {
        if self.v2 {
            return v2_coords(rest, &self.counts, &self.sep);
        }
        if self.counts.is_empty() {
            return (rest == "c").then(Vec::new);
        }
        v2_coords(rest.strip_prefix('c')?.strip_prefix(self.sep.as_str())?, &self.counts, &self.sep)
    }
}

/// The indexes of a Zarr v2 chunk key: one per axis, each canonical and below its count, joined
/// by `sep`; `0` for an array of no axes.
pub fn v2_coords(rest: &str, counts: &[u64], sep: &str) -> Option<Vec<u64>> {
    if counts.is_empty() {
        return (rest == "0").then(Vec::new);
    }
    if sep == "." && rest.contains('/') {
        return None;
    }
    let parts: Vec<&str> = rest.split(sep).collect();
    if parts.len() != counts.len() {
        return None;
    }
    parts.iter().zip(counts).map(|(s, &n)| if canonical_index(s, n) { s.parse().ok() } else { None }).collect()
}

/// What a document says of the storage of an array that has a grid.
#[derive(Clone, Debug)]
pub struct Storage {
    /// the units along each of the document's axes
    pub axes: Vec<AxisUnits>,
    /// the bytes of one element the fill value gives, where it gives them
    pub fill: Option<Vec<u8>>,
    pub chain: Chain,
    pub keys: Keys,
}

/// What a document says of an array, read for its description.
#[derive(Clone, Debug)]
pub struct Layout {
    /// its document's shape, in the document's order
    pub shape: Vec<u64>,
    /// storage axis i is the document's axis `order[i]`; None where they are one
    pub order: Option<Vec<u32>>,
    pub item_bits: Option<u64>,
    /// None where the IR does not model the array's storage
    pub storage: Option<Storage>,
}

impl Layout {
    /// The description of the array, keyed by `element` with `declarations`, and its rows: each
    /// unit `units` gives, its indexes in the document's order and where its bytes are (a row's
    /// `element` or `ranges`, the rest of which this sets). A description whose storage it does
    /// not model has no rows.
    pub fn describe(&self, element: u32, declarations: Vec<Place>, units: &[(Vec<u64>, Row)], into: &mut Described) -> Result<(), String> {
        let permute = |v: &[u64]| -> Vec<u64> {
            match &self.order {
                Some(o) => o.iter().map(|&a| v[a as usize]).collect(),
                None => v.to_vec(),
            }
        };
        let k = into.arrays.len() as u32;
        let mut a = StoredArray {
            element,
            index: None,
            declarations,
            shape: Some(permute(&self.shape)),
            axes: self.order.clone(),
            item_bits: self.item_bits,
            pad_from: None,
            grid: None,
            chains: Vec::new(),
            unit_parameters: Vec::new(),
        };
        if let Some(s) = &self.storage {
            let axes = match &self.order {
                Some(o) => o.iter().map(|&i| s.axes[i as usize].clone()).collect(),
                None => s.axes.clone(),
            };
            let fill = s.fill.as_ref().map(|b| Fill::Given(Param::Value(J::String(base64(b)))));
            a.grid = Some(Grid::sparse(axes, Edge::Full, fill));
            a.chains = vec![into.chain(&s.chain)];
            for (coords, at) in units {
                into.map.push(Row { array: k, coords: permute(coords), ..at.clone() })?;
            }
        }
        into.arrays.push(a);
        Ok(())
    }
}

/// The descriptions a store's parser adds to its IR, with their chain table and chunk map.
#[derive(Default)]
pub struct Described {
    pub arrays: Vec<StoredArray>,
    pub chains: Vec<Chain>,
    pub map: ChunkMap,
}

impl Described {
    /// The position of `chain` in the table, added where it is new.
    fn chain(&mut self, chain: &Chain) -> u32 {
        match self.chains.iter().position(|c| c == chain) {
            Some(k) => k as u32,
            None => {
                self.chains.push(chain.clone());
                (self.chains.len() - 1) as u32
            }
        }
    }
}

/// The bytes as a base64 string (RFC 4648 §4, with padding).
fn base64(b: &[u8]) -> String {
    const A: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(b.len().div_ceil(3) * 4);
    for c in b.chunks(3) {
        let n = c.iter().enumerate().fold(0u32, |n, (i, &x)| n | u32::from(x) << (16 - 8 * i));
        for i in 0..4 {
            out.push(if i <= c.len() { A[(n >> (18 - 6 * i) & 63) as usize] as char } else { '=' });
        }
    }
    out
}

/// A list of integers from `lo` to 2^53 − 1.
fn ints(v: Option<&Jv>, lo: i64) -> Option<Vec<u64>> {
    v?.items().filter(|a| a.len() <= MAX_DIMS)?.iter().map(|x| x.int(lo, MAX_SAFE).map(|i| i as u64)).collect()
}

/// A serde value of a document's value, where serde holds it (not a string with a lone
/// surrogate).
fn to_j(v: &Jv) -> Option<J> {
    serde_json::from_str(&v.text()).ok()
}

fn codec(name: &str, parameters: Vec<(&str, Param)>) -> Codec {
    Codec { name: name.into(), parameters: parameters.into_iter().map(|(k, x)| (k.to_string(), x)).collect() }
}

/// The codec `source` of a Zarr codec the IR's list has no name for: the profile's name, the
/// codec's id and its configuration as the document gives it (where it gives one, and serde
/// holds it).
fn source(format: &str, id: &str, conf: Option<&Jv>) -> Codec {
    let mut p = vec![("format", Param::Value(json!(format))), ("id", Param::Value(json!(id)))];
    if let Some(c) = conf.and_then(to_j) {
        p.push(("parameters", Param::Value(c)));
    }
    codec("source", p)
}

/// The IR's codec of a numcodecs codec of id `id` (a Zarr v2 compressor or filter, or a Zarr v3
/// codec named `numcodecs.<id>`) and configuration `conf`, where the IR's list names its
/// encoding: zlib, gzip, bzip2, Zstandard, Blosc 1, an `.xz` stream (LZMA's format 1, its
/// default), or a byte shuffle of a given element size.
fn numcodec(id: &str, conf: &Jv) -> Option<Codec> {
    Some(match id {
        "zlib" => codec("zlib", vec![]),
        "gzip" => codec("gzip", vec![]),
        "bz2" => codec("bzip2", vec![]),
        "zstd" => codec("zstd", vec![]),
        "blosc" => codec("blosc", vec![]),
        "lzma" if conf.get("format").is_none_or(|f| f.int(1, 1).is_some()) => codec("xz", vec![]),
        "shuffle" => codec("shuffle", vec![("element_size", Param::Value(json!(conf.get("elementsize")?.int(1, MAX_SAFE)?)))]),
        _ => return None,
    })
}

// ---- Zarr v2

/// A Zarr v2 array's document, read for its description (spec/stored-array.md §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>); None where it
/// declares no shape of nonnegative integers, which is not described.
pub fn zarr2(z: &Jv) -> Option<Layout> {
    let shape = ints(z.get("shape"), 0)?;
    let n = shape.len();
    let descr = z.get("dtype").map(crate::zarr2::descr);
    let item = descr.as_ref().and_then(header::item_size);
    let item_bits = item.and_then(|i| i.checked_mul(8));
    let order = match z.get("order").and_then(Jv::as_str) {
        Some("F") if n >= 2 => Some(Some((0..n as u32).rev().collect())),
        Some("C" | "F") => Some(None),
        _ => None,
    };
    let chunks = ints(z.get("chunks"), 1).filter(|c| c.len() == n);
    let sep = match z.get("dimension_separator") {
        None | Some(Jv::Null) => Some("."),
        Some(v) => v.as_str().filter(|s| *s == "." || *s == "/"),
    };
    let chain = zarr2_chain(z);
    // a grid where the elements' size is known, or where a filter makes them (an object array's)
    let storage = match (&order, chunks, sep, chain) {
        (Some(_), Some(chunks), Some(sep), Some(chain)) if item_bits.is_some() || chain.first().is_some_and(|c| c.name == "source") => {
            let counts = shape.iter().zip(&chunks).map(|(&s, &c)| s.div_ceil(c)).collect();
            let fill = item.and_then(|i| zarr2_fill(z, descr.as_ref()?, i));
            Some(Storage { axes: chunks.iter().map(|&c| AxisUnits::Each(c)).collect(), fill, chain, keys: Keys { v2: true, sep: sep.into(), counts } })
        }
        _ => None,
    };
    Some(Layout { shape, order: order.flatten(), item_bits, storage })
}

/// A Zarr v2 array's chain: its filters' codecs, in order, then its compressor's; None where
/// `filters` is not null or a list of objects with an id, or `compressor` not null or one.
fn zarr2_chain(z: &Jv) -> Option<Chain> {
    let one = |f: &Jv| -> Option<Codec> {
        let id = f.get("id")?.as_str()?;
        let rest = Jv::Obj(f.without(&["id"]));
        Some(numcodec(id, f).unwrap_or_else(|| source("zarr2", id, (!rest.is_empty_object()).then_some(&rest))))
    };
    let mut chain = Vec::new();
    match z.get("filters") {
        None | Some(Jv::Null) => {}
        Some(f) => {
            for x in f.items()? {
                chain.push(one(x)?);
            }
        }
    }
    match z.get("compressor") {
        None | Some(Jv::Null) => {}
        Some(c) => chain.push(one(c)?),
    }
    Some(chain)
}

/// The bytes of one element a Zarr v2 fill value gives (spec/zarrify/zarr2.md
/// §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#3-arrays>), of an element
/// of `descr`, of `item` bytes, as NumPy makes them of the value: a number or boolean of the
/// type, in its byte order; a datetime's or timedelta's integer, or `NaT`; a string of a `U`
/// type, as UTF-32 padded with zeros; base64 of a bytes, void or structured type's bytes, padded
/// with zeros. None for a null fill value, one of an object array, and one this does not read
/// as the type's.
fn zarr2_fill(z: &Jv, descr: &Lit, item: u64) -> Option<Vec<u8>> {
    let v = z.get("fill_value").filter(|v| **v != Jv::Null)?;
    let item = item as usize;
    let t = header::dtype(crate::rules::of("npy"), descr).ok()?;
    let little = t.little.unwrap_or(true);
    let name = t.zarr.as_str().or(t.zarr["name"].as_str()).unwrap_or("").to_string();
    let padded = |b: Vec<u8>| (b.len() <= item).then(|| {
        let mut b = b;
        b.resize(item, 0);
        b
    });
    match name.as_str() {
        _ if t.trailing.is_some() || name == "struct" => padded(crate::zarr2::base64(v.as_str()?)?),
        "bool" => bool_bytes(v),
        "fixed_length_utf32" => {
            let s = match v {
                Jv::Str(s) if s.lone.is_empty() => &s.text,
                _ => return None,
            };
            padded(s.chars().flat_map(|c| ordered(u32::from(c).to_le_bytes().to_vec(), little)).collect())
        }
        "numpy.datetime64" | "numpy.timedelta64" => match v {
            _ if v.as_str() == Some("NaT") => Some(ordered(i64::MIN.to_le_bytes().to_vec(), little)),
            _ => int_bytes(v, true, 8, little),
        },
        n if n.starts_with("int") => int_bytes(v, true, item, little),
        n if n.starts_with("uint") => int_bytes(v, false, item, little),
        n if n.starts_with("float") => float_bytes(v, item, little, false),
        n if n.starts_with("complex") => {
            let (re, im) = match v.items() {
                Some([re, im]) => (re, im),
                Some(_) => return None,
                None => (v, &Jv::Int("0".into())),
            };
            let mut b = float_bytes(re, item / 2, little, false)?;
            b.extend(float_bytes(im, item / 2, little, false)?);
            Some(b)
        }
        _ => None,
    }
}

// ---- Zarr v3

/// A Zarr v3 array's document, read for its description (spec/stored-array.md §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>); None where it
/// declares no shape of nonnegative integers, which is not described.
pub fn zarr3(d: &Jv) -> Option<Layout> {
    let shape = ints(d.get("shape"), 0)?;
    let n = shape.len();
    let dt = data_type(d.get("data_type"));
    let item_bits = dt.as_ref().map(|t| t.bits);
    let codecs: Vec<(String, Option<&Jv>)> = d.get("codecs").and_then(Jv::items).unwrap_or(&[]).iter().filter_map(named).collect();
    let all_named = d.get("codecs").and_then(Jv::items).is_some_and(|c| !c.is_empty() && c.len() == codecs.len());
    // the leading transpose codecs, which give the storage order
    let mut order: Vec<u32> = (0..n as u32).collect();
    let mut k = 0;
    while let Some((name, conf)) = codecs.get(k) {
        let Some(t) = (name == "transpose").then(|| conf.and_then(|c| permutation(c.get("order"), n))).flatten() else { break };
        order = t.iter().map(|&j| order[j as usize]).collect();
        k += 1;
    }
    let mut chain = Vec::new();
    for (name, conf) in &codecs[k..] {
        if name == "bytes" {
            continue; // the elements as they are, in the byte order the document declares
        }
        let numcodecs = name.strip_prefix("numcodecs.").and_then(|id| numcodec(id, conf.unwrap_or(&Jv::Null)));
        chain.push(match name.as_str() {
            "gzip" => codec("gzip", vec![]),
            "zstd" => codec("zstd", vec![]),
            "blosc" => codec("blosc", vec![]),
            _ => numcodecs.unwrap_or_else(|| source("zarr3", name, *conf)),
        });
    }
    let (grid, gconf, _) = named_parts(d.get("chunk_grid"));
    let axes = grid.as_deref().and_then(|g| grid_axes(g, gconf, &shape));
    let keys = chunk_keys(d.get("chunk_key_encoding"));
    let transformers = d.get("storage_transformers").is_none_or(|t| t.items().is_some_and(<[Jv]>::is_empty));
    let storage = match (axes, keys) {
        (Some((axes, counts)), Some((v2, sep))) if all_named && transformers && (item_bits.is_some() || chain.first().is_some_and(|c| c.name == "source")) => {
            // the fill value's bytes, in the order the codec that writes the elements' bytes declares
            let fill = dt.as_ref().and_then(|t| zarr3_fill(d.get("fill_value")?, t, endian(&codecs[k..])));
            Some(Storage { axes, fill, chain, keys: Keys { v2, sep, counts } })
        }
        _ => None,
    };
    let order = (order.iter().enumerate().any(|(i, &a)| a as usize != i)).then_some(order);
    Some(Layout { shape, order, item_bits, storage })
}

/// An extension point's name and configuration: an object `{"name": …, "configuration": …}`,
/// or a string, its name alone; the configuration absent or an object.
fn named_parts(v: Option<&Jv>) -> (Option<String>, Option<&Jv>, bool) {
    match v {
        Some(Jv::Str(s)) => (Some(s.text.clone()), None, true),
        Some(o @ Jv::Obj(_)) => {
            let name = o.get("name").and_then(Jv::as_str).map(str::to_string);
            let conf = o.get("configuration");
            let ok = name.is_some() && conf.is_none_or(Jv::is_obj);
            (name, conf, ok)
        }
        _ => (None, None, false),
    }
}

fn named(v: &Jv) -> Option<(String, Option<&Jv>)> {
    match named_parts(Some(v)) {
        (Some(n), c, true) => Some((n, c)),
        _ => None,
    }
}

/// A permutation of `0..n`.
fn permutation(v: Option<&Jv>, n: usize) -> Option<Vec<u32>> {
    let p: Vec<u32> = v?.items()?.iter().map(|x| x.int(0, n as i64 - 1).map(|i| i as u32)).collect::<Option<_>>()?;
    let mut seen = vec![false; n];
    (p.len() == n && p.iter().all(|&i| !std::mem::replace(&mut seen[i as usize], true))).then_some(p)
}

/// The chunk key encoding: `v2` or `default`, and its separator, `.` or `/` (by default `.` for
/// `v2`, `/` for `default`).
fn chunk_keys(v: Option<&Jv>) -> Option<(bool, String)> {
    let (name, conf, ok) = named_parts(v);
    let v2 = match name.as_deref() {
        Some("v2") => true,
        Some("default") => false,
        _ => return None,
    };
    let sep = match conf.and_then(|c| c.get("separator")) {
        None => if v2 { "." } else { "/" },
        Some(s) => s.as_str().filter(|s| *s == "." || *s == "/")?,
    };
    ok.then(|| (v2, sep.to_string()))
}

/// The units along each axis of a chunk grid `name` of configuration `conf`, under `full`, and
/// the chunks along each axis: a regular grid's chunk shape, each axis `Each`; an inline
/// rectilinear grid's, an axis of one size `Each`, and a list of sizes and runs the chunks it
/// needs to cover the axis, in canonical form. None for another grid, a configuration that is not
/// the grid's, and a rectilinear axis whose chunks pass its end but are not all of one size
/// (whose last chunk is stored whole, which a `List` does not say).
fn grid_axes(name: &str, conf: Option<&Jv>, shape: &[u64]) -> Option<(Vec<AxisUnits>, Vec<u64>)> {
    let conf = conf?;
    let per_axis: &[Jv] = match name {
        "regular" => conf.get("chunk_shape")?.items()?,
        "rectilinear" if conf.get("kind").and_then(Jv::as_str) == Some("inline") => conf.get("chunk_shapes")?.items()?,
        _ => return None,
    };
    if per_axis.len() != shape.len() {
        return None;
    }
    let size = |v: &Jv| v.int(1, MAX_SAFE).map(|x| x as u64);
    let (mut axes, mut counts) = (Vec::new(), Vec::new());
    for (v, &s) in per_axis.iter().zip(shape) {
        if let Some(c) = size(v) {
            axes.push(AxisUnits::Each(c));
            counts.push(s.div_ceil(c));
            continue;
        }
        if name != "rectilinear" {
            return None;
        }
        // each chunk's size, as many as the axis needs, in runs (size, count)
        let mut runs: Vec<(u64, u64)> = Vec::new();
        let mut left = s;
        for x in v.items().filter(|a| !a.is_empty())? {
            let (n, run) = match x.items() {
                Some([a, r]) => (size(a)?, size(r)?),
                Some(_) => return None,
                None => (size(x)?, 1),
            };
            let k = run.min(left.div_ceil(n));
            if k > 0 {
                match runs.last_mut() {
                    Some(last) if last.0 == n => last.1 += k,
                    _ => runs.push((n, k)),
                }
            }
            left = left.saturating_sub(n.saturating_mul(k));
        }
        if left > 0 {
            return None;
        }
        let total: u64 = runs.iter().map(|&(n, k)| n * k).sum();
        counts.push(runs.iter().map(|r| r.1).sum());
        axes.push(match runs[..] {
            [] => AxisUnits::List(Vec::new()),
            [(n, _)] => AxisUnits::Each(n),
            _ if total == s => AxisUnits::List(runs.iter().map(|&(n, k)| if k > 1 { Run::Repeat(n, k) } else { Run::One(n) }).collect()),
            _ => return None,
        });
    }
    Some((axes, counts))
}

/// A Zarr v3 data type the description knows the elements of: its family and the bits of one.
struct DataType {
    family: &'static str,
    bits: u64,
}

/// The data types of Zarr v3's core (spec/zarrify/zarr3.md
/// §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#3-arrays>): `bool`, the
/// integers, the floats, the complex numbers and `r<N>`. None for any other, whose elements the
/// description does not know.
fn data_type(v: Option<&Jv>) -> Option<DataType> {
    let name = v?.as_str()?;
    let (family, bits) = match name {
        "bool" => ("bool", 8),
        "int8" | "int16" | "int32" | "int64" => ("int", name[3..].parse().ok()?),
        "uint8" | "uint16" | "uint32" | "uint64" => ("uint", name[4..].parse().ok()?),
        "float16" | "float32" | "float64" => ("float", name[5..].parse().ok()?),
        "complex64" | "complex128" => ("complex", name[7..].parse().ok()?),
        _ => {
            let digits = name.strip_prefix('r')?;
            let n: u64 = digits.parse().ok().filter(|_| digits.bytes().all(|c| c.is_ascii_digit()) && !digits.starts_with('0'))?;
            if n % 8 != 0 {
                return None;
            }
            ("raw", n)
        }
    };
    Some(DataType { family, bits })
}

/// The byte order of the elements' bytes, as the codecs after the leading transposes declare
/// it: the `bytes` codec's `endian`, or, where the array's first codec is `sharding_indexed`,
/// its inner codecs'; Some(None) where `bytes` declares none, and None where another codec makes
/// the elements' bytes.
fn endian(codecs: &[(String, Option<&Jv>)]) -> Option<Option<bool>> {
    let (name, conf) = codecs.first()?;
    match name.as_str() {
        "bytes" => match conf.and_then(|c| c.get("endian")) {
            None => Some(None),
            Some(e) => match e.as_str()? {
                "little" => Some(Some(true)),
                "big" => Some(Some(false)),
                _ => None,
            },
        },
        "sharding_indexed" => {
            let inner: Vec<(String, Option<&Jv>)> = (*conf)?.get("codecs")?.items()?.iter().filter_map(named).collect();
            let k = inner.iter().take_while(|(n, _)| n == "transpose").count();
            endian(&inner[k..])
        }
        _ => None,
    }
}

/// The bytes of one element a Zarr v3 fill value gives, of a core data type, in byte order
/// `order` (None where the elements' bytes have none the codecs declare): a boolean; an
/// integer; a float, as a number, `NaN`, `Infinity`, `-Infinity`, or `0x` and the hex digits of
/// its bits; a complex number, a pair of them; the bytes of `r<N>`, a list of byte values. None
/// for a value this does not read as the type's, and for one of several bytes whose order the
/// codecs do not declare.
fn zarr3_fill(v: &Jv, t: &DataType, order: Option<Option<bool>>) -> Option<Vec<u8>> {
    let size = (t.bits / 8) as usize;
    let little = match (t.family, order) {
        ("bool" | "raw", _) => true,
        (_, _) if size == 1 => true,
        (_, Some(Some(l))) => l,
        _ => return None,
    };
    match t.family {
        "bool" => bool_bytes(v),
        "int" => int_bytes(v, true, size, little),
        "uint" => int_bytes(v, false, size, little),
        "float" => float_bytes(v, size, little, true),
        "complex" => match v.items()? {
            [re, im] => {
                let mut b = float_bytes(re, size / 2, little, true)?;
                b.extend(float_bytes(im, size / 2, little, true)?);
                Some(b)
            }
            _ => None,
        },
        _ => {
            let b: Vec<u8> = v.items()?.iter().map(|x| x.int(0, 255).map(|i| i as u8)).collect::<Option<_>>()?;
            (b.len() == size).then_some(b)
        }
    }
}

// ---- values

/// Little-endian bytes in the order `little` gives.
fn ordered(mut b: Vec<u8>, little: bool) -> Vec<u8> {
    if !little {
        b.reverse();
    }
    b
}

fn bool_bytes(v: &Jv) -> Option<Vec<u8>> {
    match v {
        Jv::Bool(b) => Some(vec![u8::from(*b)]),
        _ => None,
    }
}

/// An integer of `size` bytes, signed or not, of a literal in its range or a number whose
/// value is an integer of at most 2^53 − 1.
fn int_bytes(v: &Jv, signed: bool, size: usize, little: bool) -> Option<Vec<u8>> {
    let x: i128 = match v {
        Jv::Int(d) => d.parse().ok()?,
        _ => i128::from(v.int(-MAX_SAFE, MAX_SAFE)?),
    };
    let bits = 8 * size as u32;
    let (lo, hi) = if signed { (-(1i128 << (bits - 1)), 1i128 << (bits - 1)) } else { (0, 1i128 << bits) };
    (lo <= x && x < hi).then(|| ordered(x.to_le_bytes()[..size].to_vec(), little))
}

/// A float of `size` bytes (2, 4 or 8): a number, rounded to the nearest of the type, one that
/// rounds past its largest refused; `NaN` (the quiet NaN NumPy writes), `Infinity` or
/// `-Infinity`; and, where `hex`, `0x` and the hex digits of its bits, two per byte.
fn float_bytes(v: &Jv, size: usize, little: bool, hex: bool) -> Option<Vec<u8>> {
    if ![2, 4, 8].contains(&size) {
        return None;
    }
    let bits: u64 = match v {
        Jv::Str(s) => match s.text.as_str() {
            "NaN" => [0x7e00, 0x7fc0_0000, 0x7ff8_0000_0000_0000][size.ilog2() as usize - 1],
            "Infinity" => [0x7c00, 0x7f80_0000, 0x7ff0_0000_0000_0000][size.ilog2() as usize - 1],
            "-Infinity" => [0xfc00, 0xff80_0000, 0xfff0_0000_0000_0000][size.ilog2() as usize - 1],
            t if hex && t.len() == 2 + 2 * size && t.starts_with("0x") => u64::from_str_radix(&t[2..], 16).ok()?,
            _ => return None,
        },
        _ => {
            let x = v.num().filter(|x| x.is_finite())?;
            match size {
                2 => u64::from(f16_bits(x)?),
                4 => {
                    let y = x as f32;
                    if y.is_infinite() {
                        return None;
                    }
                    u64::from(y.to_bits())
                }
                _ => x.to_bits(),
            }
        }
    };
    Some(ordered(bits.to_le_bytes()[..size].to_vec(), little))
}

/// The bits of the IEEE 754 binary16 nearest a finite `x` (ties to even), None where it rounds
/// past the largest (65504).
fn f16_bits(x: f64) -> Option<u16> {
    let sign = if x.is_sign_negative() { 0x8000u16 } else { 0 };
    let a = x.abs();
    if a == 0.0 {
        return Some(sign);
    }
    // a binary16 is m × 2^(e − 24) with m < 2^11; below 2^−14 the exponent is that of subnormals
    let mut e = (a.log2().floor() as i32).max(-14);
    // log2 may round across a power of two
    while e > -14 && a < 2f64.powi(e) {
        e -= 1;
    }
    while a >= 2f64.powi(e + 1) {
        e += 1;
    }
    let scale = 2f64.powi(10 - e);
    let m = round_even(a * scale);
    let (m, e) = if m >= 2048.0 { (round_even(a * scale / 2.0), e + 1) } else { (m, e) };
    if e > 15 {
        return None;
    }
    let m = m as u16;
    Some(if m < 1024 { sign | m } else { sign | ((e + 15) as u16) << 10 | (m - 1024) })
}

fn round_even(x: f64) -> f64 {
    let r = x.round();
    if (x - x.trunc()).abs() == 0.5 && r % 2.0 != 0.0 { r - x.signum() } else { r }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn float_bytes_are_numpys() {
        // 65504 is the largest binary16; 1 + 2^-11 ties to 1, 1 + 3 × 2^-11 to 1 + 2^-9;
        // 2^-24 is the least subnormal
        for (x, want) in [(1.0, 0x3c00u16), (-2.0, 0xc000), (65504.0, 0x7bff), (1.0 + 2f64.powi(-11), 0x3c00),
                          (1.0 + 3.0 * 2f64.powi(-11), 0x3c02), (2f64.powi(-24), 0x0001), (2f64.powi(-14), 0x0400), (0.5, 0x3800)] {
            assert_eq!(f16_bits(x), Some(want), "{x}");
        }
        assert_eq!(f16_bits(65520.0), None);
        assert_eq!(float_bytes(&Jv::Float(1.5), 4, false, false), Some(vec![0x3f, 0xc0, 0, 0]));
        assert_eq!(float_bytes(&Jv::str("0x7fc00001"), 4, true, true), Some(vec![1, 0, 0xc0, 0x7f]));
        assert_eq!(float_bytes(&Jv::str("0x7fc00001"), 4, true, false), None);
        assert_eq!(float_bytes(&Jv::Float(1e39), 4, true, false), None);
    }

    #[test]
    fn int_bytes_keep_every_digit() {
        assert_eq!(int_bytes(&Jv::Int("-9223372036854775808".into()), true, 8, true), Some(i64::MIN.to_le_bytes().to_vec()));
        assert_eq!(int_bytes(&Jv::Int("18446744073709551615".into()), false, 8, false), Some(vec![0xff; 8]));
        assert_eq!(int_bytes(&Jv::Int("256".into()), false, 1, true), None);
        assert_eq!(int_bytes(&Jv::Float(7.0), false, 2, false), Some(vec![0, 7]));
        assert_eq!(int_bytes(&Jv::Float(7.5), false, 2, false), None);
    }

    #[test]
    fn base64_pads() {
        assert_eq!(base64(b""), "");
        assert_eq!(base64(b"f"), "Zg==");
        assert_eq!(base64(b"fo"), "Zm8=");
        assert_eq!(base64(b"foo"), "Zm9v");
        assert_eq!(base64(&[0xff, 0xfe, 0xfd, 0xfc]), "//79/A==");
    }

    #[test]
    fn rectilinear_axes_are_canonical() {
        let conf = |v: &str| crate::json::parse(format!(r#"{{"kind": "inline", "chunk_shapes": {v}}}"#).as_bytes()).unwrap().value;
        let axes = |v: &str, shape: &[u64]| grid_axes("rectilinear", Some(&conf(v)), shape).map(|(a, _)| a);
        assert_eq!(axes("[[2, [3, 2]], 4]", &[8, 6]), Some(vec![AxisUnits::List(vec![Run::One(2), Run::Repeat(3, 2)]), AxisUnits::Each(4)]));
        assert_eq!(axes("[[[1, 100]]]", &[7]), Some(vec![AxisUnits::Each(1)]));
        assert_eq!(axes("[[[3, 3]]]", &[8]), Some(vec![AxisUnits::Each(3)]));
        assert_eq!(axes("[[2, [4, 2]]]", &[8]), None);
        assert_eq!(axes("[[2, 2]]", &[0]), Some(vec![AxisUnits::List(vec![])]));
        assert_eq!(axes("[[2, 1]]", &[5]), None);
    }
}
