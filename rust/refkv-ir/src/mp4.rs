//! A sans-IO parser of MP4 and QuickTime files (ISO/IEC 14496-12 and 14496-14, and Apple's
//! QuickTime file format) into the IR (spec/virtualize/mp4.md §8
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp4.md#8-mp4-profile>, and the source
//! model of §5.3 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp4.md#53-elements>).
//!
//! The rounds: the file's first 64 KiB, then each top-level box header the first read does not
//! hold, then the whole moov box (the sample tables index every sample, so the media data is
//! never read). Every box is a struct with its header, fields, table and children; each
//! track's samples are `data` elements under `samples/<T>`; the media data no sample claims
//! is gaps. The schema (`schema/mp4.json`) holds the checks that reject a file; whether a
//! track is virtualized is decided here, and recorded with the reason when it is not.

use crate::expr::V;
use crate::fed::{Fed, Word, uint};
use crate::ir::*;
use crate::nd2::Step;
use crate::rules::{self, Rules, flag, num, text};
use crate::sans;
use serde_json::{json, Value as J};

/// The first read, and the most bytes of a moov box (the schema's `max_moov`).
const HEAD: u64 = 1 << 16;
/// The boxes whose payload is boxes.
const CONTAINERS: [&[u8; 4]; 8] = [b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf", b"tref"];
/// The names of a box struct's own values, which a child box's name must not take.
const OWN: [&str; 8] = ["header", "fields", "fields_v1", "entries", "name", "data", "rest", "gaps"];
/// AAC's sampling frequencies by index (ISO/IEC 14496-3 §1.6.3.4).
const AAC_RATES: [u32; 13] = [96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350];
/// The H.264 profiles whose SPS carries the chroma format and bit depths (ITU-T H.264 §7.3.2.1.1).
const HIGH_PROFILES: [u8; 13] = [100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135];

/// Whether a file's first bytes are an MP4 or QuickTime file's (spec/virtualize.md §1.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize.md#12-input>): a box of at least 8
/// bytes whose type is `ftyp`, `moov`, `mdat`, `wide`, `free` or `skip`.
pub fn detect(head: &[u8]) -> bool {
    head.len() >= 8
        && u32::be_at(head, 0) != 0
        && [b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip"].iter().any(|t| &head[4..8] == *t)
        && (u32::be_at(head, 0) >= 8 || u32::be_at(head, 0) == 1)
}

/// A box's type as a name: printable ASCII but `/`, `~`, `%` and space as they are, every
/// other byte as `%XX` (so `©nam` is `%A9nam`).
fn type_name(t: &[u8]) -> String {
    t.iter().map(|&c| if (0x21..0x7F).contains(&c) && !b"/~%".contains(&c) { (c as char).to_string() } else { format!("%{c:02X}") }).collect()
}

fn type_text(t: &[u8]) -> String {
    t.iter().map(|&c| c as char).collect()
}

/// A box: its offset, header length, size and type.
#[derive(Clone, Copy, Debug)]
pub struct BoxRef {
    pub at: u64,
    pub head: u64,
    pub size: u64,
    pub typ: [u8; 4],
}

impl BoxRef {
    pub fn end(&self) -> u64 {
        self.at + self.size
    }
    pub fn body(&self) -> u64 {
        self.at + self.head
    }
}

/// A sample entry of a track's stsd box: its type, the fields the profile reads, and the
/// payloads of its child boxes by type.
#[derive(Default, Clone, Debug)]
struct Entry {
    typ: [u8; 4],
    width: u64,
    height: u64,
    channels: u64,
    rate: u64,
    sound_version: u64,
    children: Vec<([u8; 4], Vec<u8>)>,
}

impl Entry {
    /// The payload of the child box of type `t`, or of one in a QuickTime `wave` box (where
    /// QuickTime's sound descriptions keep their esds box).
    fn child(&self, t: &[u8; 4]) -> Option<&[u8]> {
        if let Some(c) = self.children.iter().find(|c| &c.0 == t) {
            return Some(c.1.as_slice());
        }
        let wave = &self.children.iter().find(|c| &c.0 == b"wave")?.1;
        let mut p = 0;
        while p + 8 <= wave.len() {
            let size = uint(wave, p, 4, false) as usize;
            if size < 8 || p + size > wave.len() {
                return None;
            }
            if &wave[p + 4..p + 8] == t {
                return Some(&wave[p + 8..p + size]);
            }
            p += size;
        }
        None
    }
}

/// What a trak box holds that the profile reads.
#[derive(Default, Clone, Debug)]
struct Track {
    id: u64,
    handler: [u8; 4],
    timescale: u64,
    duration: u64,
    edits: Vec<J>,
    entries: Vec<Entry>,
    stsd_count: u64,
    stts: Vec<(u64, u64)>,
    ctts: Vec<(u64, i64)>,
    stsc: Vec<(u64, u64, u64)>,
    sizes: Vec<u64>,
    fixed_size: Option<(u64, u64)>,
    offsets: Vec<u64>,
    sync: Option<Vec<u64>>,
    stz2: bool,
    has_stsz: bool,
}

#[derive(Clone, Copy, PartialEq, Debug)]
enum Phase {
    Start,
    Walk,
    Done,
}

pub struct Mp4 {
    size: u64,
    phase: Phase,
    fed: Fed,
    ir: Ir,
    rules: &'static Rules,
    facts: J,
    /// the position of the next top-level box, and the top-level boxes so far
    p: u64,
    tops: Vec<BoxRef>,
    moov: Option<Vec<u8>>,
    ftyp: Option<Vec<u8>>,
    /// the trak box being read, and those read
    cur: Option<Track>,
    tracks: Vec<Track>,
    movie: (u64, u64),
}

/// The fields of a sample entry of a video track (ISO/IEC 14496-12 §12.1.3).
const VISUAL: &str = "{reserved:u1[6],data_reference_index:>u2,pre_defined:>u2,reserved2:>u2,pre_defined2:>u4[3],width:>u2,height:>u2,horizresolution:>u4,vertresolution:>u4,reserved3:>u4,frame_count:>u2,compressorname:bytes[32],depth:>u2,pre_defined3:>i2}";
/// The fields of a sample entry of an audio track (ISO/IEC 14496-12 §12.2.3, and QuickTime's version 1 sound
/// description, whose four more fields follow).
const AUDIO: &str = "{reserved:u1[6],data_reference_index:>u2,version:>u2,revision:>u2,vendor:>u4,channel_count:>u2,sample_size:>u2,compression_id:>i2,packet_size:>u2,sample_rate:>u4}";
const AUDIO_V1: &str = "{samples_per_packet:>u4,bytes_per_packet:>u4,bytes_per_frame:>u4,bytes_per_sample:>u4}";
const FULL: &str = "{version:u1,flags:u1[3],entry_count:>u4}";

impl Mp4 {
    pub fn new(size: u64) -> Self {
        Mp4 {
            size, phase: Phase::Start, fed: Fed::default(), ir: Ir::new(size), rules: rules::of("mp4"), facts: J::Null,
            p: 0, tops: Vec::new(), moov: None, ftyp: None, cur: None, tracks: Vec::new(), movie: (0, 0),
        }
    }

    pub fn feed(&mut self, offset: u64, data: Vec<u8>) {
        self.fed.feed(offset, data);
    }

    pub fn step(&mut self) -> Result<Step, String> {
        match self.phase {
            Phase::Start => {
                self.rules.check("file", rules::env([("size", num(self.size as f64))]))?;
                self.phase = Phase::Walk;
                Ok(Step::Read(vec![(0, self.size.min(HEAD))]))
            }
            Phase::Walk => match self.walk()? {
                Some(r) => Ok(Step::Read(vec![r])),
                None => {
                    self.build()?;
                    self.phase = Phase::Done;
                    Ok(Step::Done)
                }
            },
            Phase::Done => Ok(Step::Done),
        }
    }

    /// The top-level boxes (spec/virtualize/mp4.md §8.1
    /// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp4.md#81-boxes>): the range to read
    /// next, or None when every one is known and the moov box (and the ftyp box) read.
    fn walk(&mut self) -> Res<Option<(u64, u64)>> {
        while self.p + 8 <= self.size {
            let n = (self.size - self.p).min(16);
            let Some(h) = self.fed.get(self.p, n) else { return Ok(Some((self.p, n))) };
            let b = header(h, self.p, self.size).ok_or("internal: no box header")?;
            self.check_box(&b, self.size)?;
            if &b.typ == b"moof" {
                self.rules.check("fragment", rules::env([("type", text("moof")), ("at", num(b.at as f64)), ("seen", flag(false))]))?;
            }
            if &b.typ == b"moov" {
                let moovs = self.tops.iter().filter(|t| &t.typ == b"moov").count() + 1;
                self.rules.check("movie", rules::env([
                    ("moovs", num(moovs as f64)), ("second", if moovs > 1 { num(b.at as f64) } else { V::Absent }),
                    ("size", num(b.size as f64)),
                ]))?;
                match self.fed.get(b.at, b.size) {
                    Some(m) => self.moov = Some(m.to_vec()),
                    None => return Ok(Some((b.at, b.size))),
                }
            }
            if &b.typ == b"ftyp" && b.size <= 4096 && self.ftyp.is_none() {
                match self.fed.get(b.at, b.size) {
                    Some(m) => self.ftyp = Some(m.to_vec()),
                    None => return Ok(Some((b.at, b.size))),
                }
            }
            self.tops.push(b);
            self.p = b.end();
        }
        let moovs = self.tops.iter().filter(|t| &t.typ == b"moov").count();
        self.rules.check("movie", rules::env([("moovs", num(moovs as f64)), ("second", V::Absent), ("size", num(0.0))]))?;
        Ok(None)
    }

    fn check_box(&self, b: &BoxRef, end: u64) -> Res<()> {
        self.rules.check("box", rules::env([
            ("type", text(&type_text(&b.typ))), ("at", num(b.at as f64)), ("size", num(b.size as f64)),
            ("head", num(b.head as f64)), ("end", num(end as f64)),
        ]))
    }

    /// The boxes in `[start, end)` of `buf` (whose byte 0 is at `base` in the file), each
    /// checked; and where the bytes that are no box start (8 bytes or fewer, or none).
    fn children(&self, buf: &[u8], base: u64, start: u64, end: u64) -> Res<(Vec<BoxRef>, u64)> {
        let mut out = Vec::new();
        let mut p = start;
        while p + 8 <= end {
            let h = &buf[(p - base) as usize..(end - base) as usize];
            let b = header(&h[..h.len().min(16)], p, end).ok_or("internal: no box header")?;
            self.check_box(&b, end)?;
            out.push(b);
            p = b.end();
        }
        Ok((out, p))
    }

    /// The IR: the top-level boxes in the file's order, the moov box's tree, then the samples.
    fn build(&mut self) -> Res<()> {
        let tops = self.tops.clone();
        let texts: Vec<String> = tops.iter().map(|b| type_name(&b.typ)).collect();
        let names = source_names(&texts, &["samples", "gaps"]);
        for (b, (name, nidx)) in tops.iter().zip(names) {
            let s = self.ir.struct_(0, &name, nidx, 0, Some((b.at, b.size)))?;
            let raw = self.fed.get(b.at, b.head).map(|r| r.to_vec());
            self.ir.value(s, "header", NO_INDEX, header_type(b.head), 0, (b.at, b.head), raw.as_deref())?;
            match &b.typ {
                b"moov" => {
                    let m = self.moov.take().ok_or("internal: the moov box was not read")?;
                    self.container(s, &m, b.at, b.body(), b.end())?;
                    self.moov = Some(m);
                }
                b"ftyp" if self.ftyp.is_some() => {
                    let f = self.ftyp.clone().unwrap();
                    let n = b.size - b.head;
                    if n >= 8 {
                        self.ir.value(s, "fields", NO_INDEX, "{major_brand:ascii[4],minor_version:>u4}", 0, (b.body(), 8), Some(&f[b.head as usize..b.head as usize + 8]))?;
                        let k = (n - 8) / 4;
                        if k > 0 {
                            let at = b.head as usize + 8;
                            self.ir.value(s, "entries", NO_INDEX, &format!("{{brand:ascii[4]}}[{k}]"), 0, (b.body() + 8, 4 * k), Some(&f[at..at + 4 * k as usize]))?;
                        }
                        self.rest(s, b.body() + 8 + 4 * k, b.end(), Some(&f[..]), b.at)?;
                    } else {
                        self.rest(s, b.body(), b.end(), Some(&f[..]), b.at)?;
                    }
                }
                b"mdat" => {}
                _ => {
                    if b.size > b.head {
                        let raw = self.fed.get(b.body(), b.size - b.head).map(|r| r.to_vec());
                        self.ir.value(s, "data", NO_INDEX, &format!("bytes[{}]", b.size - b.head), 0, (b.body(), b.size - b.head), raw.as_deref())?;
                    }
                }
            }
        }
        if self.p < self.size {
            // fewer than 8 bytes after the last box: a gap
        }
        self.samples()
    }

    /// The bytes `[from, to)` of a box that are none of its values: a `rest` value.
    fn rest(&mut self, parent: u32, from: u64, to: u64, buf: Option<&[u8]>, base: u64) -> Res<()> {
        if to > from {
            let raw = buf.map(|b| &b[(from - base) as usize..(to - base) as usize]);
            self.ir.value(parent, "rest", NO_INDEX, &format!("bytes[{}]", to - from), 0, (from, to - from), raw)?;
        }
        Ok(())
    }

    /// The child boxes of a container (or of a sample entry) in `[start, end)`, named by type.
    fn container(&mut self, parent: u32, m: &[u8], base: u64, start: u64, end: u64) -> Res<()> {
        let (kids, rest) = self.children(m, base, start, end)?;
        let texts: Vec<String> = kids.iter().map(|b| type_name(&b.typ)).collect();
        let names = source_names(&texts, &OWN);
        for (b, (name, nidx)) in kids.iter().zip(names) {
            self.boxed(parent, &name, nidx, b, m, base)?;
        }
        self.rest(parent, rest, end, Some(m), base)
    }

    /// One box of the moov box's tree and what it holds.
    fn boxed(&mut self, parent: u32, name: &str, nidx: u64, b: &BoxRef, m: &[u8], base: u64) -> Res<()> {
        let s = self.ir.struct_(parent, name, nidx, 0, Some((b.at, b.size)))?;
        let at = |o: u64| (o - base) as usize;
        self.ir.value(s, "header", NO_INDEX, header_type(b.head), 0, (b.at, b.head), Some(&m[at(b.at)..at(b.body())]))?;
        let body = &m[at(b.body())..at(b.end())];
        let n = body.len() as u64;
        match &b.typ {
            t if CONTAINERS.contains(&t) => {
                if t == b"trak" {
                    self.cur = Some(Track::default());
                }
                self.container(s, m, base, b.body(), b.end())?;
                if t == b"trak" {
                    let t = self.cur.take().unwrap_or_default();
                    self.tracks.push(t);
                }
            }
            b"mvex" => {
                self.rules.check("fragment", rules::env([("type", text("mvex")), ("at", num(b.at as f64)), ("seen", flag(false))]))?;
            }
            b"mvhd" | b"tkhd" | b"mdhd" => {
                let v1 = n > 0 && body[0] == 1;
                let (ty, need) = match (&b.typ, v1) {
                    (b"mvhd", false) => ("{version:u1,flags:u1[3],creation_time:>u4,modification_time:>u4,timescale:>u4,duration:>u4,rate:>i4,volume:>i2,reserved:u1[10],matrix:>i4[9],pre_defined:u1[24],next_track_id:>u4}", 100),
                    (b"mvhd", true) => ("{version:u1,flags:u1[3],creation_time:>u8,modification_time:>u8,timescale:>u4,duration:>u8,rate:>i4,volume:>i2,reserved:u1[10],matrix:>i4[9],pre_defined:u1[24],next_track_id:>u4}", 112),
                    (b"tkhd", false) => ("{version:u1,flags:u1[3],creation_time:>u4,modification_time:>u4,track_id:>u4,reserved:>u4,duration:>u4,reserved2:u1[8],layer:>i2,alternate_group:>i2,volume:>i2,reserved3:>u2,matrix:>i4[9],width:>u4,height:>u4}", 84),
                    (b"tkhd", true) => ("{version:u1,flags:u1[3],creation_time:>u8,modification_time:>u8,track_id:>u4,reserved:>u4,duration:>u8,reserved2:u1[8],layer:>i2,alternate_group:>i2,volume:>i2,reserved3:>u2,matrix:>i4[9],width:>u4,height:>u4}", 96),
                    (_, false) => ("{version:u1,flags:u1[3],creation_time:>u4,modification_time:>u4,timescale:>u4,duration:>u4,language:>u2,pre_defined:>u2}", 24),
                    (_, true) => ("{version:u1,flags:u1[3],creation_time:>u8,modification_time:>u8,timescale:>u4,duration:>u8,language:>u2,pre_defined:>u2}", 36),
                };
                if n < need {
                    return self.opaque(s, b, body);
                }
                self.ir.value(s, "fields", NO_INDEX, ty, 0, (b.body(), need), Some(&body[..need as usize]))?;
                self.rest(s, b.body() + need, b.end(), Some(m), base)?;
                let w = if v1 { 8 } else { 4 };
                match &b.typ {
                    b"mvhd" => self.movie = (uint(body, 4 + 2 * w, 4, false), uint(body, 8 + 2 * w, w, false)),
                    b"tkhd" => {
                        if let Some(t) = self.cur.as_mut() {
                            t.id = uint(body, 4 + 2 * w, 4, false);
                        }
                    }
                    _ => {
                        if let Some(t) = self.cur.as_mut() {
                            t.timescale = uint(body, 4 + 2 * w, 4, false);
                            t.duration = uint(body, 8 + 2 * w, w, false);
                        }
                    }
                }
            }
            b"hdlr" if n >= 24 => {
                self.ir.value(s, "fields", NO_INDEX, "{version:u1,flags:u1[3],pre_defined:>u4,handler_type:ascii[4],reserved:>u4[3]}", 0, (b.body(), 24), Some(&body[..24]))?;
                if n > 24 {
                    self.ir.value(s, "name", NO_INDEX, &format!("cstr[{}]", n - 24), 0, (b.body() + 24, n - 24), Some(&body[24..]))?;
                }
                // the track's handler is its mdia box's hdlr, the first in the trak box (a
                // QuickTime minf box holds another, the data handler's, of type `url ` or `alis`)
                if let Some(t) = self.cur.as_mut().filter(|t| t.handler == [0; 4]) {
                    t.handler = body[8..12].try_into().unwrap();
                }
            }
            b"elst" | b"stts" | b"ctts" | b"stsc" | b"stco" | b"co64" | b"stss" => self.table(s, b, m, base)?,
            b"stsz" => {
                self.rules.check("fields", rules::env([("type", text("stsz")), ("at", num(b.at as f64)), ("need", num(12.0)), ("have", num(n as f64))]))?;
                self.ir.value(s, "fields", NO_INDEX, "{version:u1,flags:u1[3],sample_size:>u4,sample_count:>u4}", 0, (b.body(), 12), Some(&body[..12]))?;
                let (fixed, count) = (uint(body, 4, 4, false), uint(body, 8, 4, false));
                let mut end = b.body() + 12;
                if fixed == 0 {
                    self.rules.check("table", rules::env([
                        ("type", text("stsz")), ("at", num(b.at as f64)), ("count", num(count as f64)), ("width", num(4.0)), ("have", num((n - 12) as f64)),
                    ]))?;
                    if count > 0 {
                        self.ir.value(s, "entries", NO_INDEX, &format!(">u4[{count}]"), 0, (end, 4 * count), Some(&body[12..12 + 4 * count as usize]))?;
                    }
                    if let Some(t) = self.cur.as_mut() {
                        t.sizes = (0..count as usize).map(|k| uint(body, 12 + 4 * k, 4, false)).collect();
                    }
                    end += 4 * count;
                } else if let Some(t) = self.cur.as_mut() {
                    t.fixed_size = Some((fixed, count));
                }
                if let Some(t) = self.cur.as_mut() {
                    t.has_stsz = true;
                }
                self.rest(s, end, b.end(), Some(m), base)?;
            }
            b"stz2" => {
                if let Some(t) = self.cur.as_mut() {
                    t.stz2 = true;
                }
                self.opaque(s, b, body)?;
            }
            b"stsd" => {
                self.rules.check("fields", rules::env([("type", text("stsd")), ("at", num(b.at as f64)), ("need", num(8.0)), ("have", num(n as f64))]))?;
                self.ir.value(s, "fields", NO_INDEX, FULL, 0, (b.body(), 8), Some(&body[..8]))?;
                let handler = self.cur.as_ref().map(|t| t.handler).unwrap_or(*b"    ");
                if let Some(t) = self.cur.as_mut() {
                    t.stsd_count = uint(body, 4, 4, false);
                }
                let (kids, rest) = self.children(m, base, b.body() + 8, b.end())?;
                let texts: Vec<String> = kids.iter().map(|k| type_name(&k.typ)).collect();
                for (k, (name, nidx)) in kids.iter().zip(source_names(&texts, &OWN)) {
                    self.entry(s, &name, nidx, k, m, base, &handler)?;
                }
                self.rest(s, rest, b.end(), Some(m), base)?;
            }
            _ => self.opaque(s, b, body)?,
        }
        Ok(())
    }

    /// A box whose payload the convention does not parse: one `data` value.
    fn opaque(&mut self, s: u32, b: &BoxRef, body: &[u8]) -> Res<()> {
        if !body.is_empty() {
            self.ir.value(s, "data", NO_INDEX, &format!("bytes[{}]", body.len()), 0, (b.body(), body.len() as u64), Some(body))?;
        }
        Ok(())
    }

    /// A full box of fields and a table: elst, stts, ctts, stsc, stco, co64 and stss.
    fn table(&mut self, s: u32, b: &BoxRef, m: &[u8], base: u64) -> Res<()> {
        let body = &m[(b.body() - base) as usize..(b.end() - base) as usize];
        let n = body.len() as u64;
        let t = type_text(&b.typ);
        self.rules.check("fields", rules::env([("type", text(&t)), ("at", num(b.at as f64)), ("need", num(8.0)), ("have", num(n as f64))]))?;
        let (version, count) = (body[0], uint(body, 4, 4, false));
        let (ty, width): (&str, u64) = match (&b.typ, version) {
            (b"elst", 1) => ("{segment_duration:>u8,media_time:>i8,media_rate:>i2,media_rate_fraction:>i2}", 20),
            (b"elst", _) => ("{segment_duration:>u4,media_time:>i4,media_rate:>i2,media_rate_fraction:>i2}", 12),
            (b"stts", _) => ("{sample_count:>u4,sample_delta:>u4}", 8),
            (b"ctts", 0) => ("{sample_count:>u4,sample_offset:>u4}", 8),
            (b"ctts", _) => ("{sample_count:>u4,sample_offset:>i4}", 8),
            (b"stsc", _) => ("{first_chunk:>u4,samples_per_chunk:>u4,sample_description_index:>u4}", 12),
            (b"co64", _) => (">u8", 8),
            _ => (">u4", 4),
        };
        self.rules.check("table", rules::env([
            ("type", text(&t)), ("at", num(b.at as f64)), ("count", num(count as f64)), ("width", num(width as f64)), ("have", num((n - 8) as f64)),
        ]))?;
        self.ir.value(s, "fields", NO_INDEX, FULL, 0, (b.body(), 8), Some(&body[..8]))?;
        let len = count * width;
        if count > 0 {
            self.ir.value(s, "entries", NO_INDEX, &format!("{ty}[{count}]"), 0, (b.body() + 8, len), Some(&body[8..8 + len as usize]))?;
        }
        self.rest(s, b.body() + 8 + len, b.end(), Some(m), base)?;
        let e = &body[8..8 + len as usize];
        let k = count as usize;
        let w = width as usize;
        let signed = |x: u64, bits: u32| ((x << (64 - bits)) as i64) >> (64 - bits);
        let Some(tr) = self.cur.as_mut() else { return Ok(()) };
        match &b.typ {
            b"elst" => {
                let d = if version == 1 { 8 } else { 4 };
                tr.edits = (0..k).map(|i| {
                    let r = &e[i * w..];
                    json!({"duration": uint(r, 0, d, false), "media_time": signed(uint(r, d, d, false), 8 * d as u32), "rate": signed(uint(r, 2 * d, 2, false), 16) as f64 + uint(r, 2 * d + 2, 2, false) as f64 / 65536.0})
                }).collect();
            }
            b"stts" => tr.stts = (0..k).map(|i| (uint(e, i * 8, 4, false), uint(e, i * 8 + 4, 4, false))).collect(),
            b"ctts" => tr.ctts = (0..k).map(|i| (uint(e, i * 8, 4, false), if version == 0 { uint(e, i * 8 + 4, 4, false) as i64 } else { signed(uint(e, i * 8 + 4, 4, false), 32) })).collect(),
            b"stsc" => tr.stsc = (0..k).map(|i| (uint(e, i * 12, 4, false), uint(e, i * 12 + 4, 4, false), uint(e, i * 12 + 8, 4, false))).collect(),
            b"stco" => tr.offsets = (0..k).map(|i| uint(e, i * 4, 4, false)).collect(),
            b"co64" => tr.offsets = (0..k).map(|i| uint(e, i * 8, 8, false)).collect(),
            _ => tr.sync = Some((0..k).map(|i| uint(e, i * 4, 4, false)).collect()),
        }
        Ok(())
    }

    /// A sample entry of an stsd box: its fields, by the track's handler, and its child boxes.
    #[allow(clippy::too_many_arguments)]
    fn entry(&mut self, parent: u32, name: &str, nidx: u64, b: &BoxRef, m: &[u8], base: u64, handler: &[u8; 4]) -> Res<()> {
        let s = self.ir.struct_(parent, name, nidx, 0, Some((b.at, b.size)))?;
        let at = |o: u64| (o - base) as usize;
        self.ir.value(s, "header", NO_INDEX, header_type(b.head), 0, (b.at, b.head), Some(&m[at(b.at)..at(b.body())]))?;
        let body = &m[at(b.body())..at(b.end())];
        let n = body.len() as u64;
        let mut e = Entry { typ: b.typ, ..Default::default() };
        let fields = match handler {
            b"vide" if n >= 78 => {
                self.ir.value(s, "fields", NO_INDEX, VISUAL, 0, (b.body(), 78), Some(&body[..78]))?;
                e.width = uint(body, 24, 2, false);
                e.height = uint(body, 26, 2, false);
                Some(78)
            }
            b"soun" if n >= 28 => {
                self.ir.value(s, "fields", NO_INDEX, AUDIO, 0, (b.body(), 28), Some(&body[..28]))?;
                e.sound_version = uint(body, 8, 2, false);
                e.channels = uint(body, 16, 2, false);
                e.rate = uint(body, 24, 4, false) >> 16;
                if e.sound_version == 1 && n >= 44 {
                    self.ir.value(s, "fields_v1", NO_INDEX, AUDIO_V1, 0, (b.body() + 28, 16), Some(&body[28..44]))?;
                    Some(44)
                } else if e.sound_version == 0 {
                    Some(28)
                } else {
                    None
                }
            }
            _ => None,
        };
        match fields {
            Some(f) => {
                let start = b.body() + f;
                let (kids, rest) = self.children(m, base, start, b.end())?;
                let texts: Vec<String> = kids.iter().map(|k| type_name(&k.typ)).collect();
                for (k, (cname, cidx)) in kids.iter().zip(source_names(&texts, &OWN)) {
                    let c = self.ir.struct_(s, &cname, cidx, 0, Some((k.at, k.size)))?;
                    self.ir.value(c, "header", NO_INDEX, header_type(k.head), 0, (k.at, k.head), Some(&m[at(k.at)..at(k.body())]))?;
                    let kb = &m[at(k.body())..at(k.end())];
                    self.opaque(c, k, kb)?;
                    e.children.push((k.typ, kb.to_vec()));
                }
                self.rest(s, rest, b.end(), Some(m), base)?;
            }
            None => self.opaque(s, b, body)?,
        }
        if let Some(t) = self.cur.as_mut() {
            t.entries.push(e);
        }
        Ok(())
    }

    /// Each track's samples, from its tables (spec/virtualize/mp4.md §8.2
    /// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp4.md#82-tracks>), as `data`
    /// elements; whether the profile virtualizes it, and the facts its projection reads.
    fn samples(&mut self) -> Res<()> {
        let tracks = std::mem::take(&mut self.tracks);
        let mut samples_struct = None;
        let mut facts = Vec::new();
        let mut reasons = Vec::new();
        let mut projected = 0;
        for (k, t) in tracks.iter().enumerate() {
            let (offsets, sizes) = self.layout(t)?;
            let mut f = self.classify(t, &offsets);
            f["index"] = json!(k);
            if !offsets.is_empty() {
                let ss = match samples_struct {
                    Some(s) => s,
                    None => {
                        let s = self.ir.struct_(0, "samples", NO_INDEX, 0, None)?;
                        samples_struct = Some(s);
                        s
                    }
                };
                let ts = self.ir.struct_(ss, "", k as u64, 0, None)?;
                let form = json!({
                    "geometry": f["geometry"].clone(),
                    "codec": {"name": type_text(&t.entries.first().map(|e| e.typ).unwrap_or(*b"    ")), "format": f["format"].clone()},
                    "recipe": [["src", 0, null]],
                }).to_string();
                let mut first = None;
                for (j, (&o, &n)) in offsets.iter().zip(&sizes).enumerate() {
                    self.rules.check("sample", rules::env([
                        ("track", num(t.id as f64)), ("j", num(j as f64)), ("offset", num(o as f64)), ("length", num(n as f64)), ("size", num(self.size as f64)),
                    ]))?;
                    let i = self.ir.data(ts, "", j as u64, 0, (o, n), &form)?;
                    first.get_or_insert(i);
                }
                f["first"] = json!(first);
            }
            if f["reason"].is_null() {
                projected += 1;
            } else {
                reasons.push(format!("track {} ({}): {}", t.id, type_text(&t.entries.first().map(|e| e.typ).unwrap_or(*b"none")).trim(), f["reason"].as_str().unwrap_or("")));
            }
            facts.push(f);
        }
        self.rules.check("tracks", rules::env([("projected", num(projected as f64)), ("reasons", text(&reasons.join("; ")))]))?;
        let brand = self.ftyp.as_ref().filter(|f| f.len() >= 16).map(|f| {
            let head = if uint(f, 0, 4, false) == 1 { 16 } else { 8 };
            let body = &f[head..];
            json!({
                "major": type_text(&body[0..4]), "minor": uint(body, 4, 4, false),
                "compatible": body[8..].chunks_exact(4).map(type_text).collect::<Vec<_>>(),
            })
        });
        self.facts = json!({
            "brand": brand, "timescale": self.movie.0, "duration": self.movie.1, "tracks": facts,
            "chunk_frames": crate::mp3::chunk_frames(),
        });
        Ok(())
    }

    /// Each sample's (offset, size), in decode order, from the stsz, stsc and stco or co64
    /// tables; the tables' sample counts checked against each other.
    fn layout(&self, t: &Track) -> Res<(Vec<u64>, Vec<u64>)> {
        if !t.has_stsz {
            return Ok((Vec::new(), Vec::new()));
        }
        let n = match t.fixed_size {
            Some((_, count)) => count,
            None => t.sizes.len() as u64,
        };
        self.rules.check("samples", rules::env([("track", num(t.id as f64)), ("n", num(n as f64))]))?;
        let size_of = |j: u64| match t.fixed_size {
            Some((s, _)) => s,
            None => t.sizes[j as usize],
        };
        let counts = |what: &'static str, m: u64| {
            self.rules.check("counts", rules::env([("track", num(t.id as f64)), ("what", text(what)), ("n", num(n as f64)), ("m", num(m as f64))]))
        };
        counts("stts box", t.stts.iter().map(|e| e.0).sum())?;
        if !t.ctts.is_empty() {
            counts("ctts box", t.ctts.iter().map(|e| e.0).sum())?;
        }
        let chunks = t.offsets.len() as u64;
        let bad = |detail: String| self.rules.check("chunks", rules::env([("track", num(t.id as f64)), ("ok", flag(false)), ("detail", text(&detail))]));
        if n > 0 && t.stsc.is_empty() {
            bad("is empty".into())?;
        }
        for (i, e) in t.stsc.iter().enumerate() {
            let next = t.stsc.get(i + 1).map_or(chunks + 1, |x| x.0);
            if e.0 < 1 || (i == 0 && e.0 != 1) || next <= e.0 || e.0 > chunks {
                bad(format!("entry {i} starts at chunk {}, which does not follow the one before it among the {chunks} chunks", e.0))?;
            }
            if e.1 == 0 {
                bad(format!("entry {i} gives 0 samples per chunk"))?;
            }
        }
        let mut offsets = Vec::with_capacity(n as usize);
        let mut sizes = Vec::with_capacity(n as usize);
        let mut j = 0u64;
        for (i, e) in t.stsc.iter().enumerate() {
            let next = t.stsc.get(i + 1).map_or(chunks + 1, |x| x.0);
            for c in e.0..next {
                let mut o = t.offsets[(c - 1) as usize];
                for _ in 0..e.1 {
                    if j >= n {
                        return counts("sample-to-chunk table", j + 1).map(|_| (offsets.clone(), sizes.clone()));
                    }
                    let s = size_of(j);
                    offsets.push(o);
                    sizes.push(s);
                    o += s;
                    j += 1;
                }
            }
        }
        counts("sample-to-chunk table", j)?;
        Ok((offsets, sizes))
    }

    /// Whether the profile virtualizes the track, and the facts its projection reads
    /// (spec/virtualize/mp4.md §8.2
    /// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp4.md#82-tracks>): `reason` is
    /// null for a track it virtualizes, else why it does not.
    fn classify(&self, t: &Track, offsets: &[u64]) -> J {
        let entry = t.entries.first();
        let codec = entry.map(|e| type_text(&e.typ)).unwrap_or_default();
        let mut f = json!({
            "id": t.id, "handler": type_text(&t.handler), "codec": codec, "timescale": t.timescale, "duration": t.duration,
            "edits": t.edits, "count": offsets.len(), "format": null, "geometry": null, "reason": null,
        });
        let reason = (|| -> Result<(), String> {
            let kind = match &t.handler {
                b"vide" => "video",
                b"soun" => "audio",
                h => return Err(format!("a {} track", type_text(h).trim())),
            };
            if t.stz2 {
                return Err("compact sample sizes (stz2) are not supported".into());
            }
            if offsets.is_empty() {
                return Err("no samples".into());
            }
            if t.stsd_count != 1 || t.entries.len() != 1 || t.stsc.iter().any(|e| e.2 != 1) {
                return Err("more than one sample description".into());
            }
            if t.timescale == 0 {
                return Err("a media timescale of 0".into());
            }
            let e = entry.unwrap();
            if kind == "video" {
                if &e.typ != b"avc1" {
                    return Err(format!("{} video is not supported: only H.264 (avc1) is", codec.trim()));
                }
                let avcc = e.child(b"avcC").ok_or("an avc1 sample entry without an avcC box")?;
                let sps = h264_sps(avcc).ok_or("an avcC box without a parameter set the profile reads")?;
                if sps.chroma != 1 || sps.luma != 8 || sps.chroma_depth != 8 {
                    return Err(format!("H.264 with chroma format {} and bit depth {} is not supported: only 8-bit 4:2:0 is", sps.chroma, sps.luma));
                }
                if e.width == 0 || e.height == 0 {
                    return Err("a sample entry of width or height 0".into());
                }
                let pts = presentation(t);
                let sync = t.sync.clone().map(|s| s.iter().map(|&x| x.saturating_sub(1)).collect::<Vec<u64>>()).unwrap_or_else(|| (0..offsets.len() as u64).collect());
                gops(&pts, &sync)?;
                f["format"] = json!("h264");
                f["geometry"] = json!({"width": e.width, "height": e.height, "planes": 3, "data_type": "uint8"});
                f["setup"] = json!(crate::mirror::base64(avcc));
                f["profile"] = json!(sps.profile);
                f["level"] = json!(sps.level);
                f["pts"] = json!(pts);
                f["sync"] = json!(sync);
            } else {
                if e.sound_version > 1 {
                    return Err(format!("a version {} QuickTime sound description", e.sound_version));
                }
                let (format, setup, rate, channels, spf) = match &e.typ {
                    b"mp4a" => {
                        let esds = e.child(b"esds").ok_or("an mp4a sample entry without an esds box")?;
                        let (oti, asc) = esds_config(esds).ok_or("an esds box the profile cannot read")?;
                        match oti {
                            0x40 | 0x66 | 0x67 | 0x68 => {
                                let a = aac_config(&asc).ok_or("an AudioSpecificConfig the profile cannot read")?;
                                if a.aot != 2 {
                                    return Err(format!("AAC object type {} is not supported: only AAC-LC (2) is", a.aot));
                                }
                                if a.short {
                                    return Err("960-sample AAC frames are not supported".into());
                                }
                                ("aac", Some(asc), a.rate, if a.channels > 0 { a.channels } else { e.channels }, 1024)
                            }
                            0x69 | 0x6B => ("mp3", None, e.rate, e.channels, if e.rate >= 32000 { 1152 } else { 576 }),
                            x => return Err(format!("audio of object type indication 0x{x:02x} is not supported")),
                        }
                    }
                    b".mp3" => ("mp3", None, e.rate, e.channels, if e.rate >= 32000 { 1152 } else { 576 }),
                    _ => return Err(format!("{} audio is not supported: only AAC and MP3 are", codec.trim())),
                };
                if rate == 0 || channels == 0 {
                    return Err("a sample rate or channel count of 0".into());
                }
                f["format"] = json!(format);
                f["geometry"] = json!({"samples": spf, "channels": channels, "data_type": "float32"});
                if let Some(s) = setup {
                    f["setup"] = json!(crate::mirror::base64(&s));
                }
                f["sample_rate"] = json!(rate);
                f["channels"] = json!(channels);
                f["spf"] = json!(spf);
            }
            Ok(())
        })();
        if let Err(r) = reason {
            f["reason"] = json!(r);
            f["format"] = J::Null;
        }
        f
    }

    pub fn finish(self) -> Result<(Ir, J), String> {
        sans::finish(self.phase == Phase::Done, self.ir, self.facts)
    }
}

/// The box header at the start of `h` (at `at` in the file, in a parent that ends at `end`):
/// a 32-bit size and the type, then a 64-bit size when the size is 1; a size of 0 runs to
/// `end`.
fn header(h: &[u8], at: u64, end: u64) -> Option<BoxRef> {
    if h.len() < 8 {
        return None;
    }
    let size = uint(h, 0, 4, false);
    let typ: [u8; 4] = h[4..8].try_into().unwrap();
    Some(match size {
        1 if h.len() >= 16 => BoxRef { at, head: 16, size: uint(h, 8, 8, false), typ },
        1 => BoxRef { at, head: 16, size: 0, typ },
        0 => BoxRef { at, head: 8, size: end - at, typ },
        s => BoxRef { at, head: 8, size: s, typ },
    })
}

fn header_type(head: u64) -> &'static str {
    if head == 16 { "{size:>u4,type:ascii[4],largesize:>u8}" } else { "{size:>u4,type:ascii[4]}" }
}

