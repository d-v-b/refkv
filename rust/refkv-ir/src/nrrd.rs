//! A sans-IO parser of NRRD inputs (spec/zarrify/nrrd.md, the profile's §8
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#8-nrrd-profile>) as a store run
//! ([`crate::store::StoreParser`]) whose objects are the header file and, for a detached header,
//! the data file its `data file` field names, probed by key (conventions §8.9
//! <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#89-stores>). The files the chunks
//! reference are described in place; a detached header's file, which no chunk references, is held.
//!
//! **Batches.** The header file's first 64 KiB, then as much again each time, until a blank
//! line ends the header or the file ends; for a detached header, the probe of its data file;
//! the 64 KiB windows that hold the lines `line skip` passes over; the first bytes of a gzip
//! stream; and the edge chunks the projection copies, and a detached header's file whole when its
//! first read did not hold it all. The schema (`schema/nrrd.json`) is checked
//! as the profile meets each role. The IR is emitted at the end, from what was read.

use crate::expr::V;
use crate::ir::{GAP, Ir, NO_INDEX, Res, key_name};
use crate::out::{Chunk, grid_chunks};
use crate::rules::{self, Rules, flag, num, text};
use crate::store::{StoreParser, StoreStep};
use crate::text::{text_json, utf8_or_latin1};
use serde_json::{Map, Value as J, json};

/// The bytes of the header file read first, and of each window `line skip` scans.
const WINDOW: u64 = 1 << 16;
/// The most bytes of a chunk of the image (spec/zarrify/nrrd.md §4.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#41-the-array>), as NIfTI's.
pub const CHUNK_BYTES: u64 = 1 << 17;

/// Whether a file's first bytes are a NRRD header's (spec/zarrify/nrrd.md §8
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#8-nrrd-profile>): `NRRD000`, a
/// digit from 1 to 5, and a line end.
pub fn detect(head: &[u8]) -> bool {
    head.len() >= 9 && head.starts_with(b"NRRD000") && (b'1'..=b'5').contains(&head[7]) && matches!(head[8], b'\n' | b'\r')
}

fn is_magic(t: &[u8]) -> bool {
    t.len() == 8 && t.starts_with(b"NRRD000") && (b'1'..=b'5').contains(&t[7])
}

fn shown(b: &[u8]) -> String {
    utf8_or_latin1(b)
}

/// A problem as a role's given value: absent when there is none.
fn problem(p: Option<String>) -> V {
    p.map_or(V::Absent, |p| text(&p))
}

// ---- the header (§8.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#81-the-header>)

/// A header's lines and what they say: each line's extent (its line end included), the
/// fields by name (in their order, each with its descriptor), the key/value pairs (a repeated
/// key's last value, at its first place) and the comments.
#[derive(Debug, Default, Clone)]
pub struct Header {
    pub magic: String,
    pub lines: Vec<(u64, u64)>,
    pub fields: Vec<(String, Vec<u8>)>,
    pub pairs: Vec<(Vec<u8>, Vec<u8>)>,
    pub comments: Vec<Vec<u8>>,
}

impl Header {
    pub fn field(&self, name: &str) -> Option<&[u8]> {
        self.fields.iter().find(|f| f.0 == name).map(|f| f.1.as_slice())
    }
}

/// The lines of `b`, each (start, length with its line end, text without it): a line ends
/// after a LF, its text less a CR before the LF; the last may end at `b`'s end, without one.
fn lines(b: &[u8]) -> impl Iterator<Item = (usize, usize, &[u8])> {
    let mut at = 0usize;
    std::iter::from_fn(move || {
        if at >= b.len() {
            return None;
        }
        let start = at;
        let (end, next) = match b[at..].iter().position(|&c| c == b'\n') {
            Some(k) => (at + k, at + k + 1),
            None => (b.len(), b.len()),
        };
        at = next;
        let mut t = &b[start..end];
        if next > end && t.last() == Some(&b'\r') {
            t = &t[..t.len() - 1];
        }
        Some((start, next - start, t))
    })
}

/// The header's length when a blank line (a line of no characters, after the magic) ends it in
/// `b`: through that line's end.
pub fn header_end(b: &[u8]) -> Option<usize> {
    lines(b).skip(1).find(|(_, _, t)| t.is_empty()).map(|(s, n, _)| s + n)
}

/// A key or value of a key/value pair unescaped: `\n` is a line feed and `\\` a backslash; any
/// other backslash stays as it is.
fn unescape(b: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'\\' && matches!(b.get(i + 1), Some(b'n' | b'\\')) {
            out.push(if b[i + 1] == b'n' { b'\n' } else { b'\\' });
            i += 2;
        } else {
            out.push(b[i]);
            i += 1;
        }
    }
    out
}

/// `b` without the whitespace (space, tab, CR, LF, VT, FF) that ends it.
fn trim_end(b: &[u8]) -> &[u8] {
    let n = b.iter().rposition(|c| !matches!(c, b' ' | b'\t' | b'\r' | b'\n' | 0x0b | 0x0c)).map_or(0, |k| k + 1);
    &b[..n]
}

