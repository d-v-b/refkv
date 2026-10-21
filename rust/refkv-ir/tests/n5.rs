//! The N5 store format (src/n5.rs, src/project/n5.rs, schema/n5.json; its documents read by
//! src/json.rs, which tests/zarr2.rs and tests/json.rs test): every accepted fixture store is a
//! valid store IR whose mirror rebuilds its documents and listing, is canonical and shows the
//! view it rebuilds; one test per rejected fixture (each the role or rule that rejects it), and
//! one per other way a store is rejected.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use refkv_ir::check::check;
use refkv_ir::mirror::{canonical_problem, rebuild_store, view_problem};
use refkv_ir::out::Out;
use refkv_ir::store::{store_output, StoreFormat};

fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../fixtures/n5")
}

/// A directory's regular files, by key.
fn walk(root: &Path) -> BTreeMap<String, Vec<u8>> {
    let mut out = BTreeMap::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(d) = stack.pop() {
        for e in std::fs::read_dir(&d).unwrap() {
            let e = e.unwrap();
            if e.file_type().unwrap().is_dir() {
                stack.push(e.path());
            } else {
                let k = e.path().strip_prefix(root).unwrap().to_str().unwrap().replace('\\', "/");
                out.insert(k, std::fs::read(e.path()).unwrap());
            }
        }
    }
    out
}

/// The output of the N5 format over a store in memory, with the recorded ignored keys.
fn virtualize(files: &BTreeMap<String, Vec<u8>>, ignored: &[&str]) -> Result<(Out, refkv_ir::ir::Ir), String> {
    let keys: Vec<(String, u64)> = files.iter().map(|(k, v)| (k.clone(), v.len() as u64)).collect();
    let bytes: Vec<&Vec<u8>> = files.values().collect();
    let mut r = refkv_ir::store::StoreRun::new(StoreFormat::N5, keys, 4)?;
    r.ignore(ignored.iter().map(|s| s.to_string()).collect(), Vec::new())?;
    while let Some(reqs) = r.poll()? {
        for (k, q) in reqs.iter().enumerate() {
            let b = bytes[q.object];
            r.complete(k, q.spans.iter().map(|&(a, e)| b[a as usize..e as usize].to_vec()).collect())?;
        }
    }
    let (ir, facts, stats) = r.finish()?;
    Ok((store_output(StoreFormat::N5, &ir, &facts, stats, "https://example.org/c.n5/", true)?, ir))
}

fn entry(out: &Out) -> impl Fn(&str) -> Option<Vec<u8>> + '_ {
    |k: &str| out.entries.iter().find(|e| e.0 == k).map(|e| e.1.clone())
}

fn no_source(_: u64, _: u64) -> Result<Vec<u8>, String> {
    Err("a store's mirror reads no source".into())
}

fn doc(out: &Out, key: &str) -> serde_json::Value {
    serde_json::from_slice(&entry(out)(key).unwrap_or_else(|| panic!("no {key}"))).unwrap()
}

#[test]
fn accepted_fixtures_mirror_their_documents_and_listing() {
    let mut accepted = 0;
    for p in std::fs::read_dir(fixtures()).unwrap().map(|e| e.unwrap().path()) {
        let name = p.file_name().unwrap().to_str().unwrap().to_string();
        if name.starts_with("n5_reject_") {
            continue;
        }
        let files = walk(&p);
        let (out, ir) = virtualize(&files, &[]).unwrap_or_else(|e| panic!("{name}: {e}"));
        check(&ir).unwrap_or_else(|e| panic!("{name}: {e}"));
        // the rebuild: the listing, and every node's document byte for byte
        let back = rebuild_store(&entry(&out)).unwrap_or_else(|e| panic!("{name}: {e}"));
        assert_eq!(back.len(), files.len(), "{name}");
        let mut held = 0;
        for (k, n, b) in back {
            assert_eq!(n, files[&k].len() as u64, "{name}: {k}");
            if let Some(b) = b {
                assert!(k.ends_with("attributes.json"), "{name}: {k} held");
                assert_eq!(b, files[&k], "{name}: {k}");
                held += 1;
            }
        }
        assert!(held >= 1, "{name}");
        assert_eq!(canonical_problem(&entry(&out), &mut no_source), None, "{name}");
        let keys: Vec<String> = out.entries.iter().map(|e| e.0.clone()).collect();
        assert_eq!(view_problem(&keys, &entry(&out), &mut no_source), None, "{name}");
        // refkv_source is the mirror; the root declares revision 29
        let root = doc(&out, "zarr.json");
        assert_eq!(root["attributes"]["refkv_virtualized"]["revision"], 29, "{name}");
        let own = &doc(&out, "refkv_source/zarr.json")["attributes"]["refkv_virtualized"]["n5"];
        assert!(own["ir"]["objects"].as_u64() == Some(files.len() as u64), "{name}");
        // chunks at their keys, other objects under refkv_source/objects/, each whole
        for (k, parts) in &out.refs {
            let [refkv_ir::out::Part::Obj(o, 0, n)] = parts.as_slice() else { panic!("{name}: {k}") };
            assert_eq!(*n, files[o].len() as u64);
            assert!(k == o || k.starts_with("refkv_source/objects/"), "{name}: {k}");
        }
        accepted += 1;
    }
    assert_eq!(accepted, 17);
}

