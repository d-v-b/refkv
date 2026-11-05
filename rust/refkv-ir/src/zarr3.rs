//! The Zarr v3 store format on the IR (spec/zarrify/zarr3.md §2
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#2-nodes>–§4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#4-source-metadata>, the
//! profile's §7 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#7-zarr-v3-profile>),
//! sans-IO, in both forms:
//!
//! - **the store form** ([`Zarr3Store`], a [`StoreParser`]): a store IR whose objects are the
//!   listing's. Each node's `zarr.json` read is a held object, a value per top-level member as
//!   Zarr v2's documents are; every other object stays referenced.
//! - **the zip form** ([`Zarr3Zip`], a [`crate::Parser`]): a file IR of a zip file whose entries
//!   are the store's objects (spec/zarrify/zarr3.md §7.4
//!   <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#74-the-zip-form>): its end
//!   records, central directory and the local header and data of each object that is not empty.
//!
//! Both find the candidates (the directories with a `zarr.json`), read every candidate's
//! `zarr.json` in one batch, and classify them from the root down ([`classify`]): a node's
//! document is checked by the source model schema (`schema/zarr3.json`), a node below an array
//! or a node left out is not a node, and a node that fails below the root is left out and
//! recorded. What the projection writes of a node is its own `zarr.json`, its attributes in place
//! but those the convention moves into the source metadata `S`, and `consolidated_metadata`
//! dropped. Its codecs, data type, fill value and chunk grid pass through unchanged; the
//! projection needs only each array's chunk keys.

use crate::ir::{Ir, Res, NO_INDEX};
use crate::json::{self, Jv};
use crate::nd2::Step;
use crate::project::zarr3::is_chunk;
use crate::rules::{self, Rules, Vars};
use crate::store::{StoreParser, StoreStep};
use crate::zip::{self, DEFLATE_FORM};
use serde_json::{json, Value as J};
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

pub const PROFILE: &str = "zarr3";
/// The group the mirror and the other objects go in (spec/zarrify/zarr3.md
/// §5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#5-other-objects>).
pub const SOURCE_GROUP: &str = "refkv_source";
const MAX_SAFE: i64 = 9007199254740991;
const MAX_DIMS: usize = 32;
/// The rejection of a store, listed or zipped, whose root has no node.
const NOT_ZARR: &str = "not a Zarr v3 store: the root has no zarr.json";

fn schema() -> &'static Rules {
    rules::of(PROFILE)
}

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

fn depth(path: &str) -> usize {
    if path.is_empty() { 0 } else { path.matches('/').count() + 1 }
}

/// The key of a node's document.
pub fn doc_key(path: &str) -> String {
    join(path, "zarr.json")
}

/// The candidates of a store's keys (spec/zarrify/zarr3.md
/// §2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#2-nodes>): the directories
/// with an object `zarr.json`, the root among them, or the store is rejected.
fn candidates<'a>(keys: impl Iterator<Item = &'a str>) -> Res<Vec<String>> {
    let mut c = BTreeSet::new();
    for k in keys {
        match k.rsplit_once('/') {
            Some((head, "zarr.json")) => {
                c.insert(head.to_string());
            }
            None if k == "zarr.json" => {
                c.insert(String::new());
            }
            _ => {}
        }
    }
    if !c.contains("") {
        return Err(NOT_ZARR.into());
    }
    let mut v: Vec<String> = c.into_iter().collect();
    v.sort_by(|a, b| (depth(a), a.as_str()).cmp(&(depth(b), b.as_str())));
    Ok(v)
}

/// Why a candidate's `zarr.json` of `n` bytes is not read, if it is not (spec/zarrify.md §1.6
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#16-json-documents>).
fn unread(key: &str, n: u64) -> Option<String> {
    if n == 0 {
        Some(format!("{key}: invalid JSON: an empty document"))
    } else if n > json::MAX_DOCUMENT {
        Some(format!("{key}: JSON document of {n} bytes exceeds {}", json::MAX_DOCUMENT))
    } else {
        None
    }
}

/// An array's chunk keys: its encoding (`v2`, else `default`), separator and the chunks along
/// each axis (spec/zarrify/zarr3.md
/// §3.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#31-chunk-grids-and-chunk-keys>).
#[derive(Clone, Debug)]
pub struct Grid {
    pub v2: bool,
    pub sep: String,
    pub counts: Vec<u64>,
}

