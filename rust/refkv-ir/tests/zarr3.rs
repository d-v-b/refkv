//! The Zarr v3 store format (src/zarr3.rs, src/project/zarr3.rs; spec/zarrify/zarr3.md), in both
//! forms: one test over the fixture stores and zip files it accepts (their hierarchies' counts,
//! the documents it writes, the references, and the mirror rebuilding the listing and every
//! document read, or the zip file byte for byte), and one test per rejected fixture, each with
//! its reason.

mod common;

use common::{listing, walk};
use refkv_ir::check;
use refkv_ir::mirror::{canonical_problem, load, rebuild_store, table_from_out, view_problem};
use refkv_ir::out::{Out, Part};
use refkv_ir::project::zarr3::is_chunk;
use refkv_ir::run::{output, run_bytes, Format};
use refkv_ir::store::{run_store, store_output, StoreFormat};
use refkv_ir::zarr3::is_zipped;
use serde_json::{json, Value as J};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

const UUID: &str = "95203d0a-6586-426a-b920-3db2f4f1e97e";

fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../fixtures/zarr3")
}

/// The store `files`' output, mirrored.
fn directory(files: &BTreeMap<String, Vec<u8>>) -> Result<Out, String> {
    let keys = listing(files);
    let bytes: Vec<&Vec<u8>> = files.values().collect();
    let (ir, facts, stats) = run_store(StoreFormat::Zarr3, keys, &|k| bytes[k].clone())?;
    store_output(StoreFormat::Zarr3, &ir, &facts, stats, "https://data.test/s/", true, false).map_err(String::from)
}

/// The zip file's output, mirrored, and its IR.
fn zip(data: &[u8]) -> Result<(Out, refkv_ir::ir::Ir), String> {
    let (ir, facts, stats) = run_bytes(Format::Zarr3, data)?;
    Ok((output(Format::Zarr3, &ir, &facts, stats, "https://data.test/s.zip", true, &Default::default(), true)?, ir))
}

fn run(name: &str) -> Result<Out, String> {
    let path = fixtures().join(name);
    if path.is_dir() {
        directory(&walk(&path))
    } else {
        zip(&std::fs::read(&path).unwrap()).map(|(out, _)| out)
    }
}

fn doc(out: &Out, key: &str) -> J {
    let b = &out.entries.iter().find(|e| e.0 == key).unwrap_or_else(|| panic!("no {key}")).1;
    serde_json::from_slice(b).unwrap()
}

fn refs(out: &Out) -> BTreeMap<&str, &[Part]> {
    out.refs.iter().map(|(k, p)| (k.as_str(), p.as_slice())).collect()
}

/// The zip file's mirror rebuilds it byte for byte, and is canonical, with the view it shows.
fn check_zip(path: &Path, data: &[u8], out: &Out, ir: &refkv_ir::ir::Ir) {
    check::check(ir).unwrap_or_else(|m| panic!("{path:?}: {m}"));
    let get = |k: &str| out.entries.iter().find(|e| e.0 == k).map(|e| e.1.clone());
    let mut read = |o: u64, n: u64| -> Result<Vec<u8>, String> { Ok(data[o as usize..(o + n) as usize].to_vec()) };
    let back = load(&table_from_out(out).unwrap(), &mut read).unwrap();
    let mut rebuilt = Vec::new();
    for (o, n) in check::leaves(&back).unwrap() {
        rebuilt.extend_from_slice(&data[o as usize..(o + n) as usize]);
    }
    assert!(rebuilt == data, "{path:?} does not rebuild");
    assert_eq!(canonical_problem(&get, &mut read), None, "{path:?}");
    let keys: Vec<String> = out.entries.iter().map(|e| e.0.clone()).collect();
    assert_eq!(view_problem(&keys, &get, &mut read), None, "{path:?}");
}

