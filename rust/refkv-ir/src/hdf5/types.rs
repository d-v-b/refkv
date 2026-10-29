//! HDF5 datatypes, decoded whole (spec/zarrify/hdf5.md §3
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#3-datasets>): every class, so
//! that a compound's or an enumeration's members can be walked, a datatype classified as the
//! Zarr data type it presents as (or as none), described as JSON, and a value of it decoded as
//! JSON (spec/zarrify/hdf5.md §6 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#6-attributes>).

use super::{bad, le, R};
use crate::out::int_json;
use crate::text::text_json;
use serde_json::{json, Map, Value as J};

/// Compounds, enumerations, arrays and sequences nest at most this deep.
const MAX_DEPTH: usize = 16;

/// A datatype message, decoded.
#[derive(Debug, Clone, PartialEq)]
pub struct Dt {
    pub version: u8,
    pub size: u64,
    pub kind: Kind,
    /// the message's length in bytes
    pub len: usize,
}

#[derive(Debug, Clone, PartialEq)]
pub enum Kind {
    /// class 0: big-endian, signed, bit offset, precision
    Int { big: bool, signed: bool, offset: u64, precision: u64 },
    /// class 1: big-endian, the IEEE binary width it is (16, 32, 64), or None
    Float { big: bool, ieee: Option<u32> },
    Time,
    /// class 3: padding (0 null-terminated, 1 null-padded, 2 space-padded), UTF-8
    Str { pad: u8, utf8: bool },
    /// class 4: big-endian, bit offset, precision
    Bitfield { big: bool, offset: u64, precision: u64 },
    Opaque { tag: String },
    Compound(Vec<Member>),
    /// class 7: 0 object, 1 region (datatype versions 1 to 3); other kinds (version 4)
    Reference { kind: u8 },
    Enum { base: Box<Dt>, members: Vec<(Vec<u8>, Vec<u8>)> },
    /// class 9: a string (else a sequence of `base`), its padding and UTF-8
    Vlen { string: bool, pad: u8, utf8: bool, base: Box<Dt> },
    Array { dims: Vec<u64>, base: Box<Dt> },
}

#[derive(Debug, Clone, PartialEq)]
pub struct Member {
    pub name: Vec<u8>,
    pub offset: u64,
    pub dt: Dt,
}

/// The NUL-terminated name at `p` of `d`, and the offset past it (padded to 8 bytes, the
/// name's start being 8-aligned, when `pad`).
fn name_at(d: &[u8], p: usize, pad: bool) -> R<(Vec<u8>, usize)> {
    let Some(e) = d.get(p..).and_then(|t| t.iter().position(|&c| c == 0)) else { return bad("a datatype member's name runs past its message") };
    let n = e + 1;
    let next = if pad { p + n.div_ceil(8) * 8 } else { p + n };
    Ok((d[p..p + e].to_vec(), next))
}

/// The datatype message at the start of `d`.
pub fn parse(d: &[u8]) -> R<Dt> {
    parse_at(d, 0)
}

