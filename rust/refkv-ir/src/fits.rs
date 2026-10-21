//! A sans-IO parser of FITS files (images, multi-extension files, binary and ASCII tables)
//! into the IR (spec/virtualize/fits.md §15
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/fits.md#15-fits-profile>, and the
//! source model of §6.1
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/fits.md#61-elements>).
//!
//! Rounds: a window of up to 64 KiB at each HDU's first byte, extended by windows until the
//! header's END card is found; the data is never read. The schema (`schema/fits.json`) holds
//! every check, in the profile's order. The IR is one struct per HDU: its header (one value
//! of 80-byte cards), its data (an image's values; a table's rows, gap and heap) and its
//! padding, and a gap for any bytes after the last HDU (special records).

use crate::check;
use crate::ir::*;
use crate::nd2::Step;
use crate::rules::{self, Rules, Vars, flag, num, text};
use serde_json::{json, Value as J};

/// A FITS block, and a card.
pub const BLOCK: u64 = 2880;
pub const CARD: usize = 80;
const CARDS_PER_BLOCK: u64 = 36;
/// The bytes read at a time while looking for a header's END card.
pub const WINDOW: u64 = 1 << 16;

type Res<T> = Result<T, String>;

/// Whether a file's first bytes are a FITS file's (spec/virtualize.md §1.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize.md#12-input>): `SIMPLE  =` and a
/// fixed-format `T` in column 30.
pub fn detect(head: &[u8]) -> bool {
    head.len() >= 30 && &head[..9] == b"SIMPLE  =" && head[29] == b'T'
}

/// A card's value (FITS Standard 4.0 §4.2).
#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    /// no value: a commentary card, or a value indicator with nothing after it
    None,
    Logical(bool),
    /// an integer, exactly (an optional sign and digits, within i128)
    Int(i128),
    /// a real: its binary64 value (a `D` exponent read as `E`) and its text
    Real(f64, String),
    /// a string: quotes undoubled, trailing spaces trimmed, `CONTINUE` cards joined
    Str(String),
    Complex(f64, f64),
    /// a value field the standard does not define, as text
    Unparsed(String),
}

/// A card: its keyword (a `HIERARCH` card's as written after `HIERARCH`, up to `=`), its
/// value and its comment.
#[derive(Clone, Debug, PartialEq)]
pub struct Card {
    pub keyword: String,
    pub value: Value,
    pub comment: Option<String>,
}

fn integer(v: &str) -> Option<i128> {
    let digits = v.strip_prefix(['+', '-']).unwrap_or(v);
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    v.parse().ok()
}

fn real(v: &str) -> Option<f64> {
    let v = v.trim();
    if v.is_empty() || !v.bytes().all(|b| b.is_ascii_digit() || matches!(b, b'+' | b'-' | b'.' | b'E' | b'e' | b'D' | b'd')) {
        return None;
    }
    v.replace(['D', 'd'], "E").parse().ok()
}

/// The comment after a value: the text after `/`, trimmed; none when the rest is not a
/// comment.
fn comment_of(after: &str) -> Option<String> {
    after.trim_start().strip_prefix('/').map(|c| c.trim().to_string())
}

/// A value field (bytes 11 to 80 of a card) as a value and a comment.
pub fn parse_value(field: &str) -> (Value, Option<String>) {
    let t = field.trim_start();
    if t.is_empty() {
        return (Value::None, None);
    }
    if let Some(rest) = t.strip_prefix('\'') {
        let cs: Vec<char> = rest.chars().collect();
        let (mut out, mut i, mut closed) = (String::new(), 0, false);
        while i < cs.len() {
            if cs[i] == '\'' {
                if cs.get(i + 1) == Some(&'\'') {
                    out.push('\'');
                    i += 2;
                    continue;
                }
                closed = true;
                i += 1;
                break;
            }
            out.push(cs[i]);
            i += 1;
        }
        if !closed {
            return (Value::Unparsed(t.trim_end().to_string()), None);
        }
        let after: String = cs[i..].iter().collect();
        return (Value::Str(out.trim_end().to_string()), comment_of(&after));
    }
    if t.starts_with('/') {
        return (Value::None, comment_of(t));
    }
    let (val, after) = if t.starts_with('(') {
        match t.find(')') {
            Some(k) => (&t[..=k], &t[k + 1..]),
            None => (t, ""),
        }
    } else {
        match t.find('/') {
            Some(k) => (&t[..k], &t[k..]),
            None => (t, ""),
        }
    };
    let val = val.trim();
    let value = match val {
        "T" => Value::Logical(true),
        "F" => Value::Logical(false),
        v if v.starts_with('(') && v.ends_with(')') => {
            let parts: Vec<&str> = v[1..v.len() - 1].split(',').collect();
            match (parts.len(), parts.first().and_then(|p| real(p)), parts.get(1).and_then(|p| real(p))) {
                (2, Some(r), Some(i)) => Value::Complex(r, i),
                _ => Value::Unparsed(v.to_string()),
            }
        }
        v => {
            if let Some(n) = integer(v) {
                Value::Int(n)
            } else if let Some(x) = real(v) {
                Value::Real(x, v.to_string())
            } else {
                Value::Unparsed(v.to_string())
            }
        }
    };
    (value, comment_of(after))
}

