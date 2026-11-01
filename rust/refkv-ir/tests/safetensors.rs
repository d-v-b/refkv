//! The safetensors parser (src/safetensors.rs; spec/zarrify/safetensors.md): every accepted fixture's IR rebuilds it and
//! presents its tensors, and one test per rejected fixture.

mod common;

use refkv_ir::safetensors::Safetensors;

#[test]
fn every_accepted_fixture_rebuilds_and_presents_its_arrays() {
    let dir = common::fixtures().join("safetensors");
    let mut names: Vec<_> = std::fs::read_dir(&dir).unwrap().map(|e| e.unwrap().path()).collect();
    names.sort();
    let mut seen = 0;
    for path in names.iter().filter(|p| !p.file_name().unwrap().to_str().unwrap().contains("reject")) {
        let data = std::fs::read(path).unwrap();
        let (ir, facts) = refkv_ir::parse_bytes::<Safetensors>(&data).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
        assert_eq!(common::rebuilt(&ir, &data), data, "{}", path.display());
        assert!(facts["arrays"].is_array(), "{}", path.display());
        seen += 1;
    }
    assert!(seen > 10);
}

// one test per case of conformance/zarrify/rejects/safetensors.json (build.rs), each the message its check gives
macro_rules! reject {
    ($name:ident, $input:literal, $msg:literal) => {
        common::reject!($name, Safetensors, concat!("safetensors/", $input), $msg);
    };
}
include!(concat!(env!("OUT_DIR"), "/rejects_safetensors.rs"));
