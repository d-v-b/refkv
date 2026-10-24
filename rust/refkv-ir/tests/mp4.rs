//! The MP4 parser (src/mp4.rs; spec/virtualize/mp4.md): one test per rejected fixture.

mod common;

use refkv_ir::mp4::Mp4;

// one test per rejected fixture, each the message its check gives
// one test per case of conformance/virtualize/rejects/mp4.json (build.rs), each the message its check gives
macro_rules! reject {
    ($name:ident, $input:literal, $msg:literal) => {
        common::reject!($name, Mp4, concat!("mp4/", $input), $msg);
    };
}
include!(concat!(env!("OUT_DIR"), "/rejects_mp4.rs"));
