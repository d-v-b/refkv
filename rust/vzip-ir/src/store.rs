//! A store run (conventions §8.9
//! <https://github.com/d-v-b/vzip/blob/main/spec/conventions.md#89-stores>): a store format's
//! parser and the object reads it asks for, sans-IO. The host lists the store (S3
//! `ListObjectsV2` or a directory walk, under the reader policy) and gives the listing to
//! [`StoreRun::new`], which builds the IR's directories and objects ([`Ir::listing`]); then
//! the host loops `poll()` (the object reads to make now, concurrently, at most the host's
//! connections at a time), `complete()` (each one's bytes), until `poll()` gives None; then
//! `finish()` or `into_output()`.
//!
//! **Batches.** A parser asks for a batch of reads at a time: (object, offset, length) ranges,
//! a whole document being `(object, 0, size)`. A store format reads its documents in as few
//! batches as its structure allows (a Zarr v2 store's every `.zarray`, `.zgroup` and
//! `.zattrs` in one, since the listing names them all), so the reads of a batch run
//! concurrently, as #37's prefetch of documents does today. Each object of a batch is one
//! request: its ranges, merged when at most 4 KiB apart (amplification at most 4), are its
//! spans, each a range request (a whole object is one). Objects are never merged: each is
//! its own URL.
//!
//! **Held objects.** The run keeps the bytes of every object it read whole; at the end, every
//! object the parser gave children (a held object) takes them ([`Ir::hold_all`]), so the mirror
//! rebuilds it. A held object the run did not read whole is an error of the parser.
//!
//! **Budget.** Requests at most `2^20 + 4 × objects`, bytes at most `2^24 + 2 × Z` (Z the
//! listed sizes' sum); a range outside its object is refused.

use crate::ir::{Ir, Res};
use serde_json::{json, Value as J};
use std::collections::HashMap;

/// A batch of reads a store parser asks for, or done.
pub enum StoreStep {
    /// (object, offset, length): `object` indexes the listing.
    Read(Vec<(usize, u64, u64)>),
    Done,
}

/// A store format's sans-IO parser: it starts from the IR of the listing (`ir`, with
/// `objects[k]` the element of listed key `k`) and adds the held objects' elements.
pub trait StoreParser: Send + Sync {
    fn step(&mut self) -> Res<StoreStep>;
    fn feed(&mut self, object: usize, offset: u64, data: &[u8]);
    /// The empty objects whose keys end in `/` (each with its `/`), which the listing ignores
    /// and does not record (spec/virtualize.md §1.4
    /// <https://github.com/d-v-b/vzip/blob/main/spec/virtualize.md#14-store-inputs>), told before
    /// the first step ([`StoreRun::ignore`]): a SAFE directory's are its directories. The
    /// recorded ignored keys reach the projection in the facts instead.
    fn folders(&mut self, _folders: &[String]) {}
    /// The IR (finished: `check::finish`) and the facts its projection reads.
    fn finish(self: Box<Self>) -> Res<(Ir, J)>;
    /// The profile the store is decided to be, once the parser has decided (a Zarr v2 store
    /// is OME-Zarr's when its root declares OME-NGFF 0.4); None before that.
    fn profile(&self) -> Option<&'static str> {
        None
    }
}

/// The store formats the core reads (the store profiles move onto the IR one by one).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum StoreFormat {
    /// A Zarr v2 store (`zarr2.rs`; spec/virtualize/zarr2.md
    /// <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/zarr2.md>).
    Zarr2,
    /// A Sentinel-2 SAFE product's directory (`safe/`; spec/virtualize/safe.md
    /// <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/safe.md>).
    Safe,
}

impl StoreFormat {
    pub fn from_name(s: &str) -> Option<StoreFormat> {
        match s {
            "zarr2" => Some(StoreFormat::Zarr2),
            "safe" => Some(StoreFormat::Safe),
            _ => None,
        }
    }
    pub fn name(self) -> &'static str {
        match self {
            StoreFormat::Zarr2 => "zarr2",
            StoreFormat::Safe => "safe",
        }
    }
    /// The convention the mirror declares.
    pub fn profile(self) -> &'static str {
        match self {
            StoreFormat::Zarr2 => "zarr2",
            StoreFormat::Safe => "safe",
        }
    }
    fn parser(self, ir: Ir, objects: Vec<u32>, keys: &[(String, u64)]) -> Res<Box<dyn StoreParser>> {
        match self {
            StoreFormat::Zarr2 => Ok(Box::new(crate::zarr2::Zarr2::new(ir, objects, keys)?)),
            StoreFormat::Safe => Ok(Box::new(crate::safe::Safe::new_store(ir, objects, keys))),
        }
    }
}

