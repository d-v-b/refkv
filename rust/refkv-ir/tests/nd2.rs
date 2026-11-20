//! The ND2 parser (src/nd2.rs; spec/zarrify/nd2.md): one test per rejected fixture.

mod common;

use refkv_ir::nd2::Nd2;

// one test per case of conformance/zarrify/rejects/nd2.json (build.rs), each the message its check gives
macro_rules! reject {
    ($name:ident, $input:literal, $msg:literal) => {
        common::reject!($name, Nd2, concat!("nd2/", $input), $msg);
    };
}
include!(concat!(env!("OUT_DIR"), "/rejects_nd2.rs"));

/// Each accepted fixture's facts name its descriptions by their keys (spec/stored-array.md §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): `declared_by`
/// the `ImageAttributesLV!` chunk, which keys the one array of the frames the loops count, whose
/// rows are of the frames the facts count as placed, each a frame's `pixels`; and `unplaced` the
/// `frames` element, which keys an array, indexed from 0, for each frame chunk past them, whose
/// rows are of a frame's `data`.
#[test]
fn every_accepted_fixtures_facts_name_their_descriptions() {
    let (mut files, mut past, mut rows) = (0, 0, 0);
    for e in std::fs::read_dir(common::fixtures().join("nd2")).unwrap() {
        let p = e.unwrap().path();
        let name = p.file_name().unwrap().to_str().unwrap().to_string();
        if name.contains("reject") || !name.ends_with(".nd2") {
            continue;
        }
        let (ir, facts) = refkv_ir::parse_bytes::<Nd2>(&std::fs::read(&p).unwrap()).unwrap();
        let s = &ir.stored;
        let key = facts["declared_by"].as_u64().unwrap() as u32;
        assert_eq!(ir.path(key), "chunks/ImageAttributesLV!", "{name}");
        let image: Vec<usize> = (0..s.arrays.len()).filter(|&k| s.arrays[k].element == key).collect();
        let [k] = image[..] else { panic!("{name}: {} descriptions of the frames", image.len()) };
        assert!(s.arrays[k].index.is_none() && s.arrays[k].grid.is_some(), "{name}");
        let of = |k: usize| (0..s.map.len()).filter(move |&r| s.map.array[r] == k as u32);
        let frames: std::collections::BTreeSet<u64> = of(k).map(|r| s.map.coords_of(r)[0]).collect();
        assert_eq!(frames.len() as u64, facts["placed"].as_u64().unwrap(), "{name}");
        assert!(of(k).all(|r| ir.path(s.map.element[r].unwrap()).ends_with("/pixels")), "{name}");
        rows += of(k).count();
        let unplaced = &facts["unplaced"];
        let frames_element = unplaced["declared_by"].as_u64().unwrap() as u32;
        assert_eq!(ir.path(frames_element), "frames", "{name}");
        let beyond: Vec<usize> = (0..s.arrays.len()).filter(|&k| s.arrays[k].element == frames_element).collect();
        let indexes: Vec<Option<u64>> = beyond.iter().map(|&k| s.arrays[k].index).collect();
        assert_eq!(indexes, (0..unplaced["count"].as_u64().unwrap()).map(Some).collect::<Vec<_>>(), "{name}");
        assert_eq!(1 + beyond.len(), s.arrays.len(), "{name}");
        for &k in &beyond {
            assert!(of(k).all(|r| ir.path(s.map.element[r].unwrap()).ends_with("/data") && s.map.range[r].is_some()), "{name}");
        }
        files += 1;
        past += beyond.len();
    }
    assert!(files >= 30 && past >= 5 && rows >= 14000, "{files} files, {past} arrays past the count, {rows} rows");
}
