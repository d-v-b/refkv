//! The sans-IO HDF4 parser (spec/zarrify/hdf4.md
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md>): the scientific datasets of
//! numeric types that the SD interface wrote, their attributes, and the hierarchy of the writer's
//! Vgroups, as a Zarr hierarchy.
//!
//! **Sans-IO by replay**, as the HDF5 reader is (`hdf5/mod.rs`): `analyze` reads the file from
//! the start over the bytes fed so far, through a [`Src`] that records each range it lacks; a
//! round asks for every range one pass lacked, and the next pass goes further. Each pass reads in
//! the profile's order (spec/zarrify/hdf4.md §9.2
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#92-reading>): the descriptor
//! blocks, one per round; every header element at once; then what the headers name (link tables,
//! attribute and chunk table records, the chunks' headers), each independent thing going on past a
//! miss ([`Acc`]), so that a rejection never depends on the order of the reads.
//!
//! `finish` emits the IR: the magic number, each descriptor block, and every element a descriptor
//! describes, held when the pass read it and referenced (a `data` element) when not.

use crate::hdf5::types::{float_json, int_of};
use crate::hdf5::{Acc, Fail, Src, R};
use crate::ir::{Ir, NO_INDEX};
use crate::nd2::Step;
use crate::rules::{self, Rules};
use crate::text::{text_json, utf8_or_latin1};
use serde_json::{json, Map, Value as J};
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

/// The file's first 4 bytes (spec/zarrify/hdf4.md §9
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#9-hdf4-profile>).
pub const MAGIC: [u8; 4] = [0x0e, 0x03, 0x13, 0x01];

// Tags (HDF4's htags.h)
pub const NULL: u16 = 1;
pub const LINKED: u16 = 20;
pub const VERSION: u16 = 30;
pub const COMPRESSED: u16 = 40;
pub const CHUNK: u16 = 61;
pub const NT: u16 = 106;
pub const SDD: u16 = 701;
pub const SD: u16 = 702;
pub const NDG: u16 = 720;
pub const VH: u16 = 1962;
pub const VS: u16 = 1963;
pub const VG: u16 = 1965;
/// A descriptor's offset and length of an element never written (`hfile.h`'s INVALID_OFFSET and
/// INVALID_LENGTH, −1 as a `uint32`).
const UNWRITTEN: u64 = 0xFFFF_FFFF;
/// The bit a special element's tag adds to its base tag.
pub const SPECIAL: u16 = 0x4000;

// Special element codes (hfile.h)
const SP_LINKED: u16 = 1;
const SP_EXTERNAL: u16 = 2;
const SP_COMPRESSED: u16 = 3;
const SP_CHUNKED: u16 = 5;
const SP_LAST: u16 = 7;

// Compression codes (hcomp.h)
const COMP_NONE: u16 = 0;
const COMP_DEFLATE: u16 = 4;
/// The compression methods by code, as the source metadata names them.
fn compression_name(c: u16) -> String {
    match c {
        0 => "none".into(),
        1 => "rle".into(),
        2 => "nbit".into(),
        3 => "skipping_huffman".into(),
        4 => "deflate".into(),
        5 => "szip".into(),
        7 => "jpeg".into(),
        12 => "imcomp".into(),
        c => format!("code {c}"),
    }
}

/// The SD and GR interfaces' own Vgroup classes (hlimits.h, INTERNAL_HDF_VGS): bookkeeping, not
/// groups (spec/zarrify/hdf4.md §2.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#21-groups>).
const INTERNAL_VGS: [&[u8]; 6] = [b"Var0.0", b"Dim0.0", b"UDim0.0", b"CDF0.0", b"RIG0.0", b"RI0.0"];
/// The interfaces' own Vdata classes (INTERNAL_HDF_VDS), the chunk tables' a prefix.
const INTERNAL_VDS: [&[u8]; 7] = [b"DimVal0.0", b"DimVal0.1", b"Attr0.0", b"SDSVar", b"CoordVar", b"RIATTR0.0N", b"RIATTR0.0C"];
const CHUNK_TABLE_CLASS: &[u8] = b"_HDF_CHK_TBL_";
const ATTRIBUTE: &[u8] = b"Attr0.0";

/// The names a node may not take at the root, and the attribute names the hierarchy writes itself.
const RESERVED_ROOT: &[&str] = &["refkv_source"];
const RESERVED_ATTRIBUTES: &[&str] = &["zarr_conventions", "refkv_zarrified"];

/// The bytes the first round reads: the magic number and, in most files, the first descriptor
/// block.
const FIRST: u64 = 1 << 16;
/// The bytes a descriptor block's read asks for: 64 KiB, which holds the blocks that follow it
/// when a writer spaced them with the headers between (MODIS's, of 16 descriptors each, every
/// kilobyte or so).
const BLOCK_HINT: u64 = 1 << 16;

fn bad<T>(m: impl Into<String>) -> R<T> {
    Err(Fail::Bad(m.into()))
}

/// A big-endian reader of a structure's bytes: each read None past their end.
struct Rd<'a> {
    b: &'a [u8],
    p: usize,
}

impl<'a> Rd<'a> {
    fn new(b: &'a [u8]) -> Self {
        Rd { b, p: 0 }
    }
    fn at(b: &'a [u8], p: usize) -> Self {
        Rd { b, p }
    }
    fn bytes(&mut self, n: usize) -> Option<&'a [u8]> {
        let s = self.b.get(self.p..self.p.checked_add(n)?)?;
        self.p += n;
        Some(s)
    }
    fn u8(&mut self) -> Option<u8> {
        Some(self.bytes(1)?[0])
    }
    fn u16(&mut self) -> Option<u16> {
        let b = self.bytes(2)?;
        Some(u16::from_be_bytes([b[0], b[1]]))
    }
    fn u32(&mut self) -> Option<u32> {
        let b = self.bytes(4)?;
        Some(u32::from_be_bytes([b[0], b[1], b[2], b[3]]))
    }
}

/// A data descriptor: an element's tag, reference number, offset and length.
#[derive(Clone, Copy, Debug)]
pub struct Dd {
    pub tag: u16,
    pub rf: u16,
    pub off: u64,
    pub len: u64,
}

/// A descriptor block: its offset, descriptor count and bytes.
struct Block {
    at: u64,
    ndd: u64,
}

/// A Vgroup (VG element): its members, name, class and, in version 4, attributes.
#[derive(Clone, Debug, Default)]
pub struct Vg {
    pub members: Vec<(u16, u16)>,
    pub name: Vec<u8>,
    pub class: Vec<u8>,
    pub version: u16,
    pub attrs: Vec<(u16, u16)>,
}

/// A Vdata's header (VH element).
#[derive(Clone, Debug, Default)]
pub struct Vh {
    pub interlace: u16,
    pub nvert: u32,
    pub ivsize: u16,
    /// (name, type, isize, offset, order)
    pub fields: Vec<(Vec<u8>, u16, u16, u16, u16)>,
    pub name: Vec<u8>,
    pub class: Vec<u8>,
    /// its version, the `uint16` 5 bytes before its end
    pub version: u16,
    /// a version 4 header's attributes: (field index, or -1 for the whole Vdata; tag; ref)
    pub attrs: Vec<(i32, u16, u16)>,
}

/// A logical element's bytes, as ranges of the file (with the descriptor that holds each) or
/// zeros (a linked block never written).
#[derive(Clone, Debug, Default)]
pub struct Logical {
    pub len: u64,
    /// (descriptor index or None for zeros, offset, length)
    pub segs: Vec<(Option<usize>, u64, u64)>,
}

impl Logical {
    /// Its first `n` bytes.
    fn prefix(&self, n: u64) -> Logical {
        let mut out = Logical { len: n.min(self.len), segs: Vec::new() };
        let mut left = out.len;
        for &(d, o, m) in &self.segs {
            if left == 0 {
                break;
            }
            let k = m.min(left);
            out.segs.push((d, o, k));
            left -= k;
        }
        out
    }
    /// Its `n` bytes from `at`.
    fn range(&self, mut at: u64, n: u64) -> Logical {
        let mut out = Logical { len: 0, segs: Vec::new() };
        let mut left = n;
        for &(d, o, m) in &self.segs {
            if left == 0 {
                break;
            }
            if at >= m {
                at -= m;
                continue;
            }
            let k = left.min(m - at);
            out.segs.push((d, if d.is_some() { o + at } else { 0 }, k));
            out.len += k;
            left -= k;
            at = 0;
        }
        out
    }
    fn json(&self) -> J {
        J::Array(self.segs.iter().map(|&(d, o, n)| if d.is_some() { json!([o, n]) } else { json!([J::Null, n]) }).collect())
    }
}

/// A special element's header.
#[derive(Clone, Debug)]
enum Special {
    Linked { length: u64, block: u64, per_table: u64, link: u16 },
    External,
    Compressed { length: u64, data: u16, model: u16, method: u16 },
    Chunked(Box<ChunkedHeader>),
    /// another known code (4, 6, 7)
    Other,
}

#[derive(Clone, Debug)]
struct ChunkedHeader {
    length: u64,
    table: u16,
    /// (flag, dimension length, chunk length) per axis
    dims: Vec<(u32, u64, u64)>,
    fill: Vec<u8>,
}

/// How an element is stored, by its descriptors.
enum Stored {
    None,
    Plain(Logical),
    Compressed { length: u64, method: u16, model: u16, data: Logical },
    Chunked(usize),
    External,
    Other,
}

/// The Zarr data type and size of a numeric type code (hntdefs.h), and whether it is signed;
/// None for text and the types the SD interface does not read.
fn numeric(code: u8) -> Option<(&'static str, u64, bool)> {
    Some(match code {
        3 => ("uint8", 1, false),
        5 => ("float32", 4, true),
        6 => ("float64", 8, true),
        20 => ("int8", 1, true),
        21 => ("uint8", 1, false),
        22 => ("int16", 2, true),
        23 => ("uint16", 2, false),
        24 => ("int32", 4, true),
        25 => ("uint32", 4, false),
        _ => return None,
    })
}

/// A number type's name, as the source metadata gives it.
fn type_name(code: u16) -> String {
    match code & 0xff {
        3 => "uchar8".into(),
        4 => "char8".into(),
        5 => "float32".into(),
        6 => "float64".into(),
        20 => "int8".into(),
        21 => "uint8".into(),
        22 => "int16".into(),
        23 => "uint16".into(),
        24 => "int32".into(),
        25 => "uint32".into(),
        26 => "int64".into(),
        27 => "uint64".into(),
        c => format!("type {c}"),
    }
}