fn parse_at(d: &[u8], depth: usize) -> R<Dt> {
    if depth > MAX_DEPTH {
        return bad("datatypes nested too deep");
    }
    let cv = le(d, 0, 1)?;
    let (version, cls) = ((cv >> 4) as u8, (cv & 15) as u8);
    if !(1..=5).contains(&version) {
        return bad(format!("unsupported datatype message version {version}"));
    }
    let bits = le(d, 1, 3)? as u32;
    let size = le(d, 4, 4)?;
    let p = 8usize;
    let (kind, len) = match cls {
        0 => (Kind::Int { big: bits & 1 != 0, signed: bits & 8 != 0, offset: le(d, p, 2)?, precision: le(d, p + 2, 2)? }, p + 4),
        1 => {
            let (offset, precision) = (le(d, p, 2)?, le(d, p + 2, 2)?);
            let locs = [le(d, p + 4, 1)?, le(d, p + 5, 1)?, le(d, p + 6, 1)?, le(d, p + 7, 1)?];
            let bias = le(d, p + 8, 4)?;
            let sign = (bits >> 8) & 0xFF;
            let normal = bits & 0x40 == 0 && (bits >> 4) & 3 == 2 && offset == 0;
            let ieee = [(16u32, [10u64, 5, 0, 10], 15u64), (32, [23, 8, 0, 23], 127), (64, [52, 11, 0, 52], 1023)]
                .into_iter()
                .find(|&(w, l, b)| normal && size * 8 == w as u64 && precision == w as u64 && locs == l && bias == b && sign == w - 1)
                .map(|x| x.0);
            (Kind::Float { big: bits & 1 != 0, ieee }, p + 12)
        }
        2 => (Kind::Time, p + 2),
        3 => (Kind::Str { pad: (bits & 15) as u8, utf8: (bits >> 4) & 15 == 1 }, p),
        4 => (Kind::Bitfield { big: bits & 1 != 0, offset: le(d, p, 2)?, precision: le(d, p + 2, 2)? }, p + 4),
        5 => {
            let n = (bits & 0xFF) as usize;
            let tag = d.get(p..p + n).ok_or(super::Fail::Bad("an opaque datatype's tag runs past its message".into()))?;
            (Kind::Opaque { tag: crate::text::utf8_or_latin1(tag.split(|&c| c == 0).next().unwrap_or(&[])) }, p + n)
        }
        6 => {
            let count = (bits & 0xFFFF) as usize;
            let mut q = p;
            let mut members = Vec::with_capacity(count.min(1024));
            for _ in 0..count {
                let (name, mut r) = name_at(d, q, version < 3)?;
                let offset = match version {
                    1 | 2 => {
                        let o = le(d, r, 4)?;
                        r += 4;
                        if version == 1 {
                            // dimensionality, reserved, permutation, reserved, four sizes: a
                            // member of rank 0 is the only kind read
                            if le(d, r, 1)? != 0 {
                                return bad("a compound member with dimensions (datatype version 1)");
                            }
                            r += 28;
                        }
                        o
                    }
                    _ => {
                        let k = [1usize, 2, 3, 4].into_iter().find(|&k| size < 1u64 << (8 * k)).unwrap_or(8);
                        let o = le(d, r, k)?;
                        r += k;
                        o
                    }
                };
                let dt = parse_at(d.get(r..).unwrap_or(&[]), depth + 1)?;
                q = r + dt.len;
                members.push(Member { name, offset, dt });
            }
            (Kind::Compound(members), q)
        }
        7 => {
            if version >= 4 {
                // the revised references (H5R_REF): their encoding is not read
                (Kind::Reference { kind: 0x80 | (bits & 15) as u8 }, d.len())
            } else {
                (Kind::Reference { kind: (bits & 15) as u8 }, p)
            }
        }
        8 => {
            let base = parse_at(d.get(p..).unwrap_or(&[]), depth + 1)?;
            let count = (bits & 0xFFFF) as usize;
            let mut q = p + base.len;
            let mut names = Vec::with_capacity(count.min(1024));
            for _ in 0..count {
                let (name, r) = name_at(d, q, version < 3)?;
                names.push(name);
                q = r;
            }
            let w = base.size as usize;
            let mut members = Vec::with_capacity(names.len());
            for (k, name) in names.into_iter().enumerate() {
                let v = d.get(q + k * w..q + (k + 1) * w).ok_or(super::Fail::Bad("an enumeration's values run past its message".into()))?;
                members.push((name, v.to_vec()));
            }
            (Kind::Enum { base: Box::new(base), members }, q + count * w)
        }
        9 => {
            let base = parse_at(d.get(p..).unwrap_or(&[]), depth + 1)?;
            let len = p + base.len;
            (Kind::Vlen { string: bits & 15 == 1, pad: ((bits >> 4) & 15) as u8, utf8: (bits >> 8) & 15 == 1, base: Box::new(base) }, len)
        }
        10 => {
            let rank = le(d, p, 1)? as usize;
            let mut q = p + if version < 3 { 4 } else { 1 };
            let mut dims = Vec::with_capacity(rank);
            for _ in 0..rank {
                dims.push(le(d, q, 4)?);
                q += 4;
            }
            if version < 3 {
                q += 4 * rank;
            }
            let base = parse_at(d.get(q..).unwrap_or(&[]), depth + 1)?;
            let len = q + base.len;
            (Kind::Array { dims, base: Box::new(base) }, len)
        }
        _ => return bad(format!("unsupported datatype class {cls}")),
    };
    if len > d.len() {
        return bad("a datatype runs past its message");
    }
    Ok(Dt { version, size, kind, len })
}