#[test]
fn the_fixtures_have_their_hierarchies_and_mirrors() {
    let cases = [
        ("zarr3_hierarchy", json!({"arrays": 7, "groups": 4, "shardedArrays": 1, "chunks": 12, "emptyChunks": 1, "otherObjects": 5})),
        ("zarr3_hierarchy.zip", json!({"arrays": 7, "groups": 4, "chunks": 12, "emptyChunks": 1, "superseded": 2, "form": "zip"})),
        ("zarr3_scalar_root", json!({"arrays": 1, "groups": 0, "chunks": 1, "otherObjects": 1})),
        ("zarr3_codecs", json!({"arrays": 6, "groups": 1, "chunks": 6})),
        ("zarr3_rectilinear", json!({"arrays": 2, "chunks": 8, "otherObjects": 2})),
        ("zarr3_conventions", json!({"groups": 8})),
        ("zarr3_left_out", json!({"arrays": 1, "groups": 1, "chunks": 1, "notPresented": 15})),
        ("zarr3_deflated_documents.zip", json!({"arrays": 1, "groups": 1, "chunks": 2})),
        ("zarr3_compressed_chunks.zip", json!({"arrays": 1, "chunks": 2, "notPresented": 1})),
    ];
    for (name, want) in &cases {
        let out = run(name).unwrap_or_else(|e| panic!("{name}: {e}"));
        for (k, v) in want.as_object().unwrap() {
            assert_eq!(&out.summary[k], v, "{name} {k}: {}", out.summary);
        }
    }
    // every fixture accepted is one of these, and rebuilds
    let mut accepted = 0;
    for e in std::fs::read_dir(fixtures()).unwrap() {
        let p = e.unwrap().path();
        let name = p.file_name().unwrap().to_str().unwrap().to_string();
        if name.contains("reject") {
            continue;
        }
        accepted += 1;
        assert!(cases.iter().any(|c| c.0 == name), "{name} is not tested");
        if p.is_dir() {
            let files = walk(&p);
            let out = directory(&files).unwrap();
            let get = |k: &str| out.entries.iter().find(|e| e.0 == k).map(|e| e.1.clone());
            let rebuilt = rebuild_store(&get).unwrap();
            assert_eq!(rebuilt.len(), files.len(), "{name}");
            for (k, n, b) in rebuilt {
                assert_eq!(n, files[&k].len() as u64, "{name} {k}");
                if let Some(b) = b {
                    assert!(b == files[&k], "{name} {k}");
                }
            }
        } else {
            let data = std::fs::read(&p).unwrap();
            assert!(is_zipped(&data[..data.len().min(552)]), "{name}");
            let (out, ir) = zip(&data).unwrap();
            check_zip(&p, &data, &out, &ir);
        }
    }
    assert_eq!(accepted, cases.len());
}

#[test]
fn documents_pass_through_with_the_declaration() {
    let h = run("zarr3_hierarchy").unwrap();
    let files = walk(&fixtures().join("zarr3_hierarchy"));
    let source = |k: &str| -> J { serde_json::from_slice(&files[k]).unwrap() };
    // the root: its attributes in place, then the declaration; consolidated metadata dropped
    let root = doc(&h, "zarr.json");
    assert!(source("zarr.json").get("consolidated_metadata").is_some());
    assert_eq!(root.get("consolidated_metadata"), None);
    let a = &root["attributes"];
    assert_eq!((&a["title"], a["n"].to_string()), (&json!("a hierarchy"), "18446744073709551615".to_string()));
    assert_eq!(a["zarr_conventions"][0]["uuid"], UUID);
    assert_eq!(a["refkv_zarrified"], json!({"profile": "zarr3", "version": 0, "source": {"url": "https://data.test/s/"}}));
    // an array without source metadata: its zarr.json, unchanged
    for key in ["a/zarr.json", "sharded/zarr.json", "g/v2/zarr.json", "scalar/zarr.json"] {
        assert_eq!(doc(&h, key), source(key), "{key}");
    }
    // the implicit group deep/, and the node inside an array, kept as an object
    assert_eq!(doc(&h, "deep/zarr.json"), json!({"zarr_format": 3, "node_type": "group", "attributes": {}}));
    let r = refs(&h);
    assert!(!h.entries.iter().any(|e| e.0 == "a/inner/zarr.json"));
    for (key, at) in [("a/inner/zarr.json", "refkv_source/objects/a/inner/zarr.json~"), ("g/.zgroup", "refkv_source/objects/g/.zgroup~"),
                      ("g/zarr.json~", "refkv_source/objects/g/zarr.json~~"), ("a/c/9/9", "refkv_source/objects/a/c/9/9")] {
        assert!(matches!(r[at], [Part::Obj(k, 0, _)] if k == key), "{at}");
    }
    // chunks at their keys, each its object whole; a shard whole
    for key in ["a/c/0/0", "a/c/1/0", "g/dot/c.1", "g/v2/1.0", "g/v2_slash/0/1", "scalar/c", "sharded/c/1/0"] {
        assert!(matches!(r[key], [Part::Obj(k, 0, n)] if k == key && *n == files[key].len() as u64), "{key}");
    }
    assert!(!r.contains_key("a/c/1/1") && !r.contains_key("g/v2_slash/1/1"));
    // the zip form: the same documents, a chunk its entry's stored bytes
    let data = std::fs::read(fixtures().join("zarr3_hierarchy.zip")).unwrap();
    let (z, _) = zip(&data).unwrap();
    assert_eq!(doc(&z, "a/zarr.json"), source("a/zarr.json"));
    assert_eq!(doc(&z, "zarr.json")["attributes"]["title"], "a hierarchy"); // the last entry of the name
    let [Part::Src(at, n)] = refs(&z)["sharded/c/1/0"] else { panic!() };
    assert_eq!(&data[*at as usize..(at + n) as usize], &files["sharded/c/1/0"][..]);
    assert!(!refs(&z).keys().any(|k| k.starts_with("refkv_source/")));
    // the rectilinear grid's chunks, and not the one beyond it
    let rect = refs(&run("zarr3_rectilinear").unwrap()).keys().map(|k| k.to_string()).collect::<Vec<_>>();
    assert!(rect.contains(&"r/c/2/1".into()) && rect.contains(&"refkv_source/objects/r/c/3/0".into()), "{rect:?}");
    // a list of more sizes than the axis needs: the chunks that cover it, and no more
    assert!(rect.contains(&"over/c/6".into()) && rect.contains(&"refkv_source/objects/over/c/7".into()), "{rect:?}");
}

