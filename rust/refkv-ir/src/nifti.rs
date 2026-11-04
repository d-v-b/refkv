//! A sans-IO parser of NIfTI-1 and NIfTI-2 single files into the IR (spec/zarrify/nifti.md §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#7-nifti-profile>, and the
//! source model of §5.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#53-elements>).
//!
//! Rounds: (1) the first 552 bytes (the header and its extender); (2) the extension
//! chain, a window of up to 1 MiB at a time from the extension it stopped at. The
//! schema (`schema/nifti.json`) holds every check, in the profile's order. The IR is
//! the header (one record value), the extender, each extension (one record value:
//! its `esize`, `ecode` and data, text for the text codes), the voxel data (one
//! `data` element) and gaps for the rest (padding, bytes the chain does not hold,
//! bytes after the voxels).
//!
//! A file gzipped whole (§7.4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#74-gzipped-files>) takes
//! other rounds: the start of the stream, inflated as far as the header (and as much of the
//! chain as `SCAN` holds), and its trailer. Its IR is one `data` element, the whole file; what the
//! stream's start holds is in the facts, as the file's bytes do not show it.

use crate::fed::{Fed, Word};
use crate::gz;
use crate::ir::*;
use crate::nd2::Step;
use crate::record::{Field, Header, Num};
use crate::rules::{self, Rules, flag, num, text};
use crate::sans;
use serde_json::{Value as J, json};

/// For tests: the payload past which an edge chunk is copied (None: MAX_PAYLOAD), of
/// every profile that cuts contiguous values (`out::set_copy_threshold`). With NIfTI's
/// chunk limit no file pads an edge chunk past MAX_PAYLOAD in practice, so the copy
/// (spec/zarrify/nifti.md §7.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#72-chunks>) is reached
/// this way.
pub fn set_copy_threshold(n: Option<usize>) {
    crate::out::set_copy_threshold(n);
}

/// The bytes a run reads first, of the file or of a gzipped file's stream.
const HEAD: u64 = 552;
/// The bytes of the chain read at a time.
const SCAN: u64 = 1 << 20;
/// The largest value the view shows, which the parser keeps (conventions §8.7
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#87-the-view>).
const SHOWN: u64 = 1024;
/// The ecodes of text extensions: AFNI, XCEDE and CIFTI XML, comments, MRS JSON.
pub const TEXT_CODES: [i32; 5] = [4, 6, 8, 32, 44];

/// The header's fields of each version (`i2`, `i4`, `i8`, `u1`, `f4`, `f8`, or `c` for
/// text), in the standard's order (spec/zarrify/nifti.md §2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#2-the-header>).
pub const NIFTI1: [Field; 43] = [
    ("sizeof_hdr", 0, "i4", 1), ("data_type", 4, "c", 10), ("db_name", 14, "c", 18), ("extents", 32, "i4", 1),
    ("session_error", 36, "i2", 1), ("regular", 38, "c", 1), ("dim_info", 39, "u1", 1), ("dim", 40, "i2", 8),
    ("intent_p1", 56, "f4", 1), ("intent_p2", 60, "f4", 1), ("intent_p3", 64, "f4", 1), ("intent_code", 68, "i2", 1),
    ("datatype", 70, "i2", 1), ("bitpix", 72, "i2", 1), ("slice_start", 74, "i2", 1), ("pixdim", 76, "f4", 8),
    ("vox_offset", 108, "f4", 1), ("scl_slope", 112, "f4", 1), ("scl_inter", 116, "f4", 1), ("slice_end", 120, "i2", 1),
    ("slice_code", 122, "u1", 1), ("xyzt_units", 123, "u1", 1), ("cal_max", 124, "f4", 1), ("cal_min", 128, "f4", 1),
    ("slice_duration", 132, "f4", 1), ("toffset", 136, "f4", 1), ("glmax", 140, "i4", 1), ("glmin", 144, "i4", 1),
    ("descrip", 148, "c", 80), ("aux_file", 228, "c", 24), ("qform_code", 252, "i2", 1), ("sform_code", 254, "i2", 1),
    ("quatern_b", 256, "f4", 1), ("quatern_c", 260, "f4", 1), ("quatern_d", 264, "f4", 1), ("qoffset_x", 268, "f4", 1),
    ("qoffset_y", 272, "f4", 1), ("qoffset_z", 276, "f4", 1), ("srow_x", 280, "f4", 4), ("srow_y", 296, "f4", 4),
    ("srow_z", 312, "f4", 4), ("intent_name", 328, "c", 16), ("magic", 344, "c", 4),
];