/// Each sample's presentation time, in decode order: the decode time from stts, plus the
/// composition offset of ctts.
fn presentation(t: &Track) -> Vec<i64> {
    let mut out = Vec::new();
    let mut d = 0i64;
    for &(count, delta) in &t.stts {
        for _ in 0..count {
            out.push(d);
            d += delta as i64;
        }
    }
    let mut j = 0;
    for &(count, off) in &t.ctts {
        for _ in 0..count {
            if j < out.len() {
                out[j] += off;
            }
            j += 1;
        }
    }
    out
}

/// Whether the groups of pictures are closed (spec/virtualize/mp4.md §8.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp4.md#82-tracks>): the first sample
/// is a sync sample, no two samples share a presentation time, no group is longer than
/// `max_group` samples, and every sample before a sync sample in decode order is presented
/// before it and every sample after it is presented after it.
pub fn gops(pts: &[i64], sync: &[u64]) -> Result<(), String> {
    let n = pts.len();
    if sync.first() != Some(&0) {
        return Err("the first sample is not a sync sample".into());
    }
    let mut sorted = pts.to_vec();
    sorted.sort_unstable();
    if sorted.windows(2).any(|w| w[0] == w[1]) {
        return Err("two samples share a presentation time".into());
    }
    let limit = rules::of("mp4").limit("max_group") as u64;
    let mut prev = 0u64;
    for &s in sync.iter().skip(1).chain(std::iter::once(&(n as u64))) {
        if s > prev + limit {
            return Err(format!("a group of pictures of {} samples, more than {limit}", s - prev));
        }
        prev = s;
    }
    let mut suffix_min = vec![i64::MAX; n + 1];
    for d in (0..n).rev() {
        suffix_min[d] = suffix_min[d + 1].min(pts[d]);
    }
    let mut prefix_max = i64::MIN;
    let mut next = 0usize;
    for d in 0..n {
        if next < sync.len() && sync[next] as usize == d {
            next += 1;
            if pts[d] < prefix_max || suffix_min[d] < pts[d] {
                let lead = (d..n).find(|&x| pts[x] < pts[d]).unwrap_or(d);
                return Err(format!("an open group of pictures: sample {lead}, after the sync sample {d} in decode order, is presented before it"));
            }
        }
        prefix_max = prefix_max.max(pts[d]);
    }
    Ok(())
}

