//! The stored arrays of a SAFE product (spec/stored-array.md §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>), of either form:
//! each band file's image, keyed by its `codestream` struct. The parser walks a band file's
//! tile-parts, one a tile in raster order, so a tile is a unit: the element `tiles/<k>`, its
//! tile-part from its SOT marker, which decodes with the codestream's main header, a place of
//! the chain's one codec.

use super::jp2::Codestream;
use crate::array::{AxisUnits, Chain, ChunkMap, Codec, Edge, Fill, Grid, Param, Place, Row, StoredArray};
use crate::ir::Res;
use std::collections::BTreeMap;

/// What a band file's description needs, taken as its elements are emitted.
pub(super) struct Reached {
    /// the `codestream` struct, which declares the image
    pub codestream: u32,
    /// the elements that say what a sample is: the `siz` value, then the `contents` of each
    /// `jp2h` box before the codestream
    pub declarations: Vec<u32>,
    /// the length of the main header, from the codestream's start to its first SOT
    pub header: u64,
    /// the image's size, `Ysiz` then `Xsiz`
    pub size: (u64, u64),
    /// the tiles' size, `YTsiz` then `XTsiz`
    pub tile: (u64, u64),
    /// `Csiz`: 1 or 3
    pub components: u64,
    /// the components' precision, in bits
    pub precision: u64,
    /// the `tiles/<k>` structs, in raster order
    pub tiles: Vec<u32>,
}

impl Reached {
    /// A band file's, of the codestream the walk read and the elements made of it.
    pub fn of(cs: &Codestream, codestream: u32, declarations: Vec<u32>, tiles: Vec<u32>) -> Reached {
        Reached {
            codestream,
            declarations,
            header: cs.tiles.first().map_or(0, |t| t.0 - cs.c0),
            size: (cs.height, cs.width),
            tile: (cs.tile_h, cs.tile_w),
            components: cs.components,
            precision: cs.precision,
            tiles,
        }
    }
}

