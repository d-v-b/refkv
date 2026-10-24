//! A sans-IO parser of FITS files (images, multi-extension files, binary and ASCII tables,
//! tile-compressed images) into the IR (spec/virtualize/fits.md §9
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/fits.md#9-fits-profile>, and the
//! source model of §6.1
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/fits.md#61-elements>).
//!
//! Rounds: a window of up to 64 KiB at each HDU's first byte, extended by windows until the
//! header's END card is found, and for a tile-compressed image its main table (the tile
//! descriptors); the data is never read. The schema (`schema/fits.json`) holds every check, in
//! the profile's order. The IR is one struct per HDU: its header (one value of 80-byte cards),
//! its data (an image's values; a table's rows, gap and heap; a tile-compressed image's table
//! and the heap in chunks of tiles) and its padding, and a gap for any bytes after the last
//! HDU (special records).

use crate::check;
use crate::expr::V;
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
/// The most bytes of a chunk of a tile-compressed image, decoded (§3.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/fits.md#33-tile-compressed-images>).
pub const TILE_CHUNK_BYTES: u64 = 1 << 20;

type Res<T> = Result<T, String>;

/// Whether a file's first bytes are a FITS file's (spec/virtualize.md §1.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize.md#12-input>): `SIMPLE  =` and a
/// fixed-format `T` in column 30.
pub fn detect(head: &[u8]) -> bool {
    head.len() >= 30 && &head[..9] == b"SIMPLE  =" && head[29] == b'T'
}

/// A card's value (FITS Standard 4.0, section 4.2).
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
/// the string they continue (FITS Standard 4.0, section 4.2.1.2); a string's trailing spaces
/// are not significant.
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

