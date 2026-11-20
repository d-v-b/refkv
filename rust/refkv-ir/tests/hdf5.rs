//! The HDF5 parser (src/hdf5/file.rs; spec/zarrify/hdf5.md): one test per rejected fixture, an
//! Imaris file handed to the IMS parser, and the mirror's rebuild of each accepted fixture.

mod common;

use common::{fixtures, rebuilt};
use refkv_ir::hdf5::Hdf5;
use refkv_ir::Parser;

// one test per case of conformance/zarrify/rejects/hdf5.json (build.rs), each the message its check gives
macro_rules! reject {
    ($name:ident, $input:literal, $msg:literal) => {
        common::reject!($name, Hdf5, concat!("hdf5/", $input), $msg);
    };
}
include!(concat!(env!("OUT_DIR"), "/rejects_hdf5.rs"));

/// Every accepted fixture parses, and its IR rebuilds the file byte for byte.
/// The parse of a file on its own, the siblings it names found absent.
fn alone(data: &[u8]) -> Result<(refkv_ir::ir::Ir, serde_json::Value), String> {
    let mut p = Hdf5::new(data.len() as u64);
    p.resolve(Default::default());
    loop {
        match p.step()? {
            refkv_ir::nd2::Step::Read(r) => {
                for (o, n) in r {
                    p.feed(o, data[o as usize..(o + n) as usize].to_vec());
                }
            }
            _ => break,
        }
    }
    p.finish()
}

#[test]
fn accepted_fixtures_rebuild() {
    let mut n = 0;
    for e in std::fs::read_dir(fixtures().join("hdf5")).unwrap() {
        let p = e.unwrap().path();
        let name = p.file_name().unwrap().to_str().unwrap().to_string();
        if name.contains("reject") || !(name.ends_with(".h5") || name.ends_with(".nwb")) {
            continue;
        }
        let data = std::fs::read(&p).unwrap();
        let (ir, facts) = alone(&data).unwrap_or_else(|e| panic!("{name}: {e}"));
        assert!(facts["nodes"].as_array().is_some_and(|n| !n.is_empty()), "{name}");
        assert_eq!(rebuilt(&ir, &data), data, "{name}");
        n += 1;
    }
    assert!(n >= 10, "{n} fixtures");
}

/// Every array of an accepted fixture's facts names its description by the description's key:
/// the struct of the dataset's object, each object's its own.
#[test]
fn every_accepted_fixtures_arrays_name_their_descriptions() {
    let mut arrays = 0;
    for e in std::fs::read_dir(fixtures().join("hdf5")).unwrap() {
        let p = e.unwrap().path();
        let name = p.file_name().unwrap().to_str().unwrap().to_string();
        if name.contains("reject") || !(name.ends_with(".h5") || name.ends_with(".nwb")) {
            continue;
        }
        let (ir, facts) = alone(&std::fs::read(&p).unwrap()).unwrap_or_else(|e| panic!("{name}: {e}"));
        let keys: Vec<u32> = ir.stored.arrays.iter().map(|d| d.element).collect();
        let mut by_address = std::collections::HashMap::new();
        for node in facts["nodes"].as_array().unwrap() {
            let Some(a) = node.get("array") else { continue };
            let at = format!("{name}: {}", node["path"]);
            let key = a["declared_by"].as_u64().unwrap_or_else(|| panic!("{at}: no declared_by")) as u32;
            assert_eq!(keys.iter().filter(|&&k| k == key).count(), 1, "{at}");
            assert_eq!(ir.names.get(ir.name[ir.parent[key as usize] as usize]), "objects", "{at}");
            by_address.insert(node["own"]["address"].as_u64().unwrap(), key);
            arrays += 1;
        }
        let mut distinct: Vec<u32> = by_address.values().copied().collect();
        distinct.sort_unstable();
        distinct.dedup();
        assert_eq!(distinct.len(), by_address.len(), "{name}: two objects name one description");
    }
    assert!(arrays >= 100, "{arrays} arrays");
}

/// In a store run, each array of the facts names its description as the store IR numbers it: an
/// element of the array's object, the named file's or a linked file's.
#[test]
fn a_store_runs_arrays_name_their_descriptions_in_their_objects() {
    use refkv_ir::store::{StoreFormat, StoreRun};
    let dir = fixtures().join("hdf5");
    let key = "hdf5_external.h5".to_string();
    let mut files = std::collections::BTreeMap::from([(key.clone(), std::fs::read(dir.join(&key)).unwrap())]);
    let mut run = StoreRun::new(StoreFormat::Hdf5, vec![(key.clone(), files[&key].len() as u64)], 4).unwrap();
    while let Some(reqs) = run.poll().unwrap() {
        if reqs.is_empty() {
            for k in run.probes() {
                let data = std::fs::read(dir.join(&k)).ok();
                run.found(data.as_ref().map(|d| d.len() as u64)).unwrap();
                files.extend(data.map(|d| (k, d)));
            }
            continue;
        }
        for (k, q) in reqs.iter().enumerate() {
            let data = &files[&q.key];
            run.complete(k, q.spans.iter().map(|&(a, b)| data[a as usize..b as usize].to_vec()).collect()).unwrap();
        }
    }
    let (ir, facts, _) = run.finish().unwrap();
    let keys: Vec<u32> = ir.stored.arrays.iter().map(|d| d.element).collect();
    let mut objects = std::collections::BTreeSet::new();
    for node in facts["nodes"].as_array().unwrap() {
        let Some(a) = node.get("array") else { continue };
        let at = node["path"].to_string();
        let key = a["declared_by"].as_u64().unwrap_or_else(|| panic!("{at}: no declared_by")) as u32;
        assert_eq!(keys.iter().filter(|&&k| k == key).count(), 1, "{at}");
        let object = a["store"]["object"].as_u64().unwrap() as u32;
        let objects_of = ir.parent[key as usize];
        assert_eq!((ir.names.get(ir.name[objects_of as usize]), ir.parent[objects_of as usize]), ("objects", object), "{at}");
        objects.insert(object);
    }
    assert_eq!(objects.len(), 2, "arrays of the named file and of a linked one");
}

/// An Imaris file is the IMS parser's: the HDF5 parser finds it and hands it over, and the IR
/// is the IMS parser's.
#[test]
fn an_imaris_file_is_found_to_be_ims() {
    let data = std::fs::read(fixtures().join("ims/ims_earliest_uint16_deflate.ims")).unwrap();
    let mut p = Hdf5::new(data.len() as u64);
    loop {
        match p.step().unwrap() {
            refkv_ir::nd2::Step::Read(r) => {
                for (o, n) in r {
                    p.feed(o, data[o as usize..(o + n) as usize].to_vec());
                }
            }
            refkv_ir::nd2::Step::Beside => panic!("the file names siblings"),
            refkv_ir::nd2::Step::Done => break,
        }
    }
    assert_eq!(Parser::found(&p), Some("ims"));
    let (_, facts) = p.finish().unwrap();
    let (_, direct) = refkv_ir::parse_bytes::<refkv_ir::ims::Ims>(&data).unwrap();
    assert_eq!(facts["arrays"], direct["arrays"]);
}
