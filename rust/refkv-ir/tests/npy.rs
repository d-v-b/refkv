//! The NumPy parser (src/npy; spec/zarrify/npy.md): every accepted fixture's IR rebuilds it and
//! presents its arrays, and one test per rejected fixture.

mod common;

use refkv_ir::npy::Npy;

#[test]
fn every_accepted_fixture_rebuilds_and_presents_its_arrays() {
    let dir = common::fixtures().join("npy");
    let mut names: Vec<_> = std::fs::read_dir(&dir).unwrap().map(|e| e.unwrap().path()).collect();
    names.sort();
    let mut seen = 0;
    for path in names.iter().filter(|p| !p.file_name().unwrap().to_str().unwrap().contains("reject")) {
        let data = std::fs::read(path).unwrap();
        let (ir, facts) = refkv_ir::parse_bytes::<Npy>(&data).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
        assert_eq!(common::rebuilt(&ir, &data), data, "{}", path.display());
        assert!(!facts["arrays"].as_array().unwrap().is_empty(), "{}", path.display());
        seen += 1;
    }
    assert!(seen > 30);
}

/// Every accepted fixture's stored arrays (spec/stored-array.md
/// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md>) agree with the facts its
/// projection reads: each presented array of a `.npy` file or stored `.npz` member is described,
/// keyed and declared by its header, in storage order (a Fortran-order array's axes reversed), its
/// elements of the descr's size, one unit, its data element, stored as is, with no fill value; a
/// stored member whose array is not presented but whose header was read is described with no grid;
/// a deflated member's array is not described.
#[test]
fn every_accepted_fixtures_arrays_are_described_as_its_facts_give_them() {
    use refkv_ir::array::{AxisUnits, Edge, Fill, Place};
    let dir = common::fixtures().join("npy");
    let mut names: Vec<_> = std::fs::read_dir(&dir).unwrap().map(|e| e.unwrap().path()).collect();
    names.sort();
    let (mut described, mut ungridded) = (0, 0);
    for path in names.iter().filter(|p| !p.file_name().unwrap().to_str().unwrap().contains("reject")) {
        let at = path.display().to_string();
        let (ir, facts) = refkv_ir::parse_bytes::<Npy>(&std::fs::read(path).unwrap()).unwrap();
        let s = &ir.stored;
        let n = |v: &serde_json::Value| v.as_u64().unwrap();
        let facts_arrays = facts["arrays"].as_array().unwrap();
        let stored: Vec<_> = facts_arrays.iter().filter(|a| a.get("deflate").is_none()).collect();
        let gridded: Vec<(usize, _)> = s.arrays.iter().enumerate().filter(|(_, d)| d.grid.is_some()).collect();
        assert_eq!(gridded.len(), stored.len(), "{at}");
        assert_eq!(s.chains, if stored.is_empty() { vec![] } else { vec![vec![]] }, "{at}");
        for ((k, d), a) in gridded.into_iter().zip(&stored) {
            let shape: Vec<u64> = a["shape"].as_array().unwrap().iter().map(n).collect();
            let reversed = a["fortran_order"].as_bool().unwrap() && shape.len() >= 2;
            let want: Vec<u64> = if reversed { shape.iter().rev().copied().collect() } else { shape.clone() };
            assert_eq!(d.shape, want, "{at}");
            assert_eq!(d.axes, reversed.then(|| (0..shape.len() as u32).rev().collect()), "{at}");
            assert_eq!(d.item_bits, Some(n(&a["item"]) * 8), "{at}");
            assert_eq!(d.index, None, "{at}");
            assert_eq!(d.declarations, vec![Place::Element { element: d.element, member: None, range: None }], "{at}");
            assert_eq!(ir.names.get(ir.name[d.element as usize]), "header", "{at}");
            let h = d.element as usize;
            assert_eq!(ir.start[h] + ir.len[h], n(&a["offset"]), "{at}: the header ends where the data starts");
            let g = d.grid.as_ref().unwrap();
            assert_eq!(g.edge, Edge::Short, "{at}");
            let axes: Vec<AxisUnits> = want.iter().map(|&x| if x == 0 { AxisUnits::List(vec![]) } else { AxisUnits::Each(x) }).collect();
            assert_eq!(g.axes, axes, "{at}");
            assert_eq!((d.chains.clone(), d.fill.clone(), d.pad_from, d.unit_parameters.len()), (vec![0], Fill::Undefined, None, 0), "{at}");
            let rows: Vec<usize> = (0..s.map.len()).filter(|&r| s.map.array[r] == k as u32).collect();
            if n(&a["length"]) == 0 {
                assert!(rows.is_empty(), "{at}");
            } else {
                assert_eq!(rows.len(), 1, "{at}");
                let r = rows[0];
                assert_eq!(s.map.element[r], Some(n(&a["data"]) as u32), "{at}");
                assert_eq!(s.map.coords_of(r), vec![0; want.len()], "{at}");
                assert_eq!((s.map.member[r], s.map.ranges_of(r), s.map.chain[r], s.map.skips[r]), (None, None, None, None), "{at}");
            }
            described += 1;
        }
        // the stored members whose header was read and whose array is not presented
        let members = facts["own"].get("members").and_then(|m| m.as_array()).cloned().unwrap_or_default();
        let unpresented: Vec<_> = members.iter().filter(|m| m.get("reason").is_some() && m.get("header").is_some() && n(&m["method"]) == 0).collect();
        let none: Vec<_> = s.arrays.iter().filter(|d| d.grid.is_none()).collect();
        assert_eq!(none.len(), unpresented.len(), "{at}");
        for (d, m) in none.into_iter().zip(unpresented) {
            let shape: Vec<u64> = m["header"]["shape"].as_array().unwrap().iter().map(n).collect();
            let reversed = m["header"]["fortran_order"].as_bool().unwrap() && shape.len() >= 2;
            assert_eq!(d.shape, if reversed { shape.iter().rev().copied().collect() } else { shape }, "{at}");
            assert!(d.chains.is_empty(), "{at}");
            ungridded += 1;
        }
    }
    assert!(described > 30 && ungridded > 0, "{described} described, {ungridded} with no grid");
}

// one test per case of conformance/zarrify/rejects/npy.json (build.rs), each the message its check gives
macro_rules! reject {
    ($name:ident, $input:literal, $msg:literal) => {
        common::reject!($name, Npy, concat!("npy/", $input), $msg);
    };
}
include!(concat!(env!("OUT_DIR"), "/rejects_npy.rs"));
