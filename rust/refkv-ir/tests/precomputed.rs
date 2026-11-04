//! The precomputed store format (src/precomputed.rs, src/project/precomputed.rs,
//! schema/precomputed.json): every accepted fixture store is a valid store IR whose mirror
//! rebuilds its `info` and listing, is canonical and shows the view it rebuilds, and whose chunk
//! references are each a position header and one object; one test per rejected fixture, one of
//! the scales that are not presented, and one of the shard arithmetic on BANC's numbers.

mod common;

use common::{drive, listing, walk};
use refkv_ir::check::check;
use refkv_ir::json;
use refkv_ir::mirror::{canonical_problem, rebuild_store, view_problem};
use refkv_ir::out::{Out, Part};
use refkv_ir::precomputed::{read_scale, shard_box};
use refkv_ir::store::{store_output, StoreFormat};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../fixtures/precomputed")
}

fn zarrify(files: &BTreeMap<String, Vec<u8>>) -> Result<(Out, refkv_ir::ir::Ir), String> {
    let keys = listing(files);
    let bytes: Vec<&Vec<u8>> = files.values().collect();
    let mut r = refkv_ir::store::StoreRun::new(StoreFormat::Precomputed, keys, 4)?;
    drive(&mut r, &bytes)?;
    let (ir, facts, stats) = r.finish()?;
    Ok((store_output(StoreFormat::Precomputed, &ir, &facts, stats, "https://example.org/v/", true)?, ir))
}

fn entry(out: &Out) -> impl Fn(&str) -> Option<Vec<u8>> + '_ {
    |k: &str| out.entries.iter().find(|e| e.0 == k).map(|e| e.1.clone())
}

fn no_source(_: u64, _: u64) -> Result<Vec<u8>, String> {
    Err("a store's mirror reads no source".into())
}

#[test]
fn accepted_fixtures_mirror_their_info_and_listing() {
    let mut accepted = 0;
    for p in std::fs::read_dir(fixtures()).unwrap().map(|e| e.unwrap().path()) {
        let name = p.file_name().unwrap().to_str().unwrap().to_string();
        if name.starts_with("precomputed_reject_") {
            continue;
        }
        let files = walk(&p);
        let (out, ir) = zarrify(&files).unwrap_or_else(|e| panic!("{name}: {e}"));
        check(&ir).unwrap_or_else(|e| panic!("{name}: {e}"));
        let back = rebuild_store(&entry(&out)).unwrap_or_else(|e| panic!("{name}: {e}"));
        assert_eq!(back.len(), files.len(), "{name}");
        let mut held = 0;
        for (k, n, b) in back {
            assert_eq!(n, files[&k].len() as u64, "{name}: {k}");
            if let Some(b) = b {
                assert_eq!(b, files[&k], "{name}: {k}");
                held += 1;
            }
        }
        assert_eq!(held, 1, "{name}: only info is held");
        assert_eq!(canonical_problem(&entry(&out), &mut no_source), None, "{name}");
        let keys: Vec<String> = out.entries.iter().map(|e| e.0.clone()).collect();
        assert_eq!(view_problem(&keys, &entry(&out), &mut no_source), None, "{name}");
        // every object is referenced once, or empty, or info
        let mut referenced = BTreeMap::new();
        for (k, parts) in &out.refs {
            let obj = parts.iter().find_map(|p| match p {
                Part::Obj(o, 0, n) => Some((o.clone(), *n)),
                _ => None,
            });
            let (o, n) = obj.unwrap_or_else(|| panic!("{name}: {k} references no object"));
            assert_eq!(n, files[&o].len() as u64, "{name}: {k}");
            match parts.as_slice() {
                [Part::Lit(h), Part::Obj(..)] => assert!(h.len() == 12 && k.contains("/c/"), "{name}: {k}"),
                [Part::Obj(..)] => assert!(k.starts_with("refkv_source/objects/"), "{name}: {k}"),
                _ => panic!("{name}: {k}: {parts:?}"),
            }
            assert!(referenced.insert(o.clone(), ()).is_none(), "{name}: {o} referenced twice");
        }
        for (k, b) in &files {
            assert_eq!(referenced.contains_key(k), !b.is_empty() && k != "info", "{name}: {k}");
        }
        accepted += 1;
    }
    assert_eq!(accepted, 19);
}

