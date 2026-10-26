//! A sans-IO parser of TIFF, BigTIFF and OME-TIFF files into the IR
//! (spec/zarrify/tiff.md §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#7-tiff-profile>,
//! spec/zarrify/tiff.md): it never reads; it asks for batches
//! of ranges (`step`), is given their bytes (`feed`), and holds them only until
//! the step that uses them.
//!
//! Rounds: (1) the header; (2) the main chain, one IFD a round (each IFD names
//! the next); (3) the out-of-line values of the chain's IFDs that the profile
//! reads, or that are small; (4) the SubIFDs, a depth a round, with their values;
//! (5) the values the layout needs that were not read yet (each level's tile
//! tables), as the layout asks for them; (6) the IFDs other pointer tags lead to,
//! for the IR only. The schema (`schema/tiff.json`) is checked as the profile
//! meets each role: a file that fails is rejected there, and nowhere else.
//!
//! **The QPTIFF variant** (spec/zarrify/qptiff.md
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md>). After the header (and
//! the NDPI test), the first IFD's Software tag decides whether the file is an Akoya QPTIFF
//! (`detect_qptiff`, spec/zarrify/qptiff.md §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#7-qptiff-profile>, which
//! never rejects). A QPTIFF is read as a TIFF, with the table of `QPTIFF_TABLE` (the strip
//! tables, RowsPerStrip and the position tags added), the schema `schema/qptiff.json`, and its
//! own layout (`derive_qptiff`: the channels are the FullResolution images, the levels the runs
//! of ReducedResolution images, tiled or in strips), whose facts the QPTIFF projection
//! (`project/qptiff.rs`) reads; `self.qptiff` marks it.
//!
//! **The NDPI variant** (spec/zarrify/ndpi.md
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md>). After the header, the
//! parser tests the file for NDPI (§7
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#7-ndpi-profile>, which never
//! rejects): a little-endian magic-42 TIFF whose IFD at the u64 at bytes 4–11 has tag 65420. An
//! NDPI is read by the same rounds with these extension points, each a method that asks
//! `self.ndpi`:
//! - the IFD layout (`ifd_len`, `next_of`, `ifd`): a u16 entry count, 12-byte entries, a u64
//!   next-IFD offset, then a u32 high word per entry, which extends the entry's value field
//!   (`Ent::high`: an out-of-line value is at `low + high × 2^32`, and `ints` reads an in-line
//!   LONG or IFD of count 1 as `low + high × 2^32`); the high words are `tags/<tag>/high`;
//! - the tags (`table`, `layout_tag`, `is_pointer`): NDPI's table and layout tags, with
//!   SubIFDs followed as a pointer tag (an NDPI's tree is its main chain only), against the
//!   schema `schema/ndpi.json`;
//! - the checks of the chain (`check_offset`) and the layout (`derive_ndpi`, with the strips'
//!   JPEG headers and restart intervals read by `ndpi.rs`), whose facts the NDPI projection
//!   (`project/ndpi.rs`) reads.

use crate::expr::V;
use crate::fed::{Fed, Word};
use crate::ir::*;
use crate::nd2::Step;
use crate::lzw;
use crate::ndpi;
use crate::out::Part;
use crate::rules::{self, Rules, flag, int, num, text};
use crate::sans;
use crate::xml::{self, Ev};
use serde_json::{Value as J, json};
use std::collections::{BTreeMap, HashMap, HashSet};

const TABLE: [u16; 21] = [
    256, 257, 258, 259, 262, 266, 270, 277, 282, 283, 284, 296, 317, 322, 323, 324, 325, 330, 339, 347, 530,
];
const SCALARS: [u16; 13] = [256, 257, 259, 262, 266, 277, 282, 283, 284, 296, 317, 322, 323];
const LAYOUT: [u16; 11] = [273, 279, 288, 289, 324, 325, 513, 514, 519, 520, 521];
/// The Adobe APP14 marker that tells a decoder a 3-component stream is RGB (transform 0),
/// written before a JPEG tile's tables.
pub const ADOBE: [u8; 15] = [
    0xFF, 0xEE, 0x00, 0x0E, 0x41, 0x64, 0x6F, 0x62, 0x65, 0x00, 0x64, 0x00, 0x00, 0x00, 0x00,
];
const WINDOW: u64 = 4096; // the bytes asked at an IFD, before its entry count is known

fn is_table(tag: u16) -> bool {
    TABLE.contains(&tag)
}

/// The tags of NDPI's table (spec/zarrify/ndpi.md §2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#2-the-source>), and the tags
/// NDPI adds to TIFF's layout tags (the tile tables' high bytes, McuStarts and
/// McuStartsHighBytes).
const NDPI_TABLE: [u16; 23] = [
    256, 257, 258, 259, 262, 273, 277, 279, 282, 283, 296, 322, 323, 324, 325, 65324, 65325, 65420, 65421, 65422, 65423,
    65426, 65432,
];
const NDPI_SCALARS: [u16; 16] = [256, 257, 259, 262, 273, 277, 279, 282, 283, 296, 322, 323, 65420, 65421, 65422, 65423];
const NDPI_LAYOUT: [u16; 4] = [65324, 65325, 65426, 65432];
/// NDPI's acquisition tags (spec/zarrify/ndpi.md §2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#2-the-source>): Fluorescence,
/// Wavelength and ExposureTime, each read when of its field types.
const NDPI_ACQUISITION: [(u16, &str, &[u16]); 3] =
    [(65434, "Fluorescence", &[2]), (65451, "Wavelength", &[11, 12]), (65454, "ExposureTime", &[3, 4])];
/// The tags of QPTIFF's table (spec/zarrify/qptiff.md §2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#2-the-source>): TIFF's, with
/// the strip tables, RowsPerStrip and the position tags.
const QPTIFF_TABLE: [u16; 26] = [
    256, 257, 258, 259, 262, 266, 270, 273, 277, 278, 279, 282, 283, 284, 286, 287, 296, 317, 322, 323, 324, 325, 330, 339, 347,
    530,
];
const QPTIFF_SCALARS: [u16; 16] = [256, 257, 259, 262, 266, 277, 278, 282, 283, 284, 286, 287, 296, 317, 322, 323];
/// What a QPTIFF's Software tag starts with (spec/zarrify/qptiff.md §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#7-qptiff-profile>).
const QPI_SOFTWARE: &[u8] = b"PerkinElmer-QPI";
const MAX_SAFE: u64 = (1 << 53) - 1;

fn is_used(tag: u16) -> bool {
    is_table(tag) && tag != 270 && tag != 330
}

fn is_scalar(tag: u16) -> bool {
    SCALARS.contains(&tag)
}

pub fn type_size(t: u16) -> Option<u64> {
    Some(match t {
        1 | 2 | 6 | 7 => 1,
        3 | 8 => 2,
        4 | 9 | 11 | 13 => 4,
        5 | 10 | 12 | 16 | 17 | 18 => 8,
        _ => return None,
    })
}

fn unsigned(t: u16) -> bool {
    matches!(t, 1 | 3 | 4 | 13 | 16 | 18)
}

/// An NDPI entry whose value is its 4 bytes plus its high word: an in-line LONG or IFD of
/// count 1, but for the high tables (OffsetHighBytes, ByteCountHighBytes and
/// McuStartsHighBytes), whose values are the 32-bit values they store (spec/zarrify/ndpi.md §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#7-ndpi-profile>).
fn ndpi_high_word(e: &Ent) -> bool {
    e.inline && e.count == 1 && matches!(e.typ, 4 | 13) && !matches!(e.tag, 65324 | 65325 | 65432)
}


fn label(tag: u16) -> String {
    match tag {
        330 => "subifds".into(),
        34665 => "exif".into(),
        34853 => "gps".into(),
        40965 => "interoperability".into(),
        _ => format!("ifd_{tag}"),
    }
}

pub fn value_type(le: bool, typ: u16, count: u64) -> String {
    let e = if le { "<" } else { ">" };
    match typ {
        1 => format!("u1[{count}]"),
        6 => format!("i1[{count}]"),
        2 => format!("ascii[{count}]"),
        7 => format!("bytes[{count}]"),
        5 => format!("{e}u4[{count},2]"),
        10 => format!("{e}i4[{count},2]"),
        _ => {
            let t = match typ {
                3 => "u2",
                4 | 13 => "u4",
                8 => "i2",
                9 => "i4",
                11 => "f4",
                12 => "f8",
                16 | 18 => "u8",
                _ => "i8",
            };
            format!("{e}{t}[{count}]")
        }
    }
}

#[derive(Clone)]
struct Ent {
    tag: u16,
    typ: u16,
    count: u64,
    field: Vec<u8>,
    /// the value's bytes (count × type size), when the type is known
    nbytes: Option<u64>,
    inline: bool,
    /// the value's offset, when out of line
    off: u64,
    /// its value element (in line, or out of line within the file)
    val: Option<u32>,
    dup: bool,
    /// NDPI: the entry's high word, and its element
    high: u64,
    hi: Option<u32>,
}

#[derive(Clone)]
struct Ifd {
    offset: u64,
    el: u32,
    depth: u32,
    tree: bool,
    entries: Vec<Ent>,
    /// the first entry of each tag
    first: HashMap<u16, usize>,
    /// SubIFDs read, in order
    subs: Vec<usize>,
}

impl Ifd {
    fn field(&self, tag: u16) -> Option<&Ent> {
        self.first.get(&tag).map(|&k| &self.entries[k])
    }
    /// A tag of the table that passed the field checks (the profile's `fields`).
    fn has(&self, tag: u16) -> bool {
        self.field(tag).is_some()
    }
}

#[derive(PartialEq, Clone, Copy, Debug)]
enum Phase {
    Start,
    Header,
    Detect,
    DetectEntries,
    DetectQpi,
    Chain,
    ChainValues,
    SubPlan,
    SubRead,
    SubValues,
    Layout,
    Pointers,
    Done,
}

/// A loaded value, as the profile reads it.
#[derive(Clone, Debug)]
enum Loaded {
    Ints(Vec<u64>),
    Pairs(Vec<(u64, u64)>),
    Bytes(Vec<u8>),
}

enum Stop {
    Need(Vec<(u64, u64)>),
    Reject(String),
}

impl From<String> for Stop {
    fn from(s: String) -> Self {
        Stop::Reject(s)
    }
}

type Got<T> = Result<T, Stop>;

/// An IFD to read: (parent element, name, name index, offset, depth, in the tree, parent IFD)
#[derive(Clone)]
struct Pending {
    parent: u32,
    name: String,
    nidx: u64,
    offset: u64,
    depth: u32,
    tree: bool,
    from: Option<usize>,
}

pub struct Tiff {
    size: u64,
    phase: Phase,
    fed: Fed,
    pub ir: Ir,
    rules: &'static Rules,
    le: bool,
    big: bool,
    hs: u64,
    ifds: Vec<Ifd>,
    by_offset: HashMap<u64, usize>,
    chain: Vec<usize>,
    next: u64,
    pending: Vec<Pending>,
    bodies: bool,
    depth: u32,
    /// value extents read: (offset, length) -> the first element holding them
    values: HashMap<(u64, u64), u32>,
    /// value prefixes the layout read: (offset, length) -> bytes
    heads: HashMap<(u64, u64), Vec<u8>>,
    asked_values: Vec<(u64, u64, Vec<u32>)>,
    layout_asked: Vec<(u64, u64)>,
    chain_window: u64,
    last_window: (u64, u64),
    eager: u64,
    layout: Option<Layout>,
    /// IFD extents, for pointer targets: start -> end
    extents: BTreeMap<u64, u64>,
    tries: u64,
    todo: Vec<(usize, u16, String, u64)>,
    ptr_state: u8,
    made: Vec<usize>,
    data_keys: HashMap<String, u32>,
    /// what the parse asked for
    pub asked: sans::Asked,
    /// the file is an NDPI (the variant of the module's documentation)
    pub ndpi: bool,
    /// the file is a QPTIFF (the variant of the module's documentation)
    pub qptiff: bool,
    /// the header's bytes, kept until the NDPI test decides how to read it
    head: Vec<u8>,
}

struct Level {
    w: u64,
    h: u64,
    /// the chunk's width and height: the tile's, or, for a level in strips (QPTIFF), the
    /// image's width and the rows per strip
    tw: u64,
    th: u64,
    ifds: Vec<usize>,
    /// the level is in strips (QPTIFF)
    strips: bool,
    /// how a short last strip is padded to the chunk (QPTIFF, spec/zarrify/qptiff.md §4.2
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#42-tiles-and-strips>):
    /// `{"zeros": n}` (uncompressed) or `{"lzw": [{c, k, prefix, tail}, ...]}`; null when none
    pad: J,
}

struct Layout {
    facts: J,
    /// (IFD, JPEG prefix) of every IFD a level uses
    prefixes: HashMap<usize, Vec<u8>>,
    levels: Vec<Level>,
    planes: Option<Vec<usize>>,
}

type Fmt = (u64, u64, u64, u64, u64, u64, Option<u64>);

impl Tiff {
    pub fn new(size: u64) -> Self {
        Tiff {
            size,
            phase: Phase::Start,
            fed: Fed::default(),
            ir: Ir::new(size),
            rules: rules::of("tiff"),
            le: true,
            big: false,
            hs: 8,
            ifds: Vec::new(),
            by_offset: HashMap::new(),
            chain: Vec::new(),
            next: 0,
            pending: Vec::new(),
            bodies: false,
            depth: 0,
            values: HashMap::new(),
            heads: HashMap::new(),
            asked_values: Vec::new(),
            layout_asked: Vec::new(),
            chain_window: WINDOW,
            last_window: (0, 0),
            eager: 0,
            layout: None,
            extents: BTreeMap::new(),
            tries: 0,
            todo: Vec::new(),
            ptr_state: 0,
            made: Vec::new(),
            data_keys: HashMap::new(),
            asked: sans::Asked::default(),
            ndpi: false,
            qptiff: false,
            head: Vec::new(),
        }
    }

    // ---- the extension points of the NDPI variant

    /// A tag of the table the variant checks.
    fn table(&self, tag: u16) -> bool {
        if self.ndpi {
            NDPI_TABLE.contains(&tag)
        } else if self.qptiff {
            QPTIFF_TABLE.contains(&tag)
        } else {
            is_table(tag)
        }
    }

    /// A tag of the table whose values the layout reads for every IFD it uses.
    fn used(&self, tag: u16) -> bool {
        if self.qptiff { QPTIFF_TABLE.contains(&tag) && tag != 270 && tag != 330 } else { is_used(tag) }
    }

    /// A scalar tag of the table: its first value is read.
    fn scalar(&self, tag: u16) -> bool {
        if self.qptiff { QPTIFF_SCALARS.contains(&tag) } else { is_scalar(tag) }
    }

    /// A layout tag: its values only locate the file's bytes.
    fn layout_tag(&self, tag: u16) -> bool {
        LAYOUT.contains(&tag) || (self.ndpi && NDPI_LAYOUT.contains(&tag))
    }

    /// A pointer tag (spec/zarrify/tiff.md §5.2
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#52-which-ifds>), of the
    /// variant's table and layout tags.
    fn is_pointer(&self, tag: u16, typ: u16) -> bool {
        match tag {
            330 | 34665 | 34853 | 40965 => unsigned(typ),
            400 => typ == 4,
            _ => matches!(typ, 13 | 18) && !self.table(tag) && !self.layout_tag(tag),
        }
    }

    /// The offset of an IFD's next-IFD offset, after its count and entries.
    fn next_of(&self, count: u64) -> usize {
        (self.cs() + count * self.es()) as usize
    }

    pub fn feed(&mut self, offset: u64, data: Vec<u8>) {
        self.fed.feed(offset, data);
    }

    fn ask(&mut self, r: Vec<(u64, u64)>) -> Step {
        self.asked.ask(r)
    }

    fn cs(&self) -> u64 {
        if self.big { 8 } else { 2 }
    }
    fn es(&self) -> u64 {
        if self.big { 20 } else { 12 }
    }
    fn fs(&self) -> u64 {
        if self.big { 8 } else { 4 }
    }
    fn word(&self, b: &[u8], at: usize) -> u64 {
        if self.big { u64::at(b, at, self.le) } else { u32::at(b, at, self.le) as u64 }
    }
    fn within(&self, o: u64, n: u64) -> bool {
        o.checked_add(n).is_some_and(|e| e <= self.size)
    }

