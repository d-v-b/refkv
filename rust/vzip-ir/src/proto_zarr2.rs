//! The prototype of a store format on the IR (conventions §8.9
//! <https://github.com/d-v-b/vzip/blob/main/spec/conventions.md#89-stores>): a Zarr v2 store's
//! listing, and its documents (every nonempty `.zarray`, `.zgroup` and `.zattrs`, read in one
//! batch) held whole, each as one text value `json` of its bytes. Every other object (chunk
//! objects among them) is referenced: an object with no children. It decides nothing a Zarr
//! v2 profile decides (which documents are nodes', the OME-NGFF test, rejections): it shows
//! the store IR's mirror, canonical form, fold and rebuild on real stores, and is used by no
//! production path (spec/virtualize/zarr2.md stays the profile).

use crate::ir::{Ir, Res, NO_INDEX};
use crate::store::{StoreParser, StoreStep};
use serde_json::{json, Value as J};
use std::collections::HashMap;

const DOCUMENTS: [&str; 3] = [".zarray", ".zgroup", ".zattrs"];

pub struct Zarr2Proto {
    ir: Ir,
    objects: Vec<u32>,
    docs: Vec<(usize, u64)>,
    asked: bool,
    got: HashMap<usize, Vec<u8>>,
}

impl Zarr2Proto {
    pub fn new(ir: Ir, objects: Vec<u32>, keys: &[(String, u64)]) -> Zarr2Proto {
        let docs = keys.iter().enumerate()
            .filter(|(_, (k, n))| *n > 0 && DOCUMENTS.contains(&k.rsplit('/').next().unwrap_or("")))
            .map(|(i, (_, n))| (i, *n)).collect();
        Zarr2Proto { ir, objects, docs, asked: false, got: HashMap::new() }
    }
}

impl StoreParser for Zarr2Proto {
    fn step(&mut self) -> Res<StoreStep> {
        if self.asked || self.docs.is_empty() {
            return Ok(StoreStep::Done);
        }
        self.asked = true;
        Ok(StoreStep::Read(self.docs.iter().map(|&(k, n)| (k, 0, n)).collect()))
    }

    fn feed(&mut self, object: usize, _offset: u64, data: &[u8]) {
        self.got.insert(object, data.to_vec());
    }

    fn finish(self: Box<Self>) -> Res<(Ir, J)> {
        let Zarr2Proto { mut ir, objects, docs, got, .. } = *self;
        for &(k, n) in &docs {
            let b = got.get(&k).ok_or("internal: a document not read")?;
            let ty = if std::str::from_utf8(b).is_ok() { format!("ascii[{n}]") } else { format!("bytes[{n}]") };
            ir.value(objects[k], "json", NO_INDEX, &ty, 0, (0, n), None)?;
        }
        crate::check::finish(&mut ir)?;
        Ok((ir, json!({"documents": docs.len()})))
    }
}
