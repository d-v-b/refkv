//! The ENVI parser (src/envi.rs; spec/zarrify/envi.md): every input it accepts is a valid store
//! IR whose objects (the header file, held, and the data file, described in place) rebuild from
//! their leaves, whose mirror is canonical and whose view is the one it rebuilds; its array is laid
//! out as the convention says (the values are checked against NumPy and GDAL by
//! python/tests/ir/test_envi.py and js/test/envi/verify.py); and one test per rejected fixture
//! (each the role the schema names), the run driven as a host drives it, its probes answered from
//! the fixture directory.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use refkv_ir::check;
use refkv_ir::ir::{Ir, OBJECT};
use refkv_ir::mirror::{canonical_problem, load, table_from_out, view_problem};
use refkv_ir::out::{Out, Part};
use refkv_ir::store::{StoreFormat, StoreRun};
use serde_json::{json, Value as J};

/// A run's output, its IR, and the files it made objects, by key.
type Ran = (Out, Ir, BTreeMap<String, Vec<u8>>);

fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../fixtures/envi")
}

/// Runs the ENVI store format on the header file `key` of `files` (by key), probing for its data
/// file among them: (the output, the IR, the files the run made objects), and the facts, or the
/// rejection.
fn run_with_facts(key: &str, mut files: BTreeMap<String, Vec<u8>>) -> Result<(Ran, J), String> {
    let mut run = StoreRun::new(StoreFormat::Envi, vec![(key.to_string(), files[key].len() as u64)], 4)?;
    while let Some(reqs) = run.poll()? {
        if reqs.is_empty() {
            for k in run.probes().to_vec() {
                run.found(files.get(&k).map(|d| d.len() as u64))?;
            }
            continue;
        }
        for (k, q) in reqs.iter().enumerate() {
            let data = &files[&q.key];
            run.complete(k, q.spans.iter().map(|&(a, b)| data[a as usize..b as usize].to_vec()).collect())?;
        }
    }
    let (ir, facts, stats) = run.finish()?;
    let out = refkv_ir::store::store_output(StoreFormat::Envi, &ir, &facts, stats, &Default::default(), "https://example.org/envi/", true, false)?;
    let objects: std::collections::HashSet<String> = ir.sources().objects.iter().map(|&o| ir.key(o)).collect();
    files.retain(|k, _| objects.contains(k));
    Ok(((out, ir, files), facts))
}

/// Runs the fixture at `path`, its directory's files beside it.
fn run(path: &Path) -> Result<Ran, String> {
    run_with_facts(path.file_name().unwrap().to_str().unwrap(), beside(path)).map(|r| r.0)
}

/// The files of the directory of the fixture at `path`, by key.
fn beside(path: &Path) -> BTreeMap<String, Vec<u8>> {
    std::fs::read_dir(path.parent().unwrap()).unwrap().map(|e| e.unwrap().path()).filter(|p| p.is_file())
        .map(|p| (p.file_name().unwrap().to_str().unwrap().to_string(), std::fs::read(&p).unwrap())).collect()
}

/// The accepted fixtures' header files.
fn inputs() -> Vec<PathBuf> {
    let mut v: Vec<PathBuf> = std::fs::read_dir(fixtures()).unwrap().map(|e| e.unwrap().path())
        .filter(|p| p.extension().is_some_and(|e| e.eq_ignore_ascii_case("hdr")) && !p.to_string_lossy().contains("reject")).collect();
    v.sort();
    v
}

fn entry(out: &Out) -> impl Fn(&str) -> Option<Vec<u8>> + '_ {
    |k: &str| out.entries.iter().find(|e| e.0 == k).map(|e| e.1.clone())
}

fn doc(out: &Out, k: &str) -> J {
    serde_json::from_slice(&entry(out)(k).unwrap_or_else(|| panic!("no {k}"))).unwrap()
}

