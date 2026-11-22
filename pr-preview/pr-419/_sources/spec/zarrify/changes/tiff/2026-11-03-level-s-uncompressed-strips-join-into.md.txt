Changed 2026-11-03: a level's uncompressed strips join into chunks of at most 2^20 bytes and 2^10
strips ([§4.5](../../tiff.md#45-strips), [§7.3](../../tiff.md#73-chunks)) in place of one chunk a strip, so that an image of
one row a strip no longer has a key a row (d-v-b/refkv#195); a level in compressed strips, one
whose two strips do not fit, and one with a strip that does not hold exactly its rows keep a
chunk a strip. The output of a file with such a level changes, and its `omero` windows, which
sample a chunk, may too.
