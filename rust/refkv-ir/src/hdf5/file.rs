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
//! 3. **Data**: the elements of every dataset whose elements the description decodes
//!    (variable-length strings, object references and records: their storage, read and decoded),
//!    the global heap collections that strings name, and the Adler-32 of the chunks deflate
//!    skipped.
//! 4. **Description**: every object of the file as it gives it (spec/zarrify/hdf5.md §8.7
//!    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#87-the-description>), with
//!    none of the hierarchy's decisions: the projection makes them (`crate::project::hdf5_nodes`).
//!
//! `finish` emits the IR as the IMS walk does (spec/zarrify/ims.md §5
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ims.md#5-source-metadata>), every
//! dataset's storage described whatever the room.

use super::types::{self, describe, present, values_json, Dt, Field, FieldKind, Kind, Lookup, Present};
use super::walk::{lenient, visit, Emit, Visit};
use super::{le, Acc, Collection, Dataset, Fail, Index, Layout, Target, CHECKSUM, DEFLATE, FLETCHER32, H5, R, SHUFFLE, UNDEFINED};
use crate::beside::{written_key, Written};
use crate::ir::{Ir, NO_INDEX};
use crate::step::Step;
use crate::project::hdf5_nodes::{chunk_parts, ChunkPart};
use crate::rules::{self, num, Rules, Vars};
use crate::text::{text_json, utf8_or_latin1};
use serde_json::{json, Map, Value as J};
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::sync::Arc as Rc;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Phase {
    Sniff,
    Walk,
    Check,
    Names,
    Data,
    Facts,
    /// an H5J file's channels, each an MP4 file (`crate::h5j`)
    Channels,
    Done,
}

/// The external link type of a link message.
const EXTERNAL: u8 = 64;
/// The external data files message.
const EXTERNAL_FILES: u16 = 0x07;

/// The filters the parser itself undoes, to read strings, references and records.
const READABLE: [u64; 3] = [DEFLATE, SHUFFLE, FLETCHER32];

pub struct Hdf5 {
    /// the keys the file names as siblings, and of them those its external links name
    names: BTreeSet<String>,
    link_keys: BTreeSet<String>,
    /// each dataset's external slots, by its address
    slots: HashMap<u64, Vec<super::ExternalSlot>>,
    /// the siblings a store run found, with their sizes; None in a file run
    found: Option<HashMap<String, u64>>,
    h: H5,
    rules: &'static Rules,
    phase: Phase,
    /// the IMS parser, once the file is found to be an Imaris file
    ims: Option<Box<crate::ims::Ims>>,
    /// the bytes fed while sniffing, which the IMS parser is given
    log: Vec<(u64, Vec<u8>)>,
    /// the file is an H5J file (spec/zarrify/h5j.md): found so by the sniff, or required to be by
    /// a run of the H5J profile; and its channels' parses, once the description is made
    h5j: bool,
    h5j_required: bool,
    /// a parser of a store run, which reads an H5J file as HDF5: the H5J profile reads one file
    h5j_off: bool,
    channels: Option<crate::h5j::Channels>,
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
    /// each chunk deflate skipped, by its dataset's header and grid index: its bytes' Adler-32
    adlers: HashMap<(u64, u128), u32>,
    /// each dataset whose elements or skipped chunks do not read, with why
    unread: HashMap<u64, String>,
    /// the description, once made
    facts: Option<J>,
    /// the walk's room in elements, when a test sets it (else the schema's)
    pub room: Option<u64>,
    /// the strings counted toward the budget before the file's, when a test sets it (so that a
    /// small file reaches the budget); else none
    pub strings_before: u128,
}

impl Hdf5 {
    /// The read rounds the parse took (the reader's replay), which a store run's summary holds.
    pub(crate) fn rounds(&self) -> u64 {
        self.rounds
    }

    pub fn new(size: u64) -> Self {
        let mut h = H5::new(size);
        h.symtab_soft = true;
        Hdf5 {
            h,
            rules: rules::of("hdf5"),
            phase: Phase::Sniff,
            ims: None,
            log: Vec::new(),
            h5j: false,
            h5j_required: false,
            h5j_off: false,
            channels: None,
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
            adlers: HashMap::new(),
            unread: HashMap::new(),
            facts: None,
            room: None,
            strings_before: 0,
            names: BTreeSet::new(),
            link_keys: BTreeSet::new(),
            slots: HashMap::new(),
            found: None,
        }
    }

