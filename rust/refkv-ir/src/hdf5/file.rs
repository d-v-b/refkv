//! The sans-IO HDF5 parser (spec/zarrify/hdf5.md
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md>): any HDF5 file, NWB files
//! first among them, as a Zarr hierarchy of its groups and datasets. Its phases, each run in
//! rounds (the reader's replay):
//!
//! 1. **Sniff**: the root group's links. An Imaris file (a link `DataSet` to a group with a link
//!    `ResolutionLevel 0`) is the IMS profile's: the parser hands every byte to the IMS parser
//!    and is that parser from then on (`found`).
//! 2. **Walk**: every object reachable from the root group by hard links, by level (the IMS
//!    walk's `visit`), then each object read strictly, so that a structure the hierarchy
//!    depends on rejects the input when it cannot be read.
//! 3. **Data**: the pointers of the datasets of variable-length strings and object references
//!    (their storage, read and decoded) and the global heap collections that strings name.
//! 4. **Facts**: the hierarchy's nodes, as the projection writes them.
//!
//! `finish` emits the IR as the IMS walk does (spec/zarrify/ims.md §5
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ims.md#5-source-metadata>), every
//! presented dataset's storage described whatever the room.

use super::types::{self, describe, present, values_json, Dt, Field, FieldKind, Kind, Lookup, Present};
use super::walk::{lenient, visit, Emit, Visit};
use super::{le, Acc, Collection, Dataset, Fail, Index, Layout, Target, H5, R, UNDEFINED};
use crate::ir::{Ir, NO_INDEX};
use crate::nd2::Step;
use crate::rules::{self, num, Rules, Vars};
use crate::text::utf8_or_latin1;
use serde_json::{json, Map, Value as J};
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::sync::Arc as Rc;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Phase {
    Sniff,
    Walk,
    Check,
    Data,
    Facts,
    Done,
}

/// The names a node of the hierarchy may not take at the root (the mirror's node).
const RESERVED_ROOT: &[&str] = &["refkv_source"];
/// The attribute names the hierarchy writes itself (spec/zarrify/hdf5.md §2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#2-the-hierarchy>).
const RESERVED_ATTRIBUTES: &[&str] = &["zarr_conventions", "refkv_zarrified", "_LINKS", "_DTYPE", "_REFERENCE_FIELDS"];

/// The external link type of a link message.
const EXTERNAL: u8 = 64;
/// The external data files message.
const EXTERNAL_FILES: u16 = 0x07;

pub struct Hdf5 {
    h: H5,
    rules: &'static Rules,
    phase: Phase,
    /// the IMS parser, once the file is found to be an Imaris file
    ims: Option<Box<crate::ims::Ims>>,
    /// the bytes fed while sniffing, which the IMS parser is given
    log: Vec<(u64, Vec<u8>)>,
    rounds: u64,
    requests: u64,
    requested: u64,
    visited: HashMap<u64, Visit>,
    level: Vec<u64>,
    depth: u64,
    extension: Option<Visit>,
    /// the objects in canonical order, each with its canonical path's segments
    order: Vec<(u64, Vec<Vec<u8>>)>,
    paths: HashMap<u64, String>,
    /// each presented dataset of strings or references: its elements' bytes, row-major
    storage: HashMap<u64, Vec<u8>>,
    /// the datasets of strings among them
    strings: BTreeSet<u64>,
    /// the datasets of records among them, with their datatypes
    records: HashMap<u64, Dt>,
    /// the global heap collections read, by address
    heaps: BTreeMap<u64, Rc<Collection>>,
    facts: Option<J>,
    /// the walk's room in elements, when a test sets it (else the schema's)
    pub room: Option<u64>,
}

/// What the hierarchy holds of an object, decided in the walk's order.
enum Decision {
    Group,
    Dataset(Box<Dataset>, Dt, Present),
    /// the object is not a node: why
    Not(&'static str),
}

impl Hdf5 {
    pub fn new(size: u64) -> Self {
        let mut h = H5::new(size);
        h.symtab_soft = true;
        Hdf5 {
            h,
            rules: rules::of("hdf5"),
            phase: Phase::Sniff,
            ims: None,
            log: Vec::new(),
            rounds: 0,
            requests: 0,
            requested: 0,
            visited: HashMap::new(),
            level: Vec::new(),
            depth: 0,
            extension: None,
            order: Vec::new(),
            paths: HashMap::new(),
            storage: HashMap::new(),
            strings: BTreeSet::new(),
            records: HashMap::new(),
            heaps: BTreeMap::new(),
            facts: None,
            room: None,
        }
    }

    pub fn feed(&mut self, offset: u64, data: Vec<u8>) {
        if let Some(ims) = self.ims.as_mut() {
            ims.feed(offset, data);
            return;
        }
        if self.phase == Phase::Sniff {
            self.log.push((offset, data.clone()));
        }
        self.h.src.store.feed(offset, data);
    }

    fn check(&self, role: &str, mut env: Vars<'static>) -> R<()> {
        self.rules.check(role, &mut env).map_err(Fail::Bad)
    }

    fn limit(&self, name: &str) -> u64 {
        self.rules.limit(name) as u64
    }

    pub fn step(&mut self) -> Result<Step, String> {
        loop {
            if let Some(ims) = self.ims.as_mut() {
                return ims.step();
            }
            self.h.src.want.clear();
            let r = match self.phase {
                Phase::Sniff => self.sniff(),
                Phase::Walk => self.walk(),
                Phase::Check => self.strict(),
                Phase::Data => self.data(),
                Phase::Facts => self.make_facts(),
                Phase::Done => return Ok(Step::Done),
            };
            match r {
                Ok(()) => {
                    if self.ims.is_some() {
                        continue;
                    }
                    self.phase = match self.phase {
                        Phase::Sniff => Phase::Walk,
                        Phase::Walk => Phase::Check,
                        Phase::Check => Phase::Data,
                        Phase::Data => Phase::Facts,
                        _ => Phase::Done,
                    };
                }
                Err(Fail::Bad(m)) => return Err(m),
                Err(Fail::Miss) => {
                    let mut want = std::mem::take(&mut self.h.src.want);
                    if want.is_empty() {
                        return Err("internal: a round wants nothing".into());
                    }
                    self.rounds += 1;
                    if self.rounds as f64 > self.rules.limit("max_rounds") {
                        return Err(format!("more than {} rounds of dependent reads", self.rules.limit("max_rounds")));
                    }
                    want.sort_unstable();
                    want.dedup();
                    let mut merged: Vec<(u64, u64)> = Vec::new();
                    for (o, n) in want {
                        if let Some(last) = merged.last_mut() {
                            if o <= last.0 + last.1 {
                                last.1 = last.1.max(o + n - last.0);
                                continue;
                            }
                        }
                        merged.push((o, n));
                    }
                    self.requests += merged.len() as u64;
                    self.requested += merged.iter().map(|r| r.1).sum::<u64>();
                    return Ok(Step::Read(merged));
                }
            }
        }
    }