/// `b` without the spaces and tabs around it.
fn trim(b: &[u8]) -> &[u8] {
    let s = b.iter().position(|c| *c != b' ' && *c != b'\t').unwrap_or(b.len());
    let e = b.iter().rposition(|c| *c != b' ' && *c != b'\t').map_or(s, |k| k + 1);
    &b[s..e.max(s)]
}

/// Parses the header's lines (spec/zarrify/nrrd.md §8.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#81-the-header>): `b` is the
/// header, through the blank line that ends it, or to its file's end.
pub fn parse(rules: &Rules, b: &[u8]) -> Res<Header> {
    let mut h = Header::default();
    for (k, (start, n, t)) in lines(b).enumerate() {
        h.lines.push((start as u64, n as u64));
        if k == 0 {
            rules.check("magic", rules::env([("ok", flag(is_magic(t)))]))?;
            h.magic = shown(t);
            continue;
        }
        if t.is_empty() {
            break;
        }
        let p = line(rules, &mut h, t);
        rules.check("line", rules::env([("number", num((k + 1) as f64)), ("problem", problem(p))]))?;
        if h.field("data file").is_some_and(|d| words(d).first() == Some(&&b"LIST"[..])) {
            break; // the lines after `data file: LIST` name the data files, which the profile rejects
        }
    }
    Ok(h)
}

/// One line of the header after the magic, a comment, a key/value pair or a field, added to
/// `h`: what is wrong with it, if anything.
fn line(rules: &Rules, h: &mut Header, t: &[u8]) -> Option<String> {
    if t[0] == b'#' {
        let c = &t[t.iter().position(|&c| c != b'#' && c != b' ').unwrap_or(t.len())..];
        if !c.is_empty() {
            h.comments.push(c.to_vec());
        }
        return None;
    }
    if let Some(k) = t.windows(2).position(|w| w == b":=") {
        if k == 0 {
            return Some("a key/value pair with an empty key".into());
        }
        let (key, value) = (unescape(&t[..k]), unescape(&t[k + 2..]));
        match h.pairs.iter_mut().find(|p| p.0 == key) {
            Some(p) => p.1 = value,
            None => h.pairs.push((key, value)),
        }
        return None;
    }
    let Some(colon) = t.iter().position(|&c| c == b':') else {
        return Some(format!("\"{}\" is not a field, a key/value pair or a comment", shown(t)));
    };
    let id = &t[..colon];
    let rest = &t[colon + 1..];
    if !rest.is_empty() && rest[0] != b' ' {
        return Some(format!("the field identifier \"{}\" is not followed by \": \"", shown(id)));
    }
    let Some(name) = rules.entry("fields", &shown(id).to_ascii_lowercase()).and_then(J::as_str) else {
        return Some(format!("\"{}\" is not a NRRD field", shown(id)));
    };
    if h.field(name).is_some() {
        return Some(format!("the {name} field is given twice"));
    }
    h.fields.push((name.to_string(), trim(trim_end(rest)).to_vec()));
    None
}

// ---- the fields (§8.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#82-the-fields>)

/// A field's descriptor split at spaces and tabs.
fn words(d: &[u8]) -> Vec<&[u8]> {
    d.split(|&c| c == b' ' || c == b'\t').filter(|w| !w.is_empty()).collect()
}

/// A decimal integer: an optional sign and digits.
fn integer(w: &[u8]) -> Option<i64> {
    let s = std::str::from_utf8(w).ok()?;
    let digits = s.strip_prefix(['-', '+']).unwrap_or(s);
    if digits.is_empty() || !digits.bytes().all(|c| c.is_ascii_digit()) {
        return None;
    }
    s.parse().ok()
}

/// A floating-point number as NRRD reads one (its section 2): a text holding `nan` (in any
/// case) is NaN, else one holding `-inf` is −∞, else one holding `inf` +∞; any other is a
/// decimal number, an optional sign, digits with an optional fraction, and an optional exponent,
/// correctly rounded.
pub fn float(w: &[u8]) -> Option<f64> {
    let s = std::str::from_utf8(w).ok()?;
    let l = s.to_ascii_lowercase();
    if l.contains("nan") {
        return Some(f64::NAN);
    }
    if l.contains("-inf") {
        return Some(f64::NEG_INFINITY);
    }
    if l.contains("inf") {
        return Some(f64::INFINITY);
    }
    let b = s.as_bytes();
    let digits = |i: usize| b[i..].iter().take_while(|c| c.is_ascii_digit()).count();
    let mut i = usize::from(matches!(b.first(), Some(b'+' | b'-')));
    let whole = digits(i);
    i += whole;
    let mut fraction = 0;
    if b.get(i) == Some(&b'.') {
        fraction = digits(i + 1);
        i += 1 + fraction;
    }
    if whole + fraction == 0 {
        return None;
    }
    if matches!(b.get(i), Some(b'e' | b'E')) {
        i += 1;
        i += usize::from(matches!(b.get(i), Some(b'+' | b'-')));
        let e = digits(i);
        if e == 0 {
            return None;
        }
        i += e;
    }
    if i != b.len() {
        return None;
    }
    s.parse().ok()
}

