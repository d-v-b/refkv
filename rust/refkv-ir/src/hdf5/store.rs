//! An HDF5 file that names siblings, read as a store (spec/zarrify/hdf5.md §8.6
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#86-external-files>): the named
//! file's parser, paused at `Step::Beside` while the run probes the keys it names, then a parser of
//! each file an external link names. The hierarchy is the named file's, with what each link names
//! at the link's path, one level deep, and the mirror each file's IR at its object.

use super::file::{name_axes, node_name, Hdf5};
use crate::beside::{written_key, Tree, Written};
use crate::ir::{Ir, Res};
use crate::nd2::Step;
use crate::store::{StoreParser, StoreStep};
use serde_json::{json, Value as J};
use std::collections::HashMap;

/// The reason a linked object is not presented: its links and references name paths of its own
/// file, which the hierarchy does not hold.
const EXTERNAL_REFERENCE: &str = "external_reference";

#[derive(PartialEq)]
enum Stage {
    Named,
    Probed,
    Following,
    Done,
}

/// A file a link names, by its listing index; no parser once it does not read as HDF5.
struct Follower {
    object: usize,
    parser: Option<Hdf5>,
    done: bool,
}

pub struct Hdf5Store {
    ir: Ir,
    tree: Tree,
    keys: Vec<(String, u64)>,
    elems: Vec<u32>,
    named: Hdf5,
    stage: Stage,
    followers: Vec<Follower>,
}

impl Hdf5Store {
    pub fn new(ir: Ir, objects: Vec<u32>, keys: &[(String, u64)]) -> Res<Hdf5Store> {
        let [(_, size)] = keys else { return Err("internal: an HDF5 input lists its named file alone".into()) };
        Ok(Hdf5Store {
            ir,
            tree: Tree::default(),
            keys: keys.to_vec(),
            elems: objects,
            named: Hdf5::new(*size),
            stage: Stage::Named,
            followers: Vec::new(),
        })
    }

    /// A parser of each found file a link names, which follows nothing further.
    fn follow(&mut self) {
        for (object, (key, size)) in self.keys.iter().enumerate().skip(1) {
            if self.named.link_keys().contains(key) {
                let mut parser = Hdf5::new(*size);
                parser.resolve(HashMap::new());
                self.followers.push(Follower { object, parser: Some(parser), done: false });
            }
        }
    }

    /// The next reads of the files links name; a file that does not read as HDF5 is not followed.
    fn following(&mut self) -> Vec<(usize, u64, u64)> {
        let mut reads = Vec::new();
        for f in &mut self.followers {
            let Some(parser) = f.parser.as_mut().filter(|_| !f.done) else { continue };
            match parser.step() {
                Ok(Step::Read(r)) => reads.extend(r.into_iter().map(|(o, n)| (f.object, o, n))),
                Ok(_) => f.done = true,
                Err(_) => f.parser = None,
            }
        }
        reads
    }
}

/// Whether `v` holds an object reference (spec/zarrify/hdf5.md §5
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#5-object-references>).
fn holds_reference(v: &J) -> bool {
    match v {
        J::Object(m) => m.contains_key("_REFERENCE") || m.values().any(holds_reference),
        J::Array(a) => a.iter().any(holds_reference),
        _ => false,
    }
}

/// The nodes of a file at and below `target` (an HDF5 path) moved to the path `at`, without what
/// names paths of their file; and the paths left out (`at` itself, or below it).
fn linked_nodes(nodes: &[J], target: &str, at: &str) -> (Vec<J>, Vec<String>) {
    let target = target.trim_matches('/');
    let (mut moved, mut left_out) = (Vec::new(), Vec::new());
    for node in nodes {
        let path = node["path"].as_str().unwrap_or("");
        let suffix = if target.is_empty() {
            path
        } else if path == target {
            ""
        } else if let Some(s) = path.strip_prefix(target).and_then(|s| s.strip_prefix('/')) {
            s
        } else {
            continue;
        };
        let new = if suffix.is_empty() { at.to_string() } else { format!("{at}/{suffix}") };
        let references = node.get("array").is_some_and(|a| a["store"]["kind"] == "references")
            || node["attributes"].get("_REFERENCE_FIELDS").is_some();
        if references {
            left_out.push(new);
            continue;
        }
        let mut node = node.clone();
        let names: Vec<String> = node["attributes"].as_object().into_iter().flatten()
            .filter(|(k, v)| *k == "_LINKS" || holds_reference(v))
            .map(|(k, _)| k.clone())
            .collect();
        for k in names {
            if let Some(m) = node["attributes"].as_object_mut() {
                m.remove(&k);
            }
            node["own"]["unpresented_attributes"][k] = json!(EXTERNAL_REFERENCE);
        }
        node["segments"] = json!(new.split('/').count());
        node["path"] = json!(new);
        moved.push(node);
    }
    (moved, left_out)
}

/// A file's facts with its storage elements as `map` numbers them in the store IR, and each array's
/// object `object`.
fn placed(facts: &mut J, map: &[u32], object: u32) {
    for node in facts["nodes"].as_array_mut().into_iter().flatten() {
        let Some(store) = node.get_mut("array").map(|a| &mut a["store"]) else { continue };
        if let Some(e) = store["element"].as_u64() {
            store["element"] = json!(map[e as usize]);
        }
        store["object"] = json!(object);
    }
}

