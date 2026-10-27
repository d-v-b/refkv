//! The metadata tree of a `.vsi` file (spec/zarrify/vsi.md §2
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#2-the-vsi-file>): containers
//! of fields, parsed from the whole file held in memory, with every byte of every region
//! accounted for; its IR elements (§6.2
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#62-the-ir-mirror>) and its
//! translation to JSON (§6.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#61-the-root>).
//!
//! **Layout** (as read from Olympus's files; Bio-Formats' CellSensReader reads the same bytes
//! more loosely). A **container** is a 24-byte header (`header_size` 24, `version` 21321,
//! `volume_version`, `data_offset`, `flags` whose low 28 bits count the fields, 4 reserved
//! bytes) followed by its fields, which tile its **region** `[fp, end)`. A **field** is a 16-byte
//! header (`type`, `tag`, `next`, `size`), plus a second tag when bit 27 of `type` is set; its
//! region runs from its header to `fp + next`, or to the container's end for the last field
//! (`next` 0). Its **data** starts after the header: `size` bytes, except that a field with a
//! second tag whose `size` is not 0 has its data up to `fp + 20 + size` (the writer counts
//! those from the container's start), and an **inline** field (bit 30) has none, its `size`
//! being its value. An **extended** field (bit 28) holds, by its type: a **volume** (0), one
//! container filling its data; a **property set** (1) or an **mdim volume** (2), one container
//! filling its region; a **TIFF IFD** (10), whose data region holds an IFD of the TIFF chain,
//! with its values and strips. Whatever is left of a field's region after its data (the
//! coarse-level IFDs sit there, after an inline `Coarse pyramid level` field) holds TIFF
//! structures too, or padding.

use crate::fed::Word;
use crate::ir::{Ir, NO_INDEX, Res};
use crate::rules::{self, Rules, flag, num};
use serde_json::{Value as J, json};

pub const CONTAINER_HEADER: u64 = 24;
pub const CONTAINER_VERSION: u16 = 21321;
pub const CONTAINER_TYPE: &str = "{header_size:<u2,version:<u2,volume_version:<i4,data_offset:<i8,flags:<u4,reserved:<u4}";
const FIELD_TYPE: &str = "{type:<u4,tag:<u4,next:<u4,size:<u4}";
const FIELD_TYPE2: &str = "{type:<u4,tag:<u4,next:<u4,size:<u4,tag2:<u4}";
/// Opaque values longer than this are written to the root's JSON as their length alone.
pub const MAX_JSON_BYTES: usize = 1024;

pub const VOLUME: u32 = 0;
pub const PROPERTY_SET: u32 = 1;
pub const MDIM_VOLUME: u32 = 2;
pub const TIFF_IFD: u32 = 10;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Kind {
    /// a value of `size` bytes (a text, a number, an array, a blob)
    Value,
    /// an inline value: `size` is the value, and the field has no data
    Inline,
    Volume,
    PropertySet,
    MdimVolume,
    /// a TIFF IFD's region (type 10), or an extended field of a type the table lacks
    TiffIfd,
}

#[derive(Clone, Debug)]
pub struct Field {
    pub offset: u64,
    /// the header's length: 16, or 20 with a second tag
    pub header: u64,
    pub ftype: u32,
    pub real: u32,
    pub tag: u32,
    pub tag2: Option<u32>,
    pub next: u32,
    pub size: u32,
    pub kind: Kind,
    /// the data: `(start, end)`; `start == end` for an inline field or one without data
    pub data: (u64, u64),
    /// the region's end
    pub end: u64,
    pub container: Option<Box<Container>>,
}

#[derive(Clone, Debug)]
pub struct Container {
    pub offset: u64,
    pub end: u64,
    pub header_size: u16,
    pub version: u16,
    pub volume_version: i32,
    pub data_offset: i64,
    pub flags: u32,
    pub fields: Vec<Field>,
}

impl Field {
    pub fn is_inline(&self) -> bool {
        self.kind == Kind::Inline
    }
    pub fn data_len(&self) -> u64 {
        self.data.1 - self.data.0
    }
    /// The container a volume, property set or mdim volume holds.
    pub fn child(&self) -> Option<&Container> {
        self.container.as_deref()
    }
}

impl Container {
    pub fn count(&self) -> u32 {
        self.flags & 0x0fff_ffff
    }
    /// The first field of `tag`.
    pub fn field(&self, tag: u32) -> Option<&Field> {
        self.fields.iter().find(|f| f.tag == tag)
    }
    /// Every field of `tag`.
    pub fn fields_of(&self, tag: u32) -> impl Iterator<Item = &Field> {
        self.fields.iter().filter(move |f| f.tag == tag)
    }
    /// The container the first extended field of `tag` holds.
    pub fn volume(&self, tag: u32) -> Option<&Container> {
        self.field(tag).and_then(|f| f.child())
    }
}