/// The value the SD interface reads where a dataset holds no data and has no `_FillValue`: netCDF's
/// default of the type's netCDF type, as `NC_arrayfill` writes it (spec/zarrify/hdf4.md §3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#3-datasets>): `char` 0, `byte`
/// −127 (every byte 0x81, so 129 unsigned), `short` −32767, `long` −2147483647 (32769 and
/// 2147483649 unsigned), `float` and `double` 9.9692099683868690e36.
fn default_fill(code: u8) -> J {
    match code {
        3 => json!(0),
        20 => json!(-127),
        21 => json!(129),
        22 => json!(-32767),
        23 => json!(32769),
        24 => json!(-2147483647),
        25 => json!(2147483649u64),
        // 15 × 2^119, netCDF's FILL_FLOAT and FILL_DOUBLE, exact in both
        5 => float_json(&0x7CF0_0000u32.to_be_bytes(), true),
        _ => float_json(&0x479E_0000_0000_0000u64.to_be_bytes(), true),
    }
}

/// A Zarr fill value or attribute value of one number of `code` in `b`.
fn number_json(code: u8, b: &[u8], big: bool) -> J {
    match code {
        5 | 6 => float_json(b, big),
        _ => crate::out::int_json(int_of(b, big, matches!(code, 20 | 22 | 24 | 26))),
    }
}

/// A dataset of the SD interface: a Vgroup of class `Var0.0` in the file's `CDF0.0` Vgroup.
#[derive(Clone, Debug, Default)]
struct Var {
    vg: u16,
    name: Vec<u8>,
    /// (name, unlimited) of each dimension Vgroup, in order
    dims: Vec<(Vec<u8>, bool)>,
    attrs: Vec<u16>,
    ndg: Option<u16>,
    sd: Option<u16>,
    nt: Option<u16>,
    sdd: Option<u16>,
    /// whether a `CoordVar` or `SDSVar` Vdata marks it a coordinate variable
    marked: Option<bool>,
}

/// An attribute as the hierarchy presents it, or why not.
struct Attr {
    name: Vec<u8>,
    /// its type and count, as the source metadata describes it
    described: J,
    value: Result<J, &'static str>,
    /// its number type code (base) and bytes, for `_FillValue`
    raw: Option<(u8, bool, Vec<u8>)>,
}

/// What the parse found, which `finish` turns into the IR.
struct Found {
    blocks: Vec<Block>,
    dds: Vec<Dd>,
    /// the descriptors whose elements were read whole
    held: BTreeSet<usize>,
}

pub struct Hdf4 {
    size: u64,
    src: Src,
    rules: &'static Rules,
    asked: crate::sans::Asked,
    started: bool,
    found: Option<Found>,
    facts: J,
}

/// The parse of one pass: what is read so far, and the elements read whole.
struct Pass<'a> {
    src: &'a mut Src,
    rules: &'static Rules,
    dds: Vec<Dd>,
    by: HashMap<(u16, u16), usize>,
    held: BTreeSet<usize>,
    bytes: HashMap<usize, Vec<u8>>,
}

impl Pass<'_> {
    fn limit(&self, name: &str) -> u64 {
        self.rules.limit(name) as u64
    }

    fn what(&self, i: usize) -> String {
        let d = self.dds[i];
        format!("tag {} ref {}", d.tag, d.rf)
    }

    /// Element `i`'s bytes, whole, which the IR holds.
    fn element(&mut self, i: usize) -> R<Vec<u8>> {
        if let Some(b) = self.bytes.get(&i) {
            return Ok(b.clone());
        }
        let d = self.dds[i];
        if d.len == 0 {
            return Ok(Vec::new());
        }
        // its exact bytes, which its descriptor gives: the planner joins the reads that are near
        let b = match self.src.get(d.off, d.len) {
            Some(b) => b.to_vec(),
            None => {
                self.src.want.push((d.off, d.len));
                return Err(Fail::Miss);
            }
        };
        self.held.insert(i);
        self.bytes.insert(i, b.clone());
        Ok(b)
    }

    /// The bytes of a header element (at most `header_bytes`).
    fn header(&mut self, i: usize) -> R<Vec<u8>> {
        let d = self.dds[i];
        if d.len > self.limit("header_bytes") {
            return bad(format!("budget: {} is {} bytes, more than {} of a header", self.what(i), d.len, self.limit("header_bytes")));
        }
        self.element(i)
    }

    fn special(&mut self, i: usize) -> R<Special> {
        let b = self.header(i)?;
        let short = || Fail::Bad(format!("the special element {} is too short for its header", self.what(i)));
        let mut r = Rd::new(&b);
        let code = r.u16().ok_or_else(short)?;
        Ok(match code {
            SP_LINKED => {
                let (length, block, per_table, link) = (r.u32().ok_or_else(short)?, r.u32().ok_or_else(short)?, r.u32().ok_or_else(short)?, r.u16().ok_or_else(short)?);
                if per_table == 0 || per_table > 0xffff {
                    return bad(format!("the linked element {} has {per_table} blocks a link table", self.what(i)));
                }
                Special::Linked { length: length as u64, block: block as u64, per_table: per_table as u64, link }
            }
            SP_EXTERNAL => Special::External,
            SP_COMPRESSED => {
                let _version = r.u16().ok_or_else(short)?;
                let length = r.u32().ok_or_else(short)?;
                let data = r.u16().ok_or_else(short)?;
                let model = r.u16().ok_or_else(short)?;
                let method = r.u16().ok_or_else(short)?;
                Special::Compressed { length: length as u64, data, model, method }
            }
            SP_CHUNKED => {
                let _hlen = r.u32().ok_or_else(short)?;
                let _version = r.u8().ok_or_else(short)?;
                let _flag = r.u32().ok_or_else(short)?;
                let length = r.u32().ok_or_else(short)?;
                let _chunk_size = r.u32().ok_or_else(short)?;
                let _nt_size = r.u32().ok_or_else(short)?;
                let _table_tag = r.u16().ok_or_else(short)?;
                let table = r.u16().ok_or_else(short)?;
                let _sp = (r.u16().ok_or_else(short)?, r.u16().ok_or_else(short)?);
                let ndims = r.u32().ok_or_else(short)?;
                if ndims as usize > b.len() / 12 {
                    return Err(short());
                }
                let mut dims = Vec::with_capacity(ndims as usize);
                for _ in 0..ndims {
                    dims.push((r.u32().ok_or_else(short)?, r.u32().ok_or_else(short)? as u64, r.u32().ok_or_else(short)? as u64));
                }
                let fill_len = r.u32().ok_or_else(short)?;
                let fill = r.bytes(fill_len as usize).ok_or_else(short)?.to_vec();
                Special::Chunked(Box::new(ChunkedHeader { length: length as u64, table, dims, fill }))
            }
            c if c <= SP_LAST && c > 0 => Special::Other,
            c => return bad(format!("the special element {} has the unknown code {c}", self.what(i))),
        })
    }

    /// The bytes of the linked element `i` (header `h`) as ranges of its blocks: block 0 as long as
    /// its descriptor says, each other `block` bytes; a block of ref 0 reads as zeros (HDF4's
    /// `HLPread`).
    fn linked(&mut self, i: usize, length: u64, block: u64, per_table: u64, link: u16) -> R<Logical> {
        let block_len = |pass: &Self, r: u16| -> R<Option<(usize, Dd)>> {
            if r == 0 {
                return Ok(None);
            }
            match pass.by.get(&(LINKED, r)) {
                Some(&k) => Ok(Some((k, pass.dds[k]))),
                None => bad(format!("{} names the block ref {r}, which no descriptor describes", pass.what(i))),
            }
        };
        // the link tables the length needs, in their chain: the blocks it needs are the first and
        // enough of `block` bytes for the rest
        let most = self.limit("max_linked_blocks");
        let mut refs: Vec<u16> = Vec::new();
        let mut needed: Option<u64> = None;
        let mut seen = HashSet::new();
        let mut next = link;
        while next != 0 && needed.is_none_or(|n| (refs.len() as u64) < n) {
            if !seen.insert(next) {
                return bad(format!("the link tables of {} loop", self.what(i)));
            }
            let Some(&t) = self.by.get(&(LINKED, next)) else {
                return bad(format!("{} names the link table ref {next}, which no descriptor describes", self.what(i)));
            };
            let b = self.header(t)?;
            let mut r = Rd::new(&b);
            let this = next;
            let short = || Fail::Bad(format!("the link table ref {this} of {} is shorter than its {per_table} blocks", self.what(i)));
            next = r.u16().ok_or_else(short)?;
            for _ in 0..per_table {
                refs.push(r.u16().ok_or_else(short)?);
            }
            if needed.is_none() {
                let first = match refs.first() {
                    Some(&r) => block_len(self, r)?.map_or(block, |(_, d)| d.len),
                    None => block,
                };
                needed = Some(match length.checked_sub(first) {
                    None | Some(0) => 1,
                    Some(_) if block == 0 => u64::MAX,
                    Some(rest) => 1 + rest.div_ceil(block),
                });
            }
            if needed.is_some_and(|n| n > most) {
                return bad(format!("budget: {} needs more than {most} linked blocks", self.what(i)));
            }
        }
        let first = match refs.first() {
            Some(&r) => block_len(self, r)?.map_or(block, |(_, d)| d.len),
            None => block,
        };
        let mut out = Logical { len: length, segs: Vec::new() };
        let mut pos = 0u64;
        for (k, &r) in refs.iter().enumerate() {
            if pos >= length {
                break;
            }
            let cap = if k == 0 { first } else { block };
            let need = cap.min(length - pos);
            if need > 0 {
                match block_len(self, r)? {
                    None => out.segs.push((None, 0, need)),
                    Some((d, dd)) => {
                        if dd.len < need {
                            return bad(format!("the block ref {r} of {} is {} bytes, short of the {need} it holds", self.what(i), dd.len));
                        }
                        out.segs.push((Some(d), dd.off, need));
                    }
                }
            }
            pos += need;
        }
        if pos < length {
            return bad(format!("the blocks of {} hold {pos} of its {length} bytes", self.what(i)));
        }
        Ok(out)
    }

    /// How the element `tag` `rf` is stored: plain, special, or not described.
    fn stored(&mut self, tag: u16, rf: u16) -> R<Stored> {
        if let Some(&i) = self.by.get(&(tag, rf)) {
            let d = self.dds[i];
            return Ok(Stored::Plain(Logical { len: d.len, segs: if d.len > 0 { vec![(Some(i), d.off, d.len)] } else { Vec::new() } }));
        }
        let Some(&i) = self.by.get(&(tag | SPECIAL, rf)) else { return Ok(Stored::None) };
        Ok(match self.special(i)? {
            Special::Linked { length, block, per_table, link } => Stored::Plain(self.linked(i, length, block, per_table, link)?),
            Special::External => Stored::External,
            Special::Compressed { length, data, model, method } => {
                // the stream: a plain element, or linked blocks; never another special element, so
                // that no chain of them is followed
                let data = if let Some(&k) = self.by.get(&(COMPRESSED, data)) {
                    let d = self.dds[k];
                    Logical { len: d.len, segs: if d.len > 0 { vec![(Some(k), d.off, d.len)] } else { Vec::new() } }
                } else if let Some(&k) = self.by.get(&(COMPRESSED | SPECIAL, data)) {
                    match self.special(k)? {
                        Special::Linked { length, block, per_table, link } => self.linked(k, length, block, per_table, link)?,
                        _ => return bad(format!("the compressed element {} holds its data in a special element other than linked blocks", self.what(i))),
                    }
                } else {
                    Logical::default()
                };
                Stored::Compressed { length, method, model, data }
            }
            Special::Chunked(_) => Stored::Chunked(i),
            Special::Other => Stored::Other,
        })
    }

    /// The bytes of a logical element, reading each block whole.
    fn read(&mut self, l: &Logical) -> R<Vec<u8>> {
        let mut out = Vec::with_capacity(l.len as usize);
        let mut acc = Acc::default();
        let mut parts = Vec::new();
        for &(d, o, n) in &l.segs {
            match d {
                None => parts.push(Some(vec![0; n as usize])),
                Some(d) => {
                    let start = (o - self.dds[d].off) as usize;
                    parts.push(acc.take(self.element(d)).map(|b| b[start..start + n as usize].to_vec()));
                }
            }
            if acc.stopped() {
                break;
            }
        }
        acc.done()?;
        for p in parts.into_iter().flatten() {
            out.extend_from_slice(&p);
        }
        Ok(out)
    }
}

