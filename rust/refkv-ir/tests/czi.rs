//! The CZI parser (src/czi.rs; spec/zarrify/czi.md): one test per rejected fixture, and a
//! segment id read as its 16 bytes.

mod common;

use common::fixtures;
use refkv_ir::czi::Czi;
use refkv_ir::parse_bytes;

// one test per rejected fixture, each the message its check gives
// one test per case of conformance/zarrify/rejects/czi.json (build.rs), each the message its check gives
macro_rules! reject {
    ($name:ident, $input:literal, $msg:literal) => {
        common::reject!($name, Czi, concat!("czi/", $input), $msg);
    };
}
include!(concat!(env!("OUT_DIR"), "/rejects_czi.rs"));

#[test]
fn a_segment_id_with_bytes_after_its_nul_is_not_the_id() {
    // spec/zarrify/czi.md §2.1
    // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#21-segments>: the 16 bytes of
    // the id, NUL-padded; a byte after the NUL makes another id
    let mut data = std::fs::read(fixtures().join("czi/czi_gray8_single.czi")).unwrap();
    let at = (0..data.len() - 16).find(|&k| data[k..k + 15] == *b"ZISRAWSUBBLOCK\0").unwrap();
    data[at + 15] = 0xDF;
    match parse_bytes::<Czi>(&data) {
        Err(m) => assert!(m.contains("is not a ZISRAWSUBBLOCK segment"), "{m}"),
        Ok(_) => panic!("accepted"),
    }
}
