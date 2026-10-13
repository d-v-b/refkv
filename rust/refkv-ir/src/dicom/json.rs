//! The root's source metadata of a DICOM file (spec/virtualize/dicom.md §5
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/dicom.md#5-source-metadata>): its File
//! Meta Information and dataset in the DICOM JSON Model (PS3.18 §F.2), without the members
//! that would make the root large, and the paths the root names. Revision 25 wrote the moved
//! members and the arrays the paths name under `vzip_source` (now `refkv_source`); the IR mirror holds all of it
//! now, and the root is unchanged.

use super::elements::*;
use crate::mirror::base64;
use crate::out::{json_size, js_string_size, MAX_CHUNK, SOURCE_NODE};
use serde_json::{json, Map, Value as J};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::OnceLock;

const MULTI: [&str; 13] = ["AE", "AS", "CS", "DA", "DS", "DT", "IS", "LO", "PN", "SH", "TM", "UC", "UI"];
const LEADING: [&str; 12] = ["AE", "AS", "CS", "DA", "DS", "DT", "IS", "LO", "PN", "SH", "TM", "UI"];
const CHARSET: u32 = 0x0008_0005;
const MULTIBYTE: [&[u8]; 6] = [b"GB18030", b"GBK", b"ISO 2022 IR 58", b"ISO 2022 IR 87", b"ISO 2022 IR 149", b"ISO 2022 IR 159"];
const CHARSET_SPLIT: [&str; 4] = ["LO", "PN", "SH", "UC"];
const PN_GROUPS: [&str; 3] = ["Alphabetic", "Ideographic", "Phonetic"];
/// the most bytes of a binary value, or values of a numeric, DS or IS one, kept as JSON
const INLINE: u64 = 64;
/// the longest text value kept as JSON
pub const MAX_TEXT: u64 = 1 << 16;
const MAX_MEMBER: usize = 1 << 14;
const MAX_ROOT: usize = 1 << 16;
pub const PADDING: u32 = 0xFFFC_FFFC;
pub const EOT_TAGS: [u32; 2] = [0x7FE0_0001, 0x7FE0_0002];
const PER_FRAME: &str = "52009230";
const GATHER_ITEMS: usize = 64;
const MAX_PATHS: usize = 1024;
const MAX_STRUCTURES: usize = 1 << 16;
const MAX_COST: u64 = 16;
const AS_BYTES: [&str; 3] = ["InlineBinary", "BulkDataURI", "Gathered"];
const MAX_SAFE: i128 = (1 << 53) - 1;
const MAX_EXPONENT_DIGITS: usize = 5;
const MAX_INTEGER_DIGITS: usize = 16;

pub fn is_text(vr: &str) -> bool {
    MULTI.contains(&vr) || matches!(vr, "LT" | "ST" | "UT" | "UR")
}

/// (data type, value size) of a binary VR.
pub fn binary(vr: &str) -> Option<(&'static str, u64)> {
    Some(match vr {
        "OB" | "UN" => ("uint8", 1),
        "OW" => ("uint16", 2),
        "OF" => ("float32", 4),
        "OD" => ("float64", 8),
        "OL" => ("uint32", 4),
        "OV" => ("uint64", 8),
        _ => return None,
    })
}

/// (data type, value size) of a numeric VR.
pub fn numeric(vr: &str) -> Option<(&'static str, u64)> {
    Some(match vr {
        "FL" => ("float32", 4),
        "FD" => ("float64", 8),
        "SL" => ("int32", 4),
        "SS" => ("int16", 2),
        "UL" => ("uint32", 4),
        "US" => ("uint16", 2),
        "SV" => ("int64", 8),
        "UV" => ("uint64", 8),
        _ => return None,
    })
}

/// The size of one value of an AT or numeric VR.
pub fn value_size(vr: &str) -> Option<u64> {
    if vr == "AT" { Some(4) } else { numeric(vr).map(|x| x.1) }
}

// ---- the data dictionary (spec/virtualize/dicom.md §5
// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/dicom.md#5-source-metadata>)

pub static DICTIONARY: &str = include_str!("../../schema/dicom_dictionary.json");

struct Dictionary {
    tags: HashMap<u32, &'static str>,
    masks: Vec<(String, &'static str)>,
}

fn vr_static(s: &str) -> &'static str {
    const ALL: [&str; 34] = [
        "AE", "AS", "AT", "CS", "DA", "DS", "DT", "FD", "FL", "IS", "LO", "LT", "OB", "OD", "OF", "OL", "OV", "OW", "PN", "SH", "SL", "SQ",
        "SS", "ST", "SV", "TM", "UC", "UI", "UL", "UN", "UR", "US", "UT", "UV",
    ];
    ALL.iter().find(|v| **v == s).copied().unwrap_or("UN")
}

fn dictionary() -> &'static Dictionary {
    static D: OnceLock<Dictionary> = OnceLock::new();
    D.get_or_init(|| {
        let j: J = serde_json::from_str(DICTIONARY).expect("the DICOM dictionary is JSON");
        let mut tags = HashMap::new();
        for (vr, s) in j["tags"].as_object().unwrap() {
            let s = s.as_str().unwrap();
            for k in (0..s.len()).step_by(8) {
                tags.insert(u32::from_str_radix(&s[k..k + 8], 16).unwrap(), vr_static(vr));
            }
        }
        let masks = j["masks"].as_object().unwrap().iter().map(|(m, v)| (m.clone(), vr_static(v.as_str().unwrap()))).collect();
        Dictionary { tags, masks }
    })
}

