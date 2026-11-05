Changed 2026-11-04: an array that cannot be presented is left out and recorded where it rejected
the store, unless it is the root ([§3.4](../../zarr2.md#34-arrays-that-are-not-presented)). The data types are
[NPY's table](../../npy.md#3-data-types), so `|S<n>`, `|V<n>` and `U`, complex, date-time and structured
types are presented, with their fill values ([§3](../../zarr2.md#3-arrays)); `|O` is presented with the filter
`vlen-utf8`, and `fixedscaleoffset` and `quantize` are presented ([§3.3](../../zarr2.md#33-filters)); the
compressors `lz4`, `bz2` and `lzma` are presented ([§3.1](../../zarr2.md#31-compressors)).
