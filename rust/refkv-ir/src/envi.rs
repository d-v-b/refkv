//! A sans-IO parser of ENVI rasters (spec/zarrify/envi.md, the profile's §8
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#8-envi-profile>) as a store run
//! ([`crate::store::StoreParser`]) whose objects are the header file, which the input names, and
//! the data file beside it, probed by key (conventions §8.9
//! <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#89-stores>). The data file is
//! described in place; the header file, which no chunk references, is held.
//!
//! **Batches.** The header file whole; the probes of the data file's names, one a batch, until one
//! exists (§8.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#83-the-data-file>);
//! and, for a gzip file, its first three bytes. The schema (`schema/envi.json`) is checked as the
//! profile meets each role. The IR is emitted at the end, from what was read, and its array
//! described (`envi/stored.rs`; spec/stored-array.md §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>).

use crate::expr::V;
use crate::ir::{GAP, Ir, NO_INDEX, Res, key_name};
use crate::rules::{self, Rules, flag, num, text};
use crate::store::{StoreParser, StoreStep};
use crate::text::{text_json, utf8_or_latin1};
use serde_json::{Map, Value as J, json};

#[path = "envi/stored.rs"]
mod stored;

/// Whether a file's first bytes are an ENVI header's (spec/zarrify/envi.md §8
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#8-envi-profile>): a first line
/// that is `ENVI`, in any case, with spaces and tabs around it, ended by a LF within them.
pub fn detect(head: &[u8]) -> bool {
    head.iter().position(|&c| c == b'\n').is_some_and(|k| is_magic(strip_cr(&head[..k])))
}

fn is_magic(t: &[u8]) -> bool {
    trim(t).eq_ignore_ascii_case(b"ENVI")
}

fn strip_cr(t: &[u8]) -> &[u8] {
    t.strip_suffix(b"\r").unwrap_or(t)
}

fn shown(b: &[u8]) -> String {
    utf8_or_latin1(b)
}

/// A problem as a role's given value: absent when there is none.
fn problem(p: Option<String>) -> V {
    p.map_or(V::Absent, |p| text(&p))
}

/// `b` without the spaces and tabs around it.
fn trim(b: &[u8]) -> &[u8] {
    let s = b.iter().position(|c| *c != b' ' && *c != b'\t').unwrap_or(b.len());
    let e = b.iter().rposition(|c| *c != b' ' && *c != b'\t').map_or(s, |k| k + 1);
    &b[s..e.max(s)]
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
        let t = if next > end { strip_cr(&b[start..end]) } else { &b[start..end] };
        Some((start, next - start, t))
    })
}

// ---- the header (§2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#2-the-header>)

/// A header's lines and what they say: each line's extent (its line end included), the fields
/// by name (in their order, a repeated name's last value at its first place), where each field's
/// value starts (its first line's position among `lines`, and the value's offset and length in that
/// line, when the value is on one line) and the comments.
#[derive(Debug, Default, Clone)]
pub struct Header {
    pub lines: Vec<(u64, u64)>,
    pub fields: Vec<(String, Vec<u8>)>,
    pub places: Vec<(usize, Option<(u64, u64)>)>,
    pub comments: Vec<Vec<u8>>,
}

impl Header {
    pub fn field(&self, name: &str) -> Option<&[u8]> {
        self.fields.iter().find(|f| f.0 == name).map(|f| f.1.as_slice())
    }

    /// Where the field `name` is: its line's position among `lines`, and, for a value on one
    /// line, its (offset, length) in that line.
    pub fn place(&self, name: &str) -> Option<(usize, Option<(u64, u64)>)> {
        let k = self.fields.iter().position(|f| f.0 == name)?;
        self.places.get(k).copied()
    }
}

