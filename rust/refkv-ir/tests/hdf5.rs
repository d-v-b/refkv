//! The HDF5 parser (src/hdf5/file.rs; spec/zarrify/hdf5.md): one test per rejected fixture, an
//! Imaris file handed to the IMS parser, the mirror's rebuild of each accepted fixture, and the
//! description's datasets and their stored arrays.

mod common;

use common::{fixtures, rebuilt};
use refkv_ir::hdf5::Hdf5;
use refkv_ir::Parser;

// one test per case of conformance/zarrify/rejects/hdf5.json (build.rs), each the message its check
// gives: the parser's, or, for a dataset whose elements do not read, the hierarchy's that presents it
macro_rules! reject {
    ($name:ident, $input:literal, $msg:literal) => {
        #[test]
        fn $name() {
            let data = std::fs::read(common::fixtures().join(concat!("hdf5/", $input))).unwrap();
            let got = refkv_ir::parse_bytes::<Hdf5>(&data).and_then(|(_, facts)| refkv_ir::project::hdf5_nodes::nodes(&facts).map(|_| ()));
            match got {
                Err(m) => assert!(m.starts_with($msg), "{m}"),
                Ok(_) => panic!("{} was accepted", $input),
            }
        }
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
        assert!(facts["objects"].as_array().is_some_and(|n| !n.is_empty()), "{name}");
        assert_eq!(rebuilt(&ir, &data), data, "{name}");
        n += 1;
    }
    assert!(n >= 10, "{n} fixtures");
}

/// Every dataset of an accepted fixture's description has one stored-array description, keyed by
/// the struct of its object (`objects/<i>`, `i` its position among the description's objects), and
/// its storage element, where it has one, lies in that struct.
#[test]
fn every_accepted_fixtures_datasets_have_their_descriptions() {
    let mut datasets = 0;
    for e in std::fs::read_dir(fixtures().join("hdf5")).unwrap() {
        let p = e.unwrap().path();
        let name = p.file_name().unwrap().to_str().unwrap().to_string();
        if name.contains("reject") || !(name.ends_with(".h5") || name.ends_with(".nwb")) {
            continue;
        }
        let (ir, facts) = alone(&std::fs::read(&p).unwrap()).unwrap_or_else(|e| panic!("{name}: {e}"));
        let keys: Vec<u32> = ir.stored.arrays.iter().map(|d| d.element).collect();
        for (i, o) in facts["objects"].as_array().unwrap().iter().enumerate() {
            if o["kind"] != "dataset" {
                continue;
            }
            let at = format!("{name}: {}", o["segments"]);
            let found: Vec<u32> = keys.iter().copied().filter(|&k| ir.path(k) == format!("objects/{i}")).collect();
            assert_eq!(found.len(), 1, "{at}");
            if let Some(el) = o["storage"]["element"].as_u64() {
                assert!(ir.path(el as u32).starts_with(&format!("objects/{i}/")), "{at}");
            }
            datasets += 1;
        }
    }
    assert!(datasets >= 100, "{datasets} datasets");
}

/// In a store run, the description of each file holds its datasets' storage elements as the store
/// IR numbers them, each in the file's object.
#[test]
fn a_store_runs_datasets_name_their_storage_in_their_objects() {
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
    let mut objects = std::collections::BTreeSet::new();
    for (key, d) in facts["files"].as_object().unwrap() {
        for o in d["objects"].as_array().unwrap() {
            let Some(el) = o["storage"]["element"].as_u64() else { continue };
            let object = o["storage"]["object"].as_u64().unwrap() as u32;
            let mut up = el as u32;
            while up != 0 && up != object {
                up = ir.parent[up as usize];
            }
            assert_eq!(up, object, "{key}: {}", o["segments"]);
            objects.insert(object);
        }
    }
    assert_eq!(objects.len(), 2, "datasets of the named file and of a linked one");
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