/// The Zarr core data type of a plain number: a full-precision integer or bitfield of 1, 2, 4 or
/// 8 bytes, or an IEEE float; with its byte order (big-endian).
pub fn number(dt: &Dt) -> Option<(String, bool)> {
    match dt.kind {
        Kind::Int { big, signed, offset: 0, precision } if matches!(dt.size, 1 | 2 | 4 | 8) && precision == 8 * dt.size => {
            Some((format!("{}int{}", if signed { "" } else { "u" }, 8 * dt.size), big))
        }
        Kind::Bitfield { big, offset: 0, precision } if matches!(dt.size, 1 | 2 | 4 | 8) && precision == 8 * dt.size => {
            Some((format!("uint{}", 8 * dt.size), big))
        }
        Kind::Float { big, ieee: Some(w) } => Some((format!("float{w}"), big)),
        _ => None,
    }
}

/// Whether `dt` is h5py's `bool`: an enumeration of a 1-byte integer whose members are exactly
/// `FALSE = 0` and `TRUE = 1`.
pub fn is_bool(dt: &Dt) -> bool {
    let Kind::Enum { base, members } = &dt.kind else { return false };
    if dt.size != 1 || number(base).is_none() || matches!(base.kind, Kind::Float { .. }) || members.len() != 2 {
        return false;
    }
    let mut got: Vec<(&[u8], u8)> = members.iter().map(|(n, v)| (n.as_slice(), v[0])).collect();
    got.sort();
    got == [(&b"FALSE"[..], 0), (&b"TRUE"[..], 1)]
}

/// How a dataset of `dt` is presented (spec/zarrify/hdf5.md §3.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#31-data-types>).
#[derive(Debug, Clone, PartialEq)]
pub enum Present {
    /// a fixed-size data type: its Zarr data type (a string, or a `struct` object), the byte
    /// order of its bytes codec, and whether it is one byte wide (no byte order)
    Fixed { data_type: J, big: bool, one_byte: bool },
    /// a fixed-length string of `n` bytes: `uint8` with a trailing axis of `n`
    Bytes { n: u64 },
    /// variable-length strings
    Strings,
    /// object references
    References,
    /// a compound with string or reference members, presented as hdmf-zarr writes one: a
    /// `struct` whose strings and references (as paths) are fixed-length UTF-32, written whole
    Records(Vec<Field>),
}

/// A member of a compound presented as records: its name, offset, and kind.
#[derive(Debug, Clone, PartialEq)]
pub struct Field {
    pub name: String,
    pub offset: u64,
    pub kind: FieldKind,
}

#[derive(Debug, Clone, PartialEq)]
pub enum FieldKind {
    /// a fixed-size member: its Zarr data type, size, and byte order
    Fixed { data_type: J, size: u64, big: bool },
    /// a variable-length string member, or a fixed-length one of its padding
    Text { fixed: Option<u8> },
    /// an object reference member
    Reference,
}

