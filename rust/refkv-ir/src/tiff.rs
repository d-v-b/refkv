//! A sans-IO parser of TIFF, BigTIFF and OME-TIFF files into the IR
//! (spec/zarrify/tiff.md §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#7-tiff-profile>,
//! spec/zarrify/tiff.md): it never reads; it asks for batches
//! of ranges (`step`), is given their bytes (`feed`), and holds them only until
//! the step that uses them.
//!
//! Rounds: (1) the header; (2) the main chain, one IFD a round (each IFD names
//! the next); (3) the out-of-line values of the chain's IFDs that the profile
//! reads, their tile and strip tables (whatever their size: the IR lists every IFD's
//! tiles), and those that are small; (4) the SubIFDs, a depth a round, with their values;
//! (5) the values the layout needs that were not read yet, as the layout asks for them;
//! (6) the IFDs other pointer tags lead to, with their values, for the IR only. The
//! schema (`schema/tiff.json`) is checked as the profile meets each role: a file that
//! fails is rejected there, and nowhere else.
//!
//! **The QPTIFF variant** (spec/zarrify/qptiff.md
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md>). After the header (and
//! the NDPI test), the first IFD's Software tag decides whether the file is an Akoya QPTIFF
//! (`detect_qptiff`, spec/zarrify/qptiff.md §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#7-qptiff-profile>, which
//! never rejects). A QPTIFF is read as a TIFF, with the table of `QPTIFF_TABLE` (the position
//! tags added), the schema `schema/qptiff.json`, and its
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
use crate::rules::{self, Rules, flag, int, num, text};
use crate::sans;
use crate::xml::{self, Ev};
use serde_json::{Value as J, json};
use std::collections::{BTreeMap, HashMap, HashSet};

const TABLE: [u16; 24] = [
    256, 257, 258, 259, 262, 266, 270, 273, 277, 278, 279, 282, 283, 284, 296, 317, 322, 323, 324, 325, 330, 339, 347, 530,
];
const SCALARS: [u16; 14] = [256, 257, 259, 262, 266, 277, 278, 282, 283, 284, 296, 317, 322, 323];
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
/// the position tags.
const QPTIFF_TABLE: [u16; 26] = [
    256, 257, 258, 259, 262, 266, 270, 273, 277, 278, 279, 282, 283, 284, 286, 287, 296, 317, 322, 323, 324, 325, 330, 339, 347,
    530,
];
const QPTIFF_SCALARS: [u16; 16] = [256, 257, 259, 262, 266, 277, 278, 282, 283, 284, 286, 287, 296, 317, 322, 323];
/// What a QPTIFF's Software tag starts with (spec/zarrify/qptiff.md §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#7-qptiff-profile>).
const QPI_SOFTWARE: &[u8] = b"PerkinElmer-QPI";
const MAX_SAFE: u64 = (1 << 53) - 1;
/// The most bytes and strips of a chunk of joined strips (spec/zarrify/tiff.md §4.5
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>).
const MAX_JOINED_BYTES: u128 = 1 << 20;
const MAX_JOINED_STRIPS: u128 = 1 << 10;
/// The most decoded bytes of a shard of compressed strips (spec/zarrify/tiff.md §4.5
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>).
const MAX_SHARD_BYTES: u128 = 1 << 30;

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
    /// The file names siblings (`Ask`, of the file's name when the parser knows it).
    Beside(Ask),
}

/// A parser's part in an image across files (spec/zarrify/tiff.md §7.4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#74-several-files>).
#[derive(Default, PartialEq)]
enum Role {
    /// A file run, which does not know the file's name; it stops at a sibling.
    #[default]
    Alone,
    /// The named file of a store run, at this key: it says which files there are, and which plane
    /// each holds.
    Named(String),
    /// Another file of a store run, whose planes the named file's parser gives it.
    Sibling,
}

/// What the file names: the siblings to probe, the `UUID` each `TiffData` gives its file (OME-XML),
/// and the numbered family it starts (Micro-Manager).
#[derive(Default)]
struct Ask {
    wanted: Vec<String>,
    uuids: HashMap<String, String>,
    family: Option<Family>,
}

/// A Micro-Manager stack's later files, whose count no file records: `<stem>_<next>.ome.tif`,
/// until the planes are all found or one is absent.
#[derive(Clone)]
struct Family {
    stem: String,
    next: u32,
    expected: u64,
    ended: bool,
}

/// An entry of a Micro-Manager file's index map.
#[derive(Clone, Copy)]
struct MmEntry {
    channel: u32,
    slice: u32,
    frame: u32,
    position: u32,
    ifd: u32,
}

/// A Micro-Manager stack file's own header (spec/zarrify/tiff.md §7.5
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#75-micro-manager-stacks>).
#[derive(Clone)]
struct Mm {
    summary: J,
    entries: Vec<MmEntry>,
}

impl Mm {
    /// The planes its summary says the stack has.
    fn expected(&self) -> u64 {
        let n = |k: &str| self.summary[k].as_u64().unwrap_or(1).max(1);
        n("Frames").saturating_mul(n("Slices")).saturating_mul(n("Channels"))
    }
}

/// What the named file's parser needs of a sibling that waits for its planes.
struct SiblingFacts {
    /// its main chain's IFD offsets
    offsets: Vec<u64>,
    mm: Option<Mm>,
}

/// The size and format every plane of an image across files has.
#[derive(Clone)]
pub struct Expected {
    dims: (u64, u64, bool, u64, u64),
    format: Fmt,
}

/// The read ahead of a Micro-Manager file's IFDs, whose offsets its index map holds.
#[derive(Clone, Copy, PartialEq)]
enum Prefetch {
    Start,
    Header,
    Map(u64),
    Entries(u64, u64),
    Done,
}

/// The bytes read of each IFD an index map names.
const PREFETCH_IFD: u64 = 1024;

/// A parser's state as part of an image across files.
#[derive(Default)]
struct SetState {
    role: Role,
    ask: Ask,
    siblings: Option<Vec<SiblingFacts>>,
    /// a sibling's planes, as main-chain indices, and what each must be
    given: Option<(Vec<usize>, Expected)>,
    /// a sibling's own `UUID`: its OME-XML's, or its Micro-Manager summary's
    identity: Option<String>,
    mm: Option<Mm>,
    /// each IFD's tiles and strips elements
    tiles: HashMap<usize, (Option<u32>, Option<u32>)>,
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
    /// its part in an image across files
    set: SetState,
    /// how far the read of a Micro-Manager file's IFDs ahead of the chain has gone (`mm_prefetch`)
    prefetch: Prefetch,
}

/// The bytes of a row of `w` pixels of `samples` samples of `bits` bits each, which a row of packed
/// samples rounds up to a whole byte (spec/zarrify/tiff.md §4.5
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>).
fn row_size(w: u64, bits: u64, samples: u64) -> u128 {
    (w as u128 * bits as u128 * samples as u128).div_ceil(8)
}

/// An EER compression's skip, horizontal and vertical bits (spec/zarrify/tiff.md §4.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#43-data-types-and-codecs>): 8, 2
/// and 2 for 65000; 7, 2 and 2 for 65001; and tags 65007 to 65009 for 65002, `tag(t, default)`
/// giving each, 7, 2 and 2 without its tag.
pub fn eer_bits(compression: u64, tag: impl Fn(u16, u64) -> u64) -> (u64, u64, u64) {
    match compression {
        65000 => (8, 2, 2),
        65001 => (7, 2, 2),
        _ => (tag(65007, 7), tag(65008, 2), tag(65009, 2)),
    }
}

/// Whether a compression is EER's (spec/zarrify/tiff.md §4.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#43-data-types-and-codecs>).
fn is_eer(compression: u64) -> bool {
    matches!(compression, 65000..=65002)
}

struct Level {
    w: u64,
    h: u64,
    /// the chunk's width and height: the tile's, or, for a level in strips, the image's width
    /// and the rows per strip
    tw: u64,
    th: u64,
    ifds: Vec<usize>,
    /// the level is in strips
    strips: bool,
    /// the strips of a chunk of the level, more than 1 when its uncompressed strips are joined
    /// (spec/zarrify/tiff.md §4.5
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>)
    join: u64,
    /// the strips of a shard of the level, more than 1 when its compressed strips are the inner
    /// chunks of shards (spec/zarrify/tiff.md §4.5
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>)
    shard: u64,
    /// the level's last strip is short and cannot be padded, so that its chunks are those of a
    /// `rectilinear` grid (spec/zarrify/tiff.md §4.5
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>)
    rectilinear: bool,
    /// how a short last strip is padded to the chunk (spec/zarrify/tiff.md §4.5
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>):
    /// `{"zeros": [{c, k, zeros}, ...]}` (uncompressed) or `{"lzw": [{c, k, prefix, tail}, ...]}`,
    /// each strip keyed by its IFD's index in the level and its own; null when none
    pad: J,
}

