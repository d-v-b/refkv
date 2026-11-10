//! The stored array of an MRC file (spec/stored-array.md §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): the file's
//! data, keyed by its `header` value, as the header declares it: `nz` sections of `ny` rows of
//! `nx` values, one unit, the `data` element. A mode 101 file's values of 4 bits are packed least
//! significant bit first, a row ending on a byte. A file gzipped whole is keyed by its `data`
//! element, the stream, which is its one unit: the values follow the header and the extended
//! header in what it inflates to.

use super::{FIELDS, IMOD_FLAGS, IMOD_STAMP};
use crate::array::{AxisUnits, Chain, ChunkMap, Codec, Edge, Fill, Grid, Param, Place, Row, StoredArray};
use crate::ir::Res;
use serde_json::json;

/// The bytes of IMOD's stamp, and of its flags: an `i4` each.
const IMOD_WORD: u64 = 4;

/// What the array's description is built from, as the parser read the header.
pub(super) struct Reached {
    /// the element that declares the array: the `header` value, or, of a gzipped file, the
    /// `data` element, the stream that holds the header
    pub element: u32,
    /// the `data` element
    pub data: u32,
    /// `nx`, `ny` and `nz`
    pub dims: [u64; 3],
    /// `mode`
    pub mode: i64,
    /// the bits of one value of the mode
    pub item_bits: u64,
    /// whether the mode packs its values, of fewer bits than a byte
    pub packed: bool,
    /// of a gzipped file: the bytes its stream holds before the values, the header and the
    /// extended header
    pub gzip: Option<u64>,
}

/// The range a header field takes of the header: its offset and its bytes.
fn field(name: &str) -> (u64, u64) {
    let &(_, offset, kind, count) = FIELDS.iter().find(|f| f.0 == name).expect("a field of the header");
    let size = match kind {
        "i4" | "f4" => 4,
        "l" => 80,
        _ => 1,
    };
    (offset as u64, (size * count) as u64)
}

/// Where the file declares what its values are: of a file whose header is an element, the
/// machine stamp, which gives the byte order, then `mode`, then, for mode 0, whose sign they
/// decide, `nversion` and IMOD's stamp and flags; of a gzipped file, the stream, which holds them.
fn declarations(r: &Reached) -> Vec<Place> {
    if r.gzip.is_some() {
        return vec![Place::Element { element: r.element, member: None, range: None }];
    }
    let mut ranges = vec![field("machst"), field("mode")];
    if r.mode == 0 {
        ranges.extend([field("nversion"), (IMOD_STAMP as u64, IMOD_WORD), (IMOD_FLAGS as u64, IMOD_WORD)]);
    }
    ranges.into_iter().map(|range| Place::Element { element: r.element, member: None, range: Some(range) }).collect()
}

/// The chain of the file's one unit, in encoding order: a packed mode's values are packed least
/// significant bit first; a gzipped file's follow `skip` bytes of the stream.
fn chain(r: &Reached) -> Chain {
    let codec = |name: &str, parameter: Option<(&str, u64)>| Codec {
        name: name.into(),
        parameters: parameter.into_iter().map(|(k, v)| (k.to_string(), Param::Value(json!(v)))).collect(),
    };
    let mut chain = Vec::new();
    if r.packed {
        chain.push(codec("lsb_first", Some(("bits", r.item_bits))));
    }
    if let Some(skip) = r.gzip {
        chain.push(codec("skip", Some(("length", skip))));
        chain.push(codec("gzip", None));
    }
    chain
}

