# HDF4: stored arrays

What the HDF4 parser describes as stored arrays ([stored-array.md](../../stored-array.md)), part of [the HDF4 profile](../hdf4.md).

Every dataset of the SD interface the parser
reaches is a stored array, whether or not the profile presents it: each `Var0.0` Vgroup of the
file's `CDF0.0` Vgroup ([hdf4.md §3](../hdf4.md#3-datasets)). It is keyed by that
Vgroup's element (`elements/1965/<ref>`), with no index. The description gives the key: each
dataset's `element` ([hdf4.md §9.6](../hdf4.md#96-the-description)) is that element's id, of every
dataset with a stored array, presented or not. The writer's Vdatas (tables) and the
datasets' attributes are not described yet.

- **Declarations.** The number type's element (`elements/106/<ref>`), whose type code and width
  say what an element is and whose class byte gives the byte order (4: little-endian; else
  big-endian), then the dimension record's (`elements/701/<ref>`), where the Vgroup names them;
  then, of a `_FillValue` the description does not give (below), its Vdata header.
- **`shape`** is the dimension record's sizes, in its order: HDF4 stores an array in its declared
  order, so `axes` is absent. An unlimited first dimension's size is the records the data holds,
  its bytes divided by a record's, as the SD interface reads it: the dimension record keeps the
  size at its creation, and no place holds the records.
- **`item_bits`** is the bits of the type the number type's code names: 8, 16, 32 or 64; of text
  (`char8`), 8.
- **Data stored whole** (as one element, in linked blocks, or compressed whole) is one unit, each
  axis `Each` of its length, or an empty `List` for an axis of length 0, under `short`. Its row is
  the one element that holds its bytes (the data element, the compressed stream's, or a linked
  block's), with the range of them where the element holds more; or, where several linked blocks
  hold them, `ranges` of the file, the blocks' bytes in order. The chain is empty,
  `zlib` for data compressed with deflate (method 4, which HDF4 writes as a zlib stream), or
  `source` for another method, with `format` `hdf4` and `id` the method's code; its row is then
  the compressed stream. A dataset with no data, or whose compressed data decodes to no bytes, has
  no row: every element is the fill value.
- **A chunked dataset** has the grid of its chunk lengths, `Each` on every axis, under `full`: the
  library stores every chunk at the full chunk size. Each chunk the chunk table lists is a row, at
  the coordinates the table gives, as a dataset stored whole is (above), in the chain of its own
  method: the array has a chain for each method its chunks use, the empty one first. A chunk of an
  unlimited dimension past the records lies outside the array, and has no row; a chunk the table
  does not list has none.
- **No grid** for data the parser does not locate: in an external file, in a special element of
  another kind, of a compression model other than 0, or in linked blocks one of which was never
  written, which the library reads as zeros and no place holds. Nor where the data holds fewer
  bytes than the dataset, or the number type or the dimension record does not read. Such a
  dataset still has its `shape` and `item_bits`, but for an unlimited first dimension, whose size
  only the data gives: it has no `shape`, unless the parser located its data.
- **The grid** is sparse, whole or chunked: a dataset may hold no data, and a chunk table need not
  list every chunk. Its fill value is `given`: a chunked dataset's chunk header's (its bytes after
  the header's dimensions), where it is one element long; else the `_FillValue` attribute's first
  element, its place in the attribute's Vdata, where it is of the dataset's number type and byte
  order (of text, its first byte). A `_FillValue` of the dataset's type in another byte order
  leaves none, its Vdata header among the declarations. With neither, it is `format_default`, the
  SD interface's: netCDF's default of the number type's netCDF type, as `NC_arrayfill` writes it
  (`char` 0, of `char8` and `uchar8`; `byte` −127, `short` −32767 and `long` −2147483647, as
  bytes of the number type, so 129, 32769 and 2147483649 of the unsigned types; and `float` and
  `double` 9.9692099683868690e36), which a `_FillValue` of another type does not change.
