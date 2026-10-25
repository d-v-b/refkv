//! A sans-IO parser of FLAC files (RFC 9639 <https://www.rfc-editor.org/rfc/rfc9639>) into the IR
//! (spec/zarrify/flac.md §8
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/flac.md#8-flac-profile>, and the source
//! model of §5.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/flac.md#53-elements>).
//!
//! The rounds: the file's first 64 KiB and its last 128 bytes (an ID3v1 tag), then what the
//! metadata blocks need that the first read does not hold (a block's header; the bodies the
//! profile decodes, and a picture's fields, not its data, nor padding nor an application's
//! data), then the frames in windows of 16 MiB, each frame's end found by its CRC-16 and the
//! next frame's header (a frame header gives no length). The schema (`schema/flac.json`) holds
//! every check, in the profile's order.

use crate::expr::V;
use crate::fed::{Fed, Overlap, Word};
use crate::ir::*;
use crate::nd2::Step;
use crate::rules::{self, Rules, flag, num, text};
use crate::sans::{self, Asked};
use crate::text::text_json;
use serde_json::{json, Value as J};
use std::sync::atomic::{AtomicUsize, Ordering};

/// The first read, and the most a metadata read asks for beyond what it needs.
const HEAD: u64 = 1 << 16;
/// The bytes of frames a round reads (`Flac::with_window`, in tests).
pub const WINDOW: u64 = 1 << 24;
/// The longest a frame header is: sync and codes (4), a 7-byte coded number, an uncommon
/// block size and sample rate (2 each), the CRC-8.
const MAX_HEADER: u64 = 16;

/// The metadata block types' names, by type (RFC 9639 §8.1
/// <https://www.rfc-editor.org/rfc/rfc9639#section-8.1>).
pub const BLOCKS: [&str; 7] = ["STREAMINFO", "PADDING", "APPLICATION", "SEEKTABLE", "VORBIS_COMMENT", "CUESHEET", "PICTURE"];
/// A metadata block's 4-byte header as one record type (conventions §8.6
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#86-types>).
pub const BLOCK_HEADER_TYPE: &str = "{type:u1,length:u1[3]}";
/// The STREAMINFO block's body.
pub const STREAMINFO_TYPE: &str = "{min_block_size:>u2,max_block_size:>u2,min_frame_size:u1[3],max_frame_size:u1[3],rate_channels_bits_samples:>u8,md5:bytes[16]}";
/// A seek point (RFC 9639 §8.5.1).
pub const SEEK_POINT_TYPE: &str = "{sample:>u8,offset:>u8,samples:>u2}";
/// An ID3v2 tag's 10-byte header (as the MP3 profile's).
pub const ID3V2_HEADER_TYPE: &str = crate::mp3::ID3V2_HEADER_TYPE;
/// The ID3v1 tag (as the MP3 profile's).
pub const ID3V1_TYPE: &str = crate::mp3::ID3V1_TYPE;

/// The frames of a shard (spec/zarrify/flac.md §4.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/flac.md#41-the-array>), but in tests
/// (`set_shard_frames`). Each run reads it once, into its facts.
pub const DEFAULT_SHARD_FRAMES: usize = 512;
static SHARD_FRAMES: AtomicUsize = AtomicUsize::new(DEFAULT_SHARD_FRAMES);

/// For tests: the frames of a shard (None: the default, 512), so that small files have several.
pub fn set_shard_frames(n: Option<usize>) {
    SHARD_FRAMES.store(n.unwrap_or(DEFAULT_SHARD_FRAMES).max(1), Ordering::Relaxed);
}

pub fn shard_frames() -> usize {
    SHARD_FRAMES.load(Ordering::Relaxed)
}

/// The CRC-8 of a frame header (RFC 9639 §9.1.8): polynomial x^8 + x^2 + x + 1, initial 0.
pub fn crc8(b: &[u8]) -> u8 {
    let mut c = 0u8;
    for &x in b {
        c ^= x;
        for _ in 0..8 {
            c = if c & 0x80 != 0 { (c << 1) ^ 0x07 } else { c << 1 };
        }
    }
    c
}

/// The CRC-16 table of a frame (RFC 9639 §9.3): polynomial x^16 + x^15 + x^2 + 1, initial 0.
const CRC16: [u16; 256] = {
    let mut t = [0u16; 256];
    let mut i = 0;
    while i < 256 {
        let mut c = (i as u16) << 8;
        let mut k = 0;
        while k < 8 {
            c = if c & 0x8000 != 0 { (c << 1) ^ 0x8005 } else { c << 1 };
            k += 1;
        }
        t[i] = c;
        i += 1;
    }
    t
};