    pub fn step(&mut self) -> Result<Step, String> {
        loop {
            match self.phase {
                Phase::Start => {
                    self.phase = Phase::Header;
                    return Ok(self.ask(vec![(0, 16.min(self.size))]));
                }
                Phase::Header => {
                    let n = 16.min(self.size);
                    let h = self.fed.get(0, n).ok_or("internal: no header")?.to_vec();
                    self.fed.clear();
                    // the NDPI test (spec/zarrify/ndpi.md §7
                    // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#7-ndpi-profile>):
                    // the IFD at V, the u64 at bytes 4–11, of a little-endian classic TIFF
                    if h.len() >= 12 && h[..4] == *b"II*\0" {
                        let v = u64::at(&h, 4, true);
                        if v >= 16 && v.checked_add(2).is_some_and(|e| e <= self.size) {
                            self.head = h;
                            self.phase = Phase::Detect;
                            return Ok(self.ask(vec![(v, WINDOW.min(self.size - v))]));
                        }
                    }
                    self.header(&h)?;
                    self.phase = Phase::DetectQpi;
                }
                Phase::DetectQpi => {
                    if let Some(s) = self.detect_qptiff()? {
                        return Ok(s);
                    }
                    self.phase = Phase::Chain;
                }
                Phase::Detect => {
                    let h = std::mem::take(&mut self.head);
                    let v = u64::at(&h, 4, true);
                    let n = u16::at(self.fed.get(v, 2).ok_or("internal: no NDPI entry count")?, 0, true) as u64;
                    let end = v + 2 + 12 * n;
                    if end <= self.size && self.fed.get(v + 2, 12 * n).is_none() {
                        self.head = h;
                        self.phase = Phase::DetectEntries;
                        return Ok(self.ask(vec![(v + 2, 12 * n)]));
                    }
                    self.decide(&h, v, n)?;
                }
                Phase::DetectEntries => {
                    let h = std::mem::take(&mut self.head);
                    let v = u64::at(&h, 4, true);
                    let n = u16::at(self.fed.get(v, 2).ok_or("internal: no NDPI entry count")?, 0, true) as u64;
                    self.decide(&h, v, n)?;
                }
                Phase::Chain => {
                    // the main chain, an IFD a round: each names the next
                    if let Some(s) = self.read_pending()? {
                        return Ok(s);
                    }
                    if self.next == 0 {
                        self.phase = Phase::ChainValues;
                        continue;
                    }
                    let o = self.next;
                    if self.ndpi {
                        self.check_ndpi_offset(o, true, true)?;
                    } else {
                        self.check_offset(o, true)?;
                    }
                    self.pending = vec![Pending {
                        parent: 0,
                        name: "ifds/".into(),
                        nidx: self.chain.len() as u64,
                        offset: o,
                        depth: 0,
                        tree: true,
                        from: None,
                    }];
                    self.bodies = false;
                    // the IFD may lie in the window the last one was read from (a chain
                    // written in order); else a window from it, growing while the chain
                    // keeps to the bytes just after the last window
                    if self.fed.get(o, self.cs()).is_some() {
                        continue;
                    }
                    let (a, b) = self.last_window;
                    self.chain_window = if o >= b && o < b + self.chain_window { (2 * self.chain_window).min(1 << 20) } else { WINDOW };
                    let _ = a;
                    let n = self.chain_window.min(self.size - o).max(self.cs());
                    self.last_window = (o, o + n);
                    self.fed.clear();
                    return Ok(self.ask(vec![(o, n)]));
                }
                Phase::ChainValues | Phase::SubValues => {
                    if let Some(s) = self.read_values()? {
                        return Ok(s);
                    }
                    self.phase = Phase::SubPlan;
                }
                Phase::SubPlan => {
                    // the SubIFDs of the tree's IFDs at this depth, checked before they are read
                    // (an NDPI's tree is its main chain: its SubIFDs are a pointer tag's)
                    if self.depth >= 4 || self.ndpi {
                        self.phase = Phase::Layout;
                        continue;
                    }
                    let next = self.plan_subs()?;
                    self.depth += 1;
                    if next.is_empty() {
                        self.phase = Phase::Layout;
                        continue;
                    }
                    self.pending = next;
                    self.bodies = false;
                    self.phase = Phase::SubRead;
                    let r = self.heads_of_pending();
                    return Ok(self.ask(r));
                }
                Phase::SubRead => {
                    if let Some(s) = self.read_pending()? {
                        return Ok(s);
                    }
                    self.phase = Phase::SubValues;
                }
                Phase::Layout => {
                    if !self.layout_asked.is_empty() {
                        let asked = std::mem::take(&mut self.layout_asked);
                        for (o, n) in asked {
                            let b = self.fed.get(o, n).ok_or("internal: a value the layout asked for was not fed")?.to_vec();
                            self.heads.insert((o, n), b);
                        }
                        self.fed.clear();
                    }
                    let derived = if self.ndpi {
                        self.derive_ndpi()
                    } else if self.qptiff {
                        self.derive_qptiff()
                    } else {
                        self.derive()
                    };
                    match derived {
                        Ok(l) => {
                            self.layout = Some(l);
                            self.emit_data()?;
                            self.keep_tables();
                            self.heads.clear();
                            self.start_pointers();
                            self.phase = Phase::Pointers;
                        }
                        Err(Stop::Reject(m)) => return Err(m),
                        Err(Stop::Need(mut r)) => {
                            r.sort_unstable();
                            r.dedup();
                            if r.is_empty() || r.iter().any(|x| self.heads.contains_key(x)) {
                                return Err("internal: the layout asked again for bytes it has".into());
                            }
                            self.layout_asked = r.clone();
                            return Ok(self.ask(r));
                        }
                    }
                }
                Phase::Pointers => {
                    if let Some(s) = self.pointers()? {
                        return Ok(s);
                    }
                    self.phase = Phase::Done;
                }
                Phase::Done => return Ok(Step::Done),
            }
        }
    }

    /// The SubIFDs of the tree's IFDs at the current depth, in order, each checked
    /// as the profile reads it (its values, then its offset).
    fn plan_subs(&mut self) -> Res<Vec<Pending>> {
        let at: Vec<usize> = (0..self.ifds.len())
            .filter(|&k| self.ifds[k].tree && self.ifds[k].depth == self.depth)
            .collect();
        let mut next = Vec::new();
        let mut seen: HashSet<u64> = self.by_offset.keys().copied().collect();
        for k in at {
            let Some(e) = self.ifds[k].field(330).cloned() else { continue };
            let vals = self.ints(&e, false).ok_or("internal: SubIFDs not read")?;
            self.rules.check(
                "values",
                rules::env([
                    ("tag", int(330)),
                    ("type", int(e.typ as i128)),
                    ("largest", num(vals.iter().copied().max().unwrap_or(0) as f64)),
                ]),
            )?;
            for (j, &o) in vals.iter().enumerate() {
                self.rules.check(
                    "ifd_offset",
                    rules::env([
                        ("offset", num(o as f64)),
                        ("chain", flag(false)),
                        ("header_size", num(self.hs as f64)),
                        ("seen", flag(seen.contains(&o))),
                        ("read", num(seen.len() as f64)),
                        ("count_within", flag(self.within(o, self.cs()))),
                        ("within", flag(true)),
                    ]),
                )?;
                seen.insert(o);
                next.push(Pending {
                    parent: self.ifds[k].el,
                    name: "subifds/".into(),
                    nidx: j as u64,
                    offset: o,
                    depth: self.depth + 1,
                    tree: true,
                    from: Some(k),
                });
            }
        }
        Ok(next)
    }

    // ---- the header and IFDs

    fn header(&mut self, h: &[u8]) -> Res<()> {
        let order = match h.get(..2) {
            Some(b"II") => "II",
            Some(b"MM") => "MM",
            _ => "other",
        };
        self.le = order == "II";
        let magic = if h.len() >= 4 { u16::at(h, 2, self.le) } else { 0 };
        let (os, rs) = if h.len() >= 8 { (u16::at(h, 4, self.le), u16::at(h, 6, self.le)) } else { (0, 0) };
        self.rules.check(
            "header",
            rules::env([
                ("length", int(h.len() as i128)),
                ("order", text(order)),
                ("magic", int(magic as i128)),
                ("offset_size", int(os as i128)),
                ("reserved", int(rs as i128)),
            ]),
        )?;
        self.big = magic == 43;
        let e = if self.le { "<" } else { ">" };
        self.hs = if self.big { 16 } else { 8 };
        let n = self.hs;
        let s = self.ir.struct_(0, "header", NO_INDEX, 0, Some((0, n)))?;
        self.ir.value(s, "byte_order", NO_INDEX, "ascii[2]", 0, (0, 2), Some(&h[..2]))?;
        self.ir.value(s, "magic", NO_INDEX, &format!("{e}u2"), 0, (2, 2), Some(&h[2..4]))?;
        if self.big {
            self.ir.value(s, "offset_size", NO_INDEX, &format!("{e}u2"), 0, (4, 2), Some(&h[4..6]))?;
            self.ir.value(s, "reserved", NO_INDEX, &format!("{e}u2"), 0, (6, 2), Some(&h[6..8]))?;
        }
        let w = if self.big { "u8" } else { "u4" };
        self.ir.value(s, "first_ifd", NO_INDEX, &format!("{e}{w}"), 0, (n / 2, n / 2), Some(&h[n as usize / 2..n as usize]))?;
        self.next = self.word(h, n as usize / 2);
        Ok(())
    }

    /// Decides whether the file is NDPI, from its header `h` and the IFD at `v` of `n`
    /// entries (fed), and reads the header as the variant does.
    fn decide(&mut self, h: &[u8], v: u64, n: u64) -> Res<()> {
        let within = v + 2 + 12 * n <= self.size;
        let ndpi = within
            && self.fed.get(v + 2, 12 * n).is_some_and(|e| (0..n as usize).any(|i| u16::at(e, 12 * i, true) == 65420));
        if ndpi {
            self.ndpi = true;
            self.rules = rules::of("ndpi");
            self.hs = 16;
            let s = self.ir.struct_(0, "header", NO_INDEX, 0, Some((0, 12)))?;
            self.ir.value(s, "byte_order", NO_INDEX, "ascii[2]", 0, (0, 2), Some(&h[..2]))?;
            self.ir.value(s, "magic", NO_INDEX, "<u2", 0, (2, 2), Some(&h[2..4]))?;
            self.ir.value(s, "first_ifd", NO_INDEX, "<u8", 0, (4, 8), Some(&h[4..12]))?;
            self.next = v;
            self.phase = Phase::Chain;
        } else {
            self.header(h)?;
            self.phase = Phase::DetectQpi;
        }
        Ok(())
    }

    /// The QPTIFF test (spec/zarrify/qptiff.md §7
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#7-qptiff-profile>): the
    /// first IFD's Software tag (305), ASCII, whose value starts with `PerkinElmer-QPI`. Asks for
    /// the bytes it needs (the IFD's count and entries, then the value's first 15 bytes); a file
    /// that fails any part of the test is a TIFF, so the test never rejects.
    fn detect_qptiff(&mut self) -> Res<Option<Step>> {
        let o = self.next;
        let (cs, es, fs) = (self.cs(), self.es(), self.fs());
        if !self.within(o, cs) {
            return Ok(None);
        }
        let Some(c) = self.fed.get(o, cs) else {
            let n = WINDOW.min(self.size - o).max(cs);
            return Ok(Some(self.ask(vec![(o, n)])));
        };
        let count = self.word_n(c);
        let Some(n) = count.checked_mul(es) else { return Ok(None) };
        if !self.within(o + cs, n) {
            return Ok(None);
        }
        let Some(body) = self.fed.get(o + cs, n) else {
            return Ok(Some(self.ask(vec![(o + cs, n)])));
        };
        let Some(raw) = body.chunks_exact(es as usize).find(|e| u16::at(e, 0, self.le) == 305).map(|e| e.to_vec()) else {
            return Ok(None);
        };
        let (typ, count) = (u16::at(&raw, 2, self.le), self.word(&raw, 4));
        let want = QPI_SOFTWARE.len() as u64;
        if typ != 2 || count < want {
            return Ok(None); // a longer value than the field holds: never in line
        }
        let off = self.word(&raw, 4 + fs as usize);
        if !self.within(off, want) {
            return Ok(None);
        }
        let Some(b) = self.fed.get(off, want) else {
            return Ok(Some(self.ask(vec![(off, want)])));
        };
        if b == QPI_SOFTWARE {
            self.qptiff = true;
            self.rules = rules::of("qptiff");
        }
        Ok(None)
    }

    /// An NDPI IFD (the `ifd_offset` role of `schema/ndpi.json`): in the file, not read
    /// before, within the IFD limit; its count and its body within the file.
    fn check_ndpi_offset(&self, o: u64, count_within: bool, within: bool) -> Res<()> {
        self.rules.check(
            "ifd_offset",
            rules::env([
                ("offset", num(o as f64)),
                ("safe", flag(o <= MAX_SAFE)),
                ("seen", flag(self.by_offset.contains_key(&o))),
                ("read", num(self.by_offset.len() as f64)),
                ("count_within", flag(count_within && self.within(o, 2))),
                ("within", flag(within)),
            ]),
        )
    }

    fn check_offset(&self, o: u64, chain: bool) -> Res<()> {
        self.rules.check(
            "ifd_offset",
            rules::env([
                ("offset", num(o as f64)),
                ("chain", flag(chain)),
                ("header_size", num(self.hs as f64)),
                ("seen", flag(self.by_offset.contains_key(&o))),
                ("read", num(self.by_offset.len() as f64)),
                ("count_within", flag(self.within(o, self.cs()))),
                ("within", flag(true)),
            ]),
        )
    }

    /// The values the layout read whole (tile and strip tables past the eager budget)
    /// become their elements' values, as smaller ones are: the mirror reads the
    /// tiles' extents from them.
    fn keep_tables(&mut self) {
        let mut put = Vec::new();
        for f in &self.ifds {
            for e in &f.entries {
                let (Some(v), Some(nb)) = (e.val, e.nbytes) else { continue };
                if e.inline || self.ir.value_bytes(v).is_some() {
                    continue;
                }
                if let Some(b) = self.heads.get(&(e.off, nb)) {
                    put.push((v, b.clone()));
                }
            }
        }
        for (v, b) in put {
            self.ir.put_value(v, &b);
        }
        self.ir.sort_values();
    }

    /// The first bytes of each pending IFD: its entry count, and a window of entries.
    fn heads_of_pending(&self) -> Vec<(u64, u64)> {
        self.pending
            .iter()
            .filter(|p| self.within(p.offset, self.cs()))
            .map(|p| (p.offset, WINDOW.min(self.size - p.offset).max(self.cs())))
            .collect()
    }

    /// The IFD's length: entry count, entries, next offset (None past u64).
    fn ifd_len(&self, count: u64) -> Option<u64> {
        if self.ndpi {
            // the count, the entries, the u64 next offset and a u32 high word per entry
            return count.checked_mul(16)?.checked_add(10);
        }
        count.checked_mul(self.es())?.checked_add(self.cs() + self.fs())
    }

    /// Reads the pending IFDs (their counts, then whatever of their bodies the
    /// window did not hold); None when they are all read.
    fn read_pending(&mut self) -> Res<Option<Step>> {
        if self.pending.is_empty() {
            return Ok(None);
        }
        let mut more = Vec::new();
        if !self.bodies {
            for p in &self.pending {
                let Some(c) = self.fed.get(p.offset, self.cs()) else {
                    return Err(format!("internal: no entry count at {}", p.offset));
                };
                let count = self.word_n(c);
                let len = self.ifd_len(count);
                let within = len.is_some_and(|n| self.within(p.offset, n));
                if !within && self.ndpi {
                    self.check_ndpi_offset(p.offset, true, false)?;
                }
                if !within {
                    self.rules.check(
                        "ifd_offset",
                        rules::env([
                            ("offset", num(p.offset as f64)),
                            ("chain", flag(p.tree && p.depth == 0)),
                            ("header_size", num(self.hs as f64)),
                            ("seen", flag(false)),
                            ("read", num(0.0)),
                            ("count_within", flag(true)),
                            ("within", flag(false)),
                        ]),
                    )?;
                }
                let n = len.unwrap();
                if self.fed.get(p.offset, n).is_none() {
                    more.push((p.offset, n));
                }
            }
            self.bodies = true;
            if !more.is_empty() {
                // the IFDs the window did not hold: their whole bodies
                return Ok(Some(self.ask(more)));
            }
        }
        let pending = std::mem::take(&mut self.pending);
        for p in pending {
            let c = self.fed.get(p.offset, self.cs()).ok_or("internal: no entry count")?;
            let count = self.word_n(c);
            let n = self.ifd_len(count).unwrap();
            let body = self.fed.get(p.offset, n).ok_or("internal: no IFD body")?.to_vec();
            let k = self.ifd(&p, &body)?;
            if p.tree && p.depth == 0 {
                self.chain.push(k);
                if self.ndpi {
                    self.next = u64::at(&body, self.next_of(count), true);
                } else {
                    let f = self.fs() as usize;
                    self.next = self.word(&body, body.len() - f);
                }
            }
            if let Some(parent) = p.from {
                self.ifds[parent].subs.push(k);
            }
        }
        if self.phase != Phase::Chain {
            self.fed.clear(); // the chain keeps its window for the next IFD
        }
        Ok(None)
    }