pub const NIFTI2: [Field; 37] = [
    ("sizeof_hdr", 0, "i4", 1), ("magic", 4, "c", 8), ("datatype", 12, "i2", 1), ("bitpix", 14, "i2", 1),
    ("dim", 16, "i8", 8), ("intent_p1", 80, "f8", 1), ("intent_p2", 88, "f8", 1), ("intent_p3", 96, "f8", 1),
    ("pixdim", 104, "f8", 8), ("vox_offset", 168, "i8", 1), ("scl_slope", 176, "f8", 1), ("scl_inter", 184, "f8", 1),
    ("cal_max", 192, "f8", 1), ("cal_min", 200, "f8", 1), ("slice_duration", 208, "f8", 1), ("toffset", 216, "f8", 1),
    ("slice_start", 224, "i8", 1), ("slice_end", 232, "i8", 1), ("descrip", 240, "c", 80), ("aux_file", 320, "c", 24),
    ("qform_code", 344, "i4", 1), ("sform_code", 348, "i4", 1), ("quatern_b", 352, "f8", 1), ("quatern_c", 360, "f8", 1),
    ("quatern_d", 368, "f8", 1), ("qoffset_x", 376, "f8", 1), ("qoffset_y", 384, "f8", 1), ("qoffset_z", 392, "f8", 1),
    ("srow_x", 400, "f8", 4), ("srow_y", 432, "f8", 4), ("srow_z", 464, "f8", 4), ("slice_code", 496, "i4", 1),
    ("xyzt_units", 500, "i4", 1), ("intent_code", 504, "i4", 1), ("intent_name", 508, "c", 16), ("dim_info", 524, "u1", 1),
    ("unused_str", 525, "c", 15),
];

/// The NIfTI version a file's first bytes select (spec/zarrify.md §1.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>), if one.
pub fn detect(head: &[u8]) -> Option<u8> {
    if head.len() < 12 {
        return None;
    }
    let sizes = [i32::le_at(head, 0), i32::be_at(head, 0)];
    if sizes.contains(&348) && head.len() >= 348 && &head[344..348] == b"n+1\0" {
        return Some(1);
    }
    if sizes.contains(&540) && &head[4..12] == b"n+2\0\r\n\x1a\n" {
        return Some(2);
    }
    None
}

/// The NIfTI version a header-and-image pair's first bytes select (magic `ni1` or `ni2`, spec/zarrify/nifti.md §7.5
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#75-header-and-image-pairs>), if one.
pub fn detect_pair(head: &[u8]) -> Option<u8> {
    if head.len() < 12 {
        return None;
    }
    let sizes = [i32::le_at(head, 0), i32::be_at(head, 0)];
    if sizes.contains(&348) && head.len() >= 348 && &head[344..348] == b"ni1\0" {
        return Some(1);
    }
    if sizes.contains(&540) && &head[4..12] == b"ni2\0\r\n\x1a\n" {
        return Some(2);
    }
    None
}

/// The header at the start of `raw` (at least its length), of the version `detect` gives, in
/// the byte order in which its first field is the header's length.
pub fn header(raw: &[u8], version: u8) -> Header<'_> {
    let length = if version == 1 { 348 } else { 540 };
    let little = i32::le_at(raw, 0) == length;
    Header { raw: &raw[..length as usize], little, fields: if version == 1 { &NIFTI1 } else { &NIFTI2 } }
}

/// What the checks of a header give (spec/zarrify/nifti.md §7.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#71-header>): its length, its fields, the voxels' data type and samples, the voxel offset, and the voxel data's size.
pub(crate) struct Checked<'a> {
    pub length: u64,
    pub h: Header<'a>,
    pub dtype: String,
    pub samples: u64,
    pub vox: u64,
    pub total: u128,
}

