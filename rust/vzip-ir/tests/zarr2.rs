//! The Zarr v2 store format (src/zarr2.rs, src/project/zarr2.rs; spec/virtualize/zarr2.md): one
//! test over the fixture stores it accepts (their hierarchies' counts, and the mirror rebuilding
//! every document read), one of the JSON documents it reads, and one test per rejected fixture
//! store, each with the reason (the schema's message where a role rejects it).

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use vzip_ir::json;
use vzip_ir::mirror::rebuild_store;
use vzip_ir::out::{Out, Part};
use vzip_ir::project::zarr2::{is_chunk, object_key};
use vzip_ir::store::{run_store, store_output, StoreFormat, StoreRun};

fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../fixtures/zarr2")
}

fn walk(root: &Path) -> BTreeMap<String, Vec<u8>> {
    let mut out = BTreeMap::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(d) = stack.pop() {
        for e in std::fs::read_dir(&d).unwrap() {
            let e = e.unwrap();
            let t = e.file_type().unwrap();
            if t.is_dir() {
                stack.push(e.path());
            } else if t.is_file() {
                let k = e.path().strip_prefix(root).unwrap().to_str().unwrap().replace('\\', "/");
                out.insert(k, std::fs::read(e.path()).unwrap());
            }
        }
    }
    out
}

/// The output of the store `files`, mirrored.
fn output(files: &BTreeMap<String, Vec<u8>>) -> Result<Out, String> {
    let keys: Vec<(String, u64)> = files.iter().map(|(k, v)| (k.clone(), v.len() as u64)).collect();
    let bytes: Vec<&Vec<u8>> = files.values().collect();
    let (ir, facts, stats) = run_store(StoreFormat::Zarr2, keys, &|k| bytes[k].clone())?;
    store_output(StoreFormat::Zarr2, &ir, &facts, stats, "https://data.test/s/", true)
}

fn run(name: &str) -> Result<Out, String> {
    output(&walk(&fixtures().join(name)))
}

fn doc(out: &Out, key: &str) -> serde_json::Value {
    let b = &out.entries.iter().find(|e| e.0 == key).unwrap_or_else(|| panic!("no {key}")).1;
    serde_json::from_slice(b).unwrap()
}

#[test]
fn the_fixture_stores_have_their_hierarchies_and_mirrors() {
    let cases: [(&str, serde_json::Value); 8] = [
        ("zarr2_compressors", serde_json::json!({"arrays": 10, "groups": 1, "chunks": 12})),
        ("zarr2_dtypes", serde_json::json!({"arrays": 14})),
        ("zarr2_fill_values", serde_json::json!({"emptyChunks": 1, "chunks": 10})),
        ("zarr2_scalar_root", serde_json::json!({"arrays": 1, "groups": 0, "chunks": 1})),
        ("zarr2_hierarchy", serde_json::json!({"arrays": 5, "groups": 6, "chunks": 5})),
        ("zarr2_ome_attrs", serde_json::json!({"groups": 8})),
        ("zarr2_source_metadata", serde_json::json!({"arrays": 10, "otherObjects": 0})),
        ("zarr2_objects", serde_json::json!({"chunks": 3, "otherObjects": 6})),
    ];
    for (name, want) in cases {
        let out = run(name).unwrap_or_else(|e| panic!("{name}: {e}"));
        for (k, v) in want.as_object().unwrap() {
            assert_eq!(&out.summary[k], v, "{name} {k}: {}", out.summary);
        }
    }
    let c = run("zarr2_compressors").unwrap();
    assert_eq!(doc(&c, "zlib_f/zarr.json")["codecs"], serde_json::json!([
        {"name": "transpose", "configuration": {"order": [2, 1, 0]}},
        {"name": "bytes", "configuration": {"endian": "little"}},
        {"name": "zlib", "configuration": {"level": 1}},
    ]));
    assert_eq!(doc(&c, "blosc_autoshuffle_i2/zarr.json")["codecs"][1],
               serde_json::json!({"name": "blosc", "configuration": {"cname": "lz4hc", "clevel": 2, "shuffle": "shuffle", "typesize": 2, "blocksize": 0}}));
    let f = run("zarr2_fill_values").unwrap();
    let fills: Vec<_> = ["nan", "inf", "neg_inf", "null_int", "null_bool"].iter().map(|p| doc(&f, &format!("{p}/zarr.json"))["fill_value"].clone()).collect();
    assert_eq!(fills, serde_json::json!(["NaN", "Infinity", "-Infinity", 0, false]).as_array().unwrap().clone());
    // chunks at their keys, other objects under vzip_source/objects/ (escaped), each one object whole
    let h = run("zarr2_hierarchy").unwrap();
    let refs: Vec<&str> = h.refs.iter().map(|r| r.0.as_str()).collect();
    assert!(refs.contains(&"a/c/0/0") && refs.contains(&"vzip_source/objects/.zmetadata"), "{refs:?}");
    for (k, parts) in &h.refs {
        let [Part::Obj(key, 0, _)] = parts.as_slice() else { panic!("{k}: {parts:?}") };
        assert!(k == key || k.starts_with("vzip_source/objects/"), "{k} {key}");
    }
    // every accepted store's mirror rebuilds its listing and the documents read
    let mut accepted = 0;
    for e in std::fs::read_dir(fixtures()).unwrap() {
        let p = e.unwrap().path();
        let files = walk(&p);
        let Ok(out) = output(&files) else { continue };
        accepted += 1;
        let get = |k: &str| out.entries.iter().find(|e| e.0 == k).map(|e| e.1.clone());
        for (k, n, b) in rebuild_store(&get).unwrap() {
            assert_eq!(n, files[&k].len() as u64, "{p:?} {k}");
            if let Some(b) = b {
                assert!(b == files[&k], "{p:?} {k}");
            }
        }
    }
    assert!(accepted >= 14, "{accepted}");
}