/// The CRC-16 `c` continued over `b`: over a frame, its footer included, it is 0.
pub fn crc16(c: u16, b: &[u8]) -> u16 {
    b.iter().fold(c, |c, &x| (c << 8) ^ CRC16[((c >> 8) as u8 ^ x) as usize])
}

/// A frame header's fields (RFC 9639 §9.1 <https://www.rfc-editor.org/rfc/rfc9639#section-9.1>).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct Head {
    /// its length in bytes, the CRC-8 included
    pub len: u64,
    /// the blocking strategy bit: a variable block size stream
    pub variable: bool,
    pub block: u64,
    /// the sample rate, or None: the STREAMINFO block's
    pub rate: Option<u64>,
    /// the channels bits (0 to 10)
    pub assignment: u8,
    /// the bit depth, or None: the STREAMINFO block's
    pub bits: Option<u64>,
    /// the coded number: a frame number, or for a variable block size a sample number
    pub number: u64,
}

impl Head {
    pub fn channels(&self) -> u64 {
        if self.assignment < 8 { self.assignment as u64 + 1 } else { 2 }
    }

    /// The header at the start of `b`: Ok when `b` starts with a valid one (the sync code,
    /// no reserved or forbidden code, a coded number of the extended UTF-8 form, and its
    /// CRC-8), else what is wrong with it; None when `b` ends before the header does.
    pub fn parse(b: &[u8]) -> Option<Result<Head, &'static str>> {
        if b.len() < 2 {
            return None;
        }
        if b[0] != 0xFF || b[1] & 0xFE != 0xF8 {
            return Some(Err("no frame sync code"));
        }
        if b.len() < 5 {
            return None;
        }
        let (bs, sr, ch, bd) = (b[2] >> 4, b[2] & 15, b[3] >> 4, (b[3] >> 1) & 7);
        let why = if bs == 0 {
            "a reserved block size"
        } else if sr == 15 {
            "a forbidden sample rate"
        } else if ch > 10 {
            "a reserved channel assignment"
        } else if bd == 3 {
            "a reserved bit depth"
        } else if b[3] & 1 != 0 {
            "a reserved bit set"
        } else {
            ""
        };
        if !why.is_empty() {
            return Some(Err(why));
        }
        // the coded number: extended UTF-8 (RFC 9639 §9.1.5), at most 7 bytes
        let first = b[4];
        let (n, mut number) = match first.leading_ones() {
            0 => (1, first as u64),
            k @ 2..=7 => (k as usize, (first & (0x7F >> k)) as u64),
            _ => return Some(Err("an invalid coded number")),
        };
        if b.len() < 4 + n {
            return None;
        }
        for &c in &b[5..4 + n] {
            if c & 0xC0 != 0x80 {
                return Some(Err("an invalid coded number"));
            }
            number = number << 6 | (c & 0x3F) as u64;
        }
        let mut at = 4 + n;
        let extra = |code: u8, eight: u8, sixteen: u8| if code == eight { 1 } else if code == sixteen { 2 } else { 0 };
        let (bsn, srn) = (extra(bs, 6, 7), extra(sr, 12, 13).max(extra(sr, 12, 14)));
        if b.len() < at + bsn + srn + 1 {
            return None;
        }
        let block = match bs {
            1 => 192,
            2..=5 => 144 << bs,
            6 => b[at] as u64 + 1,
            7 => u16::be_at(b, at) as u64 + 1,
            _ => 1 << bs,
        };
        if bs == 7 && block == 65536 {
            return Some(Err("the forbidden block size 65536"));
        }
        at += bsn;
        let rate = match sr {
            0 => None,
            12 => Some(b[at] as u64 * 1000),
            13 => Some(u16::be_at(b, at) as u64),
            14 => Some(u16::be_at(b, at) as u64 * 10),
            _ => Some([0, 88200, 176400, 192000, 8000, 16000, 22050, 24000, 32000, 44100, 48000, 96000][sr as usize]),
        };
        at += srn;
        if crc8(&b[..at]) != b[at] {
            return Some(Err("a CRC-8 that does not match"));
        }
        let bits = [None, Some(8), Some(12), None, Some(16), Some(20), Some(24), Some(32)][bd as usize];
        Some(Ok(Head { len: at as u64 + 1, variable: b[1] & 1 != 0, block, rate, assignment: ch, bits, number }))
    }
}

/// The STREAMINFO block's fields (RFC 9639 §8.2).
#[derive(Clone, Copy, Debug, Default)]
pub struct StreamInfo {
    pub min_block: u64,
    pub max_block: u64,
    pub min_frame: u64,
    pub max_frame: u64,
    pub rate: u64,
    pub channels: u64,
    pub bits: u64,
    pub total: u64,
    pub md5: [u8; 16],
}

