//! The safetensors profile's store form (spec/zarrify/safetensors.md §9
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#9-the-store-form>): a
//! store of safetensors files, such as a model's repository of shards, sans-IO. Each
//! `.safetensors` object's header is read and checked as the file form reads it (`check`), and the
//! object is described in place: its length, its header and its tensors' data are its elements,
//! read at its URL. Each `*.safetensors.index.json`, and each `config.json` beside a shard, is
//! read whole as a JSON document and held. A shard whose header fails a check is not presented,
//! with the check's message, and the store is not rejected.
//!
//! Rounds: every shard's first 64 KiB and every document whole, then the rest of each header
//! longer than the first read.

use super::{check, first_read, header_length, Header};
use crate::fed::{Fed, Overlap};
use crate::ir::{Ir, Res};
use crate::json;
use crate::rules::{self, Rules};
use crate::store::{StoreParser, StoreStep};
use serde_json::{json, Map, Value as J};
use std::collections::{BTreeMap, BTreeSet, HashMap};

/// The key of a shard.
pub fn is_shard(key: &str) -> bool {
    key.ends_with(".safetensors")
}

/// The key of a sharded checkpoint's index.
fn is_index(key: &str) -> bool {
    key.ends_with(".safetensors.index.json")
}

/// The directory of a key: its prefix before its last `/` (the root's, empty).
fn dir_of(key: &str) -> &str {
    key.rfind('/').map_or("", |i| &key[..i])
}

/// The path of a shard's group, its key, or why it has none: a key segment that is not a Zarr
/// node name, or a first segment `refkv_source` (spec/zarrify/safetensors.md §9.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#91-the-hierarchy>).
fn shard_path(key: &str) -> Result<String, String> {
    if key.split('/').any(|p| p.starts_with("__") || p == "zarr.json") {
        return Err("name: a segment of its key is not a Zarr node name".into());
    }
    if key.split('/').next() == Some(crate::out::SOURCE_NODE) {
        return Err(format!("reserved: its key starts with {}", crate::out::SOURCE_NODE));
    }
    Ok(key.to_string())
}

/// A shard: its listing index, and once read, its header's length, its checked header, or why it
/// is not presented.
struct Shard {
    k: usize,
    n: Option<u64>,
    header: Option<Header>,
    reason: Option<String>,
}

/// A JSON document the store form reads whole: an index or a configuration.
struct Doc {
    k: usize,
    bytes: Option<Vec<u8>>,
}

#[derive(Clone, Copy, PartialEq, Debug)]
enum Phase {
    Start,
    Rest,
    Done,
}

pub struct SafetensorsStore {
    rules: &'static Rules,
    phase: Phase,
    ir: Option<Ir>,
    keys: Vec<(String, u64)>,
    elems: Vec<u32>,
    fed: HashMap<usize, Fed>,
    shards: Vec<Shard>,
    docs: Vec<Doc>,
}

impl SafetensorsStore {
    pub fn new(ir: Ir, objects: Vec<u32>, keys: &[(String, u64)]) -> Res<SafetensorsStore> {
        let dirs: BTreeSet<&str> = keys.iter().map(|k| k.0.as_str()).filter(|k| is_shard(k)).map(dir_of).collect();
        let shards = (0..keys.len()).filter(|&k| is_shard(&keys[k].0)).map(|k| Shard { k, n: None, header: None, reason: None }).collect();
        let docs = (0..keys.len())
            .filter(|&k| {
                let key = keys[k].0.as_str();
                is_index(key) || (key.rsplit('/').next() == Some("config.json") && dirs.contains(dir_of(key)))
            })
            .map(|k| Doc { k, bytes: None })
            .collect();
        Ok(SafetensorsStore {
            rules: rules::of("safetensors"), phase: Phase::Start, ir: Some(ir), keys: keys.to_vec(), elems: objects,
            fed: HashMap::new(), shards, docs,
        })
    }