/// Parses the header file's lines (spec/zarrify/envi.md §2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#2-the-header>): the magic
/// first, then fields, comments and lines that are neither, a value in braces running on to the
/// line that closes it.
pub fn parse(rules: &Rules, b: &[u8]) -> Res<Header> {
    let mut h = Header::default();
    let mut open: Option<usize> = None; // the field whose braced value runs on
    for (k, (start, n, t)) in lines(b).enumerate() {
        h.lines.push((start as u64, n as u64));
        if k == 0 {
            rules.check("magic", rules::env([("ok", flag(is_magic(t)))]))?;
            continue;
        }
        if let Some(f) = open {
            let v = &mut h.fields[f].1;
            v.push(b'\n');
            v.extend_from_slice(t);
            if trim(t).ends_with(b"}") {
                open = None;
            }
            continue;
        }
        let tt = trim(t);
        if tt.is_empty() {
            continue;
        }
        if tt[0] == b';' {
            h.comments.push(trim(&tt[1..]).to_vec());
            continue;
        }
        let Some(eq) = t.iter().position(|&c| c == b'=') else { continue };
        let key = shown(trim(&t[..eq])).to_ascii_lowercase();
        if key.is_empty() {
            continue;
        }
        let after = &t[eq + 1..];
        let lead = after.iter().take_while(|c| matches!(c, b' ' | b'\t')).count();
        let value = trim(after).to_vec();
        let runs_on = value.first() == Some(&b'{') && !value.ends_with(b"}");
        let place = (k, (!runs_on).then_some(((eq + 1 + lead) as u64, value.len() as u64)));
        let f = match h.fields.iter().position(|f| f.0 == key) {
            Some(f) => {
                h.fields[f].1 = value;
                h.places[f] = place;
                f
            }
            None => {
                h.fields.push((key, value));
                h.places.push(place);
                h.fields.len() - 1
            }
        };
        if runs_on {
            open = Some(f);
        }
    }
    if h.lines.is_empty() {
        rules.check("magic", rules::env([("ok", flag(false))]))?;
    }
    Ok(h)
}

// ---- the fields (§8.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#82-the-fields>)

/// A decimal integer: an optional sign and digits.
fn integer(w: &[u8]) -> Option<i64> {
    let s = std::str::from_utf8(w).ok()?;
    let digits = s.strip_prefix(['-', '+']).unwrap_or(s);
    if digits.is_empty() || !digits.bytes().all(|c| c.is_ascii_digit()) {
        return None;
    }
    s.parse().ok()
}

/// What the header's fields give the layout (spec/zarrify/envi.md §8.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#82-the-fields>), checked.
#[derive(Debug, Clone)]
pub struct Fields {
    pub samples: u64,
    pub lines: u64,
    pub bands: u64,
    pub interleave: String,
    pub code: u64,
    pub item: u64,
    pub big: bool,
    pub offset: u64,
    pub gzip: bool,
}

impl Fields {
    /// The data's shape, outermost first (spec/zarrify/envi.md §4.1
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#41-the-array>).
    pub fn stored_shape(&self) -> Vec<u64> {
        let of = |a: &str| match a {
            "band" => self.bands,
            "line" => self.lines,
            _ => self.samples,
        };
        stored_axes(&self.interleave).iter().map(|a| of(a)).collect()
    }
}

/// The data's axes, outermost first, by interleave.
pub fn stored_axes(interleave: &str) -> [&'static str; 3] {
    match interleave {
        "bil" => ["line", "band", "sample"],
        "bip" => ["line", "sample", "band"],
        _ => ["band", "line", "sample"],
    }
}

/// The value check of a field: a problem rejects, naming the field and its value.
fn check_value(rules: &Rules, field: &str, d: &[u8], p: Option<String>) -> Res<()> {
    rules.check("value", rules::env([("field", text(field)), ("shown", text(&shown(d))), ("problem", problem(p))]))
}

/// The value of a field the header must have.
fn required<'a>(rules: &Rules, h: &'a Header, field: &str) -> Res<&'a [u8]> {
    let d = h.field(field);
    rules.check("required", rules::env([("field", text(field)), ("present", flag(d.is_some()))]))?;
    Ok(d.unwrap_or_default())
}

