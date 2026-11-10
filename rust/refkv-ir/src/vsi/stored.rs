//! The stored arrays of a VSI slide (spec/stored-array.md §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>). Of each tile
//! file, an array for each level, keyed by the file's `ets` element with the level as its index,
//! and a chunk map row for every tile of the level's grid. Of the `.vsi` file, the image of each
//! IFD of its chain that has elements, keyed by the IFD's struct and described as a TIFF's, by
//! the TIFF parser's rules (`tiff/stored.rs`). Built on the finished IR, from what the parser
//! read: no tile's bytes, and no strip's.

use super::tiff::{Emitted, Ifd};
use super::{coord_size, ets, EtsFile, Vsi};
use crate::array::{AxisUnits, Chain, ChunkMap, Codec, Edge, Fill, Grid, Param, Place, Row, StoredArray};
use crate::ir::{Ir, Res};
use crate::tiff::stored::{tiled, Descriptions, Fields, Image};
use crate::tiff::{type_size, Tiff};
use serde_json::json;
use std::collections::{BTreeMap, HashMap};

/// The `format` of a `source` codec of a slide's arrays.
const FORMAT: &str = "vsi";

/// Where the ETS header holds its background color: one pixel's samples, in the pixel type.
const BACKGROUND: u64 = 108;

/// An IFD of the `.vsi` file's chain, as its image's description reads it: the fields, read from
/// the file as the parser reads them (`Ifd::ints`: BYTE, SHORT, LONG and IFD values the file
/// holds), and the elements `emit_region` made of the IFD.
struct Read<'a> {
    ifd: &'a Ifd,
    file: &'a [u8],
    made: &'a Emitted,
    /// the `.vsi` file's `header` element, whose first two bytes are the byte order mark
    header: u32,
}

impl Read<'_> {
    fn value(&self, tag: u16, range: Option<(u64, u64)>) -> Option<Place> {
        Some(Place::Element { element: *self.made.values.get(&tag)?, member: None, range })
    }
}

impl Fields for Read<'_> {
    fn has(&self, tag: u16) -> bool {
        self.ifd.entry(tag).is_some()
    }
    fn first(&self, tag: u16) -> Option<u64> {
        self.ifd.int(self.file, tag)
    }
    fn every(&self, tag: u16) -> Option<Vec<u64>> {
        self.ifd.ints(self.file, tag)
    }
    fn value_place(&self, tag: u16) -> Option<Place> {
        self.value(tag, None)
    }
    fn first_param(&self, tag: u16) -> Option<Param> {
        let e = self.ifd.entry(tag).filter(|e| matches!(e.typ, 1 | 3 | 4 | 13))?;
        let range = (e.count > 1).then_some((0, type_size(e.typ)?));
        self.value(tag, range).map(Param::Source)
    }
    fn byte_order(&self) -> Place {
        Place::Element { element: self.header, member: None, range: Some((0, 2)) }
    }
    fn format(&self) -> &'static str {
        FORMAT
    }
}

fn codec(name: &str) -> Codec {
    Codec { name: name.into(), parameters: BTreeMap::new() }
}

/// The chain of a tile file's tiles, by its header's compression and pixel type: the encoding's
/// codec where the list has one for samples of the pixel type's size, else `source`.
fn chain_of_tiles(e: &ets::Ets) -> Chain {
    match e.compression {
        ets::RAW => Vec::new(),
        // a JPEG stream (ISO/IEC 10918-1), the lossless process's too, of samples of 8 or 16 bits
        ets::JPEG | ets::LOSSLESS_JPEG if matches!(e.bytes, 1 | 2) => vec![codec("jpeg")],
        ets::JPEG2000 if matches!(e.bytes, 1 | 2) => vec![codec("jpeg2k")],
        _ => {
            let parameters = [("format", json!(FORMAT)), ("id", json!(e.compression))];
            vec![Codec { name: "source".into(), parameters: parameters.into_iter().map(|(k, v)| (k.to_string(), Param::Value(v))).collect() }]
        }
    }
}

/// The fill value of a tile file's arrays: the background color's first sample, at its place in
/// the ETS header `header`, where the tiles are uncompressed (their elements are then the pixel
/// type's samples, as the background's are) and every sample of the background is the same (an
/// element is one sample). Any other background is one the IR does not express: the header is
/// among the declarations.
fn fill_of_tiles(e: &ets::Ets, header: u32) -> Fill {
    let n = e.bytes as usize;
    let same = (1..e.channels.max(1) as usize).all(|i| e.background[i * n..(i + 1) * n] == e.background[..n]);
    if e.compression != ets::RAW || !same {
        return Fill::Undefined;
    }
    Fill::Given(Param::Source(Place::Element { element: header, member: None, range: Some((BACKGROUND, e.bytes)) }))
}

