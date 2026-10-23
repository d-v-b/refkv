//! A run: a format's parser and the read planner together, sans-IO. The host
//! loops `poll()` (the requests to make now, concurrently), `complete()` (each
//! one's bytes and time), until `poll()` gives None; then `finish()`.

use crate::czi::Czi;
use crate::dicom::Dicom;
use crate::ims::Ims;
use crate::ir::Ir;
use crate::nd2::{Nd2, Step};
use crate::plan::{Planner, Request};
use crate::mp3::Mp3;
use crate::mrc::Mrc;
use crate::nifti::Nifti;
use crate::safe::Safe;
use crate::tiff::Tiff;
use crate::Parser;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Format {
    Nd2,
    Tiff,
    Czi,
    Dicom,
    Nifti,
    Ims,
    /// a Sentinel-2 SAFE product's zip form (`.SAFE.zip`)
    Safe,
    /// A TIFF the TIFF parser found to be NDPI: a run of `Tiff` becomes one of `Ndpi`
    /// when its parser does (spec/virtualize/ndpi.md §4
    /// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/ndpi.md#4-ndpi-profile>), so
    /// hosts never test for NDPI themselves.
    Ndpi,
    /// an MRC2014 file (a CCP4 map, an image stack, a volume or a volume stack)
    Mrc,
    /// an MP3 file (MPEG-1, MPEG-2 or MPEG-2.5 Layer III audio), read whole
    Mp3,
    /// An Olympus VSI slide: a `.vsi` file whose tile files lie beside it, virtualized as a
    /// store run of the `vsi` store format (spec/virtualize/vsi.md §15.2
    /// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#152-the-tile-files>), never
    /// as a file run: `sniff` names it so that a host sends the file to [`crate::store::StoreRun`].
    Vsi,
    /// A TIFF the TIFF parser found to be an Akoya QPTIFF (spec/virtualize/qptiff.md §16
    /// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/qptiff.md#16-qptiff-profile>), as
    /// `Ndpi`: a run of `Tiff` becomes one of `Qptiff`.
    Qptiff,
}

impl Format {
    /// The format's name: its profile's in the registry (spec/virtualize/profiles.json), which
    /// hosts pass across every boundary (there are no numbers).
    pub fn name(self) -> &'static str {
        match self {
            Format::Nd2 => "nd2",
            Format::Tiff => "tiff",
            Format::Czi => "czi",
            Format::Dicom => "dicom",
            Format::Nifti => "nifti",
            Format::Ims => "ims",
            Format::Safe => "safe",
            Format::Ndpi => "ndpi",
            Format::Mrc => "mrc",
            Format::Mp3 => "mp3",
            Format::Vsi => "vsi",
            Format::Qptiff => "qptiff",
        }
    }
    /// Whether the format's input, a file, is virtualized by a store run rather than a file
    /// run: VSI, whose `.vsi` file is read with the tile files beside it (`StoreFormat::Vsi`); a
    /// file run of it only rejects, so a host hands it to its `vsi` module.
    pub fn store_run(self) -> bool {
        matches!(self, Format::Vsi)
    }

    /// The format of a name ("nd2", "tiff", "czi", "dicom", "nifti", "ims", "safe", "ndpi", "mrc", "vsi", "qptiff", "mp3").
    pub fn from_name(s: &str) -> Option<Format> {
        [Format::Nd2, Format::Tiff, Format::Czi, Format::Dicom, Format::Nifti, Format::Ims, Format::Safe, Format::Ndpi, Format::Mrc, Format::Vsi, Format::Qptiff, Format::Mp3].into_iter().find(|f| f.name() == s)
    }
    /// The format a file's first bytes give, if one of these: a hint, the format of its
    /// signature (any zip file is taken for a SAFE product's zip form, which then rejects one
    /// that is not, spec/virtualize/safe.md §12.1
    /// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/safe.md#121-choosing-the-profile>).
    pub fn sniff(head: &[u8]) -> Option<Format> {
        // DICOM first: a dual-personality file's preamble may hold a TIFF header
        if head.len() >= 132 && &head[128..132] == b"DICM" {
            return Some(Format::Dicom);
        }
        if head.len() >= 4 && head[..4] == [0xda, 0xce, 0xbe, 0x0a] {
            return Some(Format::Nd2);
        }
        // a VSI file is a TIFF whose byte 8 starts its metadata tree (spec/virtualize/vsi.md §15.1
        // <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#151-the-vsi-file>)
        if crate::vsi::detect(head) {
            return Some(Format::Vsi);
        }
        if head.len() >= 4 && [b"II*\0", b"MM\0*", b"II+\0", b"MM\0+"].iter().any(|m| head[..4] == m[..]) {
            return Some(Format::Tiff);
        }
        if head.len() >= 16 && &head[..16] == b"ZISRAWFILE\0\0\0\0\0\0" {
            return Some(Format::Czi);
        }
        if crate::nifti::detect(head).is_some() {
            return Some(Format::Nifti);
        }
        if head.len() >= 8 && &head[..8] == crate::hdf5::SIGNATURE {
            return Some(Format::Ims);
        }
        if head.len() >= 4 && &head[..4] == b"PK\x03\x04" {
            return Some(Format::Safe);
        }
        if crate::mrc::detect(head) {
            return Some(Format::Mrc);
        }
        if crate::mp3::detect(head) {
            return Some(Format::Mp3);
        }
        None
    }
}