/// The VR the data dictionary gives a tag, or None.
pub fn dictionary_vr(tag: u32) -> Option<&'static str> {
    if tag >> 16 & 1 == 1 {
        let e = tag & 0xFFFF;
        return if (0x10..=0xFF).contains(&e) { Some("LO") } else { None };
    }
    let d = dictionary();
    if let Some(v) = d.tags.get(&tag) {
        return Some(v);
    }
    let key = tag_key(tag);
    d.masks.iter().find(|(m, _)| m.bytes().zip(key.bytes()).all(|(m, k)| m == b'x' || m == k)).map(|x| x.1)
}

pub fn is_multibyte(value: &[u8]) -> bool {
    value.split(|&c| c == b'\\').any(|v| MULTIBYTE.contains(&strip(v, b" \0", b" \0")))
}

/// `b` without leading bytes of `left` and trailing bytes of `right`.
fn strip<'a>(b: &'a [u8], left: &[u8], right: &[u8]) -> &'a [u8] {
    let mut s = 0;
    let mut e = b.len();
    while e > 0 && right.contains(&b[e - 1]) {
        e -= 1;
    }
    while s < e && left.contains(&b[s]) {
        s += 1;
    }
    &b[s..e]
}

// ---- numbers and text (conventions §6
// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#6-source-metadata-as-json>)

pub fn json_float(v: f64) -> J {
    if v.is_nan() {
        json!("NaN")
    } else if v.is_infinite() {
        json!(if v > 0.0 { "Infinity" } else { "-Infinity" })
    } else {
        json!(v)
    }
}

pub fn json_int(v: i128) -> J {
    if v.abs() <= MAX_SAFE {
        if v < 0 { json!(v as i64) } else { json!(v as u64) }
    } else {
        json!(v.to_string())
    }
}

/// UTF-8 when valid, else {"latin1": ...}.
pub fn text_json(b: &[u8]) -> J {
    match std::str::from_utf8(b) {
        Ok(s) => json!(s),
        Err(_) => json!({"latin1": latin1(b)}),
    }
}

fn latin1(b: &[u8]) -> String {
    b.iter().map(|&c| c as char).collect()
}

/// `[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?`, whole.
pub fn is_decimal(t: &[u8]) -> bool {
    let mut i = 0;
    if i < t.len() && (t[i] == b'+' || t[i] == b'-') {
        i += 1;
    }
    let a = i;
    while i < t.len() && t[i].is_ascii_digit() {
        i += 1;
    }
    let int_digits = i - a;
    let mut frac_digits = 0;
    if i < t.len() && t[i] == b'.' {
        i += 1;
        let f = i;
        while i < t.len() && t[i].is_ascii_digit() {
            i += 1;
        }
        frac_digits = i - f;
    }
    if int_digits == 0 && frac_digits == 0 {
        return false;
    }
    if i < t.len() && (t[i] == b'e' || t[i] == b'E') {
        i += 1;
        if i < t.len() && (t[i] == b'+' || t[i] == b'-') {
            i += 1;
        }
        let e = i;
        while i < t.len() && t[i].is_ascii_digit() {
            i += 1;
        }
        if i == e {
            return false;
        }
    }
    i == t.len()
}

/// `[+-]?[0-9]+`, whole.
pub fn is_integer(t: &[u8]) -> bool {
    let d = if t.first().is_some_and(|&c| c == b'+' || c == b'-') { &t[1..] } else { t };
    !d.is_empty() && d.iter().all(u8::is_ascii_digit)
}

/// A decimal string's value, normalized: (negative, digits without leading or trailing
/// zeros, exponent of the last digit); zero is (false, "", 0). None for a value that is
/// not zero and whose exponent has more than 5 digits. `t` is a decimal (or Rust's `{:e}`).
fn decimal_key(t: &str) -> Option<(bool, String, i64)> {
    let b = t.as_bytes();
    let mut i = 0;
    let negative = b.first() == Some(&b'-');
    if i < b.len() && (b[i] == b'+' || b[i] == b'-') {
        i += 1;
    }
    let a = i;
    while i < b.len() && b[i].is_ascii_digit() {
        i += 1;
    }
    let int = &t[a..i];
    let mut frac = "";
    if i < b.len() && b[i] == b'.' {
        let f = i + 1;
        i = f;
        while i < b.len() && b[i].is_ascii_digit() {
            i += 1;
        }
        frac = &t[f..i];
    }
    let mut exp_negative = false;
    let mut exp = "";
    if i < b.len() && (b[i] == b'e' || b[i] == b'E') {
        i += 1;
        if i < b.len() && (b[i] == b'+' || b[i] == b'-') {
            exp_negative = b[i] == b'-';
            i += 1;
        }
        exp = &t[i..];
    }
    let joined = format!("{int}{frac}");
    let digits = joined.trim_start_matches('0');
    if digits.is_empty() {
        return Some((false, String::new(), 0));
    }
    let exp_digits = exp.trim_start_matches('0');
    if exp_digits.len() > MAX_EXPONENT_DIGITS {
        return None;
    }
    let stripped = digits.trim_end_matches('0');
    let e: i64 = if exp_digits.is_empty() { 0 } else { exp_digits.parse().unwrap() };
    let exponent = if exp_negative { -e } else { e } - frac.len() as i64 + (digits.len() - stripped.len()) as i64;
    Some((negative, stripped.to_string(), exponent))
}

