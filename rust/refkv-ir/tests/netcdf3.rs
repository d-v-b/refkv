//! The netCDF3 parser (src/netcdf3/; spec/zarrify/netcdf3.md): one test per rejected fixture, the
//! mirror's rebuild of each accepted fixture, its variables' descriptions keyed by the header, and
//! no panic on any cut or corrupted copy of one. The values are checked against netCDF4-python and
//! scipy by python/tests/ir/test_netcdf3.py.

mod common;

use common::{fixtures, rebuilt};
use refkv_ir::netcdf3::Netcdf3;
use refkv_ir::parse_bytes;

// one test per case of conformance/zarrify/rejects/netcdf3.json (build.rs), each the message its check gives
macro_rules! reject {
    ($name:ident, $input:literal, $msg:literal) => {
        common::reject!($name, Netcdf3, concat!("netcdf3/", $input), $msg);
    };
}
include!(concat!(env!("OUT_DIR"), "/rejects_netcdf3.rs"));

fn accepted() -> Vec<(String, Vec<u8>)> {
    let mut out: Vec<(String, Vec<u8>)> = std::fs::read_dir(fixtures().join("netcdf3"))
        .unwrap()
        .map(|e| e.unwrap().path())
        .filter(|p| p.extension().is_some_and(|x| x == "nc") && !p.to_string_lossy().contains("reject"))
        .map(|p| (p.file_name().unwrap().to_string_lossy().into_owned(), std::fs::read(&p).unwrap()))
        .collect();
    out.sort();
    out
}

/// Every accepted fixture parses, its IR rebuilds the file byte for byte, every variable has a
/// description keyed by the header at its position, and every record chunk lies in the file.
#[test]
fn accepted_fixtures_rebuild() {
    let all = accepted();
    assert!(all.len() >= 5, "{} fixtures", all.len());
    for (name, data) in all {
        let (ir, facts) = parse_bytes::<Netcdf3>(&data).unwrap_or_else(|e| panic!("{name}: {e}"));
        assert_eq!(rebuilt(&ir, &data), data, "{name}");
        let header = facts["declared_by"].as_u64().unwrap() as u32;
        assert_eq!(ir.path(header), "header", "{name}");
        let keys: Vec<(u32, Option<u64>)> = ir.stored.arrays.iter().map(|d| (d.element, d.index)).collect();
        for a in facts["arrays"].as_array().unwrap() {
            let k = a["index"].as_u64().unwrap();
            assert!(keys.contains(&(header, Some(k))), "{name}: {}", a["path"]);
            let s = &a["store"];
            if s["kind"] == "records" {
                let records = s["records"].as_u64().unwrap();
                if records > 0 {
                    let end = s["offset"].as_u64().unwrap() + (records - 1) * s["stride"].as_u64().unwrap() + s["length"].as_u64().unwrap();
                    assert!(end <= data.len() as u64, "{name}: {}", a["path"]);
                }
            }
        }
    }
}

/// Every prefix of the classic and the 64-bit data fixtures, and every copy with one byte of its
/// header changed, is accepted or rejected, never a panic: the wasm build must not abort.
#[test]
fn cut_and_corrupted_files_never_panic() {
    for name in ["netcdf3/netcdf3_classic.nc", "netcdf3/netcdf3_64bit_data.nc"] {
        let data = std::fs::read(fixtures().join(name)).unwrap();
        for n in 0..data.len() {
            let _ = parse_bytes::<Netcdf3>(&data[..n]);
        }
        let header = parse_bytes::<Netcdf3>(&data).unwrap().1["root"]["own"]["header"].as_u64().unwrap() as usize;
        for k in 0..header {
            for v in [0xFF, 0x00, data[k] ^ 0x40, data[k].wrapping_add(1)] {
                let mut d = data.clone();
                d[k] = v;
                let _ = parse_bytes::<Netcdf3>(&d);
            }
        }
    }
}
