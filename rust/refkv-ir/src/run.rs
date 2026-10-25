//! A run: a format's parser and the read planner together, sans-IO. The host
//! loops `poll()` (the requests to make now, concurrently), `complete()` (each
//! one's bytes and time), until `poll()` gives None; then `finish()`.

use crate::ir::Ir;
use crate::nd2::Step;
use crate::out::Out;
use crate::plan::{Planner, Request};
use crate::project::Copied;
use crate::Parser;
use serde_json::Value as J;

// `Format`: one variant per file profile of the registry (profiles/; build.rs)
include!(concat!(env!("OUT_DIR"), "/formats.rs"));

impl Format {
    /// Whether the format's input, a file, is zarrified by a store run rather than a file
    /// run (`FileProfile::store_run`).
    pub fn store_run(self) -> bool {
        self.profile().store_run()
    }

    /// The format of a profile's name, if a file profile's.
    pub fn from_name(s: &str) -> Option<Format> {
        Format::ALL.iter().copied().find(|f| f.name() == s)
    }

    /// The format a file's first bytes give, if one of these: the first, in the registry's
    /// `sniff` order, whose profile's `detect` takes them (spec/zarrify.md §1.2
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>).
    pub fn sniff(head: &[u8]) -> Option<Format> {
        Format::SNIFFED.iter().copied().find(|f| f.profile().detect(head))
    }
}

/// A file profile's half of a run, which its module declares as `pub struct Profile` (and
/// `Format::profile` names): its signature, its parser, how the planner answers it, and its
/// projection. The run and the hosts dispatch on nothing else.
pub trait FileProfile: Sync {
    /// Whether a file's first bytes (up to 552 of them) are the profile's signature; asked only
    /// of a profile the registry gives a `sniff` precedence.
    fn detect(&self, head: &[u8]) -> bool {
        let _ = head;
        false
    }
    /// A parser of a file of `size` bytes.
    fn parser(&self, size: u64) -> AnyParser;
    /// Whether the parser takes whole responses (it finds each range in them), rather than
    /// exactly the ranges it asked for.
    fn whole(&self) -> bool {
        true
    }
    /// Whether the input, a file, is zarrified by a store run rather than a file run (a file
    /// run of it only rejects, so a host hands it to its store run).
    fn store_run(&self) -> bool {
        false
    }
    /// The image projection: today's hierarchy, without `refkv_source`. `copied` holds the
    /// bytes of the ranges `reads` gives.
    fn project(&self, ir: &Ir, facts: &J, url: &str, copied: &Copied) -> Result<Out, String>;
    /// The source's ranges the projection copies into the archive or decodes, which the run
    /// reads after the parser: a projection reads nothing itself.
    fn reads(&self, ir: &Ir, facts: &J) -> Result<Vec<(u64, u64)>, String> {
        let _ = (ir, facts);
        Ok(Vec::new())
    }
    /// What the output adds beside the mirror (`output`, when it mirrors).
    fn mirrored(&self, out: &mut Out, facts: &J) -> Result<(), String> {
        let _ = (out, facts);
        Ok(())
    }
}

/// A file run's parser, of any format.
pub struct AnyParser(Box<dyn Parse>);

/// `Parser`, as a trait object (a run is sent between threads: the Python host releases the GIL).
trait Parse: Send + Sync {
    fn step(&mut self) -> Result<Step, String>;
    fn feed(&mut self, o: u64, b: Vec<u8>);
    fn finish(self: Box<Self>) -> Result<(Ir, J), String>;
    fn found(&self) -> Option<&'static str>;
}

impl<P: Parser + Send + Sync> Parse for P {
    fn step(&mut self) -> Result<Step, String> {
        Parser::step(self)
    }
    fn feed(&mut self, o: u64, b: Vec<u8>) {
        Parser::feed(self, o, b)
    }
    fn finish(self: Box<Self>) -> Result<(Ir, J), String> {
        Parser::finish(*self)
    }
    fn found(&self) -> Option<&'static str> {
        Parser::found(self)
    }
}

/// The parser of a format no file run reads: it rejects with its message.
struct Refused(&'static str);

impl Parse for Refused {
    fn step(&mut self) -> Result<Step, String> {
        Err(self.0.into())
    }
    fn feed(&mut self, _: u64, _: Vec<u8>) {}
    fn finish(self: Box<Self>) -> Result<(Ir, J), String> {
        Err(self.0.into())
    }
    fn found(&self) -> Option<&'static str> {
        None
    }
}