impl Hdf4 {
    pub fn new(size: u64) -> Self {
        Hdf4 { size, src: Src::new(size), rules: rules::of("hdf4"), asked: Default::default(), started: false, found: None, facts: J::Null }
    }

    pub fn feed(&mut self, offset: u64, data: Vec<u8>) {
        self.src.store.feed(offset, data);
    }

    pub fn step(&mut self) -> Result<Step, String> {
        if self.found.is_some() {
            return Ok(Step::Done);
        }
        if !self.started {
            self.started = true;
            return Ok(self.asked.ask(vec![(0, self.size.min(FIRST))]));
        }
        self.src.want.clear();
        match self.analyze() {
            Ok(()) => Ok(Step::Done),
            Err(Fail::Bad(m)) => Err(m),
            Err(Fail::Miss) => {
                if self.asked.rounds >= self.rules.limit("max_rounds") as u64 {
                    return Err(format!("more than {} rounds of dependent reads", self.rules.limit("max_rounds")));
                }
                let want = std::mem::take(&mut self.src.want);
                if want.is_empty() {
                    return Err("internal: a read missed and asked for nothing".into());
                }
                Ok(self.asked.ask(want))
            }
        }
    }

    /// One pass over the bytes fed so far (spec/zarrify/hdf4.md §9.2
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#92-reading>): the facts and
    /// what the IR is built from, or the first failure in the profile's order.
    fn analyze(&mut self) -> R<()> {
        let size = self.size;
        if size < 4 || self.src.read(0, 4)? != MAGIC {
            return bad("not an HDF4 file");
        }
        // the descriptor blocks
        let (mut blocks, mut dds) = (Vec::new(), Vec::new());
        let mut seen = HashSet::new();
        let mut at = 4u64;
        let (max_blocks, max_dds) = (self.rules.limit("max_blocks") as u64, self.rules.limit("max_descriptors") as u64);
        let mut total = 0u64;
        loop {
            if blocks.len() as u64 >= max_blocks {
                return bad(format!("budget: more than {max_blocks} descriptor blocks"));
            }
            if !seen.insert(at) {
                return bad(format!("the descriptor blocks loop at byte {at}"));
            }
            if at.checked_add(6).is_none_or(|e| e > size) {
                return bad(format!("the descriptor block at byte {at} runs past the end of the file"));
            }
            let h = self.src.read_hint(at, 6, BLOCK_HINT)?;
            let mut r = Rd::new(&h);
            let (ndd, next) = (r.u16().unwrap_or(0) as i16, r.u32().unwrap_or(0) as u64);
            if ndd < 0 {
                return bad(format!("the descriptor block at byte {at} has {ndd} descriptors"));
            }
            let n = 12 * ndd as u64;
            if at + 6 + n > size {
                return bad(format!("the descriptor block at byte {at} runs past the end of the file"));
            }
            total += ndd as u64;
            if total > max_dds {
                return bad(format!("budget: more than {max_dds} descriptors"));
            }
            let d = self.src.read(at + 6, n)?;
            let mut r = Rd::new(&d);
            for _ in 0..ndd {
                let (tag, rf, off, len) = (r.u16().unwrap_or(0), r.u16().unwrap_or(0), r.u32().unwrap_or(0) as u64, r.u32().unwrap_or(0) as u64);
                // an element created and never written: the library's INVALID_OFFSET and
                // INVALID_LENGTH, -1 each (an empty Vdata's records)
                let len = if (off, len) == (UNWRITTEN, UNWRITTEN) { 0 } else { len };
                if tag != NULL {
                    dds.push(Dd { tag, rf, off, len });
                }
            }
            blocks.push(Block { at, ndd: ndd as u64 });
            if next == 0 {
                break;
            }
            at = next;
        }
        let mut by = HashMap::with_capacity(dds.len());
        for (i, d) in dds.iter().enumerate() {
            if by.insert((d.tag, d.rf), i).is_some() {
                return bad(format!("two descriptors describe tag {} ref {}", d.tag, d.rf));
            }
            if d.len > 0 && d.off.checked_add(d.len).is_none_or(|e| e > size) {
                return bad(format!("tag {} ref {} ({} bytes at byte {}) runs past the end of the file", d.tag, d.rf, d.len, d.off));
            }
        }
        let mut p = Pass { src: &mut self.src, rules: self.rules, dds, by, held: BTreeSet::new(), bytes: HashMap::new() };
        // every header element, in one round
        let mut acc = Acc::default();
        for i in 0..p.dds.len() {
            let t = p.dds[i].tag;
            if matches!(t, NT | SDD | NDG | VH | VG | VERSION) || t & 0xC000 == SPECIAL {
                acc.take(p.header(i));
                if acc.stopped() {
                    break;
                }
            }
        }
        acc.done()?;
        // every special element's header, which a code outside 1 to 7 rejects
        for i in 0..p.dds.len() {
            if p.dds[i].tag & 0xC000 == SPECIAL {
                p.special(i)?;
            }
        }
        let mut vgs: BTreeMap<u16, Vg> = BTreeMap::new();
        let mut vhs: BTreeMap<u16, Vh> = BTreeMap::new();
        for i in 0..p.dds.len() {
            let d = p.dds[i];
            match d.tag {
                VG => {
                    let b = p.header(i)?;
                    vgs.insert(d.rf, parse_vg(&b).ok_or_else(|| Fail::Bad(format!("the Vgroup ref {} runs past its element", d.rf)))?);
                }
                VH => {
                    let b = p.header(i)?;
                    vhs.insert(d.rf, parse_vh(&b).ok_or_else(|| Fail::Bad(format!("the Vdata header ref {} runs past its element", d.rf)))?);
                }
                _ => {}
            }
        }
        let facts = Self::facts_of(&mut p, &vgs, &vhs)?;
        let Pass { dds, held, .. } = p;
        self.found = Some(Found { blocks, dds, held });
        self.facts = facts;
        Ok(())
    }
}

/// A VG element (HDF4's `vunpackvg`): the member count, tags and refs, the name, the class, the
/// expansion tag and ref, and in version 4 the flags and attributes; the version is 5 bytes from its
/// end.
pub fn parse_vg(b: &[u8]) -> Option<Vg> {
    let mut r = Rd::new(b);
    let version = Rd::at(b, b.len().checked_sub(5)?).u16()?;
    let n = r.u16()? as usize;
    let tags: Vec<u16> = (0..n).map(|_| r.u16()).collect::<Option<_>>()?;
    let refs: Vec<u16> = (0..n).map(|_| r.u16()).collect::<Option<_>>()?;
    let nl = r.u16()? as usize;
    let name = r.bytes(nl)?.to_vec();
    let cl = r.u16()? as usize;
    let class = r.bytes(cl)?.to_vec();
    let _ex = (r.u16()?, r.u16()?);
    let mut attrs = Vec::new();
    if version == 4 {
        let flags = r.u32()?;
        if flags & 1 != 0 {
            let k = r.u32()? as usize;
            if k > b.len() / 4 {
                return None;
            }
            for _ in 0..k {
                attrs.push((r.u16()?, r.u16()?));
            }
        }
    }
    Some(Vg { members: tags.into_iter().zip(refs).collect(), name, class, version, attrs })
}

