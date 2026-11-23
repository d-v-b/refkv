# HDF5: stored arrays

What the HDF5 parser describes as stored arrays ([stored-array.md](../../stored-array.md)), part of [the HDF5 profile](../hdf5.md).

Every object the parser reads whose header has
a data layout message, and neither a symbol table nor a link info message, is a stored array,
whether or not a path reaches it or the profile presents it: a dataset. It is keyed by its
object's element (`objects/<i>`), with no index: `i` is the dataset's position among the
description's `objects` ([hdf5.md §8.7](../hdf5.md#87-the-description)), whose `storage`
gives its storage element, which the store's `placed()` renumbers ([stored-array.md §2.1](../../stored-array.md#21-keys)). An
attribute's value is not described yet.

- **Declarations.** The first is the data of the dataset's datatype message, or, where that
  message is shared, of the committed datatype's datatype message it names; a header with no one
  datatype message gives none. Of a dataset the parser did not read, it is the shared message's
  own data: the description reads no byte the parser did not, so it is the same however the
  file's bytes were fetched. After it come the fill value messages whose value the description
  cannot give (below). A message is the element the IR made of its data (`messages/<k>/data`),
  or, where the IR had no room for one, a range of the source.
- **`shape`** is the dataspace's current dimensions, absent for a null dataspace and for one that
  does not read. `axes` is absent: HDF5 stores an array in its declared order.
- **`item_bits`** is eight times the datatype's size, the bytes of one stored element (for a
  variable-length type, of its heap reference); absent where the datatype does not read or its
  size is 0, and for a dataset the parser did not read whose datatype message is shared.
- **A chunked dataset** whose chunk index and filter pipeline the parser read has the grid of its
  chunk shape, `Each` on every axis, under `full`, and one chain: its filter pipeline's codecs
  ([stored-array.md §3.2](../../stored-array.md#32-the-list)), in the pipeline's order. A filter the list has no name for, or whose
  client data are fewer than its entry's parameters, is `source`, with `format` `hdf5`, its `id`,
  its `name` where the message gives one, and `parameters`, a place of all its client data,
  where it has any. Each chunk the index holds is a row: its element; or, where the chunks of
  an implicit index are one `data` element, its range of that element (no range where the chunk
  is all of the element); or, where the IR had no room for its element, its range of the source.
  `skips` is the chunk's filter mask, less the bits past the pipeline; an edge chunk the dataset
  stores unfiltered skips every codec. A chunk the index does not hold has no row.
- **A contiguous or compact dataset** is one unit in an empty chain: each axis `Each` of its
  length, or an empty `List` for an axis of length 0, under `short`. The row's bytes are the
  dataset's `data` element, or the range of its layout message's data that holds a compact
  dataset's (a range of the source where the IR had no room for the element); a dataset of no
  bytes, and a contiguous one with no address, has no row. A dataset whose stored bytes are not
  as many as its elements', and a contiguous one whose data runs past the end of the file, has no
  grid.
- **A dataset in external files** is one unit likewise, its row's `ranges` the files' ranges in
  order, each by the sibling key the run resolved for the file. It has a grid only where a store
  run found every file it reads, each as long as its part; in a file run, which looks for no
  sibling, it has none.
- **No grid** also for a virtual dataset, a null dataspace, a chunked dataset whose index the
  parser did not read, and a dataset of which it did not read the dataspace, the datatype, the
  layout, the filter pipeline message or the fill value message.
- **Sparse and dense.** A chunked dataset's grid is sparse (its index may leave a chunk out), and
  so is a contiguous one's (a dataset of no address holds no unit); a compact dataset's, and one in
  external files, is dense.
- **The fill value** of a sparse grid has HDF5's three states, as the library's
  `H5Pfill_value_defined` gives them for the dataset: none where it is undefined (the message says
  so, or the header has no fill value message of either type), `format_default` (a fill value is defined and
  the message holds none: zeros), or `given`, the place of the value's bytes in the message, which
  are an element as the datatype stores one. The new message (type 5) is read where a header has
  both. A shared message, several messages of the type read, a message that does not read, or a
  value whose length is not the datatype's size, leaves none, with those messages among the
  declarations, whatever the grid.