#[test]
fn the_view_shows_each_member_and_ignored_keys_are_recorded() {
    let files = walk(&fixtures().join("n5_cosem"));
    let (out, _) = virtualize(&files, &["x/", "a//b"]).unwrap();
    let own = &doc(&out, "refkv_source/zarr.json")["attributes"]["refkv_virtualized"]["n5"];
    assert_eq!(own["ignored"], serde_json::json!(["a//b", "x/"]));
    assert_eq!(canonical_problem(&entry(&out), &mut no_source), None);
    assert_eq!(rebuild_store(&entry(&out)).unwrap().len(), files.len());
    let tree = doc(&out, "refkv_source/tree/zarr.json");
    let view = &tree["attributes"]["refkv_virtualized"]["n5"];
    let s0 = &view["em"]["fibsem-uint8"]["s0"]["attributes.json"];
    assert_eq!(s0["dataType"], "\"uint8\"");
    assert!(s0["dimensions"].as_str().unwrap().starts_with('['));
}

fn rejected(store: &str) -> String {
    let files = walk(&fixtures().join(store));
    match virtualize(&files, &[]) {
        Ok(_) => panic!("{store} accepted"),
        Err(e) => e,
    }
}

macro_rules! reject {
    ($name:ident, $msg:expr) => {
        #[test]
        fn $name() {
            let e = rejected(stringify!($name));
            assert!(e.contains($msg), "{}: {e}", stringify!($name));
        }
    };
}

reject!(n5_reject_attributes_not_object, "attributes.json is not a JSON object");
reject!(n5_reject_blocksize_huge, "blockSize [2, 2147483648] is not 2 block sizes");
reject!(n5_reject_blocksize_mismatch, "blockSize [2] is not 2 block sizes");
reject!(n5_reject_blocksize_zero, "blockSize [2, 0] is not 2 block sizes");
reject!(n5_reject_blosc_clevel, "blosc clevel 10 is not an integer from 0 to 9");
reject!(n5_reject_blosc_cname, "blosc cname lz5 is not supported");
reject!(n5_reject_blosc_shuffle_missing, "blosc shuffle none is not supported");
reject!(n5_reject_compression_bzip2, "N5 compression bzip2 is not supported");
reject!(n5_reject_compression_jpeg, "N5 compression jpeg is not supported");
reject!(n5_reject_compression_lz4, "N5 compression lz4 is not supported");
reject!(n5_reject_compression_no_type, "compression is not an object with a string type");
reject!(n5_reject_compression_xz, "N5 compression xz is not supported");
reject!(n5_reject_datatype_object, "dataType object is not supported");
reject!(n5_reject_datatype_string, "dataType string is not supported");
reject!(n5_reject_dimensions_empty, "dimensions [] is not 1 to 32 sizes");
reject!(n5_reject_dimensions_fraction, "dimensions [4, 2.5] is not 1 to 32 sizes");
reject!(n5_reject_dimensions_huge, "dimensions [9007199254740992, 4] is not 1 to 32 sizes");
reject!(n5_reject_dimensions_negative, "dimensions [4, -1] is not 1 to 32 sizes");
reject!(n5_reject_gzip_level, "gzip level 10 is not an integer from -1 to 9");
reject!(n5_reject_gzip_usezlib_string, "gzip useZlib true is not a boolean");
reject!(n5_reject_json_bom, "JSON document starts with a byte order mark");
reject!(n5_reject_json_empty_object, "invalid JSON: the document is empty");
reject!(n5_reject_json_nan, "JSON literal NaN is not allowed");
reject!(n5_reject_json_not_utf8, "JSON document is not UTF-8");
reject!(n5_reject_json_overflow, "JSON number 1e400 is not finite in binary64");
reject!(n5_reject_json_too_deep, "JSON nests more than 256 deep");
reject!(n5_reject_json_trailing_comma, "invalid JSON: expecting a property name");
reject!(n5_reject_no_compression, "no compression");
reject!(n5_reject_no_root_attributes, "the root has no attributes.json");
reject!(n5_reject_objects_collision, "the node refkv_source is where the store's other objects go");
reject!(n5_reject_reserved_key, "output key \"__vz__/zarr.json\" is in the reserved __vz__/ space");
reject!(n5_reject_zstd_level, "zstd level 2.5 is not an integer from -131072 to 22");

#[test]
fn a_document_over_16_mib_is_refused_before_it_is_read() {
    let mut files = walk(&fixtures().join("n5_hierarchy"));
    files.insert("big/attributes.json".into(), vec![b' '; (1 << 24) + 1]);
    let e = virtualize(&files, &[]).err().unwrap();
    assert!(e.contains("big/attributes.json: JSON document of 16777217 bytes exceeds 16777216"), "{e}");
}

