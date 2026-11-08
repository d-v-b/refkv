//! The Zarr v2 store format on the IR (spec/zarrify/zarr2.md §2
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#2-nodes>–§4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#4-source-metadata>,
//! the store IR of conventions
//! §8.9 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#89-stores>), sans-IO.
//!
//! From the listing it finds the nodes (the candidates, classified from the root down), then
//! reads every node's documents (its `.zarray` or `.zgroup`, and its `.zattrs` if listed) in
//! one batch, checks each by the source model schema (`schema/zarr2.json`, in path order) and
//! derives what the projection writes: each array's `zarr.json` (without its attributes), its
//! separator and chunk grid, and each node's source metadata `S`. Each document read is a held
//! object: a value per top-level member (its value's text, `ascii[n]`, named by its key as
//! [`crate::ir::source_names`] names texts), the rest gaps; a document of more than 4096
//! members is one value, `json`. Every other object (chunks among
//! them) stays referenced. A store this format refuses is rejected with the reason. An array it
//! cannot present, below a root group, is left out and recorded instead (§3.4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#34-arrays-that-are-not-presented>).
//!
//! After the batch it decides whether the root declares OME-NGFF 0.4 (spec/zarrify.md §1.4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>), reading the
//! documents that test names and the batch did not (one batch each). A store that does is the
//! OME-Zarr profile's: the OME layer (`ome_zarr.rs`) checks it and gives what the projection
//! writes in place of the Zarr v2 convention's, and the facts' `profile` is `ome-zarr`.

use crate::ir::{Ir, Res};
use crate::json::{self, Jv};
use crate::npy::header::{self, Dtype, Lit};
use crate::rules::{self, Rules, Vars};
use crate::expr::V;
use crate::store::{StoreParser, StoreStep};
use serde_json::{json, Value as J};
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::sync::OnceLock;

pub static ZARR2_SCHEMA: &str = include_str!("../schema/zarr2.json");

pub fn schema() -> &'static Rules {
    static S: OnceLock<Rules> = OnceLock::new();
    S.get_or_init(|| Rules::new(ZARR2_SCHEMA).expect("the Zarr v2 schema is valid"))
}

/// The group the mirror and the other objects go in (spec/zarrify/zarr2.md
/// §5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#5-other-objects>).
pub const SOURCE_GROUP: &str = "refkv_source";
const MAX_SAFE: f64 = 9007199254740991.0;
pub use crate::json::MAX_MEMBERS;

/// A document's top-level members: key, and the extent (start, end) of the value's text.
type Members = Vec<(String, u64, u64)>;

fn join(parent: &str, name: &str) -> String {
    if parent.is_empty() { name.to_string() } else { format!("{parent}/{name}") }
}

/// The proper ancestors of `path` (the root `""` first), nearest last.
fn ancestors(path: &str) -> Vec<&str> {
    if path.is_empty() {
        return Vec::new();
    }
    let mut v = vec![""];
    v.extend(path.match_indices('/').map(|(at, _)| &path[..at]));
    v
}

pub struct Zarr2 {
    ir: Ir,
    objects: Vec<u32>,
    keys: Vec<(String, u64)>,
    index: HashMap<String, usize>,
    /// path -> true for an array, false for an explicit group
    nodes: BTreeMap<String, bool>,
    implicit: BTreeSet<String>,
    /// the documents to read (listing indexes), in the order the profile reads them
    docs: Vec<usize>,
    asked: bool,
    got: HashMap<usize, Vec<u8>>,
    /// whether the root declares OME-NGFF 0.4 (the OME-Zarr profile's store), once decided
    ome: Option<bool>,
    /// the documents the test of OME-NGFF 0.4 read, parsed
    tested: HashMap<usize, Res<Jv>>,
    /// the arrays with a document that is not read, and why (it is empty or too large)
    unread: BTreeMap<String, String>,
}