/// A VH element (HDF4's `vunpackvs`): interlace, record count, record size, the fields' types,
/// sizes, offsets, orders and names, the name and the class.
pub fn parse_vh(b: &[u8]) -> Option<Vh> {
    let mut r = Rd::new(b);
    let interlace = r.u16()?;
    let nvert = r.u32()?;
    let ivsize = r.u16()?;
    let n = r.u16()? as i16;
    if n < 0 {
        return None;
    }
    let n = n as usize;
    let col = |r: &mut Rd| -> Option<Vec<u16>> { (0..n).map(|_| r.u16()).collect() };
    let (types, isize, offs, order) = (col(&mut r)?, col(&mut r)?, col(&mut r)?, col(&mut r)?);
    let mut names = Vec::with_capacity(n);
    for _ in 0..n {
        let l = r.u16()? as usize;
        names.push(r.bytes(l)?.to_vec());
    }
    let l = r.u16()? as usize;
    let name = r.bytes(l)?.to_vec();
    let l = r.u16()? as usize;
    let class = r.bytes(l)?.to_vec();
    let fields = (0..n).map(|k| (names[k].clone(), types[k], isize[k], offs[k], order[k])).collect();
    let version = Rd::at(b, b.len().checked_sub(5)?).u16()?;
    let mut attrs = Vec::new();
    // the expansion tag and ref, the version and `more`, then a version 4 header's flags and,
    // when bit 0 (VS_ATTR_SET) is set, its attributes
    let _ex = (r.u16()?, r.u16()?, r.u16()?, r.u16()?);
    if version == 4 {
        let flags = r.u32()?;
        if flags & 1 != 0 {
            let k = r.u32()? as usize;
            if k > b.len() / 8 {
                return None;
            }
            for _ in 0..k {
                attrs.push((r.u32()? as i32, r.u16()?, r.u16()?));
            }
        }
    }
    Some(Vh { interlace, nvert, ivsize, fields, name, class, version, attrs })
}

/// A Zarr node name (spec/zarrify/hdf4.md §2.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#22-paths>, HDF5's rule and no `/`):
/// valid UTF-8, not empty, not `.` or `..`, not starting with `__`.
fn node_name(b: &[u8]) -> Option<String> {
    let s = std::str::from_utf8(b).ok()?;
    (!s.is_empty() && s != "." && s != ".." && !s.starts_with("__") && !s.contains('/')).then(|| s.to_string())
}

fn internal_vg(class: &[u8]) -> bool {
    INTERNAL_VGS.contains(&class)
}

fn internal_vd(class: &[u8]) -> bool {
    INTERNAL_VDS.contains(&class) || class.starts_with(CHUNK_TABLE_CLASS)
}

/// A dataset decided: its array's facts, or why it is not presented.
enum Decided {
    Array { array: J, own: Map<String, J>, chunks: u64 },
    Not(&'static str),
}

/// A writer's Vdata decided (spec/zarrify/hdf4.md §4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#4-tables>): an array of its
/// records, a group of an array per field (interlace 1), or why it is not presented; with its name
/// and attributes.
struct Table {
    name: Vec<u8>,
    form: TableForm,
    attrs: Vec<Attr>,
}

enum TableForm {
    Records { array: J, own: Map<String, J> },
    Fields { own: Map<String, J>, arrays: Vec<(Vec<u8>, J)> },
    Not(&'static str),
}

/// A Vdata field's Zarr data type, fill value, leaf count and whether it has a byte order, or why
/// it has none: `char8` of order `n` is `null_terminated_bytes` of `n`, a number of order 1 its type,
/// of order `n > 1` a `struct` of the fields `0` … `n − 1`.
fn field_type(ty: u16, isize: u16, order: u16) -> Result<(J, J, u64, bool), &'static str> {
    if ty & !0x40FF != 0 {
        return Err("datatype");
    }
    let (order, isize) = (order as u64, isize as u64);
    if order == 0 {
        return Err("fields");
    }
    let code = (ty & 0xFF) as u8;
    if code == 4 {
        if isize != order {
            return Err("fields");
        }
        return Ok((json!({"name": "null_terminated_bytes", "configuration": {"length_bytes": order}}), json!(""), 1, false));
    }
    let Some((one, size, _)) = numeric(code) else { return Err("datatype") };
    if isize != size * order {
        return Err("fields");
    }
    let zero = if code == 5 || code == 6 { json!(0.0) } else { json!(0) };
    if order == 1 {
        return Ok((json!(one), zero, 1, size > 1));
    }
    let fields: Vec<J> = (0..order).map(|k| json!({"name": k.to_string(), "data_type": one})).collect();
    let fill: Map<String, J> = (0..order).map(|k| (k.to_string(), zero.clone())).collect();
    Ok((json!({"name": "struct", "configuration": {"fields": fields}}), J::Object(fill), order, size > 1))
}

/// The `bytes` codec of a struct or value whose elements are `ordered` (of more than one byte, a
/// struct with a text field among them, which zarr-python reads as needing an order): big- or
/// little-endian; else without a byte order.
fn bytes_codec(ordered: bool, big: bool) -> J {
    if ordered {
        json!({"name": "bytes", "configuration": {"endian": if big { "big" } else { "little" }}})
    } else {
        json!({"name": "bytes"})
    }
}

impl Hdf4 {
    /// The attribute Vdatas `refs` as the hierarchy presents them (spec/zarrify/hdf4.md §7
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#7-attributes>), in order.
    fn attributes(p: &mut Pass, vhs: &BTreeMap<u16, Vh>, refs: &[u16]) -> R<Vec<Attr>> {
        let mut out = Vec::new();
        let mut acc = Acc::default();
        for &r in refs {
            let Some(vh) = vhs.get(&r) else { continue };
            let read = Self::attribute(p, vh, r);
            if let Some(a) = acc.take(read) {
                out.push(a);
            }
            if acc.stopped() {
                break;
            }
        }
        acc.done()?;
        Ok(out)
    }

    fn attribute(p: &mut Pass, vh: &Vh, r: u16) -> R<Attr> {
        let mut a = Attr { name: vh.name.clone(), described: J::Null, value: Err("attribute"), raw: None };
        let [(_, ty, isize, _, order)] = vh.fields[..] else { return Ok(a) };
        let count = order as u64 * vh.nvert as u64;
        let code = (ty & 0xff) as u8;
        let mut described = json!({"type": type_name(ty), "count": count});
        if ty & 0x4000 != 0 {
            described["endian"] = json!("little");
        }
        a.described = described;
        let total = isize as u64 * vh.nvert as u64;
        if total > p.limit("attribute_bytes") {
            a.value = Err("budget");
            return Ok(a);
        }
        let l = match p.stored(VS, r)? {
            Stored::Plain(l) => l,
            Stored::None if total == 0 => Logical::default(),
            _ => return Ok(a),
        };
        if l.len < total || vh.interlace != 0 || ty & !0x40ff != 0 {
            return Ok(a);
        }
        let b = p.read(&l.prefix(total))?;
        let big = ty & 0x4000 == 0;
        a.value = match code {
            4 => {
                a.raw = Some((4, true, b.clone()));
                Ok(text_json(&b[..b.iter().position(|&c| c == 0).unwrap_or(b.len())]))
            }
            c => match numeric(c) {
                Some((_, item, _)) if isize as u64 == item * order as u64 => {
                    let vals: Vec<J> = b.chunks(item as usize).map(|v| number_json(c, v, big)).collect();
                    if vals.len() == 1 {
                        a.raw = Some((c, big, b.clone()));
                    }
                    Ok(if vals.len() == 1 { vals.into_iter().next().unwrap_or(J::Null) } else { J::Array(vals) })
                }
                _ => Err("datatype"),
            },
        };
        Ok(a)
    }

