# Proposal: every format describes its stored arrays

Status: proposed. Follows [ir-array-model.md](ir-array-model.md), whose step 1 (the type, and
NumPy's descriptions) has landed.

## The change

Every format whose parser reaches arrays describes them on the stored-array type
([spec/stored-array.md](../../spec/stored-array.md)), in one describe-only PR per format, tested
against the format's reference reader. [profile-plugins.md](profile-plugins.md#order-of-work)
gives each format two PRs in its steps 3 and 4: the first lists the format's known structures and
specifies its description, the second moves its decisions into a plan. The stored-array part of
that first PR comes out as the describe-only PR, which may land before the plan's machine; the
known structures and the rest stay in the first PR, and the machine does not wait for any of them.
A format so has three PRs: the describe-only PR here, then profile-plugins' two.

The describe-only PRs do not need the machine: NumPy's description landed without it, and an HDF5
spike described every HDF5 dataset without it
([ir-array-model.md](ir-array-model.md#order-of-work)). Landing them first tests every format's
description against its reference reader before any plan reads it, and settles the codec list
once.

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
- **Each format's plan PR**: moving FITS's `tile_cut` to the projection, replacing TIFF's JPEG
  prefix, and deleting facts the descriptions duplicate. Until then a fact and a description say
  the same thing twice; each describe-only PR lists the facts its description duplicates.
- **Lifting a parser's refusals.** What a parser refuses today stays refused, and its arrays and
  codecs wait for the PR that lifts the refusal; among them FITS's HCOMPRESS, PLIO,
  NULL_PIXEL_MASK and UNCOMPRESSED_DATA, DICOM's Float and Double Float Pixel Data, sparse tilings
  and whole-slide images of several optical paths or focal planes, CZI's camera raw, NRRD's bzip2
  and text encodings, and ND2's compressed frames with padded rows.

## Rules every format PR follows

1. **Every array the parser reaches is described**, presented or not: a TIFF thumbnail, an
   unpresented HDF5 dataset, an HDF5 attribute, a CZI subblock the plan leaves out, a DICOM icon.
   An array whose storage the IR does not model has no grid
   ([§2.3](../../spec/stored-array.md#23-the-grid)).
2. **The facts give each description's key**, the declaring element's id, as NumPy's
   `declared_by` does. A format whose IRs are grafted renumbers it in its store code. Where the
   facts do not record the declaring element (HDF5's `objects/<i>`, GRIB's `messages/<i>`, NRRD's
   header lines), the PR adds it. A key is never in a run's subtree: an array declared inside a
   sequence of items (DICOM's icons, CZI's walked segments) is keyed by the items' nearest ancestor
   outside any run, its index the array's position among the arrays that ancestor declares, in the
   source's order, whether or not the items fold.
3. **Descriptions are built on the finished IR.** `check::finish` may make an element an alias or
   unfold a run, so a parser that cached element ids while parsing finds them again after finish.
   A row names the unit's bytes as finish leaves them: a run member's element with `member`; and
   where the unit's element became an alias, the unit's own bytes (the whole element, or its
   range) found in the alias's target: the target, named as the unit itself would be (alone where
   its recipe is the identity and the bytes are all of it, else with a `range`, its offset moved
   by the alias's offset in the target), where they lie inside it; `ranges` of the object that
   holds them (the parsed source in a file IR) where they do not; no grid where that would be
   inside a derived space.
4. **No typing.** `item_bits` is the element's size as the source stores it, or as the chain's
   first codec (decoded last) makes it ([§Step 0](#step-0-the-shared-rules)). Data type names,
   scaling (BSCALE, `scl_slope`), an axis the source does not declare (HDF5's string axis), a fill
   of zero, a crop to a reader's size, a color conversion and a transposition the output makes stay
   in the facts, for the profile. An array's axes are those the source declares: no frame axis
   where it gives no frame count, and no samples axis for one sample; a pixel it declares as
   several samples (TIFF's SamplesPerPixel above 1, NIfTI's RGB24, CZI's Bgr24) has a samples axis,
   last unless the layout is planar, of `item_bits` per sample.
5. **Declarations are listed** in the format's document, place by place, so that a profile knows
   what each is. A declaration the parser keeps only in facts today gets a place in the same PR.
6. **Codecs come from the list** that step 0 lands ([§Codecs](#codecs)). A format PR that finds an
   entry wrong fixes it in a small PR of its own to the list, which the format PR follows.
7. **Tests.** One test runs every fixture of the format (and the PR's crafted ones) against the
   reference reader: shapes, `item_bits`, grids, each row's bytes against the reader's offsets and
   lengths, and each unit decoded through its chain (by step 0's helper) against the reader's
   decoded unit, with no color conversion on either side beyond what a codec's entry says its
   decoding does. One test per refusal and per fallback.
   Fixtures the corpus lacks are crafted in the fixture generators, where the reference library or
   the generator can write them.
8. **No output change.** `just cli-identical` and the fixture comparison show none; the PR names
   each mirror change the change above allows, any fixture whose array falls back at the map's
   limit, and any crafted source the descriptions' limit refuses (none in the corpus).
9. **The spec's [§7](../../spec/stored-array.md#7-the-formats)** gets the format's paragraph, in
   NumPy's form: its arrays and keys, shape and axes, `item_bits`, grid and units, chains, fill,
   declarations, and fallbacks. `axes` reversed, here as in NumPy's, means for two or more axes,
   and covers the axes the source lists; a samples axis the pixel type adds stays last.

## Step 0: the shared rules

One PR before the formats, changing spec/stored-array.md, the IR's codec list and checks, and the
test helpers. It also adds to [§7](../../spec/stored-array.md#7-the-formats) one heading per format,
in the order of [§The formats](#the-formats), each holding a line that the format is not described
yet, so that each format PR fills its own heading and none appends to the end of the file; and it
rewords [§1](../../spec/stored-array.md#1-stored-arrays)'s "Today only the NumPy profile describes
its arrays" to point at [§7](../../spec/stored-array.md#7-the-formats)'s headings, which no format
PR then edits.

### A codec sees the unit's shape

A codec's decoding is given the unit's decoded shape (its grid unit, cut at the array's end under
`short`, whole under `full`) and the array's `item_bits` and `pad_from`, as Zarr gives a codec its
chunk's specification. These describe what the chain ends in. A codec other than the chain's
first, whose decoding makes bytes of another width (FITS's Rice), has that width as a parameter of
its own. A codec still never sees the unit's coordinates.

[§3.1](../../spec/stored-array.md#31-chains)'s sentence "a parameter that depends on the unit's
position is a column its parser computes" becomes: a parameter that depends on the unit's
position, other than through its shape, is a column. TIFF's LZW and CCITT, EER, the image codecs'
frame sizes and DICOM's RLE need the decoded size, and a short last strip's differs from the
others'; without this rule each would be a column, and a TIFF row would pass the map limit's
guarantee of three entries.

### Parameters the source gives once

A parameter is a value or a place, as [§2.2](../../spec/stored-array.md#22-places-and-parameters)
allows, and configuration the source gives once is a place
([§3.1](../../spec/stored-array.md#31-chains)). A codec's entry gives the parameter's meaning and
type; how a place's bytes make it is the format's, since one codec's configuration is spelled
differently in each source (HDF5's filter client data, a FITS card, an NRRD line, a TIFF tag). So
[§3.2](../../spec/stored-array.md#32-the-list)'s last bullet ("for a parameter given as a place, how
the parameter is made from those bytes") moves from the codec's entry to the format's document, and
its closing sentence ("Each format adds its codecs to the list as it describes its arrays") gives
way to step 0's list. The format's document says it for every place it gives as a parameter. A value
stands where the format fixes it rather than the file (CZI's hi-lo split is a shuffle of two bytes,
always). Where the file omits a card or tag whose default the format gives (FITS's BLOCKSIZE 32 and
BYTEPIX 4, EER's bit counts), the parameter is absent and the codec's entry gives the default, since
the IR holds no default the source does not write
([§1](../../spec/stored-array.md#1-stored-arrays)).

### Codec-made elements

A codec that makes elements rather than restoring stored bytes (the image codecs, GRIB's
unpacking, DICOM's RLE) states them in its entry. The image codecs make the samples their stream
declares, little-endian: an integer stream's in the smallest of 8, 16 or 32 bits that holds its
precision, a float stream's at its own width; in the components' coded order; with every component
at full resolution. A transform that is part of the codec's coding is undone (JPEG 2000's multiple
component transform, JPEG-LS's color transform, JPEG XR's internal color formats, WebP's YUV, JPEG
XL's XYB, each to the components its stream declares); a colorspace a 10918-1 stream only signals
stays (its YCbCr, under JFIF or an Adobe marker). Upsampling and the two decoders' own conversions
are what the readers here all do, since no reader offers another:
- a 10918-1 stream's subsampled components are upsampled as libjpeg does by default (its "fancy"
  triangle filter), which is deterministic;
- a JPEG 2000 codestream of three components whose second and third are subsampled is taken, as
  OpenJPEG takes it, for sYCC: upsampled and converted to RGB. Its SIZ, which says so, is a
  declaration of every array `jpeg2k` decodes, so that a profile knows the samples are RGB and
  does not convert them again.

A decoder that does otherwise makes other bytes. The array's `item_bits` is the samples' size, and
the precision the source declares (BitsPerSample 12) is a declaration. A profile converts colors
from the declarations (TIFF's Photometric) when it presents them; where the format declares no
color space (CZI's and VSI's JPEG tiles), the stream's own JFIF or Adobe marker is what it reads.

An image codec decodes its frame at the unit's shape. The format sets its grid so that the unit's
shape is the frame's: a TIFF whose last JPEG strip holds a frame of the full RowsPerStrip has the
edge `full`, which the parser learns by reading that strip's SOF, the other strips following the
tags; a frame that differs from them is caught by the checker, not the parser. Where a stream's MCUs
cover the unit's shape but its header declares less (NDPI's frame that is a row short), decoding at
the unit's shape reads the rows its data holds.

### `lsb_first`

`lsb_first` (parameter `bits`, 1 to 7) takes items of `bits` bits packed least significant bit
first: bit k of the unit is bit k mod 8, counted from the least significant, of byte ⌊k/8⌋, and an
item's first bit is its least significant. Decoding packs them most significant bit first, as
[§3.1](../../spec/stored-array.md#31-chains) packs elements, and a given fill packs as decoded.
Where `bits` does not divide 8 and the array has `pad_from`, its padding falls inside an item, and
the array's chain is `source` instead. `lsb_first(1)` reverses each byte's bits, so it would also
be TIFF's FillOrder 2, which TIFF refuses today for the compressions libtiff reverses; the note's
`bit_reverse` is not added. DICOM's 1-bit data, MRC's mode 101 and safetensors' F4 and F6 use it.

### Naming a unit's bytes

Step 0 adds to `array.rs` one function that names a unit's bytes in the finished IR as rule 3 says
(a run member's element with `member`; an alias's target, alone or with a moved `range`; `ranges`
of the object that holds them; or none inside a derived space), with its tests, so that no format
writes it. An alias the parser made itself (`Ir::alias`, which has no extent of its own) is named
by its target's elements. Where it stands for part of an array's storage (a TIFF IFD's strips
shared with another IFD's), the array it belongs to is still an array, whose rows name the
target's units. Where it stands for a whole declaring structure (a pointer IFD that repeats
another, a CZI subblock a second directory entry names), there is one array, the target's, and
the duplicate's own bytes (its IFD entries, its `directory/entries/<i>`) are a declaration of it.

### The test helper

`python/tests/ir/chains.py` decodes a unit through a chain, codec by codec, given the unit's bytes,
its parameters (places read from the source) and its shape. Step 0 writes its frame and the codecs
two or more formats use (the compressors, `shuffle`, `lsb_first`, `jpeg`, `jpeg2k`, `jpegxr`), so
that no format PR writes a shared codec's decoding; each format PR adds the codecs only it uses, in
a module of its own. Where imagecodecs lacks a codec, the helper says how it decodes:
- `skip`, `fits_quantize`, `lsb_first` and `h5_fletcher32` (HDF5's Fletcher-32 checked over the
  bytes before the checksum, which is then dropped): in Python;
- `ccitt_t6`: imagecodecs' `ccittfax4_decode`, which makes a byte per pixel, packed into rows of
  bits by the helper;
- `h5_nbit` and `h5_scaleoffset`: the unit written as the one chunk of an HDF5 file with the
  source dataset's layout, datatype and fill value (`write_direct_chunk`), since HDF5 makes these
  filters' client data from the dataset's own type, and read back by h5py;
- `h5_bitshuffle` under LZ4 or zstd: its 12-byte header parsed, each block decompressed and
  unshuffled by imagecodecs;
- `h5_szip`: imagecodecs' `szip_decode` alone, since the installed HDF5 has no szip filter, on a
  crafted fixture written by `szip_encode` through `write_direct_chunk`, the filter marked optional
  and its client data written as HDF5 would;
- `grib_unpack`: a message reassembled from the places and the unit, with sections 0, 1 and 4 made
  up for the reassembly (they are not places), decoded by eccodes; PNG packing, on which eccodes
  aborts for 1, 2 and 4 bits, decoded in Python (`png_decode` of section 7, then (R + X·2^E) /
  10^D);
- `jpeg` with `header`: the header, its SOF set to the unit's shape, the interval and an EOI spliced
  into one stream;
- `jpeg2k` with `main_header`: the main header, the tile-parts and an EOC spliced, decoded by
  OpenJPEG through imagecodecs, and the tile cropped from the image.

### The codec list

Step 0 adds every entry of [§Codecs](#codecs) to [§3.2](../../spec/stored-array.md#32-the-list)
and to the IR's list in `array.rs`, so that no two format PRs write the same entry and the format
PRs merge in any order. `array.rs`'s `ParamEntry` gains each parameter's kinds (a value of a JSON
type, a place, a column of an Arrow type) and whether it is required. The chain check tests each
`Param::Value` against its type; `StoredArray::check` tests that each required parameter of a
chain's codec is given in the codec or listed among the description's unit parameters for that
chain. A place is checked as a place, its bytes being the format's to read.

## Codecs

A name follows [§3.2](../../spec/stored-array.md#32-the-list): it states the byte format its
decoding takes, its framing included, and covers two framings only where a parameter chooses. This
list supersedes the sketch in [ir-array-model.md](ir-array-model.md#codecs). Each parameter is
given as `name: kinds`, where a kind is a value's JSON type (`int`, `str`, `bool`, `float`,
`int[]`, or `any`), `place`, or `column` with its Arrow type; a parameter is required unless marked
optional. Configuration the source gives once may be a value only where step 0 allows one. A name
also covers two framings where their standard tells them apart by their first bytes (JPEG XL's
codestream and container); step 0 adds that to
[§3.2](../../spec/stored-array.md#32-the-list)'s rule.

`source`, already in the list, takes `format`: `str`; `id`: `int`, `str`; `name` (optional): `str`;
`parameters` (optional): `any`, `place`.

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
| `h5_szip` | a 4-byte length, then the AEC stream | `options`, `pixels_per_block`, `bits_per_pixel`, `pixels_per_scanline`: each `int` or `place` | HDF5 (4) |
| `h5_bitshuffle` | HDF5's bitshuffle blocks: bare, or under LZ4 or zstd each chunk starting with a 12-byte big-endian header of its decoded length and block size, which then governs | `element_size`: `int`, `place`; `block_size` (bare chunks only, 0 the default): `int`, `place`; `compression` (`none`, `lz4` or `zstd`): `str`, `place` | HDF5 (32008) |
| `tiff_lzw` | TIFF's LZW, codes most significant bit first | | TIFF, CZI |
| `packbits` | PackBits runs | | TIFF |
| `ccitt_t6` | ITU-T T.6 data; decoding makes 1-bit rows, most significant bit first, each padded to a byte (`item_bits` 1, `pad_from` at the row) | | TIFF (4) |
| `fits_rice` | FITS's Rice blocks; decoding makes big-endian integers of `byte_pix` bytes | `block_size` (optional, 32): `int`, `place`; `byte_pix` (optional, 4): `int`, `place` | FITS (RICE_1) |
| `skip` | the elements after `length` bytes | `length`: `int`, `place` | NRRD's `byte skip` |

**Transforms**

| Codec | Decoding | Parameters | Used by |
| --- | --- | --- | --- |
| `shuffle` | ungroups bytes by their position in elements of `element_size` | `element_size`: `int`, `place` | HDF5 (2), FITS's GZIP_2, CZI's hi-lo |
| `h5_fletcher32` | checks and drops HDF5's appended checksum | | HDF5 (3) |
| `h5_nbit` | HDF5's N-bit unpacking | `parameters` (the filter's client data, which describes the element's packed fields): `int[]`, `place` | HDF5 (5) |
| `h5_scaleoffset` | HDF5's scale-offset decoding | `parameters` (the filter's client data): `int[]`, `place` | HDF5 (6) |
| `tiff_predictor` | undoes horizontal (2) or floating-point (3) differencing along each row | `predictor`, `planar_configuration`: `int`, `place`; `byte_order` (`little` or `big`): `str`, `place` | TIFF |
| `fits_quantize` | makes big-endian floats of `item_bits` from big-endian 4-byte integers | `method` (optional, NO_DITHER; ZQUANTIZ's name): `str`, `place`; `scale`, `zero`: `place`, `column` Float64; `seed` (optional, given only under a subtractive dither): `column` Int64; `zblank` (optional): `place`, `column` Int64 | FITS |
| `lsb_first` | as step 0 | `bits`: `int` | DICOM, MRC, safetensors |

**Codecs that make elements**, as step 0 says unless the entry says otherwise

| Codec | Takes | Parameters | Used by |
| --- | --- | --- | --- |
| `jpeg` | an ISO 10918-1 stream, any process | `tables` (optional; an abbreviated table stream, TIFF's tag 347): `place`; `header` (optional; the stream up to its first scan, the unit being one restart interval's entropy-coded segment, its RSTn marker excluded, decoded as after a restart; NDPI): `place` | TIFF, NDPI, VSI, DICOM, CZI |
| `jpegls` | an ISO 14495-1 stream | | DICOM |
| `jpeg2k` | an ISO 15444-1 codestream (15444-15's high throughput included), not a JP2 file | `main_header` (optional; the unit is then one tile's tile-parts, with their SOT): `place` | TIFF, VSI, DICOM, SAFE |
| `jpegxr` | a JPEG XR file in its T.832 container (starting `II BC 01`) | | NDPI, CZI |
| `jpegxl` | an ISO 18181-1 codestream or its 18181-2 container, which their first bytes tell apart | | TIFF |
| `webp` | a WebP file | | TIFF |
| `png` | a PNG file | | VSI |
| `bmp` | a BMP file | | VSI |
| `lerc` | a LERC2 blob | | TIFF, in `[lerc, zlib]` or `[lerc, zstd]` as LercParameters say |
| `eer` | EER's run-length bits; decoding makes a count per pixel, 8 bits | `skip_bits` (optional, 7), `horizontal_bits` (optional, 2), `vertical_bits` (optional, 2): `int`, `place` | TIFF (65000 to 65002) |
| `dicom_rle` | DICOM's RLE segments, byte planes most significant first | | DICOM |
| `grib_unpack` | a GRIB field's section 7 | `grid` (section 3), `packing` (section 5): `place`; `bitmap` (optional; the section 6 the field uses, its own or, under indicator 254, an earlier one): `place` | GRIB |

- `jpeg2k` with `main_header` decodes the main header as stored with the unit's tile-parts and
  makes the tile their SOT's `Isot` names, alone, at the place the file puts it; the tile's
  partition, precincts and code-blocks stay the file's.
- `lerc` makes the blob's own data type, little-endian. A blob's validity mask is not made: a
  masked pixel's bytes are zero, and the mask stays in the unit's bytes for a profile to read.
- `dicom_rle` rebuilds each sample from its byte planes, so it makes planar samples of Bits
  Allocated / 8 bytes, little-endian.
- `grib_unpack` makes 64-bit floats, little-endian, in the order section 3's scanning mode gives,
  with alternate rows' reversal (bit 0x10) undone. Columns first (bit 0x20) is the array's `axes`,
  not the codec's. A point the bitmap masks has no value in section 7, and the array's elements
  are every point, so the codec makes NaN there, the one float that is no value; the bitmap stays
  an array of its own ([§GRIB](#grib)). Complex packing's missing values (templates 5.2 and 5.3,
  octet 23) are NaN too under management 1; management 2's primary and secondary values would both
  become NaN, so such a field is `source` instead.

`source` stays the fallback: old-style JPEG, and a compression, filter or packing refkv has no name
for, with the source's number for it.

## The formats

Each section settles what is not obvious; the format's PR specifies the rest in its
[§7](../../spec/stored-array.md#7-the-formats) paragraph and its document, by rule 9, and its own
review checks them. Where a section is silent, an array is one unit, its data element, in an empty
chain, under `short`; `item_bits` is the size its header declares for an element; its fill is
`undefined`; and its declarations are the header's fields that give its shape and element. Sizes are
rough lines of Rust, spec and tests together.

### TIFF and QPTIFF

Large (about 1,500 lines).
- **Arrays.** One per IFD with an image (`ifds/<i>`, SubIFDs, pointer IFDs), thumbnails and
  labels included, keyed by the IFD's struct. OME planes and levels are assembly: the facts keep
  them. Each file of an OME-TIFF set describes its own IFDs, and `tiff_set` renumbers them on
  graft.
- **Shape**, as [ir-array-model.md](ir-array-model.md#what-an-array-is) says: `[H, W, S]` chunky,
  `[S, H, W]` planar, `[H, W]` for one sample. `item_bits` is BitsPerSample, or what a codec makes;
  a size not a multiple of 8 takes `pad_from` at the row (1 for `[H, W]` and chunky, 2 for
  planar).
- **Grid.** Tiles `[Each(TL), Each(TW)]` for one sample, chunky `[Each(TL), Each(TW), Each(S)]`,
  planar `[Each(1), Each(TL), Each(TW)]`, under `full`; strips the same with `Each(min(RPS, H))` and
  W whole, under `short`, but `Each(RPS)` under `full` where the last JPEG strip's frame is full
  size (step 0). Units are the `tiles/<j>` and `strips/<j>` elements; one of count 0 has no
  row, and one outside the file leaves the array no grid, so that a truncated file does not read as
  sparse.
- **JPEG rows.** A JPEG tile whose recipe splices tables or a marker in names the raw stream as the
  element with a `range` of its whole extent; one whose recipe is the identity names the element
  alone. TIFF's plan PR replaces the prefix.
- **Chains.** `[compression]`, with `tiff_predictor` before it for the compressions that take a
  predictor (not the image codecs: JPEG 2000 is accepted with a Predictor its streams do not use).
  FillOrder 2 stays refused for the compressions libtiff reverses; the PR that lifts that adds
  `lsb_first(1)`.
- **Fallbacks.** No grid for mixed BitsPerSample, YCbCr subsampling outside JPEG, and a tile count
  that does not match the image.
- **Declarations.** Tags 256, 257, 258, 259, 262, 266, 277, 284, 317, 320, 322, 323, 338, 339,
  347, 529, 530 and 532, LercParameters, and `header/byte_order`.
- **Checker.** tifffile per page for `shaped`, `chunks`, `dataoffsets` and `databytecounts`, and
  imagecodecs with no color conversion for each unit's decoding.
- **QPTIFF** is parsed as TIFF and comes with this PR, tested on its accepted fixtures.

### NDPI

Medium, after TIFF.
- **Units.** With McuStarts, each restart interval is a unit, a range of the strip element, on
  `[Each(mh), Each(R·mw), Each(3)]`, `full`, as tifffile reads NDPI, in `[jpeg(header)]`. Without
  them, the strip, in `[jpeg]`, where its frame covers the IFD's MCU rows and columns within 65535
  (the parser's `Frame::covers`); an image whose frame does not has no grid. McuStarts whose count
  does not match the grid are refused, as today.
- **JPEG XR IFDs** take `item_bits` and samples from the first stream's PixelFormat, whose range is
  a declaration. A macro or map IFD whose stream the parser does not read has no grid.
- **Checker.** tifffile, comparing McuStarts by hand: tifffile reads them only with tag 271, which
  the fixtures lack, and adding it would change their outputs.

### VSI

Medium, after TIFF.
- **Arrays.** Each ETS file's levels, keyed by its `ets` element with the level as index, and the
  `.vsi`'s IFDs as TIFF's are.
- **Shape.** `[d_k … d_2, h, w, S]` over the coordinates of `dimension_sizes` other than the level
  (which is the index), S only above 1, the collapse to t, c and z being the profile's. Level 0's
  `h` and `w` are the larger of its declared size (from `dimension_sizes`) and its stored tiles'
  extent (the largest tile coordinate plus one, times the tile size); a later level, which declares
  no size, has its tiles' extent. So every tile has a row. Bio-Formats' halving rule, which crops
  the levels after 0, is the profile's, as IMS's crop to `ImageSize` is; the declared sizes are
  declarations.
- **Grid and chains.** `Each(1)` on each leading axis, then the tile and S, under `full`; one unit a
  tile, its `tiles/<k>` element; the chain the ETS header's compression names, one codec;
  `item_bits` the pixel type's sample size, or what the codec makes.
- **Fill.** Given, as a place in the `ets` header, when the background's samples are equal and the
  tiles are raw; otherwise `undefined`, with the header among the declarations.
- **Checker.** Bio-Formats for shapes, tile sizes and decoded tiles (minding BGR and the
  resolution level), but imagecodecs with no color conversion for JPEG tiles, which Bio-Formats
  converts to RGB; tifffile for the `.vsi`'s IFDs.

### HDF5, NWB and IMS

Large (about 2,500 lines), from the spike's branch, about 70% of which carries over.
- **Arrays.** Every visited object with a layout message, keyed by its `objects/<i>` struct, and
  each attribute, keyed by its own attribute message's element: one unit, its value's element, in
  an empty chain.
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
  the bzip2 and szip filters. N-bit packs only reduced-precision types, which HDF5 2.0 refuses to
  create through h5py; `h5_nbit` has no fixture until one can be written another way, and the PR
  says so.
- **IMS** reads through HDF5 and comes with this PR: its stored shape is the padded dataspace.
- **Checker.** h5py.

### FITS

Medium-large (about 1,000 lines).
- **Arrays.** Keyed by `hdus/<i>/header`, the index each array's position among those its HDU
  declares, as [ir-array-model.md](ir-array-model.md#what-an-array-is) has it: an image or a
  compressed image's image, its table's rows and its heap (0, 1 and 2; its null pixel mask 3,
  once the parser reads one); a table's rows and heap (0 and 1, the heap described even when
  empty). An HDU of NAXIS 0 holds none.
- **Shape.** NAXISn…NAXIS1 (ZNAXIS for a compressed image) with `axes` reversed, as a Fortran-order
  `.npy`'s, of |BITPIX| (|ZBITPIX|) bits; rows `[NAXIS2]` of 8·NAXIS1 bits; heap `[H]` of 8. Fill
  `undefined`; BLANK, BSCALE and BZERO are declarations.
- **Units.** An image, rows or heap is one unit, its element, but a compressed image's heap,
  which `tile_cut` splits into `heap/<j>` elements, is `ranges`. A compressed image's are its tiles,
  `Each(ZTILEj)`, `short`: each a range of today's `heap/<j>` element, or the element alone where
  the tile is all of it. The describe-only PR keeps `tile_cut`. An absent tile has no row.
- **Chains.** Chain 0 is `[fits_quantize, compressor]` for a quantized image, else
  `[compressor]`, where the compressor is `fits_rice`, `gzip`, `shuffle` then `gzip` for GZIP_2,
  or none for NOCOMPRESS; chain 1 is `[gzip]` for tiles in GZIP_COMPRESSED_DATA. Under
  quantization every compressor codes 4-byte integers, so `fits_rice`'s `byte_pix` and GZIP_2's
  `shuffle` `element_size` are the value 4; unquantized, they are places (the BYTEPIX ZVAL card, and
  ZBITPIX), and `block_size` is the BLOCKSIZE ZVAL card, each the format's default as a value where
  the card is absent. `fits_quantize`'s `scale` and `zero` are the ZSCALE and ZZERO columns, or
  places where the header gives them as keywords; `seed` is a column (the tile's index plus
  ZDITHER0); `method` a place (ZQUANTIZ), NO_DITHER as a value where it is absent; `zblank` absent
  where neither the table nor the header gives ZBLANK. The first real use of columns and of a
  second chain.
- **Fallback.** An unquantized image whose BYTEPIX is not its elements' width gives `source`.
- **Checker.** astropy with `disable_image_compression=True` for each tile's bytes and
  parameters, and its codecs for the decoding.

### GRIB

Medium-large (about 500 lines).
- **Arrays.** Each field, keyed by `messages/<i>`, its index the array's position among the
  message's arrays, bitmaps and fields together, in the source's order: shape `[Nj, Ni]` (or,
  columns first, `[Ni, Nj]` with `axes` `[1, 0]`), or `[points]` for reduced grids and template
  3.101; `item_bits` 64; fill `undefined`, since NaN at a masked point is what
  `grib_unpack` makes, not a fill. Each section 6 that holds a bitmap (indicator 0) is an array
  too: shape `[points]` in the field's scan order, 1-bit items in an empty chain, its bitmap bytes
  as one unit, indexed before the field whose section 7 follows it.
- **Unit.** The field's section 7 element, in `[grib_unpack]`, whose places are its own: each field
  has a chain of its own, about 13 description entries, within the descriptions' limit of one per
  four bytes for any field of 52 bytes or more.
- **Fallbacks.** A field left out for its meaning is described fully. One whose packing or grid
  template refkv does not unpack is `source` with the template's number; one with a bitmap
  indicator 1–253 has no grid.
- **Checker.** eccodes, its values put in the codec's order: alternate rows' reversal undone, and,
  where columns come first, shaped `[Ni, Nj]` with no transpose. The four fixtures eccodes cannot
  read (the PNG packings of 1, 2 and 4 bits, on which it aborts, and the indicator-254 field of
  `grib_bitmap_254`) compare against `fixtures/grib/expected`, as the GRIB tests do today.

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
  datatype (RGB24 and RGBA32 are samples of 8 bits on a last axis of 3 or 4, which the reversal
  leaves last: `axes` `[n−1, …, 0, n]`); one empty chain; fill `undefined`.
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
- **Arrays.** Keyed by the header file's root element (the header has no element of its own):
  shape `sizes` with `axes` reversed, `item_bits` from `type`; its `dimension`, `type`, `sizes`,
  `endian`, `encoding`, `line skip`, `byte skip` and `data file` lines are declarations,
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

Medium.
- **Arrays.** Pixel Data, keyed by `dataset/7FE00010`; each Overlay Data (60xx,3000), keyed by its
  own element, 1-bit in `[lsb_first(1)]`, or no grid under Explicit VR Big Endian, whose OW words
  swap its bytes; the palette's three LUT data elements, each keyed by its own element, shape their
  length / 2 in OW words of 16 bits (8-bit entries packed two to a word are the profile's to read,
  from the descriptor, a declaration); the LUT Data (0028,3006) of the Modality and VOI LUT
  Sequences, keyed as rule 2 says, shape and `item_bits` from its LUT Descriptor, a declaration;
  and an icon's Pixel Data, keyed by the Icon Image Sequence element as rule 2 says, its frame a
  run member where the items fold.
- **Shape.** `[N, Rows, Cols, S]`, or `[N, S, Rows, Cols]` for planar native and RLE data, N only
  where Number of Frames is given (kept when 1) and S only above 1, one unit a frame; `item_bits`
  Bits Allocated, or what the codec makes; fill `undefined` (Pixel Padding Value is a declaration).
  A whole-slide image's frames are its tiles, and the total matrix, with the dimension organization
  that places them, is a declaration the plan assembles from, as OME's planes and CZI's subblocks
  are.
- **Units.** A native frame is a run member (`member`), or `frames/0` alone where the file holds
  one frame; a frame of one fragment is its
  `fragments/<j>` element; a frame of several is `ranges` of the parsed source. Frames whose bits
  do not end on a byte make the array one unit. ND2 and DICOM give the first real `member` rows.
- **Codecs.** `jpeg`, `jpegls`, `jpeg2k`, `dicom_rle`, and `lsb_first(1)` for 1-bit data.
- **Checker.** pydicom: `generate_frames` for encapsulated rows' bytes and Pixel Data's offsets for
  native ones; `pixel_array` (or `iter_pixels`) with `raw=True` for the decoding, but imagecodecs
  for a JPEG 2000 frame with subsampled chroma, which pydicom's decoders upsample otherwise. The PR
  adds pylibjpeg with its libjpeg and openjpeg plugins and pyjpegls to the dev group, since pydicom
  has no decoder installed for the corpus's lossless JPEG, JPEG-LS and HTJ2K files.

### CZI

Medium-large.
- **Arrays.** One per subblock, keyed by its `subblocks/<i>` struct, one unit each; placement,
  overlap and pyramids are the plan's. Each `ZISRAWSUBBLOCK` segment the walk finds that no
  directory entry names declares an array too, with its own copy of its entry, keyed as rule 2
  says; only deleted segments stay bytes. Shape is the entry's stored size, confirmed or replaced by
  the stream's frame size for JPEG, lossless JPEG and JPEG XR: `[H, W]` for gray pixel types,
  `[H, W, S]` for Bgr24, Bgra32 and Bgr48, samples in BGR order as stored, or in the order a
  compressed subblock's codec makes them; `item_bits` per sample;
  fill `undefined`. A subblock with no size has no grid.
- **Codecs.** `jpeg`, `jpegxr`, `zstd`, `[shuffle, zstd]` for Zstd1's hi-lo, and `tiff_lzw`, which
  [czi.md](../../spec/zarrify/czi.md) gives as TIFF's. Zstd1's header is its own element, a
  declaration.
- **Declarations.** The directory entry and dimensions; a part file's master entry, which no
  element of the part's IR holds, as an extent of the master's object.
- **Grafts.** `czi_set` is the first graft of many descriptions (about 10^5 in a large slide).
- **Checker.** czifile's subblock directory and raw segment bytes; the decoding by the helper,
  since czifile's `decode` swaps BGR to RGB, sets alpha and converts JPEG's colors, and does not
  decode lossless JPEG (compression 3).

### ND2

Medium.
- **Arrays.** One, keyed by `chunks/ImageAttributesLV!`: shape `[N, H, W, C]`, N the product of the
  loops' counts, as the parser reads it (no N axis where there are no loops, no C axis for one
  component); `item_bits` is `uiBpcInMemory`; fill `undefined`. Frames at or past N, which the
  loops do not place and which have no `pixels` element, are a second array (index 1) of the same
  frame shape, one unit each, a `range` of its `frames/<f>/data` element past the 8-byte
  timestamp.
- **Units.** Each frame's `pixels` element, a run member, whose `rows` recipe already drops the
  padding; chain `[zlib]` where compressed.
- **Checker.** nd2's attributes, frame offsets and `read_frame`.

### LIF and XLEF

Medium.
- **Arrays.** One per image, keyed by the XML element that declares it, its index the array's
  position among the arrays that element declares: shape the dimensions' NumberOfElements, slowest
  first (`axes` where the strides order them otherwise), with the channels as an axis; `item_bits`
  the channels' Resolution rounded up to 8, 16, 32 or 64, the Resolution a declaration; fill
  `undefined`. An image whose channels differ in size or resolution is one array per channel. The
  declaration is that element; a character range inside its UTF-16 text is not given.
- **Units.** One per image where its strides nest; where they do not (padded rows, planar channel
  offsets), rows or planes, each the image's element with a `range`; no grid where they do not tile
  it (zero strides).
- **Checker.** Bio-Formats, which reads LIF, for shapes and decoded planes.

### SAFE

Medium.
- **Arrays.** One per band file, keyed by its `codestream` struct: shape `[H, W]` from SIZ, or
  `[H, W, 3]` for a true-color image's three components; `item_bits` the samples `jpeg2k` makes;
  fill `undefined`; SIZ a declaration. Grid `Each(tile)` on each image axis, `Each(3)` on the
  third, `short`.
- **Units.** Each `tiles/<k>` element, its tile-parts with their SOT, in `[jpeg2k(main_header)]`.
  The SIZ refkv rebuilds and the empty-tile tail are the projection's.
- **Checker.** SIZ and SOT read by a small reader in the test for the grid and the tiles, since
  rasterio's block shapes give the whole image; rasterio's window reads for the decoding, against
  the helper's `jpeg2k`.

## Order of work

1. **Step 0.**
2. **The formats**, each its own PR to main, in any order but for TIFF before NDPI and VSI, and
   NIfTI before MRC and NumPy's deflated members. Each touches its own parser, its own paragraph of
   [§7](../../spec/stored-array.md#7-the-formats) and its document, so they run side by side, three
   or four at a time.

When every format here has its description, profile-plugins' steps 3 and 4 are left with each
format's known structures and plan.

## Alternatives considered

- **Describing each format inside its plan PR**, as ir-array-model.md first said. It waits on the
  machine, and settles each format's codecs one plan at a time.
- **A column for a unit's decoded shape.** It would put a TIFF's rows past the map limit's
  guarantee, and repeat what the grid already says.
- **A bit-order flag on the description** in place of `lsb_first`. It changes the type for three
  formats where one codec does.
- **A `color_transform` parameter on `jpeg`** for the Adobe marker refkv splices in. It would be
  refkv's reading of the source's declarations, and converting colors is presentation.
- **Upsampling a JPEG's chroma by replication**, which keeps every coded value. No reader here
  does it (imagecodecs ignores its flag), so neither the helper nor a checker could produce it.
- **Decoding a JPEG 2000 tile at the origin**, with a rewritten SIZ. The code-block partition and
  the wavelet's phase follow the tile's place, so a tile not aligned to them decodes wrong.
- **Each format writing its own codec entries.** Fifteen PRs appending to one table and one list
  conflict, and a later format would find an earlier one's name already taken differently.
- **Moving FITS's `tile_cut` in its describe-only PR.** It changes the mirror for every compressed
  image, which the plan PR changes anyway.

## Open questions

1. Whether VSI's lossless JPEG is an ISO 10918-1 lossless stream, which decides whether it is
   `jpeg`. VSI's PR answers it from Bio-Formats, and if not, adds an entry by rule 6.