/// The checks of the header of the given `version` at the start of `got`, of a file of `size` bytes, up to
/// the voxel offset's, which is at least `first`; `voxel_data`'s is the caller's, which knows the voxels' file.
pub(crate) fn check_header<'a>(rules: &Rules, got: &'a [u8], version: u8, size: u64, first: impl Fn(u64) -> u64) -> Res<Checked<'a>> {
    let length: u64 = if version == 1 { 348 } else { 540 };
    rules.check(
        "file",
        rules::env([("version", num(version)), ("length", num(length as f64)), ("size", num(size as f64))]),
    )?;
    let h = header(got, version);
    let dim = h.values("dim");
    let n = dim[0].i();
    rules.check("dimensions", rules::env([("n", num(n as f64))]))?;
    let mut sizes = [1i64; 8];
    for i in 1..=n as usize {
        let d = dim[i].i();
        rules.check(
            "dimension",
            rules::env([("i", num(i as f64)), ("size", num(d as f64)), ("shown", text(&d.to_string()))]),
        )?;
        sizes[i] = d;
    }
    rules.check("unused_dimensions", rules::env([("d6", num(sizes[6] as f64)), ("d7", num(sizes[7] as f64))]))?;
    let code = h.get("datatype").i();
    let bitpix = h.get("bitpix").i();
    rules.check("data_type", rules::env([("code", num(code as f64)), ("bitpix", num(bitpix as f64)), ("n", num(n as f64))]))?;
    let spec = &rules.table("datatypes")[code.to_string()];
    let (bits, samples) = (spec["bits"].as_u64().unwrap_or(8), spec["samples"].as_u64().unwrap_or(1));
    let (vox, integral, shown) = match h.get("vox_offset") {
        Num::Float(v) if v.is_finite() && v.fract() == 0.0 => (v, true, format!("{}", v as i128)),
        Num::Float(v) => (v, false, py_float(v)),
        Num::Int(v) => (v as f64, true, v.to_string()),
    };
    rules.check(
        "voxel_offset",
        rules::env([("vox", num(vox)), ("integral", flag(integral)), ("shown", text(&shown)), ("first", num(first(length) as f64))]),
    )?;
    let total = sizes[1..6].iter().fold(bits as u128 / 8, |a, &s| a.saturating_mul(s as u128));
    let dtype = spec["dtype"].as_str().unwrap_or("uint8").to_string();
    Ok(Checked { length, h, dtype, samples, vox: vox as u64, total })
}

/// The extensions in `block`, which starts at offset `q` of the object `parent` (spec/zarrify/nifti.md §5
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#5-source-metadata>), the next numbered `k`:
/// the bytes of `block` it took them from, and whether the chain goes on past them. It stops at an
/// `esize` below 8 or past `end`.
pub(crate) fn chain_values(ir: &mut Ir, parent: u32, block: &[u8], q: u64, end: u64, little: bool, k: &mut u64) -> Res<(u64, bool)> {
    let e = if little { "<" } else { ">" };
    let i32_at = |at: usize| i32::at(block, at, little);
    let mut p = 0usize;
    while p + 8 <= block.len() {
        let (esize, ecode) = (i32_at(p), i32_at(p + 4));
        let at = q + p as u64;
        if esize < 8 || at + esize as u64 > end {
            return Ok((p as u64, false));
        }
        let esize = esize as u64;
        let keep = esize <= SHOWN;
        if keep && p + esize as usize > block.len() {
            break; // a shown extension past the window: the next window starts at it
        }
        let ty = match esize - 8 {
            0 => format!("{{esize:{e}i4,ecode:{e}i4}}"),
            d if TEXT_CODES.contains(&ecode) => format!("{{esize:{e}i4,ecode:{e}i4,data:cstr[{d}]}}"),
            d => format!("{{esize:{e}i4,ecode:{e}i4,data:bytes[{d}]}}"),
        };
        let raw = if keep { Some(&block[p..p + esize as usize]) } else { None };
        ir.value(parent, "extensions/", *k, &ty, 0, (at, esize), raw)?;
        *k += 1;
        p += esize as usize;
    }
    Ok((p as u64, true))
}

#[derive(Clone, Copy, PartialEq, Debug)]
enum Phase {
    Start,
    Head,
    Gzip,
    Chain,
    Done,
}

pub struct Nifti {
    size: u64,
    phase: Phase,
    fed: Fed,
    ir: Ir,
    rules: &'static Rules,
    little: bool,
    vox: u64,
    /// where the chain goes on, whether it does, and the next extension's index
    q: u64,
    chaining: bool,
    k: u64,
    /// a gzipped file's reads, and how much of its stream a read asks for
    gzip: Option<gz::Reader>,
    want: u64,
    facts: J,
}

impl Nifti {
    pub fn new(size: u64) -> Self {
        Nifti {
            size,
            phase: Phase::Start,
            fed: Fed::default(),
            ir: Ir::new(size),
            rules: rules::of("nifti"),
            little: true,
            vox: 0,
            q: 0,
            chaining: false,
            k: 0,
            gzip: None,
            want: HEAD,
            facts: J::Null,
        }
    }

    pub fn feed(&mut self, offset: u64, data: Vec<u8>) {
        self.fed.feed(offset, data);
    }

