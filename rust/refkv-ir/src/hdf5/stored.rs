//! The stored arrays of an HDF5 file (spec/stored-array.md §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): a description
//! for every dataset the walk visited, keyed by its `objects/<i>` struct, and a chunk map row for
//! every unit it holds, built from the finished IR. Shared by the HDF5 and IMS parsers, which emit
//! the IR through [`super::walk::Emit`].

use super::walk::{Recorded, Visit};
use super::{le, parse_dataspace, parse_datatype, parse_filters, ravel, ExternalSlot, Filter, Header, Layout, Msg, H5};
use crate::array::{AxisUnits, Chain, ChunkMap, Codec, Edge, Fill, Grid, Param, Place, Row, StoredArray};
use crate::beside::{written_key, Written};
use crate::ir::{Ir, Res};
use serde_json::{json, Value as J};
use std::collections::{BTreeMap, HashMap};

/// The fill value message of files before HDF5 1.6.
const OLD_FILL_VALUE: u16 = 0x04;

/// The place of message `k` of header `h` (with `range` within its data): the element the
/// emission made of its data, or, past the room, a range of the parsed source.
fn msg_place(rec: &Recorded, base: u64, h: &Header, k: usize, m: &Msg, range: Option<(u64, u64)>) -> Place {
    match rec.msg.get(&(h.at, k)) {
        Some(&e) => Place::Element { element: e, member: None, range },
        None => {
            let (o, n) = range.unwrap_or((0, m.data.len() as u64));
            Place::Extent { object: None, offset: m.at + base + o, length: n }
        }
    }
}

/// The messages of type `ty`, each with its position in `h.msgs`.
fn all(h: &Header, ty: u16) -> Vec<(usize, &Msg)> {
    h.msgs.iter().enumerate().filter(|(_, m)| m.ty == ty).collect()
}

/// The one message of type `ty`, with its position in `h.msgs`.
fn one(h: &Header, ty: u16) -> Option<(usize, &Msg)> {
    match all(h, ty)[..] {
        [m] => Some(m),
        _ => None,
    }
}

/// Where the dataset declares its elements: its datatype message, or, where the message is
/// shared and the reader read the dataset (`read`), the committed datatype's message it points
/// at. A dataset the reader did not read keeps the shared message itself: the walk may not have
/// read the committed datatype, and the description reads no bytes the walk did not.
fn datatype_place(h5: &mut H5, rec: &Recorded, base: u64, h: &Header, read: bool) -> Res<Option<Place>> {
    let Some((k, m)) = one(h, super::DATATYPE) else { return Ok(None) };
    if m.flags & 2 != 0 && read {
        let committed = h5.committed(&m.data).and_then(|(_, at)| h5.header(at));
        let c = committed.map_err(|_| "internal: a dataset's committed datatype does not read again")?;
        if let Some((k2, m2)) = one(&c, super::DATATYPE) {
            return Ok(Some(msg_place(rec, base, &c, k2, m2, None)));
        }
    }
    Ok(Some(msg_place(rec, base, h, k, m, None)))
}

/// What a fill value message says.
#[derive(Debug, PartialEq)]
enum Said {
    Undefined,
    Default,
    /// a value: its (offset, length) in the message's data
    Given(u64, u64),
}

/// A value of `n` bytes at `at` of a message's data `d`, or None where it runs past them.
fn given(d: &[u8], at: u64, n: u64) -> Option<Said> {
    (at + n <= d.len() as u64).then_some(Said::Given(at, n))
}

/// What a fill value message (0x05) says, as the library decodes it (H5Ofill.c's
/// `H5O_fill_new_decode`): in versions 1 and 2 undefined unless the message says a value is
/// defined, then the default for a size of 0; in version 3 by its flags. None where it does not
/// read: an unknown version, a value cut short, a size the library reads as negative, or both of
/// version 3's flags.
fn new_fill(d: &[u8]) -> Option<Said> {
    match le(d, 0, 1).ok()? {
        1 | 2 => {
            if le(d, 3, 1).ok()? == 0 {
                return Some(Said::Undefined);
            }
            match le(d, 4, 4).ok()? {
                0 => Some(Said::Default),
                n if n >= 1 << 31 => None,
                n => given(d, 8, n),
            }
        }
        3 => {
            let flags = le(d, 1, 1).ok()?;
            match (flags & 0x10 != 0, flags & 0x20 != 0) {
                (true, true) => None,
                (true, false) => Some(Said::Undefined),
                (false, false) => Some(Said::Default),
                (false, true) => match le(d, 2, 4).ok()? {
                    0 => Some(Said::Default),
                    n => given(d, 6, n),
                },
            }
        }
        _ => None,
    }
}