impl Grid {
    fn json(&self) -> J {
        json!({"v2": self.v2, "sep": self.sep, "counts": self.counts})
    }
}

/// A positive integer of at most 2^53 − 1.
fn size(v: &Jv) -> Option<u64> {
    v.int(1, MAX_SAFE).map(|x| x as u64)
}

/// The chunks along each axis of an array of `shape` whose chunk grid is `grid` (`name` its
/// name, `conf` its configuration), or None when the configuration is not that grid's for
/// `shape.len()` dimensions.
fn counts(name: &str, conf: Option<&Jv>, shape: &[u64]) -> Option<Vec<u64>> {
    let conf = conf?;
    let per_axis: &[Jv] = match name {
        "regular" => conf.get("chunk_shape")?.items()?,
        "rectilinear" if conf.get("kind").and_then(Jv::as_str) == Some("inline") => conf.get("chunk_shapes")?.items()?,
        _ => return None,
    };
    if per_axis.len() != shape.len() {
        return None;
    }
    let mut out = Vec::with_capacity(shape.len());
    for (v, &s) in per_axis.iter().zip(shape) {
        if let Some(c) = size(v) {
            out.push(s.div_ceil(c));
            continue;
        }
        // rectilinear: a nonempty list of sizes, each an integer or a run [size, count]
        if name != "rectilinear" {
            return None;
        }
        let items = v.items().filter(|a| !a.is_empty())?;
        let mut n = 0u64;
        for x in items {
            let k = match x.items() {
                Some([s, c]) => size(s).and(size(c))?,
                Some(_) => return None,
                None => size(x).map(|_| 1)?,
            };
            n = n.checked_add(k).filter(|&n| n <= MAX_SAFE as u64)?;
        }
        out.push(n);
    }
    Some(out)
}

