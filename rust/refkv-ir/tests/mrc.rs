//! The MRC parser (src/mrc.rs; spec/virtualize/mrc.md): one test per rejected fixture.

mod common;

use refkv_ir::mrc::Mrc;

// one test per rejected fixture, each the message its check gives
// one test per case of conformance/virtualize/rejects/mrc.json (build.rs), each the message its check gives
macro_rules! reject {
    ($name:ident, $input:literal, $msg:literal) => {
        common::reject!($name, Mrc, concat!("mrc/", $input), $msg);
    };
}
include!(concat!(env!("OUT_DIR"), "/rejects_mrc.rs"));