impl StreamInfo {
    pub fn parse(b: &[u8]) -> StreamInfo {
        let u24 = |at: usize| (b[at] as u64) << 16 | (b[at + 1] as u64) << 8 | b[at + 2] as u64;
        let packed = u64::be_at(b, 10);
        let mut md5 = [0u8; 16];
        md5.copy_from_slice(&b[18..34]);
        StreamInfo {
            min_block: u16::be_at(b, 0) as u64,
            max_block: u16::be_at(b, 2) as u64,
            min_frame: u24(4),
            max_frame: u24(7),
            rate: packed >> 44,
            channels: (packed >> 41 & 7) + 1,
            bits: (packed >> 36 & 31) + 1,
            total: packed & ((1 << 36) - 1),
            md5,
        }
    }

    pub fn json(&self) -> J {
        let md5: String = self.md5.iter().map(|b| format!("{b:02x}")).collect();
        json!({
            "min_block_size": self.min_block, "max_block_size": self.max_block, "min_frame_size": self.min_frame,
            "max_frame_size": self.max_frame, "sample_rate": self.rate, "channels": self.channels,
            "bits_per_sample": self.bits, "total_samples": self.total, "md5": md5,
        })
    }
}

/// A reader of a metadata block's body: Err when it ends early.
struct Cursor<'a> {
    b: &'a [u8],
    at: usize,
}

impl<'a> Cursor<'a> {
    fn take(&mut self, n: usize) -> Result<&'a [u8], ()> {
        let s = self.b.get(self.at..self.at.checked_add(n).ok_or(())?).ok_or(())?;
        self.at += n;
        Ok(s)
    }
    fn u32_le(&mut self) -> Result<usize, ()> {
        Ok(u32::le_at(self.take(4)?, 0) as usize)
    }
    fn u32_be(&mut self) -> Result<u64, ()> {
        Ok(u32::be_at(self.take(4)?, 0) as u64)
    }
    fn u64_be(&mut self) -> Result<u64, ()> {
        Ok(u64::be_at(self.take(8)?, 0))
    }
    fn u8(&mut self) -> Result<u8, ()> {
        Ok(self.take(1)?[0])
    }
}

/// A Vorbis comment block's vendor string and fields (RFC 9639 §8.6), as text values; Err when
/// a length runs past the block or bytes are left after the last field.
fn vorbis_json(b: &[u8]) -> Result<J, ()> {
    let mut c = Cursor { b, at: 0 };
    let n = c.u32_le()?;
    let vendor = text_json(c.take(n)?);
    let count = c.u32_le()?;
    let mut fields = Vec::new();
    for _ in 0..count {
        let n = c.u32_le()?;
        fields.push(text_json(c.take(n)?));
    }
    if c.at != b.len() {
        return Err(());
    }
    Ok(json!({"vendor": vendor, "comments": fields}))
}

/// The text of a fixed-size ASCII field: its bytes up to the first NUL, as a text value.
fn field_text(b: &[u8]) -> J {
    text_json(&b[..b.iter().position(|&c| c == 0).unwrap_or(b.len())])
}

/// A cuesheet block's fields (RFC 9639 §8.7); Err when its tracks run past the block or bytes
/// are left after them.
fn cuesheet_json(b: &[u8]) -> Result<J, ()> {
    let mut c = Cursor { b, at: 0 };
    let catalog = field_text(c.take(128)?);
    let lead_in = c.u64_be()?;
    let cd = c.u8()? & 0x80 != 0;
    c.take(258)?;
    let count = c.u8()?;
    let mut tracks = Vec::new();
    for _ in 0..count {
        let offset = c.u64_be()?;
        let number = c.u8()?;
        let isrc = field_text(c.take(12)?);
        let flags = c.u8()?;
        c.take(13)?;
        let points = c.u8()?;
        let mut indexes = Vec::new();
        for _ in 0..points {
            let o = c.u64_be()?;
            let n = c.u8()?;
            c.take(3)?;
            indexes.push(json!({"offset": o, "number": n}));
        }
        tracks.push(json!({
            "offset": offset, "number": number, "isrc": isrc, "audio": flags & 0x80 == 0,
            "pre_emphasis": flags & 0x40 != 0, "indexes": indexes,
        }));
    }
    if c.at != b.len() {
        return Err(());
    }
    Ok(json!({"catalog": catalog, "lead_in": lead_in, "cd": cd, "tracks": tracks}))
}