/// The fields of an H.264 sequence parameter set the profile reads.
#[derive(Debug, PartialEq)]
pub struct Sps {
    pub profile: u8,
    pub level: u8,
    pub chroma: u64,
    pub luma: u64,
    pub chroma_depth: u64,
}

/// The first SPS of an avcC box (ISO/IEC 14496-15 §5.3.3.1): its profile and level, and, for
/// the high profiles, its chroma format and bit depths (else 4:2:0 and 8 bits).
pub fn h264_sps(avcc: &[u8]) -> Option<Sps> {
    if avcc.len() < 8 || avcc[5] & 0x1F == 0 {
        return None;
    }
    let len = uint(avcc, 6, 2, false) as usize;
    let nal = avcc.get(8..8 + len)?;
    if nal.len() < 4 || nal[0] & 0x1F != 7 {
        return None;
    }
    // the RBSP: the NAL unit without its header and its emulation prevention bytes
    let mut rbsp = Vec::with_capacity(nal.len());
    let mut zeros = 0;
    for &c in &nal[1..] {
        if zeros >= 2 && c == 3 {
            zeros = 0;
            continue;
        }
        zeros = if c == 0 { zeros + 1 } else { 0 };
        rbsp.push(c);
    }
    let (profile, level) = (rbsp[0], rbsp[2]);
    let mut r = Bits { b: &rbsp[3..], at: 0 };
    r.ue()?;
    let (mut chroma, mut luma, mut chroma_depth) = (1, 8, 8);
    if HIGH_PROFILES.contains(&profile) {
        chroma = r.ue()?;
        if chroma == 3 {
            r.bit()?;
        }
        luma = r.ue()? + 8;
        chroma_depth = r.ue()? + 8;
    }
    Some(Sps { profile, level, chroma, luma, chroma_depth })
}