/// An extension point's name and configuration: an object `{"name": …, "configuration": …}`,
/// or a string, its name alone. `ok` when it is one of these, with a configuration that is
/// absent or an object.
fn named(v: Option<&Jv>) -> (Option<String>, Option<&Jv>, bool) {
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

/// What the projection needs of an array that is presented.
#[derive(Clone, Debug)]
pub struct ArrayInfo {
    pub grid: Grid,
    pub sharded: bool,
}

/// Checks a node's document (spec/zarrify/zarr3.md
/// §2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#2-nodes>,
/// §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#3-arrays>): an array's
/// chunk keys, or None for a group.
fn check(shown: &str, key: &str, d: &Jv) -> Res<Option<ArrayInfo>> {
    let s = schema();
    let mut env = Vars::default();
    env.insert("key", rules::text(key));
    env.insert("object", rules::flag(d.is_obj()));
    env.insert("zarr_format", d.get("zarr_format").map(Jv::to_v).unwrap_or(crate::expr::V::Absent));
    env.insert("node_type", d.get("node_type").map(Jv::to_v).unwrap_or(crate::expr::V::Absent));
    env.insert("attributes_ok", rules::flag(d.get("attributes").is_none_or(Jv::is_obj)));
    s.check("node", &mut env)?;
    if d.get("node_type").and_then(Jv::as_str) != Some("array") {
        return Ok(None);
    }
    let shape: Option<Vec<u64>> = d.get("shape").and_then(Jv::items).filter(|a| a.len() <= MAX_DIMS)
        .and_then(|a| a.iter().map(|x| x.int(0, MAX_SAFE).map(|v| v as u64)).collect());
    let n = shape.as_ref().map_or(0, Vec::len);
    let (grid, conf, _) = named(d.get("chunk_grid"));
    let grid = grid.unwrap_or_default();
    let counts = shape.as_deref().and_then(|sh| counts(&grid, conf, sh));
    let (encoding, econf, encoding_ok) = named(d.get("chunk_key_encoding"));
    let encoding = encoding.unwrap_or_default();
    let separator = econf.and_then(|c| c.get("separator"));
    let codecs = d.get("codecs").and_then(Jv::items).unwrap_or(&[]);
    let mut env = Vars::default();
    env.insert("path", rules::text(shown));
    env.insert("shape", d.get("shape").map(Jv::to_v).unwrap_or(crate::expr::V::Absent));
    env.insert("shape_ok", rules::flag(shape.is_some()));
    env.insert("n", rules::num(n as f64));
    env.insert("grid", rules::text(&grid));
    env.insert("grid_ok", rules::flag(counts.is_some()));
    env.insert("encoding", rules::text(&encoding));
    env.insert("encoding_ok", rules::flag(encoding_ok));
    env.insert("separator", separator.map(Jv::to_v).unwrap_or(crate::expr::V::Absent));
    env.insert("transformers", rules::flag(d.get("storage_transformers").is_none_or(|t| t.items().is_some_and(<[Jv]>::is_empty))));
    env.insert("data_type", rules::flag(d.has("data_type")));
    env.insert("fill_value", rules::flag(d.has("fill_value")));
    env.insert("codecs", rules::flag(!codecs.is_empty()));
    s.check("array", &mut env)?;
    let v2 = encoding == "v2";
    let sep = separator.and_then(Jv::as_str).unwrap_or(if v2 { "." } else { "/" }).to_string();
    let counts = counts.ok_or("internal: a chunk grid the schema checked has no chunks")?;
    let sharded = codecs.iter().any(|c| named(Some(c)).0.as_deref() == Some("sharding_indexed"));
    Ok(Some(ArrayInfo { grid: Grid { v2, sep, counts }, sharded }))
}

/// The convention's CMO's UUID.
fn uuid() -> String {
    crate::out::convention(PROFILE)["uuid"].as_str().unwrap_or("").to_string()
}

/// A node's `zarr.json` as the hierarchy writes it before the declaration, and the attributes
/// moved into its source metadata (spec/zarrify/zarr3.md
/// §4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#4-source-metadata>): its
/// members in order but `consolidated_metadata`, with `attributes` the attributes kept in
/// place (appended when it has none). `refkv_zarrified` moves, and so does `zarr_conventions`
/// unless it is a nonempty array of which no element is an object whose `uuid` is the
/// convention's.
fn rewrite(d: &Jv) -> (Jv, Jv) {
    let id = uuid();
    let follows = |v: &Jv| v.items().is_some_and(|a| !a.is_empty() && !a.iter().any(|c| c.get("uuid").and_then(Jv::as_str) == Some(id.as_str())));
    let (mut kept, mut moved) = (Vec::new(), Vec::new());
    for (k, v) in d.get("attributes").map(Jv::members).unwrap_or(&[]) {
        let plain = k.lone.is_empty();
        if plain && (k.text == crate::out::CONVENTION_KEY || (k.text == "zarr_conventions" && !follows(v))) {
            moved.push((k.clone(), v.clone()));
        } else {
            kept.push((k.clone(), v.clone()));
        }
    }
    let mut doc = Jv::Obj(d.without(&["consolidated_metadata"]));
    doc.set("attributes", Jv::Obj(kept));
    (doc, Jv::Obj(moved))
}

/// A store's nodes, classified: what the projection writes of each, and what the mirror holds.
struct Classified {
    /// path -> (its `zarr.json` before the declaration, its moved attributes, its array)
    presented: BTreeMap<String, (Jv, Jv, Option<ArrayInfo>)>,
    left_out: BTreeMap<String, String>,
    implicit: BTreeSet<String>,
    /// the nodes whose document was read: path -> its top-level members' extents, empty when it
    /// is not a JSON object
    held: BTreeMap<String, Vec<(String, u64, u64)>>,
}

/// Classifies the candidates `cands` (from the root down), each with its document's bytes or why
/// they were not read (spec/zarrify/zarr3.md
/// §2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#2-nodes>,
/// §3.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#33-nodes-that-are-not-presented>).
fn classify(cands: &[String], docs: &HashMap<String, Result<Vec<u8>, String>>) -> Res<Classified> {
    let mut c = Classified { presented: BTreeMap::new(), left_out: BTreeMap::new(), implicit: BTreeSet::new(), held: BTreeMap::new() };
    // the arrays and the nodes left out: a candidate below one is not a node
    let mut closed: HashSet<&str> = HashSet::new();
    for p in cands {
        if ancestors(p).iter().any(|a| closed.contains(a)) {
            continue;
        }
        let shown = if p.is_empty() { "/" } else { p.as_str() };
        let key = doc_key(p);
        let got = docs.get(p).cloned().unwrap_or_else(|| Err(format!("internal: {key} was not read")));
        let checked = got.and_then(|b| {
            let d = json::parse(&b).map_err(|e| format!("{key}: {e}"));
            c.held.insert(p.clone(), d.as_ref().map(|d| d.members.clone()).unwrap_or_default());
            let d = d?;
            Ok((check(shown, &key, &d.value)?, d.value))
        });
        match checked {
            Ok((array, d)) => {
                if array.is_some() {
                    closed.insert(p);
                }
                let (doc, moved) = rewrite(&d);
                c.presented.insert(p.clone(), (doc, moved, array));
            }
            Err(reason) if p.is_empty() => return Err(reason),
            Err(reason) => {
                closed.insert(p);
                c.left_out.insert(p.clone(), reason);
            }
        }
    }
    for p in c.presented.keys().chain(c.left_out.keys()) {
        for a in ancestors(p) {
            if !c.presented.contains_key(a) {
                c.implicit.insert(a.to_string());
            }
        }
    }
    // the source metadata (the IR's mirror, the other objects) is the group refkv_source
    let all: BTreeSet<&String> = c.presented.keys().chain(c.left_out.keys()).chain(c.implicit.iter()).collect();
    for p in all {
        if p == SOURCE_GROUP || p.starts_with("refkv_source/") {
            return Err(format!("the node {p} is where the store's source metadata goes ({SOURCE_GROUP}/)"));
        }
    }
    Ok(c)
}

impl Classified {
    /// The presented array a key is below (the nearest), and the rest of the key after it.
    fn owner<'k>(&self, key: &'k str) -> Option<(&'k str, &str, &ArrayInfo)> {
        let mut found = None;
        if let Some((_, _, Some(a))) = self.presented.get("") {
            found = Some((key, "", a));
        }
        for (at, _) in key.match_indices('/') {
            if let Some((p, (_, _, Some(a)))) = self.presented.get_key_value(&key[..at]) {
                found = Some((&key[at + 1..], p.as_str(), a));
            }
        }
        found
    }

    /// Whether a key is a chunk key of a presented array (spec/zarrify/zarr3.md
    /// §3.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#31-chunk-grids-and-chunk-keys>),
    /// and of which.
    fn chunk_of(&self, key: &str) -> Option<String> {
        let (rest, path, a) = self.owner(key)?;
        is_chunk(rest, a.grid.v2, &a.grid.sep, &a.grid.counts).then(|| path.to_string())
    }

    /// The facts the projection reads: each node, and the documents held.
    fn facts(&self, form: &str) -> J {
        let mut nodes = Vec::new();
        for (path, (doc, moved, array)) in &self.presented {
            let mut own = Jv::obj(vec![]);
            if !moved.is_empty_object() {
                own.set("attributes", moved.clone());
            }
            if path.is_empty() && !self.left_out.is_empty() {
                // only a group has nodes below it, and a root that fails rejects the store
                own.set("not_presented", Jv::Arr(self.left_out.iter().map(|(p, r)| Jv::obj(vec![("path", Jv::str(p)), ("reason", Jv::str(r))])).collect()));
            }
            let mut node = json!({"path": path, "kind": if array.is_some() { "array" } else { "group" }, "doc": doc.text()});
            if let Some(a) = array {
                node["grid"] = a.grid.json();
                node["sharded"] = json!(a.sharded);
            }
            if !own.is_empty_object() {
                node["s"] = json!(own.text());
            }
            nodes.push(node);
        }
        for p in &self.implicit {
            nodes.push(json!({"path": p, "kind": "implicit"}));
        }
        let documents: Vec<String> = self.held.keys().map(|p| doc_key(p)).collect();
        let mut facts = json!({"form": form, "profile": PROFILE, "nodes": nodes, "documents": documents});
        if !self.left_out.is_empty() {
            facts["notPresented"] = json!(self.left_out.len());
        }
        facts
    }
}