/// One request: object `object` (its key `key`), the spans (start, end) to read of it.
#[derive(Clone, Debug, PartialEq)]
pub struct ObjectRequest {
    pub object: usize,
    pub key: String,
    pub spans: Vec<(u64, u64)>,
}

#[derive(Default, Debug, Clone)]
pub struct StoreStats {
    pub objects: u64,
    pub batches: u64,
    pub ranges: u64,
    pub asked: u64,
    pub requests: u64,
    pub spans: u64,
    pub bytes: u64,
}

/// The gap (bytes) below which two ranges of one object are one span, and the amplification cap.
const GAP: u64 = 4 << 10;
const CAP: f64 = 4.0;

pub struct StoreRun {
    pub format: StoreFormat,
    keys: Vec<(String, u64)>,
    objects: Vec<u32>,
    parser: Option<Box<dyn StoreParser>>,
    asked: Vec<(usize, u64, u64)>,
    round: Vec<ObjectRequest>,
    got: Vec<Option<Vec<Vec<u8>>>>,
    whole: HashMap<usize, Vec<u8>>,
    read_any: Vec<bool>,
    concurrency: usize,
    reads_limit: u64,
    bytes_limit: u64,
    pub stats: StoreStats,
    done: bool,
    ignored: Vec<String>,
}

impl StoreRun {
    /// A run over the listing `keys` (relative keys, sizes). `concurrency`: the requests the
    /// host makes at a time (it shares a large object's spans among them).
    pub fn new(format: StoreFormat, keys: Vec<(String, u64)>, concurrency: usize) -> Res<StoreRun> {
        Self::with(format, keys, concurrency, &|ir, objects, keys| format.parser(ir, objects, keys))
    }

    /// A run of the parser `make` gives (for the listing's IR, its objects and keys), as `format`.
    #[allow(clippy::type_complexity)]
    pub fn with(format: StoreFormat, keys: Vec<(String, u64)>, concurrency: usize,
                make: &dyn Fn(Ir, Vec<u32>, &[(String, u64)]) -> Res<Box<dyn StoreParser>>) -> Res<StoreRun> {
        let mut ir = Ir::new_store();
        let objects = ir.listing(&keys)?;
        let parser = make(ir, objects.clone(), &keys)?;
        let total: u64 = keys.iter().map(|k| k.1).fold(0u64, |a, b| a.saturating_add(b));
        Ok(StoreRun {
            format,
            read_any: vec![false; keys.len()],
            reads_limit: (1 << 20) + 4 * keys.len() as u64,
            bytes_limit: (1u64 << 24).saturating_add(total.saturating_mul(2)),
            keys,
            objects,
            parser: Some(parser),
            asked: Vec::new(),
            round: Vec::new(),
            got: Vec::new(),
            whole: HashMap::new(),
            concurrency: concurrency.max(1),
            stats: StoreStats::default(),
            done: false,
            ignored: Vec::new(),
        })
    }

    /// The keys the listing ignores (spec/virtualize.md §1.4
    /// <https://github.com/d-v-b/vzip/blob/main/spec/virtualize.md#14-store-inputs>), which are
    /// not objects of the IR, given before the first poll: `keys`, the relative keys of the
    /// ignored objects the store reader recorded, and `folders`, those of the empty objects
    /// whose keys end in `/` (each with its `/`), which are not recorded. The parser is told
    /// the folders before its first step ([`StoreParser::folders`]: a SAFE directory's empty
    /// `d/` objects are its directories); the facts carry the recorded keys (`ignored`, in the
    /// order of their UTF-8 bytes) to the projection, which records them as its convention
    /// says ([`crate::project::store::record_ignored`]).
    pub fn ignore(&mut self, keys: Vec<String>, folders: Vec<String>) -> Res<()> {
        if self.stats.batches > 0 || self.done {
            return Err("internal: the ignored keys given after the first poll".into());
        }
        let p = self.parser.as_mut().ok_or("internal: no parser")?;
        p.folders(&folders);
        self.ignored = keys;
        self.ignored.sort_by(|a, b| a.as_bytes().cmp(b.as_bytes()));
        Ok(())
    }

