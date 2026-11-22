//! The Zarr v3 convention's decisions (spec/zarrify/zarr3.md §2
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#2-nodes>–§4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#4-source-metadata>), made from
//! the parser's description of the store (§7.6
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#76-the-description>): its
//! candidates classified from the root down ([`classify`]), a node's document checked by the source
//! model schema (`schema/zarr3.json`), a node below an array or a node left out not a node, and a
//! node that fails below the root left out and recorded; a zipped array with a chunk the zip file
//! compresses left out; and what the projection writes of each node, its own `zarr.json` with its
//! attributes in place but those the convention moves into the source metadata `S`, and
//! `consolidated_metadata` dropped. [`hierarchy`] gives them as the projection (`zarr3.rs`) writes
//! them.
//!
//! The parser runs [`classify`] too, for one thing: the documents the IR holds are those of the
//! candidates the classification reaches, so the mirror follows it.

use crate::ir::Res;
use crate::json::{self, Jv};
use crate::project::zarr3::is_chunk;
use crate::rules::{self, Rules, Vars};
use crate::zarr3::{ancestors, doc_key, PROFILE, SOURCE_GROUP};
use serde_json::{json, Value as J};
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

const MAX_SAFE: i64 = 9007199254740991;
const MAX_DIMS: usize = 32;

fn schema() -> &'static Rules {
    rules::of(PROFILE)
}

/// The planned the hierarchy is written from (`project/zarr3.rs`), of the store's description:
/// each node presented, with what it writes of it and its chunk keys, and the nodes left out and
/// why. A root that fails rejects the store.
pub fn hierarchy(description: &J) -> Res<J> {
    let form = description["form"].as_str().unwrap_or("store");
    let mut cands = Vec::new();
    let mut docs: HashMap<String, Result<Vec<u8>, String>> = HashMap::new();
    for c in description["candidates"].as_array().into_iter().flatten() {
        let path = c["path"].as_str().unwrap_or("").to_string();
        let doc = match (c["json"].as_str(), c["unread"].as_str().or(c["error"].as_str())) {
            (Some(text), _) => Ok(text.as_bytes().to_vec()),
            (None, Some(why)) => Err(why.to_string()),
            (None, None) => Err(format!("internal: the candidate {path:?} has no document in the description")),
        };
        docs.insert(path.clone(), doc);
        cands.push(path);
    }
    let mut c = classify(&cands, &docs)?;
    if form == "zip" {
        // an array with a chunk that is not stored is left out: its chunks cannot be referenced
        let mut compressed: BTreeMap<String, String> = BTreeMap::new();
        for e in description["compressed"].as_array().into_iter().flatten() {
            let (key, method) = (e[0].as_str().unwrap_or(""), e[1].as_u64().unwrap_or(0));
            if let Some(path) = c.chunk_of(key) {
                compressed.entry(path.clone()).or_insert_with(|| {
                    let shown = if path.is_empty() { "/" } else { path.as_str() };
                    format!("{shown}: chunk {key} is compressed in the zip file (method {method})")
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
    }
    let mut planned = c.planned(form);
    // the documents the IR holds, as the parser held them: a document that is not JSON among them
    planned["documents"] = description["documents"].clone();
    for k in ["objects", "ignored", "counts"] {
        if !description[k].is_null() {
            planned[k] = description[k].clone();
        }
    }
    Ok(planned)
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
/// `shape.len()` dimensions. A rectilinear axis's chunks are those its list needs to cover the
/// axis, in order (none for an axis of 0), and a list that does not cover it is not the grid's.
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
        let (mut n, mut left) = (0u64, s);
        for x in items {
            let (size, run) = match x.items() {
                Some([a, r]) => (size(a)?, size(r)?),
                Some(_) => return None,
                None => (size(x)?, 1),
            };
            // the chunks of this run the axis still needs
            let k = run.min(left.div_ceil(size));
            n += k;
            left = left.saturating_sub(size.saturating_mul(k));
        }
        if left > 0 {
            return None;
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
    // a separator written as null is not one of the two, nor absent
    env.insert("separator", separator.map(|v| if *v == Jv::Null { rules::text("null") } else { v.to_v() }).unwrap_or(crate::expr::V::Absent));
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
/// convention's or whose `name` is `refkv_zarrified` (another refkv convention's CMO, whose
/// member moves).
fn rewrite(d: &Jv) -> (Jv, Jv) {
    let id = uuid();
    let refkv = |c: &Jv| c.get("uuid").and_then(Jv::as_str) == Some(id.as_str()) || c.get("name").and_then(Jv::as_str) == Some(crate::out::CONVENTION_KEY);
    let follows = |v: &Jv| v.items().is_some_and(|a| !a.is_empty() && !a.iter().any(refkv));
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
pub struct Classified {
    /// path -> (its `zarr.json` before the declaration, its moved attributes, its array)
    pub presented: BTreeMap<String, (Jv, Jv, Option<ArrayInfo>)>,
    pub left_out: BTreeMap<String, String>,
    pub implicit: BTreeSet<String>,
    /// the nodes whose document was read: path -> its top-level members' extents, empty when it
    /// is not a JSON object
    pub held: BTreeMap<String, Vec<(String, u64, u64)>>,
}

/// Classifies the candidates `cands` (from the root down), each with its document's bytes or why
/// they were not read (spec/zarrify/zarr3.md
/// §2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#2-nodes>,
/// §3.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#33-nodes-that-are-not-presented>).
pub fn classify(cands: &[String], docs: &HashMap<String, Result<Vec<u8>, String>>) -> Res<Classified> {
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

    /// The planned the projection reads: each node, and the documents held.
    fn planned(&self, form: &str) -> J {
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
        let mut planned = json!({"form": form, "profile": PROFILE, "nodes": nodes, "documents": documents});
        if !self.left_out.is_empty() {
            planned["notPresented"] = json!(self.left_out.len());
        }
        planned
    }
}

