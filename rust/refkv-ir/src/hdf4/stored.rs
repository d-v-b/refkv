//! The stored arrays of an HDF4 file (spec/stored-array.md §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): every dataset
//! of the SD interface the parser reaches, keyed by its `Var0.0` Vgroup. The parse records what
//! each description needs by descriptor ([`Desc`]), as it decides the dataset's facts; `finish`
//! turns the descriptors into the elements it makes of them.

use super::{Dd, Logical};
use crate::array::{AxisUnits, Chain, ChunkMap, Codec, Edge, Fill, Grid, Param, Place, Row, StoredArray};
use crate::ir::Res;
use serde_json::json;
use std::collections::BTreeMap;

/// How a unit's bytes are encoded: as they are, deflated (a zlib stream), or by another of HDF4's
/// methods, by its code.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub(super) enum Method {
    Plain,
    Deflate,
    Other(u16),
}

impl Method {
    fn chain(self) -> Chain {
        match self {
            Method::Plain => Vec::new(),
            Method::Deflate => vec![Codec { name: "zlib".into(), parameters: BTreeMap::new() }],
            Method::Other(m) => vec![Codec {
                name: "source".into(),
                parameters: BTreeMap::from([
                    ("format".to_string(), Param::Value(json!("hdf4"))),
                    ("id".to_string(), Param::Value(json!(m))),
                ]),
            }],
        }
    }
}

/// How a dataset's data is cut into units, as the source stores it.
#[derive(Clone, Debug)]
pub(super) enum Layout {
    /// one unit: its bytes and their encoding, or no row where the dataset holds no data
    Whole(Option<(Logical, Method)>),
    /// chunks of `chunk`, stored full: each one's coordinates, bytes and encoding
    Chunked { chunk: Vec<u64>, rows: Vec<(Vec<u64>, Logical, Method)> },
}

/// A dataset's fill value: none, as far as the parse read; a `_FillValue` the description does not
/// give, whose Vdata header is the last declaration; the SD interface's default; or `len` bytes at
/// `offset` in a descriptor's element.
#[derive(Clone, Debug, Default)]
pub(super) enum DescFill {
    #[default]
    Undefined,
    Declared(usize),
    FormatDefault,
    At { dd: usize, offset: u64, len: u64 },
}

/// What a dataset's description is built from, by descriptor index: its `Var0.0` Vgroup, its
/// declarations (the number type and the dimension record), its shape, the bytes of one element, how its data is stored where the parser locates it,
/// and its fill value.
#[derive(Clone, Debug, Default)]
pub(super) struct Desc {
    pub vg: Option<usize>,
    pub declarations: Vec<usize>,
    pub shape: Option<Vec<u64>>,
    pub item: Option<u64>,
    pub layout: Option<Layout>,
    pub fill: DescFill,
}

/// The place of a logical element's bytes, as a chunk map row's: one descriptor's element (with a
/// range where they are not all of it), or ranges of the file; None where a linked block never
/// written reads as zeros, which no place holds.
fn unit(l: &Logical, dds: &[Dd], el: &[Option<u32>]) -> Option<(Option<u32>, Option<(u64, u64)>, Option<Vec<(Option<String>, u64, u64)>>)> {
    if l.segs.iter().any(|s| s.0.is_none()) {
        return None;
    }
    if let [(Some(d), o, n)] = l.segs[..] {
        let e = el[d]?;
        let range = (o != dds[d].off || n != dds[d].len).then(|| (o - dds[d].off, n));
        return Some((Some(e), range, None));
    }
    Some((None, None, Some(l.segs.iter().map(|&(_, o, n)| (None, o, n)).collect())))
}

