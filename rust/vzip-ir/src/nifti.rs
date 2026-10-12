//! A sans-IO parser of NIfTI-1 and NIfTI-2 single files into the IR
//! ([spec/virtualize/nifti.md](https://github.com/d-v-b/vzip/blob/main/spec/virtualize/nifti.md),
//! the profile's [§7](https://github.com/d-v-b/vzip/blob/main/spec/virtualize/nifti.md#7-nifti-profile)).
//!
//! Rounds: (1) the first 552 bytes (the header and its extender); (2) the extension
//! chain, a window of up to 1 MiB at a time from the extension it stopped at. The
//! schema (`schema/nifti.json`) holds every check, in the profile's order. The IR is
//! the header (one record value), the extender, each extension (one record value:
//! its `esize`, `ecode` and data, text for the text codes), the voxel data (one
//! `data` element) and gaps for the rest (padding, bytes the chain does not hold,
//! bytes after the voxels).

use crate::check;
use crate::fed::Fed;
use crate::ir::*;
use crate::nd2::Step;
use crate::rules::{self, Rules, Vars, flag, num, text};
use serde_json::{Value as J, json};

/// The bytes of the chain read at a time.
const SCAN: u64 = 1 << 20;
/// The largest value the view shows, which the parser keeps (conventions §8.7
/// <https://github.com/d-v-b/vzip/blob/main/spec/conventions.md#87-the-view>).
const SHOWN: u64 = 1024;
/// The ecodes of text extensions: AFNI, XCEDE and CIFTI XML, comments, MRS JSON.
pub const TEXT_CODES: [i32; 5] = [4, 6, 8, 32, 44];

/// A header field: its name, offset, kind (`i2`, `i4`, `i8`, `u1`, `f4`, `f8`, or `c`
/// for text) and count (values, or a text's bytes), in the standard's order
/// (spec/virtualize/nifti.md §2
/// <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/nifti.md#2-the-header>).
pub type Field = (&'static str, usize, &'static str, usize);

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

/// The NIfTI version a file's first bytes select (spec/virtualize.md §1.2
/// <https://github.com/d-v-b/vzip/blob/main/spec/virtualize.md#12-input>), if one.
pub fn detect(head: &[u8]) -> Option<u8> {
    if head.len() < 12 {
        return None;
    }
    let b: [u8; 4] = head[..4].try_into().unwrap();
    let sizes = [i32::from_le_bytes(b), i32::from_be_bytes(b)];
    if sizes.contains(&348) && head.len() >= 348 && &head[344..348] == b"n+1\0" {
        return Some(1);
    }
    if sizes.contains(&540) && &head[4..12] == b"n+2\0\r\n\x1a\n" {
        return Some(2);
    }
    None
}

/// A header's bytes, version and byte order, read field by field.
pub struct Header<'a> {
    pub raw: &'a [u8],
    pub version: u8,
    pub little: bool,
}

/// A field's value: integers exactly, floats as binary64 (an f32 converted exactly).
#[derive(Clone, Copy, Debug)]
pub enum Num {
    Int(i64),
    Float(f64),
}

impl Num {
    pub fn f(self) -> f64 {
        match self {
            Num::Int(v) => v as f64,
            Num::Float(v) => v,
        }
    }
    pub fn i(self) -> i64 {
        match self {
            Num::Int(v) => v,
            Num::Float(v) => v as i64,
        }
    }
}

impl<'a> Header<'a> {
    /// The header at the start of `raw` (at least its length), of the version `detect` gives.
    pub fn new(raw: &'a [u8], version: u8) -> Header<'a> {
        let length = if version == 1 { 348 } else { 540 };
        let little = i32::from_le_bytes(raw[..4].try_into().unwrap()) == length;
        Header { raw: &raw[..length as usize], version, little }
    }

    pub fn fields(&self) -> &'static [Field] {
        if self.version == 1 { &NIFTI1 } else { &NIFTI2 }
    }

