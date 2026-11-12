//! The SAFE parser in both forms (src/safe/; spec/zarrify/safe.md): every product fixture
//! it accepts is a valid IR whose mirror is canonical, whose view is the one it rebuilds, and
//! that rebuilds its source (a directory's held documents from the mirror alone, a zip file
//! byte for byte from its leaves); its band files are described in place; and one test per
//! rejected fixture (each the role the schema names).

mod common;

use common::{listing, walk};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use refkv_ir::check;
use refkv_ir::ir::{Ir, OBJECT};
use refkv_ir::mirror::{canonical_problem, load, rebuild_store, table_from_out, view_problem};
use refkv_ir::out::Out;
use refkv_ir::run::{output, run_bytes, Format};
use refkv_ir::store::{run_store, store_output, StoreFormat};

fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../fixtures/safe")
}

fn entry(out: &Out) -> impl Fn(&str) -> Option<Vec<u8>> + '_ {
    |k: &str| out.entries.iter().find(|e| e.0 == k).map(|e| e.1.clone())
}

/// The directory form's output (mirrored), its IR, or the rejection.
fn directory(files: &BTreeMap<String, Vec<u8>>) -> Result<(Out, Ir), String> {
    let keys = listing(&files);
    let bytes: Vec<&Vec<u8>> = files.values().collect();
    let (ir, facts, stats) = run_store(StoreFormat::Safe, keys, &|k| bytes[k].clone())?;
    Ok((store_output(StoreFormat::Safe, &ir, &facts, stats, "https://example.org/p/", true, false)?, ir))
}

/// The zip form's output (mirrored), its IR, or the rejection.
fn zip(data: &[u8]) -> Result<(Out, Ir), String> {
    let (ir, facts, stats) = run_bytes(Format::Safe, data)?;
    Ok((output(Format::Safe, &ir, &facts, stats, "https://example.org/p.SAFE.zip", true, &Default::default(), true)?, ir))
}

fn check_directory(path: &Path, files: &BTreeMap<String, Vec<u8>>, out: &Out, ir: &Ir) {
    check::check(ir).unwrap_or_else(|m| panic!("{path:?}: {m}"));
    let get = entry(out);
    // every held document from the mirror alone, the listing whole
    let rebuilt = rebuild_store(&get).unwrap();
    assert_eq!(rebuilt.len(), files.len(), "{path:?}");
    for (k, n, b) in &rebuilt {
        assert_eq!(*n as usize, files[k].len(), "{path:?} {k}");
        if let Some(b) = b {
            assert!(b == &files[k], "{path:?}: {k} is not rebuilt");
        }
    }
    // the band files are described in place; the XML documents read whole are held
    let back = load(&table_from_out(out).unwrap(), &mut |_, _| Err("no source".into())).unwrap();
    let src = back.sources();
    let jp2 = src.objects.iter().filter(|&&o| back.in_place(o)).count();
    assert!(jp2 > 0 && src.objects.iter().all(|&o| back.kind[o as usize] == OBJECT), "{path:?}");
    let mut read = |v: u64, n: u64| -> Result<Vec<u8>, String> {
        let (o, base) = src.at(&back, v).ok_or("no object at that virtual offset")?;
        let x = v - base;
        if x + n > back.len[o as usize] {
            return Err(format!("[{x}, {}) runs past {:?}", x + n, back.key(o)));
        }
        Ok(files[&back.key(o)][x as usize..(x + n) as usize].to_vec())
    };
    assert_eq!(canonical_problem(&get, &mut read), None, "{path:?}");
    let keys: Vec<String> = out.entries.iter().map(|e| e.0.clone()).collect();
    assert_eq!(view_problem(&keys, &get, &mut read), None, "{path:?}");
}