    /// The facts: the hierarchy's nodes, as the projection writes them, and the summary.
    fn facts_of(p: &mut Pass, vgs: &BTreeMap<u16, Vg>, vhs: &BTreeMap<u16, Vh>) -> R<J> {
        let class_of = |r: u16| vgs.get(&r).map(|v| v.class.as_slice());
        // the SD interface's datasets: the Var0.0 members of the first CDF0.0 Vgroup
        let cdf = vgs.iter().find(|(_, v)| v.class == b"CDF0.0").map(|(&r, _)| r);
        let mut vars: Vec<Var> = Vec::new();
        if let Some(c) = cdf {
            for &(t, r) in &vgs[&c].members {
                if t != VG || class_of(r) != Some(b"Var0.0") {
                    continue;
                }
                let v = &vgs[&r];
                let mut var = Var { vg: r, name: v.name.clone(), ..Default::default() };
                for &(t, m) in &v.members {
                    match t {
                        VG => match vgs.get(&m) {
                            Some(d) if d.class == b"Dim0.0" || d.class == b"UDim0.0" => var.dims.push((d.name.clone(), d.class == b"UDim0.0")),
                            _ => {}
                        },
                        VH => match vhs.get(&m) {
                            Some(h) if h.class == ATTRIBUTE => var.attrs.push(m),
                            Some(h) if h.class == b"SDSVar" => var.marked = Some(false),
                            Some(h) if h.class == b"CoordVar" => var.marked = Some(true),
                            _ => {}
                        },
                        NDG => var.ndg = Some(m),
                        SD => var.sd = Some(m),
                        NT => var.nt = Some(m),
                        SDD => var.sdd = Some(m),
                        _ => {}
                    }
                }
                vars.push(var);
            }
        }
        // every attribute's records, in one round: what the datasets, the file and the groups read next
        let mut acc = Acc::default();
        for (&r, h) in vhs {
            if h.class == ATTRIBUTE {
                acc.take(Self::attribute(p, h, r));
                if acc.stopped() {
                    break;
                }
            }
        }
        acc.done()?;
        // each dataset decided, and its attributes; in one round each
        let mut decided = Vec::with_capacity(vars.len());
        let mut acc = Acc::default();
        for var in &vars {
            if let Some(d) = acc.take(Self::decide(p, var, vhs)) {
                decided.push(d);
            }
            if acc.stopped() {
                break;
            }
        }
        acc.done()?;
        let mut var_attrs = Vec::with_capacity(vars.len());
        for var in &vars {
            var_attrs.push(Self::attributes(p, vhs, &var.attrs)?);
        }
        let file_attrs = match cdf {
            Some(c) => {
                let refs: Vec<u16> = vgs[&c].members.iter().filter(|m| m.0 == VH && vhs.get(&m.1).is_some_and(|h| h.class == ATTRIBUTE)).map(|m| m.1).collect();
                Self::attributes(p, vhs, &refs)?
            }
            None => Vec::new(),
        };
        // the writer's Vgroups, each one's attributes: its Attr0.0 members and its version 4 attributes
        let writer: Vec<u16> = vgs.iter().filter(|(_, v)| !internal_vg(&v.class)).map(|(&r, _)| r).collect();
        let mut group_attrs: HashMap<u16, Vec<Attr>> = HashMap::new();
        for &g in &writer {
            let v = &vgs[&g];
            let refs: Vec<u16> = v.members.iter().chain(&v.attrs).filter(|m| m.0 == VH && vhs.get(&m.1).is_some_and(|h| h.class == ATTRIBUTE)).map(|m| m.1).collect();
            group_attrs.insert(g, Self::attributes(p, vhs, &refs)?);
        }

        // the hierarchy: the writer's Vgroups no other writer's Vgroup holds, in ref order, then any
        // left (a loop), each depth first; then the datasets in no Vgroup at the root
        let by_ndg: HashMap<u16, usize> = vars.iter().enumerate().filter_map(|(k, v)| Some((v.ndg?, k))).collect();
        let by_vg: HashMap<u16, usize> = vars.iter().enumerate().map(|(k, v)| (v.vg, k)).collect();
        let writer_set: HashSet<u16> = writer.iter().copied().collect();
        let held: HashSet<u16> = writer.iter().flat_map(|g| vgs[g].members.iter().filter(|m| m.0 == VG).map(|m| m.1)).filter(|r| writer_set.contains(r)).collect();
        let dataset_of = |t: u16, r: u16| -> Option<usize> {
            match t {
                NDG => by_ndg.get(&r).copied(),
                VG => by_vg.get(&r).copied(),
                _ => None,
            }
        };
        let internal_members: HashSet<u16> = vgs.values().filter(|v| internal_vg(&v.class)).flat_map(|v| v.members.iter().filter(|m| m.0 == VH).map(|m| m.1)).collect();

        // the writer's tables: the Vdatas of a class not the interfaces' own that no interface's
        // Vgroup holds, each decided in one round
        let mut table_defs = BTreeMap::new();
        let mut acc = Acc::default();
        for (&r, h) in vhs {
            if internal_vd(&h.class) || internal_members.contains(&r) {
                continue;
            }
            if let Some(t) = acc.take(Self::table(p, h, r, vhs)) {
                table_defs.insert(r, t);
            }
            if acc.stopped() {
                break;
            }
        }
        acc.done()?;
        let mut b = Builder { nodes: Vec::new(), paths: HashSet::new(), reached: HashSet::new(), tables: HashSet::new(), retry: HashMap::new(), paths_vg: HashMap::new(), paths_ds: HashMap::new(), paths_tables: HashMap::new(), table_defs, unpresented: BTreeMap::new(), groups: 0, arrays: 0, chunks: 0 };
        // the root
        let mut root_own = Map::new();
        if let Some(i) = p.dds.iter().position(|d| d.tag == VERSION) {
            let v = p.header(i)?;
            let mut r = Rd::new(&v);
            if let (Some(major), Some(minor), Some(release)) = (r.u32(), r.u32(), r.u32()) {
                let rest = &v[12..];
                root_own.insert("version".into(), json!({"major": major, "minor": minor, "release": release,
                                                          "text": text_json(&rest[..rest.iter().position(|&c| c == 0).unwrap_or(rest.len())])}));
            }
        }
        root_own.insert("cdf".into(), cdf.map_or(J::Null, |c| json!(c)));
        let root = b.group(String::new(), Vec::new(), file_attrs, root_own);
        let mut roots: Vec<u16> = writer.iter().copied().filter(|r| !held.contains(r)).collect();
        let mut walked: HashSet<u16> = HashSet::new();
        let mut left: Vec<u16> = writer.clone();
        loop {
            for g in std::mem::take(&mut roots) {
                if walked.contains(&g) {
                    continue;
                }
                Self::walk(p.rules, &mut b, root, g, vgs, vhs, &vars, &decided, &var_attrs, &mut group_attrs, &dataset_of, &mut walked)?;
            }
            left.retain(|g| !walked.contains(g));
            match left.first() {
                Some(&g) => roots.push(g),
                None => break,
            }
        }
        for k in 0..vars.len() {
            // a dataset the walk did not reach: in no Vgroup, or only in Vgroups not presented
            if !b.reached.contains(&k) {
                b.dataset(root, k, &vars[k], &decided[k], &var_attrs[k]);
            }
        }
        // the writer's tables the walk did not reach, and the datasets of a file without a CDF0.0
        // Vgroup, which the profile does not present
        let tables: Vec<u16> = b.table_defs.keys().copied().filter(|r| !b.tables.contains(r)).collect();
        for r in tables {
            b.table(root, r);
        }
        if cdf.is_none() {
            for d in p.dds.iter().filter(|d| d.tag == NDG) {
                b.not(root, None, NDG, d.rf, "dfsd");
            }
        }
        let Builder { mut nodes, unpresented, groups, arrays, chunks, .. } = b;
        let mut counts: BTreeMap<String, u64> = BTreeMap::new();
        for (node, list) in unpresented {
            for u in &list {
                *counts.entry(u["reason"].as_str().unwrap_or("").to_string()).or_default() += 1;
            }
            nodes[node]["own"]["unpresented"] = J::Array(list);
        }
        let summary = json!({"groups": groups, "arrays": arrays, "unpresented": counts, "chunks": chunks,
                             "descriptors": p.dds.len()});
        Ok(json!({"nodes": nodes, "summary": summary}))
    }

    /// The writer's Vgroup `g` under node `parent`, and what it holds: depth first, each group's
    /// members in their order (spec/zarrify/hdf4.md §2.2
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#22-paths>).
    #[allow(clippy::too_many_arguments)]
    fn walk(rules: &Rules, b: &mut Builder, parent: usize, g: u16, vgs: &BTreeMap<u16, Vg>, vhs: &BTreeMap<u16, Vh>, vars: &[Var], decided: &[Decided],
            var_attrs: &[Vec<Attr>], group_attrs: &mut HashMap<u16, Vec<Attr>>, dataset_of: &dyn Fn(u16, u16) -> Option<usize>,
            walked: &mut HashSet<u16>) -> R<()> {
        // the group presented for Vgroup `g` under `parent` at `depth`, or None
        let mut enter = |b: &mut Builder, parent: usize, g: u16, depth: usize| -> Option<usize> {
            let v = &vgs[&g];
            if let Some(path) = b.paths_vg.get(&g).cloned() {
                b.link(parent, &v.name, VG, g, &path);
                return None;
            }
            walked.insert(g);
            if depth > rules.limit("max_depth") as usize {
                b.not(parent, Some(&v.name), VG, g, "depth");
                return None;
            }
            let name = b.name_in(parent, &v.name, VG, g).ok()?;
            let mut own = Map::new();
            own.insert("ref".into(), json!(g));
            own.insert("class".into(), text_json(&v.class));
            let node = b.group(name, vec![parent], group_attrs.remove(&g).unwrap_or_default(), own);
            b.paths_vg.insert(g, b.nodes[node]["path"].as_str().unwrap_or("").to_string());
            Some(node)
        };
        // (group node, Vgroup, the next member, depth)
        let mut stack: Vec<(usize, u16, usize, usize)> = Vec::new();
        if let Some(node) = enter(b, parent, g, 1) {
            stack.push((node, g, 0, 1));
        }
        while let Some(top) = stack.last_mut() {
            let (node, g, k, depth) = *top;
            let Some(&(t, r)) = vgs[&g].members.get(k) else {
                stack.pop();
                continue;
            };
            top.2 += 1;
            if let Some(k) = dataset_of(t, r) {
                // presented once, at its first visit where it can be; recorded once, where it is first
                // not presented, but for a name another member of a group holds
                match b.paths_ds.get(&k).cloned() {
                    Some(path) => b.link(node, &vars[k].name, t, r, &path),
                    None if b.reached.contains(&k) && b.retry.get(&k).is_none_or(|g| g.contains(&node)) => {}
                    None => b.dataset(node, k, &vars[k], &decided[k], &var_attrs[k]),
                }
                continue;
            }
            match t {
                VG if vgs.get(&r).is_some_and(|c| !internal_vg(&c.class)) => {
                    if let Some(child) = enter(b, node, r, depth + 1) {
                        stack.push((child, r, 0, depth + 1));
                    }
                }
                VG => b.not(node, vgs.get(&r).map(|c| c.name.as_slice()), t, r, "object"),
                VH => match vhs.get(&r) {
                    Some(h) if h.class == ATTRIBUTE => {}
                    Some(_) if b.table_defs.contains_key(&r) => b.table(node, r),
                    Some(h) => b.not(node, Some(&h.name), t, r, "object"),
                    None => b.not(node, None, t, r, "object"),
                },
                _ => b.not(node, None, t, r, "object"),
            }
        }
        Ok(())
    }

    /// Whether and how the writer's Vdata `vh`, of ref `r`, is presented (spec/zarrify/hdf4.md §4
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#4-tables>).
    fn table(p: &mut Pass, vh: &Vh, r: u16, vhs: &BTreeMap<u16, Vh>) -> R<Table> {
        let attr_refs: Vec<u16> = vh.attrs.iter().filter(|a| a.0 == -1 && a.1 == VH).map(|a| a.2).collect();
        let attrs = Self::attributes(p, vhs, &attr_refs)?;
        let form = Self::table_form(p, vh, r, vhs)?;
        Ok(Table { name: vh.name.clone(), form, attrs })
    }

