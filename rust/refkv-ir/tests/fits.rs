//! The FITS parser (src/fits.rs; spec/zarrify/fits.md): one test per rejected fixture.

mod common;

use refkv_ir::fits::Fits;

// FITS (spec/zarrify/fits.md §9.3
// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#93-checks>): one per rejected
// fixture, each the message its check gives.
// one test per case of conformance/zarrify/rejects/fits.json (build.rs), each the message its check gives
macro_rules! reject {
    ($name:ident, $input:literal, $msg:literal) => {
        common::reject!($name, Fits, concat!("fits/", $input), $msg);
    };
}
include!(concat!(env!("OUT_DIR"), "/rejects_fits.rs"));

/// Each HDU of an accepted fixture names its descriptions by their key, the element of its header
/// (spec/stored-array.md §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): an image's
/// one, a table's two and a tile-compressed image's three, indexed from 0, and none for an HDU of
/// no axes. A tile's row is in an element of its HDU's heap, and every description is one an HDU
/// names.
#[test]
fn every_accepted_fixtures_hdus_name_their_descriptions() {
    let (mut hdus, mut arrays, mut tiles) = (0, 0, 0);
    for e in std::fs::read_dir(common::fixtures().join("fits")).unwrap() {
        let p = e.unwrap().path();
        let name = p.file_name().unwrap().to_str().unwrap().to_string();
        if name.contains("reject") || !name.contains(".fits") {
            continue;
        }
        let (ir, facts) = refkv_ir::parse_bytes::<Fits>(&std::fs::read(&p).unwrap()).unwrap();
        let s = &ir.stored;
        let mut named = 0;
        for (i, h) in facts["hdus"].as_array().unwrap().iter().enumerate() {
            let at = format!("{name} HDU {i}");
            let tiled = !h["compression"].is_null();
            let want = match h["kind"].as_str().unwrap() {
                "empty" => 0,
                "table" => 2,
                _ if tiled => 3,
                _ => 1,
            };
            hdus += 1;
            let Some(key) = h["declared_by"].as_u64() else {
                assert_eq!(want, 0, "{at}");
                continue;
            };
            assert_eq!(Some(key), h["elements"]["header"].as_u64(), "{at}");
            assert!(ir.path(key as u32).ends_with(&format!("hdus/{i}/header")), "{at}: {}", ir.path(key as u32));
            let described: Vec<usize> = (0..s.arrays.len()).filter(|&k| s.arrays[k].element == key as u32).collect();
            let indexes: Vec<Option<u64>> = described.iter().map(|&k| s.arrays[k].index).collect();
            assert_eq!(indexes, (0..want).map(Some).collect::<Vec<_>>(), "{at}");
            named += described.len();
            if tiled {
                // each tile the table holds is a row, a range of its chunk of the heap
                let chunks: Vec<u64> = h["compression"]["chunks"].as_array().unwrap().iter().filter_map(|c| c["element"].as_u64()).collect();
                let rows: Vec<usize> = (0..s.map.len()).filter(|&r| s.map.array[r] == described[0] as u32).collect();
                assert_eq!(rows.len() as u64, h["compression"]["present"].as_u64().unwrap(), "{at}");
                assert!(rows.iter().all(|&r| chunks.contains(&(s.map.element[r].unwrap() as u64))), "{at}");
                tiles += rows.len();
            }
        }
        assert_eq!(named, s.arrays.len(), "{name}");
        arrays += named;
    }
    assert!(hdus >= 200 && arrays >= 200 && tiles >= 1000, "{hdus} HDUs, {arrays} arrays, {tiles} tiles");
}