impl Vsi {
    /// The arrays of the tile file `file`, one a level: `header` is its `ets` element, the arrays'
    /// key, and `tiles` its tiles' elements, by their index in the directory.
    fn levels(&self, out: &mut Descriptions, file: &EtsFile, header: u32, tiles: &[u32]) -> Res<()> {
        let s = &self.stacks[file.stack];
        let (sis, e) = (&file.sis, &file.ets);
        let nc = sis.coordinates as usize;
        let level_coord = e.use_pyramid.then_some(nc - 1);
        let level_of = |t: &ets::Tile| level_coord.map_or(0, |c| t.coords[c] as usize);
        // the coordinates beyond the column and the row that are axes, the last first
        let outer: Vec<usize> = (2..nc).filter(|&c| Some(c) != level_coord).rev().collect();
        let (tw, th, samples) = (e.tile_width as u64, e.tile_height as u64, e.channels as u64);
        // each level's last column and row of tiles, where it holds a tile
        let count = file.tiles.iter().map(level_of).max().map_or(1, |l| l + 1);
        let mut last: Vec<Option<(u64, u64)>> = vec![None; count];
        for t in &file.tiles {
            let (x, y) = (t.coords[0] as u64, t.coords[1] as u64);
            let at = &mut last[level_of(t)];
            *at = Some(at.map_or((x, y), |(mx, my)| (mx.max(x), my.max(y))));
        }
        let chain = chain_of_tiles(e);
        let fill = fill_of_tiles(e, header);
        let mut arrays = Vec::with_capacity(count);
        for (l, held) in last.iter().enumerate() {
            // level 0 is of the stack's declared size; a later level, which declares none, is as
            // large as its tiles reach, and one that holds no tile has no extent
            let size = if l == 0 {
                Some((s.height.unwrap_or(0), s.width.unwrap_or(0)))
            } else {
                held.map(|(mx, my)| (th * (my + 1), tw * (mx + 1)))
            };
            let mut a = StoredArray {
                element: header,
                index: Some(l as u64),
                declarations: vec![Place::Element { element: header, member: None, range: None }],
                shape: None,
                axes: None,
                item_bits: Some(8 * e.bytes),
                pad_from: None,
                grid: None,
                chains: Vec::new(),
                fill: fill.clone(),
                unit_parameters: Vec::new(),
            };
            if let Some((h, w)) = size {
                let mut shape: Vec<u64> = outer.iter().map(|&c| coord_size(e, c) as u64).collect();
                shape.extend([h, w]);
                // the source lists the column's coordinate first, then the row's, then the others:
                // the reverse of the storage order; the samples, which it counts apart, come last
                let listed = shape.len() as u32;
                let mut axes: Vec<u32> = (0..listed).rev().collect();
                let mut units: Vec<AxisUnits> = outer.iter().map(|_| AxisUnits::Each(1)).collect();
                units.extend([AxisUnits::Each(th), AxisUnits::Each(tw)]);
                if samples > 1 {
                    shape.push(samples);
                    axes.push(listed);
                    units.push(AxisUnits::Each(samples));
                }
                a.shape = Some(shape);
                a.axes = Some(axes);
                // a tile at the right or bottom edge is a whole tile
                a.grid = Some(Grid { axes: units, edge: Edge::Full });
                a.chains = vec![out.chain(chain.clone())];
            }
            arrays.push((out.arrays.len() as u32, size.map(|(h, w)| (h.div_ceil(th), w.div_ceil(tw)))));
            out.arrays.push(a);
        }
        for (t, &element) in file.tiles.iter().zip(tiles) {
            let (array, Some((rows, columns))) = arrays[level_of(t)] else { continue };
            let (x, y) = (t.coords[0] as u64, t.coords[1] as u64);
            // a tile outside its level's grid (of level 0: outside the stack's declared size) is
            // no unit of the array
            if x >= columns || y >= rows {
                continue;
            }
            let mut coords: Vec<u64> = outer.iter().map(|&c| t.coords[c] as u64).collect();
            coords.extend([y, x]);
            if samples > 1 {
                coords.push(0);
            }
            out.map.push(Row { array, coords, element: Some(element), ..Row::default() })?;
        }
        Ok(())
    }

    /// The descriptions of the slide's arrays, their chains and their rows, on the finished IR
    /// `ir`: `header` is the `.vsi` file's `header` element, `made` what `emit_region` made of
    /// each IFD of the chain that lies in a TIFF region, by its index, and `tiles_of` each tile
    /// file's `ets` element with its tiles' elements.
    pub(super) fn described(&self, ir: &Ir, header: u32, made: &HashMap<usize, Emitted>, tiles_of: &[(u32, Vec<u32>)]) -> Res<(Vec<StoredArray>, Vec<Chain>, ChunkMap)> {
        let mut out = Descriptions::default();
        let chain: HashMap<usize, &Ifd> = self.ifds.iter().filter(|f| !f.exif).map(|f| (f.index, f)).collect();
        let mut ks: Vec<usize> = made.keys().copied().collect();
        ks.sort_by_key(|k| made[k].el);
        for k in ks {
            let Some(&ifd) = chain.get(&k) else { continue };
            let read = Read { ifd, file: &self.file, made: &made[&k], header };
            // an IFD that declares no size declares no image
            let Some(im) = Image::of(&read, None) else { continue };
            let a = im.array(&read, read.made.el);
            // an image in tiles takes the strips' table for its tiles where it has none of its own
            let table = if tiled(&read) { read.made.tiles.as_ref().or(read.made.strips.as_ref()) } else { read.made.strips.as_ref() };
            let table = table.and_then(|t| t.as_deref());
            let stored = Tiff::chain_of_image(&read, &im).and_then(|c| Some((c, Tiff::units_of_image(&read, &im, table, &|e| ir.len[e as usize])?)));
            out.image(a, &im, stored)?;
        }
        for (file, (ets, tiles)) in self.files.iter().zip(tiles_of) {
            self.levels(&mut out, file, *ets, tiles)?;
        }
        Ok((out.arrays, out.chains, out.map))
    }
}
