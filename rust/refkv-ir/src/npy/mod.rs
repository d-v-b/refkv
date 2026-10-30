//! A sans-IO parser of NumPy files into the IR (spec/zarrify/npy.md §8
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#8-numpy-profile>, and the
//! source model of §5.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#53-elements>):
//! a `.npy` file, one NPY array, or a `.npz` archive, a ZIP file of them.
//!
//! Rounds: the file's first 4096 bytes, which hold a `.npy` file's preamble and header (the
//! rest of a longer header in a second round); or, for a `.npz` archive, its end records, its
//! ZIP64 records, its central directory, each member's local header with a window after it,
//! which holds a stored `.npy` member's preamble and header, and the rest of a longer one; a
//! deflated member's window is the start of its stream, which is inflated as far as its header
//! (more of the stream in later rounds, when the header needs it). The schema
//! (`schema/npy.json`) holds every check, in the profile's order.

pub mod header;

use crate::expr::V;
use crate::fed::{Fed, Overlap, uint};
use crate::ir::*;
use crate::nd2::Step;
use crate::rules::{self, Rules, flag, int, num, text};
use crate::sans::{self, Asked};
use crate::zip::{self, DEFLATE_FORM};
use header::{Dtype, Header};
use serde_json::{Map, Value as J, json};
use std::collections::HashSet;

pub const MAGIC: &[u8; 6] = b"\x93NUMPY";
/// The first read, and the window read after each candidate member's local header.
const WINDOW: u64 = 4096;

/// Whether a file's first bytes are a `.npy` file's (spec/zarrify/npy.md §8.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#81-signature>).
fn is_npy(head: &[u8]) -> bool {
    head.len() >= 6 && &head[..6] == MAGIC
}

/// Whether they are a `.npz` archive's: a ZIP local file header whose file name, within them,
/// ends with `.npy`.
fn is_npz(head: &[u8]) -> bool {
    if head.len() < 30 || &head[..4] != zip::LOCAL {
        return false;
    }
    let n = uint(head, 26, 2, true) as usize;
    head.len() >= 30 + n && head[30..30 + n].ends_with(b".npy")
}

pub fn detect(head: &[u8]) -> bool {
    is_npy(head) || is_npz(head)
}

/// An NPY array's preamble, header and data, as far as they were read and checked: the reason
/// it is not presented, when one of its checks failed (a `.npz` member's; a `.npy` file's
/// failure rejects the input).
#[derive(Clone, Debug, Default)]
struct Array {
    /// where its preamble starts
    base: u64,
    preamble: Vec<u8>,
    version: (u8, u8),
    /// the header's offset and length, and its bytes, when it lies within the limit
    at: u64,
    n: u64,
    raw: Option<Vec<u8>>,
    header: Option<Header>,
    dtype: Option<Dtype>,
    /// the data's offset and length, when presented
    data: Option<(u64, u64)>,
    reason: Option<&'static str>,
}

/// One step of reading an NPY array from the bytes fed.
enum Read {
    /// the bytes it needs next
    Need(u64, u64),
    Done(Array),
}

/// The start of a raw deflate stream, from its first bytes `data`: at most `cap` bytes of its
/// output, and whether more of the stream could give more (the output stopped short of `cap`
/// where `data` ran out, not where the stream ended or broke).
fn inflate_prefix(data: &[u8], cap: u64) -> (Vec<u8>, bool) {
    use miniz_oxide::inflate::stream::{InflateState, inflate};
    use miniz_oxide::{DataFormat, MZFlush, MZStatus};
    let mut st = InflateState::new_boxed(DataFormat::Raw);
    let mut out = Vec::new();
    let mut input = data;
    let mut buf = vec![0u8; 1 << 16];
    while (out.len() as u64) < cap {
        let room = (cap - out.len() as u64).min(buf.len() as u64) as usize;
        let r = inflate(&mut st, input, &mut buf[..room], MZFlush::None);
        out.extend_from_slice(&buf[..r.bytes_written]);
        input = &input[r.bytes_consumed..];
        match r.status {
            Ok(MZStatus::Ok) if r.bytes_written > 0 || r.bytes_consumed > 0 => {}
            Ok(MZStatus::StreamEnd) | Err(miniz_oxide::MZError::Data) => return (out, false),
            _ => {
                let more = (out.len() as u64) < cap && input.is_empty();
                return (out, more);
            }
        }
    }
    (out, false)
}