/// The data file, which the chunks reference, is described in place, and the header file is held
/// (no chunk references it); the leaves rebuild every file from its own bytes, the mirror is
/// canonical and its view is the one the table and the files give, and every reference lies
/// within a file of the input.
fn check_input(path: &Path, files: &BTreeMap<String, Vec<u8>>, out: &Out, ir: &Ir) {
    check::check(ir).unwrap_or_else(|m| panic!("{path:?}: {m}"));
    let src = ir.sources();
    assert_eq!(src.objects.len(), files.len(), "{path:?}");
    let referenced: std::collections::HashSet<&str> = out.refs.iter().flat_map(|r| &r.1)
        .filter_map(|p| if let Part::Obj(k, _, _) = p { Some(k.as_str()) } else { None }).collect();
    // the header file is the one the root names, and is held
    let root = doc(out, "zarr.json");
    let p = &root["attributes"]["refkv_zarrified"];
    let header = p["source"]["url"].as_str().unwrap().rsplit('/').next().unwrap().to_string();
    for &o in &src.objects {
        let key = ir.key(o);
        let held = key == header;
        assert!(ir.kind[o as usize] == OBJECT && ir.in_place(o) != held, "{path:?}: {key} is held or not as it should be");
        assert_eq!(referenced.contains(key.as_str()), !held, "{path:?}: {key}");
    }
    let mut rebuilt: BTreeMap<String, Vec<u8>> = BTreeMap::new();
    for (mut v, mut n) in check::leaves(ir).unwrap() {
        while n > 0 {
            let (o, base) = src.at(ir, v).unwrap();
            let key = ir.key(o);
            let x = (v - base) as usize;
            let take = (n as usize).min(ir.len[o as usize] as usize - x);
            rebuilt.entry(key.clone()).or_default().extend_from_slice(&files[&key][x..x + take]);
            v += take as u64;
            n -= take as u64;
        }
    }
    for (k, b) in files {
        assert!(rebuilt.get(k).is_some_and(|r| r == b), "{path:?}: {k} is not rebuilt");
    }
    let get = entry(out);
    let back = load(&table_from_out(out).unwrap(), &mut |_, _| Err("no source".into())).unwrap_or_else(|e| panic!("{path:?}: {e}"));
    assert_eq!(back.len(), ir.len(), "{path:?}");
    let bsrc = back.sources();
    let mut read = |v: u64, n: u64| -> Result<Vec<u8>, String> {
        let (o, base) = bsrc.at(&back, v).ok_or("no object at that virtual offset")?;
        let x = (v - base) as usize;
        let b = &files[&back.key(o)];
        b.get(x..x + n as usize).map(|s| s.to_vec()).ok_or_else(|| format!("[{x}, {}) runs past {:?}", x + n as usize, back.key(o)))
    };
    assert_eq!(canonical_problem(&get, &mut read), None, "{path:?}");
    let keys: Vec<String> = out.entries.iter().map(|e| e.0.clone()).collect();
    assert_eq!(view_problem(&keys, &get, &mut read), None, "{path:?}");
    for (k, parts) in &out.refs {
        for p in parts {
            if let Part::Obj(key, o, n) = p {
                let size = files.get(key).map(|b| b.len() as u64).unwrap_or_else(|| panic!("{path:?}: {k} references {key}, not a file of the input"));
                assert!(o + n <= size, "{path:?}: {k} references [{o}, {}) of the {size}-byte {key}", o + n);
            }
        }
    }
}

#[test]
fn accepted_inputs_are_valid_store_irs_whose_mirrors_rebuild_their_files() {
    let paths = inputs();
    for path in &paths {
        let (out, ir, files) = run(path).unwrap_or_else(|m| panic!("{path:?}: {m}"));
        check_input(path, &files, &out, &ir);
        assert_eq!(out.summary["format"], "envi");
        assert_eq!(files.len(), 2, "{path:?}");
    }
    assert_eq!(paths.len(), 20);
}

/// The example of spec/zarrify/envi.md §6
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#6-example>: a BIL file of
/// big-endian int16, one chunk a line, transposed into `c`, `y`, `x`; the root keeps every field.
#[test]
fn the_documents_example() {
    let (out, _, _) = run(&fixtures().join("envi_bil_int16_big.hdr")).unwrap();
    let root = doc(&out, "zarr.json");
    let p = &root["attributes"]["refkv_zarrified"];
    assert_eq!(p["source"]["url"], "https://example.org/envi/envi_bil_int16_big.hdr");
    assert_eq!(p["envi"]["fields"]["band names"], "{red, green, blue}");
    assert_eq!((&p["envi"]["data_type"], &p["envi"]["interleave"], &p["envi"]["byte_order"]), (&json!("int16"), &json!("bil"), &json!("big")));
    assert_eq!(root["attributes"]["ome"]["multiscales"][0]["axes"], json!([
        {"name": "c", "type": "channel"}, {"name": "y", "type": "space"}, {"name": "x", "type": "space"}]));
    let a = doc(&out, "0/zarr.json");
    assert_eq!((&a["shape"], &a["chunk_grid"]["configuration"]["chunk_shape"]), (&json!([3, 4, 5]), &json!([3, 1, 5])));
    assert_eq!(a["codecs"], json!([{"name": "transpose", "configuration": {"order": [1, 0, 2]}}, {"name": "bytes", "configuration": {"endian": "big"}}]));
    let line = |i: u64| (format!("0/c/0/{i}/0"), vec![Part::Obj("envi_bil_int16_big.img".into(), 30 * i, 30)]);
    assert_eq!(out.refs, (0..4).map(line).collect::<Vec<_>>());
}

