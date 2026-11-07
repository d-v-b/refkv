//! The GRIB parser (src/grib/; spec/zarrify/grib.md): every input it accepts is a valid store IR
//! whose objects (the GRIB file, described in place, and an index beside it, held) rebuild from
//! their leaves, whose mirror is canonical and whose view is the one it rebuilds; its index is a
//! hint, so an input zarrifies the same with or without it; its fields are laid out as the
//! convention says (the values are checked against ecCodes by python/tests/ir/test_grib.py); and
//! one test per rejected fixture (each the role the schema names), the run driven as a host
//! drives it, its probe answered from the fixture directory.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use refkv_ir::check;
use refkv_ir::ir::{Ir, OBJECT};
use refkv_ir::mirror::{canonical_problem, load, table_from_out, view_problem};
use refkv_ir::out::{Out, Part};
use refkv_ir::store::{StoreFormat, StoreRun};
use serde_json::Value as J;

/// A run's output, its IR, and the files it made objects, by key.
type Ran = (Out, Ir, BTreeMap<String, Vec<u8>>);

fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../fixtures/grib")
}

/// Runs the GRIB store format on the file `key` of `files` (by key), probing for an index among
/// them: (the output, the IR, the files the run made objects), or the rejection.
fn run_files(key: &str, mut files: BTreeMap<String, Vec<u8>>) -> Result<Ran, String> {
    let mut run = StoreRun::new(StoreFormat::Grib, vec![(key.to_string(), files[key].len() as u64)], 4)?;
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
    let out = refkv_ir::store::store_output(StoreFormat::Grib, &ir, &facts, stats, "https://example.org/grib/", true, false)?;
    let objects: std::collections::HashSet<String> = ir.sources().objects.iter().map(|&o| ir.key(o)).collect();
    files.retain(|k, _| objects.contains(k));
    Ok((out, ir, files))
}

/// The file at `path` alone, or with the files of its directory beside it (`beside`).
fn files_of(path: &Path, beside: bool) -> BTreeMap<String, Vec<u8>> {
    let mut files = BTreeMap::new();
    let name = path.file_name().unwrap().to_str().unwrap().to_string();
    files.insert(name.clone(), std::fs::read(path).unwrap());
    if beside {
        for e in std::fs::read_dir(path.parent().unwrap()).unwrap() {
            let p = e.unwrap().path();
            let k = p.file_name().unwrap().to_str().unwrap().to_string();
            if p.is_file() && k.starts_with(name.split('.').next().unwrap()) {
                files.entry(k).or_insert_with(|| std::fs::read(&p).unwrap());
            }
        }
    }
    files
}

fn entry(out: &Out) -> impl Fn(&str) -> Option<Vec<u8>> + '_ {
    |k: &str| out.entries.iter().find(|e| e.0 == k).map(|e| e.1.clone())
}

pub fn doc(out: &Out, k: &str) -> J {
    serde_json::from_slice(&entry(out)(k).unwrap_or_else(|| panic!("no {k}"))).unwrap()
}