    /// The keys the file names as siblings, once its run has stepped to `Step::Beside`.
    pub fn sibling_keys(&self) -> &BTreeSet<String> {
        &self.names
    }

    /// The siblings a store run found, by key with their sizes: the parse goes on past
    /// `Step::Beside`, presenting what they let it. A sibling's parser is given none, so that links
    /// are followed one level deep.
    pub fn resolve(&mut self, found: HashMap<String, u64>) {
        self.found = Some(found);
    }

    /// The keys of the files external links name, which a store run reads.
    pub fn link_keys(&self) -> &BTreeSet<String> {
        &self.link_keys
    }

    /// A parser that never finds the file to be an H5J file, as a store run's are (the H5J profile
    /// reads one file, spec/zarrify/h5j.md §8.1
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/h5j.md#81-choosing-the-profile>).
    pub fn not_h5j(&mut self) {
        self.h5j_off = true;
    }

    /// Whether the parser handed the file to the IMS profile.
    pub fn is_ims(&self) -> bool {
        self.ims.is_some()
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
                Phase::Names => self.read_names(),
                Phase::Data => self.data(),
                Phase::Facts => self.make_description(),
                Phase::Channels => self.read_channels(),
                Phase::Done => return Ok(Step::Done),
            };
            match r {
                Ok(()) => {
                    if self.ims.is_some() {
                        continue;
                    }
                    let named = self.phase == Phase::Names;
                    let channels_read = self.phase == Phase::Channels;
                    self.phase = match self.phase {
                        Phase::Sniff => Phase::Walk,
                        Phase::Walk => Phase::Check,
                        Phase::Check => Phase::Names,
                        Phase::Names => Phase::Data,
                        Phase::Data => Phase::Facts,
                        Phase::Facts if self.h5j => Phase::Channels,
                        _ => Phase::Done,
                    };
                    // an H5J file is read alone: the files it names are not read (spec/zarrify/h5j.md
                    // §8.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/h5j.md#81-choosing-the-profile>)
                    if (named || channels_read) && self.found.is_none() && !self.names.is_empty() && !self.h5j {
                        return Ok(Step::Beside);
                    }
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
        if imaris && !self.h5j_required {
            let mut ims = Box::new(crate::ims::Ims::new(self.h.file_size()));
            for (o, d) in std::mem::take(&mut self.log) {
                ims.feed(o, d);
            }
            self.ims = Some(ims);
        } else {
            // an H5J file (spec/zarrify/h5j.md §8.1
            // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/h5j.md#81-choosing-the-profile>)
            self.h5j = !self.h5j_off && crate::h5j::is_h5j(&mut self.h, sb.root)?;
            if self.h5j_required && !self.h5j {
                return super::bad("not an H5J file: the root has no group Channels of links Channel_<n>");
            }
        }
        self.log.clear();
        Ok(())
    }

    /// An H5J file's channels: each one's MP4 file read, its reads moved by its dataset's address
    /// (`crate::h5j::Channels`).
    fn read_channels(&mut self) -> R<()> {
        if self.channels.is_none() {
            let description = self.facts.as_ref().ok_or(Fail::Bad("internal: no description".into()))?;
            let nodes = crate::project::hdf5_nodes::nodes(description).map_err(Fail::Bad)?;
            match crate::h5j::Channels::new(&nodes.nodes) {
                Ok(c) => self.channels = Some(c),
                Err(m) if self.h5j_required => return Err(Fail::Bad(m)),
                // too many channels: a file the sniff chose is the HDF5 profile's (spec/zarrify/h5j.md
                // §8.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/h5j.md#83-checks>)
                Err(_) => {
                    self.h5j = false;
                    return Ok(());
                }
            }
        }
        let channels = self.channels.as_mut().expect("the channels");
        if !channels.advance(&self.h.src.store, &mut self.h.src.want) {
            return Err(Fail::Miss);
        }
        // with no channel to present, a file the sniff chose is the HDF5 profile's; one named as the
        // H5J profile's input is rejected (spec/zarrify/h5j.md §8.3
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/h5j.md#83-checks>)
        if let Some(m) = channels.none_presented() {
            if self.h5j_required {
                return Err(Fail::Bad(m));
            }
            self.h5j = false;
            self.channels = None;
        }
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

    /// The keys the file names, and each dataset's external slots (spec/zarrify/hdf5.md §8.6
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#86-external-files>): a name
    /// the steps of a written name reject stays recorded and names no key.
    fn read_names(&mut self) -> R<()> {
        let mut acc = Acc::default();
        for (at, _) in self.order.clone() {
            let r = (|| -> R<()> {
                let h = self.h.header(at)?;
                if h.has(super::SYMBOL_TABLE) || h.has(super::LINK_INFO) {
                    let Some(links) = lenient(self.h.links(at))? else { return Ok(()) };
                    for (_, t) in &links.list {
                        if let Target::Other(EXTERNAL, Some(v)) = t {
                            let file = v.get(1..).and_then(|b| b.split(|&c| c == 0).next()).unwrap_or(&[]);
                            let key = written_key(file, "", Written::default());
                            self.link_keys.extend(key.clone());
                            self.names.extend(key);
                        }
                    }
                } else if h.has(EXTERNAL_FILES) {
                    let Some(slots) = lenient(self.h.external_slots(&h))? else { return Ok(()) };
                    self.names.extend(slots.iter().filter_map(|s| written_key(&s.name, "", Written::default())));
                    self.slots.insert(at, slots);
                }
                Ok(())
            })();
            acc.take(r);
            if acc.stopped() {
                break;
            }
        }
        acc.done()
    }

    /// The ranges of the contiguous data of `total` bytes of the dataset at `at` in its external
    /// files, as (key, offset, length), in the data's order; None unless a store run found every
    /// file the data lies in and each range lies within its file.
    fn external_ranges(&self, at: u64, total: u128) -> Option<Vec<(String, u64, u64)>> {
        super::stored::external_ranges(self.slots.get(&at)?, total, self.found.as_ref()?)
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
                        b = match *f {
                            DEFLATE => miniz_oxide::inflate::decompress_to_vec_zlib_with_limit(&b, cb).map_err(|e| Fail::Bad(format!("a chunk does not inflate: {e:?}")))?,
                            FLETCHER32 => b[..b.len().saturating_sub(CHECKSUM as usize)].to_vec(),
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

    /// The Adler-32 of each chunk of the dataset at `at` that deflate skipped, which the literal
    /// around it needs (spec/zarrify/hdf5.md §3.2
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#32-chunks>).
    fn read_skipped(&mut self, at: u64, ds: &Dataset, size: u64) -> R<()> {
        if ds.layout != Layout::Chunked {
            return Ok(());
        }
        let ch = self.h.chunks(at, ds, true)?;
        let bytes = super::product(&ds.chunk).saturating_mul(size as u128);
        let Some(parts) = chunk_parts(&ds.filters, bytes, ch.list.iter().map(|c| (c.2, c.3)), self.limit("max_storage_bytes")) else { return Ok(()) };
        let mut acc = Acc::default();
        for (c, part) in ch.list.iter().zip(parts) {
            let Some(ChunkPart { stored, wrap: Some(DEFLATE) }) = part else { continue };
            let idx = super::ravel(&c.0, &ds.grid);
            if self.adlers.contains_key(&(at, idx)) {
                continue;
            }
            if let Some(b) = acc.take(self.h.read(c.1, stored)) {
                self.adlers.insert((at, idx), adler32(&b));
            }
        }
        acc.done()
    }

    fn data(&mut self) -> R<()> {
        let mut acc = Acc::default();
        // the strings decoded so far: a dataset past the budget is not read (spec/zarrify/hdf5.md
        // §8.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#83-checks>)
        let mut strings = self.strings_before;
        for (at, _) in self.order.clone() {
            let r = (|| -> R<()> {
                let h = self.h.header(at)?;
                if h.has(super::SYMBOL_TABLE) || h.has(super::LINK_INFO) || !h.has(super::LAYOUT) {
                    return Ok(());
                }
                let ds = self.h.dataset(at, true)?;
                let dt = types::parse(&self.h.datatype(&h)?.0)?;
                let p = decodable(h.has(EXTERNAL_FILES), &ds, &dt);
                // every string dataset counts, in every round, whether it was read in an earlier
                // one or not: the budget passes at the same dataset whatever the rounds
                if p == Some(Present::Strings) {
                    strings = strings.saturating_add(super::product(&ds.dims));
                }
                // a dataset read, or found not to read, in an earlier round is done
                if self.storage.contains_key(&at) || self.unread.contains_key(&at) {
                    return Ok(());
                }
                let Some(p) = p else { return self.read_skipped(at, &ds, dt.size) };
                if p == Present::Strings {
                    self.check("strings", rules::env([("strings", num(strings as f64))]))?;
                }
                let b = self.elements(at, &ds, dt.size)?;
                if let Present::Records(_) = p {
                    self.records.insert(at, dt.clone());
                }
                if p == Present::Strings {
                    self.strings.insert(at);
                }
                self.storage.insert(at, b);
                Ok(())
            })();
            // a dataset whose elements do not read is described so; the hierarchy rejects the
            // input if it presents the dataset
            match r {
                Err(Fail::Bad(m)) => {
                    self.unread.insert(at, m);
                }
                r => {
                    acc.take(r);
                }
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
            // a collection that does not read is none: a dataset's string that names it is
            // described so, an attribute's string not decoded
            if let Some(Some(col)) = acc.take(lenient(self.h.collection(c))) {
                self.heaps.insert(c, col);
            }
            if acc.stopped() {
                break;
            }
        }
        acc.done()
    }

    // ---- phase 4: the description (spec/zarrify/hdf5.md §8.7
    // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#87-the-description>)

    /// The attributes of an object, in ascending byte order of their names: each one's name, and
    /// its datatype, dimensions and value, or that it is shared, or why it does not read.
    fn attributes_description(&self, attrs: &super::Attrs) -> J {
        let mut list: Vec<&super::Attr> = attrs.list.iter().collect();
        list.sort_by(|a, b| a.name.cmp(&b.name));
        let mut out = Vec::with_capacity(list.len());
        for a in list {
            let name = utf8_or_latin1(&a.name);
            let mut e = Map::new();
            e.insert("name".into(), json!(name));
            let r = (|| -> R<()> {
                let parts = super::attribute_parts_at(&a.data)?;
                if parts.flags & 3 != 0 {
                    e.insert("shared".into(), json!(true));
                    return Ok(());
                }
                let d = &a.data;
                let t = &d[parts.datatype.0 as usize..(parts.datatype.0 + parts.datatype.1) as usize];
                let dt = types::parse(t)?;
                e.insert("datatype".into(), describe(&dt));
                e.insert("type".into(), json!(hex(t)));
                let space = super::parse_dataspace(&d[parts.dataspace.0 as usize..(parts.dataspace.0 + parts.dataspace.1) as usize])?;
                let Some(dims) = space.dims else {
                    e.insert("dims".into(), J::Null);
                    e.insert("value".into(), J::Null);
                    return Ok(());
                };
                let n = super::product(&dims).saturating_mul(dt.size as u128);
                if parts.data as u128 + n > d.len() as u128 {
                    return super::bad(format!("attribute {name}: its data runs past its message"));
                }
                let data = &d[parts.data as usize..(parts.data as u128 + n) as usize];
                e.insert("dims".into(), json!(dims));
                if super::product(&dims) == 1 {
                    e.insert("data".into(), json!(hex(data)));
                }
                if let Some(v) = values_json(&dt, &dims, data, self) {
                    e.insert("value".into(), v);
                }
                Ok(())
            })();
            if let Err(f) = r {
                let m = match f {
                    Fail::Bad(m) => m,
                    Fail::Miss => "internal: an attribute's message is not read".into(),
                };
                e = Map::new();
                e.insert("name".into(), json!(name));
                e.insert("error".into(), json!(m));
            }
            out.push(J::Object(e));
        }
        J::Array(out)
    }

    /// An object of the file as the description gives it: its address, the segments of its
    /// canonical path, its kind and attributes; a group's links; a dataset's datatype, dataspace,
    /// layout, filters, fill value, storage, dimension scale structures and decoded elements.
    fn object_json(&mut self, at: u64, segs: &[Vec<u8>]) -> R<J> {
        let h = self.h.header(at)?;
        let group = h.has(super::SYMBOL_TABLE) || h.has(super::LINK_INFO);
        let kind = if group {
            "group"
        } else if h.has(super::LAYOUT) {
            "dataset"
        } else if h.has(super::DATATYPE) {
            "named_datatype"
        } else {
            "object"
        };
        let mut o = Map::new();
        o.insert("address".into(), json!(at));
        o.insert("segments".into(), J::Array(segs.iter().map(|s| text_json(s)).collect()));
        o.insert("kind".into(), json!(kind));
        let attrs = self.h.attributes(at)?;
        o.insert("attributes".into(), self.attributes_description(&attrs));
        if group {
            let links = self.h.links(at)?;
            let mut list: Vec<&(Vec<u8>, Target)> = links.list.iter().collect();
            list.sort_by(|a, b| a.0.cmp(&b.0));
            let links_json: Vec<J> = list
                .into_iter()
                .map(|(n, t)| match t {
                    Target::Soft(p) => json!({"name": text_json(n), "soft": utf8_or_latin1(p)}),
                    Target::Hard(a) => json!({"name": text_json(n), "hard": a}),
                    Target::Other(kind, value) => {
                        let (file, path) = match (kind, value) {
                            (&EXTERNAL, Some(v)) => external_value(v),
                            _ => (J::Null, J::Null),
                        };
                        json!({"name": text_json(n), "type": kind, "file": file, "path": path})
                    }
                })
                .collect();
            o.insert("links".into(), J::Array(links_json));
            if let Some(created) = &links.created {
                o.insert("link_order".into(), J::Object(created.iter().map(|(n, c)| (utf8_or_latin1(n), json!(c))).collect()));
            }
        } else if kind == "dataset" {
            // its dimensions, from the dataspace message, whatever else of it reads
            let space = h.message(super::DATASPACE, true).and_then(|m| super::parse_dataspace(m.unwrap_or_default()));
            if let Some(dims) = lenient(space)?.and_then(|s| s.dims) {
                o.insert("dims".into(), json!(dims));
            }
            match self.dataset_json(at, &h, &attrs) {
                Ok(d) => o.extend(d),
                Err(Fail::Bad(m)) => {
                    o.insert("error".into(), json!(m));
                }
                Err(Fail::Miss) => return Err(Fail::Miss),
            }
            if let Some(m) = self.unread.get(&at) {
                o.insert("unread".into(), json!(m));
            }
        }
        Ok(J::Object(o))
    }

    /// What the description gives of the dataset at `at` beyond its attributes.
    fn dataset_json(&mut self, at: u64, h: &super::Header, attrs: &super::Attrs) -> R<Map<String, J>> {
        let base = self.h.src.base;
        let ds = self.h.dataset(at, true)?;
        let (type_message, _) = self.h.datatype(h)?;
        let dt = types::parse(&type_message)?;
        let mut d = Map::new();
        d.insert("datatype".into(), describe(&dt));
        d.insert("type".into(), json!(hex(&type_message)));
        d.insert("dims".into(), json!(ds.dims));
        if let Some(m) = &ds.maxdims {
            d.insert("maxdims".into(), J::Array(m.iter().map(|&v| if v == UNDEFINED { J::Null } else { json!(v) }).collect()));
        }
        d.insert("layout".into(), json!(match ds.layout {
            Layout::Compact => "compact",
            Layout::Contiguous => "contiguous",
            Layout::Chunked => "chunked",
            Layout::Null => "null",
            Layout::Virtual => "virtual",
        }));
        d.insert("filters".into(), J::Array(ds.filters.iter().zip(&ds.client_data).map(|(f, cd)| json!({"id": f, "values": cd})).collect()));
        if let Some(f) = &ds.fill {
            d.insert("fill".into(), json!(hex(f)));
        }
        if h.has(EXTERNAL_FILES) {
            let total = super::product(&ds.dims).saturating_mul(dt.size as u128);
            let ranges = self.external_ranges(at, total).map(|r| J::Array(r.iter().map(|r| json!([r.0, r.1, r.2])).collect()));
            d.insert("external".into(), ranges.unwrap_or(J::Null));
        }
        let storage = match ds.layout {
            Layout::Compact => json!({"offset": base + ds.compact.0, "length": ds.compact.1.len()}),
            Layout::Contiguous => {
                let (address, length) = ds.contiguous;
                let mut s = json!({"address": address, "length": length});
                if let Some(a) = address {
                    s["offset"] = json!(base + a);
                    if a.checked_add(length).is_none_or(|e| e > self.h.size()) {
                        s["outside"] = json!(true);
                    }
                }
                s
            }
            Layout::Chunked => {
                d.insert("chunk".into(), json!(ds.chunk));
                d.insert("grid".into(), json!(ds.grid));
                d.insert("index".into(), json!(match ds.index {
                    Some(Index::Btree1) => "btree1",
                    Some(Index::Single) => "single",
                    Some(Index::Implicit) => "implicit",
                    Some(Index::Farray) => "fixed_array",
                    Some(Index::Earray) => "extensible_array",
                    Some(Index::Btree2) => "btree2",
                    None => "none",
                }));
                d.insert("raw_edges".into(), json!(ds.raw_edges));
                let ch = self.h.chunks(at, &ds, true)?;
                let shortest = ch.list.iter().filter(|c| c.3 == 0).map(|c| c.2).min();
                let masked: Vec<J> = ch
                    .list
                    .iter()
                    .filter(|c| c.3 != 0)
                    .map(|c| {
                        let idx = super::ravel(&c.0, &ds.grid);
                        let mut m = json!({"index": idx as u64, "size": c.2, "mask": c.3});
                        if let Some(adler) = self.adlers.get(&(at, idx)) {
                            m["adler"] = json!(adler);
                        }
                        m
                    })
                    .collect();
                json!({"count": ch.list.len(), "shortest": shortest, "masked": masked})
            }
            Layout::Null | Layout::Virtual => json!({}),
        };
        d.insert("storage".into(), storage);
        // the dimension scale structures (HDF5's Dimension Scales): the dataset's `CLASS` and
        // `NAME` as fixed-length strings, and what its `DIMENSION_LIST` attaches to each axis
        let mut scale = Map::new();
        for name in ["CLASS", "NAME"] {
            if let Some(t) = fixed_attribute(attrs, name.as_bytes()) {
                scale.insert(name.into(), json!(utf8_or_latin1(&t)));
            }
        }
        if !scale.is_empty() {
            d.insert("dimension_scale".into(), J::Object(scale));
        }
        if let Some(a) = attrs.list.iter().find(|a| a.name == b"DIMENSION_LIST") {
            d.insert("dimension_list".into(), self.dimension_list(&a.data, ds.dims.len()));
        }
        if let Some(b) = self.storage.get(&at) {
            let values = match present(&dt) {
                Ok(Present::Strings) => self.string_spans(at, b),
                Ok(Present::Records(fields)) => match self.record_texts(at, &fields, b, dt.size) {
                    Ok(texts) => json!({"elements": hex(b), "texts": texts}),
                    Err(Fail::Bad(m)) => json!({"invalid": m}),
                    Err(Fail::Miss) => return Err(Fail::Miss),
                },
                _ => {
                    let paths: Vec<J> = (0..b.len() / 8).map(|k| json!(self.paths.get(&le(b, 8 * k, 8).unwrap_or(UNDEFINED)).cloned().unwrap_or_default())).collect();
                    json!({"paths": paths})
                }
            };
            d.insert("values".into(), values);
        }
        Ok(d)
    }

    /// A `DIMENSION_LIST` attribute's message `data` of a dataset of `rank` axes: the count of
    /// scales it attaches to each entry (none when its data is not whole entries), and the scales
    /// of each axis by their addresses (none when it is not a list of object references of one
    /// entry an axis), an axis whose scales do not read none; or that it does not read.
    fn dimension_list(&self, data: &[u8], rank: usize) -> J {
        let Ok((t, dims, data)) = super::attribute_parts(data) else { return json!({"unread": true}) };
        let counts = (data.len() % 16 == 0).then(|| data.chunks(16).map(|e| u32::from_le_bytes([e[0], e[1], e[2], e[3]])).collect::<Vec<_>>());
        let attached = types::parse(&t).is_ok_and(|dt| {
            dt.size == 16 && matches!(&dt.kind, Kind::Vlen { string: false, base, .. } if matches!(base.kind, Kind::Reference { kind: 0 }))
        });
        let scales = (attached && dims == [rank as u64] && data.len() == 16 * rank).then(|| {
            (0..rank)
                .map(|k| {
                    let (n, c, i) = (le(&data, 16 * k, 4).unwrap_or(0), le(&data, 16 * k + 4, 8).unwrap_or(0), le(&data, 16 * k + 12, 4).unwrap_or(0));
                    if n == 0 {
                        return json!([]);
                    }
                    match self.heap(c, i) {
                        Some(b) => J::Array(b.chunks_exact(8).map(|a| json!(u64::from_le_bytes(a.try_into().expect("8 bytes")))).collect()),
                        None => J::Null,
                    }
                })
                .collect::<Vec<_>>()
        });
        json!({"counts": counts, "scales": scales})
    }

    /// The strings of a dataset of variable-length strings (its elements' bytes `b`): each one's
    /// (offset, length) in the file, (0, 0) for an empty one, and their count; or why one does not
    /// read.
    fn string_spans(&self, at: u64, b: &[u8]) -> J {
        let base = self.h.src.base;
        let r = (|| -> R<Vec<J>> {
            let mut spans = Vec::with_capacity(b.len() / 8);
            for k in 0..b.len() / 16 {
                let (n, c, i) = (le(b, 16 * k, 4)?, le(b, 16 * k + 4, 8)?, le(b, 16 * k + 12, 4)?);
                let (o, len) = if n == 0 || c == 0 || c == UNDEFINED {
                    (0, 0)
                } else {
                    let col = self.heaps.get(&c).ok_or(Fail::Bad(format!("{}: element {k} names no global heap collection", self.path(at))))?;
                    let &(o, m) = col.objects.get(&i).ok_or(Fail::Bad(format!("{}: element {k} names no object of its collection", self.path(at))))?;
                    if m != n {
                        return super::bad(format!("{}: element {k} is {n} bytes long, its heap object {m}", self.path(at)));
                    }
                    (base + o + 16, n)
                };
                spans.push(json!(o));
                spans.push(json!(len));
            }
            Ok(spans)
        })();
        match r {
            Ok(spans) => json!({"spans": spans, "count": b.len() / 16}),
            Err(Fail::Bad(m)) => json!({"invalid": m}),
            Err(Fail::Miss) => json!({"invalid": "internal: a string's heap is not read"}),
        }
    }

    /// The texts of each string and reference member of a dataset of records (its elements' bytes
    /// `b`, `size` bytes each), by member in the order `fields` gives them, a fixed-size member's
    /// none: a string's text, a reference's path.
    fn record_texts(&self, at: u64, fields: &[Field], b: &[u8], size: u64) -> R<J> {
        let size = size as usize;
        let count = if size == 0 { 0 } else { b.len() / size };
        let mut out = Vec::with_capacity(fields.len());
        for (j, f) in fields.iter().enumerate() {
            let mut texts = Vec::new();
            if !matches!(f.kind, FieldKind::Fixed { .. }) {
                for k in 0..count {
                    let v = &b[k * size + f.offset as usize..];
                    let text = match f.kind {
                        FieldKind::Reference => self.paths.get(&le(v, 0, 8)?).cloned().unwrap_or_default(),
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
                    texts.push(json!(text));
                }
            }
            out.push(J::Array(texts));
        }
        Ok(J::Array(out))
    }

    fn make_description(&mut self) -> R<()> {
        let sb = self.h.superblock()?;
        let mut objects = Vec::with_capacity(self.order.len());
        for (at, segs) in self.order.clone() {
            objects.push(self.object_json(at, &segs)?);
        }
        self.facts = Some(json!({"superblock": sb.version, "user_block": self.h.src.base, "objects": objects}));
        Ok(())
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
        // every dataset's storage is described, whatever the room: an output may present any of them
        let must: HashSet<u64> = facts["objects"]
            .as_array()
            .into_iter()
            .flatten()
            .filter(|o| o["kind"] == "dataset")
            .filter_map(|o| o["address"].as_u64())
            .collect();
        let room = self.room.unwrap_or(self.limit("walk_elements") + size / self.limit("walk_bytes_per_element"));
        let inner = self.channels.as_ref().map(|c| c.inner()).unwrap_or_default();
        let mut e = Emit { ir: Ir::new(self.h.file_size()), h: &self.h, chunks_of: HashMap::new(), data_of: HashMap::new(), room, must, rec: Default::default(), inner, grafted: HashMap::new() };
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
        // the datasets first, whatever the room, then the other objects while it lasts
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
        // each dataset's storage element: the struct of its chunks, or its contiguous data
        for o in facts["objects"].as_array_mut().into_iter().flatten() {
            let Some(at) = o["address"].as_u64().filter(|_| o["kind"] == "dataset") else { continue };
            let el = match o["layout"].as_str() {
                Some("chunked") => e.chunks_of.get(&at),
                _ => e.data_of.get(&at),
            };
            if let (Some(&el), Some(storage)) = (el, o.get_mut("storage")) {
                storage["element"] = json!(el);
            }
        }
        let super::walk::Emit { mut ir, rec, data_of, grafted, .. } = e;
        ir.sort_values();
        crate::check::finish(&mut ir)?;
        let em = super::stored::Emitted { order: &order, structs: &structs, rec: &rec, data_of: &data_of };
        let grafts = self.channels.as_ref().map(|c| c.grafts(&grafted)).unwrap_or_default();
        super::stored::describe(&mut ir, &mut self.h, &em, &self.visited, &self.slots, self.found.as_ref(), &grafts)?;
        if let Some(c) = &self.channels {
            facts["h5j"] = c.description(&grafted)?;
        }
        facts["requests"] = json!(self.requests);
        facts["requested"] = json!(self.requested);
        facts["rounds"] = json!(self.rounds);
        Ok((ir, facts))
    }
}

impl Lookup for Hdf5 {
    fn heap(&self, collection: u64, index: u64) -> Option<Vec<u8>> {
        let col = self.heaps.get(&collection)?;
        let &(o, n) = col.objects.get(&index)?;
        self.h.src.get(o + 16, n).map(|b| b.to_vec())
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
            Kind::Vlen { base: of, .. } if matches!(of.kind, Kind::Reference { kind: 0 }) => offsets.push(base),
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

/// The bytes, to their first NUL, of the object's fixed-length string attribute `name`.
fn fixed_attribute(attrs: &super::Attrs, name: &[u8]) -> Option<Vec<u8>> {
    let a = attrs.list.iter().find(|a| a.name == name)?;
    let (t, _, data) = super::attribute_parts(&a.data).ok()?;
    let dt = types::parse(&t).ok()?;
    if !matches!(dt.kind, Kind::Str { .. }) {
        return None;
    }
    Some(data.split(|&b| b == 0).next().unwrap_or(&[]).to_vec())
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

/// RFC 1950's Adler-32.
fn adler32(b: &[u8]) -> u32 {
    let (mut a, mut c) = (1u32, 0u32);
    for run in b.chunks(5552) {
        for &x in run {
            a += x as u32;
            c += a;
        }
        a %= 65521;
        c %= 65521;
    }
    c << 16 | a
}

/// Bytes as lowercase hexadecimal, and back.
pub fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

pub fn unhex(s: &str) -> Vec<u8> {
    (0..s.len() / 2).filter_map(|k| u8::from_str_radix(&s[2 * k..2 * k + 2], 16).ok()).collect()
}

/// How a dataset whose elements the description decodes is presented (spec/zarrify/hdf5.md §8.7
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#87-the-description>): one of
/// variable-length strings, object references or records, of no external files, held in as many
/// bytes as its elements, compact, contiguous, or chunked with only the filters the parser undoes
/// (Fletcher32 last) and its edge chunks whole; else None.
fn decodable(external: bool, ds: &Dataset, dt: &Dt) -> Option<Present> {
    let p = present(dt).ok().filter(|p| matches!(p, Present::Strings | Present::References | Present::Records(_)))?;
    let bytes = super::product(&ds.dims).saturating_mul(dt.size as u128);
    let k = ds.filters.len();
    let held = match ds.layout {
        Layout::Compact => ds.compact.1.len() as u128 == bytes,
        Layout::Contiguous => ds.contiguous.0.is_none() || ds.contiguous.1 as u128 == bytes,
        Layout::Chunked => !ds.raw_edges && ds.filters.iter().enumerate().all(|(i, &f)| READABLE.contains(&f) && (f != FLETCHER32 || i + 1 == k)),
        _ => false,
    };
    (!external && held).then_some(p)
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
        if self.ims.is_some() {
            Some("ims")
        } else if self.h5j {
            Some("h5j")
        } else {
            None
        }
    }
}

/// The HDF5 parser of a run of the H5J profile, which requires the file to be an H5J file
/// (spec/zarrify/h5j.md <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/h5j.md>).
pub struct H5jParser(Hdf5);

impl crate::Parser for H5jParser {
    fn new(size: u64) -> Self {
        let mut h = Hdf5::new(size);
        h.h5j_required = true;
        H5jParser(h)
    }
    fn step(&mut self) -> Result<Step, String> {
        self.0.step()
    }
    fn feed(&mut self, offset: u64, data: Vec<u8>) {
        self.0.feed(offset, data)
    }
    fn finish(self) -> Result<(Ir, J), String> {
        self.0.finish()
    }
    fn found(&self) -> Option<&'static str> {
        crate::Parser::found(&self.0)
    }
}

/// The HDF5 profile's file run (spec/zarrify/hdf5.md).
pub struct Profile;

impl crate::run::FileProfile for Profile {
    /// The HDF5 signature at offset 0.
    fn detect(&self, head: &[u8]) -> bool {
        head.len() >= 8 && &head[..8] == super::SIGNATURE
    }
    /// A superblock after a user block.
    fn detect_late(&self, head: &[u8]) -> bool {
        super::user_block(head).is_some()
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
    fn described(&self) -> bool {
        true
    }
}