/// What an old fill value message (0x04) says: a size of 0 is undefined (`H5D__open_oid` maps it
/// to -1).
fn old_fill(d: &[u8]) -> Option<Said> {
    match le(d, 0, 4).ok()? {
        0 => Some(Said::Undefined),
        n => given(d, 4, n),
    }
}

/// The dataset's fill value, of an element of `size` bytes, in the three states HDF5 tells apart
/// (H5P's `fill_value_defined`), with the places that join the description's declarations: those
/// of fill value messages whose value the description cannot give (a shared message, several, one
/// that does not read, or a value that is not of the element's size), which then is `undefined`.
/// A header with no fill value message of either kind defines none (`H5D__open_oid`).
fn fill_of(rec: &Recorded, base: u64, h: &Header, size: Option<u64>) -> (Fill, Vec<Place>) {
    let new = all(h, super::FILL_VALUE);
    let (found, said): (Vec<(usize, &Msg)>, fn(&[u8]) -> Option<Said>) = if new.is_empty() { (all(h, OLD_FILL_VALUE), old_fill) } else { (new, new_fill) };
    let places = |found: &[(usize, &Msg)]| found.iter().map(|&(k, m)| msg_place(rec, base, h, k, m, None)).collect();
    let (k, m) = match found[..] {
        [] => return (Fill::Undefined, Vec::new()),
        [(k, m)] if m.flags & 2 == 0 => (k, m),
        _ => return (Fill::Undefined, places(&found)),
    };
    match said(&m.data) {
        Some(Said::Undefined) => (Fill::Undefined, Vec::new()),
        Some(Said::Default) => (Fill::FormatDefault, Vec::new()),
        Some(Said::Given(at, n)) if size == Some(n) => (Fill::Given(Param::Source(msg_place(rec, base, h, k, m, Some((at, n))))), Vec::new()),
        _ => (Fill::Undefined, places(&found)),
    }
}

fn named(name: &str, parameters: Vec<(&str, Param)>) -> Codec {
    Codec { name: name.into(), parameters: parameters.into_iter().map(|(k, x)| (k.to_string(), x)).collect() }
}

/// The codec of filter `f`, value `j` of whose client data lies at `cd(j)`: its entry of the
/// list (spec/stored-array.md §3.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#32-the-list>), or the general
/// fallback `source` for a filter the list has no name for, or whose client data are too few for
/// its entry (the library's filter then refuses the chunk).
fn codec(f: &Filter, cd: impl Fn(usize, usize) -> Param) -> Codec {
    let n = f.client.len();
    let at = |j: usize| cd(j, 1);
    match f.id {
        super::DEFLATE => named("zlib", vec![]),
        super::SHUFFLE if n >= 1 => named("shuffle", vec![("element_size", at(0))]),
        super::FLETCHER32 => named("h5_fletcher32", vec![]),
        SZIP if n >= 4 => named("h5_szip", vec![("options", at(0)), ("pixels_per_block", at(1)), ("bits_per_pixel", at(2)), ("pixels_per_scanline", at(3))]),
        NBIT if n >= 1 => named("h5_nbit", vec![("parameters", cd(0, n))]),
        SCALEOFFSET if n >= 1 => named("h5_scaleoffset", vec![("parameters", cd(0, n))]),
        BZIP2 => named("bzip2", vec![]),
        super::LZF => named("lzf", vec![]),
        super::BLOSC => named("blosc", vec![]),
        super::LZ4 => named("h5_lz4", vec![]),
        super::BITSHUFFLE if n >= 3 => {
            let mut p = vec![("element_size", at(2))];
            if n >= 4 {
                p.push(("block_size", at(3)));
            }
            if n >= 5 {
                p.push(("compression", at(4)));
            }
            named("h5_bitshuffle", p)
        }
        super::ZSTD => named("zstd", vec![]),
        _ => {
            let mut p = vec![("format", Param::Value(json!("hdf5"))), ("id", Param::Value(json!(f.id)))];
            if let Some(name) = &f.name {
                p.push(("name", Param::Value(json!(name))));
            }
            if n >= 1 {
                p.push(("parameters", cd(0, n)));
            }
            named("source", p)
        }
    }
}

