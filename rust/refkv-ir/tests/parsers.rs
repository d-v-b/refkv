//! The file parsers (every file profile of the registry, spec/zarrify/profiles/): every
//! fixture they accept is a valid IR that rebuilds its source, and the fixtures named as rejected
//! are. Each format's own tests, one per rejected fixture among them, are tests/<format>.rs.

mod common;

use common::{fixtures, rebuilt};
use refkv_ir::check;
use refkv_ir::run::{run_bytes, Format};
use std::path::Path;

/// The files under `dir` (its subdirectories' too) with one of `suffixes`, sorted.
fn files(dir: &Path, suffixes: &[&str]) -> Vec<std::path::PathBuf> {
    let mut out = Vec::new();
    for e in std::fs::read_dir(dir).unwrap() {
        let path = e.unwrap().path();
        if path.is_dir() {
            out.extend(files(&path, suffixes));
        } else if suffixes.iter().any(|s| path.to_string_lossy().ends_with(s)) {
            out.push(path);
        }
    }
    out.sort();
    out
}

/// Every file profile's fixtures (fixtures/<p>, the files of the registry's suffixes): one
/// named as rejected (`unsupported_*`, `*_reject_*`; conformance/zarrify/HARNESS.md) is
/// rejected, and every other is accepted as a valid IR that rebuilds its source.
#[test]
fn accepted_fixtures_are_valid_irs_that_rebuild() {
    for e in refkv_ir::out::registry().iter().filter(|e| e["kind"] == "file") {
        let name = e["name"].as_str().unwrap();
        let f = Format::from_name(name).unwrap_or_else(|| panic!("{name}: no parser"));
        if f.store_run() {
            continue; // a file input read as a store run (VSI): tests/vsi.rs drives it with its tile files
        }
        let suffixes: Vec<&str> = e["suffixes"].as_array().unwrap().iter().map(|s| s.as_str().unwrap()).collect();
        let mut accepted = 0;
        for path in files(&fixtures().join(name), &suffixes) {
            let file = path.file_name().unwrap().to_string_lossy().into_owned();
            let rejected = file.contains("reject") || file.starts_with("unsupported");
            let data = std::fs::read(&path).unwrap();
            match run_bytes(f, &data) {
                Ok((ir, _, _)) => {
                    assert!(!rejected, "{path:?} was accepted");
                    check::check(&ir).unwrap_or_else(|m| panic!("{path:?}: {m}"));
                    assert!(rebuilt(&ir, &data) == data, "{path:?} does not rebuild");
                    accepted += 1;
                }
                Err(m) => assert!(rejected, "{path:?} was rejected: {m}"),
            }
        }
        assert!(accepted > 0, "{name}: no fixture accepted");
    }
}
