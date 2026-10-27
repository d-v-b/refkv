//! A sans-IO parser of DICOM Part 10 files into the IR (spec/zarrify/dicom.md): it never
//! reads; it asks for batches of ranges (`step`), is given their bytes (`feed`), and holds
//! them until it is done.
//!
//! Rounds: (1) the first 64 KiB; (2) the **scan** of the File Meta Information and the
//! dataset to Pixel Data, in windows that double from 64 KiB to 16 MiB while the walk keeps
//! to the bytes after the last one, skipping values of more than 64 KiB that are not
//! sequences; (3) the profile (spec/zarrify/dicom.md §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#7-dicom-profile>), which
//! checks the schema (`schema/dicom.json`) and reads the pixel data's structure: the Basic
//! Offset Table, then the frames' item headers an Extended Offset Table names (one batch), or
//! the fragments' item headers, those the Basic Offset Table predicts in one batch; then the
//! frames' first two bytes for a JPEG with three samples; (4) the scan of the elements after
//! Pixel Data; then the root's source metadata (`json.rs`) and the IR are built from what
//! was read.

pub mod elements;
pub mod json;

use crate::fed::Word;
use crate::ir::*;
use crate::nd2::Step;
use crate::out::{payload_size, Part};
use crate::rules::{self, Rules, Vars, flag, num, text};
use crate::sans;
use crate::text::{is_decimal, latin1};
use elements::*;
use json::{dictionary_vr, is_integer, is_text, numeric, Pixel, Translator, EOT_TAGS, MAX_TEXT, PADDING};
use serde_json::{json, Map, Value as J};
use std::collections::{HashMap, HashSet};

const FIRST: u64 = 1 << 16;
const MAX_WINDOW: u64 = 1 << 24;
/// SOI and the Adobe APP14 marker, without its last byte, the color transform
/// (spec/zarrify/dicom.md §7.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#75-frames>).
pub const ADOBE: [u8; 17] = [0xFF, 0xD8, 0xFF, 0xEE, 0x00, 0x0E, 0x41, 0x64, 0x6F, 0x62, 0x65, 0x00, 0x64, 0x00, 0x00, 0x00, 0x00];
/// The most bytes of an element the IR holds (the view shows at most 1024).
const HOLD: u64 = 1 << 10;
/// The most times the root's translation and the IR's walk, which the scan prepares, start
/// again for bytes they missed (each start walks the whole dataset again). The scan and
/// the profile need no such bound: each of their rounds asks for bytes not yet fed, and
/// `step` fails when a range asked for was not fed.
const MAX_RETRIES: u32 = 64;

#[derive(Clone, Copy, PartialEq, Debug)]
enum Phase {
    Start,
    Scan,
    Profile,
    After,
    Translate,
    Done,
}

// ---- the scan: the bytes the walks will need, read in windows

#[derive(Clone, Debug)]
enum Frame {
    Meta { pos: u64 },
    Data { pos: u64, end: u64, defined: bool, enc: Enc, depth: u32, top: bool, after: bool },
    Seq { pos: u64, end: Option<u64>, limit: u64, enc: Enc, depth: u32 },
    Frag { pos: u64, limit: u64, little: bool },
}

struct Slot {
    f: Frame,
    /// a sequence of defined length: a structure that breaks a rule inside it is its bytes
    tolerant: bool,
    /// its parent goes on where it ends
    resume: bool,
}

enum Act {
    Next,
    Push(Slot),
    Complete(u64),
    Stop,
}

/// Whether an element of defined length may be a sequence that the walks enter, and in which
/// encoding (spec/zarrify/dicom.md §5
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#5-source-metadata>).
fn sequence_encoding(el: &El, enc: Enc) -> Option<Enc> {
    if !enc.explicit || el.vr_is("UN") {
        (dictionary_vr(el.tag) == Some("SQ")).then_some(if enc.explicit { IMPLICIT_LE } else { enc })
    } else {
        el.vr_is("SQ").then_some(enc)
    }
}

/// The first string value of a value: split at 5C, without leading and trailing 20 and 00.
fn first_string(v: &[u8]) -> &[u8] {
    let p = v.split(|&c| c == b'\\').next().unwrap_or(&[]);
    let mut s = 0;
    let mut e = p.len();
    while e > s && (p[e - 1] == b' ' || p[e - 1] == 0) {
        e -= 1;
    }
    while s < e && (p[s] == b' ' || p[s] == 0) {
        s += 1;
    }
    &p[s..e]
}

/// A transfer syntax's (encoding, codec), from the schema's table.
fn syntax(rules: &Rules, uid: &[u8]) -> Option<(Enc, &'static str)> {
    let s = rules.table("syntaxes").get(latin1(uid))?;
    let codec = match s["codec"].as_str()? {
        "jpeg" => "jpeg",
        "jpeg2k" => "jpeg2k",
        _ => "native",
    };
    Some((Enc { explicit: s["explicit"].as_bool()?, little: s["little"].as_bool()? }, codec))
}

// ---- the profile's attributes (spec/zarrify/dicom.md §2.2
// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#22-attributes>)

struct Vals<'a> {
    b: &'a Bytes,
    ds: Option<&'a Dataset>,
    rules: &'static Rules,
}

impl<'a> Vals<'a> {
    fn element(&self, tag: u32, vr: &str) -> R<Option<El>> {
        let Some(w) = self.ds.and_then(|d| d.get(&tag)) else { return Ok(None) };
        let el = w.el;
        if el.length == 0 {
            return Ok(None);
        }
        let mut env = rules::env([
            ("tag", text(&tag_name(tag))),
            ("vr", el.vr.map(|v| text(vr_str(&v))).unwrap_or(crate::expr::V::Absent)),
            ("expected", text(vr)),
            ("undefined", flag(el.undefined())),
        ]);
        check(self.rules, "attribute", &mut env)?;
        Ok(Some(el))
    }

    fn whole(&self, el: &El, n: u64) -> R<()> {
        let mut env = rules::env([("tag", text(&tag_name(el.tag))), ("whole", flag(el.length as u64 % n == 0)), ("n", num(n as f64))]);
        check(self.rules, "whole_values", &mut env)
    }

    fn integer(&self, tag: u32, vr: &str) -> R<Option<u64>> {
        let Some(el) = self.element(tag, vr)? else { return Ok(None) };
        if vr == "US" {
            self.whole(&el, 2)?;
            Ok(Some(self.b.u16(el.value, el.little)? as u64))
        } else {
            self.whole(&el, 4)?;
            Ok(Some(self.b.u32(el.value, el.little)? as u64))
        }
    }

    fn integers64(&self, tag: u32) -> R<Option<Vec<u64>>> {
        let Some(el) = self.element(tag, "OV")? else { return Ok(None) };
        self.whole(&el, 8)?;
        let raw = self.b.get(el.value, el.length as u64)?;
        let v: Vec<u64> = raw
            .chunks(8)
            .map(|c| u64::at(c, 0, el.little))
            .collect();
        let largest = v.iter().copied().max().unwrap_or(0);
        let mut env = rules::env([("tag", text(&tag_name(tag))), ("largest", num(largest as f64))]);
        // the comparison as integers: an f64 rounds 2^53 down to 2^53 - 1 + 1 alike
        if largest > (1u64 << 53) - 1 {
            env.insert("largest", num(f64::INFINITY));
        }
        check(self.rules, "integers64", &mut env)?;
        Ok(Some(v))
    }

    fn tag(&self, tag: u32) -> R<Option<u32>> {
        let Some(el) = self.element(tag, "AT")? else { return Ok(None) };
        self.whole(&el, 4)?;
        Ok(Some((self.b.u16(el.value, el.little)? as u32) << 16 | self.b.u16(el.value + 2, el.little)? as u32))
    }

