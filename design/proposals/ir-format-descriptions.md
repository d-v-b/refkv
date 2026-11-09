# Proposal: every format describes its stored arrays

Status: proposed. Follows [ir-array-model.md](ir-array-model.md), whose step 1 (the type, and
NumPy's descriptions) has landed.

## The change

Every format whose parser reaches arrays describes them on the stored-array type
([spec/stored-array.md](../../spec/stored-array.md)), in one describe-only PR per format, tested
against the format's reference reader. [profile-plugins.md](profile-plugins.md#order-of-work)
gives each format two PRs in its steps 3 and 4. Of the first, which lists the format's known
structures and specifies its description, a describe-only PR takes the stored arrays; the known
structures and the rest of the description stay in that first PR. The second PR (the plan, the
facts' removal, the sizing decisions) stays after the plan's machine.

The describe-only PRs do not need the machine: NumPy's description landed without it, and an HDF5
spike described every HDF5 dataset without it
([ir-array-model.md](ir-array-model.md#order-of-work)). Landing them first tests every format's
description against its reference reader before any plan reads it, and settles the codec list
once. They may land before the machine, and the machine does not wait for them.

A describe-only PR changes no output. It changes the IR mirror's forms only where a declaration
or unit needs a place the IR does not have (a gzipped header, [§Gzipped files](#gzipped-files)),
and names each such change.

### In scope

TIFF with QPTIFF, NDPI, VSI, HDF5 with NWB and IMS, FITS, GRIB, NIfTI, MRC with MRC datasets,
NRRD, safetensors, DICOM, CZI, ND2, LIF with XLEF, SAFE, and NumPy's deflated `.npz` members.

### Not in scope

- **Coded streams** (MP4, MP3, FLAC), which need a description of their own kind
  ([ir-array-model.md](ir-array-model.md#not-in-this-proposal)).
- **The store profiles** (Zarr v2 with OME-Zarr, Zarr v3, N5, precomputed), whose array documents
  already describe their arrays.
- **DICOM series**, whose store form is composed in Python and has no IR of its own; it gets its
  description when profile-plugins' step 4 gives it a store IR that grafts the files' IRs.
- **HDF5 attributes.** They are arrays the source declares, but values the IR already holds as
  elements, read whole; a description would tell a profile nothing it computes on. They are
  described when a profile needs one, and HDF5's document says so.
- **Each format's second PR**: moving FITS's `tile_cut` to the projection, replacing TIFF's JPEG
  prefix, and deleting facts the descriptions duplicate. Until then a fact and a description say
  the same thing twice; each describe-only PR lists the facts its description duplicates.
- **Lifting a parser's refusals.** A file the parser refuses today (FITS's HCOMPRESS and PLIO,
  a NULL_PIXEL_MASK) stays refused, and its codecs wait for the PR that lifts the refusal.

## Rules every format PR follows

1. **Every array the parser reaches is described**, presented or not: a TIFF thumbnail, an
   unpresented HDF5 dataset, a CZI subblock the plan leaves out, a DICOM icon. An array whose
   storage the IR does not model has no grid ([§2.3](../../spec/stored-array.md#23-the-grid)).
   Structures that are not arrays the source declares (a CZI segment no directory entry names)
   stay bytes, and the format's document says so.
2. **The facts give each description's key**, the declaring element's id, as NumPy's
   `declared_by` does. A format whose IRs are grafted renumbers it in its store code. Where the
   facts do not record the declaring element (HDF5's `objects/<i>`, GRIB's `messages/<i>`, NRRD's
   header lines), the PR adds it. A key is never in a run's subtree: an array declared inside a
   folded item is keyed by the nearest element above the run that declares it, with the item's
   position as index.
3. **Descriptions are built on the finished IR.** `check::finish` may make an element an alias or
   unfold a run, so a parser that cached element ids while parsing finds them again after finish.
   A row names the unit's bytes as finish leaves them: a run member's element with `member`; for
   an alias, its target alone when their extents agree, a `range` of the target when the alias
   lies inside it, and `ranges` of the parsed source otherwise.
4. **No typing.** `item_bits` is the element's size as the source stores it, or as the chain's
   last codec makes it ([§Step 0](#step-0-the-shared-rules)). Data type names, scaling (BSCALE,
   `scl_slope`), an axis the source does not declare (HDF5's string axis), a fill of zero, a crop
   to a reader's size and a transposition the output makes stay in the facts, for the profile.
5. **Declarations are listed** in the format's document, place by place, so that a profile knows
   what each is. A declaration the parser keeps only in facts today gets a place in the same PR.
6. **Codecs come from the list** that step 0 lands ([§Codecs](#codecs)). A format PR that finds an
   entry wrong fixes it in its own PR and says so.
7. **Tests.** One test runs every fixture of the format (and the PR's crafted ones) against the
   reference reader: shapes, `item_bits`, grids, each row's bytes against the reader's offsets and
   lengths, and each unit decoded through its chain (by step 0's helper) against the reader's
   decoded unit. One test per refusal and per fallback. Fixtures the corpus lacks are crafted in
   the fixture generators, where the reference library or the generator can write them.
8. **No output change.** `just cli-identical` and the fixture comparison show none; the PR names
   each mirror change the change above allows, and any fixture whose array falls back at the map's
   limit.
9. **The spec's [§7](../../spec/stored-array.md#7-the-formats)** gets the format's paragraph, in
   NumPy's form: its arrays and keys, shape and axes, `item_bits`, grid and units, chains, fill,
   declarations, and fallbacks.

## Step 0: the shared rules

One PR before the formats, changing spec/stored-array.md, the IR's checks and the test helpers.

### A codec sees the unit's shape

A codec's decoding is given the unit's decoded shape (its grid unit, cut at the array's end under
`short`, whole under `full`) and the array's `item_bits` and `pad_from`, as Zarr gives a codec its
chunk's specification. These describe what the chain ends in. A codec that is not the chain's last
and makes bytes of another width (FITS's Rice decoding to integers of BYTEPIX bytes under
quantization) has that width as a parameter of its own. A codec still never sees the unit's
coordinates.

[§3.1](../../spec/stored-array.md#31-chains)'s sentence "a parameter that depends on the unit's
position is a column its parser computes" becomes: a parameter that depends on the unit's position,
other than through its shape, is a column. TIFF's LZW and CCITT, EER, JPEG's frame size and DICOM's
RLE need the decoded size, and a short last strip's differs from the others'; without this rule each
would be a column, and a TIFF row would pass the map limit's guarantee of three entries.

### Parameters the source gives once

A parameter is a value or a place, as [§2.2](../../spec/stored-array.md#22-places-and-parameters)
allows, and configuration the source gives once is a place
([§3.1](../../spec/stored-array.md#31-chains)). A codec's entry gives the parameter's meaning; how
a place's bytes make it is the format's, since one codec's configuration is spelled differently in
each source (HDF5's filter client data, a FITS card, an NRRD line, a TIFF tag). Each format's
document says, for every place it gives as a parameter, how its bytes make the value. A value
stands only where the format fixes it rather than the file (CZI's hi-lo split is a shuffle of two
bytes, always).

### Codec-made elements

An image codec makes samples, not stored bytes. Its entry states them: each sample little-endian,
in the smallest of 8, 16 or 32 bits that holds the stream's precision, in the order of its
components as coded, with no color conversion (a JPEG's YCbCr stays YCbCr). The array's
`item_bits` is that size, and the precision the source declares (BitsPerSample 12) is a
declaration. A profile converts colors from the declarations (TIFF's Photometric) when it presents
them.

### `lsb_first`

`lsb_first` (parameter `bits`, 1 to 7) takes items of `bits` bits packed least significant bit
first: bit k of the unit is bit k mod 8, counted from the least significant, of byte ⌊k/8⌋, and an
item's first bit is its least significant. Decoding packs them most significant bit first, as
[§3.1](../../spec/stored-array.md#31-chains) packs elements, and a given fill packs as decoded.
Where `bits` does not divide 8 and the array has `pad_from`, its padding falls inside an item, and
the array's chain is `source` instead. `lsb_first(1)` reverses each byte's bits, so it is also
TIFF's FillOrder 2, and the note's `bit_reverse` is not added. DICOM's 1-bit data, MRC's mode 101
and safetensors' F4 and F6 use it.

### The test helper

`python/tests/ir/chains.py` decodes a unit through a chain, codec by codec, by imagecodecs or the
reference library, given the unit's bytes, its parameters (places read from the source) and its
shape. Step 0 gives every codec of [§Codecs](#codecs) an entry in it, which raises until the
format that first uses the codec implements it.

### The codec list

Step 0 adds every entry of [§Codecs](#codecs) to
[§3.2](../../spec/stored-array.md#32-the-list) and to the IR's list in `array.rs`, so that no two
format PRs write the same entry and the format PRs merge in any order.

## Codecs

A name follows [§3.2](../../spec/stored-array.md#32-the-list): it states the byte format its
decoding takes, its framing included, and covers two framings only where a parameter chooses. This
list supersedes the sketch in [ir-array-model.md](ir-array-model.md#codecs).

**Compression and framing**

| Codec | Takes | Parameters | Used by |
| --- | --- | --- | --- |
| `zlib` | RFC 1950, with its header and Adler-32 | | TIFF (compressions 8 and 32946), HDF5 (deflate), ND2, LERC's deflate |
| `gzip` | RFC 1952, one or more members | | FITS (GZIP_1), NRRD |
| `zstd` | Zstandard frames (RFC 8878), one or more | | TIFF, HDF5 (32015), CZI, LERC's zstd |
| `xz` | the .xz container | | TIFF |
| `bzip2` | the .bz2 stream | | HDF5 (307) |
| `lzf` | LZF with no header | | HDF5 (32000) |
| `blosc` | Blosc's own frame | | HDF5 (32001) |
| `h5_lz4` | HDF5's LZ4 block layout | | HDF5 (32004) |
| `h5_szip` | a 4-byte length, then the AEC stream | `options`, `pixels_per_block`, `bits_per_pixel`, `pixels_per_scanline` | HDF5 (4) |
| `h5_bitshuffle` | HDF5's bitshuffle blocks | `element_size`, `block_size`, `compression` (none, LZ4 or zstd, which frame the blocks) | HDF5 (32008) |
| `tiff_lzw` | TIFF's LZW, codes most significant bit first | | TIFF, CZI if its LZW is TIFF's |
| `packbits` | PackBits runs | | TIFF |
| `fits_rice` | FITS's Rice blocks | `block_size`, `byte_pix` | FITS (RICE_1) |
| `skip` | the elements after `length` bytes | `length` | NRRD's `byte skip` |

**Transforms**

| Codec | Decoding | Parameters | Used by |
| --- | --- | --- | --- |
| `shuffle` | ungroups bytes by their position in elements of `element_size` | `element_size` | HDF5 (2), FITS's GZIP_2, CZI's hi-lo |
| `fletcher32` | checks and drops HDF5's appended checksum | | HDF5 (3) |
| `nbit` | HDF5's N-bit unpacking | the filter's client data | HDF5 (5) |
| `scaleoffset` | HDF5's scale-offset decoding | the filter's client data | HDF5 (6) |
| `tiff_predictor` | undoes horizontal (2) or floating-point (3) differencing along each row | `predictor`, `byte_order`, `planar_configuration` | TIFF |
| `fits_quantize` | makes big-endian floats from quantized integers | `method`, `scale`, `zero`, `seed`, `zblank` | FITS |
| `lsb_first` | as step 0 | `bits` | DICOM, MRC, safetensors, TIFF's FillOrder 2 |

**Images**, each making samples as step 0 says

| Codec | Takes | Parameters | Used by |
| --- | --- | --- | --- |
| `jpeg` | an ISO 10918-1 stream, any process | `tables`: an abbreviated table stream (TIFF's tag 347); `header`: the stream up to its first scan, the unit being the scan after it (NDPI) | TIFF, NDPI, VSI, DICOM, CZI |
| `jpegls` | an ISO 14495-1 stream | | DICOM |
| `jpeg2k` | an ISO 15444-1 codestream (15444-15's high throughput included), not a JP2 file | `main_header`: the unit is then one tile's tile-parts, and decoding makes that tile alone | TIFF, VSI, DICOM, SAFE |
| `jpegxr` | an ITU-T T.832 codestream | | NDPI, CZI |
| `jpegxl` | an ISO 18181 codestream or container | | TIFF |
| `webp` | a WebP file | | TIFF |
| `png` | a PNG file | | VSI |
| `bmp` | a BMP file | | VSI |
| `lerc` | a LERC2 blob | | TIFF, in `[lerc, zlib]` or `[lerc, zstd]` as LercParameters say |
| `ccitt_t6` | ITU-T T.6 data | | TIFF (4) |
| `eer` | EER's run-length bits | `skip_bits`, `horizontal_bits`, `vertical_bits` | TIFF (65000 to 65002) |
| `dicom_rle` | DICOM's RLE segments, byte planes most significant first | | DICOM |
| `grib_unpack` | a GRIB field's section 7 | sections 3 and 5, and the bitmap | GRIB |

Every image codec takes its frame's size from the unit's shape, not from the stream's headers, so
NDPI's frame that is a row short needs no spliced SOF. `jpeg2k` with `main_header` decodes the
main header with the tile's size (from the unit's shape) and the tile-parts as tile 0.
`dicom_rle` rebuilds each sample from its byte planes, so it makes planar samples of Bits
Allocated / 8 bytes, little-endian. `grib_unpack` makes 64-bit floats, little-endian, row-major
in the order section 3's scanning mode gives (alternate rows reversed under bit 0x10, columns first
under 0x20, both undone), with NaN where the bitmap masks a point.

`source` stays the fallback: old-style JPEG, a compression or filter refkv has no name for.

## The formats

Sizes are rough lines of Rust, spec and tests together.

### TIFF and QPTIFF

Large (about 1,500 lines).
- **Arrays.** One per IFD with an image (`ifds/<i>`, SubIFDs, pointer IFDs), thumbnails and
  labels included, keyed by the IFD's struct. OME planes and levels are assembly: the facts keep
  them. Each file of an OME-TIFF set describes its own IFDs, and `tiff_set` renumbers them on
  graft.
- **Shape**, as [ir-array-model.md](ir-array-model.md#what-an-array-is) says: `[H, W, S]` chunky,
  `[S, H, W]` planar, `[H, W]` for one sample. `item_bits` is BitsPerSample, or the container a
  codec makes; a size not a multiple of 8 takes `pad_from` at the row (1 for `[H, W]` and chunky,
  2 for planar).
- **Grid.** Chunky tiles `[Each(TL), Each(TW), Each(S)]`, planar `[Each(1), Each(TL), Each(TW)]`,
  under `full`; strips the same with `Each(min(RPS, H))` and W whole, under `short`. Units are the
  `tiles/<j>` and `strips/<j>` elements; one of count 0, or outside the file, has no row.
- **JPEG rows.** A JPEG tile's recipe splices its tables in, so its row names the raw stream as
  the element with a `range` of its whole extent. TIFF's second PR replaces the prefix.
- **Chains.** `[tiff_predictor, compression]`, with `lsb_first(1)` first in decoding under
  FillOrder 2 for the compressions libtiff reverses for.
- **Fallbacks.** No grid for mixed BitsPerSample, YCbCr subsampling outside JPEG, and a tile count
  that does not match the image.
- **Declarations.** Tags 256, 257, 258, 259, 262, 266, 277, 284, 317, 322, 323, 338, 339 and
  347, LercParameters, and `header/byte_order`.
- **Checker.** tifffile per page: `shaped`, `chunks`, `dataoffsets`, `databytecounts`, and
  `page.decode` of each unit, compared after converting colors from Photometric.
- **QPTIFF** is parsed as TIFF and comes with this PR, tested on its accepted fixtures.

### NDPI

Medium, after TIFF.
- **Units.** With McuStarts, each restart interval is a unit, a range of the strip element, on
  `[Each(mh), Each(R·mw), Each(3)]`, `full`, as tifffile reads NDPI, in `[jpeg(header)]`. Without
  them, the strip, in `[jpeg]`, whose frame size comes from the unit's shape.
- **JPEG XR IFDs** take `item_bits` and samples from the first stream's PixelFormat, whose range is
  a declaration. A macro or map IFD whose stream the parser does not read has no grid.
- **Checker.** tifffile, comparing McuStarts by hand: tifffile reads them only with tag 271, which
  the fixtures lack, and adding it would change their outputs.

### VSI

Medium, after TIFF.
- **Arrays.** Each ETS file's levels, keyed by its `ets` element with the level as index, and the
  `.vsi`'s IFDs as TIFF's are.
- **Shape.** `[d_k … d_2, h, w, S]` over every coordinate of `dimension_sizes`, the collapse to t,
  c and z being the profile's. A level's `h` and `w` are its stored tiles' extent, the largest tile
  coordinate plus one times the tile size, under `full`: every tile has a row. Bio-Formats' halving
  rule, which crops the levels after 0, is the profile's, as IMS's crop to `ImageSize` is.
- **Fill.** Given, as a place in the `ets` header, when the background's samples are equal and the
  tiles are raw (the header's byte order is the stored one); otherwise `undefined`, with the header
  among the declarations.
- **Checker.** Bio-Formats (shapes, tile sizes, decoded tiles, minding BGR and the resolution
  level), and tifffile for the `.vsi`'s IFDs.

### HDF5, NWB and IMS

Large (about 2,500 lines), from the spike's branch, about 70% of which carries over.
- **Arrays.** Every visited object with a layout message, keyed by its `objects/<i>` struct.
- **Spike rework.** `shape: None` for the null dataspace; the codec names above; `source` with the
  filter's name; filter configuration as places in the filter message's client data, which HDF5's
  document says how to read; the spike's second filter parser folded into `parse_filters`; an
  implicit index whose chunk is its element's whole extent names the element alone.
- **The note's duties**
  ([ir-array-model.md](ir-array-model.md#order-of-work)): the fill value's states kept in
  `Dataset`; a shared fill message resolved through the shared-message heap; a wrong-sized fill
  tested against the library, falling back to `undefined` with the message among the declarations;
  chunks compared with `get_chunk_info_by_coord`; listing stopped once an array's units alone pass
  the map's limit.
- **Crafted fixtures** for undefined, shared, old-style (message 0x04) and wrong-sized fills, and
  the nbit and bzip2 filters.
- **IMS** reads through HDF5 and comes with this PR: its stored shape is the padded dataspace.
- **Checker.** h5py.

### FITS

Medium-large (about 1,000 lines).
- **Arrays.** Keyed by `hdus/<i>/header`, the index each array's position in its HDU's list: an
  image (0); a table's rows and heap (0 and 1, the heap described even when empty); a compressed
  image's image, rows and heap (0, 1 and 2). An HDU of NAXIS 0 holds no array.
- **Shape.** NAXISn…NAXIS1 (ZNAXIS for a compressed image) with `axes` reversed, as a Fortran-order
  `.npy`'s; rows `[NAXIS2]` of 8·NAXIS1 bits; heap `[H]` of 8.
- **Units.** An image, rows or heap is one unit. A compressed image's are its tiles,
  `Each(ZTILEj)`, `short`: each a range of today's `heap/<j>` element, or the element alone where
  the tile is all of it. The describe-only PR keeps `tile_cut`. An absent tile has no row.
- **Chains.** Chain 0 is `[fits_quantize, compressor]` for a quantized image, else
  `[compressor]`, where the compressor is `fits_rice`, `gzip`, or `shuffle` then `gzip` for GZIP_2;
  chain 1 is `[gzip]` for tiles in GZIP_COMPRESSED_DATA. `fits_quantize`'s `scale`, `zero` and
  `zblank` are columns where the table has them and places in the header where it does not; `seed`
  is a column (the tile's index plus ZDITHER0); `method` a place. The first real use of columns
  and of a second chain.
- **Fallback.** An unquantized image whose BYTEPIX is not its elements' width gives `source`.
- **Checker.** astropy with `disable_image_compression=True` for each tile's bytes and
  parameters, and its codecs for the decoding.

### GRIB

Medium-large (about 500 lines).
- **Arrays.** One per field, keyed by `messages/<i>` with the field's position in its message as
  index. Shape `[Nj, Ni]`, or `[points]` for reduced grids; `item_bits` 64; fill `undefined`, since
  NaN at a masked point is what `grib_unpack` makes, not a fill.
- **Unit.** The field's section 7 element, in `[grib_unpack]`, whose places (sections 3 and 5, its
  bitmap or, under indicator 254, an earlier field's) are its own: each field has a chain of its
  own, about 13 description entries, within the descriptions' limit of one per four bytes for any
  field of 52 bytes or more.
- **Fallbacks.** A field left out for its meaning is described fully; one left out for its
  storage (grid template, packing, a bitmap indicator 1–253) has no grid.
- **Checker.** eccodes, its values reordered by the scanning mode where columns come first.

### Gzipped files

A gzipped NIfTI or MRC file's header and voxels lie in the stream, which no element holds today.
Their PRs give the file one derived element over its bytes in space 0, with `header` and the voxels
in its derived space, as [ir-array-model.md](ir-array-model.md#what-an-array-is) planned and as a
`.fits.gz` already has. Validating the mirror's view of the header re-derives the stream, while the
parser reads only its start: the PRs keep validation to the stream's start, and name the mirror's
changed forms. A helper in `gz.rs` serves both, and NumPy's deflated `.npz` members, whose
`header` and `data` become children of their derived space in a small PR after NIfTI's.

### NIfTI

Small for plain files and pairs; medium with gzip.
- **Arrays.** Keyed by `header`. Shape `dim[n]…dim[1]` with `axes` reversed; `item_bits` from the
  datatype (RGB24 is 24, with no sample axis); one empty chain; fill `undefined`.
- **Checker.** nibabel.

### MRC and MRC datasets

Small; medium with gzip; after NIfTI.
- **Arrays.** Keyed by `header`. Shape `[nz, ny, nx]` with `axes` `[2, 1, 0]`; `item_bits` by mode.
- **Mode 101.** 4-bit items, `pad_from` 2, chain `[lsb_first(4)]`.
- **MRC datasets** have no arrays of their own: they reuse the file's description, and the PR
  checks their outputs are unchanged.
- **Checker.** mrcfile; for modes 3 and 101, which it does not read, the test's nibble decoder.

### NRRD

Small-medium.
- **Arrays.** Keyed by the header file's root element (the header has no element of its own), with
  its `dimension`, `type`, `sizes`, `endian`, `encoding` and `byte skip` lines as declarations,
  which the parser starts recording. A detached data file is an object of the store, so its unit
  is an element there.
- **Chains.** Raw `[]`; gzip `[skip, gzip]` (encoding order), `skip`'s `length` the `byte skip`
  line, since NRRD counts it in inflated bytes.
- **Checker.** pynrrd.

### safetensors

Small-medium.
- **Arrays.** Keyed by `header`, each tensor's index its position in the header. F4 is 4 bits in
  `[lsb_first(4)]`; F6_E2M3 and F6_E3M2 are 6 bits in `[lsb_first(6)]`.
- **Checker.** `safetensors.deserialize` for each row's bytes; ml_dtypes' `float4_e2m1fn` for F4's
  values. Nothing independent unpacks F6, whose test rests on
  [safetensors.md](../../spec/zarrify/safetensors.md) alone.

### DICOM

Medium-large.
- **Arrays.** Pixel Data, Float Pixel Data and Double Float Pixel Data (7FE0,0010/0008/0009),
  keyed by their elements; each Overlay Data (60xx,3000), 1-bit in `[lsb_first(1)]`; the palette's
  three LUT data elements, shape their length / 2 in OW words of 16 bits (8-bit entries packed two
  to a word are the profile's to read, from the descriptor, a declaration); and an icon's Pixel
  Data, keyed by the Icon Image Sequence element with the item's position as index, its frame a run
  member where the items fold.
- **Shape.** `[N, Rows, Cols, S]`, or `[N, S, Rows, Cols]` for planar native and RLE data, N kept
  when 1. A whole-slide image's is its total matrix, with leading axes for its optical paths and
  focal planes, under `full`; a sparse tiling whose frame positions fall on the tile grid has a row
  at each, and one whose do not has no grid.
- **Units.** A native frame is a run member (`member`); a frame of one fragment is its
  `fragments/<j>` element; a frame of several is `ranges` of the parsed source. Frames whose bits
  do not end on a byte make the array one unit. ND2 and DICOM give the first real `member` rows.
- **Codecs.** `jpeg`, `jpegls`, `jpeg2k`, `dicom_rle`, and `lsb_first(1)` for 1-bit data.
- **Checker.** pydicom: `generate_frames` for each row's bytes, `pixel_array` for the decoding.

### CZI

Medium-large.
- **Arrays.** One per subblock, keyed by its `subblocks/<i>` struct, one unit each; placement,
  overlap and pyramids are the plan's. Shape is the directory entry's stored size, confirmed or
  replaced by the stream's frame size for JPEG, lossless JPEG and JPEG XR. An unplaced subblock
  with no size has no grid.
- **Codecs.** `jpeg`, `jpegxr`, `zstd`, `[shuffle, zstd]` for Zstd1's hi-lo, and CZI's LZW. Zstd1's
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
- **Checker.** Bio-Formats, which reads LIF, for shapes and decoded planes.

### SAFE

Medium.
- **Arrays.** One per band file, keyed by its `codestream` struct. Grid `Each(tile)`, `short`.
- **Units.** Each `tiles/<k>` element, its tile-parts with their SOT, in `[jpeg2k(main_header)]`.
  The SIZ refkv rebuilds and the empty-tile tail are the projection's.
- **Checker.** rasterio's block shapes and window reads, and the helper's `jpeg2k`, which rewrites
  SIZ and the tile index before imagecodecs decodes.

## Order of work

1. **Step 0.**
2. **The formats**, each its own PR to main, in any order but for TIFF before NDPI and VSI, and
   NIfTI before MRC and NumPy's deflated members. Each touches its own parser, its own paragraph of
   [§7](../../spec/stored-array.md#7-the-formats) and its document, so they run side by side, three
   or four at a time.

When every format here has its description, ir-array-model.md's step 2 and profile-plugins'
steps 3 and 4 are left with each format's known structures and second PR.

## Alternatives considered

- **Describing each format inside its plan PR**, as ir-array-model.md first said. It waits on the
  machine, and settles each format's codecs one plan at a time.
- **A column for a unit's decoded shape.** It would put a TIFF's rows past the map limit's
  guarantee, and repeat what the grid already says.
- **A bit-order flag on the description** in place of `lsb_first`. It changes the type for three
  formats where one codec does.
- **A `color_transform` parameter on `jpeg`** for the Adobe marker refkv splices in. It would be
  refkv's reading of the source's declarations, and converting colors is presentation.
- **Each format writing its own codec entries.** Fifteen PRs appending to one table and one list
  conflict, and a later format would find an earlier one's name already taken differently.
- **Moving FITS's `tile_cut` in its describe-only PR.** It changes the mirror for every compressed
  image, which the second PR changes anyway.

## Open questions

1. Whether VSI's lossless JPEG and CZI's LZW are the standard ones, which decides whether they are
   `jpeg` and `tiff_lzw`. Each PR answers it from its reference reader, and adds an entry if not.
