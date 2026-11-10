//! The stored arrays of a LIF, LOF or LIFEXT file (spec/stored-array.md §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): one for each
//! image, keyed by the XML that declares it, with the image's index. An image's samples lie in its
//! memory block where its dimensions' and channels' `BytesInc` put them, so its storage order is
//! read off those: the axes by the bytes between two of their elements, the largest first. Where
//! every axis packs the faster ones end to end, the image is one unit; where one does not (rows
//! with bytes between them, channels apart), each run of packed samples is a unit, a range of the
//! block's data; and where an axis steps by fewer bytes than the faster ones span, the axes do
//! not nest (the samples share bytes, or lie in an order no order of the axes gives) and the array
//! has no grid.

use crate::array::{AxisUnits, Chain, ChunkMap, Edge, Fill, Grid, Place, Row, StoredArray};
use crate::ir::{Ir, Res, DATA, NO_INDEX};
use std::collections::HashMap;

/// What an image's description needs, taken as its layout is checked.
pub(super) struct Reached {
    /// each dimension's `NumberOfElements` and `BytesInc`, in the XML's order
    pub dims: Vec<(u64, u64)>,
    /// the channels' `BytesInc`, ascending, no two alike
    pub offsets: Vec<u64>,
    /// the bytes of one sample
    pub item: u64,
    /// the memory block that holds its samples
    pub block: usize,
    /// where the image starts in the block's data: 0, or the `Offset` of a project's `Block`
    pub start: u64,
}

/// One axis of an image, as its bytes lay it out.
#[derive(Clone, Copy, Debug, PartialEq)]
pub(super) struct Axis {
    /// its position in the source's list: a dimension's in the XML, or, of the channels, the
    /// position after the last dimension's
    pub source: u32,
    /// its elements
    pub n: u64,
    /// the fewest bytes from one of its elements to the next: a dimension's `BytesInc`, or the
    /// least difference of two channels' `BytesInc`
    step: u64,
    /// whether every element is `step` bytes after the one before it
    even: bool,
    /// whether it is the channels', whose elements are where their `BytesInc` say
    channels: bool,
}

/// An image's axes in storage order, the slowest first, and how its samples are cut into units.
#[derive(Debug, PartialEq)]
pub(super) struct Laid {
    pub axes: Vec<Axis>,
    /// of axes that nest: how many of the first have a unit for each of their elements, and the
    /// bytes of a unit; None where an axis steps by fewer bytes than the faster ones span
    pub units: Option<(usize, u64)>,
}

/// The layout the byte increments give. The axes are the dimensions and, of an image of more
/// than one channel, the channels; the faster of two is the one of fewer bytes a step, and, of
/// two alike, the one the source lists first (a LIF lists its fastest dimension first). An axis
/// of one element steps nowhere, and is placed by its `BytesInc` all the same.
pub(super) fn laid(dims: &[(u64, u64)], offsets: &[u64], item: u64) -> Laid {
    let mut axes: Vec<Axis> =
        dims.iter().enumerate().map(|(j, &(n, step))| Axis { source: j as u32, n, step, even: true, channels: false }).collect();
    let first = offsets.first().copied().unwrap_or(0);
    if offsets.len() > 1 {
        let gaps = || offsets.windows(2).map(|w| w[1].saturating_sub(w[0]));
        let step = gaps().min().unwrap_or(0);
        axes.push(Axis { source: dims.len() as u32, n: offsets.len() as u64, step, even: gaps().all(|g| g == step), channels: true });
    }
    axes.sort_by_key(|a| (a.step, a.source));
    // from the fastest: the bytes the axes so far span, from their first sample to the end of
    // their last, and whether they pack them end to end
    let mut span = item as u128;
    let mut packed = true;
    let (mut inner, mut unit) = (0, span);
    let mut nests = true;
    for (k, a) in axes.iter().enumerate() {
        if a.n > 1 {
            if (a.step as u128) < span {
                nests = false;
                break;
            }
            packed = packed && a.even && a.step as u128 == span;
            let last = if a.channels { offsets[offsets.len() - 1].saturating_sub(first) as u128 } else { (a.n - 1) as u128 * a.step as u128 };
            span += last;
        }
        if packed {
            (inner, unit) = (k + 1, span);
        }
    }
    axes.reverse();
    let units = u64::try_from(unit).ok().filter(|_| nests).map(|bytes| (axes.len() - inner, bytes));
    Laid { axes, units }
}