/// One 80-byte record as a card. A `CONTINUE` card is a card whose keyword is `CONTINUE`
/// and whose value is the string it continues (joined by `parse_cards`).
pub fn parse_card(rec: &[u8]) -> Card {
    let s: String = rec[..CARD].iter().map(|&b| b as char).collect();
    let keyword = s[..8].trim_end().to_string();
    if keyword == "HIERARCH" && &s[8..10] != "= " {
        if let Some(eq) = s[9..].find('=') {
            let name = s[9..9 + eq].trim().to_string();
            let (value, comment) = parse_value(&s[10 + eq..]);
            return Card { keyword: name, value, comment };
        }
    } else if &s[8..10] == "= " {
        let (value, comment) = parse_value(&s[10..]);
        return Card { keyword, value, comment };
    } else if keyword == "CONTINUE" {
        let (value, comment) = parse_value(&s[10..]);
        if let Value::Str(_) = value {
            return Card { keyword, value, comment };
        }
    }
    let comment = s[8..].trim_end().to_string();
    Card { keyword, value: Value::None, comment: Some(comment) }
}

/// The cards of a header's records up to (not including) END, `CONTINUE` cards joined to
/// the string they continue (FITS Standard 4.0 §4.2.1.2).
pub fn parse_cards(records: &[u8], count: usize) -> Vec<Card> {
    let mut out: Vec<Card> = Vec::with_capacity(count);
    for k in 0..count {
        let card = parse_card(&records[k * CARD..]);
        if card.keyword == "CONTINUE" {
            if let (Some(last), Value::Str(more)) = (out.last_mut(), &card.value) {
                if let Value::Str(s) = &mut last.value {
                    if s.ends_with('&') {
                        s.pop();
                        s.push_str(more);
                        match (&mut last.comment, &card.comment) {
                            (Some(a), Some(b)) if !b.is_empty() => {
                                if !a.is_empty() {
                                    a.push(' ');
                                }
                                a.push_str(b);
                            }
                            (None, Some(b)) => last.comment = Some(b.clone()),
                            _ => {}
                        }
                        continue;
                    }
                }
            }
        }
        out.push(card);
    }
    for c in &mut out {
        if let Value::Str(s) = &mut c.value {
            let n = s.trim_end().len();
            s.truncate(n);
        }
    }
    out
}

/// The first card of `keyword`.
pub fn card<'a>(cards: &'a [Card], keyword: &str) -> Option<&'a Card> {
    cards.iter().find(|c| c.keyword == keyword)
}

#[derive(Clone, Copy, PartialEq, Debug)]
enum Phase {
    Start,
    Hdu,
    Done,
}

pub struct Fits {
    size: u64,
    phase: Phase,
    /// the bytes fed from `buf_start`, contiguous
    buf: Vec<u8>,
    buf_start: u64,
    /// the HDU being read: its first byte and its index
    offset: u64,
    index: u64,
    ir: Ir,
    rules: &'static Rules,
    hdus: Vec<J>,
}

impl Fits {
    pub fn new(size: u64) -> Self {
        Fits { size, phase: Phase::Start, buf: Vec::new(), buf_start: 0, offset: 0, index: 0, ir: Ir::new(size), rules: rules::fits(), hdus: Vec::new() }
    }

    pub fn feed(&mut self, offset: u64, data: Vec<u8>) {
        if offset == self.buf_start + self.buf.len() as u64 {
            self.buf.extend_from_slice(&data);
        } else if offset == self.buf_start && data.len() >= self.buf.len() {
            self.buf = data;
        } else if offset > self.buf_start && offset < self.buf_start + self.buf.len() as u64 {
            self.buf.truncate((offset - self.buf_start) as usize);
            self.buf.extend_from_slice(&data);
        } else {
            self.buf_start = offset;
            self.buf = data;
        }
    }