/// The members of a compound with a string or reference member, as records; or why there are
/// none: a gap, a nested compound, or a member of another kind.
fn records(dt: &Dt) -> Result<Vec<Field>, &'static str> {
    let Kind::Compound(members) = &dt.kind else { return Err("datatype") };
    let mut sorted: Vec<&Member> = members.iter().collect();
    sorted.sort_by_key(|m| m.offset);
    let mut at = 0u64;
    let mut out = Vec::with_capacity(sorted.len());
    for m in sorted {
        if m.offset != at {
            return Err("datatype");
        }
        let Ok(name) = String::from_utf8(m.name.clone()) else { return Err("datatype") };
        if name.is_empty() || out.iter().any(|f: &Field| f.name == name) {
            return Err("datatype");
        }
        let kind = match &m.dt.kind {
            Kind::Vlen { string: true, base, .. } if base.size == 1 && m.dt.size == 16 => FieldKind::Text { fixed: None },
            Kind::Str { pad, .. } => FieldKind::Text { fixed: Some(*pad) },
            Kind::Reference { kind: 0 } if m.dt.size == 8 => FieldKind::Reference,
            Kind::Compound(_) => return Err("datatype"),
            _ => {
                let (t, big) = fixed(&m.dt)?;
                FieldKind::Fixed { data_type: t, size: m.dt.size, big: big.unwrap_or(false) }
            }
        };
        out.push(Field { name, offset: m.offset, kind });
        at += m.dt.size;
    }
    if at != dt.size || !out.iter().any(|f| !matches!(f.kind, FieldKind::Fixed { .. })) {
        return Err("datatype");
    }
    Ok(out)
}

/// The Zarr data type of a fixed-size `dt`, as a `struct` field's or an array's, with its byte
/// order (None for one-byte data), or why there is none.
fn fixed(dt: &Dt) -> Result<(J, Option<bool>), &'static str> {
    if let Some((t, big)) = number(dt) {
        return Ok((json!(t), (dt.size > 1).then_some(big)));
    }
    if is_bool(dt) {
        return Ok((json!("bool"), None));
    }
    match &dt.kind {
        Kind::Enum { base, .. } => match number(base) {
            Some((t, big)) if !matches!(base.kind, Kind::Float { .. }) => Ok((json!(t), (base.size > 1).then_some(big))),
            _ => Err("datatype"),
        },
        Kind::Compound(members) => {
            let mut sorted: Vec<&Member> = members.iter().collect();
            sorted.sort_by_key(|m| m.offset);
            let mut at = 0u64;
            let mut order: Option<bool> = None;
            let mut fields = Vec::with_capacity(sorted.len());
            for m in sorted {
                if m.offset != at || m.name.is_empty() {
                    return Err("datatype");
                }
                let Ok(name) = String::from_utf8(m.name.clone()) else { return Err("datatype") };
                let (t, big) = fixed(&m.dt)?;
                if let Some(b) = big {
                    if order.is_some_and(|o| o != b) {
                        return Err("datatype");
                    }
                    order = Some(b);
                }
                fields.push(json!({"name": name, "data_type": t}));
                at += m.dt.size;
            }
            if at != dt.size || fields.is_empty() {
                return Err("datatype");
            }
            // a record's byte order is its members' (little-endian when every member is one byte)
            Ok((json!({"name": "struct", "configuration": {"fields": fields}}), Some(order.unwrap_or(false))))
        }
        _ => Err("datatype"),
    }
}

/// How a dataset of `dt` is presented, or the reason it is not.
pub fn present(dt: &Dt) -> Result<Present, &'static str> {
    match &dt.kind {
        Kind::Vlen { string: true, base, .. } if base.size == 1 && dt.size == 16 => Ok(Present::Strings),
        Kind::Str { .. } => Ok(Present::Bytes { n: dt.size }),
        Kind::Reference { kind: 0 } if dt.size == 8 => Ok(Present::References),
        Kind::Compound(_) => match fixed(dt) {
            Ok((data_type, big)) => Ok(Present::Fixed { data_type, big: big.unwrap_or(false), one_byte: big.is_none() }),
            Err(_) => records(dt).map(Present::Records),
        },
        _ => {
            let (data_type, big) = fixed(dt)?;
            Ok(Present::Fixed { data_type, big: big.unwrap_or(false), one_byte: big.is_none() })
        }
    }
}