/// The child of `parent` named `name`, with no name index.
fn child(ir: &Ir, parent: u32, name: &str) -> Option<u32> {
    ir.children(parent).iter().copied().find(|&c| ir.names.get(ir.name[c as usize]) == name && ir.nidx[c as usize] == NO_INDEX)
}

/// The element that declares the file's images, in the finished IR: the XML of a LOF's XML block
/// (`description/xml`), or the XML header (`header/xml`).
pub(super) fn declaring(ir: &Ir) -> Option<u32> {
    let block = child(ir, 0, "description").or_else(|| child(ir, 0, "header"))?;
    child(ir, block, "xml")
}

/// The data element of each memory block that is an image's, in the finished IR, by the block's
/// index, with its length.
fn block_data(ir: &Ir) -> HashMap<u64, (u32, u64)> {
    let mut out = HashMap::new();
    for &b in ir.children(0) {
        if ir.names.get(ir.name[b as usize]) != "blocks/" {
            continue;
        }
        for &c in ir.children(b) {
            if ir.kind[c as usize] == DATA && ir.name_of(c) == "data" {
                out.insert(ir.nidx[b as usize], (c, ir.len[c as usize]));
            }
        }
    }
    out
}

/// The descriptions of the images, in their order, keyed by `key` with their indexes; their one
/// chain, of samples stored as they are; and their rows. `limit` is the chunk map's, of the file
/// (spec/stored-array.md §6 <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#6-in-the-ir>):
/// an image whose rows alone would pass it keeps none there, so they are not built here either.
pub(super) fn described(ir: &Ir, key: u32, images: &[Reached], limit: u64) -> Res<(Vec<StoredArray>, Vec<Chain>, ChunkMap)> {
    let blocks = block_data(ir);
    let mut arrays = Vec::with_capacity(images.len());
    let mut map = ChunkMap::default();
    for (k, im) in images.iter().enumerate() {
        let l = laid(&im.dims, &im.offsets, im.item);
        let rank = l.axes.len();
        let shape: Vec<u64> = l.axes.iter().map(|a| a.n).collect();
        let order: Vec<u32> = l.axes.iter().map(|a| a.source).collect();
        let first = im.offsets.first().copied().unwrap_or(0);
        // where element `i` of an axis is, in bytes after its first
        let at = |a: &Axis, i: u64| if a.channels { (im.offsets[i as usize] - first) as u128 } else { i as u128 * a.step as u128 };
        let mut grid = None;
        if let (Some((outer, bytes)), Some(&(element, held))) = (l.units, blocks.get(&(im.block as u64))) {
            let outer_axes = &l.axes[..outer];
            let count: u128 = outer_axes.iter().map(|a| a.n as u128).product();
            // the farthest unit's end: within the block, as the parser checked of the image
            let base = im.start as u128 + first as u128;
            let end = base + outer_axes.iter().map(|a| at(a, a.n - 1)).sum::<u128>() + bytes as u128;
            if count.saturating_mul(1 + rank as u128) <= limit as u128 && end <= held as u128 {
                let mut index = vec![0u64; outer];
                for _ in 0..count as u64 {
                    let offset = (base + outer_axes.iter().zip(&index).map(|(a, &i)| at(a, i)).sum::<u128>()) as u64;
                    let mut coords = index.clone();
                    coords.resize(rank, 0);
                    // a unit that is all of the block's data is the element alone (its recipe is
                    // the identity)
                    let range = ((offset, bytes) != (0, held)).then_some((offset, bytes));
                    map.push(Row { array: k as u32, coords, element: Some(element), range, ..Row::default() })?;
                    for (i, a) in index.iter_mut().zip(outer_axes).rev() {
                        *i += 1;
                        if *i < a.n {
                            break;
                        }
                        *i = 0;
                    }
                }
                let each = |i: usize| AxisUnits::Each(if i < outer { 1 } else { shape[i] });
                grid = Some(Grid { axes: (0..rank).map(each).collect(), edge: Edge::Short });
            }
        }
        arrays.push(StoredArray {
            element: key,
            index: Some(k as u64),
            declarations: vec![Place::Element { element: key, member: None, range: None }],
            axes: order.iter().enumerate().any(|(i, &s)| s != i as u32).then_some(order),
            shape: Some(shape),
            item_bits: Some(im.item * 8),
            pad_from: None,
            chains: if grid.is_some() { vec![0] } else { Vec::new() },
            grid,
            fill: Fill::Undefined,
            unit_parameters: Vec::new(),
        });
    }
    Ok((arrays, vec![Chain::new()], map))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// (position in the source's list, length) of each axis, the slowest first.
    fn order(l: &Laid) -> Vec<(u32, u64)> {
        l.axes.iter().map(|a| (a.source, a.n)).collect()
    }

    /// The axes are in the order of their bytes a step, the largest first, the channels where
    /// their offsets put them; the units are the runs the axes pack end to end.
    #[test]
    fn the_byte_increments_give_the_storage_order_and_the_units() {
        let cases: &[(&str, &[(u64, u64)], &[u64], u64, &[(u32, u64)], (usize, u64))] = &[
            ("a plane", &[(16, 1), (12, 16)], &[0], 1, &[(1, 12), (0, 16)], (0, 192)),
            ("a line", &[(12, 1)], &[0], 1, &[(0, 12)], (0, 12)),
            ("channels between y and z", &[(10, 2), (8, 20), (5, 480)], &[0, 160, 320], 2, &[(2, 5), (3, 3), (1, 8), (0, 10)], (0, 2400)),
            ("channels outermost", &[(9, 2), (7, 18), (6, 126)], &[0, 756], 2, &[(3, 2), (2, 6), (1, 7), (0, 9)], (0, 1512)),
            ("interleaved channels", &[(11, 3), (9, 33)], &[0, 1, 2], 1, &[(1, 9), (0, 11), (2, 3)], (0, 297)),
            ("padded rows", &[(7, 3), (5, 24)], &[0, 1, 2], 1, &[(1, 5), (0, 7), (2, 3)], (1, 21)),
            ("planes apart", &[(4, 1), (3, 4), (2, 20)], &[0], 1, &[(2, 2), (1, 3), (0, 4)], (1, 12)),
            ("channels unevenly apart", &[(4, 1), (3, 4)], &[5, 17, 40], 1, &[(2, 3), (1, 3), (0, 4)], (1, 12)),
            ("the slowest listed first", &[(3, 8), (4, 2)], &[0], 2, &[(0, 3), (1, 4)], (0, 24)),
            ("an axis of one element", &[(4, 1), (3, 4), (1, 12), (1, 12)], &[0], 1, &[(3, 1), (2, 1), (1, 3), (0, 4)], (0, 12)),
            ("an axis of one element and no step", &[(4, 1), (1, 0), (3, 4)], &[0], 1, &[(2, 3), (0, 4), (1, 1)], (0, 12)),
            ("an axis of one element past rows apart", &[(4, 1), (3, 6), (1, 99)], &[0], 1, &[(2, 1), (1, 3), (0, 4)], (2, 4)),
        ];
        for &(name, dims, offsets, item, want, units) in cases {
            let l = laid(dims, offsets, item);
            assert_eq!((order(&l), l.units), (want.to_vec(), Some(units)), "{name}");
        }
    }

    /// A used dimension of no bytes a step repeats the samples: they are no array of distinct
    /// bytes.
    #[test]
    fn a_dimension_of_no_step_has_no_units() {
        let l = laid(&[(4, 1), (3, 4), (5, 0)], &[0], 1);
        assert_eq!((order(&l), l.units), (vec![(1, 3), (0, 4), (2, 5)], None));
    }

    /// A dimension that steps by fewer bytes than the faster ones span overlaps them.
    #[test]
    fn a_dimension_that_overlaps_the_faster_ones_has_no_units() {
        let l = laid(&[(4, 1), (3, 4), (2, 11)], &[0], 1);
        assert_eq!((order(&l), l.units), (vec![(2, 2), (1, 3), (0, 4)], None));
    }

    /// Channels whose planes share bytes: their offsets are closer than a plane is long.
    #[test]
    fn channels_that_overlap_have_no_units() {
        let l = laid(&[(4, 1), (3, 4)], &[0, 6], 1);
        assert_eq!((order(&l), l.units), (vec![(2, 2), (1, 3), (0, 4)], None));
    }

    /// Channels unevenly apart, with a dimension's steps between them: no sample shares a byte,
    /// but the planes lie in an order that neither the channels within the dimension nor the
    /// dimension within the channels gives.
    #[test]
    fn channels_unevenly_apart_around_a_dimension_have_no_units() {
        let l = laid(&[(4, 1), (3, 4), (2, 50)], &[0, 12, 100], 1);
        assert_eq!((order(&l), l.units), (vec![(2, 2), (3, 3), (1, 3), (0, 4)], None));
    }
}
