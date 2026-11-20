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

/// The header a description's entry of `arrays` gives as text, parsed, and the presented data
/// type of its `descr`.
fn declared(a: &serde_json::Value) -> (refkv_ir::npy::header::Header, refkv_ir::npy::header::Dtype) {
    let rules = refkv_ir::rules::of("npy");
    let h = refkv_ir::npy::header::parse(a["header"].as_str().unwrap(), 64).unwrap();
    let t = refkv_ir::npy::header::dtype(rules, &h.descr).unwrap();
    (h, t)
}

/// Every accepted fixture's stored arrays (spec/stored-array.md
/// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md>) agree with what each array's
/// header declares, which the parser's description gives as text: each presented array of a
/// `.npy` file or stored `.npz` member is described,
/// keyed and declared by its header, in storage order (a Fortran-order array's axes reversed), its
/// elements of the descr's size, one unit, its data element, stored as is, with no fill value; a
/// member whose array is not presented but whose header was read is described with no grid. A
/// deflated member's header and data are in the space of its stream's derived element, whose form
/// gives where the header ends (`prefix`) and, for a presented array, what the stream inflates to
/// (`size`).
#[test]
fn every_accepted_fixtures_arrays_are_described_as_their_headers_declare_them() {
    use refkv_ir::array::{AxisUnits, Edge, Place};
    let dir = common::fixtures().join("npy");
    let mut names: Vec<_> = std::fs::read_dir(&dir).unwrap().map(|e| e.unwrap().path()).collect();
    names.sort();
    let (mut described, mut ungridded, mut deflated) = (0, 0, 0);
    for path in names.iter().filter(|p| !p.file_name().unwrap().to_str().unwrap().contains("reject")) {
        let at = path.display().to_string();
        let (ir, description) = refkv_ir::parse_bytes::<Npy>(&std::fs::read(path).unwrap()).unwrap();
        let s = &ir.stored;
        let n = |v: &serde_json::Value| v.as_u64().unwrap();
        let presented = description["arrays"].as_array().unwrap();
        let stored: Vec<_> = presented.iter().collect();
        let gridded: Vec<(usize, _)> = s.arrays.iter().enumerate().filter(|(_, d)| d.grid.is_some()).collect();
        assert_eq!(gridded.len(), stored.len(), "{at}");
        assert_eq!(s.chains, if stored.is_empty() { vec![] } else { vec![vec![]] }, "{at}");
        for ((k, d), a) in gridded.into_iter().zip(&stored) {
            let (h, t) = declared(a);
            let shape = h.shape.clone();
            let reversed = h.fortran_order && shape.len() >= 2;
            let want: Vec<u64> = if reversed { shape.iter().rev().copied().collect() } else { shape.clone() };
            assert_eq!(a["stored"].as_u64(), Some(d.element as u64), "{at}: the description keys the array's");
            assert_eq!(d.shape.as_ref(), Some(&want), "{at}");
            assert_eq!(d.axes, reversed.then(|| (0..shape.len() as u32).rev().collect()), "{at}");
            assert_eq!(d.item_bits, Some(t.item * 8), "{at}");
            assert_eq!(d.index, None, "{at}");
            assert_eq!(d.declarations, vec![Place::Element { element: d.element, member: None, range: None }], "{at}");
            assert_eq!(ir.names.get(ir.name[d.element as usize]), "header", "{at}");
            let h = d.element as usize;
            let g = d.grid.as_ref().unwrap();
            assert_eq!(g.edge, Edge::Short, "{at}");
            let axes: Vec<AxisUnits> = want.iter().map(|&x| if x == 0 { AxisUnits::List(vec![]) } else { AxisUnits::Each(x) }).collect();
            assert_eq!(g.axes, axes, "{at}");
            assert_eq!((d.chains.clone(), d.grid.as_ref().map(|g| g.sparse.is_none()), d.pad_from, d.unit_parameters.len()), (vec![0], Some(true), None, 0), "{at}");
            let rows: Vec<usize> = (0..s.map.len()).filter(|&r| s.map.array[r] == k as u32).collect();
            let length = shape.iter().product::<u64>() * t.item;
            // a stored member's elements are in the file; a deflated member's, in what its stream
            // inflates to, the space of the derived element they are children of
            let space = ir.space[h];
            match a.get("deflate") {
                None => assert_eq!(space, 0, "{at}"),
                Some(d) => {
                    assert_eq!((space, ir.kind[space as usize]), (ir.parent[h], refkv_ir::ir::DERIVED), "{at}");
                    let form: serde_json::Value = serde_json::from_str(ir.form(space).unwrap()).unwrap();
                    let end = ir.start[h] + ir.len[h];
                    assert_eq!(form, serde_json::json!({"transform": "deflate", "prefix": end, "size": end + length}), "{at}");
                    assert_eq!(d, &serde_json::json!({"crc": d["crc"], "size": end + length}), "{at}");
                    deflated += 1;
                }
            }
            if length == 0 {
                assert!(rows.is_empty(), "{at}");
            } else {
                assert_eq!(rows.len(), 1, "{at}");
                let r = rows[0];
                // its unit is the `data` element beside its header, which starts where the header ends
                let e = s.map.element[r].unwrap() as usize;
                assert_eq!((ir.names.get(ir.name[e]), ir.parent[e], ir.space[e]), ("data", ir.parent[h], space), "{at}");
                assert_eq!((ir.start[e], ir.len[e]), (ir.start[h] + ir.len[h], length), "{at}");
                assert_eq!(s.map.coords_of(r), vec![0; want.len()], "{at}");
                assert_eq!((s.map.member[r], s.map.ranges_of(r), s.map.chain[r], s.map.skips[r]), (None, None, None, None), "{at}");
            }
            described += 1;
        }
        // the members whose header was read and whose array is not presented
        let members = description["own"].get("members").and_then(|m| m.as_array()).cloned().unwrap_or_default();
        let unpresented: Vec<_> = members.iter().filter(|m| m.get("reason").is_some() && m.get("header").is_some()).collect();
        let none: Vec<_> = s.arrays.iter().filter(|d| d.grid.is_none()).collect();
        assert_eq!(none.len(), unpresented.len(), "{at}");
        for (d, m) in none.into_iter().zip(unpresented) {
            let shape: Vec<u64> = m["header"]["shape"].as_array().unwrap().iter().map(n).collect();
            let reversed = m["header"]["fortran_order"].as_bool().unwrap() && shape.len() >= 2;
            assert_eq!(d.shape, Some(if reversed { shape.iter().rev().copied().collect() } else { shape }), "{at}");
            assert!(d.chains.is_empty(), "{at}");
            ungridded += 1;
        }
    }
    assert!(described > 30 && ungridded > 0 && deflated > 0, "{described} described, {ungridded} with no grid, {deflated} deflated");
}

