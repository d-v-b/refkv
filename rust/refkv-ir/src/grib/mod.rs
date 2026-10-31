//! A sans-IO parser of GRIB edition 2 files (spec/zarrify/grib.md, the profile's §8
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#8-grib-profile>) as a store run
//! ([`crate::store::StoreParser`]) whose objects are the GRIB file and, when one exists beside it,
//! its index: NOAA's `<file>.idx` or ECMWF's `<stem>.index`, probed by key (conventions §8.9
//! <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#89-stores>). The GRIB file, whose
//! messages the chunks reference, is described in place; an index, which no chunk references, is
//! held.
//!
//! **Batches.** The probe of the index keys; the GRIB file's first window, the index read whole and
//! the GRIB file's last 4 bytes; then, with an index, the head of every message it lists, at once,
//! each from 4 bytes before the message so that it holds the previous message's end; without one
//! (or once the index and the file disagree), one window at a time from where the last message
//! ended. Then the sections a head did not hold (a long section 2, 3 or 4, the header of a section
//! after a long bitmap). The index is a hint: every message is read and checked whichever way its
//! offset was found, so the output is the same with or without it. The schema
//! (`schema/grib.json`) holds every check, in the profile's order.

pub mod codec;
pub mod meta;
pub mod names;
pub mod sections;

use crate::fed::{Fed, Overlap};
use crate::ir::{GAP, Ir, NO_INDEX, Res, key_name};
use crate::rules::{self, Rules, flag, num, text};
use crate::store::{StoreParser, StoreStep};
use meta::{Grid, Ident, Product};
use sections::{END, INDICATOR, Section};
use serde_json::{Map, Value as J, json};
use std::collections::BTreeMap;

/// The window read at a message's start when its offset is known from an index.
const HEAD: u64 = 1 << 14;
/// The window read at the next message's start when walking the file.
const WALK: u64 = 1 << 16;

/// Whether a file's first bytes are a GRIB edition 2 message's: `GRIB`, then edition 2 at
/// octet 8 (spec/zarrify/grib.md §8.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#81-choosing-the-profile>).
pub fn detect(head: &[u8]) -> bool {
    head.len() >= 8 && &head[..4] == b"GRIB" && head[7] == 2
}

/// The index keys probed beside the file named `key` (spec/zarrify/grib.md §8.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#82-finding-the-messages>):
/// `<key>.idx`, and, when `key` ends in `.grib2`, `.grb2`, `.grib` or `.grb`, the key with that
/// suffix replaced by `.index`.
pub fn index_keys(key: &str) -> Vec<String> {
    let mut out = vec![format!("{key}.idx")];
    for s in [".grib2", ".grb2", ".grib", ".grb"] {
        if let Some(stem) = key.strip_suffix(s).filter(|x| !x.is_empty() && !x.ends_with('/')) {
            out.push(format!("{stem}.index"));
            break;
        }
    }
    out
}

/// The message offsets an index lists, sorted and each once, or None when it is neither form:
/// NOAA's lines `n:offset:…` (`n.k:offset:…` for the fields of a message of several, each at the
/// message's offset), or ECMWF's JSON lines with `_offset`.
fn index_offsets(b: &[u8]) -> Option<Vec<u64>> {
    let t = std::str::from_utf8(b).ok()?;
    let mut out = Vec::new();
    for line in t.lines().map(str::trim).filter(|l| !l.is_empty()) {
        let o = if line.starts_with('{') {
            serde_json::from_str::<J>(line).ok()?.get("_offset")?.as_u64()?
        } else {
            // `n`, or `n.k` for the k-th field of a message of several
            let mut f = line.split(':');
            let n = f.next()?;
            let (m, k) = n.split_once('.').unwrap_or((n, "1"));
            m.parse::<u64>().ok()?;
            k.parse::<u64>().ok()?;
            f.next()?.parse::<u64>().ok()?
        };
        out.push(o);
    }
    out.sort_unstable();
    out.dedup();
    (!out.is_empty()).then_some(out)
}

/// A message, read: its offset and length, its discipline, its sections, and the bytes of its
/// sections 1 to 5 and of section 6's first 6 octets, by section.
#[derive(Clone, Debug)]
struct Message {
    start: u64,
    length: u64,
    discipline: u8,
    head: Vec<u8>,
    sections: Vec<Section>,
    bytes: Vec<Vec<u8>>,
}

