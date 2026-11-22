# ENVI: stored arrays

What the ENVI parser describes as stored arrays ([stored-array.md](../../stored-array.md)), part of [the ENVI profile](../envi.md).

An ENVI raster is one stored array, the one
its header declares, in the store IR the profile's run builds of the header file and the data file
beside it. It is keyed by the header file's object (the header has no element of its own), with no
index. The facts give the key: `array.declared_by` is the id of that object.

- **Declarations.** The line of the `data type` field, then the lines of the `byte order` and
  `interleave` fields where the header has them: each the element `lines/<i>` of the line where
  the field starts, which holds its name, its value and its line end.
- **`shape`** is the data's, outermost first, as the interleave orders `bands`, `lines` and
  `samples` ([envi.md §4.1](../envi.md#41-the-array)). `axes` gives each storage
  axis's place in the list `samples`, `lines`, `bands`: `[2, 1, 0]` for `bsq`, `[1, 2, 0]` for
  `bil`, `[1, 0, 2]` for `bip`.
- **`item_bits`** is eight times the bytes of one element of the `data type` field's type
  ([envi.md §3](../envi.md#3-data-types)); a complex element is its two parts.
- **The grid** is one unit of the whole array: each axis `Each` of its size, under `short`. Its row
  is the `data` element of the data file, another object of the store: for a raw file, the
  array's bytes after the header offset's; for a gzip file, the whole file.
- **The chain** is one: empty for a raw file; `gzip` for a gzip file; and `skip`, then `gzip`, for
  a gzip file with a `header offset` above 0, which GDAL counts within the inflated bytes. `skip`'s
  `length` is the value of the `header offset` field, a range of the field's line. The byte order
  is a declaration, not a codec.
- **The stream is not inflated**, as the profile reads none of the data: one that inflates to other
  than the header offset's bytes and the array's is not detected.
- **No grid** for no input: the profile rejects what it does not read (other file types, framed
  data, a raw data file of another size), and builds no IR of it.
- **The grid** is dense: ENVI has no fill value. The header's `data ignore value` marks pixels to
  ignore, and is recorded, not made a fill value.