impl Zarr2 {
    /// The nodes of the listing (spec/zarrify/zarr2.md
    /// §2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#2-nodes>), and the
    /// documents to read; a store with no root node, a candidate with both `.zarray` and
    /// `.zgroup`, a node where the source metadata goes, or a document that cannot be read as
    /// JSON for its size is rejected.
    pub fn new(ir: Ir, objects: Vec<u32>, keys: &[(String, u64)]) -> Res<Zarr2> {
        let index: HashMap<String, usize> = keys.iter().enumerate().map(|(i, (k, _))| (k.clone(), i)).collect();
        let mut candidates: BTreeMap<&str, bool> = BTreeMap::new();
        for (k, _) in keys {
            let (head, name) = match k.rfind('/') {
                Some(at) => (&k[..at], &k[at + 1..]),
                None => ("", k.as_str()),
            };
            let array = match name {
                ".zarray" => true,
                ".zgroup" => false,
                _ => continue,
            };
            if candidates.insert(head, array).is_some_and(|was| was != array) {
                return Err(format!("{} has both .zarray and .zgroup", if head.is_empty() { "/" } else { head }));
            }
        }
        if !candidates.contains_key("") {
            return Err("not a Zarr v2 store: the root has no .zarray or .zgroup".into());
        }
        // classified from the root down: a candidate inside an array is not a node
        let mut order: Vec<&str> = candidates.keys().copied().collect();
        order.sort_by_key(|p| (if p.is_empty() { 0 } else { p.matches('/').count() + 1 }, *p));
        let mut nodes: BTreeMap<String, bool> = BTreeMap::new();
        let mut arrays: HashSet<&str> = HashSet::new();
        for p in order {
            if ancestors(p).iter().any(|a| arrays.contains(a)) {
                continue;
            }
            let array = candidates[p];
            nodes.insert(p.to_string(), array);
            if array {
                arrays.insert(p);
            }
        }
        let mut implicit = BTreeSet::new();
        for p in nodes.keys() {
            for a in ancestors(p) {
                if !nodes.contains_key(a) {
                    implicit.insert(a.to_string());
                }
            }
        }
        // the source metadata (the IR's mirror, the other objects) is the group refkv_source
        for p in nodes.keys().chain(implicit.iter()) {
            if p == SOURCE_GROUP || p.starts_with("refkv_source/") {
                return Err(format!("the node {p} is where the store's source metadata goes ({SOURCE_GROUP}/)"));
            }
        }
        let mut docs = Vec::new();
        let mut unread = BTreeMap::new();
        for (p, &array) in &nodes {
            let own = index[&join(p, if array { ".zarray" } else { ".zgroup" })];
            for k in std::iter::once(own).chain(index.get(&join(p, ".zattrs")).copied()) {
                let (key, n) = &keys[k];
                let problem = if *n == 0 {
                    format!("{key}: invalid JSON: an empty document")
                } else if *n > json::MAX_DOCUMENT {
                    format!("{key}: JSON document of {n} bytes exceeds {}", json::MAX_DOCUMENT)
                } else {
                    docs.push(k);
                    continue;
                };
                if !array || p.is_empty() {
                    return Err(problem);
                }
                unread.entry(p.clone()).or_insert(problem);
            }
        }
        Ok(Zarr2 { ir, objects, keys: keys.to_vec(), index, nodes, implicit, docs, asked: false, got: HashMap::new(),
                   ome: None, tested: HashMap::new(), unread })
    }

    /// Whether the root declares OME-NGFF 0.4 (spec/zarrify.md §1.4
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>), or the
    /// document to read first. A document the test reads and cannot parse rejects the store.
    fn detect(&mut self) -> Res<crate::ome_zarr::Detect> {
        let (keys, index, got, tested) = (&self.keys, &self.index, &self.got, &mut self.tested);
        crate::ome_zarr::declares_04(&|key| index.contains_key(key), &mut |key| {
            let k = index[key];
            if let Some(r) = tested.get(&k) {
                return Some(r.clone());
            }
            let n = keys[k].1;
            let r = if n == 0 {
                Err(format!("{key}: invalid JSON: an empty document"))
            } else if n > json::MAX_DOCUMENT {
                Err(format!("{key}: JSON document of {n} bytes exceeds {}", json::MAX_DOCUMENT))
            } else {
                json::parse(got.get(&k)?).map(|d| d.value).map_err(|e| format!("{key}: {e}"))
            };
            tested.insert(k, r.clone());
            Some(r)
        })
    }

    fn doc(&self, key: &str) -> Res<Option<json::Doc>> {
        let Some(&k) = self.index.get(key) else { return Ok(None) };
        let b = self.got.get(&k).ok_or_else(|| format!("internal: {key} was not read"))?;
        json::parse(b).map(Some).map_err(|e| format!("{key}: {e}"))
    }

    /// An array and its attributes, or the reason it is not presented (spec/zarrify/zarr2.md
    /// §3.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#34-arrays-that-are-not-presented>).
    /// Every document read goes in `parsed`, as what the mirror holds: one that is not JSON, a
    /// whole gap.
    fn presented(&self, path: &str, shown: &str, key: &str, akey: &str, parsed: &mut Vec<(usize, Members)>) -> Res<(Array, Option<Jv>)> {
        let mut read = |key: &str| -> Res<Option<json::Doc>> {
            let Some(&k) = self.index.get(key) else { return Ok(None) };
            if !self.got.contains_key(&k) {
                return Ok(None); // too large or empty to read: `unread`, and an other object or an empty one
            }
            let d = self.doc(key);
            parsed.push((k, d.as_ref().ok().and_then(|d| d.as_ref()).map_or_else(Vec::new, |d| d.members.clone())));
            d
        };
        let (doc, attrs) = (read(key), read(akey));
        if let Some(reason) = self.unread.get(path) {
            return Err(reason.clone());
        }
        let doc = doc?.ok_or("internal: a node without its document")?;
        let attrs = attrs?;
        if let Some(d) = &attrs {
            let mut env = Vars::default();
            env.insert("key", rules::text(akey));
            env.insert("object", rules::flag(matches!(d.value, Jv::Obj(_))));
            schema().check("zattrs", &mut env)?;
        }
        Ok((array(shown, &doc.value)?, attrs.map(|d| d.value)))
    }
}

/// The given value of a member (absent when absent or null).
fn given(z: &Jv, k: &str) -> V {
    z.get(k).map(|v| v.to_v()).unwrap_or(V::Absent)
}

