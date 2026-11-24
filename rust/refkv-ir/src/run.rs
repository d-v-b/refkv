//! A run: a format's parser and the read planner together, sans-IO. The host
//! loops `poll()` (the requests to make now, concurrently), `complete()` (each
//! one's bytes and time), until `poll()` gives None; then `finish()`.

use crate::error::{Error, Res};
use crate::ir::Ir;
use crate::step::Step;
use crate::out::{Chunking, Out};
use crate::plan::{Planner, Request};
use crate::project::Copied;
use crate::Parser;
use serde_json::Value as J;
use std::sync::Arc;

/// The bytes a file's signatures test, and of what a gzipped file inflates to (spec/zarrify.md §1.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>).
pub const SNIFF: usize = 552;
/// The bytes its late signatures test: up to a superblock after a 64 KiB user block.
pub const SNIFF_LATE: usize = 65_600;

// `Format`: one variant per file profile of the registry (profiles/; build.rs)
include!(concat!(env!("OUT_DIR"), "/formats.rs"));

impl Format {
    /// The format of a profile's name, if a file profile's.
    pub fn from_name(s: &str) -> Option<Format> {
        Format::ALL.iter().copied().find(|f| f.name() == s)
    }

    /// The format a file's first bytes give, if one of these: the first, in the registry's
    /// `sniff` order, whose profile's `detect` takes the first `SNIFF` of them, else the first
    /// whose `detect_late` takes them all, else, of a gzip member, the first whose `detect_gzipped`
    /// takes what they inflate to (spec/zarrify.md §1.2
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>). A host passes
    /// `SNIFF` bytes, and `SNIFF_LATE` if that gives no format.
    pub fn sniff(head: &[u8]) -> Option<Format> {
        let early = &head[..head.len().min(SNIFF)];
        Format::SNIFFED
            .iter()
            .copied()
            .find(|f| f.profile().detect(early))
            .or_else(|| Format::SNIFFED.iter().copied().find(|f| f.profile().detect_late(head)))
            .or_else(|| {
                let inflated = crate::gz::start(head, SNIFF as u64)?;
                Format::SNIFFED.iter().copied().find(|f| f.profile().detect_gzipped(&inflated))
            })
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
    /// Whether a file's first bytes (up to `SNIFF_LATE` of them), that no `detect` took, are the
    /// profile's late signature.
    fn detect_late(&self, head: &[u8]) -> bool {
        let _ = head;
        false
    }
    /// Whether the first bytes (up to 552 of them) a gzip member inflates to are the profile's
    /// signature, for a profile that reads a file gzipped whole.
    fn detect_gzipped(&self, inflated: &[u8]) -> bool {
        let _ = inflated;
        false
    }
    /// A parser of a file of `size` bytes.
    fn parser(&self, size: u64) -> AnyParser;
    /// Whether the parser takes whole responses (it finds each range in them), rather than
    /// exactly the ranges it asked for.
    fn whole(&self) -> bool {
        true
    }
    /// The image projection: today's hierarchy, without `refkv_source`, from the IR and the
    /// parser's description of the source (the design of d-v-b/refkv#278, "The description",
    /// each profile's in its document's section "The description"). `copied` holds the bytes of
    /// the ranges `reads` gives, which `reads` gave for the same `chunking`.
    fn project(&self, ir: &Ir, description: &J, url: &str, copied: &Copied, chunking: &Chunking) -> crate::error::Res<Out>;
    /// `project`, with the layouts the output writes beside the image (None: every one), for a
    /// profile whose projection writes some.
    fn project_layouts(&self, ir: &Ir, description: &J, url: &str, copied: &Copied, layouts: Option<&[String]>,
                       chunking: &Chunking) -> crate::error::Res<Out> {
        let _ = layouts;
        self.project(ir, description, url, copied, chunking)
    }
    /// The source's ranges the projection with `chunking` copies into the archive or decodes,
    /// which the run reads after the parser: a projection reads nothing itself.
    fn reads(&self, ir: &Ir, description: &J, chunking: &Chunking) -> crate::error::Res<Vec<(u64, u64)>> {
        let _ = (ir, description, chunking);
        Ok(Vec::new())
    }
    /// The parser's counts (`requests`, `requested`, `rounds`) the output's summary holds, after
    /// the projection's members.
    fn summary_counts(&self) -> &'static [&'static str] {
        &[]
    }
    /// What the output adds beside the mirror (`output`, when it mirrors).
    fn mirrored(&self, out: &mut Out, description: &J) -> crate::error::Res<()> {
        let _ = (out, description);
        Ok(())
    }
}