/// Parses the container at `fp` with the region end `end`, checking each container and field
/// against the schema (§9.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#91-the-vsi-file>).
pub fn parse(rules: &Rules, b: &[u8], fp: u64, end: u64, depth: u32) -> Res<Container> {
    let fits = fp.checked_add(CONTAINER_HEADER).is_some_and(|e| e <= end && end <= b.len() as u64);
    let (header_size, version, volume_version, data_offset, flags) = if fits {
        let h = &b[fp as usize..(fp + CONTAINER_HEADER) as usize];
        (u16::le_at(h, 0), u16::le_at(h, 2), i32::le_at(h, 4), i64::le_at(h, 8), u32::le_at(h, 16))
    } else {
        (0, 0, 0, 0, 0)
    };
    let count = flags & 0x0fff_ffff;
    rules.check(
        "container",
        &mut rules::env([
            ("offset", num(fp as f64)),
            ("end", num(end as f64)),
            ("header_size", num(header_size as f64)),
            ("version", num(version as f64)),
            ("data_offset", num(data_offset as f64)),
            ("count", num(count as f64)),
            ("depth", num(depth as f64)),
        ]),
    )?;
    let mut c = Container { offset: fp, end, header_size, version, volume_version, data_offset, flags, fields: Vec::with_capacity(count as usize) };
    let mut p = fp + CONTAINER_HEADER;
    for index in 0..count {
        let have = p + 16 <= end;
        let (ftype, tag, next, size) = if have {
            let h = &b[p as usize..p as usize + 16];
            (u32::le_at(h, 0), u32::le_at(h, 4), u32::le_at(h, 8), u32::le_at(h, 12))
        } else {
            (0, 0, 0, 0)
        };
        let extra = ftype & (1 << 27) != 0;
        let extended = ftype & (1 << 28) != 0;
        let inline = ftype & (1 << 30) != 0;
        let real = ftype & 0x00ff_ffff;
        let header = if extra { 20 } else { 16 };
        let q = p + header;
        let tag2 = (extra && q <= end).then(|| u32::le_at(b, p as usize + 16));
        let region_end = if next == 0 { end } else { fp.saturating_add(next as u64) };
        // where the data ends (§2.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#21-containers-and-fields>)
        let data_end = if inline {
            q
        } else if extra && size > 0 {
            fp + 20 + size as u64
        } else {
            q + size as u64
        };
        let kind = if inline {
            Kind::Inline
        } else if extended {
            match real {
                VOLUME => Kind::Volume,
                PROPERTY_SET => Kind::PropertySet,
                MDIM_VOLUME => Kind::MdimVolume,
                _ => Kind::TiffIfd,
            }
        } else {
            Kind::Value
        };
        rules.check(
            "field",
            &mut rules::env([
                ("offset", num(p as f64)),
                ("container", num(fp as f64)),
                ("end", num(end as f64)),
                ("index", num(index as f64)),
                ("count", num(count as f64)),
                ("type", num(real as f64)),
                ("tag", num(tag as f64)),
                ("next", num(next as f64)),
                ("size", num(size as f64)),
                ("extra", flag(extra)),
                ("extended", flag(extended)),
                ("inline", flag(inline)),
                ("data_end", num(data_end as f64)),
            ]),
        )?;
        let mut f = Field { offset: p, header, ftype, real, tag, tag2, next, size, kind, data: (q, data_end), end: region_end, container: None };
        match kind {
            Kind::Volume => {
                // one container fills the volume's data (§2.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#21-containers-and-fields>)
                f.container = Some(Box::new(parse(rules, b, q, data_end, depth + 1)?));
            }
            Kind::PropertySet | Kind::MdimVolume => {
                f.data = (q, region_end);
                f.container = Some(Box::new(parse(rules, b, q, region_end, depth + 1)?));
            }
            _ => {}
        }
        c.fields.push(f);
        p = region_end;
    }
    Ok(c)
}

// ---- values

/// The IR type of a value field of `real` type and `n` bytes: the table's base type, as an
/// array when `n` is a multiple of its size, else `bytes[n]`.
pub fn value_type(rules: &Rules, real: u32, n: u64) -> String {
    let base = rules.table("field_types").get(real.to_string()).and_then(|t| t["type"].as_str());
    match base {
        Some("utf16") => format!("utf16[{n}]"),
        Some(t) => {
            let w = match t.trim_start_matches('<') {
                "u1" | "i1" => 1,
                "u2" | "i2" => 2,
                "u4" | "i4" | "f4" => 4,
                _ => 8,
            };
            if n == w {
                t.to_string()
            } else if n > 0 && n % w == 0 {
                format!("{t}[{}]", n / w)
            } else {
                format!("bytes[{n}]")
            }
        }
        None => format!("bytes[{n}]"),
    }
}