// ---- the store form

/// A listed Zarr v3 store (spec/zarrify/zarr3.md
/// §7.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#72-reading>).
pub struct Zarr3Store {
    ir: Ir,
    objects: Vec<u32>,
    keys: Vec<(String, u64)>,
    cands: Vec<String>,
    /// the candidates' documents to read (listing indexes), and those not read, and why
    docs: Vec<(String, usize)>,
    unread: HashMap<String, String>,
    asked: bool,
    got: HashMap<usize, Vec<u8>>,
}

impl Zarr3Store {
    pub fn new(ir: Ir, objects: Vec<u32>, keys: &[(String, u64)]) -> Res<Zarr3Store> {
        let cands = candidates(keys.iter().map(|(k, _)| k.as_str()))?;
        let index: HashMap<&str, usize> = keys.iter().enumerate().map(|(i, (k, _))| (k.as_str(), i)).collect();
        let (mut docs, mut unread_) = (Vec::new(), HashMap::new());
        for p in &cands {
            let key = doc_key(p);
            let k = *index.get(key.as_str()).ok_or("internal: a candidate without its document")?;
            match unread(&key, keys[k].1) {
                Some(why) => {
                    unread_.insert(p.clone(), why);
                }
                None => docs.push((p.clone(), k)),
            }
        }
        Ok(Zarr3Store { ir, objects, keys: keys.to_vec(), cands, docs, unread: unread_, asked: false, got: HashMap::new() })
    }
}