    // ---- phase 1: an Imaris file is the IMS profile's (spec/zarrify/hdf5.md §8.1
    // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#81-choosing-the-profile>)

    fn sniff(&mut self) -> R<()> {
        let sb = self.h.superblock()?;
        let imaris = (|| -> R<bool> {
            let Some(root) = lenient(self.h.links(sb.root))? else { return Ok(false) };
            if !root.has("DataSet") {
                return Ok(false);
            }
            let Some(at) = lenient(self.h.follow(&root, b"DataSet"))? else { return Ok(false) };
            Ok(lenient(self.h.links(at))?.is_some_and(|l| l.has("ResolutionLevel 0")))
        })()?;
        if imaris {
            let mut ims = Box::new(crate::ims::Ims::new(self.h.size()));
            for (o, d) in std::mem::take(&mut self.log) {
                ims.feed(o, d);
            }
            self.ims = Some(ims);
        }
        self.log.clear();
        Ok(())
    }

    // ---- phase 2: the walk (spec/zarrify/hdf5.md §8.2
    // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#82-reading>)

    fn walk(&mut self) -> R<()> {
        let sb = self.h.superblock()?;
        if self.depth == 0 && self.level.is_empty() {
            self.level = vec![sb.root];
        }
        if self.extension.is_none() {
            if let Some(e) = sb.extension {
                let mut acc = Acc::default();
                let h = acc.take(lenient(self.h.header(e)));
                acc.done()?;
                self.extension = Some(Visit { header: h.flatten(), ..Default::default() });
            } else {
                self.extension = Some(Visit::default());
            }
        }
        let max_objects = self.limit("max_objects");
        let collections = self.limit("walk_collections") as usize;
        loop {
            let mut acc = Acc::default();
            for at in self.level.clone() {
                if self.visited.contains_key(&at) {
                    continue;
                }
                if let Some(v) = acc.take(visit(&mut self.h, at, collections)) {
                    self.visited.insert(at, v);
                }
            }
            acc.done()?;
            self.depth += 1;
            let mut next = BTreeSet::new();
            for at in &self.level {
                if let Some(l) = self.visited.get(at).and_then(|v| v.links.as_ref()) {
                    for (_, t) in &l.list {
                        if let Target::Hard(a) = t {
                            if !self.visited.contains_key(a) {
                                next.insert(*a);
                            }
                        }
                    }
                }
            }
            let objects = self.visited.len() as u64 + next.len() as u64;
            self.check("objects", rules::env([("objects", num(objects as f64)), ("depth", num(self.depth as f64)), ("more", rules::flag(!next.is_empty()))]))?;
            if next.is_empty() || objects > max_objects {
                break;
            }
            self.level = next.into_iter().collect();
        }
        self.tree();
        Ok(())
    }

    /// The objects in canonical order: depth first from the root group, each group's hard links
    /// in ascending byte order of their names, each object at the first path that reaches it
    /// (spec/zarrify/hdf5.md §2.2
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#22-paths>).
    fn tree(&mut self) {
        let root = self.h.sb.as_ref().map_or(0, |s| s.root);
        let mut seen = HashSet::new();
        let mut stack: Vec<(u64, Vec<Vec<u8>>)> = vec![(root, Vec::new())];
        while let Some((at, segs)) = stack.pop() {
            if !self.visited.contains_key(&at) || !seen.insert(at) {
                continue;
            }
            let path = format!("/{}", segs.iter().map(|s| utf8_or_latin1(s)).collect::<Vec<_>>().join("/"));
            self.paths.insert(at, path);
            if let Some(l) = &self.visited[&at].links {
                let mut kids: Vec<(&Vec<u8>, u64)> = l.list.iter().filter_map(|(n, t)| if let Target::Hard(a) = t { Some((n, *a)) } else { None }).collect();
                kids.sort();
                for (n, a) in kids.into_iter().rev() {
                    if !seen.contains(&a) {
                        let mut s = segs.clone();
                        s.push(n.clone());
                        stack.push((a, s));
                    }
                }
            }
            self.order.push((at, segs));
        }
    }

    fn path(&self, at: u64) -> String {
        self.paths.get(&at).cloned().unwrap_or_else(|| format!("object at {at}"))
    }

    /// Each object read strictly: its header, a group's links, its attributes, a dataset's
    /// messages and chunk index (spec/zarrify/hdf5.md §8.3
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#83-checks>).
    fn strict(&mut self) -> R<()> {
        let mut acc = Acc::default();
        for (at, _) in self.order.clone() {
            let r = (|| -> R<()> {
                let h = self.h.header(at)?;
                let (group, dataset) = (h.has(super::SYMBOL_TABLE) || h.has(super::LINK_INFO), h.has(super::LAYOUT));
                if group {
                    self.h.links(at)?;
                }
                self.h.attributes(at)?;
                if dataset && !group {
                    let ds = self.h.dataset(at, true)?;
                    if ds.layout == Layout::Chunked {
                        self.h.chunks(at, &ds, true)?;
                    }
                }
                Ok(())
            })();
            let r = r.map_err(|f| match f {
                Fail::Bad(m) => Fail::Bad(format!("{}: {m}", self.path(at))),
                f => f,
            });
            acc.take(r);
            if acc.stopped() {
                break;
            }
        }
        acc.done()
    }