/// An integer of `b` (1 to 8 bytes) in its byte order, signed or not.
pub fn int_of(b: &[u8], big: bool, signed: bool) -> i128 {
    let mut v: u64 = 0;
    for k in 0..b.len() {
        let byte = if big { b[k] } else { b[b.len() - 1 - k] };
        v = (v << 8) | byte as u64;
    }
    if signed && !b.is_empty() && b.len() < 16 {
        let bits = 8 * b.len() as u32;
        if bits < 64 && v >> (bits - 1) & 1 == 1 {
            return v as i128 - (1i128 << bits);
        }
        if bits == 64 {
            return v as i64 as i128;
        }
    }
    v as i128
}

/// A float of `b` (2, 4 or 8 bytes) as binary64.
fn float_of(b: &[u8], big: bool) -> f64 {
    let v = int_of(b, big, false) as u64;
    match b.len() {
        2 => half(v as u16),
        4 => f32::from_bits(v as u32) as f64,
        _ => f64::from_bits(v),
    }
}

/// An IEEE binary16 value as binary64.
fn half(h: u16) -> f64 {
    let (s, e, m) = ((h >> 15) as i32, ((h >> 10) & 0x1F) as i32, (h & 0x3FF) as f64);
    let v = match e {
        0 => m * 2f64.powi(-24),
        31 => {
            if m == 0.0 {
                f64::INFINITY
            } else {
                f64::NAN
            }
        }
        _ => (1.0 + m / 1024.0) * 2f64.powi(e - 15),
    };
    if s == 1 { -v } else { v }
}

/// A float of `b` (2, 4 or 8 bytes) as JSON: the Zarr v3 fill value encoding of the value
/// in its own width (spec/zarrify/hdf5.md §6
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#6-attributes>): a number,
/// "Infinity" or "-Infinity", "NaN" for the canonical NaN, and for −0 or any other NaN the
/// hex string of its bits, "0x" and 2 × width lowercase digits, most significant first.
pub fn float_json(b: &[u8], big: bool) -> J {
    let (bits, x) = (int_of(b, big, false) as u64, float_of(b, big));
    let canonical = match b.len() {
        2 => 0x7e00,
        4 => 0x7fc0_0000,
        _ => 0x7ff8_0000_0000_0000,
    };
    if (x == 0.0 && x.is_sign_negative()) || (x.is_nan() && bits != canonical) {
        json!(format!("0x{bits:0w$x}", w = 2 * b.len()))
    } else if x.is_nan() {
        json!("NaN")
    } else if x.is_infinite() {
        json!(if x > 0.0 { "Infinity" } else { "-Infinity" })
    } else {
        json!(x)
    }
}

/// A fixed-length string's text: to its first NUL, and without trailing spaces when
/// space-padded.
pub fn fixed_text(b: &[u8], pad: u8) -> J {
    let t = b.split(|&c| c == 0).next().unwrap_or(&[]);
    let t = if pad == 2 { &t[..t.iter().rposition(|&c| c != b' ').map_or(0, |e| e + 1)] } else { t };
    text_json(t)
}

/// What a value's decoding needs from the file: a variable-length element's bytes (by its
/// collection's address and object index), and an object reference's path (by address).
pub trait Lookup {
    fn heap(&self, collection: u64, index: u64) -> Option<Vec<u8>>;
    fn path(&self, address: u64) -> Option<String>;
}