/// Reads the NPY array a deflated member `e` holds from the start of its stream, inflating its
/// compressed bytes fed from `e.ds` (spec/zarrify/npy.md §8.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#83-the-archive>): the array,
/// its offsets those of the inflated stream, whose data must end it; or the compressed bytes it
/// needs next. A stream that ends or breaks before its header does, or whose header ends past
/// `max_inflated_header`, is the reason `header`.
fn read_deflated(rules: &Rules, fed: &Fed, e: &zip::Entry) -> Result<Read, String> {
    let have = fed.reach(e.ds).min(e.ds + e.cs) - e.ds;
    let compressed = fed.get(e.ds, have).unwrap_or(&[]);
    let mut want = 12;
    loop {
        let (bytes, more) = inflate_prefix(compressed, want.min(e.us));
        let got = bytes.len() as u64;
        let mut inflated = Fed::new(Overlap::MergeKeep);
        inflated.feed(0, bytes);
        match read(rules, &inflated, 0, e.us, false)? {
            Read::Need(o, len) if rules.check("inflated_header", rules::env([("end", num((o + len) as f64))])).is_err() => {
                return Ok(Read::Done(Array { reason: Some("header"), ..Array::default() }));
            }
            Read::Need(o, len) if got >= want.min(e.us) && o + len > want => want = o + len,
            Read::Need(..) if more && have < e.cs => return Ok(Read::Need(e.ds + have, (e.cs - have).min(have.max(WINDOW)))),
            Read::Need(..) => return Ok(Read::Done(Array { reason: Some("header"), ..Array::default() })),
            Read::Done(mut a) => {
                if let (None, Some((offset, total))) = (a.reason, a.data) {
                    let r = rules.check("inflated_size", rules::env([("offset", num(offset as f64)), ("total", num(total as f64)), ("size", num(e.us as f64))]));
                    if r.is_err() {
                        a.reason = Some("size");
                        a.data = None;
                    }
                }
                return Ok(Read::Done(a));
            }
        }
    }
}

/// Reads the NPY array at `base` of a file or member ending at `limit` from `fed`, checking
/// each role: a failed check is the array's reason (`strict`: the input's rejection).
fn read(rules: &Rules, fed: &Fed, base: u64, limit: u64, strict: bool) -> Result<Read, String> {
    let mut a = Array { base, ..Array::default() };
    let size = limit - base;
    let first = size.min(12);
    let Some(pre) = fed.get(base, first) else { return Ok(Read::Need(base, first)) };
    let fail = |a: &mut Array, why: &'static str, r: Result<(), String>| -> Result<bool, String> {
        match r {
            Ok(()) => Ok(false),
            Err(e) if strict => Err(e),
            Err(_) => {
                a.reason = Some(why);
                Ok(true)
            }
        }
    };
    let magic = pre.len() >= 6 && &pre[..6] == MAGIC;
    let (major, minor) = (pre.get(6).copied().unwrap_or(0), pre.get(7).copied().unwrap_or(0));
    let r = rules.check("preamble", rules::env([("size", num(size as f64)), ("magic", flag(magic)), ("major", num(major)), ("minor", num(minor))]));
    if fail(&mut a, "header", r)? {
        return Ok(Read::Done(a));
    }
    let p: u64 = if major == 1 { 10 } else { 12 };
    a.preamble = pre[..p as usize].to_vec();
    a.version = (major, minor);
    a.n = uint(pre, 8, if major == 1 { 2 } else { 4 }, true);
    a.at = base + p;
    let r = rules.check("header", rules::env([("start", num(a.at as f64)), ("length", num(a.n as f64)), ("end", num(limit as f64))]));
    if fail(&mut a, "header", r)? {
        return Ok(Read::Done(a));
    }
    let Some(raw) = fed.get(a.at, a.n) else { return Ok(Read::Need(a.at, a.n)) };
    a.raw = Some(raw.to_vec());
    // versions 1 and 2 are ISO 8859-1, every byte a character; version 3 is UTF-8
    let decoded: Option<String> = if major == 3 { std::str::from_utf8(raw).ok().map(str::to_string) } else { Some(raw.iter().map(|&b| b as char).collect()) };
    let r = rules.check("text", rules::env([("utf8", flag(decoded.is_some()))]));
    if fail(&mut a, "header", r)? {
        return Ok(Read::Done(a));
    }
    let parsed = header::parse(&decoded.unwrap_or_default(), rules.limit("max_rank") as usize);
    let problem = parsed.as_ref().err().map_or(V::Absent, |e| text(e));
    let r = rules.check("literal", rules::env([("problem", problem)]));
    if fail(&mut a, "header", r)? {
        return Ok(Read::Done(a));
    }
    let h = parsed.unwrap();
    let t = header::dtype(rules, &h.descr);
    a.header = Some(h.clone());
    let problem = t.as_ref().err().map_or(V::Absent, |e| text(e));
    let r = rules.check("data_type", rules::env([("shown", text(&h.descr.repr())), ("problem", problem)]));
    if fail(&mut a, "datatype", r)? {
        return Ok(Read::Done(a));
    }
    let t = t.unwrap();
    let total = h.shape.iter().fold(Some(t.item as u128), |acc, &s| acc.and_then(|x| x.checked_mul(s as u128)));
    let offset = a.at + a.n;
    let r = rules.check("data_size", rules::env([
        ("total", total.and_then(|b| i128::try_from(b).ok()).map_or(num(f64::INFINITY), int)),
        ("total_shown", text(&total.map_or("more than 2^128".to_string(), |b| b.to_string()))),
        ("offset", num(offset as f64)),
        ("end", num(limit as f64)),
    ]));
    if fail(&mut a, "size", r)? {
        return Ok(Read::Done(a));
    }
    a.data = Some((offset, total.unwrap() as u64));
    a.dtype = Some(t);
    Ok(Read::Done(a))
}