    fn strings(&self, tag: u32, vr: &str) -> R<Option<Vec<&'a [u8]>>> {
        let Some(el) = self.element(tag, vr)? else { return Ok(None) };
        let raw = self.b.get(el.value, el.length as u64)?;
        Ok(Some(raw.split(|&c| c == b'\\').map(|p| first_string(p)).collect()))
    }

    fn string(&self, tag: u32, vr: &str) -> R<Option<&'a [u8]>> {
        Ok(self.strings(tag, vr)?.map(|v| v[0]))
    }

    /// Number of Frames: its integer, or +-infinity past 16 digits.
    fn integer_string(&self, tag: u32) -> R<Option<f64>> {
        let Some(value) = self.string(tag, "IS")? else { return Ok(None) };
        let mut env = rules::env([
            ("tag", text(&tag_name(tag))),
            ("value", text(&format!("b'{}'", value.escape_ascii()))),
            ("integer", flag(is_integer(value))),
        ]);
        check(self.rules, "integer_string", &mut env)?;
        let negative = value[0] == b'-';
        let digits = std::str::from_utf8(value).unwrap().trim_start_matches(['+', '-']).trim_start_matches('0');
        let m = if digits.len() > 16 { f64::INFINITY } else if digits.is_empty() { 0.0 } else { digits.parse::<u64>().unwrap() as f64 };
        Ok(Some(if negative { -m } else { m }))
    }

    fn decimals(&self, tag: u32) -> R<Option<Vec<Option<f64>>>> {
        let Some(values) = self.strings(tag, "DS")? else { return Ok(None) };
        Ok(Some(
            values
                .iter()
                .map(|v| {
                    if !is_decimal(v) {
                        return None;
                    }
                    std::str::from_utf8(v).ok()?.parse::<f64>().ok().filter(|x| x.is_finite())
                })
                .collect(),
        ))
    }
}

fn check(rules: &'static Rules, role: &str, env: &mut Vars<'static>) -> R<()> {
    rules.check(role, env).map_err(Fail::Reject)
}

fn opt(v: Option<u64>) -> crate::expr::V {
    v.map(|x| num(x as f64)).unwrap_or(crate::expr::V::Absent)
}

/// The image and its pixel data's layout, as the profile reads them.
#[derive(Clone, Debug)]
pub struct Image {
    pub start: u64,
    pub enc: Enc,
    pub codec: &'static str,
    pub syntax: String,
    pub pixel: El,
    pub spp: u64,
    pub planar_native: bool,
    pub n: u64,
    pub rows: u64,
    pub columns: u64,
    pub bits_allocated: u64,
    pub bits_stored: u64,
    pub signed: bool,
    pub photometric: String,
    pub whole_slide: bool,
    pub width: u64,
    pub height: u64,
    pub across: u64,
    pub frame_axis: &'static str,
    pub scale: Vec<f64>,
    pub units: Map<String, J>,
    pub axes: Vec<&'static str>,
    pub window: J,
    pub window_finite: bool,
    pub eot: Option<Vec<u64>>,
    pub eotl: Option<Vec<u64>>,
}

#[derive(Clone, Debug, Default)]
pub struct Layout {
    /// where Pixel Data ends
    pub end: u64,
    /// native: a frame's size
    pub frame_size: u64,
    /// native: the bytes past the frames, (offset, length)
    pub extra: Option<(u64, u64)>,
    /// encapsulated: the items (item offset, data offset, data length), the Basic Offset
    /// Table's first; where the sequence's delimiter is
    pub items: Vec<(u64, u64, u32)>,
    pub delimiter: Option<u64>,
    /// the fragments of frame f are bounds[f]..bounds[f + 1] (after the Basic Offset Table)
    pub bounds: Vec<u64>,
    /// Extended Offset Table: the frames' (offset, length) of their data, whether their
    /// item headers precede them, and the members of the bytes between their items
    pub eot: Option<(Vec<(u64, u64)>, bool, Vec<(u64, u64)>)>,
    pub offset_table: bool,
    pub multiple: bool,
}

/// The fragment walk of the top-level Pixel Data, kept across rounds.
#[derive(Default)]
struct Chain {
    pos: u64,
    items: Vec<(u64, u64, u32)>,
    end: Option<u64>,
}

pub struct Dicom {
    size: u64,
    phase: Phase,
    b: Bytes,
    rules: &'static Rules,
    stack: Vec<Slot>,
    syntax: Option<Vec<u8>>,
    top_tags: HashSet<u32>,
    window: u64,
    last_end: u64,
    want: Vec<(u64, u64)>,
    /// the last batch asked for: each must be fed before the next step
    asked: Vec<(u64, u64)>,
    image: Option<Image>,
    layout: Option<Layout>,
    chain: Option<Chain>,
    retries: u32,
    pub ir: Ir,
    facts: Option<J>,
    /// what the parse asked for
    pub counts: sans::Asked,
    pub held_max: u64,
}

pub fn rules() -> &'static Rules {
    static S: std::sync::OnceLock<Rules> = std::sync::OnceLock::new();
    S.get_or_init(|| Rules::new(include_str!("../../schema/dicom.json")).expect("the DICOM schema is valid"))
}

impl Dicom {
    pub fn new(size: u64) -> Self {
        Dicom {
            size,
            phase: Phase::Start,
            b: Bytes::new(size),
            rules: rules(),
            stack: Vec::new(),
            syntax: None,
            top_tags: HashSet::new(),
            window: FIRST,
            last_end: 0,
            want: Vec::new(),
            asked: Vec::new(),
            image: None,
            layout: None,
            chain: None,
            retries: 0,
            ir: Ir::new(size),
            facts: None,
            counts: sans::Asked::default(),
            held_max: 0,
        }
    }

    pub fn feed(&mut self, offset: u64, data: Vec<u8>) {
        self.b.feed(offset, data);
        self.held_max = self.held_max.max(self.b.held());
    }

    fn ask(&mut self, r: Vec<(u64, u64)>) -> Step {
        let step = self.counts.ask(r);
        if let Step::Read(r) = &step {
            self.asked = r.clone();
        }
        step
    }

    /// A window at `o` for a walk that missed `n` bytes there: doubling while the walk
    /// keeps to the bytes after the last window.
    fn window_at(&mut self, o: u64, n: u64) -> (u64, u64) {
        self.window = if o == self.last_end { (self.window * 2).min(MAX_WINDOW) } else { FIRST };
        let end = (o + n.max(self.window)).min(self.size);
        self.last_end = end;
        (o, end - o)
    }

    /// Asks for what a walk missed (and what it said it will want), or fails.
    fn missed(&mut self, o: u64, n: u64) -> Result<Step, String> {
        if self.phase == Phase::Translate {
            self.retries += 1;
            if self.retries > MAX_RETRIES {
                return Err("internal: the DICOM parser asked for bytes too many times".into());
            }
        }
        let mut r = std::mem::take(&mut self.want);
        if !self.b.has(o, n) && o.saturating_add(n) <= self.size {
            let w = self.window_at(o, n);
            r.push(w);
        }
        r.retain(|&(a, m)| !self.b.has(a, m));
        if r.is_empty() {
            return Err(format!("internal: the DICOM parser missed bytes it was fed ({o}, {n})"));
        }
        Ok(self.ask(r))
    }