/// The JSON of one value of `dt` in `b` (its `dt.size` bytes), or None when the datatype has
/// none (spec/zarrify/hdf5.md §6
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#6-attributes>).
pub fn value_json(dt: &Dt, b: &[u8], look: &dyn Lookup) -> Option<J> {
    if b.len() as u64 != dt.size {
        return None;
    }
    if is_bool(dt) {
        return Some(json!(b[0] != 0));
    }
    match &dt.kind {
        Kind::Int { big, signed, .. } if number(dt).is_some() => Some(int_json(int_of(b, *big, *signed))),
        Kind::Bitfield { big, .. } if number(dt).is_some() => Some(int_json(int_of(b, *big, false))),
        Kind::Float { big, ieee: Some(_) } => Some(float_json(b, *big)),
        Kind::Enum { base, .. } => value_json(base, b, look),
        Kind::Str { pad, .. } => Some(fixed_text(b, *pad)),
        Kind::Vlen { string: true, base, .. } if base.size == 1 => {
            let (n, coll, index) = (le(b, 0, 4).ok()?, le(b, 4, 8).ok()?, le(b, 12, 4).ok()?);
            if n == 0 || coll == 0 || coll == super::UNDEFINED {
                return Some(json!(""));
            }
            let data = look.heap(coll, index)?;
            (data.len() as u64 == n).then(|| text_json(&data))
        }
        Kind::Reference { kind: 0 } if dt.size == 8 => {
            let a = le(b, 0, 8).ok()?;
            if a == super::UNDEFINED || a == 0 {
                return Some(J::Null);
            }
            Some(look.path(a).map_or(J::Null, |p| json!({"_REFERENCE": {"source": ".", "path": p}})))
        }
        Kind::Compound(members) => {
            let mut m = Map::new();
            for mem in members {
                let (o, n) = (mem.offset as usize, mem.dt.size as usize);
                let v = value_json(&mem.dt, b.get(o..o + n)?, look)?;
                m.insert(crate::text::utf8_or_latin1(&mem.name), v);
            }
            Some(J::Object(m))
        }
        _ => None,
    }
}

/// The JSON of the values of an attribute or a fill value: `count` values of `dt` in `data`,
/// shaped by `dims` (a scalar for none), or None when one has no JSON.
pub fn values_json(dt: &Dt, dims: &[u64], data: &[u8], look: &dyn Lookup) -> Option<J> {
    let w = dt.size as usize;
    let one = |k: usize| value_json(dt, data.get(k * w..(k + 1) * w)?, look);
    fn nest(dims: &[u64], k: &mut usize, one: &dyn Fn(usize) -> Option<J>) -> Option<J> {
        match dims.split_first() {
            None => {
                let v = one(*k);
                *k += 1;
                v
            }
            Some((&n, rest)) => {
                let mut out = Vec::with_capacity(n.min(1 << 16) as usize);
                for _ in 0..n {
                    out.push(nest(rest, k, one)?);
                }
                Some(J::Array(out))
            }
        }
    }
    let mut k = 0usize;
    nest(dims, &mut k, &one)
}

/// A description of `dt` as JSON, for the source metadata (spec/zarrify/hdf5.md §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#7-source-metadata>).
pub fn describe(dt: &Dt) -> J {
    let order = |big: bool| if big { "big" } else { "little" };
    let mut d = match &dt.kind {
        Kind::Int { big, signed, offset, precision } => json!({"class": "integer", "order": order(*big), "signed": signed, "offset": offset, "precision": precision}),
        Kind::Float { big, ieee } => json!({"class": "float", "order": order(*big), "ieee": ieee.is_some()}),
        Kind::Time => json!({"class": "time"}),
        Kind::Str { pad, utf8 } => json!({"class": "string", "padding": pad_name(*pad), "charset": if *utf8 { "utf-8" } else { "ascii" }}),
        Kind::Bitfield { big, offset, precision } => json!({"class": "bitfield", "order": order(*big), "offset": offset, "precision": precision}),
        Kind::Opaque { tag } => json!({"class": "opaque", "tag": tag}),
        Kind::Compound(members) => json!({"class": "compound", "members": members.iter().map(|m| json!({
            "name": text_json(&m.name), "offset": m.offset, "datatype": describe(&m.dt)})).collect::<Vec<_>>()}),
        Kind::Reference { kind } => json!({"class": "reference", "kind": match kind { 0 => "object", 1 => "region", _ => "other" }}),
        Kind::Enum { base, members } => {
            let w = base.size as usize;
            let mut m = Map::new();
            for (name, v) in members {
                let value = match base.kind {
                    Kind::Int { big, signed, .. } if v.len() == w => int_json(int_of(v, big, signed)),
                    _ => J::Null,
                };
                m.insert(crate::text::utf8_or_latin1(name), value);
            }
            json!({"class": "enum", "base": describe(base), "members": m})
        }
        Kind::Vlen { string: true, pad, utf8, .. } => {
            json!({"class": "vlen_string", "padding": pad_name(*pad), "charset": if *utf8 { "utf-8" } else { "ascii" }})
        }
        Kind::Vlen { base, .. } => json!({"class": "vlen_sequence", "base": describe(base)}),
        Kind::Array { dims, base } => json!({"class": "array", "dims": dims, "base": describe(base)}),
    };
    d["size"] = json!(dt.size);
    d
}

