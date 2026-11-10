//! The MRC parser (src/mrc.rs; spec/zarrify/mrc.md): one test per rejected fixture, and the
//! accepted fixtures' facts against their stored arrays' descriptions.

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

/// Each accepted fixture's facts name its array's description by the description's key
/// (spec/stored-array.md §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): the `header`
/// element, or a gzipped file's `data` element, the stream; the file's one description, with a
/// grid, whose one row is the `data` element the facts name.
#[test]
fn every_accepted_fixtures_facts_name_its_description() {
    let (mut files, mut gzipped) = (0, 0);
    for e in std::fs::read_dir(common::fixtures().join("mrc")).unwrap() {
        let p = e.unwrap().path();
        let name = p.file_name().unwrap().to_str().unwrap().to_string();
        if name.contains("reject") {
            continue;
        }
        let (ir, facts) = refkv_ir::parse_bytes::<Mrc>(&std::fs::read(&p).unwrap()).unwrap();
        let s = &ir.stored;
        let key = facts["declared_by"].as_u64().unwrap_or_else(|| panic!("{name}: no declared_by")) as u32;
        let stream = facts["gzip"].is_object();
        assert_eq!(ir.path(key), if stream { "data" } else { "header" }, "{name}");
        assert_eq!(facts["header"].as_u64().map(|h| h as u32), (!stream).then_some(key), "{name}");
        let [a] = &s.arrays[..] else { panic!("{name}: {} descriptions", s.arrays.len()) };
        assert_eq!((a.element, a.index), (key, None), "{name}");
        assert!(a.grid.is_some(), "{name}");
        assert_eq!(s.map.len(), 1, "{name}");
        assert_eq!(s.map.element[0].map(u64::from), facts["data"].as_u64(), "{name}");
        files += 1;
        gzipped += usize::from(stream);
    }
    assert!(files >= 70 && gzipped >= 4, "{files} files, {gzipped} gzipped");
}
