//! A sans-IO parser of MRC2014 files (CCP4 maps, image stacks, volumes and volume stacks)
//! into the IR (spec/virtualize/mrc.md §8
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mrc.md#8-mrc-profile>, and the
//! source model of §5.3 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mrc.md#53-elements>).
//!
//! One round: the 1024-byte header. The schema (`schema/mrc.json`) holds every check, in
//! the profile's order. The IR is the header (one record value), the extended header
//! (one opaque value, when the file has one; never read), the data (one `data` element)
//! and a gap for the bytes after it.

use crate::check;
use crate::fed::Fed;
use crate::ir::*;
use crate::nd2::Step;
use crate::nifti::{Num, width};
use crate::rules::{self, Rules, Vars, num, text};
use serde_json::{Value as J, json};

/// The header's length.
pub const HEADER: u64 = 1024;
/// IMOD's stamp (`IMOD` as a little-endian i32) at byte 152 of the header, and its flags at
/// 156, whose bit 0 says a mode 0 file's bytes are unsigned.
pub const IMOD_STAMP: usize = 152;
pub const IMOD_FLAGS: usize = 156;
pub const IMOD: i64 = 1146047817;

/// A header field: its name, offset, kind (`i4`, `f4`, `u1`, `b` for opaque bytes, `c` for
/// text, or `l` for the ten 80-byte labels) and count, in the standard's order
/// (spec/virtualize/mrc.md §2 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mrc.md#2-the-header>).
pub type Field = (&'static str, usize, &'static str, usize);

pub const FIELDS: [Field; 30] = [
    ("nx", 0, "i4", 1), ("ny", 4, "i4", 1), ("nz", 8, "i4", 1), ("mode", 12, "i4", 1),
    ("nxstart", 16, "i4", 1), ("nystart", 20, "i4", 1), ("nzstart", 24, "i4", 1),
    ("mx", 28, "i4", 1), ("my", 32, "i4", 1), ("mz", 36, "i4", 1),
    ("cella", 40, "f4", 3), ("cellb", 52, "f4", 3),
    ("mapc", 64, "i4", 1), ("mapr", 68, "i4", 1), ("maps", 72, "i4", 1),
    ("dmin", 76, "f4", 1), ("dmax", 80, "f4", 1), ("dmean", 84, "f4", 1),
    ("ispg", 88, "i4", 1), ("nsymbt", 92, "i4", 1),
    ("extra1", 96, "b", 8), ("exttyp", 104, "c", 4), ("nversion", 108, "i4", 1), ("extra2", 112, "b", 84),
    ("origin", 196, "f4", 3), ("map", 208, "c", 4), ("machst", 212, "u1", 4),
    ("rms", 216, "f4", 1), ("nlabl", 220, "i4", 1), ("label", 224, "l", 10),
];

/// Whether a file's first bytes are an MRC file's (spec/virtualize.md §1.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize.md#12-input>): `MAP` at 208.
pub fn detect(head: &[u8]) -> bool {
    head.len() >= 212 && &head[208..211] == b"MAP"
}

/// The 1024-byte header and its byte order, read field by field.
pub struct Header<'a> {
    pub raw: &'a [u8],
    pub little: bool,
}

impl<'a> Header<'a> {
    /// The header at the start of `raw` (at least 1024 bytes), in the byte order its machine
    /// stamp gives (0x11: big-endian; else little-endian).
    pub fn new(raw: &'a [u8]) -> Header<'a> {
        Header { raw: &raw[..HEADER as usize], little: raw[212] != 0x11 }
    }

    fn field(&self, name: &str) -> Field {
        *FIELDS.iter().find(|f| f.0 == name).expect("a header field")
    }

    /// The numeric value of `kind` at `at`.
    pub fn number(&self, at: usize, kind: &str) -> Num {
        let n = &self.raw[at..];
        match kind {
            "u1" => Num::Int(n[0] as i64),
            "i4" => {
                let a: [u8; 4] = n[..4].try_into().unwrap();
                Num::Int(if self.little { i32::from_le_bytes(a) } else { i32::from_be_bytes(a) } as i64)
            }
            _ => {
                let a: [u8; 4] = n[..4].try_into().unwrap();
                Num::Float(if self.little { f32::from_le_bytes(a) } else { f32::from_be_bytes(a) } as f64)
            }
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

    /// The bytes of a text, byte or label field.
    pub fn bytes(&self, name: &str) -> &'a [u8] {
        let (_, at, kind, count) = self.field(name);
        let n = if kind == "l" { 80 * count } else { count };
        &self.raw[at..at + n]
    }

    /// The header as one record type (conventions §8.6
    /// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#86-types>).
    pub fn record_type(&self) -> String {
        let e = if self.little { "<" } else { ">" };
        let parts: Vec<String> = FIELDS
            .iter()
            .map(|&(name, _, kind, count)| match kind {
                "c" => format!("{name}:cstr[{count}]"),
                "b" => format!("{name}:bytes[{count}]"),
                "l" => format!("{name}:{{text:cstr[80]}}[{count}]"),
                "u1" => format!("{name}:u1[{count}]"),
                k if count == 1 => format!("{name}:{e}{k}"),
                k => format!("{name}:{e}{k}[{count}]"),
            })
            .collect();
        format!("{{{}}}", parts.join(","))
    }
}

#[derive(Clone, Copy, PartialEq, Debug)]
enum Phase {
    Start,
    Head,
    Done,
}

pub struct Mrc {
    size: u64,
    phase: Phase,
    fed: Fed,
    ir: Ir,
    rules: &'static Rules,
    facts: J,
}

impl Mrc {
    pub fn new(size: u64) -> Self {
        Mrc { size, phase: Phase::Start, fed: Fed::default(), ir: Ir::new(size), rules: rules::of("mrc"), facts: J::Null }
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
                self.phase = Phase::Head;
                Ok(Step::Read(vec![(0, self.size.min(HEADER))]))
            }
            Phase::Head => {
                self.head()?;
                self.phase = Phase::Done;
                Ok(Step::Done)
            }
            Phase::Done => Ok(Step::Done),
        }
    }