    pub fn step(&mut self) -> Result<Step, String> {
        loop {
            match self.phase {
                Phase::Start => {
                    self.phase = Phase::Head;
                    return Ok(Step::Read(vec![(0, self.size.min(HEAD))]));
                }
                Phase::Head => {
                    let got = self.fed.get(0, self.size.min(HEAD)).ok_or("internal: no header")?.to_vec();
                    if detect_pair(&got).is_some() {
                        return Ok(Step::Beside);
                    }
                    if gz::is_member(&got) {
                        self.gzip = Some(gz::Reader::new(self.size));
                        self.phase = Phase::Gzip;
                    } else {
                        self.head(&got, None)?;
                        self.phase = Phase::Chain;
                    }
                }
                Phase::Gzip => {
                    let reader = self.gzip.as_mut().ok_or("internal: no gzip reader")?;
                    let inflated = match reader.inflate(&self.fed, self.want) {
                        Ok(inflated) => inflated,
                        Err(reads) => return Ok(reads),
                    };
                    let want = stream_wanted(&inflated.bytes);
                    if want > self.want {
                        self.want = want;
                        continue;
                    }
                    self.head(&inflated.bytes, Some(inflated.isize))?;
                    self.phase = Phase::Done;
                }
                Phase::Chain => {
                    if !self.chaining || self.q + 8 > self.vox {
                        self.phase = Phase::Done;
                        continue;
                    }
                    let n = SCAN.min(self.vox - self.q);
                    match self.fed.get(self.q, n).map(|b| b.to_vec()) {
                        Some(block) => {
                            self.fed.clear();
                            self.chain(&block)?;
                        }
                        None => return Ok(Step::Read(vec![(self.q, n)])),
                    }
                }
                Phase::Done => return Ok(Step::Done),
            }
        }
    }

    /// The header and its checks (spec/zarrify/nifti.md §7.1
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#71-header>), then the
    /// elements of the header, the extender and the voxel data. `got` is the file's first bytes,
    /// or those a gzipped file's stream holds, with its trailer's size (§7.4
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#74-gzipped-files>): a
    /// stream's size is checked against the trailer, so no file's size bounds the voxel data.
    fn head(&mut self, got: &[u8], isize: Option<u32>) -> Res<()> {
        let version = detect(got).ok_or("not a NIfTI-1 or NIfTI-2 single file")?;
        let size = if isize.is_some() { got.len() as u64 } else { self.size };
        let Checked { length, h, dtype, samples, vox, total, .. } = check_header(self.rules, got, version, size, |length| length + 4)?;
        self.rules.check(
            "voxel_data",
            rules::env([
                ("total", num(total as f64)),
                ("vox", num(vox as f64)),
                ("size", num(if isize.is_some() { self.rules.limit("max_safe") } else { self.size as f64 })),
                ("total_shown", text(&total.to_string())),
            ]),
        )?;
        if let Some(isize) = isize {
            let stream = vox as u128 + total;
            self.rules.check(
                "gzip_stream",
                rules::env([
                    ("inflated", num(got.len() as f64)),
                    ("first", num((length + 4) as f64)),
                    ("stream", num(gz::recorded(stream) as f64)),
                    ("stream_shown", text(&stream.to_string())),
                    ("isize", num(isize as f64)),
                ]),
            )?;
            let endian = if h.little { "little" } else { "big" };
            let form = json!({"geometry": {"data_type": dtype, "samples": samples},
                              "codec": {"encoding": "gzip", "endian": endian, "skip": vox},
                              "recipe": [["src", 0, null]]});
            let voxels = self.ir.data(0, "voxels", NO_INDEX, 0, (0, self.size), &form.to_string())?;
            let extender = got[length as usize..length as usize + 4].to_vec();
            let extensions = if extender[0] != 0 { extensions_in(got, length as usize + 4, vox, h.little) } else { Vec::new() };
            self.facts = json!({"version": version, "voxels": voxels, "extended": extender[0] != 0,
                                "gzip": {"skip": vox, "head": gz::head(&got[..length as usize]), "extender": extender, "extensions": extensions},
                                "copy_threshold": crate::out::copy_threshold()});
            return Ok(());
        }
        // the extender lies within the file, before the voxel data (vox >= L + 4)
        let ext = got[length as usize..length as usize + 4].to_vec();
        let header = self.ir.value(0, "header", NO_INDEX, &h.record_type(), 0, (0, length), Some(h.raw))?;
        self.ir.value(0, "extender", NO_INDEX, "u1[4]", 0, (length, 4), Some(&ext))?;
        let endian = if h.little { "little" } else { "big" };
        let form = json!({"geometry": {"data_type": dtype, "samples": samples}, "codec": {"endian": endian},
                          "recipe": [["src", 0, null]]});
        let voxels = self.ir.data(0, "voxels", NO_INDEX, 0, (vox, total as u64), &form.to_string())?;
        self.little = h.little;
        self.vox = vox;
        self.q = length + 4;
        self.chaining = ext[0] != 0;
        // the payload past which the projection copies an edge chunk, read once per run
        let copy_threshold = crate::out::copy_threshold();
        self.facts = json!({"version": version, "header": header, "voxels": voxels, "extended": ext[0] != 0,
                            "copy_threshold": copy_threshold});
        Ok(())
    }