#[test]
fn scales_that_are_not_presented_are_recorded_and_their_objects_kept() {
    let files = walk(&fixtures().join("precomputed_not_presented"));
    let (out, _) = zarrify(&files).unwrap();
    let doc: serde_json::Value = serde_json::from_slice(&entry(&out)("zarr.json").unwrap()).unwrap();
    let reasons: Vec<(u64, String)> = doc["attributes"]["refkv_zarrified"]["precomputed"]["not_presented"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| (r["index"].as_u64().unwrap(), r["reason"].as_str().unwrap().to_string()))
        .collect();
    let want = [
        (1, "its encoding webp has no decoder"),
        (2, "its hash murmurhash3_x86_128 scatters chunks across shards, so no shard is a box of chunks"),
        (3, "its shards wrap: chunk ids have 2 bits, and preshift_bits, minishard_bits and shard_bits name 0 of them, so that one shard holds several boxes of chunks"),
        (4, "its key \"../up\" is not a path within the store"),
        (5, "its key \"good\" is an earlier scale's"),
        (6, "its size is not 3 positive integers"),
        (7, "the scale is not an object"),
        (8, "fpzip holds float32 or float64, not uint8"),
        (9, "kempressed holds float32 or float64, not uint8"),
        (10, "zfpc holds int32, int64, float32 or float64, not uint8"),
    ];
    assert_eq!(reasons, want.map(|(i, r)| (i, r.to_string())));
    let refs: BTreeMap<&str, &Vec<Part>> = out.refs.iter().map(|(k, p)| (k.as_str(), p)).collect();
    for k in ["webp/0-2_0-2_0-2", "fp/0-2_0-2_0-2", "kemp/0-2_0-2_0-2", "zfp/0-2_0-2_0-2", "murmur/0.shard", "wrap/0.shard", "broken/x"] {
        assert!(refs.contains_key(format!("refkv_source/objects/{k}").as_str()), "{k}");
    }
    // the other objects of the scale it presents
    for k in ["good/0-1_0-1_0-1", "good/1-3_0-2_0-2", "good/0-3_2-4_0-2", "good/00-2_0-2_0-2", "good/README", "good/0-2_0-2_0-2.gz"] {
        assert!(refs.contains_key(format!("refkv_source/objects/{k}").as_str()), "{k}");
    }
    assert!(refs.contains_key("0/c/0/0/0"), "the chunk the bare name answers for");
    assert!(!refs.contains_key("0/c/0/0/1"), "an empty chunk has no entry");
}

#[test]
fn the_encodings_of_one_channel_of_labels_are_not_presented_for_other_volumes() {
    let files = walk(&fixtures().join("precomputed_not_presented_labels"));
    let (out, _) = zarrify(&files).unwrap();
    let doc: serde_json::Value = serde_json::from_slice(&entry(&out)("zarr.json").unwrap()).unwrap();
    let reasons: Vec<&str> = doc["attributes"]["refkv_zarrified"]["precomputed"]["not_presented"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| r["reason"].as_str().unwrap())
        .collect();
    assert_eq!(reasons, [
        "compresso holds uint8 to uint64 in 1 channel, not int8 in 3",
        "crackle holds uint8 to uint64 in 1 channel, not int8 in 3",
        "jxl holds uint8 in 1 channel, not int8 in 3",
    ]);
}

#[test]
fn a_volume_with_no_scale_to_present_has_no_image_and_keeps_its_objects() {
    let mut files = BTreeMap::new();
    files.insert("info".to_string(), br#"{"type": "image", "data_type": "uint8", "num_channels": 1,
        "scales": [{"key": "a", "size": [1, 1, 1], "chunk_sizes": [[1, 1, 1]], "resolution": [1, 1, 1], "encoding": "webp"}]}"#.to_vec());
    files.insert("a/0-1_0-1_0-1".to_string(), b"x".to_vec());
    let (out, _) = zarrify(&files).unwrap();
    let doc: serde_json::Value = serde_json::from_slice(&entry(&out)("zarr.json").unwrap()).unwrap();
    assert!(doc["attributes"].get("ome").is_none());
    assert!(entry(&out)("0/zarr.json").is_none());
    assert_eq!(out.refs.len(), 1);
    assert_eq!(out.refs[0].0, "refkv_source/objects/a/0-1_0-1_0-1");
}

#[test]
fn a_shard_is_a_box_of_chunks_on_bancs_numbers() {
    // inspected: tensorstore gives this scale a write chunk of (2048, 2304, 256) voxels
    let info = br#"{"key": "512_512_45", "size": [2048, 2304, 7010], "chunk_sizes": [[128, 128, 16]], "resolution": [512, 512, 45],
        "encoding": "jpeg", "voxel_offset": [0, 0, 0], "sharding": {"@type": "neuroglancer_uint64_sharded_v1", "hash": "identity",
        "preshift_bits": 11, "minishard_bits": 2, "shard_bits": 5, "minishard_index_encoding": "gzip", "data_encoding": "raw"}}"#;
    let v = json::parse(info).unwrap().value;
    let s = read_scale(&v, "uint8", 1).unwrap();
    assert_eq!(s.grid(), [16, 18, 439]);
    let h = s.sharding.unwrap();
    assert_eq!(h.box_shape, [16, 32, 16]);
    // shard 2's chunk ids start at 16384 (inspected): z 16 chunks on, past the box of 16
    assert_eq!(shard_box(&h, 2), Some([0, 0, 2]));
    assert_eq!(shard_box(&h, 27), Some([0, 0, 27]));
    assert_eq!(shard_box(&h, 1 << 6), None);
}

fn rejected(store: &str) -> String {
    match zarrify(&walk(&fixtures().join(store))) {
        Ok(_) => panic!("{store} accepted"),
        Err(e) => e,
    }
}

macro_rules! reject {
    ($name:ident, $input:literal, $msg:expr) => {
        #[test]
        fn $name() {
            let e = rejected($input);
            assert!(e.contains($msg), "{}: {e}", stringify!($name));
        }
    };
}

// one test per case of conformance/zarrify/rejects/precomputed.json (build.rs)
include!(concat!(env!("OUT_DIR"), "/rejects_precomputed.rs"));

#[test]
fn a_store_without_info_is_rejected() {
    let mut files = BTreeMap::new();
    files.insert("x".to_string(), b"x".to_vec());
    let e = zarrify(&files).err().unwrap();
    assert!(e.contains("not a Neuroglancer precomputed store: the root has no info"), "{e}");
}
