//! HDF5 metadata, read sans-IO (spec/virtualize/ims.md §8.1
//! <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/ims.md#81-hdf5-structures>–§8.6
//! <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/ims.md#86-attributes>): the superblock,
//! object headers and their messages, the links of groups (symbol tables, compact and dense
//! links), version 1 and 2 B-trees, local, fractal and global heaps, attributes, datasets and
//! their chunk indexes (version 1 B-tree, single chunk, implicit, fixed array, extensible array,
//! version 2 B-tree). Pixel data is never read and checksums are not checked. The module is
//! format-free: IMS (`ims.rs`) is one user of it.
//!
//! **Sans-IO by replay.** A reader never blocks: [`Src::read`] gives the bytes the host fed,
//! or records the range as wanted and fails with [`Fail::Miss`]. A computation over the file
//! is run again from the start once the wanted ranges are fed (a *round*); every structure
//! parsed is memoized by its address (a success, or its rejection: both are functions of the
//! file), so a round costs little more than what is new in it. A computation over several
//! independent things (a B-tree's children, the datasets of an image) goes on past a miss to
//! the next thing ([`Acc`]), so that one round asks for all of them: the number of rounds is
//! the depth of the file's pointer chains, not the number of structures. A failure decided by
//! bytes ([`Fail::Bad`]) is final; with misses before it, the round is retried (the outcome
//! is the one a sequential reading would give).

use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::Arc as Rc;

pub const SIGNATURE: &[u8; 8] = b"\x89HDF\r\n\x1a\n";
pub const UNDEFINED: u64 = u64::MAX;
pub const MAX_SAFE: u64 = (1 << 53) - 1;
/// Blocks of one object header (§8.2).
pub const MAX_BLOCKS: usize = 1000;
pub const MAX_BTREE2_DEPTH: u64 = 16;
/// The least a missed read asks for (a window, so that the structure's next bytes come with it).
pub const WINDOW: u64 = 1024;

// Message types (§8.2).
pub const NIL: u16 = 0x00;
pub const DATASPACE: u16 = 0x01;
pub const LINK_INFO: u16 = 0x02;
pub const DATATYPE: u16 = 0x03;
pub const FILL_VALUE: u16 = 0x05;
pub const LINK: u16 = 0x06;
pub const EXTERNAL_FILES: u16 = 0x07;
pub const LAYOUT: u16 = 0x08;
pub const FILTERS: u16 = 0x0B;
pub const ATTRIBUTE: u16 = 0x0C;
pub const CONTINUATION: u16 = 0x10;
pub const SYMBOL_TABLE: u16 = 0x11;
pub const ATTRIBUTE_INFO: u16 = 0x15;

#[derive(Debug, Clone, PartialEq)]
pub enum Fail {
    /// bytes not fed yet (they are wanted)
    Miss,
    /// the file breaks a rule
    Bad(String),
}

pub type R<T> = Result<T, Fail>;

pub fn bad<T>(m: impl Into<String>) -> R<T> {
    Err(Fail::Bad(m.into()))
}

/// The outcome of a sequence of independent computations, as a sequential reading would
/// give it: the first failure in order (a miss before a rejection is a miss: the round is
/// retried), and every miss collected meanwhile.
#[derive(Default, Debug)]
pub struct Acc {
    first: Option<Fail>,
}

impl Acc {
    pub fn take<T>(&mut self, r: R<T>) -> Option<T> {
        match r {
            Ok(v) => Some(v),
            Err(f) => {
                if self.first.is_none() {
                    self.first = Some(f);
                }
                None
            }
        }
    }
    /// A rejection came first: nothing after it matters.
    pub fn stopped(&self) -> bool {
        matches!(self.first, Some(Fail::Bad(_)))
    }
    pub fn missed(&self) -> bool {
        matches!(self.first, Some(Fail::Miss))
    }
    pub fn done(self) -> R<()> {
        match self.first {
            None => Ok(()),
            Some(f) => Err(f),
        }
    }
}

// ---- the bytes fed

/// Fed bytes, ranges that touch merged into one, so that a read finds its bytes in the one
/// range at or before it.
#[derive(Default)]
pub struct Store {
    got: BTreeMap<u64, Vec<u8>>,
    pub held: u64,
}

impl Store {
    pub fn feed(&mut self, o: u64, data: Vec<u8>) {
        if data.is_empty() {
            return;
        }
        let end = o + data.len() as u64;
        let mut lo = o;
        let mut hi = end;
        let mut old: Vec<u64> = Vec::new();
        for (&s, b) in self.got.range(..=end).rev() {
            let e = s + b.len() as u64;
            if e < o {
                break;
            }
            lo = lo.min(s);
            hi = hi.max(e);
            old.push(s);
        }
        if old.len() == 1 && lo == old[0] && hi == old[0] + self.got[&old[0]].len() as u64 {
            return; // already held
        }
        let mut buf = vec![0u8; (hi - lo) as usize];
        for s in old.iter().rev() {
            let b = self.got.remove(s).unwrap();
            self.held -= b.len() as u64;
            buf[(s - lo) as usize..(s - lo) as usize + b.len()].copy_from_slice(&b);
        }
        buf[(o - lo) as usize..(end - lo) as usize].copy_from_slice(&data);
        self.held += buf.len() as u64;
        self.got.insert(lo, buf);
    }

    pub fn get(&self, o: u64, n: u64) -> Option<&[u8]> {
        let end = o.checked_add(n)?;
        let (&s, b) = self.got.range(..=o).next_back()?;
        if end <= s + b.len() as u64 {
            Some(&b[(o - s) as usize..(end - s) as usize])
        } else {
            None
        }
    }
}

/// A file read sans-IO: what was fed, and what a round wants.
pub struct Src {
    pub size: u64,
    pub store: Store,
    pub want: Vec<(u64, u64)>,
}

impl Src {
    pub fn new(size: u64) -> Src {
        Src { size, store: Store::default(), want: Vec::new() }
    }

    /// The `n` bytes at `at`, which MUST lie within the file (§8.1).
    pub fn read(&mut self, at: u64, n: u64) -> R<Vec<u8>> {
        match at.checked_add(n) {
            Some(e) if e <= self.size => {}
            _ => return bad(format!("read of {n} bytes at {at}, outside the {}-byte file", self.size)),
        }
        if n == 0 {
            return Ok(Vec::new());
        }
        if let Some(b) = self.store.get(at, n) {
            return Ok(b.to_vec());
        }
        self.want.push((at, n.max(WINDOW).min(self.size - at)));
        Err(Fail::Miss)
    }

    pub fn within(&self, at: u64, n: u64) -> bool {
        at.checked_add(n).is_some_and(|e| e <= self.size)
    }
}

// ---- fields

/// The little-endian unsigned integer of `n` (at most 8) bytes at `at` of `d`.
pub fn le(d: &[u8], at: usize, n: usize) -> R<u64> {
    if at.checked_add(n).is_none_or(|e| e > d.len()) {
        return bad("truncated HDF5 structure");
    }
    let mut v = 0u64;
    for k in (0..n.min(8)).rev() {
        v = (v << 8) | d[at + k] as u64;
    }
    Ok(v)
}

/// An address: None if undefined (all bits set), else at most 2^53 − 1.
pub fn address(v: u64) -> R<Option<u64>> {
    if v == UNDEFINED {
        return Ok(None);
    }
    if v > MAX_SAFE {
        return bad(format!("HDF5 address {v} is too large"));
    }
    Ok(Some(v))
}

pub fn length(v: u64) -> R<u64> {
    if v > MAX_SAFE {
        return bad(format!("HDF5 length {v} is too large"));
    }
    Ok(v)
}

pub fn log2(v: u64) -> u64 {
    63 - v.max(1).leading_zeros() as u64
}

fn power(v: u64) -> bool {
    v >= 1 && v & (v - 1) == 0
}

pub fn pad8(n: u64) -> u64 {
    n.div_ceil(8) * 8
}

pub fn product(v: &[u64]) -> u128 {
    v.iter().fold(1u128, |a, &x| a.saturating_mul(x as u128))
}

/// Coordinates as messages give them: `[1, 2]`.
pub fn coords_text(v: &[u64]) -> String {
    format!("[{}]", v.iter().map(|x| x.to_string()).collect::<Vec<_>>().join(", "))
}

pub fn unravel(mut i: u128, grid: &[u64]) -> Vec<u64> {
    let mut out = vec![0; grid.len()];
    for k in (0..grid.len()).rev() {
        let g = grid[k].max(1) as u128;
        out[k] = (i % g) as u64;
        i /= g;
    }
    out
}

pub fn ravel(coords: &[u64], grid: &[u64]) -> u128 {
    coords.iter().zip(grid).fold(0u128, |i, (&c, &g)| i.saturating_mul(g as u128).saturating_add(c as u128))
}

// ---- message bodies

#[derive(Debug, Clone)]
pub struct Datatype {
    /// 0 fixed-point, 1 floating-point, 3 string, ...
    pub cls: u8,
    pub size: u64,
    /// the class bit fields
    pub bits: u32,
    pub props: Vec<u8>,
}

pub fn parse_datatype(d: &[u8]) -> R<Datatype> {
    let cv = le(d, 0, 1)?;
    if !(1..=5).contains(&(cv >> 4)) {
        return bad(format!("unsupported datatype message version {}", cv >> 4));
    }
    Ok(Datatype { cls: (cv & 15) as u8, size: le(d, 4, 4)?, bits: le(d, 1, 3)? as u32, props: d.get(8..).unwrap_or(&[]).to_vec() })
}

#[derive(Debug, Clone)]
pub struct Dataspace {
    /// None for a null dataspace; empty for a scalar
    pub dims: Option<Vec<u64>>,
    pub maxdims: Option<Vec<u64>>,
}

pub fn parse_dataspace(d: &[u8]) -> R<Dataspace> {
    let (version, rank, flags) = (le(d, 0, 1)?, le(d, 1, 1)?, le(d, 2, 1)?);
    let (kind, p) = match version {
        1 => (if rank > 0 { 1 } else { 0 }, 8usize),
        2 => {
            let kind = le(d, 3, 1)?;
            if kind > 2 || (kind != 1 && rank != 0) {
                return bad(format!("invalid dataspace type {kind}"));
            }
            (kind, 4)
        }
        _ => return bad(format!("unsupported dataspace message version {version}")),
    };
    let rank = rank as usize;
    let mut dims = Vec::with_capacity(rank);
    for i in 0..rank {
        dims.push(length(le(d, p + 8 * i, 8)?)?);
    }
    let maxdims = if flags & 1 != 0 {
        let mut m = Vec::with_capacity(rank);
        for i in 0..rank {
            m.push(le(d, p + 8 * (rank + i), 8)?);
        }
        Some(m)
    } else {
        None
    };
    Ok(Dataspace { dims: if kind == 2 { None } else { Some(dims) }, maxdims })
}