impl Level {
    /// The rows of a chunk: a tile's or a strip's, or those of the strips it joins, at most the
    /// image's length.
    fn chunk_rows(&self) -> u64 {
        if self.join > 1 { (self.th * self.join).min(self.h) } else { self.th }
    }
}

/// Makes the LZW, Deflate and zstd strips of each level the inner chunks of shards of at most
/// 2^10 strips and 2^30 decoded bytes, as even as whole strips allow (spec/zarrify/tiff.md §4.5
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>): each strip stays a
/// stream of its own, which the sharding codec finds through the shard's index. A level stays
/// one strip a chunk when it has one strip, when two do not fit, or when its last strip is short
/// and cannot be padded: the sharding codec refuses the `rectilinear` grid that then needs.
fn shard_strips(levels: &mut [Level], bits: u64, spp: u64, planar: u64, compression: u64) {
    if !matches!(compression, 5 | 8 | 32946 | 50000) {
        return;
    }
    let contig = spp > 1 && planar == 1;
    for lv in levels.iter_mut().filter(|lv| lv.strips) {
        let row = row_size(lv.w, bits, if contig { spp } else { 1 });
        let strips = lv.h.div_ceil(lv.th);
        let most = (MAX_SHARD_BYTES / (lv.th as u128 * row)).min(MAX_JOINED_STRIPS) as u64;
        if strips >= 2 && most >= 2 && (compression == 5 || lv.h % lv.th == 0) {
            lv.shard = strips.div_ceil(strips.div_ceil(most));
        }
    }
}