/// The fields the layout reads, each checked, in this order (spec/zarrify/envi.md §8.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#82-the-fields>).
pub fn fields(rules: &Rules, h: &Header) -> Res<Fields> {
    let max_safe = rules.limit("max_safe") as i64;
    if let Some(d) = h.field("file type") {
        rules.check("file_type", rules::env([("shown", text(&shown(d))), ("ok", flag(d.eq_ignore_ascii_case(b"ENVI Standard")))]))?;
    }
    for f in ["major frame offsets", "minor frame offsets"] {
        rules.check("framed", rules::env([("field", text(f)), ("present", flag(h.field(f).is_some()))]))?;
    }
    let size = |field: &str| -> Res<u64> {
        let d = required(rules, h, field)?;
        let v = integer(d).filter(|v| (1..=max_safe).contains(v));
        check_value(rules, field, d, v.is_none().then(|| "is not an integer from 1 to 2^53 - 1".into()))?;
        Ok(v.unwrap_or(1) as u64)
    };
    let (samples, lines, bands) = (size("samples")?, size("lines")?, size("bands")?);

    let d = required(rules, h, "interleave")?;
    let interleave = shown(d).to_ascii_lowercase();
    let known = rules.entry("interleaves", &interleave).is_some();
    check_value(rules, "interleave", d, (!known).then(|| "is not bsq, bil or bip".into()))?;

    let d = required(rules, h, "data type")?;
    let code = integer(d).filter(|v| *v > 0);
    let spec = code.and_then(|c| rules.entry("types", &c.to_string()));
    check_value(rules, "data type", d, spec.is_none().then(|| "is not the code of an ENVI data type of a flat array (1 to 6, 9, or 12 to 15)".into()))?;
    let spec = spec.ok_or("internal: a data type the schema does not name")?;
    let item = spec["bytes"].as_u64().unwrap_or(1);

    let flag_field = |field: &str| -> Res<bool> {
        let Some(d) = h.field(field) else { return Ok(false) };
        let v = integer(d).filter(|v| (0..=1).contains(v));
        check_value(rules, field, d, v.is_none().then(|| "is not 0 or 1".into()))?;
        Ok(v == Some(1))
    };
    let big = flag_field("byte order")?;
    let offset = match h.field("header offset") {
        Some(d) => {
            let v = integer(d).filter(|v| (0..=max_safe).contains(v));
            check_value(rules, "header offset", d, v.is_none().then(|| "is not an integer from 0 to 2^53 - 1".into()))?;
            v.unwrap_or(0) as u64
        }
        None => 0,
    };
    let gzip = flag_field("file compression")?;
    Ok(Fields { samples, lines, bands, interleave, code: code.unwrap_or(0) as u64, item, big, offset, gzip })
}

/// The keys the profile probes for the data file, in order (spec/zarrify/envi.md §8.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#83-the-data-file>): the
/// header's key less `.hdr`, then that followed by each extension of the schema's table, in
/// capitals when the header's `.HDR` is. None when the key does not end in `.hdr`.
pub fn data_keys(rules: &Rules, header_key: &str) -> Option<Vec<String>> {
    let n = header_key.len();
    let suffix = header_key.get(n.checked_sub(4)?..)?;
    if n <= 4 || !suffix.eq_ignore_ascii_case(".hdr") || header_key.ends_with("/.hdr") {
        return None;
    }
    let stem = &header_key[..n - 4];
    let upper = suffix == ".HDR";
    let mut out = vec![stem.to_string()];
    for e in rules.entry("extensions", "names").and_then(J::as_array).into_iter().flatten().filter_map(J::as_str) {
        out.push(format!("{stem}{}", if upper { e.to_ascii_uppercase() } else { e.to_string() }));
    }
    Some(out)
}

// ---- the run (§8 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#8-envi-profile>)

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Phase {
    Head,
    Probed,
    Data,
    Done,
}

pub struct Envi {
    rules: &'static Rules,
    phase: Phase,
    ir: Option<Ir>,
    keys: Vec<(String, u64)>,
    elems: Vec<u32>,
    head: Vec<u8>,
    header: Option<Header>,
    fields: Option<Fields>,
    /// the keys to probe for the data file, and how many have been
    candidates: Vec<String>,
    probed: usize,
    /// the data file's position among `keys` (0 until found)
    data: usize,
    got: std::collections::HashMap<(usize, u64), Vec<u8>>,
    batches: u64,
}

impl Envi {
    /// The listing's IR, its objects' elements and keys: one key, the header file.
    pub fn new(ir: Ir, objects: Vec<u32>, keys: &[(String, u64)]) -> Res<Envi> {
        if keys.len() != 1 || keys[0].0.contains('/') {
            return Err(format!("an ENVI run starts from the header file alone, not {} objects", keys.len()));
        }
        Ok(Envi {
            rules: rules::of("envi"),
            phase: Phase::Head,
            ir: Some(ir),
            keys: keys.to_vec(),
            elems: objects,
            head: Vec::new(),
            header: None,
            fields: None,
            candidates: Vec::new(),
            probed: 0,
            data: 0,
            got: Default::default(),
            batches: 0,
        })
    }

