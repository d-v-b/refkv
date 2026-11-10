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
    for path in names.iter().filter(|p| p.is_file() && !p.file_name().unwrap().to_str().unwrap().contains("reject")) {
        let data = std::fs::read(path).unwrap();
        let (ir, facts) = refkv_ir::parse_bytes::<Safetensors>(&data).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
        assert_eq!(common::rebuilt(&ir, &data), data, "{}", path.display());
        assert!(facts["arrays"].is_array(), "{}", path.display());
        seen += 1;
    }
    assert!(seen > 10);
}

/// The descriptions keyed by element `key`, in the IR's order, their indexes checked: 0 to n − 1.
fn keyed(ir: &refkv_ir::ir::Ir, key: u32, at: &str) -> Vec<usize> {
    let s = &ir.stored;
    let described: Vec<usize> = (0..s.arrays.len()).filter(|&k| s.arrays[k].element == key).collect();
    let indexes: Vec<Option<u64>> = described.iter().map(|&k| s.arrays[k].index).collect();
    assert_eq!(indexes, (0..described.len() as u64).map(Some).collect::<Vec<_>>(), "{at}");
    described
}

/// A file's facts (a shard's, in a store) against the descriptions its `declared_by` keys: one for
/// each tensor of `own.tensors`, at its position, of its shape; and each presented tensor's entry
/// of `arrays` gives its index, its element's bits and, where it has bytes, the element of its one
/// row. The tensors and the presented ones counted.
fn check_facts(ir: &refkv_ir::ir::Ir, facts: &serde_json::Value, at: &str) -> (usize, usize) {
    let s = &ir.stored;
    let key = facts["declared_by"].as_u64().unwrap_or_else(|| panic!("{at}: no declared_by")) as u32;
    assert_eq!(ir.path(key).rsplit('/').next(), Some("header"), "{at}");
    let described = keyed(ir, key, at);
    let tensors = facts["own"]["tensors"].as_array().unwrap();
    assert_eq!(described.len(), tensors.len(), "{at}");
    let shape = |v: &serde_json::Value| v.as_array().unwrap().iter().map(|n| n.as_u64().unwrap()).collect::<Vec<_>>();
    for (t, &k) in tensors.iter().zip(&described) {
        assert_eq!(s.arrays[k].shape, Some(shape(&t["shape"])), "{at} {}", t["name"]);
    }
    let arrays = facts["arrays"].as_array().unwrap();
    for a in arrays {
        let at = format!("{at} {}", a["path"]);
        let k = described[a["index"].as_u64().unwrap() as usize];
        assert_eq!(tensors[a["index"].as_u64().unwrap() as usize]["path"], a["path"], "{at}");
        assert_eq!(s.arrays[k].shape, Some(shape(&a["shape"])), "{at}");
        assert_eq!(s.arrays[k].item_bits, a["bits"].as_u64(), "{at}");
        let rows: Vec<Option<u32>> = (0..s.map.len()).filter(|&r| s.map.array[r] == k as u32).map(|r| s.map.element[r]).collect();
        let data: Vec<Option<u32>> = a["data"].as_u64().map(|d| Some(d as u32)).into_iter().collect();
        assert_eq!(rows, data, "{at}");
    }
    (tensors.len(), arrays.len())
}

/// Each accepted fixture's facts name its tensors' descriptions by their key, the element of its
/// header (spec/stored-array.md §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>), and no
/// description has another key.
#[test]
fn every_accepted_fixtures_facts_name_their_descriptions() {
    let (mut tensors, mut presented) = (0, 0);
    for e in std::fs::read_dir(common::fixtures().join("safetensors")).unwrap() {
        let p = e.unwrap().path();
        let name = p.file_name().unwrap().to_str().unwrap().to_string();
        if !p.is_file() || name.contains("reject") {
            continue;
        }
        let (ir, facts) = refkv_ir::parse_bytes::<Safetensors>(&std::fs::read(&p).unwrap()).unwrap();
        assert_eq!(ir.path(facts["declared_by"].as_u64().unwrap() as u32), "header", "{name}");
        let (t, a) = check_facts(&ir, &facts, &name);
        assert_eq!(ir.stored.arrays.len(), t, "{name}");
        tensors += t;
        presented += a;
    }
    assert!(tensors >= 40 && presented >= 35 && presented < tensors, "{tensors} tensors, {presented} presented");
}

/// In the store form, the facts' `declared_by` gives, for each shard of `own.files`, the element
/// that keys its tensors' descriptions: a presented shard's `header`, as the shard's own facts
/// give it; the object of a shard that is not presented for its key, whose rows are ranges of it;
/// and none for a shard whose header fails a check.
#[test]
fn a_stores_facts_name_each_shards_descriptions() {
    use refkv_ir::store::{run_store, StoreFormat};
    let (mut headers, mut objects, mut none) = (0, 0, 0);
    for store in ["st_store_model", "st_store_mixed"] {
        let files = common::walk(&common::fixtures().join("safetensors").join(store));
        let data: Vec<&Vec<u8>> = files.values().collect();
        let (ir, facts, _) = run_store(StoreFormat::Safetensors, common::listing(&files), &|k| data[k].clone()).unwrap();
        let s = &ir.stored;
        let listed = facts["own"]["files"].as_array().unwrap();
        let keys = facts["declared_by"].as_array().unwrap();
        assert_eq!(keys.len(), listed.len(), "{store}");
        let mut described = 0;
        for (f, key) in listed.iter().zip(keys) {
            let name = f["key"].as_str().unwrap();
            let at = format!("{store} {name}");
            let Some(key) = key.as_u64().map(|k| k as u32) else {
                // its header fails a check: the reason is the check's message
                assert!(!["name:", "reserved:", "path:"].iter().any(|r| f["reason"].as_str().unwrap().starts_with(r)), "{at}");
                none += 1;
                continue;
            };
            if f["path"].is_string() {
                let shard = facts["shards"].as_array().unwrap().iter().find(|s| s["key"] == f["key"]).unwrap();
                assert_eq!(shard["facts"]["declared_by"].as_u64(), Some(key as u64), "{at}");
                assert_eq!(ir.path(key), format!("{name}/header"), "{at}");
                described += check_facts(&ir, &shard["facts"], &at).0;
                headers += 1;
            } else {
                assert!(f["reason"].as_str().unwrap().starts_with("name:"), "{at}");
                assert_eq!((ir.kind[key as usize], ir.path(key)), (refkv_ir::ir::OBJECT, name.to_string()), "{at}");
                let mine = keyed(&ir, key, &at);
                assert!(!mine.is_empty(), "{at}");
                for &k in &mine {
                    let rows: Vec<usize> = (0..s.map.len()).filter(|&r| s.map.array[r] == k as u32).collect();
                    assert!(rows.iter().all(|&r| s.map.element[r] == Some(key) && s.map.range[r].is_some()), "{at}");
                }
                described += mine.len();
                objects += 1;
            }
        }
        assert_eq!(s.arrays.len(), described, "{store}");
    }
    assert_eq!((headers, objects, none), (5, 1, 1));
}

// one test per case of conformance/zarrify/rejects/safetensors.json (build.rs), each the message its check gives
macro_rules! reject {
    ($name:ident, $input:literal, $msg:literal) => {
        common::reject!($name, Safetensors, concat!("safetensors/", $input), $msg);
    };
}
include!(concat!(env!("OUT_DIR"), "/rejects_safetensors.rs"));
