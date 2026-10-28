//! Writes the standalone store that js/test/phkv.test.ts reads through the wasm32 reader:
//! `cargo run --example wasm_fixture -- <path> [object-url]`. The tests know the values by their
//! formulas, so the two must agree: `big` and `object` below, and the keys. The references name
//! `object-url` (default `https://data.test/object`; demo/e2e_phkv.mjs gives a relative one).

use refkv_phkv::{Codec, Options, Writer};
use refkv_vzip::pb::Range;

fn main() {
    let mut args = std::env::args().skip(1);
    let path = args.next().expect("usage: wasm_fixture <path> [object-url]");
    let object_url = args.next().unwrap_or_else(|| "https://data.test/object".into());
    // the 400,000 bytes of a/plain, a/deflate and a/zstd: i % 97, as 32-bit little-endian
    let big: Vec<u8> = (0..100_000u32).flat_map(|i| (i % 97).to_le_bytes()).collect();
    let opts = Options { inline_max: 64, ..Options::default() };
    let mut w = Writer::new(Vec::new(), opts);
    w.add_bytes("a/zarr.json", b"{\"zarr_format\":3}").unwrap();
    w.add_bytes_with("a/plain", &big, Codec::None).unwrap();
    w.add_bytes_with("a/deflate", &big, Codec::Deflate).unwrap();
    w.add_bytes_with("a/zstd", &big, Codec::Zstd).unwrap();
    // the object holds 5000 bytes: i % 256
    let url = w.url(&object_url).unwrap();
    w.add_ranges("a/ref", &[Range::of(url, 100, 300)]).unwrap();
    w.add_ranges("a/concat", &[Range::of(url, 0, 10), Range::literal(b"--".to_vec()), Range::of(url, 4990, 10)]).unwrap();
    let inner = w.internal("a/plain").unwrap();
    w.add_ranges("a/via-key", &[Range::of(inner, 4, 8)]).unwrap();
    for i in 0..3000 {
        w.add_bytes(&format!("many/{i:05}"), format!("value {i}").as_bytes()).unwrap();
    }
    std::fs::write(path, w.close().unwrap()).unwrap();
}