    /// What the hierarchy holds of the object at `at`, whose name is `name` (None for the root).
    fn decide(&mut self, at: u64, name: Option<&[u8]>) -> R<Decision> {
        if let Some(n) = name {
            let ok = std::str::from_utf8(n).is_ok_and(|s| !s.is_empty() && s != "." && s != ".." && !s.starts_with("__") && !s.contains('/'));
            if !ok {
                return Ok(Decision::Not("name"));
            }
        }
        let h = self.h.header(at)?;
        if h.has(super::SYMBOL_TABLE) || h.has(super::LINK_INFO) {
            return Ok(Decision::Group);
        }
        if !h.has(super::LAYOUT) {
            return Ok(Decision::Not(if h.has(super::DATATYPE) { "named_datatype" } else { "object" }));
        }
        let ds = self.h.dataset(at, true)?;
        let (type_message, _) = self.h.datatype(&h)?;
        let dt = types::parse(&type_message)?;
        if h.has(EXTERNAL_FILES) {
            return Ok(Decision::Not("external_storage"));
        }
        match ds.layout {
            Layout::Null => return Ok(Decision::Not("null_dataspace")),
            Layout::Virtual => return Ok(Decision::Not("layout")),
            _ => {}
        }
        let p = match present(&dt) {
            Ok(p) => p,
            Err(why) => return Ok(Decision::Not(why)),
        };
        if ds.layout == Layout::Chunked {
            if ds.filters.iter().any(|f| !matches!(f, 1 | 2)) {
                return Ok(Decision::Not("filter"));
            }
            if ds.raw_edges {
                return Ok(Decision::Not("edge_chunks"));
            }
            // a chunk a filter skipped (an optional filter that failed, as deflate does on
            // bytes it cannot shrink) has its own codecs, which an array cannot give it; the
            // pointers of strings, references and records are decoded here, by each chunk's mask
            let ch = self.h.chunks(at, &ds, true)?;
            let decoded = matches!(present(&dt), Ok(Present::Strings | Present::References | Present::Records(_)));
            if !decoded && ch.list.iter().any(|c| c.3 != 0) {
                return Ok(Decision::Not("filter_mask"));
            }
        }
        let count = super::product(&ds.dims);
        let bytes = count.saturating_mul(dt.size as u128);
        match ds.layout {
            Layout::Contiguous if ds.contiguous.0.is_some() && ds.contiguous.1 as u128 != bytes => return Ok(Decision::Not("size")),
            Layout::Compact if ds.compact.1.len() as u128 != bytes => return Ok(Decision::Not("size")),
            _ => {}
        }
        if let Some(f) = &ds.fill {
            if fill_json(&dt, &p, f).is_none() {
                return Ok(Decision::Not("fill_value"));
            }
        }
        Ok(Decision::Dataset(Box::new(ds), dt, p))
    }

    // ---- phase 3: the pointers of strings and references (spec/zarrify/hdf5.md §4
    // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#4-variable-length-strings>, §5
    // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#5-object-references>)

    /// The dataset's elements' bytes, row-major: its contiguous storage, its compact data, or
    /// its chunks decoded (deflate and shuffle) and placed; unallocated parts are zero.
    fn elements(&mut self, at: u64, ds: &Dataset, size: u64) -> R<Vec<u8>> {
        let count = super::product(&ds.dims);
        let total = count.saturating_mul(size as u128);
        self.check("storage", rules::env([("bytes", num(total as f64))]))?;
        let total = total as usize;
        match ds.layout {
            Layout::Compact => Ok(ds.compact.1.clone()),
            Layout::Contiguous => match ds.contiguous.0 {
                Some(a) => self.h.read(a, total as u64),
                None => Ok(vec![0; total]),
            },
            _ => {
                let ch = self.h.chunks(at, ds, true)?;
                let mut out = vec![0u8; total];
                let cb = super::product(&ds.chunk) as usize * size as usize;
                let mut acc = Acc::default();
                let mut got = Vec::new();
                for (coords, a, n, mask) in &ch.list {
                    if let Some(b) = acc.take(self.h.read(*a, *n)) {
                        got.push((coords.clone(), b, *mask));
                    }
                }
                acc.done()?;
                for (coords, raw, mask) in got {
                    let mut b = raw;
                    for (i, f) in ds.filters.iter().enumerate().rev() {
                        if mask >> i & 1 != 0 {
                            continue;
                        }
                        b = match f {
                            1 => miniz_oxide::inflate::decompress_to_vec_zlib_with_limit(&b, cb).map_err(|e| Fail::Bad(format!("a chunk does not inflate: {e:?}")))?,
                            _ => unshuffle(&b, size as usize),
                        };
                    }
                    if b.len() != cb {
                        return super::bad("a chunk's bytes are not its byte count");
                    }
                    place(&mut out, &b, &coords, &ds.chunk, &ds.dims, size as usize);
                }
                Ok(out)
            }
        }
    }

    fn data(&mut self) -> R<()> {
        let mut acc = Acc::default();
        for (at, segs) in self.order.clone() {
            let r = (|| -> R<()> {
                let name = segs.last().map(|s| s.as_slice());
                let Decision::Dataset(ds, dt, p) = self.decide(at, name)? else { return Ok(()) };
                if !matches!(p, Present::Strings | Present::References | Present::Records(_)) || self.storage.contains_key(&at) {
                    return Ok(());
                }
                let b = self.elements(at, &ds, dt.size)?;
                if let Present::Records(_) = p {
                    self.records.insert(at, dt.clone());
                }
                if p == Present::Strings {
                    self.strings.insert(at);
                    let total: usize = self.strings.iter().filter_map(|s| self.storage.get(s)).map(|b| b.len() / 16).sum::<usize>() + b.len() / 16;
                    self.check("strings", rules::env([("strings", num(total as f64))]))?;
                }
                self.storage.insert(at, b);
                Ok(())
            })();
            let r = r.map_err(|f| match f {
                Fail::Bad(m) => Fail::Bad(format!("{}: {m}", self.path(at))),
                f => f,
            });
            acc.take(r);
            if acc.stopped() {
                break;
            }
        }
        acc.done()?;
        // the collections that the datasets' strings name
        let mut wanted: BTreeSet<u64> = BTreeSet::new();
        for at in &self.strings {
            let b = &self.storage[at];
            for k in 0..b.len() / 16 {
                let (n, c) = (le(b, 16 * k, 4)?, le(b, 16 * k + 4, 8)?);
                if n > 0 && c != 0 && c != UNDEFINED {
                    wanted.insert(c);
                }
            }
        }
        // the collections that the records' strings name
        for (at, dt) in &self.records {
            let b = &self.storage[at];
            vlen_collections(dt, b.len() / dt.size.max(1) as usize, b, &mut wanted);
        }
        // the collections that the attributes' strings name
        for (at, _) in self.order.clone() {
            let Some(attrs) = lenient(self.h.attributes(at))? else { continue };
            for a in &attrs.list {
                let Ok((t, dims, data)) = super::attribute_parts(&a.data) else { continue };
                let Ok(dt) = types::parse(&t) else { continue };
                vlen_collections(&dt, super::product(&dims) as usize, &data, &mut wanted);
            }
        }
        let mut acc = Acc::default();
        for c in wanted {
            if self.heaps.contains_key(&c) {
                continue;
            }
            // a collection that does not read is none: the dataset whose string names it is
            // rejected by its path (`make_facts`), an attribute's string not presented
            if let Some(Some(col)) = acc.take(lenient(self.h.collection(c))) {
                self.heaps.insert(c, col);
            }
            if acc.stopped() {
                break;
            }
        }
        acc.done()
    }

