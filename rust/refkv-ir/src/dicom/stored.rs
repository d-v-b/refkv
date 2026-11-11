//! The stored arrays of a DICOM file (spec/stored-array.md §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): the top-level
//! Pixel Data, keyed by its element, a frame a unit, or every frame one unit where 1-bit frames
//! do not end on a byte; each lookup table of a `PALETTE COLOR` image the profile reads, keyed by
//! its own element; and each Pixel Data in an item of a sequence (an icon image), keyed by the
//! sequence of the top-level dataset it lies in. Built from the finished IR, whose elements say
//! where each unit's bytes are, and from what the parser read of the image; of a Pixel Data in an
//! item, from the values the IR holds of its item's attributes.

use super::elements::{tag_key, El};
use super::json::is_integer;
use super::{first_string, frame_ranges, Image, Layout};
use crate::array::{AxisUnits, Chain, ChunkMap, Codec, Edge, Fill, Grid, Param, Place, Row, StoredArray};
use crate::ir::{Ir, Res, DATA, NONE, NO_INDEX, STRUCT, VALUE};
use serde_json::{json, Value as J};
use std::collections::{BTreeMap, HashMap};

/// The attributes that declare what a Pixel Data's elements are, after the transfer syntax:
/// Photometric Interpretation, Bits Allocated, Bits Stored, High Bit, Pixel Representation, Pixel
/// Padding Value, Pixel Padding Range Limit, and the palette's descriptors, tables and segmented
/// tables.
const DECLARING: [u32; 16] = [
    0x0028_0004, 0x0028_0100, 0x0028_0101, 0x0028_0102, 0x0028_0103, 0x0028_0120, 0x0028_0121, 0x0028_1101, 0x0028_1102, 0x0028_1103,
    0x0028_1201, 0x0028_1202, 0x0028_1203, 0x0028_1221, 0x0028_1222, 0x0028_1223,
];
const TRANSFER_SYNTAX: u32 = 0x0002_0010;

/// The descriptions, their chains and their rows, and the keys the facts give: the top-level
/// Pixel Data's element; the lookup tables' elements, red, green and blue; and each element that
/// keys the Pixel Data of its items, with how many it keys.
pub(super) struct Described {
    pub arrays: Vec<StoredArray>,
    pub chains: Vec<Chain>,
    pub map: ChunkMap,
    pub pixel: u32,
    pub palette: Option<Vec<u32>>,
    pub nested: Vec<(u32, u64)>,
}

/// The frames a Pixel Data holds, as its attributes say.
struct Frames {
    rows: u64,
    columns: u64,
    samples: u64,
    /// each sample of a frame a plane of its own, the planes one after another
    planar: bool,
    /// Number of Frames, where the dataset has one
    declared: Option<u64>,
}

impl Frames {
    fn count(&self) -> u64 {
        self.declared.unwrap_or(1)
    }

    /// A frame's axes, in storage order: the samples last, or first where each is a plane; none
    /// for one sample.
    fn within(&self) -> Vec<u64> {
        match (self.samples, self.planar) {
            (1, _) => vec![self.rows, self.columns],
            (s, true) => vec![s, self.rows, self.columns],
            (s, false) => vec![self.rows, self.columns, s],
        }
    }

    /// The frames' axis, where Number of Frames declares one, then a frame's.
    fn shape(&self) -> Vec<u64> {
        self.declared.into_iter().chain(self.within()).collect()
    }

    /// `together` frames a unit, whole.
    fn grid(&self, together: u64) -> Grid {
        let axes = self.declared.map(|_| together).into_iter().chain(self.within()).map(AxisUnits::Each).collect();
        Grid { axes, edge: Edge::Short }
    }

    fn coords(&self, unit: u64) -> Vec<u64> {
        let mut coords = vec![0; self.shape().len()];
        if self.declared.is_some() {
            coords[0] = unit;
        }
        coords
    }
}

