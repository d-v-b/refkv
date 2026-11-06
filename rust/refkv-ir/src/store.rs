//! A store run (conventions §8.9
//! <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#89-stores>): a store format's
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
//! concurrently, as #37's prefetch of documents does today. An object's ranges of a batch, merged
//! when at most 4 KiB apart (amplification at most 4), are its spans, each a range request (a
//! whole object is one), in requests of at most 16 spans, which the host makes concurrently, so
//! that far apart ranges of one object (a GRIB file's message heads) are read at once. Objects
//! are never merged: each is its own URL.
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

/// The most spans of one object a request holds (the hosts read a request's spans in turn, and
/// its requests concurrently).
const SPANS_PER_REQUEST: usize = 16;

/// A batch of reads a store parser asks for, or done.
pub enum StoreStep {
    /// (object, offset, length): `object` indexes the listing.
    Read(Vec<(usize, u64, u64)>),
    /// Keys the parser needs to know about, which the listing does not have (a VSI input's
    /// tile files, spec/zarrify/vsi.md §9.2
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#92-the-tile-files>): the
    /// host tells which exist, and their sizes ([`StoreRun::found`]), each found one becoming an
    /// object of the run and of the IR ([`StoreParser::found`]).
    Probe(Vec<String>),
    Done,
}

/// A store format's sans-IO parser: it starts from the IR of the listing (`ir`, with
/// `objects[k]` the element of listed key `k`) and adds the held objects' elements.
pub trait StoreParser: Send + Sync {
    fn step(&mut self) -> Res<StoreStep>;
    fn feed(&mut self, object: usize, offset: u64, data: &[u8]);
    /// The empty objects whose keys end in `/` (each with its `/`), which the listing ignores
    /// and does not record (spec/zarrify.md §1.4
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>), told before
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
    /// A probed key (`StoreStep::Probe`), in the order asked: `index` is its listing index when
    /// it exists (`size` its size), and the parser adds it to the IR as an object, whose element
    /// it returns; None for a key that does not exist.
    fn found(&mut self, key: &str, index: usize, size: Option<u64>) -> Res<Option<u32>> {
        let _ = (key, index, size);
        Err("internal: this store format probes no keys".into())
    }
    /// The host found the probed key `written` at the key `key` (spec/zarrify.md §1.9
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#19-sources-beside-the-input>,
    /// **Case**), told before its [`StoreParser::found`]: `found`'s key is `key`, and is not called
    /// when that key is an object already.
    fn resolved(&mut self, written: &str, key: &str) -> Res<()> {
        let _ = (written, key);
        Err("internal: this store format's keys are found as written".into())
    }
    /// Whether the probed key `key` is a sibling whose absence the profile reads as the named
    /// file having none (GRIB's index files): a run that refuses siblings
    /// ([`StoreRun::refuse_siblings`]) answers such a probe absent, and rejects any other.
    fn optional(&self, key: &str) -> bool {
        let _ = key;
        false
    }
}

// `StoreFormat`: one variant per profile of the registry with a store run (profiles/; build.rs)
include!(concat!(env!("OUT_DIR"), "/store_formats.rs"));

