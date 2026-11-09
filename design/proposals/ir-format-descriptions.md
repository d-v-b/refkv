# Proposal: every format describes its stored arrays

Status: proposed. Follows [ir-array-model.md](ir-array-model.md), whose step 1 (the type, and
NumPy's descriptions) has landed.

## The change

Every format whose parser reaches arrays describes them on the stored-array type
([spec/stored-array.md](../../spec/stored-array.md)), in one describe-only PR per format. This is
the first of the two PRs [profile-plugins.md](profile-plugins.md#order-of-work) gives each format
in its steps 3 and 4: it lists the format's structures, specifies its description and tests it
against the format's reference reader. The second PR (the plan, the facts' removal, the sizing
decisions) stays where profile-plugins puts it, after the plan's machine.

The first PRs do not need the machine: NumPy's description landed without it, and an HDF5 spike
described every HDF5 dataset without it
([ir-array-model.md](ir-array-model.md#order-of-work)). Landing them first gives every format's
description a test against its reference reader before any plan reads it, and settles the codec
list once rather than inside each plan PR.

A describe-only PR changes no output. It may change the IR mirror's forms only where a
description needs a place the IR does not have (a gzipped header, [§NIfTI](#nifti)), and names
each such change.

### In scope

TIFF with QPTIFF, NDPI, VSI, HDF5 with NWB and IMS, FITS, GRIB, NIfTI, MRC with MRC datasets,
NRRD, safetensors, DICOM, CZI, ND2, LIF with XLEF, and SAFE.

### Not in scope

- **Coded streams** (MP4, MP3, FLAC), which need a description of their own kind
  ([ir-array-model.md](ir-array-model.md#not-in-this-proposal)).
- **The store profiles** (Zarr v2 with OME-Zarr, Zarr v3, N5, precomputed), whose array documents
  already describe their arrays.
- **DICOM series**, whose store form is composed in Python and has no IR of its own; it gets its
  description when profile-plugins' step 4 gives it a store IR that grafts the files' IRs.
- **Every second PR's work**: moving FITS's `tile_cut` to the projection, replacing TIFF's JPEG
  prefix, and deleting facts the descriptions duplicate. Until then a fact and a description say
  the same thing twice; each format PR lists the facts its description duplicates.

## Rules every format PR follows

1. **Every array the parser reaches is described**, presented or not: a TIFF thumbnail, an
   unpresented HDF5 dataset, a CZI subblock the plan leaves out. An array whose storage the IR does
   not model has no grid ([§2.3](../../spec/stored-array.md#23-the-grid)).
2. **The facts give each description's key**, the declaring element's id, as NumPy's
   `declared_by` does. A format whose IRs are grafted renumbers it in its store code. Where the
   facts do not yet record the declaring element (HDF5's `objects/<i>`, GRIB's `messages/<i>`,
   NRRD's header lines), the PR adds it.
3. **Descriptions are built on the finished IR.** `check::finish` may make an element an alias or
   unfold a run, so a parser that cached element ids while parsing finds them again after finish.
   A row names the element that holds the unit's bytes: an alias's target, and a run member's
   element with `member`.
4. **No typing.** `item_bits` is the element's size as the source declares it. Data type names,
   scaling (BSCALE, `scl_slope`), an added axis (HDF5's string axis, a sample axis a format does
   not declare), a fill of zero and a transposition the output makes stay in the facts, for the
   profile.
5. **Declarations are listed** in the format's document, place by place, so that a profile knows
   what each is.
6. **Codecs come from the list.** A PR adds the entries it first needs to
   [§3.2](../../spec/stored-array.md#32-the-list), with the names [§Codecs](#codecs) settles; a
   later PR reuses an entry and extends it only through a parameter.
7. **Tests.** One test runs every fixture of the format (and the PR's crafted ones) against the
   reference reader: shapes, `item_bits`, grids, each row's bytes against the reader's offsets and
   lengths, and each unit decoded through its chain (by the test helper of
   [§Step 0](#step-0-the-shared-rules)) against the reader's decoded unit. One test per refusal and
   per fallback. Fixtures the corpus lacks are crafted, in the fixture generators, when the
   reference library can write them.
8. **No output change.** `just cli-identical` and the fixture comparison show no change; the PR
   names any mirror change [§The change](#the-change) allows and any fixture whose array falls
   back at the map's limit.
9. **The spec's [§7](../../spec/stored-array.md#7-the-formats)** gets the format's paragraph, in
   NumPy's form: its arrays and keys, shape and axes, `item_bits`, grid and units, chains, fill,
   declarations, and its fallbacks.

## Step 0: the shared rules

One PR before the formats, changing spec/stored-array.md and the IR's checks:

- **A codec sees the unit's shape.** A codec's decoding is given the unit's decoded shape (its grid
  unit, cut at the array's end under `short`, whole under `full`) and the array's `item_bits`, as
  Zarr gives a codec its chunk's specification. It still never sees the unit's coordinates. TIFF's
  LZW, CCITT and EER, NDPI's JPEG frame and DICOM's RLE need the decoded size, and a short last
  strip's differs from the others'; without this rule each would be a column, and a rank-3 TIFF row
  would pass the map limit's guarantee of three entries.
- **`lsb_first`** (parameter `bits`, a value): the unit's items, of `bits` bits each, are packed
  least significant bit first, a little-endian bit stream (bit k is bit k mod 8, counted from the
  least significant, of byte ⌊k/8⌋; an item's first bit is its least significant). Decoding packs
  them most significant bit first, as [§3.1](../../spec/stored-array.md#31-chains) packs elements. DICOM's 1-bit frames, MRC's mode 101
  (4 bits) and safetensors' F4 and F6 use it. Where `bits` does not divide 8, an array with
  `pad_from` gives `source` instead, since its padding would fall inside an item.
- **The test helper**: `python/tests/ir/chains.py`, which decodes a unit through a chain, codec by
  codec, by imagecodecs or the reference library, given the unit's bytes, its parameters (places
  read from the source) and its shape. Each format PR registers its codecs there.
- **The shared codecs' entries** ([§Codecs](#codecs)): the compressors and image codecs several
  formats use, so that no two format PRs write the same entry.

## Codecs

A name follows [§3.2](../../spec/stored-array.md#32-the-list): it states the byte format its decoding takes, its framing included, and a name
covers two framings only when a parameter chooses between them. Configuration the source defines
once is a place.

**Step 0's entries**, used by several formats:

| Codec | Takes | Parameters | Used by |
| --- | --- | --- | --- |
| `zlib` | RFC 1950, with its header and Adler-32 | | TIFF (compressions 8 and 32946), HDF5 (the deflate filter), ND2 |
| `gzip` | RFC 1952, one or more members | | FITS (GZIP_1), NRRD |
| `zstd` | Zstandard frames (RFC 8878), one or more | | TIFF, HDF5 (`zstd` filter 32015), CZI |
| `xz` | the .xz container | | TIFF |
| `bzip2` | the .bz2 stream | | HDF5 (filter 307) |
| `shuffle` | bytes grouped by position in elements of `element_size` | `element_size` (value) | HDF5's shuffle, FITS's GZIP_2, CZI's Zstd1 hi-lo |
| `lsb_first` | see above | `bits` (value) | DICOM, MRC, safetensors |
| `jpeg` | an ISO 10918-1 interchange stream, any process (baseline, extended, progressive, lossless) | `tables`: an abbreviated table stream, a place (TIFF's tag 347); `header`: the stream's start up to its first scan, a place, the unit being the scan data after it (NDPI); `color_transform`: whether the decoder converts YCbCr to RGB, a value, which replaces the Adobe marker refkv splices in today | TIFF, NDPI, VSI, DICOM, CZI |
| `jpegls` | an ISO 14495-1 stream | | DICOM |
| `jpeg2k` | an ISO 15444-1 codestream (high throughput, 15444-15, included; it declares itself), not a JP2 file | `main_header`: a place, the unit then being one tile's tile-parts (SAFE) | TIFF, VSI, DICOM, SAFE |
| `jpegxr` | an ITU-T T.832 codestream as the format frames it (to confirm that NDPI and CZI frame it alike) | | NDPI, CZI |
| `png` | a PNG file | | VSI |

The entries say what decoding makes: JPEG's samples in the order its `color_transform` leaves
them; PNG's 16-bit samples big-endian, as PNG stores them. With `header`, `jpeg` takes the frame
size from the unit's shape, so NDPI's frame that is a row short needs no spliced SOF0.

**Each format's own entries**, added by its PR:

- TIFF: `tiff_lzw`, `packbits`, `tiff_predictor` (horizontal differencing; its byte order a place,
  `header/byte_order`), `tiff_float_predictor`, `lerc`, `webp`, `jpegxl`, `ccitt_t6`, `eer`, and
  `source` for old-style JPEG and compressions refkv has no name for. A bit-reversed FillOrder is
  a `bit_reverse` entry.
- HDF5: `h5_lz4`, `h5_szip`, `h5_bitshuffle` (one entry; its parameter chooses LZ4 or none, and its
  zstd mode), `h5_nbit`, `h5_scaleoffset`, `h5_fletcher32`, `lzf`, `blosc`, and `source` for other
  filters.
- FITS: `fits_rice`, `fits_hcompress`, `fits_plio`, `fits_quantize` (its parameters `scale`,
  `zero`, `seed` and `zblank` as columns, `method` a value; its decoding makes big-endian floats,
  so that the quantized chain and the GZIP_COMPRESSED_DATA chain end in the same bytes).
- GRIB: `grib_unpack`, whose parameters are places (the message's sections 3 and 5 and its
  bitmap element, or an earlier field's bitmap under indicator 254), and whose decoding makes
  64-bit floats with NaN at masked points.
- NRRD: `skip` (`length`, a value: the decoded bytes before the elements, NRRD's `byte skip`).
- VSI: `bmp`, and `ljpeg` if its lossless JPEG is not an ISO 10918-1 stream.
- DICOM: `dicom_rle` (its decoding makes planar samples, most significant byte plane first).
- CZI: CZI's LZW is `tiff_lzw` if it is TIFF's; otherwise an entry of its own.

## The formats

Sizes are a PR's rough lines of Rust, spec and tests together.

### TIFF and QPTIFF

Large (about 1,500 lines).
- **Arrays.** One per IFD with an image (`ifds/<i>`, SubIFDs, pointer IFDs), thumbnails and labels
  included, keyed by the IFD's struct. OME planes and levels are assembly: the facts keep them.
  Each file of an OME-TIFF set describes its own IFDs, and `tiff_set` renumbers them on graft.
- **Shape.** Always rank 3, as TIFF declares SamplesPerPixel (default 1): chunky `[H, W, S]`,
  planar `[S, H, W]`. `item_bits` is BitsPerSample; 1-bit and 12-bit samples take `pad_from`.
- **Grid.** Tiles `Each(TL), Each(TW)` (and `Each(S)`), `full`; strips `Each(min(RPS, H))`,
  `short`. Units are the `tiles/<j>` and `strips/<j>` elements; a strip or tile of count 0, or
  outside the file, has no row.
- **JPEG rows.** A JPEG tile's recipe splices its tables in, so its row names the raw stream as
  the element with a `range` of its whole extent. TIFF's second PR replaces the prefix.
- **Fallbacks.** No grid for mixed BitsPerSample, YCbCr subsampling outside JPEG, and a tile count
  that does not match the image.
- **Declarations.** Tags 256, 257, 258, 259, 262, 277, 284, 317, 322, 323, 338 and 339, and
  `header/byte_order`.
- **Checker.** tifffile per page: `shaped`, `chunks`, `dataoffsets`, `databytecounts`,
  `tags[347]`, and `page.decode` of each unit.
- **QPTIFF** is parsed as TIFF and comes with this PR, tested on its accepted fixtures.

### NDPI

Medium, after TIFF.
- **Units.** With McuStarts, each restart interval is a unit, a range of the strip element, on
  `Each(mh), Each(R·mw), Each(3)`, `full`, as tifffile reads NDPI. Without it, the strip.
- **Codec.** `jpeg` with `header` (the strip's start up to its first scan) and `color_transform`.
- **JPEG XR IFDs** take `item_bits` and samples from the first stream's PixelFormat, whose range is
  a declaration. A macro or map IFD whose stream the parser does not read has no grid.
- **Checker.** tifffile, comparing McuStarts by hand: tifffile reads them only with tag 271, which
  the fixtures lack, and adding it would change their outputs.

### VSI

Medium, after TIFF.
- **Arrays.** Each ETS file's levels, keyed by its `ets` element with the level as index, and the
  `.vsi`'s IFDs as TIFF's are.
- **Shape.** `[d_k … d_2, h, w, S]` over every coordinate of `dimension_sizes`; the collapse to
  t, c and z is the profile's. Levels after 0 take Bio-Formats' halving rule, as today; the
  format's document says so.
- **Tiles outside a level's computed size** have no row. The IR keeps their elements, and the
  document lists them as bytes no array names.
- **Fill.** Given, as a place in the `ets` header, when the background's samples are equal;
  otherwise `undefined`, with the header among the declarations.
- **Checker.** Bio-Formats (shapes, tile sizes, decoded tiles, minding BGR and the resolution
  level), and tifffile for the `.vsi`'s IFDs.

### HDF5, NWB and IMS

Large (about 2,500 lines), from the spike's branch, about 70% of which carries over.
- **Arrays.** Every visited object with a layout message, keyed by its `objects/<i>` struct.
  Attributes are not stored arrays.
- **Spike rework.** `shape: None` for the null dataspace; the codec names above; `source` with the
  filter's name; shuffle, szip and bitshuffle configuration as places in the filter message (its
  client data), not copied as values, by [§3.1](../../spec/stored-array.md#31-chains)'s rule for configuration the source defines once; the
  spike's
  second filter parser folded into `parse_filters`; an implicit index whose chunk is its element's
  whole extent names the element alone.
- **The note's duties**
  ([ir-array-model.md](ir-array-model.md#order-of-work)): the fill value's states kept in
  `Dataset`; a shared fill message resolved through the shared-message heap; a wrong-sized fill
  tested against the library, falling back to `undefined` with the message among the declarations;
  chunks compared with `get_chunk_info_by_coord`; listing stopped once an array's units alone pass
  the map's limit.
- **Crafted fixtures** for undefined, shared, old-style (message 0x04) and wrong-sized fills, and
  nbit and bzip2 filters, written with h5py's low-level API where it can.
- **IMS** reads through HDF5 and comes with this PR: its stored shape is the padded dataspace, and
  cropping to `ImageSize` is the profile's.
- **Checker.** h5py.

### FITS

Medium-large (about 1,000 lines).
- **Arrays.** Keyed by `hdus/<i>/header`, always with an index: an image or a compressed image 0;
  a table's rows 0 and its heap 1 (described even when empty).
- **Shape.** NAXISn…NAXIS1 (ZNAXIS for a compressed image) with `axes` reversed, as a Fortran-order
  `.npy`'s; rows `[NAXIS2]` of 8·NAXIS1 bits; heap `[H]` of 8.
- **Units.** An image, rows or heap is one unit. A compressed image's are its tiles,
  `Each(ZTILEj)`, `short`, each a range of today's `heap/<j>` element: the describe-only PR keeps
  `tile_cut`, and FITS's second PR moves it. An absent tile has no row.
- **Chains.** Chain 0 is `[fits_quantize, compressor]` for a quantized image, else
  `[compressor]`; chain 1 is `[gzip]` for tiles in GZIP_COMPRESSED_DATA. The first real use of
  columns and of a second chain.
- **Fallback.** Rice output of a width that is not the elements' gives `source`.
- **Checker.** astropy with `disable_image_compression=True` for each tile's bytes and
  parameters, and its codecs for the decoding.

### GRIB

Medium-large (about 500 lines).
- **Arrays.** One per field, keyed by `messages/<i>` with the field's position in its message as
  index. Shape `[Nj, Ni]`, or `[points]` for reduced grids; `item_bits` 64; fill `undefined`.
- **Unit.** The field's section 7 element.
- **Chains.** Each field's `grib_unpack` names its own places, so each field has a chain of its
  own: about 13 description entries a field, within the descriptions' limit of one per four bytes
  for any field of 52 bytes or more.
- **Fallbacks.** A field left out for its meaning is described fully; one left out for its
  storage (grid template, packing, a bitmap indicator 1–253) has no grid.
- **Checker.** eccodes.

### NIfTI

Small for plain files and pairs; medium for gzip.
- **Arrays.** Keyed by `header`. Shape `dim[n]…dim[1]` with `axes` reversed; `item_bits` from the
  datatype (RGB24 is 24, with no sample axis); one empty chain; fill `undefined`.
- **Gzipped files.** The header lies in the stream, which no element holds today. The PR adds one
  derived element of the stream (its inflated bytes, through the header, extensions and voxels),
  as `.fits.gz` already has, so that the header is a place and the voxels a unit. The mirror's
  forms change for gzipped files, and the PR names how. A helper in `gz.rs` serves MRC too.
- **Checker.** nibabel.

### MRC and MRC datasets

Small; medium with gzip.
- **Arrays.** Keyed by `header`. Shape `[nz, ny, nx]` with `axes` `[2, 1, 0]`; `item_bits` by mode.
- **Mode 101.** 4-bit items, `pad_from` 2, chain `[lsb_first(4)]`.
- **Gzipped files** as NIfTI's, by the same helper.
- **MRC datasets** have no arrays of their own: they reuse the file's description, and the PR
  checks their outputs are unchanged.
- **Checker.** mrcfile; for modes 3 and 101, which it does not read, the test's nibble decoder.

### NRRD

Small-medium.
- **Arrays.** Keyed by the header file's root element (the header has no element of its own), with
  its `dimension`, `type`, `sizes` and `endian` lines as declarations, which the parser starts
  recording. A detached data file is an object of the store, so its unit is an element there.
- **Chains.** Raw `[]`; gzip `[skip, gzip]` (encoding order), since `byte skip` counts inflated
  bytes.
- **Checker.** pynrrd.

### safetensors

Small-medium.
- **Arrays.** Keyed by `header`, each tensor's index its position in the header. F4 and F6 are the
  first items under a byte: `item_bits` 4 and 6, chain `[lsb_first]`. The store form is the first
  store whose parser builds its descriptions itself, with no graft.
- **Checker.** `safetensors.deserialize`, every dtype included.

### DICOM

Medium.
- **Arrays.** Pixel Data, keyed by `dataset/7FE00010`, and the palette's three LUT data elements,
  each keyed by itself, with `item_bits` 16 as their VR OW stores them (8-bit entries in 16-bit
  words are the profile's to read). An icon's Pixel Data lies in sequence items, which may fold,
  and is not described; the document says so.
- **Shape.** `[N, Rows, Cols, S]`, or `[N, S, Rows, Cols]` for planar native and RLE data, or the
  whole-slide image's total matrix; N kept when 1.
- **Units.** A native frame is a run member (`member`); a frame of one fragment is its
  `fragments/<j>` element; a frame of several is `ranges` of the parsed source. Frames whose bits
  do not end on a byte make the array one unit. ND2 and DICOM are the first real `member` rows.
- **Codecs.** `jpeg`, `jpegls`, `jpeg2k`, `dicom_rle`, and `lsb_first(1)` for 1-bit data.
- **Checker.** pydicom: `generate_frames` for each row's bytes, `pixel_array` for the decoding.

### CZI

Medium-large.
- **Arrays.** One per subblock, keyed by its `subblocks/<i>` struct, one unit each; placement,
  overlap and pyramids are the plan's. Shape is the coded size from the codec's header. An
  unplaced subblock with no coded size has no grid.
- **Codecs.** `jpeg`, `jpegxr`, `zstd`, `[shuffle, zstd]` for Zstd1 hi-lo, and CZI's LZW. Zstd1's
  header is its own element, a declaration.
- **Declarations.** The directory entry and dimensions; a part file's master entry, which no
  element of the part's IR holds, as an extent of the master's object.
- **Grafts.** `czi_set` is the first graft of many descriptions (about 10^5 in a large slide).
- **Checker.** czifile's subblock directory and `decode`.

### ND2

Medium.
- **Arrays.** One, keyed by `chunks/ImageAttributesLV!`: shape `[N, H, W, C]`, N the declared
  `uiSequenceCount`.
- **Units.** Each frame's `pixels` element, a run member, whose `rows` recipe already drops the
  padding; chain `[zlib]` where compressed.
- **Checker.** nd2's attributes, frame offsets and `read_frame`.

### LIF and XLEF

Medium.
- **Arrays.** One per image, keyed by the XML element that declares it, with the image's position
  in it as index. The declaration is that element; a character range inside its UTF-16 text is not
  given.
- **Units.** One per image where its strides nest; rows or planes as ranges where they do not
  (padded rows, planar channel offsets); no grid where they do not tile it (zero strides).
- **Checker.** Bio-Formats, which reads LIF, for shapes and decoded planes; readlif is not a
  dependency.

### SAFE

Medium.
- **Arrays.** One per band file, keyed by its `codestream` struct. Grid `Each(tile)`, `short`.
- **Units.** Each `tiles/<k>` element, its tile-parts with their SOT; chain
  `[jpeg2k(main_header)]`. The SIZ refkv rebuilds and the empty-tile tail are the projection's.
- **Checker.** rasterio's block shapes and window reads, and imagecodecs for each tile.

## Order of work

1. **Step 0.**
2. **The formats**, each its own PR to main, in any order but for TIFF before NDPI and VSI, and
   NIfTI before MRC. They touch their own parsers and append to the spec's [§3.2](../../spec/stored-array.md#32-the-list) and [§7](../../spec/stored-array.md#7-the-formats), so they
   can run side by side, three or four at a time, and merge in any order.

When every format here has its description, ir-array-model.md's step 2 and profile-plugins'
steps 3 and 4 are left with each format's second PR.

## Alternatives considered

- **Describing each format inside its plan PR**, as ir-array-model.md first said. It waits on the
  machine, and settles each format's codecs one plan at a time.
- **A column for a unit's decoded shape.** It would put a rank-3 TIFF's rows past the map limit's
  guarantee, and repeat what the grid already says.
- **A bit-order flag on the description** in place of `lsb_first`. It changes the type for three
  formats where one codec does.
- **Moving FITS's `tile_cut` in its describe-only PR.** It changes the mirror for every compressed
  image, which the second PR changes anyway.

## Open questions

1. Whether VSI's lossless JPEG, and CZI's LZW, are the standard ones, which decides whether they
   share `jpeg` and `tiff_lzw`. Each PR answers it from its reference reader.