    /// The shards' and documents' elements, and the facts (spec/zarrify/safetensors.md §9
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#9-the-store-form>).
    fn emit(&mut self) -> Res<J> {
        let mut ir = self.ir.take().ok_or("internal: no IR")?;
        let keys = &self.keys;
        // a shard whose key is another shard's directory has no group (S3 allows `a` and `a/b`)
        let shard_dirs: BTreeSet<String> = self.shards.iter()
            .flat_map(|s| keys[s.k].0.match_indices('/').map(|(i, _)| keys[s.k].0[..i].to_string()).collect::<Vec<_>>())
            .collect();
        let mut files = Vec::new();
        let mut presented = Vec::new();
        let mut tensors_of: HashMap<String, (String, BTreeSet<String>)> = HashMap::new();
        for s in &mut self.shards {
            let key = &keys[s.k].0;
            if s.reason.is_none() && shard_dirs.contains(key) {
                s.reason = Some("path: its key is another shard's directory".into());
            }
            let path = match (&s.reason, shard_path(key)) {
                (None, Ok(p)) => p,
                (None, Err(why)) => {
                    s.reason = Some(why);
                    String::new()
                }
                _ => String::new(),
            };
            let Some(h) = s.header.as_ref().filter(|_| s.reason.is_none()) else {
                files.push(json!({"key": key, "reason": s.reason.clone().unwrap_or_default()}));
                continue;
            };
            let e = self.elems[s.k];
            ir.describe_in_place(e)?;
            let facts = h.emit(self.rules, &mut ir, e)?;
            let names: BTreeSet<String> = facts["own"]["tensors"].as_array().into_iter().flatten()
                .filter_map(|t| t["name"].as_str().map(str::to_string)).collect();
            tensors_of.insert(key.clone(), (path.clone(), names));
            files.push(json!({"key": key, "path": path}));
            presented.push(json!({"key": key, "path": path, "facts": facts}));
        }
        // the documents: each index and configuration read whole and held, or why not
        let mut indexes = Vec::new();
        let mut configs = Vec::new();
        let mut held = BTreeSet::new();
        for d in &self.docs {
            let (key, size) = (&keys[d.k].0, keys[d.k].1);
            let parsed = match &d.bytes {
                Some(b) => json::parse(b),
                None => Err(format!("a JSON document of {size} bytes is longer than {}", json::MAX_DOCUMENT)),
            };
            let doc = match parsed {
                Ok(doc) => {
                    json::hold(&mut ir, self.elems[d.k], size, &doc.members)?;
                    held.insert(d.k);
                    Some(doc.value)
                }
                Err(e) => {
                    let record = json!({"key": key, "problem": e});
                    if is_index(key) { indexes.push(record) } else { configs.push(record) }
                    continue;
                }
            };
            let value: J = doc.and_then(|v| serde_json::from_str(&v.text()).ok()).unwrap_or(J::Null);
            if is_index(key) {
                indexes.push(index_record(key, &value, &tensors_of));
            } else {
                configs.push(json!({"key": key, "config": value}));
            }
        }
        let presented_keys: BTreeSet<&str> = presented.iter().filter_map(|p| p["key"].as_str()).collect();
        let others: Vec<J> = (0..keys.len())
            .filter(|&k| !held.contains(&k) && !presented_keys.contains(keys[k].0.as_str()) && keys[k].1 > 0)
            .map(|k| json!([keys[k].0, keys[k].1]))
            .collect();
        let dirs: BTreeSet<&str> = presented.iter().filter_map(|p| p["path"].as_str())
            .flat_map(|p| p.match_indices('/').map(move |(i, _)| &p[..i])).collect();
        let mut own = Map::new();
        own.insert("files".into(), J::Array(files));
        if !indexes.is_empty() {
            own.insert("indexes".into(), J::Array(indexes));
        }
        if !configs.is_empty() {
            own.insert("configs".into(), J::Array(configs));
        }
        crate::check::finish(&mut ir)?;
        self.ir = Some(ir);
        Ok(json!({"profile": "safetensors", "own": own, "shards": presented, "dirs": dirs, "others": others}))
    }
}

