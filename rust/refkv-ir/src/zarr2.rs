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
use crate::npy::header::Lit;
use crate::rules::Rules;
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
        // an array's document that is not read (empty or too large) is described as such; a
        // group's, or the root's, rejects the store
        let mut docs = Vec::new();
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
            }
        }
        Ok(Zarr2 { ir, objects, keys: keys.to_vec(), index, nodes, implicit, docs, asked: false, got: HashMap::new(),
                   ome: None, tested: HashMap::new() })
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

    /// The description of the document `key`, if the listing has it (spec/zarrify/zarr2.md
    /// §7.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#73-the-description>):
    /// its key and its JSON as parsed, or why it does not parse, or why it was not read; and,
    /// for one read, the extents of its top-level members, which the IR holds (none for one that
    /// is not a JSON object, a whole gap).
    fn document(&self, key: &str) -> Option<(J, Option<(usize, Members)>)> {
        let &k = self.index.get(key)?;
        let Some(b) = self.got.get(&k) else {
            let n = self.keys[k].1;
            let why = if n == 0 { format!("{key}: invalid JSON: an empty document") } else { format!("{key}: JSON document of {n} bytes exceeds {}", json::MAX_DOCUMENT) };
            return Some((json!({"key": key, "unread": why}), None));
        };
        Some(match json::parse(b) {
            Ok(d) => (json!({"key": key, "json": d.value.text()}), Some((k, d.members))),
            Err(e) => (json!({"key": key, "error": format!("{key}: {e}")}), Some((k, Vec::new()))),
        })
    }
}

/// A `.zarray`'s `dtype` as [`header::dtype`] reads a header's: a string, or a list of fields.
pub(crate) fn descr(v: &Jv) -> Lit {
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

    /// The IR and the store's description (spec/zarrify/zarr2.md
    /// §7.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#73-the-description>):
    /// each node's documents as parsed, with none of the output's decisions, which the projection
    /// makes (`project/zarr2_nodes.rs`).
    fn finish(self: Box<Self>) -> Res<(Ir, J)> {
        let ome = self.ome.ok_or("internal: the parser is not done")?;
        let mut nodes = Vec::new();
        // each document read (a listing index) and its top-level members' extents, which the IR
        // holds, in the order of the nodes, each node's own document first
        let mut parsed: Vec<(usize, Members)> = Vec::new();
        for (path, &is_array) in &self.nodes {
            let key = join(path, if is_array { ".zarray" } else { ".zgroup" });
            let mut node = json!({"path": path, "kind": if is_array { "array" } else { "group" }});
            for (member, k) in [("document", key), ("attributes", join(path, ".zattrs"))] {
                if let Some((d, held)) = self.document(&k) {
                    node[member] = d;
                    parsed.extend(held);
                }
            }
            nodes.push(node);
        }
        for p in &self.implicit {
            nodes.push(json!({"path": p, "kind": "implicit"}));
        }
        // the stored arrays: every array node whose `.zarray` reads with a shape, presented or
        // not (spec/stored-array.md §7 <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>)
        let layouts: Vec<(String, usize, crate::zarr_stored::Layout)> = self.nodes.iter().filter(|(_, a)| **a).filter_map(|(path, _)| {
            let key = join(path, ".zarray");
            let k = *self.index.get(&key)?;
            let d = json::parse(self.got.get(&k)?).ok()?;
            Some((path.clone(), k, crate::zarr_stored::zarr2(&d.value)?))
        }).collect();
        let Zarr2 { mut ir, objects, keys, .. } = *self;
        // each document read is a held object: a value per top-level member, the rest gaps (a
        // document of no member is a gap, one of more than MAX_MEMBERS one value, `json`)
        for (k, members) in &parsed {
            json::hold(&mut ir, objects[*k], keys[*k].1, members)?;
        }
        crate::check::finish(&mut ir)?;
        describe(&mut ir, &objects, &keys, &layouts)?;
        let documents: Vec<&str> = parsed.iter().map(|(k, _)| keys[*k].0.as_str()).collect();
        Ok((ir, json!({"profile": if ome { "ome-zarr" } else { "zarr2" }, "nodes": nodes, "documents": documents})))
    }
}

/// Adds the stored arrays of a store whose arrays are `layouts` (each its path, its document's
/// listing index and what the document says), from its finished IR (spec/stored-array.md §6
/// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#6-in-the-ir>): each keyed by
/// its document's object, its one declaration, and its units the objects of its chunk keys that
/// are not empty, each the whole object, by its key.
fn describe(ir: &mut Ir, objects: &[u32], keys: &[(String, u64)], layouts: &[(String, usize, crate::zarr_stored::Layout)]) -> Res<()> {
    let at: HashMap<&str, usize> = layouts.iter().enumerate().map(|(i, (p, _, _))| (p.as_str(), i)).collect();
    let mut units: Vec<Vec<(Vec<u64>, crate::array::Row)>> = vec![Vec::new(); layouts.len()];
    for (key, n) in keys {
        if *n == 0 {
            continue;
        }
        // the array that is the key's nearest proper ancestor (no array is inside another)
        let mut owner = at.get("").map(|&i| (key.as_str(), i));
        for (cut, _) in key.match_indices('/') {
            if let Some(&i) = at.get(&key[..cut]) {
                owner = Some((&key[cut + 1..], i));
            }
        }
        let Some((rest, i)) = owner else { continue };
        if let Some(c) = layouts[i].2.storage.as_ref().and_then(|s| s.keys.coords(rest)) {
            units[i].push((c, crate::array::Row { ranges: Some(vec![(Some(key.clone()), 0, *n)]), ..Default::default() }));
        }
    }
    let mut d = crate::zarr_stored::Described::default();
    for ((_, k, layout), units) in layouts.iter().zip(&units) {
        let e = objects[*k];
        layout.describe(e, vec![crate::array::Place::Element { element: e, member: None, range: None }], units, &mut d)?;
    }
    ir.describe(d.arrays, d.chains, d.map)
}

/// The parser of the profile's store run (`StoreFormat`; crate::store).
pub fn store_parser(ir: crate::ir::Ir, objects: Vec<u32>, keys: &[(String, u64)], _chunking: &crate::out::Chunking) -> crate::ir::Res<Box<dyn crate::store::StoreParser>> {
    Ok(Box::new(Zarr2::new(ir, objects, keys)?))
}