    // ---- phase 4: the facts (spec/zarrify/hdf5.md §2
    // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#2-the-hierarchy>)

    /// The JSON of the attributes of the object at `at` (spec/zarrify/hdf5.md §6
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#6-attributes>): the
    /// attributes presented, their datatypes, and those not presented, each with why.
    fn attributes_json(&mut self, at: u64) -> R<(Map<String, J>, Map<String, J>, Map<String, J>)> {
        let attrs = self.h.attributes(at)?;
        let mut values: Vec<(String, J)> = Vec::new();
        let (mut types_of, mut not) = (Map::new(), Map::new());
        let mut list: Vec<&super::Attr> = attrs.list.iter().collect();
        list.sort_by(|a, b| a.name.cmp(&b.name));
        for a in list {
            let name = utf8_or_latin1(&a.name);
            if RESERVED_ATTRIBUTES.contains(&name.as_str()) {
                not.insert(name, json!("reserved"));
                continue;
            }
            let parts = super::attribute_parts_at(&a.data)?;
            if parts.flags & 3 != 0 {
                not.insert(name, json!("shared"));
                continue;
            }
            let d = &a.data;
            let t = &d[parts.datatype.0 as usize..(parts.datatype.0 + parts.datatype.1) as usize];
            let dt = types::parse(t)?;
            types_of.insert(name.clone(), describe(&dt));
            let space = super::parse_dataspace(&d[parts.dataspace.0 as usize..(parts.dataspace.0 + parts.dataspace.1) as usize])?;
            let Some(dims) = space.dims else {
                values.push((name, J::Null));
                continue;
            };
            let n = super::product(&dims).saturating_mul(dt.size as u128);
            if parts.data as u128 + n > d.len() as u128 {
                return super::bad(format!("attribute {name}: its data runs past its message"));
            }
            let data = &d[parts.data as usize..(parts.data as u128 + n) as usize];
            match values_json(&dt, &dims, data, &*self) {
                Some(v) => values.push((name, v)),
                None => {
                    not.insert(name, json!("datatype"));
                }
            }
        }
        // the budget: the largest attributes left out first, until the rest fit
        let budget = self.limit("attribute_bytes") as usize;
        let mut sizes: Vec<(usize, usize)> = values.iter().enumerate().map(|(k, (n, v))| (crate::out::json_size(v) + n.len() + 4, k)).collect();
        let mut total: usize = sizes.iter().map(|s| s.0).sum();
        sizes.sort_by(|a, b| b.0.cmp(&a.0).then(a.1.cmp(&b.1)));
        let mut dropped = HashSet::new();
        for (s, k) in sizes {
            if total <= budget {
                break;
            }
            total -= s;
            dropped.insert(k);
        }
        let mut out = Map::new();
        for (k, (n, v)) in values.into_iter().enumerate() {
            if dropped.contains(&k) {
                not.insert(n, json!("budget"));
            } else {
                out.insert(n, v);
            }
        }
        Ok((out, types_of, not))
    }