    fn table_form(p: &mut Pass, vh: &Vh, r: u16, vhs: &BTreeMap<u16, Vh>) -> R<TableForm> {
        let mut own = Map::new();
        own.insert("ref".into(), json!(r));
        own.insert("class".into(), text_json(&vh.class));
        own.insert("interlace".into(), json!(vh.interlace));
        own.insert("records".into(), json!(vh.nvert));
        let described: Vec<J> = vh.fields.iter().map(|(n, t, isize, off, order)| {
            let mut f = json!({"name": text_json(n), "type": type_name(*t), "order": order, "offset": off, "size": isize});
            if t & 0x4000 != 0 {
                f["endian"] = json!("little");
            }
            f
        }).collect();
        own.insert("fields".into(), J::Array(described));
        // each field's attributes, by the field's name
        let mut by_field: Map<String, J> = Map::new();
        for &(k, t, a) in &vh.attrs {
            let Some(field) = usize::try_from(k).ok().and_then(|k| vh.fields.get(k)) else { continue };
            if t != VH {
                continue;
            }
            for at in Self::attributes(p, vhs, &[a])? {
                if let Ok(v) = at.value {
                    let e = by_field.entry(utf8_or_latin1(&field.0)).or_insert_with(|| json!({}));
                    e[utf8_or_latin1(&at.name)] = v;
                }
            }
        }
        if !by_field.is_empty() {
            own.insert("field_attributes".into(), J::Object(by_field));
        }
        if vh.version <= 2 {
            return Ok(TableForm::Not("version"));
        }
        if vh.fields.is_empty() || vh.interlace > 1 {
            return Ok(TableForm::Not("fields"));
        }
        let mut types = Vec::with_capacity(vh.fields.len());
        let mut big: Option<bool> = None;
        let (mut names, mut leaves) = (HashSet::new(), 0u64);
        for (name, ty, isize, _, order) in &vh.fields {
            let (dt, fill, n, ordered) = match field_type(*ty, *isize, *order) {
                Ok(t) => t,
                Err(why) => return Ok(TableForm::Not(why)),
            };
            if ordered {
                let b = ty & 0x4000 == 0;
                if big.is_some_and(|x| x != b) {
                    return Ok(TableForm::Not("byte_order"));
                }
                big = Some(b);
            }
            if !names.insert(utf8_or_latin1(name)) {
                return Ok(TableForm::Not("fields"));
            }
            leaves += n;
            types.push((utf8_or_latin1(name), dt, fill, ordered, *isize as u64));
        }
        if leaves > p.limit("max_fields") {
            return Ok(TableForm::Not("fields"));
        }
        // interlace 0: each record its fields in order, at their offsets, nothing between
        let mut at = 0u64;
        for (_, _, isize, off, _) in &vh.fields {
            if vh.interlace == 0 && *off as u64 != at {
                return Ok(TableForm::Not("fields"));
            }
            at += *isize as u64;
        }
        if at != vh.ivsize as u64 {
            return Ok(TableForm::Not("fields"));
        }
        let record = at;
        if record > p.limit("record_bytes") {
            return Ok(TableForm::Not("record_size"));
        }
        let rows = vh.nvert as u64;
        let total = rows * record;
        let data = match p.stored(VS, r)? {
            Stored::Plain(l) if l.len >= total => l,
            Stored::None if total == 0 => Logical::default(),
            Stored::Plain(_) | Stored::None => return Ok(TableForm::Not("length")),
            Stored::External => return Ok(TableForm::Not("external")),
            _ => return Ok(TableForm::Not("special")),
        };
        let store = |l: Logical| if l.len == 0 { json!({"kind": "none"}) } else { json!({"kind": "contiguous", "segments": l.json()}) };
        if vh.interlace == 0 {
            let fields: Vec<J> = types.iter().map(|t| json!({"name": t.0, "data_type": t.1})).collect();
            let fill: Map<String, J> = types.iter().map(|t| (t.0.clone(), t.2.clone())).collect();
            let array = json!({"shape": [rows], "data_type": {"name": "struct", "configuration": {"fields": fields}}, "item": record,
                               "fill_value": fill, "codecs": [bytes_codec(types.iter().any(|t| t.4 > 1), big.unwrap_or(true))],
                               "store": store(data.prefix(total)), "dimension_names": ["record"]});
            return Ok(TableForm::Records { array, own });
        }
        // interlace 1: each field's values together, the fields in order
        let mut arrays = Vec::with_capacity(types.len());
        let mut at = 0u64;
        for ((name, dt, fill, ordered, isize), field) in types.into_iter().zip(&vh.fields) {
            let n = rows * isize;
            let array = json!({"shape": [rows], "data_type": dt, "item": isize, "fill_value": fill,
                               "codecs": [bytes_codec(ordered || isize > 1, field.1 & 0x4000 == 0)], "store": store(data.range(at, n)),
                               "dimension_names": ["record"]});
            arrays.push((name.into_bytes(), array));
            at += n;
        }
        Ok(TableForm::Fields { own, arrays })
    }

    /// Whether and how the dataset `var` is an array (spec/zarrify/hdf4.md §3
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#3-datasets>).
    fn decide(p: &mut Pass, var: &Var, vhs: &BTreeMap<u16, Vh>) -> R<Decided> {
        let mut own = Map::new();
        own.insert("ref".into(), json!(var.vg));
        if let Some(n) = var.ndg {
            own.insert("ndg".into(), json!(n));
        }
        // the number type
        let Some(nt) = var.nt.and_then(|r| p.by.get(&(NT, r)).copied()) else { return Ok(Decided::Not("number_type")) };
        let ntb = p.header(nt)?;
        let [_, code, width, class, ..] = ntb[..] else { return Ok(Decided::Not("number_type")) };
        own.insert("number_type".into(), json!({"type": type_name(code as u16), "width": width, "class": class}));
        // a dataset of text: its bytes are read as `uint8` until its last axis is made its strings'
        // length (spec/zarrify/hdf4.md §3.3
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#33-text>)
        let text = code == 4;
        let Some((data_type, item, _)) = (if text { Some(("uint8", 1, false)) } else { numeric(code) }) else {
            return Ok(Decided::Not("number_type"));
        };
        let big = match class {
            _ if item == 1 => true,
            0 | 1 => true,
            4 => false,
            _ => return Ok(Decided::Not("number_type")),
        };
        // the dimensions
        let Some(sdd) = var.sdd.and_then(|r| p.by.get(&(SDD, r)).copied()) else { return Ok(Decided::Not("dimensions")) };
        let sb = p.header(sdd)?;
        let mut r = Rd::new(&sb);
        let rank = r.u16().unwrap_or(0) as usize;
        let sizes: Option<Vec<u64>> = (0..rank).map(|_| r.u32().map(|v| v as u64)).collect();
        let Some(mut shape) = sizes else { return Ok(Decided::Not("dimensions")) };
        if rank == 0 || rank != var.dims.len() {
            return Ok(Decided::Not("dimensions"));
        }
        let unlimited = var.dims[0].1;
        // the data
        let stored = match var.sd {
            Some(r) => p.stored(SD, r)?,
            None => Stored::None,
        };
        let (count, layout) = match &stored {
            Stored::None => (0, "none"),
            Stored::Plain(l) => (l.len, if l.segs.len() > 1 || l.segs.iter().any(|s| s.0.is_none_or(|d| p.dds[d].tag == LINKED)) { "linked" } else { "contiguous" }),
            Stored::Compressed { length, .. } => (*length, "compressed"),
            Stored::Chunked(i) => match p.special(*i)? {
                Special::Chunked(h) => (h.length, "chunked"),
                _ => (0, "chunked"),
            },
            Stored::External => return Ok(Decided::Not("external")),
            Stored::Other => return Ok(Decided::Not("special")),
        };
        own.insert("layout".into(), json!(layout));
        if unlimited {
            own.insert("unlimited".into(), json!(true));
            own.insert("sdd".into(), json!(shape));
            let record = shape[1..].iter().try_fold(item, |a, &v| a.checked_mul(v)).unwrap_or(0);
            shape[0] = count.checked_div(record).unwrap_or(0);
        }
        let Some(nbytes) = shape.iter().try_fold(item, |a, &v| a.checked_mul(v)) else { return Ok(Decided::Not("size")) };
        let names: Vec<J> = var.dims.iter().map(|d| json!(utf8_or_latin1(&d.0))).collect();
        if let Some(m) = var.marked {
            own.insert("coordinate".into(), json!(m));
        }
        let mut codecs = vec![if item == 1 { json!({"name": "bytes"}) } else { json!({"name": "bytes", "configuration": {"endian": if big { "big" } else { "little" }}}) }];
        let zlib = json!({"name": "numcodecs.zlib", "configuration": {"level": 1}});
        let mut chunk_fill: Option<Vec<u8>> = None;
        let mut nchunks = 0u64;
        let store = match stored {
            _ if nbytes == 0 => json!({"kind": "none"}),
            Stored::None => json!({"kind": "none"}),
            Stored::Plain(l) => {
                if l.len < nbytes {
                    return Ok(Decided::Not("length"));
                }
                json!({"kind": "contiguous", "segments": l.prefix(nbytes).json()})
            }
            Stored::Compressed { length, method, model, data } => {
                own.insert("compression".into(), json!(compression_name(method)));
                if length == 0 {
                    json!({"kind": "none"})
                } else if model != 0 {
                    return Ok(Decided::Not("compression"));
                } else if method == COMP_NONE {
                    if data.len < nbytes {
                        return Ok(Decided::Not("length"));
                    }
                    json!({"kind": "contiguous", "segments": data.prefix(nbytes).json()})
                } else if method == COMP_DEFLATE {
                    if length != nbytes || data.len == 0 {
                        return Ok(Decided::Not("length"));
                    }
                    codecs.push(zlib.clone());
                    json!({"kind": "whole", "segments": data.json()})
                } else {
                    return Ok(Decided::Not("compression"));
                }
            }
            Stored::Chunked(i) => {
                let Special::Chunked(h) = p.special(i)? else { return Ok(Decided::Not("special")) };
                match Self::chunked(p, i, &h, &shape, item, vhs)? {
                    Ok((chunk, chunks, method)) => {
                        own.insert("chunks".into(), json!(chunk));
                        if let Some(m) = method {
                            own.insert("compression".into(), json!(compression_name(m)));
                            if m == COMP_DEFLATE {
                                codecs.push(zlib.clone());
                            }
                        }
                        if h.fill.len() as u64 == item {
                            chunk_fill = Some(h.fill.clone());
                            own.insert("chunk_fill_value".into(), json!(crate::mirror::base64(&h.fill)));
                        }
                        nchunks = chunks.len() as u64;
                        json!({"kind": "chunked", "chunk": chunk, "chunks": chunks})
                    }
                    Err(why) => return Ok(Decided::Not(why)),
                }
            }
            Stored::External | Stored::Other => return Ok(Decided::Not("special")),
        };
        // the fill value: the chunk header's, else `_FillValue`'s, else netCDF's default
        let fill_attr = Self::attributes(p, vhs, &var.attrs)?.into_iter().find(|a| a.name == b"_FillValue").and_then(|a| a.raw);
        if text {
            let byte = match (&chunk_fill, &fill_attr) {
                (Some(f), _) => f.first().copied().unwrap_or(0),
                (None, Some((4, _, raw))) => raw.first().copied().unwrap_or(0),
                _ => 0,
            };
            return Ok(Self::as_text(shape, names, store, codecs, byte, own, nchunks));
        }
        let fill = match (&chunk_fill, &fill_attr) {
            (Some(f), _) => number_json(code, f, big),
            (None, Some((c, b, raw))) if *c == code || numeric(*c).map(|n| n.0) == Some(data_type) && numeric(*c).map(|n| n.1) == Some(item) => number_json(code, raw, *b),
            _ => default_fill(code),
        };
        Ok(Decided::Array {
            array: json!({"shape": shape, "data_type": data_type, "item": item, "fill_value": fill, "codecs": codecs, "store": store,
                          "dimension_names": names}),
            own,
            chunks: nchunks,
        })
    }

