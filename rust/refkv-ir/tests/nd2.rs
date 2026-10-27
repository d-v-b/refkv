//! The ND2 parser (src/nd2.rs; spec/zarrify/nd2.md): one test per rejected fixture.

mod common;

use refkv_ir::nd2::Nd2;

// one test per case of conformance/zarrify/rejects/nd2.json (build.rs), each the message its check gives
macro_rules! reject {
    ($name:ident, $input:literal, $msg:literal) => {
        common::reject!($name, Nd2, concat!("nd2/", $input), $msg);
    };
}
include!(concat!(env!("OUT_DIR"), "/rejects_nd2.rs"));
