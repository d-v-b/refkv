//! The stored array of a FLAC stream (spec/stored-array.md §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): its samples,
//! keyed by the STREAMINFO block, a frame a unit, in a `flac` chain whose `setup` is that block.

use crate::array::{AxisUnits, Chain, ChunkMap, Codec, Edge, Fill, Grid, Param, Place, Row, StoredArray};
use crate::ir::Res;
use std::collections::BTreeMap;

/// The description of a stream whose STREAMINFO block is the value `info`, of `channels`
/// channels and samples of `bits` bits, and whose frames are `frames`, each its element and its
/// samples, in order: the parser checked that there is one at least, and that every frame but
/// the last holds the first one's samples.
pub(crate) fn described(info: u32, channels: u64, bits: u64, frames: &[(u32, u64)]) -> Res<(Vec<StoredArray>, Vec<Chain>, ChunkMap)> {
    let samples: u64 = frames.iter().map(|f| f.1).sum();
    let block = frames.first().map_or(0, |f| f.1);
    let streaminfo = Place::Element { element: info, member: None, range: None };
    let setup = Param::Source(streaminfo.clone());
    let chain: Chain = vec![Codec { name: "flac".into(), parameters: BTreeMap::from([("setup".to_string(), setup)]) }];
    let mut map = ChunkMap::default();
    for (j, &(element, _)) in frames.iter().enumerate() {
        map.push(Row { array: 0, coords: vec![j as u64, 0], element: Some(element), ..Row::default() })?;
    }
    let array = StoredArray {
        element: info,
        index: None,
        declarations: vec![streaminfo],
        shape: Some(vec![samples, channels]),
        axes: None,
        // each sample in whole bytes, as STREAMINFO's MD5 takes it (RFC 9639 §8.2)
        item_bits: Some(bits.div_ceil(8) * 8),
        pad_from: None,
        grid: Some(Grid { axes: vec![AxisUnits::Each(block), AxisUnits::Each(channels)], edge: Edge::Short }),
        chains: vec![0],
        fill: Fill::Undefined,
        unit_parameters: Vec::new(),
    };
    Ok((vec![array], vec![chain], map))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The samples, then the channels; a frame a unit, the last one shorter; each sample in whole
    /// bytes; the STREAMINFO block both the declaration and the codec's setup.
    #[test]
    fn a_frame_is_a_unit_of_the_samples_and_their_channels() {
        for (bits, item) in [(8, 8), (12, 16), (16, 16), (20, 24), (24, 24), (32, 32)] {
            let (arrays, chains, map) = described(3, 2, bits, &[(10, 1024), (11, 1024), (12, 100)]).unwrap();
            let a = &arrays[0];
            assert_eq!((a.element, a.shape.as_ref(), a.item_bits), (3, Some(&vec![2148, 2]), Some(item)), "{bits}");
            assert_eq!(a.grid, Some(Grid { axes: vec![AxisUnits::Each(1024), AxisUnits::Each(2)], edge: Edge::Short }));
            assert_eq!(a.declarations, vec![Place::Element { element: 3, member: None, range: None }]);
            assert_eq!(chains[0][0].name, "flac");
            assert_eq!(chains[0][0].parameters["setup"], Param::Source(Place::Element { element: 3, member: None, range: None }));
            assert_eq!((map.len(), map.element.clone(), map.coords_of(2)), (3, vec![Some(10), Some(11), Some(12)], &[2u64, 0][..]));
        }
    }

    /// One frame: the axis's one unit is the axis.
    #[test]
    fn a_single_frame_is_one_unit() {
        let (arrays, _, _) = described(1, 1, 16, &[(2, 220)]).unwrap();
        assert_eq!(arrays[0].grid.as_ref().unwrap().axes[0], AxisUnits::Each(220));
    }
}