    fn word_n(&self, b: &[u8]) -> u64 {
        if self.big { u64::at(b, 0, self.le) } else { u16::at(b, 0, self.le) as u64 }
    }

    /// Emits the IFD at `p.offset` from its bytes, checking its fields (tree IFDs).
    fn ifd(&mut self, p: &Pending, body: &[u8]) -> Res<usize> {
        let (cs, es, fs) = (self.cs() as usize, self.es() as usize, self.fs() as usize);
        let o = p.offset;
        let count = self.word_n(body);
        let end = o + body.len() as u64;
        let e = if self.le { "<" } else { ">" };
        let wt = if self.big { "u8" } else { "u4" };
        let s = self.ir.struct_(p.parent, &p.name, p.nidx, 0, Some((o, end - o)))?;
        let ct = format!("{e}{}", if self.big { "u8" } else { "u2" });
        self.ir.value(s, "entry_count", NO_INDEX, &ct, 0, (o, cs as u64), Some(&body[..cs]))?;
        let k = self.ifds.len();
        self.by_offset.insert(o, k);
        self.extents.insert(o, end);
        let mut ifd = Ifd {
            offset: o,
            el: s,
            depth: p.depth,
            tree: p.tree,
            entries: Vec::with_capacity(count as usize),
            first: HashMap::new(),
            subs: Vec::new(),
        };
        let mut dups: HashMap<u16, u32> = HashMap::new();
        for i in 0..count as usize {
            let at = cs + i * es;
            let raw = &body[at..at + es];
            let tag = u16::at(raw, 0, self.le);
            let typ = u16::at(raw, 2, self.le);
            let n = self.word(raw, 4);
            let field = raw[4 + fs..4 + 2 * fs].to_vec();
            let nbytes = type_size(typ).and_then(|z| z.checked_mul(n));
            // NDPI never stores ASCII in line (spec/zarrify/ndpi.md §7
            // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#7-ndpi-profile>)
            let inline = nbytes.is_some_and(|b| b <= fs as u64 && !(self.ndpi && typ == 2 && b > 0));
            // NDPI: the entry's high word, after the next-IFD offset
            let hat = self.next_of(count) + 8 + 4 * i;
            let high = if self.ndpi { u32::at(body, hat, true) as u64 } else { 0 };
            let off = if inline { 0 } else { self.word(&field, 0) + (high << 32) };
            let dup = *dups.get(&tag).unwrap_or(&0);
            dups.insert(tag, dup + 1);
            let t = if dup == 0 {
                self.ir.struct_(s, "tags/", tag as u64, 0, Some((o + at as u64, es as u64)))?
            } else {
                self.ir.struct_(s, &format!("tags/{tag}~{dup}"), NO_INDEX, 0, Some((o + at as u64, es as u64)))?
            };
            let head = format!("{{tag:{e}u2,type:{e}u2,count:{e}{wt}");
            let ea = o + at as u64;
            let mut val = None;
            match nbytes {
                None => {
                    self.ir.value(t, "entry", NO_INDEX, &format!("{head},field:bytes[{fs}]}}"), 0, (ea, es as u64), Some(raw))?;
                }
                Some(nb) if inline => {
                    self.ir.value(t, "entry", NO_INDEX, &format!("{head}}}"), 0, (ea, 4 + fs as u64), Some(&raw[..4 + fs]))?;
                    if nb > 0 {
                        let vt = value_type(self.le, typ, n);
                        val = Some(self.ir.value(t, "value", NO_INDEX, &vt, 0, (ea + 4 + fs as u64, nb), Some(&field[..nb as usize]))?);
                    }
                }
                Some(nb) => {
                    self.ir.value(t, "entry", NO_INDEX, &format!("{head},offset:{e}{wt}}}"), 0, (ea, es as u64), Some(raw))?;
                    if self.within(off, nb) {
                        let vt = value_type(self.le, typ, n);
                        val = Some(self.ir.value(t, "value", NO_INDEX, &vt, 0, (off, nb), None)?);
                    }
                }
            }
            let hi = if self.ndpi {
                Some(self.ir.value(t, "high", NO_INDEX, "<u4", 0, (o + hat as u64, 4), Some(&body[hat..hat + 4]))?)
            } else {
                None
            };
            let ent = Ent { tag, typ, count: n, field, nbytes, inline, off, val, dup: dup > 0, high, hi };
            if dup == 0 && self.table(tag) && p.tree {
                let outside = !inline && !nbytes.is_some_and(|nb| self.within(off, nb));
                let mut env = rules::env([
                    ("tag", int(tag as i128)),
                    ("type", int(typ as i128)),
                    ("count", num(n as f64)),
                    ("outside", flag(outside)),
                ]);
                // the type is checked first: the value's place is known only for a known type
                self.rules.check("field", &mut env)?;
            }
            if dup == 0 {
                ifd.first.insert(tag, ifd.entries.len());
            }
            ifd.entries.push(ent);
        }
        let f0 = cs + count as usize * es;
        if self.ndpi {
            self.ir.value(s, "next_ifd", NO_INDEX, "<u8", 0, (o + f0 as u64, 8), Some(&body[f0..f0 + 8]))?;
        } else {
            self.ir.value(s, "next_ifd", NO_INDEX, &format!("{e}{wt}"), 0, (o + f0 as u64, fs as u64), Some(&body[f0..f0 + fs]))?;
        }
        self.ifds.push(ifd);
        Ok(k)
    }

    // ---- values

    /// An out-of-line value's bytes, when read (as an element's value, or as a head).
    fn bytes_of(&self, e: &Ent, n: u64) -> Option<Vec<u8>> {
        if e.inline {
            return Some(e.field[..n as usize].to_vec());
        }
        if let Some(v) = e.val.and_then(|v| self.ir.value_bytes(v)) {
            if n as usize <= v.len() {
                return Some(v[..n as usize].to_vec());
            }
        }
        self.heads.get(&(e.off, n)).cloned()
    }

    fn ints(&self, e: &Ent, first: bool) -> Option<Vec<u64>> {
        let z = type_size(e.typ)?;
        let n = if first { 1.min(e.count) } else { e.count };
        if self.ndpi && ndpi_high_word(e) {
            // an NDPI LONG or IFD of count 1 in line: its 4 bytes plus its high word
            return Some(vec![u32::at(&e.field, 0, true) as u64 + (e.high << 32)]);
        }
        let b = self.bytes_of(e, n * z)?;
        Some(
            b.chunks(z as usize)
                .map(|c| match e.typ {
                    1 => c[0] as u64,
                    3 => u16::at(c, 0, self.le) as u64,
                    4 | 13 => u32::at(c, 0, self.le) as u64,
                    _ => u64::at(c, 0, self.le),
                })
                .collect(),
        )
    }

    /// Asks for the out-of-line values of the IFDs read since the last time: those
    /// the profile reads whatever their size, and others up to 64 KiB (within the
    /// eager budget). None when there are none to read, or once they are read.
    fn read_values(&mut self) -> Res<Option<Step>> {
        if !self.asked_values.is_empty() {
            let asked = std::mem::take(&mut self.asked_values);
            for (o, n, els) in asked {
                let Some(b) = self.fed.get(o, n).map(|b| b.to_vec()) else {
                    return Err(format!("internal: value [{o}, {}) not fed", o + n));
                };
                if els.is_empty() {
                    self.heads.insert((o, n), b);
                    continue;
                }
                self.ir.put_value(els[0], &b);
                self.values.insert((o, n), els[0]);
                for &e in &els[1..] {
                    self.ir.put_value_of(e, els[0]);
                }
            }
            self.fed.clear();
            self.ir.sort_values();
        }
        let max_decode = self.rules.limit("max_decode") as u64;
        let eager_limit = self.rules.limit("eager_decode") as u64;
        let mut want: BTreeMap<(u64, u64), Vec<u32>> = BTreeMap::new();
        for k in 0..self.ifds.len() {
            let f = &self.ifds[k];
            if f.entries.iter().all(|e| e.val.is_none() || e.inline) {
                continue;
            }
            for e in &f.entries {
                let Some(v) = e.val else { continue };
                if e.inline || self.ir.value_bytes(v).is_some() {
                    continue;
                }
                let n = e.nbytes.unwrap();
                if let Some(&first) = self.values.get(&(e.off, n)) {
                    if self.ir.value_bytes(first).is_some() {
                        self.ir.put_value_of(v, first);
                        continue;
                    }
                }
                let required = e.tag == 330 && f.tree && f.depth < 4 && !e.dup;
                let describe = !e.dup && (self.table(e.tag) || self.layout_tag(e.tag) || self.is_pointer(e.tag, e.typ));
                let eager = describe && n <= max_decode && (want.contains_key(&(e.off, n)) || self.eager + n <= eager_limit);
                // every numeric value of at most 1024 bytes is held, whatever its tag: the
                // mirror's view shows it (conventions §8.7
                // <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#87-the-view>)
                let shown = n <= crate::mirror::VIEW_VALUE && e.typ != 7 && type_size(e.typ).is_some();
                if required || eager || shown {
                    let w = want.entry((e.off, n)).or_default();
                    if w.is_empty() && !required && eager {
                        self.eager += n;
                    }
                    w.push(v);
                }
            }
        }
        self.ir.sort_values();
        if want.is_empty() {
            return Ok(None);
        }
        // a value already asked for in this batch is read once
        self.asked_values = want.into_iter().map(|((o, n), v)| (o, n, v)).collect();
        let r: Vec<(u64, u64)> = self.asked_values.iter().map(|x| (x.0, x.1)).collect();
        Ok(Some(self.ask(r)))
    }
}

// ---- the layout (spec/zarrify/tiff.md §4
// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#4-the-image>), as the profile
// reads it

type Vals = HashMap<u16, Loaded>;

/// The OME-XML of spec/zarrify/tiff.md §3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#3-ome-xml>: the first image's
/// name, its Pixels' attributes, its TiffData elements, its first Plane at (0, 0, 0), and the
/// attributes of its first `MAX_OMERO` Channels.
#[derive(Default, Debug)]
struct Ome {
    name: Option<String>,
    pixels: Vec<(String, String)>,
    tiff_data: Vec<(Vec<(String, String)>, Option<String>)>,
    plane: Option<Vec<(String, String)>>,
    channels: Vec<Vec<(String, String)>>,
}

/// The most channels an image's `omero` lists (spec/zarrify/tiff.md §4.4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#44-arrays>), so that the root
/// does not grow with the channel count.
pub const MAX_OMERO: u64 = 64;

fn get<'a>(attrs: &'a [(String, String)], k: &str) -> Option<&'a str> {
    attrs.iter().find(|(n, _)| n == k).map(|(_, v)| v.as_str())
}

fn decoded_attrs(t: &xml::TagRef, x: &str) -> Vec<(String, String)> {
    let mut out: Vec<(String, String)> = Vec::new();
    for (k, v, _) in t.attrs(x) {
        if !out.iter().any(|(n, _)| *n == k) {
            out.push((k, xml::decode_refs(&v)));
        }
    }
    out
}

/// Whether `x` has an OME start tag, and if so its OME-XML, in one pass with
/// no tree. A Plane's TheZ, TheC and TheT are read as integers in order until
/// one is not 0 or one Plane is at (0, 0, 0): the first that is not an integer
/// rejects.
fn read_ome(x: &str, rules: &Rules) -> Res<Option<Ome>> {
    let mut ome = Ome::default();
    let mut is_ome = false;
    let mut image_seen = false;
    // 0: before Pixels, 1: inside, 2: after
    let mut state = 0;
    let mut searching: Option<usize> = None; // the TiffData whose UUID is looked for
    let mut text_from: Option<(usize, usize)> = None; // (TiffData, end of its UUID start tag)
    let mut skips: Vec<(usize, usize)> = Vec::new();
    let mut plane_error: Option<String> = None;
    let mut plane_done = false;
    for ev in xml::scan(x) {
        let t = match ev {
            Ev::Skip(a, b) => {
                if text_from.is_some() {
                    skips.push((a, b));
                }
                continue;
            }
            Ev::Tag(t) => t,
        };
        if let Some((k, from)) = text_from.take() {
            let mut s = String::new();
            let mut pos = from;
            for &(a, b) in &skips {
                s.push_str(&x[pos..a]);
                pos = b;
            }
            s.push_str(&x[pos..t.start]);
            skips.clear();
            ome.tiff_data[k].1 = Some(xml::decode_refs(&s).trim_matches([' ', '\t', '\r', '\n']).to_string());
        }
        if !t.is_end && t.name == "OME" {
            is_ome = true;
        }
        if !t.is_end && t.name == "Image" && !image_seen {
            image_seen = true;
            ome.name = t.attr(x, "Name").map(|v| v.0);
        }
        match state {
            0 => {
                if !t.is_end && t.name == "Pixels" {
                    ome.pixels = decoded_attrs(&t, x);
                    state = if t.self_closing { 2 } else { 1 };
                }
            }
            1 => {
                if t.is_end && t.name == "Pixels" {
                    state = 2;
                    searching = None;
                    continue;
                }
                if let Some(k) = searching {
                    if t.name == "TiffData" {
                        searching = None;
                    } else if !t.is_end && t.name == "UUID" {
                        searching = None;
                        let attrs = decoded_attrs(&t, x);
                        if let Some(f) = get(&attrs, "FileName") {
                            ome.tiff_data[k].1 = Some(f.to_string());
                        } else if t.self_closing {
                            ome.tiff_data[k].1 = Some(String::new());
                        } else {
                            text_from = Some((k, t.end));
                        }
                    }
                }
                if !t.is_end && t.name == "Channel" && (ome.channels.len() as u64) < MAX_OMERO {
                    ome.channels.push(decoded_attrs(&t, x));
                }
                if !t.is_end && t.name == "TiffData" {
                    ome.tiff_data.push((decoded_attrs(&t, x), None));
                    if !t.self_closing {
                        searching = Some(ome.tiff_data.len() - 1);
                    }
                }
                if !t.is_end && t.name == "Plane" && !plane_done && plane_error.is_none() {
                    let attrs = decoded_attrs(&t, x);
                    let mut zero = true;
                    for k in ["TheZ", "TheC", "TheT"] {
                        match int_attr(rules, &attrs, k, 0, 0) {
                            Ok(0) => {}
                            Ok(_) => {
                                zero = false;
                                break;
                            }
                            Err(m) => {
                                plane_error = Some(m);
                                zero = false;
                                break;
                            }
                        }
                    }
                    if zero {
                        ome.plane = Some(attrs);
                        plane_done = true;
                    }
                }
            }
            _ => {}
        }
    }
    if let Some((k, from)) = text_from {
        let mut s = String::new();
        let mut pos = from;
        for &(a, b) in &skips {
            s.push_str(&x[pos..a]);
            pos = b;
        }
        s.push_str(&x[pos..]);
        ome.tiff_data[k].1 = Some(xml::decode_refs(&s).trim_matches([' ', '\t', '\r', '\n']).to_string());
    }
    if !is_ome {
        return Ok(None);
    }
    if let Some(m) = plane_error {
        return Err(m);
    }
    Ok(Some(ome))
}

/// An OME-XML attribute read as an integer (the schema's `integer_attribute`).
fn int_attr(rules: &Rules, attrs: &[(String, String)], key: &str, default: u64, minimum: u64) -> Res<u64> {
    let Some(v) = get(attrs, key) else {
        return Ok(default);
    };
    let s = v.trim_matches([' ', '\t', '\r', '\n']);
    let digits = !s.is_empty() && s.bytes().all(|c| c.is_ascii_digit());
    let value = if digits {
        s.bytes().fold(0u128, |a, c| a.saturating_mul(10).saturating_add((c - b'0') as u128))
    } else {
        0
    };
    let mut env = rules::env([
        ("key", text(key)),
        ("text", text(v)),
        ("digits", flag(digits)),
        ("value", num(value as f64)),
        ("minimum", num(minimum as f64)),
    ]);
    rules.check("integer_attribute", &mut env)?;
    Ok(value as u64)
}

/// The `name = value` fields of an Aperio ImageDescription (spec/zarrify/tiff.md §4.4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#44-arrays>), or None.
fn aperio_fields(d: &str) -> Option<Vec<(String, String)>> {
    if !d.starts_with("Aperio") {
        return None;
    }
    let mut out: Vec<(String, String)> = Vec::new();
    for part in d.split('|') {
        if let Some((n, v)) = part.split_once('=') {
            let n = n.trim_matches([' ', '\t', '\r', '\n']).to_string();
            if !out.iter().any(|(k, _)| *k == n) {
                out.push((n, v.trim_matches([' ', '\t', '\r', '\n']).to_string()));
            }
        }
    }
    Some(out)
}