struct Bits<'a> {
    b: &'a [u8],
    at: usize,
}

impl Bits<'_> {
    fn bit(&mut self) -> Option<u64> {
        let byte = *self.b.get(self.at / 8)?;
        let v = (byte >> (7 - self.at % 8)) & 1;
        self.at += 1;
        Some(v as u64)
    }
    fn bits(&mut self, n: u32) -> Option<u64> {
        (0..n).try_fold(0u64, |a, _| Some(a << 1 | self.bit()?))
    }
    /// An unsigned exp-Golomb code.
    fn ue(&mut self) -> Option<u64> {
        let mut zeros = 0;
        while self.bit()? == 0 {
            zeros += 1;
            if zeros > 31 {
                return None;
            }
        }
        Some((1u64 << zeros) - 1 + self.bits(zeros)?)
    }
}

/// The object type indication and the decoder specific info (an AudioSpecificConfig) of an
/// esds box's ES descriptor (ISO/IEC 14496-1 §7.2.6.5).
pub fn esds_config(esds: &[u8]) -> Option<(u8, Vec<u8>)> {
    fn descriptor(b: &[u8], at: usize) -> Option<(u8, usize, usize)> {
        let tag = *b.get(at)?;
        let mut len = 0usize;
        let mut p = at + 1;
        for _ in 0..4 {
            let c = *b.get(p)?;
            p += 1;
            len = len << 7 | (c & 0x7F) as usize;
            if c & 0x80 == 0 {
                break;
            }
        }
        Some((tag, p, len))
    }
    let (tag, p, _) = descriptor(esds, 4)?;
    if tag != 3 {
        return None;
    }
    let flags = *esds.get(p + 2)?;
    let mut q = p + 3;
    if flags & 0x80 != 0 {
        q += 2;
    }
    if flags & 0x40 != 0 {
        q += 1 + *esds.get(q)? as usize;
    }
    if flags & 0x20 != 0 {
        q += 2;
    }
    let (tag, p, len) = descriptor(esds, q)?;
    if tag != 4 || len < 13 {
        return None;
    }
    let oti = *esds.get(p)?;
    let asc = match descriptor(esds, p + 13) {
        Some((5, s, n)) => esds.get(s..s + n)?.to_vec(),
        _ => Vec::new(),
    };
    Some((oti, asc))
}

