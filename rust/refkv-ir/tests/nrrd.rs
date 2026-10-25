//! The NRRD parser (src/nrrd.rs; spec/zarrify/nrrd.md): every input it accepts is a valid store
//! IR whose objects (the header file and a detached header's data file, described in place)
//! rebuild from their leaves, whose mirror is canonical and whose view is the one it rebuilds;
//! its arrays are laid out as the convention says (the values are checked against pynrrd by
//! python/tests/ir/test_nrrd.py and js/test/nrrd/verify.py); and one test per rejected fixture
//! (each the role the schema names), the run driven as a host drives it, its probe answered from
//! the fixture directory.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use refkv_ir::check;
use refkv_ir::ir::{Ir, OBJECT};
use refkv_ir::mirror::{canonical_problem, load, table_from_out, view_problem};
use refkv_ir::out::{Out, Part};
use refkv_ir::run::Format;
use refkv_ir::store::{StoreFormat, StoreRun};
use serde_json::{json, Value as J};

/// A run's output, its IR, and the files it made objects, by key.
type Ran = (Out, Ir, BTreeMap<String, Vec<u8>>);

fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../fixtures/nrrd")
}

/// Runs the NRRD store format on the header file `key` of `files` (by key), probing for a data
/// file among them: (the output, the IR, the files the run made objects), or the rejection.
fn run_files(key: &str, mut files: BTreeMap<String, Vec<u8>>) -> Result<Ran, String> {
    let mut run = StoreRun::new(StoreFormat::Nrrd, vec![(key.to_string(), files[key].len() as u64)], 4)?;
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
    let out = refkv_ir::store::store_output(StoreFormat::Nrrd, &ir, &facts, stats, "https://example.org/nrrd/", true)?;
    let objects: std::collections::HashSet<String> = ir.sources().objects.iter().map(|&o| ir.key(o)).collect();
    files.retain(|k, _| objects.contains(k));
    Ok((out, ir, files))
}

/// Runs the fixture at `path`, its directory's files beside it.
fn run(path: &Path) -> Result<Ran, String> {
    let dir = path.parent().unwrap();
    let mut files = BTreeMap::new();
    let mut stack = vec![dir.to_path_buf()];
    while let Some(d) = stack.pop() {
        for e in std::fs::read_dir(&d).unwrap() {
            let p = e.unwrap().path();
            if p.is_dir() {
                stack.push(p);
            } else {
                let k = p.strip_prefix(dir).unwrap().iter().map(|c| c.to_str().unwrap()).collect::<Vec<_>>().join("/");
                files.insert(k, std::fs::read(&p).unwrap());
            }
        }
    }
    run_files(path.file_name().unwrap().to_str().unwrap(), files)
}

fn entry(out: &Out) -> impl Fn(&str) -> Option<Vec<u8>> + '_ {
    |k: &str| out.entries.iter().find(|e| e.0 == k).map(|e| e.1.clone())
}

fn doc(out: &Out, k: &str) -> J {
    serde_json::from_slice(&entry(out)(k).unwrap_or_else(|| panic!("no {k}"))).unwrap()
}