/// A picture block's fields before its data (RFC 9639 §8.8), from the first bytes of its body:
/// (their length, the data's length, the fields as JSON); Err(Some(n)) when the first `n`
/// bytes are needed to read them, Err(None) when a length runs past the block of `len` bytes.
pub fn picture_fields(b: &[u8], len: u64) -> Result<(u64, u64, J), Option<u64>> {
    let mut c = Cursor { b, at: 0 };
    let need = |at: usize, n: u64| Err(if at as u64 + n > len { None } else { Some(at as u64 + n) });
    let Ok(kind) = c.u32_be() else { return need(0, 8) };
    let Ok(mime_len) = c.u32_be() else { return need(0, 8) };
    let Ok(mime) = c.take(mime_len as usize) else { return need(8, mime_len + 4) };
    let mime = text_json(mime);
    let Ok(desc_len) = c.u32_be() else { return need(c.at, 4) };
    let at = c.at;
    let Ok(desc) = c.take(desc_len as usize) else { return need(at, desc_len + 20) };
    let desc = text_json(desc);
    let at = c.at;
    let (Ok(w), Ok(h), Ok(depth), Ok(colors), Ok(n)) = (c.u32_be(), c.u32_be(), c.u32_be(), c.u32_be(), c.u32_be()) else {
        return need(at, 20);
    };
    let fields = c.at as u64;
    if fields + n != len {
        return Err(None);
    }
    Ok((fields, n, json!({
        "type": kind, "media_type": mime, "description": desc, "width": w, "height": h, "depth": depth, "colors": colors,
        "size": n,
    })))
}

/// Whether a file's first bytes are a FLAC file's (spec/zarrify.md §1.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>): `fLaC` at 0, or after
/// the ID3v2 tags at the start, when `head` holds it.
pub fn detect(head: &[u8]) -> bool {
    let mut p = 0usize;
    while head.len() >= p + 10 && &head[p..p + 3] == b"ID3" {
        let s = &head[p + 6..p + 10];
        if s.iter().any(|&c| c >= 0x80) {
            return false;
        }
        let size = s.iter().fold(0usize, |a, &c| a << 7 | (c & 0x7F) as usize);
        p += 10 + size + if head[p + 5] & 0x10 != 0 { 10 } else { 0 };
    }
    head.get(p..p + 4) == Some(b"fLaC")
}

#[derive(Clone, Copy, PartialEq, Debug)]
enum Phase {
    Start,
    Tags,
    Marker,
    Blocks,
    Frames,
    Done,
}

/// A metadata block: its type and body length.
#[derive(Clone, Copy, Debug)]
struct Block {
    kind: u8,
    len: u64,
}

/// The scan of the frames: the current frame's start, its header, and the CRC-16 of its bytes
/// so far, up to `s`.
#[derive(Clone, Copy, Debug)]
struct Scan {
    p: u64,
    head: Head,
    s: u64,
    crc: u16,
    /// the CRC-16 is 0 at `s`, whose header is not tested yet
    pending: bool,
}

pub struct Flac {
    size: u64,
    phase: Phase,
    fed: Fed,
    asked: Asked,
    ir: Ir,
    rules: &'static Rules,
    /// the ID3v2 tags' end, the fLaC marker's offset
    a: u64,
    /// where the frames end: the file's end, or the ID3v1 tag's start
    e: u64,
    id3v2: Vec<J>,
    /// the ID3v1 tag's offset and bytes
    id3v1: Option<(u64, Vec<u8>)>,
    /// the next metadata block's offset, and the blocks so far
    m: u64,
    blocks: Vec<Block>,
    info: Option<StreamInfo>,
    info_value: u32,
    meta: J,
    cuesheets: Vec<J>,
    pictures: Vec<J>,
    applications: Vec<J>,
    /// where the frames start
    f0: u64,
    scan: Option<Scan>,
    /// each frame's offset, length and block size
    frames: Vec<(u64, u64, u64)>,
    /// the bytes of frames a round reads
    window: u64,
    facts: J,
}

impl Flac {
    pub fn new(size: u64) -> Self {
        Flac {
            size, phase: Phase::Start, fed: Fed::new(Overlap::MergeNew), asked: Asked::default(), ir: Ir::new(size),
            rules: rules::of("flac"), a: 0, e: size, id3v2: Vec::new(), id3v1: None, m: 0, blocks: Vec::new(), info: None,
            info_value: 0, meta: json!({}), cuesheets: Vec::new(), pictures: Vec::new(), applications: Vec::new(), f0: 0, scan: None,
            frames: Vec::new(), window: WINDOW, facts: J::Null,
        }
    }

    /// For tests: a parser that reads `window` bytes of frames a round, so that small files'
    /// frames and headers straddle its reads.
    pub fn with_window(size: u64, window: u64) -> Self {
        Flac { window: window.max(1), ..Flac::new(size) }
    }

    pub fn feed(&mut self, offset: u64, data: Vec<u8>) {
        self.fed.feed(offset, data);
    }