#[test]
fn documents_are_read_as_json_by_the_store_rules() {
    // integers keep every digit; a repeated member is the last one, in the first one's place
    let d = json::parse(br#"{"a": 123456789012345678901234567890, "b": -0, "a": [1.5e3, "\ud800x"]}"#).unwrap();
    assert_eq!(d.value.text(), r#"{"a":[1500.0,"\ud800x"],"b":0}"#);
    assert_eq!(d.members.iter().map(|m| m.0.as_str()).collect::<Vec<_>>(), ["a", "b", "a"]);
    for (bad, why) in [
        (&b"\xef\xbb\xbf{}"[..], "byte order mark"),
        (b"{\"a\": NaN}", "NaN"),
        (b"[1e400]", "not finite"),
        (b"{\"a\": 1,}", "invalid JSON"),
        (b"\"\xff\"", "not UTF-8"),
        (b"{\"a\": \"\x01\"}", "control character"),
    ] {
        let e = json::parse(bad).err().unwrap();
        assert!(e.contains(why), "{bad:?}: {e}");
    }
    // an unpaired surrogate escape is a key of its own, not the text its marker reads as
    let d = json::parse(br#"{"\ud800": 1, "\ufffdd800": 2, "\uD800": 3}"#).unwrap();
    assert_eq!(d.value.text(), r#"{"\ud800":3,"\ufffdd800":2}"#);
    let d = json::parse(br#"{"\ud800\ufffdd800": 1, "\ufffdd800\ud800": 2}"#).unwrap();
    assert_eq!(d.value.members().len(), 2);
    // a .zattrs nesting 256 deep is a node's attributes, which its source metadata nests deeper
    let mut files = walk(&fixtures().join("zarr2_hierarchy"));
    files.insert(".zattrs".into(), format!("{{\"a\":{}{}}}", "[".repeat(255), "]".repeat(255)).into_bytes());
    output(&files).unwrap();
    // a document of more than 4096 members is one value, `json`, and rebuilds as any other
    for (n, values) in [(4096usize, 4096usize), (4097, 1)] {
        let mut files = walk(&fixtures().join("zarr2_hierarchy"));
        let doc = format!("{{{}}}", (0..n).map(|i| format!("\"k{i}\":{i}")).collect::<Vec<_>>().join(","));
        files.insert(".zattrs".into(), doc.into_bytes());
        let keys: Vec<(String, u64)> = files.iter().map(|(k, v)| (k.clone(), v.len() as u64)).collect();
        let bytes: Vec<&Vec<u8>> = files.values().collect();
        let (ir, _, _) = run_store(StoreFormat::Zarr2, keys, &|k| bytes[k].clone()).unwrap();
        let o = (0..ir.len() as u32).find(|&i| ir.kind[i as usize] == vzip_ir::ir::OBJECT && ir.key(i) == ".zattrs").unwrap();
        let kids: Vec<u32> = ir.children(o).iter().copied().filter(|&c| ir.kind[c as usize] == vzip_ir::ir::VALUE).collect();
        assert_eq!(kids.len(), values, "{n}");
        if values == 1 {
            let c = kids[0] as usize;
            assert_eq!((ir.names.get(ir.name[c]), ir.start[c], ir.len[c]), ("json", 0, files[".zattrs"].len() as u64));
        }
        let out = output(&files).unwrap();
        let get = |k: &str| out.entries.iter().find(|e| e.0 == k).map(|e| e.1.clone());
        let rebuilt = rebuild_store(&get).unwrap();
        assert!(rebuilt.iter().any(|(k, _, b)| k == ".zattrs" && b.as_deref() == Some(&files[".zattrs"][..])), "{n}");
    }
    let deep = format!("{}{}", "[".repeat(257), "]".repeat(257));
    assert!(json::parse(deep.as_bytes()).err().unwrap().contains("nests more than 256"));
}

#[test]
fn other_objects_are_escaped_and_chunk_keys_follow_the_grid() {
    for (key, at) in [("a/README", "a/README"), ("zarr.json", "zarr.json~"), ("a/.zarray", "a/.zarray~"),
                      ("x/.zgroup~~", "x/.zgroup~~~"), ("x/zarr.jsonx", "x/zarr.jsonx"), ("~", "~")] {
        assert_eq!(object_key(key), format!("vzip_source/objects/{at}"), "{key}");
    }
    for (rest, grid, sep, chunk) in [
        ("0", &[][..], ".", true), ("1", &[][..], ".", false), ("0.1", &[2, 2][..], ".", true),
        ("0/1", &[2, 2][..], "/", true), ("0/1", &[2, 2][..], ".", false), ("0.1", &[2, 2][..], "/", false),
        ("01.0", &[20, 2][..], ".", false), ("2.0", &[2, 2][..], ".", false), ("0", &[2, 2][..], ".", false),
        ("0.0.0", &[2, 2][..], ".", false), ("+1.0", &[2, 2][..], ".", false), ("0.0", &[0, 2][..], ".", false),
    ] {
        assert_eq!(is_chunk(rest, grid, sep), chunk, "{rest} {grid:?} {sep}");
    }
}

#[test]
fn the_ignored_keys_are_vzip_source_ignored_json_in_byte_order() {
    let files = walk(&fixtures().join("zarr2_hierarchy"));
    for ignored in [vec![], vec!["b//c".to_string(), "./x".into(), "\u{e9}/..".into()]] {
        let keys: Vec<(String, u64)> = files.iter().map(|(k, v)| (k.clone(), v.len() as u64)).collect();
        let bytes: Vec<&Vec<u8>> = files.values().collect();
        let mut r = StoreRun::new(StoreFormat::Zarr2, keys, 4).unwrap();
        r.ignore(ignored.clone());
        while let Some(reqs) = r.poll().unwrap() {
            for (k, q) in reqs.iter().enumerate() {
                r.complete(k, q.spans.iter().map(|&(a, e)| bytes[q.object][a as usize..e as usize].to_vec()).collect()).unwrap();
            }
        }
        let out = r.into_output("https://data.test/h/", true).unwrap();
        let doc = out.entries.iter().find(|e| e.0 == "vzip_source/ignored.json").map(|e| String::from_utf8(e.1.clone()).unwrap());
        assert_eq!(doc, (!ignored.is_empty()).then(|| "{\"ignored\":[\"./x\",\"b//c\",\"\u{e9}/..\"]}".to_string()));
    }
}

macro_rules! reject {
    ($name:ident, $dir:expr, $why:expr) => {
        #[test]
        fn $name() {
            let e = run($dir).err().unwrap_or_else(|| panic!("{} accepted", $dir));
            assert!(e.contains($why), "{}: {e}", $dir);
        }
    };
}

reject!(zarr2_reject_blosc_clevel, "zarr2_reject_blosc_clevel", "blosc clevel none is not an integer from 0 to 9");
reject!(zarr2_reject_blosc_cname, "zarr2_reject_blosc_cname", "blosc cname snappy2 is not supported");
reject!(zarr2_reject_blosc_shuffle, "zarr2_reject_blosc_shuffle", "blosc shuffle 3 is not supported");
reject!(zarr2_reject_chunks_zero, "zarr2_reject_chunks_zero", "chunks [2, 0] is not 2 chunk sizes");
reject!(zarr2_reject_compressor_bz2, "zarr2_reject_compressor_bz2", "compressor bz2 is not supported");
reject!(zarr2_reject_compressor_lz4, "zarr2_reject_compressor_lz4", "compressor lz4 is not supported");
reject!(zarr2_reject_compressor_no_id, "zarr2_reject_compressor_no_id", "compressor is not null or an object with a string id");
reject!(zarr2_reject_dtype_bytes, "zarr2_reject_dtype_bytes", "dtype |S8 is not supported");
reject!(zarr2_reject_dtype_complex, "zarr2_reject_dtype_complex", "dtype <c8 is not supported");
reject!(zarr2_reject_dtype_datetime, "zarr2_reject_dtype_datetime", "dtype <M8[ns] is not supported");
reject!(zarr2_reject_dtype_no_byteorder, "zarr2_reject_dtype_no_byteorder", "dtype |u2 is not supported");
reject!(zarr2_reject_dtype_object, "zarr2_reject_dtype_object", "dtype |O is not supported");
reject!(zarr2_reject_dtype_structured, "zarr2_reject_dtype_structured", "dtype [[a, <u2], [b, <f4]] is not supported");
reject!(zarr2_reject_dtype_unicode, "zarr2_reject_dtype_unicode", "dtype <U4 is not supported");
reject!(zarr2_reject_fill_base64, "zarr2_reject_fill_base64", "fill_value AAAAAAAA+H8= is not a float64");
reject!(zarr2_reject_fill_bool_int, "zarr2_reject_fill_bool_int", "fill_value 1 is not a bool");
reject!(zarr2_reject_fill_f4_overflow, "zarr2_reject_fill_f4_overflow", "is not a float32");
reject!(zarr2_reject_fill_fraction_int, "zarr2_reject_fill_fraction_int", "fill_value 0.5 is not a uint16");
reject!(zarr2_reject_fill_out_of_range, "zarr2_reject_fill_out_of_range", "fill_value 300 is not a uint8");
reject!(zarr2_reject_fill_string_int, "zarr2_reject_fill_string_int", "fill_value NaN is not a uint16");
reject!(zarr2_reject_fill_uint64_max, "zarr2_reject_fill_uint64_max", "is not a uint64");
reject!(zarr2_reject_filters, "zarr2_reject_filters", "filters are not supported");
reject!(zarr2_reject_gzip_level, "zarr2_reject_gzip_level", "gzip level 1 is not an integer from -1 to 9");
reject!(zarr2_reject_json_duplicate_last_wins, "zarr2_reject_json_duplicate_last_wins", "order K is not C or F");
reject!(zarr2_reject_json_nan_literal, "zarr2_reject_json_nan_literal", ".zarray: JSON literal NaN is not allowed");
reject!(zarr2_reject_no_root_metadata, "zarr2_reject_no_root_metadata", "the root has no .zarray or .zgroup");
reject!(zarr2_reject_objects_collision, "zarr2_reject_objects_collision", "the node vzip_source is where the store's source metadata goes");
reject!(zarr2_reject_order, "zarr2_reject_order", "order A is not C or F");
reject!(zarr2_reject_order_missing, "zarr2_reject_order_missing", "order none is not C or F");
reject!(zarr2_reject_separator, "zarr2_reject_separator", "dimension_separator - is not . or /");
reject!(zarr2_reject_shape_chunks_mismatch, "zarr2_reject_shape_chunks_mismatch", "chunks [2] is not 2 chunk sizes");
reject!(zarr2_reject_shape_negative, "zarr2_reject_shape_negative", "shape [4, -4] is not up to 32 sizes");
reject!(zarr2_reject_zarr_format_3, "zarr2_reject_zarr_format_3", ".zarray: zarr_format is not 2");
reject!(zarr2_reject_zarray_and_zgroup, "zarr2_reject_zarray_and_zgroup", "/ has both .zarray and .zgroup");
reject!(zarr2_reject_zattrs_not_object, "zarr2_reject_zattrs_not_object", "x/.zattrs is not a JSON object");
reject!(zarr2_reject_zgroup_format, "zarr2_reject_zgroup_format", ".zgroup: zarr_format is not 2");
reject!(zarr2_reject_zlib_level, "zarr2_reject_zlib_level", "zlib level 10 is not an integer from -1 to 9");
reject!(zarr2_reject_zstd_checksum, "zarr2_reject_zstd_checksum", "zstd checksum 1 is not a boolean");
reject!(zarr2_reject_zstd_level, "zarr2_reject_zstd_level", "zstd level 23 is not an integer from -131072 to 22");