    fn check(&self, role: &str, mut env: Vars<'static>) -> Res<()> {
        self.rules.check(role, &mut env)
    }

    fn limit(&self, name: &str) -> u64 {
        self.rules.limit(name) as u64
    }

    pub fn step(&mut self) -> Result<Step, String> {
        loop {
            match self.phase {
                Phase::Start => {
                    self.phase = Phase::Hdu;
                    return Ok(Step::Read(vec![(0, self.size.min(WINDOW))]));
                }
                Phase::Hdu => {
                    let left = self.size - self.offset;
                    if self.index > 0 && left < CARD as u64 {
                        // too short for a card: special records, a gap
                        self.phase = Phase::Done;
                        continue;
                    }
                    if self.offset < self.buf_start || self.offset > self.buf_start + self.buf.len() as u64 {
                        self.buf.clear();
                        self.buf_start = self.offset;
                    } else if self.offset > self.buf_start {
                        self.buf.drain(..(self.offset - self.buf_start) as usize);
                        self.buf_start = self.offset;
                    }
                    let have = self.buf.len() as u64;
                    let want = left.min(WINDOW);
                    if have < want.min(CARD as u64) {
                        return Ok(Step::Read(vec![(self.offset + have, want - have)]));
                    }
                    if self.index == 0 {
                        if !detect(&self.buf) {
                            return Err("not a FITS file".into());
                        }
                        self.check("file", rules::env([("size", num(self.size as f64))]))?;
                    } else if &self.buf[..9] != b"XTENSION=" {
                        // not an extension: special records, a gap
                        self.phase = Phase::Done;
                        continue;
                    }
                    let limit = left.min(self.limit("max_header"));
                    match self.scan()? {
                        Some(records) => {
                            let header_bytes = (records as u64).div_ceil(CARDS_PER_BLOCK) * BLOCK;
                            let need = header_bytes.min(left);
                            if have < need {
                                return Ok(Step::Read(vec![(self.offset + have, (need - have).min(WINDOW))]));
                            }
                            self.hdu(records, header_bytes)?;
                        }
                        None => {
                            if have < limit {
                                return Ok(Step::Read(vec![(self.offset + have, (limit - have).min(WINDOW))]));
                            }
                            self.check(
                                "header_end",
                                rules::env([("hdu", num(self.index as f64)), ("offset", num(self.offset as f64)), ("scanned", num(limit as f64)), ("found", flag(false))]),
                            )?;
                        }
                    }
                }
                Phase::Done => return Ok(Step::Done),
            }
        }
    }

    /// The records of the header in `buf` up to and including END, when END is there; each
    /// scanned record is checked to be printable ASCII (§15.3
    /// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/fits.md#153-checks>).
    fn scan(&self) -> Res<Option<usize>> {
        let mut k = 0;
        while (k + 1) * CARD <= self.buf.len() {
            let rec = &self.buf[k * CARD..(k + 1) * CARD];
            if let Some(at) = rec.iter().position(|&b| !(32..=126).contains(&b)) {
                self.check(
                    "card_text",
                    rules::env([
                        ("hdu", num(self.index as f64)),
                        ("card", num(k as f64)),
                        ("byte", num(at as f64)),
                        ("value", num(rec[at] as f64)),
                        ("shown", text(&format!("0x{:02x}", rec[at]))),
                    ]),
                )?;
            }
            if &rec[..8] == b"END     " {
                return Ok(Some(k + 1));
            }
            k += 1;
        }
        Ok(None)
    }

    fn mandatory(&self, cards: &[Card], position: usize, expected: &str) -> Res<()> {
        let keyword = cards.get(position).map_or("END", |c| c.keyword.as_str());
        self.check(
            "mandatory",
            rules::env([("hdu", num(self.index as f64)), ("position", num(position as f64)), ("keyword", text(keyword)), ("expected", text(expected))]),
        )
    }

    /// The integer value of the card at `position` (mandatory, and an integer).
    fn mandatory_int(&self, cards: &[Card], position: usize, expected: &str) -> Res<i128> {
        self.mandatory(cards, position, expected)?;
        self.int_of(&cards[position])
    }

    fn int_of(&self, c: &Card) -> Res<i128> {
        let (is_int, shown) = match &c.value {
            Value::Int(n) => (true, n.to_string()),
            other => (false, value_text(other)),
        };
        self.check(
            "integer_card",
            rules::env([("hdu", num(self.index as f64)), ("keyword", text(&c.keyword)), ("value", text(&shown)), ("is_int", flag(is_int))]),
        )?;
        match c.value {
            Value::Int(n) => Ok(n),
            _ => Err("internal: not an integer".into()),
        }
    }