    pub fn step(&mut self) -> Result<Step, String> {
        if self.phase == Phase::Start {
            self.phase = Phase::Tags;
            let mut r = vec![(0, self.size.min(HEAD))];
            if self.size > HEAD {
                r.push((self.size - 128.min(self.size), 128.min(self.size)));
            }
            return Ok(self.asked.ask(r));
        }
        while self.phase != Phase::Done {
            let need = match self.phase {
                Phase::Tags => self.tags()?,
                Phase::Marker => self.marker()?,
                Phase::Blocks => self.block()?,
                Phase::Frames => self.frame()?,
                _ => None,
            };
            if let Some(r) = need {
                return Ok(self.asked.ask(vec![r]));
            }
        }
        Ok(Step::Done)
    }

    /// The bytes [o, o + n), or the range to read for them: from `o`, at least `n` bytes and
    /// up to HEAD, within the file.
    fn need(&self, o: u64, n: u64) -> Result<&[u8], (u64, u64)> {
        self.fed.get(o, n).ok_or((o, n.max(HEAD).min(self.size - o)))
    }

    /// The ID3v2 tags at the start (as the MP3 profile reads them), then the ID3v1 tag at the
    /// end, which `e` stops before.
    fn tags(&mut self) -> Res<Option<(u64, u64)>> {
        let (n, p) = (self.size, self.a);
        if n >= p + 3 {
            let h = match self.need(p, (n - p).min(10)) {
                Ok(h) => h.to_vec(),
                Err(r) => return Ok(Some(r)),
            };
            if &h[..3] == b"ID3" {
                let have = n - p;
                let (size, footer, bad) = if have >= 10 {
                    let s = &h[6..10];
                    (s.iter().fold(0u64, |a, &c| a << 7 | (c & 0x7F) as u64), h[5] & 0x10 != 0, s.iter().position(|&c| c >= 0x80))
                } else {
                    (0, false, None)
                };
                let total = 10 + size + if footer { 10 } else { 0 };
                self.rules.check("id3v2", rules::env([
                    ("at", num(p as f64)), ("size", num(n as f64)), ("have", num(have as f64)), ("total", num(total as f64)),
                    ("syncsafe", flag(bad.is_none())), ("byte", bad.map_or(V::Absent, |k| num(h[6 + k] as f64))),
                ]))?;
                let tag = self.ir.struct_(0, "id3v2/", self.id3v2.len() as u64, 0, Some((p, total)))?;
                self.ir.value(tag, "header", NO_INDEX, ID3V2_HEADER_TYPE, 0, (p, 10), Some(&h[..10]))?;
                if size > 0 {
                    self.ir.value(tag, "data", NO_INDEX, &format!("bytes[{size}]"), 0, (p + 10, size), None)?;
                }
                if footer {
                    self.ir.value(tag, "footer", NO_INDEX, "bytes[10]", 0, (p + 10 + size, 10), None)?;
                }
                self.id3v2.push(json!({"version": [h[3], h[4]], "flags": h[5], "size": size, "footer": footer}));
                self.a += total;
                return Ok(None);
            }
        }
        if n >= self.a + 128 {
            match self.need(n - 128, 128) {
                Ok(t) if &t[..3] == b"TAG" => {
                    self.id3v1 = Some((n - 128, t.to_vec()));
                    self.e = n - 128;
                }
                Ok(_) => {}
                Err(_) => return Ok(Some((n - 128, 128))),
            }
        }
        self.phase = Phase::Marker;
        Ok(None)
    }

    fn marker(&mut self) -> Res<Option<(u64, u64)>> {
        let a = self.a;
        let b = if self.e >= a + 4 {
            match self.need(a, 4) {
                Ok(b) => b.to_vec(),
                Err(r) => return Ok(Some(r)),
            }
        } else {
            Vec::new()
        };
        self.rules.check("marker", rules::env([("at", num(a as f64)), ("marker", flag(b == b"fLaC"))]))?;
        self.ir.value(0, "marker", NO_INDEX, "ascii[4]", 0, (a, 4), Some(&b))?;
        self.m = a + 4;
        self.phase = Phase::Blocks;
        Ok(None)
    }

