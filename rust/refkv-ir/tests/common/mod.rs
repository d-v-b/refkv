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
pub fn drive(r: &mut StoreRun, objects: &[impl AsRef<[u8]>]) -> refkv_ir::error::Res<()> {
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
                Err(m) => assert!(m.to_string().starts_with($msg), "{m}"),
                Ok(_) => panic!("{} was accepted", $file),
            }
        }
    };
}
pub(crate) use reject;

/// The TIFF parser's IR and description of a file, and refkv's plan of it (`tiff::plan::plan`),
/// of the profile the parser found: Err with the parser's rejection, or the plan's.
pub fn tiff_plan(data: &[u8]) -> refkv_ir::error::Res<(refkv_ir::ir::Ir, serde_json::Value, serde_json::Value)> {
    use refkv_ir::step::Step;
    let mut p = refkv_ir::tiff::Tiff::new(data.len() as u64);
    loop {
        match p.step()? {
            Step::Read(r) => {
                for (o, n) in r {
                    p.feed(o, data[o as usize..(o + n) as usize].to_vec());
                }
            }
            Step::Beside => return Err(refkv_ir::error::Error::rejected("the file names siblings, which its bytes alone cannot give")),
            Step::Done => break,
        }
    }
    let profile = if p.ndpi { "ndpi" } else if p.qptiff { "qptiff" } else { "tiff" };
    let (ir, description) = p.finish()?;
    let plan = refkv_ir::tiff::plan::plan(profile, &description)?;
    Ok((ir, description, plan))
}

/// A test that the TIFF parser or refkv's plan rejects the fixture `$file` with a message that
/// starts with `$msg` (the role the schema names).
macro_rules! reject_tiff {
    ($name:ident, $file:expr, $msg:expr) => {
        #[test]
        fn $name() {
            let data = std::fs::read($crate::common::fixtures().join($file)).unwrap();
            match $crate::common::tiff_plan(&data) {
                Err(m) => assert!(m.to_string().starts_with($msg), "{m}"),
                Ok(_) => panic!("{} was accepted", $file),
            }
        }
    };
}
pub(crate) use reject_tiff;