/// A gzip file is one chunk of the whole file, whose header offset `refkv.skip` passes over in the
/// inflated bytes; a complex data type's fill value is a pair.
#[test]
fn a_gzip_file_is_one_chunk_whose_header_offset_is_skipped() {
    let (out, _, _) = run(&fixtures().join("envi_gzip_header_offset.hdr")).unwrap();
    let a = doc(&out, "0/zarr.json");
    assert_eq!(a["chunk_grid"]["configuration"]["chunk_shape"], json!([3, 4, 5]));
    assert_eq!(a["codecs"], json!([{"name": "bytes", "configuration": {"endian": "big"}},
        {"name": "refkv.skip", "configuration": {"length": 32}}, {"name": "gzip", "configuration": {"level": 1}}]));
    let size = std::fs::metadata(fixtures().join("envi_gzip_header_offset.img")).unwrap().len();
    assert_eq!(out.refs, vec![("0/c/0/0/0".to_string(), vec![Part::Obj("envi_gzip_header_offset.img".into(), 0, size)])]);
    let (out, _, _) = run(&fixtures().join("envi_type_6_complex64.hdr")).unwrap();
    assert_eq!(doc(&out, "0/zarr.json")["fill_value"], json!([0, 0]));
}

macro_rules! reject {
    ($name:ident, $file:expr, $msg:expr) => {
        #[test]
        fn $name() {
            match run(&fixtures().join($file)) {
                Err(m) => assert!(m.starts_with($msg), "{m}"),
                Ok(_) => panic!("{} was accepted", $file),
            }
        }
    };
}

// one test per case of conformance/zarrify/rejects/envi.json (build.rs)
include!(concat!(env!("OUT_DIR"), "/rejects_envi.rs"));

/// Each accepted fixture's description names its array's stored description by the key, the
/// header file's object (spec/stored-array.md §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): the IR's one
/// description, with no index, whose one row is the `data` element of the data file, the bytes
/// the description places the data at, in the chain of the compression and the header offset.
/// The description holds none of the output's decisions (spec/zarrify/envi.md §8.7
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#87-the-description>): its
/// `data_type` is the header's code, and it has no Zarr data type, codec, chunk or axis name.
#[test]
fn every_accepted_fixtures_description_names_their_stored_array() {
    let (mut n, mut skips) = (0, 0);
    for path in inputs() {
        let name = path.file_name().unwrap().to_str().unwrap().to_string();
        let ((_, ir, files), d) = run_with_facts(&name, beside(&path)).unwrap_or_else(|m| panic!("{name}: {m}"));
        let s = &ir.stored;
        for k in ["own", "array", "data_key", "header_key", "codecs", "chunks", "chunk_shape", "dimension_names", "fill_value"] {
            assert!(d.get(k).is_none(), "{name}: {k}");
        }
        assert!(d["data_type"].is_u64(), "{name}: {}", d["data_type"]);
        assert_eq!(d["header_file"], json!({"key": name, "size": files[&name].len()}), "{name}");
        let data_key = d["data_file"]["key"].as_str().unwrap();
        assert_eq!(d["data_file"]["size"], json!(files[data_key].len()), "{name}");
        let key = d["elements"]["header"].as_u64().unwrap() as u32;
        assert_eq!(ir.kind[key as usize], OBJECT, "{name}");
        assert_eq!(ir.key(key), name);
        let [described] = &s.arrays[..] else { panic!("{name}: {} descriptions", s.arrays.len()) };
        assert_eq!((described.element, described.index), (key, None), "{name}");
        assert!(described.grid.is_some() && !s.undescribed && s.undescribed_keys.is_empty(), "{name}");
        assert_eq!((s.map.len(), s.map.range[0], s.map.has_ranges[0]), (1, None, false), "{name}");
        let unit = s.map.element[0].unwrap();
        assert_eq!(Some(unit as u64), d["elements"]["data"].as_u64(), "{name}");
        assert_eq!(ir.path(unit).rsplit('/').next(), Some("data"), "{name}: {}", ir.path(unit));
        assert_eq!(ir.key(ir.sources().of[unit as usize]), data_key, "{name}");
        assert_eq!(json!([ir.start[unit as usize], ir.len[unit as usize]]), d["data_range"], "{name}");
        let chain: Vec<&str> = s.chains[described.chains[0] as usize].iter().map(|c| c.name.as_str()).collect();
        let want: &[&str] = match (d["file_compression"].as_u64().unwrap(), d["header_offset"].as_u64().unwrap()) {
            (1, o) if o > 0 => &["skip", "gzip"],
            (1, _) => &["gzip"],
            _ => &[],
        };
        assert_eq!(chain, want, "{name}");
        n += 1;
        skips += usize::from(chain.len() == 2);
    }
    assert_eq!((n, skips), (20, 1));
}

/// Every `envi_reject_*` header has a test above.
#[test]
fn every_reject_fixture_has_a_test() {
    let src = include_str!(concat!(env!("OUT_DIR"), "/rejects_envi.rs"));
    for e in std::fs::read_dir(fixtures()).unwrap() {
        let name = e.unwrap().file_name().to_str().unwrap().to_string();
        if name.starts_with("envi_reject") && name.ends_with(".hdr") {
            assert!(src.contains(&format!("\"{name}\"")), "{name} has no test");
        }
    }
}