    /// The profile the run's store is decided to be so far (the format's own until its parser
    /// decides otherwise), which a rejection names: a host asks it after the last poll, before
    /// the output, whose checks are that profile's.
    pub fn profile(&self) -> &'static str {
        self.parser.as_ref().and_then(|p| p.profile()).unwrap_or(self.format.profile())
    }

    /// The requests to make now, concurrently; None when the parser is done.
    pub fn poll(&mut self) -> Res<Option<Vec<ObjectRequest>>> {
        if self.done {
            return Ok(None);
        }
        // a batch is pending until served, even one of empty ranges only (no request)
        if !self.round.is_empty() || !self.asked.is_empty() {
            if self.got.iter().any(|g| g.is_none()) {
                return Err("poll before every request of the batch completed".into());
            }
            self.serve()?;
        }
        let p = self.parser.as_mut().ok_or("internal: no parser")?;
        match p.step()? {
            StoreStep::Done => {
                self.done = true;
                Ok(None)
            }
            StoreStep::Read(ranges) => {
                self.begin(ranges)?;
                Ok(Some(self.round.clone()))
            }
        }
    }

    fn begin(&mut self, ranges: Vec<(usize, u64, u64)>) -> Res<()> {
        if ranges.is_empty() {
            return Err("internal: a batch of no reads".into());
        }
        let mut by: std::collections::BTreeMap<usize, Vec<(u64, u64)>> = Default::default();
        for &(k, o, n) in &ranges {
            let (key, size) = self.keys.get(k).ok_or(format!("internal: a read of object {k}, which the listing has not"))?;
            if o.checked_add(n).is_none_or(|e| e > *size) {
                return Err(format!("{key}: a read of [{o}, {}) outside its {size} bytes", o.saturating_add(n)));
            }
            by.entry(k).or_default().push((o, n));
            self.stats.asked += n;
        }
        self.stats.batches += 1;
        self.stats.ranges += ranges.len() as u64;
        self.asked = ranges;
        self.round.clear();
        for (k, rs) in by {
            let spans = crate::plan::plan_spans(&rs, GAP, CAP, self.concurrency);
            if spans.is_empty() {
                continue;
            }
            if !self.read_any[k] {
                self.read_any[k] = true;
                self.stats.objects += 1;
            }
            self.stats.requests += 1;
            self.stats.spans += spans.len() as u64;
            self.stats.bytes += spans.iter().map(|s| s.1 - s.0).sum::<u64>();
            self.round.push(ObjectRequest { object: k, key: self.keys[k].0.clone(), spans });
        }
        if self.stats.requests > self.reads_limit {
            return Err(format!("budget: more than {} object reads", self.reads_limit));
        }
        if self.stats.bytes > self.bytes_limit {
            return Err(format!("budget: more than {} bytes read", self.bytes_limit));
        }
        self.got = vec![None; self.round.len()];
        Ok(())
    }

    /// The spans of request `k` of the last poll (none: no such request).
    pub fn round_spans(&self, k: usize) -> Vec<(u64, u64)> {
        self.round.get(k).map(|q| q.spans.clone()).unwrap_or_default()
    }

    /// Request `k` of the last poll: each span's bytes.
    pub fn complete(&mut self, k: usize, parts: Vec<Vec<u8>>) -> Res<()> {
        let q = self.round.get(k).ok_or(format!("no request {k}"))?;
        if parts.len() != q.spans.len() || parts.iter().zip(&q.spans).any(|(p, s)| p.len() as u64 != s.1 - s.0) {
            return Err(format!("{}: the bytes given are not those of the spans asked", q.key));
        }
        self.got[k] = Some(parts);
        Ok(())
    }

    /// Feeds the batch's ranges from the spans read, and keeps the objects read whole.
    fn serve(&mut self) -> Res<()> {
        let round = std::mem::take(&mut self.round);
        let got = std::mem::take(&mut self.got);
        let mut spans: HashMap<usize, Vec<((u64, u64), Vec<u8>)>> = HashMap::new();
        for (q, parts) in round.into_iter().zip(got) {
            let size = self.keys[q.object].1;
            let parts = parts.unwrap();
            if q.spans.len() == 1 && q.spans[0] == (0, size) {
                self.whole.insert(q.object, parts[0].clone());
            }
            spans.entry(q.object).or_default().extend(q.spans.into_iter().zip(parts));
        }
        let p = self.parser.as_mut().ok_or("internal: no parser")?;
        for (k, o, n) in std::mem::take(&mut self.asked) {
            if n == 0 {
                p.feed(k, o, &[]);
                continue;
            }
            let (s, b) = spans[&k].iter().find(|(s, _)| s.0 <= o && o + n <= s.1).ok_or("internal: a range no span holds")?;
            p.feed(k, o, &b[(o - s.0) as usize..(o - s.0 + n) as usize]);
        }
        Ok(())
    }

    /// The IR (its held objects holding their bytes), the facts, and the run's counts.
    pub fn finish(mut self) -> Res<(Ir, J, J)> {
        if !self.done {
            return Err("the run is not done".into());
        }
        let p = self.parser.take().ok_or("internal: no parser")?;
        let (mut ir, mut facts) = p.finish()?;
        if let Some(m) = facts.as_object_mut().filter(|_| !self.ignored.is_empty()) {
            m.insert("ignored".into(), json!(self.ignored));
        }
        ir.sort_values();
        let kids = ir.has_children();
        // objects that hold bytes already (the parser's) need no whole read
        let mut want = Vec::new();
        for (k, &o) in self.objects.iter().enumerate() {
            let ou = o as usize;
            if !ir.held(&kids, ou) || ir.value_bytes(o).is_some() {
                continue;
            }
            want.push((k, o));
        }
        let mut held = Vec::with_capacity(want.len());
        for (k, o) in want {
            let b = self.whole.get(&k).ok_or(format!("internal: the held object {:?} was not read whole", self.keys[k].0))?;
            held.push((o, b.as_slice()));
        }
        ir.hold_all(held)?;
        let stats = self.stats_json();
        Ok((ir, facts, stats))
    }

    /// The run's counts: objects read, batches, ranges asked and their bytes, requests, spans
    /// and bytes read, objects listed.
    pub fn stats_json(&self) -> J {
        let s = &self.stats;
        json!({"objects": s.objects, "batches": s.batches, "ranges": s.ranges, "asked": s.asked,
               "requests": s.requests, "spans": s.spans, "bytes": s.bytes, "listed": self.keys.len()})
    }

    /// Finishes and mirrors: the output (the format's projection, and the mirror under
    /// `vzip_source` when `mirror`).
    pub fn into_output(self, url: &str, mirror: bool) -> Res<crate::out::Out> {
        let format = self.format;
        let (ir, facts, stats) = self.finish()?;
        store_output(format, &ir, &facts, stats, url, mirror)
    }
}