    fn field(&self, name: &str) -> Field {
        *self.fields().iter().find(|f| f.0 == name).expect("a header field")
    }

    /// The numeric value of `kind` at `at`.
    pub fn number(&self, at: usize, kind: &str) -> Num {
        let n = &self.raw[at..];
        macro_rules! get {
            ($t:ty, $w:expr) => {{
                let a: [u8; $w] = n[..$w].try_into().unwrap();
                if self.little { <$t>::from_le_bytes(a) } else { <$t>::from_be_bytes(a) }
            }};
        }
        match kind {
            "u1" => Num::Int(n[0] as i64),
            "i2" => Num::Int(get!(i16, 2) as i64),
            "i4" => Num::Int(get!(i32, 4) as i64),
            "i8" => Num::Int(get!(i64, 8)),
            "f4" => Num::Float(get!(f32, 4) as f64),
            _ => Num::Float(get!(f64, 8)),
        }
    }

    /// The values of a numeric field.
    pub fn values(&self, name: &str) -> Vec<Num> {
        let (_, at, kind, count) = self.field(name);
        let w = width(kind);
        (0..count).map(|k| self.number(at + k * w, kind)).collect()
    }

    pub fn get(&self, name: &str) -> Num {
        self.values(name)[0]
    }

    /// The header as one record type (conventions §8.6
    /// <https://github.com/d-v-b/vzip/blob/main/spec/conventions.md#86-types>).
    pub fn record_type(&self) -> String {
        let e = if self.little { "<" } else { ">" };
        let parts: Vec<String> = self
            .fields()
            .iter()
            .map(|&(name, _, kind, count)| match kind {
                "c" => format!("{name}:cstr[{count}]"),
                "u1" if count == 1 => format!("{name}:u1"),
                k if count == 1 => format!("{name}:{e}{k}"),
                k => format!("{name}:{e}{k}[{count}]"),
            })
            .collect();
        format!("{{{}}}", parts.join(","))
    }
}

pub fn width(kind: &str) -> usize {
    match kind {
        "u1" => 1,
        "i2" => 2,
        "i4" | "f4" => 4,
        _ => 8,
    }
}

#[derive(Clone, Copy, PartialEq, Debug)]
enum Phase {
    Start,
    Head,
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
    facts: J,
}

impl Nifti {
    pub fn new(size: u64) -> Self {
        Nifti {
            size,
            phase: Phase::Start,
            fed: Fed::default(),
            ir: Ir::new(size),
            rules: rules::nifti(),
            little: true,
            vox: 0,
            q: 0,
            chaining: false,
            k: 0,
            facts: J::Null,
        }
    }

    pub fn feed(&mut self, offset: u64, data: Vec<u8>) {
        self.fed.feed(offset, data);
    }

    fn check(&self, role: &str, mut env: Vars<'static>) -> Res<()> {
        self.rules.check(role, &mut env)
    }

