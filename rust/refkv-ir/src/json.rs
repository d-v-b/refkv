//! A store's JSON documents as the store profiles read them
//! (spec/zarrify.md §1.6 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#16-json-documents>):
//! UTF-8 without a byte order mark, at most 16 MiB, nesting at most 256 deep, no `NaN` or
//! `Infinity` literal, every number finite in binary64, a repeated member the last one
//! written (in the place of the first). An integer literal (no fraction, no exponent) keeps
//! every digit, so a document's members are copied exactly; any other number is its
//! binary64 value. A string keeps the literal it was written as, so that one with an
//! unpaired surrogate escape is copied as written.
//!
//! [`parse`] also gives the extents of a top-level object's members' values, which a
//! store parser makes the values of the document's IR.

use serde_json::Value as J;
use std::collections::HashMap;

pub const MAX_DOCUMENT: u64 = 1 << 24;
pub const MAX_DEPTH: usize = 256;

/// A string: its text, its literal as written, and where its unpaired surrogate escapes are
/// (`lone`: the byte offsets in `text` of each one's marker, `\u{FFFD}` and the escape's four
/// hex digits in lower case): two strings are one when their texts and markers are.
#[derive(Clone, Debug)]
pub struct Js {
    pub text: String,
    pub raw: String,
    pub lone: Vec<u32>,
}

impl Js {
    pub fn new(s: &str) -> Js {
        Js { text: s.to_string(), raw: serde_json::to_string(s).unwrap(), lone: Vec::new() }
    }
}

impl Js {
    /// The text, when no escape in it left a surrogate unpaired.
    pub fn as_str(&self) -> Option<&str> {
        self.lone.is_empty().then_some(self.text.as_str())
    }
}

impl PartialEq for Js {
    fn eq(&self, o: &Js) -> bool {
        self.text == o.text && self.lone == o.lone
    }
}

/// A JSON value. `Int` is an integer literal's digits (with its sign; `-0` is `0`).
#[derive(Clone, Debug, PartialEq)]
pub enum Jv {
    Null,
    Bool(bool),
    Int(String),
    Float(f64),
    Str(Js),
    Arr(Vec<Jv>),
    Obj(Vec<(Js, Jv)>),
}

impl Jv {
    pub fn str(s: &str) -> Jv {
        Jv::Str(Js::new(s))
    }

    /// The member `k` of an object (None when absent or not an object).
    pub fn get(&self, k: &str) -> Option<&Jv> {
        match self {
            Jv::Obj(m) => m.iter().find(|(n, _)| n.lone.is_empty() && n.text == k).map(|(_, v)| v),
            _ => None,
        }
    }

    pub fn members(&self) -> &[(Js, Jv)] {
        match self {
            Jv::Obj(m) => m,
            _ => &[],
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Jv::Str(s) => Some(&s.text),
            _ => None,
        }
    }

    pub fn is_number(&self) -> bool {
        matches!(self, Jv::Int(_) | Jv::Float(_))
    }

    /// A number's binary64 value.
    pub fn num(&self) -> Option<f64> {
        match self {
            Jv::Int(d) => d.parse().ok(),
            Jv::Float(x) => Some(*x),
            _ => None,
        }
    }

    /// An integer of spec/zarrify.md §1.6
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#16-json-documents> from `lo` to `hi` (within ±(2^53 − 1)): an integer literal, or a
    /// number whose value is an integer.
    pub fn int(&self, lo: i64, hi: i64) -> Option<i64> {
        const MAX_SAFE: f64 = 9007199254740991.0;
        let x = self.num()?;
        if x.fract() != 0.0 || x.abs() > MAX_SAFE {
            return None;
        }
        let v = x as i64;
        (lo <= v && v <= hi).then_some(v)
    }

    /// An object of the given members.
    pub fn obj(m: Vec<(&str, Jv)>) -> Jv {
        Jv::Obj(m.into_iter().map(|(k, v)| (Js::new(k), v)).collect())
    }