/// What reading a message from the bytes fed gives.
enum Scan {
    Need(Vec<(u64, u64)>),
    Done(Message),
    /// no `GRIB` at the offset
    NotGrib,
}

/// The bytes of a section this profile reads: sections 1 to 5 whole, section 6's first 6 octets
/// (its bitmap indicator), section 7's header.
fn wanted(s: &Section) -> u64 {
    match s.number {
        6 => s.length.min(6),
        7 => 5,
        _ => s.length,
    }
}

/// Reads the message at `start` of a file of `size` bytes from `fed`.
fn scan(rules: &Rules, fed: &Fed, start: u64, size: u64) -> Res<Scan> {
    let Some(head) = fed.get(start, INDICATOR.min(size - start)) else { return Ok(Scan::Need(vec![(start, HEAD.min(size - start))])) };
    if head.len() < 4 || &head[..4] != b"GRIB" {
        return Ok(Scan::NotGrib);
    }
    let (edition, length) = if head.len() as u64 == INDICATOR { (head[7], sections::uint(head, 8, 8)) } else { (0, 0) };
    rules.check("indicator", rules::env([("at", num(start as f64)), ("edition", num(edition)), ("whole", flag(head.len() as u64 == INDICATOR)),
                                        ("length", num(length as f64)), ("end", num(size as f64))]))?;
    let ind = sections::indicator(head)?;
    let mut need = Vec::new();
    let tail = start + length - 4;
    match fed.get(tail, 4) {
        Some(e) => rules.check("end", rules::env([("at", num(start as f64)), ("ok", flag(e == END))]))?,
        None => need.push((tail, 4)),
    }
    // the sections, as far as their headers are fed
    let mut secs = Vec::new();
    let mut pos = start + INDICATOR;
    while pos < tail {
        let Some(h) = fed.get(pos, 5) else {
            need.push((pos, HEAD.min(tail - pos)));
            break;
        };
        let (n, number) = (sections::uint(h, 0, 4), h[4]);
        rules.check("section", rules::env([("at", num(start as f64)), ("offset", num((pos - start) as f64)), ("number", num(number)),
                                          ("length", num(n as f64)), ("room", num((tail - pos) as f64))]))?;
        secs.push(Section { number, offset: pos - start, length: n });
        pos += n;
    }
    let mut bytes = Vec::new();
    for s in &secs {
        let w = wanted(s);
        if s.number != 7 && s.number != 6 {
            rules.check("section_size", rules::env([("at", num(start as f64)), ("number", num(s.number)), ("length", num(s.length as f64))]))?;
        }
        match fed.get(start + s.offset, w) {
            Some(b) => bytes.push(b.to_vec()),
            None => need.push((start + s.offset, w)),
        }
    }
    if !need.is_empty() {
        return Ok(Scan::Need(need));
    }
    if let Err(problem) = sections::fields(&secs) {
        rules.check("order", rules::env([("at", num(start as f64)), ("problem", text(&problem))]))?;
    }
    Ok(Scan::Done(Message { start, length, discipline: ind.discipline, head: head.to_vec(), sections: secs, bytes }))
}

/// A field of a message, interpreted (spec/zarrify/grib.md §2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#2-the-source>).
#[derive(Clone, Debug)]
struct Field {
    message: usize,
    discipline: u8,
    index: usize,
    ident: Ident,
    grid: Grid,
    product: Product,
    packing: meta::Packing,
    bitmap: u8,
    reason: Option<&'static str>,
    /// the field's sections 1 to 7 as a message of its own would hold them, as (offset, length)
    /// in the file: a reused bitmap's section 6 in place of its own (spec/zarrify/grib.md §8.5
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#85-chunks>)
    parts: Vec<(u64, u64)>,
    /// the message holds this field alone
    single: bool,
}

