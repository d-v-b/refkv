# FITS: stored arrays

What the FITS parser describes as stored arrays ([stored-array.md](../../stored-array.md)), part of [the FITS profile](../fits.md).

Every HDU with an axis (`NAXIS` above 0) has
descriptions, whether or not the profile presents them, keyed by its `header` element
(`hdus/<i>/header`), a kind of element that may declare several arrays: each of an HDU's has an
index. An image (the primary HDU, or an `IMAGE` extension) has one array, index 0; a table
(`TABLE` or `BINTABLE`) two, its rows (0) and its heap (1); a tile-compressed image (a `BINTABLE`
with `ZIMAGE = T`) three, its image (0) and its table's rows (1) and heap (2). The parser's
description gives the key: each HDU's `elements` has its `header`'s id, which keys its arrays
unless it is of no axes, and so holds none ([fits.md §9.6](../fits.md#96-the-description)). In a compressed file ([fits.md §2.1](../fits.md#21-a-compressed-file))
the headers and the units' elements are in the `stream` element's space: a unit's bytes are the
decoded file's.

- **Declarations.** The header's element: its cards give the array's axes and what its
  elements are (`BITPIX`, `BSCALE`, `BZERO` and `BLANK`; a table's `TFORMn`; a tile-compressed
  image's `Z` cards). A tile-compressed image whose table has a `ZSCALE`, `ZZERO` or `ZBLANK`
  column, which gives each tile's scale, zero or blank value, has a second, the table's rows (the
  `table` element), each tile's values in its row, at the places the header's `TFORMn` give the
  columns. A parameter given as a place of a card is the card's 80 bytes, a range of the header's
  element, and is the card's value (FITS Standard 4.0 section 4.2), an integer or a string.
- **An image.** `shape` is `NAXISn` to `NAXIS1`, the slowest first, and for two or more axes
  `axes` is their reverse, since the header lists `NAXIS1` first. `item_bits` is |`BITPIX`|. It
  is one unit in an empty chain: each axis `Each` of its length, or an empty `List` for an axis
  of length 0, under `short`. Its row is the HDU's `data` element, or, where the HDU's data is
  longer than the image (an `IMAGE` extension whose `PCOUNT` is not 0, or whose `GCOUNT` is not
  1), the range of that element that holds the image, its first bytes, as astropy reads it; an
  image of no elements has no row. The elements are as stored, big-endian: `BSCALE` and `BZERO`
  are the profile's.
- **A table's rows** are an array of shape [`NAXIS2`], a row an element: `item_bits` is 8 ×
  `NAXIS1`. It is one unit, the `rows` element, in an empty chain, gridded as an image, with no
  row where the rows have no bytes. What a row's bytes are (its columns' fields, an ASCII
  table's text) is the profile's, from the header's `TFORMn` and `TBCOLn`. Rows of no bytes
  (`NAXIS1` of 0) have no `item_bits` and no grid; nor have a binary table's rows where its
  `TFORMn` are not all forms the profile reads
  ([fits.md §4.1](../fits.md#41-rows-as-a-struct)) or their fields' bytes are not
  `NAXIS1` in all, since readers do not agree on where such a row is: the standard's rows are
  `NAXIS1` bytes each, as the profile cuts them, and astropy's the fields' bytes, one row after
  another. An ASCII table's row is `NAXIS1` characters, which its fields need not fill.
- **A table's heap** is the `H` bytes from `THEAP` to the end of the data
  ([fits.md §4.3](../fits.md#43-the-heap)), of shape [`H`] and `item_bits` 8: one
  unit, the `heap` element, in an empty chain. An empty heap is described too, with no row; an
  ASCII table's is empty unless its `PCOUNT` is not 0. Which of its bytes a row's descriptor
  names is the profile's.
- **A tile-compressed image's image** has `shape` `ZNAXISn` to `ZNAXIS1`, `axes` as an image's,
  and `item_bits` |`ZBITPIX`|, or, where it is quantized (below), 32. Its grid is its tiles:
  `Each` of `ZTILEn` to `ZTILE1` (with the defaults of
  [fits.md §3.3](../fits.md#33-tile-compressed-images)), under `short`, an edge tile
  cut to the image. Each row of its table that holds a tile is a row of the chunk
  map, at the tile's coordinates (the table's rows are the tiles in storage order, `ZNAXIS1`'s
  fastest): the tile's bytes in the heap, a range of the element of its chunk of the heap
  (`hdus/<i>/heap/<j>`, [fits.md §6.1](../fits.md#61-elements)), or that element
  alone where the tile is all of it. A table row with both descriptors empty holds no tile, and
  has no row.
  - **The first chain** is that of a tile in `COMPRESSED_DATA`, by `ZCMPTYPE`: `fits_rice` for
    `RICE_1` and `RICE_ONE`, its `block_size` and `byte_pix` the places of the `ZVALi` cards of
    the parameters `BLOCKSIZE` and `BYTEPIX`
    ([fits.md §3.3](../fits.md#33-tile-compressed-images)), each absent where the
    header has no such card; `gzip` for `GZIP_1`; `shuffle`, then `gzip`, for `GZIP_2`, the
    shuffle's `element_size` a value, the bytes of an element (4 under quantization), for which
    the header has no card; and no codec for `NOCOMPRESS`.
  - **Quantization.** A float image (`ZBITPIX` below 0) whose table has the columns `ZSCALE` and
    `ZZERO` is quantized (FITS Standard 4.0 section 10.2): its elements are the integers its
    tiles store, each 4 bytes, big-endian, two's complement, which the codecs above code, and its
    description stops at them. The profile makes the image's floats of them with what the
    declarations give: the dither's name, the `ZQUANTIZ` card's value (`NO_DITHER`,
    `SUBTRACTIVE_DITHER_1` or `SUBTRACTIVE_DITHER_2`; without the card, `NO_DITHER`); the tile's
    scale and zero, its row's `ZSCALE` and `ZZERO`; and the integer of a pixel of no value, its
    row's `ZBLANK` where the table has that column, else the header's `ZBLANK` card, else its
    `BLANK` card, as astropy reads it, else none. An integer I is then NaN where it is that
    blank; else 0 where it is −2147483646 under `SUBTRACTIVE_DITHER_2`; else I × scale + zero
    without a dither, and (I − R + 0.5) × scale + zero under one, computed in binary64, each
    operation rounded, in that order, then rounded to |`ZBITPIX`| bits. R is the dither's next
    random number, of the 10,000 cfitsio's `fits_init_randoms` makes (from s = 1, s ← 16807 × s
    mod (2^31 − 1) each time, the number s / (2^31 − 1) as a binary32). The source stores no seed
    of a tile's dither: it is computed, the tile's index in the table (from 0, the tiles in
    storage order) plus `ZDITHER0`. A tile starts at the number at floor(500 × N), N the number at
    (seed − 1) mod 10000, and takes one for each pixel, whatever its integer; after the last
    number it takes up the N after its own (after the last, the first) and goes on from
    floor(500 × N).
  - **The second chain.** A tile in `GZIP_COMPRESSED_DATA` is a gzip stream of its elements as
    an image stores them, never quantized. Where the table has that column, the image is not
    quantized and the first chain is not `gzip` alone, the description has a second chain,
    `gzip`, which those tiles' rows name; where the first chain is `gzip` alone, they are in it.
    A quantized image has no second chain: such a tile holds its floats, not the integers of its
    other tiles (below).
  - **`source`.** A `RICE_1` image whose `BYTEPIX` (4 where absent) is not the bytes of an
    element (not 4, where quantized), or whose elements are floats that are not quantized, has
    the one codec `source` as its first chain, with `format` `fits` and `id` the `ZCMPTYPE`
    card's string: `fits_rice` makes integers of `BYTEPIX` bytes, which
    are then not the image's elements. astropy converts their values to the image's type; the
    profile's codec does not.
  - **A tile's width.** A `GZIP_1`, `GZIP_2` or `NOCOMPRESS` tile, and one in
    `GZIP_COMPRESSED_DATA`, is described as holding its elements at their own size (4-byte
    integers under quantization), as the profile reads it. astropy takes the size of a tile's
    values from its decoded length, and converts those of a tile stored at another size: the
    parser reads no tile, so no description tells such a tile from the others.
  - **No grid** for an image that readers do not agree on, which keeps its shape, `item_bits`
    and declarations: an integer image whose table has the columns `ZSCALE` and `ZZERO` (astropy
    scales its values where there is no dither and refuses it where there is one; the profile
    reads them as stored); a float image without those columns whose header has a `ZSCALE` or a
    `ZZERO` card (cfitsio takes the cards as every tile's and dequantizes; astropy and the
    profile read the values as stored) or a `ZBLANK` or a `BLANK` card (astropy makes a stored
    value equal to it NaN; the profile reads it as stored); an image that is not quantized whose
    table has a `ZBLANK` column; a quantized image with neither that column nor a `ZBLANK` card
    whose `BLANK` card is no integer; a quantized image of which a tile is in
    `GZIP_COMPRESSED_DATA`, whose units would hold two kinds of elements, its floats and its
    integers; and an image whose table's rows are not its columns' fields (as a table's, above),
    whose descriptors are then not where the parser read them.
  - **Its table's rows and heap** are described as a binary table's: the rows one unit, the
    `table` element; the heap one unit, the range of the HDU's element (`hdus/<i>`) that holds
    it, since its bytes are in the elements of its tiles' chunks.
- **The grid** is dense, but a tile-compressed image's, which is sparse, a table row whose
  descriptors are both empty leaving its tile out: FITS has no fill value. `BLANK`, the stored
  value of a pixel with none, is a declaration.