    /// Sets the member `k` (replacing it in its place, or appending it).
    pub fn set(&mut self, k: &str, v: Jv) {
        if let Jv::Obj(m) = self {
            match m.iter_mut().find(|(n, _)| n.lone.is_empty() && n.text == k) {
                Some(e) => e.1 = v,
                None => m.push((Js::new(k), v)),
            }
        }
    }

    pub fn is_empty_object(&self) -> bool {
        matches!(self, Jv::Obj(m) if m.is_empty())
    }

    /// A serde value as a JSON value (its numbers are serde's: integers as written).
    pub fn from_j(j: &J) -> Jv {
        match j {
            J::Null => Jv::Null,
            J::Bool(b) => Jv::Bool(*b),
            J::Number(n) if n.is_i64() || n.is_u64() => Jv::Int(n.to_string()),
            J::Number(n) => Jv::Float(n.as_f64().unwrap_or(0.0)),
            J::String(s) => Jv::str(s),
            J::Array(a) => Jv::Arr(a.iter().map(Jv::from_j).collect()),
            J::Object(o) => Jv::Obj(o.iter().map(|(k, v)| (Js::new(k), Jv::from_j(v))).collect()),
        }
    }

    /// The value for the schema's expressions: numbers as binary64, `null` absent.
    pub fn to_v(&self) -> crate::expr::V {
        use crate::expr::V;
        match self {
            Jv::Null => V::Absent,
            Jv::Bool(b) => V::Bool(*b),
            Jv::Int(_) | Jv::Float(_) => V::Num(self.num().unwrap_or(f64::NAN)),
            Jv::Str(s) => V::Str(s.text.clone()),
            Jv::Arr(a) => V::List(a.iter().map(|x| x.to_v()).collect()),
            Jv::Obj(m) => V::Obj(m.iter().map(|(k, v)| (k.text.clone(), v.to_v())).collect()),
        }
    }

    /// Compact JSON text (no whitespace), strings as written, integers with every digit.
    pub fn write(&self, out: &mut String) {
        match self {
            Jv::Null => out.push_str("null"),
            Jv::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
            Jv::Int(d) => out.push_str(d),
            Jv::Float(x) => out.push_str(&serde_json::to_string(x).unwrap_or_else(|_| "null".into())),
            Jv::Str(s) => out.push_str(&s.raw),
            Jv::Arr(a) => {
                out.push('[');
                for (k, v) in a.iter().enumerate() {
                    if k > 0 {
                        out.push(',');
                    }
                    v.write(out);
                }
                out.push(']');
            }
            Jv::Obj(m) => {
                out.push('{');
                for (k, (n, v)) in m.iter().enumerate() {
                    if k > 0 {
                        out.push(',');
                    }
                    out.push_str(&n.raw);
                    out.push(':');
                    v.write(out);
                }
                out.push('}');
            }
        }
    }

    pub fn text(&self) -> String {
        let mut s = String::new();
        self.write(&mut s);
        s
    }

    /// The JSON text, cut to `n` characters (for messages).
    pub fn short(&self, n: usize) -> String {
        let t = self.text();
        match t.char_indices().nth(n) {
            Some((at, _)) => t[..at].to_string(),
            None => t,
        }
    }
}

/// A parsed document, and, when it is an object, each member's key and the extent
/// `(start, end)` of its value's text, in the order written (a repeated key included).
pub struct Doc {
    pub value: Jv,
    pub members: Vec<(String, u64, u64)>,
}

/// The document `data` by spec/zarrify.md §1.6
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#16-json-documents>, or the reason it is rejected.
pub fn parse(data: &[u8]) -> Result<Doc, String> {
    parse_within(data, MAX_DEPTH)
}