/// The descriptions of the band files' images, their chains and their rows.
pub(super) fn described(bands: &[Reached]) -> Res<(Vec<StoredArray>, Vec<Chain>, ChunkMap)> {
    let mut arrays: Vec<StoredArray> = Vec::new();
    let mut chains: Vec<Chain> = Vec::new();
    let mut map = ChunkMap::default();
    let place = |element: u32| Place::Element { element, member: None, range: None };
    for b in bands {
        let array = arrays.len() as u32;
        let (height, width) = b.size;
        // the samples come last, as the codec makes them, where the image has more than one
        let samples = (b.components > 1).then_some(b.components);
        // a tile's bytes decode with the main header before the tiles: the codestream's first bytes
        let header = Place::Element { element: b.codestream, member: None, range: Some((0, b.header)) };
        // (a chain of its own for each band file, whose header no other file's chain names)
        chains.push(vec![Codec { name: "jpeg2k_tile".into(), parameters: BTreeMap::from([("header".to_string(), Param::Source(header))]) }]);
        // a tile is cut at the image's right and bottom edges, and one larger than the image is
        // the image
        let lengths = [b.tile.0.min(height), b.tile.1.min(width)];
        arrays.push(StoredArray {
            element: b.codestream,
            index: None,
            declarations: b.declarations.iter().map(|&e| place(e)).collect(),
            shape: Some([height, width].into_iter().chain(samples).collect()),
            // SIZ gives Xsiz before Ysiz; the samples' count, Csiz, is no extent of its list
            axes: Some([1, 0].into_iter().chain(samples.map(|_| 2)).collect()),
            // the codec makes samples of whole bytes: one for a precision of up to 8 bits, else two
            item_bits: Some(if b.precision <= 8 { 8 } else { 16 }),
            pad_from: None,
            grid: Some(Grid { axes: lengths.into_iter().chain(samples).map(AxisUnits::Each).collect(), edge: Edge::Short }),
            chains: vec![array],
            fill: Fill::Undefined,
            unit_parameters: Vec::new(),
        });
        let across = width.div_ceil(b.tile.1);
        for (k, &e) in b.tiles.iter().enumerate() {
            let k = k as u64;
            let coords = [k / across, k % across].into_iter().chain(samples.map(|_| 0)).collect();
            map.push(Row { array, coords, element: Some(e), ..Row::default() })?;
        }
    }
    Ok((arrays, chains, map))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn reached(components: u64, precision: u64, size: (u64, u64), tile: (u64, u64), tiles: Vec<u32>) -> Reached {
        Reached { codestream: 7, declarations: vec![9, 4], header: 120, size, tile, components, precision, tiles }
    }

    /// A band file's image is its tiles, each a unit of the tile's chain: one sample a pixel or
    /// three last, of one byte up to 8 bits of precision and two above, the tiles cut at the
    /// image's edges.
    #[test]
    fn an_image_is_its_tiles() {
        let each = |v: &[u64]| v.iter().map(|&n| AxisUnits::Each(n)).collect::<Vec<_>>();
        for (components, precision, size, tile, shape, axes, bits, units, coords) in [
            (1, 15, (5, 7), (2, 4), vec![5, 7], vec![1, 0], 16, vec![2, 4], vec![vec![0, 0], vec![0, 1], vec![1, 0], vec![1, 1], vec![2, 0], vec![2, 1]]),
            (3, 8, (5, 7), (4, 4), vec![5, 7, 3], vec![1, 0, 2], 8, vec![4, 4, 3], vec![vec![0, 0, 0], vec![0, 1, 0], vec![1, 0, 0], vec![1, 1, 0]]),
            (1, 9, (5, 7), (8, 16), vec![5, 7], vec![1, 0], 16, vec![5, 7], vec![vec![0, 0]]),
            (1, 1, (3, 3), (3, 1), vec![3, 3], vec![1, 0], 8, vec![3, 1], vec![vec![0, 0], vec![0, 1], vec![0, 2]]),
        ] {
            let tiles: Vec<u32> = (0..coords.len() as u32).map(|k| 20 + 3 * k).collect();
            let (arrays, chains, map) = described(&[reached(components, precision, size, tile, tiles.clone())]).unwrap();
            let [a] = &arrays[..] else { panic!("{} arrays", arrays.len()) };
            assert_eq!((a.element, a.index), (7, None));
            assert_eq!(a.declarations, vec![Place::Element { element: 9, member: None, range: None }, Place::Element { element: 4, member: None, range: None }]);
            assert_eq!((a.shape.clone(), a.axes.clone(), a.item_bits, a.pad_from), (Some(shape), Some(axes), Some(bits), None));
            assert_eq!(a.grid, Some(Grid { axes: each(&units), edge: Edge::Short }));
            assert_eq!((a.chains.clone(), a.fill.clone(), a.unit_parameters.len()), (vec![0], Fill::Undefined, 0));
            let header = Param::Source(Place::Element { element: 7, member: None, range: Some((0, 120)) });
            assert_eq!(chains, vec![vec![Codec { name: "jpeg2k_tile".into(), parameters: BTreeMap::from([("header".to_string(), header)]) }]]);
            assert_eq!(map.len(), coords.len());
            for (k, c) in coords.iter().enumerate() {
                assert_eq!((map.array[k], map.coords_of(k), map.element[k], map.range[k]), (0, &c[..], Some(tiles[k]), None));
            }
        }
    }

    /// Each band file has a chain of its own, whose header is a place of its own codestream.
    #[test]
    fn each_band_file_has_its_own_chain() {
        let mut second = reached(1, 15, (4, 4), (4, 4), vec![40]);
        second.codestream = 30;
        let (arrays, chains, map) = described(&[reached(1, 15, (4, 4), (4, 4), vec![20]), second]).unwrap();
        assert_eq!(arrays.iter().map(|a| a.chains.clone()).collect::<Vec<_>>(), vec![vec![0], vec![1]]);
        assert_eq!(chains.len(), 2);
        assert_eq!((map.array.clone(), map.element.clone()), (vec![0, 1], vec![Some(20), Some(40)]));
    }
}
