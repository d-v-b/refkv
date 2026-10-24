//! The FITS parser (src/fits.rs; spec/virtualize/fits.md): one test per rejected fixture.

mod common;

use refkv_ir::fits::Fits;

// FITS (spec/virtualize/fits.md §9.3
// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/fits.md#93-checks>): one per rejected
// fixture, each the message its check gives.
// one test per case of conformance/virtualize/rejects/fits.json (build.rs), each the message its check gives
macro_rules! reject {
    ($name:ident, $input:literal, $msg:literal) => {
        common::reject!($name, Fits, concat!("fits/", $input), $msg);
    };
}
include!(concat!(env!("OUT_DIR"), "/rejects_fits.rs"));