/// A DS value as a number when that number is the text's decimal value exactly.
pub fn ds_number(t: &str) -> Option<f64> {
    if !is_decimal(t.as_bytes()) {
        return None;
    }
    let key = decimal_key(t)?;
    let v: f64 = t.parse().ok()?;
    if !v.is_finite() {
        return None;
    }
    // the shortest digits that round to v, as Python's repr and ECMAScript write them
    if decimal_key(&format!("{v:e}")) != Some(key) {
        return None;
    }
    Some(v)
}

/// An IS value of the form [+-]?[0-9]+: a number up to 2^53 - 1, else the string of its digits.
fn is_number(t: &str) -> J {
    let negative = t.starts_with('-');
    let digits = t.trim_start_matches(['+', '-']).trim_start_matches('0');
    if digits.len() > MAX_INTEGER_DIGITS {
        return json!(format!("{}{digits}", if negative { "-" } else { "" }));
    }
    let v: i128 = if digits.is_empty() { 0 } else { digits.parse().unwrap() };
    json_int(if negative { -v } else { v })
}

/// One string value: trimmed, decoded, then typed (PS3.18 §F.2.3).
fn text_value(vr: &str, b: &[u8]) -> J {
    let b = strip(b, if LEADING.contains(&vr) { b" " } else { b"" }, b" \0");
    if b.is_empty() {
        return J::Null;
    }
    let Ok(t) = std::str::from_utf8(b) else {
        return json!({"latin1": latin1(b)});
    };
    match vr {
        "PN" => {
            let groups: Map<String, J> =
                PN_GROUPS.iter().zip(t.splitn(3, '=')).filter(|(_, g)| !g.is_empty()).map(|(k, g)| (k.to_string(), json!(g))).collect();
            if groups.is_empty() { J::Null } else { J::Object(groups) }
        }
        "DS" => ds_number(t).map(|v| json!(v)).unwrap_or_else(|| json!(t)),
        "IS" if is_integer(b) => is_number(t),
        _ => json!(t),
    }
}

/// A DS or IS value's typed values when every value is a number exactly: (data type, bytes).
fn typed_values(vr: &str, data: &[u8]) -> Option<(&'static str, u64)> {
    let mut n = 0u64;
    for p in data.split(|&c| c == b'\\') {
        let p = strip(p, b" ", b" \0");
        if !p.is_ascii() {
            return None;
        }
        let t = std::str::from_utf8(p).ok()?;
        if vr == "DS" {
            ds_number(t)?;
        } else {
            if !is_integer(p) || t.trim_start_matches(['+', '-']).trim_start_matches('0').len() > 19 {
                return None;
            }
            let v: i128 = t.parse().ok()?;
            if v < -(1i128 << 63) || v >= 1i128 << 63 {
                return None;
            }
        }
        n += 1;
    }
    Some((if vr == "DS" { "float64" } else { "int64" }, 8 * n))
}

/// An element of defined length with VR `vr` and value `data` (never SQ).
pub fn value_json(vr: &str, data: &[u8], little: bool, multibyte: bool) -> J {
    if data.is_empty() {
        return json!({"vr": vr});
    }
    if is_text(vr) {
        if multibyte && CHARSET_SPLIT.contains(&vr) {
            return json!({"vr": vr, "InlineBinary": base64(data)});
        }
        let values: Vec<J> = if MULTI.contains(&vr) {
            data.split(|&c| c == b'\\').map(|p| text_value(vr, p)).collect()
        } else {
            vec![text_value(vr, data)]
        };
        return json!({"vr": vr, "Value": values});
    }
    let size = value_size(vr);
    if size.is_some_and(|s| data.len() as u64 % s != 0) {
        return json!({"vr": vr, "InlineBinary": base64(data)});
    }
    let u16_at = |k: usize| if little { u16::from_le_bytes([data[k], data[k + 1]]) } else { u16::from_be_bytes([data[k], data[k + 1]]) };
    if vr == "AT" {
        let v: Vec<J> = (0..data.len() / 4).map(|i| json!(format!("{:04X}{:04X}", u16_at(4 * i), u16_at(4 * i + 2)))).collect();
        return json!({"vr": vr, "Value": v});
    }
    if let Some((_, s)) = numeric(vr) {
        let s = s as usize;
        let values: Vec<J> = data
            .chunks(s)
            .map(|c| {
                let mut b = [0u8; 8];
                if little {
                    b[..s].copy_from_slice(c);
                } else {
                    for (k, &x) in c.iter().rev().enumerate() {
                        b[k] = x;
                    }
                }
                let u = u64::from_le_bytes(b);
                match vr {
                    "FL" => json_float(f32::from_bits(u as u32) as f64),
                    "FD" => json_float(f64::from_bits(u)),
                    "SL" => json_int(u as u32 as i32 as i128),
                    "SS" => json_int(u as u16 as i16 as i128),
                    "SV" => json_int(u as i64 as i128),
                    _ => json_int(u as i128),
                }
            })
            .collect();
        return json!({"vr": vr, "Value": values});
    }
    json!({"vr": vr, "InlineBinary": base64(data)})
}

