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

/// Each accepted fixture's descriptions are keyed by their chunks (spec/stored-array.md §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): the
/// `ImageAttributesLV!` chunk keys the one array of the frames the loops count (their counts'
/// product, its first axis), whose rows are each a frame's `pixels`; and the `frames` element keys
/// an array, indexed from 0, for each frame chunk past them, whose rows are of a frame's `data`.
#[test]
fn every_accepted_fixtures_descriptions_are_keyed_by_their_chunks() {
    let (mut files, mut past_arrays, mut rows) = (0, 0, 0);
    for e in std::fs::read_dir(common::fixtures().join("nd2")).unwrap() {
        let p = e.unwrap().path();
        let name = p.file_name().unwrap().to_str().unwrap().to_string();
        if name.contains("reject") || !name.ends_with(".nd2") {
            continue;
        }
        let (ir, description) = refkv_ir::parse_bytes::<Nd2>(&std::fs::read(&p).unwrap()).unwrap();
        let s = &ir.stored;
        let by_path = |path: &str| (0..ir.len() as u32).find(|&i| ir.path(i) == path).unwrap();
        let key = by_path("chunks/ImageAttributesLV!");
        let image: Vec<usize> = (0..s.arrays.len()).filter(|&k| s.arrays[k].element == key).collect();
        let [k] = image[..] else { panic!("{name}: {} descriptions of the frames", image.len()) };
        assert!(s.arrays[k].index.is_none() && s.arrays[k].grid.is_some(), "{name}");
        let counted: u64 = description["loops"].as_array().unwrap().iter().map(|l| l["count"].as_u64().unwrap()).product();
        assert_eq!(s.arrays[k].shape.as_ref().unwrap()[0], counted, "{name}");
        // the frame chunks the IR holds data of, by their numbers: `frames/<f>` with `pixels` (a
        // frame the loops count) or `data` (one past them)
        let frames_element = by_path("frames");
        let held = |part: &str| -> std::collections::BTreeSet<u64> {
            let mut out = std::collections::BTreeSet::new();
            for &c in ir.children(frames_element) {
                if ir.children(c).iter().any(|&d| ir.name_of(d) == part) {
                    let first = ir.nidx[c as usize];
                    out.extend((0..ir.run(c).map_or(1, |r| r.0)).map(|j| first + j));
                }
            }
            out
        };
        let (placed, past) = (held("pixels"), held("data"));
        let of = |k: usize| (0..s.map.len()).filter(move |&r| s.map.array[r] == k as u32);
        assert!(of(k).all(|r| ir.path(s.map.element[r].unwrap()).ends_with("/pixels")), "{name}");
        let frames: std::collections::BTreeSet<u64> = of(k).map(|r| s.map.coords_of(r)[0]).collect();
        assert_eq!(frames, placed, "{name}");
        assert!(placed.iter().all(|&f| f < counted) && past.iter().all(|&f| f >= counted), "{name}");
        rows += of(k).count();
        let beyond: Vec<usize> = (0..s.arrays.len()).filter(|&k| s.arrays[k].element == frames_element).collect();
        let indexes: Vec<Option<u64>> = beyond.iter().map(|&k| s.arrays[k].index).collect();
        assert_eq!(indexes, (0..past.len() as u64).map(Some).collect::<Vec<_>>(), "{name}");
        assert_eq!(1 + beyond.len(), s.arrays.len(), "{name}");
        for &k in &beyond {
            assert!(of(k).all(|r| ir.path(s.map.element[r].unwrap()).ends_with("/data") && s.map.range[r].is_some()), "{name}");
        }
        files += 1;
        past_arrays += beyond.len();
    }
    assert!(files >= 30 && past_arrays >= 5 && rows >= 14000, "{files} files, {past_arrays} arrays past the count, {rows} rows");
}