    pub fn step(&mut self) -> Result<Step, String> {
        // every range asked for was fed: so each round reads bytes not yet fed, and ends
        if let Some(&(o, n)) = self.asked.iter().find(|&&(o, n)| !self.b.has(o, n)) {
            return Err(format!("internal: the range ({o}, {n}) was asked for and not fed"));
        }
        self.asked.clear();
        loop {
            match self.phase {
                Phase::Start => {
                    let mut env = rules::env([("size", num(self.size as f64)), ("magic", flag(true))]);
                    if self.size < 132 {
                        check(self.rules, "file", &mut env).map_err(fail_text)?;
                    }
                    self.phase = Phase::Scan;
                    self.stack.push(Slot { f: Frame::Meta { pos: 132 }, tolerant: false, resume: false });
                    self.last_end = FIRST.min(self.size);
                    return Ok(self.ask(vec![(0, FIRST.min(self.size))]));
                }
                Phase::Scan | Phase::After => match self.scan() {
                    Err(Fail::Miss(o, n)) => return self.missed(o, n),
                    Err(Fail::Reject(m)) => return Err(m.trim_start_matches('\u{0}').to_string()),
                    Ok(()) => {
                        self.phase = if self.phase == Phase::Scan { Phase::Profile } else { Phase::Translate };
                    }
                },
                Phase::Profile => match self.profile() {
                    Err(Fail::Miss(o, n)) => return self.missed(o, n),
                    Err(Fail::Reject(m)) => return Err(m.trim_start_matches('\u{0}').to_string()),
                    Ok(()) => {
                        let lay = self.layout.as_ref().unwrap();
                        let enc = self.image.as_ref().unwrap().enc;
                        self.stack.push(Slot {
                            f: Frame::Data { pos: lay.end, end: self.size, defined: false, enc, depth: 0, top: true, after: true },
                            tolerant: false,
                            resume: false,
                        });
                        self.phase = Phase::After;
                    }
                },
                Phase::Translate => {
                    let mark = self.ir.mark();
                    match self.build() {
                        Err(Fail::Miss(o, n)) => {
                            self.ir.rollback(mark);
                            return self.missed(o, n);
                        }
                        Err(Fail::Reject(m)) => return Err(m.trim_start_matches('\u{0}').to_string()),
                        Ok(()) => {
                            self.phase = Phase::Done;
                            return Ok(Step::Done);
                        }
                    }
                }
                Phase::Done => return Ok(Step::Done),
            }
        }
    }

    // ---- the scan

    fn scan(&mut self) -> R<()> {
        loop {
            let Some(slot) = self.stack.last() else { return Ok(()) };
            let f = slot.f.clone();
            let act = match self.scan_one(&f) {
                Ok(a) => a,
                Err(Fail::Reject(_)) => {
                    // back to the sequence of defined length the failure is inside, if any
                    loop {
                        match self.stack.pop() {
                            Some(s) if s.tolerant => break,
                            Some(_) => {}
                            None => return Ok(()),
                        }
                    }
                    continue;
                }
                Err(m) => return Err(m),
            };
            match act {
                Act::Next => {}
                Act::Push(s) => self.stack.push(s),
                Act::Complete(p) => {
                    let s = self.stack.pop().unwrap();
                    if s.resume {
                        if let Some(parent) = self.stack.last_mut() {
                            match &mut parent.f {
                                Frame::Data { pos, .. } | Frame::Seq { pos, .. } => *pos = p,
                                _ => {}
                            }
                        }
                    }
                }
                Act::Stop => {
                    self.stack.clear();
                    return Ok(());
                }
            }
        }
    }

    fn set_pos(&mut self, p: u64) {
        match &mut self.stack.last_mut().unwrap().f {
            Frame::Meta { pos } | Frame::Data { pos, .. } | Frame::Seq { pos, .. } | Frame::Frag { pos, .. } => *pos = p,
        }
    }

    fn scan_one(&mut self, f: &Frame) -> R<Act> {
        let size = self.size;
        match *f {
            Frame::Meta { pos } => {
                if pos + 2 > size || self.b.u16(pos, true)? != 2 {
                    let Some((enc, _)) = self.syntax.as_deref().and_then(|s| syntax(self.rules, s)) else { return Ok(Act::Stop) };
                    self.stack.pop();
                    return Ok(Act::Push(Slot {
                        f: Frame::Data { pos, end: size, defined: false, enc, depth: 0, top: true, after: false },
                        tolerant: false,
                        resume: false,
                    }));
                }
                let el = self.b.header(pos, size, EXPLICIT_LE)?;
                if el.undefined() || el.end() > size {
                    return Ok(Act::Stop);
                }
                if el.length as u64 <= MAX_TEXT {
                    let v = self.b.get(el.value, el.length as u64)?;
                    if el.tag == 0x0002_0010 && self.syntax.is_none() {
                        self.syntax = Some(first_string(v).to_vec());
                    }
                }
                self.set_pos(el.end());
                Ok(Act::Next)
            }
            Frame::Data { pos, end, defined, enc, depth, top, after } => {
                if defined && pos == end {
                    return Ok(Act::Complete(pos));
                }
                if pos >= end {
                    return Ok(Act::Stop);
                }
                let el = match self.b.header(pos, end, enc) {
                    Err(Fail::Reject(_)) if after => return Ok(Act::Stop),
                    r => r?,
                };
                if el.tag >> 16 == 0xFFFE {
                    if el.tag == ITEM_END && !defined && !top && el.length == 0 {
                        return Ok(Act::Complete(el.value));
                    }
                    return if top { Ok(Act::Stop) } else { reject("unexpected item tag") };
                }
                if top && !after && el.tag == PIXEL_DATA {
                    self.top_tags.insert(el.tag);
                    return Ok(Act::Stop);
                }
                if after && self.top_tags.contains(&el.tag) {
                    return Ok(Act::Stop);
                }
                if el.undefined() {
                    if enc.explicit && !el.vr_is("SQ") && !el.vr_is("UN") && !(el.tag == PIXEL_DATA && (el.vr_is("OB") || el.vr_is("OW"))) {
                        return reject("undefined length");
                    }
                    if top {
                        self.top_tags.insert(el.tag);
                    }
                    let f = if el.tag == PIXEL_DATA {
                        Frame::Frag { pos: el.value, limit: end, little: enc.little }
                    } else {
                        let inner = if el.vr_is("UN") { IMPLICIT_LE } else { enc };
                        Frame::Seq { pos: el.value, end: None, limit: end, enc: inner, depth }
                    };
                    return Ok(Act::Push(Slot { f, tolerant: false, resume: true }));
                }
                if el.end() > end {
                    return reject("past its container");
                }
                let n = el.length as u64;
                let inner = sequence_encoding(&el, enc);
                if inner.is_none() && (n <= MAX_TEXT || (top && EOT_TAGS.contains(&el.tag))) {
                    self.b.get(el.value, n)?;
                }
                if top {
                    self.top_tags.insert(el.tag);
                }
                self.set_pos(el.end());
                match inner {
                    Some(inner) if n > 0 => Ok(Act::Push(Slot {
                        f: Frame::Seq { pos: el.value, end: Some(el.end()), limit: end, enc: inner, depth },
                        tolerant: true,
                        resume: false,
                    })),
                    _ => Ok(Act::Next),
                }
            }
            Frame::Seq { pos, end, limit, enc, depth } => {
                let container = end.unwrap_or(limit);
                if end == Some(pos) {
                    return Ok(Act::Complete(pos));
                }
                let el = self.b.header(pos, container, enc)?;
                if end.is_none() && el.tag == SEQUENCE_END && el.length == 0 {
                    return Ok(Act::Complete(el.value));
                }
                if el.tag != ITEM || depth + 1 > MAX_DEPTH {
                    return reject("expected an item");
                }
                if el.undefined() {
                    return Ok(Act::Push(Slot {
                        f: Frame::Data { pos: el.value, end: container, defined: false, enc, depth: depth + 1, top: false, after: false },
                        tolerant: false,
                        resume: true,
                    }));
                }
                if el.end() > container {
                    return reject("past its container");
                }
                self.set_pos(el.end());
                Ok(Act::Push(Slot {
                    f: Frame::Data { pos: el.value, end: el.end(), defined: true, enc, depth: depth + 1, top: false, after: false },
                    tolerant: false,
                    resume: false,
                }))
            }
            Frame::Frag { pos, limit, little } => match fragment(&self.b, pos, limit, little)? {
                Frag::End(e) => Ok(Act::Complete(e)),
                Frag::Item(n) => {
                    self.set_pos(pos + 8 + n as u64);
                    Ok(Act::Next)
                }
            },
        }
    }