/// An index's record (spec/zarrify/safetensors.md §9.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#92-source-metadata>): its
/// `metadata`, and how its `weight_map` disagrees with the shards beside it, which the shards'
/// headers decide.
fn index_record(key: &str, value: &J, tensors_of: &HashMap<String, (String, BTreeSet<String>)>) -> J {
    let dir = dir_of(key);
    let mut problems = Vec::new();
    let mut listed: BTreeMap<&str, BTreeSet<&str>> = BTreeMap::new();
    match value["weight_map"].as_object() {
        None => problems.push("it has no weight_map object".to_string()),
        Some(map) => {
            for (tensor, file) in map {
                let Some(file) = file.as_str() else {
                    problems.push(format!("{tensor:?} names no file"));
                    continue;
                };
                let shard = if dir.is_empty() { file.to_string() } else { format!("{dir}/{file}") };
                match tensors_of.get(&shard) {
                    None => problems.push(format!("{tensor:?} is in {file:?}, which is not a presented shard")),
                    Some((_, names)) if !names.contains(tensor) => problems.push(format!("{tensor:?} is not in {file:?}")),
                    Some(_) => {
                        listed.entry(file).or_default().insert(tensor.as_str());
                    }
                }
            }
            for (file, set) in &listed {
                let shard = if dir.is_empty() { file.to_string() } else { format!("{dir}/{file}") };
                if let Some((_, names)) = tensors_of.get(&shard) {
                    let missing = names.iter().filter(|n| !set.contains(n.as_str())).count();
                    if missing > 0 {
                        problems.push(format!("{missing} tensors of {file:?} are not in its weight_map"));
                    }
                }
            }
        }
    }
    let count = problems.len();
    problems.truncate(20);
    let mut r = json!({"key": key, "agrees": count == 0});
    if let Some(m) = value.get("metadata") {
        r["metadata"] = m.clone();
    }
    if count > 0 {
        r["problems"] = json!(problems);
        r["problem_count"] = json!(count);
    }
    r
}

impl StoreParser for SafetensorsStore {
    fn step(&mut self) -> Res<StoreStep> {
        let window = first_read(self.rules);
        let mut asked = Vec::new();
        match self.phase {
            Phase::Start => {
                self.phase = Phase::Rest;
                for s in &self.shards {
                    let size = self.keys[s.k].1;
                    if size > 0 {
                        asked.push((s.k, 0, size.min(window)));
                    }
                }
                for d in &self.docs {
                    let size = self.keys[d.k].1;
                    if size > 0 && size <= json::MAX_DOCUMENT {
                        asked.push((d.k, 0, size));
                    }
                }
            }
            Phase::Rest => {
                self.phase = Phase::Done;
                for s in &mut self.shards {
                    let size = self.keys[s.k].1;
                    let fed = self.fed.get(&s.k);
                    let first = fed.and_then(|f| f.get(0, 8.min(size))).unwrap_or(&[]);
                    match header_length(self.rules, size, first) {
                        Err(e) => s.reason = Some(e),
                        Ok(n) => {
                            s.n = Some(n);
                            let have = fed.map_or(0, |f| f.reach(0));
                            if 8 + n > have {
                                asked.push((s.k, have, 8 + n - have));
                            }
                        }
                    }
                }
            }
            Phase::Done => {
                for s in &mut self.shards {
                    let (Some(n), None) = (s.n, &s.reason) else { continue };
                    let size = self.keys[s.k].1;
                    let raw = self.fed.get(&s.k).and_then(|f| f.get(8, n)).ok_or("internal: a header was not fed")?.to_vec();
                    match check(self.rules, size, n, raw) {
                        Ok(h) => s.header = Some(h),
                        Err(e) => s.reason = Some(e),
                    }
                }
                return Ok(StoreStep::Done);
            }
        }
        if asked.is_empty() {
            return self.step();
        }
        Ok(StoreStep::Read(asked))
    }

    fn feed(&mut self, object: usize, offset: u64, data: &[u8]) {
        if let Some(d) = self.docs.iter_mut().find(|d| d.k == object) {
            d.bytes = Some(data.to_vec());
            return;
        }
        self.fed.entry(object).or_insert_with(|| Fed::new(Overlap::MergeKeep)).feed(offset, data.to_vec());
    }

    fn finish(mut self: Box<Self>) -> Res<(Ir, J)> {
        if self.phase != Phase::Done {
            return Err("internal: the parse is not done".into());
        }
        let facts = self.emit()?;
        Ok((self.ir.take().ok_or("internal: no IR")?, facts))
    }

    fn profile(&self) -> Option<&'static str> {
        Some("safetensors")
    }
}
