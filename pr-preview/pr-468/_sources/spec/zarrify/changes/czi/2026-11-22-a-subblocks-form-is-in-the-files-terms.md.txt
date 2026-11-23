Changed 2026-11-22: a placed subblock's `data` form is in the file's terms
([§5.3](../../czi.md#53-elements)): its geometry gives its shape, pixel type and compression with
no Zarr data type, and it has a codec only when its codec header says more than its compression
(`{"lossless": true}` for a lossless JpgFile stream, `{"hi_lo_byte_unpacking": true}` for Zstd1's
hi-lo packing), in place of the Zarr codec chain. The projection decides the data type and codecs,
as it did. The IR mirror of every file with a placed subblock changes; no other output does.
