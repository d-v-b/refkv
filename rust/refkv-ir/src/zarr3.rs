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

use crate::error::Error;

use crate::ir::{Ir, Res, NO_INDEX};
use crate::json::{self, Jv};
use crate::step::Step;
use crate::project::zarr3_nodes::classify;
use crate::rules::{self, Rules};
use crate::store::{StoreParser, StoreStep};
use crate::array::Row;
use crate::zarr_stored::Layout;
use crate::zip::{self, DEFLATE_FORM};
use serde_json::{json, Value as J};
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

pub const PROFILE: &str = "zarr3";
/// The group the mirror and the other objects go in (spec/zarrify/zarr3.md
/// §5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#5-other-objects>).
pub const SOURCE_GROUP: &str = "refkv_source";
/// The rejection of a store, listed or zipped, whose root has no node.
const NOT_ZARR: &str = "not a Zarr v3 store: the root has no zarr.json";

fn schema() -> &'static Rules {
    rules::of(PROFILE)
}

fn join(parent: &str, name: &str) -> String {
    if parent.is_empty() { name.to_string() } else { format!("{parent}/{name}") }
}

/// The proper ancestors of `path` (the root `""` first), nearest last.
pub(crate) fn ancestors(path: &str) -> Vec<&str> {
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
        return Err(Error::rejected(NOT_ZARR));
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

/// Each candidate's document as the description gives it (spec/zarrify/zarr3.md
/// §7.6 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#76-the-description>): its
/// JSON as parsed, or why it does not parse, or why it was not read.
fn candidates_described(cands: &[String], docs: &HashMap<String, crate::error::Res<Vec<u8>>>) -> Res<Vec<J>> {
    cands.iter().map(|p| match docs.get(p) {
        Some(Ok(b)) => Ok(match json::parse(b) {
            Ok(d) => json!({"path": p, "json": d.value.text()}),
            Err(e) => json!({"path": p, "error": format!("{}: {e}", doc_key(p))}),
        }),
        Some(Err(why)) => Ok(json!({"path": p, "unread": why.to_string()})),
        None => Err(Error::internal(format!("{} was not read", doc_key(p)))),
    }).collect()
}

/// The documents the IR holds, by node, with their top-level members' extents: those of the
/// candidates the profile's classification reaches (`project/zarr3_nodes.rs`), so that the
/// mirror follows it; none where the classification rejects the store, which the projection
/// then rejects.
fn held(cands: &[String], docs: &HashMap<String, crate::error::Res<Vec<u8>>>) -> BTreeMap<String, Vec<(String, u64, u64)>> {
    classify(cands, docs).map(|c| c.held).unwrap_or_default()
}

/// The arrays a store's documents declare (spec/stored-array.md §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): each candidate
/// that is not below an array, whose document reads as a JSON object with `node_type` `array` and
/// a shape, whether or not the profile presents it; by path, with what its document says.
fn layouts(cands: &[String], docs: &HashMap<String, crate::error::Res<Vec<u8>>>) -> BTreeMap<String, Layout> {
    let mut out = BTreeMap::new();
    for p in cands {
        if ancestors(p).iter().any(|a| out.contains_key(*a)) {
            continue;
        }
        let Some(Ok(b)) = docs.get(p) else { continue };
        let Ok(d) = json::parse(b) else { continue };
        if d.value.get("node_type").and_then(Jv::as_str) != Some("array") {
            continue;
        }
        if let Some(l) = crate::zarr_stored::zarr3(&d.value) {
            out.insert(p.clone(), l);
        }
    }
    out
}

/// The array of `layouts` a key is below (the nearest), and the chunk indexes the key names, if
/// it is one of its chunk keys.
fn unit_of<'a>(layouts: &'a BTreeMap<String, Layout>, key: &str) -> Option<(&'a str, Vec<u64>)> {
    let mut owner = layouts.get_key_value("").map(|(p, l)| (key, p, l));
    for (at, _) in key.match_indices('/') {
        if let Some((p, l)) = layouts.get_key_value(&key[..at]) {
            owner = Some((&key[at + 1..], p, l));
        }
    }
    let (rest, p, l) = owner?;
    Some((p.as_str(), l.storage.as_ref()?.keys.coords(rest)?))
}