/// The filter identifiers of a filter pipeline message, in pipeline order.
pub fn parse_filters(d: Option<&[u8]>) -> R<Vec<u64>> {
    let Some(d) = d else { return Ok(Vec::new()) };
    let (version, count) = (le(d, 0, 1)?, le(d, 1, 1)?);
    if version != 1 && version != 2 {
        return bad(format!("unsupported filter pipeline message version {version}"));
    }
    let mut p: u64 = if version == 1 { 8 } else { 2 };
    let mut ids = Vec::new();
    for _ in 0..count {
        let fid = le(d, p as usize, 2)?;
        let name_length;
        if version == 1 || fid >= 256 {
            name_length = le(d, p as usize + 2, 2)?;
            p += 4;
        } else {
            name_length = 0;
            p += 2;
        }
        let values = le(d, p as usize + 2, 2)?;
        p += 4;
        p += if version == 1 { pad8(name_length) } else { name_length };
        p += 4 * values + if version == 1 && values % 2 == 1 { 4 } else { 0 };
        if p > d.len() as u64 {
            return bad("truncated filter pipeline message");
        }
        ids.push(fid);
    }
    Ok(ids)
}

/// The fill value of a fill value message, or no bytes when it gives none.
pub fn fill_value(d: &[u8]) -> R<Vec<u8>> {
    let version = le(d, 0, 1)?;
    let (size, p) = match version {
        1 | 2 => {
            if version == 2 && le(d, 3, 1)? == 0 {
                return Ok(Vec::new());
            }
            (le(d, 4, 4)?, 8u64)
        }
        3 => {
            if le(d, 1, 1)? & 0x20 == 0 {
                return Ok(Vec::new());
            }
            (le(d, 2, 4)?, 6)
        }
        _ => return bad(format!("unsupported fill value message version {version}")),
    };
    if p + size > d.len() as u64 {
        return bad("truncated fill value message");
    }
    Ok(d[p as usize..(p + size) as usize].to_vec())
}

#[derive(Debug, Clone, PartialEq)]
pub enum Target {
    Hard(u64),
    Soft(Vec<u8>),
    /// any other type: (type, value, or None when it does not lie within the message)
    Other(u8, Option<Vec<u8>>),
}

/// A link message: (name, target) (§8.3).
pub fn parse_link(d: &[u8]) -> R<(Vec<u8>, Target)> {
    if le(d, 0, 1)? != 1 {
        return bad("unsupported link message version");
    }
    let flags = le(d, 1, 1)?;
    let mut p = 2usize;
    let mut kind = 0u64;
    if flags & 8 != 0 {
        kind = le(d, p, 1)?;
        p += 1;
    }
    p += if flags & 4 != 0 { 8 } else { 0 } + if flags & 16 != 0 { 1 } else { 0 };
    let n = 1usize << (flags & 3);
    let name_length = le(d, p, n)? as usize;
    p += n;
    if p.checked_add(name_length).is_none_or(|e| e > d.len()) {
        return bad("truncated link message");
    }
    let name = d[p..p + name_length].to_vec();
    let q = p + name_length;
    if kind == 1 {
        let m = le(d, q, 2)? as usize;
        if q + 2 + m > d.len() {
            return bad("truncated soft link");
        }
        return Ok((name, Target::Soft(d[q + 2..q + 2 + m].to_vec())));
    }
    if kind != 0 {
        let v = if q + 2 <= d.len() {
            let m = le(d, q, 2)? as usize;
            (q + 2 + m <= d.len()).then(|| d[q + 2..q + 2 + m].to_vec())
        } else {
            None
        };
        return Ok((name, Target::Other(kind as u8, v)));
    }
    match address(le(d, q, 8)?)? {
        Some(t) => Ok((name, Target::Hard(t))),
        None => bad("hard link to an undefined address"),
    }
}

/// (flags, name size, datatype size, dataspace size, name start) of an attribute message.
fn attribute_layout(d: &[u8]) -> R<(u64, u64, u64, u64, u64)> {
    let version = le(d, 0, 1)?;
    if !(1..=3).contains(&version) {
        return bad(format!("unsupported attribute message version {version}"));
    }
    let flags = if version > 1 { le(d, 1, 1)? } else { 0 };
    Ok((flags, le(d, 2, 2)?, le(d, 4, 2)?, le(d, 6, 2)?, if version == 3 { 9 } else { 8 }))
}

pub fn attribute_name(d: &[u8]) -> R<Vec<u8>> {
    let (_, name_size, _, _, p) = attribute_layout(d)?;
    if p + name_size > d.len() as u64 {
        return bad("truncated attribute message");
    }
    let raw = &d[p as usize..(p + name_size) as usize];
    Ok(raw.split(|&c| c == 0).next().unwrap_or(&[]).to_vec())
}

/// Where an attribute message's parts lie in it: its prelude's length, name, datatype,
/// dataspace (each an offset and its size), and the data's offset.
#[derive(Debug, Clone)]
pub struct AttrParts {
    pub flags: u64,
    pub name: (u64, u64),
    pub datatype: (u64, u64),
    pub dataspace: (u64, u64),
    pub data: u64,
}

pub fn attribute_parts_at(d: &[u8]) -> R<AttrParts> {
    let (flags, name_size, type_size, space_size, p) = attribute_layout(d)?;
    let v1 = le(d, 0, 1)? == 1;
    let pad = |n: u64| if v1 { pad8(n) } else { n };
    let name = (p, name_size);
    let t = p + pad(name_size);
    let s = t + pad(type_size);
    let data = s + pad(space_size);
    if data > d.len() as u64 || t + type_size > d.len() as u64 || s + space_size > d.len() as u64 {
        return bad("truncated attribute message");
    }
    Ok(AttrParts { flags, name, datatype: (t, type_size), dataspace: (s, space_size), data })
}

/// An attribute's (datatype message, dimensions, data) (§8.6): its flag bits 0 and 1
/// clear, its dataspace not null, its data within the message.
pub fn attribute_parts(d: &[u8]) -> R<(Vec<u8>, Vec<u64>, Vec<u8>)> {
    let (flags, name_size, type_size, space_size, mut p) = attribute_layout(d)?;
    if flags & 3 != 0 {
        return bad("attribute with a shared datatype or dataspace");
    }
    let v1 = le(d, 0, 1)? == 1;
    let pad = |n: u64| if v1 { pad8(n) } else { n };
    p += pad(name_size);
    if p + type_size > d.len() as u64 {
        return bad("truncated attribute message");
    }
    let q = p;
    let dt = parse_datatype(&d[p as usize..(p + type_size) as usize])?;
    p += pad(type_size);
    if p + space_size > d.len() as u64 {
        return bad("truncated attribute message");
    }
    let space = parse_dataspace(&d[p as usize..(p + space_size) as usize])?;
    p += pad(space_size);
    let Some(dims) = space.dims else { return bad("attribute with a null dataspace") };
    let n = product(&dims).saturating_mul(dt.size as u128);
    if p as u128 + n > d.len() as u128 {
        return bad("truncated attribute data");
    }
    Ok((d[q as usize..(q + type_size) as usize].to_vec(), dims, d[p as usize..(p as u128 + n) as usize].to_vec()))
}

/// An attribute's (datatype, element count, data) (§8.6).
pub fn attribute_value(d: &[u8]) -> R<(Datatype, u128, Vec<u8>)> {
    let (t, dims, data) = attribute_parts(d)?;
    Ok((parse_datatype(&t)?, product(&dims), data))
}

// ---- parsed structures

#[derive(Debug, Clone)]
pub struct Msg {
    pub ty: u16,
    pub flags: u8,
    /// the message header's offset in the file
    pub head: u64,
    /// the data's offset in the file
    pub at: u64,
    pub data: Vec<u8>,
}

/// An object header (§8.2): its blocks and messages, as the file lays them out.
#[derive(Debug, Clone)]
pub struct Header {
    pub at: u64,
    pub v2: bool,
    pub flags: u8,
    /// the bytes from `at` before the first block (v1: 16; v2: signature to the first block's size)
    pub prefix: u64,
    /// each block's messages area (offset, length); a version 2 continuation block has `OCHK`
    /// before it and a checksum after it, as the first block has a checksum after it
    pub blocks: Vec<(u64, u64)>,
    /// every message, continuation messages included, in order
    pub msgs: Vec<Msg>,
    /// the size of a message header
    pub mh: u64,
}

impl Header {
    /// The messages the sections read: every one but continuations.
    pub fn messages(&self) -> impl Iterator<Item = &Msg> {
        self.msgs.iter().filter(|m| m.ty != CONTINUATION)
    }

    /// The data of the one message of type `typ` (§8.2).
    pub fn message(&self, typ: u16, required: bool) -> R<Option<&[u8]>> {
        let found: Vec<&Msg> = self.messages().filter(|m| m.ty == typ).collect();
        if found.len() > 1 {
            return bad(format!("object header has {} messages of type {typ}", found.len()));
        }
        let Some(m) = found.first() else {
            if required {
                return bad(format!("object header has no message of type {typ}"));
            }
            return Ok(None);
        };
        if m.flags & 2 != 0 {
            return bad(format!("shared message of type {typ}"));
        }
        Ok(Some(&m.data))
    }

    pub fn has(&self, typ: u16) -> bool {
        self.messages().any(|m| m.ty == typ)
    }
}

#[derive(Debug, Clone)]
pub struct LocalHeap {
    pub at: u64,
    pub data_at: u64,
    pub data: Vec<u8>,
}

/// A version 1 B-tree node: (type, level, entries, the body after the 24-byte header).
#[derive(Debug, Clone)]
struct Node1 {
    ty: u8,
    level: u8,
    entries: u64,
    body: Vec<u8>,
}

/// A version 1 B-tree, read: its nodes (offset, length) and its leaves' (key, child).
#[derive(Debug, Clone, Default)]
pub struct Bt1 {
    pub nodes: Vec<(u64, u64)>,
    pub leaves: Vec<(Vec<u8>, u64)>,
}

/// A version 2 B-tree, read: header at `at` (38 bytes), its nodes (offset, length with the
/// checksum), its records in order.
#[derive(Debug, Clone, Default)]
pub struct Bt2 {
    pub at: u64,
    pub nodes: Vec<(u64, u64)>,
    pub records: Vec<Vec<u8>>,
}