/// A dataset's members sorted by name.
fn sorted(out: Map<String, J>) -> Map<String, J> {
    let mut v: Vec<(String, J)> = out.into_iter().collect();
    v.sort_by(|a, b| a.0.cmp(&b.0));
    v.into_iter().collect()
}

/// A value kept as an array in revision 25 (a BulkDataURI's, or a gathered one): what the
/// gathered bounds and arrays depend on.
#[derive(Clone)]
struct Extent {
    rest: String,
    n: u64,
    data_type: &'static str,
    little: bool,
    /// a gathered DS or IS value's typed values: (data type, bytes)
    values: Option<(&'static str, u64)>,
    dropped: bool,
}

/// The group lengths of one dataset: each is layout, and dropped, when it is the only
/// element of its tag in the dataset, of an even group, UL (or implicit) with 4 bytes,
/// and its value is the length of the run of elements of its group that follows it.
#[derive(Default)]
struct GroupLengths {
    /// group, key, value, where the run starts, the value's gathered extent
    open: Option<(u32, String, Option<u32>, u64, Option<usize>)>,
    closed: Vec<(String, Option<u32>, u64, Option<usize>)>,
}

impl GroupLengths {
    fn step(&mut self, tag: u32, pos: u64) {
        if self.open.as_ref().is_some_and(|o| tag >> 16 != o.0) {
            self.close(pos);
        }
    }
    fn close(&mut self, pos: u64) {
        if let Some((_, key, value, start, extent)) = self.open.take() {
            self.closed.push((key, value, pos - start, extent));
        }
    }
    fn begin(&mut self, el: &El, enc: Enc, b: &Bytes, extent: Option<usize>) -> R<()> {
        let mut value = None;
        if el.tag >> 16 & 1 == 0 && el.length == 4 && (!enc.explicit || el.vr_is("UL")) {
            value = Some(b.u32(el.value, el.little)?);
        }
        self.open = Some((el.tag >> 16, tag_key(el.tag), value, el.end(), extent));
        Ok(())
    }
    fn finish(&mut self, pos: u64, out: &mut Map<String, J>, extents: &mut [Extent]) {
        self.close(pos);
        let duplicated: HashSet<String> = out
            .get("duplicates")
            .and_then(|d| d.as_array())
            .map(|d| d.iter().filter_map(|x| x.as_object()).flat_map(|o| o.keys().cloned()).collect())
            .unwrap_or_default();
        for (key, value, length, extent) in self.closed.drain(..) {
            if value.is_some_and(|v| v as u64 == length) && !duplicated.contains(&key) {
                out.swap_remove(&key); // the members are sorted after
                if let Some(e) = extent {
                    extents[e].dropped = true;
                }
            }
        }
    }
}

/// What the profile found that the root's source metadata records.
pub struct Pixel {
    /// where the top-level Pixel Data ends
    pub end: u64,
    /// native pixel data past the frames, unless one 00 byte
    pub extra: Option<(u64, u64)>,
    /// an Extended Offset Table skips bytes: whether the frames' item headers are among them
    pub unreferenced: Option<bool>,
    /// the profile read the frames from the Extended Offset Table
    pub eot_layout: bool,
    /// a frame has more than one fragment
    pub fragments: bool,
    /// the Basic Offset Table is not empty
    pub offset_table: bool,
}

/// Translates every element (the profile has accepted the file's structure).
pub struct Translator<'a> {
    b: &'a Bytes,
    extents: Vec<Extent>,
    omitted: HashSet<String>,
    within: Option<String>,
}

type Obj = Map<String, J>;

impl<'a> Translator<'a> {
    pub fn new(b: &'a Bytes) -> Self {
        Translator { b, extents: Vec::new(), omitted: HashSet::new(), within: None }
    }