/// The descriptions of `descs`, each descriptor `d`'s element `el[d]` (None for one of no bytes).
pub(super) fn described(descs: &[Desc], dds: &[Dd], el: &[Option<u32>]) -> Res<(Vec<StoredArray>, Vec<Chain>, ChunkMap)> {
    let mut chains: Vec<Chain> = Vec::new();
    let mut chain_of: BTreeMap<Method, u32> = BTreeMap::new();
    let mut arrays = Vec::new();
    let mut map = ChunkMap::default();
    let mut keys = std::collections::BTreeSet::new();
    for d in descs {
        // a Vgroup the file lists twice among its datasets is one array
        let Some(key) = d.vg.and_then(|v| el[v]).filter(|&k| keys.insert(k)) else { continue };
        let array = arrays.len() as u32;
        let mut declarations: Vec<Place> = d.declarations.iter().filter_map(|&k| el[k]).map(|element| Place::Element { element, member: None, range: None }).collect();
        let fill = match d.fill {
            DescFill::Undefined => Fill::Undefined,
            DescFill::Declared(vh) => {
                declarations.extend(el[vh].map(|element| Place::Element { element, member: None, range: None }));
                Fill::Undefined
            }
            DescFill::FormatDefault => Fill::FormatDefault,
            DescFill::At { dd, offset, len } => match el[dd] {
                Some(element) => Fill::Given(Param::Source(Place::Element { element, member: None, range: Some((offset, len)) })),
                None => Fill::Undefined,
            },
        };
        declarations.dedup();
        let mut a = StoredArray {
            element: key,
            index: None,
            declarations,
            shape: d.shape.clone(),
            axes: None,
            item_bits: d.item.map(|n| 8 * n),
            pad_from: None,
            grid: None,
            chains: Vec::new(),
            fill,
            unit_parameters: Vec::new(),
        };
        // the units and their rows, or none where one of them has no place
        let units: Option<(Grid, Vec<(Vec<u64>, &Logical, Method)>)> = match (&d.layout, &d.shape) {
            (Some(Layout::Whole(u)), Some(shape)) => {
                let axes = shape.iter().map(|&n| if n == 0 { AxisUnits::List(Vec::new()) } else { AxisUnits::Each(n) }).collect();
                Some((Grid { axes, edge: Edge::Short }, u.iter().map(|(l, m)| (vec![0; shape.len()], l, *m)).collect()))
            }
            (Some(Layout::Chunked { chunk, rows }), Some(_)) => {
                let axes = chunk.iter().map(|&n| AxisUnits::Each(n)).collect();
                Some((Grid { axes, edge: Edge::Full }, rows.iter().map(|(at, l, m)| (at.clone(), l, *m)).collect()))
            }
            _ => None,
        };
        let placed: Option<Vec<_>> = units.as_ref().and_then(|(_, rows)| rows.iter().map(|(at, l, m)| unit(l, dds, el).map(|u| (at.clone(), u, *m))).collect());
        if let (Some((grid, _)), Some(placed), Some(_)) = (units, placed, d.item) {
            // the array's chains: the plain one first, where a unit has it, then by method
            let mut methods: Vec<Method> = placed.iter().map(|p| p.2).collect();
            methods.sort();
            methods.dedup();
            if methods.is_empty() {
                methods.push(Method::Plain);
            }
            for &m in &methods {
                let c = *chain_of.entry(m).or_insert_with(|| {
                    chains.push(m.chain());
                    chains.len() as u32 - 1
                });
                a.chains.push(c);
            }
            a.grid = Some(grid);
            for (at, (element, range, ranges), m) in placed {
                let chain = methods.iter().position(|&x| x == m).filter(|&k| k > 0).map(|k| k as u32);
                map.push(Row { array, coords: at, element, range, ranges, chain, ..Row::default() })?;
            }
        }
        arrays.push(a);
    }
    Ok((arrays, chains, map))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dds() -> Vec<Dd> {
        vec![Dd { tag: 1965, rf: 2, off: 100, len: 20 }, Dd { tag: 106, rf: 2, off: 120, len: 4 }, Dd { tag: 701, rf: 2, off: 124, len: 10 },
             Dd { tag: 702, rf: 2, off: 200, len: 64 }, Dd { tag: 61, rf: 5, off: 300, len: 30 }, Dd { tag: 61, rf: 6, off: 330, len: 32 },
             Dd { tag: 1965, rf: 7, off: 400, len: 20 }, Dd { tag: 1965, rf: 8, off: 420, len: 20 }, Dd { tag: 1965, rf: 9, off: 440, len: 20 }]
    }

    fn el() -> Vec<Option<u32>> {
        (0..9).map(|k| Some(k + 2)).collect()
    }

    fn desc(layout: Layout, shape: Vec<u64>) -> Desc {
        keyed(0, layout, shape)
    }

    /// A dataset's, of the Vgroup of descriptor `vg`.
    fn keyed(vg: usize, layout: Layout, shape: Vec<u64>) -> Desc {
        Desc { vg: Some(vg), declarations: vec![1, 2], shape: Some(shape), item: Some(4), layout: Some(layout), fill: DescFill::FormatDefault }
    }

    /// One unit of all the data, at the data element (a range of it where the element holds more),
    /// or ranges of the file where linked blocks hold it; chunks under `full`, each with its chain.
    #[test]
    fn a_dataset_is_described_as_its_layout_stores_it() {
        let whole = |segs: Vec<(Option<usize>, u64, u64)>| Layout::Whole(Some((Logical { len: segs.iter().map(|s| s.2).sum(), segs }, Method::Plain)));
        let (arrays, chains, map) = described(&[
            keyed(0, whole(vec![(Some(3), 200, 64)]), vec![4, 4]),
            keyed(6, whole(vec![(Some(3), 200, 48)]), vec![3, 4]),
            keyed(7, whole(vec![(Some(3), 200, 16), (Some(4), 300, 16)]), vec![8]),
            keyed(8, Layout::Chunked { chunk: vec![2, 4], rows: vec![
                (vec![0, 0], Logical { len: 30, segs: vec![(Some(4), 300, 30)] }, Method::Deflate),
                (vec![1, 0], Logical { len: 32, segs: vec![(Some(5), 330, 32)] }, Method::Plain),
            ] }, vec![3, 4]),
        ], &dds(), &el()).unwrap();
        assert_eq!(arrays.iter().map(|a| a.element).collect::<Vec<_>>(), vec![2, 8, 9, 10]);
        assert_eq!(arrays[0].grid, Some(Grid { axes: vec![AxisUnits::Each(4), AxisUnits::Each(4)], edge: Edge::Short }));
        assert_eq!((map.element[0], map.range[0]), (Some(5), None));
        assert_eq!((map.element[1], map.range[1]), (Some(5), Some((0, 48))));
        assert_eq!(map.element[2], None);
        assert_eq!(arrays[3].grid, Some(Grid { axes: vec![AxisUnits::Each(2), AxisUnits::Each(4)], edge: Edge::Full }));
        assert_eq!(arrays[3].chains.iter().map(|&c| chains[c as usize].len()).collect::<Vec<_>>(), vec![0, 1]);
        assert_eq!((map.chain[3], map.chain[4]), (Some(1), None));
        assert_eq!((arrays[0].item_bits, arrays[0].fill.clone()), (Some(32), Fill::FormatDefault));
    }

    /// A dataset's Vgroup listed twice is one array.
    #[test]
    fn a_vgroup_listed_twice_is_one_array() {
        let d = desc(Layout::Whole(None), vec![2]);
        let (arrays, _, _) = described(&[d.clone(), d], &dds(), &el()).unwrap();
        assert_eq!(arrays.len(), 1);
    }

    /// A linked block never written reads as zeros, which no place holds: the array has no grid.
    #[test]
    fn zeros_of_a_block_never_written_leave_no_grid() {
        let l = Logical { len: 64, segs: vec![(Some(3), 200, 32), (None, 0, 32)] };
        let (arrays, _, map) = described(&[desc(Layout::Whole(Some((l, Method::Plain))), vec![16])], &dds(), &el()).unwrap();
        assert_eq!((arrays[0].grid.as_ref(), map.len()), (None, 0));
    }
}