    // ---- the profile (spec/zarrify/dicom.md §7
    // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#7-dicom-profile>)

    fn profile(&mut self) -> R<()> {
        if self.image.is_none() {
            self.image = Some(self.attributes()?);
        }
        let img = self.image.clone().unwrap();
        let layout = self.pixel_layout(&img)?;
        self.frames_check(&img, &layout)?;
        self.layout = Some(layout);
        Ok(())
    }

    fn attributes(&self) -> R<Image> {
        let b = &self.b;
        let size = self.size;
        let r = self.rules;
        let magic = size >= 132 && b.get(128, 4)? == b"DICM";
        check(r, "file", &mut rules::env([("size", num(size as f64)), ("magic", flag(magic))]))?;
        let walker = Walker { b };
        let (meta, start) = walker.meta()?;
        let uid = Vals { b, ds: Some(&meta), rules: r }.string(0x0002_0010, "UI")?;
        check(r, "transfer_syntax", &mut rules::env([("syntax", uid.map(|u| text(&latin1(u))).unwrap_or(crate::expr::V::Absent))]))?;
        let (enc, codec) = syntax(r, uid.unwrap()).unwrap();
        let (top, _) = walker.dataset(start, size, false, enc, 0, true)?;
        let pixel = top[&PIXEL_DATA].el;
        let measures = top
            .get(&0x5200_9229)
            .and_then(|s| s.items.as_ref())
            .and_then(|i| i.first())
            .and_then(|i| i.get(&0x0028_9110))
            .and_then(|p| p.items.as_ref())
            .and_then(|i| i.first());
        let v = Vals { b, ds: Some(&top), rules: r };
        let m = Vals { b, ds: measures, rules: r };
        let sop_class = v.string(0x0008_0016, "UI")?;
        let organization = v.string(0x0020_9311, "CS")?;
        let spp = v.integer(0x0028_0002, "US")?;
        let photometric = v.string(0x0028_0004, "CS")?;
        let planar = v.integer(0x0028_0006, "US")?;
        let frames = v.integer_string(0x0028_0008)?;
        let rows = v.integer(0x0028_0010, "US")?;
        let columns = v.integer(0x0028_0011, "US")?;
        let bits_allocated = v.integer(0x0028_0100, "US")?;
        let bits_stored = v.integer(0x0028_0101, "US")?;
        let high_bit = v.integer(0x0028_0102, "US")?;
        let representation = v.integer(0x0028_0103, "US")?;
        let center = v.decimals(0x0028_1050)?;
        let width = v.decimals(0x0028_1051)?;
        let intercept = v.decimals(0x0028_1052)?;
        let slope = v.decimals(0x0028_1053)?;
        let total_columns = v.integer(0x0048_0006, "UL")?;
        let total_rows = v.integer(0x0048_0007, "UL")?;
        let optical_paths = v.integer(0x0048_0302, "UL")?;
        let focal_planes = v.integer(0x0048_0303, "UL")?;
        let eot = v.integers64(0x7FE0_0001)?;
        let eotl = v.integers64(0x7FE0_0002)?;
        let increment = v.tag(0x0028_0009)?;
        let frame_time = v.decimals(0x0018_1063)?;
        check(r, "pixel_vr", &mut rules::env([("vr", pixel.vr.map(|x| text(vr_str(&x))).unwrap_or(crate::expr::V::Absent))]))?;
        let mut fg = Vec::new();
        for tag in [0x0028_0030, 0x0018_0088] {
            let in_item = m.decimals(tag)?;
            let at_top = v.decimals(tag)?;
            fg.push(in_item.or(at_top));
        }

        // spec/zarrify/dicom.md §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#3-the-image>
        let n = frames.unwrap_or(1.0);
        let key = format!("{codec}/{}", spp.map(|s| s.to_string()).unwrap_or_default());
        let mut env = rules::env([
            ("spp", opt(spp)),
            ("photometric", photometric.map(|p| text(&latin1(p))).unwrap_or(crate::expr::V::Absent)),
            ("rows", opt(rows)),
            ("columns", opt(columns)),
            ("bits_allocated", opt(bits_allocated)),
            ("bits_stored", opt(bits_stored)),
            ("representation", opt(representation)),
            ("planar", opt(planar)),
            ("n", num(n)),
            ("high_bit", opt(high_bit)),
            ("codec", text(codec)),
        ]);
        env.insert("key", text(&key));
        check(r, "image", &mut env)?;
        let (spp, rows, columns) = (spp.unwrap(), rows.unwrap(), columns.unwrap());
        let (bits_allocated, bits_stored, representation) = (bits_allocated.unwrap(), bits_stored.unwrap(), representation.unwrap());
        let photometric = latin1(photometric.unwrap());
        let n = n as u64;
        let whole_slide = sop_class.is_some_and(|s| s == self.rules.table("whole_slide").as_str().unwrap().as_bytes());
        let (width_px, height_px, across) = if whole_slide {
            let across = total_columns.map(|w| w.div_ceil(columns));
            let down = total_rows.map(|h| h.div_ceil(rows));
            let tiles = across.zip(down).is_some_and(|(a, d)| (a as u128) * (d as u128) == n as u128);
            let mut env = rules::env([
                ("organization", organization.map(|o| text(&latin1(o))).unwrap_or(crate::expr::V::Absent)),
                ("width", opt(total_columns)),
                ("height", opt(total_rows)),
                ("optical_paths", opt(optical_paths)),
                ("focal_planes", opt(focal_planes)),
                ("n", num(n as f64)),
                ("across", opt(across)),
                ("down", opt(down)),
                ("tiles", flag(tiles)),
            ]);
            check(r, "whole_slide", &mut env)?;
            (total_columns.unwrap(), total_rows.unwrap(), across.unwrap())
        } else {
            (columns, rows, 1)
        };

        // spec/zarrify/dicom.md §4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#4-output>
        let fa = if matches!(increment, Some(0x0018_1063 | 0x0018_1065)) { "t" } else { "z" };
        let mut axes = Vec::new();
        if spp == 3 {
            axes.push("c");
        }
        if !whole_slide && n > 1 {
            axes.push(fa);
        }
        axes.extend(["y", "x"]);
        let spacing = &fg[0];
        let usable = spacing.as_ref().is_some_and(|s| s.len() == 2 && s.iter().all(|x| x.is_some_and(|x| x > 0.0)));
        let step = fg[1].as_ref().and_then(|b| b[0]).filter(|x| *x > 0.0);
        let period = if increment == Some(0x0018_1063) { frame_time.as_ref().and_then(|t| t[0]).filter(|x| *x > 0.0).map(|x| x / 1000.0) } else { None };
        let scale_of = |a: &str| -> f64 {
            match a {
                "z" => step.unwrap_or(1.0),
                "t" => period.unwrap_or(1.0),
                "y" if usable => spacing.as_ref().unwrap()[0].unwrap(),
                "x" if usable => spacing.as_ref().unwrap()[1].unwrap(),
                _ => 1.0,
            }
        };
        let scale = axes.iter().map(|a| scale_of(a)).collect();
        let mut units = Map::new();
        for a in &axes {
            let u = match *a {
                "z" if step.is_some() => "millimeter",
                "t" if period.is_some() => "second",
                "y" | "x" if usable => "millimeter",
                _ => continue,
            };
            units.insert(a.to_string(), json!(u));
        }
        let (low, high): (i64, i64) = if representation == 0 {
            (0, (1i64 << bits_stored) - 1)
        } else {
            (-(1i64 << (bits_stored - 1)), (1i64 << (bits_stored - 1)) - 1)
        };
        let mut window_finite = true;
        let mut window = json!({"min": low, "max": high, "start": low, "end": high});
        if spp == 1 {
            if let (Some(c), Some(w)) = (center.as_ref().and_then(|c| c[0]), width.as_ref().and_then(|w| w[0])) {
                let k = match &intercept { None => Some(0.0), Some(i) => i[0] };
                let s = match &slope { None => Some(1.0), Some(s) => s[0] };
                if let (true, Some(k), Some(s)) = (w >= 1.0, k, s) {
                    if s != 0.0 {
                        let (mut a, mut e) = ((c - w / 2.0 - k) / s, (c + w / 2.0 - k) / s);
                        if s < 0.0 {
                            std::mem::swap(&mut a, &mut e);
                        }
                        window_finite = a.is_finite() && e.is_finite();
                        window["start"] = json!(a);
                        window["end"] = json!(e);
                    }
                }
            }
        }
        Ok(Image {
            start,
            enc,
            codec,
            syntax: latin1(uid.unwrap()),
            pixel,
            spp,
            planar_native: codec == "native" && spp == 3 && planar == Some(1),
            n,
            rows,
            columns,
            bits_allocated,
            bits_stored,
            signed: representation == 1,
            photometric,
            whole_slide,
            width: width_px,
            height: height_px,
            across,
            frame_axis: fa,
            scale,
            units,
            axes,
            window,
            window_finite,
            eot,
            eotl,
        })
    }

