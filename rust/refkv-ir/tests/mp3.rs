//! The MP3 parser (src/mp3.rs; spec/zarrify/mp3.md): one test per rejected fixture.

mod common;

use refkv_ir::mp3::Mp3;

// one test per rejected fixture, each the message its check gives
// one test per case of conformance/zarrify/rejects/mp3.json (build.rs), each the message its check gives
macro_rules! reject {
    ($name:ident, $input:literal, $msg:literal) => {
        common::reject!($name, Mp3, concat!("mp3/", $input), $msg);
    };
}
include!(concat!(env!("OUT_DIR"), "/rejects_mp3.rs"));