/// A UTF-16LE text value as JSON: the code units up to the first NUL, decoded (a lone
/// surrogate as U+FFFD, as Bio-Formats' decoder does).
fn utf16_json(d: &[u8]) -> J {
    let units: Vec<u16> = d.chunks_exact(2).map(|c| u16::le_at(c, 0)).collect();
    let n = units.iter().position(|&u| u == 0).unwrap_or(units.len());
    J::String(String::from_utf16_lossy(&units[..n]))
}

fn int_json(v: i128) -> J {
    const MAX: i128 = (1 << 53) - 1;
    if (-MAX..=MAX).contains(&v) { json!(v as i64) } else { J::String(v.to_string()) }
}

/// A value field's data as JSON (§6.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#61-the-root>): numbers by the
/// field type (an array when the data holds several), text decoded from UTF-16LE, a boolean,
/// and opaque bytes as base64 up to 1024 bytes, else `{"bytes": n}`.
pub fn value_json(rules: &Rules, real: u32, d: &[u8]) -> J {
    let base = rules.table("field_types").get(real.to_string()).and_then(|t| t["type"].as_str());
    let Some(t) = base else { return opaque(d) };
    if t == "utf16" {
        return if d.len() % 2 == 0 { utf16_json(d) } else { opaque(d) };
    }
    if real == 12 {
        return if d.len() == 1 { json!(d[0] != 0) } else { opaque(d) };
    }
    let code = t.trim_start_matches('<');
    let w = match code {
        "u1" | "i1" => 1,
        "u2" | "i2" => 2,
        "u4" | "i4" | "f4" => 4,
        _ => 8,
    };
    if d.is_empty() || d.len() % w != 0 {
        return opaque(d);
    }
    let one = |c: &[u8]| -> J {
        match code {
            "u1" => json!(c[0]),
            "i1" => json!(c[0] as i8),
            "u2" => json!(u16::le_at(c, 0)),
            "i2" => json!(i16::le_at(c, 0)),
            "u4" => json!(u32::le_at(c, 0)),
            "i4" => json!(i32::le_at(c, 0)),
            "u8" => int_json(u64::le_at(c, 0) as i128),
            "i8" => int_json(i64::le_at(c, 0) as i128),
            "f4" => float_json(f32::le_at(c, 0) as f64),
            _ => float_json(f64::le_at(c, 0)),
        }
    };
    if d.len() == w {
        return one(d);
    }
    if d.len() > MAX_JSON_BYTES {
        return json!({"bytes": d.len()});
    }
    J::Array(d.chunks_exact(w).map(one).collect())
}

fn float_json(v: f64) -> J {
    if v.is_nan() {
        J::String("NaN".into())
    } else if v.is_infinite() {
        J::String(if v > 0.0 { "Infinity".into() } else { "-Infinity".into() })
    } else {
        json!(v)
    }
}

fn opaque(d: &[u8]) -> J {
    if d.len() > MAX_JSON_BYTES { json!({"bytes": d.len()}) } else { J::String(crate::mirror::base64(d)) }
}

/// The tree as JSON (§6.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#61-the-root>):
/// a container is `{"fields": [...]}`; a field has `tag`, `tag2` when it has one, `type`,
/// `name` when the table names its tag, and then `value` (a value field's data, or an inline
/// field's number, with `"inline": true`), `fields` (a volume's, property set's or mdim
/// volume's container) or `ifd` (a TIFF IFD field: the IFD index its second tag names, and
/// the length of its data region).
pub fn container_json(rules: &Rules, b: &[u8], c: &Container) -> J {
    let fields: Vec<J> = c.fields.iter().map(|f| field_json(rules, b, f)).collect();
    json!({"fields": fields})
}

fn field_json(rules: &Rules, b: &[u8], f: &Field) -> J {
    let mut m = serde_json::Map::new();
    m.insert("tag".into(), json!(f.tag));
    if let Some(t2) = f.tag2 {
        m.insert("tag2".into(), json!(t2));
    }
    m.insert("type".into(), json!(f.real));
    if let Some(n) = rules.table("tag_names").get(f.tag.to_string()).and_then(|v| v.as_str()) {
        m.insert("name".into(), json!(n));
    }
    match f.kind {
        Kind::Inline => {
            m.insert("inline".into(), json!(true));
            m.insert("value".into(), json!(f.size));
        }
        Kind::Value => {
            m.insert("value".into(), value_json(rules, f.real, &b[f.data.0 as usize..f.data.1 as usize]));
        }
        Kind::Volume | Kind::PropertySet | Kind::MdimVolume => {
            m.insert("fields".into(), f.child().map(|c| container_json(rules, b, c)["fields"].clone()).unwrap_or(J::Array(vec![])));
        }
        Kind::TiffIfd => {
            let mut v = serde_json::Map::new();
            if let Some(t2) = f.tag2 {
                v.insert("index".into(), json!(t2));
            }
            v.insert("bytes".into(), json!(f.data_len()));
            m.insert("ifd".into(), J::Object(v));
        }
    }
    J::Object(m)
}