impl StoreFormat {
    /// The convention the mirror declares.
    pub fn profile(self) -> &'static str {
        self.name()
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
    /// the keys the parser asked the host to probe, not yet answered (`found`)
    probes: Vec<String>,
    answered: usize,
    /// the keys probed so far, at most `beside::MAX_FILES`
    probed: u64,
    /// the keys of the objects so far
    known: std::collections::HashSet<String>,
    /// whether the host is asked to probe (false: [`StoreRun::refuse_siblings`])
    siblings: bool,
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
            known: keys.iter().map(|k| k.0.clone()).collect(),
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
            probes: Vec::new(),
            answered: 0,
            probed: 0,
            siblings: true,
        })
    }

    /// Refuses every sibling (spec/zarrify.md §1.9
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#19-sources-beside-the-input>,
    /// **Refusing siblings**), before the first poll: the host is asked to probe no key, so the run
    /// reads nothing but the listing's objects. A probe of a sibling the profile calls optional
    /// ([`StoreParser::optional`]) is answered absent by the run itself, and any other rejects the
    /// input, `siblings refused: the input names the file "<key>"`.
    pub fn refuse_siblings(&mut self) -> Res<()> {
        if self.stats.batches > 0 || self.done {
            return Err("internal: siblings refused after the first poll".into());
        }
        self.siblings = false;
        Ok(())
    }

    /// The keys the listing ignores (spec/zarrify.md §1.4
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>), which are
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

    /// The requests to make now, concurrently; None when the parser is done. An empty list is
    /// a batch of probes ([`StoreRun::probes`]): the host answers each with [`StoreRun::found`],
    /// then polls again.
    pub fn poll(&mut self) -> Res<Option<Vec<ObjectRequest>>> {
        loop {
            let got = self.step()?;
            // a batch of probes the run answered itself, siblings being refused: the next step
            if self.siblings || got.as_ref().is_none_or(|g| !g.is_empty()) {
                return Ok(got);
            }
            let p = self.parser.as_ref().ok_or("internal: no parser")?;
            if let Some(k) = self.probes.iter().find(|k| !p.optional(k)) {
                return Err(format!("siblings refused: the input names the file \"{k}\""));
            }
            for _ in 0..self.probes.len() {
                self.found(None)?;
            }
        }
    }

    /// One step of [`StoreRun::poll`].
    fn step(&mut self) -> Res<Option<Vec<ObjectRequest>>> {
        if self.done {
            return Ok(None);
        }
        if self.answered < self.probes.len() {
            return Err("poll before every probe of the batch was answered".into());
        }
        self.probes.clear();
        self.answered = 0;
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
            StoreStep::Probe(keys) => {
                if keys.is_empty() {
                    return Err("internal: a batch of no probes".into());
                }
                let mut seen: std::collections::HashSet<&str> = self.keys.iter().map(|k| k.0.as_str()).collect();
                for k in &keys {
                    if k.is_empty() || k.split('/').any(|s| s.is_empty() || s == "." || s == "..") || !seen.insert(k.as_str()) {
                        return Err(format!("internal: the probed key {k:?} is listed already, or is not a key"));
                    }
                }
                self.probed += keys.len() as u64;
                if self.probed > crate::beside::MAX_FILES {
                    return Err(format!("budget: the input names more than {} files", crate::beside::MAX_FILES));
                }
                self.stats.batches += 1;
                self.probes = keys;
                self.answered = 0;
                Ok(Some(Vec::new()))
            }
        }
    }

    /// The keys of the pending batch of probes, in the order the host answers them.
    pub fn probes(&self) -> &[String] {
        &self.probes
    }

    /// The answer to the next probe of the batch (in order): the key's size when it exists,
    /// None when it does not. A found key becomes an object of the run, its reads budgeted
    /// like a listed one's.
    pub fn found(&mut self, size: Option<u64>) -> Res<()> {
        self.answer(size, None)
    }

    /// The answer to the next probe of the batch when the host found it at another key, by listing
    /// (spec/zarrify.md §1.9
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#19-sources-beside-the-input>,
    /// **Case**): `key` is the object's key, which may be an object of the run already.
    pub fn found_as(&mut self, size: u64, key: &str) -> Res<()> {
        if key.is_empty() || key.split('/').any(|s| s.is_empty() || s == "." || s == "..") {
            return Err(format!("internal: the key {key:?} a probe was found at is not a key"));
        }
        self.answer(Some(size), Some(key))
    }

    fn answer(&mut self, size: Option<u64>, at: Option<&str>) -> Res<()> {
        let k = self.answered;
        if k >= self.probes.len() {
            return Err("a probe answered that was not asked".into());
        }
        let written = self.probes[k].clone();
        self.answered += 1;
        let key = at.map_or_else(|| written.clone(), str::to_string);
        let index = self.keys.len();
        let p = self.parser.as_mut().ok_or("internal: no parser")?;
        if key != written {
            p.resolved(&written, &key)?;
        }
        if size.is_some() && self.known.contains(&key) {
            return Ok(());
        }
        let e = p.found(&key, index, size)?;
        if let (Some(size), Some(e)) = (size, e) {
            self.known.insert(key.clone());
            self.keys.push((key, size));
            self.objects.push(e);
            self.read_any.push(false);
            self.reads_limit += 4;
            self.bytes_limit = self.bytes_limit.saturating_add(size.saturating_mul(2));
        }
        Ok(())
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
            self.stats.spans += spans.len() as u64;
            self.stats.bytes += spans.iter().map(|s| s.1 - s.0).sum::<u64>();
            // an object's spans in requests of at most SPANS_PER_REQUEST, which the host makes
            // concurrently: a GRIB file's message heads, megabytes apart, are read at once (a
            // fixed count, not the host's concurrency, so that every host makes the same requests)
            for group in spans.chunks(SPANS_PER_REQUEST) {
                self.stats.requests += 1;
                self.round.push(ObjectRequest { object: k, key: self.keys[k].0.clone(), spans: group.to_vec() });
            }
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
    /// `refkv_source` when `mirror`).
    pub fn into_output(self, url: &str, mirror: bool) -> Res<crate::out::Out> {
        let format = self.format;
        let (ir, facts, stats) = self.finish()?;
        store_output(format, &ir, &facts, stats, url, mirror)
    }
}