/// The GRIB file (the root's source) is described in place and referenced; an index is held and
/// not referenced; the leaves rebuild every file from its own bytes, the mirror is canonical and
/// its view is the one the table and the files give, and every reference lies within the file.
pub fn check_input(path: &Path, files: &BTreeMap<String, Vec<u8>>, out: &Out, ir: &Ir) {
    check::check(ir).unwrap_or_else(|m| panic!("{path:?}: {m}"));
    let src = ir.sources();
    assert_eq!(src.objects.len(), files.len(), "{path:?}");
    let referenced: std::collections::HashSet<&str> = out.refs.iter().flat_map(|r| &r.1)
        .filter_map(|p| if let Part::Obj(k, _, _) = p { Some(k.as_str()) } else { None }).collect();
    let root = doc(out, "zarr.json");
    let url = root["attributes"]["refkv_zarrified"]["source"]["url"].as_str().unwrap();
    let grib = url.rsplit('/').next().unwrap().to_string();
    for &o in &src.objects {
        let key = ir.key(o);
        let is_grib = key == grib;
        assert!(ir.kind[o as usize] == OBJECT && ir.in_place(o) == is_grib, "{path:?}: {key} is held or not as it should be");
        assert_eq!(referenced.contains(key.as_str()), is_grib, "{path:?}: {key}");
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

/// An output's documents and references.
type Layout = (Vec<(String, Vec<u8>)>, Vec<(String, Vec<Part>)>);

/// The output's documents and references, but the mirror's (which holds the index when there is
/// one) and the summary's counts.
fn layout(out: &Out) -> Layout {
    let keep = |k: &str| !k.starts_with(refkv_ir::out::SOURCE_NODE) && k != "zarr.json";
    (out.entries.iter().filter(|e| keep(&e.0)).cloned().collect(), out.refs.clone())
}

/// The accepted fixtures, and any GRIB files named by `REFKV_GRIB` (a path list, for files that
/// are not checked in): each is a valid store IR as `check_input` says, and its layout is the same
/// with the index beside it as without.
#[test]
fn accepted_inputs_are_valid() {
    let mut paths: Vec<PathBuf> = std::fs::read_dir(fixtures()).map(|d| d.map(|e| e.unwrap().path()).collect()).unwrap_or_default();
    paths.retain(|p| p.extension().is_some_and(|x| x == "grib2") && !p.file_name().unwrap().to_str().unwrap().contains("_reject_"));
    if let Ok(extra) = std::env::var("REFKV_GRIB") {
        paths.extend(std::env::split_paths(&extra));
    }
    paths.sort();
    for path in &paths {
        let (out, ir, files) = run_files(path.file_name().unwrap().to_str().unwrap(), files_of(path, true))
            .unwrap_or_else(|e| panic!("{path:?}: {e}"));
        check_input(path, &files, &out, &ir);
        let (alone, _, _) = run_files(path.file_name().unwrap().to_str().unwrap(), files_of(path, false))
            .unwrap_or_else(|e| panic!("{path:?}: {e}"));
        assert_eq!(layout(&out), layout(&alone), "{path:?}: the index changes the layout");
        if std::env::var("REFKV_GRIB_SHOW").is_ok() {
            eprintln!("{path:?}: {}", serde_json::to_string_pretty(&out.summary).unwrap());
        }
    }
}

/// Runs the fixture at `path`, the files of its directory beside it.
fn run(path: &Path) -> Result<Ran, String> {
    run_files(path.file_name().unwrap().to_str().unwrap(), files_of(path, true))
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

// one test per case of conformance/zarrify/rejects/grib.json (build.rs)
include!(concat!(env!("OUT_DIR"), "/rejects_grib.rs"));

/// Every `grib_reject_*` fixture has a test above.
#[test]
fn every_reject_fixture_has_a_test() {
    let src = include_str!(concat!(env!("OUT_DIR"), "/rejects_grib.rs"));
    for e in std::fs::read_dir(fixtures()).unwrap() {
        let name = e.unwrap().file_name().to_str().unwrap().to_string();
        if name.contains("_reject_") {
            assert!(src.contains(&format!("{name:?}")), "{name} has no case in conformance/zarrify/rejects/grib.json");
        }
    }
}

/// The checks no fixture breaks, in files made here: a file with no message (padding alone, the
/// rest of it at most 4096 bytes), and a section 2 longer than the profile reads.
#[test]
fn reject_no_message() {
    let files = BTreeMap::from([("f.grib2".to_string(), vec![0u8; 100])]);
    let m = run_files("f.grib2", files).err().unwrap();
    assert!(m.starts_with("no GRIB message"), "{m}");
}

#[test]
fn reject_section_size() {
    let simple = std::fs::read(fixtures().join("grib_simple.grib2")).unwrap();
    let s1 = u32::from_be_bytes(simple[16..20].try_into().unwrap()) as usize;
    let at = 16 + s1;
    let local = 5 + (1 << 20);
    let mut b = simple[..at].to_vec();
    b.extend_from_slice(&(local as u32).to_be_bytes());
    b.push(2);
    b.resize(at + local, 0);
    b.extend_from_slice(&simple[at..]);
    let total = b.len() as u64;
    b[8..16].copy_from_slice(&total.to_be_bytes());
    let m = run_files("f.grib2", BTreeMap::from([("f.grib2".to_string(), b)])).err().unwrap();
    assert!(m.starts_with("the section 2 of the GRIB message at byte 0 has 1048581 octets, more than the profile reads"), "{m}");
}

/// The fixture `name` with `patch` applied to its section 3 (its bytes from its length on): the
/// rejection of a file whose one field is then not presented.
fn rejection_with_section3(name: &str, patch: impl Fn(&mut Vec<u8>)) -> String {
    let b = std::fs::read(fixtures().join(name)).unwrap();
    let ind = refkv_ir::grib::sections::indicator(&b).unwrap();
    let all = refkv_ir::grib::sections::sections(&b, ind.length).unwrap();
    let s = all.iter().find(|s| s.number == 3).unwrap();
    let (at, end) = (s.offset as usize, (s.offset + s.length) as usize);
    let mut s3 = b[at..end].to_vec();
    patch(&mut s3);
    let n = s3.len() as u32;
    s3[..4].copy_from_slice(&n.to_be_bytes());
    let mut out = b[..at].to_vec();
    out.extend_from_slice(&s3);
    out.extend_from_slice(&b[end..]);
    let total = out.len() as u64;
    out[8..16].copy_from_slice(&total.to_be_bytes());
    run_files("f.grib2", BTreeMap::from([("f.grib2".to_string(), out)])).err().expect("the file was accepted")
}

/// The message that says the file's one field is not presented, for `reason`.
fn none_presented(reason: &str) -> String {
    format!("none of the 1 GRIB fields can be presented: {reason}")
}

/// Sets the 4 octets at octet `at` of a section.
fn set4(s: &mut [u8], at: usize, v: u32) {
    s[at - 1..at + 3].copy_from_slice(&v.to_be_bytes());
}

/// A Gaussian grid whose N is 0.
#[test]
fn gaussian_of_no_parallels_is_not_presented() {
    let m = rejection_with_section3("grib_gaussian.grib2", |s| set4(s, 68, 0));
    assert_eq!(m, none_presented("gaussian"));
}

/// A Gaussian grid of more parallels than the profile computes.
#[test]
fn gaussian_of_too_many_parallels_is_not_presented() {
    let m = rejection_with_section3("grib_gaussian.grib2", |s| set4(s, 68, 8193));
    assert_eq!(m, none_presented("gaussian"));
}

/// A Gaussian grid whose first latitude is halfway between two rows.
#[test]
fn gaussian_off_its_rows_is_not_presented() {
    let m = rejection_with_section3("grib_gaussian.grib2", |s| set4(s, 47, 75_000_000));
    assert_eq!(m, none_presented("gaussian"));
}

/// A Gaussian grid whose rows run past a pole: scanning north from the first row.
#[test]
fn gaussian_past_its_pole_is_not_presented() {
    let m = rejection_with_section3("grib_gaussian.grib2", |s| s[71] = 0x40);
    assert_eq!(m, none_presented("gaussian"));
}

/// A polar stereographic grid of an Earth the profile cannot model.
#[test]
fn polar_of_another_earth_is_not_presented() {
    let m = rejection_with_section3("grib_polar.grib2", |s| s[14] = 200);
    assert_eq!(m, none_presented("earth"));
}

/// A bipolar polar stereographic grid.
#[test]
fn polar_bipolar_is_not_presented() {
    let m = rejection_with_section3("grib_polar.grib2", |s| s[63] = 0x40);
    assert_eq!(m, none_presented("earth"));
}

/// A polar stereographic grid whose LaD is in the other hemisphere.
#[test]
fn polar_of_the_other_hemisphere_is_not_presented() {
    let m = rejection_with_section3("grib_polar.grib2", |s| s[63] = 0x80);
    assert_eq!(m, none_presented("projection"));
}

/// A polar stereographic grid whose increments are missing.
#[test]
fn polar_without_increments_is_not_presented() {
    let m = rejection_with_section3("grib_polar.grib2", |s| set4(s, 56, u32::MAX));
    assert_eq!(m, none_presented("projection"));
}

/// A Mercator grid true to scale at a pole.
#[test]
fn mercator_at_a_pole_is_not_presented() {
    let m = rejection_with_section3("grib_mercator.grib2", |s| set4(s, 48, 90_000_000));
    assert_eq!(m, none_presented("projection"));
}

/// A Mercator grid of an Earth the profile cannot model.
#[test]
fn mercator_of_another_earth_is_not_presented() {
    let m = rejection_with_section3("grib_mercator.grib2", |s| s[14] = 200);
    assert_eq!(m, none_presented("earth"));
}

/// A rotated grid of an Earth the profile cannot model, which its CRS would need.
#[test]
fn rotated_of_another_earth_is_not_presented() {
    let m = rejection_with_section3("grib_rotated.grib2", |s| s[14] = 200);
    assert_eq!(m, none_presented("earth"));
}

/// A reduced grid whose list gives the points along meridians.
#[test]
fn reduced_along_meridians_is_not_presented() {
    let m = rejection_with_section3("grib_reduced_ll.grib2", |s| s[11] = 2);
    assert_eq!(m, none_presented("reduced"));
}

/// A reduced grid whose list does not add up to its points.
#[test]
fn reduced_of_another_sum_is_not_presented() {
    let m = rejection_with_section3("grib_reduced_ll.grib2", |s| {
        let at = 72 + usize::from(s[10]) - 1;
        s[at] += 1;
    });
    assert_eq!(m, none_presented("reduced"));
}

/// A reduced grid whose list is cut short: fewer numbers than rows.
#[test]
fn reduced_of_a_short_list_is_not_presented() {
    let m = rejection_with_section3("grib_reduced_ll.grib2", |s| {
        s.truncate(s.len() - 1);
    });
    assert_eq!(m, none_presented("reduced"));
}

/// A reduced grid whose rows do not go round the Earth: its last longitude moved.
#[test]
fn reduced_of_an_area_is_not_presented() {
    let m = rejection_with_section3("grib_reduced_ll.grib2", |s| set4(s, 60, 90_000_000));
    assert_eq!(m, none_presented("reduced"));
}

/// A reduced grid on a template that has no list.
#[test]
fn reduced_on_another_template_is_not_presented() {
    let m = rejection_with_section3("grib_polar.grib2", |s| {
        s[10] = 2;
        s[11] = 1;
    });
    assert_eq!(m, none_presented("reduced"));
}

/// An unstructured grid of no points.
#[test]
fn unstructured_of_no_points_is_not_presented() {
    let m = rejection_with_section3("grib_unstructured.grib2", |s| set4(s, 7, 0));
    assert_eq!(m, none_presented("grid_size"));
}

/// A section 3 too short for each new template (spec/zarrify/grib.md §8.4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#84-checks-and-rejection>).
#[test]
fn reject_short_sections_of_each_template() {
    for (name, want) in [
        ("grib_gaussian.grib2", "template 3.40 has 71 octets, fewer than 72"),
        ("grib_rotated.grib2", "template 3.1 has 83 octets, fewer than 84"),
        ("grib_polar.grib2", "template 3.20 has 64 octets, fewer than 65"),
        ("grib_mercator.grib2", "template 3.10 has 71 octets, fewer than 72"),
        ("grib_unstructured.grib2", "template 3.101 has 34 octets, fewer than 35"),
    ] {
        let need: usize = want.split("fewer than ").nth(1).unwrap().parse().unwrap();
        let m = rejection_with_section3(name, |s| s.truncate(need - 1));
        assert!(m.contains(&format!("a GRIB section 3 of {want}")), "{name}: {m}");
    }
}