impl StoreParser for Zarr3Store {
    fn step(&mut self) -> Res<StoreStep> {
        if !self.asked && !self.docs.is_empty() {
            self.asked = true;
            // every candidate's document, which the listing names, in one batch
            return Ok(StoreStep::Read(self.docs.iter().map(|&(_, k)| (k, 0, self.keys[k].1)).collect()));
        }
        Ok(StoreStep::Done)
    }

    fn profile(&self) -> Option<&'static str> {
        Some(PROFILE)
    }

    fn feed(&mut self, object: usize, _offset: u64, data: &[u8]) {
        self.got.insert(object, data.to_vec());
    }

    fn finish(self: Box<Self>) -> Res<(Ir, J)> {
        let mut docs: HashMap<String, Result<Vec<u8>, String>> = self.unread.iter().map(|(p, w)| (p.clone(), Err(w.clone()))).collect();
        for (p, k) in &self.docs {
            let b = self.got.get(k).cloned().ok_or_else(|| format!("internal: {} was not read", self.keys[*k].0))?;
            docs.insert(p.clone(), Ok(b));
        }
        let c = classify(&self.cands, &docs)?;
        let facts = c.facts("store");
        let Zarr3Store { mut ir, objects, keys, docs: read, .. } = *self;
        let index: HashMap<&str, usize> = read.iter().map(|(p, k)| (p.as_str(), *k)).collect();
        // each node's document read is a held object: a value per top-level member, the rest
        // gaps (one that is not a JSON object, a gap)
        for (p, members) in &c.held {
            let k = *index.get(p.as_str()).ok_or("internal: a document held that was not read")?;
            json::hold(&mut ir, objects[k], keys[k].1, members)?;
        }
        crate::check::finish(&mut ir)?;
        Ok((ir, facts))
    }
}

/// The parser of the profile's store run (`StoreFormat`; crate::store).
pub fn store_parser(ir: Ir, objects: Vec<u32>, keys: &[(String, u64)]) -> Res<Box<dyn StoreParser>> {
    Ok(Box::new(Zarr3Store::new(ir, objects, keys)?))
}

// ---- the zip form

#[derive(PartialEq, Eq, Clone, Copy, Debug)]
enum Phase {
    Tail,
    Locator,
    Record,
    Cd,
    Local,
    Docs,
    Done,
}

/// A zipped Zarr v3 store (spec/zarrify/zarr3.md
/// §7.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#74-the-zip-form>).
pub struct Zarr3Zip {
    size: u64,
    phase: Phase,
    begun: bool,
    end: zip::End,
    entries: Vec<zip::Entry>,
    raw: HashMap<&'static str, Vec<u8>>,
    asked: Vec<(u64, u64)>,
    got: HashMap<(u64, u64), Vec<u8>>,
    /// the store's objects: key -> entry (the last of its name), in key order
    objects: BTreeMap<String, usize>,
    ignored: Vec<String>,
    superseded: usize,
    cands: Vec<String>,
    docs: HashMap<String, Result<Vec<u8>, String>>,
    /// the entries of the candidates' documents read, and whether each is deflated
    reading: Vec<(String, usize, bool)>,
}

/// Whether a relative key is one spec/zarrify.md §1.4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs> ignores.
fn ignored_key(k: &str) -> bool {
    k.is_empty() || k.ends_with('/') || k.split('/').any(|s| s.is_empty() || s == "." || s == "..")
}

impl Zarr3Zip {
    pub fn new(size: u64) -> Zarr3Zip {
        Zarr3Zip {
            size, phase: Phase::Tail, begun: false, end: zip::End::default(), entries: Vec::new(), raw: HashMap::new(),
            asked: Vec::new(), got: HashMap::new(), objects: BTreeMap::new(), ignored: Vec::new(), superseded: 0,
            cands: Vec::new(), docs: HashMap::new(), reading: Vec::new(),
        }
    }