    /// The pixel data's layout (spec/zarrify/dicom.md §7.5
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#75-frames>).
    fn pixel_layout(&mut self, img: &Image) -> R<Layout> {
        let r = self.rules;
        let size = self.size;
        let pixel = img.pixel;
        let n = img.n;
        let frame_size = img.rows * img.columns * img.spp * (img.bits_allocated / 8);
        let mut lay = Layout { frame_size, ..Default::default() };
        if img.codec == "native" {
            let within = !pixel.undefined() && pixel.end() <= size;
            let enough = (pixel.length as u128) >= n as u128 * frame_size as u128;
            let mut env = rules::env([
                ("undefined", flag(pixel.undefined())),
                ("within", flag(within)),
                ("length", num(pixel.length as f64)),
                ("size", num(size as f64)),
                ("n", num(n as f64)),
                ("frame_size", num(frame_size as f64)),
                ("enough", flag(enough)),
                ("ow8_big", flag(!img.enc.little && img.bits_allocated == 8 && pixel.vr_is("OW"))),
            ]);
            check(r, "native", &mut env)?;
            lay.end = pixel.end();
            let extra = pixel.length as u64 - n * frame_size;
            if extra > 1 || (extra == 1 && self.b.get(lay.end - 1, 1)? != [0]) {
                lay.extra = Some((pixel.value + n * frame_size, extra));
            }
            return Ok(lay);
        }
        let within = pixel.value + 8 <= size;
        let (tag, bot_length) = if pixel.undefined() && within {
            ((self.b.u16(pixel.value, true)? as u32) << 16 | self.b.u16(pixel.value + 2, true)? as u32, self.b.u32(pixel.value + 4, true)?)
        } else {
            (0, 0)
        };
        let mut env = rules::env([
            ("defined", flag(!pixel.undefined())),
            ("within", flag(within)),
            ("item", flag(tag == ITEM && bot_length != UNDEFINED && bot_length % 4 == 0)),
        ]);
        check(r, "encapsulated", &mut env)?;
        let q = pixel.value + 8 + bot_length as u64;
        let (eot, eotl) = (img.eot.clone(), img.eotl.clone());
        if eot.is_some() || eotl.is_some() {
            let counts = eot.as_ref().is_some_and(|e| e.len() as u64 == n) && eotl.as_ref().is_some_and(|e| e.len() as u64 == n);
            let mut env = rules::env([("both", flag(eot.is_some() && eotl.is_some())), ("counts", flag(counts)), ("bot_length", num(bot_length as f64))]);
            check(r, "extended_offset_table", &mut env)?;
            let (eot, eotl) = (eot.unwrap(), eotl.unwrap());
            let data: Vec<(u64, u64)> = (0..n as usize).map(|f| (q + eot[f] + 8, eotl[f])).collect();
            for &(o, m) in &data {
                check(r, "range", &mut rules::env([("start", num(o as f64)), ("end", num((o + m) as f64)), ("size", num(size as f64))]))?;
            }
            // each frame's 8 bytes before its data: one batch
            self.want = data.iter().map(|&(o, _)| (o - 8, 8)).filter(|&(o, m)| !self.b.has(o, m)).collect();
            if let Some(&(o, m)) = self.want.first() {
                return Err(Fail::Miss(o, m));
            }
            let headers = data.iter().all(|&(o, m)| {
                let h = self.b.get(o - 8, 8).unwrap();
                h[..4] == [0xFE, 0xFF, 0x00, 0xE0] && u32::le_at(h, 4) as u64 == m
            });
            let mut spans: Vec<(u64, u64)> = data.iter().map(|&(o, m)| (o - 8, 8 + m)).collect();
            spans.sort_unstable();
            let disjoint = spans.windows(2).all(|w| w[0].0 + w[0].1 <= w[1].0);
            check(r, "extended_frames", &mut rules::env([("disjoint", flag(disjoint))]))?;
            let mut items: Vec<(u64, u64)> = data.iter().map(|&(o, m)| if headers { (o - 8, 8 + m) } else { (o, m) }).collect();
            items.sort_unstable();
            let mut members = Vec::new();
            let mut end = q;
            for &(o, m) in &items {
                members.push((end, o.saturating_sub(end)));
                end = end.max(o + m);
            }
            if end + 8 <= size {
                let h = self.b.get(end, 8)?;
                if h == [0xFE, 0xFF, 0xDD, 0xE0, 0, 0, 0, 0] {
                    lay.delimiter = Some(end);
                    end += 8;
                }
            }
            lay.end = end;
            lay.eot = Some((data, headers, members));
            return Ok(lay);
        }
        // the fragments' item headers, kept across rounds
        let chain = self.chain.get_or_insert_with(|| Chain { pos: pixel.value, ..Default::default() });
        while chain.end.is_none() {
            match fragment(&self.b, chain.pos, size, true) {
                Ok(Frag::End(e)) => chain.end = Some(e),
                Ok(Frag::Item(m)) => {
                    chain.items.push((chain.pos, chain.pos + 8, m));
                    chain.pos += 8 + m as u64;
                }
                Err(Fail::Miss(o, m)) => {
                    // the headers the Basic Offset Table predicts, with the window at the walk
                    let mut want = Vec::new();
                    if bot_length > 0 && self.b.has(pixel.value + 8, bot_length as u64) {
                        let raw = self.b.get(pixel.value + 8, bot_length as u64).unwrap();
                        for c in raw.chunks(4) {
                            let p = q + u32::le_at(c, 0) as u64;
                            if p > chain.pos && p + 8 <= size && !self.b.has(p, 8) {
                                want.push((p, 8));
                            }
                        }
                    } else if bot_length > 0 && bot_length as u64 <= MAX_WINDOW && pixel.value + 8 + bot_length as u64 <= size {
                        want.push((pixel.value + 8, bot_length as u64));
                    }
                    self.want = want;
                    return Err(Fail::Miss(o, m));
                }
                Err(e) => return Err(e),
            }
        }
        let items: Vec<(u64, u64, u32)> = chain.items.clone();
        let chain_end = chain.end.unwrap();
        let frags = &items[1..];
        let bot: Vec<u64> = if bot_length > 0 {
            self.b.get(pixel.value + 8, bot_length as u64)?.chunks(4).map(|c| u32::le_at(c, 0) as u64).collect()
        } else {
            Vec::new()
        };
        let position: HashMap<u64, usize> = frags.iter().enumerate().map(|(i, &(p, _, _))| (p - q, i)).collect();
        let increasing = bot.first() == Some(&0) && bot.windows(2).all(|w| w[0] < w[1]);
        let mut env = rules::env([
            ("count", num(frags.len() as f64)),
            ("offsets", num(bot.len() as f64)),
            ("n", num(n as f64)),
            ("increasing", flag(increasing)),
            ("positions", flag(bot.iter().all(|o| position.contains_key(o)))),
        ]);
        check(r, "fragments", &mut env)?;
        let bounds: Vec<u64> = if !bot.is_empty() {
            bot.iter().map(|o| position[o] as u64).chain([frags.len() as u64]).collect()
        } else if frags.len() as u64 == n {
            (0..=n).collect()
        } else {
            vec![0, frags.len() as u64]
        };
        lay.multiple = bounds.windows(2).any(|w| w[1] - w[0] > 1);
        lay.offset_table = !bot.is_empty();
        lay.items = items;
        lay.delimiter = Some(chain_end - 8);
        lay.end = chain_end;
        lay.bounds = bounds;
        Ok(lay)
    }