/// An IFD's JPEG prefix (spec/zarrify/tiff.md §7.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#73-chunks>): the Adobe marker
/// for 3 samples, then JPEGTables without its SOI and EOI; whether the IFD has JPEGTables, and
/// whether they are well formed (the `plane` role). Empty when the compression is not JPEG.
fn jpeg_prefix(m: &Vals, jpeg: bool, spp: u64, photometric: Option<u64>) -> (Vec<u8>, bool, bool) {
    if !jpeg {
        return (Vec::new(), false, true);
    }
    let tables = match m.get(&347) {
        Some(Loaded::Bytes(t)) => Some(t.as_slice()),
        _ => None,
    };
    let (prefix, tables_ok) = jpeg_frame(spp, photometric == Some(2), tables);
    (prefix, tables.is_some(), tables_ok)
}

/// What a JPEG stream of a TIFF image starts with (spec/zarrify/tiff.md §7.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#73-chunks>): the Adobe
/// marker for 3 samples (transform 0 when the image is RGB, else 1), then the JPEGTables
/// `tables` without their SOI and EOI; and whether the tables are well formed (tables that are
/// not are left out).
pub fn jpeg_frame(samples: u64, rgb: bool, tables: Option<&[u8]>) -> (Vec<u8>, bool) {
    let mut prefix = Vec::new();
    if samples == 3 {
        prefix.extend_from_slice(&ADOBE);
        prefix.push(if rgb { 0 } else { 1 });
    }
    let ok = tables.is_none_or(|t| t.len() >= 4 && t[..2] == [0xFF, 0xD8] && t[t.len() - 2..] == [0xFF, 0xD9]);
    if let Some(t) = tables.filter(|_| ok) {
        prefix.extend_from_slice(&t[2..t.len() - 2]);
    }
    (prefix, ok)
}

/// The forms of a strip or tile whose geometry and codec are `base`: read whole, and, when
/// the image has a shared JPEG prefix, framed by it (the stream's SOI, the prefix, the rest).
pub fn tile_forms(base: &J, shared: Option<u32>) -> (String, Option<String>) {
    let form = |recipe: J| {
        let mut j = base.clone();
        j["recipe"] = recipe;
        j.to_string()
    };
    (form(json!([["src", 0, null]])), shared.map(|x| form(json!([["src", 0, 2], ["shared", x], ["src", 2, null]]))))
}

fn first_int(m: &Vals, tag: u16) -> Option<u64> {
    match m.get(&tag) {
        Some(Loaded::Ints(v)) => v.first().copied(),
        _ => None,
    }
}

impl Tiff {
    fn view_ints(&self, b: &[u8], typ: u16) -> Vec<u64> {
        let z = type_size(typ).unwrap_or(1) as usize;
        b.chunks(z)
            .map(|c| match typ {
                1 => c[0] as u64,
                3 => u16::at(c, 0, self.le) as u64,
                4 | 13 => u32::at(c, 0, self.le) as u64,
                _ => u64::at(c, 0, self.le),
            })
            .collect()
    }

    /// The profile's `load`: the values of the table's tags an IFD's layout reads
    /// (all of them, or all but its tile tables), and IFD 0's ImageDescription.
    /// Asks for what was not read yet, for every IFD of `ks` at once.
    fn load(&self, ks: &[usize], tiles: bool, description: bool) -> Got<Vec<Vals>> {
        let mut need = Vec::new();
        let mut out = Vec::new();
        for &k in ks {
            let f = &self.ifds[k];
            let mut m = Vals::new();
            let mut tags: Vec<(u16, usize)> = f.first.iter().map(|(&t, &i)| (t, i)).collect();
            tags.sort_unstable_by_key(|x| x.1);
            for (tag, i) in tags {
                let e = &f.entries[i];
                let used = (self.used(tag) && (tiles || !matches!(tag, 324 | 325 | 273 | 279)))
                    || (tag == 270 && description && e.typ == 2);
                if !used {
                    continue;
                }
                let z = type_size(e.typ).unwrap();
                let n = match tag {
                    270 | 347 => e.count * z,
                    282 | 283 | 286 | 287 => z,
                    _ if self.scalar(tag) => z,
                    _ => e.count * z,
                };
                let n = if e.count == 0 { 0 } else { n };
                match self.bytes_of(e, n) {
                    None => need.push((e.off, n)),
                    Some(b) => {
                        let v = match tag {
                            270 | 347 => Loaded::Bytes(b),
                            282 | 283 | 286 | 287 => Loaded::Pairs(vec![(
                                u32::at(&b, 0, self.le) as u64,
                                u32::at(&b, 4, self.le) as u64,
                            )]),
                            _ => Loaded::Ints(self.view_ints(&b, e.typ)),
                        };
                        if let Loaded::Ints(v) = &v {
                            let mut env = rules::env([
                                ("tag", int(tag as i128)),
                                ("type", int(e.typ as i128)),
                                ("largest", num(v.iter().copied().max().unwrap_or(0) as f64)),
                            ]);
                            if need.is_empty() {
                                self.rules.check("values", &mut env)?;
                            }
                        }
                        m.insert(tag, v);
                    }
                }
            }
            out.push(m);
        }
        if need.is_empty() { Ok(out) } else { Err(Stop::Need(need)) }
    }

    fn req(&self, k: usize, m: &Vals, tag: u16, default: Option<u64>) -> Got<u64> {
        if let Some(v) = first_int(m, tag) {
            return Ok(v);
        }
        if let Some(d) = default {
            return Ok(d);
        }
        self.rules.check(
            "required",
            rules::env([("offset", num(self.ifds[k].offset as f64)), ("tag", int(tag as i128)), ("present", flag(false))]),
        )?;
        Ok(0)
    }

    fn reqs(&self, k: usize, m: &Vals, tag: u16) -> Got<Vec<u64>> {
        match m.get(&tag) {
            Some(Loaded::Ints(v)) => Ok(v.clone()),
            _ => {
                self.rules.check(
                    "required",
                    rules::env([("offset", num(self.ifds[k].offset as f64)), ("tag", int(tag as i128)), ("present", flag(false))]),
                )?;
                Ok(vec![])
            }
        }
    }

    fn fmt(&self, k: usize, m: &Vals) -> Got<Fmt> {
        let bits = self.reqs(k, m, 258)?;
        let formats = match m.get(&339) {
            Some(Loaded::Ints(v)) => v.clone(),
            _ => vec![1],
        };
        let spp = self.req(k, m, 277, Some(1))?;
        let planar = if spp > 1 { self.req(k, m, 284, Some(1))? } else { 1 };
        let num_list = |v: &[u64]| V::List(v.iter().map(|&x| V::Num(x as f64)).collect());
        self.rules.check(
            "format",
            rules::env([
                ("bits", num_list(&bits)),
                ("formats", num_list(&formats)),
                ("spp", num(spp as f64)),
                ("planar", num(planar as f64)),
            ]),
        )?;
        let compression = self.req(k, m, 259, Some(1))?;
        let photometric = if compression == 7 { first_int(m, 262) } else { None };
        Ok((bits[0], spp, formats[0], planar, compression, self.req(k, m, 317, Some(1))?, photometric))
    }

    fn tiled(&self, k: usize) -> bool {
        self.ifds[k].has(322) && self.ifds[k].has(324)
    }

    fn check_size(&self, k: usize, m: &Vals) -> Got<()> {
        let w = self.req(k, m, 256, None)?;
        let h = self.req(k, m, 257, None)?;
        let tiled = self.tiled(k);
        let has_counts = self.ifds[k].has(325);
        let (mut tw, mut th) = (1, 1);
        if tiled && has_counts && w >= 1 && h >= 1 {
            tw = self.req(k, m, 322, None)?;
            th = self.req(k, m, 323, None)?;
        }
        self.rules.check(
            "size",
            rules::env([
                ("offset", num(self.ifds[k].offset as f64)),
                ("width", num(w as f64)),
                ("height", num(h as f64)),
                ("tiled", flag(tiled)),
                ("has_counts", flag(has_counts)),
                ("tile_width", num(tw as f64)),
                ("tile_height", num(th as f64)),
            ]),
        )?;
        Ok(())
    }

    fn check_samples(&self, k: usize, m: &Vals) -> Got<()> {
        let compression = self.req(k, m, 259, Some(1))?;
        let fill = self.req(k, m, 266, Some(1))?;
        let photometric = self.req(k, m, 262, Some(0))?;
        let sub = match m.get(&530) {
            Some(Loaded::Ints(v)) => v.clone(),
            _ => vec![2, 2],
        };
        self.rules.check(
            "samples",
            rules::env([
                ("offset", num(self.ifds[k].offset as f64)),
                ("compression", num(compression as f64)),
                ("fill_order", num(fill as f64)),
                ("photometric", num(photometric as f64)),
                ("subsampling", V::List(sub.iter().map(|&x| V::Num(x as f64)).collect())),
            ]),
        )?;
        Ok(())
    }

    /// A level of the IFDs `ks` (one per plane): each loaded and checked.
    fn level(&self, ks: &[usize], vals: &mut HashMap<usize, Vals>) -> Got<Level> {
        let mut uniq: Vec<usize> = ks.to_vec();
        uniq.sort_unstable();
        uniq.dedup();
        let loaded = self.load(&uniq, true, false)?;
        for (k, m) in uniq.iter().zip(loaded) {
            let mut m = m;
            if let Some(old) = vals.get(k) {
                if let Some(d) = old.get(&270) {
                    m.insert(270, d.clone());
                }
            }
            vals.insert(*k, m);
        }
        for &k in ks {
            let m = &vals[&k];
            self.check_size(k, m)?;
            self.fmt(k, m)?;
            self.check_samples(k, m)?;
        }
        for &k in ks {
            self.rules.check(
                "level_ifd",
                rules::env([("offset", num(self.ifds[k].offset as f64)), ("tiled", flag(self.tiled(k))), ("same", flag(true))]),
            )?;
        }
        let k0 = ks[0];
        let m0 = &vals[&k0];
        let dims = |k: usize, m: &Vals| -> Got<(u64, u64, u64, u64)> {
            Ok((
                self.req(k, m, 256, None)?,
                self.req(k, m, 257, None)?,
                self.req(k, m, 322, None)?,
                self.req(k, m, 323, None)?,
            ))
        };
        let d0 = dims(k0, m0)?;
        let f0 = self.fmt(k0, m0)?;
        for &k in ks {
            let same = dims(k, &vals[&k])? == d0 && self.fmt(k, &vals[&k])? == f0;
            self.rules.check(
                "level_ifd",
                rules::env([("offset", num(self.ifds[k].offset as f64)), ("tiled", flag(true)), ("same", flag(same))]),
            )?;
        }
        Ok(Level { w: d0.0, h: d0.1, tw: d0.2, th: d0.3, ifds: ks.to_vec(), strips: false, pad: J::Null })
    }