    fn is_true(cards: &[Card], keyword: &str) -> bool {
        matches!(card(cards, keyword).map(|c| &c.value), Some(Value::Logical(true)))
    }

    /// The HDU whose header is the first `records` records of `buf` (END included), with its
    /// checks (§15.3 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/fits.md#153-checks>),
    /// then its elements (§6.1
    /// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/fits.md#61-elements>).
    fn hdu(&mut self, records: usize, header_bytes: u64) -> Res<()> {
        let hdu = self.index as f64;
        let cards = parse_cards(&self.buf, records - 1);
        let primary = self.index == 0;
        self.mandatory(&cards, 0, if primary { "SIMPLE" } else { "XTENSION" })?;
        let bitpix = self.mandatory_int(&cards, 1, "BITPIX")?;
        self.check("bitpix", rules::env([("hdu", num(hdu)), ("value", num(bitpix as f64))]))?;
        let naxis = self.mandatory_int(&cards, 2, "NAXIS")?;
        self.check("naxis", rules::env([("hdu", num(hdu)), ("value", num(naxis as f64))]))?;
        let mut axes = Vec::with_capacity(naxis as usize);
        for n in 1..=naxis as usize {
            let v = self.mandatory_int(&cards, 2 + n, &format!("NAXIS{n}"))?;
            self.check("naxisn", rules::env([("hdu", num(hdu)), ("n", num(n as f64)), ("value", num(v as f64))]))?;
            axes.push(v as u64);
        }
        let (xtension, kind) = if primary {
            ("PRIMARY".to_string(), "image")
        } else {
            let name = match &cards[0].value {
                Value::Str(s) => s.trim().to_string(),
                other => value_text(other),
            };
            let mut env = rules::env([("hdu", num(hdu)), ("name", text(&name))]);
            self.rules.check("xtension", &mut env)?;
            let kind = match env.0.iter().find(|(k, _)| *k == "kind").map(|(_, v)| v.clone()) {
                Some(crate::expr::V::Str(s)) if s == "image" => "image",
                _ => "table",
            };
            (name, kind)
        };
        let after = 3 + naxis as usize;
        let (pcount, gcount) = if primary {
            (0i128, 1i128)
        } else {
            let p = self.mandatory_int(&cards, after, "PCOUNT")?;
            self.check("pcount", rules::env([("hdu", num(hdu)), ("value", num(p as f64))]))?;
            let g = self.mandatory_int(&cards, after + 1, "GCOUNT")?;
            self.check("gcount", rules::env([("hdu", num(hdu)), ("value", num(g as f64))]))?;
            (p, g)
        };
        self.check("groups", rules::env([("hdu", num(hdu)), ("groups", flag(Self::is_true(&cards, "GROUPS")))]))?;
        // the data's size, exactly (FITS Standard 4.0 §4.4.1.1, §7.1.1)
        let spec = &self.rules.table("data_types")[bitpix.to_string()];
        let item = spec["bytes"].as_u64().unwrap_or(1) as u128;
        let total: u128 = if naxis == 0 {
            0
        } else {
            let prod = axes.iter().fold(1u128, |a, &s| a.saturating_mul(s as u128));
            item.saturating_mul(gcount as u128).saturating_mul((pcount as u128).saturating_add(prod))
        };
        let data_offset = self.offset + header_bytes;
        self.check(
            "data_size",
            rules::env([
                ("hdu", num(hdu)),
                ("total", num(total as f64)),
                ("total_shown", text(&total.to_string())),
                ("offset", num(data_offset as f64)),
                ("size", num(self.size as f64)),
            ]),
        )?;
        let total = total as u64;
        // tables (§15.3): BITPIX 8, NAXIS 2, TFIELDS and a TFORMn per column, the heap's place
        let mut table = J::Null;
        if kind == "table" {
            let tfields = self.mandatory_int(&cards, after + 2, "TFIELDS")?;
            self.check(
                "table",
                rules::env([("hdu", num(hdu)), ("xtension", text(&xtension)), ("bitpix", num(bitpix as f64)), ("naxis", num(naxis as f64)), ("tfields", num(tfields as f64))]),
            )?;
            let mut forms = Vec::with_capacity(tfields as usize);
            for n in 1..=tfields {
                let c = card(&cards, &format!("TFORM{n}"));
                self.check("tform", rules::env([("hdu", num(hdu)), ("n", num(n as f64)), ("found", flag(c.is_some()))]))?;
                forms.push(match &c.unwrap().value {
                    Value::Str(s) => s.trim().to_string(),
                    other => value_text(other),
                });
            }
            let rows = axes[0] * axes[1];
            let theap = match card(&cards, "THEAP") {
                Some(c) => self.int_of(c)?,
                None => rows as i128,
            };
            self.check("heap", rules::env([("hdu", num(hdu)), ("theap", num(theap as f64)), ("rows", num(rows as f64)), ("pcount", num(pcount as f64))]))?;
            self.check("ztable", rules::env([("hdu", num(hdu)), ("ztable", flag(Self::is_true(&cards, "ZTABLE")))]))?;
            self.check("zimage", rules::env([("hdu", num(hdu)), ("zimage", flag(Self::is_true(&cards, "ZIMAGE")))]))?;
            let gap = theap as u64 - rows;
            table = json!({"tfields": tfields, "tform": forms, "row_bytes": axes[0], "rows": axes[1], "theap": theap, "gap": gap, "heap": total - rows - gap});
        }
        // the elements
        let data_end = data_offset + total;
        let padded_end = data_offset + total.div_ceil(BLOCK) * BLOCK;
        let hdu_end = padded_end.min(self.size);
        let s = self.ir.struct_(0, "hdus/", self.index, 0, Some((self.offset, hdu_end - self.offset)))?;
        let ty = format!("{{card:ascii[80]}}[{}]", header_bytes / CARD as u64);
        let header = self.ir.value(s, "header", NO_INDEX, &ty, 0, (self.offset, header_bytes), Some(&self.buf[..header_bytes as usize]))?;
        let mut elements = json!({"header": header});
        let empty = naxis == 0;
        if kind == "image" && total > 0 {
            let shape: Vec<u64> = axes.iter().rev().copied().collect();
            let form = json!({"geometry": {"data_type": spec["dtype"], "shape": shape}, "codec": {"endian": "big"}, "recipe": [["src", 0, null]]});
            elements["data"] = json!(self.ir.data(s, "data", NO_INDEX, 0, (data_offset, total), &form.to_string())?);
        } else if kind == "table" {
            let (rows, gap, heap) = (table["row_bytes"].as_u64().unwrap() * table["rows"].as_u64().unwrap(), table["gap"].as_u64().unwrap(), table["heap"].as_u64().unwrap());
            if rows > 0 {
                let form = json!({"geometry": {"rows": table["rows"], "row_bytes": table["row_bytes"]}, "recipe": [["src", 0, null]]});
                elements["rows"] = json!(self.ir.data(s, "rows", NO_INDEX, 0, (data_offset, rows), &form.to_string())?);
            }
            if gap > 0 {
                elements["gap"] = json!(self.ir.value(s, "gap", NO_INDEX, &format!("bytes[{gap}]"), 0, (data_offset + rows, gap), None)?);
            }
            if heap > 0 {
                let form = json!({"geometry": {"bytes": heap}, "recipe": [["src", 0, null]]});
                elements["heap"] = json!(self.ir.data(s, "heap", NO_INDEX, 0, (data_offset + rows + gap, heap), &form.to_string())?);
            }
        }
        let pad = hdu_end.saturating_sub(data_end);
        if pad > 0 {
            elements["pad"] = json!(self.ir.value(s, "pad", NO_INDEX, &format!("bytes[{pad}]"), 0, (data_end, pad), None)?);
        }
        let name = card(&cards, "EXTNAME").and_then(|c| match &c.value {
            Value::Str(s) => Some(s.trim().to_string()),
            _ => None,
        });
        let version = card(&cards, "EXTVER").and_then(|c| match c.value {
            Value::Int(n) => Some(n as i64),
            _ => None,
        });
        self.hdus.push(json!({
            "index": self.index, "type": if primary { "primary".to_string() } else { xtension.to_lowercase() },
            "kind": if empty { "empty" } else { kind }, "offset": self.offset, "header_bytes": header_bytes,
            "cards": records - 1, "data_offset": data_offset, "data_bytes": total, "pad": pad,
            "bitpix": bitpix as i64, "data_type": spec["dtype"], "naxis": axes, "pcount": pcount as i64, "gcount": gcount as i64,
            "name": name, "version": version, "table": table, "elements": elements,
        }));
        // the next HDU (the window's remaining bytes are its first bytes, often all of it)
        self.index += 1;
        self.offset = padded_end;
        if padded_end >= self.size {
            self.phase = Phase::Done;
        } else {
            self.check("hdu_count", rules::env([("count", num(self.index as f64 + 1.0))]))?;
        }
        Ok(())
    }

