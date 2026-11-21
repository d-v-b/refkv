# Zarr v2: stored arrays

What the Zarr v2 parser describes as stored arrays ([stored-array.md](../../stored-array.md)), part of [the Zarr v2 profile](../zarr2.md).

Every array node of the store
([zarr2.md §2](../zarr2.md#2-nodes)) whose `.zarray` the parser reads, as a JSON
document whose `shape` is a list of at most 32 nonnegative integers, is a stored array, whether or
not the profile presents it ([zarr2.md §3.4](../zarr2.md#34-arrays-that-are-not-presented)):
the store declares an array there, and nothing above it. It is keyed by the object of its
`.zarray`, with no index, which a profile finds by the document's key. A store parser describes
its own arrays, each in that object ([stored-array.md §6](../../stored-array.md#6-in-the-ir)). The same parser reads an
[OME-Zarr](../ome-zarr.md) store, whose arrays it describes alike.

- **Declarations.** The `.zarray` object, whole: its `dtype`, `fill_value`, `filters` and
  `compressor` say what the elements are; the description reads them only as far as this
  paragraph says.
- **`shape`** is the document's `shape`, reversed for an `order` of `F` and two or more axes, whose
  `axes` then give the document's order: Zarr v2 stores each chunk in its array's order, and its
  chunk keys name the chunks in the document's order of the axes.
- **`item_bits`** is eight times the size of an element of `dtype` as NumPy lays it out, read as a
  NumPy header's `descr` is (the NumPy paragraph above), a structure's fields given as lists: absent
  for `|O`, an object array's, and for any type of no size NumPy knows.
- **The grid** is the document's `chunks`, each axis `Each` of its chunk length, under `full`: Zarr
  v2 stores a chunk past an axis's end whole. A unit is a chunk object of the array that is not
  empty, its row `ranges`, the whole object by its key: the key below the array's path, its
  indexes canonical and below each axis's count of chunks, joined by `dimension_separator` (`.`
  when absent or null), `0` for an array of no axes ([zarr2.md §3.2](../zarr2.md#32-chunks)).
  An empty object holds no unit: a reader cannot decode it, and the profile references none.
- **The chain** is one: a codec for each filter, in order, then one for the compressor. A codec
  whose encoding the list names is that entry: `zlib`, `gzip`, `bz2` (as `bzip2`), `zstd`,
  `blosc`, `lzma` of `format` 1 or none (an `.xz` stream, as `xz`) and `shuffle`, its
  `element_size` the filter's `elementsize`, a value. Any other is `source`, with `format`
  `zarr2`, `id` the codec's `id`, and `parameters` its other members as written, where it has any
  (numcodecs' `lz4`, which frames its block with its length, and the filters that take the array's
  elements: `delta`, `fixedscaleoffset`, `quantize`, `vlen-utf8` and the rest). An array of no
  `item_bits` has a grid only where its chain begins with `source`, which makes its elements
  (`vlen-utf8`, of an object array's strings).
- **The fill value.** The grid is sparse: a store may leave a chunk out. Its fill is absent for a
  null `fill_value`, which Zarr v2 reads as none; otherwise it is `given`, a value, the bytes NumPy
  makes of it in `dtype`, in its byte order: a boolean; an integer in the type's range; a float as
  a number (rounded to the type, never past its largest), `NaN`, `Infinity` or `-Infinity`; a
  complex number as a pair of those, or one, its imaginary part 0; a datetime or timedelta as an
  integer or `NaT`; a `U` type's string as UTF-32, padded with zeros; and base64 of a bytes, void or
  structured type's bytes, padded with zeros to the element. A value this does not read as the
  type's gives no fill: the `.zarray` among the declarations holds it.
- **No grid** for an array whose `.zarray` has no `order` of `C` or `F`, no `chunks` of positive
  integers as many as its axes, a `dimension_separator` other than `.` or `/`, `filters` that are
  not null or a list of objects with an `id`, or a `compressor` that is not null or one.
- **Not described**: a node whose `.zarray` is empty, too large to read, not JSON, or has no shape
  as above. A group declares no array.
