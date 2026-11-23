# Zarr v3: stored arrays

What the Zarr v3 parser describes as stored arrays ([stored-array.md](../../stored-array.md)), part of [the Zarr v3 profile](../zarr3.md).

Every candidate of the store
([zarr3.md §2](../zarr3.md#2-nodes)) that is below no other described array, and
whose `zarr.json` the parser reads, as a JSON object whose `node_type` is `array` and whose
`shape` is a list of at most 32 nonnegative integers, is a stored array, whether or not the
profile presents it ([zarr3.md §3.3](../zarr3.md#33-nodes-that-are-not-presented)).
Listed, it is keyed by the object of its `zarr.json`; in the zip form
([zarr3.md §7.4](../zarr3.md#74-the-zip-form)) by the entry of its `zarr.json`
(`entries/<i>`); with no index either way, which a profile finds by the document's key. A store
parser describes its own arrays, each in that object ([stored-array.md §6](../../stored-array.md#6-in-the-ir)).

- **Declarations.** The `zarr.json`: listed, its object, whole; zipped, its entry's `data` element,
  as the IR holds it: the document's text, or, deflated, the stream, a derived element; but a
  document the IR does not hold as a node's (one below a node the profile leaves out,
  [zarr3.md §3.3](../zarr3.md#33-nodes-that-are-not-presented)) is its bytes as the
  zip file stores them, of a deflated one the stream as bytes. Its `data_type`, `fill_value` and
  `codecs` (the `bytes` codec's `endian`) say what the elements are.
- **`shape`** is the document's `shape`, in the order its leading `transpose` codecs give, whose
  `axes` then give the document's order: storage axis `j` is the document's axis `order[j]` after
  each in turn. Any `transpose` after another codec is a codec of the chain.
- **`item_bits`** is the bits of a data type of Zarr v3's core: 8 for `bool`, `int8` and `uint8`;
  16, 32 and 64 for the integers and floats of those sizes; 64 and 128 for `complex64` and
  `complex128`; `N` for `r<N>`. Any other data type (an extension's, such as `string`) gives none.
- **The grid** is the document's `chunk_grid`, under `full`: `regular`, each axis `Each` of its
  `chunk_shape`; `rectilinear` of the `inline` kind, an axis given as one size `Each` of it, and an
  axis given as a list the chunks it needs to cover the axis (each run cut there), `Each` where
  they are of one size and a `List` of them otherwise. A unit is a chunk object of the array that
  is not empty, by the chunk key encoding ([zarr3.md §3.1](../zarr3.md#31-chunk-grids-and-chunk-keys)):
  listed, its row `ranges`, the whole object by its key; zipped, its row the `data` element of its
  entry, which the zip file stores (method 0).
- **The chain** is one: a codec for each of the document's codecs after the leading transposes,
  but `bytes`, which lays the elements out as they are in the order its `endian` declares. `gzip`,
  `zstd` and `blosc`, and the `numcodecs.` codecs whose encodings the list names (as for Zarr v2, [zarr2/stored-arrays.md](../zarr2/stored-arrays.md):
  `numcodecs.zlib`, `numcodecs.gzip`, `numcodecs.bz2`, `numcodecs.zstd`, `numcodecs.blosc`,
  `numcodecs.lzma` of `format` 1 or none, and `numcodecs.shuffle`), are those entries. Any other
  is `source`, with `format` `zarr3`, `id` the codec's name, and `parameters` its configuration,
  where it has one: `crc32c`, `vlen-utf8`, and `sharding_indexed`, whose units are its shards. An
  array of no `item_bits` has a grid only where its chain begins with `source`.
- **The fill value.** The grid is sparse: a store may leave a chunk out. Its fill is `given`, a
  value, the bytes of the document's `fill_value` for a core data type: a boolean; an integer in
  the type's range; a float as a number (rounded to the type, never past its largest), `NaN`,
  `Infinity`, `-Infinity`, or `0x` and the hex digits of its bits; a complex number as a pair of
  those; `r<N>`'s bytes as a list of byte values. An element of more than one byte takes the order
  of the codec that lays the elements out: the `bytes` codec after the leading transposes, or the
  first of a leading `sharding_indexed` codec's inner codecs, after their transposes. A value
  this does not read, a data type outside the core, and an order no codec declares give no fill:
  the document among the declarations holds it.
- **No grid** for an array whose document has another chunk grid or one whose configuration is
  not the grid's, a rectilinear axis whose chunks pass its end and are not all of one size (its
  last chunk is stored whole, which a `List` does not say), a chunk key encoding other than
  `default` and `v2` with a separator `.` or `/`, `storage_transformers`, or `codecs` that are not
  a nonempty list of codecs each named; and, zipped, an array of which the zip file compresses a
  chunk: its unit is no bytes of the file as its chain takes them.
- **Not described**: a candidate whose `zarr.json` is empty, too large to read, not JSON, not an
  array's, or has no shape as above, or is below a described array. A group declares no array.