/// What the projection needs of an array's `.zarray`.
struct Array {
    /// its `zarr.json`, without its attributes
    doc: Jv,
    /// its dimension separator
    sep: String,
    /// its chunks along each of the dimensions of its `.zarray`
    grid: Vec<u64>,
    /// its metadata `M`
    meta: Jv,
    /// whether a `|S<n>` or `|V<n>` element's bytes are its last axis
    trailing: bool,
}

/// A `.zarray`'s `dtype` as [`header::dtype`] reads a header's: a string, or a list of fields.
fn descr(v: &Jv) -> Lit {
    fn part(v: &Jv) -> Lit {
        match v {
            Jv::Str(s) => Lit::Str(s.text.clone()),
            Jv::Arr(a) => Lit::Tuple(a.iter().map(part).collect()),
            other => Lit::Str(other.text()),
        }
    }
    match v {
        Jv::Arr(a) => Lit::List(a.iter().map(|f| match f {
            // a field's type, the second of a pair, is a type string or a list of fields
            Jv::Arr(p) => Lit::Tuple(p.iter().enumerate().map(|(i, x)| if i == 1 && matches!(x, Jv::Arr(_)) { descr(x) } else { part(x) }).collect()),
            other => part(other),
        }).collect()),
        other => part(other),
    }
}

/// A `.zarray`'s data type (spec/zarrify/zarr2.md
/// §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#3-arrays>): NPY's table
/// (spec/zarrify/npy.md §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#3-data-types>),
/// and `|O` when its one filter is `vlen-utf8`.
fn data_type(z: &Jv) -> Result<Dtype, String> {
    let Some(d) = z.get("dtype") else { return Err("it has none".into()) };
    if d.as_str() == Some("|O") {
        let vlen = matches!(z.get("filters"), Some(Jv::Arr(f)) if f.len() == 1 && f[0].get("id").and_then(|i| i.as_str()) == Some("vlen-utf8"));
        return if vlen {
            Ok(Dtype { zarr: json!("string"), item: 1, little: None, trailing: None, fill: json!("") })
        } else {
            Err("an object array has no data type without the one filter vlen-utf8".into())
        };
    }
    header::dtype(rules::of("npy"), &descr(d))
}

/// The bytes of a base64 string (RFC 4648 §4, with padding).
pub(crate) fn base64(s: &str) -> Option<Vec<u8>> {
    let digits = s.trim_end_matches('=');
    if s.len() % 4 != 0 || s.len() - digits.len() > 2 {
        return None;
    }
    let mut out = Vec::with_capacity(s.len() / 4 * 3);
    let (mut bits, mut have) = (0u32, 0u32);
    for c in digits.bytes() {
        let v = match c {
            b'A'..=b'Z' => c - b'A',
            b'a'..=b'z' => c - b'a' + 26,
            b'0'..=b'9' => c - b'0' + 52,
            b'+' => 62,
            b'/' => 63,
            _ => return None,
        };
        bits = bits << 6 | u32::from(v);
        have += 6;
        if have >= 8 {
            have -= 8;
            out.push((bits >> have) as u8);
            bits &= (1 << have) - 1;
        }
    }
    Some(out)
}

/// A float fill value as Zarr v3 writes it, given the type's size: a negative zero, which JSON
/// writers do not all keep, as the hex fill of its bits; an integer literal beyond 2^53 − 1 as
/// its binary64 value; any other as it is.
fn float_fill(v: &Jv, b: u64) -> Jv {
    match v {
        Jv::Int(d) => match d.parse::<f64>() {
            Ok(x) if x.abs() > MAX_SAFE => Jv::Float(x),
            _ => v.clone(),
        },
        Jv::Float(x) if *x == 0.0 && x.is_sign_negative() => Jv::str(match b { 2 => "0x8000", 4 => "0x80000000", _ => "0x8000000000000000" }),
        other => other.clone(),
    }
}

/// An integer literal beyond 2^53 − 1, which [`float_fill`] does not write as it is.
fn beyond_safe(v: &Jv) -> bool {
    matches!(v, Jv::Int(d) if d.parse::<f64>().is_ok_and(|x| x.abs() > MAX_SAFE))
}

/// Whether a part of a complex fill value is one a float of `b` bytes takes.
fn float_part(v: &Jv, b: u64) -> bool {
    match v {
        Jv::Str(s) => ["NaN", "Infinity", "-Infinity"].contains(&s.text.as_str()),
        Jv::Int(_) | Jv::Float(_) => b == 8 || v.num().is_some_and(|x| x.abs() <= 3.4028234663852886e38),
        _ => false,
    }
}

