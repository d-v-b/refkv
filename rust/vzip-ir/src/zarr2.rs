//! The Zarr v2 store format on the IR (spec/virtualize/zarr2.md
//! [§2](https://github.com/d-v-b/vzip/blob/main/spec/virtualize/zarr2.md#2-nodes)–[§4](https://github.com/d-v-b/vzip/blob/main/spec/virtualize/zarr2.md#4-source-metadata),
//! the store IR of conventions
//! [§8.9](https://github.com/d-v-b/vzip/blob/main/spec/conventions.md#89-stores)), sans-IO.
//!
//! From the listing it finds the nodes (the candidates, classified from the root down), then
//! reads every node's documents (its `.zarray` or `.zgroup`, and its `.zattrs` if listed) in
//! one batch, checks each by the source model schema (`schema/zarr2.json`, in path order) and
//! derives what the projection writes: each array's `zarr.json` (without its attributes), its
//! separator and chunk grid, and each node's source metadata `S`. Each document read is a held
//! object: a value per top-level member (its value's text, `ascii[n]`, named by its key as
//! [`crate::ir::source_names`] names texts), the rest gaps. Every other object (chunks among
//! them) stays referenced. A store this format refuses is rejected with the reason.

use crate::ir::{source_names, Ir, Res, GAP};
use crate::json::{self, Jv};
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

/// The group the mirror and the other objects go in (spec/virtualize/zarr2.md
/// [§5](https://github.com/d-v-b/vzip/blob/main/spec/virtualize/zarr2.md#5-other-objects)).
pub const SOURCE_GROUP: &str = "vzip_source";
const MAX_SAFE: f64 = 9007199254740991.0;

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
}

impl Zarr2 {
    /// The nodes of the listing (spec/virtualize/zarr2.md
    /// [§2](https://github.com/d-v-b/vzip/blob/main/spec/virtualize/zarr2.md#2-nodes)), and the
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
        // the source metadata (the IR's mirror, the other objects) is the group vzip_source
        for p in nodes.keys().chain(implicit.iter()) {
            if p == SOURCE_GROUP || p.starts_with("vzip_source/") {
                return Err(format!("the node {p} is where the store's source metadata goes ({SOURCE_GROUP}/)"));
            }
        }
        let mut docs = Vec::new();
        for (p, &array) in &nodes {
            docs.push(index[&join(p, if array { ".zarray" } else { ".zgroup" })]);
            if let Some(&k) = index.get(&join(p, ".zattrs")) {
                docs.push(k);
            }
        }
        for &k in &docs {
            let (key, n) = &keys[k];
            if *n == 0 {
                return Err(format!("{key}: invalid JSON: an empty document"));
            }
            if *n > json::MAX_DOCUMENT {
                return Err(format!("{key}: JSON document of {n} bytes exceeds {}", json::MAX_DOCUMENT));
            }
        }
        Ok(Zarr2 { ir, objects, keys: keys.to_vec(), index, nodes, implicit, docs, asked: false, got: HashMap::new() })
    }

    fn doc(&self, key: &str) -> Res<Option<json::Doc>> {
        let Some(&k) = self.index.get(key) else { return Ok(None) };
        let b = self.got.get(&k).ok_or_else(|| format!("internal: {key} was not read"))?;
        json::parse(b).map(Some).map_err(|e| format!("{key}: {e}"))
    }
}

/// The given value of a member (absent when absent or null).
fn given(z: &Jv, k: &str) -> V {
    z.get(k).map(|v| v.to_v()).unwrap_or(V::Absent)
}