/// Adds the stored arrays `layouts` to a finished IR: each keyed by `element[path]`, its
/// document's element `document[path]` its one declaration, and its units `units` (path,
/// indexes, where its bytes are).
fn describe(ir: &mut Ir, layouts: &BTreeMap<String, Layout>, element: &HashMap<String, u32>, document: &HashMap<String, u32>, units: Vec<(String, Vec<u64>, Row)>) -> Res<()> {
    let mut by: HashMap<String, Vec<(Vec<u64>, Row)>> = HashMap::new();
    for (p, c, e) in units {
        by.entry(p).or_default().push((c, e));
    }
    let mut d = crate::zarr_stored::Described::default();
    for (p, l) in layouts {
        let (Some(&e), Some(&doc)) = (element.get(p), document.get(p)) else {
            return Err(Error::internal(format!("the array {p:?} has no document in the IR")));
        };
        let place = crate::array::Place::Element { element: doc, member: None, range: None };
        l.describe(e, vec![place], by.get(p).map_or(&[][..], Vec::as_slice), &mut d)?;
    }
    ir.describe(d.arrays, d.chains, d.map)
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
            let k = *index.get(key.as_str()).ok_or_else(|| Error::internal("a candidate without its document"))?;
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
        let mut docs: HashMap<String, crate::error::Res<Vec<u8>>> = self.unread.iter().map(|(p, w)| (p.clone(), Err(Error::Rejected(w.clone())))).collect();
        for (p, k) in &self.docs {
            let b = self.got.get(k).cloned().ok_or_else(|| Error::internal(format!("{} was not read", self.keys[*k].0)))?;
            docs.insert(p.clone(), Ok(b));
        }
        let held = held(&self.cands, &docs);
        let documents: Vec<String> = held.keys().map(|p| doc_key(p)).collect();
        let description = json!({"form": "store", "profile": PROFILE, "candidates": candidates_described(&self.cands, &docs)?, "documents": documents});
        let layouts = layouts(&self.cands, &docs);
        let Zarr3Store { mut ir, objects, keys, docs: read, .. } = *self;
        let index: HashMap<&str, usize> = read.iter().map(|(p, k)| (p.as_str(), *k)).collect();
        // each node's document read is a held object: a value per top-level member, the rest
        // gaps (one that is not a JSON object, a gap)
        for (p, members) in &held {
            let k = *index.get(p.as_str()).ok_or_else(|| Error::internal("a document held that was not read"))?;
            json::hold(&mut ir, objects[k], keys[k].1, members)?;
        }
        crate::check::finish(&mut ir)?;
        // the stored arrays: each keyed by its document's object, its units the objects of its
        // chunk keys that are not empty, each the whole object, by its key
        let element: HashMap<String, u32> = layouts.keys().filter_map(|p| Some((p.clone(), objects[*index.get(p.as_str())?]))).collect();
        let units = keys.iter().filter(|(_, n)| *n > 0).filter_map(|(key, n)| {
            let (p, c) = unit_of(&layouts, key)?;
            Some((p.to_string(), c, Row { ranges: Some(vec![(Some(key.clone()), 0, *n)]), ..Row::default() }))
        }).collect();
        describe(&mut ir, &layouts, &element, &element, units)?;
        Ok((ir, description))
    }
}

