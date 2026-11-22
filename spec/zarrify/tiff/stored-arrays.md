# TIFF: stored arrays

What the TIFF parser describes as stored arrays ([stored-array.md](../../stored-array.md)), part of [the TIFF profile](../tiff.md).

Every IFD the parser reads (of the main
chain, a SubIFD, or one another pointer tag leads to) whose ImageWidth (256) and ImageLength (257)
are unsigned values it holds declares an image, a stored array, whether or not the profile presents
it, keyed by the IFD's element, with no index. The planes of an OME-TIFF and the levels of a
pyramid are each an array: how they assemble is the profile's. The facts give the keys: each
level's `declared_by` lists, in the order of its `planes`, the id of each plane's IFD, null for a
plane no file of the run holds, and a set's store renumbers them as it does `planes`
([stored-array.md §2.1](../../stored-array.md#21-keys)). Below, a field's value is the first of its values, and a field the IFD lacks
has TIFF 6.0's default.

Each plane after the first of an ImageJ stack in one IFD
([tiff.md §4.1](../tiff.md#41-planes)), which IFD 0 and its ImageDescription declare
together, is a stored array too, keyed by its `imagej/planes/<p>` element, with no index, which
`declared_by` gives for it. Its description is IFD 0's, whatever that is, with the `value`
element of IFD 0's ImageDescription (270) as one more declaration, the last; where it has a
grid, its rows are the plane's own strips, `imagej/planes/<p>/strips/<j>`.

- **Declarations.** The header's `byte_order`, then the `value` element of each of these fields
  the IFD has with a value inside the file: BitsPerSample (258), PhotometricInterpretation (262),
  ColorMap (320), ExtraSamples (338), SampleFormat (339), YCbCrCoefficients (529),
  YCbCrSubSampling (530) and ReferenceBlackWhite (532).
- **`shape`** is ImageLength, then ImageWidth; with SamplesPerPixel (277) other than 1, the
  samples come last, or first where PlanarConfiguration (284) is 2. An ImageDepth (32997) other
  than 1 comes before ImageLength. `axes` is absent: TIFF names its dimensions by tag, and lists
  them in no order.
- **`item_bits`** is BitsPerSample, where every sample has the same; absent otherwise. Where it
  is not a multiple of 8 and the image has a grid, `pad_from` is the columns' axis (1, or 2 with
  the samples first): a unit's rows each end on a whole byte. An image the IR does not model (No
  grid, below) has none; one that falls back at the limits of [stored-array.md §6](../../stored-array.md#6-in-the-ir) keeps it.
- **The grid.** An IFD with TileWidth (322) and TileLength (323) is cut into tiles, `Each` of
  TileLength and of TileWidth, under `full`, as libtiff and tifffile read it; its tables are
  TileOffsets (324) and TileByteCounts (325), or, where it has not both, StripOffsets (273) and
  StripByteCounts (279). Another is cut into strips, `Each` of RowsPerStrip (278), at most
  ImageLength, and of ImageWidth, under `short`, its tables the strips'. Interleaved samples are a
  last axis of one unit; with the samples first, each is a unit of its own, and the tables list
  the first sample's units, then the next's. Each entry of the tables that holds bytes is a row,
  its element the entry's `data` element (`tiles/<j>` or `strips/<j>`, of the IFD the tables are
  first met in); a JPEG tile or strip whose element's recipe is not the identity (it splices in a
  marker, or the tables) is that element with a `range` of all its extent ([stored-array.md §4.2](../../stored-array.md#42-rows)). An
  entry of no bytes at offset 0, as a sparse file's writer leaves a unit out, has no row.
- **The chain** is one, by Compression (259): empty for 1; `tiff_lzw` for 5, `zlib` for 8 and
  32946, `xz` for 34925 and `zstd` for 50000, each after `tiff_predictor` where Predictor (317) is
  2 or 3; `packbits` for 32773; `ccitt_t6` for 4 (one sample of 1 bit); `jpeg` for 7 (samples of 8
  bits), with `tables` where the IFD has JPEGTables (347); `jpeg2k` for 33003, 33004, 33005 and
  34712 (8 or 16 bits); `jpegxl` for 50002 and 52546 (8, 16 or 32 bits); `webp` for 50001 (8
  bits); `lerc` for 34887
  (8, 16, 32 or 64 bits), before `zlib` or `zstd` where the second value of LercParameters (50674)
  is 1 or 2; `eer` for 65000 and 65001 (one sample of 1 bit), with their fixed bit counts as
  values, and for 65002, with the places of those of the fields 65007, 65008 and 65009 the IFD has
  with unsigned values. The streams of `jpeg`, `jpeg2k`, `jpegxl`, `webp` and `eer` use no
  predictor, and libtiff and tifffile pass a Predictor over under them, as the chain does. Any
  other compression, one of these with samples of another size, and LERC whose LercParameters are
  not unsigned integers with a second value of 0, 1 or 2, is `source` with `format` `tiff` and
  `id` the Compression: the profile then reads the IFD for the rest.
- **No grid** for an image the IR does not model: BitsPerSample that differ; FillOrder (266)
  other than 1; a Predictor other than 1 under a compression that is neither LZW, Deflate, LZMA
  or zstd nor one of those five, such as none, PackBits, CCITT and LERC (libtiff applies a
  predictor only within those four coders, and tifffile applies one to whatever it does not
  decode as an image, so readers disagree); under those four, a Predictor other than 1, 2 and 3,
  Predictor 2 with samples that are not integers of 8, 16, 32 or 64 bits, and Predictor 3 with
  samples that are not floats of 32 or 64 bits (by SampleFormat, which every sample must share);
  PhotometricInterpretation 6
  with a YCbCrSubSampling other than (1, 1) under a compression that restores the stored bytes (1,
  5, 8, 32773, 32946, 34925, 50000), whose samples are then not an array of the image's shape; an
  ImageDepth other than 1; a PlanarConfiguration other than 1 and 2 with more than one sample; a
  width, a length, a sample count, a tile size or a RowsPerStrip of 0; an IFD without both of its
  units' tables, as unsigned values the parser holds; tables that do not list as many units as
  the grid has, or that differ in length; an entry of no bytes at an offset other than 0, which
  readers do not agree on (libtiff sizes a lone strip from the image and refuses one among
  others, and tifffile reads it as absent); and an entry whose bytes do not lie in the file, so
  that a file cut short does not read as an image with units its source does not hold.
- **More bytes than the unit's.** A tile or strip may hold, or decode to, more bytes than its
  unit's elements take (a tile stored at a larger sample size than its fields give): the unit's
  are the first, as libtiff and tifffile read them.
- **The grid** is sparse, an entry of no bytes at offset 0 leaving its unit out, and has no fill
  value: TIFF has none. NDPI's grid of restart intervals (below) is dense.
