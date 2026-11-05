Changed 2026-10-27: an LZW tile's or strip's codec is `refkv.tiff_lzw`, refkv's own, in place of
`imagecodecs_lzw` (imagecodecs-zarr's codec of that name reads the whole stream), and a Deflate
one's `numcodecs.zlib`, in place of `zlib` ([conventions §3](../../../conventions.md#3-arrays),
d-v-b/refkv#90).