/// The description of the file's array, its chain and its one row. MRC has no fill value.
pub(super) fn described(r: &Reached) -> Res<(Vec<StoredArray>, Vec<Chain>, ChunkMap)> {
    let [nx, ny, nz] = r.dims;
    let shape = vec![nz, ny, nx];
    let mut map = ChunkMap::default();
    map.push(Row { array: 0, coords: vec![0; shape.len()], element: Some(r.data), ..Row::default() })?;
    let array = StoredArray {
        element: r.element,
        index: None,
        declarations: declarations(r),
        // the header gives the columns first, and the file stores the sections slowest
        axes: Some(vec![2, 1, 0]),
        item_bits: Some(r.item_bits),
        // a packed mode's rows each end on a byte
        pad_from: r.packed.then_some(shape.len() as u32 - 1),
        // one unit, the whole array: the header's checks leave no axis of length 0
        grid: Some(Grid { axes: shape.iter().map(|&n| AxisUnits::Each(n)).collect(), edge: Edge::Short }),
        shape: Some(shape),
        chains: vec![0],
        fill: Fill::Undefined,
        unit_parameters: Vec::new(),
    };
    Ok((vec![array], vec![chain(r)], map))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn reached(mode: i64, item_bits: u64, gzip: Option<u64>) -> Reached {
        Reached { element: 1, data: 2, dims: [5, 3, 2], mode, item_bits, packed: item_bits < 8, gzip }
    }

    fn names(r: &Reached) -> Vec<(String, Vec<(String, Param)>)> {
        chain(r).into_iter().map(|c| (c.name, c.parameters.into_iter().collect())).collect()
    }

    /// The fields that declare the values are ranges of the header, by the header's own table of
    /// fields: the machine stamp and the mode, and for mode 0 the three that decide its sign.
    #[test]
    fn the_declarations_are_the_fields_that_say_what_a_value_is() {
        let ranges = |r: &Reached| -> Vec<(u64, u64)> {
            declarations(r).into_iter().map(|p| match p {
                Place::Element { element: 1, member: None, range: Some(range) } => range,
                p => panic!("{p:?}"),
            }).collect()
        };
        assert_eq!(ranges(&reached(2, 32, None)), vec![(212, 4), (12, 4)]);
        assert_eq!(ranges(&reached(0, 8, None)), vec![(212, 4), (12, 4), (108, 4), (152, 4), (156, 4)]);
        assert_eq!(declarations(&reached(0, 8, Some(1024))), vec![Place::Element { element: 1, member: None, range: None }]);
    }

    /// The chain is in encoding order: the values packed, then, of a gzipped file, put after the
    /// header and deflated.
    #[test]
    fn the_chain_is_in_encoding_order() {
        let value = |name: &str, v: u64| vec![(name.to_string(), Param::Value(json!(v)))];
        assert_eq!(names(&reached(2, 32, None)), vec![]);
        assert_eq!(names(&reached(101, 4, None)), vec![("lsb_first".to_string(), value("bits", 4))]);
        assert_eq!(names(&reached(2, 32, Some(1184))), vec![("skip".to_string(), value("length", 1184)), ("gzip".to_string(), vec![])]);
        assert_eq!(
            names(&reached(101, 4, Some(1024))),
            vec![("lsb_first".to_string(), value("bits", 4)), ("skip".to_string(), value("length", 1024)), ("gzip".to_string(), vec![])]
        );
    }

    /// The array is the sections, the rows and the columns, which the header gives last first,
    /// in one unit; a packed mode's rows end on a byte.
    #[test]
    fn the_array_is_one_unit_of_the_headers_sizes() {
        let (arrays, chains, map) = described(&reached(101, 4, None)).unwrap();
        let a = &arrays[0];
        assert_eq!((a.shape.clone(), a.axes.clone(), a.item_bits, a.pad_from), (Some(vec![2, 3, 5]), Some(vec![2, 1, 0]), Some(4), Some(2)));
        assert_eq!(a.grid, Some(Grid { axes: vec![AxisUnits::Each(2), AxisUnits::Each(3), AxisUnits::Each(5)], edge: Edge::Short }));
        assert_eq!((a.element, a.index, a.chains.clone(), chains.len()), (1, None, vec![0], 1));
        assert_eq!((map.len(), map.element[0], map.coords_of(0)), (1, Some(2), &[0, 0, 0][..]));
        assert_eq!(described(&reached(2, 32, None)).unwrap().0[0].pad_from, None);
    }
}
