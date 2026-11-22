//! The MRC parser (src/mrc.rs; spec/zarrify/mrc.md): one test per rejected fixture, and the
//! accepted fixtures' descriptions against their stored arrays' descriptions.

mod common;

use refkv_ir::mrc::Mrc;

// one test per rejected fixture, each the message its check gives
// one test per case of conformance/zarrify/rejects/mrc.json (build.rs), each the message its check gives
macro_rules! reject {
    ($name:ident, $input:literal, $msg:literal) => {
        common::reject!($name, Mrc, concat!("mrc/", $input), $msg);
    };
}
include!(concat!(env!("OUT_DIR"), "/rejects_mrc.rs"));

/// Each accepted fixture's description (spec/zarrify/mrc.md §8.5
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mrc.md#85-the-description>) names its
/// array's stored description by its key (spec/stored-array.md §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): the `header`
/// element, or a gzipped file's `data` element, the stream; the file's one stored description, with
/// a grid, whose one row is the `data` element it names. It holds none of the output's decisions.
#[test]
fn every_accepted_fixtures_description_names_its_stored_array() {
    let (mut files, mut gzipped) = (0, 0);
    for e in std::fs::read_dir(common::fixtures().join("mrc")).unwrap() {
        let p = e.unwrap().path();
        let name = p.file_name().unwrap().to_str().unwrap().to_string();
        if name.contains("reject") {
            continue;
        }
        let (ir, description) = refkv_ir::parse_bytes::<Mrc>(&std::fs::read(&p).unwrap()).unwrap();
        let s = &ir.stored;
        for decision in ["declared_by", "data_type", "copy_threshold", "kind", "axes"] {
            assert!(description.get(decision).is_none(), "{name}: {decision}");
        }
        let elements = &description["elements"];
        let stream = description["gzip"].is_object();
        let key = elements[if stream { "data" } else { "header" }].as_u64().unwrap() as u32;
        assert_eq!(ir.path(key), if stream { "data" } else { "header" }, "{name}");
        let [a] = &s.arrays[..] else { panic!("{name}: {} descriptions", s.arrays.len()) };
        assert_eq!((a.element, a.index), (key, None), "{name}");
        assert!(a.grid.is_some(), "{name}");
        assert_eq!(s.map.len(), 1, "{name}");
        assert_eq!(s.map.element[0].map(u64::from), elements["data"].as_u64(), "{name}");
        files += 1;
        gzipped += usize::from(stream);
    }
    assert!(files >= 70 && gzipped >= 4, "{files} files, {gzipped} gzipped");
}