/// The quoted strings of a descriptor (`"a" "b c"`, `\"` a quote within one), or None when it
/// is not quoted strings separated by spaces and tabs.
pub fn quoted(d: &[u8]) -> Option<Vec<Vec<u8>>> {
    let mut out = Vec::new();
    let mut i = 0;
    while i < d.len() {
        match d[i] {
            b' ' | b'\t' => i += 1,
            b'"' => {
                let mut s = Vec::new();
                i += 1;
                loop {
                    match d.get(i) {
                        None => return None,
                        Some(b'"') => break,
                        Some(b'\\') if d.get(i + 1) == Some(&b'"') => {
                            s.push(b'"');
                            i += 2;
                        }
                        Some(&c) => {
                            s.push(c);
                            i += 1;
                        }
                    }
                }
                i += 1;
                if i < d.len() && d[i] != b' ' && d[i] != b'\t' {
                    return None;
                }
                out.push(s);
            }
            _ => return None,
        }
    }
    Some(out)
}

/// The vectors of a descriptor (`(1,0,0) none (0,1.5,0)`): each `none` (in any case; None) or
/// `(`, numbers separated by `,`, `)`, spaces and tabs allowed around the numbers; None when it
/// is not such vectors separated by spaces and tabs.
pub fn vectors(d: &[u8]) -> Option<Vec<Option<Vec<f64>>>> {
    let mut out = Vec::new();
    let mut i = 0;
    while i < d.len() {
        match d[i] {
            b' ' | b'\t' => i += 1,
            b'(' => {
                let close = i + d[i..].iter().position(|&c| c == b')')?;
                let v: Option<Vec<f64>> = d[i + 1..close].split(|&c| c == b',').map(|p| float(trim(p))).collect();
                out.push(Some(v?));
                i = close + 1;
                if i < d.len() && d[i] != b' ' && d[i] != b'\t' {
                    return None;
                }
            }
            _ => {
                let end = i + d[i..].iter().position(|&c| c == b' ' || c == b'\t').unwrap_or(d.len() - i);
                if !d[i..end].eq_ignore_ascii_case(b"none") {
                    return None;
                }
                out.push(None);
                i = end;
            }
        }
    }
    Some(out)
}

/// What the header's fields give the layout (spec/zarrify/nrrd.md §8.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#82-the-fields>), checked.
#[derive(Debug, Clone)]
pub struct Fields {
    pub dimension: usize,
    pub data_type: String,
    pub item: u64,
    pub encoding: String,
    pub little: Option<bool>,
    pub sizes: Vec<u64>,
    pub line_skip: u64,
    pub byte_skip: i64,
    pub data_file: Option<String>,
    pub directions: Option<Vec<Option<Vec<f64>>>>,
    pub origin: Option<Vec<f64>>,
    pub space_units: Option<Vec<Vec<u8>>>,
    pub spacings: Option<Vec<f64>>,
    pub units: Option<Vec<Vec<u8>>>,
    pub kinds: Option<Vec<String>>,
    pub min: Option<f64>,
    pub max: Option<f64>,
}

/// The value check of a field: a problem rejects, naming the field and its descriptor.
fn check_value(rules: &Rules, field: &str, d: &[u8], p: Option<String>) -> Res<()> {
    rules.check("value", rules::env([("field", text(field)), ("shown", text(&shown(d))), ("problem", problem(p))]))
}

/// The descriptor of a field the header must have (`why`: why, when it says).
fn required<'a>(rules: &Rules, h: &'a Header, field: &str, why: &str) -> Res<&'a [u8]> {
    let d = h.field(field);
    rules.check("required", rules::env([("field", text(field)), ("present", flag(d.is_some())), ("why", text(why))]))?;
    Ok(d.unwrap_or_default())
}

/// How a field of several values is read: its count `n` and what sets it (`of`), how its
/// descriptor splits (into words, or quoted strings), and what each value must be (`what`).
struct Shape<'a> {
    n: usize,
    of: &'a str,
    split: fn(&[u8]) -> Option<Vec<Vec<u8>>>,
    what: &'a str,
}

/// A field of `shape.n` values (one per axis, or per space coordinate), each `one` of its words or
/// quoted strings, checked: None when the header lacks it.
fn values<T>(rules: &Rules, h: &Header, field: &str, shape: Shape, one: impl Fn(&[u8]) -> Option<T>) -> Res<Option<Vec<T>>> {
    let Shape { n, of, split, what } = shape;
    let Some(d) = h.field(field) else { return Ok(None) };
    let v: Option<Vec<T>> = split(d).and_then(|w| w.iter().map(|x| one(x)).collect());
    let p = match &v {
        None => Some(format!("is not {what}")),
        Some(v) if v.len() != n => Some(format!("has {} values, not {n} ({of})", v.len())),
        _ => None,
    };
    check_value(rules, field, d, p)?;
    Ok(v)
}