    fn derive(&self) -> Got<Layout> {
        self.rules.check("images", rules::env([("ifds", num(self.chain.len() as f64))]))?;
        let i0 = self.chain[0];
        let mut vals: HashMap<usize, Vals> = HashMap::new();
        let m0 = self.load(&[i0], false, true)?.pop().unwrap();
        vals.insert(i0, m0.clone());
        // IFD 0's ImageDescription: ASCII, up to its first NUL; OME-XML when it is UTF-8 with an OME start tag
        let mut description: Option<String> = None;
        let mut ome: Option<Ome> = None;
        if self.ifds[i0].field(270).is_some_and(|e| e.typ == 2) {
            if let Some(Loaded::Bytes(d)) = m0.get(&270) {
                let raw = d.split(|&c| c == 0).next().unwrap_or(&[]);
                if let Ok(t) = std::str::from_utf8(raw) {
                    ome = read_ome(t, self.rules)?;
                    description = Some(t.to_string());
                }
            }
        }
        let f0 = self.fmt(i0, &m0)?;
        let (bits, spp, sample_format, planar, compression, predictor, photometric) = f0;
        self.rules.check("predictor", rules::env([("compression", num(compression as f64)), ("predictor", num(predictor as f64))]))?;
        // the planes (spec/zarrify/tiff.md §3
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#3-ome-xml>, §4.1
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#41-planes>), in (t, c,
        // z) order, as main-chain IFD indices
        let empty = vec![];
        let px = ome.as_ref().map(|o| &o.pixels).unwrap_or(&empty);
        let size_z = int_attr(self.rules, px, "SizeZ", 1, 1)?;
        let size_t = int_attr(self.rules, px, "SizeT", 1, 1)?;
        let mut size_c = int_attr(self.rules, px, "SizeC", spp, 1)?;
        let order = get(px, "DimensionOrder").unwrap_or("XYZCT").to_string();
        if ome.is_some() || (spp > 1 && size_c != spp) {
            self.rules.check(
                "ome_pixels",
                rules::env([("order", text(if ome.is_some() { &order } else { "XYZCT" })), ("spp", num(spp as f64)), ("size_c", num(size_c as f64))]),
            )?;
        }
        if spp > 1 && size_c != spp {
            size_c = spp;
        }
        let plane_c = if spp > 1 { 1 } else { size_c };
        let count = (size_t as u128) * (plane_c as u128) * (size_z as u128);
        let files = ome.as_ref().map(|o| {
            let mut u: Vec<&String> = o.tiff_data.iter().filter_map(|t| t.1.as_ref()).collect();
            u.sort();
            u.dedup();
            u.len()
        });
        self.rules.check("ome_planes", rules::env([("planes", num(count as f64)), ("files", num(0.0))]))?;
        let count = count as usize;
        let mut plane_ifd: Vec<i128> = vec![-1; count];
        let plane = |t: u64, c: u64, z: u64| ((t * plane_c + c) * size_z + z) as usize;
        match &ome {
            None => plane_ifd[0] = 0,
            Some(o) => {
                self.rules.check("ome_planes", rules::env([("planes", num(count as f64)), ("files", num(files.unwrap() as f64))]))?;
                let sizes = |d: u8| match d {
                    b'Z' => size_z,
                    b'C' => plane_c,
                    _ => size_t,
                };
                let default = vec![(vec![], None)];
                let entries = if o.tiff_data.is_empty() { &default } else { &o.tiff_data };
                let bound = 4 * count as u128 + 1000;
                let mut total: u128 = 0;
                let ord = order.as_bytes();
                for (a, _) in entries.iter() {
                    let mut pos = [0u64; 3]; // Z, C, T
                    pos[0] = int_attr(self.rules, a, "FirstZ", 0, 0)?;
                    pos[1] = int_attr(self.rules, a, "FirstC", 0, 0)?;
                    pos[2] = int_attr(self.rules, a, "FirstT", 0, 0)?;
                    let inside = pos[0] < size_z && pos[1] < plane_c && pos[2] < size_t;
                    self.rules.check(
                        "tiff_data",
                        rules::env([("inside", flag(inside)), ("total", num(0.0)), ("bound", num(bound as f64))]),
                    )?;
                    let mut ifd = int_attr(self.rules, a, "IFD", 0, 0)? as i128;
                    let dflt = if entries.len() == 1 && get(a, "IFD").is_none() { count as u64 } else { 1 };
                    let n = int_attr(self.rules, a, "PlaneCount", dflt, 1)? as u128;
                    let idx = |d: u8| match d {
                        b'Z' => 0,
                        b'C' => 1,
                        _ => 2,
                    };
                    let (d0, d1, d2) = (ord[2], ord[3], ord[4]);
                    let first = pos[idx(d0)] as u128
                        + sizes(d0) as u128 * (pos[idx(d1)] as u128 + sizes(d1) as u128 * pos[idx(d2)] as u128);
                    let mut n = n.min(count as u128 - first);
                    total += n;
                    self.rules.check(
                        "tiff_data",
                        rules::env([("inside", flag(true)), ("total", num(total as f64)), ("bound", num(bound as f64))]),
                    )?;
                    'fill: while n > 0 {
                        n -= 1;
                        plane_ifd[plane(pos[2], pos[1], pos[0])] = ifd;
                        ifd += 1;
                        for &d in &ord[2..] {
                            let i = idx(d);
                            pos[i] += 1;
                            if pos[i] < sizes(d) {
                                continue 'fill;
                            }
                            pos[i] = 0;
                        }
                        break;
                    }
                }
            }
        }
        let mapped = plane_ifd.iter().all(|&i| i >= 0 && (i as usize) < self.chain.len());
        self.rules.check("plane_map", rules::env([("mapped", flag(mapped))]))?;
        let planes: Vec<usize> = plane_ifd.iter().map(|&i| self.chain[i as usize]).collect();
        // the levels (spec/zarrify/tiff.md §4.2
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#42-pyramid-levels>)
        let mut levels: Vec<Level> = Vec::new();
        let subs0 = self.ifds[i0].subs.clone();
        if !subs0.is_empty() {
            let s = subs0.len();
            for &p in &planes {
                self.rules.check(
                    "subifd_levels",
                    rules::env([
                        ("offset", num(self.ifds[p].offset as f64)),
                        ("subs", num(self.ifds[p].subs.len() as f64)),
                        ("levels", num(s as f64)),
                    ]),
                )?;
            }
            levels.push(self.level(&planes, &mut vals)?);
            for k in 0..s {
                let ks: Vec<usize> = planes.iter().map(|&p| self.ifds[p].subs[k]).collect();
                levels.push(self.level(&ks, &mut vals)?);
            }
        } else {
            levels.push(self.level(&planes, &mut vals)?);
            if ome.is_none() {
                for &k in &self.chain[1..] {
                    let (pw, ph) = (levels.last().unwrap().w, levels.last().unwrap().h);
                    if !(self.tiled(k) && self.ifds[k].has(258)) {
                        continue;
                    }
                    let m = self.load(&[k], false, false)?.pop().unwrap();
                    self.check_size(k, &m)?;
                    let (w, h) = (self.req(k, &m, 256, None)?, self.req(k, &m, 257, None)?);
                    let f = self.fmt(k, &m)?;
                    vals.entry(k).or_insert(m);
                    if f == f0 && w < pw && h < ph {
                        levels.push(self.level(&[k], &mut vals)?);
                    }
                }
            }
        }
        for lv in &levels {
            for &k in &lv.ifds {
                let same = self.fmt(k, &vals[&k])? == f0;
                self.rules.check("pyramid", rules::env([("same", flag(same))]))?;
            }
        }
        // data type and codecs (spec/zarrify/tiff.md §4.3
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#43-data-types-and-codecs>)
        let mut env = rules::env([
            ("bits", num(bits as f64)),
            ("sample_format", num(sample_format as f64)),
            ("spp", num(spp as f64)),
            ("planar", num(planar as f64)),
            ("compression", num(compression as f64)),
            ("photometric", photometric.map(|p| num(p as f64)).unwrap_or(V::Absent)),
        ]);
        self.rules.check("image", &mut env)?;
        // the tiles of every plane of every level
        let contig = spp > 1 && planar == 1;
        let samples = if spp > 1 && !contig { spp } else { 1 };
        let jpeg = compression == 7;
        let mut prefixes = HashMap::new();
        for lv in &levels {
            let across = lv.w.div_ceil(lv.tw);
            let down = lv.h.div_ceil(lv.th);
            let per = across as u128 * down as u128;
            for &k in &lv.ifds {
                let m = &vals[&k];
                let offsets = self.reqs(k, m, 324)?;
                let counts = self.reqs(k, m, 325)?;
                let (prefix, has_tables, tables_ok) = jpeg_prefix(m, jpeg, spp, photometric);
                self.rules.check(
                    "plane",
                    rules::env([
                        ("offset", num(self.ifds[k].offset as f64)),
                        ("jpeg", flag(jpeg)),
                        ("has_tables", flag(has_tables)),
                        ("tables_ok", flag(tables_ok)),
                        ("tiles", num(offsets.len() as f64)),
                        ("counts", num(counts.len() as f64)),
                        ("expected", num((samples as u128 * per) as f64)),
                    ]),
                )?;
                if prefixes.contains_key(&k) {
                    continue;
                }
                for (j, (&o, &c)) in offsets.iter().zip(&counts).enumerate() {
                    if c == 0 {
                        continue;
                    }
                    let within = (o as u128 + c as u128) <= self.size as u128;
                    self.rules.check(
                        "tile",
                        rules::env([
                            ("k", num(j as f64)),
                            ("offset", num(self.ifds[k].offset as f64)),
                            ("within", flag(within)),
                            ("count", num(c as f64)),
                            ("jpeg", flag(jpeg)),
                        ]),
                    )?;
                }
                if jpeg {
                    prefixes.insert(k, prefix);
                } else {
                    prefixes.insert(k, Vec::new());
                }
            }
        }
        // pixel size and position (spec/zarrify/tiff.md §4.4
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#44-arrays>)
        let unit_of = |u: &str| self.rules.table("units").get(u).and_then(|x| x.as_str()).map(|x| x.to_string());
        let length = |u: &str| self.rules.table("lengths").get(u).and_then(|x| x.as_f64());
        let mut units: BTreeMap<char, String> = BTreeMap::new();
        let mut sizes: HashMap<char, f64> = HashMap::new();
        let mut centre: Option<(f64, f64)> = None;
        let mut corner: Option<(f64, f64)> = None;
        if let Some(o) = &ome {
            for (d, a) in [("Z", 'z'), ("Y", 'y'), ("X", 'x')] {
                if let Some(v) = get(&o.pixels, &format!("PhysicalSize{d}")).and_then(|v| xml::decimal_value(v, true)) {
                    sizes.insert(a, v);
                    if let Some(u) = unit_of(get(&o.pixels, &format!("PhysicalSize{d}Unit")).unwrap_or("µm")) {
                        units.insert(a, u);
                    }
                }
            }
            let stage = o.plane.clone().unwrap_or_default();
            let pos = |d: &str| get(&stage, &format!("Position{d}")).and_then(|v| xml::decimal_value(v, false));
            let pos_unit = |d: &str| unit_of(get(&stage, &format!("Position{d}Unit")).unwrap_or(""));
            let mut c = [0.0; 2];
            let mut all = true;
            for (i, (d, a)) in [("X", 'x'), ("Y", 'y')].into_iter().enumerate() {
                match (pos(d), pos_unit(d).and_then(|u| length(&u)), units.get(&a).and_then(|u| length(u))) {
                    (Some(p), Some(lp), Some(lu)) => c[i] = p * (lp / lu),
                    _ => all = false,
                }
            }
            if all {
                centre = Some((c[0], c[1]));
            }
        } else {
            let fields = description.as_deref().and_then(aperio_fields);
            let mpp = fields.as_ref().and_then(|f| get(f, "MPP")).and_then(|v| xml::decimal_value(v, true));
            if let Some(mpp) = mpp {
                sizes.insert('x', mpp);
                sizes.insert('y', mpp);
                units.insert('x', "micrometer".into());
                units.insert('y', "micrometer".into());
                let f = fields.unwrap();
                if let (Some(l), Some(t)) = (get(&f, "Left").and_then(|v| xml::decimal_value(v, false)), get(&f, "Top").and_then(|v| xml::decimal_value(v, false))) {
                    corner = Some((l * 1000.0, t * 1000.0));
                }
            } else {
                let per_unit = self.rules.table("resolution_units").get(first_int(&vals[&i0], 296).unwrap_or(0).to_string()).and_then(|x| x.as_f64());
                for (tag, a) in [(282u16, 'x'), (283, 'y')] {
                    if let (Some(pu), Some(Loaded::Pairs(r))) = (per_unit, vals[&i0].get(&tag)) {
                        let (n, d) = r[0];
                        if n > 0 && d > 0 {
                            let pixel = pu / (n as f64 / d as f64);
                            if pixel < 25.4 {
                                sizes.insert(a, pixel);
                                units.insert(a, "micrometer".into());
                            }
                        }
                    }
                }
            }
        }
        let axes: Vec<char> = [('t', size_t > 1), ('c', size_c > 1), ('z', size_z > 1), ('y', true), ('x', true)]
            .into_iter()
            .filter(|x| x.1)
            .map(|x| x.0)
            .collect();
        let size_of = |a: char| sizes.get(&a).copied();
        let (bw, bh) = (levels[0].w as f64, levels[0].h as f64);
        let scales: Vec<Vec<J>> = levels
            .iter()
            .map(|lv| {
                axes.iter()
                    .map(|&a| match a {
                        't' | 'c' => json!(1),
                        'z' => size_of('z').map(|v| json!(v)).unwrap_or(json!(1)),
                        'y' => json!(size_of('y').unwrap_or(1.0) * (bh / lv.h as f64)),
                        _ => json!(size_of('x').unwrap_or(1.0) * (bw / lv.w as f64)),
                    })
                    .collect()
            })
            .collect();
        let mut translation = J::Null;
        if units.contains_key(&'x') && units.contains_key(&'y') {
            if let Some((cx, cy)) = centre {
                let (sx, sy) = (size_of('x').unwrap_or(1.0), size_of('y').unwrap_or(1.0));
                corner = Some((cx - bw * sx / 2.0, cy - bh * sy / 2.0));
            }
            if let Some((x, y)) = corner {
                self.rules.check("translation", rules::env([("x", num(x)), ("y", num(y))]))?;
                translation = J::Array(axes.iter().map(|&a| match a {
                    'x' => json!(x),
                    'y' => json!(y),
                    _ => json!(0),
                }).collect());
            }
        }
        let name = ome.as_ref().and_then(|o| o.name.clone()).filter(|n| !n.is_empty());
        let pair = |m: &Vals, t: u16| match m.get(&t) {
            Some(Loaded::Pairs(p)) => json!([p[0].0, p[0].1]),
            _ => J::Null,
        };
        let m0 = &vals[&i0];
        let facts = json!({
            "little": self.le,
            "bigtiff": self.big,
            "ifds": self.chain.len(),
            "bits": bits,
            "spp": spp,
            "sample_format": sample_format,
            "planar": planar,
            "compression": compression,
            "photometric": photometric,
            "sizes": {"t": size_t, "c": size_c, "z": size_z},
            "plane_c": plane_c,
            "ome": ome.as_ref().map(|o| json!({
                "name": o.name,
                "pixels": o.pixels.iter().map(|(k, v)| (k.clone(), J::String(v.clone()))).collect::<serde_json::Map<_, _>>(),
                "plane": o.plane.as_ref().map(|p| p.iter().map(|(k, v)| (k.clone(), J::String(v.clone()))).collect::<serde_json::Map<_, _>>()),
                "channels": o.channels.iter().map(|c| json!({"name": get(c, "Name"), "color": get(c, "Color")})).collect::<Vec<_>>(),
            })),
            "interpretation": first_int(m0, 262),
            "axes": axes.iter().map(|a| a.to_string()).collect::<Vec<_>>(),
            "units": units.iter().map(|(a, u)| (a.to_string(), J::String(u.clone()))).collect::<serde_json::Map<_, _>>(),
            "scales": scales,
            "translation": translation,
            "name": name,
            "resolution_unit": first_int(m0, 296).unwrap_or(0),
            "x_resolution": pair(m0, 282),
            "y_resolution": pair(m0, 283),
        });
        let _ = &vals;
        Ok(Layout { facts, prefixes, levels, planes: ome.as_ref().map(|_| planes.clone()) })
    }
}

// ---- the strips and tiles, the planes, and the IFDs other pointer tags lead to

impl Tiff {
    /// An IFD's JPEG prefix by its own tags (for an IFD no level uses).
    fn own_prefix(&self, k: usize) -> Vec<u8> {
        if self.ndpi {
            return Vec::new(); // an NDPI strip is a whole JPEG stream
        }
        let f = &self.ifds[k];
        let one = |t: u16| f.field(t).filter(|e| unsigned(e.typ)).and_then(|e| self.ints(e, true)).and_then(|v| v.first().copied());
        if one(259).unwrap_or(1) != 7 {
            return Vec::new();
        }
        let tables = f.field(347).and_then(|e| self.bytes_of(e, e.nbytes.unwrap_or(0)));
        jpeg_frame(one(277).unwrap_or(1), one(262) == Some(2), tables.as_deref()).0
    }

    /// The IFD's strips and tiles as `data` elements, with geometry,
    /// codec and recipe; an IFD whose tables and codec are an earlier IFD's names
    /// its tiles by alias. Returns the tiles and strips structs (or what their aliases name).
    fn emit_ifd_data(&mut self, k: usize, by_key: &mut HashMap<String, u32>) -> Res<(Option<u32>, Option<u32>)> {
        let (mut tiles, mut strips) = (None, None);
        for (name, ot, ct) in [("tiles", 324u16, 325u16), ("strips", 273, 279)] {
            let f = &self.ifds[k];
            let (Some(eo), Some(ec)) = (f.field(ot).cloned(), f.field(ct).cloned()) else { continue };
            if !unsigned(eo.typ) || !unsigned(ec.typ) {
                continue;
            }
            let (Some(mut offsets), Some(mut counts)) = (self.ints(&eo, false), self.ints(&ec, false)) else { continue };
            let at = |e: &Ent| if e.inline { format!("i{}", e.field.len()) } else { format!("{}+{}", e.off, e.nbytes.unwrap_or(0)) };
            // an NDPI's tiles: each offset and byte count with its high bytes
            let mut highs = String::new();
            if self.ndpi && name == "tiles" {
                if let Some(t) = self.ndpi_tiles(k) {
                    (offsets, counts) = (t.offsets, t.counts);
                    for tag in t.added {
                        highs.push_str(&format!("|{tag}:{}", f.field(tag).map(at).unwrap_or_default()));
                    }
                }
            }
            let prefix = match self.layout.as_ref().and_then(|l| l.prefixes.get(&k)) {
                Some(p) => p.clone(),
                None => self.own_prefix(k),
            };
            let one = |t: u16| f.field(t).filter(|e| unsigned(e.typ)).and_then(|e| self.ints(e, true)).and_then(|v| v.first().copied());
            let (rows, cols) = if name == "tiles" {
                (one(323), one(322))
            } else {
                (one(278).or(one(257)), one(256))
            };
            let base = json!({
                "geometry": {"shape": [rows, cols], "samples": one(277).unwrap_or(1),
                             "bits": f.field(258).and_then(|e| self.ints(e, true)).and_then(|v| v.first().copied())},
                "codec": {"compression": one(259).unwrap_or(1)},
            });
            let key = if eo.inline || ec.inline {
                format!("{k}/{name}") // in-line tables are the IFD's own
            } else {
                format!("{name}|{}|{}|{}|{}{highs}", at(&eo), at(&ec), base, prefix.iter().map(|b| format!("{b:02x}")).collect::<String>())
            };
            let parent = self.ifds[k].el;
            if let Some(&t) = by_key.get(&key) {
                self.ir.alias(parent, name, NO_INDEX, t)?;
                if name == "tiles" {
                    tiles = Some(t);
                } else {
                    strips = Some(t);
                }
                continue;
            }
            let s = self.ir.struct_(parent, name, NO_INDEX, 0, None)?;
            by_key.insert(key, s);
            let shared = if prefix.is_empty() { None } else { Some(self.ir.share(&prefix)) };
            let (plain, framed) = tile_forms(&base, shared);
            // one element a tile, not folded into runs: the mirror folds the tiles of a
            // table into one column run whose extents it reads from the table itself
            for (j, (&o, &c)) in offsets.iter().zip(&counts).enumerate() {
                if c == 0 || !self.within(o, c) {
                    continue;
                }
                let form = match &framed {
                    Some(fr) if c > 2 => fr.as_str(),
                    _ => plain.as_str(),
                };
                self.ir.data(s, "", j as u64, 0, (o, c), form)?;
            }
            if name == "tiles" {
                tiles = Some(s);
            } else {
                strips = Some(s);
            }
        }
        Ok((tiles, strips))
    }

    fn emit_data(&mut self) -> Res<()> {
        let mut by_key = HashMap::new();
        let mut tiles_of: HashMap<usize, (Option<u32>, Option<u32>)> = HashMap::new();
        for k in 0..self.ifds.len() {
            let t = self.emit_ifd_data(k, &mut by_key)?;
            tiles_of.insert(k, t);
        }
        self.data_keys = by_key;
        let lay = self.layout.as_mut().unwrap();
        if self.ndpi {
            // derive_ndpi wrote the levels; a tiled JPEG XR level's chunks are its IFD's tiles
            for lv in lay.facts["levels"].as_array_mut().into_iter().flatten() {
                if let Some(k) = lv["tiles_ifd"].as_u64() {
                    lv["tiles"] = json!(tiles_of.get(&(k as usize)).and_then(|t| t.0));
                }
            }
            return Ok(());
        }
        let levels: Vec<J> = lay
            .levels
            .iter()
            .map(|lv| {
                // a level's chunks are its IFDs' tiles, or (QPTIFF) strips
                let of = |k: &usize| tiles_of.get(k).copied().and_then(|(t, s)| if lv.strips { s } else { t });
                json!({"w": lv.w, "h": lv.h, "tw": lv.tw, "th": lv.th, "strips": lv.strips, "pad": lv.pad,
                       "planes": lv.ifds.iter().map(of).collect::<Vec<_>>()})
            })
            .collect();
        lay.facts["levels"] = J::Array(levels);
        if let Some(planes) = lay.planes.clone() {
            let o = self.ir.struct_(0, "ome", NO_INDEX, 0, None)?;
            for (p, k) in planes.iter().enumerate() {
                let el = self.ifds[*k].el;
                self.ir.alias(o, "planes/", p as u64, el)?;
            }
        }
        Ok(())
    }

