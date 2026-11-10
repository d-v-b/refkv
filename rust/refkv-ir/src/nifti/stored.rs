//! The stored array of a NIfTI file (spec/stored-array.md §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): its voxels, one
//! array in one unit, as the header declares them. A single file's and a pair's are keyed by the
//! `header`, their unit the `voxels` element, stored as is. A gzipped file's header lies in its
//! stream, where no element holds it: its array is keyed by the stream, its unit, which a chain of
//! `skip` and `gzip` decodes to the voxels.

use crate::array::{AxisUnits, Chain, ChunkMap, Codec, Edge, Fill, Grid, Param, Place, Row, StoredArray};
use crate::ir::Res;
use serde_json::json;
use std::collections::BTreeMap;

/// What the voxels' description needs, taken as the header is checked.
pub(crate) struct Voxels {
    /// the element that declares the array: the `header`, or, of a gzipped file, the stream the
    /// header lies in
    pub key: u32,
    /// the `header`, where an element holds it
    pub header: Option<u32>,
    /// the element that holds the voxels: `voxels`, or a gzipped file's stream
    pub data: u32,
    /// the sizes of the header's `dim[1]` to `dim[n]`, in its order
    pub dims: Vec<u64>,
    /// the bits of a voxel, by the header's `datatype`
    pub bits: u64,
    /// of a gzipped file, the bytes its stream holds before the voxels: the header's `vox_offset`
    pub skip: Option<u64>,
}

/// The description of the voxels, its chain and its one row. The header lists its dimensions the
/// fastest-varying first, so storage order is its order reversed. NIfTI defines no fill value.
pub(crate) fn described(v: &Voxels) -> Res<(Vec<StoredArray>, Vec<Chain>, ChunkMap)> {
    let n = v.dims.len();
    let shape: Vec<u64> = v.dims.iter().rev().copied().collect();
    // as stored; or, of a gzipped file, after the stream's bytes before the voxels, in encoding
    // order: decoding inflates the stream, then passes over them
    let chain: Chain = match v.skip {
        Some(length) => vec![
            Codec { name: "skip".into(), parameters: BTreeMap::from([("length".to_string(), Param::Value(json!(length)))]) },
            Codec { name: "gzip".into(), parameters: BTreeMap::new() },
        ],
        None => Vec::new(),
    };
    let mut map = ChunkMap::default();
    map.push(Row { array: 0, coords: vec![0; n], element: Some(v.data), ..Row::default() })?;
    let array = StoredArray {
        element: v.key,
        index: None,
        declarations: v.header.iter().map(|&element| Place::Element { element, member: None, range: None }).collect(),
        axes: (n >= 2).then(|| (0..n as u32).rev().collect()),
        // one unit of the whole array: every dimension is at least 1
        grid: Some(Grid { axes: shape.iter().map(|&s| AxisUnits::Each(s)).collect(), edge: Edge::Short }),
        shape: Some(shape),
        item_bits: Some(v.bits),
        pad_from: None,
        chains: vec![0],
        fill: Fill::Undefined,
        unit_parameters: Vec::new(),
    };
    Ok((vec![array], vec![chain], map))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn voxels(dims: &[u64], skip: Option<u64>) -> Voxels {
        Voxels { key: 1, header: skip.is_none().then_some(1), data: 3, dims: dims.to_vec(), bits: 16, skip }
    }

    /// The header's dimensions are stored the last one slowest: the shape is its order reversed,
    /// which `axes` says for two or more, and the grid is one unit of all of it.
    #[test]
    fn the_shape_is_the_headers_dimensions_reversed() {
        for (dims, shape, axes) in [
            (vec![7], vec![7], None),
            (vec![2, 3], vec![3, 2], Some(vec![1, 0])),
            (vec![4, 5, 6, 1, 2], vec![2, 1, 6, 5, 4], Some(vec![4, 3, 2, 1, 0])),
        ] {
            let (arrays, chains, map) = described(&voxels(&dims, None)).unwrap();
            let a = &arrays[0];
            assert_eq!((a.shape.as_ref(), a.axes.as_ref(), a.item_bits), (Some(&shape), axes.as_ref(), Some(16)), "{dims:?}");
            let each: Vec<AxisUnits> = shape.iter().map(|&s| AxisUnits::Each(s)).collect();
            assert_eq!(a.grid, Some(Grid { axes: each, edge: Edge::Short }), "{dims:?}");
            assert_eq!((a.element, a.declarations.len(), &a.chains, chains.len()), (1, 1, &vec![0], 1), "{dims:?}");
            assert!(chains[0].is_empty(), "{dims:?}");
            assert_eq!((map.len(), map.element[0], map.coords_of(0).len()), (1, Some(3), dims.len()), "{dims:?}");
        }
    }

    /// A gzipped file's voxels come after `skip` bytes of what its stream inflates to; no element
    /// holds the header, so the description lists no declaration.
    #[test]
    fn a_gzipped_files_voxels_are_the_stream_inflated_past_the_header() {
        let (arrays, chains, _) = described(&voxels(&[2, 3], Some(352))).unwrap();
        let names: Vec<&str> = chains[0].iter().map(|c| c.name.as_str()).collect();
        assert_eq!(names, ["skip", "gzip"]);
        assert_eq!(chains[0][0].parameters.get("length"), Some(&Param::Value(json!(352))));
        assert!(arrays[0].declarations.is_empty());
    }
}