/// The fields the layout reads, each checked, in this order (spec/zarrify/nrrd.md §8.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#82-the-fields>); `header_key`
/// is the header file's key, which the data file must not be.
pub fn fields(rules: &Rules, h: &Header, header_key: &str) -> Res<Fields> {
    let max_dim = rules.limit("max_dimension") as i64;
    let max_safe = rules.limit("max_safe") as i64;
    let d = required(rules, h, "dimension", "")?;
    let dimension = integer(trim(d)).filter(|v| (1..=max_dim).contains(v));
    check_value(rules, "dimension", d, dimension.is_none().then(|| format!("is not an integer from 1 to {max_dim}")))?;
    let n = dimension.unwrap_or(1) as usize;

    let d = required(rules, h, "type", "")?;
    let name = shown(trim(d)).to_ascii_lowercase();
    rules.check("type", rules::env([("name", text(&name))]))?;
    let spec = rules.entry("types", &name).ok_or("internal: a type the schema does not name")?;
    let (data_type, item) = (spec["dtype"].as_str().unwrap_or("uint8").to_string(), spec["bytes"].as_u64().unwrap_or(1));

    let d = required(rules, h, "encoding", "")?;
    let name = shown(trim(d)).to_ascii_lowercase();
    rules.check("encoding", rules::env([("name", text(&name))]))?;
    let encoding = rules.entry("encodings", &name).and_then(J::as_str).ok_or("internal: an encoding the schema does not name")?.to_string();

    let little = match h.field("endian") {
        Some(d) => {
            let e = shown(trim(d)).to_ascii_lowercase();
            check_value(rules, "endian", d, (e != "little" && e != "big").then(|| "is not little or big".into()))?;
            Some(e == "little")
        }
        None if item > 1 => {
            required(rules, h, "endian", " (its type has more than 1 byte)")?;
            None
        }
        None => None,
    };

    required(rules, h, "sizes", "")?;
    let split: fn(&[u8]) -> Option<Vec<Vec<u8>>> = |d| Some(words(d).into_iter().map(<[u8]>::to_vec).collect());
    let per_axis = |what| Shape { n, of: "the dimension", split, what };
    let sizes = values(rules, h, "sizes", per_axis("integers from 1 to 2^53 - 1"), |w| integer(w).filter(|v| (1..=max_safe).contains(v)))?
        .unwrap_or_default().into_iter().map(|v| v as u64).collect();

    let skip = |field: &str, lo: i64| -> Res<i64> {
        let Some(d) = h.field(field) else { return Ok(0) };
        let v = integer(trim(d)).filter(|v| (lo..=max_safe).contains(v));
        check_value(rules, field, d, v.is_none().then(|| format!("is not an integer from {lo} to 2^53 - 1")))?;
        Ok(v.unwrap_or(0))
    };
    let line_skip = skip("line skip", 0)? as u64;
    let byte_skip = skip("byte skip", -1)?;
    rules.check("skips", rules::env([("encoding", text(&encoding)), ("byte_skip", num(byte_skip as f64))]))?;

    let data_file = match h.field("data file") {
        Some(d) => Some(data_file(rules, d, header_key)?),
        None => None,
    };

    // the space, which the orientation fields need
    let space_dimension = match (h.field("space"), h.field("space dimension")) {
        (Some(_), Some(d)) => {
            check_value(rules, "space dimension", d, Some("is given with a space field".into()))?;
            None
        }
        (Some(d), None) => {
            let sd = rules.entry("spaces", &shown(trim(d)).to_ascii_lowercase()).and_then(J::as_u64);
            check_value(rules, "space", d, sd.is_none().then(|| "is not a NRRD space".into()))?;
            sd.map(|v| v as usize)
        }
        (None, Some(d)) => {
            let v = integer(trim(d)).filter(|v| (1..=max_dim).contains(v));
            check_value(rules, "space dimension", d, v.is_none().then(|| format!("is not an integer from 1 to {max_dim}")))?;
            v.map(|v| v as usize)
        }
        (None, None) => None,
    };
    for f in ["space directions", "space origin", "space units", "measurement frame"] {
        if let (Some(d), None) = (h.field(f), space_dimension) {
            check_value(rules, f, d, Some("is given without a space or space dimension field".into()))?;
        }
    }
    let sd = space_dimension.unwrap_or(0);
    let directions = match h.field("space directions") {
        Some(d) => {
            let v = vectors(d);
            let p = match &v {
                None => Some("is not vectors and none".to_string()),
                Some(v) if v.len() != n => Some(format!("has {} values, not {n} (the dimension)", v.len())),
                Some(v) if v.iter().flatten().any(|x| x.len() != sd) => Some(format!("has a vector of other than {sd} components (the space's dimension)")),
                _ => None,
            };
            check_value(rules, "space directions", d, p)?;
            v
        }
        None => None,
    };
    let origin = match h.field("space origin") {
        Some(d) => {
            let v = vectors(d);
            let ok = matches!(&v, Some(v) if v.len() == 1 && v[0].as_ref().is_some_and(|x| x.len() == sd));
            check_value(rules, "space origin", d, (!ok).then(|| format!("is not one vector of {sd} components (the space's dimension)")))?;
            v.and_then(|mut v| v.remove(0))
        }
        None => None,
    };
    let strings = |n, of| Shape { n, of, split: quoted, what: "quoted strings" };
    let space_units = values(rules, h, "space units", strings(sd, "the space's dimension"), |w| Some(w.to_vec()))?;
    let spacings = values(rules, h, "spacings", per_axis("numbers"), float)?;
    let units = values(rules, h, "units", strings(n, "the dimension"), |w| Some(w.to_vec()))?;
    let kinds = values(rules, h, "kinds", per_axis("NRRD kinds"), |w| {
        let k = shown(w).to_ascii_lowercase();
        rules.entry("kinds", &k).map(|_| k)
    })?;
    let number = |field: &str| h.field(field).and_then(|d| float(trim(d)));
    Ok(Fields {
        dimension: n, data_type, item, encoding, little, sizes, line_skip, byte_skip, data_file,
        directions, origin, space_units, spacings, units, kinds, min: number("min"), max: number("max"),
    })
}

