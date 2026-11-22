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
use crate::step::Step;
use crate::rules::{self, Rules};
use crate::text::text_json;
use serde_json::{json, Value as J};
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

mod describe;
mod stored;
use stored::{Desc, DescFill, Layout, Method};

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
/// The class of an attribute's Vdata (spec/zarrify/hdf4.md §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#7-attributes>).
pub const ATTRIBUTE: &[u8] = b"Attr0.0";

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
    Other(u16),
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
    Other(u16),
}

/// The Zarr data type and size of a numeric type code (hntdefs.h), and whether it is signed;
/// None for text and the types the SD interface does not read.
pub fn numeric(code: u8) -> Option<(&'static str, u64, bool)> {
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

/// A Zarr fill value or attribute value of one number of `code` in `b`.
pub fn number_json(code: u8, b: &[u8], big: bool) -> J {
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

/// What the parse found, which `finish` turns into the IR.
struct Found {
    blocks: Vec<Block>,
    dds: Vec<Dd>,
    /// the descriptors whose elements were read whole
    held: BTreeSet<usize>,
    /// what each dataset's description is built from
    descs: Vec<Desc>,
}

pub struct Hdf4 {
    size: u64,
    src: Src,
    rules: &'static Rules,
    asked: crate::sans::Asked,
    started: bool,
    found: Option<Found>,
    /// the description of the file (`describe`)
    description: J,
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
            c if c <= SP_LAST && c > 0 => Special::Other(c),
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
            Special::Other(c) => Stored::Other(c),
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
        Hdf4 { size, src: Src::new(size), rules: rules::of("hdf4"), asked: Default::default(), started: false, found: None, description: J::Null }
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
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#92-reading>): the description and
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
        let description = describe::description(&mut p, &vgs, &vhs)?;
        // what each dataset's stored array is built from, in one round
        let (_, vars) = vars_of(&vgs, &vhs);
        let mut descs = Vec::with_capacity(vars.len());
        let mut acc = Acc::default();
        for var in &vars {
            if let Some(d) = acc.take(Self::desc_of(&mut p, var, &vhs)) {
                descs.push(d);
            }
            if acc.stopped() {
                break;
            }
        }
        acc.done()?;
        let Pass { dds, held, .. } = p;
        self.found = Some(Found { blocks, dds, held, descs });
        self.description = description;
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

/// The SD interface's datasets: the `Var0.0` members of the first `CDF0.0` Vgroup, whose ref is
/// given too, each with its members sorted by what they are.
fn vars_of(vgs: &BTreeMap<u16, Vg>, vhs: &BTreeMap<u16, Vh>) -> (Option<u16>, Vec<Var>) {
    let class_of = |r: u16| vgs.get(&r).map(|v| v.class.as_slice());
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
    (cdf, vars)
}

impl Hdf4 {
    /// What the stored array of the dataset `var` is built from ([`Desc`]), as far as the parse
    /// reads it: its declarations, shape, element size, how its data is stored and its fill value.
    /// Of a dataset whose number type and dimensions the SD interface reads, it checks each chunk's
    /// length (spec/zarrify/hdf4.md §9.3
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#93-checks>).
    fn desc_of(p: &mut Pass, var: &Var, vhs: &BTreeMap<u16, Vh>) -> R<Desc> {
        let mut desc = Desc { vg: p.by.get(&(VG, var.vg)).copied(), ..Default::default() };
        // the number type
        let Some(nt) = var.nt.and_then(|r| p.by.get(&(NT, r)).copied()) else { return Ok(desc) };
        let ntb = p.header(nt)?;
        desc.declarations.push(nt);
        let [_, code, _, class, ..] = ntb[..] else { return Ok(desc) };
        // a dataset of text: its bytes, as `uint8`
        let Some((_, item, _)) = (if code == 4 { Some(("uint8", 1, false)) } else { numeric(code) }) else { return Ok(desc) };
        desc.item = Some(item);
        let big = match class {
            _ if item == 1 => true,
            0 | 1 => true,
            4 => false,
            _ => return Ok(desc),
        };
        // the fill value: `_FillValue`'s, else the default, unless a chunk header's (below),
        // whatever the data's storage and whether the parser locates it
        desc.fill = DescFill::FormatDefault;
        Self::described_fill(p, var, vhs, code, item, big, &mut desc)?;
        // the dimensions
        let Some(sdd) = var.sdd.and_then(|r| p.by.get(&(SDD, r)).copied()) else { return Ok(desc) };
        let sb = p.header(sdd)?;
        desc.declarations.push(sdd);
        let mut r = Rd::new(&sb);
        let rank = r.u16().unwrap_or(0) as usize;
        let sizes: Option<Vec<u64>> = (0..rank).map(|_| r.u32().map(|v| v as u64)).collect();
        let Some(mut shape) = sizes else { return Ok(desc) };
        if rank == 0 || rank != var.dims.len() {
            return Ok(desc);
        }
        let unlimited = var.dims[0].1;
        // the shape, unless the records the data holds give an unlimited dimension's size (below)
        if !unlimited {
            desc.shape = Some(shape.clone());
        }
        let stored = match var.sd {
            Some(r) => p.stored(SD, r)?,
            None => Stored::None,
        };
        let count = match &stored {
            Stored::None => 0,
            Stored::Plain(l) => l.len,
            Stored::Compressed { length, .. } => *length,
            Stored::Chunked(i) => match p.special(*i)? {
                Special::Chunked(h) => h.length,
                _ => 0,
            },
            Stored::External | Stored::Other(_) => return Ok(desc),
        };
        if unlimited {
            let record = shape[1..].iter().try_fold(item, |a, &v| a.checked_mul(v)).unwrap_or(0);
            shape[0] = count.checked_div(record).unwrap_or(0);
        }
        let Some(nbytes) = shape.iter().try_fold(item, |a, &v| a.checked_mul(v)) else { return Ok(desc) };
        desc.shape = Some(shape.clone());
        match stored {
            _ if nbytes == 0 => desc.layout = Some(Layout::Whole(None)),
            Stored::None => desc.layout = Some(Layout::Whole(None)),
            Stored::Plain(l) => {
                if l.len >= nbytes {
                    desc.layout = Some(Layout::Whole(Some((l.prefix(nbytes), Method::Plain))));
                }
            }
            Stored::Compressed { length, method, model, data } => {
                if length == 0 {
                    desc.layout = Some(Layout::Whole(None));
                } else if model != 0 {
                } else if method == COMP_NONE {
                    if data.len >= nbytes {
                        desc.layout = Some(Layout::Whole(Some((data.prefix(nbytes), Method::Plain))));
                    }
                } else if method == COMP_DEFLATE {
                    if length == nbytes && data.len > 0 {
                        desc.layout = Some(Layout::Whole(Some((data, Method::Deflate))));
                    }
                } else if length == nbytes && data.len > 0 {
                    // described in the method's `source` codec, though not presented
                    desc.layout = Some(Layout::Whole(Some((data, Method::Other(method)))));
                }
            }
            Stored::Chunked(i) => {
                let Special::Chunked(h) = p.special(i)? else { return Ok(desc) };
                if h.fill.len() as u64 == item {
                    // the header's fill value, after its fields and its dimensions' (`special`)
                    desc.fill = DescFill::At { dd: i, offset: 39 + 12 * h.dims.len() as u64, len: item };
                }
                Self::chunk_layout(p, i, &h, &shape, item, vhs, &mut desc.layout)?;
            }
            Stored::External | Stored::Other(_) => {}
        }
        Ok(desc)
    }

    /// A `_FillValue` the description gives, as the stored array reads it: the place of its bytes,
    /// where it is of the dataset's type (its first byte, of text) and byte order and one element
    /// holds them; else its Vdata header among the declarations and the fill undefined. A dataset
    /// with none, or one of another type, which the SD interface does not read, has its default.
    #[allow(clippy::too_many_arguments)]
    fn described_fill(p: &mut Pass, var: &Var, vhs: &BTreeMap<u16, Vh>, code: u8, item: u64, big: bool, desc: &mut Desc) -> R<()> {
        let Some((&r, vh)) = var.attrs.iter().find_map(|r| vhs.get(r).filter(|h| h.name == b"_FillValue").map(|h| (r, h))) else { return Ok(()) };
        let Some((c, b, raw)) = describe::attribute_raw(p, r, vh)? else { return Ok(()) };
        let kind = |c: u8| numeric(c).map(|n| (n.0, n.1));
        let read = if code == 4 { c == 4 } else { c == code || kind(c).is_some() && kind(c) == kind(code) };
        if !read || (raw.len() as u64) < item {
            return Ok(());
        }
        let first = match p.stored(VS, r)? {
            Stored::Plain(l) => l.segs.first().copied(),
            _ => None,
        };
        desc.fill = match first {
            Some((Some(d), o, n)) if n >= item && (b == big || item == 1) => DescFill::At { dd: d, offset: o - p.dds[d].off, len: item },
            _ => p.by.get(&(VH, r)).map_or(DescFill::Undefined, |&h| DescFill::Declared(h)),
        };
        Ok(())
    }

    /// Where every chunk inside the chunked dataset `i` (header `h`, of `shape` and elements of
    /// `item` bytes) is placed and how it is encoded, `layout`, which describes chunks of any method
    /// and of mixed methods; with each chunk's length checked.
    fn chunk_layout(p: &mut Pass, i: usize, h: &ChunkedHeader, shape: &[u64], item: u64, vhs: &BTreeMap<u16, Vh>, layout: &mut Option<Layout>) -> R<()> {
        let what = p.what(i);
        if h.dims.len() != shape.len() {
            return Ok(());
        }
        let chunk: Vec<u64> = h.dims.iter().map(|d| d.2).collect();
        let Some(chunk_bytes) = chunk.iter().try_fold(item, |a, &v| a.checked_mul(v)) else { return Ok(()) };
        let rows = describe::chunk_rows(p, i, h, vhs)?;
        let mut acc = Acc::default();
        // the chunks the description places, and whether it places every one
        let (mut placed, mut every) = (Vec::new(), true);
        for (at, ct, cr) in rows {
            // a chunk of the unlimited axis past the records lies outside the array
            let outside = at.iter().zip(&chunk).zip(shape).any(|((a, c), s)| a * c >= *s);
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
                    (l.prefix(chunk_bytes), Method::Plain)
                }
                Stored::Compressed { length, method: m, model, data } => {
                    if length != chunk_bytes {
                        return bad(format!("the chunk at {at:?} of {what} decodes to {length} bytes, not {chunk_bytes}"));
                    }
                    if model != 0 || !matches!(m, COMP_DEFLATE | COMP_NONE) {
                        match (model, data.len) {
                            _ if outside => {}
                            (0, 1..) => placed.push((at.clone(), data.clone(), Method::Other(m))),
                            _ => every = false,
                        }
                        continue;
                    }
                    // a stream as long as the chunk (stored) or of a byte at least (deflated)
                    if data.len < if m == COMP_NONE { chunk_bytes } else { 1 } {
                        return bad(format!("the chunk at {at:?} of {what} holds {} bytes of its stream", data.len));
                    }
                    if m == COMP_NONE { (data.prefix(chunk_bytes), Method::Plain) } else { (data, Method::Deflate) }
                }
                Stored::None => return bad(format!("the chunk at {at:?} of {what} names tag {ct} ref {cr}, which no descriptor describes")),
                _ => return Ok(()),
            };
            if !outside {
                placed.push((at, segs, m));
            }
        }
        acc.done()?;
        if every {
            *layout = Some(Layout::Chunked { chunk, rows: placed });
        }
        Ok(())
    }
    /// The finished IR and the description of the file.
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
        // each descriptor's element, for the descriptions
        let mut el: Vec<Option<u32>> = vec![None; found.dds.len()];
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
                el[i] = Some(ir.value(els, &name, d.rf as u64, &ty, 0, (d.off, d.len), Some(get(d.off, d.len)?))?);
            } else {
                el[i] = Some(ir.data(els, &name, d.rf as u64, 0, (d.off, d.len), r#"{"recipe":[["src",0,null]]}"#)?);
            }
        }
        let (mut ir, mut description) = crate::sans::finish(true, ir, self.description)?;
        let (arrays, chains, map) = stored::described(&found.descs, &found.dds, &el)?;
        // each dataset's stored array, by the element that keys it, its Vgroup's ref
        let key_of: HashMap<u64, u32> = arrays.iter().map(|a| (ir.nidx[a.element as usize], a.element)).collect();
        ir.describe(arrays, chains, map)?;
        for d in description["datasets"].as_array_mut().into_iter().flatten() {
            d["element"] = d["vgroup"].as_u64().and_then(|r| key_of.get(&r)).map_or(J::Null, |&e| json!(e));
        }
        self.asked.put(&mut description);
        Ok((ir, description))
    }
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
    fn project(&self, ir: &Ir, description: &J, url: &str, copied: &crate::project::Copied) -> Result<crate::out::Out, String> {
        crate::project::hdf4::project(ir, description, url, copied)
    }
    /// The summary holds the parser's `rounds`.
    fn summary_counts(&self) -> &'static [&'static str] {
        &["rounds"]
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
}
