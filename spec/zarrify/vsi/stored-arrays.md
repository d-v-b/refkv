# VSI: stored arrays

What the VSI parser describes as stored arrays ([stored-array.md](../../stored-array.md)), part of [the VSI profile](../vsi.md).

A VSI slide's arrays are described by its store
parser itself ([stored-array.md §6](../../stored-array.md#6-in-the-ir)), in the store IR of the `.vsi` file and its tile files: each
tile file's levels, and the images of the `.vsi` file's IFDs, whether or not the profile presents
them. The facts give the keys: each image's `declared_by` is the id of the element that declares
its arrays, a pyramid's tile file's `ets` element or an IFD image's IFD; and the facts'
`declared_by` lists, by its index in the chain, the id of each IFD whose image is described, null
for an IFD with no description, so that what names an IFD by that index (a stack's `coarse_ifd`,
an entry of `not_presented`) finds its image's.

- **A tile file's arrays** ([vsi.md §3](../vsi.md#3-the-tile-files)) are its levels,
  one array a level, from 0 to the greatest level coordinate among its tiles (level 0 alone
  without `use_pyramid`, and in a file of no tiles). They are keyed by the file's `ets` element,
  the ETS header, a kind of element that may declare several arrays: a level's index is the level.
  That element is their one declaration: its `pixel_type`, `channels`, `colorspace`,
  `compression`, `background` and `component_order`.
- **`shape`.** A tile's record lists its coordinates: the column's, the row's, then the others.
  Storage order is their reverse, without the level's: the coordinates after the row's, the last
  first, then the rows and the columns; and, where `channels` is above 1, that many samples last,
  which a tile interleaves. `axes` gives the records' order: of `n` axes before the samples,
  `n − 1` down to 0, and then `n` for the samples, which the header counts apart. A coordinate
  after the row's has the size `dimension_sizes` gives it, or 1 where the header's `dimensions`
  do not reach it. Level 0 has the stack's width and height, which the slide declares in the
  `.vsi` file's tree (its `Image boundary`), and which Bio-Formats reads. A later level declares
  no size: it is as large as its tiles reach, as many tile heights as its greatest row coordinate
  plus one, and as many tile widths as its greatest column coordinate plus one. (Bio-Formats
  halves the level before, and stops at that extent,
  [vsi.md §4.1](../vsi.md#41-the-pyramid): that crop is the profile's.)
- **`item_bits`** is eight times the bytes of a sample of the header's `pixel_type`.
- **The grid** is a tile a unit: `Each(1)` on every axis before the rows, then `Each` of
  `tile_height`, of `tile_width` and, where there are samples, of `channels`, under `full` (a tile
  at the right or bottom edge is a whole tile). Each record of the directory whose tile lies in
  its level's grid is a row: its `tiles/<k>` element. A tile of level 0 past the stack's width or
  height is no unit of the array the slide declares, and has no row (Bio-Formats does not read it
  either); every tile of a later level lies in its grid.
- **The chain** is one for every level of a file, by the header's `compression`: empty for 0 (the
  samples little-endian); `jpeg`, with no `tables`, for 2 (JPEG) and for 5 (lossless JPEG, a
  stream of the same standard), and `jpeg2k` for 3, where a sample is one or two bytes. Any other
  is `source` with `format` `vsi` and `id` the `compression`: 8 (PNG) and 9 (BMP), which the list
  has no codec for, and JPEG or JPEG 2000 of larger samples, which those codecs' entries do not
  cover. The parser reads no tile, so a stream that holds other samples
  than the header says is not detected.
- **The grid** is sparse: a tile the directory does not list is the background. The fill value is
  given, as the place of the background's first sample in the `ets` element (a sample's bytes at
  offset 108), where the tiles are uncompressed and the background's samples are all the same.
  Otherwise there is none, the background being in the declaration: an
  element is one sample, and what a codec's decoding makes (a JPEG stream's components, before
  any color conversion) need not be in the background's color space.
- **No grid**, and no shape, for a level after 0 that holds no tile, below one that holds some:
  the file declares no extent for it.
- **An IFD's image** ([vsi.md §2.3](../vsi.md#23-the-tiff-images)). Every IFD of the
  `.vsi` file's chain that lies in a TIFF region of the tree, and declares a size, is described
  as a TIFF's, by the same code, keyed by its struct (`ifds/<j>`), with no index: the macro image,
  a stack's default sample IFD, and each stack's coarse thumbnail. The byte order's declaration
  is the first two bytes of the file's `header` element; a field's unsigned values are those of
  the types BYTE, SHORT, LONG and IFD; and `source` has `format` `vsi`. A JPEG strip whose
  element's recipe splices in a marker and the tables is that element with a `range` of all its
  extent ([stored-array.md §4.2](../../stored-array.md#42-rows)).
- **Not described.** An IFD that lies in no TIFF region has no element, so its image has no key
  and is not described yet; an EXIF IFD declares no image.