    /// Each frame's ranges: their data, the SOI of a JPEG with three samples, and their
    /// references' sizes.
    fn frames_check(&mut self, img: &Image, lay: &Layout) -> R<()> {
        let r = self.rules;
        if img.codec == "native" {
            return check(r, "window", &mut rules::env([("finite", flag(img.window_finite))]));
        }
        let adobe = img.codec == "jpeg" && img.spp == 3;
        let frames = frame_ranges(img, lay);
        if adobe {
            self.want = frames.iter().filter_map(|f| f.first()).filter(|&&(o, m)| m > 2 && !self.b.has(o, 2)).map(|&(o, _)| (o, 2)).collect();
            if let Some(&(o, m)) = self.want.first() {
                return Err(Fail::Miss(o, m));
            }
        }
        for (f, ranges) in frames.iter().enumerate() {
            let first = ranges.first().map_or(0, |x| x.1);
            let soi = adobe && first > 2 && self.b.get(ranges[0].0, 2)? == [0xFF, 0xD8];
            let mut env = rules::env([
                ("f", num(f as f64)),
                ("ranges", num(ranges.len() as f64)),
                ("adobe", flag(adobe)),
                ("first", num(first as f64)),
                ("soi", flag(soi)),
            ]);
            check(r, "frame", &mut env)?;
        }
        check(r, "window", &mut rules::env([("finite", flag(img.window_finite))]))?;
        for (f, ranges) in frames.iter().enumerate() {
            let parts = parts_of(ranges, adobe.then(|| adobe_marker(&img.photometric)));
            for p in &parts {
                if let Part::Src(o, m) = p {
                    check(r, "range", &mut rules::env([("start", num(*o as f64)), ("end", num((o + m) as f64)), ("size", num(self.size as f64))]))?;
                }
            }
            check(r, "payload", &mut rules::env([("f", num(f as f64)), ("payload", num(payload_size(&parts) as f64))]))?;
        }
        Ok(())
    }

    // ---- the root's source metadata and the IR

    fn build(&mut self) -> R<()> {
        let img = self.image.clone().unwrap();
        let lay = self.layout.clone().unwrap();
        let pixel = Pixel {
            end: lay.end,
            extra: lay.extra,
            unreferenced: lay.eot.as_ref().and_then(|(_, headers, members)| members.iter().any(|m| m.1 > 0).then_some(!headers)),
            eot_layout: lay.eot.is_some(),
            fragments: lay.multiple,
            offset_table: lay.offset_table,
        };
        let (root, tail) = Translator::new(&self.b).root(img.start, img.enc, &pixel)?;
        let mut e = Emit { b: &self.b, ir: &mut self.ir, form: String::new() };
        let pixel_el = e.file(&img, &lay, tail)?;
        self.facts = Some(facts(&img, &lay, root, pixel_el));
        Ok(())
    }

    pub fn finish(self) -> Result<(Ir, J), String> {
        let mut facts = self.facts.unwrap_or(J::Null);
        self.counts.put(&mut facts);
        facts["held_max"] = json!(self.held_max);
        sans::finish(self.phase == Phase::Done, self.ir, facts)
    }
}

fn fail_text(f: Fail) -> String {
    match f {
        Fail::Reject(m) => m,
        Fail::Miss(o, n) => format!("internal: missed ({o}, {n})"),
    }
}

/// The last byte of the Adobe marker: the color transform (0 for RGB, 1 for YCbCr).
pub fn adobe_marker(photometric: &str) -> u8 {
    u8::from(photometric != "RGB")
}

/// Each frame's ranges of data, in order, without empty ones (the parts before the JPEG
/// color marker).
pub fn frame_ranges(img: &Image, lay: &Layout) -> Vec<Vec<(u64, u64)>> {
    if let Some((data, _, _)) = &lay.eot {
        return data.iter().map(|&r| if r.1 > 0 { vec![r] } else { vec![] }).collect();
    }
    let frags = &lay.items[1..];
    lay.bounds
        .windows(2)
        .take(img.n as usize)
        .map(|w| frags[w[0] as usize..w[1] as usize].iter().filter(|x| x.2 > 0).map(|&(_, o, m)| (o, m as u64)).collect())
        .collect()
}

/// A frame's reference parts: its ranges, the first made explicit about its color
/// transform when `adobe` gives it.
pub fn parts_of(ranges: &[(u64, u64)], adobe: Option<u8>) -> Vec<Part> {
    let mut parts: Vec<Part> = ranges.iter().map(|&(o, m)| Part::Src(o, m)).collect();
    if let (Some(t), Some(&(o, m))) = (adobe, ranges.first()) {
        let mut lit = ADOBE.to_vec();
        lit.push(t);
        parts[0] = Part::Src(o + 2, m - 2);
        parts.insert(0, Part::Lit(lit));
    }
    parts
}

/// The facts the projection reads.
fn facts(img: &Image, lay: &Layout, root: Map<String, J>, pixel: u32) -> J {
    let shape: Vec<u64> = img.axes.iter().map(|a| match *a { "c" => 3, "y" => img.height, "x" => img.width, _ => img.n }).collect();
    let chunk: Vec<u64> = img.axes.iter().map(|a| match *a { "c" => if img.planar_native { 1 } else { 3 }, "y" => img.rows, "x" => img.columns, _ => 1 }).collect();
    let data_type = format!("{}int{}", if img.signed { "" } else { "u" }, img.bits_allocated);
    let layout = if img.codec == "native" { "native" } else if lay.eot.is_some() { "eot" } else { "fragments" };
    json!({
        "root": root,
        "axes": img.axes,
        "shape": shape,
        "chunk_shape": chunk,
        "data_type": data_type,
        "codec": img.codec,
        "little": img.enc.little,
        "planar": img.planar_native,
        "spp": img.spp,
        "n": img.n,
        "whole_slide": img.whole_slide,
        "across": img.across,
        "frame_axis": img.frame_axis,
        "scale": img.scale,
        "units": img.units,
        "window": img.window,
        "photometric": img.photometric,
        "syntax": img.syntax,
        "pixel": pixel,
        "layout": layout,
        "bounds": lay.bounds,
        "adobe": (img.codec == "jpeg" && img.spp == 3).then(|| adobe_marker(&img.photometric)),
    })
}

// ---- the IR (spec/zarrify/dicom.md §5
// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#5-source-metadata>)

fn order(little: bool) -> &'static str {
    if little { "<" } else { ">" }
}

/// The fields of an element's header, as a record type's (without braces).
fn header_fields(el: &El, enc: Enc) -> String {
    let e = order(enc.little);
    match el.vr {
        Some(v) if is_long(&v) => format!("group:{e}u2,element:{e}u2,vr:ascii[2],reserved:{e}u2,length:{e}u4"),
        Some(_) => format!("group:{e}u2,element:{e}u2,vr:ascii[2],length:{e}u2"),
        None => format!("group:{e}u2,element:{e}u2,length:{e}u4"),
    }
}

fn is_long(v: &Vr) -> bool {
    LONG_VRS.contains(&vr_str(v))
}