    fn read(&self, o: u64, n: u64) -> R<&'a [u8]> {
        self.b.get(o, n)
    }

    fn array(&mut self, vr: &str, path: &str, n: u64, data_type: &'static str, little: bool) -> J {
        self.extents.push(Extent { rest: String::new(), n, data_type, little, values: None, dropped: false });
        json!({"vr": vr, "BulkDataURI": format!("{SOURCE_NODE}/{path}")})
    }

    fn gather(&mut self, vr: &str, el: &El, path: &str) -> R<J> {
        let within = self.within.as_deref().unwrap();
        let rel = &path[within.len() + 1..];
        let (_, rest) = rel.split_once('/').unwrap();
        let n = el.length as u64;
        let (mut data_type, size) = binary(vr).or_else(|| numeric(vr)).unwrap_or(("uint8", 1));
        if n % size != 0 {
            data_type = "uint8";
        }
        let values = if (vr == "DS" || vr == "IS") && n <= MAX_TEXT { typed_values(vr, self.read(el.value, n)?) } else { None };
        self.extents.push(Extent {
            rest: rest.to_string(),
            n,
            data_type,
            little: el.little || data_type == "uint8",
            values,
            dropped: false,
        });
        Ok(json!({"vr": vr, "Gathered": true}))
    }

    /// An element of defined length with VR `vr`: not a sequence, or the bytes of one
    /// that breaks a rule.
    fn member(&mut self, vr: &str, el: &El, path: &str, multibyte: bool) -> R<J> {
        let n = el.length as u64;
        if n == 0 {
            return Ok(json!({"vr": vr}));
        }
        if self.within.is_some() {
            return self.gather(vr, el, path);
        }
        let size = value_size(vr);
        if binary(vr).is_some() || vr == "SQ" || size.is_some_and(|s| n % s != 0) {
            if n <= INLINE {
                return Ok(json!({"vr": vr, "InlineBinary": base64(self.read(el.value, n)?)}));
            }
            let (mut dtype, s) = binary(vr).unwrap_or(("uint8", 1));
            if n % s != 0 {
                dtype = "uint8";
            }
            return Ok(self.array(vr, path, n, dtype, el.little));
        }
        if let Some((dtype, s)) = numeric(vr) {
            if n / s > INLINE {
                return Ok(self.array(vr, path, n, dtype, el.little));
            }
        }
        if is_text(vr) && n > MAX_TEXT {
            return Ok(self.array(vr, path, n, "uint8", true));
        }
        let data = self.read(el.value, n)?;
        if (vr == "DS" || vr == "IS") && data.iter().filter(|&&c| c == b'\\').count() as u64 >= INLINE {
            return Ok(self.array(vr, path, n, "uint8", true));
        }
        Ok(value_json(vr, data, el.little, multibyte))
    }

    /// A sequence of defined length; if it breaks a rule, its bytes.
    fn sequence_or_bytes(&mut self, vr_if_bytes: &str, el: &El, enc: Enc, end: u64, depth: u32, path: &str, multibyte: bool) -> R<J> {
        let mark = self.extents.len();
        let within = self.within.clone();
        match self.items(el, Some(el.end()), end, enc, depth, path, multibyte, vr_if_bytes) {
            Ok((a, _)) => Ok(a),
            Err(Fail::Reject(_)) => {
                self.extents.truncate(mark);
                self.within = within;
                self.member(vr_if_bytes, el, path, multibyte)
            }
            Err(m) => Err(m),
        }
    }

    fn defined(&mut self, el: &El, enc: Enc, end: u64, depth: u32, path: &str, multibyte: bool) -> R<J> {
        if !enc.explicit || el.vr_is("UN") {
            let mut el = *el;
            if el.vr_is("UN") {
                el.little = true;
            }
            let vr = dictionary_vr(el.tag).unwrap_or("UN");
            if vr == "SQ" {
                return self.sequence_or_bytes("UN", &el, if enc.explicit { IMPLICIT_LE } else { enc }, end, depth, path, multibyte);
            }
            return self.member(vr, &el, path, multibyte);
        }
        if el.vr_is("SQ") {
            return self.sequence_or_bytes("SQ", el, enc, end, depth, path, multibyte);
        }
        let vr = el.vr_text();
        self.member(&vr, el, path, multibyte)
    }

    /// Translates `el` into `out` (at path `base`, within `end`); where the next element starts.
    #[allow(clippy::too_many_arguments)]
    fn element(&mut self, el: &El, enc: Enc, out: &mut Obj, end: u64, depth: u32, base: &str, multibyte: bool, top: bool) -> R<u64> {
        let key = tag_key(el.tag);
        let duplicate = out.contains_key(&key) || (top && self.omitted.contains(&key));
        let path = if duplicate {
            let k = out.get("duplicates").and_then(|d| d.as_array()).map_or(0, |d| d.len());
            format!("{base}/duplicates/{k}/{key}")
        } else {
            format!("{base}/{key}")
        };
        let (mut attribute, pos) = if el.undefined() {
            if enc.explicit && !el.vr_is("SQ") && !el.vr_is("UN") && !(el.tag == PIXEL_DATA && (el.vr_is("OB") || el.vr_is("OW"))) {
                return reject(format!("undefined length with VR {}", el.vr_text()));
            }
            if el.tag == PIXEL_DATA {
                let (fragments, pos) = Walker { b: self.b }.fragments(el.value, end, enc.little)?;
                let vr = el.vr.map(|v| vr_str(&v).to_string()).unwrap_or_else(|| "OB".into());
                let mut a = json!({"vr": vr});
                if !fragments.is_empty() {
                    a["BulkDataURI"] = json!(format!("{SOURCE_NODE}/{path}"));
                }
                (a, pos)
            } else {
                let inner = if el.vr_is("UN") { IMPLICIT_LE } else { enc };
                let vr_if_bytes = if enc.explicit && el.vr_is("SQ") { "SQ" } else { "UN" };
                self.items(el, None, end, inner, depth, &path, multibyte, vr_if_bytes)?
            }
        } else {
            if el.end() > end {
                return reject(format!("the element at {} runs past its container", el.value));
            }
            if el.tag == PADDING {
                return Ok(el.end());
            }
            (self.defined(el, enc, end, depth, &path, multibyte)?, el.end())
        };
        if enc.explicit && !enc.little && el.vr_is("UN") && AS_BYTES.iter().any(|k| attribute.get(*k).is_some()) {
            attribute["LittleEndian"] = json!(true);
        }
        if duplicate {
            let d = out.entry("duplicates").or_insert_with(|| json!([]));
            d.as_array_mut().unwrap().push(json!({ key: attribute }));
        } else {
            out.insert(key, attribute);
        }
        Ok(pos)
    }

    #[allow(clippy::too_many_arguments)]
    fn translate(&mut self, el: &El, enc: Enc, out: &mut Obj, end: u64, depth: u32, base: &str, multibyte: bool, pos: u64, groups: &mut GroupLengths, top: bool) -> R<u64> {
        groups.step(el.tag, pos);
        let key = tag_key(el.tag);
        let first = !out.contains_key(&key);
        let planned = self.extents.len();
        let after = self.element(el, enc, out, end, depth, base, multibyte, top)?;
        if el.tag & 0xFFFF == 0 && !el.undefined() && first && out.contains_key(&key) {
            let extent = (self.extents.len() > planned).then(|| self.extents.len() - 1);
            groups.begin(el, enc, self.b, extent)?;
        }
        Ok(after)
    }

    fn meta(&mut self, start: u64) -> R<Obj> {
        let mut out = Obj::new();
        let mut groups = GroupLengths::default();
        let mut pos = 132;
        while pos < start {
            let el = self.b.header(pos, start, EXPLICIT_LE)?;
            pos = self.translate(&el, EXPLICIT_LE, &mut out, start, 0, "meta", false, pos, &mut groups, false)?;
        }
        groups.finish(pos, &mut out, &mut self.extents);
        Ok(sorted(out))
    }

    /// An item's dataset, under the character set of the dataset that holds it unless its
    /// own Specific Character Set says otherwise.
    #[allow(clippy::too_many_arguments)]
    fn dataset(&mut self, mut pos: u64, end: u64, defined: bool, enc: Enc, depth: u32, base: &str, mut multibyte: bool) -> R<(J, u64)> {
        let mut out = Obj::new();
        let mut groups = GroupLengths::default();
        let mut charset = false;
        loop {
            if defined && pos == end {
                groups.finish(pos, &mut out, &mut self.extents);
                return Ok((J::Object(sorted(out)), pos));
            }
            let el = self.b.header(pos, end, enc)?;
            if el.tag >> 16 == 0xFFFE {
                if el.tag == ITEM_END && !defined && el.length == 0 {
                    groups.finish(pos, &mut out, &mut self.extents);
                    return Ok((J::Object(sorted(out)), el.value));
                }
                return reject(format!("unexpected item tag at {pos}"));
            }
            pos = self.translate(&el, enc, &mut out, end, depth, base, multibyte, pos, &mut groups, false)?;
            if el.tag == CHARSET && !el.undefined() && !charset {
                charset = true;
                multibyte = is_multibyte(self.read(el.value, el.length as u64)?);
            }
        }
    }

    /// A sequence's attribute and where it ends: its items, or, gathered, its structures,
    /// unless past the bounds of one, when it keeps its bytes (`vr_if_bytes`).
    #[allow(clippy::too_many_arguments)]
    fn items(&mut self, el: &El, end: Option<u64>, limit: u64, enc: Enc, depth: u32, path: &str, multibyte: bool, vr_if_bytes: &str) -> R<(J, u64)> {
        if self.within.is_some() {
            let (items, pos) = self.sequence(el.value, end, limit, enc, depth, path, multibyte, None)?;
            return Ok((json!({"vr": "SQ", "Value": items.unwrap()}), pos));
        }
        let mark = self.extents.len();
        if path != format!("dataset/{PER_FRAME}") {
            let (some, pos) = self.sequence(el.value, end, limit, enc, depth, path, multibyte, Some(GATHER_ITEMS))?;
            if let Some(items) = some {
                return Ok((json!({"vr": "SQ", "Value": items}), pos));
            }
            self.extents.truncate(mark);
        }
        self.within = Some(path.to_string());
        let walked = self.sequence(el.value, end, limit, enc, depth, path, multibyte, None);
        self.within = None;
        let (items, pos) = walked?;
        let items = items.unwrap();
        let length = end.unwrap_or(pos - 8) - el.value;
        match self.gathered(items, mark, length) {
            Some(a) => Ok((a, pos)),
            None => {
                self.extents.truncate(mark);
                let mut bytes = *el;
                bytes.length = length as u32;
                Ok((self.member(vr_if_bytes, &bytes, path, multibyte)?, pos))
            }
        }
    }

    /// The attribute of a gathered sequence whose `length` bytes hold `items`; None when it
    /// is past the bounds.
    fn gathered(&mut self, items: Vec<J>, mark: usize, length: u64) -> Option<J> {
        let count = items.len();
        let mut columns: BTreeMap<&str, Vec<&Extent>> = BTreeMap::new();
        for e in &self.extents[mark..] {
            if !e.dropped {
                columns.entry(e.rest.as_str()).or_default().push(e);
            }
        }
        let mut structures: Vec<J> = Vec::new();
        let mut index: HashMap<String, usize> = HashMap::new();
        for item in items {
            let k = serde_json::to_string(&item).unwrap();
            if !index.contains_key(&k) {
                index.insert(k, structures.len());
                structures.push(item);
            }
        }
        let structures = J::Array(structures);
        let cost: u64 = columns.values().map(|m| gathered_cost(m, count as u64)).sum();
        if columns.len() > MAX_PATHS || json_size(&structures) > MAX_STRUCTURES || cost > MAX_COST * length {
            return None;
        }
        self.extents.truncate(mark);
        Some(if count > 0 { json!({"vr": "SQ", "Structures": structures}) } else { json!({"vr": "SQ", "Value": []}) })
    }

    /// A sequence's items and where it ends; with `most`, None at the item after the first `most`.
    #[allow(clippy::too_many_arguments)]
    fn sequence(&mut self, mut pos: u64, end: Option<u64>, limit: u64, enc: Enc, depth: u32, base: &str, multibyte: bool, most: Option<usize>) -> R<(Option<Vec<J>>, u64)> {
        let container = end.unwrap_or(limit);
        let mut items = Vec::new();
        loop {
            if end == Some(pos) {
                return Ok((Some(items), pos));
            }
            let el = self.b.header(pos, container, enc)?;
            if end.is_none() && el.tag == SEQUENCE_END && el.length == 0 {
                return Ok((Some(items), el.value));
            }
            if el.tag != ITEM || depth + 1 > MAX_DEPTH {
                return reject(format!("expected an item at {pos}"));
            }
            if most == Some(items.len()) {
                return Ok((None, pos));
            }
            let path = format!("{base}/{}", items.len());
            let ds = if el.undefined() {
                let (ds, p) = self.dataset(el.value, container, false, enc, depth + 1, &path, multibyte)?;
                pos = p;
                ds
            } else {
                if el.end() > container {
                    return reject(format!("the item at {pos} runs past its container"));
                }
                let (ds, _) = self.dataset(el.value, el.end(), true, enc, depth + 1, &path, multibyte)?;
                pos = el.end();
                ds
            };
            items.push(ds);
        }
    }

    /// The root's source metadata, and where the elements after Pixel Data end (the
    /// trailing bytes start).
    pub fn root(mut self, start: u64, enc: Enc, pixel: &Pixel) -> R<(Obj, u64)> {
        let size = self.b.size;
        let mut root = Obj::new();
        let preamble = self.read(0, 128)?;
        if preamble.iter().any(|&c| c != 0) {
            root.insert("preamble".into(), json!(base64(preamble)));
        }
        let mut meta = self.meta(start)?;
        let mut dataset = Obj::new();
        let mut groups = GroupLengths::default();
        let mut seen: HashSet<u32> = HashSet::new();
        let (mut multibyte, mut charset) = (false, false);
        let mut pos = start;
        loop {
            let el = self.b.header(pos, size, enc)?;
            if el.tag == PIXEL_DATA {
                groups.step(el.tag, pos);
                let vr = el.vr.map(|v| vr_str(&v).to_string()).unwrap_or_else(|| "OW".into());
                dataset.insert(tag_key(el.tag), json!({"vr": vr}));
                seen.insert(el.tag);
                break;
            }
            if pixel.eot_layout && EOT_TAGS.contains(&el.tag) && !seen.contains(&el.tag) {
                groups.step(el.tag, pos);
                self.omitted.insert(tag_key(el.tag));
                pos = el.end();
            } else {
                pos = self.translate(&el, enc, &mut dataset, size, 0, "dataset", multibyte, pos, &mut groups, true)?;
            }
            seen.insert(el.tag);
            if el.tag == CHARSET && !el.undefined() && !charset {
                charset = true;
                multibyte = is_multibyte(self.read(el.value, el.length as u64)?);
            }
        }
        // after Pixel Data: elements as long as they parse and are not duplicates, then
        // the rest as bytes
        let mut tail = pixel.end;
        pos = pixel.end;
        let mut mark = self.extents.len();
        while pos < size {
            let el = match self.b.header(pos, size, enc) {
                Ok(el) => el,
                Err(Fail::Reject(_)) => break,
                Err(m) => return Err(m),
            };
            if el.tag >> 16 == 0xFFFE || seen.contains(&el.tag) {
                break;
            }
            let within = self.within.clone();
            match self.translate(&el, enc, &mut dataset, size, 0, "dataset", multibyte, pos, &mut groups, true) {
                Ok(p) => pos = p,
                Err(Fail::Reject(_)) => {
                    self.within = within;
                    break;
                }
                Err(m) => return Err(m),
            }
            seen.insert(el.tag);
            tail = pos;
            mark = self.extents.len();
        }
        self.extents.truncate(mark);
        groups.finish(tail, &mut dataset, &mut self.extents);
        dataset.swap_remove(PER_FRAME);
        move_large(&mut meta, &mut dataset);
        root.insert("meta".into(), J::Object(sorted(meta)));
        root.insert("dataset".into(), J::Object(sorted(dataset)));
        if pixel.extra.is_some() {
            root.insert("pixel_extra".into(), json!(format!("{SOURCE_NODE}/pixel_extra")));
        }
        if let Some(headers) = pixel.unreferenced {
            root.insert("pixel_unreferenced".into(), json!(format!("{SOURCE_NODE}/pixel_unreferenced")));
            if headers {
                root.insert("pixel_unreferenced_headers".into(), json!(true));
            }
        }
        if pixel.fragments {
            root.insert("pixel_fragments".into(), json!(format!("{SOURCE_NODE}/pixel_fragments")));
        }
        if pixel.offset_table {
            root.insert("pixel_offset_table".into(), json!(true));
        }
        if tail < size {
            root.insert("trailing".into(), json!(format!("{SOURCE_NODE}/trailing")));
        }
        Ok((root, tail))
    }
}