/// The fields of message `m` (its index `i`), each with the reason it is not presented, if any
/// (spec/zarrify/grib.md §2.6
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#26-what-is-not-presented>).
fn fields_of(rules: &Rules, i: usize, m: &Message) -> Res<Vec<Field>> {
    let body = |s: Option<Section>| -> &[u8] {
        let k = s.and_then(|s| m.sections.iter().position(|x| *x == s)).unwrap_or(0);
        &m.bytes[k]
    };
    let mut out = Vec::new();
    let mut had_bitmap = false;
    let mut last_bitmap: Option<Section> = None;
    let all = sections::fields(&m.sections)?;
    let single = all.len() == 1;
    for (k, f) in all.into_iter().enumerate() {
        let ident = meta::ident(body(f.s1));
        let grid = meta::grid(body(f.s3));
        let packing = meta::packing(body(f.s5));
        let bitmap = meta::bitmap(body(f.s6));
        let problem = [ident.as_ref().err(), grid.as_ref().err(), packing.as_ref().err(), bitmap.as_ref().err()].into_iter().flatten().next();
        if let Some(p) = problem {
            rules.check("order", rules::env([("at", num(m.start as f64)), ("problem", text(p))]))?;
        }
        let (ident, grid, packing, bitmap) = (ident?, grid?, packing?, bitmap?);
        let product = meta::product(body(f.s4), ident.reference);
        if let Err(p) = &product {
            rules.check("order", rules::env([("at", num(m.start as f64)), ("problem", text(p))]))?;
        }
        let product = product?;
        let reason = grid.reason.or(product.reason).or_else(|| {
            if !meta::DECODED.contains(&packing.template) {
                Some("packing")
            } else if (1..=253).contains(&bitmap) || (bitmap == 254 && !had_bitmap) {
                Some("bitmap")
            } else if bitmap == 255 && packing.values != grid.points {
                Some("values")
            } else {
                None
            }
        });
        had_bitmap |= bitmap == 0;
        if bitmap == 0 {
            last_bitmap = f.s6;
        }
        let s6 = if bitmap == 254 { last_bitmap.or(f.s6) } else { f.s6 };
        let parts = [f.s1, f.s2, f.s3, f.s4, f.s5, s6, f.s7].into_iter().flatten().map(|x| (m.start + x.offset, x.length)).collect();
        out.push(Field { message: i, discipline: m.discipline, index: k, ident, grid, product, packing, bitmap, reason, parts, single });
    }
    Ok(out)
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Phase {
    Probe,
    Head,
    Messages,
    Done,
}

pub struct Grib {
    rules: &'static Rules,
    phase: Phase,
    ir: Option<Ir>,
    keys: Vec<(String, u64)>,
    elems: Vec<u32>,
    fed: Fed,
    /// the index objects found, and their bytes once read
    indexes: Vec<(usize, Option<Vec<u8>>)>,
    /// the offsets the index lists, while the file agrees with it
    listed: Option<Vec<u64>>,
    /// the messages read, by offset, and the gaps between them
    messages: BTreeMap<u64, Message>,
    gaps: Vec<(u64, u64)>,
    /// where the next message starts: the end of the messages read in order from the start
    pos: u64,
    asked: Vec<(usize, u64, u64)>,
    batches: u64,
}

impl Grib {
    pub fn new(ir: Ir, objects: Vec<u32>, keys: &[(String, u64)]) -> Res<Grib> {
        if keys.len() != 1 || keys[0].0.contains('/') {
            return Err(format!("a GRIB run starts from the GRIB file alone, not {} objects", keys.len()));
        }
        Ok(Grib {
            rules: rules::of("grib"), phase: Phase::Probe, ir: Some(ir), keys: keys.to_vec(), elems: objects,
            fed: Fed::new(Overlap::MergeKeep), indexes: Vec::new(), listed: None, messages: BTreeMap::new(),
            gaps: Vec::new(), pos: 0, asked: Vec::new(), batches: 0,
        })
    }

    fn size(&self) -> u64 {
        self.keys[0].1
    }

    /// Advances over the messages read in order from `pos`, and the padding between them: the
    /// reads that the next ones need, or none when the file is read to its end.
    fn advance_messages(&mut self) -> Res<Vec<(u64, u64)>> {
        let size = self.size();
        loop {
            if self.pos >= size {
                return Ok(Vec::new());
            }
            if let Some(m) = self.messages.get(&self.pos) {
                self.pos += m.length;
                continue;
            }
            // the offsets still to read: those an index lists, or the next one
            let starts: Vec<u64> = match &self.listed {
                Some(l) if l.contains(&self.pos) => l.iter().copied().filter(|&s| s >= self.pos && !self.messages.contains_key(&s)).collect(),
                _ => {
                    // walking: the messages read at offsets the index listed past here are read
                    // again if the walk reaches them
                    if self.listed.take().is_some() {
                        let pos = self.pos;
                        self.messages.retain(|&s, _| s < pos);
                    }
                    vec![self.pos]
                }
            };
            let mut need = Vec::new();
            for s in starts {
                match scan(self.rules, &self.fed, s, size)? {
                    Scan::Need(r) => need.extend(r.into_iter().map(|(o, n)| {
                        // a head from 4 bytes before, for the previous message's end
                        if o == s && s >= 4 { (s - 4, n + 4) } else { (o, n) }
                    })),
                    Scan::Done(m) => {
                        self.messages.insert(s, m);
                    }
                    Scan::NotGrib if s == self.pos => {
                        // padding: bytes before the next message, or after the last
                        let limit = self.rules.limit("max_padding") as u64;
                        let window = (limit + 4).min(size - s);
                        let Some(b) = self.fed.get(s, window) else {
                            need.push((s, window));
                            continue;
                        };
                        let at = b.windows(4).position(|w| w == b"GRIB").map(|p| p as u64);
                        let gap = at.unwrap_or(size - s);
                        self.rules.check("padding", rules::env([("at", num(s as f64)), ("length", num(gap as f64)), ("found", flag(at.is_some() || window == size - s))]))?;
                        self.gaps.push((s, gap));
                        self.pos = s + gap;
                        self.listed = None;
                    }
                    // an offset the index lists that is not a message's: the index is set aside
                    Scan::NotGrib => self.listed = None,
                }
            }
            if !need.is_empty() {
                return Ok(need);
            }
        }
    }

    /// The IR (spec/zarrify/grib.md §5.3
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#53-elements>) and the facts.
    fn emit(&mut self) -> Res<J> {
        let mut ir = self.ir.take().ok_or("internal: no IR")?;
        let file = self.elems[0];
        ir.describe_in_place(file)?;
        let mut fields = Vec::new();
        let mut claimed: Vec<(u64, u64)> = self.gaps.clone();
        for (i, m) in self.messages.values().enumerate() {
            let s = ir.struct_(file, "messages/", i as u64, 0, Some((m.start, m.length)))?;
            ir.value(s, "indicator", NO_INDEX, "{magic:ascii[4],reserved:bytes[2],discipline:u1,edition:u1,length:>u8}", 0,
                     (m.start, INDICATOR), Some(&m.head))?;
            let found = fields_of(self.rules, i, m)?;
            let mut field = 0u64;
            for (j, (x, b)) in m.sections.iter().zip(&m.bytes).enumerate() {
                let at = (m.start + x.offset, x.length);
                match x.number {
                    7 => {
                        let f = &found[field as usize];
                        let form = json!({"geometry": {"points": f.grid.points, "values": f.packing.values},
                                          "codec": {"packing": f.packing.template, "field": field}, "recipe": [["src", 0, null]]});
                        ir.data(s, "sections/", j as u64, 0, at, &form.to_string())?;
                        field += 1;
                    }
                    6 if x.length > 6 => {
                        let t = ir.struct_(s, "sections/", j as u64, 0, Some(at))?;
                        ir.value(t, "header", NO_INDEX, "{length:>u4,number:u1,indicator:u1}", 0, (at.0, 6), Some(b))?;
                        let form = json!({"geometry": {"bits": (x.length - 6) * 8}, "codec": {"bitmap": "msb first"}, "recipe": [["src", 0, null]]});
                        ir.data(t, "bitmap", NO_INDEX, 0, (at.0 + 6, x.length - 6), &form.to_string())?;
                    }
                    6 => {
                        ir.value(s, "sections/", j as u64, "{length:>u4,number:u1,indicator:u1}", 0, at, Some(b))?;
                    }
                    _ if x.length == 5 => {
                        ir.value(s, "sections/", j as u64, "{length:>u4,number:u1}", 0, at, Some(b))?;
                    }
                    _ => {
                        ir.value(s, "sections/", j as u64, &format!("{{length:>u4,number:u1,body:bytes[{}]}}", x.length - 5), 0, at, Some(b))?;
                    }
                }
            }
            let end = m.start + m.length - 4;
            ir.value(s, "end", NO_INDEX, "ascii[4]", 0, (end, 4), Some(END))?;
            claimed.push((m.start, m.length));
            fields.extend(found);
        }
        // the gaps (spec/zarrify/grib.md §5.3
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#53-elements>): padding
        for &(o, n) in &self.gaps {
            ir.add(GAP, file, "gaps/", o, "", 0, Some((o, n)))?;
        }
        claimed.sort_unstable();
        // an index is held: the archive's mirror keeps its bytes
        let mut index = J::Null;
        for (k, b) in &self.indexes {
            let Some(b) = b else { continue };
            ir.value(self.elems[*k], "text", NO_INDEX, &format!("ascii[{}]", b.len()), 0, (0, b.len() as u64), Some(b))?;
            if index.is_null() && self.listed.is_some() {
                index = json!(self.keys[*k].0);
            }
        }
        let presented = fields.iter().filter(|f| f.reason.is_none()).count();
        let reasons: Vec<String> = fields.iter().filter_map(|f| f.reason).collect::<std::collections::BTreeSet<_>>().into_iter().map(str::to_string).collect();
        self.rules.check("fields", rules::env([("count", num(presented as f64)), ("fields", num(fields.len() as f64)), ("reasons", text(&reasons.join(", ")))]))?;
        ir.sort_values();
        crate::check::finish(&mut ir)?;
        self.ir = Some(ir);
        let messages: Vec<J> = self.messages.values().map(|m| json!([m.start, m.length])).collect();
        let fields: Vec<J> = fields.iter().map(field_json).collect();
        let unpresented: Vec<J> = fields.iter().filter(|f| f.get("reason").is_some())
            .map(|f| json!({"message": f["message"], "field": f["field"], "reason": f["reason"]})).collect();
        Ok(json!({
            "file_key": self.keys[0].0,
            "index_key": index,
            "own": {"messages": messages.len(), "fields": fields.len(), "index": index, "unpresented": unpresented},
            "messages": messages,
            "fields": fields,
            "counts": {"batches": self.batches},
        }))
    }
}

/// A field's facts: what the projection groups, names and places it by.
fn field_json(f: &Field) -> J {
    let discipline = f.discipline;
    let hex = |b: &[u8]| b.iter().map(|x| format!("{x:02x}")).collect::<String>();
    let mut m = Map::new();
    m.insert("message".into(), json!(f.message));
    m.insert("field".into(), json!(f.index));
    m.insert("reference".into(), json!(f.ident.reference));
    m.insert("centre".into(), json!(f.ident.centre));
    m.insert("discipline".into(), json!(discipline));
    m.insert("identity".into(), json!(format!("{discipline:02x}.{}.{}", hex(&f.ident.key), hex(&f.product.key))));
    m.insert("grid".into(), json!(hex(&f.grid.key)));
    m.insert("product".into(), json!(f.product.template));
    m.insert("category".into(), json!(f.product.category));
    m.insert("number".into(), json!(f.product.number));
    m.insert("surfaces".into(), json!([[f.product.first.kind, f.product.first.value], [f.product.second.kind, f.product.second.value]]));
    m.insert("step".into(), json!(f.product.step));
    m.insert("member".into(), json!(f.product.member));
    m.insert("statistic".into(), json!(f.product.statistic.map(|(p, l)| json!([p, l]))));
    m.insert("qualifier".into(), json!(f.product.qualifier));
    m.insert("packing".into(), json!(f.packing.template));
    m.insert("bitmap".into(), json!(f.bitmap));
    m.insert("shape".into(), json!([f.grid.ny, f.grid.nx]));
    m.insert("single".into(), json!(f.single));
    m.insert("parts".into(), json!(f.parts));
    m.insert("grid_info".into(), grid_json(&f.grid));
    if let Some(r) = f.reason {
        m.insert("reason".into(), json!(r));
    }
    J::Object(m)
}

/// A grid's facts: its template, shape, scanning mode, Earth and projection.
fn grid_json(g: &Grid) -> J {
    let earth = match g.earth {
        Some(meta::Earth::Sphere(r)) => json!({"radius": r}),
        Some(meta::Earth::Ellipsoid { a, inverse_flattening }) => json!({"a": a, "inverse_flattening": inverse_flattening}),
        None => J::Null,
    };
    let projection = match &g.projection {
        Some(meta::Projection::LatLon { lat0, lon0, dlat, dlon }) => json!({"kind": "latlon", "lat0": lat0, "lon0": lon0, "dlat": dlat, "dlon": dlon}),
        Some(meta::Projection::Lambert { lat0, lon0, lad, lov, dx, dy, latin1, latin2, south }) =>
            json!({"kind": "lambert", "lat0": lat0, "lon0": lon0, "lad": lad, "lov": lov, "dx": dx, "dy": dy, "latin1": latin1, "latin2": latin2, "south": south}),
        None => J::Null,
    };
    json!({"template": g.template, "nx": g.nx, "ny": g.ny, "scan": g.scan, "earth": earth, "projection": projection})
}

impl StoreParser for Grib {
    fn step(&mut self) -> Res<StoreStep> {
        loop {
            self.asked.clear();
            match self.phase {
                Phase::Probe => {
                    self.phase = Phase::Head;
                    self.batches += 1;
                    return Ok(StoreStep::Probe(index_keys(&self.keys[0].0)));
                }
                Phase::Head => {
                    let size = self.size();
                    self.rules.check("file", rules::env([("size", num(size as f64))]))?;
                    let max = self.rules.limit("max_index") as u64;
                    for (k, _) in &self.indexes {
                        let n = self.keys[*k].1;
                        if n > 0 && n <= max {
                            self.asked.push((*k, 0, n));
                        }
                    }
                    self.asked.push((0, 0, WALK.min(size)));
                    if size > WALK {
                        self.asked.push((0, size - 4, 4));
                    }
                    self.phase = Phase::Messages;
                }
                Phase::Messages => {
                    if self.listed.is_none() && self.messages.is_empty() && self.pos == 0 {
                        // the first index that lists offsets, read whole
                        self.listed = self.indexes.iter().filter_map(|(_, b)| b.as_deref().and_then(index_offsets)).next()
                            .filter(|l| l.first() == Some(&0) && l.iter().all(|&o| o < self.size()));
                    }
                    let need = self.advance_messages()?;
                    if need.is_empty() {
                        let n = self.messages.len();
                        self.rules.check("messages", rules::env([("count", num(n as f64))]))?;
                        self.phase = Phase::Done;
                        return Ok(StoreStep::Done);
                    }
                    let size = self.size();
                    let mut need: Vec<(u64, u64)> = need.into_iter().map(|(o, n)| (o, n.min(size - o))).collect();
                    // walking, a window rather than a head: the messages after it may be in it
                    if self.listed.is_none() {
                        need = need.into_iter().map(|(o, n)| if n >= HEAD { (o, WALK.min(size - o)) } else { (o, n) }).collect();
                    }
                    self.asked.extend(need.into_iter().map(|(o, n)| (0, o, n)));
                }
                Phase::Done => return Ok(StoreStep::Done),
            }
            if !self.asked.is_empty() {
                self.batches += 1;
                return Ok(StoreStep::Read(self.asked.clone()));
            }
        }
    }

    fn feed(&mut self, object: usize, offset: u64, data: &[u8]) {
        if object == 0 {
            self.fed.feed(offset, data.to_vec());
        } else if let Some(x) = self.indexes.iter_mut().find(|x| x.0 == object) {
            x.1 = Some(data.to_vec());
        }
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
        self.indexes.push((index, None));
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

/// A GRIB file's run: zarrified as a store run of the `grib` store format, with its index when
/// one is beside it (spec/zarrify/grib.md §8
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#8-grib-profile>), never as a
/// file run: `Format::sniff` names it so that a host sends the file to
/// [`crate::store::StoreRun`].
pub struct Profile;

impl crate::run::FileProfile for Profile {
    /// `GRIB` at byte 0 and edition 2 (`detect`).
    fn detect(&self, head: &[u8]) -> bool {
        detect(head)
    }
    fn parser(&self, _size: u64) -> crate::run::AnyParser {
        crate::run::AnyParser::refused("a GRIB file is zarrified as a store run, with its index")
    }
    fn store_run(&self) -> bool {
        true
    }
    fn project(&self, _: &Ir, _: &J, _: &str, _: &crate::project::Copied) -> Result<crate::out::Out, String> {
        Err("a GRIB file is zarrified as a store run".into())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn index_keys_and_offsets() {
        assert_eq!(index_keys("gfs.t00z.pgrb2.0p25.f006"), vec!["gfs.t00z.pgrb2.0p25.f006.idx"]);
        assert_eq!(index_keys("a/b-fc.grib2"), vec!["a/b-fc.grib2.idx", "a/b-fc.index"]);
        assert_eq!(index_offsets(b"1:0:d=2026102900:PRMSL:mean sea level:6 hour fcst:\n2:1003:d=x\n"), Some(vec![0, 1003]));
        assert_eq!(index_offsets(b"{\"_offset\": 10, \"_length\": 5}\n{\"_offset\": 0, \"_length\": 10}\n"), Some(vec![0, 10]));
        assert_eq!(index_offsets(b"1:0:d=x\n2.1:50:d=x\n2.2:50:d=x\n3:90:d=x\n"), Some(vec![0, 50, 90]));
        assert_eq!(index_offsets(b"not an index"), None);
    }
}