/// The output of a store parse: the format's projection, the mirror under `vzip_source` when
/// `mirror`, and a summary: the projection's, with the run's counts, the IR's element count
/// (and, but for SAFE, whose projection counts its objects itself, the format's name and the
/// IR's object count) and what the mirror's table folded. Then the ignored keys (and a SAFE
/// directory's empty directories) beside the mirror (`record_ignored`).
pub fn store_output(format: StoreFormat, ir: &Ir, facts: &J, stats: J, url: &str, mirror: bool) -> Res<crate::out::Out> {
    let mut out = match format {
        StoreFormat::Zarr2 => crate::project::zarr2::project(ir, facts, url)?,
        StoreFormat::Safe => crate::project::safe::project(ir, facts, url)?,
    };
    let mut summary = std::mem::take(&mut out.summary);
    if let Some(m) = summary.as_object_mut() {
        if format != StoreFormat::Safe {
            // the profile: a Zarr v2 store whose root declares OME-NGFF 0.4 is OME-Zarr's
            m.insert("format".into(), json!(facts["profile"].as_str().unwrap_or(format.name())));
        }
        m.insert("reads".into(), stats);
        m.insert("elements".into(), json!(ir.len()));
        if format != StoreFormat::Safe {
            m.insert("objects".into(), json!(ir.objects));
        }
    }
    out.summary = summary;
    let profile = facts["profile"].as_str().unwrap_or(format.profile());
    if mirror {
        let folded = crate::mirror::mirror(ir, &mut out, profile)?;
        out.summary["folded"] = folded.json();
    }
    crate::project::store::record_ignored(&mut out, facts, profile)?;
    Ok(out)
}

/// Runs a store format over objects in memory through a run (tests, tools): `read(k)` gives
/// listed object `k`'s bytes.
pub fn run_store(format: StoreFormat, keys: Vec<(String, u64)>, read: &dyn Fn(usize) -> Vec<u8>) -> Res<(Ir, J, J)> {
    let mut r = StoreRun::new(format, keys, 8)?;
    while let Some(reqs) = r.poll()? {
        for (k, q) in reqs.iter().enumerate() {
            let b = read(q.object);
            r.complete(k, q.spans.iter().map(|&(a, e)| b[a as usize..e as usize].to_vec()).collect())?;
        }
    }
    r.finish()
}