    pub fn finish(mut self) -> Result<(Ir, J), String> {
        if self.phase != Phase::Done {
            return Err("internal: the parse is not done".into());
        }
        self.ir.sort_values();
        check::finish(&mut self.ir)?;
        let facts = json!({"hdus": self.hdus, "copy_threshold": crate::out::copy_threshold()});
        Ok((self.ir, facts))
    }
}

/// A value as a message shows it.
pub fn value_text(v: &Value) -> String {
    match v {
        Value::None => String::new(),
        Value::Logical(b) => if *b { "T" } else { "F" }.to_string(),
        Value::Int(n) => n.to_string(),
        Value::Real(_, t) | Value::Str(t) | Value::Unparsed(t) => t.clone(),
        Value::Complex(r, i) => format!("({r}, {i})"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rec(s: &str) -> Vec<u8> {
        format!("{s:<80}").into_bytes()
    }

    #[test]
    fn cards_of_every_value_type() {
        let c = parse_card(&rec("SIMPLE  =                    T / conforms to FITS"));
        assert_eq!(c, Card { keyword: "SIMPLE".into(), value: Value::Logical(true), comment: Some("conforms to FITS".into()) });
        assert_eq!(parse_card(&rec("BITPIX  =                  -32")).value, Value::Int(-32));
        assert_eq!(parse_card(&rec("BIGINT  =      123456789012345678901 / big")).value, Value::Int(123456789012345678901));
        assert_eq!(parse_card(&rec("DEXPON  =        1.5000000000D+03 / a real")).value, Value::Real(1500.0, "1.5000000000D+03".into()));
        assert_eq!(parse_card(&rec("CRVAL1  =   229.537988811 / deg")).value, Value::Real(229.537988811, "229.537988811".into()));
        assert_eq!(parse_card(&rec("CPLX    =           (1.5, 2.5) / c")).value, Value::Complex(1.5, 2.5));
        let s = parse_card(&rec("QUOTED  = 'it''s   ' / a string"));
        assert_eq!(s.value, Value::Str("it's".into()));
        assert_eq!(s.comment.as_deref(), Some("a string"));
        assert_eq!(parse_card(&rec("EMPTYSTR= ''")).value, Value::Str(String::new()));
        assert_eq!(parse_card(&rec("NOVALUE =")).value, Value::None);
        assert_eq!(parse_card(&rec("ODD     = 12abc / text")).value, Value::Unparsed("12abc".into()));
        let h = parse_card(&rec("HIERARCH ESO INS OPTI-3 ID = 'ESO#427 ' / a HIERARCH card"));
        assert_eq!(h, Card { keyword: "ESO INS OPTI-3 ID".into(), value: Value::Str("ESO#427".into()), comment: Some("a HIERARCH card".into()) });
        let c = parse_card(&rec("COMMENT a comment card"));
        assert_eq!((c.keyword.as_str(), &c.value, c.comment.as_deref()), ("COMMENT", &Value::None, Some("a comment card")));
        let b = parse_card(&rec("        blank card text"));
        assert_eq!((b.keyword.as_str(), b.comment.as_deref()), ("", Some("blank card text")));
    }

    #[test]
    fn continue_cards_join_long_strings() {
        let mut recs = rec("LONGSTR = 'the first part &' / c1");
        recs.extend(rec("CONTINUE  'and the second &'"));
        recs.extend(rec("CONTINUE  'and the last' / c3"));
        recs.extend(rec("CONTINUE  'orphan'"));
        recs.extend(rec("END"));
        let cards = parse_cards(&recs, 4);
        assert_eq!(cards.len(), 2);
        assert_eq!(cards[0].value, Value::Str("the first part and the second and the last".into()));
        assert_eq!(cards[0].comment.as_deref(), Some("c1 c3"));
        assert_eq!(cards[1].keyword, "CONTINUE");
    }

    #[test]
    fn detects_only_a_fixed_format_simple_t() {
        assert!(detect(&rec("SIMPLE  =                    T")));
        assert!(!detect(&rec("SIMPLE  =                    F")));
        assert!(!detect(&rec("XTENSION= 'IMAGE   '")));
        assert!(!detect(b"SIMPLE  ="));
    }
}
