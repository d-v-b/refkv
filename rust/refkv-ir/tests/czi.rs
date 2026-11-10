//! The CZI parser (src/czi.rs; spec/zarrify/czi.md): one test per rejected fixture, and a
//! segment id read as its 16 bytes.

mod common;

use common::fixtures;
use refkv_ir::czi::Czi;
use refkv_ir::parse_bytes;

// one test per rejected fixture, each the message its check gives
// one test per case of conformance/zarrify/rejects/czi.json (build.rs), each the message its check gives
macro_rules! reject {
    ($name:ident, $input:literal, $msg:literal) => {
        common::reject!($name, Czi, concat!("czi/", $input), $msg);
    };
}
include!(concat!(env!("OUT_DIR"), "/rejects_czi.rs"));

#[test]
fn a_segment_id_with_bytes_after_its_nul_is_not_the_id() {
    // spec/zarrify/czi.md §2.1
    // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#21-segments>: the 16 bytes of
    // the id, NUL-padded; a byte after the NUL makes another id
    let mut data = std::fs::read(fixtures().join("czi/czi_gray8_single.czi")).unwrap();
    let at = (0..data.len() - 16).find(|&k| data[k..k + 15] == *b"ZISRAWSUBBLOCK\0").unwrap();
    data[at + 15] = 0xDF;
    match parse_bytes::<Czi>(&data) {
        Err(m) => assert!(m.contains("is not a ZISRAWSUBBLOCK segment"), "{m}"),
        Ok(_) => panic!("accepted"),
    }
}

/// The IR and the facts of the input `key` of fixtures/czi: a file's by its parser, and, of a
/// file that names siblings, a store run's, which finds its parts in the directory.
fn parsed(key: &str) -> Result<(refkv_ir::ir::Ir, serde_json::Value), String> {
    use refkv_ir::store::{StoreFormat, StoreRun};
    let dir = fixtures().join("czi");
    let data = std::fs::read(dir.join(key)).unwrap();
    match parse_bytes::<Czi>(&data) {
        Err(m) if m.starts_with("the file names siblings") => {}
        other => return other,
    }
    let mut files = std::collections::BTreeMap::from([(key.to_string(), data)]);
    let mut run = StoreRun::new(StoreFormat::Czi, vec![(key.to_string(), files[key].len() as u64)], 4)?;
    while let Some(reqs) = run.poll()? {
        if reqs.is_empty() {
            for k in run.probes() {
                let found = std::fs::read(dir.join(&k)).ok();
                run.found(found.as_ref().map(|d| d.len() as u64))?;
                files.extend(found.map(|d| (k, d)));
            }
            continue;
        }
        for (k, q) in reqs.iter().enumerate() {
            let object = &files[&q.key];
            run.complete(k, q.spans.iter().map(|&(a, b)| object[a as usize..b as usize].to_vec()).collect())?;
        }
    }
    let (ir, facts, _) = run.finish()?;
    Ok((ir, facts))
}

/// Every accepted fixture's facts name its subblocks' descriptions by their keys, the subblocks'
/// structs (spec/stored-array.md §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): `declared_by`
/// has an id for each entry whose subblock the run read, each description is keyed by one of
/// them, with no index, and an id that several entries give is one description's. Each cell of a
/// level and each member of a tile array names, in its `declared_by`, the description of the
/// subblock that holds its data element, which has a grid and one row. An image across files
/// names them as its store IR numbers them, each in the file that holds the subblock.
#[test]
fn every_accepted_fixtures_facts_name_their_descriptions() {
    let (mut inputs, mut refused, mut entries, mut elsewhere, mut again, mut placed, mut stores) = (0, 0, 0, 0, 0, 0, 0);
    for e in std::fs::read_dir(fixtures().join("czi")).unwrap() {
        let name = e.unwrap().file_name().into_string().unwrap();
        if name.contains("reject") || !name.ends_with(".czi") {
            continue;
        }
        let (ir, facts) = match parsed(&name) {
            Ok(read) => read,
            // a part of another fixture's image, and the fixture whose part is another image's
            Err(m) => {
                assert!(m.contains("zarrify that one") || m.contains("is not part of this image"), "{name}: {m}");
                refused += 1;
                continue;
            }
        };
        inputs += 1;
        stores += usize::from(ir.kind[1] == refkv_ir::ir::OBJECT);
        let s = &ir.stored;
        let described = |key: u32| -> Vec<usize> { (0..s.arrays.len()).filter(|&k| s.arrays[k].element == key).collect() };
        let keys: Vec<Option<u32>> = facts["declared_by"].as_array().unwrap().iter().map(|k| k.as_u64().map(|k| k as u32)).collect();
        let mut named = std::collections::BTreeSet::new();
        for (i, key) in keys.iter().enumerate() {
            let at = format!("{name} entry {i}");
            entries += 1;
            let Some(key) = *key else {
                // an entry of the named file whose subblock is in a part
                elsewhere += 1;
                continue;
            };
            assert_eq!(ir.kind[key as usize], refkv_ir::ir::STRUCT, "{at}");
            assert!(ir.path(key).rsplit('/').nth(1).is_some_and(|n| n == "subblocks"), "{at}: {}", ir.path(key));
            let [k] = described(key)[..] else { panic!("{at}: {} descriptions", described(key).len()) };
            assert_eq!(s.arrays[k].index, None, "{at}");
            again += usize::from(!named.insert(key));
        }
        assert_eq!(named.len(), s.arrays.len(), "{name}: a description no entry names");
        // each subblock the profile places, by its data element
        let levels = facts["images"].as_array().unwrap().iter().flat_map(|im| im["levels"].as_array().unwrap());
        let lists = levels.map(|lv| (&lv["cells"], &lv["declared_by"]));
        let tiles = facts["tiles"].as_array().unwrap().iter().map(|t| (&t["members"], &t["declared_by"]));
        for (members, declared) in lists.chain(tiles) {
            let (members, declared) = (members.as_array().unwrap(), declared.as_array().unwrap());
            assert_eq!(members.len(), declared.len(), "{name}");
            for (m, key) in members.iter().zip(declared) {
                let (data, key) = (m[0].as_u64().unwrap() as u32, key.as_u64().unwrap() as u32);
                let at = format!("{name} {}", ir.path(data));
                assert!(named.contains(&key), "{at}");
                assert_eq!(ir.parent[data as usize], key, "{at}");
                let [k] = described(key)[..] else { panic!("{at}: {} descriptions", described(key).len()) };
                assert!(s.arrays[k].grid.is_some(), "{at}");
                let rows: Vec<usize> = (0..s.map.len()).filter(|&r| s.map.array[r] == k as u32).collect();
                let [r] = rows[..] else { panic!("{at}: {} rows", rows.len()) };
                // the unit is the data element, or, of a Zstd1 subblock, a range of the file that
                // ends where the element does
                match s.map.ranges_of(r) {
                    None => assert_eq!(s.map.element[r], Some(data), "{at}"),
                    Some(ranges) => {
                        let end = ir.start[data as usize] + ir.len[data as usize];
                        assert_eq!(ranges.len(), 1, "{at}");
                        assert_eq!(s.map.range_offset[ranges.start] + s.map.range_length[ranges.start], end, "{at}");
                    }
                }
                placed += 1;
            }
        }
    }
    assert_eq!((inputs, refused, stores), (44, 8, 7));
    // the entry named again is a part's own, of a subblock its master's directory names too
    assert_eq!((entries, elsewhere, again, placed), (202, 3, 1, 183));
}