const SZIP: u64 = 4;
const NBIT: u64 = 5;
const SCALEOFFSET: u64 = 6;
const BZIP2: u64 = 307;

/// A grid of one unit holding the whole array: each axis `Each` of its length, an empty `List`
/// for an axis of length 0.
fn whole(dims: &[u64]) -> Grid {
    Grid { axes: dims.iter().map(|&n| if n == 0 { AxisUnits::List(Vec::new()) } else { AxisUnits::Each(n) }).collect(), edge: Edge::Short }
}

/// The ranges, in order, of the external files that hold a dataset of `total` bytes, by the
/// sibling keys of its `slots`; None where a name has no key, the slots do not hold the data, or
/// the run did not find a file, or found it shorter than its part (`found`: the siblings the run
/// found, with their sizes).
pub(super) fn external_ranges(slots: &[ExternalSlot], total: u128, found: &HashMap<String, u64>) -> Option<Vec<(String, u64, u64)>> {
    let mut left = total;
    let mut ranges = Vec::new();
    for s in slots {
        if left == 0 {
            break;
        }
        let key = written_key(&s.name, "", Written::default())?;
        let n = if s.size == super::EXTERNAL_UNLIMITED { left } else { (s.size as u128).min(left) };
        if s.offset as u128 + n > *found.get(&key)? as u128 {
            return None;
        }
        if n > 0 {
            ranges.push((key, s.offset, u64::try_from(n).ok()?));
        }
        left -= n;
    }
    (left == 0).then_some(ranges)
}

/// What the emission recorded of the datasets, beside their headers.
pub struct Emitted<'a> {
    /// `order[i]` is the header address of the object whose struct is `structs[i]`
    pub order: &'a [u64],
    pub structs: &'a [u32],
    pub rec: &'a Recorded,
    /// each contiguous dataset's data element, by its header's address
    pub data_of: &'a HashMap<u64, u32>,
}