/// Every object is described in place, its leaves rebuild it from its own bytes, the mirror is
/// canonical and its view is the one the table and the files give, and every reference lies
/// within a file of the input.
fn check_input(path: &Path, files: &BTreeMap<String, Vec<u8>>, out: &Out, ir: &Ir) {
    check::check(ir).unwrap_or_else(|m| panic!("{path:?}: {m}"));
    let src = ir.sources();
    assert_eq!(src.objects.len(), files.len(), "{path:?}");
    for &o in &src.objects {
        assert!(ir.kind[o as usize] == OBJECT && ir.in_place(o), "{path:?}: {} is not described in place", ir.key(o));
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
    let mut n = 0;
    for e in std::fs::read_dir(fixtures()).unwrap() {
        let path = e.unwrap().path();
        let name = path.file_name().unwrap().to_str().unwrap().to_string();
        if !(name.ends_with(".nrrd") || name.ends_with(".nhdr")) || name.contains("reject") {
            continue;
        }
        let (out, ir, files) = run(&path).unwrap_or_else(|m| panic!("{path:?}: {m}"));
        check_input(&path, &files, &out, &ir);
        assert_eq!(out.summary["format"], "nrrd");
        assert_eq!(files.len(), if name.ends_with(".nhdr") { 2 } else { 1 }, "{name}");
        n += 1;
    }
    assert_eq!(n, 40);
}

/// An axis-aligned orientation is applied: the scales are the directions' diagonal, the
/// translation the origin, the units the space units; the root keeps every field.
#[test]
fn an_axis_aligned_orientation_places_the_image() {
    let (out, ir, _) = run(&fixtures().join("nrrd_space_lps_aligned.nrrd")).unwrap();
    let root = doc(&out, "zarr.json");
    let ms = &root["attributes"]["ome"]["multiscales"][0];
    assert_eq!(ms["datasets"][0]["coordinateTransformations"], json!([
        {"type": "scale", "scale": [1.25, 0.75, 0.5]}, {"type": "translation", "translation": [40.0, 3.5, -12.0]}]));
    assert_eq!(ms["axes"], json!([{"name": "z", "type": "space", "unit": "millimeter"},
        {"name": "y", "type": "space", "unit": "millimeter"}, {"name": "x", "type": "space", "unit": "millimeter"}]));
    let p = &root["attributes"]["refkv_zarrified"];
    assert_eq!(p["source"]["url"], "https://example.org/nrrd/nrrd_space_lps_aligned.nrrd");
    let own = &p["nrrd"];
    assert_eq!(own["magic"], "NRRD0004");
    assert_eq!(own["fields"]["space directions"], "(0.5,0,0) (0,0.75,0) (0,0,1.25)");
    assert_eq!(own["axes"], json!(["x", "y", "z"]));
    assert_eq!(own["orientation"], json!({"applied": true}));
    assert_eq!(own["byte_order"], "little");
    let a = doc(&out, "0/zarr.json");
    assert_eq!(a["codecs"], json!([{"name": "bytes", "configuration": {"endian": "little"}}]));
    // one chunk, the data's 240 bytes after the header
    let header = 241;
    assert_eq!(out.refs, vec![("0/c/0/0/0".to_string(), vec![Part::Obj("nrrd_space_lps_aligned.nrrd".into(), header, 240)])]);
    assert_eq!(ir.len(), 15);
}

/// An oblique orientation is not applied: the scales are the directions' lengths, with no
/// translation.
#[test]
fn an_oblique_orientation_scales_by_the_directions_lengths() {
    let (out, _, _) = run(&fixtures().join("nrrd_space_oblique.nrrd")).unwrap();
    let root = doc(&out, "zarr.json");
    assert_eq!(root["attributes"]["ome"]["multiscales"][0]["datasets"][0]["coordinateTransformations"],
               json!([{"type": "scale", "scale": [0.5, 0.75, 0.5]}]));
    assert_eq!(root["attributes"]["refkv_zarrified"]["nrrd"]["orientation"], json!({"applied": false}));
    assert_eq!(doc(&out, "0/zarr.json")["codecs"][0], json!({"name": "bytes", "configuration": {"endian": "big"}}));
}

/// A gzip stream is one chunk: the whole array, the stream to the file's end, decoded by the
/// `gzip` codec.
#[test]
fn a_gzip_stream_is_one_chunk_of_the_whole_array() {
    let (out, _, files) = run(&fixtures().join("nrrd_gzip_3d.nrrd")).unwrap();
    let a = doc(&out, "0/zarr.json");
    assert_eq!(a["shape"], json!([4, 5, 6]));
    assert_eq!(a["chunk_grid"]["configuration"]["chunk_shape"], json!([4, 5, 6]));
    assert_eq!(a["codecs"], json!([{"name": "bytes", "configuration": {"endian": "little"}}, {"name": "gzip", "configuration": {"level": 1}}]));
    let size = files["nrrd_gzip_3d.nrrd"].len() as u64;
    match out.refs.as_slice() {
        [(k, parts)] => {
            assert_eq!(k, "0/c/0/0/0");
            assert!(matches!(parts.as_slice(), [Part::Obj(f, o, n)] if f == "nrrd_gzip_3d.nrrd" && o + n == size));
        }
        other => panic!("{other:?}"),
    }
}

/// A detached header's data file, in a directory below it, is the second object; line skip
/// passes over its first line.
#[test]
fn a_detached_header_references_its_data_file() {
    let (out, _, files) = run(&fixtures().join("nrrd_detached_gzip.nhdr")).unwrap();
    let key = "nrrd_detached_gzip_files/data.raw.gz";
    assert_eq!(files.keys().collect::<Vec<_>>(), ["nrrd_detached_gzip.nhdr", key]);
    let own = &doc(&out, "zarr.json")["attributes"]["refkv_zarrified"]["nrrd"];
    assert_eq!(own["data_file"], key);
    assert_eq!(own["encoding"], "gzip");
    let size = files[key].len() as u64;
    assert_eq!(out.refs, vec![("0/c/0/0/0".to_string(), vec![Part::Obj(key.into(), 8, size - 8)])]);
}

/// A color axis stored innermost is the channel axis, put first by `transpose`, with the colors
/// of its kind.
#[test]
fn a_color_axis_is_the_channel_axis() {
    let (out, _, _) = run(&fixtures().join("nrrd_kinds_rgb.nrrd")).unwrap();
    let a = doc(&out, "0/zarr.json");
    assert_eq!(a["dimension_names"], json!(["c", "y", "x"]));
    assert_eq!(a["codecs"][0], json!({"name": "transpose", "configuration": {"order": [1, 2, 0]}}));
    let ch = &doc(&out, "zarr.json")["attributes"]["ome"]["omero"]["channels"];
    assert_eq!((ch[0]["label"].as_str(), ch[2]["color"].as_str()), (Some("R"), Some("0000FF")));
}

/// Comments, key/value pairs (unescaped, a repeated key's last value) and every field are on the
/// root.
#[test]
fn the_root_keeps_every_field_pair_and_comment() {
    let (out, _, _) = run(&fixtures().join("nrrd_header_everything.nrrd")).unwrap();
    let own = &doc(&out, "zarr.json")["attributes"]["refkv_zarrified"]["nrrd"];
    assert_eq!(own["comments"], json!(["a comment", "another, ## and all"]));
    assert_eq!(own["key_values"], json!({"escaped": "a\nline\\end", "repeated": "last", "with spaces ": " and = signs:="}));
    assert_eq!(own["fields"]["old max"], "2");
    assert_eq!(own["fields"]["axis maxs"], "4 3 2");
    assert_eq!(own["fields"]["labels"], "\"x \\\"one\\\"\" \"y\" \"z\"");
    assert_eq!(own["fields"].as_object().unwrap().len(), 19);
    let ch = &doc(&out, "zarr.json")["attributes"]["ome"]["omero"]["channels"];
    assert_eq!(ch[0]["window"], json!({"min": 3.0, "max": 249.5, "start": 3.0, "end": 249.5}));
}

/// The cut of contiguous values (conventions §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays>), with an
/// edge chunk's padding as a literal.
#[test]
fn the_data_is_cut_into_chunks_of_at_most_128_kib() {
    let (out, _, _) = run(&fixtures().join("nrrd_chunk_edge.nrrd")).unwrap();
    assert_eq!(doc(&out, "0/zarr.json")["chunk_grid"]["configuration"]["chunk_shape"], json!([16, 30, 100]));
    let last = out.refs.iter().find(|r| r.0 == "0/c/2/0/0").unwrap();
    assert!(matches!(last.1.as_slice(), [Part::Obj(_, _, 90000), Part::Lit(pad)] if pad.len() == 6000));
}

/// A spatial axis whose direction has no component of its own (three axes in a 2-D space) is
/// placed by the directions' lengths: the orientation is not applied.
#[test]
fn a_direction_without_the_axis_component_is_not_applied() {
    let (out, _, _) = run(&fixtures().join("nrrd_space_dimension_short.nrrd")).unwrap();
    let root = doc(&out, "zarr.json");
    assert_eq!(root["attributes"]["refkv_zarrified"]["nrrd"]["orientation"], json!({"applied": false}));
    assert_eq!(root["attributes"]["ome"]["multiscales"][0]["datasets"][0]["coordinateTransformations"],
               json!([{"type": "scale", "scale": [1, 1.0, 1.0]}]));
}

/// `about`, which describes each of the schema's tables, names no field, type, encoding, space or
/// kind.
#[test]
fn about_names_no_field_type_encoding_or_space() {
    let input = |lines: &str| BTreeMap::from([("a.nrrd".to_string(), format!("NRRD0004\n{lines}\n\nx").into_bytes())]);
    let fields = "type: uchar\ndimension: 2\nsizes: 1 1\nencoding: raw";
    let space = format!("{fields}\nspace: about");
    let kinds = format!("{fields}\nkinds: domain about");
    let cases = [
        ("about: x", "line 2 of the header: \"about\" is not a NRRD field"),
        ("type: about\ndimension: 2\nsizes: 1 1\nencoding: raw", "unknown NRRD type \"about\""),
        ("type: uchar\ndimension: 2\nsizes: 1 1\nencoding: about", "unknown NRRD encoding \"about\""),
        (space.as_str(), "the space field \"about\" is not a NRRD space"),
        (kinds.as_str(), "the kinds field \"domain about\" is not NRRD kinds"),
    ];
    for (lines, message) in cases {
        assert_eq!(run_files("a.nrrd", input(lines)).err().as_deref(), Some(message), "{lines}");
    }
    assert!(run_files("a.nrrd", input(fields)).is_ok());
}

#[test]
fn a_header_longer_than_16_mib_without_a_blank_line_is_rejected() {
    let mut b = b"NRRD0004\n".to_vec();
    while b.len() <= 1 << 24 {
        b.extend_from_slice(b"# a comment line, of many\n");
    }
    b.extend_from_slice(b"\n");
    let files = BTreeMap::from([("big.nrrd".to_string(), b)]);
    assert_eq!(run_files("big.nrrd", files).err().as_deref(), Some("no blank line ends the NRRD header within its first 16777216 bytes"));
}

#[test]
fn sniff_finds_a_nrrd_header() {
    assert_eq!(Format::sniff(b"NRRD0004\ntype: short\n"), Some(Format::Nrrd));
    assert_eq!(Format::sniff(b"NRRD0005\r\n"), Some(Format::Nrrd));
    assert_eq!(Format::sniff(b"NRRD0006\n"), None);
    assert_eq!(Format::sniff(b"NRRD00.01\n"), None);
}

#[test]
fn a_nrrd_file_run_is_refused() {
    let data = std::fs::read(fixtures().join("nrrd_raw_uint8_2d.nrrd")).unwrap();
    assert_eq!(refkv_ir::run::run_bytes(Format::Nrrd, &data).err().as_deref(), Some("a NRRD file is zarrified as a store run, with its data file"));
}

#[test]
fn a_listing_of_more_than_the_header_file_is_refused() {
    let r = StoreRun::new(StoreFormat::Nrrd, vec![("a.nhdr".into(), 100), ("a.raw".into(), 100)], 1);
    assert!(r.err().unwrap().starts_with("a NRRD run starts from the header file alone"));
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

// one test per case of conformance/zarrify/rejects/nrrd.json (build.rs)
include!(concat!(env!("OUT_DIR"), "/rejects_nrrd.rs"));

/// Every `nrrd_reject_*` fixture has a test above.
#[test]
fn every_reject_fixture_has_a_test() {
    let src = include_str!(concat!(env!("OUT_DIR"), "/rejects_nrrd.rs"));
    for e in std::fs::read_dir(fixtures()).unwrap() {
        let name = e.unwrap().file_name().to_str().unwrap().to_string();
        if name.starts_with("nrrd_reject") {
            assert!(src.contains(&format!("\"{name}\"")), "{name} has no test");
        }
    }
}