/// Each array's description is found by its key, its `header` element (spec/stored-array.md §2.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#21-keys>): a `.npy` file's is
/// `header`, and a `.npz` archive's member's `entries/<i>/data/header`, i its position among the
/// source metadata's `members`, for every member whose header was read, stored or deflated,
/// presented or not; the parser's description gives a presented array's key as its `stored`, and
/// for a deflated member its `deflate`, the central directory's CRC-32 and size.
#[test]
fn every_fixtures_descriptions_are_found_by_their_header_elements() {
    let dir = common::fixtures().join("npy");
    let mut names: Vec<_> = std::fs::read_dir(&dir).unwrap().map(|e| e.unwrap().path()).collect();
    names.sort();
    let mut found = 0;
    for path in names.iter().filter(|p| !p.file_name().unwrap().to_str().unwrap().contains("reject")) {
        let at = path.display().to_string();
        let (ir, description) = refkv_ir::parse_bytes::<Npy>(&std::fs::read(path).unwrap()).unwrap();
        let by_path: std::collections::HashMap<String, &refkv_ir::array::StoredArray> =
            ir.stored.arrays.iter().map(|d| (ir.path(d.element), d)).collect();
        assert!(by_path.values().all(|d| d.index.is_none()), "{at}");
        assert_eq!(by_path.len(), ir.stored.arrays.len(), "{at}: one description a header");
        found += ir.stored.arrays.len();
        let presented = description["arrays"].as_array().unwrap();
        let Some(members) = description["own"].get("members").and_then(|m| m.as_array()) else {
            assert_eq!(by_path.keys().collect::<Vec<_>>(), vec!["header"], "{at}");
            assert_eq!(presented[0]["stored"].as_u64(), Some(by_path["header"].element as u64), "{at}");
            continue;
        };
        let mut described = 0;
        for (i, m) in members.iter().enumerate() {
            let key = by_path.get(&format!("entries/{i}/data/header"));
            assert_eq!(key.is_some(), m.get("header").is_some(), "{at}: member {i}");
            described += key.is_some() as usize;
            if let Some(a) = presented.iter().find(|a| m.get("path") == Some(&a["path"])) {
                assert_eq!(a["stored"].as_u64(), key.map(|d| d.element as u64), "{at}: member {i}");
                assert_eq!(a.get("deflate").is_some(), m["method"].as_u64() == Some(8), "{at}: member {i}");
                if let Some(d) = a.get("deflate") {
                    assert_eq!(d["size"], m["size"], "{at}: member {i}");
                }
            }
        }
        assert_eq!(described, by_path.len(), "{at}");
    }
    assert!(found > 40, "{found}");
}