// ---- the IR

/// The regions of the tree that hold TIFF structures: a TIFF IFD field's data, and the rest of
/// any field's region after its data (its IFD elements go under the field's struct).
pub struct TiffRegion {
    pub start: u64,
    pub end: u64,
    /// the field's struct element
    pub parent: u32,
}

/// Emits a container's elements under `parent` (an object, or a field's struct) as the
/// struct `name`, and collects the TIFF regions.
pub fn emit(rules: &Rules, ir: &mut Ir, b: &[u8], c: &Container, parent: u32, name: &str, regions: &mut Vec<TiffRegion>) -> Res<u32> {
    let s = ir.struct_(parent, name, NO_INDEX, 0, Some((c.offset, c.end - c.offset)))?;
    let h = &b[c.offset as usize..(c.offset + CONTAINER_HEADER) as usize];
    ir.value(s, "header", NO_INDEX, CONTAINER_TYPE, 0, (c.offset, CONTAINER_HEADER), Some(h))?;
    if c.fields.is_empty() && c.end > c.offset + CONTAINER_HEADER {
        regions.push(TiffRegion { start: c.offset + CONTAINER_HEADER, end: c.end, parent: s });
    }
    for (k, f) in c.fields.iter().enumerate() {
        let fs = ir.struct_(s, "fields/", k as u64, 0, Some((f.offset, f.end - f.offset)))?;
        let hb = &b[f.offset as usize..(f.offset + f.header) as usize];
        ir.value(fs, "header", NO_INDEX, if f.tag2.is_some() { FIELD_TYPE2 } else { FIELD_TYPE }, 0, (f.offset, f.header), Some(hb))?;
        match f.kind {
            Kind::Value if f.data.1 > f.data.0 => {
                let n = f.data_len();
                let ty = value_type(rules, f.real, n);
                let d = &b[f.data.0 as usize..f.data.1 as usize];
                ir.value(fs, "value", NO_INDEX, &ty, 0, (f.data.0, n), Some(d))?;
            }
            Kind::Volume | Kind::PropertySet | Kind::MdimVolume => {
                if let Some(child) = f.child() {
                    emit(rules, ir, b, child, fs, "container", regions)?;
                }
            }
            Kind::TiffIfd if f.data.1 > f.data.0 => {
                regions.push(TiffRegion { start: f.data.0, end: f.data.1, parent: fs });
            }
            _ => {}
        }
        if f.end > f.data.1 && f.kind != Kind::PropertySet && f.kind != Kind::MdimVolume {
            regions.push(TiffRegion { start: f.data.1, end: f.end, parent: fs });
        }
    }
    Ok(s)
}

// ---- queries

/// The first value field of `tag` in `c` as JSON (its data decoded), if any.
pub fn value_of(rules: &Rules, b: &[u8], c: &Container, tag: u32) -> Option<J> {
    let f = c.fields.iter().find(|f| f.tag == tag && matches!(f.kind, Kind::Value | Kind::Inline))?;
    Some(if f.kind == Kind::Inline { json!(f.size) } else { value_json(rules, f.real, &b[f.data.0 as usize..f.data.1 as usize]) })
}

/// A number from the first value field of `tag` in `c`.
pub fn number_of(rules: &Rules, b: &[u8], c: &Container, tag: u32) -> Option<f64> {
    value_of(rules, b, c, tag)?.as_f64()
}

/// The numbers of the first value field of `tag` in `c` (one number as a list of one).
pub fn numbers_of(rules: &Rules, b: &[u8], c: &Container, tag: u32) -> Option<Vec<f64>> {
    match value_of(rules, b, c, tag)? {
        J::Number(n) => Some(vec![n.as_f64()?]),
        J::Array(a) => a.iter().map(|v| v.as_f64()).collect(),
        _ => None,
    }
}

/// The text of the first value field of `tag` in `c`.
pub fn text_of(rules: &Rules, b: &[u8], c: &Container, tag: u32) -> Option<String> {
    value_of(rules, b, c, tag)?.as_str().map(str::to_string)
}

/// The `Value` (tag 268435458) of an mdim volume field of `tag` in `c`, and its `Units` text.
pub fn mdim_value(rules: &Rules, b: &[u8], c: &Container, tag: u32) -> Option<(J, Option<String>)> {
    let v = c.fields.iter().find(|f| f.tag == tag && f.kind == Kind::MdimVolume)?.child()?;
    let value = value_of(rules, b, v, 268435458)?;
    Some((value, text_of(rules, b, v, 268435456)))
}