    /// The header and its checks (spec/virtualize/mrc.md §8.1
    /// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mrc.md#81-header>), then the
    /// elements of the header, the extended header and the data.
    fn head(&mut self) -> Res<()> {
        let got = self.fed.get(0, self.size.min(HEADER)).ok_or("internal: no header")?.to_vec();
        if !detect(&got) {
            return Err("not an MRC file".into());
        }
        self.check("file", rules::env([("size", num(self.size as f64))]))?;
        let stamp = &got[212..216];
        let shown = stamp.iter().map(|b| format!("{b:02x}")).collect::<Vec<_>>().join(" ");
        self.check("byte_order", rules::env([("stamp", num(stamp[0] as f64)), ("shown", text(&shown))]))?;
        let h = Header::new(&got);
        let mut dims = [0i64; 3];
        for (k, name) in ["nx", "ny", "nz"].iter().enumerate() {
            let d = h.get(name).i();
            self.check("dimension", rules::env([("name", text(name)), ("size", num(d as f64))]))?;
            dims[k] = d;
        }
        let code = h.get("mode").i();
        self.check("mode", rules::env([("code", num(code as f64))]))?;
        let spec = &self.rules.table("modes")[code.to_string()];
        let bytes = spec["bytes"].as_u64().unwrap_or(1);
        // IMOD marks a mode 0 file whose bytes are unsigned (spec/virtualize/mrc.md §3
        // <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mrc.md#3-data-types>)
        let imod_unsigned = code == 0 && h.number(IMOD_STAMP, "i4").i() == IMOD && h.number(IMOD_FLAGS, "i4").i() & 1 == 1;
        let dtype = if imod_unsigned { "uint8" } else { spec["dtype"].as_str().unwrap_or("int8") };
        let nsymbt = h.get("nsymbt").i();
        self.check("extended_header", rules::env([("nsymbt", num(nsymbt as f64)), ("size", num(self.size as f64))]))?;
        let (ispg, mz) = (h.get("ispg").i(), h.get("mz").i());
        if ispg >= 401 {
            self.check(
                "volume_stack",
                rules::env([("ispg", num(ispg as f64)), ("nz", num(dims[2] as f64)), ("mz", num(mz as f64))]),
            )?;
        }
        let total = dims.iter().fold(bytes as u128, |a, &s| a.saturating_mul(s as u128));
        let offset = HEADER + nsymbt as u64;
        self.check(
            "data_size",
            rules::env([
                ("total", num(total as f64)),
                ("total_shown", text(&total.to_string())),
                ("offset", num(offset as f64)),
                ("size", num(self.size as f64)),
            ]),
        )?;
        let header = self.ir.value(0, "header", NO_INDEX, &h.record_type(), 0, (0, HEADER), Some(h.raw))?;
        if nsymbt > 0 {
            self.ir.value(0, "extended", NO_INDEX, &format!("bytes[{nsymbt}]"), 0, (HEADER, nsymbt as u64), None)?;
        }
        let endian = if h.little { "little" } else { "big" };
        let form = json!({"geometry": {"data_type": dtype, "samples": 1}, "codec": {"endian": endian},
                          "recipe": [["src", 0, null]]});
        let data = self.ir.data(0, "data", NO_INDEX, 0, (offset, total as u64), &form.to_string())?;
        // the payload past which the projection copies an edge chunk, read once per run
        self.facts = json!({"header": header, "data": data, "data_type": dtype, "copy_threshold": crate::out::copy_threshold()});
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

crate::parser!(Mrc);

/// The MRC profile's file run (spec/virtualize/mrc.md).
pub struct Profile;

impl crate::run::FileProfile for Profile {
    /// `MAP` at byte 208 (`detect`).
    fn detect(&self, head: &[u8]) -> bool {
        detect(head)
    }
    fn parser(&self, size: u64) -> crate::run::AnyParser {
        crate::run::AnyParser::of::<Mrc>(size)
    }
    fn project(&self, ir: &crate::ir::Ir, facts: &serde_json::Value, url: &str, copied: &crate::project::Copied) -> Result<crate::out::Out, String> {
        crate::project::mrc::project(ir, facts, url, copied)
    }
    fn reads(&self, ir: &crate::ir::Ir, facts: &serde_json::Value) -> Result<Vec<(u64, u64)>, String> {
        crate::project::mrc::reads(ir, facts)
    }
}