    /// The next metadata block (spec/zarrify/flac.md §8.1
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/flac.md#81-the-stream>): its
    /// header and the fields of its body the profile reads.
    fn block(&mut self) -> Res<Option<(u64, u64)>> {
        let (m, e) = (self.m, self.e);
        let k = self.blocks.len();
        let head = if e >= m + 4 {
            match self.need(m, 4) {
                Ok(h) => h.to_vec(),
                Err(r) => return Ok(Some(r)),
            }
        } else {
            Vec::new()
        };
        let (kind, len) = if head.len() == 4 {
            (head[0] & 0x7F, (head[1] as u64) << 16 | (head[2] as u64) << 8 | head[3] as u64)
        } else {
            (0, 0)
        };
        let seen = |t: u8| self.blocks.iter().filter(|b| b.kind == t).count();
        self.rules.check("block", rules::env([
            ("at", num(m as f64)), ("end", num(e as f64)), ("have", num(head.len() as f64)), ("index", num(k as f64)),
            ("type", num(kind as f64)), ("length", num(len as f64)), ("total", num(4.0 + len as f64)),
            ("again", flag(matches!(kind, 0 | 3 | 4) && seen(kind) > 0)), ("aligned", flag(len % 18 == 0)),
            ("name", text(BLOCKS.get(kind as usize).copied().unwrap_or("reserved"))),
        ]))?;
        let body = m + 4;
        // the bytes of the body the profile reads: a picture's fields, as far as they are known
        let read = match kind {
            0 | 3 | 4 | 5 => len,
            2 => 4,
            6 => {
                let held = self.fed.from(body);
                match picture_fields(&held[..held.len().min(len as usize)], len) {
                    Err(Some(n)) => n,
                    _ => (held.len() as u64).min(len),
                }
            }
            _ => 0,
        };
        let b = match self.fed.get(body, read) {
            _ if read == 0 => Vec::new(),
            Some(b) => b.to_vec(),
            None => return Ok(Some((body, read))),
        };
        if kind == 6 && picture_fields(&b, len).is_err_and(|n| n.is_some()) {
            return Ok(None); // more of the picture's fields to read
        }
        let s = self.ir.struct_(0, "metadata/", k as u64, 0, Some((m, 4 + len)))?;
        self.ir.value(s, "header", NO_INDEX, BLOCK_HEADER_TYPE, 0, (m, 4), Some(&head))?;
        let bytes = |len: u64| format!("bytes[{len}]");
        match kind {
            0 => {
                self.info_value = self.ir.value(s, "streaminfo", NO_INDEX, STREAMINFO_TYPE, 0, (body, 34), Some(&b))?;
                let si = StreamInfo::parse(&b);
                self.rules.check("streaminfo", rules::env([
                    ("at", num(m as f64)), ("bits", num(si.bits as f64)), ("rate", num(si.rate as f64)),
                ]))?;
                self.info = Some(si);
            }
            3 => {
                let n = len / 18;
                if n > 0 {
                    self.ir.value(s, "seek_points", NO_INDEX, &format!("{SEEK_POINT_TYPE}[{n}]"), 0, (body, len), Some(&b))?;
                }
                let placeholders = (0..n as usize).filter(|&i| u64::be_at(&b, 18 * i) == u64::MAX).count();
                self.meta["seek_table"] = json!({"points": n as usize - placeholders, "placeholders": placeholders});
            }
            4 => {
                let v = vorbis_json(&b);
                self.rules.check("vorbis_comment", rules::env([("at", num(m as f64)), ("valid", flag(v.is_ok()))]))?;
                self.ir.value(s, "vorbis_comment", NO_INDEX, &bytes(len), 0, (body, len), None)?;
                self.meta["vorbis_comment"] = v.unwrap_or(J::Null);
            }
            5 => {
                let v = cuesheet_json(&b);
                self.rules.check("cuesheet", rules::env([("at", num(m as f64)), ("valid", flag(v.is_ok()))]))?;
                self.ir.value(s, "cuesheet", NO_INDEX, &bytes(len), 0, (body, len), None)?;
                self.cuesheets.push(v.unwrap_or(J::Null));
            }
            6 => {
                let v = picture_fields(&b, len);
                self.rules.check("picture", rules::env([("at", num(m as f64)), ("valid", flag(v.is_ok()))]))?;
                let (fields, n, mut j) = v.map_err(|_| "internal: a picture's fields")?;
                self.ir.value(s, "fields", NO_INDEX, &bytes(fields), 0, (body, fields), None)?;
                if n > 0 {
                    let d = self.ir.value(s, "data", NO_INDEX, &bytes(n), 0, (body + fields, n), None)?;
                    j["element"] = json!(d);
                }
                self.pictures.push(j);
            }
            2 => {
                self.ir.value(s, "id", NO_INDEX, "ascii[4]", 0, (body, 4), Some(&b))?;
                if len > 4 {
                    self.ir.value(s, "data", NO_INDEX, &bytes(len - 4), 0, (body + 4, len - 4), None)?;
                }
                self.applications.push(json!({"id": text_json(&b), "size": len - 4}));
            }
            1 if len > 0 => {
                self.ir.value(s, "padding", NO_INDEX, &bytes(len), 0, (body, len), None)?;
            }
            _ if len > 0 => {
                self.ir.value(s, "data", NO_INDEX, &bytes(len), 0, (body, len), None)?;
            }
            _ => {}
        }
        self.blocks.push(Block { kind, len });
        self.m = body + len;
        if head[0] & 0x80 != 0 {
            self.f0 = self.m;
            self.phase = Phase::Frames;
        }
        Ok(None)
    }