    /// The pointer tags of the IFDs read, in tree order (spec/zarrify/tiff.md §5
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#5-source-metadata>).
    fn start_pointers(&mut self) {
        self.todo.clear();
        let mut ks: Vec<usize> = (0..self.ifds.len()).collect();
        ks.sort_by_key(|&k| self.ifds[k].el);
        self.queue_pointers(&ks);
    }

    fn queue_pointers(&mut self, ks: &[usize]) {
        for &k in ks {
            let f = &self.ifds[k];
            for e in &f.entries {
                if e.dup || !self.is_pointer(e.tag, e.typ) || (e.tag == 330 && f.tree && !self.ndpi) {
                    continue;
                }
                let Some(v) = self.ints(e, false) else { continue };
                let l = label(e.tag);
                for (j, &o) in v.iter().enumerate() {
                    let name = if v.len() > 1 { format!("{l}/{j}") } else { l.clone() };
                    self.todo.push((k, e.tag, name, o));
                }
            }
        }
    }

    /// The IFDs pointer tags lead to: an alias when read before; else, within
    /// depth 4 and 10,000 tries, the IFD at the offset when its entries lie within
    /// the file and it overlaps no IFD read. None when there are none left.
    fn pointers(&mut self) -> Res<Option<Step>> {
        loop {
            match self.ptr_state {
                0 => {
                    if self.todo.is_empty() {
                        return Ok(None);
                    }
                    let todo = std::mem::take(&mut self.todo);
                    let max_depth = self.rules.limit("max_depth") as u32;
                    let max_tries = self.rules.limit("max_tries") as u64;
                    let mut cands = Vec::new();
                    for (k, _tag, name, o) in todo {
                        let parent = self.ifds[k].el;
                        if let Some(&t) = self.by_offset.get(&o) {
                            let el = self.ifds[t].el;
                            self.ir.alias(parent, &name, NO_INDEX, el)?;
                        } else if self.ifds[k].depth < max_depth && self.tries < max_tries {
                            self.tries += 1;
                            if o >= self.hs && self.within(o, self.cs()) {
                                cands.push(Pending {
                                    parent,
                                    name,
                                    nidx: NO_INDEX,
                                    offset: o,
                                    depth: self.ifds[k].depth + 1,
                                    tree: false,
                                    from: None,
                                });
                            }
                        }
                    }
                    if cands.is_empty() {
                        continue;
                    }
                    self.pending = cands;
                    self.ptr_state = 1;
                    let r = self.heads_of_pending();
                    return Ok(Some(self.ask(r)));
                }
                1 => {
                    // keep the candidates whose entries are within the file and overlap no IFD read
                    let pending = std::mem::take(&mut self.pending);
                    let mut keep = Vec::new();
                    let mut more = Vec::new();
                    let mut accepted = HashSet::new();
                    for p in pending {
                        if accepted.contains(&p.offset) {
                            keep.push(p); // read in this batch: an alias of it
                            continue;
                        }
                        let Some(c) = self.fed.get(p.offset, self.cs()) else { continue };
                        let count = self.word_n(c);
                        let Some(n) = self.ifd_len(count) else { continue };
                        let end = p.offset.saturating_add(n);
                        if count == 0 || !self.within(p.offset, n) || self.by_offset.contains_key(&p.offset) {
                            continue;
                        }
                        if let Some((_, &e)) = self.extents.range(..end).next_back() {
                            if e > p.offset {
                                continue;
                            }
                        }
                        self.extents.insert(p.offset, end);
                        accepted.insert(p.offset);
                        if self.fed.get(p.offset, n).is_none() {
                            more.push((p.offset, n));
                        }
                        keep.push(p);
                    }
                    for o in &accepted {
                        self.extents.remove(o);
                    }
                    self.pending = keep;
                    self.ptr_state = 2;
                    if !more.is_empty() {
                        return Ok(Some(self.ask(more)));
                    }
                }
                2 => {
                    let pending = std::mem::take(&mut self.pending);
                    let mut made = Vec::new();
                    for p in pending {
                        if self.by_offset.contains_key(&p.offset) {
                            let el = self.ifds[self.by_offset[&p.offset]].el;
                            self.ir.alias(p.parent, &p.name, NO_INDEX, el)?;
                            continue;
                        }
                        let c = self.fed.get(p.offset, self.cs()).ok_or("internal: no pointer IFD count")?;
                        let n = self.ifd_len(self.word_n(c)).unwrap();
                        let body = self.fed.get(p.offset, n).ok_or("internal: no pointer IFD body")?.to_vec();
                        made.push(self.ifd(&p, &body)?);
                    }
                    self.fed.clear();
                    self.made = made;
                    self.ptr_state = 3;
                }
                _ => {
                    if let Some(s) = self.read_values()? {
                        return Ok(Some(s));
                    }
                    let made = std::mem::take(&mut self.made);
                    let mut by_key = std::mem::take(&mut self.data_keys);
                    for &k in &made {
                        self.emit_ifd_data(k, &mut by_key)?;
                    }
                    self.data_keys = by_key;
                    self.queue_pointers(&made);
                    self.ptr_state = 0;
                }
            }
        }
    }

    /// The IR, finished (aliases and gaps), and the facts the projection reads.
    pub fn finish(self) -> Result<(Ir, J), String> {
        let mut facts = self.layout.map(|l| l.facts).unwrap_or(J::Null);
        self.asked.put(&mut facts);
        sans::finish(self.phase == Phase::Done, self.ir, facts)
    }
}

// ---- the NDPI layout (spec/zarrify/ndpi.md §3
// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#3-levels>, §4
// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#4-the-image>), as the profile
// reads it

/// An NDPI tag's values, as the profile reads them.
#[derive(Clone, Debug)]
enum Nv {
    Ints(Vec<u64>),
    Signed(Vec<i64>),
    Floats(Vec<f64>),
    Rats(Vec<(u64, u64)>),
}

impl Nv {
    fn ints(&self) -> Option<&[u64]> {
        match self {
            Nv::Ints(v) => Some(v),
            _ => None,
        }
    }
    /// The first value as a number.
    fn first(&self) -> Option<f64> {
        match self {
            Nv::Ints(v) => v.first().map(|&x| x as f64),
            Nv::Signed(v) => v.first().map(|&x| x as f64),
            Nv::Floats(v) => v.first().copied(),
            Nv::Rats(_) => None,
        }
    }
}

type NVals = HashMap<u16, Nv>;

/// An NDPI IFD's tiles (`Tiff::ndpi_tiles`): offsets and byte counts with their high bytes,
/// whether the high tables match the tile tables, and the high tables added.
struct NdpiTiles {
    offsets: Vec<u64>,
    counts: Vec<u64>,
    matched: bool,
    added: Vec<u16>,
}

/// A level's McuStarts with its McuStartsHighBytes (`derive_ndpi`).
struct McuStarts {
    /// McuStarts, plus McuStartsHighBytes × 2^32 when `added`.
    starts: Vec<u64>,
    /// McuStartsHighBytes is absent or has as many values as McuStarts.
    same_length: bool,
    /// Checked only when `same_length` (true otherwise): McuStarts is not one in-line LONG, or
    /// McuStartsHighBytes is absent or holds its entry's high word, which its value has already.
    high_word: bool,
    /// McuStartsHighBytes is in `starts`: present, of the same length, and McuStarts not one
    /// in-line LONG.
    added: bool,
}

impl Tiff {
    /// The values of IFD `k`'s tags of NDPI's table (those of `only`, or all): a scalar's
    /// first, an array's every value. The bytes not read yet are added to `need` (and the
    /// tag left out); with `check`, an integer tag's values are checked (the `values` role).
    fn nload(&self, k: usize, only: Option<&[u16]>, need: &mut Vec<(u64, u64)>, check: bool) -> Res<NVals> {
        let f = &self.ifds[k];
        let mut tags: Vec<(u16, usize)> = f
            .first
            .iter()
            .map(|(&t, &i)| (t, i))
            .filter(|&(t, _)| self.table(t) && only.is_none_or(|o| o.contains(&t)))
            .collect();
        tags.sort_unstable_by_key(|x| x.1);
        let mut out = NVals::new();
        for (tag, i) in tags {
            let e = &f.entries[i];
            let z = type_size(e.typ).ok_or("internal: a checked NDPI tag of unknown type")?;
            let n = if NDPI_SCALARS.contains(&tag) { 1.min(e.count) } else { e.count };
            let v = if matches!(e.typ, 1 | 3 | 4 | 13 | 16 | 18) {
                let Some(mut v) = self.ints(e, n == 1 && e.count >= 1) else {
                    need.push((e.off, n * z));
                    continue;
                };
                v.truncate(n as usize);
                if check {
                    let safe = v.iter().all(|&x| x <= MAX_SAFE);
                    self.rules.check("values", rules::env([("tag", int(tag as i128)), ("safe", flag(safe))]))?;
                }
                Nv::Ints(v)
            } else {
                let Some(b) = self.bytes_of(e, n * z) else {
                    need.push((e.off, n * z));
                    continue;
                };
                let c = b.chunks_exact(z as usize);
                match e.typ {
                    8 => Nv::Signed(c.map(|x| i16::le_at(x, 0) as i64).collect()),
                    9 => Nv::Signed(c.map(|x| i32::le_at(x, 0) as i64).collect()),
                    11 => Nv::Floats(c.map(|x| f32::le_at(x, 0) as f64).collect()),
                    12 => Nv::Floats(c.map(|x| f64::le_at(x, 0)).collect()),
                    5 => Nv::Rats(c.map(|x| (u32::at(x, 0, true) as u64, u32::at(x, 4, true) as u64)).collect()),
                    _ => return Err("internal: an NDPI tag of an unexpected type".into()),
                }
            };
            out.insert(tag, v);
        }
        Ok(out)
    }

    /// A tag's first value, or the `required` role's rejection naming it.
    fn nreq(&self, m: &NVals, tag: u16, what: &str) -> Res<f64> {
        match m.get(&tag).and_then(|v| v.first()) {
            Some(x) => Ok(x),
            None => {
                self.rules.check("required", rules::env([("what", text(what)), ("present", flag(false))]))?;
                Ok(0.0)
            }
        }
    }

    fn nint(m: &NVals, tag: u16) -> Option<u64> {
        m.get(&tag).and_then(|v| v.ints()).and_then(|v| v.first().copied())
    }

    /// An entry's elements for the projection: its value, field type, and its high word when
    /// its value has it (`ndpi_high_word`).
    fn ent_facts(&self, k: usize, tag: u16) -> J {
        match self.ifds[k].field(tag) {
            Some(e) => json!({"value": e.val, "type": e.typ, "high": if ndpi_high_word(e) { e.hi } else { None }}),
            None => J::Null,
        }
    }

    /// The frame header of the strip at `s0` of `n` bytes, of a level without McuStarts
    /// (`ndpi::frame`), from its first bytes read so far: `FRAME_READ` of them, then, while
    /// the header continues past them, at least twice as many, up to `MAX_FRAME_READ` and the
    /// strip's end (past which it has none). Err: the first bytes to read next.
    fn ndpi_frame(&self, s0: u64, n: u64) -> Result<Option<ndpi::Frame>, (u64, u64)> {
        let cap = n.min(ndpi::MAX_FRAME_READ);
        let mut have = n.min(ndpi::FRAME_READ);
        loop {
            let Some(b) = self.heads.get(&(s0, have)) else { return Err((s0, have)) };
            match ndpi::frame(b) {
                ndpi::Scan::Frame(f) => return Ok(Some(f)),
                ndpi::Scan::Need(k) if k <= cap => have = cap.min(k.max(2 * have)),
                _ => return Ok(None),
            }
        }
    }

    /// The header of a JPEG XR level's first stream at `s0` of `n` bytes (`ndpi::stream`), from
    /// its first bytes read so far: `STREAM_READ` of them, then, while the header continues past
    /// them, at least twice as many, up to `MAX_STREAM_READ` and the stream's end. Err: the
    /// first bytes to read next.
    fn ndpi_stream(&self, s0: u64, n: u64) -> Result<Option<ndpi::Stream>, (u64, u64)> {
        let mut have = n.min(ndpi::STREAM_READ);
        loop {
            let Some(b) = self.heads.get(&(s0, have)) else { return Err((s0, have)) };
            match ndpi::stream(b, n) {
                Ok(h) => return Ok(h),
                Err(k) if k > have => have = k,
                Err(_) => return Ok(None),
            }
        }
    }

    /// An NDPI IFD's tiles (spec/zarrify/ndpi.md §7
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#7-ndpi-profile>), for the
    /// layout and the source model's `tiles` alike: TileOffsets and TileByteCounts, each value
    /// plus its high table's times 2^32. A table of one in-line LONG has its entry's high word
    /// in its value instead, which its high table must hold. None until the tables are read.
    fn ndpi_tiles(&self, k: usize) -> Option<NdpiTiles> {
        let f = &self.ifds[k];
        let read = |t: u16| f.field(t).filter(|e| unsigned(e.typ)).and_then(|e| Some((e, self.ints(e, false)?)));
        let ((eo, offsets), (ec, counts)) = (read(324)?, read(325)?);
        let mut t = NdpiTiles { offsets, counts, matched: true, added: Vec::new() };
        for (tag, table) in [(65324u16, eo), (65325, ec)] {
            let Some((_, high)) = read(tag) else { continue };
            let values = if tag == 65324 { &mut t.offsets } else { &mut t.counts };
            if high.len() != eo.count as usize {
                t.matched = false;
            } else if ndpi_high_word(table) {
                t.matched &= high[0] == table.high;
            } else {
                *values = ndpi::combine(std::mem::take(values), Some(&high));
                t.added.push(tag);
            }
        }
        Some(t)
    }

    /// A JPEG XR level's first stream: its strip, or its first tile whose byte count is above 0.
    fn ndpi_first(&self, k: usize, m: &NVals, tiled: bool) -> Option<(u64, u64)> {
        if tiled {
            let t = self.ndpi_tiles(k)?;
            t.offsets.iter().zip(&t.counts).find(|x| *x.1 > 0).map(|(&o, &c)| (o, c))
        } else {
            Some((Self::nint(m, 273)?, Self::nint(m, 279)?))
        }
    }

    /// Level 0's acquisition tags (spec/zarrify/ndpi.md §5
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#5-source-metadata>), as the
    /// root's source metadata, and the Fluorescence a single channel is labeled with (§4
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#4-the-image>). A tag of
    /// another field type, without a value, with a value outside the file, or a Fluorescence of
    /// more than `VIEW_VALUE` bytes, is left out; the bytes not read yet are added to `need`.
    fn ndpi_acquisition(&self, k: usize, need: &mut Vec<(u64, u64)>) -> (serde_json::Map<String, J>, Option<String>) {
        let f = &self.ifds[k];
        let (mut own, mut label) = (serde_json::Map::new(), None);
        for (tag, name, types) in NDPI_ACQUISITION {
            let Some(e) = f.field(tag).filter(|e| types.contains(&e.typ) && e.count > 0) else { continue };
            let z = type_size(e.typ).unwrap_or(1);
            let n = if e.typ == 2 { e.count } else { z };
            if (e.typ == 2 && n > crate::mirror::VIEW_VALUE) || !(e.inline || self.within(e.off, n)) {
                continue;
            }
            let v = if e.typ == 2 {
                self.bytes_of(e, n).map(|b| {
                    let t = &b[..b.iter().position(|&c| c == 0).unwrap_or(b.len())];
                    if !t.is_empty() && t.len() <= crate::project::MAX_NAME {
                        label = std::str::from_utf8(t).ok().map(str::to_string);
                    }
                    crate::text::text_json(t)
                })
            } else if e.typ == 11 {
                self.bytes_of(e, n).map(|b| crate::project::float_json(u32::le_at(&b, 0) as u64, 4, f32::le_at(&b, 0) as f64))
            } else if e.typ == 12 {
                self.bytes_of(e, n).map(|b| crate::project::float_json(u64::le_at(&b, 0), 8, f64::le_at(&b, 0)))
            } else {
                self.ints(e, true).map(|v| if v[0] <= MAX_SAFE { json!(v[0]) } else { json!(v[0].to_string()) })
            };
            match v {
                Some(v) => {
                    own.insert(name.into(), v);
                }
                None => need.push((e.off, n)),
            }
        }
        (own, label)
    }