/// How native frames of `bits` bits a sample are cut into units, in a value of `length` bytes:
/// (the frames of a unit, a unit's bytes, the units). A frame is a unit, but 1-bit frames, which
/// follow each other with no padding, are one unit together where a frame's bits do not end on a
/// byte (spec/zarrify/dicom.md §7.5
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#75-frames>). None where the
/// value does not hold the frames.
fn cut(f: &Frames, bits: u64, length: u64) -> Option<(u64, u64, u64)> {
    let n = f.count() as u128;
    let frame_bits = (f.rows as u128).checked_mul(f.columns as u128)?.checked_mul(f.samples as u128)?.checked_mul(bits as u128)?;
    let frame = frame_bits.div_ceil(8);
    let whole = bits == 1 && !frame_bits.is_multiple_of(8);
    let needed = if whole { n.checked_mul(frame_bits)?.div_ceil(8) } else { n.checked_mul(frame)? };
    if needed > length as u128 {
        return None;
    }
    Some(if whole { (f.count(), needed as u64, 1) } else { (1, frame as u64, f.count()) })
}

/// The chain of native samples: stored as they are, or, of 1 bit, packed from each byte's least
/// significant bit.
fn native_chain(bits: u64) -> Chain {
    if bits == 1 { vec![codec("lsb_first", [("bits", Param::Value(json!(1)))])] } else { Vec::new() }
}

fn codec<const N: usize>(name: &str, parameters: [(&str, Param); N]) -> Codec {
    Codec { name: name.into(), parameters: parameters.into_iter().map(|(k, v)| (k.to_string(), v)).collect() }
}

/// The child of `parent` named `name`, with no name index.
fn child(ir: &Ir, parent: u32, name: &str) -> Option<u32> {
    ir.children(parent).iter().copied().find(|&c| ir.nidx[c as usize] == NO_INDEX && ir.names.get(ir.name[c as usize]) == name)
}

/// The header of a data element the IR holds as one value (`{H,v:T}`, spec/zarrify/dicom.md §5.4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#54-elements>), read off its
/// type: its bytes, whether it has a VR, and its byte order.
struct Header {
    length: u64,
    explicit: bool,
    little: bool,
}

fn header(ir: &Ir, e: u32) -> Option<Header> {
    if ir.kind[e as usize] != VALUE {
        return None;
    }
    let ty = ir.types.get(ir.ty[e as usize]);
    let order = ty.strip_prefix("{group:")?;
    let length = if ty.contains(",reserved:") { 12 } else { 8 };
    (ir.len[e as usize] >= length).then_some(Header { length, explicit: ty.contains(",vr:ascii[2],"), little: order.starts_with('<') })
}

/// The value of the attribute `tag` of the dataset `ds`, as the IR holds it: its bytes, in the
/// byte order `little`, where it has any and its VR is `vr` (or it has none, in implicit VR).
fn attribute<'a>(ir: &'a Ir, ds: u32, tag: u32, vr: &str) -> Option<(&'a [u8], bool)> {
    let c = child(ir, ds, &tag_key(tag))?;
    let h = header(ir, c)?;
    let raw = ir.value_bytes(c)?;
    let value = raw.get(h.length as usize..).filter(|v| !v.is_empty())?;
    (!h.explicit || &raw[4..6] == vr.as_bytes()).then_some((value, h.little))
}

/// The first value of an attribute of VR US.
fn unsigned(ir: &Ir, ds: u32, tag: u32) -> Option<u64> {
    let (v, little) = attribute(ir, ds, tag, "US")?;
    (v.len() % 2 == 0).then(|| if little { u16::from_le_bytes([v[0], v[1]]) } else { u16::from_be_bytes([v[0], v[1]]) } as u64)
}

/// A unit's bytes, named in the finished IR: an element (in a member of its run), all of it or a
/// range of it; or ranges of the parsed source.
enum Named {
    Element(u32, Option<u64>, Option<(u64, u64)>),
    Ranges(Vec<(u64, u64)>),
}