/// (data type, row bytes) of one path's 2-D array, or None for a family.
fn gathered_kind(members: &[&Extent]) -> Option<u64> {
    let kinds: HashSet<(&str, u64, bool)> =
        members.iter().map(|e| match e.values { Some((t, n)) => (t, n, true), None => (e.data_type, e.n, e.little) }).collect();
    if kinds.len() == 1 {
        let (_, n, _) = kinds.into_iter().next().unwrap();
        if n <= MAX_CHUNK {
            return Some(n);
        }
    }
    let lengths: HashSet<u64> = members.iter().map(|e| e.n).collect();
    if lengths.len() == 1 && members[0].n <= MAX_CHUNK {
        return Some(members[0].n);
    }
    None
}

/// The bytes one path's array holds: a family's offsets and data; a 2-D array's rows, only
/// those with a value when they are sparse.
fn gathered_cost(members: &[&Extent], count: u64) -> u64 {
    match gathered_kind(members) {
        None => 8 * (count + 1) + members.iter().map(|e| e.n).sum::<u64>(),
        Some(n) => (if 2 * (members.len() as u64) < count { members.len() as u64 } else { count }) * n,
    }
}

fn object_size(sizes: &[(String, usize)]) -> usize {
    2 + sizes.len().saturating_sub(1) + sizes.iter().map(|(k, n)| js_string_size(k) + 1 + n).sum::<usize>()
}