/// The fields of an AudioSpecificConfig the profile reads (ISO/IEC 14496-3 §1.6.2.1).
#[derive(Debug, PartialEq)]
pub struct Asc {
    pub aot: u64,
    pub rate: u64,
    pub channels: u64,
    pub short: bool,
}

pub fn aac_config(asc: &[u8]) -> Option<Asc> {
    let mut r = Bits { b: asc, at: 0 };
    let mut aot = r.bits(5)?;
    if aot == 31 {
        aot = 32 + r.bits(6)?;
    }
    let index = r.bits(4)?;
    let rate = if index == 15 { r.bits(24)? } else { *AAC_RATES.get(index as usize)? as u64 };
    let channels = r.bits(4)?;
    let short = if aot == 2 { r.bit()? == 1 } else { false };
    Some(Asc { aot, rate, channels, short })
}

crate::parser!(Mp4);

/// The MP4 profile's file run (spec/virtualize/mp4.md): the top-level box headers, then the moov
/// box.
pub struct Profile;

impl crate::run::FileProfile for Profile {
    /// A box at offset 0 of one of the top-level types (`detect`).
    fn detect(&self, head: &[u8]) -> bool {
        detect(head)
    }
    fn parser(&self, size: u64) -> crate::run::AnyParser {
        crate::run::AnyParser::of::<Mp4>(size)
    }
    /// It takes exactly the ranges it asked for.
    fn whole(&self) -> bool {
        false
    }
    fn project(&self, ir: &crate::ir::Ir, facts: &serde_json::Value, url: &str, copied: &crate::project::Copied) -> Result<crate::out::Out, String> {
        crate::project::mp4::project(ir, facts, url, copied)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Closed groups pass; a sample after a sync sample presented before it, a first sample
    /// that is not a sync sample, and two samples of one time fail.
    #[test]
    fn groups_of_pictures_are_closed() {
        // I0 P3 B1 B2 | I4 P7 B5 B6, in decode order
        assert_eq!(gops(&[0, 3, 1, 2, 4, 7, 5, 6], &[0, 4]), Ok(()));
        // I4 is followed in decode order by B3, presented before it: open
        assert!(gops(&[0, 2, 1, 4, 3, 5], &[0, 3]).unwrap_err().starts_with("an open group of pictures: sample 4"));
        assert_eq!(gops(&[0, 1], &[1]), Err("the first sample is not a sync sample".into()));
        assert_eq!(gops(&[0, 0], &[0]), Err("two samples share a presentation time".into()));
    }

    /// x264's avcC boxes for a High profile 8-bit and a High 10 profile 10-bit 4:2:0 stream
    /// (the fixtures mp4_h264_bframes.mp4 and mp4_h264_10bit_aac.mp4), and an AAC-LC mono
    /// 16 kHz config.
    #[test]
    fn parameter_sets_give_the_format() {
        let eight = [0x01, 0x64, 0x00, 0x0A, 0xFF, 0xE1, 0x00, 0x18, 0x67, 0x64, 0x00, 0x0A, 0xAC, 0xD9, 0x44, 0x7B, 0x01, 0x10, 0x00, 0x00, 0x03,
                     0x00, 0x10, 0x00, 0x00, 0x03, 0x01, 0x40, 0xF1, 0x22, 0x59, 0x60, 0x01, 0x00, 0x06, 0x68, 0xEB, 0xE3, 0xCB, 0x22, 0xC0];
        assert_eq!(h264_sps(&eight), Some(Sps { profile: 100, level: 10, chroma: 1, luma: 8, chroma_depth: 8 }));
        let ten = [0x01, 0x6E, 0x00, 0x0A, 0xFF, 0xE1, 0x00, 0x18, 0x67, 0x6E, 0x00, 0x0A, 0xA6, 0xCD, 0x94, 0x47, 0xB0, 0x11, 0x00, 0x00, 0x03,
                   0x00, 0x01, 0x00, 0x00, 0x03, 0x00, 0x14, 0x0F, 0x12, 0x25, 0x96, 0x01, 0x00, 0x06, 0x68, 0xEB, 0xE3, 0xCB, 0x22, 0xC0];
        assert_eq!(h264_sps(&ten), Some(Sps { profile: 110, level: 10, chroma: 1, luma: 10, chroma_depth: 10 }));
        assert_eq!(aac_config(&[0x14, 0x08]), Some(Asc { aot: 2, rate: 16000, channels: 1, short: false }));
    }
}