    fn take(&mut self, at: u64, n: u64, what: &str) -> Res<Vec<u8>> {
        self.got.remove(&(at, n)).ok_or_else(|| format!("internal: no {what}"))
    }

    fn ask_cd(&mut self) -> Res<()> {
        let end = zip::cd_end(&self.end);
        if self.end.cd_offset.checked_add(self.end.cd_size) != Some(end) {
            return Err("a zip file's central directory does not end where its end records start".into());
        }
        self.phase = Phase::Cd;
        if self.end.cd_size > 0 {
            self.asked.push((self.end.cd_offset, self.end.cd_size));
        }
        Ok(())
    }

    /// The store's objects from the central directory: each entry's name is a key, the last
    /// entry of a name its object; ignored keys recorded as a listing's are.
    fn names(&mut self) {
        for (i, e) in self.entries.iter().enumerate() {
            if ignored_key(&e.name) {
                // a directory entry of no bytes is a folder marker, which is not recorded
                if !(e.us == 0 && (e.name.is_empty() || e.name.ends_with('/'))) {
                    self.ignored.push(e.name.clone());
                }
                continue;
            }
            if self.objects.insert(e.name.clone(), i).is_some() {
                self.superseded += 1;
            }
        }
        self.ignored.sort_by(|a, b| a.as_bytes().cmp(b.as_bytes()));
    }

    fn advance(&mut self) -> Res<()> {
        let n = self.size;
        let s = schema();
        match self.phase {
            Phase::Tail if !self.begun => {
                self.begun = true;
                if n == 0 {
                    return Err("not a zip file: no end of central directory record".into());
                }
                let t = n.min(zip::MAX_TAIL);
                self.asked.push((n - t, t));
            }
            Phase::Tail => {
                let t = n.min(zip::MAX_TAIL);
                let tail = self.take(n - t, t, "tail")?;
                self.end = zip::eocd(s, &tail, n)?;
                self.raw.insert("tail", tail);
                if let Some((loc, _)) = self.end.zip64 {
                    self.phase = Phase::Locator;
                    self.asked.push((loc, 20));
                } else {
                    self.ask_cd()?;
                }
            }
            Phase::Locator => {
                let loc = self.end.zip64.ok_or("internal: no ZIP64 locator")?.0;
                let b = self.take(loc, 20, "locator")?;
                let rec = zip::zip64_locator(&b, &self.end)?;
                self.raw.insert("locator", b);
                self.end.zip64 = Some((loc, rec));
                self.phase = Phase::Record;
                self.asked.push((rec, 56));
            }
            Phase::Record => {
                let rec = self.end.zip64.ok_or("internal: no ZIP64 record")?.1;
                let b = self.take(rec, 56, "ZIP64 record")?;
                zip::zip64_record(s, &b, &mut self.end, rec)?;
                self.raw.insert("record", b);
                self.ask_cd()?;
            }
            Phase::Cd => {
                let (o, len) = (self.end.cd_offset, self.end.cd_size);
                let cd = if len == 0 { Vec::new() } else { self.take(o, len, "central directory")? };
                self.entries = zip::central_directory(s, &cd, &self.end)?;
                self.raw.insert("cd", cd);
                self.names();
                self.cands = candidates(self.objects.keys().map(String::as_str))?;
                for &i in self.objects.values() {
                    let e = &self.entries[i];
                    if e.us == 0 {
                        continue;
                    }
                    if e.lho.checked_add(30).is_none_or(|x| x > o) {
                        return Err("a zip entry's local header is beyond the entries".into());
                    }
                    self.asked.push((e.lho, 30));
                }
                self.phase = Phase::Local;
                if self.asked.is_empty() {
                    self.docs_step()?;
                }
            }
            Phase::Local => {
                let mut read = Vec::new();
                let objects: Vec<(String, usize)> = self.objects.iter().map(|(k, &i)| (k.clone(), i)).collect();
                for (key, i) in objects {
                    if self.entries[i].us == 0 {
                        continue;
                    }
                    let lho = self.entries[i].lho;
                    let head = self.got.get(&(lho, 30)).ok_or("internal: no local header")?.clone();
                    zip::local_header(&head, &mut self.entries[i], self.end.cd_offset)?;
                    read.push(i);
                    let e = &self.entries[i];
                    if e.method == 0 {
                        let short: String = key.chars().take(200).collect();
                        s.check("zip_stored", &mut rules::env([("key", rules::text(&short)), ("same_size", rules::flag(e.cs == e.us))]))?;
                    }
                }
                self.got.clear();
                zip::disjoint(&self.entries, &read)?;
                self.docs_step()?;
            }
            Phase::Docs => {
                let reading = std::mem::take(&mut self.reading);
                for (p, i, deflated) in reading {
                    let (ds, cs, us) = (self.entries[i].ds, self.entries[i].cs, self.entries[i].us);
                    let r = if deflated {
                        let b = self.take(ds, cs, "document")?;
                        zip::inflate(&b, &self.entries[i]).map_err(|why| format!("{}: {why}", doc_key(&p)))
                    } else {
                        Ok(self.take(ds, us, "document")?)
                    };
                    self.docs.insert(p, r);
                }
                self.phase = Phase::Done;
            }
            Phase::Done => {}
        }
        Ok(())
    }