/// An array's `zarr.json` without its attributes, its separator, its chunk grid and its
/// metadata `M` (spec/virtualize/zarr2.md
/// [§3](https://github.com/d-v-b/vzip/blob/main/spec/virtualize/zarr2.md#3-arrays),
/// [§4](https://github.com/d-v-b/vzip/blob/main/spec/virtualize/zarr2.md#4-source-metadata)).
fn array(path: &str, z: &Jv) -> Res<(Jv, String, Vec<u64>, Jv)> {
    let s = schema();
    let mut env = Vars::default();
    env.insert("path", rules::text(path));
    env.insert("object", rules::flag(matches!(z, Jv::Obj(_))));
    for k in ["zarr_format", "shape", "chunks", "dtype", "order", "filters", "dimension_separator"] {
        env.insert(k, given(z, k));
    }
    s.check("zarray", &mut env)?;
    let dtype = z.get("dtype").and_then(|v| v.as_str()).unwrap_or("");
    let t = &s.table("dtypes")[dtype];
    let (dt, b, byteorder) = (t["data_type"].as_str().unwrap_or(""), t["b"].as_u64().unwrap_or(1), t["order"].as_str().unwrap_or(""));
    let ints = |k: &str| -> Vec<i64> {
        match z.get(k) {
            Some(Jv::Arr(a)) => a.iter().map(|x| x.int(i64::MIN, i64::MAX).unwrap_or(0)).collect(),
            _ => Vec::new(),
        }
    };
    let (shape, chunks) = (ints("shape"), ints("chunks"));
    let n = shape.len();
    let sep = z.get("dimension_separator").and_then(|v| v.as_str()).unwrap_or(".").to_string();
    // the fill value F
    let fv = z.get("fill_value").filter(|v| **v != Jv::Null);
    let family = if dt == "bool" { "bool" } else if dt.starts_with("float") { "float" } else { "int" };
    let mut env = Vars::default();
    env.insert("path", rules::text(path));
    env.insert("fill", fv.map(|v| v.to_v()).unwrap_or(V::Absent));
    env.insert("data_type", rules::text(dt));
    env.insert("b", rules::num(b as f64));
    env.insert("family", rules::text(family));
    s.check("fill", &mut env)?;
    let fill = match fv {
        None if dt == "bool" => Jv::Bool(false),
        None => Jv::Int("0".into()),
        Some(Jv::Bool(x)) => Jv::Bool(*x),
        Some(v @ Jv::Str(_)) => v.clone(),
        Some(v) if family == "int" => Jv::Int(v.int(i64::MIN, i64::MAX).unwrap_or(0).to_string()),
        Some(Jv::Int(d)) => match d.parse::<f64>() {
            Ok(x) if x.abs() > MAX_SAFE => Jv::Float(x),
            _ => Jv::Int(d.clone()),
        },
        Some(Jv::Float(x)) if *x == 0.0 && x.is_sign_negative() => {
            // -0.0, which JSON writers do not all keep: the Zarr v3 hex fill of its bits
            Jv::str(match b { 2 => "0x8000", 4 => "0x80000000", _ => "0x8000000000000000" })
        }
        Some(v) => v.clone(),
    };
    let mut codecs = Vec::new();
    if z.get("order").and_then(|v| v.as_str()) == Some("F") && n >= 2 {
        let order = (0..n).rev().map(|i| Jv::Int(i.to_string())).collect();
        codecs.push(Jv::obj(vec![("name", Jv::str("transpose")), ("configuration", Jv::obj(vec![("order", Jv::Arr(order))]))]));
    }
    codecs.push(if b == 1 {
        Jv::obj(vec![("name", Jv::str("bytes"))])
    } else {
        let e = if byteorder == "<" { "little" } else { "big" };
        Jv::obj(vec![("name", Jv::str("bytes")), ("configuration", Jv::obj(vec![("endian", Jv::str(e))]))])
    });
    let (codec, extra) = compressor(path, z.get("compressor").filter(|v| **v != Jv::Null), b)?;
    codecs.extend(codec);
    // M: the members §3 does not read, as written; null fill values; a float type's integer
    // fill beyond 2^53 − 1; the compressor's members its codec does not carry
    let read: Vec<&str> = s.table("zarray_read").as_array().into_iter().flatten().filter_map(|x| x.as_str()).collect();
    let mut meta = Jv::Obj(z.members().iter().filter(|(k, _)| !read.contains(&k.text.as_str())).cloned().collect());
    match fv {
        None => meta.set("fill_value", Jv::Null),
        Some(v @ Jv::Int(d)) if family == "float" && d.parse::<f64>().is_ok_and(|x| x.abs() > MAX_SAFE) => meta.set("fill_value", v.clone()),
        _ => {}
    }
    if !extra.is_empty_object() {
        meta.set("compressor", extra);
    }
    let grid = shape.iter().zip(&chunks).map(|(&s, &c)| (s as u64).div_ceil(c.max(1) as u64)).collect();
    let as_ints = |v: &[i64]| Jv::Arr(v.iter().map(|x| Jv::Int(x.to_string())).collect());
    let doc = Jv::obj(vec![
        ("zarr_format", Jv::Int("3".into())),
        ("node_type", Jv::str("array")),
        ("shape", as_ints(&shape)),
        ("data_type", Jv::str(dt)),
        ("chunk_grid", Jv::obj(vec![("name", Jv::str("regular")), ("configuration", Jv::obj(vec![("chunk_shape", as_ints(&chunks))]))])),
        ("chunk_key_encoding", Jv::obj(vec![("name", Jv::str("v2")), ("configuration", Jv::obj(vec![("separator", Jv::str(&sep))]))])),
        ("fill_value", fill),
        ("codecs", Jv::Arr(codecs)),
    ]);
    Ok((doc, sep, grid, meta))
}