    fn make_facts(&mut self) -> R<()> {
        let sb = self.h.superblock()?;
        let mut nodes = Vec::new();
        let mut counts: BTreeMap<&'static str, u64> = BTreeMap::new();
        let mut not_counts: BTreeMap<String, u64> = BTreeMap::new();
        let (mut n_strings, mut n_refs, mut chunks_total) = (0u64, 0u64, 0u64);
        let mut link_counts = [0u64; 3];
        let order = self.order.clone();
        let segs_of: HashMap<u64, &Vec<Vec<u8>>> = order.iter().map(|(a, s)| (*a, s)).collect();
        let parent_ok = |segs: &Vec<Vec<u8>>, presented: &HashSet<Vec<Vec<u8>>>| segs.len() <= 1 || presented.contains(&segs[..segs.len() - 1].to_vec());
        let mut presented_paths: HashSet<Vec<Vec<u8>>> = HashSet::new();
        let mut unpresented_of: HashMap<Vec<Vec<u8>>, Map<String, J>> = HashMap::new();
        for (at, segs) in &order {
            if !segs.is_empty() && !parent_ok(segs, &presented_paths) {
                continue;
            }
            let name = segs.last().map(|s| s.as_slice());
            let mut d = self.decide(*at, name)?;
            if segs.len() == 1 && RESERVED_ROOT.contains(&utf8_or_latin1(&segs[0]).as_str()) {
                d = Decision::Not("reserved");
            }
            let key = segs.iter().map(|s| utf8_or_latin1(s)).collect::<Vec<_>>().join("/");
            if let Decision::Not(why) = d {
                let parent = segs[..segs.len().saturating_sub(1)].to_vec();
                unpresented_of.entry(parent).or_default().insert(utf8_or_latin1(segs.last().map_or(&[][..], |s| s)), json!(why));
                *not_counts.entry(why.to_string()).or_default() += 1;
                continue;
            }
            presented_paths.insert(segs.clone());
            let (attributes, attr_types, attr_not) = self.attributes_json(*at)?;
            let mut own = Map::new();
            own.insert("address".into(), json!(at));
            match d {
                Decision::Group => {
                    *counts.entry("groups").or_default() += 1;
                    let links = self.h.links(*at)?;
                    let (mut links_json, mut external) = (Vec::new(), Vec::new());
                    let mut list: Vec<&(Vec<u8>, Target)> = links.list.iter().collect();
                    list.sort_by(|a, b| a.0.cmp(&b.0));
                    for (n, t) in list {
                        let name = utf8_or_latin1(n);
                        match t {
                            Target::Soft(p) => {
                                link_counts[0] += 1;
                                links_json.push(json!({"name": name, "source": ".", "path": utf8_or_latin1(p)}));
                            }
                            Target::Hard(a) => {
                                let mut child = segs.clone();
                                child.push(n.clone());
                                let canonical = segs_of.get(a).is_some_and(|s| **s == child);
                                if !canonical {
                                    link_counts[1] += 1;
                                    links_json.push(json!({"name": name, "source": ".", "path": self.path(*a)}));
                                }
                            }
                            Target::Other(kind, value) => {
                                link_counts[2] += 1;
                                let (file, path) = match (kind, value) {
                                    (&EXTERNAL, Some(v)) => external_value(v),
                                    _ => (J::Null, J::Null),
                                };
                                external.push(json!({"name": name, "type": kind, "file": file, "path": path}));
                            }
                        }
                    }
                    if !attr_types.is_empty() {
                        own.insert("attributes".into(), J::Object(attr_types));
                    }
                    if !attr_not.is_empty() {
                        own.insert("unpresented_attributes".into(), J::Object(attr_not));
                    }
                    if !external.is_empty() {
                        own.insert("external_links".into(), J::Array(external));
                    }
                    if segs.is_empty() {
                        own.insert("superblock".into(), json!(sb.version));
                    }
                    let mut attrs = attributes;
                    if !links_json.is_empty() {
                        attrs.insert("_LINKS".into(), J::Array(links_json));
                    }
                    nodes.push(json!({"path": key, "segments": segs.len(), "attributes": attrs, "own": own}));
                }
                Decision::Dataset(ds, dt, p) => {
                    *counts.entry("arrays").or_default() += 1;
                    let mut attrs = attributes;
                    let (mut data_type, codecs, mut fill, trailing) = array_type(&ds, &dt, &p);
                    let mut record_type: Option<(J, J)> = None;
                    let store = match (&p, ds.layout.clone()) {
                        (Present::Strings, _) => {
                            let b = self.storage.get(at).cloned().unwrap_or_default();
                            let mut spans = Vec::with_capacity(b.len() / 8);
                            for k in 0..b.len() / 16 {
                                let (n, c, i) = (le(&b, 16 * k, 4)?, le(&b, 16 * k + 4, 8)?, le(&b, 16 * k + 12, 4)?);
                                let (o, len) = if n == 0 || c == 0 || c == UNDEFINED {
                                    (0, 0)
                                } else {
                                    let col = self.heaps.get(&c).ok_or(Fail::Bad(format!("{}: element {k} names no global heap collection", self.path(*at))))?;
                                    let &(o, m) = col.objects.get(&i).ok_or(Fail::Bad(format!("{}: element {k} names no object of its collection", self.path(*at))))?;
                                    if m != n {
                                        return super::bad(format!("{}: element {k} is {n} bytes long, its heap object {m}", self.path(*at)));
                                    }
                                    (o + 16, n)
                                };
                                spans.push(json!(o));
                                spans.push(json!(len));
                            }
                            n_strings += (b.len() / 16) as u64;
                            json!({"kind": "strings", "spans": spans})
                        }
                        (Present::Records(fields), _) => {
                            let b = self.storage.get(at).cloned().unwrap_or_default();
                            let (data_type, fill, bytes, item) = self.records_of(*at, fields, &b, dt.size)?;
                            let refs: Vec<&str> = fields.iter().filter(|f| f.kind == FieldKind::Reference).map(|f| f.name.as_str()).collect();
                            if !refs.is_empty() {
                                attrs.insert("_REFERENCE_FIELDS".into(), json!(refs));
                            }
                            record_type = Some((data_type, fill));
                            json!({"kind": "records", "hex": hex(&bytes), "item": item})
                        }
                        (Present::References, _) => {
                            let b = self.storage.get(at).cloned().unwrap_or_default();
                            let paths: Vec<J> = (0..b.len() / 8)
                                .map(|k| {
                                    let a = le(&b, 8 * k, 8).unwrap_or(UNDEFINED);
                                    json!(self.paths.get(&a).cloned().unwrap_or_default())
                                })
                                .collect();
                            n_refs += paths.len() as u64;
                            attrs.insert("_DTYPE".into(), json!("object_reference"));
                            json!({"kind": "references", "paths": paths})
                        }
                        (_, Layout::Compact) => json!({"kind": "compact", "offset": ds.compact.0, "length": ds.compact.1.len()}),
                        (_, Layout::Contiguous) => match ds.contiguous.0 {
                            Some(a) if ds.contiguous.1 > 0 => {
                                if a.checked_add(ds.contiguous.1).is_none_or(|e| e > self.h.size()) {
                                    return super::bad(format!("{}: its data lies outside the file", self.path(*at)));
                                }
                                json!({"kind": "contiguous", "at": at, "offset": a, "length": ds.contiguous.1, "item": if trailing.is_some() { 1 } else { dt.size }})
                            }
                            _ => json!({"kind": "none"}),
                        },
                        (_, _) => {
                            let ch = self.h.chunks(*at, &ds, true)?;
                            chunks_total += ch.list.len() as u64;
                            let mut chunk = ds.chunk.clone();
                            if let Some(n) = trailing {
                                chunk.push(n);
                            }
                            if ch.list.is_empty() {
                                json!({"kind": "none", "chunk": chunk})
                            } else {
                                json!({"kind": "chunked", "at": at, "grid": ds.grid, "chunk": chunk, "trailing": trailing.is_some()})
                            }
                        }
                    };
                    if let Some((t, f)) = record_type {
                        data_type = t;
                        fill = f;
                    }
                    let mut shape = ds.dims.clone();
                    if let Some(n) = trailing {
                        shape.push(n);
                    }
                    own.insert("datatype".into(), describe(&dt));
                    if let Some(m) = &ds.maxdims {
                        own.insert("maxshape".into(), J::Array(m.iter().map(|&v| if v == UNDEFINED { J::Null } else { json!(v) }).collect()));
                    }
                    own.insert("layout".into(), json!(match ds.layout {
                        Layout::Compact => "compact",
                        Layout::Contiguous => "contiguous",
                        _ => "chunked",
                    }));
                    if ds.layout == Layout::Chunked {
                        own.insert("chunks".into(), json!(ds.chunk));
                        own.insert("index".into(), json!(match ds.index {
                            Some(Index::Btree1) => "btree1",
                            Some(Index::Single) => "single",
                            Some(Index::Implicit) => "implicit",
                            Some(Index::Farray) => "fixed_array",
                            Some(Index::Earray) => "extensible_array",
                            Some(Index::Btree2) => "btree2",
                            None => "none",
                        }));
                    }
                    if !ds.filters.is_empty() {
                        own.insert("filters".into(), json!(ds.filters));
                    }
                    if let Some(f) = &ds.fill {
                        own.insert("fill_value".into(), json!(crate::mirror::base64(f)));
                    }
                    if !attr_types.is_empty() {
                        own.insert("attributes".into(), J::Object(attr_types));
                    }
                    if !attr_not.is_empty() {
                        own.insert("unpresented_attributes".into(), J::Object(attr_not));
                    }
                    nodes.push(json!({"path": key, "segments": segs.len(), "attributes": attrs, "own": own,
                                      "array": {"shape": shape, "data_type": data_type, "fill_value": fill, "codecs": codecs, "store": store}}));
                }
                Decision::Not(_) => unreachable!(),
            }
        }
        // each group's children that are not nodes, by the group's path
        let by_key: HashMap<String, Map<String, J>> = unpresented_of
            .into_iter()
            .map(|(segs, m)| (segs.iter().map(|s| utf8_or_latin1(s)).collect::<Vec<_>>().join("/"), m))
            .collect();
        for node in nodes.iter_mut() {
            if node.get("array").is_some() {
                continue;
            }
            if let Some(m) = node["path"].as_str().and_then(|k| by_key.get(k)) {
                node["own"]["unpresented"] = J::Object(m.clone());
            }
        }
        let root_attrs = nodes.first().map(|n| n["attributes"].clone()).unwrap_or(J::Null);
        let mut summary = json!({
            "objects": self.order.len(),
            "groups": counts.get("groups").copied().unwrap_or(0),
            "arrays": counts.get("arrays").copied().unwrap_or(0),
            "unpresented": not_counts,
            "strings": n_strings,
            "references": n_refs,
            "links": {"soft": link_counts[0], "hard": link_counts[1], "external": link_counts[2]},
            "chunks": chunks_total,
        });
        if root_attrs["neurodata_type"] == json!("NWBFile") {
            let mut namespaces = Map::new();
            if let Some(J::Object(r)) = root_attrs.get(".specloc") {
                if let Some(p) = r.get("_REFERENCE").and_then(|x| x["path"].as_str()) {
                    let prefix = p.trim_start_matches('/').to_string();
                    for (_, segs) in &self.order {
                        let key = segs.iter().map(|s| utf8_or_latin1(s)).collect::<Vec<_>>();
                        let pre: Vec<&str> = prefix.split('/').filter(|s| !s.is_empty()).collect();
                        if key.len() == pre.len() + 2 && key[..pre.len()] == pre[..] {
                            let e = namespaces.entry(key[pre.len()].clone()).or_insert(json!([]));
                            e.as_array_mut().unwrap().push(json!(key[pre.len() + 1]));
                        }
                    }
                }
            }
            summary["nwb"] = json!({"version": root_attrs["nwb_version"], "namespaces": namespaces});
        }
        self.facts = Some(json!({"nodes": nodes, "copy_threshold": crate::out::copy_threshold(), "summary": summary}));
        Ok(())
    }