impl Array {
    /// Its NPY header as source metadata (spec/zarrify/npy.md §5.1
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#51-the-root>), once its
    /// header is read.
    fn header_json(&self) -> Option<J> {
        let h = self.header.as_ref()?;
        Some(json!({
            "version": [self.version.0, self.version.1],
            "descr": h.descr.json(),
            "fortran_order": h.fortran_order,
            "shape": h.shape,
            "offset": self.at + self.n,
        }))
    }

    /// Its elements under `parent`: the preamble, the header and the data (presented, and not
    /// empty).
    fn emit(&self, ir: &mut Ir, parent: u32) -> Result<Option<u32>, String> {
        let p = self.preamble.len() as u64;
        let ty = if p == 10 { "{magic:bytes[6],major:u1,minor:u1,header_length:<u2}" } else { "{magic:bytes[6],major:u1,minor:u1,header_length:<u4}" };
        ir.value(parent, "preamble", NO_INDEX, ty, 0, (self.base, p), Some(&self.preamble))?;
        if self.n > 0 {
            ir.value(parent, "header", NO_INDEX, &format!("ascii[{}]", self.n), 0, (self.at, self.n), self.raw.as_deref())?;
        }
        let (Some((o, b)), Some(t), Some(h)) = (self.data, &self.dtype, &self.header) else { return Ok(None) };
        if b == 0 {
            return Ok(None);
        }
        let endian = t.little.map(|l| if l { "little" } else { "big" });
        let form = json!({
            "geometry": {"data_type": t.zarr, "shape": h.shape, "order": if h.fortran_order { "F" } else { "C" }},
            "codec": {"endian": endian},
            "recipe": [["src", 0, null]],
        });
        Ok(Some(ir.data(parent, "data", NO_INDEX, 0, (o, b), &form.to_string())?))
    }

    /// The facts the projection reads of it, presented at `path`, its data element `data`; a
    /// deflated member's `deflate`, its stream's range, CRC-32 and size and the bytes before its
    /// data.
    fn facts(&self, path: &str, data: Option<u32>, deflated: Option<&zip::Entry>) -> J {
        let (h, t) = (self.header.as_ref().unwrap(), self.dtype.as_ref().unwrap());
        let (offset, length) = self.data.unwrap();
        let mut f = json!({
            "path": path, "data": data, "offset": offset, "length": length,
            "data_type": t.zarr, "item": t.item, "little": t.little, "trailing": t.trailing, "fill": t.fill,
            "fortran_order": h.fortran_order, "shape": h.shape,
        });
        if let Some(e) = deflated {
            f["deflate"] = json!({"start": e.ds, "length": e.cs, "crc": e.crc, "size": e.us, "skip": offset});
        }
        f
    }
}