impl AnyParser {
    pub fn new(f: Format, size: u64) -> Self {
        f.profile().parser(size)
    }
    /// A parser `P` of a file of `size` bytes.
    pub fn of<P: Parser + Send + Sync + 'static>(size: u64) -> Self {
        AnyParser(Box::new(P::new(size)))
    }
    /// The parser of a format no file run reads, which rejects with `message`.
    pub fn refused(message: &'static str) -> Self {
        AnyParser(Box::new(Refused(message)))
    }
    pub fn step(&mut self) -> Result<Step, String> {
        self.0.step()
    }
    pub fn feed(&mut self, o: u64, b: Vec<u8>) {
        self.0.feed(o, b)
    }
    pub fn finish(self) -> Result<(Ir, J), String> {
        self.0.finish()
    }
    /// The profile the parser found the file to be, if it decides it (`Parser::found`).
    pub fn found(&self) -> Option<&'static str> {
        self.0.found()
    }
}

pub struct Run {
    pub format: Format,
    parser: Option<AnyParser>,
    /// the parser's IR and facts, once it is done; then the values the view shows
    /// that it did not read are read (`fill`) and held
    parsed: Option<(Ir, serde_json::Value)>,
    fill: Vec<(u32, u64, u64)>,
    /// the fill batch's ranges, and the planner's (requests, bytes) before and after it
    fill_counts: (u64, (u64, u64), (u64, u64)),
    /// the source's ranges the projection copies or samples (`project::reads`), read after the fill,
    /// and their bytes
    copies: Vec<(u64, u64)>,
    copied: crate::project::Copied,
    pub planner: Planner,
    /// Whether the parser takes whole responses (`FileProfile::whole`).
    whole: bool,
    in_batch: bool,
    done: bool,
}

impl Run {
    pub fn new(format: Format, size: u64, remote: bool, concurrency: usize) -> Self {
        Run {
            format,
            parser: Some(AnyParser::new(format, size)),
            parsed: None,
            fill: Vec::new(),
            fill_counts: (0, (0, 0), (0, 0)),
            copies: Vec::new(),
            copied: Default::default(),
            planner: Planner::new(size, remote, concurrency),
            whole: format.profile().whole(),
            in_batch: false,
            done: false,
        }
    }

    /// The requests to make now, concurrently; None when the parser is done.
    pub fn poll(&mut self) -> Result<Option<Vec<Request>>, String> {
        loop {
            if self.done {
                return Ok(None);
            }
            if self.in_batch {
                let r = self.planner.requests();
                if !r.is_empty() {
                    return Ok(Some(r));
                }
                let got = self.planner.take()?;
                self.in_batch = false;
                if !self.copies.is_empty() {
                    // the ranges the projection copies, read last
                    let at: std::collections::HashMap<(u64, u64), Vec<u8>> =
                        got.into_iter().map(|(o, b)| ((o, b.len() as u64), b)).collect();
                    for r in std::mem::take(&mut self.copies) {
                        let b = at.get(&r).ok_or("internal: a range the projection copies was not read")?;
                        self.copied.insert(r, b.clone());
                    }
                    self.done = true;
                    return Ok(None);
                }
                if let Some((ir, _)) = self.parsed.as_mut() {
                    // the values the view shows, read after the parser
                    let at: std::collections::HashMap<(u64, u64), &Vec<u8>> =
                        got.iter().map(|(o, b)| ((*o, b.len() as u64), b)).collect();
                    for &(i, o, n) in &self.fill {
                        let b = at.get(&(o, n)).ok_or("internal: a value the view shows was not read")?;
                        ir.put_value(i, b);
                    }
                    ir.sort_values();
                    self.fill_counts.2 = (self.planner.stats.requests, self.planner.stats.bytes);
                    return self.read_copies();
                }
                let p = self.parser.as_mut().ok_or("internal: no parser")?;
                for (o, b) in got {
                    p.feed(o, b);
                }
            }
            let p = self.parser.as_mut().ok_or("internal: no parser")?;
            match p.step()? {
                Step::Read(r) => {
                    self.planner.begin(r, self.whole);
                    self.in_batch = true;
                }
                Step::Done => {
                    let p = self.parser.take().unwrap();
                    // a TIFF the TIFF parser found to be NDPI or QPTIFF is a run of that format
                    if let Some(found) = p.found() {
                        self.format = Format::from_name(found).ok_or("internal: a parser found no file profile")?;
                    }
                    let (ir, facts) = p.finish()?;
                    self.fill = unread_shown(&ir, self.format.name());
                    let ranges: Vec<(u64, u64)> = {
                        let mut r: Vec<(u64, u64)> = self.fill.iter().map(|&(_, o, n)| (o, n)).collect();
                        r.sort_unstable();
                        r.dedup();
                        r
                    };
                    self.parsed = Some((ir, facts));
                    if ranges.is_empty() {
                        return self.read_copies();
                    }
                    self.fill_counts = (ranges.len() as u64, (self.planner.stats.requests, self.planner.stats.bytes), (0, 0));
                    self.planner.begin(ranges, false);
                    self.in_batch = true;
                }
            }
        }
    }