#[derive(Debug, Clone)]
pub enum LinkStorage {
    /// old-style: the B-tree's nodes, the local heap and the symbol table nodes (offset, length)
    Symtab { heap: LocalHeap, nodes: Vec<(u64, u64)>, snods: Vec<(u64, u64)> },
    Compact,
    /// new-style dense: the fractal heap and the name index; each link's object (offset, length)
    Dense { heap: u64, index: Bt2, objects: Vec<(u64, u64)> },
}

#[derive(Debug, Clone)]
pub struct Links {
    pub list: Vec<(Vec<u8>, Target)>,
    pub by_name: HashMap<Vec<u8>, usize>,
    pub storage: LinkStorage,
}

impl Links {
    pub fn get(&self, name: &[u8]) -> Option<&Target> {
        self.by_name.get(name).map(|&i| &self.list[i].1)
    }
    pub fn has(&self, name: &str) -> bool {
        self.by_name.contains_key(name.as_bytes())
    }
}

/// An attribute: its name, its message's bytes, where the file holds them, and whether it
/// is an object of the dense heap (else a message of the header).
#[derive(Debug, Clone)]
pub struct Attr {
    pub name: Vec<u8>,
    pub data: Vec<u8>,
    pub at: u64,
    pub dense: bool,
}

#[derive(Debug, Clone, Default)]
pub struct Attrs {
    pub list: Vec<Attr>,
    pub by_name: HashMap<Vec<u8>, usize>,
    /// dense attributes: the fractal heap and the name index
    pub dense: Option<(u64, Bt2)>,
}

