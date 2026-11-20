//! The stored arrays of a netCDF3 file (spec/stored-array.md §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): every variable
//! of the header, keyed by the `header` element, a kind of element that declares several arrays,
//! with its position in the header's variable list as its index. A fixed-size variable is one
//! unit, its `variables/<k>` element; a record variable is a unit per record, each its slice of
//! the `records` element. The grid is dense: the file stores every unit.

use crate::array::{AxisUnits, Chain, ChunkMap, Edge, Grid, Place, Row, StoredArray};
use crate::ir::Res;

/// Where a variable's units are.
#[derive(Clone, Copy, Debug)]
pub(super) enum Unit {
    /// a fixed-size variable's data element
    Whole(u32),
    /// a record variable's slices in the `records` element: the first's offset in it, the record
    /// size and the slice's bytes
    Records { element: u32, offset: u64, stride: u64, slice: u64 },
}

/// A variable as the parser placed it.
pub(super) struct Var {
    /// the offset of its `nc_type` in the header
    pub type_at: u64,
    pub bits: u64,
    /// its dimensions' lengths, a record variable's first the record count
    pub shape: Vec<u64>,
    /// None: no unit (a record variable of no records)
    pub unit: Option<Unit>,
}

/// The descriptions of the header's variables, their one empty chain, and their rows.
pub(super) fn described(header: u32, vars: &[Var]) -> Res<(Vec<StoredArray>, Vec<Chain>, ChunkMap)> {
    let mut map = ChunkMap::default();
    let mut arrays = Vec::with_capacity(vars.len());
    let at = |range: (u64, u64)| Place::Element { element: header, member: None, range: Some(range) };
    for (k, v) in vars.iter().enumerate() {
        let declarations = vec![at((v.type_at, 4))];
        let record = matches!(v.unit, Some(Unit::Records { .. })) || v.shape.first() == Some(&0);
        // a record variable's records are a unit each; an axis of length 0 has the declared one
        let axes = v.shape.iter().enumerate().map(|(a, &n)| AxisUnits::Each(if a == 0 && record { 1 } else { n.max(1) })).collect();
        match v.unit {
            Some(Unit::Whole(e)) => map.push(Row { array: k as u32, coords: vec![0; v.shape.len()], element: Some(e), ..Row::default() })?,
            Some(Unit::Records { element, offset, stride, slice }) => {
                for r in 0..v.shape.first().copied().unwrap_or(0) {
                    let mut coords = vec![0; v.shape.len()];
                    coords[0] = r;
                    let o = r.checked_mul(stride).and_then(|o| o.checked_add(offset)).ok_or("internal: a record past 2^64 bytes")?;
                    map.push(Row { array: k as u32, coords, element: Some(element), range: Some((o, slice)), ..Row::default() })?;
                }
            }
            None => {}
        }
        arrays.push(StoredArray {
            element: header,
            index: Some(k as u64),
            declarations,
            shape: Some(v.shape.clone()),
            axes: None,
            item_bits: Some(v.bits),
            pad_from: None,
            // every unit is stored: a fixed-size variable's data, and each of the records
            grid: Some(Grid::dense(axes, Edge::Short)),
            chains: vec![0],
            unit_parameters: Vec::new(),
        });
    }
    Ok((arrays, vec![Vec::new()], map))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A fixed-size variable is one unit; a record variable a unit per record, each its slice at
    /// its record's offset; the grids are dense.
    #[test]
    fn variables_are_units_of_the_file() {
        let vars = [
            Var { type_at: 40, bits: 32, shape: vec![2, 3], unit: Some(Unit::Whole(5)) },
            Var { type_at: 80, bits: 16, shape: vec![3, 2], unit: Some(Unit::Records { element: 6, offset: 4, stride: 12, slice: 4 }) },
            Var { type_at: 120, bits: 8, shape: vec![0], unit: None },
        ];
        let (arrays, chains, map) = described(1, &vars).unwrap();
        assert_eq!(chains, vec![Vec::new()]);
        assert_eq!(arrays.iter().map(|a| (a.element, a.index)).collect::<Vec<_>>(), vec![(1, Some(0)), (1, Some(1)), (1, Some(2))]);
        let at = |o, n| Place::Element { element: 1, member: None, range: Some((o, n)) };
        assert_eq!(arrays[1].declarations, vec![at(80, 4)]);
        assert_eq!(arrays[1].grid, Some(Grid::dense(vec![AxisUnits::Each(1), AxisUnits::Each(2)], Edge::Short)));
        assert_eq!(arrays[2].grid, Some(Grid::dense(vec![AxisUnits::Each(1)], Edge::Short)));
        assert_eq!(map.len(), 4);
        assert_eq!((map.element[0], map.coords_of(0)), (Some(5), &[0, 0][..]));
        assert_eq!((map.element[3], map.coords_of(3), map.range[3]), (Some(6), &[2, 0][..], Some((28, 4))));
    }
}