#[test]
fn a_scale_that_is_not_finite_is_refused() {
    let mut files = walk(&fixtures().join("n5_viewer_downsampling"));
    files.insert("g/attributes.json".into(), br#"{"pixelResolution": [1e300, 1e300], "scales": [[1, 1], [1e300, 2]]}"#.to_vec());
    let e = virtualize(&files, &[]).err().unwrap();
    assert!(e.contains("g: a scale is not finite"), "{e}");
}

#[test]
fn a_candidate_inside_a_dataset_is_never_read() {
    let mut files = walk(&fixtures().join("n5_root_dataset"));
    files.insert("x/attributes.json".into(), b"not JSON".to_vec());
    let (out, _) = virtualize(&files, &[]).unwrap();
    assert!(out.refs.iter().any(|r| r.0 == "refkv_source/objects/x/attributes.json"));
}

/// A root dataset whose compression is `c`.
fn compressed(c: &str) -> String {
    let doc = format!(r#"{{"dimensions": [4], "blockSize": [2], "dataType": "uint16", "compression": {c}}}"#);
    let files = BTreeMap::from([("attributes.json".to_string(), doc.into_bytes())]);
    virtualize(&files, &[]).err().unwrap_or_default()
}

#[test]
fn a_level_that_is_a_list_is_refused() {
    assert!(compressed(r#"{"type": "zstd", "level": [5]}"#).contains("zstd level [5]"));
    assert!(compressed(r#"{"type": "gzip", "level": [1]}"#).contains("gzip level [1]"));
}

#[test]
fn a_blosc_clevel_that_is_a_list_is_refused() {
    assert!(compressed(r#"{"type": "blosc", "cname": "lz4", "clevel": [], "shuffle": 1}"#).contains("blosc clevel"));
}

#[test]
fn a_blosc_shuffle_that_is_a_list_is_refused() {
    assert!(compressed(r#"{"type": "blosc", "cname": "lz4", "clevel": 5, "shuffle": [2]}"#).contains("blosc shuffle"));
}

#[test]
fn a_blosc_blocksize_that_is_a_list_is_refused() {
    let e = compressed(r#"{"type": "blosc", "cname": "lz4", "clevel": 5, "shuffle": 1, "blocksize": [0]}"#);
    assert!(e.contains("blosc blocksize"), "{e}");
}

#[test]
fn a_node_at_refkv_source_is_refused_even_with_nothing_else_to_keep() {
    // the mirror is always refkv_source (spec/virtualize/n5.md §6
    // <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/n5.md#6-other-objects>)
    let files = BTreeMap::from([
        ("attributes.json".to_string(), b"{}".to_vec()),
        ("refkv_source/attributes.json".to_string(), b"{}".to_vec()),
    ]);
    let e = virtualize(&files, &[]).err().unwrap();
    assert!(e.contains("the node refkv_source is where"), "{e}");
}

#[test]
fn ignored_keys_under_a_root_dataset_are_recorded_beside_its_mirror() {
    let files = walk(&fixtures().join("n5_root_dataset"));
    let (out, _) = virtualize(&files, &["b/", "a//c"]).unwrap();
    let own = &doc(&out, "refkv_source/zarr.json")["attributes"]["refkv_virtualized"]["n5"];
    assert_eq!(own["ignored"], serde_json::json!(["a//c", "b/"]));
    assert!(own["ir"].is_object());
    assert_eq!(canonical_problem(&entry(&out), &mut no_source), None);
    assert_eq!(rebuild_store(&entry(&out)).unwrap().len(), files.len());
    assert!(entry(&out)("refkv_source/empty.json").is_none());
}

#[test]
fn a_document_of_more_than_4096_members_is_one_value() {
    // the root document's object is element 1 (the root is 0); a member named json is renamed
    for members in [4096usize, 4097] {
        let doc = format!("{{\"json\":0,{}}}", (1..members).map(|k| format!("\"m{k}\":{k}")).collect::<Vec<_>>().join(","));
        let n = doc.len() as u64;
        let files = BTreeMap::from([("attributes.json".to_string(), doc.into_bytes())]);
        let (out, ir) = virtualize(&files, &[]).unwrap();
        let kids: Vec<u32> = (0..ir.len() as u32).filter(|&i| ir.parent[i as usize] == 1).collect();
        let named = |i: u32| (ir.names.get(ir.name[i as usize]).to_string(), ir.nidx[i as usize]);
        if members > 4096 {
            assert_eq!(kids.len(), 1);
            assert_eq!((named(kids[0]).0.as_str(), ir.start[kids[0] as usize], ir.len[kids[0] as usize]), ("json", 0, n));
        } else {
            let values = kids.iter().filter(|&&i| ir.kind[i as usize] == refkv_ir::ir::VALUE).count();
            assert_eq!(values, 4096);
            assert!(kids.iter().any(|&i| named(i) == ("json~".to_string(), 0)));
            assert!(!kids.iter().any(|&i| named(i).0 == "json"));
        }
        assert_eq!(canonical_problem(&entry(&out), &mut no_source), None);
        assert_eq!(out.summary["objects"], 1);
    }
}
