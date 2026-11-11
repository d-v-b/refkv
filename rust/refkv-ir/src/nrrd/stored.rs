//! The stored array of a NRRD input (spec/stored-array.md §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): the one array
//! its header declares, keyed by the header file's object, its data one unit, the `data` element
//! of the data's file. Raw data is stored as is; gzip data is a gzip stream, after which a
//! positive `byte skip` passes over its first inflated bytes.

use crate::array::{AxisUnits, Chain, ChunkMap, Codec, Edge, Fill, Grid, Param, Place, Row, StoredArray};
use crate::ir::Res;
use std::collections::BTreeMap;

/// What the array's description needs, taken as the IR is emitted.
pub(super) struct Reached {
    /// the header file's object, which declares the array
    pub header: u32,
    /// the lines of the fields that say what an element is: `type`, then `endian` where the
    /// header has one
    pub declarations: Vec<u32>,
    /// the `sizes` field's, in the header's order: the fastest axis first
    pub sizes: Vec<u64>,
    /// the bytes of one element, by the `type` field
    pub item: u64,
    /// whether the encoding is gzip (else raw)
    pub gzip: bool,
    /// of a gzip encoding with a positive `byte skip`: the field's line, and its descriptor's
    /// (offset, length) in the line
    pub skip: Option<(u32, (u64, u64))>,
    /// the `data` element: the data's bytes, or the gzip stream
    pub data: u32,
}

/// The array's description, its chain and its one row.
pub(super) fn described(a: &Reached) -> Res<(Vec<StoredArray>, Vec<Chain>, ChunkMap)> {
    let rank = a.sizes.len();
    let place = |element: u32| Place::Element { element, member: None, range: None };
    // the encoding's steps, in encoding order: the skipped bytes go before the array's, and the
    // whole is compressed
    let mut chain = Chain::new();
    if let Some((line, within)) = a.skip {
        let length = Param::Source(Place::Element { element: line, member: None, range: Some(within) });
        chain.push(Codec { name: "skip".into(), parameters: BTreeMap::from([("length".to_string(), length)]) });
    }
    if a.gzip {
        chain.push(Codec { name: "gzip".into(), parameters: BTreeMap::new() });
    }
    let array = StoredArray {
        element: a.header,
        index: None,
        declarations: a.declarations.iter().map(|&e| place(e)).collect(),
        // the data holds the header's last axis outermost
        shape: Some(a.sizes.iter().rev().copied().collect()),
        axes: (rank >= 2).then(|| (0..rank as u32).rev().collect()),
        item_bits: Some(a.item * 8),
        pad_from: None,
        // one unit of the whole array (the profile rejects an axis of no samples)
        grid: Some(Grid { axes: a.sizes.iter().rev().map(|&n| AxisUnits::Each(n)).collect(), edge: Edge::Short }),
        chains: vec![0],
        fill: Fill::Undefined,
        unit_parameters: Vec::new(),
    };
    let mut map = ChunkMap::default();
    map.push(Row { array: 0, coords: vec![0; rank], element: Some(a.data), ..Row::default() })?;
    Ok((vec![array], vec![chain], map))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn reached(gzip: bool, skip: Option<(u32, (u64, u64))>) -> Reached {
        Reached { header: 1, declarations: vec![3, 5], sizes: vec![6, 5, 4], item: 2, gzip, skip, data: 9 }
    }

    /// The array is its header's sizes, the last outermost, in one unit: raw data in an empty
    /// chain, a gzip stream in `gzip`, and one whose first bytes a byte skip passes over in
    /// `skip` (its length where the header writes it), then `gzip`.
    #[test]
    fn the_array_is_one_unit_of_the_encodings_chain() {
        let names = |c: &Chain| c.iter().map(|x| x.name.clone()).collect::<Vec<_>>();
        for (gzip, skip, want) in [(false, None, vec![]), (true, None, vec!["gzip"]), (true, Some((7, (11, 1))), vec!["skip", "gzip"])] {
            let (arrays, chains, map) = described(&reached(gzip, skip)).unwrap();
            let [a] = &arrays[..] else { panic!("{} arrays", arrays.len()) };
            assert_eq!((a.element, a.index), (1, None));
            assert_eq!(a.declarations, vec![Place::Element { element: 3, member: None, range: None }, Place::Element { element: 5, member: None, range: None }]);
            assert_eq!((a.shape.clone(), a.axes.clone(), a.item_bits, a.pad_from), (Some(vec![4, 5, 6]), Some(vec![2, 1, 0]), Some(16), None));
            assert_eq!(a.grid, Some(Grid { axes: vec![AxisUnits::Each(4), AxisUnits::Each(5), AxisUnits::Each(6)], edge: Edge::Short }));
            assert_eq!((a.chains.clone(), a.fill.clone(), a.unit_parameters.len()), (vec![0], Fill::Undefined, 0));
            assert_eq!(chains.len(), 1);
            assert_eq!(names(&chains[0]), want);
            if let Some((line, within)) = skip {
                let length = Param::Source(Place::Element { element: line, member: None, range: Some(within) });
                assert_eq!(chains[0][0].parameters, BTreeMap::from([("length".to_string(), length)]));
            }
            assert_eq!((map.len(), map.array[0], map.coords_of(0), map.element[0], map.range[0]), (1, 0, &[0, 0, 0][..], Some(9), None));
        }
    }
}