    /// Asks for every candidate's document: a stored one's bytes, a deflated one's stream to
    /// inflate; one that is empty, too large or otherwise compressed is not read.
    fn docs_step(&mut self) -> Res<()> {
        let s = schema();
        let mut total = 0u64;
        for p in self.cands.clone() {
            let key = doc_key(&p);
            let i = *self.objects.get(&key).ok_or("internal: a candidate without its document")?;
            let e = &self.entries[i];
            let too_large = (e.method == 8 && e.cs > json::MAX_DOCUMENT).then(|| format!("{key}: JSON document of {} compressed bytes exceeds {}", e.cs, json::MAX_DOCUMENT));
            let why = unread(&key, e.us).or(too_large).or_else(|| {
                (e.method != 0 && e.method != 8).then(|| format!("{key}: the zip entry has the compression method {}", e.method))
            });
            if let Some(why) = why {
                self.docs.insert(p, Err(why));
                continue;
            }
            let deflated = e.method == 8;
            if deflated {
                total = total.saturating_add(e.us);
                self.asked.push((e.ds, e.cs));
            } else {
                self.asked.push((e.ds, e.us));
            }
            self.reading.push((p, i, deflated));
        }
        s.check("zip_inflated", &mut rules::env([("total", rules::num(total as f64))]))?;
        self.phase = if self.reading.is_empty() { Phase::Done } else { Phase::Docs };
        Ok(())
    }

    pub fn step(&mut self) -> Res<Step> {
        // the batch asked last was fed: its bytes are in `got`
        self.asked.clear();
        loop {
            if self.phase == Phase::Done {
                return Ok(Step::Done);
            }
            self.advance()?;
            if !self.asked.is_empty() {
                self.asked.sort_unstable();
                self.asked.dedup();
                return Ok(Step::Read(self.asked.clone()));
            }
        }
    }

    pub fn feed(&mut self, offset: u64, data: Vec<u8>) {
        let key = (offset, data.len() as u64);
        if self.asked.contains(&key) {
            self.got.insert(key, data);
        }
    }

    pub fn finish(self) -> Res<(Ir, J)> {
        if self.phase != Phase::Done {
            return Err("internal: the parse is not done".into());
        }
        let mut c = classify(&self.cands, &self.docs)?;
        // an array with a chunk that is not stored is left out: its chunks cannot be referenced
        let mut compressed: BTreeMap<String, String> = BTreeMap::new();
        for (key, &i) in &self.objects {
            let e = &self.entries[i];
            if e.method == 0 || e.us == 0 {
                continue;
            }
            if let Some(path) = c.chunk_of(key) {
                compressed.entry(path.clone()).or_insert_with(|| {
                    let shown = if path.is_empty() { "/" } else { path.as_str() };
                    format!("{shown}: chunk {key} is compressed in the zip file (method {})", e.method)
                });
            }
        }
        for (path, reason) in compressed {
            if path.is_empty() {
                return Err(reason);
            }
            c.presented.remove(&path);
            c.left_out.insert(path, reason);
        }
        let mut facts = c.facts("zip");
        let (mut objects, mut empty) = (Vec::new(), 0u64);
        let documents: HashSet<String> = c.held.keys().map(|p| doc_key(p)).collect();
        for (key, &i) in &self.objects {
            let e = &self.entries[i];
            if e.us == 0 {
                empty += 1;
            }
            if documents.contains(key) {
                continue;
            }
            if e.method == 0 {
                objects.push(json!([key, e.ds, e.us]));
            }
        }
        facts["objects"] = json!(objects);
        facts["ignored"] = json!(self.ignored);
        facts["counts"] = json!({"entries": self.entries.len(), "objects": self.objects.len(), "superseded": self.superseded, "emptyObjects": empty});
        let ir = self.emit(&c)?;
        Ok((ir, facts))
    }