#[test]
fn attributes_stay_in_place_but_the_convention_s_own() {
    let c = run("zarr3_conventions").unwrap();
    let attrs = |p: &str| doc(&c, &if p.is_empty() { "zarr.json".into() } else { format!("{p}/zarr.json") })["attributes"].clone();
    let own = |p: &str| attrs(p)["refkv_zarrified"]["zarr3"].clone();
    let proj = json!({"uuid": "f17cb550-5864-4468-aeb7-f3180cfb622f", "name": "proj:", "description": "a GeoZarr convention"});
    // other conventions' CMOs follow the convention's own on a node that declares it
    let root = attrs("");
    assert_eq!((root["zarr_conventions"][0]["uuid"].as_str(), &root["zarr_conventions"][1]), (Some(UUID), &proj));
    assert_eq!((&root["proj:code"], &root["ome"]), (&json!("EPSG:4326"), &json!({"version": "0.5"})));
    // a node with no source metadata keeps its attributes as they are
    assert_eq!(attrs("kept"), json!({"zarr_conventions": [proj], "proj:code": "EPSG:32633"}));
    assert_eq!(attrs("bare"), json!({}));
    // moved: refkv_zarrified, and a zarr_conventions that is not a nonempty array of other CMOs
    assert_eq!(attrs("nested")["keep"], 1);
    assert_eq!(own("nested"), json!({"attributes": {"refkv_zarrified": {"profile": "tiff"}}}));
    assert_eq!(own("object"), json!({"attributes": {"zarr_conventions": {"not": "a list"}}}));
    assert_eq!(own("empty"), json!({"attributes": {"zarr_conventions": []}}));
    assert_eq!(own("ours")["attributes"]["zarr_conventions"][0]["uuid"], UUID);
    // a refkv hierarchy's own convention moves with its member, so none is declared without it
    assert_eq!(own("again")["attributes"]["zarr_conventions"][1], proj);
    assert_eq!(own("again")["attributes"]["refkv_zarrified"], json!({"profile": "tiff"}));
    for p in ["nested", "object", "empty", "ours", "again"] {
        assert_eq!(attrs(p)["zarr_conventions"].as_array().map(Vec::len), Some(1), "{p}");
    }
}