fn str_of(c: &Card) -> String {
    match &c.value {
        Value::Str(s) => s.trim().to_string(),
        other => value_text(other),
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

// ---- the tile grid of a tile-compressed image

/// A chunk of a tile-compressed image: its coords in the chunk grid, the storage index of its
/// first tile and its tiles (consecutive in storage order).
#[derive(Clone, Debug, PartialEq)]
pub struct TileChunk {
    pub coords: Vec<u64>,
    pub first: u64,
    pub count: u64,
}

/// The chunk grid of a tile-compressed image (§3.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/fits.md#33-tile-compressed-images>):
/// for the image shape and tile shape (both in the array's order, the slowest axis first) and
/// the bytes of a decoded pixel, the chunk shape, the chunk grid's shape, and the chunks.
pub fn tile_cut(shape: &[u64], tile: &[u64], item: u64) -> (Vec<u64>, Vec<u64>, Vec<TileChunk>) {
    let n = shape.len();
    let tiles: Vec<u64> = (0..n).map(|j| shape[j].div_ceil(tile[j].max(1))).collect();
    if tiles.iter().any(|&g| g == 0) {
        return (tile.to_vec(), tiles, Vec::new());
    }
    // the bytes of one tile, and of one index along each axis of the tile grid
    let tile_bytes = tile.iter().fold(item, |a, &t| a.saturating_mul(t));
    let span = |j: usize| tiles[j + 1..].iter().fold(tile_bytes, |a, &g| a.saturating_mul(g));
    let a = (0..n).find(|&j| span(j) <= TILE_CHUNK_BYTES).unwrap_or(n - 1);
    // c tiles per chunk along a, as conventions §7
    // <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays> cuts
    // contiguous values: k chunks of
    // ceil(n / k), k = ceil(n / floor(L / s)), at least one tile
    let per = (TILE_CHUNK_BYTES / span(a).max(1)).clamp(1, tiles[a]);
    let c = tiles[a].div_ceil(tiles[a].div_ceil(per));
    let chunk_shape: Vec<u64> = (0..n).map(|j| if j < a { tile[j] } else if j == a { c * tile[a] } else { shape[j] }).collect();
    let grid: Vec<u64> = (0..n).map(|j| if j < a { tiles[j] } else if j == a { tiles[a].div_ceil(c) } else { 1 }).collect();
    let after: u64 = tiles[a + 1..].iter().product();
    let mut chunks = Vec::new();
    let mut coords = vec![0u64; n];
    loop {
        // the first tile: coords along the axes before a, q * c along a, 0 after
        let mut first = 0u64;
        for j in 0..n {
            let t = if j < a { coords[j] } else if j == a { coords[a] * c } else { 0 };
            first = first * tiles[j] + t;
        }
        let along = c.min(tiles[a] - coords[a] * c);
        chunks.push(TileChunk { coords: coords.clone(), first, count: along * after });
        // the next chunk, in C order over the grid
        let mut j = n;
        loop {
            if j == 0 {
                return (chunk_shape, grid, chunks);
            }
            j -= 1;
            coords[j] += 1;
            if coords[j] < grid[j] {
                break;
            }
            coords[j] = 0;
        }
    }
}

/// The tile's coords in the tile grid and its shape, for storage index `r` (axis 1 fastest:
/// the last of `shape` here).
pub fn tile_at(shape: &[u64], tile: &[u64], r: u64) -> (Vec<u64>, Vec<u64>) {
    let n = shape.len();
    let tiles: Vec<u64> = (0..n).map(|j| shape[j].div_ceil(tile[j].max(1))).collect();
    let mut coords = vec![0u64; n];
    let mut rest = r;
    for j in (0..n).rev() {
        coords[j] = rest % tiles[j];
        rest /= tiles[j];
    }
    let extent: Vec<u64> = (0..n).map(|j| tile[j].min(shape[j] - coords[j] * tile[j])).collect();
    (coords, extent)
}

// ---- the parser

#[derive(Clone, Copy, PartialEq, Debug)]
enum Phase {
    Start,
    Hdu,
    Done,
}

/// A tile-compressed image's columns: each present column's byte offset in a row, and
/// whether a descriptor column is `Q` (64-bit).
#[derive(Clone, Debug, Default)]
struct Columns {
    compressed: Option<(u64, bool)>,
    gzip: Option<(u64, bool)>,
    zscale: Option<u64>,
    zzero: Option<u64>,
    zblank: Option<u64>,
    row_bytes: u64,
}

/// A tile-compressed image's description, from its header, until its table is read.
#[derive(Clone, Debug)]
struct Comp {
    codec: String,
    zbitpix: i64,
    data_type: String,
    /// the image's shape and tile shape, in the array's order (slowest axis first)
    shape: Vec<u64>,
    tile: Vec<u64>,
    blocksize: i64,
    bytepix: i64,
    quantize: String,
    dither_seed: Option<i64>,
    zblank: Option<i64>,
    columns: Columns,
    rows: u64,
}

/// A tile-compressed image whose table has not been read yet.
struct Pending {
    hdu: usize,
    node: u32,
    table: u32,
    data_offset: u64,
    rows_bytes: u64,
    heap_offset: u64,
    heap: u64,
    comp: Comp,
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
    pending: Option<Pending>,
    table_buf: Option<Vec<u8>>,
}

impl Fits {
    pub fn new(size: u64) -> Self {
        Fits {
            size,
            phase: Phase::Start,
            buf: Vec::new(),
            buf_start: 0,
            offset: 0,
            index: 0,
            ir: Ir::new(size),
            rules: rules::of("fits"),
            hdus: Vec::new(),
            pending: None,
            table_buf: None,
        }
    }

    pub fn feed(&mut self, offset: u64, data: Vec<u8>) {
        if let Some(p) = &self.pending {
            if offset == p.data_offset && data.len() as u64 >= p.rows_bytes {
                self.table_buf = Some(data);
                return;
            }
        }
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
                    if let Some(p) = &self.pending {
                        if self.table_buf.is_none() {
                            // the table, with the next HDU's first window when the current window does not reach it
                            let mut reads = vec![(p.data_offset, p.rows_bytes)];
                            let end = self.buf_start + self.buf.len() as u64;
                            if self.offset < self.size && self.offset >= end {
                                reads.push((self.offset, (self.size - self.offset).min(WINDOW)));
                            }
                            return Ok(Step::Read(reads));
                        }
                        self.table()?;
                        if self.offset >= self.size {
                            self.phase = Phase::Done;
                        }
                        continue;
                    }
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
    /// scanned record is checked to be printable ASCII (§9.3
    /// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/fits.md#93-checks>).
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

    /// The integer value of the card of `keyword`, when there is one (and it is an integer).
    fn opt_int(&self, cards: &[Card], keyword: &str) -> Res<Option<i128>> {
        match card(cards, keyword) {
            Some(c) => self.int_of(c).map(Some),
            None => Ok(None),
        }
    }

    fn is_true(cards: &[Card], keyword: &str) -> bool {
        matches!(card(cards, keyword).map(|c| &c.value), Some(Value::Logical(true)))
    }

    /// The HDU whose header is the first `records` records of `buf` (END included), with its
    /// checks (§9.3 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/fits.md#93-checks>),
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
            let name = str_of(&cards[0]);
            let mut env = rules::env([("hdu", num(hdu)), ("name", text(&name))]);
            self.rules.check("xtension", &mut env)?;
            let kind = match env.0.iter().find(|(k, _)| *k == "kind").map(|(_, v)| v.clone()) {
                Some(V::Str(s)) if s == "image" => "image",
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
        // the data's size, exactly (FITS Standard 4.0, sections 4.4.1.1 and 7.1.1)
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
        // tables (§9.3 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/fits.md#93-checks>):
        // BITPIX 8, NAXIS 2, TFIELDS and a TFORMn per column, the heap's place
        let mut table = J::Null;
        let mut comp: Option<Comp> = None;
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
                forms.push(str_of(c.unwrap()));
            }
            let rows = axes[0] * axes[1];
            let theap = match card(&cards, "THEAP") {
                Some(c) => self.int_of(c)?,
                None => rows as i128,
            };
            self.check("heap", rules::env([("hdu", num(hdu)), ("theap", num(theap as f64)), ("rows", num(rows as f64)), ("pcount", num(pcount as f64))]))?;
            self.check("ztable", rules::env([("hdu", num(hdu)), ("ztable", flag(Self::is_true(&cards, "ZTABLE")))]))?;
            let gap = theap as u64 - rows;
            table = json!({"tfields": tfields, "tform": forms, "row_bytes": axes[0], "rows": axes[1], "theap": theap, "gap": gap, "heap": total - rows - gap});
            if xtension == "BINTABLE" && Self::is_true(&cards, "ZIMAGE") {
                self.check("table_size", rules::env([("hdu", num(hdu)), ("bytes", num(rows as f64))]))?;
                comp = Some(self.compressed(&cards, &forms, axes[1])?);
            }
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
        let (rows, gap, heap) = match table.as_object() {
            Some(t) => (t["row_bytes"].as_u64().unwrap() * t["rows"].as_u64().unwrap(), t["gap"].as_u64().unwrap(), t["heap"].as_u64().unwrap()),
            None => (0, 0, 0),
        };
        if kind == "image" && total > 0 {
            let shape: Vec<u64> = axes.iter().rev().copied().collect();
            let form = json!({"geometry": {"data_type": spec["dtype"], "shape": shape}, "codec": {"endian": "big"}, "recipe": [["src", 0, null]]});
            elements["data"] = json!(self.ir.data(s, "data", NO_INDEX, 0, (data_offset, total), &form.to_string())?);
        } else if let Some(c) = &comp {
            // the main table (held once read), the gap; the heap's chunks follow the table
            let ty = Self::table_type(&c.columns, c.rows);
            if rows > 0 {
                elements["table"] = json!(self.ir.value(s, "table", NO_INDEX, &ty, 0, (data_offset, rows), None)?);
            }
            if gap > 0 {
                elements["gap"] = json!(self.ir.value(s, "gap", NO_INDEX, &format!("bytes[{gap}]"), 0, (data_offset + rows, gap), None)?);
            }
        } else if kind == "table" {
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
        let kind_out = if empty { "empty" } else if comp.is_some() { "image" } else { kind };
        let data_type = match &comp {
            Some(c) => json!(c.data_type),
            None => spec["dtype"].clone(),
        };
        self.hdus.push(json!({
            "index": self.index, "type": if primary { "primary".to_string() } else { xtension.to_lowercase() },
            "kind": kind_out, "offset": self.offset, "header_bytes": header_bytes,
            "cards": records - 1, "data_offset": data_offset, "data_bytes": total, "pad": pad,
            "bitpix": bitpix as i64, "data_type": data_type, "naxis": axes, "pcount": pcount as i64, "gcount": gcount as i64,
            "name": name, "version": version, "table": table, "compression": J::Null, "elements": elements,
        }));
        if let Some(c) = comp {
            self.pending = Some(Pending {
                hdu: self.hdus.len() - 1,
                node: s,
                table: elements["table"].as_u64().map_or(u32::MAX, |v| v as u32),
                data_offset,
                rows_bytes: rows,
                heap_offset: data_offset + rows + gap,
                heap,
                comp: c,
            });
        }
        // the next HDU (the window's remaining bytes are its first bytes, often all of it)
        self.index += 1;
        self.offset = padded_end;
        if padded_end >= self.size && self.pending.is_none() {
            self.phase = Phase::Done;
        } else if padded_end < self.size {
            self.check("hdu_count", rules::env([("count", num(self.index as f64 + 1.0))]))?;
        }
        Ok(())
    }

    /// A tile-compressed image's cards and columns, checked (§9.3
    /// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/fits.md#93-checks>).
    fn compressed(&self, cards: &[Card], forms: &[String], rows: u64) -> Res<Comp> {
        let hdu = self.index as f64;
        let name = card(cards, "ZCMPTYPE").map(str_of).unwrap_or_default();
        let mut env = rules::env([("hdu", num(hdu)), ("name", text(&name))]);
        self.rules.check("zcmptype", &mut env)?;
        let codec = match env.0.iter().find(|(k, _)| *k == "codec").map(|(_, v)| v.clone()) {
            Some(V::Str(s)) => s,
            _ => return Err("internal: no codec".into()),
        };
        let zbitpix = self.opt_int(cards, "ZBITPIX")?.unwrap_or(0);
        self.check("zbitpix", rules::env([("hdu", num(hdu)), ("value", num(zbitpix as f64))]))?;
        let spec = &self.rules.table("data_types")[zbitpix.to_string()];
        let znaxis = self.opt_int(cards, "ZNAXIS")?.unwrap_or(0);
        self.check("znaxis", rules::env([("hdu", num(hdu)), ("value", num(znaxis as f64))]))?;
        let (mut shape_fits, mut tile_fits) = (Vec::new(), Vec::new());
        for n in 1..=znaxis as usize {
            let size = self.opt_int(cards, &format!("ZNAXIS{n}"))?.unwrap_or(-1);
            self.check("znaxisn", rules::env([("hdu", num(hdu)), ("n", num(n as f64)), ("value", num(size as f64))]))?;
            let tile = self.opt_int(cards, &format!("ZTILE{n}"))?.unwrap_or(if n == 1 { size } else { 1 });
            self.check("ztile", rules::env([("hdu", num(hdu)), ("n", num(n as f64)), ("value", num(tile as f64)), ("size", num(size as f64))]))?;
            shape_fits.push(size as u64);
            tile_fits.push(tile as u64);
        }
        // the algorithm's parameters
        let (mut blocksize, mut bytepix) = (32i128, 4i128);
        for k in 1..=9 {
            match card(cards, &format!("ZNAME{k}")).map(str_of).as_deref() {
                Some("BLOCKSIZE") => blocksize = self.opt_int(cards, &format!("ZVAL{k}"))?.unwrap_or(32),
                Some("BYTEPIX") => bytepix = self.opt_int(cards, &format!("ZVAL{k}"))?.unwrap_or(4),
                _ => {}
            }
        }
        if codec == "RICE_1" {
            self.check("rice", rules::env([("hdu", num(hdu)), ("blocksize", num(blocksize as f64)), ("bytepix", num(bytepix as f64))]))?;
        }
        let quantize = card(cards, "ZQUANTIZ").map(str_of).unwrap_or_else(|| "NO_DITHER".into());
        let mut env = rules::env([("hdu", num(hdu)), ("name", text(&quantize))]);
        self.rules.check("zquantiz", &mut env)?;
        let dithered = matches!(env.0.iter().find(|(k, _)| *k == "method").map(|(_, v)| v.clone()), Some(V::Num(m)) if m > 0.0);
        let dither_seed = if dithered {
            let v = self.opt_int(cards, "ZDITHER0")?;
            self.check("zdither0", rules::env([("hdu", num(hdu)), ("name", text(&quantize)), ("found", flag(v.is_some())), ("value", num(v.unwrap_or(0) as f64))]))?;
            v.map(|v| v as i64)
        } else {
            None
        };
        let zblank = self.opt_int(cards, "ZBLANK")?.map(|v| v as i64);
        // the columns
        let mut columns = Columns::default();
        let mut at = 0u64;
        for (k, form) in forms.iter().enumerate() {
            let name = card(cards, &format!("TTYPE{}", k + 1)).map(str_of).unwrap_or_default().to_uppercase();
            let expected = self.rules.table("tile_columns")[&name].as_str().unwrap_or("").to_string();
            let unsupported = self.rules.table("unsupported_columns")[&name].is_number();
            let known = !expected.is_empty();
            let descriptor = |f: &str| -> Option<bool> {
                let f = f.trim_start_matches('1');
                let f = f.split('(').next().unwrap_or(f);
                match f {
                    "PB" => Some(false),
                    "QB" => Some(true),
                    _ => None,
                }
            };
            let (form_ok, width) = match name.as_str() {
                "COMPRESSED_DATA" | "GZIP_COMPRESSED_DATA" => match descriptor(form) {
                    Some(q) => (true, if q { 16 } else { 8 }),
                    None => (false, 0),
                },
                "ZSCALE" | "ZZERO" => (form.trim_start_matches('1') == "D", 8),
                "ZBLANK" => (form.trim_start_matches('1') == "J", 4),
                _ => (true, 0),
            };
            self.check(
                "tile_column",
                rules::env([
                    ("hdu", num(hdu)),
                    ("name", text(&name)),
                    ("form", text(form)),
                    ("unsupported", flag(unsupported)),
                    ("known", flag(known)),
                    ("form_ok", flag(form_ok)),
                    ("expected", text(&expected)),
                ]),
            )?;
            match name.as_str() {
                "COMPRESSED_DATA" => columns.compressed = Some((at, width == 16)),
                "GZIP_COMPRESSED_DATA" => columns.gzip = Some((at, width == 16)),
                "ZSCALE" => columns.zscale = Some(at),
                "ZZERO" => columns.zzero = Some(at),
                "ZBLANK" => columns.zblank = Some(at),
                _ => {}
            }
            at += width;
        }
        columns.row_bytes = at;
        self.check(
            "tile_columns",
            rules::env([
                ("hdu", num(hdu)),
                ("has_compressed", flag(columns.compressed.is_some())),
                ("has_scale", flag(columns.zscale.is_some())),
                ("has_zero", flag(columns.zzero.is_some())),
            ]),
        )?;
        let shape: Vec<u64> = shape_fits.iter().rev().copied().collect();
        let tile: Vec<u64> = tile_fits.iter().rev().copied().collect();
        let tiles: u64 = (0..shape.len()).map(|j| shape[j].div_ceil(tile[j].max(1))).product();
        self.check("tile_count", rules::env([("hdu", num(hdu)), ("rows", num(rows as f64)), ("tiles", num(tiles as f64))]))?;
        Ok(Comp {
            codec,
            zbitpix: zbitpix as i64,
            data_type: spec["dtype"].as_str().unwrap_or("uint8").to_string(),
            shape,
            tile,
            blocksize: blocksize as i64,
            bytepix: bytepix as i64,
            quantize,
            dither_seed,
            zblank,
            columns,
            rows,
        })
    }

    /// The main table's record type: one record per row, a field per column in row order.
    fn table_type(c: &Columns, rows: u64) -> String {
        let mut fields: Vec<(u64, String)> = Vec::new();
        if let Some((at, q)) = c.compressed {
            fields.push((at, format!("COMPRESSED_DATA:{{n:>i{0},o:>i{0}}}", if q { 8 } else { 4 })));
        }
        if let Some((at, q)) = c.gzip {
            fields.push((at, format!("GZIP_COMPRESSED_DATA:{{n:>i{0},o:>i{0}}}", if q { 8 } else { 4 })));
        }
        if let Some(at) = c.zscale {
            fields.push((at, "ZSCALE:>f8".into()));
        }
        if let Some(at) = c.zzero {
            fields.push((at, "ZZERO:>f8".into()));
        }
        if let Some(at) = c.zblank {
            fields.push((at, "ZBLANK:>i4".into()));
        }
        fields.sort();
        format!("{{{}}}[{rows}]", fields.into_iter().map(|(_, f)| f).collect::<Vec<_>>().join(","))
    }

    /// The tile-compressed image whose table was just read: the descriptors checked, the
    /// heap's chunks as elements, the facts.
    fn table(&mut self) -> Res<()> {
        let p = self.pending.take().ok_or("internal: no pending table")?;
        let raw = self.table_buf.take().ok_or("internal: no table bytes")?;
        let hdu = p.hdu as f64;
        let c = &p.comp;
        if p.table != u32::MAX {
            self.ir.put_value(p.table, &raw[..p.rows_bytes as usize]);
        }
        let read = |at: usize, q: bool, row: u64| -> (i64, i64) {
            let o = (row * c.columns.row_bytes) as usize + at;
            if q {
                (i64::from_be_bytes(raw[o..o + 8].try_into().unwrap()), i64::from_be_bytes(raw[o + 8..o + 16].try_into().unwrap()))
            } else {
                (i32::from_be_bytes(raw[o..o + 4].try_into().unwrap()) as i64, i32::from_be_bytes(raw[o + 4..o + 8].try_into().unwrap()) as i64)
            }
        };
        // each tile's bytes in the heap: (start, length) from the heap's first byte, or None
        let mut tiles: Vec<Option<(u64, u64)>> = Vec::with_capacity(c.rows as usize);
        let (mut previous, mut end) = (0u64, 0u64);
        for r in 0..c.rows {
            let (at, q) = c.columns.compressed.unwrap();
            let (n, o) = read(at as usize, q, r);
            let gz = c.columns.gzip.map(|(at, q)| read(at as usize, q, r));
            let both = n != 0 && gz.is_some_and(|(gn, _)| gn != 0);
            self.check("tile_one", rules::env([("hdu", num(hdu)), ("tile", num(r as f64)), ("both", flag(both))]))?;
            let (n, o) = if n != 0 { (n, o) } else { gz.filter(|(gn, _)| *gn != 0).unwrap_or((0, 0)) };
            if n == 0 {
                tiles.push(None);
                continue;
            }
            self.check(
                "tile_descriptor",
                rules::env([("hdu", num(hdu)), ("tile", num(r as f64)), ("offset", num(o as f64)), ("length", num(n as f64)), ("heap", num(p.heap as f64))]),
            )?;
            if tiles.iter().any(|t| t.is_some()) {
                self.check(
                    "tile_order",
                    rules::env([("hdu", num(hdu)), ("tile", num(r as f64)), ("offset", num(o as f64)), ("previous", num(previous as f64)), ("end", num(end as f64))]),
                )?;
            }
            previous = r;
            end = (o + n) as u64;
            tiles.push(Some((o as u64, n as u64)));
        }
        // the chunks: an element per chunk with a present tile
        let item = c.zbitpix.unsigned_abs() / 8;
        let (chunk_shape, grid, chunks) = tile_cut(&c.shape, &c.tile, item);
        let mut chunk_facts = Vec::with_capacity(chunks.len());
        for (j, ch) in chunks.iter().enumerate() {
            let present: Vec<(u64, u64)> = (ch.first..ch.first + ch.count).filter_map(|r| tiles[r as usize]).collect();
            let element = if let (Some(&(s0, _)), Some(&(s1, n1))) = (present.first(), present.last()) {
                let start = p.heap_offset + s0;
                let length = s1 + n1 - s0;
                let adjacent = present.windows(2).all(|w| w[0].0 + w[0].1 == w[1].0);
                let recipe: Vec<J> = if adjacent {
                    vec![json!(["src", 0, null])]
                } else {
                    present.iter().map(|&(s, n)| json!(["src", s - s0, n])).collect()
                };
                let form = json!({"geometry": {"tiles": [ch.first, ch.count]}, "recipe": recipe});
                Some(self.ir.data(p.node, "heap/", j as u64, 0, (start, length), &form.to_string())?)
            } else {
                None
            };
            chunk_facts.push(json!({"coords": ch.coords, "first": ch.first, "count": ch.count, "element": element}));
        }
        let h = &mut self.hdus[p.hdu];
        h["compression"] = json!({
            "type": c.codec, "parameters": {"BLOCKSIZE": c.blocksize, "BYTEPIX": c.bytepix}, "zbitpix": c.zbitpix,
            "data_type": c.data_type, "shape": c.shape, "tile": c.tile, "quantize": c.quantize, "dither_seed": c.dither_seed,
            "zblank": c.zblank, "columns": {
                "COMPRESSED_DATA": c.columns.compressed.map(|(at, q)| json!({"offset": at, "q": q})),
                "GZIP_COMPRESSED_DATA": c.columns.gzip.map(|(at, q)| json!({"offset": at, "q": q})),
                "ZSCALE": c.columns.zscale, "ZZERO": c.columns.zzero, "ZBLANK": c.columns.zblank,
            },
            "row_bytes": c.columns.row_bytes, "rows": c.rows, "tiles": tiles.len(),
            "present": tiles.iter().filter(|t| t.is_some()).count(),
            "chunk_shape": chunk_shape, "grid": grid, "chunks": chunk_facts,
        });
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

crate::parser!(Fits);

/// The FITS profile's file run (spec/virtualize/fits.md).
pub struct Profile;

impl crate::run::FileProfile for Profile {
    /// `SIMPLE  =` and a fixed-format `T` in column 30 (`detect`).
    fn detect(&self, head: &[u8]) -> bool {
        detect(head)
    }
    fn parser(&self, size: u64) -> crate::run::AnyParser {
        crate::run::AnyParser::of::<Fits>(size)
    }
    fn project(&self, ir: &crate::ir::Ir, facts: &serde_json::Value, url: &str, copied: &crate::project::Copied) -> Result<crate::out::Out, String> {
        crate::project::fits::project(ir, facts, url, copied)
    }
    fn reads(&self, ir: &crate::ir::Ir, facts: &serde_json::Value) -> Result<Vec<(u64, u64)>, String> {
        crate::project::fits::reads(ir, facts)
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

    /// The tile grid cut (§3.3 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/fits.md#33-tile-compressed-images>).
    #[test]
    fn tile_cut_rows_squares_and_planes() {
        // DECam: 4094 row tiles of 2046 float32 (8184 bytes): 128 rows per chunk, 32 chunks
        let (shape, grid, chunks) = tile_cut(&[4094, 2046], &[1, 2046], 4);
        assert_eq!((shape, grid, chunks.len()), (vec![128, 2046], vec![32, 1], 32));
        assert_eq!(chunks[0], TileChunk { coords: vec![0, 0], first: 0, count: 128 });
        assert_eq!(chunks[31], TileChunk { coords: vec![31, 0], first: 3968, count: 126 });
        // Legacy Surveys: 100 x 100 float32 tiles on 3600 x 3600: a tile row is 1.44 MB, over the
        // limit, so the cut is along axis 1: 18 tiles per chunk, 2 chunks per tile row
        let (shape, grid, chunks) = tile_cut(&[3600, 3600], &[100, 100], 4);
        assert_eq!((shape, grid, chunks.len()), (vec![100, 1800], vec![36, 2], 72));
        assert_eq!(chunks[3], TileChunk { coords: vec![1, 1], first: 54, count: 18 });
        // a cube tiled by plane, 6 planes of 20 x 30 int16 (1200 bytes): all 6 in one chunk
        let (shape, grid, chunks) = tile_cut(&[6, 20, 30], &[1, 20, 30], 2);
        assert_eq!((shape, grid, chunks.len()), (vec![6, 20, 30], vec![1, 1, 1], 1));
        assert_eq!(chunks[0], TileChunk { coords: vec![0, 0, 0], first: 0, count: 6 });
        // an axis of 0: no chunks
        assert_eq!(tile_cut(&[0, 30], &[1, 30], 2).2.len(), 0);
        // the tile at a storage index: coords and its (edge) extent
        assert_eq!(tile_at(&[230, 350], &[100, 100], 7), (vec![1, 3], vec![100, 50]));
        assert_eq!(tile_at(&[230, 350], &[100, 100], 11), (vec![2, 3], vec![30, 50]));
    }
}