struct Layout {
    facts: J,
    /// (IFD, JPEG prefix) of every IFD a level uses
    prefixes: HashMap<usize, Vec<u8>>,
    levels: Vec<Level>,
    planes: Option<Vec<usize>>,
    /// each plane's (file, main-chain index), the named file being file 0, when a plane may lie in
    /// another file or be absent (spec/zarrify/tiff.md §7.4
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#74-several-files>)
    plan: Option<Vec<Option<(usize, usize)>>>,
    expected: Option<Expected>,
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
            set: SetState::default(),
            prefetch: Prefetch::Start,
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
                    if let Some(s) = self.mm_prefetch()? {
                        return Ok(s);
                    }
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
                    let waiting = self.set.role == Role::Sibling && self.set.given.is_none();
                    let derived = if waiting {
                        self.sibling_head().map(|()| None)
                    } else if self.set.role == Role::Sibling {
                        self.derive_sibling().map(Some)
                    } else if self.ndpi {
                        self.derive_ndpi().map(Some)
                    } else if self.qptiff {
                        self.derive_qptiff().map(Some)
                    } else {
                        self.derive().map(Some)
                    };
                    match derived {
                        Ok(None) => return Ok(Step::Beside),
                        Err(Stop::Beside(ask)) => {
                            self.set.ask = ask;
                            return Ok(Step::Beside);
                        }
                        Ok(Some(l)) => {
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

    /// The values the layout read whole (past the eager budget, such as a long
    /// ImageDescription) become their elements' values, as smaller ones are.
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

    /// The tags of an IFD's tile and strip tables that `emit_ifd_data` reads: TileOffsets and
    /// TileByteCounts (with an NDPI's high tables), and StripOffsets and StripByteCounts, each
    /// pair when the IFD has both of unsigned types.
    fn data_tables(&self, f: &Ifd) -> Vec<u16> {
        let mut tags = Vec::new();
        for (o, c) in [(324u16, 325u16), (273, 279)] {
            if [o, c].iter().all(|&t| f.field(t).is_some_and(|e| unsigned(e.typ))) {
                tags.extend([o, c]);
                if self.ndpi && o == 324 {
                    tags.extend([65324, 65325]);
                }
            }
        }
        tags
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
    /// the profile reads whatever their size, every IFD's tile and strip tables (the
    /// source metadata lists the tiles of every IFD with both tables, spec/zarrify/tiff.md §5.1
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#51-elements>), and
    /// others up to 64 KiB (within the eager budget). None when there are none to read, or
    /// once they are read.
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
            let tables = self.data_tables(f);
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
                let required = (e.tag == 330 && f.tree && f.depth < 4 && !e.dup) || (!e.dup && tables.contains(&e.tag));
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
    /// the root's `UUID`, and each `TiffData` that has a `UUID`'s text beside a `FileName`
    uuid: Option<String>,
    uuid_texts: HashMap<usize, String>,
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
    let mut named_text = false; // that UUID has a FileName, so its text is only its identity
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
            let text = xml::decode_refs(&s).trim_matches([' ', '\t', '\r', '\n']).to_string();
            ome.uuid_texts.insert(k, text.clone());
            if !std::mem::take(&mut named_text) {
                ome.tiff_data[k].1 = Some(text);
            }
        }
        if !t.is_end && t.name == "OME" {
            is_ome = true;
            ome.uuid = get(&decoded_attrs(&t, x), "UUID").map(str::to_string);
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
                            named_text = !t.self_closing;
                        }
                        if t.self_closing {
                            if ome.tiff_data[k].1.is_none() {
                                ome.tiff_data[k].1 = Some(String::new());
                            }
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
        let text = xml::decode_refs(&s).trim_matches([' ', '\t', '\r', '\n']).to_string();
        ome.uuid_texts.insert(k, text.clone());
        if !named_text {
            ome.tiff_data[k].1 = Some(text);
        }
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

/// A Micro-Manager stack's planes across its files.
struct MmStack {
    /// frames, channels, slices
    sizes: (u64, u64, u64),
    /// (frame, channel, slice, file, main-chain index)
    entries: Vec<(u64, u64, u64, usize, usize)>,
}

impl MmStack {
    /// Each plane's (file, main-chain index), in the plane order of (t, c, z).
    fn plan(&self, plane_c: u64, size_z: u64, count: usize) -> Vec<Option<(usize, usize)>> {
        let mut plan = vec![None; count];
        for &(t, c, z, f, i) in &self.entries {
            let at = ((t * plane_c + c) * size_z + z) as usize;
            if let Some(p) = plan.get_mut(at) {
                *p = Some((f, i));
            }
        }
        plan
    }
}

// ---- an image across files (spec/zarrify/tiff.md §7.4
// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#74-several-files>)
impl Tiff {
    /// The parser of the named file of a store run, at `key`.
    pub fn named(size: u64, key: &str) -> Tiff {
        let mut t = Tiff::new(size);
        t.set.role = Role::Named(key.to_string());
        t
    }

    /// The parser of another file of a store run.
    pub fn sibling(size: u64) -> Tiff {
        let mut t = Tiff::new(size);
        t.set.role = Role::Sibling;
        t
    }

    pub fn is_done(&self) -> bool {
        self.phase == Phase::Done
    }

    /// The keys to probe, which the last `Step::Beside` asked for.
    pub fn wanted(&mut self) -> Vec<String> {
        std::mem::take(&mut self.set.ask.wanted)
    }

    /// The main chain's length of file `f`, the named file being 0.
    fn chain_of(&self, f: usize) -> usize {
        match (f, &self.set.siblings) {
            (0, _) => self.chain.len(),
            (f, Some(s)) => s.get(f - 1).map_or(0, |x| x.offsets.len()),
            _ => 0,
        }
    }

    /// A range the layout reads, once.
    fn at(&self, o: u64, n: u64) -> Got<Vec<u8>> {
        self.heads.get(&(o, n)).cloned().ok_or(Stop::Need(vec![(o, n)]))
    }

    /// The file's Micro-Manager header, when it has one (spec/zarrify/tiff.md §7.5
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#75-micro-manager-stacks>):
    /// the index map's and the summary's headers at their places, a summary that is a JSON object.
    /// Anything else is a TIFF like another.
    fn micromanager(&self) -> Got<Option<Mm>> {
        if !self.le || self.big || !self.within(0, 40) {
            return Ok(None);
        }
        let word = |b: &[u8], i: usize| u32::at(b, i, true);
        let head = self.at(8, 32)?;
        if word(&head, 0) != 54773648 || word(&head, 24) != 2355492 {
            return Ok(None);
        }
        let (index, length) = (word(&head, 4) as u64, word(&head, 28) as u64);
        if length > 1 << 24 || !self.within(40, length) || !self.within(index, 8) {
            return Ok(None);
        }
        let summary: J = match serde_json::from_slice(&self.at(40, length)?) {
            Ok(v @ J::Object(_)) => v,
            _ => return Ok(None),
        };
        let h = self.at(index, 8)?;
        let n = word(&h, 4) as u64;
        if word(&h, 0) != 3453623 || n == 0 || !self.within(index + 8, 20 * n) {
            return Ok(None);
        }
        let b = self.at(index + 8, 20 * n)?;
        let entries = b.chunks_exact(20).map(|e| MmEntry {
            channel: word(e, 0),
            slice: word(e, 4),
            frame: word(e, 8),
            position: word(e, 12),
            ifd: word(e, 16),
        }).collect();
        Ok(Some(Mm { summary, entries }))
    }

    /// The next read of a Micro-Manager file's IFDs ahead of the main chain, so that a stack of
    /// many planes is not an IFD a request: its index map names them all, and the chain's walk
    /// then finds them read. A file without the header costs only the read of its bytes 8 to 39.
    fn mm_prefetch(&mut self) -> Res<Option<Step>> {
        loop {
            match self.prefetch {
                Prefetch::Done => return Ok(None),
                Prefetch::Start => {
                    if !self.le || self.big || self.ndpi || self.qptiff || !self.within(0, 40) || !self.chain.is_empty() {
                        self.prefetch = Prefetch::Done;
                        continue;
                    }
                    self.prefetch = Prefetch::Header;
                    return Ok(Some(self.ask(vec![(8, 32)])));
                }
                Prefetch::Header => {
                    let h = self.fed.get(8, 32).ok_or("internal: no header")?.to_vec();
                    self.fed.clear();
                    let index = u32::at(&h, 4, true) as u64;
                    if u32::at(&h, 0, true) != 54773648 || u32::at(&h, 24, true) != 2355492 || !self.within(index, 8) {
                        self.prefetch = Prefetch::Done;
                        continue;
                    }
                    self.prefetch = Prefetch::Map(index);
                    return Ok(Some(self.ask(vec![(index, 8)])));
                }
                Prefetch::Map(index) => {
                    let h = self.fed.get(index, 8).ok_or("internal: no index map's header")?.to_vec();
                    self.fed.clear();
                    let n = u32::at(&h, 4, true) as u64;
                    if u32::at(&h, 0, true) != 3453623 || n == 0 || n > 100000 || !self.within(index + 8, 20 * n) {
                        self.prefetch = Prefetch::Done;
                        continue;
                    }
                    self.prefetch = Prefetch::Entries(index + 8, n);
                    return Ok(Some(self.ask(vec![(index + 8, 20 * n)])));
                }
                Prefetch::Entries(at, n) => {
                    let b = self.fed.get(at, 20 * n).ok_or("internal: no index map")?.to_vec();
                    self.fed.clear();
                    self.prefetch = Prefetch::Done;
                    let ranges: Vec<(u64, u64)> = b.chunks_exact(20).map(|e| u32::at(e, 16, true) as u64)
                        .filter(|&o| o < self.size).map(|o| (o, PREFETCH_IFD.min(self.size - o))).collect();
                    if ranges.is_empty() {
                        continue;
                    }
                    return Ok(Some(self.ask(ranges)));
                }
            }
        }
    }

    /// The stack's planes, once every file is known; else what to probe next: a file run stops
    /// at a stack its own index map does not cover, and a store run asks for the family's first.
    fn mm_stack(&self, m: &Mm) -> Got<MmStack> {
        let covered = m.entries.len() as u64 >= m.expected();
        if let Role::Named(key) = &self.set.role {
            let part = key.strip_suffix(".ome.tif").and_then(|s| s.rsplit_once('_')).filter(|(s, d)| {
                !d.is_empty() && d.bytes().all(|c| c.is_ascii_digit()) && s.contains("_MMStack")
            });
            let first = part.map(|(s, _)| format!("{s}.ome.tif"));
            self.rules.check("micromanager_part", rules::env([("key", text(key)), ("later", flag(first.is_some())), ("first", text(first.as_deref().unwrap_or("")))]))?;
        }
        if !covered && self.set.siblings.is_none() {
            let family = match &self.set.role {
                Role::Named(key) => key.strip_suffix(".ome.tif").map(|s| Family { stem: s.to_string(), next: 1, expected: m.expected(), ended: false }),
                _ => None,
            };
            let wanted = family.iter().map(|f| format!("{}_{}.ome.tif", f.stem, f.next)).collect();
            return Err(Stop::Beside(Ask { wanted, family: family.map(|f| Family { next: 2, ..f }), uuids: HashMap::new() }));
        }
        let mut files: Vec<(usize, &Mm, Vec<u64>)> = vec![(0, m, self.chain.iter().map(|&k| self.ifds[k].offset).collect())];
        for (k, s) in self.set.siblings.iter().flatten().enumerate() {
            if let Some(o) = &s.mm {
                files.push((k + 1, o, s.offsets.clone()));
            }
        }
        let position = m.entries[0].position;
        let mut sizes = (1, 1, 1);
        let mut entries = Vec::new();
        for (f, file, offsets) in files {
            let at: HashMap<u64, usize> = offsets.iter().enumerate().map(|(i, &o)| (o, i)).collect();
            for e in &file.entries {
                self.rules.check("micromanager", rules::env([("one_position", flag(e.position == position)), ("found", flag(at.contains_key(&(e.ifd as u64))))]))?;
                let (t, c, z) = (e.frame as u64, e.channel as u64, e.slice as u64);
                sizes = (sizes.0.max(t + 1), sizes.1.max(c + 1), sizes.2.max(z + 1));
                entries.push((t, c, z, f, at[&(e.ifd as u64)]));
            }
        }
        Ok(MmStack { sizes, entries })
    }

    /// Each TiffData's file (0: this one; k: `wanted`'s k-th) of a document that names several,
    /// once a store run knows them; a file run stops here.
    fn ome_files(&self, o: &Ome) -> Got<Vec<usize>> {
        let Role::Named(own) = &self.set.role else {
            return Err(Stop::Beside(Ask::default()));
        };
        let mut wanted: Vec<String> = Vec::new();
        let mut uuids = HashMap::new();
        let mut files = Vec::with_capacity(o.tiff_data.len());
        for (k, (attrs_, id)) in o.tiff_data.iter().enumerate() {
            let _ = attrs_;
            let file = match id {
                None => 0,
                Some(id) => {
                    let text = o.uuid_texts.get(&k).cloned().unwrap_or_default();
                    if *id == text {
                        // no FileName: only this file can be told by its UUID alone
                        let own_uuid = o.uuid.as_deref() == Some(id.as_str());
                        self.rules.check("set_uuid", rules::env([("uuid", self::text(id)), ("ok", flag(own_uuid))]))?;
                        0
                    } else {
                        let key = crate::beside::written_key(id.as_bytes(), "", Default::default());
                        self.rules.check("set_name", rules::env([("name", self::text(id)), ("ok", flag(key.is_some()))]))?;
                        let key = key.unwrap_or_default();
                        if key == *own {
                            0
                        } else {
                            uuids.insert(key.clone(), text);
                            match wanted.iter().position(|w| *w == key) {
                                Some(i) => i + 1,
                                None => {
                                    wanted.push(key);
                                    wanted.len()
                                }
                            }
                        }
                    }
                }
            };
            files.push(file);
        }
        if self.set.siblings.is_none() {
            return Err(Stop::Beside(Ask { wanted, uuids, family: None }));
        }
        Ok(files)
    }

    /// What a sibling reads before it waits for its planes: who it says it is.
    fn sibling_head(&mut self) -> Got<()> {
        let Some(&i0) = self.chain.first() else { return Ok(()) };
        let m0 = self.load(&[i0], false, true)?.pop().unwrap();
        let mut ome = None;
        if let Some(Loaded::Bytes(d)) = m0.get(&270) {
            if let Ok(t) = std::str::from_utf8(d.split(|&c| c == 0).next().unwrap_or(&[])) {
                ome = read_ome(t, self.rules)?;
            }
        }
        let mm = if ome.is_none() { self.micromanager()? } else { None };
        self.set.identity = match (&ome, &mm) {
            (Some(o), _) => o.uuid.clone(),
            (_, Some(m)) => m.summary["UUID"].as_str().map(str::to_string),
            _ => None,
        };
        self.set.mm = mm;
        Ok(())
    }

    /// The named file's check of a sibling that waits for its planes (spec/zarrify.md §1.9
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#19-sources-beside-the-input>,
    /// **Identity**).
    pub fn check_sibling(&self, key: &str, s: &Tiff) -> Res<()> {
        let same = if self.set.ask.family.is_some() {
            s.set.mm.is_some() && s.set.identity.is_some() && s.set.identity == self.mm_uuid()
        } else {
            match (self.set.ask.uuids.get(key).filter(|u| !u.is_empty()), &s.set.identity) {
                (Some(want), Some(have)) => want == have,
                _ => true,
            }
        };
        self.rules.check("set_identity", rules::env([("key", text(key)), ("same", flag(same))]))
    }

    fn mm_uuid(&self) -> Option<String> {
        self.micromanager().ok().flatten().and_then(|m| m.summary["UUID"].as_str().map(str::to_string))
    }

    /// The next key of the family, while the planes found fall short of the summary's.
    pub fn next_of_family(&mut self, siblings: &[&Tiff]) -> Option<String> {
        let own = self.micromanager().ok().flatten()?.entries.len() as u64;
        let found: u64 = siblings.iter().filter_map(|s| s.set.mm.as_ref()).map(|m| m.entries.len() as u64).sum();
        let f = self.set.ask.family.as_mut().filter(|f| !f.ended && own + found < f.expected)?;
        let key = format!("{}_{}.ome.tif", f.stem, f.next);
        f.next += 1;
        Some(key)
    }

    /// The answer that `key` does not exist: the family ends there, and a file the document names
    /// rejects the input.
    pub fn sibling_absent(&mut self, key: &str) -> Res<()> {
        if let Some(f) = self.set.ask.family.as_mut() {
            f.ended = true;
            return Ok(());
        }
        self.rules.check("set_file", rules::env([("key", text(key)), ("found", flag(false))]))
    }

    /// The siblings are all known, each waiting for its planes.
    pub fn give_siblings(&mut self, siblings: &[&Tiff]) {
        self.set.siblings = Some(siblings.iter().map(|s| SiblingFacts {
            offsets: s.chain.iter().map(|&k| s.ifds[k].offset).collect(),
            mm: s.set.mm.clone(),
        }).collect());
    }

    /// Each sibling's planes, as main-chain indices.
    pub fn sibling_plan(&self) -> Vec<Vec<usize>> {
        let n = self.set.siblings.as_ref().map_or(0, Vec::len);
        let mut out = vec![Vec::new(); n];
        for &(f, i) in self.layout.iter().flat_map(|l| l.plan.iter().flatten().flatten()) {
            if f > 0 {
                out[f - 1].push(i);
            }
        }
        out
    }

    pub fn expected(&self) -> Option<Expected> {
        self.layout.as_ref().and_then(|l| l.expected.clone())
    }

    /// A sibling's planes, and what they must be.
    pub fn give_planes(&mut self, planes: Vec<usize>, expected: Expected) {
        self.set.given = Some((planes, expected));
    }

    /// Each main-chain IFD's tiles or strips element, as the image's planes are.
    pub fn tile_elements(&self) -> Vec<Option<u32>> {
        let strips = match (&self.set.given, self.layout.as_ref().and_then(|l| l.expected.as_ref())) {
            (Some((_, e)), _) | (None, Some(e)) => !e.dims.2,
            _ => false,
        };
        self.chain.iter().map(|k| self.set.tiles.get(k).and_then(|&(t, s)| if strips { s } else { t })).collect()
    }

    /// A sibling's layout: each of its planes is of the image's size and format, and its tiles
    /// or strips lie in it.
    fn derive_sibling(&self) -> Got<Layout> {
        let (planes, expected) = self.set.given.as_ref().ok_or_else(|| Stop::Reject("internal: a sibling without planes".into()))?;
        let ks: Vec<usize> = planes.iter().map(|&i| self.chain[i]).collect();
        let mut prefixes = HashMap::new();
        if !ks.is_empty() {
            let mut vals = HashMap::new();
            let lv = self.level(&ks, &mut vals)?;
            let f = self.fmt(ks[0], &vals[&ks[0]])?;
            let same = (lv.w, lv.h, !lv.strips, lv.tw, lv.th) == expected.dims && f == expected.format;
            self.rules.check("set_format", rules::env([("same", flag(same))]))?;
            self.check_tiles(&lv, &vals, f, &mut prefixes)?;
        }
        Ok(Layout { facts: J::Null, prefixes, levels: Vec::new(), planes: None, plan: None, expected: None })
    }

    /// The tiles, or strips, of each plane of a level (spec/zarrify/tiff.md §4
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#4-the-image>).
    fn check_tiles(&self, lv: &Level, vals: &HashMap<usize, Vals>, f: Fmt, prefixes: &mut HashMap<usize, Vec<u8>>) -> Got<()> {
        let (_, spp, _, planar, compression, _, photometric) = f;
        let contig = spp > 1 && planar == 1;
        let samples = if spp > 1 && !contig { spp } else { 1 };
        let jpeg = compression == 7;
        let across = if lv.strips { 1 } else { lv.w.div_ceil(lv.tw) };
        let down = lv.h.div_ceil(lv.th);
        let per = across as u128 * down as u128;
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
            prefixes.insert(k, if jpeg { prefix } else { Vec::new() });
        }
        Ok(())
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
        // TIFF 6.0's default, which a bilevel image's writer may rely on
        let bits = match m.get(&258) {
            Some(Loaded::Ints(v)) => v.clone(),
            _ => vec![1],
        };
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

    /// The main-chain indexes of an EER file's frames: the IFDs of IFD 0's size and format, as
    /// `pages` tests them, in chain order. Each frame's tags are read, in one batch.
    fn eer_frames(&self, i0: usize, f0: &Fmt) -> Got<Vec<usize>> {
        let loaded = self.load(&self.chain, false, false)?;
        let size = |k: usize, m: &Vals| (first_int(m, 256), first_int(m, 257), self.tiled(k));
        let at0 = self.chain.iter().position(|&k| k == i0).unwrap_or(0);
        let want = size(i0, &loaded[at0]);
        let mut frames = Vec::new();
        for (i, (&k, m)) in self.chain.iter().zip(&loaded).enumerate() {
            let format = match self.fmt(k, m) {
                Ok(f) => Some(f),
                Err(Stop::Reject(_)) => None,
                Err(e) => return Err(e),
            };
            if size(k, m) == want && format.as_ref() == Some(f0) {
                frames.push(i);
            }
        }
        Ok(frames)
    }

    /// An EER image's skip, horizontal and vertical bits (spec/zarrify/tiff.md §4.3
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#43-data-types-and-codecs>): from
    /// its compression, but for 65002's, from tags 65007 to 65009, each 7, 2 and 2 without its tag.
    /// A tag of another field type counts as absent: these are outside the table, and read only here.
    fn eer_bits(&self, k: usize, compression: u64) -> (u64, u64, u64) {
        eer_bits(compression, |t, default| {
            let value = self.ifds[k].field(t).filter(|e| unsigned(e.typ)).and_then(|e| self.ints(e, true));
            value.and_then(|v| v.first().copied()).unwrap_or(default)
        })
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
        // a plane in strips (spec/zarrify/tiff.md §4.5
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>): its strips and RowsPerStrip
        for &k in ks {
            if !self.tiled(k) {
                self.check_strips(k, &vals[&k])?;
            }
        }
        let k0 = ks[0];
        let m0 = &vals[&k0];
        // the size, and the chunk's: the tile's, or the image's width and the rows per strip
        let dims = |k: usize, m: &Vals| -> Got<(u64, u64, bool, u64, u64)> {
            let (w, h) = (self.req(k, m, 256, None)?, self.req(k, m, 257, None)?);
            Ok(if self.tiled(k) {
                (w, h, true, self.req(k, m, 322, None)?, self.req(k, m, 323, None)?)
            } else {
                (w, h, false, w, self.req(k, m, 278, Some(u32::MAX as u64))?.min(h))
            })
        };
        let d0 = dims(k0, m0)?;
        let f0 = self.fmt(k0, m0)?;
        for &k in ks {
            let same = dims(k, &vals[&k])? == d0 && self.fmt(k, &vals[&k])? == f0;
            self.rules.check("level_ifd", rules::env([("offset", num(self.ifds[k].offset as f64)), ("same", flag(same))]))?;
        }
        Ok(Level { w: d0.0, h: d0.1, tw: d0.3, th: d0.4, ifds: ks.to_vec(), strips: !d0.2, join: 1, shard: 1, rectilinear: false, pad: J::Null })
    }

    /// The `strips` role of an IFD that is not tiled: StripOffsets, StripByteCounts and a
    /// RowsPerStrip of at least 1.
    fn check_strips(&self, k: usize, m: &Vals) -> Got<()> {
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
        Ok(())
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
        self.rules.check(
            "predictor",
            rules::env([
                ("compression", num(compression as f64)),
                ("predictor", num(predictor as f64)),
                ("sample_format", num(sample_format as f64)),
                ("bits", num(bits as f64)),
            ]),
        )?;
        // the planes (spec/zarrify/tiff.md §3
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#3-ome-xml>, §4.1
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#41-planes>), in (t, c,
        // z) order, as main-chain IFD indices
        let empty = vec![];
        let px = ome.as_ref().map(|o| &o.pixels).unwrap_or(&empty);
        // a Micro-Manager stack (spec/zarrify/tiff.md §7.5
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#75-micro-manager-stacks>):
        // its index maps give the sizes and the planes
        let mm = if ome.is_none() { self.micromanager()? } else { None };
        let mm_stack = match &mm {
            Some(m) => Some(self.mm_stack(m)?),
            None => None,
        };
        let size_z = match &mm_stack {
            Some(s) => s.sizes.2,
            None => int_attr(self.rules, px, "SizeZ", 1, 1)?,
        };
        // an EER file's frames, without OME-XML: the main-chain IFDs of IFD 0's size and format
        // (spec/zarrify/tiff.md §4.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#41-planes>)
        let frames = if is_eer(compression) && ome.is_none() && mm_stack.is_none() { Some(self.eer_frames(i0, &f0)?) } else { None };
        let size_t = match (&mm_stack, &frames) {
            (Some(s), _) => s.sizes.0,
            (None, Some(f)) => f.len() as u64,
            _ => int_attr(self.rules, px, "SizeT", 1, 1)?,
        };
        let mut size_c = match &mm_stack {
            Some(s) => s.sizes.1.max(if spp > 1 { spp } else { 1 }),
            None => int_attr(self.rules, px, "SizeC", spp, 1)?,
        };
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
        self.rules.check("ome_planes", rules::env([("planes", num(count as f64))]))?;
        let count = count as usize;
        let mut plane_ifd: Vec<i128> = vec![-1; count];
        let plane = |t: u64, c: u64, z: u64| ((t * plane_c + c) * size_z + z) as usize;
        // the files the OME-XML names, by each TiffData (spec/zarrify/tiff.md §7.4
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#74-several-files>)
        let tiff_data_file = match &ome {
            Some(o) if files.unwrap() > 1 => Some(self.ome_files(o)?),
            _ => None,
        };
        let mut plane_file: Vec<usize> = vec![0; if tiff_data_file.is_some() { count } else { 0 }];
        match &ome {
            None => match &frames {
                Some(f) => plane_ifd.iter_mut().zip(f).for_each(|(p, &i)| *p = i as i128),
                None => plane_ifd[0] = 0,
            },
            Some(o) => {
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
                for (entry, (a, _)) in entries.iter().enumerate() {
                    let file = tiff_data_file.as_ref().map_or(0, |f| f.get(entry).copied().unwrap_or(0));
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
                        let at = plane(pos[2], pos[1], pos[0]);
                        plane_ifd[at] = ifd;
                        if let Some(f) = plane_file.get_mut(at) {
                            *f = file;
                        }
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
        // each plane's (file, main-chain index), when a plane may lie in another file or be absent
        let plan: Option<Vec<Option<(usize, usize)>>> = match (&mm_stack, &tiff_data_file) {
            (Some(s), _) => Some(s.plan(plane_c, size_z, count)),
            (None, Some(_)) => Some(plane_ifd.iter().zip(&plane_file).map(|(&i, &f)| (i >= 0).then_some((f, i as usize))).collect()),
            _ => None,
        };
        let mapped = match &plan {
            Some(p) => mm_stack.is_some() || p.iter().all(|x| x.is_some_and(|(f, i)| i < self.chain_of(f))),
            None => plane_ifd.iter().all(|&i| i >= 0 && (i as usize) < self.chain.len()),
        };
        self.rules.check("plane_map", rules::env([("mapped", flag(mapped))]))?;
        let planes: Vec<usize> = match &plan {
            // the planes this file holds: the other files check theirs (`derive_sibling`)
            Some(p) => {
                let own: Vec<usize> = p.iter().flatten().filter(|x| x.0 == 0).map(|x| self.chain[x.1]).collect();
                if own.is_empty() { vec![i0] } else { own }
            }
            None => plane_ifd.iter().map(|&i| self.chain[i as usize]).collect(),
        };
        // the levels (spec/zarrify/tiff.md §4.2
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#42-pyramid-levels>)
        let mut levels: Vec<Level> = Vec::new();
        let subs0 = self.ifds[i0].subs.clone();
        self.rules.check("set_layout", rules::env([("set", flag(plan.is_some())), ("flat", flag(subs0.is_empty())), ("whole", flag(true))]))?;
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
            if ome.is_none() && plan.is_none() {
                for &k in &self.chain[1..] {
                    let (pw, ph) = (levels.last().unwrap().w, levels.last().unwrap().h);
                    if !self.tiled(k) {
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
                // a stack of pages in strips, as ImageJ and tifffile write them without OME-XML
                // (spec/zarrify/tiff.md §4.2
                // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#42-pyramid-levels>):
                // a later page of IFD 0's size and format is a plane the layout cannot map
                if levels[0].strips {
                    let used: HashSet<usize> = levels.iter().flat_map(|l| l.ifds.iter().copied()).collect();
                    let (w0, h0) = (levels[0].w, levels[0].h);
                    // a page's width and length are in line: only a page of IFD 0's size has its values
                    // loaded, one at a time, so that a stack is rejected at its first such page; one
                    // whose values do not load (a malformed tag) is not a page of IFD 0's format
                    let one = |k: usize, t: u16| {
                        self.ifds[k].field(t).filter(|e| unsigned(e.typ)).and_then(|e| self.ints(e, true)).and_then(|v| v.first().copied())
                    };
                    for &k in &self.chain[1..] {
                        if used.contains(&k) || (one(k, 256), one(k, 257)) != (Some(w0), Some(h0)) {
                            continue;
                        }
                        let m = match self.load(&[k], false, false) {
                            Ok(mut m) => m.pop().unwrap(),
                            Err(Stop::Reject(_)) => continue,
                            Err(e) => return Err(e),
                        };
                        let format = match self.fmt(k, &m) {
                            Ok(f) => Some(f),
                            Err(Stop::Reject(_)) => None, // not of IFD 0's format, whatever it is
                            Err(e) => return Err(e),
                        };
                        let page = format == Some(f0);
                        self.rules.check("pages", rules::env([("offset", num(self.ifds[k].offset as f64)), ("page", flag(page))]))?;
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
        // an EER image's codec reads the tag's bits, and decodes one shape for a level's chunks
        let eer = if is_eer(compression) { Some(self.eer_bits(i0, compression)) } else { None };
        if let Some(bits) = eer {
            for lv in &levels {
                let short = lv.strips && lv.h % lv.th != 0;
                for &k in &lv.ifds {
                    let same = self.eer_bits(k, compression) == bits;
                    self.rules.check("eer", rules::env([("offset", num(self.ifds[k].offset as f64)), ("same", flag(same)), ("short", flag(short))]))?;
                }
            }
        }
        // the tiles, or strips, of every plane of every level
        let mut prefixes = HashMap::new();
        for lv in &levels {
            self.check_tiles(lv, &vals, f0, &mut prefixes)?;
        }
        // uncompressed strips joined into larger chunks, then each level's short last chunk padded,
        // but for an image whose planes may lie in several files, of one strip a chunk
        // (spec/zarrify/tiff.md §7.4
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#74-several-files>)
        if plan.is_some() {
            let lv = &levels[0];
            let whole = !lv.strips || lv.h.is_multiple_of(lv.th);
            self.rules.check("set_layout", rules::env([("set", flag(true)), ("flat", flag(true)), ("whole", flag(whole))]))?;
        } else {
            self.join_strips(&mut levels, &vals, bits, spp, planar, compression)?;
            shard_strips(&mut levels, bits, spp, planar, compression);
        }
        // a level in strips whose last strip is short, padded to the chunk (spec/zarrify/tiff.md §4.5
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>): one that
        // cannot be padded has a `rectilinear` grid
        if plan.is_none() {
            let padded = self.pad_strips(&mut levels, &vals, bits, spp, planar, compression)?;
            for (lv, padded) in levels.iter_mut().zip(padded) {
                lv.rectilinear = !padded;
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
        // a Micro-Manager summary's sizes, in place of the resolution tags'
        if let Some(m) = &mm {
            let positive = |k: &str| m.summary[k].as_f64().map(f64::abs).filter(|v| *v > 0.0);
            if let Some(v) = positive("PixelSize_um") {
                for a in ['x', 'y'] {
                    sizes.insert(a, v);
                    units.insert(a, "micrometer".into());
                }
            }
            if let Some(v) = positive("z-step_um") {
                sizes.insert('z', v);
                units.insert('z', "micrometer".into());
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
            "predictor": predictor,
            "photometric": photometric,
            "eer": eer.map(|(skip, horz, vert)| json!({"skipbits": skip, "horzbits": horz, "vertbits": vert})),
            "sizes": {"t": size_t, "c": size_c, "z": size_z},
            "plane_c": plane_c,
            "ome": ome.as_ref().map(|o| json!({
                "name": o.name,
                "pixels": o.pixels.iter().map(|(k, v)| (k.clone(), J::String(v.clone()))).collect::<serde_json::Map<_, _>>(),
                "plane": o.plane.as_ref().map(|p| p.iter().map(|(k, v)| (k.clone(), J::String(v.clone()))).collect::<serde_json::Map<_, _>>()),
                "channels": o.channels.iter().map(|c| json!({"name": get(c, "Name"), "color": get(c, "Color")})).collect::<Vec<_>>(),
            })).or(mm.as_ref().map(|m| json!({
                "channels": m.summary["ChNames"].as_array().into_iter().flatten().take(MAX_OMERO as usize).map(|n| json!({"name": n})).collect::<Vec<_>>(),
            }))),
            "micromanager": mm.as_ref().map(|m| &m.summary),
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
        let expected = plan.as_ref().map(|_| Expected {
            dims: (levels[0].w, levels[0].h, !levels[0].strips, levels[0].tw, levels[0].th),
            format: f0,
        });
        let mut facts = facts;
        if plan.is_some() && matches!(self.set.role, Role::Named(_)) {
            facts["plan"] = json!(plan.as_ref().unwrap().iter().map(|p| p.map(|(f, i)| [f, i])).collect::<Vec<_>>());
        }
        let planes = if plan.is_some() { None } else { ome.as_ref().map(|_| planes.clone()) };
        Ok(Layout { facts, prefixes, levels, planes, plan, expected })
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
        self.set.tiles = tiles_of.clone();
        let lay = self.layout.as_mut().unwrap();
        if self.ndpi {
            // derive_ndpi wrote the levels; a tiled JPEG XR level's chunks are its IFD's tiles
            for lv in lay.facts["levels"].as_array_mut().into_iter().flatten() {
                for plane in lv["planes"].as_array_mut().into_iter().flatten() {
                    if let Some(k) = plane["tiles_ifd"].as_u64() {
                        plane["tiles"] = json!(tiles_of.get(&(k as usize)).and_then(|t| t.0));
                    }
                }
            }
            return Ok(());
        }
        let (plan, chain) = (lay.plan.as_ref(), &self.chain);
        let levels: Vec<J> = lay
            .levels
            .iter()
            .map(|lv| {
                // a level's chunks are its IFDs' tiles, or strips
                let of = |k: &usize| tiles_of.get(k).copied().and_then(|(t, s)| if lv.strips { s } else { t });
                let planes = match plan {
                    Some(p) => p.iter().map(|x| x.filter(|x| x.0 == 0).and_then(|x| of(&chain[x.1]))).collect::<Vec<_>>(),
                    None => lv.ifds.iter().map(of).collect::<Vec<_>>(),
                };
                json!({"w": lv.w, "h": lv.h, "tw": lv.tw, "th": lv.th, "strips": lv.strips, "join": lv.join, "shard": lv.shard, "rectilinear": lv.rectilinear, "pad": lv.pad,
                       "planes": planes})
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
                self.ints(e, true).map(|v| json!(v[0]))
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

    /// An image's focal offset in nanometers (spec/zarrify/ndpi.md §2
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#2-the-source>): the first
    /// value of tag 65424, when of its field types, within the file, and at most 2^53 - 1 from 0;
    /// the bytes not read yet are added to `need`. It never rejects.
    fn ndpi_focal(&self, k: usize, need: &mut Vec<(u64, u64)>) -> Option<i64> {
        let e = self.ifds[k].field(65424).filter(|e| matches!(e.typ, 3 | 4 | 8 | 9) && e.count > 0)?;
        let z = type_size(e.typ)?;
        if !(e.inline || self.within(e.off, z)) {
            return None;
        }
        let Some(b) = self.bytes_of(e, z) else {
            need.push((e.off, z));
            return None;
        };
        let v = match e.typ {
            8 => i16::le_at(&b, 0) as i64,
            9 => i32::le_at(&b, 0) as i64,
            _ => i64::try_from(*self.ints(e, true)?.first()?).ok()?,
        };
        (v.unsigned_abs() <= MAX_SAFE).then_some(v)
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
        // focal offsets are read only when a Magnification has more than one image
        // (spec/zarrify/ndpi.md §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#3-levels>)
        let mut seen = HashSet::new();
        let repeated = (0..self.chain.len())
            .filter(|&j| positive(j))
            .filter_map(|j| mags[j].get(&65421).and_then(|v| v.first()))
            .any(|m| !seen.insert(m.to_bits()));
        let mut first_level = true;
        for (j, &k) in self.chain.iter().enumerate() {
            if !positive(j) {
                continue;
            }
            let m = self.nload(k, None, &mut need, false)?;
            if first_level {
                // the first image's acquisition tags, when out of line and not read yet
                self.ndpi_acquisition(k, &mut need);
                first_level = false;
            }
            if repeated {
                self.ndpi_focal(k, &mut need);
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
        let mut levels: Vec<(usize, f64, u64, u64, u64, bool)> = Vec::new(); // the images: (IFD, magnification, w, h, compression, tiled)
        let mut last_size: Option<(u64, u64)> = None; // of the last new Magnification's first image
        let mut magnifications = HashSet::new();
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
            // an image of an earlier Magnification is a plane of that level, not a new level
            let new_magnification = magnifications.insert(mag.to_bits());
            let smaller = !new_magnification || last_size.is_none_or(|l| w < l.0 && h < l.1);
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
                    ("width", num(w as f64)),
                    ("height", num(h as f64)),
                ]),
            )?;
            if new_magnification {
                last_size = Some((w, h));
            }
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
        // by image: its facts, its grid, and what the focal plane rule reads of it
        let mut out: Vec<J> = Vec::new();
        let mut grids: Vec<Option<ndpi::Grid>> = Vec::new();
        let mut images: Vec<ndpi::Image> = Vec::new();
        // the pixel format of level 0's first JPEG XR stream (spec/zarrify/ndpi.md §3
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#3-levels>)
        let mut format0: Option<[u8; 16]> = None;
        for (li, &(k, mag, w, h, compression, tiled)) in levels.iter().enumerate() {
            let m = vals(k);
            let offset = if repeated { self.ndpi_focal(k, &mut need) } else { None };
            let image = |cut: ndpi::Cut, framed: bool| ndpi::Image { magnification: mag, width: w, height: h, cut, framed, offset };
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
                    "tw": cw, "th": ch, "strip": s0, "length": n,
                    "tiles_ifd": if tiled { json!(k) } else { J::Null },
                }));
                grids.push(None);
                images.push(image(ndpi::Cut::Fixed(ch, cw), true));
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
                        out.push(J::Null);
                        grids.push(None);
                        images.push(image(ndpi::Cut::Fixed(h, w), false));
                        continue;
                    }
                    frame = json!(f.at);
                }
            }
            let grid = match head {
                None => None,
                Some(McuStarts { starts, .. }) => {
                    let Some(hb) = self.heads.get(&(s0, starts[0])).cloned() else {
                        // a header the batch above did not foresee: ask for it, when it is in the file
                        if !self.within(s0, starts[0]) {
                            return Err(Stop::Reject("internal: a strip header outside the file passed the checks".into()));
                        }
                        return Err(Stop::Need(vec![(s0, starts[0])]));
                    };
                    let hd = ndpi::header(&hb, self.rules)?;
                    let (before, after) = (ndpi::piece(&mut pieces, &hb[..hd.sof.0]), ndpi::piece(&mut pieces, &hb[hd.sof.1..]));
                    Some(ndpi::Grid::new(s0, n, w, h, hb, hd, starts, &before, &after, self.rules)?)
                }
            };
            let high = if added { self.ent_facts(k, 65432) } else { J::Null };
            let cut = grid.as_ref().map_or(ndpi::Cut::Fixed(h, w), |g| ndpi::Cut::Intervals { restart: g.h.restart, mw: g.h.mw, mh: g.h.mh });
            out.push(json!({"strip": s0, "length": n, "starts": self.ent_facts(k, 65426), "high": high, "grid": J::Null, "frame": frame}));
            grids.push(grid);
            images.push(image(cut, true));
        }
        // the levels and their planes (spec/zarrify/ndpi.md §3
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#3-levels>); with images to
        // choose from, the data sources are numbered again by the first use of those kept, and a
        // level's planes get one chunk height (§7
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#7-ndpi-profile>)
        let chosen = ndpi::levels(&images);
        if repeated {
            let mut pieces: Vec<Vec<u8>> = Vec::new();
            for level in &chosen.levels {
                let (mut at, mut planes) = (Vec::new(), Vec::new());
                for &i in level {
                    if let Some(g) = grids[i].take() {
                        let (a, b) = g.pieces();
                        let (before, after) = (ndpi::piece(&mut pieces, a), ndpi::piece(&mut pieces, b));
                        at.push(i);
                        planes.push((g, before, after));
                    }
                }
                ndpi::Grid::one_height(&mut planes, self.rules)?;
                for (i, (g, _, _)) in at.into_iter().zip(planes) {
                    grids[i] = Some(g);
                }
            }
        }
        let offsets = chosen.offsets.as_deref().unwrap_or(&[]);
        let stacked = offsets.len() > 1;
        // `z`'s scale and position, when its planes are evenly spaced (§4
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#4-the-image>)
        let step = if stacked { ndpi::even_step(offsets) } else { None };
        if step.is_some() {
            units.insert("z".into(), json!("micrometer"));
        }
        let (scale_z, start_z) = step.map_or((1.0, 0.0), |s| (s as f64 / 1000.0, offsets[0] as f64 / 1000.0));
        let mut facts_of_levels = Vec::with_capacity(chosen.levels.len());
        let mut scales = Vec::with_capacity(chosen.levels.len());
        for level in &chosen.levels {
            let (w, h) = (images[level[0]].width, images[level[0]].height);
            let planes: Vec<J> = level
                .iter()
                .map(|&i| {
                    let mut p = out[i].take();
                    if let Some(g) = &grids[i] {
                        p["grid"] = g.facts();
                    }
                    p
                })
                .collect();
            facts_of_levels.push(json!({"w": w, "h": h, "planes": planes}));
            let (sy, sx) = (py.unwrap_or(1.0) * (bh as f64 / h as f64), px.unwrap_or(1.0) * (bw as f64 / w as f64));
            scales.push(if stacked { json!([1, scale_z, sy, sx]) } else { json!([1, sy, sx]) });
        }
        let mut position = None;
        if let (Some(sx), Some(sy), Some(ox), Some(oy)) =
            (px, py, m0.get(&65422).and_then(|v| v.first()), m0.get(&65423).and_then(|v| v.first()))
        {
            let (x, y) = (ox / 1000.0 - bw as f64 * sx / 2.0, oy / 1000.0 - bh as f64 * sy / 2.0);
            self.rules.check("translation", rules::env([("x", num(x)), ("y", num(y))]))?;
            position = Some((y, x));
        }
        let translation = match (stacked, position) {
            (false, Some((y, x))) => json!([0, y, x]),
            (true, Some((y, x))) => json!([0, start_z, y, x]),
            (true, None) if step.is_some() => json!([0, start_z, 0, 0]),
            _ => J::Null,
        };
        // the pixels (spec/zarrify/ndpi.md §3
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#3-levels>), and level 0's
        // acquisition tags (§5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#5-source-metadata>)
        let (data_type, samples, codec) = match format0.as_ref().and_then(ndpi::pixel_format) {
            Some((t, s)) => (t, s, "imagecodecs_jpegxr"),
            None => ("uint8", 3, "imagecodecs_jpeg"),
        };
        let (mut own, label) = self.ndpi_acquisition(k0, &mut need);
        if !need.is_empty() {
            return Err(Stop::Need(need));
        }
        // what the source metadata keeps of the focal planes (§5
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#5-source-metadata>)
        if let Some(o) = &chosen.offsets {
            own.insert("FocalOffsets".into(), json!(o));
        }
        if !chosen.left_out.is_empty() {
            let entries: Vec<J> = chosen
                .left_out
                .iter()
                .map(|&(i, reason)| {
                    let im = &images[i];
                    let mut e = json!({
                        "ifd": self.chain.iter().position(|&k| k == levels[i].0),
                        "magnification": crate::project::float_json(im.magnification.to_bits(), 8, im.magnification),
                        "width": im.width,
                        "height": im.height,
                        "reason": reason,
                    });
                    if let Some(o) = im.offset {
                        e["offset"] = json!(o);
                    }
                    e
                })
                .collect();
            own.insert("LeftOut".into(), J::Array(entries));
        }
        let axes: &[&str] = if stacked { &["c", "z", "y", "x"] } else { &["c", "y", "x"] };
        let facts = json!({
            "profile": "ndpi",
            "ifds": self.chain.len(),
            "axes": axes,
            "data_type": data_type,
            "samples": samples,
            "codec": codec,
            "label": label,
            "own": own,
            "units": units,
            "scales": scales,
            "translation": translation,
            "levels": facts_of_levels,
        });
        Ok(Layout { facts, prefixes: HashMap::new(), levels: Vec::new(), planes: None, plan: None, expected: None })
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
                self.check_strips(k, m)?;
                (w, self.req(k, m, 278, Some(u32::MAX as u64))?.min(h))
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
        Ok(Level { w, h, tw, th, ifds: ks.to_vec(), strips: !tiled, join: 1, shard: 1, rectilinear: false, pad: J::Null })
    }

    /// Joins the strips of each uncompressed level into chunks of at most 2^20 bytes and 2^10
    /// strips, as even as whole strips allow (spec/zarrify/tiff.md §4.5
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>). A level stays one strip a chunk
    /// when two strips do not fit, or when a strip does not hold exactly its rows' bytes, which
    /// a chunk could not then find by position.
    fn join_strips(&self, levels: &mut [Level], vals: &HashMap<usize, Vals>, bits: u64, spp: u64, planar: u64, compression: u64) -> Got<()> {
        if compression != 1 {
            return Ok(());
        }
        let contig = spp > 1 && planar == 1;
        for lv in levels.iter_mut().filter(|lv| lv.strips) {
            let row = row_size(lv.w, bits, if contig { spp } else { 1 });
            let strips = lv.h.div_ceil(lv.th);
            let most = (MAX_JOINED_BYTES / (lv.th as u128 * row)).min(MAX_JOINED_STRIPS) as u64;
            if strips < 2 || most < 2 {
                continue;
            }
            let last = lv.h - lv.th * (strips - 1);
            let mut whole = true;
            for &k in &lv.ifds {
                let counts = self.reqs(k, &vals[&k], 279)?;
                whole &= counts.iter().enumerate().all(|(j, &n)| {
                    let rows = if j as u64 % strips == strips - 1 { last } else { lv.th };
                    n as u128 == rows as u128 * row
                });
            }
            if whole {
                lv.join = strips.div_ceil(strips.div_ceil(most));
            }
        }
        Ok(())
    }

    /// Pads each level in strips whose last strip is short to the chunk (spec/zarrify/tiff.md §4.5
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>,
    /// spec/zarrify/qptiff.md §4.2
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#42-tiles-and-strips>): with
    /// zeros when uncompressed, or with a literal tail spliced into the strip's LZW stream, which is
    /// read for that (asked for, when it was not yet); a level of another compression cannot be
    /// padded, which the profile's `padding` role decides. Returns whether each level is kept: padded,
    /// or without a short last strip. Each strip's pad is keyed by its IFD's index in the level and
    /// the strip's.
    fn pad_strips(&self, levels: &mut [Level], vals: &HashMap<usize, Vals>, bits: u64, spp: u64, planar: u64, compression: u64) -> Got<Vec<bool>> {
        let contig = spp > 1 && planar == 1;
        let samples = if spp > 1 && !contig { spp } else { 1 };
        // the sizes in bytes, from the file's integers, in u128 (a product that does not fit in u64
        // saturates, and is then more padding than the `padding` role allows)
        let row_bytes = |lv: &Level| row_size(lv.w, bits, if contig { spp } else { 1 });
        let bytes = |x: u128| u64::try_from(x).unwrap_or(u64::MAX);
        // the bytes of an LZW strip of `n` bytes the scan reads to find where its output reaches
        // `target` bytes (spec/zarrify/tiff.md §7.1
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#71-reading>): at most
        // 3 × target + 64, as a stream that puts a Clear code (12 bits at most) before each
        // literal (9 bits) spends 21 bits on a byte it decodes
        let read = |n: u64, target: u64| n.min(bytes(3 * target as u128 + 64));
        let last_strips = |lv: &Level, k: usize, m: &Vals| -> Got<Vec<(usize, u64, u64)>> {
            let down = lv.h.div_ceil(lv.th);
            let (offsets, counts) = (self.reqs(k, m, 273)?, self.reqs(k, m, 279)?);
            Ok((0..samples)
                .map(|s| {
                    let j = (s * down + down - 1) as usize;
                    (j, offsets[j], counts[j])
                })
                .collect())
        };
        let short_of = |lv: &Level| -> Option<u64> {
            let rows = lv.chunk_rows();
            let down = lv.h.div_ceil(rows);
            let short = lv.h - rows * (down - 1);
            (lv.strips && down > 1 && short != rows).then_some(short)
        };
        let mut need = Vec::new();
        // the distinct ranges of the strips read for their tails, whose bytes together are at most
        // the file's (as strips that do not overlap hold)
        let mut tails: HashSet<(u64, u64)> = HashSet::new();
        if compression == 5 {
            for (li, lv) in levels.iter().enumerate() {
                let Some(short) = short_of(lv) else { continue };
                // the zeros an LZW tail would hold, and the bytes of a strip read for it, within the
                // limits before its strips are read
                let missing = bytes((lv.chunk_rows() - short) as u128 * row_bytes(lv));
                let target = bytes(short as u128 * row_bytes(lv));
                let mut most_read = 0;
                for &k in &lv.ifds {
                    for (_, _, c) in last_strips(lv, k, &vals[&k])? {
                        most_read = most_read.max(read(c, target));
                    }
                }
                self.rules.check(
                    "padding",
                    rules::env([
                        ("level", num(li as f64)),
                        ("compression", num(compression as f64)),
                        ("kept", flag(true)),
                        ("zeros", num(missing as f64)),
                        ("read", num(most_read as f64)),
                    ]),
                )?;
                for &k in &lv.ifds {
                    for (_, o, c) in last_strips(lv, k, &vals[&k])? {
                        let r = read(c, target);
                        if c > 0 && tails.insert((o, r)) && !self.heads.contains_key(&(o, r)) {
                            need.push((o, r));
                        }
                    }
                }
            }
        }
        let total: u128 = tails.iter().map(|&(_, r)| r as u128).sum();
        self.rules.check("tails", rules::env([("read", num(total as f64)), ("size", num(self.size as f64))]))?;
        if !need.is_empty() {
            return Err(Stop::Need(need));
        }
        let mut keep = vec![true; levels.len()];
        for (li, lv) in levels.iter_mut().enumerate() {
            let Some(short) = short_of(lv) else { continue };
            let rows = row_bytes(lv);
            let missing = bytes((lv.chunk_rows() - short) as u128 * rows);
            // the most zero bytes a strip of the level is padded with
            let mut most = missing;
            match compression {
                1 => {
                    // each strip with the zeros its byte count lacks of the chunk's size (none
                    // when it holds the chunk, as a strip written whole does)
                    let mut pads = Vec::new();
                    most = 0;
                    for (c, &k) in lv.ifds.iter().enumerate() {
                        for (j, _, n) in last_strips(lv, k, &vals[&k])? {
                            let z = if lv.join > 1 { missing } else { bytes(lv.th as u128 * rows).saturating_sub(n) };
                            if n > 0 && z > 0 {
                                most = most.max(z);
                                pads.push(json!({"c": c, "k": j, "zeros": z}));
                            }
                        }
                    }
                    lv.pad = json!({"zeros": pads});
                }
                5 => {
                    let mut pads = Vec::new();
                    for (c, &k) in lv.ifds.iter().enumerate() {
                        for (j, o, n) in last_strips(lv, k, &vals[&k])? {
                            if n == 0 {
                                continue; // an absent strip: an absent chunk
                            }
                            let b = self
                                .heads
                                .get(&(o, read(n, bytes(short as u128 * rows))))
                                .ok_or_else(|| Stop::Reject("internal: a strip the layout asked for was not fed".into()))?;
                            let (ok, problem, pad) = match lzw::scan(b, bytes(short as u128 * rows)).and_then(|cut| lzw::tail(b, cut, missing)) {
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
                _ => {
                    keep[li] = false; // not padded: no zeros
                    most = 0;
                }
            }
            self.rules.check(
                "padding",
                rules::env([
                    ("level", num(li as f64)),
                    ("compression", num(compression as f64)),
                    ("kept", flag(keep[li])),
                    ("zeros", num(most as f64)),
                ]),
            )?;
        }
        Ok(keep)
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
        self.rules.check(
            "predictor",
            rules::env([
                ("compression", num(compression as f64)),
                ("predictor", num(predictor as f64)),
                ("sample_format", num(sample_format as f64)),
            ]),
        )?;
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
        let keep = self.pad_strips(&mut levels, &vals, bits, spp, planar, compression)?;
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
            "predictor": predictor,
            "photometric": photometric,
            "channels": channels,
            "axes": axes,
            "units": units,
            "scales": scales,
            "translation": translation,
            "name": name,
        });
        Ok(Layout { facts, prefixes, levels, planes: None, plan: None, expected: None })
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

/// The parser of the profile's store run (`StoreFormat`; crate::store).
pub fn store_parser(ir: crate::ir::Ir, objects: Vec<u32>, keys: &[(String, u64)]) -> crate::ir::Res<Box<dyn crate::store::StoreParser>> {
    Ok(Box::new(crate::tiff_set::TiffSet::new(ir, objects, keys)?))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn compressed_strips_shard_within_the_limits() {
        // (w, h, th, in strips, bits, spp, planar, compression, the strips of a shard)
        let cases: [(u64, u64, u64, bool, u64, u64, u64, u64, u64); 20] = [
            (1344, 1024, 1, true, 16, 1, 1, 5, 1024),          // LZW, one shard a plane
            (1344, 1024, 1, true, 16, 1, 1, 8, 1024),          // Deflate
            (1344, 1024, 1, true, 16, 1, 1, 32946, 1024),      // the other Deflate
            (1344, 1024, 1, true, 16, 1, 1, 50000, 1024),      // zstd
            (16, 4096, 1, true, 8, 1, 1, 8, 1024),             // at the strip limit
            (16, 3000, 1, true, 8, 1, 1, 8, 1000),             // 3000 strips, 3 even shards
            (1 << 16, 1 << 16, 32, true, 8, 1, 1, 8, 512),     // 2^21 bytes a strip: the byte limit first
            (1 << 16, 1 << 14, 16, true, 8, 3, 1, 8, 256),     // interleaved samples count in a row's bytes
            (1 << 16, 1 << 14, 16, true, 8, 3, 2, 8, 1024),    // planar samples do not
            (1 << 19, 1 << 11, 1 << 10, true, 8, 1, 1, 8, 2),  // two strips of 2^29 bytes fit
            (1000, 2, 1, true, 8, 1, 1, 8, 2),                 // two strips are enough
            (100, 1, 1, true, 8, 1, 1, 8, 1),                  // one strip: no shard
            (1 << 20, 1 << 11, 1 << 10, true, 8, 1, 1, 8, 1),  // two strips of 2^30 bytes do not fit
            (1344, 1024, 1, false, 16, 1, 1, 8, 1),            // tiles: no shard
            (1344, 1024, 1, true, 16, 1, 1, 1, 1),             // uncompressed strips join instead
            (1344, 1024, 8, true, 8, 1, 1, 7, 1),              // JPEG
            (1344, 1024, 8, true, 8, 1, 1, 34712, 1),          // JPEG 2000
            (1344, 1000, 16, true, 8, 1, 1, 5, 63),            // a short last LZW strip is padded, and a strip
            (1344, 1000, 16, true, 8, 1, 1, 8, 1),             // a short last Deflate strip is not: no shard
            (1344, 1000, 16, true, 8, 1, 1, 50000, 1),         // nor one of zstd
        ];
        for (w, h, th, strips, bits, spp, planar, compression, want) in cases {
            let mut levels = [Level { w, h, tw: w, th, ifds: vec![], strips, join: 1, shard: 1, rectilinear: false, pad: J::Null }];
            shard_strips(&mut levels, bits, spp, planar, compression);
            assert_eq!(levels[0].shard, want, "{w} x {h}, {th} rows, compression {compression}");
        }
    }
}