    /// A `char8` dataset of `shape`, read as bytes, as an array of strings
    /// (spec/zarrify/hdf4.md §3.3
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#33-text>): `null_terminated_bytes`
    /// of its last axis's length, its other axes the array's, its chunks the same bytes with that
    /// axis dropped from their coordinates; every byte of the fill value the fill byte.
    #[allow(clippy::too_many_arguments)]
    fn as_text(shape: Vec<u64>, names: Vec<J>, mut store: J, codecs: Vec<J>, fill: u8, mut own: Map<String, J>, nchunks: u64) -> Decided {
        let Some((&length, outer)) = shape.split_last() else { return Decided::Not("dimensions") };
        if length == 0 || length > 1 << 20 {
            return Decided::Not("size");
        }
        if store["kind"] == "chunked" {
            if store["chunk"].as_array().and_then(|c| c.last()).and_then(J::as_u64) != Some(length) {
                return Decided::Not("chunks");
            }
            if let Some(c) = store["chunk"].as_array_mut() {
                c.pop();
            }
            for ch in store["chunks"].as_array_mut().into_iter().flatten() {
                if let Some(at) = ch["at"].as_array_mut() {
                    at.pop();
                }
            }
        }
        own.insert("text_axis".into(), names.last().cloned().unwrap_or(J::Null));
        let fill = if fill == 0 { json!("") } else { json!(crate::mirror::base64(&vec![fill; length as usize])) };
        Decided::Array {
            array: json!({"shape": outer, "data_type": {"name": "null_terminated_bytes", "configuration": {"length_bytes": length}},
                          "item": length, "fill_value": fill, "codecs": codecs, "store": store,
                          "dimension_names": names[..names.len() - 1]}),
            own,
            chunks: nchunks,
        }
    }

    /// A chunked dataset's chunk shape and chunks (each its grid coordinates and the segments of its
    /// stored bytes) and the chunks' compression, or why it is not presented.
    #[allow(clippy::type_complexity)]
    fn chunked(p: &mut Pass, i: usize, h: &ChunkedHeader, shape: &[u64], item: u64, vhs: &BTreeMap<u16, Vh>)
               -> R<Result<(Vec<u64>, Vec<J>, Option<u16>), &'static str>> {
        let what = p.what(i);
        if h.dims.len() != shape.len() {
            return Ok(Err("dimensions"));
        }
        let chunk: Vec<u64> = h.dims.iter().map(|d| d.2).collect();
        if chunk.contains(&0) {
            return bad(format!("the chunked element {what} has a chunk length of 0"));
        }
        let grid: Vec<u64> = h.dims.iter().map(|d| d.1.div_ceil(d.2)).collect();
        let Some(chunk_bytes) = chunk.iter().try_fold(item, |a, &v| a.checked_mul(v)) else { return Ok(Err("size")) };
        let Some(vh) = vhs.get(&h.table) else {
            return bad(format!("the chunk table ref {} of {what} has no Vdata header", h.table));
        };
        let rank = shape.len();
        let record = 4 * rank as u64 + 4;
        if vh.ivsize as u64 != record {
            return bad(format!("the chunk table ref {} of {what} has records of {} bytes, not {record}", h.table, vh.ivsize));
        }
        let rows = vh.nvert as u64;
        if rows > p.limit("max_chunks") {
            return bad(format!("budget: the chunk table ref {} of {what} has more than {} chunks", h.table, p.limit("max_chunks")));
        }
        if rows * record > p.limit("chunk_table_bytes") {
            return bad(format!("budget: the chunk table ref {} of {what} is more than {} bytes", h.table, p.limit("chunk_table_bytes")));
        }
        let table = match p.stored(VS, h.table)? {
            Stored::Plain(l) if l.len >= rows * record => p.read(&l.prefix(rows * record))?,
            Stored::None if rows == 0 => Vec::new(),
            _ => return bad(format!("the chunk table ref {} of {what} holds fewer than its {rows} records", h.table)),
        };
        let mut seen = HashSet::new();
        let mut chunks = Vec::new();
        let mut method: Option<Option<u16>> = None;
        let mut acc = Acc::default();
        let mut mixed = false;
        for k in 0..rows as usize {
            let mut r = Rd::new(&table[k * record as usize..(k + 1) * record as usize]);
            let at: Vec<u64> = (0..rank).map(|_| r.u32().unwrap_or(0) as u64).collect();
            let (ct, cr) = (r.u16().unwrap_or(0), r.u16().unwrap_or(0));
            if at.iter().zip(&grid).any(|(a, g)| a >= g) {
                return bad(format!("the chunk table of {what} places a chunk at {at:?}, outside its grid {grid:?}"));
            }
            if !seen.insert(at.clone()) {
                return bad(format!("the chunk table of {what} places two chunks at {at:?}"));
            }
            let Some(st) = acc.take(p.stored(ct, cr)) else {
                if acc.stopped() {
                    break;
                }
                continue;
            };
            let (segs, m) = match st {
                Stored::Plain(l) => {
                    if l.len < chunk_bytes {
                        return bad(format!("the chunk at {at:?} of {what} is {} bytes, short of {chunk_bytes}", l.len));
                    }
                    (l.prefix(chunk_bytes), None)
                }
                Stored::Compressed { length, method: m, model, data } => {
                    if length != chunk_bytes {
                        return bad(format!("the chunk at {at:?} of {what} decodes to {length} bytes, not {chunk_bytes}"));
                    }
                    if model != 0 || !matches!(m, COMP_DEFLATE | COMP_NONE) {
                        if method.is_none() {
                            method = Some(Some(m));
                        }
                        mixed = mixed || method != Some(Some(m));
                        continue;
                    }
                    // a stream as long as the chunk (stored) or of a byte at least (deflated)
                    if data.len < if m == COMP_NONE { chunk_bytes } else { 1 } {
                        return bad(format!("the chunk at {at:?} of {what} holds {} bytes of its stream", data.len));
                    }
                    if m == COMP_NONE {
                        (data.prefix(chunk_bytes), None)
                    } else {
                        (data, Some(m))
                    }
                }
                Stored::None => return bad(format!("the chunk at {at:?} of {what} names tag {ct} ref {cr}, which no descriptor describes")),
                _ => return Ok(Err("special")),
            };
            match method {
                None => method = Some(m),
                Some(prev) if prev != m => mixed = true,
                _ => {}
            }
            // a chunk of the unlimited axis past the records is not presented
            if at.iter().zip(&chunk).zip(shape).any(|((a, c), s)| a * c >= *s) {
                continue;
            }
            chunks.push(json!({"at": at, "segments": segs.json()}));
        }
        acc.done()?;
        if mixed {
            return Ok(Err("chunks"));
        }
        let method = method.flatten();
        if method.is_some_and(|m| m != COMP_DEFLATE) {
            return Ok(Err("compression"));
        }
        Ok(Ok((chunk, chunks, method)))
    }

    /// The finished IR and the facts.
    pub fn finish(self) -> Result<(Ir, J), String> {
        let Some(found) = self.found else { return Err("internal: the parse is not done".into()) };
        let mut ir = Ir::new(self.size);
        let src = &self.src;
        let get = |o: u64, n: u64| src.get(o, n).ok_or_else(|| format!("internal: the bytes at {o} were not read"));
        ir.value(0, "magic", NO_INDEX, "bytes[4]", 0, (0, 4), Some(&MAGIC))?;
        for (k, bl) in found.blocks.iter().enumerate() {
            let n = 6 + 12 * bl.ndd;
            let ty = if bl.ndd == 0 {
                "{count:>i2,next:>u4}".to_string()
            } else {
                format!("{{count:>i2,next:>u4,descriptors:{{tag:>u2,ref:>u2,offset:>u4,length:>u4}}[{}]}}", bl.ndd)
            };
            ir.value(0, "descriptors/", k as u64, &ty, 0, (bl.at, n), Some(get(bl.at, n)?))?;
        }
        let els = ir.struct_(0, "elements", NO_INDEX, 0, None)?;
        for (i, d) in found.dds.iter().enumerate() {
            if d.len == 0 {
                continue;
            }
            let name = format!("{}/", d.tag);
            if found.held.contains(&i) {
                let ty = match (d.tag, d.len) {
                    (NT, 4) => "{version:u1,type:u1,width:u1,class:u1}".to_string(),
                    _ => format!("bytes[{}]", d.len),
                };
                ir.value(els, &name, d.rf as u64, &ty, 0, (d.off, d.len), Some(get(d.off, d.len)?))?;
            } else {
                ir.data(els, &name, d.rf as u64, 0, (d.off, d.len), r#"{"recipe":[["src",0,null]]}"#)?;
            }
        }
        let (ir, mut facts) = crate::sans::finish(true, ir, self.facts)?;
        facts["copy_threshold"] = json!(crate::out::copy_threshold());
        self.asked.put(&mut facts);
        facts["summary"]["rounds"] = json!(self.asked.rounds);
        Ok((ir, facts))
    }
}

/// The nodes as the walk presents them, each group's unpresented members, and the counts.
struct Builder {
    nodes: Vec<J>,
    /// the nodes' paths
    paths: HashSet<String>,
    /// the datasets and tables the walk reached, presented or recorded
    reached: HashSet<usize>,
    tables: HashSet<u16>,
    /// the writer's tables, decided, and the canonical path of each presented
    table_defs: BTreeMap<u16, Table>,
    paths_tables: HashMap<u16, String>,
    /// the datasets reached and not presented for their name alone, with the groups that recorded them
    retry: HashMap<usize, HashSet<usize>>,
    /// the canonical path of each Vgroup and dataset presented
    paths_vg: HashMap<u16, String>,
    paths_ds: HashMap<usize, String>,
    unpresented: BTreeMap<usize, Vec<J>>,
    groups: u64,
    arrays: u64,
    chunks: u64,
}

impl Builder {
    fn path_of(&self, parent: &[usize], name: &str) -> String {
        match parent.first() {
            Some(&p) => {
                let base = self.nodes[p]["path"].as_str().unwrap_or("");
                if base.is_empty() { name.to_string() } else { format!("{base}/{name}") }
            }
            None => String::new(),
        }
    }

