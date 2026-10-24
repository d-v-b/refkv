//! A fixed-layout header read field by field from a table of fields: MRC's and NIfTI's. The
//! numbers decode as the record type the header is declared with (`types.rs`).

use crate::types::{self, Val};

/// A header field: its name, offset, kind and count (values, or a text's bytes). Kinds: a
/// number of the record grammar (`u1`, `i2`, `i4`, `i8`, `f4`, `f8`), `c` for text, `b` for
/// opaque bytes, and `l` for 80-byte labels.
pub type Field = (&'static str, usize, &'static str, usize);

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

/// The bytes of one value of a numeric kind.
pub fn width(kind: &str) -> usize {
    match kind {
        "u1" => 1,
        "i2" => 2,
        "i4" | "f4" => 4,
        _ => 8,
    }
}

/// A header's bytes, byte order and fields.
pub struct Header<'a> {
    pub raw: &'a [u8],
    pub little: bool,
    pub fields: &'static [Field],
}

impl<'a> Header<'a> {
    fn field(&self, name: &str) -> Field {
        *self.fields.iter().find(|f| f.0 == name).expect("a header field")
    }

    /// The numeric value of `kind` at `at`.
    pub fn number(&self, at: usize, kind: &str) -> Num {
        match types::num(kind, !self.little, &self.raw[at..at + width(kind)]) {
            Val::Float(v) => Num::Float(v),
            Val::Int(v) => Num::Int(v as i64),
            _ => unreachable!("a number"),
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
    /// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#86-types>): a single byte
    /// has no byte order.
    pub fn record_type(&self) -> String {
        let e = if self.little { "<" } else { ">" };
        let parts: Vec<String> = self
            .fields
            .iter()
            .map(|&(name, _, kind, count)| match kind {
                "c" => format!("{name}:cstr[{count}]"),
                "b" => format!("{name}:bytes[{count}]"),
                "l" => format!("{name}:{{text:cstr[80]}}[{count}]"),
                "u1" if count == 1 => format!("{name}:u1"),
                "u1" => format!("{name}:u1[{count}]"),
                k if count == 1 => format!("{name}:{e}{k}"),
                k => format!("{name}:{e}{k}[{count}]"),
            })
            .collect();
        format!("{{{}}}", parts.join(","))
    }
}