/// [`parse`], nesting at most `max_depth` deep (a document the parser built from documents it
/// read, such as a node's source metadata, which nests its attributes one level deeper).
pub fn parse_within(data: &[u8], max_depth: usize) -> Result<Doc, String> {
    if data.len() as u64 > MAX_DOCUMENT {
        return Err(format!("JSON document of {} bytes exceeds {MAX_DOCUMENT}", data.len()));
    }
    if data.starts_with(b"\xef\xbb\xbf") {
        return Err("JSON document starts with a byte order mark".into());
    }
    let text = std::str::from_utf8(data).map_err(|_| "JSON document is not UTF-8".to_string())?;
    let mut p = P { s: text.as_bytes(), at: 0, depth: 0, max_depth, members: Vec::new() };
    p.ws();
    let value = p.value(true)?;
    p.ws();
    if p.at != p.s.len() {
        return Err(p.fail("extra data"));
    }
    Ok(Doc { value, members: p.members })
}

struct P<'a> {
    s: &'a [u8],
    at: usize,
    depth: usize,
    max_depth: usize,
    members: Vec<(String, u64, u64)>,
}

impl P<'_> {
    fn fail(&self, why: &str) -> String {
        format!("invalid JSON: {why} at byte {}", self.at)
    }

    fn ws(&mut self) {
        while self.at < self.s.len() && matches!(self.s[self.at], b' ' | b'\t' | b'\n' | b'\r') {
            self.at += 1;
        }
    }

    fn peek(&self) -> Option<u8> {
        self.s.get(self.at).copied()
    }

    fn lit(&mut self, t: &str) -> bool {
        if self.s[self.at..].starts_with(t.as_bytes()) {
            self.at += t.len();
            true
        } else {
            false
        }
    }

    fn value(&mut self, top: bool) -> Result<Jv, String> {
        match self.peek() {
            None => Err(self.fail("expecting a value")),
            Some(b'{') => self.object(top),
            Some(b'[') => self.array(),
            Some(b'"') => Ok(Jv::Str(self.string()?)),
            Some(b't') if self.lit("true") => Ok(Jv::Bool(true)),
            Some(b'f') if self.lit("false") => Ok(Jv::Bool(false)),
            Some(b'n') if self.lit("null") => Ok(Jv::Null),
            Some(b'N') if self.lit("NaN") => Err("JSON literal NaN is not allowed".into()),
            Some(b'I') if self.lit("Infinity") => Err("JSON literal Infinity is not allowed".into()),
            Some(b'-') if self.s[self.at..].starts_with(b"-Infinity") => Err("JSON literal -Infinity is not allowed".into()),
            Some(c) if c == b'-' || c.is_ascii_digit() => self.number(),
            Some(_) => Err(self.fail("expecting a value")),
        }
    }

    fn enter(&mut self) -> Result<(), String> {
        self.depth += 1;
        if self.depth > self.max_depth {
            return Err(format!("JSON nests more than {} deep", self.max_depth));
        }
        self.at += 1;
        Ok(())
    }

    fn object(&mut self, top: bool) -> Result<Jv, String> {
        self.enter()?;
        let mut m: Vec<(Js, Jv)> = Vec::new();
        let mut index: HashMap<(Vec<u32>, String), usize> = HashMap::new();
        self.ws();
        if self.peek() == Some(b'}') {
            self.at += 1;
            self.depth -= 1;
            return Ok(Jv::Obj(m));
        }
        loop {
            self.ws();
            if self.peek() != Some(b'"') {
                return Err(self.fail("expecting a property name in double quotes"));
            }
            let k = self.string()?;
            self.ws();
            if self.peek() != Some(b':') {
                return Err(self.fail("expecting ':'"));
            }
            self.at += 1;
            self.ws();
            let start = self.at;
            let v = self.value(false)?;
            if top {
                self.members.push((k.text.clone(), start as u64, self.at as u64));
            }
            match index.get(&(k.lone.clone(), k.text.clone())) {
                Some(&i) => m[i].1 = v, // the last one written, in the place of the first
                None => {
                    index.insert((k.lone.clone(), k.text.clone()), m.len());
                    m.push((k, v));
                }
            }
            self.ws();
            match self.peek() {
                Some(b',') => self.at += 1,
                Some(b'}') => {
                    self.at += 1;
                    break;
                }
                _ => return Err(self.fail("expecting ',' or '}'")),
            }
        }
        self.depth -= 1;
        Ok(Jv::Obj(m))
    }

    fn array(&mut self) -> Result<Jv, String> {
        self.enter()?;
        let mut a = Vec::new();
        self.ws();
        if self.peek() == Some(b']') {
            self.at += 1;
            self.depth -= 1;
            return Ok(Jv::Arr(a));
        }
        loop {
            self.ws();
            a.push(self.value(false)?);
            self.ws();
            match self.peek() {
                Some(b',') => self.at += 1,
                Some(b']') => {
                    self.at += 1;
                    break;
                }
                _ => return Err(self.fail("expecting ',' or ']'")),
            }
        }
        self.depth -= 1;
        Ok(Jv::Arr(a))
    }

    fn hex4(&mut self) -> Result<u32, String> {
        let h = self.s.get(self.at..self.at + 4).ok_or_else(|| self.fail("invalid \\u escape"))?;
        let t = std::str::from_utf8(h).map_err(|_| self.fail("invalid \\u escape"))?;
        if !t.bytes().all(|c| c.is_ascii_hexdigit()) {
            return Err(self.fail("invalid \\u escape"));
        }
        self.at += 4;
        Ok(u32::from_str_radix(t, 16).unwrap())
    }

    fn string(&mut self) -> Result<Js, String> {
        let start = self.at;
        self.at += 1;
        let mut text = String::new();
        let mut lone = Vec::new();
        loop {
            // the plain bytes up to the next quote, backslash or control character, at once (the
            // document is UTF-8, and these are ASCII, so the run ends on a character boundary)
            let run = self.s[self.at..].iter().position(|&c| c == b'"' || c == b'\\' || c < 0x20)
                .ok_or_else(|| { self.at = self.s.len(); self.fail("unterminated string") })?;
            text.push_str(std::str::from_utf8(&self.s[self.at..self.at + run]).unwrap());
            self.at += run;
            match self.s[self.at] {
                b'"' => {
                    self.at += 1;
                    break;
                }
                b'\\' => {
                    self.at += 1;
                    let e = self.peek().ok_or_else(|| self.fail("unterminated string"))?;
                    self.at += 1;
                    match e {
                        b'"' => text.push('"'),
                        b'\\' => text.push('\\'),
                        b'/' => text.push('/'),
                        b'b' => text.push('\u{8}'),
                        b'f' => text.push('\u{c}'),
                        b'n' => text.push('\n'),
                        b'r' => text.push('\r'),
                        b't' => text.push('\t'),
                        b'u' => {
                            let u = self.hex4()?;
                            if (0xd800..0xdc00).contains(&u) && self.s[self.at..].starts_with(b"\\u") {
                                let back = self.at;
                                self.at += 2;
                                let l = self.hex4()?;
                                if (0xdc00..0xe000).contains(&l) {
                                    text.push(char::from_u32(0x10000 + ((u - 0xd800) << 10) + (l - 0xdc00)).unwrap());
                                    continue;
                                }
                                self.at = back;
                            }
                            match char::from_u32(u) {
                                Some(ch) => text.push(ch),
                                None => {
                                    // an unpaired surrogate
                                    lone.push(text.len() as u32);
                                    text.push('\u{FFFD}');
                                    text.push_str(&format!("{u:04x}"));
                                }
                            }
                        }
                        _ => return Err(self.fail("invalid escape")),
                    }
                }
                _ => return Err(self.fail("invalid control character in a string")),
            }
        }
        let raw = std::str::from_utf8(&self.s[start..self.at]).unwrap().to_string();
        Ok(Js { text, raw, lone })
    }

    fn number(&mut self) -> Result<Jv, String> {
        let start = self.at;
        let s = self.s;
        let mut i = self.at;
        if s.get(i) == Some(&b'-') {
            i += 1;
        }
        match s.get(i) {
            Some(b'0') => i += 1,
            Some(c) if c.is_ascii_digit() => {
                while s.get(i).is_some_and(|c| c.is_ascii_digit()) {
                    i += 1;
                }
            }
            _ => return Err(self.fail("expecting a value")),
        }
        let mut int = true;
        if s.get(i) == Some(&b'.') && s.get(i + 1).is_some_and(|c| c.is_ascii_digit()) {
            int = false;
            i += 1;
            while s.get(i).is_some_and(|c| c.is_ascii_digit()) {
                i += 1;
            }
        }
        if matches!(s.get(i), Some(b'e' | b'E')) {
            let mut j = i + 1;
            if matches!(s.get(j), Some(b'+' | b'-')) {
                j += 1;
            }
            if s.get(j).is_some_and(|c| c.is_ascii_digit()) {
                int = false;
                i = j;
                while s.get(i).is_some_and(|c| c.is_ascii_digit()) {
                    i += 1;
                }
            }
        }
        self.at = i;
        let t = std::str::from_utf8(&s[start..i]).unwrap();
        let x: f64 = t.parse().map_err(|_| self.fail("invalid number"))?;
        if !x.is_finite() {
            return Err(format!("JSON number {} is not finite in binary64", &t[..t.len().min(40)]));
        }
        if int {
            let d = if t == "-0" { "0" } else { t };
            return Ok(Jv::Int(d.to_string()));
        }
        Ok(Jv::Float(x))
    }
}

