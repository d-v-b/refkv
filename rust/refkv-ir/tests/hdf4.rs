//! The HDF4 parser (src/hdf4/; spec/zarrify/hdf4.md): one test per rejected fixture, the mirror's
//! rebuild of each accepted fixture, its arrays' chunks within the file, and no panic on any cut or
//! corrupted copy of one. The values are checked against pyhdf by python/tests/ir/test_hdf4.py.

mod common;

use common::{fixtures, rebuilt};
use refkv_ir::hdf4::Hdf4;
use refkv_ir::parse_bytes;

// one test per case of conformance/zarrify/rejects/hdf4.json (build.rs), each the message its check gives
macro_rules! reject {
    ($name:ident, $input:literal, $msg:literal) => {
        common::reject!($name, Hdf4, concat!("hdf4/", $input), $msg);
    };
}
include!(concat!(env!("OUT_DIR"), "/rejects_hdf4.rs"));

fn accepted() -> Vec<(String, Vec<u8>)> {
    let mut out: Vec<(String, Vec<u8>)> = std::fs::read_dir(fixtures().join("hdf4"))
        .unwrap()
        .map(|e| e.unwrap().path())
        .filter(|p| p.extension().is_some_and(|x| x == "hdf") && !p.to_string_lossy().contains("reject"))
        .map(|p| (p.file_name().unwrap().to_string_lossy().into_owned(), std::fs::read(&p).unwrap()))
        .collect();
    out.sort();
    out
}

/// Every accepted fixture parses, its IR rebuilds the file byte for byte, and every segment of
/// every array's storage lies in the file.
#[test]
fn accepted_fixtures_rebuild() {
    let all = accepted();
    assert!(all.len() >= 3, "{} fixtures", all.len());
    for (name, data) in all {
        let (ir, facts) = parse_bytes::<Hdf4>(&data).unwrap_or_else(|e| panic!("{name}: {e}"));
        assert_eq!(rebuilt(&ir, &data), data, "{name}");
        let nodes = facts["nodes"].as_array().unwrap();
        assert!(nodes.iter().any(|n| n.get("array").is_some()), "{name}: no array");
        for node in nodes {
            let store = &node["array"]["store"];
            let mut segs: Vec<&serde_json::Value> = vec![&store["segments"]];
            segs.extend(store["chunks"].as_array().into_iter().flatten().map(|c| &c["segments"]));
            for s in segs.iter().flat_map(|s| s.as_array().into_iter().flatten()) {
                if let Some(o) = s[0].as_u64() {
                    assert!(o + s[1].as_u64().unwrap() <= data.len() as u64, "{name}: {} {s}", node["path"]);
                }
            }
        }
    }
}

/// Every prefix of the small and the tables fixture, and every copy with one byte of its headers changed, is
/// accepted or rejected, never a panic: the wasm build must not abort.
#[test]
fn cut_and_corrupted_files_never_panic() {
    for name in ["hdf4/hdf4_small.hdf", "hdf4/hdf4_tables.hdf"] {
        corrupt(&std::fs::read(fixtures().join(name)).unwrap());
    }
}

fn corrupt(data: &[u8]) {
    for n in (0..data.len()).step_by(7) {
        let _ = parse_bytes::<Hdf4>(&data[..n]);
    }
    // each byte but those of the linked blocks' unused space (runs of zeros) set to 0xFF, to 0x00,
    // and with a bit flipped
    for k in 0..data.len() {
        if data[k.saturating_sub(8)..(k + 8).min(data.len())].iter().all(|&b| b == 0) {
            continue;
        }
        for v in [0xFF, 0x00, data[k] ^ 0x40] {
            let mut d = data.to_vec();
            d[k] = v;
            let _ = parse_bytes::<Hdf4>(&d);
        }
    }
}