/// The key of a detached header's one data file (spec/zarrify/nrrd.md §8.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#83-the-data-file>), relative to
/// the header's directory: its descriptor without `.` segments.
fn data_file(rules: &Rules, d: &[u8], header_key: &str) -> Res<String> {
    let d = trim(d);
    let s = shown(d);
    let w = words(d);
    let several = w.first().is_some_and(|f| *f == b"LIST")
        || ((4..=5).contains(&w.len()) && w[0].contains(&b'%') && w[1..].iter().all(|x| integer(x).is_some()));
    let segments: Vec<&str> = s.split('/').filter(|x| *x != ".").collect();
    let relative = std::str::from_utf8(d).is_ok() && !s.starts_with('/') && !segments.is_empty()
        && segments.iter().all(|x| !x.is_empty() && *x != ".." && !x.contains(['\\', ':']));
    let key = segments.join("/");
    rules.check("data_file", rules::env([("shown", text(&s)), ("several", flag(several)), ("relative", flag(relative)),
                                          ("itself", flag(key == header_key))]))?;
    Ok(key)
}

/// Each axis's place in the image (spec/zarrify/nrrd.md §4.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#41-the-array>), in the header's
/// axis order (fastest first): `x`, `y` and `z` for the spatial axes in that order, `t` and `c`.
pub fn axes(rules: &Rules, f: &Fields) -> Res<Vec<&'static str>> {
    let roles: Vec<&str> = (0..f.dimension)
        .map(|i| match f.kinds.as_ref().and_then(|k| rules.entry("kinds", &k[i])).and_then(J::as_str).unwrap_or("unknown") {
            "unknown" => match &f.directions {
                Some(d) if d[i].is_none() => "channel",
                _ => "space",
            },
            k => k,
        })
        .collect();
    let count = |r: &str| roles.iter().filter(|x| **x == r).count() as f64;
    rules.check("axes", rules::env([("spatial", num(count("space"))), ("times", num(count("time"))), ("channels", num(count("channel")))]))?;
    let mut spatial = ["x", "y", "z"].into_iter();
    roles.iter().map(|r| match *r {
        "time" => Ok("t"),
        "channel" => Ok("c"),
        "space" => spatial.next().ok_or_else(|| "internal: more than 3 spatial axes".to_string()),
        other => Err(format!("internal: the role {other:?}")),
    }).collect()
}

// ---- the run (§8.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#84-the-data>)

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Phase {
    Head,
    Probed,
    Skip,
    Data,
    Copies,
    Done,
}

pub struct Nrrd {
    rules: &'static Rules,
    phase: Phase,
    ir: Option<Ir>,
    keys: Vec<(String, u64)>,
    elems: Vec<u32>,
    /// the header file's bytes read, from its start
    head: Vec<u8>,
    header: Option<Header>,
    fields: Option<Fields>,
    axes: Vec<&'static str>,
    /// the data's object (0: the header file), where the data may start in it (past the
    /// header and the lines line skip passes over), and the data's extent
    data: usize,
    base: u64,
    start: u64,
    length: u64,
    /// line skip's scan: the line ends found, where the next window starts, and where it began
    scanning: bool,
    found_lines: u64,
    scan: u64,
    scan_from: u64,
    copies: Vec<(u64, u64)>,
    copied: Vec<(u64, Vec<u8>)>,
    asked: Vec<(usize, u64, u64)>,
    got: std::collections::HashMap<(usize, u64, u64), Vec<u8>>,
    batches: u64,
    copy_threshold: usize,
}

impl Nrrd {
    /// The listing's IR, its objects' elements and keys: one key, the header file.
    pub fn new(ir: Ir, objects: Vec<u32>, keys: &[(String, u64)]) -> Res<Nrrd> {
        if keys.len() != 1 || keys[0].0.contains('/') {
            return Err(format!("a NRRD run starts from the header file alone, not {} objects", keys.len()));
        }
        Ok(Nrrd {
            rules: rules::of("nrrd"),
            phase: Phase::Head,
            ir: Some(ir),
            keys: keys.to_vec(),
            elems: objects,
            head: Vec::new(),
            header: None,
            fields: None,
            axes: Vec::new(),
            data: 0,
            base: 0,
            start: 0,
            length: 0,
            scanning: false,
            found_lines: 0,
            scan: 0,
            scan_from: 0,
            copies: Vec::new(),
            copied: Vec::new(),
            asked: Vec::new(),
            got: Default::default(),
            batches: 0,
            copy_threshold: crate::out::copy_threshold(),
        })
    }

    /// The bytes fed for the read of `object` at `offset` (the one asked last there).
    fn take_at(&mut self, object: usize, offset: u64) -> Option<Vec<u8>> {
        let k = self.got.keys().find(|k| k.0 == object && k.1 == offset).copied()?;
        self.got.remove(&k)
    }