/// The bytes `(offset, length)` of the source, which the parser gave the element `e` (in member
/// `member` of its run): that element, alone where they are all of it, else with their range of
/// it; and the range of the source where the finished IR has no such element that holds them
/// (none of the parser's layouts makes one: a frame's bytes are no other element's).
fn named(ir: &Ir, roots: &[u32], e: Option<(u32, Option<u64>)>, (offset, length): (u64, u64)) -> Named {
    let ranges = Named::Ranges(vec![(offset, length)]);
    let Some((e, member)) = e else { return ranges };
    let mut start = ir.start[e as usize];
    if let Some(j) = member {
        let Some((_, stride)) = ir.run(roots[e as usize]) else { return ranges };
        start += j * stride;
    }
    let len = ir.len[e as usize];
    if !matches!(ir.kind[e as usize], DATA | VALUE) || ir.space[e as usize] != 0 || offset < start || offset - start > len || length > len - (offset - start) {
        return ranges;
    }
    let range = (offset - start, length);
    Named::Element(e, member, (range != (0, len)).then_some(range))
}

fn row(array: u32, coords: Vec<u64>, unit: Named) -> Row {
    match unit {
        Named::Element(e, member, range) => Row { array, coords, element: Some(e), member, range, ..Row::default() },
        Named::Ranges(r) => Row { array, coords, ranges: Some(r.into_iter().map(|(o, n)| (None, o, n)).collect()), ..Row::default() },
    }
}

/// The data elements of the top-level Pixel Data named `name` (`frames/` or `fragments/`), by
/// name index, as the finished IR holds them: one by one, or as the members of a run.
struct Held {
    single: HashMap<u64, u32>,
    runs: Vec<(u64, u64, u32)>,
}

impl Held {
    fn new(ir: &Ir, pixel: u32, name: &str) -> Held {
        let mut held = Held { single: HashMap::new(), runs: Vec::new() };
        for &c in ir.children(pixel) {
            let k = ir.nidx[c as usize];
            if k == NO_INDEX || ir.names.get(ir.name[c as usize]) != name {
                continue;
            }
            match ir.run(c) {
                Some((count, _)) => held.runs.push((k, count, c)),
                None => {
                    held.single.insert(k, c);
                }
            }
        }
        held
    }

    /// The element of name index `k`, with the member of its run it is.
    fn get(&self, k: u64) -> Option<(u32, Option<u64>)> {
        if let Some(&c) = self.single.get(&k) {
            return Some((c, None));
        }
        self.runs.iter().find(|r| r.0 <= k && k - r.0 < r.1).map(|r| (r.2, Some(k - r.0)))
    }
}

struct Build<'a> {
    ir: &'a Ir,
    /// the run root of each element
    roots: Vec<u32>,
    arrays: Vec<StoredArray>,
    chains: Vec<Chain>,
    chain_index: BTreeMap<String, u32>,
    map: ChunkMap,
    /// the entries of the chunk map's limit: an array whose rows alone pass it has no grid
    /// (spec/stored-array.md §6 <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#6-in-the-ir>),
    /// and its rows are never made
    room: u64,
    /// the element of the Transfer Syntax UID, which gives native samples' byte order
    syntax: Option<u32>,
}