    /// The checks of frame `j`, at `at`, of header `h`: a fixed block size, STREAMINFO's sample
    /// rate, channel count and bit depth (a header's own, or STREAMINFO's for code 0), the number
    /// `j`, and no more samples than the first frame.
    fn frame_check(&self, at: u64, j: u64, h: &Head) -> Res<()> {
        let si = self.info.ok_or("internal: no STREAMINFO")?;
        let (rate, bits) = (h.rate.unwrap_or(si.rate), h.bits.unwrap_or(si.bits));
        let (what, from, to) = if rate != si.rate {
            ("sample rate", si.rate, rate)
        } else if h.channels() != si.channels {
            ("channel count", si.channels, h.channels())
        } else if bits != si.bits {
            ("bit depth", si.bits, bits)
        } else {
            ("", 0, 0)
        };
        let block = self.frames.first().map_or(h.block, |f| f.2);
        self.rules.check("frame", rules::env([
            ("at", num(at as f64)), ("variable", flag(h.variable)), ("same", flag(what.is_empty())), ("what", text(what)),
            ("from", num(from as f64)), ("to", num(to as f64)), ("index", num(j as f64)), ("number", num(h.number as f64)),
            ("samples", num(h.block as f64)), ("block", num(block as f64)),
        ]))
    }

    /// The frames (spec/zarrify/flac.md §8.1
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/flac.md#81-the-stream>), one at a
    /// time: a frame ends at the first byte after its header where the CRC-16 of its bytes is 0
    /// and either the frames end or a valid frame header starts (which must be numbered one
    /// more).
    fn frame(&mut self) -> Res<Option<(u64, u64)>> {
        let e = self.e;
        let mut sc = match self.scan {
            Some(sc) => sc,
            None => {
                let p = self.f0;
                let b = self.fed.from(p);
                let b = &b[..b.len().min(e.saturating_sub(p).min(MAX_HEADER) as usize)];
                let h = match Head::parse(b) {
                    Some(h) => h,
                    // the header's bytes are not all held: read on from what is
                    None if (b.len() as u64) < (e - p).min(MAX_HEADER) => {
                        let at = p + b.len() as u64;
                        return Ok(Some((at, (e - at).min(self.window))));
                    }
                    None => Err("a header that ends with the frames"),
                };
                let j = self.frames.len() as u64;
                self.rules.check("header", rules::env([
                    ("at", num(p as f64)), ("frames", flag(p < e)), ("valid", flag(h.is_ok())), ("why", text(h.err().unwrap_or(""))),
                    ("index", num(j as f64)),
                ]))?;
                let h = h.map_err(|_| "internal: an invalid header")?;
                self.frame_check(p, j, &h)?;
                Scan { p, head: h, s: p, crc: 0, pending: false }
            }
        };
        let j = self.frames.len() as u64;
        let max = self.rules.limit("max_frame") as u64;
        let data = self.fed.from(sc.p);
        let data = &data[..data.len().min((e - sc.p) as usize)];
        let reach = sc.p + data.len() as u64;
        let mut end = None;
        loop {
            if sc.pending {
                if sc.s == e {
                    end = Some(e);
                    break;
                }
                let rest = &data[(sc.s - sc.p) as usize..];
                if (rest.len() as u64) < (e - sc.s).min(MAX_HEADER) {
                    break; // the next header's bytes are not held: read on from here
                }
                // a valid header, whatever its number (`frame_check` checks it): the CRC-16 of a
                // run of whole frames is 0 at each one's end, so a header numbered otherwise
                // ends the frame too, rather than letting it run on to the next match
                if matches!(Head::parse(&rest[..rest.len().min(MAX_HEADER as usize)]), Some(Ok(_))) {
                    end = Some(sc.s);
                    break;
                }
                sc.pending = false;
            }
            if sc.s >= reach || sc.s - sc.p > max {
                break;
            }
            sc.crc = (sc.crc << 8) ^ CRC16[((sc.crc >> 8) as u8 ^ data[(sc.s - sc.p) as usize]) as usize];
            sc.s += 1;
            sc.pending = sc.crc == 0 && sc.s >= sc.p + sc.head.len + 3;
        }
        let Some(q) = end else {
            let longer = sc.s - sc.p > max;
            if reach < e && !longer {
                // the bytes before the frame are done with: dropped once a window, not per frame
                self.fed.keep_from(sc.p);
                self.scan = Some(sc);
                return Ok(Some((reach, (e - reach).min(self.window))));
            }
            self.rules.check("end", rules::env([
                ("at", num(sc.p as f64)), ("index", num(j as f64)), ("found", flag(false)), ("longer", flag(longer)),
            ]))?;
            return Err("internal: a frame without an end".into());
        };
        // the frame [p, q): only the last may hold fewer samples than the first
        if let Some(&(o, _, b)) = self.frames.last() {
            let first = self.frames[0].2;
            self.rules.check("short", rules::env([("at", num(o as f64)), ("samples", num(b as f64)), ("block", num(first as f64))]))?;
        }
        self.frames.push((sc.p, q - sc.p, sc.head.block));
        if q == e {
            self.scan = None;
            self.finish_frames()?;
            self.phase = Phase::Done;
            return Ok(None);
        }
        let b = self.fed.from(q);
        let h = Head::parse(&b[..b.len().min(MAX_HEADER as usize)]).and_then(Result::ok).ok_or("internal: no next header")?;
        self.frame_check(q, j + 1, &h)?;
        self.scan = Some(Scan { p: q, head: h, s: q, crc: 0, pending: false });
        Ok(None)
    }