    /// The extensions in a window of the chain from `q` (`chain_values`).
    fn chain(&mut self, block: &[u8]) -> Res<()> {
        let (took, goes_on) = chain_values(&mut self.ir, 0, block, self.q, self.vox, self.little, &mut self.k)?;
        self.chaining = goes_on;
        self.q += took;
        Ok(())
    }

    pub fn finish(self) -> Result<(Ir, J), String> {
        sans::finish(self.phase == Phase::Done, self.ir, self.facts)
    }
}

/// How many of a gzipped file's stream's bytes its first `got` ask for: its first bytes, and when
/// its extender says it has extensions, its chain as far as `SCAN` from where the chain starts, or
/// the voxel data if that is first.
fn stream_wanted(got: &[u8]) -> u64 {
    let Some(version) = detect(got) else { return HEAD };
    let length = if version == 1 { 348 } else { 540 };
    if got.len() < length + 4 || got[length] == 0 {
        return HEAD;
    }
    let vox = match header(got, version).get("vox_offset") {
        Num::Float(v) if v.is_finite() && v > 0.0 => v as u64,
        Num::Int(v) if v > 0 => v as u64,
        _ => return HEAD,
    };
    (length as u64 + 4 + SCAN).min(vox).max(HEAD)
}

/// The extensions of a gzipped file's chain that `got` holds, from `start`, for the root's source
/// metadata: each one's `esize` and `ecode`, and its text when a text code's of at most `SHOWN`
/// bytes. The chain stops as `Nifti::chain` says, and where `got` does.
fn extensions_in(got: &[u8], start: usize, vox: u64, little: bool) -> Vec<J> {
    let mut found = Vec::new();
    let mut p = start;
    while p + 8 <= got.len() {
        let (esize, ecode) = (i32::at(got, p, little), i32::at(got, p + 4, little));
        if esize < 8 || p as u64 + esize as u64 > vox {
            break;
        }
        let end = p + esize as usize;
        let mut extension = json!({"esize": esize, "ecode": ecode});
        if TEXT_CODES.contains(&ecode) && esize as u64 <= SHOWN && end <= got.len() {
            extension["data"] = crate::project::json_text(&got[p + 8..end]);
        }
        found.push(extension);
        p = end;
    }
    found
}

/// A binary64 as Python's `str` writes it, for messages.
fn py_float(v: f64) -> String {
    if v.is_nan() {
        "nan".into()
    } else if v.is_infinite() {
        if v > 0.0 { "inf".into() } else { "-inf".into() }
    } else {
        format!("{v}")
    }
}

crate::parser!(Nifti);

/// The NIfTI profile's file run (spec/zarrify/nifti.md).
pub struct Profile;

impl crate::run::FileProfile for Profile {
    /// A NIfTI-1 or NIfTI-2 single-file header (`detect`), or a pair's (`detect_pair`).
    fn detect(&self, head: &[u8]) -> bool {
        detect(head).is_some() || detect_pair(head).is_some()
    }
    /// A NIfTI file gzipped whole (§7.4
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#74-gzipped-files>).
    fn detect_gzipped(&self, inflated: &[u8]) -> bool {
        detect(inflated).is_some()
    }
    fn parser(&self, size: u64) -> crate::run::AnyParser {
        crate::run::AnyParser::of::<Nifti>(size)
    }
    fn project(&self, ir: &crate::ir::Ir, facts: &serde_json::Value, url: &str, copied: &crate::project::Copied) -> Result<crate::out::Out, String> {
        crate::project::nifti::project(ir, facts, url, copied)
    }
    fn reads(&self, ir: &crate::ir::Ir, facts: &serde_json::Value) -> Result<Vec<(u64, u64)>, String> {
        crate::project::nifti::reads(ir, facts)
    }
}