    /// A dataset of records as hdmf-zarr writes it (spec/zarrify/hdf5.md §3.3
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#33-compounds>): its
    /// `struct` data type, whose strings and references are fixed-length UTF-32 of the longest
    /// one's code points (at least 1), its fill value, its elements' bytes (little-endian), and an
    /// element's size.
    fn records_of(&self, at: u64, fields: &[Field], b: &[u8], size: u64) -> R<(J, J, Vec<u8>, u64)> {
        let size = size as usize;
        let count = if size == 0 { 0 } else { b.len() / size };
        // each string and reference member's texts, as code points
        let mut texts: Vec<Vec<Vec<char>>> = vec![Vec::new(); fields.len()];
        for (j, f) in fields.iter().enumerate() {
            if matches!(f.kind, FieldKind::Fixed { .. }) {
                continue;
            }
            for k in 0..count {
                let v = &b[k * size + f.offset as usize..];
                let text = match f.kind {
                    FieldKind::Reference => {
                        let a = le(v, 0, 8)?;
                        self.paths.get(&a).cloned().unwrap_or_default()
                    }
                    FieldKind::Text { fixed: Some(pad) } => {
                        let n = fields.get(j + 1).map_or(size as u64, |g| g.offset) - f.offset;
                        match types::fixed_text(&v[..n as usize], pad) {
                            J::String(t) => t,
                            o => o["latin1"].as_str().unwrap_or("").to_string(),
                        }
                    }
                    _ => {
                        let (n, c, i) = (le(v, 0, 4)?, le(v, 4, 8)?, le(v, 12, 4)?);
                        if n == 0 || c == 0 || c == UNDEFINED {
                            String::new()
                        } else {
                            let d = self.heap(c, i).ok_or(Fail::Bad(format!("{}: record {k} names no global heap object", self.path(at))))?;
                            utf8_or_latin1(&d)
                        }
                    }
                };
                texts[j].push(text.chars().collect());
            }
        }
        let widths: Vec<usize> = texts.iter().map(|t| t.iter().map(|c| c.len()).max().unwrap_or(0).max(1)).collect();
        let mut out_fields = Vec::with_capacity(fields.len());
        let mut fill = Map::new();
        let mut item = 0u64;
        for (j, f) in fields.iter().enumerate() {
            let t = match &f.kind {
                FieldKind::Fixed { data_type, size, .. } => {
                    item += size;
                    fill.insert(f.name.clone(), zero_of(data_type));
                    data_type.clone()
                }
                _ => {
                    item += 4 * widths[j] as u64;
                    fill.insert(f.name.clone(), json!(""));
                    json!({"name": "fixed_length_utf32", "configuration": {"length_bytes": 4 * widths[j]}})
                }
            };
            out_fields.push(json!({"name": f.name, "data_type": t}));
        }
        let mut bytes = Vec::with_capacity(count * item as usize);
        for k in 0..count {
            for (j, f) in fields.iter().enumerate() {
                match &f.kind {
                    FieldKind::Fixed { size: n, big, .. } => {
                        let at = k * size + f.offset as usize;
                        let v = &b[at..at + *n as usize];
                        if *big {
                            bytes.extend(v.iter().rev());
                        } else {
                            bytes.extend_from_slice(v);
                        }
                    }
                    _ => {
                        let t = &texts[j][k];
                        for c in t {
                            bytes.extend_from_slice(&(*c as u32).to_le_bytes());
                        }
                        bytes.resize(bytes.len() + 4 * (widths[j] - t.len()), 0);
                    }
                }
            }
        }
        Ok((json!({"name": "struct", "configuration": {"fields": out_fields}}), J::Object(fill), bytes, item))
    }