    /// The header file, read whole: its header and fields, checked.
    fn read_header(&mut self) -> Res<()> {
        let h = parse(self.rules, &self.head)?;
        let f = fields(self.rules, &h)?;
        self.header = Some(h);
        self.fields = Some(f);
        Ok(())
    }

    /// The data's extent in the data file (spec/zarrify/envi.md §8.4
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#84-the-data>): `(start,
    /// length)`, checked.
    fn place_data(&self) -> Res<(u64, u64)> {
        let f = self.fields.as_ref().ok_or("internal: no fields")?;
        let (key, size) = self.keys[self.data].clone();
        let total = f.stored_shape().iter().fold(f.item as u128, |a, &s| a.saturating_mul(s as u128));
        self.rules.check("data", rules::env([
            ("total", num(total as f64)), ("total_shown", text(&total.to_string())), ("offset", num(f.offset as f64)),
            ("size", num(size as f64)), ("key", text(&key)), ("gzip", flag(f.gzip)),
        ]))?;
        Ok(if f.gzip { (0, size) } else { (f.offset, total as u64) })
    }

    /// The IR (spec/zarrify/envi.md §5.3
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#53-elements>) and the
    /// description (§8.7 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#87-the-description>).
    fn emit(&mut self) -> Res<J> {
        let h = self.header.clone().ok_or("internal: no header")?;
        let f = self.fields.clone().ok_or("internal: no fields")?;
        let (start, length) = self.place_data()?;
        let mut ir = self.ir.take().ok_or("internal: no IR")?;
        // the header file, which no chunk references, is held: the run read it whole
        let mut lines = Vec::with_capacity(h.lines.len());
        for (i, &(o, n)) in h.lines.iter().enumerate() {
            lines.push(ir.value(self.elems[0], "lines/", i as u64, &format!("ascii[{n}]"), 0, (o, n), Some(&self.head[o as usize..(o + n) as usize]))?);
        }
        let o = self.elems[self.data];
        ir.describe_in_place(o)?;
        let mut codec = Map::new();
        codec.insert("interleave".into(), json!(f.interleave));
        if f.item > 1 {
            codec.insert("endian".into(), json!(if f.big { "big" } else { "little" }));
        }
        if f.gzip {
            codec.insert("compression".into(), json!("gzip"));
            if f.offset > 0 {
                codec.insert("skip".into(), json!(f.offset));
            }
        }
        // its type as the header's `data type` code gives it, not an output's data type, which is
        // its projection's (conventions §8.5
        // <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#85-forms-and-recipes>)
        let form = json!({"geometry": {"data_type": f.code, "shape": f.stored_shape()}, "codec": codec, "recipe": [["src", 0, null]]});
        let data = ir.data(o, "data", NO_INDEX, 0, (start, length), &form.to_string())?;
        let size = self.keys[self.data].1;
        if start > 0 {
            ir.add(GAP, o, "gaps/", 0, "", 0, Some((0, start)))?;
        }
        if start + length < size {
            ir.add(GAP, o, "gaps/", start + length, "", 0, Some((start + length, size - start - length)))?;
        }
        ir.sort_values();
        crate::check::finish(&mut ir)?;
        // the array, described from the finished IR (`stored.rs`): the parser folds no run and
        // makes no alias, so the elements it made keep their ids
        let line_of = |field: &str| h.place(field).map(|(k, within)| (lines[k], within));
        let reached = stored::Reached {
            header: self.elems[0],
            declarations: ["data type", "byte order", "interleave"].into_iter().filter_map(|f| line_of(f).map(|l| l.0)).collect(),
            shape: f.stored_shape(),
            axes: stored_axes(&f.interleave).map(|a| ["sample", "line", "band"].iter().position(|x| *x == a).unwrap_or(0) as u32).to_vec(),
            item: f.item,
            gzip: f.gzip,
            skip: line_of("header offset").and_then(|(l, w)| w.map(|w| (l, w))).filter(|_| f.gzip && f.offset > 0),
            data,
        };
        let (arrays, chains, map) = stored::described(&reached)?;
        ir.describe(arrays, chains, map)?;
        self.ir = Some(ir);
        let file = |k: usize| json!({"key": self.keys[k].0, "size": self.keys[k].1});
        Ok(json!({
            "header_file": file(0),
            "data_file": file(self.data),
            "fields": J::Object(h.fields.iter().map(|(k, v)| (k.clone(), text_json(v))).collect()),
            "comments": J::Array(h.comments.iter().map(|c| text_json(c)).collect()),
            "samples": f.samples, "lines": f.lines, "bands": f.bands,
            "interleave": f.interleave, "data_type": f.code, "byte_order": u8::from(f.big),
            "header_offset": f.offset, "file_compression": u8::from(f.gzip),
            "data_range": [start, length],
            "elements": {"header": self.elems[0], "data": data},
        }))
    }

