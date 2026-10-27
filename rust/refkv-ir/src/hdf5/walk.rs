//! The walk of an HDF5 file and its IR mirror, shared by the profiles that read HDF5 (IMS,
//! spec/zarrify/ims.md §5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ims.md#5-source-metadata>,
//! and HDF5, spec/zarrify/hdf5.md): `visit` reads an object as far as the reader can, never
//! rejecting, and `Emit` describes what the walk read as the IR's elements.

use super::{self as hdf5, address, le, Acc, Fail, Header, Links, R, H5};
use crate::ir::{Ir, NO_INDEX};
use serde_json::json;
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::sync::Arc as Rc;

/// What an object of the walk is, as far as the reader can read it.
#[derive(Default)]
pub struct Visit {
    pub header: Option<Rc<Header>>,
    pub links: Option<Rc<Links>>,
    pub attrs: Option<Rc<hdf5::Attrs>>,
    pub ds: Option<hdf5::Dataset>,
    pub chunks: Option<Rc<hdf5::Chunks>>,
    pub collections: Vec<Rc<hdf5::Collection>>,
}

pub fn lenient<T>(r: R<T>) -> R<Option<T>> {
    match r {
        Ok(v) => Ok(Some(v)),
        Err(Fail::Bad(_)) => Ok(None),
        Err(Fail::Miss) => Err(Fail::Miss),
    }
}

/// An object as far as the reader can read it: its header, links, attributes, dataset,
/// chunks and the global heap collections its attributes name (never rejecting).
pub fn visit(h: &mut H5, at: u64, max_collections: usize) -> R<Visit> {
    let Some(header) = lenient(h.header(at))? else { return Ok(Visit::default()) };
    let mut acc = Acc::default();
    let is_group = header.has(hdf5::SYMBOL_TABLE) || header.has(hdf5::LINK_INFO);
    let is_ds = header.has(hdf5::LAYOUT);
    let links = if is_group { acc.take(lenient(h.links(at))).flatten() } else { None };
    let attrs = acc.take(lenient(h.attributes(at))).flatten();
    // a dataset as the source metadata reads one; a dataset that reading rejects is read
    // as the layout reads one (so an image dataset the layout accepted is described)
    let ds = if is_ds {
        match acc.take(lenient(h.dataset(at, true))).flatten() {
            Some(d) => Some(d),
            None if !acc.missed() => acc.take(lenient(h.dataset(at, false))).flatten(),
            None => None,
        }
    } else {
        None
    };
    let chunks = match &ds {
        Some(d) if d.layout == hdf5::Layout::Chunked => {
            let d = d.clone();
            acc.take(lenient(h.chunks(at, &d, true))).flatten()
        }
        _ => None,
    };
    acc.done()?;
    let mut collections = Vec::new();
    if let Some(a) = &attrs {
        let mut addrs = BTreeSet::new();
        for attr in &a.list {
            let Ok(p) = hdf5::attribute_parts_at(&attr.data) else { continue };
            if p.flags & 3 != 0 {
                continue;
            }
            let d = &attr.data;
            let (Ok(dt), Ok(space)) = (
                hdf5::parse_datatype(&d[p.datatype.0 as usize..(p.datatype.0 + p.datatype.1) as usize]),
                hdf5::parse_dataspace(&d[p.dataspace.0 as usize..(p.dataspace.0 + p.dataspace.1) as usize]),
            ) else {
                continue;
            };
            let Some(dims) = space.dims else { continue };
            let (step, off) = match (dt.cls, dt.size) {
                (9, 16) => (16u64, 4u64),
                (7, 12) if dt.bits & 15 == 1 => (12, 0),
                _ => continue,
            };
            let count = hdf5::product(&dims);
            if p.data as u128 + count * step as u128 > d.len() as u128 {
                continue;
            }
            for k in 0..count as u64 {
                let o = (p.data + k * step + off) as usize;
                if let Ok(Some(a)) = address(le(d, o, 8).unwrap_or(hdf5::UNDEFINED)) {
                    if a != 0 && addrs.len() < max_collections {
                        addrs.insert(a);
                    }
                }
            }
        }
        let mut acc = Acc::default();
        for a in addrs {
            if let Some(Some(c)) = acc.take(lenient(h.collection(a))) {
                collections.push(c);
            }
        }
        acc.done()?;
    }
    Ok(Visit { header: Some(header), links, attrs, ds, chunks, collections })
}

