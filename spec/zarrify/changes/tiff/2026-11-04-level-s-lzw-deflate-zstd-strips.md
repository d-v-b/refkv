Changed 2026-11-04: a level's LZW, Deflate and zstd strips are the inner chunks of shards of at most
2^10 strips and 2^30 decoded bytes ([§4.5](../../tiff.md#45-strips), [zarrify §2.1](../../../zarrify.md#21-virtual-shards))
in place of one chunk a strip, so that an image of one compressed row a strip no longer has a key a
row (d-v-b/refkv#195). The output of a file with such a level changes; its pixels and its `omero`
windows, which sample one strip, do not. A short last strip of Deflate or zstd is still rejected.