    fn advance(&mut self) -> Res<StoreStep> {
        loop {
            match self.phase {
                Phase::Head => {
                    let size = self.keys[0].1;
                    if let Some(b) = self.got.remove(&(0, 0)) {
                        self.head = b;
                    } else {
                        let key = self.keys[0].0.clone();
                        let candidates = data_keys(self.rules, &key);
                        self.rules.check("header_name", rules::env([("key", text(&key)), ("ok", flag(candidates.is_some()))]))?;
                        self.candidates = candidates.unwrap_or_default();
                        let max = self.rules.limit("max_header") as u64;
                        self.rules.check("header_size", rules::env([("size", num(size as f64)), ("within", flag(size <= max))]))?;
                        if size > 0 {
                            self.batches += 1;
                            return Ok(StoreStep::Read(vec![(0, 0, size)]));
                        }
                    }
                    self.read_header()?;
                    self.phase = Phase::Probed;
                    self.batches += 1;
                    self.probed = 1;
                    return Ok(StoreStep::Probe(vec![self.candidates[0].clone()]));
                }
                Phase::Probed => {
                    if self.data == 0 {
                        if self.probed < self.candidates.len() {
                            self.probed += 1;
                            self.batches += 1;
                            return Ok(StoreStep::Probe(vec![self.candidates[self.probed - 1].clone()]));
                        }
                        let tried: Vec<String> = self.candidates.iter().map(|k| format!("\"{k}\"")).collect();
                        self.rules.check("data_object", rules::env([("key", text(&self.keys[0].0)), ("found", flag(false)), ("tried", text(&tried.join(", ")))]))?;
                    }
                    self.place_data()?;
                    self.phase = Phase::Data;
                    if self.fields.as_ref().is_some_and(|f| f.gzip) {
                        self.batches += 1;
                        return Ok(StoreStep::Read(vec![(self.data, 0, 3)]));
                    }
                }
                Phase::Data => {
                    if self.fields.as_ref().is_some_and(|f| f.gzip) {
                        let b = self.got.remove(&(self.data, 0)).ok_or("internal: the gzip header was not read")?;
                        let key = self.keys[self.data].0.clone();
                        self.rules.check("gzip", rules::env([("ok", flag(b == [0x1f, 0x8b, 8])), ("key", text(&key))]))?;
                    }
                    self.phase = Phase::Done;
                    return Ok(StoreStep::Done);
                }
                Phase::Done => return Ok(StoreStep::Done),
            }
        }
    }
}

impl StoreParser for Envi {
    fn step(&mut self) -> Res<StoreStep> {
        self.advance()
    }

    fn feed(&mut self, object: usize, offset: u64, data: &[u8]) {
        self.got.insert((object, offset), data.to_vec());
    }

    fn found(&mut self, key: &str, index: usize, size: Option<u64>) -> Res<Option<u32>> {
        let Some(size) = size else { return Ok(None) };
        let ir = self.ir.as_mut().ok_or("internal: no IR")?;
        let (n, x) = key_name(key, false);
        let e = ir.object(0, &n, x, size)?;
        if index != self.keys.len() {
            return Err("internal: a found object out of order".into());
        }
        self.keys.push((key.to_string(), size));
        self.elems.push(e);
        self.data = index;
        Ok(Some(e))
    }

    /// Every name probed is one a rule only suggests (spec/zarrify/envi.md §8.3
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#83-the-data-file>): one
    /// outside the scope is absent.
    fn optional(&self, _key: &str) -> bool {
        true
    }