impl StoreParser for Hdf5Store {
    fn step(&mut self) -> Res<StoreStep> {
        loop {
            match self.stage {
                Stage::Named => match self.named.step()? {
                    Step::Read(r) => return Ok(StoreStep::Read(r.into_iter().map(|(o, n)| (0, o, n)).collect())),
                    Step::Beside => {
                        self.stage = Stage::Probed;
                        let keys: Vec<String> = self.named.sibling_keys().iter().filter(|k| **k != self.keys[0].0).cloned().collect();
                        if !keys.is_empty() {
                            return Ok(StoreStep::Probe(keys));
                        }
                    }
                    Step::Done => {
                        self.follow();
                        self.stage = Stage::Following;
                    }
                },
                Stage::Probed => {
                    self.named.resolve(self.keys.iter().cloned().collect());
                    self.stage = Stage::Named;
                }
                Stage::Following => {
                    let reads = self.following();
                    if !reads.is_empty() {
                        return Ok(StoreStep::Read(reads));
                    }
                    if self.followers.iter().all(|f| f.done || f.parser.is_none()) {
                        self.stage = Stage::Done;
                    }
                }
                Stage::Done => return Ok(StoreStep::Done),
            }
        }
    }

    fn feed(&mut self, object: usize, offset: u64, data: &[u8]) {
        if object == 0 {
            self.named.feed(offset, data.to_vec());
        } else if let Some(p) = self.followers.iter_mut().find(|f| f.object == object).and_then(|f| f.parser.as_mut()) {
            p.feed(offset, data.to_vec());
        }
    }

    /// A key the file names that is absent stays recorded: an HDF5 name may dangle.
    fn found(&mut self, key: &str, index: usize, size: Option<u64>) -> Res<Option<u32>> {
        let Some(size) = size else { return Ok(None) };
        if index != self.keys.len() {
            return Err("internal: a found object out of order".into());
        }
        let e = self.tree.object(&mut self.ir, key, size)?;
        self.keys.push((key.to_string(), size));
        self.elems.push(e);
        Ok(Some(e))
    }

    fn finish(self: Box<Self>) -> Res<(Ir, J)> {
        if self.stage != Stage::Done {
            return Err("internal: the parse is not done".into());
        }
        let Hdf5Store { mut ir, keys, elems, named, followers, .. } = *self;
        let (named_ir, mut facts) = named.finish()?;
        ir.describe_in_place(elems[0])?;
        let map = ir.graft(&named_ir, elems[0])?;
        placed(&mut facts, &map, elems[0]);
        let mut files: HashMap<String, J> = HashMap::new();
        files.insert(keys[0].0.clone(), facts.clone());
        for f in followers {
            let Some(parser) = f.parser.filter(|p| !p.is_ims()) else { continue };
            let Ok((file_ir, mut file)) = parser.finish() else { continue };
            ir.describe_in_place(elems[f.object])?;
            let map = ir.graft(&file_ir, elems[f.object])?;
            placed(&mut file, &map, elems[f.object]);
            files.insert(keys[f.object].0.clone(), file);
        }
        let mut added = Vec::new();
        for group in facts["nodes"].as_array_mut().into_iter().flatten() {
            let links: Vec<J> = group["own"]["external_links"].as_array().cloned().unwrap_or_default();
            let path = group["path"].as_str().unwrap_or("").to_string();
            for link in links {
                let (Some(name), Some(file), Some(target)) = (link["name"].as_str(), link["file"].as_str(), link["path"].as_str()) else { continue };
                let Some(key) = written_key(file.as_bytes(), "", Written::default()).filter(|_| node_name(name)) else { continue };
                let Some(source) = files.get(&key) else { continue };
                let at = if path.is_empty() { name.to_string() } else { format!("{path}/{name}") };
                let (mut moved, left_out) = linked_nodes(source["nodes"].as_array().map_or(&[], |n| n), target, &at);
                for out in left_out {
                    let (parent, child) = out.rsplit_once('/').unwrap_or(("", &out));
                    if out == at {
                        group["own"]["unpresented"][child] = json!(EXTERNAL_REFERENCE);
                    } else if let Some(n) = moved.iter_mut().find(|n| n["path"] == parent) {
                        n["own"]["unpresented"][child] = json!(EXTERNAL_REFERENCE);
                    }
                }
                if let Some(top) = moved.iter_mut().find(|n| n["path"] == at.as_str()) {
                    top["own"]["external"] = json!({"file": key, "path": target});
                }
                added.extend(moved);
            }
        }
        let (groups, arrays) = added.iter().fold((0u64, 0u64), |(g, a), n: &J| if n.get("array").is_some() { (g, a + 1) } else { (g + 1, a) });
        if let Some(nodes) = facts["nodes"].as_array_mut() {
            if let Some(root) = nodes.first_mut().filter(|_| keys.len() > 1) {
                root["own"]["siblings"] = json!(keys[1..].iter().map(|k| &k.0).collect::<Vec<_>>());
            }
            nodes.extend(added);
            // a linked object is a member of the group that links it
            name_axes(nodes);
        }
        facts["named_key"] = json!(keys[0].0);
        // no edge chunk is copied: the run's reads have ended, and its padding is the zero data source's
        facts["copy_threshold"] = json!(1u64 << 40);
        facts["summary"]["groups"] = json!(facts["summary"]["groups"].as_u64().unwrap_or(0) + groups);
        facts["summary"]["arrays"] = json!(facts["summary"]["arrays"].as_u64().unwrap_or(0) + arrays);
        ir.sort_values();
        crate::check::finish(&mut ir)?;
        Ok((ir, facts))
    }
}