/// The fill value `F` of an array of a family the table of limits does not cover, and whether
/// `M` keeps the member as written (spec/zarrify/zarr2.md
/// §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#3-arrays>,
/// §4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#4-source-metadata>); None
/// for a value the family does not take.
fn other_fill(family: &str, t: &Dtype, fv: Option<&Jv>) -> Option<(Jv, bool)> {
    let zero = Jv::from_j(&t.fill);
    let Some(v) = fv else { return Some((zero, false)) };
    let string = match v {
        Jv::Str(s) if s.lone.is_empty() => Some(s.text.as_str()),
        _ => None,
    };
    match family {
        "complex" => {
            let b = t.item / 2;
            match v {
                Jv::Arr(a) if a.len() == 2 && a.iter().all(|x| float_part(x, b)) => {
                    Some((Jv::Arr(a.iter().map(|x| float_fill(x, b)).collect()), a.iter().any(beyond_safe)))
                }
                _ if float_part(v, b) => Some((Jv::Arr(vec![float_fill(v, b), Jv::Int("0".into())]), true)),
                _ => None,
            }
        }
        "time" => match v {
            // NaT is the least int64, past what a JSON number holds exactly
            Jv::Int(d) => d.parse::<i64>().ok().map(|i| (Jv::Int(i.to_string()), false)),
            Jv::Float(_) => v.int(i64::MIN, i64::MAX).map(|i| (Jv::Int(i.to_string()), false)),
            _ if string == Some("NaT") => Some((v.clone(), false)),
            _ => None,
        },
        "text" => string.filter(|x| x.chars().count() as u64 * 4 <= t.item).map(|_| (v.clone(), false)),
        "bytes" => {
            let n = t.trailing.unwrap_or(0) as usize;
            let b = base64(string?)?;
            if b.len() > n {
                return None;
            }
            // shorter than the element, it ends in zeros, as NumPy pads it
            let first = if b.len() == n { *b.first()? } else { 0 };
            b.iter().all(|&x| x == first).then(|| (Jv::Int(first.to_string()), true))
        }
        "struct" => {
            let b = base64(string?)?;
            (b.len() as u64 <= t.item && b.iter().all(|&x| x == 0)).then_some((zero, true))
        }
        "string" => match v {
            _ if string.is_some() => Some((v.clone(), false)),
            // what zarr-python 2 writes for an object array, whose fill it does not use
            _ if v.int(0, 0).is_some() => Some((zero, true)),
            _ => None,
        },
        _ => None,
    }
}

/// An array's filters' codecs (spec/zarrify/zarr2.md
/// §3.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#33-filters>), and the
/// size and byte order of what their last gives the `bytes` codec.
fn filters(path: &str, z: &Jv, t: &Dtype) -> Res<(Vec<Jv>, u64, Option<bool>)> {
    let s = schema();
    let (mut size, mut little) = (if t.trailing.is_some() { 1 } else { t.item }, t.little);
    let given = z.get("filters").filter(|f| **f != Jv::Null);
    let list = given.is_none_or(|f| matches!(f, Jv::Arr(a) if a.iter().all(|x| x.get("id").is_some_and(|i| i.as_str().is_some()))));
    s.check("filters", &mut rules::env([("path", rules::text(path)), ("list", rules::flag(list))]))?;
    let mut before = z.get("dtype").and_then(|d| d.as_str()).unwrap_or("").to_string();
    let fs = match given {
        Some(Jv::Arr(a)) => a.as_slice(),
        _ => &[],
    };
    let mut codecs = Vec::new();
    for f in fs {
        let id = f.get("id").and_then(|i| i.as_str()).unwrap_or("");
        let table = s.table("filters");
        let takes: Vec<&str> = table[id].as_array().into_iter().flatten().filter_map(|x| x.as_str()).collect();
        let members = f.members().iter().all(|(k, _)| takes.contains(&k.text.as_str()));
        let (dtype, astype) = (f.get("dtype").and_then(|d| d.as_str()), f.get("astype").filter(|a| **a != Jv::Null).and_then(|a| a.as_str()));
        // the numeric types a filter takes: integers and floats, and for quantize floats alone
        let number = |text: Option<&str>| -> Option<Dtype> { header::dtype(rules::of("npy"), &Lit::Str(text?.to_string())).ok() };
        let is = |t: &Dtype, kinds: &[&str]| t.zarr.as_str().is_some_and(|n| kinds.iter().any(|k| n.starts_with(k)));
        let result = number(astype.or(dtype));
        let kinds: &[&str] = if id == "quantize" { &["float"] } else { &["int", "uint", "float"] };
        let dtype_kind = id == "vlen-utf8" || number(dtype).is_some_and(|t| is(&t, kinds));
        let astype_ok = id == "vlen-utf8" || (result.as_ref().is_some_and(|t| is(t, kinds)) && (id != "quantize" || astype.is_none() || astype == dtype));
        let finite = |k: &str| f.get(k).and_then(Jv::num).is_some_and(f64::is_finite);
        let numbers = match id {
            "fixedscaleoffset" => finite("scale") && finite("offset"),
            "quantize" => f.get("digits").and_then(|d| d.int(i64::MIN, i64::MAX)).is_some(),
            _ => true,
        };
        s.check("filter", &mut rules::env([
            ("path", rules::text(path)), ("id", rules::text(id)), ("members", rules::flag(members)),
            ("alone", rules::flag(id != "vlen-utf8" || (fs.len() == 1 && before == "|O"))), ("numbers", rules::flag(numbers)),
            ("dtype", rules::text(dtype.unwrap_or(""))), ("before", rules::text(&before)),
            ("dtype_ok", rules::flag(id == "vlen-utf8" || dtype == Some(before.as_str()))), ("dtype_kind", rules::flag(dtype_kind)),
            ("astype", rules::text(astype.unwrap_or(""))), ("astype_ok", rules::flag(astype_ok)),
        ]))?;
        let name = if id == "vlen-utf8" { id.to_string() } else { format!("numcodecs.{id}") };
        codecs.push(if id == "vlen-utf8" {
            Jv::obj(vec![("name", Jv::str(&name))])
        } else {
            Jv::obj(vec![("name", Jv::str(&name)), ("configuration", Jv::Obj(f.without(&["id"])))])
        });
        if let Some(r) = result.filter(|_| id != "vlen-utf8") {
            size = r.item;
            little = r.little;
            before = astype.or(dtype).unwrap_or("").to_string();
        }
    }
    Ok((codecs, size, little))
}