/// The IR's emission from what the phases read.
pub struct Emit<'a> {
    pub ir: Ir,
    pub h: &'a H5,
    /// dataset header -> the struct of its chunks
    pub chunks_of: HashMap<u64, u32>,
    /// the elements the walk describes, at most (the IR's records and the mirror's rows
    /// leave room for the gaps between them): past it, structures are not described
    pub room: u64,
    /// the image's datasets, whose chunks are described whatever the room
    pub must: HashSet<u64>,
}

pub type Res<T> = Result<T, String>;

pub fn num_type(code: char, size: u64, big: bool) -> String {
    if size == 1 {
        format!("{code}1")
    } else {
        format!("{}{code}{size}", if big { ">" } else { "<" })
    }
}

impl Emit<'_> {
    pub fn full(&self) -> bool {
        self.ir.len() as u64 >= self.room
    }

    /// The object at `at` has no more room: it is not one of the image's datasets, and the
    /// IR is full.
    pub fn spent(&self, at: u64) -> bool {
        !self.must.contains(&at) && self.full()
    }

    /// A value of `ty` over `[o, o + n)`, with its bytes when the view may show it.
    pub fn val(&mut self, parent: u32, name: &str, nidx: u64, ty: &str, o: u64, n: u64) -> Res<Option<u32>> {
        if n == 0 || !self.h.src.within(o, n) {
            return Ok(None);
        }
        let raw = if n <= 1024 && !ty.starts_with("bytes") { self.h.src.store.get(o, n).map(|b| b.to_vec()) } else { None };
        Ok(Some(self.ir.value(parent, name, nidx, ty, 0, (o, n), raw.as_deref())?))
    }

    pub fn bytes(&mut self, parent: u32, name: &str, nidx: u64, o: u64, n: u64) -> Res<Option<u32>> {
        self.val(parent, name, nidx, &format!("bytes[{n}]"), o, n)
    }

    pub fn superblock(&mut self, sb: &hdf5::Super) -> Res<()> {
        let ty = match (sb.version, sb.len) {
            (2 | 3, 48) => "{signature:bytes[8],version:u1,offset_size:u1,length_size:u1,flags:u1,base:<u8,extension:<u8,eof:<u8,root:<u8,checksum:<u4}".to_string(),
            (0 | 1, n) if n == 96 || n == 100 => format!(
                "{{signature:bytes[8],version:u1,free_space_version:u1,root_version:u1,reserved:u1,shared_header_version:u1,offset_size:u1,length_size:u1,reserved1:u1,leaf_k:<u2,internal_k:<u2,flags:<u4{},base:<u8,free_space:<u8,eof:<u8,driver:<u8,root_name:<u8,root_header:<u8,root_cache_type:<u4,root_reserved:<u4,root_scratch:bytes[16]}}",
                if n == 100 { ",indexed_k:<u2,reserved2:<u2" } else { "" }
            ),
            (_, n) => format!("bytes[{n}]"),
        };
        self.val(0, "superblock", NO_INDEX, &ty, 0, sb.len)?;
        Ok(())
    }

    /// An object header's prefix, blocks and messages.
    pub fn header(&mut self, parent: u32, h: &Header) -> Res<()> {
        let ty = if h.v2 {
            let mut t = String::from("{signature:ascii[4],version:u1,flags:u1");
            if h.flags & 0x20 != 0 {
                t.push_str(",times:<u4[4]");
            }
            if h.flags & 0x10 != 0 {
                t.push_str(",phase:<u2[2]");
            }
            t.push_str(&format!(",size:{}}}", num_type('u', 1 << (h.flags & 3), false)));
            t
        } else {
            "{version:u1,reserved:u1,messages:<u2,references:<u4,size:<u4,padding:bytes[4]}".to_string()
        };
        self.val(parent, "prefix", NO_INDEX, &ty, h.at, h.prefix)?;
        if h.v2 {
            let (s, n) = h.blocks[0];
            self.val(parent, "checksum", NO_INDEX, "<u4", s + n, 4)?;
            if h.blocks.len() > 1 {
                let b = self.ir.struct_(parent, "blocks", NO_INDEX, 0, None)?;
                for (j, &(s, n)) in h.blocks.iter().enumerate().skip(1) {
                    let k = self.ir.struct_(b, "", j as u64, 0, None)?;
                    self.val(k, "signature", NO_INDEX, "ascii[4]", s - 4, 4)?;
                    self.val(k, "checksum", NO_INDEX, "<u4", s + n, 4)?;
                }
            }
        }
        if h.msgs.is_empty() {
            return Ok(());
        }
        let ms = self.ir.struct_(parent, "messages", NO_INDEX, 0, None)?;
        let mty = match h.mh {
            8 => "{type:<u2,size:<u2,flags:u1,reserved:bytes[3]}",
            6 => "{type:u1,size:<u2,flags:u1,order:<u2}",
            _ => "{type:u1,size:<u2,flags:u1}",
        };
        for (k, m) in h.msgs.iter().enumerate() {
            if self.spent(h.at) {
                break;
            }
            let s = self.ir.struct_(ms, "", k as u64, 0, None)?;
            self.val(s, "header", NO_INDEX, mty, m.head, h.mh)?;
            let n = m.data.len() as u64;
            match m.ty {
                hdf5::ATTRIBUTE if m.flags & 2 == 0 => self.attribute(s, "data", NO_INDEX, m.at, &m.data)?,
                hdf5::LINK if m.flags & 2 == 0 => self.link(s, "data", NO_INDEX, m.at, &m.data)?,
                _ => {
                    self.bytes(s, "data", NO_INDEX, m.at, n)?;
                }
            }
        }
        Ok(())
    }

    /// An attribute message (in a header, or a heap's object) at `at`: its parts.
    pub fn attribute(&mut self, parent: u32, name: &str, nidx: u64, at: u64, d: &[u8]) -> Res<()> {
        let Ok(p) = hdf5::attribute_parts_at(d) else {
            self.bytes(parent, name, nidx, at, d.len() as u64)?;
            return Ok(());
        };
        let s = self.ir.struct_(parent, name, nidx, 0, None)?;
        let pre = if p.name.0 == 9 {
            "{version:u1,flags:u1,name_size:<u2,datatype_size:<u2,dataspace_size:<u2,encoding:u1}"
        } else {
            "{version:u1,flags:u1,name_size:<u2,datatype_size:<u2,dataspace_size:<u2}"
        };
        self.val(s, "prelude", NO_INDEX, pre, at, p.name.0)?;
        self.val(s, "name", NO_INDEX, &format!("cstr[{}]", p.name.1), at + p.name.0, p.name.1)?;
        self.bytes(s, "datatype", NO_INDEX, at + p.datatype.0, p.datatype.1)?;
        self.bytes(s, "dataspace", NO_INDEX, at + p.dataspace.0, p.dataspace.1)?;
        if p.flags & 3 != 0 {
            let rest = d.len() as u64 - p.data;
            self.bytes(s, "value", NO_INDEX, at + p.data, rest)?;
            return Ok(());
        }
        let dt = hdf5::parse_datatype(&d[p.datatype.0 as usize..(p.datatype.0 + p.datatype.1) as usize]);
        let space = hdf5::parse_dataspace(&d[p.dataspace.0 as usize..(p.dataspace.0 + p.dataspace.1) as usize]);
        let (Ok(dt), Ok(space)) = (dt, space) else {
            self.bytes(s, "value", NO_INDEX, at + p.data, d.len() as u64 - p.data)?;
            return Ok(());
        };
        let Some(dims) = space.dims else { return Ok(()) };
        let count = hdf5::product(&dims);
        let n = count.saturating_mul(dt.size as u128);
        if p.data as u128 + n > d.len() as u128 || n == 0 {
            self.bytes(s, "value", NO_INDEX, at + p.data, d.len() as u64 - p.data)?;
            return Ok(());
        }
        let n = n as u64;
        let big = dt.bits & 1 != 0;
        let plain = |dt: &hdf5::Datatype| le(&dt.props, 0, 2).ok() == Some(0) && le(&dt.props, 2, 2).ok() == Some(8 * dt.size);
        let ieee = |dt: &hdf5::Datatype, e: [u8; 4], bias: u64, sign: u32| {
            dt.bits & 0x40 == 0
                && (dt.bits >> 4) & 3 == 2
                && (dt.bits >> 8) & 0xFF == sign
                && plain(dt)
                && dt.props.get(4..8) == Some(&e[..])
                && le(&dt.props, 8, 4).ok() == Some(bias)
        };
        let dims_text = if dims.is_empty() { String::new() } else { format!("[{count}]") };
        let ty = match (dt.cls, dt.size) {
            (3, _) => format!("ascii[{n}]"),
            (0, 1 | 2 | 4 | 8) if plain(&dt) => format!("{}{dims_text}", num_type(if dt.bits & 8 != 0 { 'i' } else { 'u' }, dt.size, big)),
            (1, 4) if ieee(&dt, [23, 8, 0, 23], 127, 31) => format!("{}{dims_text}", num_type('f', 4, big)),
            (1, 8) if ieee(&dt, [52, 11, 0, 52], 1023, 63) => format!("{}{dims_text}", num_type('f', 8, big)),
            _ => format!("bytes[{n}]"),
        };
        self.val(s, "value", NO_INDEX, &ty, at + p.data, n)?;
        Ok(())
    }

    /// A link message at `at`: its fields, name and target.
    pub fn link(&mut self, parent: u32, name: &str, nidx: u64, at: u64, d: &[u8]) -> Res<()> {
        let parsed = (|| -> R<(String, u64, u64, u64, u64)> {
            if le(d, 0, 1)? != 1 {
                return hdf5::bad("version");
            }
            let flags = le(d, 1, 1)?;
            let mut t = String::from("{version:u1,flags:u1");
            let mut p = 2u64;
            let mut kind = 0;
            if flags & 8 != 0 {
                kind = le(d, 2, 1)?;
                t.push_str(",type:u1");
                p += 1;
            }
            if flags & 4 != 0 {
                t.push_str(",order:<u8");
                p += 8;
            }
            if flags & 16 != 0 {
                t.push_str(",charset:u1");
                p += 1;
            }
            let ls = 1u64 << (flags & 3);
            let nl = le(d, p as usize, ls as usize)?;
            t.push_str(&format!(",name_size:{}}}", num_type('u', ls, false)));
            p += ls;
            if p + nl > d.len() as u64 {
                return hdf5::bad("truncated");
            }
            Ok((t, p, nl, kind, p + nl))
        })();
        let Ok((t, p, nl, kind, q)) = parsed else {
            self.bytes(parent, name, nidx, at, d.len() as u64)?;
            return Ok(());
        };
        let s = self.ir.struct_(parent, name, nidx, 0, None)?;
        self.val(s, "fields", NO_INDEX, &t, at, p)?;
        self.val(s, "name", NO_INDEX, &format!("cstr[{nl}]"), at + p, nl)?;
        let rest = d.len() as u64 - q;
        let m = if rest >= 2 { le(d, q as usize, 2).unwrap_or(0) } else { 0 };
        let target = match kind {
            0 if rest >= 8 => Some(("<u8".to_string(), 8)),
            1 if rest >= 2 + m => Some((format!("{{size:<u2,path:ascii[{m}]}}"), 2 + m)),
            k if k > 1 && rest >= 2 + m => Some((format!("{{size:<u2,value:bytes[{m}]}}"), 2 + m)),
            _ => None,
        };
        match target {
            Some((ty, n)) if !(kind == 1 && m == 0) && !(kind > 1 && m == 0) => {
                self.val(s, "target", NO_INDEX, &ty, at + q, n)?;
            }
            Some((_, n)) => {
                self.val(s, "target", NO_INDEX, "<u2", at + q, n.min(2))?;
            }
            None => {
                self.bytes(s, "target", NO_INDEX, at + q, rest)?;
            }
        }
        Ok(())
    }

    pub fn btree2(&mut self, owner: u64, parent: u32, name: &str, t: &hdf5::Bt2) -> Res<()> {
        let s = self.ir.struct_(parent, name, NO_INDEX, 0, None)?;
        self.val(
            s,
            "header",
            NO_INDEX,
            "{signature:ascii[4],version:u1,type:u1,node_size:<u4,record_size:<u2,depth:<u2,split:u1,merge:u1,root:<u8,root_records:<u2,records:<u8,checksum:<u4}",
            t.at,
            38,
        )?;
        if !t.nodes.is_empty() {
            let ns = self.ir.struct_(s, "nodes", NO_INDEX, 0, None)?;
            for (k, &(o, n)) in t.nodes.iter().enumerate() {
                if self.spent(owner) {
                    break;
                }
                self.bytes(ns, "", k as u64, o, n)?;
            }
        }
        Ok(())
    }

    pub fn fheap(&mut self, owner: u64, parent: u32, heap: u64) -> Res<()> {
        let s = self.ir.struct_(parent, "heap", NO_INDEX, 0, None)?;
        self.val(
            s,
            "header",
            NO_INDEX,
            "{signature:ascii[4],version:u1,id_length:<u2,filter_length:<u2,flags:u1,max_managed:<u4,next_huge_id:<u8,huge_index:<u8,free_space:<u8,free_space_manager:<u8,managed_space:<u8,allocated_space:<u8,iterator_offset:<u8,managed_objects:<u8,huge_size:<u8,huge_objects:<u8,tiny_size:<u8,tiny_objects:<u8,width:<u2,start_block:<u8,max_direct:<u8,max_heap_bits:<u2,start_rows:<u2,root:<u8,rows:<u2,checksum:<u4}",
            heap,
            146,
        )?;
        if let Some(blocks) = self.h.fh_blocks.get(&heap) {
            let blocks: Vec<(u64, (bool, u64))> = blocks.iter().map(|(a, b)| (*a, *b)).collect();
            let bs = self.ir.struct_(s, "blocks", NO_INDEX, 0, None)?;
            for (k, (a, (_, n))) in blocks.into_iter().enumerate() {
                if self.spent(owner) {
                    break;
                }
                self.bytes(bs, "", k as u64, a, n)?;
            }
        }
        if let Some(huge) = self.h.huge_tree(heap) {
            let t = huge.1.clone();
            self.btree2(owner, s, "huge_index", &t)?;
        }
        Ok(())
    }

    pub fn object(&mut self, o: u32, at: u64, v: &Visit) -> Res<()> {
        let Some(h) = &v.header else { return Ok(()) };
        self.header(o, h)?;
        if let Some(l) = &v.links {
            match &l.storage {
                hdf5::LinkStorage::Symtab { heap, nodes, snods } => {
                    let s = self.ir.struct_(o, "symbol_table", NO_INDEX, 0, None)?;
                    let hp = self.ir.struct_(s, "heap", NO_INDEX, 0, None)?;
                    self.val(hp, "header", NO_INDEX, "{signature:ascii[4],version:u1,reserved:bytes[3],data_size:<u8,free_list:<u8,data:<u8}", heap.at, 32)?;
                    self.bytes(hp, "data", NO_INDEX, heap.data_at, heap.data.len() as u64)?;
                    if !nodes.is_empty() {
                        let ns = self.ir.struct_(s, "nodes", NO_INDEX, 0, None)?;
                        for (k, &(a, n)) in nodes.iter().enumerate() {
                            if self.spent(at) {
                                break;
                            }
                            let x = self.ir.struct_(ns, "", k as u64, 0, None)?;
                            self.val(x, "header", NO_INDEX, "{signature:ascii[4],type:u1,level:u1,entries:<u2,left:<u8,right:<u8}", a, 24)?;
                            self.bytes(x, "entries", NO_INDEX, a + 24, n - 24)?;
                        }
                    }
                    if !snods.is_empty() {
                        let ss = self.ir.struct_(s, "snods", NO_INDEX, 0, None)?;
                        for (k, &(a, n)) in snods.iter().enumerate() {
                            if self.spent(at) {
                                break;
                            }
                            let x = self.ir.struct_(ss, "", k as u64, 0, None)?;
                            self.val(x, "header", NO_INDEX, "{signature:ascii[4],version:u1,reserved:u1,entries:<u2}", a, 8)?;
                            let c = (n - 8) / 40;
                            if c > 0 {
                                self.val(x, "entries", NO_INDEX, &format!("{{name:<u8,header:<u8,cache_type:<u4,reserved:<u4,scratch:bytes[16]}}[{c}]"), a + 8, n - 8)?;
                            }
                        }
                    }
                }
                hdf5::LinkStorage::Compact => {}
                hdf5::LinkStorage::Dense { heap, index, objects } => {
                    let s = self.ir.struct_(o, "links", NO_INDEX, 0, None)?;
                    self.fheap(at, s, *heap)?;
                    self.btree2(at, s, "index", index)?;
                    let items = self.ir.struct_(s, "items", NO_INDEX, 0, None)?;
                    for (k, &(a, n)) in objects.iter().enumerate() {
                        if self.spent(at) {
                            break;
                        }
                        if let Some(d) = self.h.src.store.get(a, n).map(|b| b.to_vec()) {
                            self.link(items, "", k as u64, a, &d)?;
                        } else {
                            self.bytes(items, "", k as u64, a, n)?;
                        }
                    }
                }
            }
        }
        if let Some(a) = &v.attrs {
            if let Some((heap, index)) = &a.dense {
                let s = self.ir.struct_(o, "attributes", NO_INDEX, 0, None)?;
                self.fheap(at, s, *heap)?;
                self.btree2(at, s, "index", index)?;
                let items = self.ir.struct_(s, "items", NO_INDEX, 0, None)?;
                for (k, attr) in a.list.iter().filter(|x| x.dense).enumerate() {
                    if self.spent(at) {
                        break;
                    }
                    self.attribute(items, "", k as u64, attr.at, &attr.data)?;
                }
            }
        }
        if let Some(ds) = &v.ds {
            if let Some(ch) = &v.chunks {
                if !ch.parts.is_empty() {
                    let s = self.ir.struct_(o, "index", NO_INDEX, 0, None)?;
                    let mut groups: BTreeMap<&str, (u32, u64)> = BTreeMap::new();
                    for &(what, a, n) in &ch.parts {
                        if self.spent(at) {
                            break;
                        }
                        let key = match what {
                            "header" => "headers",
                            "node" => "nodes",
                            "data_block" => "data_blocks",
                            "index_block" => "index_blocks",
                            "super_block" => "super_blocks",
                            _ => "pages",
                        };
                        let g = match groups.get(key) {
                            Some(&g) => g,
                            None => {
                                let id = self.ir.struct_(s, key, NO_INDEX, 0, None)?;
                                groups.insert(key, (id, 0));
                                (id, 0)
                            }
                        };
                        self.bytes(g.0, "", g.1, a, n)?;
                        groups.get_mut(key).unwrap().1 += 1;
                    }
                }
                let image = self.must.contains(&at);
                if !image && ds.index == Some(hdf5::Index::Implicit) && !ch.list.is_empty() {
                    // an implicit index's chunks lie in the row-major order of the maximum grid,
                    // which the storage holds whole: one element for them all, with that grid
                    let max_grid: Vec<u64> = match &ds.maxdims {
                        Some(m) => m.iter().zip(&ds.chunk).map(|(&m, &c)| m.div_ceil(c)).collect(),
                        None => ds.grid.clone(),
                    };
                    let first = ch.list.iter().map(|c| c.1).min().unwrap();
                    let end = ch.list.iter().map(|c| c.1 + c.2).max().unwrap();
                    if self.h.src.within(first, end - first) {
                        let form = json!({"geometry": {"chunk": ds.chunk, "size": ds.datatype.size, "grid": max_grid}, "codec": {"filters": ds.filters}, "recipe": [["src", 0, null]]});
                        self.ir.data(o, "data", NO_INDEX, 0, (first, end - first), &form.to_string())?;
                    }
                } else if !ch.list.is_empty() && (image || !self.full()) {
                    let s = self.ir.struct_(o, "chunks", NO_INDEX, 0, None)?;
                    self.chunks_of.insert(at, s);
                    let base = json!({"geometry": {"chunk": ds.chunk, "size": ds.datatype.size}, "codec": {"filters": ds.filters}, "recipe": [["src", 0, null]]});
                    let plain = base.to_string();
                    let mut sorted: Vec<(u128, &(Vec<u64>, u64, u64, u32))> =
                        ch.list.iter().map(|c| (hdf5::ravel(&c.0, &ds.grid), c)).collect();
                    sorted.sort_by_key(|x| x.0);
                    for (idx, (_, a, n, mask)) in sorted {
                        if self.spent(at) {
                            break;
                        }
                        let form = if *mask == 0 {
                            plain.clone()
                        } else {
                            let mut f = base.clone();
                            f["codec"]["mask"] = json!(mask);
                            f.to_string()
                        };
                        self.ir.data(s, "", idx as u64, 0, (*a, *n), &form)?;
                    }
                }
            }
            if ds.layout == hdf5::Layout::Contiguous {
                if let (Some(a), n) = ds.contiguous {
                    if n > 0 && self.h.src.within(a, n) {
                        let form = json!({"geometry": {"shape": ds.dims, "size": ds.datatype.size}, "codec": {"filters": ds.filters}, "recipe": [["src", 0, null]]});
                        self.ir.data(o, "data", NO_INDEX, 0, (a, n), &form.to_string())?;
                    }
                }
            }
        }
        Ok(())
    }

    pub fn collection(&mut self, parent: u32, k: u64, c: &hdf5::Collection) -> Res<()> {
        let s = self.ir.struct_(parent, "", k, 0, None)?;
        self.val(s, "header", NO_INDEX, "{signature:ascii[4],version:u1,reserved:bytes[3],size:<u8}", c.at, 16)?;
        let mut objs = None;
        let oty = "{index:<u2,references:<u2,reserved:bytes[4],size:<u8}";
        for (p, &(i, a, n)) in c.order.iter().enumerate() {
            if self.full() {
                break;
            }
            if i == 0 {
                let f = self.ir.struct_(s, "free", NO_INDEX, 0, None)?;
                self.val(f, "header", NO_INDEX, oty, a, 16.min(c.at + c.size - a))?;
                continue;
            }
            let os = match objs {
                Some(x) => x,
                None => {
                    let x = self.ir.struct_(s, "objects", NO_INDEX, 0, None)?;
                    objs = Some(x);
                    x
                }
            };
            let x = self.ir.struct_(os, "", p as u64, 0, None)?;
            self.val(x, "header", NO_INDEX, oty, a, 16)?;
            self.bytes(x, "data", NO_INDEX, a + 16, n)?;
        }
        Ok(())
    }
}