impl Build<'_> {
    fn chain(&mut self, chain: Chain) -> u32 {
        let key = J::Array(chain.iter().map(Codec::to_json).collect()).to_string();
        *self.chain_index.entry(key).or_insert_with(|| {
            self.chains.push(chain);
            (self.chains.len() - 1) as u32
        })
    }

    /// The places that declare what the elements of a Pixel Data of the dataset `ds` are (in
    /// member `member` of its run): the Transfer Syntax UID, then each declaring attribute the
    /// dataset has with a value.
    fn declarations(&self, ds: u32, member: Option<u64>) -> Vec<Place> {
        let ir = self.ir;
        let valued = |c: &u32| header(ir, *c).is_some_and(|h| ir.len[*c as usize] > h.length);
        let syntax = self.syntax.map(|element| Place::Element { element, member: None, range: None });
        let attributes = DECLARING.iter().filter_map(|&t| child(ir, ds, &tag_key(t)).filter(valued));
        syntax.into_iter().chain(attributes.map(|element| Place::Element { element, member, range: None })).collect()
    }

    /// A description with no grid: what declares the array, its shape and its sample size, where
    /// its attributes give them.
    fn array(&mut self, element: u32, index: Option<u64>, declarations: Vec<Place>, shape: Option<Vec<u64>>, bits: Option<u64>) -> u32 {
        self.arrays.push(StoredArray {
            element,
            index,
            declarations,
            shape,
            axes: None,
            item_bits: bits,
            pad_from: None,
            grid: None,
            chains: Vec::new(),
            fill: Fill::Undefined,
            unit_parameters: Vec::new(),
        });
        (self.arrays.len() - 1) as u32
    }

    /// Whether `units` rows of `rank` coordinates, with `items` ranges, fit the chunk map's limit.
    fn fits(&self, units: u64, rank: usize, items: u64) -> bool {
        units.checked_mul(1 + rank as u64).and_then(|w| w.checked_add(items)).is_some_and(|w| w <= self.room)
    }

    fn cut(&mut self, array: u32, grid: Grid, chain: Chain) {
        let chain = self.chain(chain);
        let a = &mut self.arrays[array as usize];
        a.grid = Some(grid);
        a.chains = vec![chain];
    }
}

/// The chain of the top-level Pixel Data's frames, by the transfer syntax's codec; None where the
/// frames are not an array of the image's shape.
fn chain_of(img: &Image) -> Option<Chain> {
    let named = |name: &str| Some(vec![codec(name, [])]);
    match img.codec {
        "native" => Some(native_chain(img.bits_allocated)),
        // the list's `jpeg` is of samples of 8 or 16 bits: the parser takes lossless JPEG of Bits
        // Allocated 8 or 16 alone, every frame's precision at most that
        "jpeg" | "ljpeg" => named("jpeg"),
        "jpegls" => named("jpegls"),
        // chroma of half the width: the stream's components are not one array, and OpenJPEG
        // gives them upsampled and converted to RGB
        "jpeg2k" if img.photometric == "YBR_FULL_422" => None,
        "jpeg2k" => named("jpeg2k"),
        "dicomrle" => named("dicom_rle"),
        // an encoding the list has no entry for, by its transfer syntax
        _ => Some(vec![codec("source", [("format", Param::Value(json!("dicom"))), ("id", Param::Value(json!(img.syntax)))])]),
    }
}