    /// The header, once read (spec/zarrify/nrrd.md §8.1
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#81-the-header>): its magic is
    /// checked as soon as its first line is held, and more of the file is read until a blank line
    /// ends the header or the file ends. None while more is to be read.
    fn read_head(&mut self) -> Res<Option<usize>> {
        let size = self.keys[0].1;
        let have = self.head.len() as u64;
        if let Some((_, _, t)) = lines(&self.head).next() {
            if t.len() >= 8 || self.head.contains(&b'\n') || have == size {
                self.rules.check("magic", rules::env([("ok", flag(is_magic(t)))]))?;
            }
        } else {
            self.rules.check("magic", rules::env([("ok", flag(false))]))?;
        }
        if let Some(end) = header_end(&self.head) {
            return Ok(Some(end));
        }
        if have == size {
            return Ok(Some(self.head.len()));
        }
        let max = self.rules.limit("max_header") as u64;
        self.rules.check("header", rules::env([("within", flag(have < max))]))?;
        self.asked.push((0, have, have.min(size - have).min(max - have)));
        Ok(None)
    }

    /// The header's fields and checks (spec/zarrify/nrrd.md §8.1
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#81-the-header>–§8.3
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#83-the-data-file>): the data
    /// file to probe, when the header is detached.
    fn read_header(&mut self, end: usize) -> Res<Option<String>> {
        let h = parse(self.rules, &self.head[..end])?;
        let blank = header_end(&self.head).is_some();
        self.rules.check("attached", rules::env([("detached", flag(h.field("data file").is_some())), ("blank", flag(blank))]))?;
        let f = fields(self.rules, &h, &self.keys[0].0)?;
        self.axes = axes(self.rules, &f)?;
        let probe = f.data_file.clone();
        self.base = if probe.is_some() { 0 } else { end as u64 };
        self.header = Some(h);
        self.fields = Some(f);
        Ok(probe)
    }

    /// Line skip's scan (spec/zarrify/nrrd.md §8.4
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#84-the-data>): the window
    /// read last, then the next to read, or None when its lines are found (`base` then past them).
    fn skip_lines(&mut self, window: Option<Vec<u8>>) -> Res<Option<(usize, u64, u64)>> {
        let wanted = self.fields.as_ref().map_or(0, |f| f.line_skip);
        let (key, size) = self.keys[self.data].clone();
        if let Some(w) = window {
            for (k, _) in w.iter().enumerate().filter(|(_, c)| **c == b'\n') {
                self.found_lines += 1;
                if self.found_lines == wanted {
                    self.base = self.scan + k as u64 + 1;
                    return Ok(None);
                }
            }
            self.scan += w.len() as u64;
        }
        let scanned = self.scan - self.scan_from;
        let max = self.rules.limit("max_skip") as u64;
        self.rules.check("line_skip", rules::env([("lines", num(wanted as f64)), ("key", text(&key)),
                                                   ("end", flag(self.scan >= size)), ("within", flag(scanned < max))]))?;
        Ok(Some((self.data, self.scan, WINDOW.min(size - self.scan).min(max - scanned))))
    }

    /// The data's extent (spec/zarrify/nrrd.md §8.4
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#84-the-data>), checked: the
    /// first bytes of a gzip stream to read.
    fn place_data(&mut self) -> Res<Option<(usize, u64, u64)>> {
        let f = self.fields.as_ref().ok_or("internal: no fields")?;
        let (key, size) = self.keys[self.data].clone();
        let total = f.sizes.iter().fold(f.item as u128, |a, &s| a.saturating_mul(s as u128));
        let gzip = f.encoding == "gzip";
        let start = match f.byte_skip {
            -1 => (size as u128).checked_sub(total).map_or(0, |s| s as u64),
            k => self.base + k as u64,
        };
        self.rules.check("data", rules::env([
            ("total", num(total as f64)), ("total_shown", text(&total.to_string())), ("start", num(start as f64)),
            ("size", num(size as f64)), ("key", text(&key)), ("base", num(self.base as f64)), ("encoding", text(&f.encoding)),
        ]))?;
        self.start = start;
        self.length = if gzip { size - start } else { total as u64 };
        Ok(gzip.then_some((self.data, start, 3)))
    }

    /// The stored array's shape, its slowest axis first (spec/zarrify/nrrd.md §4.1
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#41-the-array>).
    fn stored_shape(&self) -> Vec<u64> {
        self.fields.as_ref().map(|f| f.sizes.iter().rev().copied().collect()).unwrap_or_default()
    }