    // ---- the IR

    pub fn finish(mut self) -> Result<(Ir, J), String> {
        if let Some(ims) = self.ims.take() {
            return ims.finish();
        }
        if self.phase != Phase::Done {
            return Err("internal: the parse is not done".into());
        }
        let size = self.h.size();
        let mut facts = self.facts.take().unwrap_or(J::Null);
        let must: HashSet<u64> = facts["nodes"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|n| n["array"]["store"]["at"].as_u64())
            .collect();
        let room = self.room.unwrap_or(self.limit("walk_elements") + size / self.limit("walk_bytes_per_element"));
        let mut e = Emit { ir: Ir::new(size), h: &self.h, chunks_of: HashMap::new(), data_of: HashMap::new(), room, must };
        let sb = self.h.sb.clone().ok_or("internal: no superblock")?;
        e.superblock(&sb)?;
        if let Some(ext) = self.extension.take() {
            if let Some(h) = &ext.header {
                let s = e.ir.struct_(0, "superblock_extension", NO_INDEX, 0, None)?;
                e.header(s, h)?;
            }
        }
        let mut order: Vec<u64> = self.order.iter().map(|o| o.0).collect();
        let seen: HashSet<u64> = order.iter().copied().collect();
        let mut rest: Vec<u64> = self.visited.keys().copied().filter(|a| !seen.contains(a)).collect();
        rest.sort_unstable();
        order.extend(rest);
        let objects = e.ir.struct_(0, "objects", NO_INDEX, 0, None)?;
        let mut collections: BTreeMap<u64, Rc<Collection>> = self.heaps.clone();
        let mut structs = Vec::with_capacity(order.len());
        for i in 0..order.len() {
            structs.push(e.ir.struct_(objects, "", i as u64, 0, None)?);
        }
        // the presented datasets first, whatever the room, then the others while it lasts
        let (first, rest): (Vec<usize>, Vec<usize>) = (0..order.len()).partition(|&i| e.must.contains(&order[i]));
        for i in first.into_iter().chain(rest) {
            if e.full() && !e.must.contains(&order[i]) {
                continue;
            }
            let v = &self.visited[&order[i]];
            e.object(structs[i], order[i], v)?;
            for c in &v.collections {
                collections.insert(c.at, c.clone());
            }
        }
        if !collections.is_empty() && !e.full() {
            let g = e.ir.struct_(0, "global_heaps", NO_INDEX, 0, None)?;
            for (k, c) in collections.values().enumerate() {
                if e.full() {
                    break;
                }
                e.collection(g, k as u64, c)?;
            }
        }
        // each array's storage element: the struct of its chunks, or its contiguous data
        for n in facts["nodes"].as_array_mut().into_iter().flatten() {
            let Some(a) = n.get_mut("array") else { continue };
            let store = &mut a["store"];
            let Some(at) = store["at"].as_u64() else { continue };
            let el = match store["kind"].as_str() {
                Some("chunked") => e.chunks_of.get(&at),
                _ => e.data_of.get(&at),
            };
            let Some(&el) = el else { return Err(format!("internal: the storage of the dataset at {at} is not described")) };
            store["element"] = json!(el);
        }
        let mut ir = e.ir;
        ir.sort_values();
        crate::check::finish(&mut ir)?;
        facts["requests"] = json!(self.requests);
        facts["requested"] = json!(self.requested);
        facts["rounds"] = json!(self.rounds);
        facts["summary"]["rounds"] = json!(self.rounds);
        Ok((ir, facts))
    }
}

impl Lookup for Hdf5 {
    fn heap(&self, collection: u64, index: u64) -> Option<Vec<u8>> {
        let col = self.heaps.get(&collection)?;
        let &(o, n) = col.objects.get(&index)?;
        self.h.src.store.get(o + 16, n).map(|b| b.to_vec())
    }
    fn path(&self, address: u64) -> Option<String> {
        self.paths.get(&address).cloned()
    }
}

/// The global heap collections that `count` values of `dt` in `data` name: those of
/// variable-length strings, also as compound members.
fn vlen_collections(dt: &Dt, count: usize, data: &[u8], out: &mut BTreeSet<u64>) {
    let w = dt.size as usize;
    let mut offsets = Vec::new();
    fn find(dt: &Dt, base: usize, offsets: &mut Vec<usize>) {
        match &dt.kind {
            Kind::Vlen { string: true, .. } => offsets.push(base),
            Kind::Compound(m) => m.iter().for_each(|m| find(&m.dt, base + m.offset as usize, offsets)),
            _ => {}
        }
    }
    find(dt, 0, &mut offsets);
    if offsets.is_empty() {
        return;
    }
    for k in 0..count {
        for &o in &offsets {
            let at = k * w + o;
            if let (Ok(n), Ok(c)) = (le(data, at, 4), le(data, at + 4, 8)) {
                if n > 0 && c != 0 && c != UNDEFINED {
                    out.insert(c);
                }
            }
        }
    }
}

/// An external link's value: a version and flags byte, then the file's name and the object's
/// path, each NUL-terminated.
fn external_value(v: &[u8]) -> (J, J) {
    let mut parts = v.get(1..).unwrap_or(&[]).split(|&c| c == 0);
    let file = parts.next().map_or(J::Null, crate::text::text_json);
    let path = parts.next().map_or(J::Null, crate::text::text_json);
    (file, path)
}

/// HDF5's byte shuffle undone: `b` holds each element's k-th bytes together, for k in turn.
fn unshuffle(b: &[u8], size: usize) -> Vec<u8> {
    if size <= 1 || b.len() < size {
        return b.to_vec();
    }
    let n = b.len() / size;
    let mut out = b.to_vec();
    for i in 0..n {
        for k in 0..size {
            out[i * size + k] = b[k * n + i];
        }
    }
    out
}

/// Places a chunk's elements (`size` bytes each, row-major over `chunk`) at grid coordinates
/// `coords` into `out`, the dataset's elements row-major over `dims`; elements past the
/// dataset's edge are dropped.
fn place(out: &mut [u8], b: &[u8], coords: &[u64], chunk: &[u64], dims: &[u64], size: usize) {
    let rank = dims.len();
    if rank == 0 {
        let n = size.min(out.len()).min(b.len());
        out[..n].copy_from_slice(&b[..n]);
        return;
    }
    let total = super::product(chunk) as usize;
    let mut idx = vec![0u64; rank];
    for k in 0..total {
        let mut rem = k as u64;
        for d in (0..rank).rev() {
            idx[d] = rem % chunk[d];
            rem /= chunk[d];
        }
        let mut flat = 0u64;
        let mut inside = true;
        for d in 0..rank {
            let g = coords[d] * chunk[d] + idx[d];
            if g >= dims[d] {
                inside = false;
                break;
            }
            flat = flat * dims[d] + g;
        }
        if inside {
            let (o, s) = (flat as usize * size, k * size);
            if o + size <= out.len() && s + size <= b.len() {
                out[o..o + size].copy_from_slice(&b[s..s + size]);
            }
        }
    }
}

/// The JSON fill value of a dataset of `dt` presented as `p`, from the fill value message's
/// bytes `f`; None when the data type has no JSON form of it.
fn fill_json(dt: &Dt, p: &Present, f: &[u8]) -> Option<J> {
    struct NoLookup;
    impl Lookup for NoLookup {
        fn heap(&self, _: u64, _: u64) -> Option<Vec<u8>> {
            None
        }
        fn path(&self, _: u64) -> Option<String> {
            None
        }
    }
    match p {
        Present::Strings | Present::References | Present::Records(_) => Some(json!("")),
        Present::Bytes { .. } => f.iter().all(|&b| b == f[0]).then(|| json!(f.first().copied().unwrap_or(0))),
        Present::Fixed { .. } => types::value_json(dt, f, &NoLookup),
    }
}

/// The fill value 0 of a Zarr data type: `false`, `0.0`, `""`, or a struct's object of its
/// fields' zeros.
fn zero_of(t: &J) -> J {
    match t {
        J::String(s) if s == "bool" => json!(false),
        J::String(s) if s.starts_with("float") => json!(0.0),
        J::String(s) if s == "string" => json!(""),
        J::Object(o) if o.get("name") == Some(&json!("fixed_length_utf32")) => json!(""),
        J::Object(_) => {
            let mut m = Map::new();
            for f in t["configuration"]["fields"].as_array().into_iter().flatten() {
                m.insert(f["name"].as_str().unwrap_or("").to_string(), zero_of(&f["data_type"]));
            }
            J::Object(m)
        }
        _ => json!(0),
    }
}

/// Bytes as lowercase hexadecimal, and back.
pub fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

pub fn unhex(s: &str) -> Vec<u8> {
    (0..s.len() / 2).filter_map(|k| u8::from_str_radix(&s[2 * k..2 * k + 2], 16).ok()).collect()
}

/// An array's Zarr data type, codecs, fill value, and trailing axis (a fixed-length string's
/// bytes) (spec/zarrify/hdf5.md §3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#3-datasets>).
fn array_type(ds: &Dataset, dt: &Dt, p: &Present) -> (J, J, J, Option<u64>) {
    let fill = ds.fill.as_ref().and_then(|f| fill_json(dt, p, f));
    let filters = |size: u64| -> Vec<J> {
        ds.filters
            .iter()
            .map(|f| match f {
                1 => json!({"name": "numcodecs.zlib", "configuration": {"level": 1}}),
                _ => json!({"name": "numcodecs.shuffle", "configuration": {"elementsize": size}}),
            })
            .collect()
    };
    match p {
        Present::Strings | Present::References => (json!("string"), json!([{"name": "vlen-utf8"}]), json!(""), None),
        // the data type and fill value are the records' (`records_of`)
        Present::Records(_) => (J::Null, json!([{"name": "bytes", "configuration": {"endian": "little"}}]), J::Null, None),
        Present::Bytes { n } => {
            let mut codecs = vec![json!({"name": "bytes"})];
            codecs.extend(filters(*n));
            (json!("uint8"), J::Array(codecs), fill.unwrap_or(json!(0)), Some(*n))
        }
        Present::Fixed { data_type, big, one_byte } => {
            let mut codecs = vec![if *one_byte && !data_type.is_object() {
                json!({"name": "bytes"})
            } else {
                json!({"name": "bytes", "configuration": {"endian": if *big { "big" } else { "little" }}})
            }];
            codecs.extend(filters(dt.size));
            let fill = fill.unwrap_or_else(|| zero_of(data_type));
            (data_type.clone(), J::Array(codecs), fill, None)
        }
    }
}

impl crate::Parser for Hdf5 {
    fn new(size: u64) -> Self {
        Hdf5::new(size)
    }
    fn step(&mut self) -> Result<Step, String> {
        Hdf5::step(self)
    }
    fn feed(&mut self, offset: u64, data: Vec<u8>) {
        Hdf5::feed(self, offset, data)
    }
    fn finish(self) -> Result<(Ir, J), String> {
        Hdf5::finish(self)
    }
    fn found(&self) -> Option<&'static str> {
        self.ims.as_ref().map(|_| "ims")
    }
}

/// The HDF5 profile's file run (spec/zarrify/hdf5.md).
pub struct Profile;

impl crate::run::FileProfile for Profile {
    /// The HDF5 signature at offset 0.
    fn detect(&self, head: &[u8]) -> bool {
        head.len() >= 8 && &head[..8] == super::SIGNATURE
    }
    fn parser(&self, size: u64) -> crate::run::AnyParser {
        crate::run::AnyParser::of::<Hdf5>(size)
    }
    fn project(&self, ir: &Ir, facts: &J, url: &str, copied: &crate::project::Copied) -> Result<crate::out::Out, String> {
        crate::project::hdf5::project(ir, facts, url, copied)
    }
    fn reads(&self, ir: &Ir, facts: &J) -> Result<Vec<(u64, u64)>, String> {
        crate::project::hdf5::reads(ir, facts)
    }
}