/// A version 1.0 `.npy` file of the header `{'descr': <descr>, 'fortran_order': False, 'shape':
/// <shape>, }`, and no data.
fn npy_of(descr: &str, shape: &str) -> Vec<u8> {
    let mut header = format!("{{'descr': '{descr}', 'fortran_order': False, 'shape': {shape}, }}");
    while (10 + header.len() + 1) % 64 != 0 {
        header.push(' ');
    }
    header.push('\n');
    let mut out = b"\x93NUMPY\x01\x00".to_vec();
    out.extend_from_slice(&(header.len() as u16).to_le_bytes());
    out.extend_from_slice(header.as_bytes());
    out
}

/// An array of no elements whose element's bits pass 2^64 parses and is presented (the
/// description's one array, of no data), and is described with no grid: the general
/// fallback, not a rejection.
#[test]
fn an_array_whose_element_bits_pass_2_to_the_64_is_described_with_no_grid() {
    for (descr, item) in [("|V3000000000000000000", 3_000_000_000_000_000_000u64), ("<U1000000000000000000", 4_000_000_000_000_000_000)] {
        let data = npy_of(descr, "(0,)");
        let (ir, description) = refkv_ir::parse_bytes::<Npy>(&data).unwrap_or_else(|e| panic!("{descr}: {e}"));
        assert_eq!(common::rebuilt(&ir, &data), data, "{descr}");
        let arrays = description["arrays"].as_array().unwrap();
        assert_eq!(arrays.len(), 1, "{descr}");
        let (h, t) = declared(&arrays[0]);
        assert_eq!((arrays[0]["path"].as_str(), t.item, h.shape.clone()), (Some("array"), item, vec![0]), "{descr}");
        let s = &ir.stored;
        assert_eq!(arrays[0]["stored"].as_u64(), Some(s.arrays[0].element as u64), "{descr}");
        assert_eq!(s.arrays.len(), 1, "{descr}");
        assert_eq!((s.arrays[0].shape.clone(), s.arrays[0].item_bits, s.arrays[0].grid.is_none(), s.arrays[0].chains.len()), (Some(vec![0]), None, true, 0), "{descr}");
        assert!(s.chains.is_empty() && s.map.is_empty(), "{descr}");
    }
}

/// An array the profile presents whose description has no grid is projected from its `data`
/// element, not from a unit it does not have: `a<n>`, which the profile reads as `S<n>` and
/// NumPy's own layout gives no size, so that its stored array has no grid and no row.
#[test]
fn an_array_whose_description_has_no_grid_is_projected_from_its_data_element() {
    use refkv_ir::out::Part;
    let mut data = npy_of("|a5", "(3,)");
    let header_end = data.len() as u64;
    data.extend_from_slice(b"abcdefghijklmno");
    let (ir, description) = refkv_ir::parse_bytes::<Npy>(&data).unwrap();
    assert_eq!(common::rebuilt(&ir, &data), data);
    let s = &ir.stored;
    assert_eq!((s.arrays.len(), s.arrays[0].grid.is_none(), s.map.len()), (1, true, 0));
    assert_eq!(description["arrays"][0]["stored"].as_u64(), Some(s.arrays[0].element as u64));
    assert!(refkv_ir::project::npy::reads(&ir, &description).unwrap().is_empty());
    let out = refkv_ir::project::npy::project(&ir, &description, "u", &Default::default()).unwrap();
    // three elements of five bytes, uint8 with a trailing axis: one chunk, the data after the header
    assert_eq!(out.refs, vec![("array/c/0/0".to_string(), vec![Part::Src(header_end, 15)])]);
}