/// A member of a `.npz` archive: its entry, its array's path when presented, else its reason
/// (spec/zarrify/npy.md §4.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#42-an-npz-archive>).
struct Member {
    path: Option<String>,
    reason: Option<&'static str>,
    array: Option<Array>,
    /// still to be read
    pending: bool,
}

/// The path of a member named `name`, or why it has none.
fn member_path(name: &str) -> Result<String, &'static str> {
    let k = name.strip_suffix(".npy").ok_or("not_npy")?;
    if k.is_empty() || k.contains('/') || k == "." || k == ".." || k.starts_with("__") || k == "zarr.json" {
        return Err("name");
    }
    if k == crate::out::SOURCE_NODE {
        return Err("reserved");
    }
    Ok(k.to_string())
}

#[derive(Clone, Copy, PartialEq, Debug)]
enum Phase {
    Start,
    Head,
    NpyRest,
    ZipTail,
    ZipLocator,
    ZipRecord,
    ZipCd,
    ZipLocal,
    ZipNpy,
    Done,
}

pub struct Npy {
    size: u64,
    phase: Phase,
    fed: Fed,
    ir: Ir,
    rules: &'static Rules,
    asked: Asked,
    facts: J,
    array: Option<Array>,
    end: zip::End,
    entries: Vec<zip::Entry>,
    members: Vec<Member>,
    tail: Vec<u8>,
    cd: Vec<u8>,
    record: Option<Vec<u8>>,
    locator: Option<Vec<u8>>,
}

impl Npy {
    pub fn new(size: u64) -> Self {
        Npy {
            size, phase: Phase::Start, fed: Fed::new(Overlap::MergeKeep), ir: Ir::new(size), rules: rules::of("npy"),
            asked: Asked::default(), facts: J::Null, array: None, end: zip::End::default(), entries: Vec::new(),
            members: Vec::new(), tail: Vec::new(), cd: Vec::new(), record: None, locator: None,
        }
    }

    pub fn feed(&mut self, offset: u64, data: Vec<u8>) {
        self.fed.feed(offset, data);
    }

    fn got(&self, o: u64, n: u64) -> Result<Vec<u8>, String> {
        self.fed.get(o, n).map(<[u8]>::to_vec).ok_or_else(|| "internal: a read was not fed".into())
    }