/// The top-level Pixel Data's description and rows.
fn pixel_data(b: &mut Build, img: &Image, lay: &Layout, dataset: u32, pixel: u32) -> Res<()> {
    let ir = b.ir;
    let native = img.codec == "native";
    let frames = Frames {
        rows: img.rows,
        columns: img.columns,
        samples: img.spp,
        // an RLE frame decodes to a plane a sample
        planar: img.planar_native || img.codec == "dicomrle",
        declared: img.frames_declared.then_some(img.n),
    };
    let rank = frames.shape().len();
    let array = b.array(pixel, None, b.declarations(dataset, None), Some(frames.shape()), Some(img.bits_allocated));
    let Some(chain) = chain_of(img) else { return Ok(()) };
    // native frames: the units the parser cut them into; else a frame a unit, its fragments' data
    let ranges = if native { Vec::new() } else { frame_ranges(img, lay) };
    let units = if native { lay.stored } else { ranges.len() as u64 };
    let items = ranges.iter().filter(|r| r.len() > 1).map(|r| r.len() as u64).sum();
    if !b.fits(units, rank, items) {
        return Ok(());
    }
    b.cut(array, frames.grid(if lay.stored == 1 { img.n } else { 1 }), chain);
    let held = Held::new(ir, pixel, if native { "frames/" } else { "fragments/" });
    let fragments = lay.items.get(1..).unwrap_or(&[]);
    for u in 0..units {
        let unit = if native {
            named(ir, &b.roots, held.get(u), (img.pixel.value + u * lay.frame_size, lay.frame_size))
        } else {
            let mine = &ranges[u as usize];
            // the fragment that holds the frame's one range: with an Extended Offset Table,
            // frame f's is `fragments/<f>`; else the frame's one fragment that is not empty
            let k = match &lay.eot {
                Some(_) => Some(u),
                None => (lay.bounds[u as usize]..lay.bounds[u as usize + 1]).find(|&k| fragments[k as usize].2 > 0),
            };
            match mine[..] {
                [span] => named(ir, &b.roots, k.and_then(|k| held.get(k)), span),
                // several fragments: bytes no one element holds
                _ => Named::Ranges(mine.clone()),
            }
        };
        b.map.push(row(array, frames.coords(u), unit))?;
    }
    Ok(())
}

/// The lookup tables of a `PALETTE COLOR` image, each an array of its entries; their elements.
fn palette(b: &mut Build, img: &Image, dataset: u32) -> Res<Option<Vec<u32>>> {
    let ir = b.ir;
    let Some(p) = &img.palette else { return Ok(None) };
    let tables: Vec<(El, u32)> = p.els.iter().filter_map(|el| Some((*el, child(ir, dataset, &tag_key(el.tag))?))).collect();
    if tables.len() != 3 {
        return Ok(None);
    }
    for (el, e) in &tables {
        // the table's descriptor declares its entries; the transfer syntax their byte order
        let descriptor = child(ir, dataset, &tag_key(el.tag - 0x100));
        let places = b.syntax.into_iter().chain(descriptor).map(|element| Place::Element { element, member: None, range: None }).collect();
        let array = b.array(*e, None, places, Some(vec![p.entries]), Some(8 * p.cell));
        // entries of one byte in 16-bit words, which Explicit VR Big Endian swaps
        let swapped = p.cell == 1 && !img.enc.little && el.vr_is("OW");
        if swapped || !b.fits(1, 1, 0) {
            continue;
        }
        b.cut(array, Grid { axes: vec![AxisUnits::Each(p.entries)], edge: Edge::Short }, Vec::new());
        b.map.push(row(array, vec![0], named(ir, &b.roots, Some((*e, None)), (el.value, p.entries * p.cell))))?;
    }
    Ok(Some(tables.into_iter().map(|t| t.1).collect()))
}

/// Whether element `i` is an item of a sequence.
fn is_item(ir: &Ir, i: u32) -> bool {
    ir.kind[i as usize] == STRUCT && ir.nidx[i as usize] != NO_INDEX && ir.names.get(ir.name[i as usize]).is_empty()
}