/// The type of an element's value of `n` bytes, by its VR.
fn value_type(vr: &str, n: u64, little: bool) -> String {
    let e = order(little);
    if is_text(vr) {
        return format!("cstr[{n}]");
    }
    if vr == "AT" && n % 4 == 0 {
        return format!("{e}u2[{},2]", n / 4);
    }
    if let Some((_, s)) = numeric(vr) {
        if n % s == 0 {
            let code = match vr {
                "FL" => "f4",
                "FD" => "f8",
                "SL" => "i4",
                "SS" => "i2",
                "UL" => "u4",
                "US" => "u2",
                "SV" => "i8",
                _ => "u8",
            };
            return format!("{e}{code}[{}]", n / s);
        }
    }
    format!("bytes[{n}]")
}

fn hard(e: String) -> Fail {
    // a budget or IR error: never a structure that breaks a rule
    Fail::Reject(format!("\u{0}{e}"))
}

struct Emit<'a> {
    b: &'a Bytes,
    ir: &'a mut Ir,
    form: String,
}

impl Emit<'_> {
    fn hold(&self, o: u64, n: u64) -> R<Option<&[u8]>> {
        if n > HOLD {
            return Ok(None);
        }
        self.b.get(o, n).map(Some)
    }

    fn value(&mut self, parent: u32, name: &str, nidx: u64, ty: &str, o: u64, n: u64) -> R<u32> {
        let raw = self.hold(o, n)?.map(|r| r.to_vec());
        self.ir.value(parent, name, nidx, ty, 0, (o, n), raw.as_deref()).map_err(hard)
    }

    /// An item or delimiter header (`{group,element,length}`).
    fn mark_value(&mut self, parent: u32, name: &str, nidx: u64, at: u64, little: bool) -> R<u32> {
        let e = order(little);
        self.value(parent, name, nidx, &format!("{{group:{e}u2,element:{e}u2,length:{e}u4}}"), at, 8)
    }

    /// An element of defined length as one value: its header and `v`.
    fn record(&mut self, parent: u32, name: &str, nidx: u64, el: &El, enc: Enc, vr: &str, little: bool) -> R<u32> {
        let n = el.length as u64;
        let ty = format!("{{{},v:{}}}", header_fields(el, enc), value_type(vr, n, little));
        self.value(parent, name, nidx, &ty, el.at, el.end() - el.at)
    }

    /// The whole file: the root's children; the top-level Pixel Data's struct.
    fn file(&mut self, img: &Image, lay: &Layout, tail: u64) -> R<u32> {
        let size = self.b.size;
        self.value(0, "preamble", NO_INDEX, "bytes[128]", 0, 128)?;
        self.value(0, "magic", NO_INDEX, "ascii[4]", 128, 4)?;
        if img.start > 132 {
            let m = self.ir.struct_(0, "meta", NO_INDEX, 0, Some((132, img.start - 132))).map_err(hard)?;
            self.dataset(m, 132, img.start, true, EXPLICIT_LE, 0)?;
        }
        let d = self.ir.struct_(0, "dataset", NO_INDEX, 0, Some((img.start, tail - img.start))).map_err(hard)?;
        let enc = img.enc;
        let mut names = Names::default();
        let mut pos = img.start;
        let pixel;
        loop {
            let el = self.b.header(pos, size, enc)?;
            if el.tag == PIXEL_DATA {
                pixel = self.pixel(d, img, lay)?;
                break;
            }
            let eot_layout = lay.eot.is_some() && EOT_TAGS.contains(&el.tag) && !names.present.contains(&el.tag);
            pos = self.named(d, &el, enc, size, 0, &mut names, eot_layout)?;
        }
        pos = lay.end;
        while pos < tail {
            let el = self.b.header(pos, size, enc)?;
            pos = self.named(d, &el, enc, size, 0, &mut names, false)?;
        }
        if let Some((o, n)) = lay.extra {
            self.value(0, "pixel_extra", NO_INDEX, &format!("bytes[{n}]"), o, n)?;
        }
        if let Some((_, _, members)) = &lay.eot {
            if members.iter().any(|m| m.1 > 0) {
                let s = self.ir.struct_(0, "pixel_unreferenced", NO_INDEX, 0, None).map_err(hard)?;
                for (k, &(o, n)) in members.iter().enumerate() {
                    if n > 0 {
                        self.value(s, "", k as u64, &format!("bytes[{n}]"), o, n)?;
                    }
                }
            }
        }
        if tail < size {
            self.value(0, "trailing", NO_INDEX, &format!("bytes[{}]", size - tail), tail, size - tail)?;
        }
        Ok(pixel)
    }

    /// The element `el` of a dataset, named by its tag (a later one of a tag, under
    /// `duplicates/<k>`); where the next element starts.
    #[allow(clippy::too_many_arguments)]
    fn named(&mut self, parent: u32, el: &El, enc: Enc, end: u64, depth: u32, names: &mut Names, layout: bool) -> R<u64> {
        let key = tag_key(el.tag);
        let (p, name, nidx) = if el.tag == PADDING {
            names.pads += 1;
            if names.pads == 1 { (parent, key, NO_INDEX) } else { (parent, format!("{key}~"), names.pads - 1) }
        } else if names.present.contains(&el.tag) {
            let s = self.ir.struct_(parent, "duplicates/", names.dups, 0, None).map_err(hard)?;
            names.dups += 1;
            (s, key, NO_INDEX)
        } else {
            (parent, key, NO_INDEX)
        };
        let next = if layout { self.record(p, &name, nidx, el, enc, "OV", el.little).map(|_| el.end())? } else { self.element(p, &name, nidx, el, enc, end, depth)? };
        if el.tag != PADDING {
            names.present.insert(el.tag);
        }
        Ok(next)
    }

    #[allow(clippy::too_many_arguments)]
    fn element(&mut self, parent: u32, name: &str, nidx: u64, el: &El, enc: Enc, end: u64, depth: u32) -> R<u64> {
        if el.undefined() {
            if enc.explicit && !el.vr_is("SQ") && !el.vr_is("UN") && !(el.tag == PIXEL_DATA && (el.vr_is("OB") || el.vr_is("OW"))) {
                return reject(format!("undefined length with VR {}", el.vr_text()));
            }
            let s = self.ir.struct_(parent, name, nidx, 0, Some((el.at, 0))).map_err(hard)?;
            self.value(s, "header", NO_INDEX, &format!("{{{}}}", header_fields(el, enc)), el.at, el.value - el.at)?;
            let pos = if el.tag == PIXEL_DATA {
                self.fragments(s, el.value, end, enc.little, None)?
            } else {
                let inner = if el.vr_is("UN") { IMPLICIT_LE } else { enc };
                self.sequence(s, el.value, None, end, inner, depth)?
            };
            self.ir.len[s as usize] = pos - el.at;
            return Ok(pos);
        }
        if el.end() > end {
            return reject(format!("the element at {} runs past its container", el.value));
        }
        let (vr, little) = if !enc.explicit || el.vr_is("UN") {
            (dictionary_vr(el.tag).unwrap_or("UN").to_string(), enc.little || el.vr_is("UN"))
        } else {
            (el.vr_text(), enc.little)
        };
        if el.tag != PADDING {
            if let Some(inner) = sequence_encoding(el, enc) {
                let mark = self.ir.mark();
                let walked = (|| -> R<()> {
                    let s = self.ir.struct_(parent, name, nidx, 0, Some((el.at, el.end() - el.at))).map_err(hard)?;
                    self.value(s, "header", NO_INDEX, &format!("{{{}}}", header_fields(el, enc)), el.at, el.value - el.at)?;
                    self.sequence(s, el.value, Some(el.end()), end, inner, depth)?;
                    Ok(())
                })();
                match walked {
                    Ok(()) => return Ok(el.end()),
                    Err(Fail::Reject(m)) if !m.starts_with('\u{0}') => {
                        self.ir.rollback(mark);
                        self.record(parent, name, nidx, el, enc, "SQ", little)?;
                        return Ok(el.end());
                    }
                    Err(f) => return Err(f),
                }
            }
        }
        self.record(parent, name, nidx, el, enc, &vr, little)?;
        Ok(el.end())
    }

    /// A dataset's elements from `pos` into `parent`; where it ends.
    fn dataset(&mut self, parent: u32, mut pos: u64, end: u64, defined: bool, enc: Enc, depth: u32) -> R<u64> {
        let mut names = Names::default();
        loop {
            if defined && pos == end {
                return Ok(pos);
            }
            let el = self.b.header(pos, end, enc)?;
            if el.tag >> 16 == 0xFFFE {
                if el.tag == ITEM_END && !defined && el.length == 0 {
                    self.mark_value(parent, "end", NO_INDEX, pos, enc.little)?;
                    return Ok(el.value);
                }
                return reject(format!("unexpected item tag at {pos}"));
            }
            pos = self.named(parent, &el, enc, end, depth, &mut names, false)?;
        }
    }

    /// A sequence's items into its struct `s`; where it ends. Items of one shape at one
    /// stride fold into a run.
    fn sequence(&mut self, s: u32, mut pos: u64, end: Option<u64>, limit: u64, enc: Enc, depth: u32) -> R<u64> {
        let container = end.unwrap_or(limit);
        let mut i = 0u64;
        let mut prev: Option<u32> = None;
        loop {
            if end == Some(pos) {
                return Ok(pos);
            }
            let el = self.b.header(pos, container, enc)?;
            if end.is_none() && el.tag == SEQUENCE_END && el.length == 0 {
                self.mark_value(s, "end", NO_INDEX, pos, enc.little)?;
                return Ok(el.value);
            }
            if el.tag != ITEM || depth + 1 > MAX_DEPTH {
                return reject(format!("expected an item at {pos}"));
            }
            let item = self.ir.struct_(s, "", i, 0, Some((pos, 0))).map_err(hard)?;
            self.mark_value(item, "header", NO_INDEX, pos, enc.little)?;
            let next = if el.undefined() {
                self.dataset(item, el.value, container, false, enc, depth + 1)?
            } else {
                if el.end() > container {
                    return reject(format!("the item at {pos} runs past its container"));
                }
                self.dataset(item, el.value, el.end(), true, enc, depth + 1)?;
                el.end()
            };
            self.ir.len[item as usize] = next - pos;
            match prev {
                Some(p) if self.ir.fold(p, item) => {}
                _ => prev = Some(item),
            }
            pos = next;
            i += 1;
        }
    }

    /// A fragment sequence's items into `s` (the first, the Basic Offset Table, as
    /// `offset_table`); the top-level Pixel Data's fragments are data (`form`).
    fn fragments(&mut self, s: u32, mut pos: u64, limit: u64, little: bool, data: Option<&str>) -> R<u64> {
        let e = order(little);
        let mut j: Option<u64> = None;
        loop {
            match fragment(self.b, pos, limit, little)? {
                Frag::End(end) => {
                    self.mark_value(s, "end", NO_INDEX, pos, little)?;
                    return Ok(end);
                }
                Frag::Item(m) => {
                    let m = m as u64;
                    match j {
                        None => {
                            let t = self.ir.struct_(s, "offset_table", NO_INDEX, 0, Some((pos, 8 + m))).map_err(hard)?;
                            self.mark_value(t, "header", NO_INDEX, pos, little)?;
                            if m > 0 {
                                let ty = if m % 4 == 0 { format!("{e}u4[{}]", m / 4) } else { format!("bytes[{m}]") };
                                self.value(t, "offsets", NO_INDEX, &ty, pos + 8, m)?;
                            }
                            j = Some(0);
                        }
                        Some(k) => {
                            self.mark_value(s, "fragment_headers/", k, pos, little)?;
                            if m > 0 {
                                match data {
                                    Some(form) => {
                                        self.ir.data(s, "fragments/", k, 0, (pos + 8, m), form).map_err(hard)?;
                                    }
                                    None => {
                                        self.value(s, "fragments/", k, &format!("bytes[{m}]"), pos + 8, m)?;
                                    }
                                }
                            }
                            j = Some(k + 1);
                        }
                    }
                    pos += 8 + m;
                }
            }
        }
    }

    /// The top-level Pixel Data.
    fn pixel(&mut self, d: u32, img: &Image, lay: &Layout) -> R<u32> {
        let el = img.pixel;
        let s = self.ir.struct_(d, &tag_key(PIXEL_DATA), NO_INDEX, 0, Some((el.at, lay.end - el.at))).map_err(hard)?;
        self.value(s, "header", NO_INDEX, &format!("{{{}}}", header_fields(&el, img.enc)), el.at, el.value - el.at)?;
        let codec = match img.codec {
            "native" => json!({"name": "bytes", "endian": if img.enc.little { "little" } else { "big" }}),
            c => json!({"name": c}),
        };
        let geometry = json!({"rows": img.rows, "columns": img.columns, "samples": img.spp, "bits_allocated": img.bits_allocated,
            "pixel_representation": u8::from(img.signed), "photometric": img.photometric});
        self.form = crate::out::to_text(&json!({"geometry": geometry, "codec": codec, "recipe": [["src", 0, null]]}));
        let form = self.form.clone();
        if img.codec == "native" {
            let f = lay.frame_size;
            let first = self.ir.data(s, "frames/", 0, 0, (el.value, f), &form).map_err(hard)?;
            if img.n > 1 {
                let at = self.ir.runs.partition_point(|x| x.0 < first);
                self.ir.runs.insert(at, (first, img.n, f));
                if let Some(b) = self.ir.budget.as_mut() {
                    b.spent_elements += img.n - 1;
                    if b.spent_elements > b.elements {
                        return Err(hard(format!("budget: more than {} IR elements", b.elements)));
                    }
                }
            }
            let extra = el.length as u64 - img.n * f;
            if extra == 1 && lay.extra.is_none() {
                self.value(s, "padding", NO_INDEX, "bytes[1]", el.end() - 1, 1)?;
            }
            return Ok(s);
        }
        match &lay.eot {
            None => {
                self.fragments(s, el.value, self.b.size, true, Some(&form))?;
            }
            Some((data, headers, _)) => {
                let t = self.ir.struct_(s, "offset_table", NO_INDEX, 0, Some((el.value, 8))).map_err(hard)?;
                self.mark_value(t, "header", NO_INDEX, el.value, true)?;
                for (f, &(o, m)) in data.iter().enumerate() {
                    if *headers {
                        self.mark_value(s, "fragment_headers/", f as u64, o - 8, true)?;
                    }
                    self.ir.data(s, "fragments/", f as u64, 0, (o, m), &form).map_err(hard)?;
                }
                if let Some(p) = lay.delimiter {
                    self.mark_value(s, "end", NO_INDEX, p, true)?;
                }
            }
        }
        Ok(s)
    }
}

#[derive(Default)]
struct Names {
    present: HashSet<u32>,
    dups: u64,
    pads: u64,
}

crate::parser!(Dicom);

/// The DICOM profile's file run (spec/zarrify/dicom.md).
pub struct Profile;

impl crate::run::FileProfile for Profile {
    /// `DICM` after the 128-byte preamble (tested first: a dual-personality file's preamble may
    /// hold a TIFF header).
    fn detect(&self, head: &[u8]) -> bool {
        head.len() >= 132 && &head[128..132] == b"DICM"
    }
    fn parser(&self, size: u64) -> crate::run::AnyParser {
        crate::run::AnyParser::of::<Dicom>(size)
    }
    fn project(&self, ir: &crate::ir::Ir, facts: &serde_json::Value, url: &str, _copied: &crate::project::Copied) -> Result<crate::out::Out, String> {
        crate::project::dicom::project(ir, facts, url)
    }
}