    pub fn step(&mut self) -> Result<Step, String> {
        loop {
            match self.phase {
                Phase::Start => {
                    self.phase = Phase::Head;
                    return Ok(Step::Read(vec![(0, self.size.min(552))]));
                }
                Phase::Head => {
                    self.head()?;
                    self.phase = Phase::Chain;
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

    /// The header and its checks (spec/virtualize/nifti.md §7.1
    /// <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/nifti.md#71-header>), then the
    /// elements of the header, the extender and the voxel data.
    fn head(&mut self) -> Res<()> {
        let got = self.fed.get(0, self.size.min(552)).ok_or("internal: no header")?.to_vec();
        let version = detect(&got).ok_or("not a NIfTI-1 or NIfTI-2 single file")?;
        let length: u64 = if version == 1 { 348 } else { 540 };
        self.check(
            "file",
            rules::env([("version", num(version)), ("length", num(length as f64)), ("size", num(self.size as f64))]),
        )?;
        let h = Header::new(&got, version);
        let dim = h.values("dim");
        let n = dim[0].i();
        self.check("dimensions", rules::env([("n", num(n as f64))]))?;
        let mut sizes = [1i64; 8];
        for i in 1..=n as usize {
            let d = dim[i].i();
            self.check(
                "dimension",
                rules::env([("i", num(i as f64)), ("size", num(d as f64)), ("shown", text(&d.to_string()))]),
            )?;
            sizes[i] = d;
        }
        self.check("unused_dimensions", rules::env([("d6", num(sizes[6] as f64)), ("d7", num(sizes[7] as f64))]))?;
        let code = h.get("datatype").i();
        let bitpix = h.get("bitpix").i();
        self.check("data_type", rules::env([("code", num(code as f64)), ("bitpix", num(bitpix as f64)), ("n", num(n as f64))]))?;
        let spec = &self.rules.table("datatypes")[code.to_string()];
        let (bits, samples) = (spec["bits"].as_u64().unwrap_or(8), spec["samples"].as_u64().unwrap_or(1));
        let (vox, integral, shown) = match h.get("vox_offset") {
            Num::Float(v) if v.is_finite() && v.fract() == 0.0 => (v, true, format!("{}", v as i128)),
            Num::Float(v) => (v, false, py_float(v)),
            Num::Int(v) => (v as f64, true, v.to_string()),
        };
        self.check(
            "voxel_offset",
            rules::env([("vox", num(vox)), ("integral", flag(integral)), ("shown", text(&shown)), ("length", num(length as f64))]),
        )?;
        let vox = vox as u64;
        let total = sizes[1..6].iter().fold(bits as u128 / 8, |a, &s| a.saturating_mul(s as u128));
        self.check(
            "voxel_data",
            rules::env([
                ("total", num(total as f64)),
                ("vox", num(vox as f64)),
                ("size", num(self.size as f64)),
                ("total_shown", text(&total.to_string())),
            ]),
        )?;
        // the extender lies within the file, before the voxel data (vox >= L + 4)
        let ext = got[length as usize..length as usize + 4].to_vec();
        let header = self.ir.value(0, "header", NO_INDEX, &h.record_type(), 0, (0, length), Some(h.raw))?;
        self.ir.value(0, "extender", NO_INDEX, "u1[4]", 0, (length, 4), Some(&ext))?;
        let endian = if h.little { "little" } else { "big" };
        let dtype = spec["dtype"].as_str().unwrap_or("uint8");
        let form = json!({"geometry": {"data_type": dtype, "samples": samples}, "codec": {"endian": endian},
                          "recipe": [["src", 0, null]]});
        let voxels = self.ir.data(0, "voxels", NO_INDEX, 0, (vox, total as u64), &form.to_string())?;
        self.little = h.little;
        self.vox = vox;
        self.q = length + 4;
        self.chaining = ext[0] != 0;
        self.facts = json!({"version": version, "header": header, "voxels": voxels, "extended": ext[0] != 0});
        Ok(())
    }

    /// The extensions in a window of the chain from `q` (spec/virtualize/nifti.md §5
    /// <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/nifti.md#5-source-metadata>):
    /// the chain stops at an `esize` below 8 or past the voxel data.
    fn chain(&mut self, block: &[u8]) -> Res<()> {
        let e = if self.little { "<" } else { ">" };
        let i32_at = |at: usize| {
            let a: [u8; 4] = block[at..at + 4].try_into().unwrap();
            if self.little { i32::from_le_bytes(a) } else { i32::from_be_bytes(a) }
        };
        let mut p = 0usize;
        while p + 8 <= block.len() {
            let (esize, ecode) = (i32_at(p), i32_at(p + 4));
            let at = self.q + p as u64;
            if esize < 8 || at + esize as u64 > self.vox {
                self.chaining = false;
                return Ok(());
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
            self.ir.value(0, "extensions/", self.k, &ty, 0, (at, esize), raw)?;
            self.k += 1;
            p += esize as usize;
        }
        self.q += p as u64;
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