    /// The NDPI layout: levels, chunk grids, scales and translation, as facts for
    /// `project/ndpi.rs`. Asks for the values and strip headers not read yet.
    fn derive_ndpi(&self) -> Got<Layout> {
        // The bytes first, each kind in one batch, with no check: every main-chain IFD's
        // Magnification, the tags of those it makes levels, and their strips' headers (a JPEG
        // XR level's first stream's). Then the checks, in the profile's order (an IFD's
        // Magnification, then, for a level, its tags and the level's checks; then, level by
        // level, its strip or tiles, McuStarts, header and chunks), so that a file with several
        // faults is rejected for the one the profile meets first.
        let mut need = Vec::new();
        let mut mags = Vec::new();
        for &k in &self.chain {
            mags.push(self.nload(k, Some(&[65421]), &mut need, false)?);
        }
        if !need.is_empty() {
            return Err(Stop::Need(need));
        }
        let positive = |j: usize| mags[j].get(&65421).and_then(|v| v.first()).is_some_and(|m| m > 0.0);
        for (j, &k) in self.chain.iter().enumerate() {
            if positive(j) {
                self.nload(k, None, &mut need, false)?;
            }
        }
        if !need.is_empty() {
            return Err(Stop::Need(need));
        }
        // McuStarts with McuStartsHighBytes (spec/zarrify/ndpi.md §7
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#7-ndpi-profile>)
        let starts_of = |k: usize, m: &NVals| -> Option<McuStarts> {
            let starts = m.get(&65426).and_then(|v| v.ints())?.to_vec();
            let high = m.get(&65432).and_then(|v| v.ints());
            let word = self.ifds[k].field(65426).filter(|e| ndpi_high_word(e)).map(|e| e.high);
            let mut x = McuStarts { starts, same_length: true, high_word: true, added: false };
            match (high, word) {
                (Some(h), _) if h.len() != x.starts.len() => x.same_length = false,
                (Some(h), Some(w)) => x.high_word = h[0] == w,
                (h, _) => {
                    x.added = h.is_some();
                    x.starts = ndpi::combine(x.starts, h);
                }
            }
            Some(x)
        };
        let tiled_of = |k: usize, m: &NVals| {
            let f = &self.ifds[k];
            [322u16, 323].iter().all(|&t| Self::nint(m, t).is_some_and(|v| v >= 1)) && f.has(324) && f.has(325)
        };
        let mut first_level = true;
        for (j, &k) in self.chain.iter().enumerate() {
            if !positive(j) {
                continue;
            }
            let m = self.nload(k, None, &mut need, false)?;
            if first_level {
                // level 0's acquisition tags, when out of line and not read yet
                self.ndpi_acquisition(k, &mut need);
                first_level = false;
            }
            let (s0, n) = (Self::nint(&m, 273).unwrap_or(0), Self::nint(&m, 279).unwrap_or(0));
            let fits = (s0 as u128 + n as u128) <= self.size as u128;
            if Self::nint(&m, 259) == Some(ndpi::JPEGXR) {
                // a JPEG XR level: its first stream's first bytes, for its header
                if let Some((o, c)) = self.ndpi_first(k, &m, tiled_of(k, &m)).filter(|&(o, c)| c > 0 && self.within(o, c)) {
                    if let Err(r) = self.ndpi_stream(o, c) {
                        need.push(r);
                    }
                }
                continue;
            }
            match starts_of(k, &m) {
                Some(McuStarts { starts, same_length: true, high_word: true, .. }) => {
                    let first = starts.first().is_some_and(|&f| f >= 2 && f <= n);
                    if fits && first && !self.heads.contains_key(&(s0, starts[0])) {
                        need.push((s0, starts[0]));
                    }
                }
                // a strip without McuStarts: its first bytes, for its frame header
                None if fits && n >= 4 => {
                    if let Err(r) = self.ndpi_frame(s0, n) {
                        need.push(r);
                    }
                }
                _ => {}
            }
        }
        if !need.is_empty() {
            return Err(Stop::Need(need));
        }
        // the levels (spec/zarrify/ndpi.md §3
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#3-levels>)
        let mut tags: Vec<(usize, NVals)> = Vec::new();
        let mut levels: Vec<(usize, f64, u64, u64, u64, bool)> = Vec::new(); // (IFD, magnification, w, h, compression, tiled)
        for (j, &k) in self.chain.iter().enumerate() {
            self.nload(k, Some(&[65421]), &mut need, true)?; // the profile reads Magnification first
            let mag = self.nreq(&mags[j], 65421, "Magnification")?;
            if !(mag > 0.0) {
                continue;
            }
            let m = self.nload(k, None, &mut need, true)?;
            let w = self.nreq(&m, 256, "ImageWidth")? as u64;
            let h = self.nreq(&m, 257, "ImageLength")? as u64;
            let w = Self::nint(&m, 256).unwrap_or(w);
            let h = Self::nint(&m, 257).unwrap_or(h);
            let compression = Self::nint(&m, 259).unwrap_or(1);
            let photometric = if compression == 7 { Some(self.nreq(&m, 262, "PhotometricInterpretation")? as u64) } else { Self::nint(&m, 262) };
            let bits = m.get(&258).and_then(|v| v.ints()).map(|v| v.to_vec()).unwrap_or_default();
            let f = &self.ifds[k];
            let one_strip = [273u16, 279].iter().all(|&t| f.field(t).is_some_and(|e| e.count == 1));
            let tiled = tiled_of(k, &m);
            let smaller = levels.last().is_none_or(|l| w < l.2 && h < l.3);
            let new_magnification = !levels.iter().any(|l| l.1 == mag);
            self.rules.check(
                "level",
                rules::env([
                    ("compression", num(compression as f64)),
                    ("first_compression", num(levels.first().map_or(compression, |l| l.4) as f64)),
                    ("photometric", photometric.map(|p| num(p as f64)).unwrap_or(V::Absent)),
                    ("spp", num(Self::nint(&m, 277).unwrap_or(1) as f64)),
                    ("bits", V::List(bits.iter().map(|&b| V::Num(b as f64)).collect())),
                    ("one_strip", flag(one_strip)),
                    ("tiled", flag(tiled)),
                    ("smaller", flag(smaller)),
                    ("new_magnification", flag(new_magnification)),
                    ("width", num(w as f64)),
                    ("height", num(h as f64)),
                ]),
            )?;
            levels.push((k, mag, w, h, compression, tiled));
            tags.push((k, m));
        }
        self.rules.check("images", rules::env([("levels", num(levels.len() as f64))]))?;
        let vals = |k: usize| &tags.iter().find(|t| t.0 == k).unwrap().1;
        // scale and position (spec/zarrify/ndpi.md §4
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#4-the-image>)
        let (k0, _, bw, bh, _, _) = levels[0];
        let m0 = vals(k0);
        let per_unit = self
            .rules
            .table("resolution_units")
            .get(Self::nint(m0, 296).unwrap_or(0).to_string())
            .and_then(|x| x.as_f64());
        let physical = |tag: u16| -> Option<f64> {
            let pu = per_unit?;
            let Some(Nv::Rats(r)) = m0.get(&tag) else { return None };
            let (n, d) = *r.first()?;
            if n == 0 || d == 0 {
                return None;
            }
            let pixel = pu / (n as f64 / d as f64);
            (pixel < 25.4).then_some(pixel)
        };
        let (px, py) = (physical(282), physical(283));
        let mut units = serde_json::Map::new();
        for (a, p) in [("x", px), ("y", py)] {
            if p.is_some() {
                units.insert(a.into(), json!("micrometer"));
            }
        }
        // the chunks of each level; the header's pieces are data sources, numbered by first use
        let mut pieces: Vec<Vec<u8>> = Vec::new();
        let mut piece = |b: &[u8]| -> Part {
            let i = match pieces.iter().position(|p| p == b) {
                Some(i) => i,
                None => {
                    pieces.push(b.to_vec());
                    pieces.len() - 1
                }
            };
            Part::Data(i as u32 + 1, 0, b.len() as u64)
        };
        let mut out = Vec::new();
        let mut scales = Vec::new();
        // the pixel format of level 0's first JPEG XR stream (spec/zarrify/ndpi.md §3
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#3-levels>)
        let mut format0: Option<[u8; 16]> = None;
        for (li, &(k, _, w, h, compression, tiled)) in levels.iter().enumerate() {
            let m = vals(k);
            let (s0, n) = (Self::nint(m, 273).unwrap_or(0), Self::nint(m, 279).unwrap_or(0));
            if compression == ndpi::JPEGXR {
                // a JPEG XR level: its tiles, or its strip, each a chunk
                let (cw, ch) = if tiled {
                    let (tw, th) = (Self::nint(m, 322).unwrap_or(1), Self::nint(m, 323).unwrap_or(1));
                    let t = self.ndpi_tiles(k).ok_or_else(|| Stop::Reject("internal: an NDPI level's tile tables were not read".into()))?;
                    let within = t.offsets.iter().zip(&t.counts).all(|(&o, &c)| c == 0 || self.within(o, c));
                    self.rules.check(
                        "tiles",
                        rules::env([
                            ("tiles", num(h.div_ceil(th).saturating_mul(w.div_ceil(tw)) as f64)),
                            ("offsets", num(t.offsets.len() as f64)),
                            ("counts", num(t.counts.len() as f64)),
                            ("high", flag(t.matched)),
                            ("within", flag(within)),
                        ]),
                    )?;
                    (tw, th)
                } else {
                    let within = (s0 as u128 + n as u128) <= self.size as u128;
                    self.rules.check("strip", rules::env([("within", flag(within)), ("length", num(n as f64))]))?;
                    (w, h)
                };
                let first = self.ndpi_first(k, m, tiled).filter(|x| x.1 > 0);
                let header = match first {
                    Some((o, c)) => match self.ndpi_stream(o, c) {
                        Ok(x) => x,
                        Err(r) => return Err(Stop::Need(vec![r])),
                    },
                    None => None,
                };
                let format = header.map(|x| x.0);
                if li == 0 {
                    format0 = format;
                }
                let (fw, fh) = header.map_or((0, 0), |x| (x.1, x.2));
                self.rules.check(
                    "jpegxr",
                    rules::env([
                        ("stream", flag(first.is_some())),
                        ("header", flag(header.is_some())),
                        ("format", text(&format.as_ref().map(ndpi::hex).unwrap_or_default())),
                        ("supported", flag(format.as_ref().and_then(ndpi::pixel_format).is_some())),
                        ("same", flag(format.is_some() && format == format0)),
                        ("width", num(fw as f64)),
                        ("height", num(fh as f64)),
                        ("chunk_width", num(cw as f64)),
                        ("chunk_height", num(ch as f64)),
                    ]),
                )?;
                out.push(json!({
                    "w": w, "h": h, "tw": cw, "th": ch, "strip": s0, "length": n,
                    "tiles_ifd": if tiled { json!(k) } else { J::Null },
                }));
                scales.push(json!([1, py.unwrap_or(1.0) * (bh as f64 / h as f64), px.unwrap_or(1.0) * (bw as f64 / w as f64)]));
                continue;
            }
            let within = (s0 as u128 + n as u128) <= self.size as u128;
            self.rules.check("strip", rules::env([("within", flag(within)), ("length", num(n as f64))]))?;
            let head = starts_of(k, m);
            // the projection adds McuStartsHighBytes as the layout does
            let added = head.as_ref().is_some_and(|x| x.added);
            if let Some(x) = &head {
                self.rules.check(
                    "mcu_starts",
                    rules::env([
                        ("same_length", flag(x.same_length)),
                        ("high_word", flag(x.high_word)),
                        ("first", x.starts.first().map(|&f| num(f as f64)).unwrap_or(V::Absent)),
                        ("length", num(n as f64)),
                    ]),
                )?;
            }
            // a strip without McuStarts whose frame is not the level's size, but has its MCU rows
            // and columns, is spliced the level's (spec/zarrify/ndpi.md §7
            // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#7-ndpi-profile>); with
            // others, the level is not a level
            let mut frame = J::Null;
            if head.is_none() {
                let f = match self.ndpi_frame(s0, n) {
                    Ok(f) => f,
                    Err(r) => return Err(Stop::Need(vec![r])),
                };
                if let Some(f) = f.filter(|f| (f.height, f.width) != (h, w)) {
                    let covers = f.covers(h, w);
                    self.rules.check(
                        "frame",
                        rules::env([
                            ("level", num(li as f64)),
                            ("covers", flag(covers)),
                            ("frame_height", num(f.height as f64)),
                            ("frame_width", num(f.width as f64)),
                            ("height", num(h as f64)),
                            ("width", num(w as f64)),
                        ]),
                    )?;
                    if !covers {
                        continue;
                    }
                    frame = json!(f.at);
                }
            }
            let grid = match head {
                None => J::Null,
                Some(McuStarts { starts, .. }) => {
                    let Some(hb) = self.heads.get(&(s0, starts[0])).cloned() else {
                        // a header the batch above did not foresee: ask for it, when it is in the file
                        if !self.within(s0, starts[0]) {
                            return Err(Stop::Reject("internal: a strip header outside the file passed the checks".into()));
                        }
                        return Err(Stop::Need(vec![(s0, starts[0])]));
                    };
                    let hd = ndpi::header(&hb, self.rules)?;
                    let (before, after) = (piece(&hb[..hd.sof.0]), piece(&hb[hd.sof.1..]));
                    ndpi::Grid::new(s0, n, w, h, hb, hd, starts, &before, &after, self.rules)?.facts()
                }
            };
            let high = if added { self.ent_facts(k, 65432) } else { J::Null };
            out.push(json!({
                "w": w, "h": h, "strip": s0, "length": n,
                "starts": self.ent_facts(k, 65426), "high": high, "grid": grid, "frame": frame,
            }));
            scales.push(json!([1, py.unwrap_or(1.0) * (bh as f64 / h as f64), px.unwrap_or(1.0) * (bw as f64 / w as f64)]));
        }
        let mut translation = J::Null;
        if let (Some(sx), Some(sy), Some(ox), Some(oy)) =
            (px, py, m0.get(&65422).and_then(|v| v.first()), m0.get(&65423).and_then(|v| v.first()))
        {
            let (x, y) = (ox / 1000.0 - bw as f64 * sx / 2.0, oy / 1000.0 - bh as f64 * sy / 2.0);
            self.rules.check("translation", rules::env([("x", num(x)), ("y", num(y))]))?;
            translation = json!([0, y, x]);
        }
        // the pixels (spec/zarrify/ndpi.md §3
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#3-levels>), and level 0's
        // acquisition tags (§5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#5-source-metadata>)
        let (data_type, samples, codec) = match format0.as_ref().and_then(ndpi::pixel_format) {
            Some((t, s)) => (t, s, "imagecodecs_jpegxr"),
            None => ("uint8", 3, "imagecodecs_jpeg"),
        };
        let (own, label) = self.ndpi_acquisition(k0, &mut need);
        if !need.is_empty() {
            return Err(Stop::Need(need));
        }
        let facts = json!({
            "profile": "ndpi",
            "ifds": self.chain.len(),
            "axes": ["c", "y", "x"],
            "data_type": data_type,
            "samples": samples,
            "codec": codec,
            "label": label,
            "own": own,
            "units": units,
            "scales": scales,
            "translation": translation,
            "levels": out,
        });
        Ok(Layout { facts, prefixes: HashMap::new(), levels: Vec::new(), planes: None })
    }
}

// ---- the QPTIFF layout (spec/zarrify/qptiff.md §3
// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#3-images-and-channels>, §4
// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#4-the-image>), as the profile
// reads it

/// What the layout reads of a `PerkinElmer-QPI-ImageDescription`: the root element's direct
/// children ImageType, Name, Color and SlideID (the first of each), references decoded and
/// white space trimmed.
#[derive(Default, Debug, Clone)]
struct Qpi {
    image_type: Option<String>,
    name: Option<String>,
    color: Option<String>,
    slide_id: Option<String>,
}

impl Qpi {
    fn is(&self, image_type: &str) -> bool {
        self.image_type.as_deref() == Some(image_type)
    }
}

/// The ImageDescription text `x` as a QPI description, or None when it has no
/// `PerkinElmer-QPI-ImageDescription` start tag. Read with the tag scan of
/// spec/zarrify/tiff.md §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#3-ome-xml>,
/// tracking depth: an element nested deeper than the root's children (a ScanProfile's `Name`)
/// is not read. A child's text runs from its start tag to the next tag, skipped sections
/// removed.
fn qpi_description(x: &str) -> Option<Qpi> {
    let mut out: Option<Qpi> = None;
    let mut depth: i64 = -1;
    let mut pending: Option<(&str, usize)> = None;
    let mut skips: Vec<(usize, usize)> = Vec::new();
    for ev in xml::scan(x) {
        let t = match ev {
            Ev::Skip(a, b) => {
                if pending.is_some() {
                    skips.push((a, b));
                }
                continue;
            }
            Ev::Tag(t) => t,
        };
        if let Some((name, from)) = pending.take() {
            let mut s = String::new();
            let mut pos = from;
            for &(a, b) in &skips {
                if a >= pos && b <= t.start {
                    s.push_str(&x[pos..a]);
                    pos = b;
                }
            }
            s.push_str(&x[pos..t.start]);
            skips.clear();
            let v = xml::decode_refs(&s).trim_matches([' ', '\t', '\r', '\n']).to_string();
            if let Some(q) = out.as_mut() {
                let slot = match name {
                    "ImageType" => &mut q.image_type,
                    "Name" => &mut q.name,
                    "Color" => &mut q.color,
                    _ => &mut q.slide_id,
                };
                if slot.is_none() {
                    *slot = Some(v);
                }
            }
        }
        if depth < 0 {
            if !t.is_end && t.name == "PerkinElmer-QPI-ImageDescription" {
                out = Some(Qpi::default());
                if t.self_closing {
                    break;
                }
                depth = 0;
            }
            continue;
        }
        if t.is_end {
            if depth == 0 {
                break;
            }
            depth -= 1;
            continue;
        }
        if depth == 0 && !t.self_closing && matches!(t.name, "ImageType" | "Name" | "Color" | "SlideID") {
            pending = Some((t.name, t.end));
        }
        if !t.self_closing {
            depth += 1;
        }
    }
    out
}