/// The output of a store parse: the format's projection, the mirror under `refkv_source` when
/// `mirror`, and a summary: the projection's, with the run's counts, the IR's element count
/// (and, but for SAFE, whose projection counts its objects itself, the format's name and the
/// IR's object count) and what the mirror's table folded. Then the ignored keys (and a SAFE
/// directory's empty directories) beside the mirror (`record_ignored`).
pub fn store_output(format: StoreFormat, ir: &Ir, facts: &J, stats: J, url: &str, mirror: bool) -> Res<crate::out::Out> {
    let mut out = format.project(ir, facts, url)?;
    crate::shard::refuse(&out)?;
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

#[cfg(test)]
mod tests {
    use super::*;

    /// A parser that probes `batch` new keys each step, forever, or for `until` steps (then done);
    /// the keys it calls optional are those `optional` holds.
    struct Prober { batch: usize, next: usize, ir: Option<Ir>, until: usize, optional: &'static [&'static str] }

    impl Prober {
        fn new(batch: usize, ir: Ir) -> Prober {
            Prober { batch, next: 0, ir: Some(ir), until: usize::MAX, optional: &[] }
        }
    }

    impl StoreParser for Prober {
        fn step(&mut self) -> Res<StoreStep> {
            if self.next >= self.until.saturating_mul(self.batch) {
                return Ok(StoreStep::Done);
            }
            let keys = (self.next..self.next + self.batch).map(|i| format!("k{i}")).collect();
            self.next += self.batch;
            Ok(StoreStep::Probe(keys))
        }
        fn optional(&self, key: &str) -> bool {
            self.optional.contains(&key)
        }
        fn feed(&mut self, _: usize, _: u64, _: &[u8]) {}
        fn found(&mut self, _: &str, _: usize, _: Option<u64>) -> Res<Option<u32>> {
            Ok(None)
        }
        fn finish(mut self: Box<Self>) -> Res<(Ir, J)> {
            Ok((self.ir.take().unwrap(), J::Null))
        }
    }

    #[test]
    fn a_run_probes_at_most_the_bound_on_files() {
        let mut run = StoreRun::with(StoreFormat::Nrrd, vec![("a".into(), 1)], 1, &|ir, _, _| {
            Ok(Box::new(Prober::new(crate::beside::MAX_FILES as usize, ir)))
        }).unwrap();
        assert!(run.poll().unwrap().is_some());
        for _ in 0..crate::beside::MAX_FILES {
            run.found(None).unwrap();
        }
        assert_eq!(run.poll().err().as_deref(), Some("budget: the input names more than 65536 files"));
    }

    /// A run of a parser that probes two keys a step for two steps, `optional` of them optional.
    fn refusing(optional: &'static [&'static str]) -> StoreRun {
        let mut run = StoreRun::with(StoreFormat::Nrrd, vec![("a".into(), 1)], 1, &|ir, _, _| {
            Ok(Box::new(Prober { optional, until: 2, ..Prober::new(2, ir) }))
        }).unwrap();
        run.refuse_siblings().unwrap();
        run
    }

    #[test]
    fn a_run_that_refuses_siblings_answers_optional_probes_itself() {
        let mut run = refusing(&["k0", "k1", "k2", "k3"]);
        assert_eq!(run.poll().unwrap(), None);
        assert!(run.probes().is_empty());
    }

    #[test]
    fn a_run_that_refuses_siblings_rejects_a_required_probe() {
        let mut run = refusing(&["k0", "k1", "k2"]);
        assert_eq!(run.poll().err().as_deref(), Some("siblings refused: the input names the file \"k3\""));
    }

    #[test]
    fn siblings_are_refused_before_the_first_poll() {
        let mut run = StoreRun::with(StoreFormat::Nrrd, vec![("a".into(), 1)], 1, &|ir, _, _| Ok(Box::new(Prober::new(1, ir))))
            .unwrap();
        assert!(run.poll().unwrap().is_some());
        assert_eq!(run.refuse_siblings().err().as_deref(), Some("internal: siblings refused after the first poll"));
    }
}