/// An array's `zarr.json` without its attributes, its separator, its chunk grid and its
/// metadata `M` (spec/zarrify/zarr2.md
/// §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#3-arrays>,
/// §4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#4-source-metadata>).
fn array(path: &str, z: &Jv) -> Res<Array> {
    let s = schema();
    let t = data_type(z);
    let mut env = Vars::default();
    env.insert("path", rules::text(path));
    env.insert("object", rules::flag(matches!(z, Jv::Obj(_))));
    for k in ["zarr_format", "shape", "chunks", "dtype", "order", "dimension_separator"] {
        env.insert(k, given(z, k));
    }
    env.insert("dtype_problem", t.as_ref().err().map_or(V::Absent, |e| rules::text(e)));
    s.check("zarray", &mut env)?;
    let t = t?;
    let trailing = t.trailing;
    let ints = |k: &str| -> Vec<i64> {
        match z.get(k) {
            Some(Jv::Arr(a)) => a.iter().map(|x| x.int(i64::MIN, i64::MAX).unwrap_or(0)).collect(),
            _ => Vec::new(),
        }
    };
    let (mut shape, mut chunks) = (ints("shape"), ints("chunks"));
    let n = shape.len();
    let sep = z.get("dimension_separator").and_then(|v| v.as_str()).unwrap_or(".").to_string();
    // the fill value F
    let fv = z.get("fill_value").filter(|v| **v != Jv::Null);
    let name = t.zarr.as_str().or(t.zarr["name"].as_str()).unwrap_or("").to_string();
    let family = match name.as_str() {
        _ if trailing.is_some() => "bytes",
        "bool" => "bool",
        "string" => "string",
        "struct" => "struct",
        "fixed_length_utf32" => "text",
        "numpy.datetime64" | "numpy.timedelta64" => "time",
        n if n.starts_with("float") => "float",
        n if n.starts_with("complex") => "complex",
        _ => "int",
    };
    let other = other_fill(family, &t, fv);
    let mut env = Vars::default();
    env.insert("path", rules::text(path));
    env.insert("fill", fv.map(|v| v.to_v()).unwrap_or(V::Absent));
    // what a message names: a trailing axis's array is a uint8 only to Zarr
    let dtype_text = z.get("dtype").and_then(|v| v.as_str()).unwrap_or("");
    env.insert("data_type", rules::text(if trailing.is_some() { dtype_text } else { &name }));
    env.insert("b", rules::num(t.item as f64));
    env.insert("family", rules::text(family));
    env.insert("ok", rules::flag(other.is_some()));
    s.check("fill", &mut env)?;
    let mut keep_fill = false;
    let fill = match (family, fv) {
        ("bool" | "int" | "float", None) if name == "bool" => Jv::Bool(false),
        ("bool" | "int" | "float", None) => Jv::Int("0".into()),
        ("bool", Some(v)) => v.clone(),
        ("int", Some(v)) => Jv::Int(v.int(i64::MIN, i64::MAX).unwrap_or(0).to_string()),
        ("float", Some(v)) => float_fill(v, t.item),
        _ => {
            let (f, keep) = got_fill(other)?;
            keep_fill = keep;
            f
        }
    };
    let (filter_codecs, size, little) = filters(path, z, &t)?;
    let mut codecs = Vec::new();
    if z.get("order").and_then(|v| v.as_str()) == Some("F") && n >= 2 {
        // an element's bytes stay the last axis
        let order = (0..n).rev().chain(trailing.map(|_| n)).map(|i| Jv::Int(i.to_string())).collect();
        codecs.push(Jv::obj(vec![("name", Jv::str("transpose")), ("configuration", Jv::obj(vec![("order", Jv::Arr(order))]))]));
    }
    let vlen = name == "string";
    codecs.extend(filter_codecs);
    if !vlen {
        codecs.push(match little {
            None => Jv::obj(vec![("name", Jv::str("bytes"))]),
            Some(l) => Jv::obj(vec![("name", Jv::str("bytes")), ("configuration", Jv::obj(vec![("endian", Jv::str(if l { "little" } else { "big" }))]))]),
        });
    }
    // numcodecs' automatic shuffle goes by the element, and blosc's typesize by the bytes it is given
    let (codec, extra) = compressor(path, z.get("compressor").filter(|v| **v != Jv::Null), size, if trailing.is_some() { t.item } else { size })?;
    codecs.extend(codec);
    // M (§4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#4-source-metadata>): the members §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#3-arrays> does not
    // read, as written; null fill values; a float type's integer
    // fill beyond 2^53 − 1; a fill value `F` does not give back; a trailing axis's dtype; the
    // compressor's members its codec does not carry
    let read: Vec<&str> = s.table("zarray_read").as_array().into_iter().flatten().filter_map(|x| x.as_str()).collect();
    let mut meta = Jv::Obj(z.members().iter().filter(|(k, _)| !read.contains(&k.text.as_str())).cloned().collect());
    if trailing.is_some() {
        meta.set("dtype", Jv::str(dtype_text));
    }
    match fv {
        None => meta.set("fill_value", Jv::Null),
        Some(v) if keep_fill || (family == "float" && beyond_safe(v)) => meta.set("fill_value", v.clone()),
        _ => {}
    }
    if !extra.is_empty_object() {
        meta.set("compressor", extra);
    }
    let grid = shape.iter().zip(&chunks).map(|(&s, &c)| (s as u64).div_ceil(c.max(1) as u64)).collect();
    if let Some(e) = trailing {
        shape.push(e as i64);
        chunks.push(e as i64);
    }
    let as_ints = |v: &[i64]| Jv::Arr(v.iter().map(|x| Jv::Int(x.to_string())).collect());
    let doc = Jv::obj(vec![
        ("zarr_format", Jv::Int("3".into())),
        ("node_type", Jv::str("array")),
        ("shape", as_ints(&shape)),
        ("data_type", Jv::from_j(&t.zarr)),
        ("chunk_grid", Jv::obj(vec![("name", Jv::str("regular")), ("configuration", Jv::obj(vec![("chunk_shape", as_ints(&chunks))]))])),
        ("chunk_key_encoding", Jv::obj(vec![("name", Jv::str("v2")), ("configuration", Jv::obj(vec![("separator", Jv::str(&sep))]))])),
        ("fill_value", fill),
        ("codecs", Jv::Arr(codecs)),
    ]);
    Ok(Array { doc, sep, grid, meta, trailing: trailing.is_some() })
}