/// A file run's parser, of any format.
pub struct AnyParser(Box<dyn Parse>);

/// `Parser`, as a trait object (a run is sent between threads: the Python host releases the GIL).
trait Parse: Send + Sync {
    fn step(&mut self) -> crate::error::Res<Step>;
    fn feed(&mut self, o: u64, b: Vec<u8>);
    fn finish(self: Box<Self>) -> crate::error::Res<(Ir, J)>;
    fn found(&self) -> Option<&'static str>;
}

impl<P: Parser + Send + Sync> Parse for P {
    fn step(&mut self) -> crate::error::Res<Step> {
        Parser::step(self)
    }
    fn feed(&mut self, o: u64, b: Vec<u8>) {
        Parser::feed(self, o, b)
    }
    fn finish(self: Box<Self>) -> crate::error::Res<(Ir, J)> {
        Parser::finish(*self)
    }
    fn found(&self) -> Option<&'static str> {
        Parser::found(self)
    }
}

/// The parser of a format no file run reads: it rejects with its message.
struct Refused(&'static str);

impl Parse for Refused {
    fn step(&mut self) -> crate::error::Res<Step> {
        Err(self.0.into())
    }
    fn feed(&mut self, _: u64, _: Vec<u8>) {}
    fn finish(self: Box<Self>) -> crate::error::Res<(Ir, J)> {
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
    pub fn step(&mut self) -> crate::error::Res<Step> {
        self.0.step()
    }
    pub fn feed(&mut self, o: u64, b: Vec<u8>) {
        self.0.feed(o, b)
    }
    pub fn finish(self) -> crate::error::Res<(Ir, J)> {
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
    /// the parser's IR and description, and its counts, once it is done; then the values the
    /// view shows that it did not read are read (`fill`) and held. The IR is shared once the run
    /// is done (`parsed`), and not changed after
    parsed: Option<(Arc<Ir>, Arc<serde_json::Value>, serde_json::Value)>,
    fill: Vec<(u32, u64, u64)>,
    /// the fill batch's ranges, and the planner's (requests, bytes) before and after it
    fill_counts: (u64, (u64, u64), (u64, u64)),
    /// the source's ranges the projection copies or samples (`project::reads`), read after the fill,
    /// and their bytes
    copies: Vec<(u64, u64)>,
    copied: crate::project::Copied,
    /// the host asks for the projection's reads itself (`defer_projection`), and has asked
    /// (`begin_projection`)
    deferred: bool,
    projecting: bool,
    pub planner: Planner,
    /// Whether the parser takes whole responses (`FileProfile::whole`).
    whole: bool,
    in_batch: bool,
    done: bool,
    /// The parser found the file names siblings (`Step::Beside`).
    beside: bool,
    /// How the projection cuts the source, from the run's start: its reads depend on it
    chunking: Chunking,
}

impl Run {
    /// A run with the convention's chunking (`Chunking::default`).
    pub fn new(format: Format, size: u64, remote: bool, concurrency: usize) -> Self {
        Self::with_chunking(format, size, remote, concurrency, Chunking::default())
    }

    /// A run whose projection cuts the source as `chunking` says: it reads the ranges that
    /// projection copies, and every output made of it is cut so.
    pub fn with_chunking(format: Format, size: u64, remote: bool, concurrency: usize, chunking: Chunking) -> Self {
        Run {
            format,
            parser: Some(AnyParser::new(format, size)),
            parsed: None,
            fill: Vec::new(),
            fill_counts: (0, (0, 0), (0, 0)),
            copies: Vec::new(),
            copied: Default::default(),
            deferred: false,
            projecting: false,
            planner: Planner::new(size, remote, concurrency),
            whole: format.profile().whole(),
            in_batch: false,
            done: false,
            beside: false,
            chunking,
        }
    }

    /// How the run's projection cuts the source.
    pub fn chunking(&self) -> Chunking {
        self.chunking
    }

    /// The requests to make now, concurrently; None when the parser is done.
    pub fn poll(&mut self) -> Res<Option<Vec<Request>>> {
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
                        let b = at.get(&r).ok_or_else(|| Error::internal("a range the projection copies was not read"))?;
                        self.copied.insert(r, b.clone());
                    }
                    self.done = true;
                    return Ok(None);
                }
                if let Some((ir, _, _)) = self.parsed.as_mut() {
                    // the values the view shows, read after the parser
                    let at: std::collections::HashMap<(u64, u64), &Vec<u8>> =
                        got.iter().map(|(o, b)| ((*o, b.len() as u64), b)).collect();
                    let ir = Arc::get_mut(ir).ok_or_else(|| Error::internal("the IR was shared before its values were read"))?;
                    for &(i, o, n) in &self.fill {
                        let b = at.get(&(o, n)).ok_or_else(|| Error::internal("a value the view shows was not read"))?;
                        ir.put_value(i, b);
                    }
                    ir.sort_values();
                    self.fill_counts.2 = (self.planner.stats.requests, self.planner.stats.bytes);
                    return self.read_copies();
                }
                let p = self.parser.as_mut().ok_or_else(|| Error::internal("no parser"))?;
                for (o, b) in got {
                    p.feed(o, b);
                }
            }
            let p = self.parser.as_mut().ok_or_else(|| Error::internal("no parser"))?;
            match p.step()? {
                Step::Read(r) => {
                    self.planner.begin(r, self.whole);
                    self.in_batch = true;
                }
                Step::Beside => {
                    self.beside = true;
                    self.done = true;
                    return Ok(None);
                }
                Step::Done => {
                    let p = self.parser.take().unwrap();
                    // a TIFF the TIFF parser found to be NDPI or QPTIFF is a run of that format
                    if let Some(found) = p.found() {
                        self.format = Format::from_name(found).ok_or_else(|| Error::internal("a parser found no file profile"))?;
                    }
                    let (ir, mut description) = p.finish()?;
                    let counts = parse_counts(&mut description);
                    self.fill = unread_shown(&ir, self.format.name());
                    let ranges: Vec<(u64, u64)> = {
                        let mut r: Vec<(u64, u64)> = self.fill.iter().map(|&(_, o, n)| (o, n)).collect();
                        r.sort_unstable();
                        r.dedup();
                        r
                    };
                    self.parsed = Some((Arc::new(ir), Arc::new(description), counts));
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

    /// After the fill: the batch of the ranges the projection copies, if any; else done. A run
    /// whose host asks for them itself (`defer_projection`) is done here: the IR is whole.
    fn read_copies(&mut self) -> Res<Option<Vec<Request>>> {
        if (self.deferred && !self.projecting) || !self.begin_copies()? {
            self.done = true;
            return Ok(None);
        }
        self.poll()
    }

    /// Begins the batch of the ranges the projection copies (`project::reads`); false when it
    /// copies none.
    fn begin_copies(&mut self) -> Res<bool> {
        let (ir, description, _) = self.parsed.as_ref().ok_or_else(|| Error::internal("no IR"))?;
        let mut ranges = crate::project::reads(self.format, ir, description, &self.chunking)?;
        ranges.sort_unstable();
        ranges.dedup();
        if ranges.is_empty() {
            return Ok(false);
        }
        self.planner.begin(ranges.clone(), false);
        self.copies = ranges;
        self.in_batch = true;
        Ok(true)
    }

    /// Leaves the built-in projection's reads out of the run, before its first poll: `poll` then
    /// gives None once the IR is whole (parsed, and the view's values read), and reads nothing
    /// for an output. A host that wants the built-in output asks for its reads later
    /// (`begin_projection`); one that builds another output never pays for them.
    pub fn defer_projection(&mut self) {
        self.deferred = true;
    }

    /// Asks for the built-in projection's reads, of a run that left them out
    /// (`defer_projection`) and is done: the host polls again until None, and may then project
    /// (`project`). Asking again once they are read, or of a run that read them itself, asks for
    /// nothing. Asking again before they are all read (a read failed, or the host stopped
    /// polling) asks for the whole batch again, so one host at a time asks.
    pub fn begin_projection(&mut self) -> Res<()> {
        if self.beside || self.parsed.is_none() || (!self.done && !self.projecting) {
            return Err(Error::Internal("the run has no IR to project".into()));
        }
        if !self.deferred || (self.projecting && self.done) {
            return Ok(());
        }
        // an interrupted batch is dropped, and begun again
        self.in_batch = false;
        self.copies.clear();
        let more = self.begin_copies()?;
        self.projecting = true;
        self.done = !more;
        Ok(())
    }

    /// The IR and the description of a run that is done, shared: the IR is not changed after.
    pub fn parsed(&self) -> Res<(&Arc<Ir>, &serde_json::Value)> {
        if !self.done && !self.projecting {
            return Err(Error::Internal("the run is not done".into()));
        }
        if self.beside {
            return Err(Error::internal("a file that names siblings has no file run's IR"));
        }
        self.parsed.as_ref().map(|(ir, description, _)| (ir, &**description)).ok_or_else(|| Error::internal("no IR"))
    }

    /// The profile's description of the source, without the run's own counts.
    pub fn description(&self) -> Res<serde_json::Value> {
        Ok(self.parsed()?.1.clone())
    }

    /// The planner's counts, with the fill batch's: the summary's `planner`.
    pub fn stats(&self) -> serde_json::Value {
        let mut stats = self.planner.stats_json();
        // the batch that read what the parser did not keep of the view's values
        let (n, a, b) = self.fill_counts;
        let (requests, bytes) = if n == 0 { (0, 0) } else { (b.0 - a.0, b.1 - a.1) };
        stats["fill"] = serde_json::json!({"ranges": n, "requests": requests, "bytes": bytes});
        stats
    }

    /// The output (`output`) of a run that is done and has read the projection's ranges, the run
    /// kept: a host may project it again, with other options. `layouts` names the layouts the
    /// projection writes beside the image (None: every one), and `checksums` and `written` are
    /// `output`'s.
    pub fn project(&self, url: &str, mirror: bool, layouts: Option<&[String]>, checksums: bool, written: bool)
                   -> Res<crate::out::Out> {
        if !self.done || (self.deferred && !self.projecting) {
            return Err(Error::Internal("the run has not read what its projection copies".into()));
        }
        let (ir, description) = self.parsed()?;
        let counts = self.parsed.as_ref().map(|p| &p.2).ok_or_else(|| Error::internal("no IR"))?;
        let options = Options { layouts, checksums, written, chunking: self.chunking };
        output(self.format, ir, description, counts, self.stats(), url, mirror, &self.copied, &options)
    }

    /// Whether the parser found that the file names siblings, once `poll` gave None: it has no
    /// output, and the host starts over with a store run of `format` over the file alone
    /// (spec/zarrify.md §1.9
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#19-sources-beside-the-input>,
    /// **When an input is read as a store**).
    pub fn beside(&self) -> bool {
        self.beside
    }

    /// Request `k` of the last poll: each span's bytes (None: a multi-range request
    /// the server did not answer with its ranges), and the seconds it took.
    pub fn complete(&mut self, k: usize, data: Option<Vec<Vec<u8>>>, seconds: f64) -> Res<()> {
        self.planner.complete(k, data, seconds)
    }

    /// The IR, the description and the planner's counts, with the parser's (`parser`).
    pub fn finish(self) -> Res<(Ir, serde_json::Value, serde_json::Value)> {
        self.finished().map(|f| {
            let mut stats = f.stats;
            stats["parser"] = f.counts;
            (f.ir, f.description, stats)
        })
    }

    /// `finish`, with the parser's counts and the bytes of the ranges the projection copies.
    pub fn finish_copied(self) -> Res<Finished> {
        if self.deferred && !self.projecting {
            return Err(Error::Internal("the run has not read what its projection copies".into()));
        }
        self.finished()
    }

    fn finished(self) -> Res<Finished> {
        if !self.done {
            return Err(Error::Internal("the run is not done".into()));
        }
        if self.beside {
            return Err(Error::internal("a file that names siblings has no file run's output"));
        }
        let stats = self.stats();
        let (ir, description, counts) = self.parsed.ok_or_else(|| Error::internal("no IR"))?;
        let description = Arc::try_unwrap(description).unwrap_or_else(|shared| (*shared).clone());
        let ir = Arc::try_unwrap(ir).unwrap_or_else(|shared| (*shared).clone());
        Ok(Finished { ir, description, counts, stats, copied: self.copied })
    }
}

/// A finished file run: the IR, the parser's description and its counts (`parse_counts`), the
/// planner's counts, and the bytes of the ranges the projection copies.
pub struct Finished {
    pub ir: Ir,
    pub description: serde_json::Value,
    pub counts: serde_json::Value,
    pub stats: serde_json::Value,
    pub copied: crate::project::Copied,
}

/// What an output is made with besides the parse: the layouts written beside the image (None:
/// every one), whether the host records the ranges' checksums, whether it writes this output as
/// it is, and how its projection cuts the source, as the run that read its copies did
/// (`output`).
#[derive(Clone, Copy, Debug, Default)]
pub struct Options<'a> {
    pub layouts: Option<&'a [String]>,
    pub checksums: bool,
    pub written: bool,
    pub chunking: Chunking,
}

/// The parser's own counts (`requests`, `requested`, `rounds`), which a parser returns with its
/// description: removed from `description`, and returned.
pub fn parse_counts(description: &mut serde_json::Value) -> serde_json::Value {
    let mut counts = serde_json::Map::new();
    if let Some(m) = description.as_object_mut() {
        for count in ["requests", "requested", "rounds"] {
            if let Some(v) = m.remove(count) {
                counts.insert(count.into(), v);
            }
        }
    }
    serde_json::Value::Object(counts)
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

/// The output of a parse: the image projection of `format` from the IR and the description, its
/// arrays sharded as it asked (`shard::apply_all`; ranges adjacent across inner chunks joined
/// unless `options.checksums`: the host records checksums), the mirror under `refkv_source` when
/// `mirror`, and a summary with the parser's counts the profile names
/// (`FileProfile::summary_counts`), the planner's counts, the IR's element count and what the
/// mirror's table folded. `copied` holds the bytes of the ranges the projection copies (`project::reads`). Last,
/// the output is sealed (`seal::out`), the file its source 0: with its checksums when the host
/// records them and `written`, the host writing this output as it is; an output a host only builds
/// another from (`refkv.build.read`, a plugin's base) is not held to the payload limit with
/// checksums, which the output it writes is.
#[allow(clippy::too_many_arguments)]
pub fn output(format: Format, ir: &Ir, description: &serde_json::Value, counts: &serde_json::Value, stats: serde_json::Value,
              url: &str, mirror: bool, copied: &crate::project::Copied, options: &Options) -> Res<crate::out::Out> {
    let Options { layouts, checksums, written, chunking } = *options;
    let mut out = crate::project::project(format, ir, description, url, copied, layouts, &chunking)?;
    crate::shard::apply_all(&mut out, !checksums)?;
    if let Some(m) = out.summary.as_object_mut() {
        for &k in format.profile().summary_counts() {
            m.insert(k.into(), counts[k].clone());
        }
        m.insert("format".into(), format.name().into());
        m.insert("planner".into(), stats);
        m.insert("elements".into(), ir.len().into());
    }
    if mirror {
        let folded = mirrored(format, ir, description, &mut out)?;
        if let Some(m) = out.summary.as_object_mut() {
            m.insert("folded".into(), folded.json());
        }
    }
    crate::seal::out(&mut out, Some(ir.size), &Default::default(), checksums && written)?;
    Ok(out)
}

/// Writes to `out` the mirror of a file's IR under `refkv_source`, and what its profile records
/// beside it from the parser's description (the entries a Zarr v3 or SAFE zip's listing
/// ignored): what `output` writes there, and what the table folded.
pub fn mirrored(format: Format, ir: &Ir, description: &serde_json::Value, out: &mut crate::out::Out) -> Res<crate::mirror::Folded> {
    let folded = crate::mirror::mirror(ir, out, format.name())?;
    format.profile().mirrored(out, description)?;
    Ok(folded)
}

impl Run {
    /// The IR and the description of a run that is done, shared with the run: what `mirrored`
    /// reads, for a host that makes the mirror while the run is used elsewhere.
    pub fn shared(&self) -> Res<(Arc<Ir>, Arc<serde_json::Value>)> {
        self.parsed()?;
        self.parsed.as_ref().map(|(ir, description, _)| (Arc::clone(ir), Arc::clone(description))).ok_or_else(|| Error::internal("no IR"))
    }

    /// Finishes, projects and mirrors: the output (`output`), for a host that records the
    /// ranges' checksums when `checksums`.
    pub fn into_output(self, url: &str, mirror: bool, checksums: bool) -> Res<crate::out::Out> {
        let (format, chunking) = (self.format, self.chunking);
        let f = self.finish_copied()?;
        let options = Options { layouts: None, checksums, written: true, chunking };
        output(format, &f.ir, &f.description, &f.counts, f.stats, url, mirror, &f.copied, &options)
    }
}

/// Runs a format's parser over bytes in memory through the planner (tests, tools).
pub fn run_bytes(format: Format, data: &[u8]) -> Res<(Ir, serde_json::Value, serde_json::Value)> {
    let mut r = Run::new(format, data.len() as u64, false, 4);
    while let Some(reqs) = r.poll()? {
        for (k, q) in reqs.iter().enumerate() {
            let parts = q.spans.iter().map(|&(a, b)| data[a as usize..b as usize].to_vec()).collect();
            r.complete(k, Some(parts), 0.0)?;
        }
    }
    r.finish()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A parser that finds, at its first step, that the file names siblings.
    struct NamesSiblings;

    impl Parser for NamesSiblings {
        fn new(_size: u64) -> Self {
            NamesSiblings
        }
        fn step(&mut self) -> crate::error::Res<Step> {
            Ok(Step::Beside)
        }
        fn feed(&mut self, _offset: u64, _data: Vec<u8>) {}
        fn finish(self) -> crate::error::Res<(Ir, J)> {
            Err(Error::rejected("not reached"))
        }
        fn found(&self) -> Option<&'static str> {
            None
        }
    }

    #[test]
    fn a_file_that_names_siblings_ends_its_run_without_an_output() {
        let mut run = Run::new(Format::ALL[0], 8, false, 1);
        run.parser = Some(AnyParser::of::<NamesSiblings>(8));
        assert!(!run.beside());
        assert!(run.poll().unwrap().is_none());
        assert!(run.beside());
        assert_eq!(run.finish().err(), Some(Error::internal("a file that names siblings has no file run's output")));
    }

    /// A parser that is done at its first step, with an empty IR.
    struct Empty;

    impl Parser for Empty {
        fn new(_size: u64) -> Self {
            Empty
        }
        fn step(&mut self) -> crate::error::Res<Step> {
            Ok(Step::Done)
        }
        fn feed(&mut self, _offset: u64, _data: Vec<u8>) {}
        fn finish(self) -> crate::error::Res<(Ir, J)> {
            Ok((Ir::default(), serde_json::json!({})))
        }
        fn found(&self) -> Option<&'static str> {
            None
        }
    }

    #[test]
    fn a_run_that_defers_its_projection_is_done_with_its_ir_and_projects_once_asked() {
        let mut run = Run::new(Format::ALL[0], 8, false, 1);
        run.parser = Some(AnyParser::of::<Empty>(8));
        assert_eq!(run.parsed().err(), Some(Error::Internal("the run is not done".into())));
        assert_eq!(run.begin_projection().err(), Some(Error::Internal("the run has no IR to project".into())));
        run.defer_projection();
        assert!(run.poll().unwrap().is_none());
        // the IR is there, shared, and no output can be made of it yet
        let (ir, _) = run.parsed().unwrap();
        let held = Arc::clone(ir);
        assert_eq!(run.project("u", false, None, false, true).err(), Some(Error::Internal("the run has not read what its projection copies".into())));
        run.begin_projection().unwrap();
        run.begin_projection().unwrap(); // asking twice asks for nothing more
        assert!(run.poll().unwrap().is_none());
        assert!(Arc::ptr_eq(run.parsed().unwrap().0, &held));
        // the run is kept: it finishes with the IR it shared
        let f = run.finish_copied().unwrap();
        assert_eq!((f.ir.len(), f.copied.len(), &f.stats["fill"]["ranges"]), (0, 0, &serde_json::json!(0)));
    }

    #[test]
    fn a_run_that_deferred_its_projection_and_never_asked_has_no_copies_to_finish_with() {
        let mut run = Run::new(Format::ALL[0], 8, false, 1);
        run.parser = Some(AnyParser::of::<Empty>(8));
        run.defer_projection();
        assert!(run.poll().unwrap().is_none());
        assert_eq!(run.finish_copied().err(), Some(Error::Internal("the run has not read what its projection copies".into())));
    }
}
