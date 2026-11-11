//! The NDPI profile (spec/zarrify/ndpi.md), read by the TIFF parser: one test per rejected
//! fixture.

mod common;

use refkv_ir::tiff::Tiff;

// one test per rejected fixture, each the message its check gives
// one test per case of conformance/zarrify/rejects/ndpi.json (build.rs), each the message its check gives
macro_rules! reject {
    ($name:ident, $input:literal, $msg:literal) => {
        common::reject!($name, Tiff, concat!("ndpi/", $input), $msg);
    };
}
include!(concat!(env!("OUT_DIR"), "/rejects_ndpi.rs"));

/// Each plane of each level of an accepted NDPI fixture names its description by the
/// description's key, the element of the plane's IFD (spec/stored-array.md §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): a main-chain
/// IFD's, with a grid, whose rows' elements are the IFD's strip or tiles.
#[test]
fn every_accepted_fixtures_planes_name_their_descriptions() {
    let (mut planes, mut cut) = (0, 0);
    for e in std::fs::read_dir(common::fixtures().join("ndpi")).unwrap() {
        let p = e.unwrap().path();
        let name = p.file_name().unwrap().to_str().unwrap().to_string();
        if name.contains("reject") || !name.ends_with(".ndpi") {
            continue;
        }
        let (ir, facts) = refkv_ir::parse_bytes::<Tiff>(&std::fs::read(&p).unwrap()).unwrap();
        let s = &ir.stored;
        for (l, lv) in facts["levels"].as_array().unwrap().iter().enumerate() {
            let (units, keys) = (lv["planes"].as_array().unwrap(), lv["declared_by"].as_array().unwrap());
            assert_eq!(units.len(), keys.len(), "{name} level {l}");
            assert!(lv.get("ifds").is_none(), "{name} level {l}");
            for (p, key) in keys.iter().enumerate() {
                let at = format!("{name} level {l} plane {p}");
                let key = key.as_u64().unwrap() as u32;
                let described: Vec<usize> = (0..s.arrays.len()).filter(|&k| s.arrays[k].element == key).collect();
                let [k] = described[..] else { panic!("{at}: {} descriptions", described.len()) };
                assert!(s.arrays[k].grid.is_some(), "{at}");
                assert!(ir.path(key).rsplit('/').nth(1).is_some_and(|n| n == "ifds"), "{at}: {}", ir.path(key));
                let rows: Vec<usize> = (0..s.map.len()).filter(|&r| s.map.array[r] == k as u32).collect();
                assert!(!rows.is_empty(), "{at}");
                // a unit is a strip or a tile of the IFD, or a range of its one strip
                assert!(rows.iter().all(|&r| ir.parent[ir.parent[s.map.element[r].unwrap() as usize] as usize] == key), "{at}");
                planes += 1;
                cut += usize::from(rows.iter().any(|&r| s.map.range[r].is_some()));
            }
        }
    }
    assert!(planes >= 30 && cut >= 10, "{planes} planes, {cut} cut into intervals");
}
