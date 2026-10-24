//! What the per-format parser tests share: the fixtures, a rebuild from an IR's leaves, and
//! `reject!`, one test per rejected fixture.
#![allow(dead_code, unused_macros, unused_imports)]

use std::path::{Path, PathBuf};

pub fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../fixtures")
}

/// The source's bytes again, from the leaves of its IR.
pub fn rebuilt(ir: &refkv_ir::ir::Ir, data: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    for (o, n) in refkv_ir::check::leaves(ir).unwrap() {
        out.extend_from_slice(&data[o as usize..(o + n) as usize]);
    }
    out
}

/// A test that parser `$p` rejects the fixture `$file` with a message that starts with `$msg`
/// (the role the schema names).
macro_rules! reject {
    ($name:ident, $p:ty, $file:expr, $msg:expr) => {
        #[test]
        fn $name() {
            let data = std::fs::read($crate::common::fixtures().join($file)).unwrap();
            match refkv_ir::parse_bytes::<$p>(&data) {
                Err(m) => assert!(m.starts_with($msg), "{m}"),
                Ok(_) => panic!("{} was accepted", $file),
            }
        }
    };
}
pub(crate) use reject;
