//! The safetensors parser (src/safetensors.rs; spec/zarrify/safetensors.md): every accepted fixture's IR rebuilds it and
//! presents its tensors, and one test per rejected fixture.

mod common;

use refkv_ir::safetensors::plan::{plan, plan_store};
use refkv_ir::safetensors::Safetensors;

#[test]
fn every_accepted_fixture_rebuilds_and_presents_its_arrays() {
    let dir = common::fixtures().join("safetensors");
    let mut names: Vec<_> = std::fs::read_dir(&dir).unwrap().map(|e| e.unwrap().path()).collect();
    names.sort();
    let mut seen = 0;
    for path in names.iter().filter(|p| p.is_file() && !p.file_name().unwrap().to_str().unwrap().contains("reject")) {
        let data = std::fs::read(path).unwrap();
        let (ir, description) = refkv_ir::parse_bytes::<Safetensors>(&data).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
        assert_eq!(common::rebuilt(&ir, &data), data, "{}", path.display());
        assert!(plan(&description).unwrap()["arrays"].is_array(), "{}", path.display());
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

/// A header's description (a file's, or a shard's in a store) against the stored arrays its
/// `elements.header` keys: one for each tensor, at its position, of its shape, its one row its
/// data's element where it has bytes; and each tensor refkv's plan presents gives its index and its
/// element's bits. The tensors and the presented ones counted.
fn check_header(ir: &refkv_ir::ir::Ir, header: &serde_json::Value, at: &str) -> (usize, usize) {
    let s = &ir.stored;
    let key = header["elements"]["header"].as_u64().unwrap_or_else(|| panic!("{at}: no header element")) as u32;
    assert_eq!(ir.path(key).rsplit('/').next(), Some("header"), "{at}");
    let described = keyed(ir, key, at);
    let tensors = header["tensors"].as_array().unwrap();
    assert_eq!(described.len(), tensors.len(), "{at}");
    let shape = |v: &serde_json::Value| v.as_array().unwrap().iter().map(|n| n.as_u64().unwrap()).collect::<Vec<_>>();
    for (i, (t, &k)) in tensors.iter().zip(&described).enumerate() {
        assert_eq!(s.arrays[k].shape, Some(shape(&t["shape"])), "{at} {}", t["name"]);
        let rows: Vec<Option<u32>> = (0..s.map.len()).filter(|&r| s.map.array[r] == k as u32).map(|r| s.map.element[r]).collect();
        let data: Vec<Option<u32>> = header["elements"]["tensors"][i].as_u64().map(|d| Some(d as u32)).into_iter().collect();
        assert_eq!(rows, data, "{at} {}", t["name"]);
    }
    let arrays = plan(header).unwrap()["arrays"].as_array().unwrap().clone();
    for a in &arrays {
        let k = described[a["index"].as_u64().unwrap() as usize];
        assert_eq!(s.arrays[k].item_bits, a["bits"].as_u64(), "{at} {}", a["path"]);
    }
    (tensors.len(), arrays.len())
}

/// Each accepted fixture's description names its tensors' stored arrays by their key, the element
/// of its header (spec/stored-array.md §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>), and no
/// description has another key.
#[test]
fn every_accepted_fixtures_description_names_its_stored_arrays() {
    let (mut tensors, mut presented) = (0, 0);
    for e in std::fs::read_dir(common::fixtures().join("safetensors")).unwrap() {
        let p = e.unwrap().path();
        let name = p.file_name().unwrap().to_str().unwrap().to_string();
        if !p.is_file() || name.contains("reject") {
            continue;
        }
        let (ir, description) = refkv_ir::parse_bytes::<Safetensors>(&std::fs::read(&p).unwrap()).unwrap();
        assert_eq!(ir.path(description["elements"]["header"].as_u64().unwrap() as u32), "header", "{name}");
        let (t, a) = check_header(&ir, &description, &name);
        assert_eq!(ir.stored.arrays.len(), t, "{name}");
        tensors += t;
        presented += a;
    }
    assert!(tensors >= 40 && presented >= 35 && presented < tensors, "{tensors} tensors, {presented} presented");
}

/// In the store form, every shard whose header passes the checks is described in place, presented
/// or not, its tensors keyed by its own `header`; a shard whose header fails a check has none.
#[test]
fn a_stores_description_names_each_shards_stored_arrays() {
    use refkv_ir::store::{run_store, StoreFormat};
    let (mut presented, mut unpresented, mut none) = (0, 0, 0);
    for store in ["st_store_model", "st_store_mixed"] {
        let files = common::walk(&common::fixtures().join("safetensors").join(store));
        let data: Vec<&Vec<u8>> = files.values().collect();
        let listing = common::listing(&files);
        let (ir, description, _) = run_store(StoreFormat::Safetensors, listing.clone(), &|k| data[k].clone()).unwrap();
        let planned = plan_store(&description, &listing).unwrap();
        let mut described = 0;
        for (shard, file) in description["shards"].as_array().unwrap().iter().zip(planned["own"]["files"].as_array().unwrap()) {
            let name = shard["key"].as_str().unwrap();
            let at = format!("{store} {name}");
            assert_eq!(file["key"], shard["key"], "{at}");
            if shard.get("error").is_some() {
                assert_eq!(file["reason"], shard["error"], "{at}");
                none += 1;
                continue;
            }
            assert_eq!(ir.path(shard["header"]["elements"]["header"].as_u64().unwrap() as u32), format!("{name}/header"), "{at}");
            described += check_header(&ir, &shard["header"], &at).0;
            if file["path"].is_string() { presented += 1 } else { unpresented += 1 }
        }
        assert_eq!(ir.stored.arrays.len(), described, "{store}");
    }
    assert_eq!((presented, unpresented, none), (5, 1, 1));
}

// one test per case of conformance/zarrify/rejects/safetensors.json (build.rs), each the message its check gives
macro_rules! reject {
    ($name:ident, $input:literal, $msg:literal) => {
        common::reject!($name, Safetensors, concat!("safetensors/", $input), $msg);
    };
}
include!(concat!(env!("OUT_DIR"), "/rejects_safetensors.rs"));