    /// The IR (spec/zarrify/nrrd.md §5.3
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#53-elements>) and the facts.
    fn emit(&mut self) -> Res<J> {
        let h = self.header.clone().ok_or("internal: no header")?;
        let f = self.fields.clone().ok_or("internal: no fields")?;
        let mut ir = self.ir.take().ok_or("internal: no IR")?;
        let mut claimed: Vec<Vec<(u64, u64)>> = vec![Vec::new(); self.keys.len()];
        // an attached header's file is described in place, its data referenced; a detached header's
        // file, which no chunk references, is held: the archive's mirror keeps its bytes
        if self.data == 0 {
            ir.describe_in_place(self.elems[0])?;
        }
        for (i, &(o, n)) in h.lines.iter().enumerate() {
            ir.value(self.elems[0], "lines/", i as u64, &format!("ascii[{n}]"), 0, (o, n), Some(&self.head[o as usize..(o + n) as usize]))?;
            claimed[0].push((o, n));
        }
        let o = self.elems[self.data];
        if self.data != 0 {
            ir.describe_in_place(o)?;
        }
        let mut codec = Map::new();
        codec.insert("encoding".into(), json!(f.encoding));
        if let Some(l) = f.little {
            codec.insert("endian".into(), json!(if l { "little" } else { "big" }));
        }
        let form = json!({"geometry": {"data_type": f.data_type, "shape": self.stored_shape()}, "codec": codec, "recipe": [["src", 0, null]]});
        ir.data(o, "data", NO_INDEX, 0, (self.start, self.length), &form.to_string())?;
        claimed[self.data].push((self.start, self.length));
        for (k, c) in claimed.iter_mut().enumerate() {
            c.sort_unstable();
            let (obj, size) = (self.elems[k], self.keys[k].1);
            let mut pos = 0u64;
            for &(a, n) in c.iter() {
                if a > pos {
                    ir.add(GAP, obj, "gaps/", pos, "", 0, Some((pos, a - pos)))?;
                }
                pos = pos.max(a + n);
            }
            if size > pos {
                ir.add(GAP, obj, "gaps/", pos, "", 0, Some((pos, size - pos)))?;
            }
        }
        ir.sort_values();
        crate::check::finish(&mut ir)?;
        self.ir = Some(ir);
        let finite = |v: &[f64]| J::Array(v.iter().map(|x| if x.is_finite() { json!(x) } else { J::Null }).collect());
        let texts = |v: &[Vec<u8>]| J::Array(v.iter().map(|u| json!(shown(u))).collect());
        Ok(json!({
            "header_key": self.keys[0].0,
            "data_key": self.keys[self.data].0,
            "own": own(&h, &f, &self.axes, (self.data != 0).then(|| self.keys[self.data].0.as_str())),
            "array": {
                "data_type": f.data_type, "item": f.item, "encoding": f.encoding,
                "endian": f.little.map(|l| if l { "little" } else { "big" }),
                "sizes": f.sizes, "axes": self.axes, "start": self.start, "length": self.length,
                "directions": f.directions.as_ref().map(|d| d.iter().map(|v| v.as_deref().map(finite)).collect::<Vec<_>>()),
                "origin": f.origin.as_deref().map(finite),
                "space_units": f.space_units.as_deref().map(texts),
                "spacings": f.spacings.as_deref().map(finite),
                "units": f.units.as_deref().map(texts),
                "kinds": f.kinds,
                "min": f.min.filter(|x| x.is_finite()), "max": f.max.filter(|x| x.is_finite()),
            },
            "copy_threshold": self.copy_threshold,
            "copied": self.copied.iter().map(|(o, b)| json!([o, b])).collect::<Vec<_>>(),
            "counts": {"batches": self.batches},
        }))
    }

    fn advance(&mut self) -> Res<StoreStep> {
        loop {
            self.asked.clear();
            match self.phase {
                Phase::Head => {
                    if let Some(b) = self.take_at(0, self.head.len() as u64) {
                        self.head.extend(b);
                    }
                    if self.head.is_empty() && self.keys[0].1 > 0 {
                        self.asked.push((0, 0, self.keys[0].1.min(WINDOW)));
                    } else if let Some(end) = self.read_head()? {
                        match self.read_header(end)? {
                            Some(key) => {
                                self.phase = Phase::Probed;
                                self.batches += 1;
                                return Ok(StoreStep::Probe(vec![key]));
                            }
                            None => self.phase = Phase::Skip,
                        }
                    }
                }
                Phase::Probed => {
                    if self.data == 0 {
                        let key = self.fields.as_ref().and_then(|f| f.data_file.clone()).unwrap_or_default();
                        self.rules.check("data_object", rules::env([("key", text(&key)), ("found", flag(false))]))?;
                    }
                    self.phase = Phase::Skip;
                }
                Phase::Skip => {
                    let skip = self.fields.as_ref().is_some_and(|f| f.line_skip > 0 && f.byte_skip != -1);
                    let next = if !skip {
                        None
                    } else {
                        let window = if self.scanning {
                            self.take_at(self.data, self.scan)
                        } else {
                            (self.scanning, self.scan, self.scan_from) = (true, self.base, self.base);
                            None
                        };
                        self.skip_lines(window)?
                    };
                    match next {
                        Some(r) => self.asked.push(r),
                        None => {
                            self.phase = Phase::Data;
                            if let Some(r) = self.place_data()? {
                                self.asked.push(r);
                            }
                        }
                    }
                }
                Phase::Data => {
                    if self.fields.as_ref().is_some_and(|f| f.encoding == "gzip") {
                        let b = self.take_at(self.data, self.start).ok_or("internal: the gzip header was not read")?;
                        let key = self.keys[self.data].0.clone();
                        self.rules.check("gzip", rules::env([("ok", flag(b == [0x1f, 0x8b, 8])), ("start", num(self.start as f64)), ("key", text(&key))]))?;
                    } else {
                        let item = self.fields.as_ref().map_or(1, |f| f.item);
                        let (_, chunks) = grid_chunks(self.start, &self.stored_shape(), item, CHUNK_BYTES, self.copy_threshold)
                            .ok_or("internal: the data cannot be cut into chunks")?;
                        self.copies = chunks.iter().filter_map(|(_, c)| match c {
                            Chunk::Copy(o, n, _) => Some((*o, *n)),
                            _ => None,
                        }).collect();
                        self.asked.extend(self.copies.iter().map(|&(o, n)| (self.data, o, n)));
                    }
                    // a detached header file is held (§5.2
                    // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#52-the-ir-mirror>):
                    // read whole, in one request, when its first read did not hold it all
                    let size = self.keys[0].1;
                    if self.data != 0 && size > WINDOW {
                        self.asked.push((0, 0, size));
                    }
                    self.phase = Phase::Copies;
                }
                Phase::Copies => {
                    for (o, n) in std::mem::take(&mut self.copies) {
                        let b = self.got.remove(&(self.data, o, n)).ok_or("internal: a copied chunk was not read")?;
                        self.copied.push((o, b));
                    }
                    self.got.remove(&(0, 0, self.keys[0].1)); // the held header, which the run keeps
                    self.phase = Phase::Done;
                    return Ok(StoreStep::Done);
                }
                Phase::Done => return Ok(StoreStep::Done),
            }
            if !self.asked.is_empty() {
                self.batches += 1;
                return Ok(StoreStep::Read(self.asked.clone()));
            }
        }
    }
}