    pub fn step(&mut self) -> Result<Step, String> {
        let n = self.size;
        let reads = match self.phase {
            Phase::Start => {
                self.phase = Phase::Head;
                vec![(0, n.min(WINDOW))]
            }
            Phase::Head => {
                let head = self.got(0, n.min(WINDOW))?;
                if is_npy(&head) {
                    self.phase = Phase::NpyRest;
                    return self.step();
                }
                if !is_npz(&head) {
                    return Err("not a NumPy file: neither an NPY preamble nor a ZIP archive of .npy members".into());
                }
                if n == 0 {
                    return Err("not a zip file: no end of central directory record".into());
                }
                self.phase = Phase::ZipTail;
                let t = n.min(zip::MAX_TAIL);
                vec![(n - t, t)]
            }
            Phase::NpyRest => match read(self.rules, &self.fed, 0, n, true)? {
                Read::Need(o, len) => vec![(o, len)],
                Read::Done(a) => {
                    self.array = Some(a);
                    return self.done();
                }
            },
            Phase::ZipTail => {
                let t = n.min(zip::MAX_TAIL);
                self.tail = self.got(n - t, t)?;
                self.end = zip::eocd(self.rules, &self.tail, n)?;
                match self.end.zip64 {
                    Some((loc, _)) => {
                        self.phase = Phase::ZipLocator;
                        vec![(loc, 20)]
                    }
                    None => self.ask_cd()?,
                }
            }
            Phase::ZipLocator => {
                let loc = self.end.eocd - 20;
                let b = self.got(loc, 20)?;
                let rec = zip::zip64_locator(&b, &self.end)?;
                self.locator = Some(b);
                self.end.zip64 = Some((loc, rec));
                self.phase = Phase::ZipRecord;
                vec![(rec, 56)]
            }
            Phase::ZipRecord => {
                let rec = self.end.zip64.map_or(0, |z| z.1);
                let b = self.got(rec, 56)?;
                zip::zip64_record(self.rules, &b, &mut self.end, rec)?;
                self.record = Some(b);
                self.ask_cd()?
            }
            Phase::ZipCd => {
                let (o, len) = (self.end.cd_offset, self.end.cd_size);
                self.cd = if len == 0 { Vec::new() } else { self.got(o, len)? };
                self.entries = zip::central_directory(self.rules, &self.cd, &self.end)?;
                self.members()?
            }
            Phase::ZipLocal => {
                let cd_offset = self.end.cd_offset;
                for e in self.entries.iter_mut() {
                    let head = self.fed.get(e.lho, 30).ok_or("internal: no local header")?.to_vec();
                    zip::local_header(&head, e, cd_offset)?;
                }
                let all: Vec<usize> = (0..self.entries.len()).collect();
                zip::disjoint(&self.entries, &all)?;
                self.phase = Phase::ZipNpy;
                return self.step();
            }
            Phase::ZipNpy => {
                // the local headers' names, which the view shows
                let mut need: Vec<(u64, u64)> = self.entries.iter().map(|e| (e.lho + 30, e.local_nn))
                    .filter(|&(o, len)| len > 0 && !self.fed.has(o, len)).collect();
                for (m, e) in self.members.iter_mut().zip(&self.entries) {
                    if !m.pending {
                        continue;
                    }
                    let r = if e.method == 8 { read_deflated(self.rules, &self.fed, e)? } else { read(self.rules, &self.fed, e.ds, e.ds + e.cs, false)? };
                    match r {
                        Read::Need(o, len) => need.push((o, len)),
                        Read::Done(a) => {
                            m.pending = false;
                            m.reason = a.reason;
                            if a.reason.is_some() {
                                m.path = None;
                            }
                            m.array = Some(a);
                        }
                    }
                }
                if need.is_empty() {
                    return self.done();
                }
                need
            }
            Phase::Done => return Ok(Step::Done),
        };
        Ok(self.asked.ask(reads))
    }

    fn ask_cd(&mut self) -> Result<Vec<(u64, u64)>, String> {
        let end = zip::cd_end(&self.end);
        if self.end.cd_offset.checked_add(self.end.cd_size) != Some(end) {
            return Err("a zip file's central directory does not end where its end records start".into());
        }
        self.phase = Phase::ZipCd;
        Ok(if self.end.cd_size > 0 { vec![(self.end.cd_offset, self.end.cd_size)] } else { Vec::new() })
    }

    /// The members' checks and paths (spec/zarrify/npy.md §8.3
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#83-the-archive>), and the
    /// reads of their local headers, with a window for a member whose header is to be read.
    fn members(&mut self) -> Result<Vec<(u64, u64)>, String> {
        let cd_offset = self.end.cd_offset;
        let mut names = HashSet::new();
        let mut reads = Vec::new();
        for e in &self.entries {
            let short: String = e.name.chars().take(200).collect();
            self.rules.check("member", rules::env([("name", text(&short)), ("unique", flag(names.insert(e.name.clone()))),
                ("method", num(e.method as f64)), ("same_size", flag(e.cs == e.us))]))?;
            let (path, mut reason) = match member_path(&e.name) {
                Ok(p) => (Some(p), None),
                Err(r) => (None, Some(r)),
            };
            if reason.is_none() && e.method != 0 && e.method != 8 {
                reason = Some("method");
            }
            if e.lho.checked_add(30).is_none_or(|x| x > cd_offset) {
                return Err("a zip entry's local header is beyond the entries".into());
            }
            let pending = reason.is_none();
            let window = 30 + e.nn + e.ne + if pending { WINDOW } else { 0 };
            reads.push((e.lho, window.min(cd_offset - e.lho)));
            self.members.push(Member { path: if pending { path } else { None }, reason, array: None, pending });
        }
        self.phase = Phase::ZipLocal;
        Ok(reads)
    }