/// A fill value the `fill` role checked just before: its absence is the schema's and the code's
/// disagreeing, a rejection rather than a panic.
fn got_fill(v: Option<(Jv, bool)>) -> Res<(Jv, bool)> {
    v.ok_or_else(|| "internal: a fill value the schema checked is not there".to_string())
}

/// The compressor's codec (spec/zarrify/zarr2.md
/// §3.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#31-compressors>), and
/// its members the codec does not carry. `b` is the size of what it compresses, `e` the size of
/// the element numcodecs' automatic shuffle went by.
fn compressor(path: &str, c: Option<&Jv>, b: u64, e: u64) -> Res<(Option<Jv>, Jv)> {
    let s = schema();
    let mut env = Vars::default();
    env.insert("path", rules::text(path));
    env.insert("compressor", c.map(|v| v.to_v()).unwrap_or(V::Absent));
    for (k, has) in [("level", "has_level"), ("checksum", "has_checksum"), ("blocksize", "has_blocksize"),
                     ("acceleration", "has_acceleration"), ("format", "has_format"), ("check", "has_check"), ("preset", "has_preset")] {
        env.insert(has, rules::flag(c.is_some_and(|c| c.get(k).is_some())));
    }
    s.check("compressor", &mut env)?;
    let Some(c) = c else { return Ok((None, Jv::obj(vec![]))) };
    let id = c.get("id").and_then(|v| v.as_str()).unwrap_or("");
    let table = &s.table("compressors")[id];
    let mut carried: Vec<&str> = table["carried"].as_array().into_iter().flatten().filter_map(|x| x.as_str()).collect();
    let level = || -> i64 {
        match c.get("level") {
            None => table["default"].as_i64().unwrap_or(0),
            Some(v) => match v.int(i64::MIN, i64::MAX).unwrap_or(0) {
                -1 if id != "zstd" => 6, // zlib's default level, which it stands for
                l => l,
            },
        }
    };
    let int = |l: i64| Jv::Int(l.to_string());
    let codec = match id {
        // zarr-python's name for numcodecs' Zlib, which a Zarr v2 `zlib` compressor is
        "zlib" => Jv::obj(vec![("name", Jv::str("numcodecs.zlib")), ("configuration", Jv::obj(vec![("level", int(level()))]))]),
        "gzip" => Jv::obj(vec![("name", Jv::str("gzip")), ("configuration", Jv::obj(vec![("level", int(level()))]))]),
        "bz2" => Jv::obj(vec![("name", Jv::str("numcodecs.bz2")), ("configuration", Jv::obj(vec![("level", int(level()))]))]),
        "lz4" => {
            let acceleration = c.get("acceleration").cloned().unwrap_or(int(1));
            Jv::obj(vec![("name", Jv::str("numcodecs.lz4")), ("configuration", Jv::obj(vec![("acceleration", acceleration)]))])
        }
        "lzma" => {
            let member = |k: &str, default: Jv| c.get(k).cloned().unwrap_or(default);
            Jv::obj(vec![("name", Jv::str("numcodecs.lzma")), ("configuration", Jv::obj(vec![
                ("format", member("format", int(1))),
                ("check", member("check", int(-1))),
                ("preset", member("preset", Jv::Null)),
                ("filters", member("filters", Jv::Null)),
            ]))])
        }
        "zstd" => {
            let checksum = c.get("checksum").cloned().unwrap_or(Jv::Bool(false));
            Jv::obj(vec![("name", Jv::str("zstd")), ("configuration", Jv::obj(vec![("level", int(level())), ("checksum", checksum)]))])
        }
        _ => {
            let shuffle = c.get("shuffle").and_then(|v| v.int(i64::MIN, i64::MAX)).unwrap_or(0);
            let h = match shuffle {
                0 => "noshuffle",
                1 => "shuffle",
                2 => "bitshuffle",
                _ if e == 1 => "bitshuffle",
                _ => "shuffle",
            };
            if shuffle == -1 {
                carried.retain(|k| *k != "shuffle"); // -1 (automatic) is resolved: keep it
            }
            let blocksize = c.get("blocksize").and_then(|v| v.int(i64::MIN, i64::MAX)).unwrap_or(0);
            Jv::obj(vec![("name", Jv::str("blosc")), ("configuration", Jv::obj(vec![
                ("cname", c.get("cname").cloned().unwrap_or(Jv::Null)),
                ("clevel", int(c.get("clevel").and_then(|v| v.int(0, 9)).unwrap_or(0))),
                ("shuffle", Jv::str(h)),
                ("typesize", int(b as i64)),
                ("blocksize", int(blocksize)),
            ]))])
        }
    };
    let extra = Jv::Obj(c.members().iter().filter(|(k, _)| !carried.contains(&k.text.as_str())).cloned().collect());
    Ok((Some(codec), extra))
}

