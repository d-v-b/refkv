//! What the per-format parser tests share: the fixtures, a rebuild from an IR's leaves,
//! `reject!`, one test per rejected fixture, and a store in memory: its files, listing and run.
#![allow(dead_code, unused_macros, unused_imports)]

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use refkv_ir::store::StoreRun;

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

/// A directory's regular files, by `/`-separated key, keys in byte order.
pub fn walk(root: &Path) -> BTreeMap<String, Vec<u8>> {
    let mut out = BTreeMap::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(d) = stack.pop() {
        for e in std::fs::read_dir(&d).unwrap() {
            let e = e.unwrap();
            let t = e.file_type().unwrap();
            if t.is_dir() {
                stack.push(e.path());
            } else if t.is_file() {
                let rel = e.path().strip_prefix(root).unwrap().to_path_buf();
                let k = rel.iter().map(|c| c.to_str().unwrap()).collect::<Vec<_>>().join("/");
                out.insert(k, std::fs::read(e.path()).unwrap());
            }
        }
    }
    out
}

/// A store's listing, (key, size) in the order of `files`.
pub fn listing(files: &BTreeMap<String, Vec<u8>>) -> Vec<(String, u64)> {
    files.iter().map(|(k, v)| (k.clone(), v.len() as u64)).collect()
}

/// Answers each read a store run asks for from `objects` (its listing's bytes, by index) until
/// it has no more to ask.
pub fn drive(r: &mut StoreRun, objects: &[impl AsRef<[u8]>]) -> Result<(), String> {
    while let Some(reqs) = r.poll()? {
        for (k, q) in reqs.iter().enumerate() {
            let b = objects[q.object].as_ref();
            r.complete(k, q.spans.iter().map(|&(a, e)| b[a as usize..e as usize].to_vec()).collect())?;
        }
    }
    Ok(())
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