/// Each Pixel Data in an item of a sequence, in the order of the file, keyed by the element of
/// the top-level dataset its items lie in; the keys, with how many arrays each keys.
fn nested(b: &mut Build, img: &Image, pixel: u32) -> Res<Vec<(u32, u64)>> {
    let ir = b.ir;
    let name = tag_key(super::elements::PIXEL_DATA);
    // (key, offset, element, member of its run)
    let mut found: Vec<(u32, u64, u32, Option<u64>)> = Vec::new();
    for e in 1..ir.len() as u32 {
        let i = e as usize;
        if e == pixel || ir.nidx[i] != NO_INDEX || !matches!(ir.kind[i], VALUE | STRUCT) || ir.names.get(ir.name[i]) != name {
            continue;
        }
        // the parent of the outermost item it is in: an element no fold puts in a run
        let (mut key, mut at) = (None, e);
        while at != 0 && at != NONE {
            let parent = ir.parent[at as usize];
            if is_item(ir, at) {
                key = Some(parent);
            }
            at = parent;
        }
        let Some(key) = key.filter(|&k| k != NONE) else { continue };
        match ir.run(b.roots[i]).filter(|_| b.roots[i] != NONE) {
            Some((count, stride)) => found.extend((0..count).map(|j| (key, ir.start[i] + j * stride, e, Some(j)))),
            None => found.push((key, ir.start[i], e, None)),
        }
    }
    found.sort_unstable();
    let mut keys: Vec<(u32, u64)> = Vec::new();
    for (key, _, e, member) in found {
        let index = match keys.last_mut() {
            Some(last) if last.0 == key => {
                last.1 += 1;
                last.1 - 1
            }
            _ => {
                keys.push((key, 1));
                0
            }
        };
        // its dataset: the item it is in (a later Pixel Data of an item is under `duplicates/<k>`)
        let mut ds = ir.parent[e as usize];
        if ir.names.get(ir.name[ds as usize]) == "duplicates/" {
            ds = ir.parent[ds as usize];
        }
        let samples = unsigned(ir, ds, 0x0028_0002);
        let planar = unsigned(ir, ds, 0x0028_0006);
        let count = match attribute(ir, ds, 0x0028_0008, "IS") {
            None => Some(None),
            Some((v, _)) => Some(first_string(v)).filter(|v| is_integer(v) && v.len() <= 16).and_then(|v| std::str::from_utf8(v).ok()?.parse::<u64>().ok()).filter(|&n| n >= 1).map(Some),
        };
        let bits = unsigned(ir, ds, 0x0028_0100).filter(|&x| x >= 1);
        let size = unsigned(ir, ds, 0x0028_0010).zip(unsigned(ir, ds, 0x0028_0011)).filter(|&(r, c)| r >= 1 && c >= 1);
        // the frames' axes: Rows, Columns and Samples per Pixel, with Planar Configuration, which
        // places more than one sample, and a Number of Frames that reads, or none
        let frames = match (size, samples, count) {
            (Some((rows, columns)), Some(s), Some(declared)) if s == 1 || (s > 1 && matches!(planar, Some(0 | 1))) => {
                // a plane a sample: native data of Planar Configuration 1, and RLE frames
                let planes = if ir.kind[e as usize] == VALUE { planar == Some(1) } else { img.codec == "dicomrle" };
                Some(Frames { rows, columns, samples: s, planar: s > 1 && planes, declared })
            }
            _ => None,
        };
        let array = b.array(key, Some(index), b.declarations(ds, member), frames.as_ref().map(Frames::shape), bits);
        // native frames, as the profile reads the top-level ones: of 1 bit (one sample), 8, 16 or
        // 32; of one sample, or of three that are RGB or YBR_FULL; in the transfer syntax's byte
        // order, where a sample of one byte or less is not in 16-bit words that order swaps
        let (Some(frames), Some(bits), Some(h)) = (frames, bits, header(ir, e)) else { continue };
        let photometric = attribute(ir, ds, 0x0028_0004, "CS").map(|(v, _)| first_string(v));
        let colors = frames.samples == 1 || (frames.samples == 3 && matches!(photometric, Some(b"RGB" | b"YBR_FULL")));
        let sized = matches!(bits, 8 | 16 | 32) || (bits == 1 && frames.samples == 1);
        let ordered = h.little == img.enc.little && (h.little || bits >= 16);
        let Some((together, bytes, units)) = cut(&frames, bits, ir.len[e as usize] - h.length).filter(|_| colors && sized && ordered) else { continue };
        if !b.fits(units, frames.shape().len(), 0) {
            continue;
        }
        b.cut(array, frames.grid(together), native_chain(bits));
        for u in 0..units {
            let range = Some((h.length + u * bytes, bytes));
            b.map.push(Row { array, coords: frames.coords(u), element: Some(e), member, range, ..Row::default() })?;
        }
    }
    Ok(keys)
}