impl StoreParser for Zarr2 {
    fn step(&mut self) -> Res<StoreStep> {
        if !self.asked {
            self.asked = true;
            // every node's documents, which the listing names, in one batch
            return Ok(StoreStep::Read(self.docs.iter().map(|&k| (k, 0, self.keys[k].1)).collect()));
        }
        if self.ome.is_none() {
            // then whether the root declares OME-NGFF 0.4: the documents its test names that
            // the batch did not read (one inside an array, say), one batch each
            match self.detect()? {
                crate::ome_zarr::Detect::Yes => self.ome = Some(true),
                crate::ome_zarr::Detect::No => self.ome = Some(false),
                crate::ome_zarr::Detect::Need(key) => {
                    let k = self.index[&key];
                    return Ok(StoreStep::Read(vec![(k, 0, self.keys[k].1)]));
                }
            }
        }
        Ok(StoreStep::Done)
    }

    fn profile(&self) -> Option<&'static str> {
        self.ome.map(|o| if o { "ome-zarr" } else { "zarr2" })
    }

    fn feed(&mut self, object: usize, _offset: u64, data: &[u8]) {
        self.got.insert(object, data.to_vec());
    }

    fn finish(self: Box<Self>) -> Res<(Ir, J)> {
        let s = schema();
        let ome = self.ome.ok_or("internal: the parser is not done")?;
        // each node: its facts, its attributes (an explicit group's, for the OME layer) and M
        let mut found: Vec<(J, Option<Jv>, Jv)> = Vec::new();
        let mut arrays: BTreeMap<String, crate::ome_zarr::ArrayInfo> = BTreeMap::new();
        // the arrays left out, and why (§3.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#34-arrays-that-are-not-presented>)
        let mut left_out: BTreeMap<String, String> = BTreeMap::new();
        // the arrays that are not levels of an image (spec/zarrify/ome-zarr.md
        // §2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ome-zarr.md#2-hierarchy>)
        let mut no_level: BTreeMap<String, String> = BTreeMap::new();
        // each document read (a listing index) and its top-level members' extents
        let mut parsed: Vec<(usize, Members)> = Vec::new();
        for (path, &is_array) in &self.nodes {
            let shown = if path.is_empty() { "/" } else { path.as_str() };
            let key = join(path, if is_array { ".zarray" } else { ".zgroup" });
            let akey = join(path, ".zattrs");
            if is_array {
                match self.presented(path, shown, &key, &akey, &mut parsed) {
                    Ok((a, attrs)) => {
                        let dims = a.grid.len();
                        let data_type = a.doc.get("data_type").and_then(|v| v.as_str()).unwrap_or("").to_string();
                        arrays.insert(path.clone(), crate::ome_zarr::ArrayInfo { dims, data_type });
                        if a.trailing {
                            no_level.insert(path.clone(), "it has a trailing axis of bytes".into());
                        }
                        let mut node = json!({"path": path, "kind": "array", "zarr": a.doc.text(), "sep": a.sep, "grid": a.grid});
                        if a.trailing {
                            node["trailing"] = json!(true);
                        }
                        found.push((node, attrs, a.meta));
                    }
                    Err(reason) if path.is_empty() => return Err(reason),
                    Err(reason) => {
                        left_out.insert(path.clone(), reason);
                    }
                }
                continue;
            }
            let doc = self.doc(&key)?.ok_or("internal: a node without its document")?;
            let attrs = self.doc(&akey)?;
            let mut node = json!({"path": path});
            let meta = {
                let mut env = Vars::default();
                env.insert("key", rules::text(&key));
                env.insert("object", rules::flag(matches!(doc.value, Jv::Obj(_))));
                env.insert("zarr_format", given(&doc.value, "zarr_format"));
                s.check("zgroup", &mut env)?;
                node["kind"] = json!("group");
                Jv::Obj(doc.value.members().iter().filter(|(k, _)| k.text != "zarr_format").cloned().collect())
            };
            parsed.push((self.index[&key], doc.members));
            let mut a = None;
            if let Some(d) = attrs {
                let mut env = Vars::default();
                env.insert("key", rules::text(&akey));
                env.insert("object", rules::flag(matches!(d.value, Jv::Obj(_))));
                s.check("zattrs", &mut env)?;
                parsed.push((self.index[&akey], d.members));
                a = Some(d.value);
            }
            found.push((node, a, meta));
        }
        // the OME layer (spec/zarrify/ome-zarr.md
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ome-zarr.md>): its checks, and
        // what it writes in place of the Zarr v2 convention's
        let mut not_ome: BTreeMap<String, String> = BTreeMap::new();
        let layer = if ome {
            let mut groups: BTreeMap<String, Jv> = found.iter().filter(|(n, _, _)| n["kind"] == "group")
                .map(|(n, a, _)| (n["path"].as_str().unwrap_or("").to_string(), a.clone().unwrap_or(Jv::Obj(Vec::new())))).collect();
            no_level.extend(left_out.iter().map(|(p, r)| (p.clone(), format!("it is not presented: {r}"))));
            not_ome = crate::ome_zarr::not_ome(&mut groups, &no_level);
            Some(crate::ome_zarr::layer(&groups, &arrays, &self.keys)?)
        } else {
            None
        };
        let mut nodes = Vec::new();
        for (mut node, a, meta) in found {
            let path = node["path"].as_str().unwrap_or("").to_string();
            let mut own = Jv::obj(vec![]);
            // a group that is not OME keeps its whole `.zattrs`, which the layer read without its
            // OME members
            let g = layer.as_ref().and_then(|l| l.groups.get(&path)).filter(|_| !not_ome.contains_key(&path));
            let attributes = match g {
                Some(g) => Some(g.attributes.clone()),
                None => a,
            };
            if let Some(a) = attributes.filter(|a| !a.is_empty_object()) {
                own.set("attributes", a);
            }
            if !meta.is_empty_object() {
                own.set("metadata", meta);
            }
            if path.is_empty() {
                // only a group has arrays below it, and a root array that is not presented
                // rejects the store
                for (member, record) in [("not_presented", &left_out), ("not_ome", &not_ome)] {
                    if !record.is_empty() {
                        own.set(member, Jv::Arr(record.iter().map(|(p, r)| Jv::obj(vec![("path", Jv::str(p)), ("reason", Jv::str(r))])).collect()));
                    }
                }
            }
            if let Some(g) = g {
                if !g.unversioned.is_empty() {
                    own.set("unversioned", Jv::Arr(g.unversioned.iter().map(|u| Jv::str(u)).collect()));
                }
                if let Some(o) = &g.ome {
                    node["ome"] = json!(o.text());
                }
            }
            if let Some(d) = layer.as_ref().and_then(|l| l.dims.get(&path)) {
                node["dims"] = json!(d.text());
            }
            if !own.is_empty_object() {
                node["s"] = json!(own.text());
            }
            nodes.push(node);
        }
        for p in &self.implicit {
            nodes.push(json!({"path": p, "kind": "implicit"}));
        }
        let Zarr2 { mut ir, objects, keys, .. } = *self;
        // each document read is a held object: a value per top-level member, the rest gaps (a
        // document of no member is a gap, one of more than MAX_MEMBERS one value, `json`)
        for (k, members) in &parsed {
            json::hold(&mut ir, objects[*k], keys[*k].1, members)?;
        }
        crate::check::finish(&mut ir)?;
        let documents: Vec<&str> = parsed.iter().map(|(k, _)| keys[*k].0.as_str()).collect();
        let mut facts = json!({"nodes": nodes, "documents": documents, "profile": if ome { "ome-zarr" } else { "zarr2" }});
        if !left_out.is_empty() {
            facts["notPresented"] = json!(left_out.len());
        }
        if !not_ome.is_empty() {
            facts["notOme"] = json!(not_ome.len());
        }
        if let Some(l) = layer {
            facts["xml"] = json!(l.xml);
            facts["ome"] = l.summary;
        }
        Ok((ir, facts))
    }
}

/// The parser of the profile's store run (`StoreFormat`; crate::store).
pub fn store_parser(ir: crate::ir::Ir, objects: Vec<u32>, keys: &[(String, u64)]) -> crate::ir::Res<Box<dyn crate::store::StoreParser>> {
    Ok(Box::new(Zarr2::new(ir, objects, keys)?))
}