/// An array of no elements projects, its shape and no chunks, whatever its other axes are:
/// their product is not counted where one axis is 0, as the parser does not count it.
#[test]
fn an_array_of_no_elements_projects_whatever_its_other_axes() {
    for shape in ["(4294967296, 4294967296, 0)", "(0, 4294967296, 4294967296)", "(0,)"] {
        let data = npy_of("<f8", shape);
        let (ir, description) = refkv_ir::parse_bytes::<Npy>(&data).unwrap_or_else(|e| panic!("{shape}: {e}"));
        let out = refkv_ir::project::npy::project(&ir, &description, "u", &Default::default()).unwrap_or_else(|e| panic!("{shape}: {e}"));
        assert!(out.refs.is_empty() && out.entries.iter().any(|(k, _)| k == "array/zarr.json"), "{shape}");
    }
}

// one test per case of conformance/zarrify/rejects/npy.json (build.rs), each the message its check gives
macro_rules! reject {
    ($name:ident, $input:literal, $msg:literal) => {
        common::reject!($name, Npy, concat!("npy/", $input), $msg);
    };
}
include!(concat!(env!("OUT_DIR"), "/rejects_npy.rs"));

/// A deflated member's preamble and header, which lie in what its stream inflates to, are in the
/// IR mirror's view, in the document of the member's derived element, and a validator rebuilds
/// them from the source: the stream's start, inflated as far as the header's end (conventions
/// §8.8 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#88-canonical-form>). A
/// source whose stream inflates to another header fails the check.
#[test]
fn a_deflated_members_header_is_in_the_view_and_a_validator_derives_it_again() {
    use refkv_ir::mirror::{canonical_problem, mirror, view_problem};
    let data = std::fs::read(common::fixtures().join("npy").join("npz_compressed.npz")).unwrap();
    let (ir, description) = refkv_ir::parse_bytes::<Npy>(&data).unwrap();
    let header = description["arrays"][0]["stored"].as_u64().unwrap() as usize;
    let stream = ir.space[header] as usize;
    assert_eq!(ir.kind[stream], refkv_ir::ir::DERIVED);
    let mut out = refkv_ir::out::Out::new("u");
    mirror(&ir, &mut out, "npy").unwrap();
    let get = |k: &str| out.entries.iter().find(|e| e.0 == k).map(|e| e.1.clone());
    let keys: Vec<String> = out.entries.iter().map(|e| e.0.clone()).collect();
    let tree: serde_json::Value = serde_json::from_slice(&get("refkv_source/tree/zarr.json").unwrap()).unwrap();
    let member = &tree["attributes"]["refkv_zarrified"]["npy"]["entries/0"]["data"];
    assert_eq!(member["$vz"], "derived", "{member}");
    assert_eq!(member["header"], description["arrays"][0]["header"], "{member}");
    assert_eq!(member["preamble"]["major"], 1, "{member}");
    let end = (ir.start[header] + ir.len[header]) as usize;
    assert_eq!(member["$form"], serde_json::json!({"transform": "deflate", "prefix": end, "size": end + 32}).to_string(), "{member}");

    let mut read = |o: u64, n: u64| Ok(data[o as usize..(o + n) as usize].to_vec());
    assert_eq!(canonical_problem(&get, &mut read), None);
    assert_eq!(view_problem(&keys, &get, &mut read), None);

    // the same archive with another member's stream where this one's is: another header
    let other = description["arrays"][1]["stored"].as_u64().unwrap() as usize;
    let (from, to) = (ir.space[other] as usize, stream);
    let mut swapped = data.clone();
    let n = ir.len[from].min(ir.len[to]) as usize;
    let (a, b) = (ir.start[from] as usize, ir.start[to] as usize);
    swapped.copy_within(a..a + n, b);
    let mut read = |o: u64, n: u64| Ok(swapped[o as usize..(o + n) as usize].to_vec());
    assert!(view_problem(&keys, &get, &mut read).is_some());
}