/// The descriptions of the file's arrays: the top-level Pixel Data's, the lookup tables' and
/// those of the Pixel Data in items.
pub(super) fn described(ir: &Ir, img: &Image, lay: &Layout) -> Res<Described> {
    let dataset = child(ir, 0, "dataset").ok_or("internal: a DICOM IR without its dataset")?;
    let pixel = child(ir, dataset, &tag_key(super::elements::PIXEL_DATA)).ok_or("internal: a DICOM IR without its Pixel Data")?;
    let syntax = child(ir, 0, "meta").and_then(|m| child(ir, m, &tag_key(TRANSFER_SYNTAX)));
    let room = ir.budget.as_ref().map_or(u64::MAX, |b| b.rows.saturating_sub(b.spent_rows));
    let mut b = Build { ir, roots: ir.run_roots(), arrays: Vec::new(), chains: Vec::new(), chain_index: BTreeMap::new(), map: ChunkMap::default(), room, syntax };
    pixel_data(&mut b, img, lay, dataset, pixel)?;
    let palette = palette(&mut b, img, dataset)?;
    let nested = nested(&mut b, img, pixel)?;
    Ok(Described { arrays: b.arrays, chains: b.chains, map: b.map, pixel, palette, nested })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn frames(rows: u64, columns: u64, samples: u64, declared: Option<u64>) -> Frames {
        Frames { rows, columns, samples, planar: false, declared }
    }

    /// Native frames are a unit each, of whole bytes; 1-bit frames that do not end on a byte are
    /// one unit together, to the next byte. The bytes of the value past them are no unit's.
    #[test]
    fn native_frames_are_a_unit_each_or_one_together() {
        assert_eq!(cut(&frames(4, 6, 1, None), 16, 48), Some((1, 48, 1)));
        assert_eq!(cut(&frames(4, 6, 3, Some(5)), 8, 361), Some((1, 72, 5)));
        assert_eq!(cut(&frames(4, 8, 1, Some(3)), 1, 12), Some((1, 4, 3)));
        assert_eq!(cut(&frames(5, 3, 1, Some(3)), 1, 6), Some((3, 6, 1)));
        assert_eq!(cut(&frames(5, 3, 1, None), 1, 2), Some((1, 2, 1)));
    }

    #[test]
    fn a_value_shorter_than_its_frames_holds_none() {
        assert_eq!(cut(&frames(4, 6, 1, Some(2)), 8, 47), None);
        assert_eq!(cut(&frames(5, 3, 1, Some(3)), 1, 5), None);
    }

    #[test]
    fn frames_past_what_a_file_can_hold_hold_none() {
        assert_eq!(cut(&frames(u64::MAX, u64::MAX, u64::MAX, Some(u64::MAX)), 32, u64::MAX), None);
        assert_eq!(cut(&frames(u64::MAX, u64::MAX, 1, Some(u64::MAX)), 1, u64::MAX), None);
    }

    /// A frame's axes in storage order, after the frames' axis where Number of Frames declares
    /// one: a pixel's samples last, or first where each is a plane.
    #[test]
    fn a_frames_samples_are_last_or_a_plane_each() {
        assert_eq!(frames(4, 6, 1, None).shape(), [4, 6]);
        assert_eq!(frames(4, 6, 3, Some(1)).shape(), [1, 4, 6, 3]);
        assert_eq!(Frames { planar: true, ..frames(4, 6, 3, Some(2)) }.shape(), [2, 3, 4, 6]);
        assert_eq!(Frames { planar: true, ..frames(4, 6, 1, None) }.shape(), [4, 6]);
        assert_eq!(frames(4, 6, 3, Some(2)).coords(1), [1, 0, 0, 0]);
        assert_eq!(frames(4, 6, 3, None).coords(0), [0, 0, 0]);
    }
}
