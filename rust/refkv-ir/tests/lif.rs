//! The LIF parser (src/lif.rs; spec/zarrify/lif.md): one test per rejected fixture.

mod common;

use refkv_ir::lif::Lif;

// one test per case of conformance/zarrify/rejects/lif.json (build.rs), each the message its check gives
macro_rules! reject {
    ($name:ident, $input:literal, $msg:literal) => {
        common::reject!($name, Lif, concat!("lif/", $input), $msg);
    };
}
include!(concat!(env!("OUT_DIR"), "/rejects_lif.rs"));

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