pub enum AnyParser {
    Nd2(Nd2),
    Tiff(Tiff),
    Czi(Czi),
    Dicom(Dicom),
    Nifti(Nifti),
    Ims(Ims),
    Safe(Box<Safe>),
    Mrc(Mrc),
    Mp3(Mp3),
    /// a VSI file, which no file run reads (its parser is a store parser)
    Vsi,
}

impl AnyParser {
    pub fn new(f: Format, size: u64) -> Self {
        match f {
            Format::Nd2 => AnyParser::Nd2(Nd2::new(size)),
            Format::Tiff | Format::Ndpi | Format::Qptiff => AnyParser::Tiff(Tiff::new(size)),
            Format::Czi => AnyParser::Czi(Czi::new(size)),
            Format::Dicom => AnyParser::Dicom(Dicom::new(size)),
            Format::Nifti => AnyParser::Nifti(Nifti::new(size)),
            Format::Ims => AnyParser::Ims(Ims::new(size)),
            Format::Safe => AnyParser::Safe(Box::new(Safe::new(size))),
            Format::Mrc => AnyParser::Mrc(Mrc::new(size)),
            Format::Mp3 => AnyParser::Mp3(Mp3::new(size)),
            Format::Vsi => AnyParser::Vsi,
        }
    }
    pub fn step(&mut self) -> Result<Step, String> {
        match self {
            AnyParser::Nd2(x) => x.step(),
            AnyParser::Tiff(x) => x.step(),
            AnyParser::Czi(x) => x.step(),
            AnyParser::Dicom(x) => x.step(),
            AnyParser::Nifti(x) => x.step(),
            AnyParser::Ims(x) => x.step(),
            AnyParser::Safe(x) => x.step(),
            AnyParser::Mrc(x) => x.step(),
            AnyParser::Mp3(x) => x.step(),
            AnyParser::Vsi => Err("a VSI file is virtualized as a store run, with its tile files".into()),
        }
    }
    pub fn feed(&mut self, o: u64, b: Vec<u8>) {
        match self {
            AnyParser::Nd2(x) => x.feed(o, b),
            AnyParser::Tiff(x) => x.feed(o, b),
            AnyParser::Czi(x) => x.feed(o, b),
            AnyParser::Dicom(x) => x.feed(o, b),
            AnyParser::Nifti(x) => x.feed(o, b),
            AnyParser::Ims(x) => x.feed(o, b),
            AnyParser::Safe(x) => x.feed(o, b),
            AnyParser::Mrc(x) => x.feed(o, b),
            AnyParser::Mp3(x) => x.feed(o, b),
            AnyParser::Vsi => {}
        }
    }
    pub fn finish(self) -> Result<(Ir, serde_json::Value), String> {
        match self {
            AnyParser::Nd2(x) => Parser::finish(x),
            AnyParser::Tiff(x) => Parser::finish(x),
            AnyParser::Czi(x) => Parser::finish(x),
            AnyParser::Dicom(x) => Parser::finish(x),
            AnyParser::Nifti(x) => Parser::finish(x),
            AnyParser::Ims(x) => Parser::finish(x),
            AnyParser::Safe(x) => Parser::finish(*x),
            AnyParser::Mrc(x) => Parser::finish(x),
            AnyParser::Mp3(x) => Parser::finish(x),
            AnyParser::Vsi => Err("a VSI file is virtualized as a store run, with its tile files".into()),
        }
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
    /// TIFF and CZI take whole responses (they find each range in them); ND2 exact ranges.
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
            // TIFF and CZI find their ranges in whole responses; ND2, SAFE and MP3 take exact ranges
            whole: !matches!(format, Format::Nd2 | Format::Safe | Format::Mp3),
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
                    if let AnyParser::Tiff(t) = &p {
                        self.format = if t.ndpi {
                            Format::Ndpi
                        } else if t.qptiff {
                            Format::Qptiff
                        } else {
                            Format::Tiff
                        };
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

/// The output of a parse: the image projection of `format`, the mirror under
/// `refkv_source` when `mirror`, and a summary with the planner's counts, the IR's
/// element count and what the mirror's table folded. `copied` holds the bytes of the
/// ranges the projection copies (`project::reads`).
pub fn output(format: Format, ir: &Ir, facts: &serde_json::Value, stats: serde_json::Value, url: &str, mirror: bool,
              copied: &crate::project::Copied) -> Result<crate::out::Out, String> {
    let mut out = crate::project::project(format, ir, facts, url, copied)?;
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
        if format == Format::Safe {
            // its empty directories and ignored keys beside the mirror, as a SAFE directory's
            crate::project::store::record_ignored(&mut out, facts, format.name())?;
        }
    }
    Ok(out)
}

impl Run {
    /// Finishes, projects and mirrors: the output (`output`).
    pub fn into_output(self, url: &str, mirror: bool) -> Result<crate::out::Out, String> {
        let format = self.format;
        let (ir, facts, stats, copied) = self.finish_copied()?;
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