/// Describes the datasets among the objects `visited` read, as the emission `em` described them
/// in the finished IR `ir`: with `slots`, the external files' slots of each dataset the parser
/// resolved, and `found`, the siblings a store run found (None in a file run, which looks for
/// none: a dataset in external files then has no grid).
pub fn describe(ir: &mut Ir, h5: &mut H5, em: &Emitted, visited: &HashMap<u64, Visit>, slots: &HashMap<u64, Vec<ExternalSlot>>, found: Option<&HashMap<String, u64>>) -> Res<()> {
    let base = h5.src.base;
    let file_size = h5.file_size();
    let mut arrays: Vec<StoredArray> = Vec::new();
    let mut chains: Vec<Chain> = Vec::new();
    let mut chain_index: BTreeMap<String, u32> = BTreeMap::new();
    let mut map = ChunkMap::default();
    let mut chain_of = |c: Chain| -> u32 {
        let key = J::Array(c.iter().map(Codec::to_json).collect()).to_string();
        *chain_index.entry(key).or_insert_with(|| {
            chains.push(c);
            (chains.len() - 1) as u32
        })
    };
    for (i, &at) in em.order.iter().enumerate() {
        let Some(vis) = visited.get(&at) else { continue };
        let Some(h) = vis.header.clone() else { continue };
        if !h.has(super::LAYOUT) || h.has(super::SYMBOL_TABLE) || h.has(super::LINK_INFO) {
            continue;
        }
        let k = arrays.len() as u32;
        let ds = vis.ds.as_ref();
        // a dataset the reader does not read: what its messages give, and no grid
        let (shape, size) = match ds {
            Some(ds) => ((ds.layout != Layout::Null).then(|| ds.dims.clone()), ds.datatype.size),
            None => (
                one(&h, super::DATASPACE).filter(|(_, m)| m.flags & 2 == 0).and_then(|(_, m)| parse_dataspace(&m.data).ok()).and_then(|s| s.dims),
                one(&h, super::DATATYPE).filter(|(_, m)| m.flags & 2 == 0).and_then(|(_, m)| parse_datatype(&m.data).ok()).map_or(0, |d| d.size),
            ),
        };
        let size = (size > 0 && size <= u64::MAX / 8).then_some(size);
        let (fill, undecoded) = fill_of(em.rec, base, &h, size);
        let mut a = StoredArray {
            element: em.structs[i],
            index: None,
            declarations: datatype_place(h5, em.rec, base, &h, ds.is_some())?.into_iter().chain(undecoded).collect(),
            shape,
            axes: None,
            item_bits: size.map(|n| n * 8),
            pad_from: None,
            grid: None,
            chains: Vec::new(),
            fill,
            unit_parameters: Vec::new(),
        };
        let Some(ds) = ds.filter(|_| size.is_some()) else {
            arrays.push(a);
            continue;
        };
        let unit = |row: Row| Row { array: k, coords: vec![0; ds.dims.len()], ..row };
        let holds = !ds.dims.contains(&0);
        // the bytes of every element: data stored whole that is of another length gives no grid
        let total = super::product(&ds.dims).saturating_mul(ds.datatype.size as u128);
        let mut rows: Vec<Row> = Vec::new();
        let mut grid = None;
        let mut chain = Vec::new();
        match ds.layout {
            Layout::Null | Layout::Virtual => {}
            // a chunk index the reader did not read gives no grid: its rows would be a map cut short
            Layout::Chunked => {
                if let (Some(ch), Ok(filters)) = (&vis.chunks, parse_filters(one(&h, super::FILTERS).map(|(_, m)| &m.data[..]))) {
                    chain = filters
                        .iter()
                        .map(|f| {
                            codec(f, |j, n| {
                                let (fk, fm) = one(&h, super::FILTERS).expect("a pipeline of filters has its message");
                                Param::Source(msg_place(em.rec, base, &h, fk, fm, Some((f.client_at + 4 * j as u64, 4 * n as u64))))
                            })
                        })
                        .collect();
                    let every = if chain.len() >= 64 { u64::MAX } else { (1u64 << chain.len()) - 1 };
                    grid = Some(Grid { axes: ds.chunk.iter().map(|&c| AxisUnits::Each(c)).collect(), edge: Edge::Full });
                    for (coords, addr, n, mask) in &ch.list {
                        let mut row = Row { array: k, coords: coords.clone(), ..Row::default() };
                        if let Some(&e) = em.rec.chunk.get(&(at, ravel(coords, &ds.grid))) {
                            row.element = Some(e);
                        } else if let Some(&(e, first)) = em.rec.implicit.get(&at) {
                            // the one element of an implicit index's chunks: alone where the chunk is all of it
                            row.element = Some(e);
                            row.range = Some((addr - first, *n)).filter(|&r| r != (0, ir.len[e as usize]));
                        } else {
                            row.ranges = Some(vec![(None, addr + base, *n)]);
                        }
                        // the mask's bits past the pipeline name no filter
                        row.skips = Some(*mask as u64 & every).filter(|&s| s != 0);
                        rows.push(row);
                    }
                }
            }
            Layout::Contiguous if h.has(super::EXTERNAL_FILES) => {
                if let Some(ranges) = found.zip(slots.get(&at)).and_then(|(f, sl)| external_ranges(sl, total, f)) {
                    grid = Some(whole(&ds.dims));
                    if holds && !ranges.is_empty() {
                        let ranges = ranges.into_iter().map(|(key, o, n)| (Some(key), o, n)).collect();
                        rows.push(unit(Row { ranges: Some(ranges), ..Row::default() }));
                    }
                }
            }
            Layout::Contiguous => match ds.contiguous {
                // no address: the source holds no unit
                (None, _) => grid = Some(whole(&ds.dims)),
                (Some(addr), n) => {
                    let within = addr.checked_add(base).and_then(|o| o.checked_add(n)).is_some_and(|end| end <= file_size);
                    if within && n as u128 == total {
                        grid = Some(whole(&ds.dims));
                        if holds && n > 0 {
                            rows.push(unit(match em.data_of.get(&at) {
                                Some(&e) => Row { element: Some(e), ..Row::default() },
                                None => Row { ranges: Some(vec![(None, addr + base, n)]), ..Row::default() },
                            }));
                        }
                    }
                }
            },
            Layout::Compact if ds.compact.1.len() as u128 != total => {}
            Layout::Compact => {
                grid = Some(whole(&ds.dims));
                let n = ds.compact.1.len() as u64;
                if let (true, Some((lk, lm))) = (holds && n > 0, one(&h, super::LAYOUT)) {
                    rows.push(unit(match msg_place(em.rec, base, &h, lk, lm, Some((ds.compact.0 - lm.at, n))) {
                        Place::Element { element, range, .. } => Row { element: Some(element), range, ..Row::default() },
                        Place::Extent { offset, length, .. } => Row { ranges: Some(vec![(None, offset, length)]), ..Row::default() },
                    }));
                }
            }
        }
        if grid.is_some() {
            a.grid = grid;
            a.chains = vec![chain_of(chain)];
            for r in rows {
                map.push(r)?;
            }
        }
        arrays.push(a);
    }
    ir.describe(arrays, chains, map)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// What each form of the fill value messages says, as the library decodes it.
    #[test]
    fn fill_messages_say_their_state() {
        // versions 1 and 2: version, allocation time, write time, defined, then a size and value
        assert_eq!(new_fill(&[1, 2, 2, 0, 0, 0, 0, 0]), Some(Said::Undefined));
        assert_eq!(new_fill(&[1, 2, 2, 0, 2, 0, 0, 0, 7, 7]), Some(Said::Undefined));
        assert_eq!(new_fill(&[1, 2, 2, 1, 0, 0, 0, 0]), Some(Said::Default));
        assert_eq!(new_fill(&[2, 2, 2, 0]), Some(Said::Undefined));
        assert_eq!(new_fill(&[2, 2, 2, 1, 0, 0, 0, 0]), Some(Said::Default));
        assert_eq!(new_fill(&[2, 2, 2, 1, 2, 0, 0, 0, 7, 7]), Some(Said::Given(8, 2)));
        // version 3: version, flags (0x10 undefined, 0x20 a value), then a size and value
        assert_eq!(new_fill(&[3, 0x1a]), Some(Said::Undefined));
        assert_eq!(new_fill(&[3, 0x0a]), Some(Said::Default));
        assert_eq!(new_fill(&[3, 0x2a, 0, 0, 0, 0]), Some(Said::Default));
        assert_eq!(new_fill(&[3, 0x2a, 2, 0, 0, 0, 7, 7]), Some(Said::Given(6, 2)));
        // the old message: a size and value
        assert_eq!(old_fill(&[0, 0, 0, 0]), Some(Said::Undefined));
        assert_eq!(old_fill(&[2, 0, 0, 0, 7, 7]), Some(Said::Given(4, 2)));
    }

    #[test]
    fn a_fill_message_of_an_unknown_version_does_not_read() {
        assert_eq!(new_fill(&[4, 0, 0, 0, 0, 0, 0, 0]), None);
    }

    #[test]
    fn a_fill_value_cut_short_does_not_read() {
        assert_eq!(new_fill(&[2, 2, 2, 1, 4, 0, 0, 0, 7, 7]), None);
        assert_eq!(new_fill(&[3, 0x2a, 4, 0, 0, 0, 7, 7]), None);
        assert_eq!(old_fill(&[4, 0, 0, 0, 7, 7]), None);
    }

    #[test]
    fn a_fill_size_the_library_reads_as_negative_does_not_read() {
        assert_eq!(new_fill(&[2, 2, 2, 1, 0xff, 0xff, 0xff, 0xff]), None);
    }

    #[test]
    fn a_fill_message_both_undefined_and_given_does_not_read() {
        assert_eq!(new_fill(&[3, 0x3a, 2, 0, 0, 0, 7, 7]), None);
    }

    /// A filter of `n` client data values.
    fn filter(id: u64, n: usize, name: Option<&str>) -> Filter {
        Filter { id, name: name.map(String::from), client: vec![0; n], client_at: 0 }
    }

    /// The codec of `f`, each place parameter shown as the (first, count) of its client data values.
    fn codec_of(f: &Filter) -> (String, Vec<(String, J)>) {
        let c = codec(f, |j, n| Param::Value(json!([j, n])));
        let shown = |p: Param| match p {
            Param::Value(v) => v,
            Param::Source(_) => unreachable!(),
        };
        (c.name, c.parameters.into_iter().map(|(k, p)| (k, shown(p))).collect())
    }

    fn params(p: &[(&str, J)]) -> Vec<(String, J)> {
        p.iter().map(|(k, v)| (k.to_string(), v.clone())).collect()
    }

    /// Each filter's entry of the list, its parameters the client data values the entry names.
    #[test]
    fn a_filter_is_its_codec_of_the_list() {
        for (id, name) in [(super::super::DEFLATE, "zlib"), (super::super::FLETCHER32, "h5_fletcher32"), (BZIP2, "bzip2"), (super::super::LZF, "lzf"),
                           (super::super::BLOSC, "blosc"), (super::super::LZ4, "h5_lz4"), (super::super::ZSTD, "zstd")] {
            assert_eq!(codec_of(&filter(id, 7, Some("named"))), (name.to_string(), vec![]), "{id}");
            assert_eq!(codec_of(&filter(id, 0, None)), (name.to_string(), vec![]), "{id}");
        }
        assert_eq!(codec_of(&filter(super::super::SHUFFLE, 1, None)), ("shuffle".into(), params(&[("element_size", json!([0, 1]))])));
        let szip = params(&[("bits_per_pixel", json!([2, 1])), ("options", json!([0, 1])), ("pixels_per_block", json!([1, 1])), ("pixels_per_scanline", json!([3, 1]))]);
        assert_eq!(codec_of(&filter(SZIP, 4, None)), ("h5_szip".into(), szip));
        assert_eq!(codec_of(&filter(NBIT, 8, None)), ("h5_nbit".into(), params(&[("parameters", json!([0, 8]))])));
        assert_eq!(codec_of(&filter(SCALEOFFSET, 20, None)), ("h5_scaleoffset".into(), params(&[("parameters", json!([0, 20]))])));
        let (element, block, compression) = (("element_size", json!([2, 1])), ("block_size", json!([3, 1])), ("compression", json!([4, 1])));
        assert_eq!(codec_of(&filter(super::super::BITSHUFFLE, 3, None)), ("h5_bitshuffle".into(), params(&[element.clone()])));
        assert_eq!(codec_of(&filter(super::super::BITSHUFFLE, 4, None)), ("h5_bitshuffle".into(), params(&[block.clone(), element.clone()])));
        assert_eq!(codec_of(&filter(super::super::BITSHUFFLE, 5, None)), ("h5_bitshuffle".into(), params(&[block, compression, element])));
    }

    /// A filter the list has no name for is `source`, by its id, with its name and client data
    /// where it has them.
    #[test]
    fn an_unknown_filter_is_the_source_codec() {
        let (format, id) = (("format", json!("hdf5")), ("id", json!(40000)));
        assert_eq!(codec_of(&filter(40000, 0, None)), ("source".into(), params(&[format.clone(), id.clone()])));
        let all = params(&[format, id, ("name", json!("mine")), ("parameters", json!([0, 3]))]);
        assert_eq!(codec_of(&filter(40000, 3, Some("mine"))), ("source".into(), all));
    }

    /// A filter with fewer client data values than its entry's parameters is `source`.
    #[test]
    fn a_filter_of_too_few_client_data_is_the_source_codec() {
        for (id, n) in [(super::super::SHUFFLE, 0), (SZIP, 3), (NBIT, 0), (SCALEOFFSET, 0), (super::super::BITSHUFFLE, 2)] {
            let (name, p) = codec_of(&filter(id, n, None));
            assert_eq!((name.as_str(), p[1].clone()), ("source", ("id".to_string(), json!(id))), "{id}");
        }
    }

    /// A header at 100 of the messages `(type, flags, data)`, each's data 50 bytes after the last's.
    fn header(msgs: &[(u16, u8, &[u8])]) -> Header {
        let msgs = msgs.iter().enumerate()
            .map(|(k, &(ty, flags, data))| Msg { ty, flags, head: 108 + 50 * k as u64, at: 116 + 50 * k as u64, data: data.to_vec() })
            .collect();
        Header { at: 100, v2: false, flags: 0, prefix: 16, blocks: vec![(116, 400)], msgs, mh: 8 }
    }

    /// The place of `length` bytes at `offset` of the parsed source.
    fn extent(offset: u64, length: u64) -> Place {
        Place::Extent { object: None, offset, length }
    }

    const GIVEN: &[u8] = &[3, 0x2a, 2, 0, 0, 0, 7, 7];

    /// The fill value in each state, with nothing added to the declarations; a given value is the
    /// range of its message's element, or, where the emission made none, of the source past `base`.
    #[test]
    fn a_fill_value_is_one_of_three_states() {
        let rec = Recorded::default();
        let of = |msgs: &[(u16, u8, &[u8])]| fill_of(&rec, 1000, &header(msgs), Some(2));
        assert_eq!(of(&[]), (Fill::Undefined, vec![]));
        assert_eq!(of(&[(0x03, 0, &[0; 8])]), (Fill::Undefined, vec![]));
        assert_eq!(of(&[(super::super::FILL_VALUE, 0, &[3, 0x1a])]), (Fill::Undefined, vec![]));
        assert_eq!(of(&[(super::super::FILL_VALUE, 0, &[3, 0x0a])]), (Fill::FormatDefault, vec![]));
        assert_eq!(of(&[(OLD_FILL_VALUE, 0, &[0, 0, 0, 0])]), (Fill::Undefined, vec![]));
        assert_eq!(of(&[(OLD_FILL_VALUE, 0, &[2, 0, 0, 0, 7, 7])]), (Fill::Given(Param::Source(extent(1120, 2))), vec![]));
        // the new message is the one read where a header has both
        let both: &[(u16, u8, &[u8])] = &[(OLD_FILL_VALUE, 0, &[2, 0, 0, 0, 7, 7]), (super::super::FILL_VALUE, 0, GIVEN)];
        assert_eq!(of(both), (Fill::Given(Param::Source(extent(1172, 2))), vec![]));
        let mut rec = Recorded::default();
        rec.msg.insert((100, 1), 9);
        let place = Place::Element { element: 9, member: None, range: Some((6, 2)) };
        assert_eq!(fill_of(&rec, 1000, &header(both), Some(2)), (Fill::Given(Param::Source(place)), vec![]));
    }

    #[test]
    fn a_shared_fill_message_is_a_declaration() {
        let h = header(&[(super::super::FILL_VALUE, 2, &[0; 10])]);
        assert_eq!(fill_of(&Recorded::default(), 0, &h, Some(2)), (Fill::Undefined, vec![extent(116, 10)]));
    }

    #[test]
    fn several_fill_messages_are_declarations() {
        let h = header(&[(super::super::FILL_VALUE, 0, GIVEN), (super::super::FILL_VALUE, 0, &[3, 0x0a])]);
        assert_eq!(fill_of(&Recorded::default(), 0, &h, Some(2)), (Fill::Undefined, vec![extent(116, 8), extent(166, 2)]));
    }

    #[test]
    fn a_fill_message_that_does_not_read_is_a_declaration() {
        let h = header(&[(super::super::FILL_VALUE, 0, &[9, 0, 0, 0])]);
        let mut rec = Recorded::default();
        rec.msg.insert((100, 0), 4);
        let place = Place::Element { element: 4, member: None, range: None };
        assert_eq!(fill_of(&rec, 0, &h, Some(2)), (Fill::Undefined, vec![place]));
    }

    #[test]
    fn a_fill_value_not_of_the_elements_size_is_a_declaration() {
        let h = header(&[(super::super::FILL_VALUE, 0, GIVEN)]);
        assert_eq!(fill_of(&Recorded::default(), 0, &h, Some(4)), (Fill::Undefined, vec![extent(116, 8)]));
        assert_eq!(fill_of(&Recorded::default(), 0, &h, None), (Fill::Undefined, vec![extent(116, 8)]));
    }
}