fn check_zip(path: &Path, data: &[u8], out: &Out, ir: &Ir) {
    check::check(ir).unwrap_or_else(|m| panic!("{path:?}: {m}"));
    let get = entry(out);
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
fn accepted_products_are_valid_irs_whose_mirrors_rebuild_them() {
    let (mut dirs, mut zips) = (0, 0);
    let mut paths: Vec<PathBuf> = std::fs::read_dir(fixtures()).unwrap().map(|e| e.unwrap().path()).collect();
    paths.sort();
    for path in paths {
        let name = path.file_name().unwrap().to_str().unwrap();
        if name.contains("reject") {
            continue;
        }
        if path.is_dir() {
            let files = walk(&path);
            let (out, ir) = directory(&files).unwrap_or_else(|m| panic!("{path:?}: {m}"));
            check_directory(&path, &files, &out, &ir);
            dirs += 1;
        } else {
            let data = std::fs::read(&path).unwrap();
            let (out, ir) = zip(&data).unwrap_or_else(|m| panic!("{path:?}: {m}"));
            check_zip(&path, &data, &out, &ir);
            zips += 1;
        }
    }
    assert_eq!((dirs, zips), (10, 4));
}

/// Each band of an accepted product's facts names its image's description by the description's
/// key, the band file's `codestream` struct (spec/stored-array.md §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>), in both forms:
/// a description with a grid, whose rows are the struct's `tiles/<k>`, one for each tile-part the
/// facts hold.
#[test]
fn every_accepted_products_bands_name_their_descriptions() {
    let (mut bands, mut tiles) = (0, 0);
    let mut paths: Vec<PathBuf> = std::fs::read_dir(fixtures()).unwrap().map(|e| e.unwrap().path()).collect();
    paths.sort();
    for path in paths {
        let name = path.file_name().unwrap().to_str().unwrap().to_string();
        if name.contains("reject") {
            continue;
        }
        let (ir, facts) = if path.is_dir() {
            let files = walk(&path);
            let bytes: Vec<&Vec<u8>> = files.values().collect();
            let (ir, facts, _) = run_store(StoreFormat::Safe, listing(&files), &|k| bytes[k].clone()).unwrap();
            (ir, facts)
        } else {
            let (ir, facts, _) = run_bytes(Format::Safe, &std::fs::read(&path).unwrap()).unwrap();
            (ir, facts)
        };
        let s = &ir.stored;
        let listed = facts["bands"].as_array().unwrap();
        assert_eq!(s.arrays.len(), listed.len(), "{name}");
        for b in listed {
            let at = format!("{name} {}", b["key"]);
            let key = b["declared_by"].as_u64().unwrap() as u32;
            let described: Vec<usize> = (0..s.arrays.len()).filter(|&k| s.arrays[k].element == key).collect();
            let [k] = described[..] else { panic!("{at}: {} descriptions", described.len()) };
            assert!(s.arrays[k].grid.is_some() && s.arrays[k].index.is_none(), "{at}");
            assert!(ir.path(key).ends_with("/codestream"), "{at}: {}", ir.path(key));
            let rows: Vec<usize> = (0..s.map.len()).filter(|&r| s.map.array[r] == k as u32).collect();
            assert_eq!(rows.len(), b["codestream"]["tiles"].as_array().unwrap().len(), "{at}");
            // a unit is a tile of the codestream: its `tiles/<k>` struct, whole
            assert!(rows.iter().all(|&r| ir.parent[s.map.element[r].unwrap() as usize] == key && s.map.range[r].is_none()), "{at}");
            bands += 1;
            tiles += rows.len();
        }
    }
    assert_eq!((bands, tiles), (203, 2628));
}

macro_rules! reject {
    ($name:ident, $file:expr, $msg:expr) => {
        #[test]
        fn $name() {
            let path = fixtures().join($file);
            let got = if path.is_dir() {
                directory(&walk(&path)).map(|_| ())
            } else {
                zip(&std::fs::read(&path).unwrap()).map(|_| ())
            };
            match got {
                Err(m) => assert_eq!(m, $msg),
                Ok(_) => panic!("{} was accepted", $file),
            }
        }
    };
}

// one test per case of conformance/zarrify/rejects/safe.json (build.rs)
include!(concat!(env!("OUT_DIR"), "/rejects_safe.rs"));

#[test]
fn a_value_the_view_shows_unread_in_an_object_described_in_place_is_refused() {
    let mut ir = Ir::new_store();
    let o = ir.listing(&[("a".to_string(), 4)]).unwrap()[0];
    ir.describe_in_place(o).unwrap();
    ir.value(o, "v", refkv_ir::ir::NO_INDEX, ">u4", 0, (0, 4), None).unwrap();
    check::finish(&mut ir).unwrap();
    let mut out = Out::new("https://example.org/s/");
    let e = refkv_ir::mirror::mirror(&ir, &mut out, "safe").unwrap_err();
    assert!(e.contains("described in place, was not read"), "{e}");
}

#[test]
fn a_band_file_whose_main_header_passes_64_kib_is_refused_near_the_bound() {
    use refkv_ir::safe::jp2::BandReader;
    // a JP2 signature and file type, then a codestream whose main header after SIZ is COD, QCD
    // and COM segments, 4 MiB in all, then an SOT: refused once the walk passes 64 KiB, without
    // reading on
    let mut f = vec![0, 0, 0, 12, b'j', b'P', b' ', b' ', 13, 10, 0x87, 10];
    f.extend_from_slice(&[0, 0, 0, 20, b'f', b't', b'y', b'p', b'j', b'p', b'2', b' ', 0, 0, 0, 0, b'j', b'p', b'2', b' ']);
    f.extend_from_slice(&[0, 0, 0, 0, b'j', b'p', b'2', b'c', 0xFF, 0x4F, 0xFF, 0x51, 0, 41, 0, 0]);
    for v in [16u32, 16, 0, 0, 16, 16, 0, 0] {
        f.extend_from_slice(&v.to_be_bytes());
    }
    f.extend_from_slice(&[0, 1, 14, 1, 1]);
    f.extend_from_slice(&[0xFF, 0x52, 0, 12, 0, 0, 0, 1, 0, 0, 4, 4, 0, 0, 0xFF, 0x5C, 0, 4, 0, 0]);
    while f.len() < 4 << 20 {
        f.extend_from_slice(&[0xFF, 0x64, 0x03, 0xE8]);
        f.extend(std::iter::repeat_n(b'x', 1000 - 2));
    }
    f.extend_from_slice(&[0xFF, 0x90]);
    let mut r = BandReader::new(f.len() as u64);
    let rules = refkv_ir::rules::of("safe");
    let e = loop {
        match r.step(rules) {
            Ok(Some((o, n))) => r.feed(o, f[o as usize..(o + n) as usize].to_vec()),
            Ok(None) => panic!("accepted"),
            Err(e) => break e,
        }
    };
    assert_eq!(e, "a band file's main header is more than 65536 bytes");
    assert!(r.bytes <= 3 << 16, "{} bytes read of {}", r.bytes, f.len());
}
