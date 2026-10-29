//! refkv's intermediate representation (design/ARCHITECTURE.md §3
//! <https://github.com/d-v-b/refkv/blob/main/design/ARCHITECTURE.md#3-proposal-an-intermediate-representation>),
//! in Rust: a compact columnar IR with runs, its invariants, sans-IO parsers, and the checker of a
//! source model schema. The core has no I/O and builds for wasm32; the Python
//! bindings are a crate of their own (rust/refkv-python/src/lib.rs, the module `refkv._core`).

pub mod canon;
pub mod transform;
pub mod check;
pub mod coding;
pub mod cxml;
pub mod expr;
pub mod fed;
pub mod ir;
pub mod json;
pub mod lv;
pub mod lzw;
pub mod mirror;
pub mod fits_tiles;
pub mod out;
pub mod plan;
pub mod project;
pub mod record;
pub mod refs;
pub mod rules;
pub mod run;
pub mod sans;
pub mod schema;
pub mod shard;
pub mod store;
pub mod text;
pub mod types;
pub mod xml;

#[cfg(target_arch = "wasm32")]
pub mod wasm;

// Every profile's module (src/<profile>.rs or src/<profile>/mod.rs), by the registry
// (profiles/; build.rs): a format's own modules, such as its codecs, are its module's.
include!(concat!(env!("OUT_DIR"), "/modules.rs"));

/// A sans-IO parser: `step` gives the next batch of ranges to read (or done),
/// `feed` gives one range's bytes, `finish` the IR and the facts its projection reads.
pub trait Parser {
    fn new(size: u64) -> Self;
    fn step(&mut self) -> Result<nd2::Step, String>;
    fn feed(&mut self, offset: u64, data: Vec<u8>);
    fn finish(self) -> Result<(ir::Ir, serde_json::Value), String>;
    /// The profile the parser found the file to be, when the parser decides it (the TIFF
    /// parser's: TIFF, NDPI or QPTIFF); else None, and the run keeps its format.
    fn found(&self) -> Option<&'static str> {
        None
    }
}

/// `Parser` for a parser whose inherent methods are the trait's (`crate::parser!(Nd2);`, in the
/// parser's module).
macro_rules! parser {
    ($t:ty) => {
        impl $crate::Parser for $t {
            fn new(size: u64) -> Self {
                <$t>::new(size)
            }
            fn step(&mut self) -> Result<$crate::nd2::Step, String> {
                <$t>::step(self)
            }
            fn feed(&mut self, offset: u64, data: Vec<u8>) {
                <$t>::feed(self, offset, data)
            }
            fn finish(self) -> Result<($crate::ir::Ir, serde_json::Value), String> {
                <$t>::finish(self)
            }
        }
    };
}
pub(crate) use parser;

/// Runs a parser over bytes in memory (tests, tools).
pub fn parse_bytes<P: Parser>(data: &[u8]) -> Result<(ir::Ir, serde_json::Value), String> {
    let mut p = P::new(data.len() as u64);
    loop {
        match p.step()? {
            nd2::Step::Read(r) => {
                for (o, n) in r {
                    p.feed(o, data[o as usize..(o + n) as usize].to_vec());
                }
            }
            nd2::Step::Done => break,
        }
    }
    p.finish()
}