/// Leaves out of the root's `meta` and `dataset` the members that would make it large:
/// each over 16384 bytes, then, while the two are over 65536 together, the largest left
/// (of equal sizes, the first by name, `meta`'s before `dataset`'s).
fn move_large(meta: &mut Obj, dataset: &mut Obj) {
    let mut sizes: [Vec<(String, usize)>; 2] = [Vec::new(), Vec::new()];
    for (i, members) in [&mut *meta, &mut *dataset].into_iter().enumerate() {
        let keys: Vec<String> = members.keys().cloned().collect();
        for k in keys {
            let n = json_size(&members[&k]);
            if n > MAX_MEMBER {
                members.swap_remove(&k);
            } else {
                sizes[i].push((k, n));
            }
        }
    }
    let mut total: usize = sizes.iter().map(|s| object_size(s)).sum();
    let mut largest: Vec<(usize, String, usize)> = sizes.iter().enumerate().flat_map(|(i, s)| s.iter().map(move |(k, n)| (*n, k.clone(), i))).collect();
    largest.sort_by(|a, b| b.0.cmp(&a.0).then_with(|| a.1.cmp(&b.1)).then_with(|| a.2.cmp(&b.2)));
    for (n, k, i) in largest {
        if total <= MAX_ROOT {
            break;
        }
        let members = if i == 0 { &mut *meta } else { &mut *dataset };
        total -= js_string_size(&k) + 1 + n + usize::from(members.len() > 1);
        members.swap_remove(&k);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ds_numbers_are_exact() {
        for (t, v) in [("0.1", Some(0.1)), ("1.10", Some(1.1)), ("-0", Some(-0.0)), ("1e-7", Some(1e-7)), (".5", Some(0.5)), ("5.", Some(5.0)), ("+2E+3", Some(2000.0))] {
            assert_eq!(ds_number(t), v, "{t}");
        }
        for t in ["9007199254740993", "0.1000000000000000055511151231257827", "1e400", "1e123456", "abc", "", "1e", "."] {
            assert_eq!(ds_number(t), None, "{t}");
        }
    }

    #[test]
    fn text_values_are_trimmed_and_typed() {
        assert_eq!(text_value("DS", b" 2.50 "), json!(2.5));
        assert_eq!(text_value("IS", b"+0012"), json!(12));
        assert_eq!(text_value("IS", b"-12345678901234567"), json!("-12345678901234567"));
        assert_eq!(text_value("LT", b" a \0"), json!(" a"));
        assert_eq!(text_value("PN", b"A^B==C"), json!({"Alphabetic": "A^B", "Phonetic": "C"}));
        assert_eq!(text_value("CS", b"  "), J::Null);
        assert_eq!(text_value("LO", b"\xe9"), json!({"latin1": "\u{e9}"}));
    }

    #[test]
    fn values_by_vr() {
        assert_eq!(value_json("US", &[1, 0, 2, 0], true, false), json!({"vr": "US", "Value": [1, 2]}));
        assert_eq!(value_json("SS", &[0xff, 0xfe], false, false), json!({"vr": "SS", "Value": [-2]}));
        assert_eq!(value_json("AT", &[0x18, 0, 0x63, 0x10], true, false), json!({"vr": "AT", "Value": ["00181063"]}));
        assert_eq!(value_json("UL", &[1, 2, 3], true, false), json!({"vr": "UL", "InlineBinary": "AQID"}));
        assert_eq!(value_json("FD", &f64::NAN.to_le_bytes(), true, false), json!({"vr": "FD", "Value": ["NaN"]}));
        assert_eq!(value_json("UV", &u64::MAX.to_le_bytes(), true, false), json!({"vr": "UV", "Value": ["18446744073709551615"]}));
        assert_eq!(value_json("PN", b"a\\b", true, true), json!({"vr": "PN", "InlineBinary": "YVxi"}));
        assert_eq!(value_json("OB", &[], true, false), json!({"vr": "OB"}));
    }

    #[test]
    fn dictionary_vrs() {
        assert_eq!(dictionary_vr(0x0028_0010), Some("US"));
        assert_eq!(dictionary_vr(0x6002_3000), None); // OB or OW: ambiguous
        assert_eq!(dictionary_vr(0x6002_0010), Some("US")); // a mask
        assert_eq!(dictionary_vr(0x0009_0010), Some("LO")); // a private creator
        assert_eq!(dictionary_vr(0x0009_1010), None);
        assert_eq!(dictionary_vr(0x5200_9230), Some("SQ"));
    }
}