    /// The elements of the frames and the tags after them, and the facts.
    fn finish_frames(&mut self) -> Res<()> {
        let si = self.info.ok_or("internal: no STREAMINFO")?;
        let samples: u64 = self.frames.iter().map(|f| f.2).sum();
        self.rules.check("total", rules::env([("samples", num(samples as f64)), ("total", num(si.total as f64))]))?;
        let (first_o, last) = (self.frames[0].0, self.frames[self.frames.len() - 1]);
        let s = self.ir.struct_(0, "frames", NO_INDEX, 0, Some((first_o, last.0 + last.1 - first_o)))?;
        let dtype = data_type(si.bits);
        let form = |n: u64| json!({
            "geometry": {"samples": n, "channels": si.channels, "data_type": dtype},
            "codec": {"name": "flac", "sample_rate": si.rate, "bits_per_sample": si.bits},
            "recipe": [["src", 0, null]],
        }).to_string();
        let mut first = 0u32;
        for (j, &(o, n, b)) in self.frames.iter().enumerate() {
            let i = self.ir.data(s, "", j as u64, 0, (o, n), &form(b))?;
            if j == 0 {
                first = i;
            }
        }
        let id3v1 = match &self.id3v1 {
            Some((o, b)) => json!(self.ir.value(0, "id3v1", NO_INDEX, ID3V1_TYPE, 0, (*o, 128), Some(b))?),
            None => J::Null,
        };
        let blocks: Vec<J> = self.blocks.iter().map(|b| {
            json!({"type": BLOCKS.get(b.kind as usize).map_or(json!(b.kind), |n| json!(n)), "size": b.len})
        }).collect();
        let mut facts = json!({
            "frames": first, "count": self.frames.len(), "block": self.frames[0].2, "samples": samples,
            "streaminfo": si.json(), "info_value": self.info_value, "blocks": blocks, "cuesheets": self.cuesheets,
            "pictures": self.pictures,
            "applications": self.applications, "id3v2": self.id3v2, "id3v1": id3v1, "shard_frames": shard_frames(),
            "frame_bytes": self.frames.iter().map(|f| f.1).sum::<u64>(),
        });
        for k in ["seek_table", "vorbis_comment"] {
            facts[k] = self.meta[k].clone();
        }
        self.asked.put(&mut facts);
        self.facts = facts;
        Ok(())
    }

    pub fn finish(self) -> Result<(Ir, J), String> {
        sans::finish(self.phase == Phase::Done, self.ir, self.facts)
    }
}

/// The Zarr data type of samples of `bits` bits: the smallest signed integer that holds them.
pub fn data_type(bits: u64) -> &'static str {
    match bits {
        0..=8 => "int8",
        9..=16 => "int16",
        _ => "int32",
    }
}

crate::parser!(Flac);

/// The FLAC profile's file run (spec/zarrify/flac.md).
pub struct Profile;

impl crate::run::FileProfile for Profile {
    /// `fLaC`, after the ID3v2 tags at the start (`detect`).
    fn detect(&self, head: &[u8]) -> bool {
        detect(head)
    }
    fn parser(&self, size: u64) -> crate::run::AnyParser {
        crate::run::AnyParser::of::<Flac>(size)
    }
    /// It takes exactly the ranges it asked for.
    fn whole(&self) -> bool {
        false
    }
    fn project(&self, ir: &crate::ir::Ir, facts: &serde_json::Value, url: &str, copied: &crate::project::Copied) -> Result<crate::out::Out, String> {
        crate::project::flac::project(ir, facts, url, copied)
    }
}
