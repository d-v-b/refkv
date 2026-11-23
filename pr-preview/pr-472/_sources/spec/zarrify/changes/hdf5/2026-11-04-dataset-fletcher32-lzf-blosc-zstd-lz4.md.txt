Changed 2026-11-04: a dataset with Fletcher32, LZF, Blosc, zstd, LZ4 or bitshuffle is presented
([§3.1](../../hdf5.md#31-data-types), [§3.4](../../hdf5.md#34-the-codec-refkvh5_bitshuffle)), and a chunk an optional filter
skipped is read in that filter's literal form ([§3.2](../../hdf5.md#32-chunks)), so that `filter` and
`filter_mask` name only what remains.