impl Tiff {
    /// A QPTIFF level of the IFDs `ks` (one per channel), each checked (the `size`, `samples`,
    /// `strips` and `level` roles): all the same size, tiling and format, IFD 0's format, and
    /// smaller than the level before. A level in strips has the chunk width `w` and height
    /// `min(RowsPerStrip, h)`.
    fn qlevel(&self, index: usize, ks: &[usize], vals: &HashMap<usize, Vals>, f0: &Fmt, prev: Option<&Level>) -> Got<Level> {
        let mut first: Option<(u64, u64, bool, u64, u64)> = None;
        for &k in ks {
            let m = &vals[&k];
            self.check_size(k, m)?;
            let f = self.fmt(k, m)?;
            self.check_samples(k, m)?;
            let w = self.req(k, m, 256, None)?;
            let h = self.req(k, m, 257, None)?;
            let tiled = self.tiled(k);
            let (tw, th) = if tiled {
                (self.req(k, m, 322, None)?, self.req(k, m, 323, None)?)
            } else {
                let rows = self.req(k, m, 278, Some(u32::MAX as u64))?;
                self.rules.check(
                    "strips",
                    rules::env([
                        ("offset", num(self.ifds[k].offset as f64)),
                        ("has_offsets", flag(self.ifds[k].has(273))),
                        ("has_counts", flag(self.ifds[k].has(279))),
                        ("rows", num(rows as f64)),
                    ]),
                )?;
                (w, rows.min(h))
            };
            let this = (w, h, tiled, tw, th);
            let same = first.is_none_or(|x| x == this);
            let smaller = prev.is_none_or(|p| w < p.w && h < p.h);
            self.rules.check(
                "level",
                rules::env([
                    ("offset", num(self.ifds[k].offset as f64)),
                    ("level", num(index as f64)),
                    ("previous", num(index.saturating_sub(1) as f64)),
                    ("same", flag(same)),
                    ("format", flag(f == *f0)),
                    ("smaller", flag(smaller)),
                ]),
            )?;
            first.get_or_insert(this);
        }
        let (w, h, tiled, tw, th) = first.ok_or_else(|| Stop::Reject("internal: an empty QPTIFF level".into()))?;
        Ok(Level { w, h, tw, th, ifds: ks.to_vec(), strips: !tiled, pad: J::Null })
    }

    /// The QPTIFF layout: the channels (the FullResolution images at the start of the main
    /// chain), the levels (the runs of ReducedResolution images), the data type and codecs,
    /// each IFD's tiles or strips, the pixel size, the position and the channels' names and
    /// colors, as facts for `project/qptiff.rs`. Asks for the values not read yet.
    fn derive_qptiff(&self) -> Got<Layout> {
        self.rules.check("images", rules::env([("ifds", num(self.chain.len() as f64))]))?;
        // every main-chain IFD's values but its tile and strip tables, and its description, in one batch
        let loaded = self.load(&self.chain, false, true)?;
        let mut vals: HashMap<usize, Vals> = self.chain.iter().copied().zip(loaded).collect();
        let desc = |m: &Vals| -> Option<Qpi> {
            let Some(Loaded::Bytes(d)) = m.get(&270) else { return None };
            let raw = d.split(|&c| c == 0).next().unwrap_or(&[]);
            std::str::from_utf8(raw).ok().and_then(qpi_description)
        };
        let kinds: Vec<Option<Qpi>> = self.chain.iter().map(|k| desc(&vals[k])).collect();
        let is = |q: &Option<Qpi>, t: &str| q.as_ref().is_some_and(|q| q.is(t));
        let n = kinds.iter().take_while(|q| is(q, "FullResolution")).count();
        let later = kinds[n..].iter().any(|q| is(q, "FullResolution"));
        let i0 = self.chain[0];
        let m0 = vals[&i0].clone(); // kept while the tables are loaded below
        let m0 = &m0;
        let f0 = self.fmt(i0, m0)?;
        let (bits, spp, sample_format, planar, compression, predictor, photometric) = f0;
        self.rules.check(
            "baseline",
            rules::env([
                ("described", flag(kinds[0].is_some())),
                ("baseline", flag(n >= 1)),
                ("channels", num(n as f64)),
                ("later", flag(later)),
                ("spp", num(spp as f64)),
            ]),
        )?;
        self.rules.check("predictor", rules::env([("compression", num(compression as f64)), ("predictor", num(predictor as f64))]))?;
        let reduced: Vec<usize> =
            self.chain.iter().copied().zip(&kinds).filter(|(_, q)| is(q, "ReducedResolution")).map(|(k, _)| k).collect();
        self.rules.check(
            "reduced",
            rules::env([
                ("channels", num(n as f64)),
                ("reduced", num(reduced.len() as f64)),
                ("remainder", num((reduced.len() % n) as f64)),
            ]),
        )?;
        let mut levels = vec![self.qlevel(0, &self.chain[..n], &vals, &f0, None)?];
        for (i, g) in reduced.chunks(n).enumerate() {
            let lv = self.qlevel(i + 1, g, &vals, &f0, levels.last())?;
            levels.push(lv);
        }
        // data type and codecs (spec/zarrify/qptiff.md §4.1
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#41-data-types-and-codecs>)
        let mut env = rules::env([
            ("bits", num(bits as f64)),
            ("sample_format", num(sample_format as f64)),
            ("spp", num(spp as f64)),
            ("planar", num(planar as f64)),
            ("compression", num(compression as f64)),
            ("photometric", photometric.map(|p| num(p as f64)).unwrap_or(V::Absent)),
        ]);
        self.rules.check("image", &mut env)?;
        // the tiles or strips of every IFD of every level: their tables, in one batch
        let used: Vec<usize> = levels.iter().flat_map(|l| l.ifds.iter().copied()).collect();
        for (k, m) in used.iter().copied().zip(self.load(&used, true, false)?) {
            let d = vals.get(&k).and_then(|old| old.get(&270).cloned());
            let mut m = m;
            if let Some(d) = d {
                m.insert(270, d);
            }
            vals.insert(k, m);
        }
        let contig = spp > 1 && planar == 1;
        let samples = if spp > 1 && !contig { spp } else { 1 };
        let jpeg = compression == 7;
        let mut prefixes = HashMap::new();
        for lv in &levels {
            let per: u128 = if lv.strips {
                lv.h.div_ceil(lv.th) as u128
            } else {
                lv.w.div_ceil(lv.tw) as u128 * lv.h.div_ceil(lv.th) as u128
            };
            let (ot, ct) = if lv.strips { (273, 279) } else { (324, 325) };
            for &k in &lv.ifds {
                let m = &vals[&k];
                let offsets = self.reqs(k, m, ot)?;
                let counts = self.reqs(k, m, ct)?;
                let (prefix, has_tables, tables_ok) = jpeg_prefix(m, jpeg, spp, photometric);
                self.rules.check(
                    "plane",
                    rules::env([
                        ("offset", num(self.ifds[k].offset as f64)),
                        ("jpeg", flag(jpeg)),
                        ("has_tables", flag(has_tables)),
                        ("tables_ok", flag(tables_ok)),
                        ("tiles", num(offsets.len() as f64)),
                        ("counts", num(counts.len() as f64)),
                        ("expected", num((samples as u128 * per) as f64)),
                    ]),
                )?;
                if prefixes.contains_key(&k) {
                    continue;
                }
                for (j, (&o, &c)) in offsets.iter().zip(&counts).enumerate() {
                    if c == 0 {
                        continue;
                    }
                    let within = (o as u128 + c as u128) <= self.size as u128;
                    self.rules.check(
                        "tile",
                        rules::env([
                            ("k", num(j as f64)),
                            ("offset", num(self.ifds[k].offset as f64)),
                            ("within", flag(within)),
                            ("count", num(c as f64)),
                            ("jpeg", flag(jpeg)),
                        ]),
                    )?;
                }
                prefixes.insert(k, prefix);
            }
        }
        // a level in strips whose last strip is short (spec/zarrify/qptiff.md §4.2
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#42-tiles-and-strips>):
        // padded to the chunk, with zeros when uncompressed, or with a literal tail spliced into
        // the strip's LZW stream, which is read for that; of another compression, not a level
        let row_bytes = |lv: &Level| lv.w * (bits / 8) * if contig { spp } else { 1 };
        let last_strips = |lv: &Level, k: usize, m: &Vals| -> Got<Vec<(usize, u64, u64)>> {
            let down = lv.h.div_ceil(lv.th);
            let (offsets, counts) = (self.reqs(k, m, 273)?, self.reqs(k, m, 279)?);
            Ok((0..samples).map(|s| (s as usize * down as usize + down as usize - 1, 0, 0)).map(|(j, _, _)| (j, offsets[j], counts[j])).collect())
        };
        let short_of = |lv: &Level| -> Option<u64> {
            let down = lv.h.div_ceil(lv.th);
            let short = lv.h - lv.th * (down - 1);
            (lv.strips && down > 1 && short != lv.th).then_some(short)
        };
        let mut need = Vec::new();
        if compression == 5 {
            for lv in &levels {
                if short_of(lv).is_none() {
                    continue;
                }
                for &k in &lv.ifds {
                    for (_, o, c) in last_strips(lv, k, &vals[&k])? {
                        if c > 0 && !self.heads.contains_key(&(o, c)) {
                            need.push((o, c));
                        }
                    }
                }
            }
        }
        if !need.is_empty() {
            return Err(Stop::Need(need));
        }
        let mut keep = vec![true; levels.len()];
        for (li, lv) in levels.iter_mut().enumerate() {
            let Some(short) = short_of(lv) else { continue };
            let rows = row_bytes(lv);
            let missing = (lv.th - short) * rows;
            match compression {
                1 => lv.pad = json!({"zeros": missing}),
                5 => {
                    let mut pads = Vec::new();
                    for (c, &k) in lv.ifds.iter().enumerate() {
                        for (j, o, n) in last_strips(lv, k, &vals[&k])? {
                            if n == 0 {
                                continue; // an absent strip: an absent chunk
                            }
                            let b = self.heads.get(&(o, n)).ok_or_else(|| Stop::Reject("internal: a strip the layout asked for was not fed".into()))?;
                            let (ok, problem, pad) = match lzw::scan(b, short * rows).map(|cut| lzw::tail(b, cut, missing)) {
                                Ok((prefix, tail)) => (true, String::new(), json!({"c": c, "k": j, "prefix": prefix, "tail": tail})),
                                Err(e) => (false, e, J::Null),
                            };
                            self.rules.check(
                                "strip_tail",
                                rules::env([
                                    ("offset", num(self.ifds[k].offset as f64)),
                                    ("strip", num(j as f64)),
                                    ("decodes", flag(ok)),
                                    ("problem", text(&problem)),
                                ]),
                            )?;
                            pads.push(pad);
                        }
                    }
                    lv.pad = json!({"lzw": pads});
                }
                _ => keep[li] = false,
            }
            self.rules.check("padding", rules::env([("level", num(li as f64)), ("compression", num(compression as f64)), ("kept", flag(keep[li]))]))?;
        }
        let levels: Vec<Level> = levels.into_iter().zip(keep).filter(|(_, k)| *k).map(|(l, _)| l).collect();
        // pixel size (the TIFF convention's resolution tags) and position (spec/zarrify/qptiff.md §4.3
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#43-arrays>)
        let per_unit = self
            .rules
            .table("resolution_units")
            .get(first_int(m0, 296).unwrap_or(0).to_string())
            .and_then(|x| x.as_f64());
        let rational = |tag: u16| -> Option<(u64, u64)> {
            let Some(Loaded::Pairs(p)) = m0.get(&tag) else { return None };
            p.first().copied()
        };
        let physical = |tag: u16| -> Option<f64> {
            let pu = per_unit?;
            let (a, b) = rational(tag)?;
            if a == 0 || b == 0 {
                return None;
            }
            let pixel = pu / (a as f64 / b as f64);
            (pixel < 25.4).then_some(pixel)
        };
        let (px, py) = (physical(282), physical(283));
        let has_c = n > 1 || spp > 1;
        let axes: Vec<&str> = if has_c { vec!["c", "y", "x"] } else { vec!["y", "x"] };
        let mut units = serde_json::Map::new();
        for (a, p) in [("x", px), ("y", py)] {
            if p.is_some() {
                units.insert(a.into(), json!("micrometer"));
            }
        }
        let (w0, h0) = (levels[0].w, levels[0].h);
        let scales: Vec<J> = levels
            .iter()
            .map(|lv| {
                let sy = py.unwrap_or(1.0) * (h0 as f64 / lv.h as f64);
                let sx = px.unwrap_or(1.0) * (w0 as f64 / lv.w as f64);
                if has_c { json!([1, sy, sx]) } else { json!([sy, sx]) }
            })
            .collect();
        let mut translation = J::Null;
        if let (Some(_), Some(_), Some(pu)) = (px, py, per_unit) {
            let position = |tag: u16| -> Option<f64> {
                let (a, b) = rational(tag)?;
                (b != 0).then(|| a as f64 / b as f64 * pu)
            };
            if let (Some(x), Some(y)) = (position(286), position(287)) {
                self.rules.check("translation", rules::env([("x", num(x)), ("y", num(y))]))?;
                translation = if has_c { json!([0, y, x]) } else { json!([y, x]) };
            }
        }
        let channels: Vec<J> = kinds[..n]
            .iter()
            .map(|q| {
                let q = q.as_ref().expect("a channel is a described FullResolution image");
                json!({"name": q.name, "color": q.color})
            })
            .collect();
        let name = kinds[0].as_ref().and_then(|q| q.slide_id.clone()).filter(|s| !s.is_empty());
        let facts = json!({
            "profile": "qptiff",
            "little": self.le,
            "bigtiff": self.big,
            "ifds": self.chain.len(),
            "bits": bits,
            "spp": spp,
            "sample_format": sample_format,
            "planar": planar,
            "compression": compression,
            "photometric": photometric,
            "channels": channels,
            "axes": axes,
            "units": units,
            "scales": scales,
            "translation": translation,
            "name": name,
        });
        Ok(Layout { facts, prefixes, levels, planes: None })
    }
}

impl crate::Parser for Tiff {
    fn new(size: u64) -> Self {
        Tiff::new(size)
    }
    fn step(&mut self) -> Result<crate::nd2::Step, String> {
        Tiff::step(self)
    }
    fn feed(&mut self, offset: u64, data: Vec<u8>) {
        Tiff::feed(self, offset, data)
    }
    fn finish(self) -> Result<(crate::ir::Ir, serde_json::Value), String> {
        Tiff::finish(self)
    }
    fn found(&self) -> Option<&'static str> {
        Some(if self.ndpi {
            "ndpi"
        } else if self.qptiff {
            "qptiff"
        } else {
            "tiff"
        })
    }
}

/// The TIFF profile's file run (spec/zarrify/tiff.md); a run of it becomes one of NDPI or
/// QPTIFF when its parser finds the file is one (`Parser::found`).
pub struct Profile;

impl crate::run::FileProfile for Profile {
    /// A TIFF or BigTIFF header, in either byte order.
    fn detect(&self, head: &[u8]) -> bool {
        head.len() >= 4 && [b"II*\0", b"MM\0*", b"II+\0", b"MM\0+"].iter().any(|m| head[..4] == m[..])
    }
    fn parser(&self, size: u64) -> crate::run::AnyParser {
        crate::run::AnyParser::of::<Tiff>(size)
    }
    fn project(&self, ir: &crate::ir::Ir, facts: &serde_json::Value, url: &str, copied: &crate::project::Copied) -> Result<crate::out::Out, String> {
        crate::project::tiff::project(ir, facts, url, copied)
    }
    fn reads(&self, ir: &crate::ir::Ir, facts: &serde_json::Value) -> Result<Vec<(u64, u64)>, String> {
        crate::project::tiff::reads(ir, facts)
    }
}