/// The parser of the profile's store run (`StoreFormat`; crate::store).
pub fn store_parser(ir: Ir, objects: Vec<u32>, keys: &[(String, u64)], _chunking: &crate::out::Chunking) -> Res<Box<dyn StoreParser>> {
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
    /// the ranges of the batch asked last, which `feed` takes
    asking: HashSet<(u64, u64)>,
    got: HashMap<(u64, u64), Vec<u8>>,
    /// the store's objects: key -> entry (the last of its name), in key order
    objects: BTreeMap<String, usize>,
    ignored: Vec<String>,
    superseded: usize,
    cands: Vec<String>,
    docs: HashMap<String, crate::error::Res<Vec<u8>>>,
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
            asked: Vec::new(), asking: HashSet::new(), got: HashMap::new(), objects: BTreeMap::new(), ignored: Vec::new(), superseded: 0,
            cands: Vec::new(), docs: HashMap::new(), reading: Vec::new(),
        }
    }

    fn take(&mut self, at: u64, n: u64, what: &str) -> Res<Vec<u8>> {
        self.got.remove(&(at, n)).ok_or_else(|| Error::internal(format!("no {what}")))
    }

    fn ask_cd(&mut self) -> Res<()> {
        let end = zip::cd_end(&self.end);
        if self.end.cd_offset.checked_add(self.end.cd_size) != Some(end) {
            return Err(Error::rejected("a zip file's central directory does not end where its end records start"));
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
                    return Err(Error::rejected("not a zip file: no end of central directory record"));
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
                let loc = self.end.zip64.ok_or_else(|| Error::internal("no ZIP64 locator"))?.0;
                let b = self.take(loc, 20, "locator")?;
                let rec = zip::zip64_locator(&b, &self.end)?;
                self.raw.insert("locator", b);
                self.end.zip64 = Some((loc, rec));
                self.phase = Phase::Record;
                self.asked.push((rec, 56));
            }
            Phase::Record => {
                let rec = self.end.zip64.ok_or_else(|| Error::internal("no ZIP64 record"))?.1;
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
                        return Err(Error::rejected("a zip entry's local header is beyond the entries"));
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
                    let head = self.got.get(&(lho, 30)).ok_or_else(|| Error::internal("no local header"))?.clone();
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
                        zip::inflate(&b, &self.entries[i]).map_err(|why| why.context(|m| format!("{}: {m}", doc_key(&p))))
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
            let i = *self.objects.get(&key).ok_or_else(|| Error::internal("a candidate without its document"))?;
            let e = &self.entries[i];
            let too_large = (e.method == 8 && e.cs > json::MAX_DOCUMENT).then(|| format!("{key}: JSON document of {} compressed bytes exceeds {}", e.cs, json::MAX_DOCUMENT));
            let why = unread(&key, e.us).or(too_large).or_else(|| {
                (e.method != 0 && e.method != 8).then(|| format!("{key}: the zip entry has the compression method {}", e.method))
            });
            if let Some(why) = why {
                self.docs.insert(p, Err(Error::Rejected(why)));
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
                self.asking = self.asked.iter().copied().collect();
                return Ok(Step::Read(self.asked.clone()));
            }
        }
    }

    pub fn feed(&mut self, offset: u64, data: Vec<u8>) {
        let key = (offset, data.len() as u64);
        if self.asking.contains(&key) {
            self.got.insert(key, data);
        }
    }

    pub fn finish(self) -> Res<(Ir, J)> {
        if self.phase != Phase::Done {
            return Err(Error::internal("the parse is not done"));
        }
        let held = held(&self.cands, &self.docs);
        let (mut objects, mut compressed, mut empty) = (Vec::new(), Vec::new(), 0u64);
        let documents: HashSet<String> = held.keys().map(|p| doc_key(p)).collect();
        for (key, &i) in &self.objects {
            let e = &self.entries[i];
            if e.us == 0 {
                empty += 1;
            } else if e.method != 0 {
                compressed.push(json!([key, e.method]));
            }
            if documents.contains(key) {
                continue;
            }
            if e.method == 0 {
                objects.push(json!([key, e.ds, e.us]));
            }
        }
        let documents: Vec<String> = held.keys().map(|p| doc_key(p)).collect();
        let description = json!({
            "form": "zip", "profile": PROFILE, "candidates": candidates_described(&self.cands, &self.docs)?, "documents": documents,
            "objects": objects, "compressed": compressed, "ignored": self.ignored,
            "counts": {"entries": self.entries.len(), "objects": self.objects.len(), "superseded": self.superseded, "emptyObjects": empty},
        });
        let (mut ir, entries) = self.emit(&held)?;
        // the stored arrays: each keyed by its document's entry, its declaration the entry's
        // data, and its units the data of the entries of its chunk keys that are not empty; an
        // array with a chunk compressed in the zip file has no grid, its units being no bytes of
        // the file as the codecs take them
        let mut layouts = layouts(&self.cands, &self.docs);
        let mut units = Vec::new();
        let mut deflated = HashSet::new();
        for (key, &i) in &self.objects {
            let e = &self.entries[i];
            let Some((p, c)) = (e.us > 0).then(|| unit_of(&layouts, key)).flatten() else { continue };
            match entries.get(key.as_str()) {
                Some(&(_, Some(data))) if e.method == 0 => units.push((p.to_string(), c, Row { element: Some(data), ..Row::default() })),
                _ => {
                    deflated.insert(p.to_string());
                }
            }
        }
        for p in &deflated {
            if let Some(l) = layouts.get_mut(p) {
                l.storage = None;
            }
        }
        units.retain(|(p, _, _)| !deflated.contains(p));
        let element: HashMap<String, u32> = layouts.keys().filter_map(|p| Some((p.clone(), entries.get(doc_key(p).as_str())?.0))).collect();
        let document: HashMap<String, u32> = layouts.keys().filter_map(|p| Some((p.clone(), entries.get(doc_key(p).as_str())?.1?))).collect();
        describe(&mut ir, &layouts, &element, &document, units)?;
        Ok((ir, description))
    }

    /// The file IR: the zip file's records, and each object that is not empty, its local header
    /// and data: a node's document read, as text (stored) or a derived element (deflated); any
    /// other object, its bytes as stored.
    fn emit(&self, held: &BTreeMap<String, Vec<(String, u64, u64)>>) -> Res<(Ir, HashMap<&str, (u32, Option<u32>)>)> {
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
        // each object's entry and its data element, by key
        let mut made = HashMap::new();
        for (i, key) in live {
            let e = &self.entries[i];
            if e.us == 0 {
                continue;
            }
            let s = zip::emit_local(&mut ir, i as u64, e, None)?;
            let held = if key == "zarr.json" { Some("") } else { key.strip_suffix("/zarr.json") }.filter(|p| held.contains_key(*p));
            let doc = held.and_then(|p| self.docs.get(p)).and_then(|r| r.as_ref().ok());
            let data = match doc {
                Some(_) if e.method == 8 => Some(ir.derived(s, "data", 0, (e.ds, e.cs), DEFLATE_FORM)?),
                Some(b) => Some(ir.value(s, "data", NO_INDEX, &format!("ascii[{}]", e.us), 0, (e.ds, e.us), Some(b))?),
                None if e.method == 0 => Some(ir.value(s, "data", NO_INDEX, &format!("bytes[{}]", e.us), 0, (e.ds, e.us), None)?),
                None if e.cs > 0 => Some(ir.value(s, "data", NO_INDEX, &format!("bytes[{}]", e.cs), 0, (e.ds, e.cs), None)?),
                None => None,
            };
            made.insert(key, (s, data));
        }
        ir.sort_values();
        crate::check::finish(&mut ir)?;
        Ok((ir, made))
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
    fn project(&self, ir: &Ir, description: &J, url: &str, _copied: &crate::project::Copied, _chunking: &crate::out::Chunking) -> Res<crate::out::Out> {
        crate::project::zarr3::project(ir, description, url)
    }
    /// The ignored entries' names beside the mirror, as a listed store's ignored keys.
    fn mirrored(&self, out: &mut crate::out::Out, description: &J) -> Res<()> {
        crate::project::store::record_ignored(out, description, PROFILE)
    }
}
