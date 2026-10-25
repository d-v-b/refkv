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
