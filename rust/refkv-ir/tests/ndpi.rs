//! The NDPI profile (spec/virtualize/ndpi.md), read by the TIFF parser: one test per rejected
//! fixture.

mod common;

use refkv_ir::tiff::Tiff;

// one test per rejected fixture, each the message its check gives
// one test per case of conformance/virtualize/rejects/ndpi.json (build.rs), each the message its check gives
macro_rules! reject {
    ($name:ident, $input:literal, $msg:literal) => {
        common::reject!($name, Tiff, concat!("ndpi/", $input), $msg);
    };
}
include!(concat!(env!("OUT_DIR"), "/rejects_ndpi.rs"));