/// The top-level members a document may have and be held as a value per member; one with more
/// is one value, `json` (the view would show only the first few thousand of them anyway, and
/// the rows of distinct names do not fold).
pub const MAX_MEMBERS: usize = 4096;

/// The names a member cannot take plainly: the gaps' (`gaps/<o>`) and the whole document's.
pub const FIXED_NAMES: [&str; 2] = ["gaps", "json"];

/// Holds a store's document, the object `o` of `n` bytes, in its IR (conventions §8.9
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#89-stores>): a value per
/// top-level member (`members`, from [`parse`]: its value's text, `ascii[n]`, named by its key
/// as [`crate::ir::source_names`] names texts, with [`FIXED_NAMES`] fixed), the rest gaps
/// ([`crate::check::finish`] fills them); a document of no member is one gap, one of more than
/// [`MAX_MEMBERS`] one value, `json`.
pub fn hold(ir: &mut crate::ir::Ir, o: u32, n: u64, members: &[(String, u64, u64)]) -> Result<(), String> {
    use crate::ir::{source_names, GAP, NO_INDEX};
    if members.is_empty() {
        ir.add(GAP, o, "gaps/", 0, "", 0, Some((0, n)))?;
        return Ok(());
    }
    if members.len() > MAX_MEMBERS {
        ir.value(o, "json", NO_INDEX, &format!("ascii[{n}]"), 0, (0, n), None)?;
        return Ok(());
    }
    let texts: Vec<&str> = members.iter().map(|m| m.0.as_str()).collect();
    for ((name, nidx), (_, a, e)) in source_names(&texts, &FIXED_NAMES).into_iter().zip(members) {
        ir.value(o, &name, nidx, &format!("ascii[{}]", e - a), 0, (*a, e - a), None)?;
    }
    Ok(())
}

impl Jv {
    /// Whether an object has the member `k` (`null` included).
    pub fn has(&self, k: &str) -> bool {
        self.get(k).is_some()
    }

    pub fn is_obj(&self) -> bool {
        matches!(self, Jv::Obj(_))
    }

    /// An array's items.
    pub fn items(&self) -> Option<&[Jv]> {
        match self {
            Jv::Arr(a) => Some(a),
            _ => None,
        }
    }

    /// An object's members but those named in `drop`.
    pub fn without(&self, drop: &[&str]) -> Vec<(Js, Jv)> {
        self.members().iter().filter(|(k, _)| !(k.lone.is_empty() && drop.contains(&k.text.as_str()))).cloned().collect()
    }

    /// An integer, as an integer literal.
    pub fn integer(v: i64) -> Jv {
        Jv::Int(v.to_string())
    }
}