    /// The archive's elements (spec/zarrify/npy.md §5.3
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#53-elements>) and the facts.
    fn done(&mut self) -> Result<Step, String> {
        self.phase = Phase::Done;
        let mut arrays = Vec::new();
        let own = if let Some(a) = self.array.take() {
            let data = a.emit(&mut self.ir, 0)?;
            arrays.push(a.facts("array", data, None));
            json!({"kind": "npy", "array": a.header_json()})
        } else {
            let raw = zip::Records { tail: &self.tail, cd: &self.cd, record: self.record.as_deref(), locator: self.locator.as_deref() };
            zip::emit_records(&mut self.ir, self.size, &self.end, &self.entries, &raw)?;
            let mut members = Vec::new();
            let mut reasons = Vec::new();
            for (i, (m, e)) in self.members.iter().zip(&self.entries).enumerate() {
                let name = self.fed.get(e.lho + 30, e.local_nn).map(<[u8]>::to_vec);
                let s = zip::emit_local(&mut self.ir, i as u64, e, name.as_deref())?;
                let read = m.array.as_ref().filter(|a| a.raw.is_some() && e.method == 0);
                let mut data = None;
                if e.cs > 0 {
                    match read {
                        Some(a) => {
                            let d = self.ir.struct_(s, "data", NO_INDEX, 0, Some((e.ds, e.cs)))?;
                            data = a.emit(&mut self.ir, d)?;
                        }
                        // its preamble and header, which the stream holds, are in the source metadata
                        None if e.method == 8 => {
                            self.ir.derived(s, "data", 0, (e.ds, e.cs), DEFLATE_FORM)?;
                        }
                        None => {
                            self.ir.value(s, "data", NO_INDEX, &format!("bytes[{}]", e.cs), 0, (e.ds, e.cs), None)?;
                        }
                    }
                }
                let mut j = Map::new();
                j.insert("name".into(), json!(e.name));
                j.insert("method".into(), json!(e.method));
                j.insert("size".into(), json!(e.us));
                j.insert("compressed_size".into(), json!(e.cs));
                match (&m.path, m.reason) {
                    (Some(p), None) => {
                        j.insert("path".into(), json!(p));
                        arrays.push(m.array.as_ref().unwrap().facts(p, data, (e.method == 8).then_some(e)));
                    }
                    (_, r) => {
                        let r = r.unwrap_or("header");
                        j.insert("reason".into(), json!(r));
                        let short: String = e.name.chars().take(200).collect();
                        reasons.push(format!("{short} ({r})"));
                    }
                }
                if let Some(h) = m.array.as_ref().and_then(Array::header_json) {
                    j.insert("header".into(), h);
                }
                members.push(J::Object(j));
            }
            self.rules.check("arrays", rules::env([("count", num(arrays.len() as f64)), ("reasons", text(&reasons.join(", ")))]))?;
            json!({"kind": "npz", "members": members})
        };
        let mut facts = json!({"own": own, "arrays": arrays, "copy_threshold": crate::out::copy_threshold()});
        self.asked.put(&mut facts);
        self.facts = facts;
        Ok(Step::Done)
    }

    pub fn finish(self) -> Result<(Ir, J), String> {
        sans::finish(self.phase == Phase::Done, self.ir, self.facts)
    }
}

crate::parser!(Npy);

/// The NumPy profile's file run (spec/zarrify/npy.md).
pub struct Profile;

impl crate::run::FileProfile for Profile {
    /// `\x93NUMPY` at byte 0, or a ZIP local header naming a `.npy` member (`detect`).
    fn detect(&self, head: &[u8]) -> bool {
        detect(head)
    }
    fn parser(&self, size: u64) -> crate::run::AnyParser {
        crate::run::AnyParser::of::<Npy>(size)
    }
    fn project(&self, ir: &crate::ir::Ir, facts: &serde_json::Value, url: &str, copied: &crate::project::Copied) -> Result<crate::out::Out, String> {
        crate::project::npy::project(ir, facts, url, copied)
    }
    fn reads(&self, ir: &crate::ir::Ir, facts: &serde_json::Value) -> Result<Vec<(u64, u64)>, String> {
        crate::project::npy::reads(ir, facts)
    }
}