    /// The file IR: the zip file's records, and each object that is not empty, its local header
    /// and data: a node's document read, as text (stored) or a derived element (deflated); any
    /// other object, its bytes as stored.
    fn emit(&self, c: &Classified) -> Res<Ir> {
        let mut ir = Ir::new(self.size);
        let raw = zip::Records {
            tail: self.raw.get("tail").map_or(&[][..], Vec::as_slice),
            cd: self.raw.get("cd").map_or(&[][..], Vec::as_slice),
            record: self.raw.get("record").map(Vec::as_slice),
            locator: self.raw.get("locator").map(Vec::as_slice),
        };
        zip::emit_records(&mut ir, self.size, &self.end, &self.entries, &raw)?;
        let mut live: Vec<(usize, &str)> = self.objects.iter().map(|(k, &i)| (i, k.as_str())).collect();
        live.sort_unstable();
        for (i, key) in live {
            let e = &self.entries[i];
            if e.us == 0 {
                continue;
            }
            let s = zip::emit_local(&mut ir, i as u64, e, None)?;
            let held = if key == "zarr.json" { Some("") } else { key.strip_suffix("/zarr.json") }.filter(|p| c.held.contains_key(*p));
            let doc = held.and_then(|p| self.docs.get(p)).and_then(|r| r.as_ref().ok());
            match doc {
                Some(_) if e.method == 8 => {
                    ir.derived(s, "data", 0, (e.ds, e.cs), DEFLATE_FORM)?;
                }
                Some(b) => {
                    ir.value(s, "data", NO_INDEX, &format!("ascii[{}]", e.us), 0, (e.ds, e.us), Some(b))?;
                }
                None if e.method == 0 => {
                    ir.value(s, "data", NO_INDEX, &format!("bytes[{}]", e.us), 0, (e.ds, e.us), None)?;
                }
                None if e.cs > 0 => {
                    ir.value(s, "data", NO_INDEX, &format!("bytes[{}]", e.cs), 0, (e.ds, e.cs), None)?;
                }
                None => {}
            }
        }
        ir.sort_values();
        crate::check::finish(&mut ir)?;
        Ok(ir)
    }
}

crate::parser!(Zarr3Zip);

/// Whether a file's first bytes are the zip form's signature (spec/zarrify/zarr3.md
/// §7.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#71-choosing-the-profile>):
/// a ZIP local file header whose name, as far as they hold it, does not start with a segment
/// ending in `.SAFE` (a SAFE product's zip form).
pub fn is_zipped(head: &[u8]) -> bool {
    if head.len() < 30 || &head[..4] != zip::LOCAL {
        return false;
    }
    let n = crate::fed::uint(head, 26, 2, true) as usize;
    let name = &head[30..head.len().min(30 + n)];
    match name.iter().position(|&b| b == b'/') {
        Some(at) => !name[..at].ends_with(b".SAFE"),
        None => true,
    }
}

/// The Zarr v3 profile's file run, of a store's zip form.
pub struct Profile;

impl crate::run::FileProfile for Profile {
    fn detect(&self, head: &[u8]) -> bool {
        is_zipped(head)
    }
    fn parser(&self, size: u64) -> crate::run::AnyParser {
        crate::run::AnyParser::of::<Zarr3Zip>(size)
    }
    /// It takes exactly the ranges it asked for.
    fn whole(&self) -> bool {
        false
    }
    fn project(&self, ir: &Ir, facts: &J, url: &str, _copied: &crate::project::Copied) -> Res<crate::out::Out> {
        crate::project::zarr3::project(ir, facts, url)
    }
    /// The ignored entries' names beside the mirror, as a listed store's ignored keys.
    fn mirrored(&self, out: &mut crate::out::Out, facts: &J) -> Res<()> {
        crate::project::store::record_ignored(out, facts, PROFILE)
    }
}