#[test]
fn nodes_that_fail_below_the_root_are_left_out_with_their_reasons() {
    let out = run("zarr3_left_out").unwrap();
    let left: BTreeMap<String, String> = doc(&out, "zarr.json")["attributes"]["refkv_zarrified"]["zarr3"]["not_presented"]
        .as_array().unwrap().iter().map(|r| (r["path"].as_str().unwrap().into(), r["reason"].as_str().unwrap().into())).collect();
    for (path, why) in [
        ("grid", "grid: chunk grid irregular is not supported"),
        ("grid_dims", "grid_dims: chunk grid regular is not one of 2 dimensions"),
        ("encoding", "encoding: chunk key encoding hashed is not supported"),
        ("separator", "separator: separator - is not . or /"),
        ("separator_null", "separator_null: separator null is not . or /"),
        ("short_grid", "short_grid: chunk grid rectilinear is not one of 1 dimensions"),
        ("transformers", "transformers: storage_transformers is not empty"),
        ("no_codecs", "no_codecs: codecs is not a nonempty array"),
        ("no_fill", "no_fill: zarr.json has no fill_value"),
        ("shape", "shape: shape [-1] is not up to 32 sizes"),
        ("format", "format/zarr.json: zarr_format is not 3"),
        ("node_type", "node_type/zarr.json: node_type folder is not array or group"),
        ("attributes", "attributes/zarr.json: attributes is not an object"),
        ("empty_doc", "empty_doc/zarr.json: invalid JSON: an empty document"),
        ("not_json", "not_json/zarr.json: invalid JSON"),
    ] {
        assert!(left[path].starts_with(why), "{path}: {}", left[path]);
    }
    assert_eq!(left.len(), 15);
    // a node left out has no zarr.json, a candidate below it is not a node, and its objects are kept
    assert!(!out.entries.iter().any(|e| e.0 == "grid/zarr.json" || e.0 == "grid/inner/zarr.json"));
    let r = refs(&out);
    assert!(r.contains_key("refkv_source/objects/grid/c/0") && r.contains_key("refkv_source/objects/grid/inner/zarr.json~"));
    assert!(r.contains_key("ok/c/0"));
    // in the zip form, an array whose chunks are deflated
    let z = run("zarr3_compressed_chunks.zip").unwrap();
    assert_eq!(doc(&z, "zarr.json")["attributes"]["refkv_zarrified"]["zarr3"]["not_presented"],
               json!([{"path": "z", "reason": "z: chunk z/c/0 is compressed in the zip file (method 8)"}]));
    // the ignored entries, beside the zip form's mirror
    let d = run("zarr3_deflated_documents.zip").unwrap();
    assert_eq!(doc(&d, "refkv_source/zarr.json")["attributes"]["refkv_zarrified"]["zarr3"]["ignored"], json!(["b/./c", "notes/"]));
}

#[test]
fn chunk_keys_follow_the_encoding_and_the_grid() {
    for (rest, v2, sep, counts, chunk) in [
        ("c", false, "/", &[][..], true), ("0", false, "/", &[][..], false), ("0", true, ".", &[][..], true),
        ("c/0/1", false, "/", &[2, 2][..], true), ("c.0.1", false, ".", &[2, 2][..], true), ("c/0.1", false, ".", &[2, 2][..], false),
        ("c/2/0", false, "/", &[2, 2][..], false), ("0/1", false, "/", &[2, 2][..], false), ("c/01/0", false, "/", &[20, 2][..], false),
        ("1.0", true, ".", &[2, 2][..], true), ("1/0", true, "/", &[2, 2][..], true), ("c/0", false, "/", &[0][..], false),
    ] {
        assert_eq!(is_chunk(rest, v2, sep, counts), chunk, "{rest} {v2} {sep} {counts:?}");
    }
    // the signature: a zip file whose first entry is not under a .SAFE directory
    let head = |name: &[u8]| -> Vec<u8> {
        let mut h = b"PK\x03\x04".to_vec();
        h.extend([0u8; 22]);
        h.extend((name.len() as u16).to_le_bytes());
        h.extend([0u8; 2]);
        h.extend(name);
        h
    };
    for (name, zipped) in [(&b"zarr.json"[..], true), (b"a/c/0", true), (b"S2A.SAFE/manifest.safe", false), (b"S2A.SAFE/", false), (b"x.SAFE", true)] {
        assert_eq!(is_zipped(&head(name)), zipped, "{:?}", std::str::from_utf8(name));
    }
}

macro_rules! reject {
    ($name:ident, $input:expr, $why:expr) => {
        #[test]
        fn $name() {
            let e = run($input).err().unwrap_or_else(|| panic!("{} accepted", $input));
            assert!(e.contains($why), "{}: {e}", $input);
        }
    };
}

// one test per case of conformance/zarrify/rejects/zarr3.json (build.rs)
include!(concat!(env!("OUT_DIR"), "/rejects_zarr3.rs"));