fn pad_name(p: u8) -> &'static str {
    match p {
        0 => "nullterm",
        1 => "nullpad",
        2 => "spacepad",
        _ => "other",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A datatype message's 8-byte header: class and version 1, bit fields, size.
    fn head(cls: u8, bits: u32, size: u32) -> Vec<u8> {
        let mut d = vec![0x10 | cls];
        d.extend_from_slice(&bits.to_le_bytes()[..3]);
        d.extend_from_slice(&size.to_le_bytes());
        d
    }

    fn int(size: u32, signed: bool) -> Vec<u8> {
        let mut d = head(0, if signed { 8 } else { 0 }, size);
        d.extend_from_slice(&0u16.to_le_bytes());
        d.extend_from_slice(&((8 * size) as u16).to_le_bytes());
        d
    }

    fn f32le() -> Vec<u8> {
        let mut d = head(1, 0x20 | (31 << 8), 4);
        d.extend_from_slice(&[0, 0, 32, 0, 23, 8, 0, 23]);
        d.extend_from_slice(&127u32.to_le_bytes());
        d
    }

    struct None_;
    impl Lookup for None_ {
        fn heap(&self, _: u64, _: u64) -> Option<Vec<u8>> {
            None
        }
        fn path(&self, a: u64) -> Option<String> {
            Some(format!("/at{a}"))
        }
    }

    /// Each class decodes, and the classifier gives its Zarr data type: integers, floats, h5py's
    /// bool, an enumeration, a packed compound (as a struct, its members in offset order), a
    /// string, a vlen string, an object reference; a padded compound, a region reference, a
    /// sequence and an opaque type have none.
    #[test]
    fn datatypes_classify() {
        let i4 = parse(&int(4, true)).unwrap();
        assert_eq!(present(&i4), Ok(Present::Fixed { data_type: json!("int32"), big: false, one_byte: false }));
        assert_eq!(present(&parse(&int(1, false)).unwrap()), Ok(Present::Fixed { data_type: json!("uint8"), big: false, one_byte: true }));
        assert_eq!(present(&parse(&f32le()).unwrap()), Ok(Present::Fixed { data_type: json!("float32"), big: false, one_byte: false }));
        // h5py's bool: enum int8 {FALSE: 0, TRUE: 1}
        let mut b = head(8, 2, 1);
        b.extend(int(1, true));
        b.extend_from_slice(b"FALSE\0\0\0TRUE\0\0\0\0");
        b.extend_from_slice(&[0, 1]);
        let bdt = parse(&b).unwrap();
        assert!(is_bool(&bdt));
        assert_eq!(present(&bdt), Ok(Present::Fixed { data_type: json!("bool"), big: false, one_byte: true }));
        assert_eq!(value_json(&bdt, &[1], &None_), Some(json!(true)));
        // a compound {y: f4 at 4, x: u4 at 0}, version 1 members (name padded, 28 bytes of
        // dimension fields)
        let mut c = head(6, 2, 8);
        for (name, off, t) in [(&b"y\0\0\0\0\0\0\0"[..], 4u32, f32le()), (&b"x\0\0\0\0\0\0\0"[..], 0, int(4, false))] {
            c.extend_from_slice(name);
            c.extend_from_slice(&off.to_le_bytes());
            c.extend_from_slice(&[0u8; 28]);
            c.extend(t);
        }
        let cdt = parse(&c).unwrap();
        assert_eq!(present(&cdt), Ok(Present::Fixed {
            data_type: json!({"name": "struct", "configuration": {"fields": [{"name": "x", "data_type": "uint32"}, {"name": "y", "data_type": "float32"}]}}),
            big: false, one_byte: false,
        }));
        let mut v = 7u32.to_le_bytes().to_vec();
        v.extend_from_slice(&1.5f32.to_le_bytes());
        assert_eq!(value_json(&cdt, &v, &None_), Some(json!({"y": 1.5, "x": 7})));
        // a member that is an object reference: records
        let mut rc = head(6, 2, 12);
        for (name, off, t) in [(&b"i\0\0\0\0\0\0\0"[..], 0u32, int(4, true)), (&b"r\0\0\0\0\0\0\0"[..], 4, head(7, 0, 8))] {
            rc.extend_from_slice(name);
            rc.extend_from_slice(&off.to_le_bytes());
            rc.extend_from_slice(&[0u8; 28]);
            rc.extend(t);
        }
        assert_eq!(present(&parse(&rc).unwrap()), Ok(Present::Records(vec![
            Field { name: "i".into(), offset: 0, kind: FieldKind::Fixed { data_type: json!("int32"), size: 4, big: false } },
            Field { name: "r".into(), offset: 4, kind: FieldKind::Reference },
        ])));
        // the same members with a gap: no data type
        let mut g = c.clone();
        g[4] = 12;
        assert_eq!(present(&parse(&g).unwrap()), Err("datatype"));
        // strings
        let s = parse(&head(3, 0x10, 5)).unwrap();
        assert_eq!(present(&s), Ok(Present::Bytes { n: 5 }));
        assert_eq!(value_json(&s, b"ab\0\0\0", &None_), Some(json!("ab")));
        let mut vs = head(9, 1 | (1 << 8), 16);
        vs.extend(head(3, 0x10, 1));
        assert_eq!(present(&parse(&vs).unwrap()), Ok(Present::Strings));
        let r = parse(&head(7, 0, 8)).unwrap();
        assert_eq!(present(&r), Ok(Present::References));
        assert_eq!(value_json(&r, &40u64.to_le_bytes(), &None_), Some(json!({"_REFERENCE": {"source": ".", "path": "/at40"}})));
        assert_eq!(present(&parse(&head(7, 1, 12)).unwrap()), Err("datatype"));
        let mut seq = head(9, 0, 16);
        seq.extend(int(4, true));
        assert_eq!(present(&parse(&seq).unwrap()), Err("datatype"));
        let mut o = head(5, 8, 4);
        o.extend_from_slice(b"tag\0\0\0\0\0");
        assert_eq!(present(&parse(&o).unwrap()), Err("datatype"));
    }

    /// Values: integers of each width and order, a binary16, floats as Zarr fill values, and
    /// nested arrays by shape.
    #[test]
    fn values_decode() {
        assert_eq!(int_of(&[0xff, 0xfe], false, true), -257);
        assert_eq!(int_of(&[0xff, 0xfe], true, false), 0xfffe);
        assert_eq!(int_of(&[0xff; 8], false, true), -1);
        assert_eq!(half(0x3c00), 1.0);
        assert_eq!(half(0xc000), -2.0);
        assert_eq!(float_json(&f64::NAN.to_le_bytes(), false), json!("NaN"));
        assert_eq!(float_json(&[0x7f, 0xc0, 0, 1], true), json!("0x7fc00001"));
        assert_eq!(float_json(&[0, 0x7e], false), json!("NaN"));
        assert_eq!(float_json(&[0, 0xfe], false), json!("0xfe00"));
        assert_eq!(float_json(&(-0.0f64).to_le_bytes(), false), json!("0x8000000000000000"));
        assert_eq!(float_json(&f32::NEG_INFINITY.to_be_bytes(), true), json!("-Infinity"));
        assert_eq!(float_json(&0.5f32.to_le_bytes(), false), json!(0.5));
        let i2 = parse(&int(2, true)).unwrap();
        let data: Vec<u8> = (0..6i16).flat_map(|v| v.to_le_bytes()).collect();
        assert_eq!(values_json(&i2, &[2, 3], &data, &None_), Some(json!([[0, 1, 2], [3, 4, 5]])));
        assert_eq!(values_json(&i2, &[], &data[..2], &None_), Some(json!(0)));
        assert_eq!(values_json(&i2, &[0], &[], &None_), Some(json!([])));
    }
}