impl Attrs {
    pub fn get(&self, name: &str) -> Option<&[u8]> {
        self.by_name.get(name.as_bytes()).map(|&i| self.list[i].data.as_slice())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Index {
    Btree1,
    Single,
    Implicit,
    Farray,
    Earray,
    Btree2,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Layout {
    Compact,
    Contiguous,
    Chunked,
    Null,
    Virtual,
}

#[derive(Debug, Clone)]
pub struct Dataset {
    pub dims: Vec<u64>,
    pub maxdims: Option<Vec<u64>>,
    pub datatype: Datatype,
    pub chunk: Vec<u64>,
    pub grid: Vec<u64>,
    pub filters: Vec<u64>,
    pub index: Option<Index>,
    pub index_address: Option<u64>,
    pub single_size: Option<u64>,
    pub single_mask: u32,
    pub layout: Layout,
    /// a compact dataset's data: (its offset in the file, its bytes)
    pub compact: (u64, Vec<u8>),
    /// a contiguous dataset's (address or None if unallocated, size)
    pub contiguous: (Option<u64>, u64),
    pub fill: Option<Vec<u8>>,
    pub named: Option<u64>,
    pub raw_edges: bool,
}

/// A dataset's allocated chunks, and the structures of its chunk index.
#[derive(Debug, Clone, Default)]
pub struct Chunks {
    /// (grid coordinates, address, size, filter mask), in index order
    pub list: Vec<(Vec<u64>, u64, u64, u32)>,
    /// the index's structures: (what, offset, length)
    pub parts: Vec<(&'static str, u64, u64)>,
}

/// A fractal heap's parameters (§8.4).
#[derive(Debug, Clone)]
pub struct Fheap {
    pub at: u64,
    pub id_length: u64,
    pub width: u64,
    pub start: u64,
    pub max_direct: u64,
    pub root: Option<u64>,
    pub root_rows: u64,
    pub offset_size: u64,
    pub length_size: u64,
    pub direct_rows: u64,
    pub huge_tree: Option<u64>,
    /// direct blocks carry a checksum (flags bit 1)
    pub checksummed: bool,
}

/// A global heap collection: its size and objects (index -> (offset of the object's
/// 16-byte header, data length)), and every object header in order.
#[derive(Debug, Clone)]
pub struct Collection {
    pub at: u64,
    pub size: u64,
    pub objects: BTreeMap<u64, (u64, u64)>,
    /// (index, offset, data length); index 0 is the free space
    pub order: Vec<(u64, u64, u64)>,
}

#[derive(Debug, Clone)]
pub struct Super {
    pub version: u8,
    /// the superblock's length (as far as the file holds it)
    pub len: u64,
    pub root: u64,
    pub extension: Option<u64>,
}

type Memo<K, T> = HashMap<K, Result<Rc<T>, String>>;

fn memo_get<K: std::hash::Hash + Eq, T>(m: &Memo<K, T>, k: &K) -> Option<R<Rc<T>>> {
    m.get(k).map(|r| r.clone().map_err(Fail::Bad))
}

fn memo_put<K: std::hash::Hash + Eq, T>(m: &mut Memo<K, T>, k: K, r: R<T>) -> R<Rc<T>> {
    match r {
        Ok(v) => {
            let v = Rc::new(v);
            m.insert(k, Ok(v.clone()));
            Ok(v)
        }
        Err(Fail::Bad(s)) => {
            m.insert(k, Err(s.clone()));
            Err(Fail::Bad(s))
        }
        Err(Fail::Miss) => Err(Fail::Miss),
    }
}

type AddChunk<'a> = dyn FnMut(&mut Chunks, Vec<u64>, u64, u64, u32) -> R<()> + 'a;

/// An HDF5 file read sans-IO, its structures memoized by address.
pub struct H5 {
    pub src: Src,
    pub sb: Option<Super>,
    headers: Memo<u64, Header>,
    links: Memo<u64, Links>,
    attrs: Memo<u64, Attrs>,
    lheaps: Memo<u64, LocalHeap>,
    nodes1: Memo<(u64, u64), Node1>,
    nodes2: Memo<(u64, u64, u64), Vec<u8>>,
    bt2: Memo<(u64, u8), Bt2>,
    fheaps: Memo<u64, Fheap>,
    fblocks: Memo<(u64, u64, u64), ()>,
    huge: Memo<u64, (HashMap<u64, (u64, u64)>, Bt2)>,
    collections: Memo<u64, Collection>,
    chunks: Memo<u64, Chunks>,
    /// each fractal heap's blocks read: heap -> block -> (indirect, length)
    pub fh_blocks: HashMap<u64, BTreeMap<u64, (bool, u64)>>,
}

impl H5 {
    pub fn new(size: u64) -> H5 {
        H5 {
            src: Src::new(size),
            sb: None,
            headers: HashMap::new(),
            links: HashMap::new(),
            attrs: HashMap::new(),
            lheaps: HashMap::new(),
            nodes1: HashMap::new(),
            nodes2: HashMap::new(),
            bt2: HashMap::new(),
            fheaps: HashMap::new(),
            fblocks: HashMap::new(),
            huge: HashMap::new(),
            collections: HashMap::new(),
            chunks: HashMap::new(),
            fh_blocks: HashMap::new(),
        }
    }

    pub fn size(&self) -> u64 {
        self.src.size
    }

    pub fn read(&mut self, at: u64, n: u64) -> R<Vec<u8>> {
        self.src.read(at, n)
    }

    // ---- the superblock (§8.1)

    pub fn superblock(&mut self) -> R<Super> {
        if let Some(s) = &self.sb {
            return Ok(s.clone());
        }
        let head = self.read(0, 9.min(self.size()))?;
        if head.len() < 9 || head[..8] != SIGNATURE[..] {
            return bad("not an HDF5 file");
        }
        let version = head[8];
        let (sb, base_at, sizes, root_at, len) = match version {
            0 | 1 => {
                let n = if version == 1 { 76 } else { 72 };
                let sb = self.read(0, n)?;
                let base_at = if version == 1 { 28 } else { 24 };
                (sb.clone(), base_at, (sb[13], sb[14]), base_at + 40, n + 24)
            }
            2 | 3 => {
                let sb = self.read(0, 48)?;
                (sb.clone(), 12, (sb[9], sb[10]), 36, 48)
            }
            _ => return bad(format!("unsupported HDF5 superblock version {version}")),
        };
        if sizes != (8, 8) {
            return bad(format!("HDF5 offsets and lengths of {} and {} bytes are not supported", sizes.0, sizes.1));
        }
        if le(&sb, base_at, 8)? != 0 {
            return bad("HDF5 base address is not 0");
        }
        let Some(root) = address(le(&sb, root_at, 8)?)? else { return bad("no HDF5 root group") };
        let extension = if version >= 2 { address(le(&sb, 20, 8)?).ok().flatten() } else { None };
        let s = Super { version, len: len.min(self.size()), root, extension };
        self.sb = Some(s.clone());
        Ok(s)
    }

    // ---- object headers (§8.2)

    pub fn header(&mut self, at: u64) -> R<Rc<Header>> {
        if let Some(r) = memo_get(&self.headers, &at) {
            return r;
        }
        let r = self.read_header(at);
        memo_put(&mut self.headers, at, r)
    }

    fn read_header(&mut self, at: u64) -> R<Header> {
        let sig = self.read(at, 4)?;
        let (mut blocks, v2, mh, flags, prefix);
        if sig == b"OHDR" {
            let p6 = self.read(at, 6)?;
            if p6[4] != 2 {
                return bad(format!("unsupported object header version {}", p6[4]));
            }
            flags = p6[5];
            let p = at + 6 + if flags & 0x20 != 0 { 16 } else { 0 } + if flags & 0x10 != 0 { 4 } else { 0 };
            let n = 1u64 << (flags & 3);
            let first = le(&self.read(p, n)?, 0, n as usize)?;
            if first.checked_add(4).is_none() {
                return bad("object header block outside the file");
            }
            self.read(p + n, first + 4)?;
            blocks = vec![(p + n, first)];
            v2 = true;
            mh = if flags & 4 != 0 { 6 } else { 4 };
            prefix = p + n - at;
        } else {
            let pre = self.read(at, 16)?;
            if pre[0] != 1 {
                return bad(format!("no object header at {at}"));
            }
            blocks = vec![(at + 16, le(&pre, 8, 4)?)];
            v2 = false;
            mh = 8;
            flags = 0;
            prefix = 16;
        }
        let mut msgs = Vec::new();
        let mut seen: HashSet<u64> = HashSet::from([blocks[0].0]);
        let mut i = 0;
        while i < blocks.len() {
            let (start, n) = blocks[i];
            i += 1;
            let data = self.read(start, n)?;
            let mut pos = 0u64;
            while pos < n {
                if n - pos < mh {
                    if v2 {
                        break; // a gap
                    }
                    return bad("truncated object header message");
                }
                let (typ, size, mflags) = if v2 {
                    (data[pos as usize] as u16, le(&data, pos as usize + 1, 2)?, data[pos as usize + 3])
                } else {
                    (le(&data, pos as usize, 2)? as u16, le(&data, pos as usize + 2, 2)?, data[pos as usize + 4])
                };
                let body = pos + mh;
                if body + size > n {
                    return bad("object header message runs past its block");
                }
                let d = data[body as usize..(body + size) as usize].to_vec();
                pos = body + size;
                let is_cont = typ == CONTINUATION;
                let cont = if is_cont { Some(d.clone()) } else { None };
                msgs.push(Msg { ty: typ, flags: mflags, head: start + body - mh, at: start + body, data: d });
                let Some(d) = cont else { continue };
                if mflags & 2 != 0 {
                    return bad("shared continuation message");
                }
                let to = address(le(&d, 0, 8)?)?;
                let ln = length(le(&d, 8, 8)?)?;
                let Some(to) = to else { return bad("continuation to an undefined address") };
                let block = if v2 {
                    if ln < 8 || self.read(to, 4)? != b"OCHK" {
                        return bad(format!("no continuation block at {to}"));
                    }
                    self.read(to, ln)?;
                    (to + 4, ln - 8)
                } else {
                    (to, ln)
                };
                if !seen.insert(block.0) {
                    return bad(format!("object header block at {to} read twice"));
                }
                if blocks.len() >= MAX_BLOCKS {
                    return bad("too many object header blocks");
                }
                blocks.push(block);
            }
        }
        Ok(Header { at, v2, flags, prefix, blocks, msgs, mh })
    }

    /// The datatype message of the committed datatype that a shared datatype message leads
    /// to, and its object header (§8.9).
    pub fn committed(&mut self, shared: &[u8]) -> R<(Vec<u8>, u64)> {
        let version = le(shared, 0, 1)?;
        let at = if version == 1 {
            address(le(shared, 8, 8)?)?
        } else if version == 2 || (version == 3 && le(shared, 1, 1)? == 2) {
            address(le(shared, 2, 8)?)?
        } else {
            return bad("a datatype shared other than as a committed datatype");
        };
        let Some(at) = at else { return bad("a shared datatype at an undefined address") };
        let h = self.header(at)?;
        let m = h.message(DATATYPE, true)?.unwrap().to_vec();
        Ok((m, at))
    }

    /// An object header's datatype message, through a committed datatype when shared.
    pub fn datatype(&mut self, h: &Header) -> R<(Vec<u8>, Option<u64>)> {
        let found: Vec<&Msg> = h.messages().filter(|m| m.ty == DATATYPE).collect();
        if found.len() != 1 {
            return bad(format!("object header has {} messages of type {DATATYPE}", found.len()));
        }
        if found[0].flags & 2 != 0 {
            let d = found[0].data.clone();
            let (m, at) = self.committed(&d)?;
            return Ok((m, Some(at)));
        }
        Ok((found[0].data.clone(), None))
    }

    // ---- groups (§8.3)

    pub fn links(&mut self, at: u64) -> R<Rc<Links>> {
        if let Some(r) = memo_get(&self.links, &at) {
            return r;
        }
        let r = self.read_links(at);
        memo_put(&mut self.links, at, r)
    }

    fn read_links(&mut self, at: u64) -> R<Links> {
        let h = self.header(at)?;
        let table = h.message(SYMBOL_TABLE, false)?.map(|d| d.to_vec());
        let info = h.message(LINK_INFO, false)?.map(|d| d.to_vec());
        if table.is_some() && info.is_some() {
            return bad("group has both a symbol table and link info");
        }
        let mut list: Vec<(Vec<u8>, Target)> = Vec::new();
        let mut by_name: HashMap<Vec<u8>, usize> = HashMap::new();
        let mut add = |name: Vec<u8>, t: Target, list: &mut Vec<(Vec<u8>, Target)>| -> R<()> {
            if by_name.contains_key(&name) {
                return bad(format!("group has two links named {}", show_bytes(&name)));
            }
            by_name.insert(name.clone(), list.len());
            list.push((name, t));
            Ok(())
        };
        let storage;
        if let Some(table) = table {
            let (btree, heap) = (address(le(&table, 0, 8)?)?, address(le(&table, 8, 8)?)?);
            let (Some(btree), Some(heap)) = (btree, heap) else { return bad("symbol table with an undefined address") };
            // the heap and the tree are independent reads
            let mut acc = Acc::default();
            let names = acc.take(self.local_heap(heap));
            let tree = acc.take(self.btree1(btree, 0, 8));
            acc.done()?;
            let (names, tree) = (names.unwrap(), tree.unwrap());
            let mut snods = Vec::new();
            let mut acc = Acc::default();
            let mut entries_of = Vec::new();
            for &(_, snod) in &tree.leaves {
                let r = (|| -> R<(u64, Vec<u8>)> {
                    let head = self.read(snod, 8)?;
                    if &head[..4] != b"SNOD" || head[4] != 1 {
                        return bad(format!("no symbol table node at {snod}"));
                    }
                    let count = le(&head, 6, 2)?;
                    Ok((count, self.read(snod + 8, 40 * count)?))
                })();
                if let Some((count, e)) = acc.take(r) {
                    snods.push((snod, 8 + 40 * count));
                    entries_of.push((count, e));
                }
                if acc.stopped() {
                    break;
                }
            }
            acc.done()?;
            for (count, entries) in entries_of {
                for k in 0..count as usize {
                    let offset = le(&entries, 40 * k, 8)?;
                    let end = if offset < names.data.len() as u64 {
                        names.data[offset as usize..].iter().position(|&c| c == 0).map(|e| offset as usize + e)
                    } else {
                        None
                    };
                    let Some(end) = end else { return bad("link name outside the local heap") };
                    let Some(target) = address(le(&entries, 40 * k + 8, 8)?)? else {
                        return bad("symbol table entry with an undefined address");
                    };
                    add(names.data[offset as usize..end].to_vec(), Target::Hard(target), &mut list)?;
                }
            }
            storage = LinkStorage::Symtab { heap: (*names).clone(), nodes: tree.nodes.clone(), snods };
        } else if let Some(info) = info {
            if le(&info, 0, 1)? != 0 {
                return bad("unsupported link info message version");
            }
            let p = 2 + if le(&info, 1, 1)? & 1 != 0 { 8 } else { 0 };
            let (heap, btree) = (address(le(&info, p, 8)?)?, address(le(&info, p + 8, 8)?)?);
            match heap {
                None => {
                    for m in h.messages() {
                        if m.ty == LINK {
                            if m.flags & 2 != 0 {
                                return bad("shared link message");
                            }
                            let (n, t) = parse_link(&m.data)?;
                            add(n, t, &mut list)?;
                        }
                    }
                    storage = LinkStorage::Compact;
                }
                Some(heap) => {
                    let Some(btree) = btree else { return bad("dense links without a name index") };
                    let mut acc = Acc::default();
                    acc.take(self.fheap(heap));
                    let tree = acc.take(self.btree2(btree, 5, &[11]));
                    acc.done()?;
                    let tree = tree.unwrap();
                    let mut acc = Acc::default();
                    let mut got = Vec::new();
                    for rec in &tree.records {
                        let r = (|| -> R<(u64, u64, Vec<u8>)> {
                            let (o, n) = self.fh_where(heap, &rec[4..])?;
                            Ok((o, n, self.read(o, n)?))
                        })();
                        if let Some(x) = acc.take(r) {
                            got.push(x);
                        }
                        if acc.stopped() {
                            break;
                        }
                    }
                    acc.done()?;
                    let mut objects = Vec::new();
                    for (o, n, d) in got {
                        let (name, t) = parse_link(&d)?;
                        add(name, t, &mut list)?;
                        objects.push((o, n));
                    }
                    storage = LinkStorage::Dense { heap, index: (*tree).clone(), objects };
                }
            }
        } else {
            return bad(format!("object at {at} is not a group"));
        }
        Ok(Links { list, by_name, storage })
    }

    /// The object header that the link `name` of a group leads to (§8.3).
    pub fn follow(&mut self, links: &Links, name: &[u8]) -> R<u64> {
        match links.get(name) {
            Some(Target::Hard(a)) => Ok(*a),
            Some(Target::Soft(path)) => {
                let path = path.clone();
                if path.first() != Some(&b'/') {
                    return bad(format!("soft link {} to a relative path", show_bytes(name)));
                }
                let mut at = self.superblock()?.root;
                if path != b"/" {
                    for part in path[1..].split(|&c| c == b'/') {
                        let l = self.links(at)?;
                        match l.get(part) {
                            Some(Target::Hard(a)) => at = *a,
                            _ => {
                                return bad(format!(
                                    "soft link {} to {} does not resolve through hard links",
                                    show_bytes(name),
                                    show_bytes(&path)
                                ))
                            }
                        }
                    }
                }
                Ok(at)
            }
            Some(Target::Other(..)) => bad(format!("link {} is neither hard nor soft", show_bytes(name))),
            None => bad(format!("no link {}", show_bytes(name))),
        }
    }

    pub fn local_heap(&mut self, at: u64) -> R<Rc<LocalHeap>> {
        if let Some(r) = memo_get(&self.lheaps, &at) {
            return r;
        }
        let r = (|| -> R<LocalHeap> {
            let head = self.read(at, 32)?;
            if &head[..4] != b"HEAP" || head[4] != 0 {
                return bad(format!("no local heap at {at}"));
            }
            let Some(data_at) = address(le(&head, 24, 8)?)? else { return bad("local heap without a data segment") };
            let n = length(le(&head, 8, 8)?)?;
            Ok(LocalHeap { at, data_at, data: self.read(data_at, n)? })
        })();
        memo_put(&mut self.lheaps, at, r)
    }

    fn node1(&mut self, at: u64, key_size: u64) -> R<Rc<Node1>> {
        if let Some(r) = memo_get(&self.nodes1, &(at, key_size)) {
            return r;
        }
        let r = (|| -> R<Node1> {
            let head = self.read(at, 24)?;
            if &head[..4] != b"TREE" {
                return bad(format!("no B-tree node at {at}"));
            }
            let entries = le(&head, 6, 2)?;
            let body = self.read(at + 24, entries * (key_size + 8) + key_size)?;
            Ok(Node1 { ty: head[4], level: head[5], entries, body })
        })();
        memo_put(&mut self.nodes1, (at, key_size), r)
    }

    /// The version 1 B-tree at `at` (§8.3): its nodes, and its leaves' children with their
    /// keys (symbol table nodes for type 0, chunks for type 1).
    pub fn btree1(&mut self, at: u64, node_type: u8, key_size: u64) -> R<Bt1> {
        let mut out = Bt1::default();
        let mut seen = HashSet::new();
        self.walk1(at, node_type, key_size, &mut seen, None, &mut out)?;
        Ok(out)
    }

    fn walk1(&mut self, at: u64, node_type: u8, key_size: u64, seen: &mut HashSet<u64>, level: Option<u8>, out: &mut Bt1) -> R<()> {
        if !seen.insert(at) {
            return bad(format!("B-tree node at {at} read twice"));
        }
        let node = match self.node1(at, key_size) {
            Err(Fail::Bad(m)) if m.starts_with("no B-tree node") => return bad(format!("no B-tree node of type {node_type} at {at}")),
            r => r?,
        };
        if node.ty != node_type {
            return bad(format!("no B-tree node of type {node_type} at {at}"));
        }
        if let Some(l) = level {
            if node.level != l {
                return bad(format!("B-tree node at {at} has level {}, not {l}", node.level));
            }
        }
        out.nodes.push((at, 24 + node.body.len() as u64));
        let step = (key_size + 8) as usize;
        let mut acc = Acc::default();
        for i in 0..node.entries as usize {
            let r = (|| -> R<()> {
                let Some(child) = address(le(&node.body, i * step + key_size as usize, 8)?)? else {
                    return bad("B-tree child with an undefined address");
                };
                if node.level > 0 {
                    self.walk1(child, node_type, key_size, seen, Some(node.level - 1), out)
                } else if node_type == 0 {
                    if !seen.insert(child) {
                        return bad(format!("symbol table node at {child} read twice"));
                    }
                    out.leaves.push((Vec::new(), child));
                    Ok(())
                } else {
                    out.leaves.push((node.body[i * step..i * step + key_size as usize].to_vec(), child));
                    Ok(())
                }
            })();
            acc.take(r);
            if acc.stopped() {
                break;
            }
        }
        acc.done()
    }

    // ---- version 2 B-trees and fractal heaps (§8.4)

    /// The records of the version 2 B-tree at `at`, which MUST have type `typ` and a record
    /// size among `sizes`.
    pub fn btree2(&mut self, at: u64, typ: u8, sizes: &[u64]) -> R<Rc<Bt2>> {
        if let Some(r) = memo_get(&self.bt2, &(at, typ)) {
            return r;
        }
        let r = self.read_btree2(at, typ, sizes);
        memo_put(&mut self.bt2, (at, typ), r)
    }

    fn read_btree2(&mut self, at: u64, typ: u8, sizes: &[u64]) -> R<Bt2> {
        let h = self.read(at, 38)?;
        if &h[..4] != b"BTHD" || h[4] != 0 || h[5] != typ {
            return bad(format!("no version 2 B-tree of type {typ} at {at}"));
        }
        let (node_size, record_size, depth) = (le(&h, 6, 4)?, le(&h, 10, 2)?, le(&h, 12, 2)?);
        let (root, root_count) = (address(le(&h, 16, 8)?)?, le(&h, 24, 2)?);
        if depth > MAX_BTREE2_DEPTH {
            return bad(format!("version 2 B-tree depth {depth}"));
        }
        if !sizes.contains(&record_size) || node_size < 10 + record_size {
            return bad("invalid version 2 B-tree node or record size");
        }
        let leaf_max = (node_size - 10) / record_size;
        let count_size = log2(leaf_max) / 8 + 1;
        let (mut cum, mut cum_size, mut pointer) = (vec![leaf_max as u128], vec![0u64], vec![0u64]);
        for d in 1..=depth as usize {
            pointer.push(8 + count_size + if d > 1 { cum_size[d - 1] } else { 0 });
            let most = (node_size as i128 - 10 - pointer[d] as i128) / (record_size + pointer[d]) as i128;
            if most < 1 {
                return bad("version 2 B-tree nodes too small");
            }
            let c = (most as u128 + 1).saturating_mul(cum[d - 1]).saturating_add(most as u128);
            cum.push(c);
            cum_size.push((127 - c.leading_zeros() as u64) / 8 + 1);
        }
        let mut out = Bt2 { at, ..Default::default() };
        let mut seen = HashSet::new();
        if let Some(root) = root {
            self.node2(root, root_count, depth, typ, record_size, count_size, &pointer, &mut seen, &mut out)?;
        }
        Ok(out)
    }

    #[allow(clippy::too_many_arguments)]
    fn node2(&mut self, at: u64, count: u64, d: u64, typ: u8, rs: u64, cs: u64, pointer: &[u64], seen: &mut HashSet<u64>, out: &mut Bt2) -> R<()> {
        if !seen.insert(at) {
            return bad(format!("B-tree node at {at} read twice"));
        }
        let n = if d > 0 { 6 + count * rs + (count + 1) * pointer[d as usize] } else { 6 + count * rs };
        let data = match memo_get(&self.nodes2, &(at, n, d)) {
            Some(r) => r?,
            None => {
                let r = self.read(at, n);
                memo_put(&mut self.nodes2, (at, n, d), r)?
            }
        };
        let sig: &[u8] = if d > 0 { b"BTIN" } else { b"BTLF" };
        if &data[..4] != sig || data[4] != 0 || data[5] != typ {
            return bad(format!("no version 2 B-tree node at {at}"));
        }
        out.nodes.push((at, if self.src.within(at, n + 4) { n + 4 } else { n }));
        for k in 0..count {
            out.records.push(data[(6 + k * rs) as usize..(6 + (k + 1) * rs) as usize].to_vec());
        }
        if d == 0 {
            return Ok(());
        }
        let mut p = 6 + count * rs;
        let mut acc = Acc::default();
        for _ in 0..=count {
            let r = (|| -> R<()> {
                let Some(child) = address(le(&data, p as usize, 8)?)? else {
                    return bad("B-tree child with an undefined address");
                };
                let c = le(&data, p as usize + 8, cs as usize)?;
                self.node2(child, c, d - 1, typ, rs, cs, pointer, seen, out)
            })();
            acc.take(r);
            if acc.stopped() {
                break;
            }
            p += pointer[d as usize];
        }
        acc.done()
    }

    pub fn fheap(&mut self, at: u64) -> R<Rc<Fheap>> {
        if let Some(r) = memo_get(&self.fheaps, &at) {
            return r;
        }
        let r = (|| -> R<Fheap> {
            let h = self.read(at, 146)?;
            if &h[..4] != b"FRHP" || h[4] != 0 {
                return bad(format!("no fractal heap at {at}"));
            }
            let (id_length, filters) = (le(&h, 5, 2)?, le(&h, 7, 2)?);
            let max_managed = le(&h, 10, 4)?;
            let (width, start) = (le(&h, 110, 2)?, length(le(&h, 112, 8)?)?);
            let (max_direct, heap_bits) = (length(le(&h, 120, 8)?)?, le(&h, 128, 2)?);
            let (root, root_rows) = (address(le(&h, 132, 8)?)?, le(&h, 140, 2)?);
            if filters != 0 {
                return bad("filtered fractal heaps are not supported");
            }
            if !(power(width) && power(start) && power(max_direct) && max_direct >= start && (1..=64).contains(&heap_bits) && max_managed >= 1) {
                return bad("invalid fractal heap parameters");
            }
            let offset_size = heap_bits.div_ceil(8);
            let length_size = log2(max_direct).div_ceil(8).min(log2(max_managed) / 8 + 1);
            Ok(Fheap {
                at,
                id_length,
                width,
                start,
                max_direct,
                root,
                root_rows,
                offset_size,
                length_size,
                direct_rows: log2(max_direct) - log2(start) + 2,
                huge_tree: address(le(&h, 22, 8)?)?,
                checksummed: h[9] & 2 != 0,
            })
        })();
        memo_put(&mut self.fheaps, at, r)
    }

    fn note_block(&mut self, heap: u64, at: u64, indirect: bool, len: u64) {
        let len = if self.src.within(at, len) { len } else { 0 };
        self.fh_blocks.entry(heap).or_default().entry(at).or_insert((indirect, len));
    }

    /// The (address, size) of the object of the fractal heap at `heap` with this heap ID: one
    /// run of the file (§8.4).
    pub fn fh_where(&mut self, heap: u64, id: &[u8]) -> R<(u64, u64)> {
        let fh = self.fheap(heap)?;
        if id.len() as u64 != fh.id_length {
            return bad("invalid fractal heap ID");
        }
        let kind = id[0] >> 4;
        if kind == 1 {
            let key = le(id, 1, ((fh.id_length - 1).min(8)) as usize)?;
            return self.huge_object(&fh, key);
        }
        if kind != 0 || (id.len() as u64) < 1 + fh.offset_size + fh.length_size {
            return bad("only managed and huge fractal heap objects are supported");
        }
        let offset = le(id, 1, fh.offset_size as usize)?;
        let n = le(id, 1 + fh.offset_size as usize, fh.length_size as usize)?;
        let Some(root) = fh.root else { return bad("empty fractal heap") };
        let (block, start, size) = if fh.root_rows == 0 {
            (root, 0, fh.start)
        } else {
            self.locate(&fh, root, fh.root_rows, 0, offset, 0)?
        };
        let key = (heap, block, start);
        let r = match memo_get(&self.fblocks, &key) {
            Some(r) => r,
            None => {
                let r = (|| -> R<()> {
                    let head = self.read(block, 13 + fh.offset_size)?;
                    if &head[..4] != b"FHDB" || head[4] != 0 || le(&head, 13, fh.offset_size as usize)? != start {
                        return bad(format!("no fractal heap direct block at {block}"));
                    }
                    Ok(())
                })();
                memo_put(&mut self.fblocks, key, r)
            }
        };
        r?;
        self.note_block(heap, block, false, 13 + fh.offset_size + if fh.checksummed { 4 } else { 0 });
        if offset < start || offset as u128 + n as u128 > start as u128 + size as u128 {
            return bad("fractal heap object outside its block");
        }
        Ok((block + offset - start, n))
    }

    fn huge_object(&mut self, fh: &Fheap, key: u64) -> R<(u64, u64)> {
        let map = match memo_get(&self.huge, &fh.at) {
            Some(r) => r?,
            None => {
                let r = (|| -> R<(HashMap<u64, (u64, u64)>, Bt2)> {
                    let Some(tree) = fh.huge_tree else { return bad("huge fractal heap object without a B-tree") };
                    let t = self.btree2(tree, 1, &[24])?;
                    let mut m = HashMap::new();
                    for rec in &t.records {
                        let (a, n, k) = (address(le(rec, 0, 8)?)?, length(le(rec, 8, 8)?)?, le(rec, 16, 8)?);
                        let Some(a) = a else { return bad("invalid huge object record") };
                        if m.insert(k, (a, n)).is_some() {
                            return bad("invalid huge object record");
                        }
                    }
                    Ok((m, (*t).clone()))
                })();
                memo_put(&mut self.huge, fh.at, r)?
            }
        };
        map.0.get(&key).copied().ok_or_else(|| Fail::Bad(format!("no huge fractal heap object {key}")))
    }

    /// The huge objects' B-tree of a heap and its objects, when they were read.
    pub fn huge_tree(&self, heap: u64) -> Option<Rc<(HashMap<u64, (u64, u64)>, Bt2)>> {
        self.huge.get(&heap).and_then(|r| r.as_ref().ok().cloned())
    }

    fn locate(&mut self, fh: &Fheap, at: u64, rows: u64, start: u64, offset: u64, depth: u32) -> R<(u64, u64, u64)> {
        if depth > 64 {
            return bad("invalid fractal heap indirect block");
        }
        let head = self.read(at, 13 + fh.offset_size)?;
        if &head[..4] != b"FHIB" || head[4] != 0 || le(&head, 13, fh.offset_size as usize)? != start {
            return bad(format!("no fractal heap indirect block at {at}"));
        }
        self.note_block(fh.at, at, true, 13 + fh.offset_size + 8 * rows.saturating_mul(fh.width) + 4);
        let mut pos = start as u128;
        for r in 0..rows {
            let size = if r == 0 { fh.start as u128 } else { (fh.start as u128) << (r - 1).min(100) };
            if (offset as u128) < pos + size * fh.width as u128 {
                let col = (offset as u128 - pos) / size;
                let e = self.read(at + 13 + fh.offset_size + 8 * (r * fh.width + col as u64), 8)?;
                let Some(child) = address(le(&e, 0, 8)?)? else { return bad("fractal heap object in an unallocated block") };
                let cstart = (pos + col * size) as u64;
                if r < fh.direct_rows {
                    return Ok((child, cstart, size as u64));
                }
                let child_rows = log2(size as u64) as i64 - log2(fh.start * fh.width) as i64 + 1;
                if child_rows < 1 {
                    return bad("invalid fractal heap indirect block");
                }
                return self.locate(fh, child, child_rows as u64, cstart, offset, depth + 1);
            }
            pos += size * fh.width as u128;
        }
        bad("fractal heap offset outside the heap")
    }

    // ---- attributes (§8.6)

    pub fn attributes(&mut self, at: u64) -> R<Rc<Attrs>> {
        if let Some(r) = memo_get(&self.attrs, &at) {
            return r;
        }
        let r = self.read_attributes(at);
        memo_put(&mut self.attrs, at, r)
    }

    fn read_attributes(&mut self, at: u64) -> R<Attrs> {
        let h = self.header(at)?;
        let mut out = Attrs::default();
        let add = |out: &mut Attrs, data: Vec<u8>, off: u64, dense: bool| -> R<()> {
            let name = attribute_name(&data)?;
            if out.by_name.contains_key(&name) {
                return bad(format!("two attributes named {}", show_bytes(&name)));
            }
            out.by_name.insert(name.clone(), out.list.len());
            out.list.push(Attr { name, data, at: off, dense });
            Ok(())
        };
        for m in h.messages() {
            if m.ty == ATTRIBUTE {
                if m.flags & 2 != 0 {
                    return bad("shared attribute message");
                }
                add(&mut out, m.data.clone(), m.at, false)?;
            }
        }
        if let Some(info) = h.message(ATTRIBUTE_INFO, false)? {
            let info = info.to_vec();
            if le(&info, 0, 1)? != 0 {
                return bad("unsupported attribute info message version");
            }
            let p = 2 + if le(&info, 1, 1)? & 1 != 0 { 2 } else { 0 };
            let (heap, btree) = (address(le(&info, p, 8)?)?, address(le(&info, p + 8, 8)?)?);
            if let Some(heap) = heap {
                let Some(btree) = btree else { return bad("dense attributes without a name index") };
                let mut acc = Acc::default();
                acc.take(self.fheap(heap));
                let tree = acc.take(self.btree2(btree, 8, &[17]));
                acc.done()?;
                let tree = tree.unwrap();
                let mut acc = Acc::default();
                let mut got = Vec::new();
                for rec in &tree.records {
                    let r = (|| -> R<(u64, Vec<u8>)> {
                        if rec[8] & 2 != 0 {
                            return bad("shared dense attribute");
                        }
                        let (o, n) = self.fh_where(heap, &rec[..8])?;
                        Ok((o, self.read(o, n)?))
                    })();
                    if let Some(x) = acc.take(r) {
                        got.push(x);
                    }
                    if acc.stopped() {
                        break;
                    }
                }
                acc.done()?;
                for (o, d) in got {
                    add(&mut out, d, o, true)?;
                }
                out.dense = Some((heap, (*tree).clone()));
            }
        }
        Ok(out)
    }

    // ---- datasets (§8.5, §8.9)

    /// A dataset: a chunked one with a zero fill value (§8.5), or, with `any_layout`, one of
    /// any layout and fill value (§8.9).
    pub fn dataset(&mut self, at: u64, any_layout: bool) -> R<Dataset> {
        let h = self.header(at)?;
        let space = parse_dataspace(h.message(DATASPACE, true)?.unwrap())?;
        let (type_message, named) = if any_layout {
            self.datatype(&h)?
        } else {
            (h.message(DATATYPE, true)?.unwrap().to_vec(), None)
        };
        let datatype = parse_datatype(&type_message)?;
        let fill_message = h.message(FILL_VALUE, false)?.map(|d| d.to_vec());
        let filters = parse_filters(h.message(FILTERS, false)?)?;
        let layout = h.message(LAYOUT, true)?.unwrap().to_vec();
        let layout_at = h.messages().find(|m| m.ty == LAYOUT).map(|m| m.at).unwrap_or(0);
        let mut fill = match &fill_message {
            Some(f) => Some(fill_value(f)?),
            None => None,
        };
        if any_layout && fill.as_ref().is_some_and(|f| !f.is_empty() && f.len() as u64 != datatype.size) {
            return bad("the fill value is not of the datatype's size");
        }
        if fill.as_ref().is_some_and(|f| f.is_empty()) {
            fill = None;
        }
        if !any_layout && fill.as_ref().is_some_and(|f| f.iter().any(|&b| b != 0)) {
            return bad("the fill value is not zero");
        }
        let base = |layout: Layout, dims: Vec<u64>| Dataset {
            dims,
            maxdims: space.maxdims.clone(),
            datatype: datatype.clone(),
            chunk: vec![],
            grid: vec![],
            filters: filters.clone(),
            index: None,
            index_address: None,
            single_size: None,
            single_mask: 0,
            layout,
            compact: (0, vec![]),
            contiguous: (None, 0),
            fill: fill.clone(),
            named,
            raw_edges: false,
        };
        let Some(dims) = space.dims.clone() else {
            if any_layout {
                return Ok(base(Layout::Null, vec![]));
            }
            return bad("dataset with a null dataspace");
        };
        let rank = dims.len();
        let (version, cls) = (le(&layout, 0, 1)?, le(&layout, 1, 1)?);
        if !(3..=5).contains(&version) {
            return bad(format!("unsupported layout message version {version}"));
        }
        if cls == 3 {
            if any_layout {
                return Ok(base(Layout::Virtual, dims));
            }
            return bad("a virtual dataset, whose data is in other datasets");
        }
        if cls != 2 && any_layout && cls <= 1 {
            let mut ds = base(if cls == 0 { Layout::Compact } else { Layout::Contiguous }, dims);
            if cls == 0 {
                let n = le(&layout, 2, 2)?;
                if 4 + n > layout.len() as u64 {
                    return bad("truncated compact dataset");
                }
                ds.compact = (layout_at + 4, layout[4..4 + n as usize].to_vec());
            } else {
                ds.contiguous = (address(le(&layout, 2, 8)?)?, length(le(&layout, 10, 8)?)?);
            }
            return Ok(ds);
        }
        if cls != 2 {
            return bad(format!("dataset layout class {cls} is not chunked"));
        }
        let (mut single_size, mut single_mask, mut raw_edges) = (None, 0u32, false);
        let (index, index_address, cdims);
        if version == 3 {
            let ndims = le(&layout, 2, 1)? as usize;
            index = Index::Btree1;
            index_address = address(le(&layout, 3, 8)?)?;
            let mut v = Vec::new();
            for i in 0..ndims {
                v.push(le(&layout, 11 + 4 * i, 4)?);
            }
            cdims = v;
        } else {
            let (flags, ndims, enc) = (le(&layout, 2, 1)?, le(&layout, 3, 1)? as usize, le(&layout, 4, 1)? as usize);
            if !(1..=8).contains(&enc) {
                return bad(format!("invalid chunk dimension size length {enc}"));
            }
            let mut v = Vec::new();
            for i in 0..ndims {
                v.push(le(&layout, 5 + enc * i, enc)?);
            }
            cdims = v;
            let mut p = 5 + enc * ndims;
            let itype = le(&layout, p, 1)?;
            p += 1;
            index = match itype {
                1 => {
                    if flags & 2 != 0 {
                        single_size = Some(length(le(&layout, p, 8)?)?);
                        single_mask = le(&layout, p + 8, 4)? as u32;
                        p += 12;
                    }
                    Index::Single
                }
                2 => Index::Implicit,
                3 => {
                    le(&layout, p, 1)?;
                    p += 1;
                    Index::Farray
                }
                4 => {
                    le(&layout, p, 5)?;
                    p += 5;
                    Index::Earray
                }
                5 => {
                    le(&layout, p, 6)?;
                    p += 6;
                    Index::Btree2
                }
                _ => return bad(format!("unsupported chunk index type {itype}")),
            };
            index_address = address(le(&layout, p, 8)?)?;
            raw_edges = !filters.is_empty() && flags & 1 != 0;
            if !filters.is_empty() && index == Index::Single && flags & 2 == 0 {
                return bad("single filtered chunk without its size");
            }
            if !filters.is_empty() && index == Index::Implicit {
                return bad("implicit chunk index with filters");
            }
        }
        if cdims.len() != rank + 1 || cdims[rank] != datatype.size {
            return bad("chunk dimensions do not match the dataspace and datatype");
        }
        let chunk = cdims[..rank].to_vec();
        if chunk.iter().any(|&c| !(1..=MAX_SAFE).contains(&c)) {
            return bad("a chunk dimension is not from 1 to 2^53 - 1");
        }
        let mut grid = Vec::new();
        let mut total: u128 = 1;
        for (&n, &c) in dims.iter().zip(&chunk) {
            grid.push(n.div_ceil(c));
            total *= *grid.last().unwrap() as u128;
            if total > MAX_SAFE as u128 {
                return bad("more than 2^53 - 1 chunks");
            }
        }
        let mut ds = base(Layout::Chunked, dims);
        ds.chunk = chunk;
        ds.grid = grid;
        ds.index = Some(index);
        ds.index_address = index_address;
        ds.single_size = single_size;
        ds.single_mask = single_mask;
        ds.raw_edges = raw_edges;
        Ok(ds)
    }

    /// A chunk's byte count.
    pub fn nbytes(ds: &Dataset) -> u128 {
        product(&ds.chunk).saturating_mul(ds.datatype.size as u128)
    }

    /// The allocated chunks of the chunked dataset whose header is at `at` (§8.5). A chunk's
    /// filter mask MUST be 0 unless `masks`; a partial edge chunk stored unfiltered has every
    /// filter's bit set. Memoized by (header, masks).
    pub fn chunks(&mut self, at: u64, ds: &Dataset, masks: bool) -> R<Rc<Chunks>> {
        let key = at.wrapping_mul(2).wrapping_add(masks as u64);
        if let Some(r) = memo_get(&self.chunks, &key) {
            return r;
        }
        let r = self.read_chunks(ds, masks);
        memo_put(&mut self.chunks, key, r)
    }

    fn read_chunks(&mut self, ds: &Dataset, masks: bool) -> R<Chunks> {
        let mut out = Chunks::default();
        let mut seen: HashSet<Vec<u64>> = HashSet::new();
        let nbytes = Self::nbytes(ds);
        let filtered = !ds.filters.is_empty();
        let size = self.size();
        let nf = ds.filters.len().min(32);
        let mut add = |out: &mut Chunks, coords: Vec<u64>, at: u64, sz: u64, mut mask: u32| -> R<()> {
            if ds.raw_edges && coords.iter().zip(&ds.chunk).zip(&ds.dims).any(|((&c, &k), &n)| (c as u128 + 1) * k as u128 > n as u128) {
                mask = if nf >= 32 { u32::MAX } else { (1u32 << nf) - 1 };
            }
            if mask != 0 && !masks {
                return bad("a chunk's filter mask is not 0");
            }
            if !seen.insert(coords.clone()) {
                return bad(format!("chunk {} indexed twice", coords_text(&coords)));
            }
            if !filtered && sz as u128 != nbytes {
                return bad(format!("unfiltered chunk {} of {sz} bytes, not {nbytes}", coords_text(&coords)));
            }
            if sz < 1 {
                return bad(format!("chunk {} is empty", coords_text(&coords)));
            }
            if at.checked_add(sz).is_none_or(|e| e > size) {
                return bad(format!("chunk {} lies outside the file", coords_text(&coords)));
            }
            out.list.push((coords, at, sz, mask));
            Ok(())
        };
        let Some(at) = ds.index_address else { return Ok(out) };
        let rank = ds.dims.len();
        match ds.index.unwrap_or(Index::Btree1) {
            Index::Btree1 => {
                let t = self.btree1(at, 1, 8 + 8 * (rank as u64 + 1))?;
                for &(o, n) in &t.nodes {
                    out.parts.push(("node", o, n));
                }
                for (key, child) in &t.leaves {
                    let (sz, mask) = (le(key, 0, 4)?, le(key, 4, 4)? as u32);
                    let mut offsets = Vec::new();
                    for i in 0..=rank {
                        offsets.push(le(key, 8 + 8 * i, 8)?);
                    }
                    if offsets[rank] != 0 || offsets.iter().zip(&ds.chunk).zip(&ds.dims).any(|((&o, &c), &n)| o % c != 0 || o >= n) {
                        return bad(format!("invalid chunk offset {}", coords_text(&offsets)));
                    }
                    let coords = offsets.iter().zip(&ds.chunk).map(|(&o, &c)| o / c).collect();
                    add(&mut out, coords, *child, sz, mask)?;
                }
            }
            Index::Single => {
                if ds.grid.iter().any(|&g| g != 1) {
                    return bad("single chunk index for more than one chunk");
                }
                let (sz, mask) = if filtered { (ds.single_size.unwrap_or(0), ds.single_mask) } else { (nbytes.min(u64::MAX as u128) as u64, 0) };
                add(&mut out, vec![0; rank], at, sz, mask)?;
            }
            Index::Implicit => {
                let maxgrid = self.max_grid(ds, false)?.into_iter().map(|g| g.unwrap_or(0)).collect::<Vec<_>>();
                let total = product(&ds.grid);
                if total > 0 {
                    let last: Vec<u64> = ds.grid.iter().map(|g| g - 1).collect();
                    if at as u128 + (ravel(&last, &maxgrid) + 1).saturating_mul(nbytes) > size as u128 {
                        return bad("an implicit chunk index past the end of the file");
                    }
                }
                for i in 0..total {
                    let coords = unravel(i, &ds.grid);
                    let a = at as u128 + ravel(&coords, &maxgrid) * nbytes;
                    add(&mut out, coords, a as u64, nbytes as u64, 0)?;
                }
            }
            Index::Farray => self.fixed_array(ds, at, filtered, nbytes, &mut out, &mut add)?,
            Index::Earray => self.extensible_array(ds, at, filtered, nbytes, &mut out, &mut add)?,
            Index::Btree2 => {
                let r = rank as u64;
                let sizes: Vec<u64> = if filtered { (13 + 8 * r..21 + 8 * r).collect() } else { vec![8 + 8 * r] };
                let t = self.btree2(at, if filtered { 11 } else { 10 }, &sizes)?;
                out.parts.push(("header", t.at, 38));
                for &(o, n) in &t.nodes {
                    out.parts.push(("node", o, n));
                }
                for rec in &t.records {
                    let Some(ca) = address(le(rec, 0, 8)?)? else { return bad("a chunk record with an undefined address") };
                    let (mut p, mut sz, mut mask) = (8usize, nbytes.min(u64::MAX as u128) as u64, 0u32);
                    if filtered {
                        let n = rec.len() - 12 - 8 * rank;
                        sz = length(le(rec, 8, n)?)?;
                        mask = le(rec, 8 + n, 4)? as u32;
                        p += n + 4;
                    }
                    let mut coords = Vec::new();
                    for i in 0..rank {
                        coords.push(le(rec, p + 8 * i, 8)?);
                    }
                    if coords.iter().zip(&ds.grid).any(|(&c, &g)| c >= g) {
                        return bad(format!("invalid chunk coordinates {}", coords_text(&coords)));
                    }
                    add(&mut out, coords, ca, sz, mask)?;
                }
            }
        }
        Ok(out)
    }

    /// The number of chunks along each dimension at the maximum dimensions: None for an
    /// unlimited one, which only `unlimited` allows.
    fn max_grid(&self, ds: &Dataset, unlimited: bool) -> R<Vec<Option<u64>>> {
        let Some(maxdims) = &ds.maxdims else { return Ok(ds.grid.iter().map(|&g| Some(g)).collect()) };
        let mut out = Vec::new();
        for ((&n, &m), &c) in ds.dims.iter().zip(maxdims).zip(&ds.chunk) {
            if m == UNDEFINED && unlimited {
                out.push(None);
            } else if m == UNDEFINED || m > MAX_SAFE || m < n {
                return bad("a maximum dimension that the chunk index does not allow");
            } else {
                out.push(Some(m.div_ceil(c)));
            }
        }
        let p: u128 = out.iter().flatten().fold(1u128, |a, &g| a.saturating_mul(g as u128));
        if p > MAX_SAFE as u128 {
            return bad("more than 2^53 - 1 chunks");
        }
        Ok(out)
    }

    /// An array index's entry `k`: (the chunk's address or None, its size, its filter mask).
    fn entry(data: &[u8], k: usize, entry: usize, filtered: bool, nbytes: u128) -> R<(Option<u64>, u64, u32)> {
        let a = address(le(data, k * entry, 8)?)?;
        if a.is_none() || !filtered {
            return Ok((a, nbytes.min(u64::MAX as u128) as u64, 0));
        }
        Ok((a, length(le(data, k * entry + 8, entry - 12)?)?, le(data, k * entry + entry - 4, 4)? as u32))
    }

    fn fixed_array(&mut self, ds: &Dataset, at: u64, filtered: bool, nbytes: u128, out: &mut Chunks, add: &mut AddChunk) -> R<()> {
        let maxgrid: Vec<u64> = self.max_grid(ds, false)?.into_iter().map(|g| g.unwrap_or(0)).collect();
        let h = self.read(at, 28)?;
        if &h[..4] != b"FAHD" || h[4] != 0 {
            return bad(format!("no fixed array header at {at}"));
        }
        let (client, entry, page_bits, count) = (h[5], h[6] as usize, h[7] as u64, le(&h, 8, 8)?);
        let total = product(&maxgrid);
        if client as u64 != filtered as u64 || count as u128 != total {
            return bad("the fixed array does not match the dataset");
        }
        if if filtered { !(13..=20).contains(&entry) } else { entry != 8 } {
            return bad(format!("invalid fixed array entry size {entry}"));
        }
        out.parts.push(("header", at, 28));
        let Some(block) = address(le(&h, 16, 8)?)? else { return Ok(()) };
        let prefix = self.read(block, 14)?;
        if &prefix[..4] != b"FADB" || prefix[4] != 0 || prefix[5] != client {
            return bad(format!("no fixed array data block at {block}"));
        }
        let page = if page_bits >= 63 { u64::MAX } else { 1u64 << page_bits };
        let mut pages: Vec<(u64, u64, u64)> = Vec::new();
        if count > page {
            let npages = count.div_ceil(page);
            let bitmap = self.read(block + 14, npages.div_ceil(8))?;
            let start = block + 14 + bitmap.len() as u64 + 4;
            out.parts.push(("data_block", block, 14 + bitmap.len() as u64 + 4));
            for j in 0..npages {
                if bitmap[(j / 8) as usize] & (0x80 >> (j % 8)) != 0 {
                    pages.push((j * page, page.min(count - j * page), start + j * (page * entry as u64 + 4)));
                }
            }
            for &(_, n, pa) in &pages {
                out.parts.push(("page", pa, n * entry as u64 + 4));
            }
        } else {
            pages.push((0, count, block + 14));
            out.parts.push(("data_block", block, 14 + count * entry as u64 + 4));
        }
        let mut acc = Acc::default();
        let mut got = Vec::new();
        for &(first, n, pa) in &pages {
            if let Some(d) = acc.take(self.read(pa, n * entry as u64)) {
                got.push((first, n, d));
            }
            if acc.stopped() {
                break;
            }
        }
        acc.done()?;
        for (first, n, data) in got {
            for k in 0..n as usize {
                let (ca, sz, mask) = Self::entry(&data, k, entry, filtered, nbytes)?;
                let Some(ca) = ca else { continue };
                let coords = unravel(first as u128 + k as u128, &maxgrid);
                if coords.iter().zip(&ds.grid).all(|(&c, &g)| c < g) {
                    add(out, coords, ca, sz, mask)?;
                }
            }
        }
        Ok(())
    }

    fn extensible_array(&mut self, ds: &Dataset, at: u64, filtered: bool, nbytes: u128, out: &mut Chunks, add: &mut AddChunk) -> R<()> {
        let maxgrid = self.max_grid(ds, true)?;
        let unlimited: Vec<usize> = maxgrid.iter().enumerate().filter(|(_, m)| m.is_none()).map(|(i, _)| i).collect();
        if unlimited.len() != 1 {
            return bad("an extensible array index without exactly one unlimited dimension");
        }
        let u = unlimited[0];
        let others: Vec<usize> = (0..maxgrid.len()).filter(|&i| i != u).collect();
        let other_grid: Vec<u64> = others.iter().map(|&i| maxgrid[i].unwrap_or(0)).collect();
        let down = product(&other_grid);
        let limit = (ds.grid[u] as u128).saturating_mul(down);
        let h = self.read(at, 68)?;
        if &h[..4] != b"EAHD" || h[4] != 0 {
            return bad(format!("no extensible array header at {at}"));
        }
        let (client, entry, bits, ib_count, db_min, sb_min, page_bits) =
            (h[5], h[6] as usize, h[7] as u64, h[8] as u64, h[9] as u64, h[10] as u64, h[11] as u64);
        if client as u64 != filtered as u64 || if filtered { !(13..=20).contains(&entry) } else { entry != 8 } {
            return bad("the extensible array does not match the dataset");
        }
        if !(power(db_min) && power(sb_min) && log2(db_min) <= bits && bits <= 64 && page_bits <= 64) {
            return bad("invalid extensible array parameters");
        }
        out.parts.push(("header", at, 68));
        let Some(iblock) = address(le(&h, 60, 8)?)? else { return Ok(()) };
        let offset_size = bits.div_ceil(8);
        let page = if page_bits >= 64 { u128::MAX } else { 1u128 << page_bits };
        let nsblks = 1 + bits - log2(db_min);
        let ib_sblks = 2 * log2(sb_min);
        let ib_dblks = 2 * (sb_min - 1);
        if ib_sblks > nsblks {
            return bad("invalid extensible array parameters");
        }
        let head = self.read(iblock, 14)?;
        if &head[..4] != b"EAIB" || head[4] != 0 || head[5] != client {
            return bad(format!("no extensible array index block at {iblock}"));
        }
        let body_len = ib_count * entry as u64 + 8 * ib_dblks + 8 * (nsblks - ib_sblks);
        let body = self.read(iblock + 14, body_len)?;
        out.parts.push(("index_block", iblock, 14 + body_len + if self.src.within(iblock, 18 + body_len) { 4 } else { 0 }));
        let mut found: Vec<(u128, Vec<u8>, u64)> = vec![(0, body.clone(), (ib_count as u128).min(limit) as u64)];
        let dblks = body[(ib_count * entry as u64) as usize..(ib_count * entry as u64 + 8 * ib_dblks) as usize].to_vec();
        let sblks = body[(ib_count * entry as u64 + 8 * ib_dblks) as usize..].to_vec();
        let prefix = 14 + offset_size;
        // (data block address, first element, elements, page bitmap or None for the index block's, its index d)
        let mut blocks: Vec<(u64, u128, u128, Option<Vec<u8>>, u64)> = Vec::new();
        let mut start = ib_count as u128;
        let mut k = 0u64;
        let mut acc = Acc::default();
        for s in 0..nsblks {
            let count = 1u128 << (s / 2).min(100);
            let n = (1u128 << ((s + 1) / 2).min(100)) * db_min as u128;
            if start >= limit {
                break;
            }
            let needed = count.min((limit - start).div_ceil(n));
            if s < ib_sblks {
                for d in 0..needed as u64 {
                    if let Some(ba) = address(le(&dblks, (8 * (k + d)) as usize, 8)?)? {
                        blocks.push((ba, start + d as u128 * n, n, None, d));
                    }
                }
                k += count as u64;
            } else if let Some(sb) = address(le(&sblks, (8 * (s - ib_sblks)) as usize, 8)?)? {
                let npages = if n > page { n / page } else { 0 };
                let bitmap = (count * npages.div_ceil(8)) as u64;
                let r = self.read(sb, prefix + bitmap + 8 * count as u64);
                if let Some(shead) = acc.take(r) {
                    if &shead[..4] != b"EASB" || shead[4] != 0 || shead[5] != client {
                        return bad(format!("no extensible array super block at {sb}"));
                    }
                    let sl = prefix + bitmap + 8 * count as u64;
                    out.parts.push(("super_block", sb, sl + if self.src.within(sb, sl + 4) { 4 } else { 0 }));
                    let pages = shead[prefix as usize..(prefix + bitmap) as usize].to_vec();
                    for d in 0..needed as u64 {
                        if let Some(ba) = address(le(&shead, (prefix + bitmap + 8 * d) as usize, 8)?)? {
                            blocks.push((ba, start + d as u128 * n, n, Some(pages.clone()), d));
                        }
                    }
                }
                if acc.stopped() {
                    break;
                }
            }
            start += count * n;
        }
        for (ba, first, n, pages, d) in blocks {
            let r = (|| -> R<()> {
                let head = self.read(ba, prefix)?;
                if &head[..4] != b"EADB" || head[4] != 0 || head[5] != client {
                    return bad(format!("no extensible array data block at {ba}"));
                }
                let want = n.min(limit - first);
                if n <= page {
                    let len = want as u64 * entry as u64;
                    let full = prefix + n as u64 * entry as u64;
                    out.parts.push(("data_block", ba, full + if self.src.within(ba, full + 4) { 4 } else { 0 }));
                    found.push((first, self.read(ba + prefix, len)?, want as u64));
                    return Ok(());
                }
                let Some(pages) = pages else { return bad("a paged extensible array data block in the index block") };
                out.parts.push(("data_block", ba, prefix + 4));
                let npages = n / page;
                let mut j = 0u128;
                while j < npages {
                    if j * page >= want {
                        break;
                    }
                    let bit = d as u128 * npages + j;
                    if pages.get((bit / 8) as usize).is_some_and(|b| b & (0x80 >> (bit % 8)) != 0) {
                        let m = page.min(want - j * page) as u64;
                        let pa = ba + prefix + 4 + (j * (page * entry as u128 + 4)) as u64;
                        out.parts.push(("page", pa, page as u64 * entry as u64 + 4));
                        found.push((first + j * page, self.read(pa, m * entry as u64)?, m));
                    }
                    j += 1;
                }
                Ok(())
            })();
            acc.take(r);
            if acc.stopped() {
                break;
            }
        }
        acc.done()?;
        for (first, data, n) in found {
            for kk in 0..n as usize {
                let (ca, sz, mask) = Self::entry(&data, kk, entry, filtered, nbytes)?;
                let Some(ca) = ca else { continue };
                let rest = first + kk as u128;
                let (outer, inner) = (rest / down.max(1), rest % down.max(1));
                let mut coords = vec![0u64; maxgrid.len()];
                coords[u] = outer as u64;
                for (&i, c) in others.iter().zip(unravel(inner, &other_grid)) {
                    coords[i] = c;
                }
                if coords.iter().zip(&ds.grid).all(|(&c, &g)| c < g) {
                    add(out, coords, ca, sz, mask)?;
                }
            }
        }
        Ok(())
    }

    // ---- the global heap (§8.9)

    pub fn collection(&mut self, at: u64) -> R<Rc<Collection>> {
        if let Some(r) = memo_get(&self.collections, &at) {
            return r;
        }
        let r = (|| -> R<Collection> {
            let head = self.read(at, 16)?;
            if &head[..4] != b"GCOL" || head[4] != 1 {
                return bad(format!("no global heap collection at {at}"));
            }
            let size = length(le(&head, 8, 8)?)?;
            if size < 16 || at.checked_add(size).is_none_or(|e| e > self.size()) {
                return bad("a global heap collection outside the file");
            }
            let data = self.read(at, size)?;
            let mut objects = BTreeMap::new();
            let mut order = Vec::new();
            let mut p = 16u64;
            while p + 16 <= size {
                let (i, n) = (le(&data, p as usize, 2)?, length(le(&data, p as usize + 8, 8)?)?);
                if i == 0 {
                    order.push((0, at + p, (size - p - 16).min(n)));
                    break;
                }
                if p + 16 + n > size {
                    return bad("a global heap object outside its collection");
                }
                objects.entry(i).or_insert((at + p, n));
                order.push((i, at + p, n));
                p += 16 + pad8(n);
            }
            Ok(Collection { at, size, objects, order })
        })();
        memo_put(&mut self.collections, at, r)
    }
}

/// Bytes as a message shows them: quoted text (each byte a character when not UTF-8).
pub fn show_bytes(b: &[u8]) -> String {
    match std::str::from_utf8(b) {
        Ok(s) => format!("{s:?}"),
        Err(_) => format!("{:?}", b.iter().map(|&c| c as char).collect::<String>()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn store_merges_touching_ranges() {
        let mut s = Store::default();
        s.feed(10, vec![1, 2, 3]);
        s.feed(13, vec![4, 5]);
        s.feed(0, vec![9; 4]);
        s.feed(2, vec![7; 9]);
        assert_eq!(s.get(0, 15).unwrap(), &[9, 9, 7, 7, 7, 7, 7, 7, 7, 7, 7, 2, 3, 4, 5]);
        assert_eq!(s.held, 15);
        assert!(s.get(14, 2).is_none());
    }

    #[test]
    fn acc_keeps_the_first_failure_in_order() {
        let mut a = Acc::default();
        assert_eq!(a.take::<u8>(Ok(1)), Some(1));
        a.take::<u8>(Err(Fail::Miss));
        a.take::<u8>(bad("x"));
        assert!(!a.stopped());
        assert_eq!(a.done(), Err(Fail::Miss));
        let mut b = Acc::default();
        b.take::<u8>(bad("y"));
        assert!(b.stopped());
        b.take::<u8>(Err(Fail::Miss));
        assert_eq!(b.done(), Err(Fail::Bad("y".into())));
    }

    #[test]
    fn src_wants_what_it_lacks() {
        let mut s = Src::new(5000);
        assert_eq!(s.read(10, 4), Err(Fail::Miss));
        assert_eq!(s.want, vec![(10, WINDOW)]);
        assert!(matches!(s.read(4990, 20), Err(Fail::Bad(_))));
        s.store.feed(10, vec![1; 1024]);
        assert_eq!(s.read(12, 2).unwrap(), vec![1, 1]);
    }
}
