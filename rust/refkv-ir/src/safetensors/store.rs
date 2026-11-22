//! The safetensors profile's store form (spec/zarrify/safetensors.md §9
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#9-the-store-form>): a
//! store of safetensors files, such as a model's repository of shards, sans-IO. Each
//! `.safetensors` object's header is read and checked as the file form reads it (`check`), and the
//! object is described in place: its length, its header and its tensors' data are its elements,
//! read at its URL, whether or not the hierarchy presents it. Beside the IR, the parser returns a
//! description of the store (spec/zarrify/safetensors.md §9.7
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#97-the-description>), from
//! which the projection makes refkv's plan (`plan.rs`). Each `*.safetensors.index.json`, and each `config.json` beside a shard, is
//! read whole as a JSON document and held. A shard whose header fails a check is described by the
//! check's message, and the store is not rejected.
//!
//! Rounds: every shard's first 64 KiB and every document whole, then the rest of each header
//! longer than the first read.

use super::{check, first_read, header_length, stored, Header};
use crate::fed::{Fed, Overlap};
use crate::ir::{Ir, Res};
use crate::json;
use crate::rules::{self, Rules};
use crate::store::{StoreParser, StoreStep};
use serde_json::{json, Value as J};
use std::collections::{BTreeSet, HashMap};

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

    /// The shards' and documents' elements, the description of the store (spec/zarrify/safetensors.md
    /// §9.7 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#97-the-description>),
    /// and, on the finished IR, the descriptions of every tensor whose shard's header passed its
    /// checks (spec/stored-array.md §7
    /// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>). Every such
    /// shard is described in place, whether or not the hierarchy presents it, and its tensors are
    /// keyed by its `header`; a shard whose header fails a check has the check's message.
    fn emit(&mut self) -> Res<J> {
        let mut ir = self.ir.take().ok_or("internal: no IR")?;
        let keys = &self.keys;
        let mut reached = Vec::new();
        // each shard as the description gives it, in the listing's order
        let mut shards = Vec::new();
        for s in &self.shards {
            let e = self.elems[s.k];
            let mut entry = json!({"key": keys[s.k].0, "object": e});
            match (&s.header, &s.reason) {
                (Some(h), None) => {
                    ir.describe_in_place(e)?;
                    let (header, tensors) = h.emit(self.rules, &mut ir, e)?;
                    reached.push(tensors);
                    entry["header"] = header;
                }
                (_, Some(why)) => entry["error"] = json!(why),
                (None, None) => return Err("internal: a shard neither checked nor refused".into()),
            }
            shards.push(entry);
        }
        // the documents: each index and configuration read whole and held, or why not
        let mut documents = Vec::new();
        for d in &self.docs {
            let (key, size) = (&keys[d.k].0, keys[d.k].1);
            let parsed = match &d.bytes {
                Some(b) => json::parse(b),
                None => Err(format!("a JSON document of {size} bytes is longer than {}", json::MAX_DOCUMENT)),
            };
            match parsed {
                Ok(doc) => {
                    json::hold(&mut ir, self.elems[d.k], size, &doc.members)?;
                    documents.push(json!({"key": key, "object": self.elems[d.k], "json": doc.value.text()}));
                }
                Err(e) => documents.push(json!({"key": key, "object": self.elems[d.k], "error": e})),
            }
        }
        crate::check::finish(&mut ir)?;
        let (arrays, chains, map) = stored::described(&reached)?;
        ir.describe(arrays, chains, map)?;
        self.ir = Some(ir);
        Ok(json!({"shards": shards, "documents": documents}))
    }
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
        let description = self.emit()?;
        Ok((self.ir.take().ok_or("internal: no IR")?, description))
    }

    fn profile(&self) -> Option<&'static str> {
        Some("safetensors")
    }
}