    /// A child of `parent` named `name` (a node name, the first of its name in the group), or
    /// the reason it is not presented, which is recorded.
    fn name_in(&mut self, parent: usize, name: &[u8], tag: u16, rf: u16) -> Result<String, &'static str> {
        let reason = match node_name(name) {
            None => "name",
            Some(n) if self.nodes[parent]["path"] == json!("") && RESERVED_ROOT.contains(&n.as_str()) => "reserved",
            Some(n) if self.paths.contains(&self.path_of(&[parent], &n)) => "duplicate",
            Some(n) => return Ok(n),
        };
        self.not(parent, Some(name), tag, rf, reason);
        Err(reason)
    }

    fn not(&mut self, parent: usize, name: Option<&[u8]>, tag: u16, rf: u16, reason: &str) {
        let mut u = json!({"tag": tag, "ref": rf, "reason": reason});
        if let Some(n) = name {
            u["name"] = text_json(n);
        }
        self.unpresented.entry(parent).or_default().push(u);
    }

    fn link(&mut self, parent: usize, name: &[u8], tag: u16, rf: u16, path: &str) {
        let n = &mut self.nodes[parent]["own"];
        if n.get("links").is_none() {
            n["links"] = json!([]);
        }
        if let Some(l) = n["links"].as_array_mut() {
            l.push(json!({"name": text_json(name), "tag": tag, "ref": rf, "path": path}));
        }
    }

    fn group(&mut self, name: String, parent: Vec<usize>, attrs: Vec<Attr>, mut own: Map<String, J>) -> usize {
        let path = self.path_of(&parent, &name);
        let (attributes, described, not) = presented_attributes(attrs, None);
        if !described.is_empty() {
            own.insert("attributes".into(), J::Object(described));
        }
        if !not.is_empty() {
            own.insert("unpresented_attributes".into(), J::Object(not));
        }
        self.groups += u64::from(!path.is_empty());
        self.paths.insert(path.clone());
        self.nodes.push(json!({"path": path, "attributes": attributes, "own": own}));
        self.nodes.len() - 1
    }

    /// The writer's table of ref `r` under `parent`: presented at its first visit, a link at a later
    /// one, recorded once when not presented.
    fn table(&mut self, parent: usize, r: u16) {
        if let Some(path) = self.paths_tables.get(&r).cloned() {
            let name = self.table_defs.get(&r).map(|t| t.name.clone()).unwrap_or_default();
            self.link(parent, &name, VH, r, &path);
            return;
        }
        if !self.tables.insert(r) {
            return;
        }
        let Some(t) = self.table_defs.remove(&r) else { return };
        let Table { name, form, attrs } = t;
        match form {
            TableForm::Not(why) => self.not(parent, Some(&name), VH, r, why),
            TableForm::Records { array, own } => {
                let Ok(n) = self.name_in(parent, &name, VH, r) else { return };
                let path = self.array_node(parent, &n, array, own, attrs);
                self.paths_tables.insert(r, path);
            }
            TableForm::Fields { own, arrays } => {
                let Ok(n) = self.name_in(parent, &name, VH, r) else { return };
                let group = self.group(n, vec![parent], attrs, own);
                for (field, array) in arrays {
                    if let Ok(f) = self.name_in(group, &field, VH, r) {
                        self.array_node(group, &f, array, Map::new(), Vec::new());
                    }
                }
                let path = self.nodes[group]["path"].as_str().unwrap_or("").to_string();
                self.paths_tables.insert(r, path);
            }
        }
        // keep the decision, for a link from a later visit
        self.table_defs.insert(r, Table { name, form: TableForm::Not("table"), attrs: Vec::new() });
    }

    /// An array node `name` under `parent` of `array`, its own metadata and attributes: its path.
    fn array_node(&mut self, parent: usize, name: &str, array: J, mut own: Map<String, J>, attrs: Vec<Attr>) -> String {
        let path = self.path_of(&[parent], name);
        let (attributes, described, not) = presented_attributes(attrs, None);
        if !described.is_empty() {
            own.insert("attributes".into(), J::Object(described));
        }
        if !not.is_empty() {
            own.insert("unpresented_attributes".into(), J::Object(not));
        }
        self.arrays += 1;
        self.paths.insert(path.clone());
        self.nodes.push(json!({"path": path, "attributes": attributes, "own": own, "array": array}));
        path
    }

    fn dataset(&mut self, parent: usize, k: usize, var: &Var, decided: &Decided, attrs: &[Attr]) {
        self.reached.insert(k);
        let Decided::Array { array, own, chunks } = decided else {
            if let Decided::Not(why) = decided {
                self.not(parent, Some(&var.name), VG, var.vg, why);
            }
            return;
        };
        let name = match self.name_in(parent, &var.name, VG, var.vg) {
            Ok(name) => name,
            // a name another member of this group holds: the dataset is tried again where the walk
            // reaches it next in another group
            Err("duplicate") => {
                self.retry.entry(k).or_default().insert(parent);
                return;
            }
            Err(_) => return,
        };
        self.retry.remove(&k);
        let path = self.path_of(&[parent], &name);
        let mut own = own.clone();
        let float = array["data_type"].as_str().is_some_and(|t| t.starts_with("float"));
        let (attributes, described, not) = presented_attributes(attrs.iter().map(Attr::clone_ref).collect(), Some(float));
        if !described.is_empty() {
            own.insert("attributes".into(), J::Object(described));
        }
        if !not.is_empty() {
            own.insert("unpresented_attributes".into(), J::Object(not));
        }
        self.arrays += 1;
        self.chunks += chunks;
        self.paths_ds.insert(k, path.clone());
        self.paths.insert(path.clone());
        self.nodes.push(json!({"path": path, "attributes": attributes, "own": own, "array": array}));
    }
}

impl Attr {
    fn clone_ref(&self) -> Attr {
        Attr { name: self.name.clone(), described: self.described.clone(), value: self.value.clone(), raw: self.raw.clone() }
    }
}

/// A node's attributes as presented, each one's described type, and the reasons for those not
/// presented (spec/zarrify/hdf4.md §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#7-attributes>). `float`: of a
/// dataset, whether its type is a float's, for `_FillValue`'s form.
fn presented_attributes(attrs: Vec<Attr>, float: Option<bool>) -> (Map<String, J>, Map<String, J>, Map<String, J>) {
    let (mut presented, mut described, mut not) = (Map::new(), Map::new(), Map::new());
    for a in attrs {
        let name = utf8_or_latin1(&a.name);
        if presented.contains_key(&name) || not.contains_key(&name) {
            continue;
        }
        if !a.described.is_null() {
            described.insert(name.clone(), a.described.clone());
        }
        if RESERVED_ATTRIBUTES.contains(&name.as_str()) {
            not.insert(name, json!("reserved"));
            continue;
        }
        match a.value {
            Ok(mut v) => {
                // `_FillValue` in the form xarray's Zarr backend reads (spec/zarrify/hdf5.md §6
                // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#6-attributes>)
                if let (true, Some(true), Some((c, big, raw))) = (name == "_FillValue", float, &a.raw) {
                    {
                        let x = match c {
                            5 => raw.get(..4).map(|b| f32::from_bits(int_of(b, *big, false) as u32) as f64),
                            6 => raw.get(..8).map(|b| f64::from_bits(int_of(b, *big, false) as u64)),
                            _ => Some(int_of(raw, *big, matches!(c, 20 | 22 | 24)) as f64),
                        };
                        if let Some(x) = x {
                            v = json!(crate::mirror::base64(&x.to_le_bytes()));
                        }
                    }
                }
                presented.insert(name, v);
            }
            Err(why) => {
                not.insert(name, json!(why));
            }
        }
    }
    (presented, described, not)
}

crate::parser!(Hdf4);

/// The HDF4 profile's file run (spec/zarrify/hdf4.md).
pub struct Profile;

impl crate::run::FileProfile for Profile {
    /// The magic number `0e 03 13 01` at byte 0.
    fn detect(&self, head: &[u8]) -> bool {
        head.starts_with(&MAGIC)
    }
    fn parser(&self, size: u64) -> crate::run::AnyParser {
        crate::run::AnyParser::of::<Hdf4>(size)
    }
    fn project(&self, ir: &Ir, facts: &J, url: &str, copied: &crate::project::Copied) -> Result<crate::out::Out, String> {
        crate::project::hdf4::project(ir, facts, url, copied)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A VG element of members, name and class, version 3.
    fn vg(members: &[(u16, u16)], name: &[u8], class: &[u8]) -> Vec<u8> {
        let mut b = (members.len() as u16).to_be_bytes().to_vec();
        for m in members {
            b.extend_from_slice(&m.0.to_be_bytes());
        }
        for m in members {
            b.extend_from_slice(&m.1.to_be_bytes());
        }
        for s in [name, class] {
            b.extend_from_slice(&(s.len() as u16).to_be_bytes());
            b.extend_from_slice(s);
        }
        b.extend_from_slice(&[0, 0, 0, 0, 0, 3, 0, 0, 0]);
        b
    }

    #[test]
    fn vgroups_read_as_the_library_packs_them() {
        let v = parse_vg(&vg(&[(720, 2), (1965, 5)], b"g", b"c")).unwrap();
        assert_eq!((v.members, v.name, v.class, v.version), (vec![(720, 2), (1965, 5)], b"g".to_vec(), b"c".to_vec(), 3));
        // a VG cut short reads as nothing
        let b = vg(&[(720, 2)], b"name", b"");
        assert!(parse_vg(&b[..6]).is_none());
    }

    #[test]
    fn node_names_follow_hdf5s_rule_without_slashes() {
        for (name, ok) in [(&b"a b"[..], true), (b"", false), (b".", false), (b"..", false), (b"__x", false), (b"a/b", false), (b"\xff", false)] {
            assert_eq!(node_name(name).is_some(), ok, "{name:?}");
        }
    }

    #[test]
    fn default_fills_are_netcdfs() {
        assert_eq!(default_fill(20), json!(-127));
        assert_eq!(default_fill(21), json!(129));
        assert_eq!(default_fill(23), json!(32769));
        assert_eq!(default_fill(25), json!(2147483649u64));
        assert_eq!(default_fill(6), json!(9.969_209_968_386_869e36));
        assert_eq!(default_fill(5), json!(9.969_209_968_386_869e36));
    }
}
