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
        let (ir, facts) = refkv_ir::parse_bytes::<Hdf5>(&data).unwrap_or_else(|e| panic!("{name}: {e}"));
        assert!(facts["nodes"].as_array().is_some_and(|n| !n.is_empty()), "{name}");
        assert_eq!(rebuilt(&ir, &data), data, "{name}");
        n += 1;
    }
    assert!(n >= 10, "{n} fixtures");
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
            refkv_ir::nd2::Step::Done => break,
        }
    }
    assert_eq!(Parser::found(&p), Some("ims"));
    let (_, facts) = p.finish().unwrap();
    let (_, direct) = refkv_ir::parse_bytes::<refkv_ir::ims::Ims>(&data).unwrap();
    assert_eq!(facts["arrays"], direct["arrays"]);
}
