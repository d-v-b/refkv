//! zarrs' typed Zarr v3 metadata (the `zarrs` feature, `cargo test --features zarrs`): every array
//! document a file profile's projection writes for its accepted fixtures parses as zarrs'
//! `ArrayMetadataV3`, so that a Rust reader built on zarrs takes refkv's outputs.
#![cfg(feature = "zarrs")]

mod common;

use common::fixtures;
use refkv_ir::run::{run_bytes, Format};
use zarrs_metadata::v3::ArrayMetadataV3;

#[test]
fn every_array_document_parses_as_zarrs_metadata() {
    let mut arrays = 0;
    for e in refkv_ir::out::registry().iter().filter(|e| e["kind"] == "file") {
        let name = e["name"].as_str().unwrap();
        let f = Format::from_name(name).unwrap();
        if f.store_run() {
            continue;
        }
        let suffixes: Vec<&str> = e["suffixes"].as_array().unwrap().iter().map(|s| s.as_str().unwrap()).collect();
        let Ok(dir) = std::fs::read_dir(fixtures().join(name)) else { continue };
        let mut paths: Vec<_> = dir.map(|d| d.unwrap().path())
            .filter(|p| suffixes.iter().any(|s| p.to_string_lossy().ends_with(s))).collect();
        paths.sort();
        for path in paths {
            let data = std::fs::read(&path).unwrap();
            let Ok((ir, facts, _)) = run_bytes(f, &data) else { continue };
            let Ok(mut out) = refkv_ir::project::project(f, &ir, &facts, "u", &Default::default()) else { continue };
            for (key, doc) in out.take_entries("") {
                if !key.ends_with("zarr.json") {
                    continue;
                }
                let v: serde_json::Value = serde_json::from_slice(&doc).unwrap();
                if v["node_type"] == "array" {
                    serde_json::from_value::<ArrayMetadataV3>(v).unwrap_or_else(|e| panic!("{path:?} {key}: {e}"));
                    arrays += 1;
                }
            }
        }
    }
    assert!(arrays > 0);
}
