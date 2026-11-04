# phkv fixtures

`rust-deflate.phkv` is a standalone phkv store written by the reference writer (`rust/refkv-phkv`,
`refkv_phkv::Writer`) with DEFLATE for its sections and values (`Options { section_codec:
Codec::Deflate, codec: Codec::Deflate, ..Options::default() }`), to test that the TypeScript reader
(`js/src/phkv.ts`) reads what Rust writes. Its 44 keys:

- `zarr.json`, a small bytes value (inline); `big`, 20,000 bytes of `i % 253` (not inline, so
  deflated in the file); `ключ/键`, `unicode`;
- `concat`, a Concat of `data` source 0 (`0123456789`) bytes 5..9, the literal `-`, and the `url`
  source `blob.bin` bytes 1..3;
- `a/c/{i >> 6}/{i & 63}` for `i` in 0..40: a Range of `blob.bin`, bytes `30·i .. 30·i + 25`.

The `url` source `blob.bin` is not in the file: the test supplies its bytes, `(i * 7) & 255` for
byte `i`. The sources, in order: `url blob.bin`, `data 0123456789`.
