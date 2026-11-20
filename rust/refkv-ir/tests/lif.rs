//! The LIF parser (src/lif.rs; spec/zarrify/lif.md): one test per rejected fixture.

mod common;

use refkv_ir::beside;
use refkv_ir::lif::Lif;
use refkv_ir::store::{StoreFormat, StoreRun};
use std::collections::HashMap;
use std::path::{Path, PathBuf};

/// A project's store run from the directory `dir`, as a local host drives it: each key probed is
/// found by listing. Its IR and facts, or its rejection.
fn run_project(dir: &Path, named: &str) -> Result<(refkv_ir::ir::Ir, serde_json::Value, HashMap<String, Vec<u8>>), String> {
    let read = |key: &str| std::fs::read(dir.join(key)).unwrap();
    let mut files = HashMap::from([(named.to_string(), read(named))]);
    let mut run = StoreRun::new(StoreFormat::Lif, vec![(named.to_string(), files[named].len() as u64)], 4)?;
    let list = |d: &str| -> Option<Vec<String>> {
        std::fs::read_dir(dir.join(d)).ok().map(|e| e.map(|e| e.unwrap().file_name().into_string().unwrap()).collect())
    };
    while let Some(reqs) = run.poll()? {
        if reqs.is_empty() {
            for k in run.probes().to_vec() {
                let found = beside::found_key(&k, list).map_err(|(a, b)| format!("{a} {b}"))?;
                match found.filter(|f: &String| dir.join(f).is_file()) {
                    Some(f) => {
                        let data = read(&f);
                        let size = data.len() as u64;
                        files.insert(f.clone(), data);
                        if f == k { run.found(Some(size))? } else { run.found_as(size, &f)? }
                    }
                    None => run.found(None)?,
                }
            }
            continue;
        }
        for (i, q) in reqs.iter().enumerate() {
            let data = &files[&q.key];
            run.complete(i, q.spans.iter().map(|&(a, b)| data[a as usize..b as usize].to_vec()).collect())?;
        }
    }
    let (ir, facts, _) = run.finish()?;
    Ok((ir, facts, files))
}

fn lif_dir() -> PathBuf {
    common::fixtures().join("lif")
}

/// The rejection of the fixture `input`: of a file run, or a project's store run.
fn rejection(input: &str) -> String {
    let result = if input.ends_with(".xlef") {
        run_project(&lif_dir(), input).map(|_| ())
    } else {
        refkv_ir::parse_bytes::<Lif>(&std::fs::read(lif_dir().join(input)).unwrap()).map(|_| ())
    };
    result.err().unwrap_or_else(|| panic!("{input} was accepted"))
}

// one test per case of conformance/zarrify/rejects/lif.json (build.rs), each the message its check gives
macro_rules! reject {
    ($name:ident, $input:literal, $msg:literal) => {
        #[test]
        fn $name() {
            let m = rejection($input);
            assert!(m.starts_with($msg), "{m}");
        }
    };
}
include!(concat!(env!("OUT_DIR"), "/rejects_lif.rs"));

/// Each accepted project's IR is valid and rebuilds: a document held, a LOF described in place, a
/// key as the file has it; a chain of 16 levels is within the bound, and a cycle adds nothing.
#[test]
fn a_project_is_a_valid_store_ir() {
    let cases = [("lif_xlef_tree.xlef", 2, 9), ("lif_xlef_lowercase.xlef", 2, 9), ("lif_xlef_cycle.xlef", 1, 4), ("lif_xlef_chain.xlef", 0, 16),
                 ("lif_xlef_frames.xlef", 1, 5), ("lif_xlif.xlif", 1, 2), ("lif_xlcf.xlcf", 1, 3), ("lif_xllf.xllf", 1, 3)];
    for (named, images, objects) in cases {
        let (ir, facts, files) = run_project(&lif_dir(), named).unwrap_or_else(|m| panic!("{named}: {m}"));
        refkv_ir::check::check(&ir).unwrap_or_else(|m| panic!("{named}: {m}"));
        assert_eq!(facts["images"].as_array().unwrap().len(), images, "{named}");
        assert_eq!(files.len(), objects, "{named}");
        let out = refkv_ir::store::store_output(StoreFormat::Lif, &ir, &facts, serde_json::json!({}), "https://example.org/set/", true, false).map_err(String::from)
            .unwrap_or_else(|m| panic!("{named}: {m}"));
        assert_eq!(out.summary["series"], images, "{named}");
    }
}