/// The root's source metadata (spec/zarrify/nrrd.md §5.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#51-the-root>), but `orientation`,
/// which the projection adds.
fn own(h: &Header, f: &Fields, axes: &[&str], data_file: Option<&str>) -> J {
    let mut m = Map::new();
    m.insert("magic".into(), json!(h.magic));
    m.insert("fields".into(), J::Object(h.fields.iter().map(|(k, v)| (k.clone(), text_json(v))).collect()));
    let mut pairs = Map::new();
    for (k, v) in &h.pairs {
        pairs.entry(shown(k)).or_insert_with(|| text_json(v));
    }
    m.insert("key_values".into(), J::Object(pairs));
    m.insert("comments".into(), J::Array(h.comments.iter().map(|c| text_json(c)).collect()));
    m.insert("data_type".into(), json!(f.data_type));
    m.insert("encoding".into(), json!(f.encoding));
    if let Some(l) = f.little {
        m.insert("byte_order".into(), json!(if l { "little" } else { "big" }));
    }
    m.insert("axes".into(), json!(axes));
    if let Some(k) = data_file {
        m.insert("data_file".into(), json!(k));
    }
    J::Object(m)
}

impl StoreParser for Nrrd {
    fn step(&mut self) -> Res<StoreStep> {
        self.advance()
    }

    fn feed(&mut self, object: usize, offset: u64, data: &[u8]) {
        self.got.insert((object, offset, data.len() as u64), data.to_vec());
    }

    fn found(&mut self, key: &str, index: usize, size: Option<u64>) -> Res<Option<u32>> {
        let Some(size) = size else { return Ok(None) };
        let ir = self.ir.as_mut().ok_or("internal: no IR")?;
        let mut parent = 0u32;
        let mut from = 0;
        for (at, _) in key.match_indices('/') {
            let (n, x) = key_name(&key[from..at], false);
            parent = ir.struct_(parent, &n, x, 0, None)?;
            from = at + 1;
        }
        let (n, x) = key_name(&key[from..], false);
        let e = ir.object(parent, &n, x, size)?;
        if index != self.keys.len() {
            return Err("internal: a found object out of order".into());
        }
        self.keys.push((key.to_string(), size));
        self.elems.push(e);
        self.data = index;
        Ok(Some(e))
    }

    fn finish(mut self: Box<Self>) -> Res<(Ir, J)> {
        if self.phase != Phase::Done {
            return Err("internal: the parse is not done".into());
        }
        let facts = self.emit()?;
        Ok((self.ir.take().unwrap(), facts))
    }
}

/// A NRRD input's file run: a header file, with its data after it or in the data file it names,
/// zarrified as a store run of the `nrrd` store format (spec/zarrify/nrrd.md §8
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#8-nrrd-profile>), never as a
/// file run: `Format::sniff` names it so that a host sends the file to
/// [`crate::store::StoreRun`].
pub struct Profile;

impl crate::run::FileProfile for Profile {
    /// `NRRD000`, a version digit and a line end (`detect`).
    fn detect(&self, head: &[u8]) -> bool {
        detect(head)
    }
    fn parser(&self, _size: u64) -> crate::run::AnyParser {
        crate::run::AnyParser::refused("a NRRD file is zarrified as a store run, with its data file")
    }
    fn store_run(&self) -> bool {
        true
    }
    fn project(&self, _: &Ir, _: &J, _: &str, _: &crate::project::Copied) -> Result<crate::out::Out, String> {
        Err("a NRRD input is zarrified as a store run".into())
    }
}