    /// After the fill: the batch of the ranges the projection copies, if any; else done.
    fn read_copies(&mut self) -> Result<Option<Vec<Request>>, String> {
        let (ir, facts) = self.parsed.as_ref().ok_or("internal: no IR")?;
        let mut ranges = crate::project::reads(self.format, ir, facts)?;
        ranges.sort_unstable();
        ranges.dedup();
        if ranges.is_empty() {
            self.done = true;
            return Ok(None);
        }
        self.planner.begin(ranges.clone(), false);
        self.copies = ranges;
        self.in_batch = true;
        self.poll()
    }

    /// Request `k` of the last poll: each span's bytes (None: a multi-range request
    /// the server did not answer with its ranges), and the seconds it took.
    pub fn complete(&mut self, k: usize, data: Option<Vec<Vec<u8>>>, seconds: f64) -> Result<(), String> {
        self.planner.complete(k, data, seconds)
    }

    pub fn finish(self) -> Result<(Ir, serde_json::Value, serde_json::Value), String> {
        self.finish_copied().map(|(ir, facts, stats, _)| (ir, facts, stats))
    }

    /// `finish`, and the bytes of the ranges the projection copies.
    pub fn finish_copied(self) -> Result<(Ir, serde_json::Value, serde_json::Value, crate::project::Copied), String> {
        if !self.done {
            return Err("the run is not done".into());
        }
        let mut stats = self.planner.stats_json();
        // the batch that read what the parser did not keep of the view's values
        let (n, a, b) = self.fill_counts;
        let (requests, bytes) = if n == 0 { (0, 0) } else { (b.0 - a.0, b.1 - a.1) };
        stats["fill"] = serde_json::json!({"ranges": n, "requests": requests, "bytes": bytes});
        let (ir, facts) = self.parsed.ok_or("internal: no IR")?;
        Ok((ir, facts, stats, self.copied))
    }
}

/// The values the view shows (conventions §8.7
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#87-the-view>) whose bytes the
/// parser did not keep,
/// in the source: (element, offset, length). Each parser holds every such value it
/// reads; these are read after it.
pub fn unread_shown(ir: &Ir, profile: &str) -> Vec<(u32, u64, u64)> {
    (0..ir.len() as u32)
        .filter(|&i| {
            let iu = i as usize;
            ir.space[iu] == 0 && ir.len[iu] > 0 && ir.value_bytes(i).is_none() && crate::mirror::shown(ir, profile, i)
        })
        .map(|i| (i, ir.start[i as usize], ir.len[i as usize]))
        .collect()
}

/// The output of a parse: the image projection of `format`, its arrays sharded as it asked
/// (`shard::apply_all`; ranges adjacent across inner chunks joined unless the facts' `checksums`
/// is true: the host records checksums), the mirror under `refkv_source` when `mirror`, and a
/// summary with the planner's counts, the IR's element count and what the mirror's table
/// folded. `copied` holds the bytes of the ranges the projection copies (`project::reads`).
pub fn output(format: Format, ir: &Ir, facts: &serde_json::Value, stats: serde_json::Value, url: &str, mirror: bool,
              copied: &crate::project::Copied) -> Result<crate::out::Out, String> {
    let mut out = crate::project::project(format, ir, facts, url, copied)?;
    crate::shard::apply_all(&mut out, !facts["checksums"].as_bool().unwrap_or(false))?;
    if let Some(m) = out.summary.as_object_mut() {
        m.insert("format".into(), format.name().into());
        m.insert("planner".into(), stats);
        m.insert("elements".into(), ir.len().into());
    }
    if mirror {
        let folded = crate::mirror::mirror(ir, &mut out, format.name())?;
        if let Some(m) = out.summary.as_object_mut() {
            m.insert("folded".into(), folded.json());
        }
        format.profile().mirrored(&mut out, facts)?;
    }
    Ok(out)
}

impl Run {
    /// Finishes, projects and mirrors: the output (`output`), for a host that records the
    /// ranges' checksums when `checksums`.
    pub fn into_output(self, url: &str, mirror: bool, checksums: bool) -> Result<crate::out::Out, String> {
        let format = self.format;
        let (ir, mut facts, stats, copied) = self.finish_copied()?;
        facts["checksums"] = checksums.into();
        output(format, &ir, &facts, stats, url, mirror, &copied)
    }
}

/// Runs a format's parser over bytes in memory through the planner (tests, tools).
pub fn run_bytes(format: Format, data: &[u8]) -> Result<(Ir, serde_json::Value, serde_json::Value), String> {
    let mut r = Run::new(format, data.len() as u64, false, 4);
    while let Some(reqs) = r.poll()? {
        for (k, q) in reqs.iter().enumerate() {
            let parts = q.spans.iter().map(|&(a, b)| data[a as usize..b as usize].to_vec()).collect();
            r.complete(k, Some(parts), 0.0)?;
        }
    }
    r.finish()
}