/// A probed key found at a key the run has adds no object: two spellings name one file.
#[test]
fn two_spellings_of_a_key_are_one_object() {
    let dir = std::env::temp_dir().join(format!("refkv_lif_spellings_{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let solo = lif_dir().join("_lif_xlcf_");
    std::fs::copy(solo.join("Solo.xlif"), dir.join("Solo.xlif")).unwrap();
    std::fs::copy(solo.join("Solo.lof"), dir.join("Solo.lof")).unwrap();
    let uid = "00000000-0000-4000-8000-000000000001";
    let doc = format!("\u{feff}<?xml version=\"1.0\"?><LMSDataContainerHeader Version=\"2\"><Element Name=\"a\" UniqueID=\"{uid}\"><Data/>\
        <Children><Reference File=\".%5Csolo.xlif\"/><Reference File=\".%5CSOLO.XLIF\"/></Children></Element></LMSDataContainerHeader>");
    std::fs::write(dir.join("a.xlef"), doc).unwrap();
    let (_, facts, files) = run_project(&dir, "a.xlef").unwrap();
    assert_eq!(facts["documents"], 2);
    assert_eq!(facts["images"].as_array().unwrap().len(), 1);
    assert_eq!(files.len(), 3);
    std::fs::remove_dir_all(&dir).unwrap();
}

/// A block no Memory element predicts, whose ID is past the walk's guess at a header, is read
/// when each round is fed exactly the ranges it asked for (the walk once asked for the ID alone
/// and dropped the fixed part, forever).
#[test]
fn an_unpredicted_long_block_id_is_read() {
    let data = std::fs::read(common::fixtures().join("lif/lif_long_block_id.lif")).unwrap();
    let (ir, facts) = refkv_ir::parse_bytes::<Lif>(&data).unwrap();
    assert_eq!(facts["blocks"], 3);
    assert!(facts["rounds"].as_u64().unwrap() <= 5, "{}", facts["rounds"]);
    assert_eq!(common::rebuilt(&ir, &data), data);
}

/// Each image of an accepted fixture names its description by its key (spec/stored-array.md §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): `declared_by`
/// is the id of the element of the XML that declares it (`header/xml`, or a LOF's
/// `description/xml`, under the LOF's object in a project's store) and the image's index among
/// that XML's images. The IR's descriptions are the images', each with that element as its one
/// declaration and a grid whose rows' element is the data of the image's memory block.
#[test]
fn every_accepted_fixtures_images_name_their_descriptions() {
    let (mut files, mut projects, mut images, mut rows) = (0, 0, 0, 0);
    for entry in std::fs::read_dir(lif_dir()).unwrap() {
        let name = entry.unwrap().file_name().into_string().unwrap();
        if name.contains("reject") {
            continue;
        }
        let (ir, facts, project) = match name.rsplit('.').next() {
            Some("lif" | "lof" | "lifext") => {
                let (ir, facts) = refkv_ir::parse_bytes::<Lif>(&std::fs::read(lif_dir().join(&name)).unwrap()).unwrap();
                (ir, facts, false)
            }
            Some("xlef" | "xlcf" | "xlif" | "xllf") => {
                let (ir, facts, _) = run_project(&lif_dir(), &name).unwrap();
                (ir, facts, true)
            }
            _ => continue,
        };
        files += !project as u32;
        projects += project as u32;
        let s = &ir.stored;
        let listed = facts["images"].as_array().unwrap();
        assert_eq!(s.arrays.len(), listed.len(), "{name}");
        for (i, im) in listed.iter().enumerate() {
            let at = format!("{name} image {i}");
            let key = im["declared_by"][0].as_u64().unwrap_or_else(|| panic!("{at}: no declared_by")) as u32;
            let index = im["declared_by"][1].as_u64().unwrap();
            // the XML of the file that holds the image: a file's own, or a project's LOF's
            let path = ir.path(key);
            let file = if project {
                assert_eq!(index, 0, "{at}");
                path.strip_suffix("description/xml").filter(|p| p.ends_with(".lof/")).unwrap_or_else(|| panic!("{at}: {path}"))
            } else {
                assert_eq!(index, i as u64, "{at}");
                assert_eq!(path, if name.ends_with(".lof") { "description/xml" } else { "header/xml" }, "{at}");
                ""
            };
            let k = s.arrays.iter().position(|a| (a.element, a.index) == (key, Some(index))).unwrap_or_else(|| panic!("{at}: no description"));
            let a = &s.arrays[k];
            assert_eq!(a.declarations, vec![refkv_ir::array::Place::Element { element: key, member: None, range: None }], "{at}");
            assert!(a.grid.is_some(), "{at}");
            let data = format!("{file}blocks/{}/data", im["block"]);
            let units: Vec<usize> = (0..s.map.len()).filter(|&r| s.map.array[r] == k as u32).collect();
            assert!(!units.is_empty(), "{at}");
            for &r in &units {
                assert_eq!(s.map.element[r].map(|e| ir.path(e)), Some(data.clone()), "{at}");
            }
            images += 1;
            rows += units.len();
        }
    }
    // of the rows, 5 and 16384 are the padded rows of two images, and the other 35 a whole image
    assert_eq!((files, projects, images, rows), (24, 8, 37, 16424));
}