    /// The read batches, the probes included: the summary's `batches` (spec/zarrify/envi.md §8.6
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#86-summary>).
    fn counts(&self) -> J {
        json!({"batches": self.batches})
    }

    fn finish(mut self: Box<Self>) -> Res<(Ir, J)> {
        if self.phase != Phase::Done {
            return Err("internal: the parse is not done".into());
        }
        let description = self.emit()?;
        Ok((self.ir.take().ok_or("internal: no IR")?, description))
    }
}

/// An ENVI input's file run: a header file and the data file beside it, zarrified as a store run
/// of the `envi` store format (spec/zarrify/envi.md §8
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#8-envi-profile>), never as a
/// file run: `Format::sniff` names it so that a host sends the file to
/// [`crate::store::StoreRun`].
pub struct Profile;

impl crate::run::FileProfile for Profile {
    /// A first line `ENVI` (`detect`).
    fn detect(&self, head: &[u8]) -> bool {
        detect(head)
    }
    fn parser(&self, _size: u64) -> crate::run::AnyParser {
        crate::run::AnyParser::refused("an ENVI header is zarrified as a store run, with its data file")
    }
    fn project(&self, _: &Ir, _: &J, _: &str, _: &crate::project::Copied, _chunking: &crate::out::Chunking) -> Result<crate::out::Out, String> {
        Err("an ENVI input is zarrified as a store run".into())
    }
}

/// The parser of the profile's store run (`StoreFormat`; crate::store).
pub fn store_parser(ir: crate::ir::Ir, objects: Vec<u32>, keys: &[(String, u64)], _chunking: &crate::out::Chunking) -> crate::ir::Res<Box<dyn crate::store::StoreParser>> {
    Ok(Box::new(Envi::new(ir, objects, keys)?))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The signature is a first line `ENVI`, in any case, spaces and tabs around it, ended by a
    /// LF (a CR before it allowed).
    #[test]
    fn the_signature_is_a_first_line_envi() {
        for (head, want) in [
            (&b"ENVI\nsamples = 1\n"[..], true), (b"ENVI\r\n", true), (b"envi \t\n", true), (b" ENVI\n", true),
            (b"ENVI", false), (b"ENVIX\n", false), (b"NRRD0004\n", false), (b"\nENVI\n", false),
        ] {
            assert_eq!(detect(head), want, "{:?}", String::from_utf8_lossy(head));
        }
    }

    /// Fields are named in lower case, without the spaces around them; a braced value runs on to
    /// the line that closes it, its lines joined by LF; a repeated name keeps its first place and
    /// its last value; comments are kept; lines that are neither are not fields.
    #[test]
    fn the_header_is_fields_comments_and_braced_values() {
        let rules = rules::of("envi");
        let h = parse(rules, b"ENVI\r\nDescription = {a,\r\n  b}\r\n; note\r\nnot a field\r\nsamples=3\r\nSAMPLES = 4\r\n").unwrap();
        assert_eq!(h.fields, vec![("description".to_string(), b"{a,\n  b}".to_vec()), ("samples".to_string(), b"4".to_vec())]);
        assert_eq!(h.comments, vec![b"note".to_vec()]);
        assert_eq!(h.place("samples"), Some((6, Some((10, 1)))));
        assert_eq!(h.place("description"), Some((1, None)));
        assert_eq!(h.lines.len(), 7);
    }

    /// The data file's keys: the header's less `.hdr`, then each extension, in capitals for
    /// `.HDR`; none for a key that does not end in `.hdr`.
    #[test]
    fn the_data_file_is_probed_by_the_headers_name() {
        let rules = rules::of("envi");
        let k = data_keys(rules, "scene.img.hdr").unwrap();
        assert_eq!(k[0], "scene.img");
        assert_eq!(k[1], "scene.img.img");
        assert_eq!(data_keys(rules, "A.HDR").unwrap()[..3], ["A".to_string(), "A.IMG".into(), "A.DAT".into()]);
        assert_eq!(data_keys(rules, "a.Hdr").unwrap()[1], "a.img");
        assert_eq!(data_keys(rules, ".hdr"), None);
        assert_eq!(data_keys(rules, "a.txt"), None);
        assert_eq!(data_keys(rules, "hdr"), None);
    }
}