/// The compressor's codec (spec/virtualize/zarr2.md
/// [§3.1](https://github.com/d-v-b/vzip/blob/main/spec/virtualize/zarr2.md#31-compressors)), and
/// its members the codec does not carry.
fn compressor(path: &str, c: Option<&Jv>, b: u64) -> Res<(Option<Jv>, Jv)> {
    let s = schema();
    let mut env = Vars::default();
    env.insert("path", rules::text(path));
    env.insert("compressor", c.map(|v| v.to_v()).unwrap_or(V::Absent));
    for k in ["level", "checksum", "blocksize"] {
        env.insert(match k { "level" => "has_level", "checksum" => "has_checksum", _ => "has_blocksize" },
                   rules::flag(c.is_some_and(|c| c.get(k).is_some())));
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
        "zlib" | "gzip" => Jv::obj(vec![("name", Jv::str(id)), ("configuration", Jv::obj(vec![("level", int(level()))]))]),
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
                _ if b == 1 => "bitshuffle",
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
        if self.asked {
            return Ok(StoreStep::Done);
        }
        self.asked = true;
        // every node's documents, which the listing names, in one batch
        Ok(StoreStep::Read(self.docs.iter().map(|&k| (k, 0, self.keys[k].1)).collect()))
    }

    fn feed(&mut self, object: usize, _offset: u64, data: &[u8]) {
        self.got.insert(object, data.to_vec());
    }

    fn finish(self: Box<Self>) -> Res<(Ir, J)> {
        let s = schema();
        let mut nodes = Vec::new();
        // each document read (a listing index) and its top-level members' extents
        let mut parsed: Vec<(usize, Members)> = Vec::new();
        for (path, &is_array) in &self.nodes {
            let shown = if path.is_empty() { "/" } else { path.as_str() };
            let key = join(path, if is_array { ".zarray" } else { ".zgroup" });
            let doc = self.doc(&key)?.ok_or("internal: a node without its document")?;
            let akey = join(path, ".zattrs");
            let attrs = self.doc(&akey)?;
            let mut node = json!({"path": path});
            let meta = if is_array {
                let (zarr, sep, grid, meta) = array(shown, &doc.value)?;
                node["kind"] = json!("array");
                node["zarr"] = json!(zarr.text());
                node["sep"] = json!(sep);
                node["grid"] = json!(grid);
                meta
            } else {
                let mut env = Vars::default();
                env.insert("key", rules::text(&key));
                env.insert("object", rules::flag(matches!(doc.value, Jv::Obj(_))));
                env.insert("zarr_format", given(&doc.value, "zarr_format"));
                s.check("zgroup", &mut env)?;
                node["kind"] = json!("group");
                Jv::Obj(doc.value.members().iter().filter(|(k, _)| k.text != "zarr_format").cloned().collect())
            };
            parsed.push((self.index[&key], doc.members));
            let mut own = Jv::obj(vec![]);
            if let Some(a) = attrs {
                let mut env = Vars::default();
                env.insert("key", rules::text(&akey));
                env.insert("object", rules::flag(matches!(a.value, Jv::Obj(_))));
                s.check("zattrs", &mut env)?;
                if !a.value.is_empty_object() {
                    own.set("attributes", a.value);
                }
                parsed.push((self.index[&akey], a.members));
            }
            if !meta.is_empty_object() {
                own.set("metadata", meta);
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
        // each document read is a held object: a value per top-level member, the rest gaps
        for (k, members) in &parsed {
            let o = objects[*k];
            if members.is_empty() {
                ir.add(GAP, o, "gaps/", 0, "", 0, Some((0, keys[*k].1)))?;
                continue;
            }
            let texts: Vec<&str> = members.iter().map(|m| m.0.as_str()).collect();
            for ((name, nidx), (_, a, e)) in source_names(&texts, &[]).into_iter().zip(members) {
                ir.value(o, &name, nidx, &format!("ascii[{}]", e - a), 0, (*a, e - a), None)?;
            }
        }
        crate::check::finish(&mut ir)?;
        let documents: Vec<&str> = parsed.iter().map(|(k, _)| keys[*k].0.as_str()).collect();
        Ok((ir, json!({"nodes": nodes, "documents": documents})))
    }
}
