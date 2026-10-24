//! The NIfTI parser (src/nifti.rs; spec/virtualize/nifti.md): one test per rejected fixture.

mod common;

use refkv_ir::nifti::Nifti;

// one test per rejected fixture, each the message its check gives
// one test per case of conformance/virtualize/rejects/nifti.json (build.rs), each the message its check gives
macro_rules! reject {
    ($name:ident, $input:literal, $msg:literal) => {
        common::reject!($name, Nifti, concat!("nifti/", $input), $msg);
    };
}
include!(concat!(env!("OUT_DIR"), "/rejects_nifti.rs"));
