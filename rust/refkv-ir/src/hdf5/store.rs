//! An HDF5 file that names siblings, read as a store (spec/zarrify/hdf5.md §8.6
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#86-external-files>): the named
//! file's parser, paused at `Step::Beside` while the run probes the keys it names, then a parser of
//! each file an external link names. The description is each file's, and the mirror each file's IR
//! at its object; the projection presents the named file's hierarchy with what each link names at
//! the link's path, one level deep.

use super::file::Hdf5;
use crate::beside::Tree;
use crate::ir::{Ir, Res};
use crate::nd2::Step;
use crate::store::{StoreParser, StoreStep};
use serde_json::{json, Map, Value as J};
use std::collections::HashMap;

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
            named: {
                let mut p = Hdf5::new(*size);
                p.not_h5j();
                p
            },
            stage: Stage::Named,
            followers: Vec::new(),
        })
    }

    /// A parser of each found file a link names, which follows nothing further.
    fn follow(&mut self) {
        for (object, (key, size)) in self.keys.iter().enumerate().skip(1) {
            if self.named.link_keys().contains(key) {
                let mut parser = Hdf5::new(*size);
                parser.not_h5j();
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

/// A file's description with its storage elements as `map` numbers them in the store IR, and each
/// dataset's object `object`.
fn placed(description: &mut J, map: &[u32], object: u32) {
    for o in description["objects"].as_array_mut().into_iter().flatten() {
        let Some(storage) = o.get_mut("storage") else { continue };
        if let Some(e) = storage["element"].as_u64() {
            storage["element"] = json!(map[e as usize]);
        }
        storage["object"] = json!(object);
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
        files.insert(keys[0].0.clone(), facts);
        for f in followers {
            let Some(parser) = f.parser.filter(|p| !p.is_ims()) else { continue };
            let Ok((file_ir, mut file)) = parser.finish() else { continue };
            ir.describe_in_place(elems[f.object])?;
            let map = ir.graft(&file_ir, elems[f.object])?;
            placed(&mut file, &map, elems[f.object]);
            files.insert(keys[f.object].0.clone(), file);
        }
        // the projection presents the named file's hierarchy with what its links name
        // (`crate::project::hdf5::project_store`)
        // each file's description, without its parse's counts (the named file's rounds reach the
        // summary through `StoreParser::counts`)
        for d in files.values_mut() {
            if let Some(m) = d.as_object_mut() {
                for count in ["requests", "requested", "rounds"] {
                    m.remove(count);
                }
            }
        }
        let mut out = Map::new();
        out.insert("named_key".into(), json!(keys[0].0));
        out.insert("siblings".into(), json!(keys[1..].iter().map(|k| &k.0).collect::<Vec<_>>()));
        out.insert("files".into(), J::Object(files.into_iter().collect()));
        let description = J::Object(out);
        ir.sort_values();
        crate::check::finish(&mut ir)?;
        Ok((ir, description))
    }

    /// The named file's read rounds, which the summary holds (spec/zarrify/hdf5.md §8.5
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#85-summary>).
    fn counts(&self) -> J {
        json!({"rounds": self.named.rounds()})
    }
}
