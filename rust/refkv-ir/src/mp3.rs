//! A sans-IO parser of MP3 files (MPEG-1, MPEG-2 and MPEG-2.5 Layer III audio) into the IR
//! (spec/virtualize/mp3.md §8
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp3.md#8-mp3-profile>, and the
//! source model of §5.3 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp3.md#53-elements>).
//!
//! One round: the whole file (an MP3 has no index; each frame's header gives the next). The
//! schema (`schema/mp3.json`) holds every check, in the profile's order. The IR is the ID3v2
//! tags at the front (a struct each), the Xing, Info or VBRI frame (one opaque value, when the
//! stream starts with one), the audio frames (a `data` element each, under `frames`), the ID3v1
//! tag (one record value) and the APE tag (one opaque value) at the end, and gaps for the bytes
//! that are none of these.

use crate::check;
use crate::expr::V;
use crate::fed::Fed;
use crate::ir::*;
use crate::nd2::Step;
use crate::rules::{self, Rules, Vars, flag, num, text};
use serde_json::{json, Value as J};
use std::sync::atomic::{AtomicUsize, Ordering};

/// The versions by their header bits, and the layers by theirs (1 is Layer III).
pub const VERSIONS: [&str; 4] = ["MPEG-2.5", "reserved", "MPEG-2", "MPEG-1"];
pub const LAYERS: [&str; 4] = ["reserved", "III", "II", "I"];
/// The sample rates by version then index, and the channel modes by their bits.
const RATES: [[u32; 3]; 4] = [[11025, 12000, 8000], [0, 0, 0], [22050, 24000, 16000], [44100, 48000, 32000]];
pub const MODES: [&str; 4] = ["stereo", "joint_stereo", "dual_channel", "mono"];
/// Bitrates in kbps: MPEG-1, then MPEG-2 and 2.5; Layer I, II, III; then the index.
const BITRATES: [[[u32; 15]; 3]; 2] = [
    [
        [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448],
        [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384],
        [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
    ],
    [
        [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256],
        [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
        [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
    ],
];
/// The most bytes of earlier frames a frame's main data may begin in (`main_data_begin`, 9
/// bits in MPEG-1 and 8 in MPEG-2 and 2.5), by version: the bit reservoir a decoder must hold
/// before a frame to decode it as the stream does (spec/virtualize/mp3.md §8.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp3.md#82-chunks>).
pub const RESERVOIR: [u64; 4] = [255, 0, 255, 511];

/// The frames of a chunk (spec/virtualize/mp3.md §4.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp3.md#41-the-array>), but in tests
/// (`set_chunk_frames`). Each run reads it once, into its facts.
pub const DEFAULT_CHUNK_FRAMES: usize = 256;
static CHUNK_FRAMES: AtomicUsize = AtomicUsize::new(DEFAULT_CHUNK_FRAMES);

/// For tests: the frames of a chunk (None: the default, 256), so that small files have several
/// chunks and lead-ins.
pub fn set_chunk_frames(n: Option<usize>) {
    CHUNK_FRAMES.store(n.unwrap_or(DEFAULT_CHUNK_FRAMES).max(1), Ordering::Relaxed);
}

pub fn chunk_frames() -> usize {
    CHUNK_FRAMES.load(Ordering::Relaxed)
}

/// A frame header's fields (spec/virtualize/mp3.md §2
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp3.md#2-frames>).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct Head {
    /// the version bits: 0 MPEG-2.5, 2 MPEG-2, 3 MPEG-1
    pub version: u8,
    /// the layer bits: 1 Layer III, 2 Layer II, 3 Layer I
    pub layer: u8,
    /// whether a 16-bit CRC follows the header
    pub crc: bool,
    pub bitrate: u8,
    pub rate: u8,
    pub padding: bool,
    /// the channel mode bits: 0 stereo, 1 joint stereo, 2 dual channel, 3 mono
    pub mode: u8,
}

impl Head {
    /// The header at the start of `b`, when `b` starts with a **valid** one: the 11-bit sync, a
    /// version that is not reserved, a layer that is not reserved, a bitrate index from 1 to 14
    /// and a sample rate index from 0 to 2.
    pub fn parse(b: &[u8]) -> Option<Head> {
        if b.len() < 4 || b[0] != 0xFF || b[1] & 0xE0 != 0xE0 {
            return None;
        }
        let (version, layer) = ((b[1] >> 3) & 3, (b[1] >> 1) & 3);
        let (bitrate, rate) = (b[2] >> 4, (b[2] >> 2) & 3);
        if version == 1 || layer == 0 || bitrate == 0 || bitrate == 15 || rate == 3 {
            return None;
        }
        Some(Head { version, layer, crc: b[1] & 1 == 0, bitrate, rate, padding: b[2] & 2 != 0, mode: b[3] >> 6 })
    }

    /// Whether `b` starts with a free-format header: valid but for a bitrate index of 0.
    pub fn free_format(b: &[u8]) -> bool {
        b.len() >= 4 && b[0] == 0xFF && b[1] & 0xE0 == 0xE0 && (b[1] >> 3) & 3 != 1 && (b[1] >> 1) & 3 != 0
            && b[2] >> 4 == 0 && (b[2] >> 2) & 3 != 3
    }

    pub fn rate(&self) -> u32 {
        RATES[self.version as usize][self.rate as usize]
    }

    pub fn channels(&self) -> u64 {
        if self.mode == 3 { 1 } else { 2 }
    }

    pub fn kbps(&self) -> u32 {
        BITRATES[(self.version != 3) as usize][(3 - self.layer) as usize][self.bitrate as usize]
    }

    /// The frame's length in bytes, header included.
    pub fn length(&self) -> u64 {
        let (br, sr, pad) = (self.kbps() as u64 * 1000, self.rate() as u64, self.padding as u64);
        match self.layer {
            3 => (12 * br / sr + pad) * 4,
            2 => 144 * br / sr + pad,
            _ if self.version == 3 => 144 * br / sr + pad,
            _ => 72 * br / sr + pad,
        }
    }

    /// The samples per channel a Layer III frame decodes to.
    pub fn samples(&self) -> u64 {
        if self.version == 3 { 1152 } else { 576 }
    }

    /// The side information's length (Layer III).
    pub fn side(&self) -> u64 {
        match (self.version == 3, self.channels() == 1) {
            (true, true) => 17,
            (true, false) => 32,
            (false, true) => 9,
            (false, false) => 17,
        }
    }

    /// The main data's length: the frame less its header, CRC and side information.
    pub fn main(&self) -> u64 {
        self.length().saturating_sub(4 + 2 * self.crc as u64 + self.side())
    }

    /// Whether `o` is a frame of the same stream: the same version, layer, sample rate and
    /// channel count (the bitrate, padding, CRC and channel mode may differ frame by frame).
    pub fn same_stream(&self, o: &Head) -> bool {
        self.version == o.version && self.layer == o.layer && self.rate == o.rate && self.channels() == o.channels()
    }
}

/// Whether a file's first bytes are an MP3 file's (spec/virtualize.md §1.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize.md#12-input>): `ID3` at 0, or a
/// frame header of any layer whose sync, version, layer and sample rate index are valid, whatever
/// its bitrate index (the profile then rejects a layer other than III, or a free-format
/// bitrate, by name).
pub fn detect(head: &[u8]) -> bool {
    (head.len() >= 3 && &head[..3] == b"ID3")
        || (head.len() >= 4 && head[0] == 0xFF && head[1] & 0xE0 == 0xE0 && (head[1] >> 3) & 3 != 1 && (head[1] >> 1) & 3 != 0
            && (head[2] >> 2) & 3 != 3)
}

fn u32be(b: &[u8], at: usize) -> Option<u32> {
    b.get(at..at + 4).map(|a| u32::from_be_bytes(a.try_into().unwrap()))
}

fn u16be(b: &[u8], at: usize) -> Option<u16> {
    b.get(at..at + 2).map(|a| u16::from_be_bytes(a.try_into().unwrap()))
}

fn u32le(b: &[u8], at: usize) -> u32 {
    u32::from_le_bytes(b[at..at + 4].try_into().unwrap())
}

/// The offset in a frame of its Xing or Info tag, if it holds one: after the header, the CRC
/// and the side information.
fn xing_at(f: &[u8], h: &Head) -> Option<usize> {
    let at = 4 + 2 * h.crc as usize + h.side() as usize;
    matches!(f.get(at..at + 4), Some(b"Xing") | Some(b"Info")).then_some(at)
}

/// Whether the frame `f` is an info frame: a Xing or Info tag, or a VBRI tag at 36.
fn is_info(f: &[u8], h: &Head) -> bool {
    xing_at(f, h).is_some() || f.get(36..40) == Some(b"VBRI")
}

/// The fields of an info frame (spec/virtualize/mp3.md §5.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp3.md#51-the-root>): the tag, the
/// frame and byte counts and the quality it holds (null when it does not), and the encoder,
/// delay and padding of a LAME tag after a Xing or Info tag.
fn info_json(f: &[u8], h: &Head) -> J {
    let (mut frames, mut bytes, mut quality, mut encoder, mut delay, mut padding) = (J::Null, J::Null, J::Null, J::Null, J::Null, J::Null);
    let tag = if let Some(at) = xing_at(f, h) {
        let flags = u32be(f, at + 4).unwrap_or(0);
        let mut pos = at + 8;
        let field = |pos: &mut usize| {
            let v = u32be(f, *pos).map_or(J::Null, |v| json!(v));
            *pos += 4;
            v
        };
        if flags & 1 != 0 {
            frames = field(&mut pos);
        }
        if flags & 2 != 0 {
            bytes = field(&mut pos);
        }
        if flags & 4 != 0 {
            pos += 100; // the seek table
        }
        if flags & 8 != 0 {
            quality = field(&mut pos);
        }
        // the LAME tag: a 9-byte encoder version (`LAME3.99r`; `LAME4.0`, a space and a NUL),
        // then its fields (the delays at 21)
        if let Some(e) = f.get(pos..pos + 9) {
            let e = &e[..e.iter().position(|&c| c == 0).unwrap_or(9)];
            if !e.is_empty() && e.iter().all(|&c| (0x20..0x7F).contains(&c)) && e[0] != b' ' {
                encoder = json!(String::from_utf8_lossy(e).trim_end().to_string());
                if let Some(d) = f.get(pos + 21..pos + 24) {
                    delay = json!((d[0] as u32) << 4 | (d[1] as u32) >> 4);
                    padding = json!((d[1] as u32 & 0xF) << 8 | d[2] as u32);
                }
            }
        }
        String::from_utf8_lossy(&f[at..at + 4]).to_string()
    } else {
        // VBRI: version, delay, quality, bytes, frames
        delay = u16be(f, 42).map_or(J::Null, |v| json!(v));
        quality = u16be(f, 44).map_or(J::Null, |v| json!(v));
        bytes = u32be(f, 46).map_or(J::Null, |v| json!(v));
        frames = u32be(f, 50).map_or(J::Null, |v| json!(v));
        "VBRI".to_string()
    };
    json!({"tag": tag, "frames": frames, "bytes": bytes, "quality": quality, "encoder": encoder, "delay": delay, "padding": padding})
}

/// Which parameter a confirmed frame of another stream changes: the first of the version, the
/// layer, the sample rate and the channel count that differs, with the two values.
fn differs(s: &Head, h: &Head) -> (&'static str, String, String) {
    if s.version != h.version {
        ("version", VERSIONS[s.version as usize].into(), VERSIONS[h.version as usize].into())
    } else if s.layer != h.layer {
        ("layer", LAYERS[s.layer as usize].into(), LAYERS[h.layer as usize].into())
    } else if s.rate != h.rate {
        ("sample rate", s.rate().to_string(), h.rate().to_string())
    } else {
        ("channel count", s.channels().to_string(), h.channels().to_string())
    }
}

/// The ID3v1 tag as one record type (conventions §8.6
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#86-types>).
pub const ID3V1_TYPE: &str = "{tag:ascii[3],title:cstr[30],artist:cstr[30],album:cstr[30],year:ascii[4],comment:cstr[30],genre:u1}";
/// An ID3v2 tag's 10-byte header as one record type.
pub const ID3V2_HEADER_TYPE: &str = "{id:ascii[3],version:u1[2],flags:u1,size:u1[4]}";

#[derive(Clone, Copy, PartialEq, Debug)]
enum Phase {
    Start,
    Scan,
    Done,
}

pub struct Mp3 {
    size: u64,
    phase: Phase,
    fed: Fed,
    ir: Ir,
    rules: &'static Rules,
    facts: J,
}

impl Mp3 {
    pub fn new(size: u64) -> Self {
        Mp3 { size, phase: Phase::Start, fed: Fed::default(), ir: Ir::new(size), rules: rules::of("mp3"), facts: J::Null }
    }

    pub fn feed(&mut self, offset: u64, data: Vec<u8>) {
        self.fed.feed(offset, data);
    }

    fn check(&self, role: &str, mut env: Vars<'static>) -> Res<()> {
        self.rules.check(role, &mut env)
    }

    pub fn step(&mut self) -> Result<Step, String> {
        match self.phase {
            Phase::Start => {
                self.phase = Phase::Scan;
                Ok(Step::Read(vec![(0, self.size)]))
            }
            Phase::Scan => {
                self.scan()?;
                self.phase = Phase::Done;
                Ok(Step::Done)
            }
            Phase::Done => Ok(Step::Done),
        }
    }

    /// The whole file (spec/virtualize/mp3.md §8.1
    /// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp3.md#81-the-stream>): the ID3v2
    /// tags, the trailing tags, then the frames between them, and the elements of each.
    fn scan(&mut self) -> Res<()> {
        let n = self.size;
        let d = self.fed.get(0, n).ok_or("internal: the file was not read")?.to_vec();
        self.fed.clear();
        self.check("file", rules::env([("size", num(n as f64))]))?;
        // the ID3v2 tags at the front
        let mut p: u64 = 0;
        let mut id3v2 = Vec::new();
        while d.len() as u64 >= p + 3 && &d[p as usize..p as usize + 3] == b"ID3" {
            let at = p as usize;
            let have = n - p;
            let (size, footer, bad) = if have >= 10 {
                let s = &d[at + 6..at + 10];
                let size = s.iter().fold(0u64, |a, &c| a << 7 | (c & 0x7F) as u64);
                (size, d[at + 5] & 0x10 != 0, s.iter().position(|&c| c >= 0x80))
            } else {
                (0, false, None)
            };
            let total = 10 + size + if footer { 10 } else { 0 };
            self.check("id3v2", rules::env([
                ("at", num(p as f64)), ("size", num(n as f64)), ("have", num(have as f64)), ("total", num(total as f64)),
                ("syncsafe", flag(bad.is_none())), ("byte", bad.map_or(V::Absent, |k| num(d[at + 6 + k] as f64))),
            ]))?;
            let tag = self.ir.struct_(0, "id3v2/", id3v2.len() as u64, 0, Some((p, total)))?;
            self.ir.value(tag, "header", NO_INDEX, ID3V2_HEADER_TYPE, 0, (p, 10), Some(&d[at..at + 10]))?;
            if size > 0 {
                self.ir.value(tag, "data", NO_INDEX, &format!("bytes[{size}]"), 0, (p + 10, size), None)?;
            }
            if footer {
                self.ir.value(tag, "footer", NO_INDEX, "bytes[10]", 0, (p + 10 + size, 10), None)?;
            }
            id3v2.push(json!({"version": [d[at + 3], d[at + 4]], "flags": d[at + 5], "size": size, "footer": footer}));
            p += total;
        }
        let a = p;
        // the tags at the end: an ID3v1 tag, then an APE tag before it
        let mut e = n;
        let mut id3v1 = None;
        if e >= a + 128 && &d[(e - 128) as usize..(e - 125) as usize] == b"TAG" {
            e -= 128;
            id3v1 = Some(e);
        }
        let mut ape = None;
        if e >= a + 32 && &d[(e - 32) as usize..(e - 24) as usize] == b"APETAGEX" {
            let f = (e - 32) as usize;
            let (version, size, items, flags) = (u32le(&d, f + 8), u32le(&d, f + 12) as u64, u32le(&d, f + 16), u32le(&d, f + 20));
            let total = size + if flags & 0x8000_0000 != 0 { 32 } else { 0 };
            if size >= 32 && total <= e - a {
                e -= total;
                ape = Some((e, total, version, items));
            }
        }
        // the frames: a chain from each confirmed frame's end; a scan for the next after junk
        let mut frames: Vec<(u64, Head)> = Vec::new();
        let mut info: Option<(u64, Head)> = None;
        let mut stream: Option<Head> = None;
        let mut free: Option<u64> = None;
        let mut chained = false;
        let mut p = a;
        while p + 4 <= e {
            let at = p as usize;
            match Head::parse(&d[at..at + 4]) {
                Some(h) => {
                    let len = h.length();
                    let fits = p + len <= e;
                    let consistent = stream.is_none_or(|s| h.same_stream(&s));
                    let next = || {
                        let q = (p + len) as usize;
                        q + 4 <= e as usize && Head::parse(&d[q..q + 4]).is_some_and(|g| g.same_stream(&h))
                    };
                    let confirmed = fits && ((chained && consistent) || (stream.is_none() && p + len == e) || next());
                    if confirmed {
                        match stream {
                            None => {
                                self.check("stream", rules::env([
                                    ("at", num(p as f64)), ("version", text(VERSIONS[h.version as usize])),
                                    ("layer", num(h.layer as f64)), ("layer_name", text(LAYERS[h.layer as usize])),
                                ]))?;
                                stream = Some(h);
                            }
                            Some(s) if !consistent => {
                                let (what, from, to) = differs(&s, &h);
                                self.check("frame", rules::env([
                                    ("at", num(p as f64)), ("what", text(what)), ("from", text(&from)), ("to", text(&to)), ("same", flag(false)),
                                ]))?;
                            }
                            _ => {}
                        }
                        if frames.is_empty() && info.is_none() && is_info(&d[at..at + len as usize], &h) {
                            info = Some((p, h));
                        } else {
                            frames.push((p, h));
                        }
                        p += len;
                        chained = true;
                        continue;
                    }
                }
                None => {
                    if free.is_none() && Head::free_format(&d[at..at + 4]) {
                        free = Some(p);
                    }
                }
            }
            p += 1;
            chained = false;
        }
        self.check("audio", rules::env([("frames", num(frames.len() as f64)), ("free", free.map_or(V::Absent, |o| num(o as f64)))]))?;
        let s = stream.ok_or("internal: no stream")?;
        // the elements, in the file's order
        let info_json = match info {
            Some((o, h)) => {
                let len = h.length();
                self.ir.value(0, "info", NO_INDEX, &format!("bytes[{len}]"), 0, (o, len), Some(&d[o as usize..(o + len) as usize]))?;
                info_json(&d[o as usize..(o + len) as usize], &h)
            }
            None => J::Null,
        };
        let (first_o, last) = (frames[0].0, frames[frames.len() - 1]);
        let span = last.0 + last.1.length() - first_o;
        let frames_struct = self.ir.struct_(0, "frames", NO_INDEX, 0, Some((first_o, span)))?;
        let form = json!({
            "geometry": {"samples": s.samples(), "channels": s.channels(), "data_type": "int16"},
            "codec": {"name": "mp3", "version": VERSIONS[s.version as usize], "sample_rate": s.rate()},
            "recipe": [["src", 0, null]],
        })
        .to_string();
        let mut first = 0u32;
        let mut main = Vec::with_capacity(frames.len());
        let mut used = 0u64;
        let (mut crc_frames, mut kbps) = (0u64, Some(frames[0].1.kbps()));
        for (j, (o, h)) in frames.iter().enumerate() {
            let i = self.ir.data(frames_struct, "", j as u64, 0, (*o, h.length()), &form)?;
            if j == 0 {
                first = i;
            }
            main.push(h.main());
            used += h.length();
            crc_frames += h.crc as u64;
            if kbps != Some(h.kbps()) {
                kbps = None;
            }
        }
        let id3v1_json = match id3v1 {
            Some(o) => {
                let i = self.ir.value(0, "id3v1", NO_INDEX, ID3V1_TYPE, 0, (o, 128), Some(&d[o as usize..o as usize + 128]))?;
                json!(i)
            }
            None => J::Null,
        };
        let ape_json = match ape {
            Some((o, total, version, items)) => {
                self.ir.value(0, "ape", NO_INDEX, &format!("bytes[{total}]"), 0, (o, total), None)?;
                json!({"version": version, "size": total, "items": items})
            }
            None => J::Null,
        };
        let stray = e - a - used - info.map_or(0, |(_, h)| h.length());
        self.facts = json!({
            "frames": first, "count": frames.len(), "version": s.version, "layer": 3, "sample_rate": s.rate(),
            "channels": s.channels(), "mode": frames[0].1.mode, "spf": s.samples(), "main": main,
            "kbps": kbps, "crc_frames": crc_frames, "info": info_json, "id3v2": id3v2, "id3v1": id3v1_json,
            "ape": ape_json, "stray": stray, "chunk_frames": chunk_frames(),
        });
        Ok(())
    }

    pub fn finish(mut self) -> Result<(Ir, J), String> {
        if self.phase != Phase::Done {
            return Err("internal: the parse is not done".into());
        }
        self.ir.sort_values();
        check::finish(&mut self.ir)?;
        Ok((self.ir, self.facts))
    }
}

crate::parser!(Mp3);

/// The MP3 profile's file run (spec/virtualize/mp3.md): the file read whole.
pub struct Profile;

impl crate::run::FileProfile for Profile {
    /// `ID3`, or an MPEG audio frame header (`detect`).
    fn detect(&self, head: &[u8]) -> bool {
        detect(head)
    }
    fn parser(&self, size: u64) -> crate::run::AnyParser {
        crate::run::AnyParser::of::<Mp3>(size)
    }
    /// It takes exactly the ranges it asked for.
    fn whole(&self) -> bool {
        false
    }
    fn project(&self, ir: &crate::ir::Ir, facts: &serde_json::Value, url: &str, copied: &crate::project::Copied) -> Result<crate::out::Out, String> {
        crate::project::mp3::project(ir, facts, url, copied)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A 128 kbps MPEG-1 Layer III header at 44.1 kHz, joint stereo, no CRC, no padding: 417
    /// bytes, 1152 samples, 32 bytes of side information.
    #[test]
    fn a_header_gives_the_frames_length_and_layout() {
        let h = Head::parse(&[0xFF, 0xFB, 0x90, 0x64]).unwrap();
        assert_eq!((h.version, h.layer, h.crc, h.rate(), h.channels(), h.kbps()), (3, 1, false, 44100, 2, 128));
        assert_eq!((h.length(), h.samples(), h.side(), h.main()), (417, 1152, 32, 381));
        // MPEG-2.5 at 8 kHz, 16 kbps, mono, with padding and a CRC: 72 * 16000 / 8000 + 1 = 145
        let h = Head::parse(&[0xFF, 0xE2, 0x2A, 0xC0]).unwrap();
        assert_eq!((h.version, h.crc, h.rate(), h.channels(), h.length(), h.samples(), h.side(), h.main()), (0, true, 8000, 1, 145, 576, 9, 130));
        // a reserved version, a reserved layer, a free-format bitrate, an invalid sample rate
        for b in [[0xFF, 0xEB, 0x90, 0x64], [0xFF, 0xF9, 0x90, 0x64], [0xFF, 0xFB, 0x00, 0x64], [0xFF, 0xFB, 0x9C, 0x64]] {
            assert!(Head::parse(&b).is_none(), "{b:?}");
        }
        assert!(Head::free_format(&[0xFF, 0xFB, 0x00, 0x64]));
        assert!(detect(b"ID3\x04") && detect(&[0xFF, 0xFB, 0x90, 0x64]) && detect(&[0xFF, 0xFB, 0x00, 0x64]) && !detect(b"RIFF"));
        assert!(!detect(&[0xFF, 0xFB, 0x9C, 0x64]) && !detect(&[0xFF, 0xEB, 0x90, 0x64]));
    }
}
