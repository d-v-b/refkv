# Implementation plan: the FITS profile

This plan implements a FITS convention and profile (`spec/virtualize/fits.md`,
to be written as Part 1 and Part 2 of one document, as [mrc.md](../../spec/virtualize/mrc.md)
is) in the Rust core, reached from Python, the browser and the CLI, with
fixtures, a verifier, a public corpus and a docs page. It is a design note,
not normative; the convention and the profile state the decisions.

The input is **one FITS file**. [§1.3](#13-single-file-or-collection) says
why; collections are a later combine step outside any profile.

## 1. Research summary

Sources read: the FITS Standard 4.0 and the tiled-image-compression
convention 2.3; kerchunk's and VirtualiZarr's FITS parsers; astropy's
compressed-HDU code (`_tiled_compression.py`, `_quantization.py`,
`_codecs.py`); the archive documentation cited below; and public files
inspected by hand in October 2026 with dependency-free scripts kept in the
scratch directory of the research session (`fitswalk.py` walks HDUs with no
astropy; `tilecheck.py` and `heapadj.py` check tile ranges). Findings
marked **[inspected]** were checked against real bytes.

### 1.1 The format in one page

A FITS file is a sequence of **HDUs** (header-data units), each an integral
number of 2880-byte blocks. A header is 80-character ASCII cards
(`KEYWORD = value / comment`), 36 per block, ending with `END` and
space-padded to a block boundary. The data follow, zero-padded to a block
boundary, and the next HDU starts at the next block. The first HDU is the
**primary** (`SIMPLE = T`); the others are **extensions** named by
`XTENSION`: `IMAGE`, `TABLE` (ASCII) or `BINTABLE`. The data size of any
HDU is one formula:

```
bytes = |BITPIX| / 8 × GCOUNT × (PCOUNT + NAXIS1 × … × NAXISn)
```

with `PCOUNT = 0`, `GCOUNT = 1` when absent, and no data at all when
`NAXIS = 0` (the usual empty primary of a multi-extension file). A reader
walks a file by reading headers only, never data.

**Images** (primary or `IMAGE`) are big-endian, with `NAXIS1` varying
fastest: the bytes are a C-order array of shape `[NAXISn, …, NAXIS1]`, the
Zarr shape with the axes reversed. `BITPIX` is 8, 16, 32, 64 (unsigned
byte, then signed integers) or −32, −64 (IEEE floats). Unsigned 16/32/64-bit
integers are a convention: `BZERO = 32768` (2^31, 2^63) with `BSCALE = 1` on
signed storage, and `physical = BZERO + BSCALE × stored` in general.
`BLANK` marks undefined integer pixels; floats use NaN.

**Binary tables** are fixed-width rows of `NAXIS1` bytes, `NAXIS2` rows,
columns in `TFORMn` order with no padding, then a **heap** of `PCOUNT`
bytes for variable-length columns (`P`/`Q` descriptors: a `(length, heap
offset)` pair in the row). Every column of a table is a strided read.

**Tile-compressed images** (the `ZIMAGE` convention, what `fpack` writes to
`.fits.fz`) are a `BINTABLE` with one row per tile and a `COMPRESSED_DATA`
descriptor column pointing into the heap, plus for quantized floats the
per-tile `ZSCALE` and `ZZERO` columns. `ZCMPTYPE` is `RICE_1` (the default
and overwhelmingly the most common), `GZIP_1`, `GZIP_2`, `PLIO_1`,
`HCOMPRESS_1` or `NOCOMPRESS`; `ZTILEn` gives the tile shape (one row per
tile by default); `ZQUANTIZ` and `ZDITHER0` give the dithering of the
float-to-integer quantization, which depends on the tile's row index. The
original image's header, WCS included, is copied verbatim into the table's
header.

**The header is the metadata.** World coordinates are the WCS cards
(`CTYPEn`, `CRPIXn`, `CRVALn`, `CDi_j` or `PCi_j` with `CDELTn`, `CUNITn`,
alternates with an `A`–`Z` suffix, plus distortion conventions such as SIP
and TPV, or lookup extensions in HST files). They are indexed by FITS axis
number, so reversing the shape does not change them. Long strings use
`CONTINUE` cards, ESO's `HIERARCH` makes dotted keywords, and
`COMMENT`/`HISTORY` repeat freely with meaningful order.

### 1.2 How archives use it

The atomic deliverable of every archive surveyed is one self-contained FITS
file with its own complete WCS and complete data planes.

| archive, product | unit | HDUs | compression |
|---|---|---|---|
| SDSS imaging `frame-*` **[inspected]** | one file per (run, camcol, field, filter) | float32 2048×1489 image with TAN WCS; a calibration vector; two small tables | **whole-file bzip2** |
| SDSS spectra `spec-*` | one file per object | 3 `BINTABLE`s | none |
| HST `_flt`/`_flc`/`_raw` **[inspected]** | one MEF per exposure | `SCI`/`ERR`/`DQ` × chips; STIS raw: `ERR` and `DQ` are `NAXIS = 0` placeholders with full headers | none |
| HST `_drz`/`_drc` | one new file per association | `SCI`/`WHT`/`CTX` + `HDRTAB` | none |
| JWST stages 1–2 | one file per detector per exposure | `SCI` (uncal: uint16 4-D), `ERR`, `DQ`, `VAR_*`, tables, `ASDF` | none |
| JWST stage 3 `_i2d` **[inspected]** | one new mosaic per filter | `SCI` float32 10272×4755, `ERR`, `CON` int32 3-D, `WHT`, `VAR_*`, `HDRTAB`, `ASDF` | none |
| Rubin DP1/DP2 | one file per detector per visit; one per (band, tract, patch) | `IMAGE`/`MASK`/`VARIANCE` tile-compressed, then opaque `BINTABLE`s; DP2 adds JSON and `INDEX` HDUs | RICE |
| Euclid Q1 MER tile | one file per (tile, band, product) | one float32 19200×19200 primary; RMS, FLAG are sibling files | none |
| Euclid Q1 VIS frame | one MEF per exposure | 433 HDUs: 144 quadrants × `SCI`/`RMS`/`FLG`, each with its own TPV WCS | none |
| DECam InstCal **[inspected]** | one `.fz` per exposure per product (`ooi` image, `oow` weight, `ood` mask) | primary + 61 chip HDUs named by CCD, each RICE_1 float32 quantized with `SUBTRACTIVE_DITHER_1`, one tile per row | RICE |
| Pan-STARRS skycell | one `.fz` per (skycell, filter, product) | one RICE_1 int16 6240×6243 image, one tile per row | RICE |
| Legacy Surveys brick | one `.fz` per (brick, band, product) | RICE float32 3600×3600, 100×100 tiles, `SUBTRACTIVE_DITHER_2` | RICE |
| ZTF | one file per CCD quadrant per exposure per product | one float32 3072×3080 primary, TPV WCS | none (`.fz` for differences) |
| TESS/Kepler | one file per target | `LIGHTCURVE` table + `APERTURE` image; MAST repacks FFIs into one 45 GB 4-D cube per CCD | none |
| DESI | one file per HEALPix pixel | 19 HDUs | none |
| HiPS | one file per (order, pixel) | one float32 512×512 image | none |
| radio cubes | one file per (source, spectral window) | one 4-D cube with a degenerate Stokes axis | none |
| Gaia, radio visibilities | not FITS | | |

Three patterns: **multi-extension files are the norm**, with extensions of
three kinds (planes of one image, chips of a mosaic camera each with its own
WCS, and sidecar tables), and nobody stitches chips into one array inside a
file; **tile compression is the ground-based half of the world** (Pan-STARRS,
Legacy Surveys, NOIRLab, Rubin), while space archives serve uncompressed
files and SDSS alone uses whole-file bzip2; and **siblings are defined by
names**, not by the file (DECam's `ooi`/`oow`/`ood`, Euclid's mosaic/RMS/FLAG,
JWST observation directories of about 5000 objects).

**Existing virtual tools** are per file. kerchunk's `fits.py` and
VirtualiZarr's `FITSParser` (rewritten natively in October 2026) take one
URL, emit one single-chunk array per data HDU, big-endian `bytes`, a
`fixedscaleoffset` for `BZERO`/`BSCALE` (kerchunk passes `BSCALE` where
numcodecs wants `1 / BSCALE`), reject or struct-pack binary tables, and
**neither handles tile compression**: astropy's `CompImageHDU` reports
itself as an image, so both would reference raw table bytes with the
uncompressed length. astropy has no Zarr integration; it carries
unregistered numcodecs-style `Rice1`, `Gzip1`, `Gzip2`, `PLIO1`,
`HCompress1` classes and a C dequantizer.

### 1.3 Single file or collection

The profile takes a single file, for four reasons:

1. **A FITS file is never incomplete.** The SAFE profile takes a store
   because a product's pixels are many JPEG 2000 files that only the
   product's XML relates. A FITS file carries its own WCS, planes and
   tables; its siblings are other variables on the same grid, other tiles
   of a shared projection, or other exposures, none needed to read it.
2. **There is no manifest.** A collection of FITS files is defined by each
   survey's filename convention. A collection profile would be a DECam
   profile, a Rubin profile, a Euclid profile, each encoding a naming scheme
   the archive can change: the coupling the registry refactor just removed,
   reintroduced per survey, with equivalence depending on listing order.
3. **A multi-extension file already is a collection** (61 chips with their
   own WCS; `SCI`/`ERR`/`DQ`), so the file profile settles the hard design
   and a collection adds only the combining.
4. **Combining is a refkv-level operation.** The source table holds any
   number of files ([docs/virtual-datasets.md](../../docs/virtual-datasets.md)
   builds one dataset from 60 netCDF files), and the natural FITS collections
   map onto it: same-grid siblings become arrays of one group,
   same-projection tiles (Rubin patches, Pan-STARRS skycells, HiPS) differ by
   integer `CRPIX` offsets and become one array with one chunk region per
   file, exposure stacks concatenate along a new axis. These are
   VirtualiZarr's manifest-space operations, and
   [zarr-specs#399](https://github.com/zarr-developers/zarr-specs/issues/399)
   is about where they break: per-file codec parameters, which FITS has
   (per-file `BZERO`/`BSCALE`, per-HDU `ZDITHER0`, per-tile
   `ZSCALE`/`ZZERO`). The design below puts per-tile parameters *inside the
   chunk bytes*, so the codec is uniform across files and concatenation
   needs no per-chunk codec variance. That argument needs the file profile
   first.

Consequences: SDSS imaging frames (whole-file bzip2) and `.fits.gz` cannot
be virtualized and are rejected by their first bytes. Tom Robitaille's
"each chunk a valid FITS file" layout (the comment linked from the issue)
is producible with `Concat` references and no codec context, as a second
layout later, not in version 0.

### 1.4 Findings that shaped the design

- **Per-tile decodability.** All 300 RICE tiles of astropy's `comp.fits`
  decode from their own heap range alone, with astropy's `Rice1`, from the
  row's `(length, offset)` descriptor and the header's `BLOCKSIZE` and
  `BYTEPIX`, and match the full decompression **[inspected]**.
- **Tiles are stored consecutively.** The 249,734 tiles of a DECam exposure
  (61 HDUs × 4094 row tiles, 324 MB of heap, mean tile 1299 bytes, largest
  1594) have 0 gaps between consecutive tiles, and the tile order is the
  first-pixel order (axis 1 fastest) **[inspected]**. So a run of whole tile
  rows is one byte range. cfitsio and astropy both write heaps this way.
- **Per-tile parameters.** `ZSCALE` and `ZZERO` differ from tile to tile
  (2.15111 and 148.427, then 2.15111 and 139.974 in the next chip's first
  rows), and `ZDITHER0` differs per HDU (7331, 4343, …, 4280)
  **[inspected]**. The dither's random sequence is indexed by the tile's
  row number, so a decoder needs the chunk's first tile index.
- **Unsigned integers are common.** HST raw `SCI` (int16 with `BZERO =
  32768`, astropy reads it as uint16), JWST uncal, DESI masks (`BZERO =
  2^31`) **[inspected]**.
- **`NAXIS = 0` extensions with full headers** exist (STIS raw `ERR`/`DQ`
  placeholders) **[inspected]**: metadata without an array.
- **Headers are big.** A JWST primary has 282 cards; a DECam file has 62
  headers of 151 to 205 cards; a Euclid VIS frame has 433 HDUs. The cards
  cannot all sit in the root document.
- **astropy writes everything a fixture needs**: `CompImageHDU` with every
  `ZCMPTYPE`, `quantize_level`, `dither_seed` and tile shape; `BinTableHDU`
  with `P`/`Q` columns; `TableHDU`; `GroupsHDU`. `uv run --with astropy`
  works in the workspace (astropy 8.0.2); the dev dependencies gain it.

## 2. The convention (Part 1 of `spec/virtualize/fits.md`)

The hierarchy is a plain Zarr v3 group, as SAFE's is: no OME-NGFF metadata,
since FITS axes are sky coordinates under projections, not microscopy axes.
This section extends [conventions §2](../../spec/conventions.md#2-attributes)
as SAFE does: nodes carry `zarr_conventions` and `refkv_virtualized` only.

### 2.1 Declaration

The root declares the convention with `"profile": "fits"`, `"version": 0`,
the file's URL as `source.url`, and the root's source metadata
([§2.6](#26-source-metadata)) as the member `"fits"`. Every HDU node declares
it too, with that HDU's source metadata; `refkv_source` with the mirror's
description. New UUID, `schema_url` and `spec_url` as the others.

### 2.2 HDUs and nodes

The file's HDUs are numbered from 0 in file order. HDU `i` has the node
**`<i>`** (`0`, `1`, …), a child of the root. `EXTNAME` and `EXTVER` are
metadata, not paths: `EXTNAME` is optional and repeats (`SCI` ×4 in a WFPC2
file, 144 × `SCI` in Euclid, `COMPRESSED_IMAGE` by default in every `.fz`),
and kerchunk's name collisions show the alternative. The root's source
metadata lists the HDUs ([§2.6](#26-source-metadata)) so that a reader finds
`SCI,2` by lookup.

By its kind, an HDU's node is:

| kind | test | node |
|---|---|---|
| **empty** | `NAXIS = 0` | a group with no children: metadata only (the empty primary; STIS placeholders) |
| **image** | primary with `NAXIS ≥ 1`, or `XTENSION = 'IMAGE'` | an array ([§2.3](#23-images)); one with no chunks when some `NAXISn = 0` |
| **compressed image** | `XTENSION = 'BINTABLE'` with `ZIMAGE = T` | an array ([§2.4](#24-tile-compressed-images)) |
| **table** | `XTENSION = 'BINTABLE'` or `'TABLE'` otherwise | a group with the arrays `rows` and, when the heap is not empty, `heap` ([§2.5](#25-tables)) |

Random groups (`GROUPS = T`), tile-compressed tables (`ZTABLE = T`) and
registered foreign extensions are rejected by the profile in version 0.

### 2.3 Images

- **Shape:** `[NAXISn, …, NAXIS1]`.
- **Data type:** `BITPIX` 8 `uint8`, 16 `int16`, 32 `int32`, 64 `int64`,
  −32 `float32`, −64 `float64`. The stored values: `BSCALE`, `BZERO` and
  `BLANK` are recorded in the node's source metadata and not applied
  (the NIfTI precedent,
  [nifti.md §4.2](../../spec/virtualize/nifti.md#42-value-scaling)); see
  [§11](#11-open-questions-and-their-decisions) for the unsigned case.
- **Codecs:** `[{"name": "bytes", "configuration": {"endian": "big"}}]`,
  `[{"name": "bytes"}]` for `uint8`.
- **Fill value:** 0 for every type
  ([conventions §7](../../spec/conventions.md#7-source-values-as-arrays));
  `BLANK` is metadata.
- **Chunks:** contiguous values per
  [conventions §7](../../spec/conventions.md#7-source-values-as-arrays)
  with the **chunk limit 2^20 bytes**, as MRC: whole planes when small, else
  row bands, else runs of a row. A 195 MB JWST `SCI` plane of 10272 × 4755
  float32 is 191 chunks of 25 rows. Edge chunks are padded by a literal, or
  copied when the payload does not fit, as MRC's are.
- **Dimension names:** `axis<n>` for FITS axis `n` (`["axis2", "axis1"]`
  for a 2-D image), since `CTYPEn` is optional, repeats, and names a
  coordinate type rather than a dimension. A dataset layout may do better
  later ([§11](#11-open-questions-and-their-decisions)).

### 2.4 Tile-compressed images

An array of the image's shape `[ZNAXISn, …, ZNAXIS1]` and the data type of
`ZBITPIX` by the table above, with the tile shape `[ZTILEn, …, ZTILE1]`
(defaults: `ZTILE1 = ZNAXIS1`, the others 1).

**Chunks.** Tiles are numbered in storage order (tile index along axis 1
fastest), so a run of tiles that is a block of the **tile grid** `[ceil(ZNAXISn
/ ZTILEn), …, ceil(ZNAXIS1 / ZTILE1)]` spanning the full extent of every
faster axis is consecutive in storage. The chunk grid cuts the tile grid as
[conventions §7](../../spec/conventions.md#7-source-values-as-arrays) cuts
contiguous values, with a tile as the element, the element's size its
**decoded** bytes (`ZTILE1 × … × ZTILEn × |ZBITPIX| / 8`), and the limit
2^20: the same choice of cut axis and count `c`, and the chunk shape is 1
along the axes before the cut axis, `c × ZTILE` along it, and the full
extent after it. A DECam chip (one tile per row of 8184 bytes, a tile grid
of `[4094, 1]`) has chunks of 128 rows, 32 per chip, about 2000 per file; a
Legacy Surveys brick (100 × 100 float32 tiles of 40 KB, a tile grid of
`[36, 36]`, whose tile row of 1.44 MB is over the limit) is cut along axis 1
into chunks of 18 tiles, `[100, 1800]`, 72 per image. The decoded-size rule
means a `.fz` file and its `funpack`ed twin have the same chunk grid
whenever the tile rows align with the twin's row bands.

**References.** The chunk's bytes are a **literal prefix** followed by the
tiles' compressed streams, in storage order:

- the prefix is written by the virtualizer from the main table's rows (the
  only part of the table it reads): a header of two little-endian `u32`,
  the tile count `N` and the storage index `r0` of the first tile, then `N`
  records of 32 bytes: `u32` length of the tile's stream (0: the descriptor
  is null and the tile is absent), `u32` kind (0: a `ZCMPTYPE` stream from
  `COMPRESSED_DATA`; 1: a gzip member from `GZIP_COMPRESSED_DATA`), `i32`
  `ZBLANK`, `u32` flags (bit 0: `ZSCALE`/`ZZERO` given; bit 1: `ZBLANK`
  given), `f64` `ZSCALE`, `f64` `ZZERO` (0 when not given). 128 tiles make a
  4 KB prefix against about 166 KB of DECam tile data, well under the
  65519-byte payload limit. Its precedent is the JPEG tables the TIFF
  profile prepends; the difference is that this prefix is unique to its
  chunk, so it is a literal range, not a data source;
- then one range of source 0 per maximal run of adjacent tiles. When the
  chunk's tiles are consecutive and adjacent in the heap, as `fpack` and
  astropy write them, that is one range.

**Codec.** `fits_tiles`, new, in every implementation
([§4.3](#43-codecs)), an array-to-bytes codec whose configuration is
`{"compression": ZCMPTYPE, "parameters": {ZNAMEi: ZVALi…}, "zbitpix": B,
"tile_shape": [...], "shape": [...], "quantize": ZQUANTIZ, "dither_seed":
ZDITHER0}`. Decoding reads the prefix, computes each tile's shape (edge tiles
are smaller) from `r0 + k`, decodes each stream (`RICE_1` with `BLOCKSIZE`
and `BYTEPIX`; `GZIP_1`; `GZIP_2` with the byte unshuffle; `NOCOMPRESS`;
kind 1 as gzip), and for `ZBITPIX < 0` dequantizes:
`F = (I − R + 0.5) × ZSCALE + ZZERO` with the tile's dither sequence
(`SUBTRACTIVE_DITHER_1`), `F = I × ZSCALE + ZZERO` (`NO_DITHER`),
`I = −2147483647 ↦ 0.0` first for `SUBTRACTIVE_DITHER_2`, and `I = ZBLANK ↦
NaN`. The dither sequence is the standard's: 10,000 values of the
Park–Miller generator (seed 1, `x = 16807 x mod (2^31 − 1)`), tile `t`
(0-based storage index) starting at `i0 = (t + ZDITHER0) mod 10000`,
`i1 = int(RN[i0] × 500)`, advancing per pixel, NaN pixels included, and
rolling `i0` when `i1` reaches 500. Decoded tiles are placed in the chunk;
the rest of the chunk (past the array's edge) is the fill value. Nothing
in the decoder depends on the chunk's position: `r0` is in the bytes, which
is the answer to the position problem of zarr-specs#399 for this format.

`HCOMPRESS_1`, `PLIO_1`, the deprecated `UNCOMPRESSED_DATA` column with a
non-null descriptor, and `NULL_PIXEL_MASK` are rejected in version 0.

### 2.5 Tables

A column is a strided read, and a Zarr v3 struct data type cannot record
big-endian fields, so version 0 keeps a table as bytes that any FITS table
reader decodes:

- `<i>/rows`: `uint8`, shape `[NAXIS2, NAXIS1]`, dimensions `row` and
  `byte`, contiguous values cut with the 2^20 limit (whole rows);
- `<i>/heap`: `uint8`, shape `[H]`, dimension `byte`, present when the
  heap's length `H = PCOUNT − (THEAP − NAXIS1 × NAXIS2)` is at least 1,
  cut as bytes (2^24 chunks);
- the column definitions in the node's source metadata: for each column
  `TTYPEn`, `TFORMn`, `TUNITn`, `TDIMn`, `TSCALn`, `TZEROn`, `TNULLn`,
  `TDISPn` and, for ASCII tables, `TBCOLn`, as the cards give them.

This covers the `ASDF` HDU of JWST files (one row, one `NB` column: the YAML
tree is `rows[0]`), `HDRTAB`, Rubin's opaque component tables and SDSS's
small tables alike. A `fits_table` codec presenting columns is later work.

### 2.6 Source metadata

The root's `fits` member:

```json
{"hdus": [{"index": 0, "type": "primary", "name": null, "version": null, "kind": "empty",
           "bitpix": 8, "naxis": [], "pcount": 0, "gcount": 1,
           "offset": 0, "header_bytes": 23040, "data_offset": 23040, "data_bytes": 0},
          {"index": 1, "type": "image", "name": "SCI", "version": 1, "kind": "image", ...},
          {"index": 7, "type": "bintable", "name": "SCI", "version": 1, "kind": "compressed_image",
           "compression": {"type": "RICE_1", "parameters": {"BLOCKSIZE": 32, "BYTEPIX": 4},
                           "zbitpix": -32, "tile": [1, 2046], "quantize": "SUBTRACTIVE_DITHER_1", "dither_seed": 7331},
           ...}],
 "cards": [["SIMPLE", true, "conforms to FITS standard"], ["BITPIX", 8, "array data type"], ...]}
```

`cards` are the primary header's. Each HDU node's `fits` member has `index`
and that HDU's `cards`, plus for an image `bscale`, `bzero`, `blank`,
`bunit` (each `null` when absent) and for a table `columns`. A **card** is
`[keyword, value, comment]`: `keyword` the text of bytes 0–7 trimmed
(`HIERARCH` names as written after the keyword, up to `=`); `value` a JSON
string for a string (quotes undoubled, trailing spaces trimmed, `CONTINUE`
cards joined), `true`/`false` for a logical, a number for an integer (exact,
every digit, per [spec/virtualize.md §1.1](../../spec/virtualize.md#11-output-and-equivalence))
or a real (its binary64 value; `D` exponents read as `E`), `[re, im]` for a
complex, `null` for no value; `COMMENT`, `HISTORY`, blank and orphan
`CONTINUE` cards have the value `null` and their text as the comment; the
`END` card is not listed. Order and duplicates are kept. Each header's raw
bytes are in the mirror, so nothing is lost by the typing.

**Budget.** A header's cards go in the node's document while the document
stays within 65536 bytes (SAFE's budget for XML); past it, `cards` is
`null` and the header is read from the mirror. The root lists every HDU;
with about 150 bytes per entry, 433 HDUs are 65 KB, the largest known case.

### 2.7 Source model (the IR)

Elements, with `k` the HDU's header blocks, `D` its data bytes, `p` its
padding, and types of
[conventions §8.6](../../spec/conventions.md#86-types):

| path | kind | type | extent: what |
|---|---|---|---|
| `""` | struct | | the whole file |
| `hdus/<i>` | struct | | HDU `i`, its blocks |
| `hdus/<i>/header` | value | `{card:ascii[80]}[36k]` | the header blocks, `END` and blank padding cards included (so the parser holds it and the view shows it as a spill past 1024 bytes) |
| `hdus/<i>/data` | data | | an image's `D` bytes: geometry `{"data_type", "shape"}`, codec `{"endian": "big"}`, recipe `[["src", 0, null]]` |
| `hdus/<i>/rows` | data | | a table's `NAXIS1 × NAXIS2` bytes: geometry `{"rows", "row_bytes"}` |
| `hdus/<i>/table` | value | record `[NAXIS2]` | a compressed image's main table, as a record per row typed from `TFORMn` (`{data:{n:>i4,o:>i4},zscale:>f8,zzero:>f8}[4094]` for DECam); held by the parser |
| `hdus/<i>/gap` | value | `bytes[g]` | `THEAP − NAXIS1 × NAXIS2` bytes before the heap, when `g > 0` |
| `hdus/<i>/heap` | data | | a table's heap, one element |
| `hdus/<i>/heap/<j>` | data | | a compressed image's chunk `j`: the bytes from its first tile's start to its last tile's end; recipe `[["src", 0, null]]` when the tiles are adjacent, else one `src` part per tile (a form per chunk, costly, kept for correctness) |
| `hdus/<i>/pad` | value | `bytes[p]` | the data padding to the block boundary, when `p > 0` (shorter at a truncated end of file) |
| `gaps/<offset>` | gap | | heap bytes no tile uses; bytes after the last HDU (special records) |

The geometry of `heap/<j>` names its tiles (`{"tiles": [r0, N]}`); the
projection builds the prefix from the `table` value, not from the form. The
forms of a file's chunks deduplicate to a handful (the distinct `N`).

### 2.8 What the layout does not express

Informative, as [mrc.md §7](../../spec/virtualize/mrc.md#7-what-the-layout-does-not-express):
the WCS (kept as cards, not interpreted: SIP, TPV and lookup distortions are
a library's job), `BSCALE`/`BZERO` (recorded), the ASDF tree and Rubin's
JSON (bytes of a table), checksums (cards), table columns (bytes), random
groups and tile-compressed tables (rejected).

## 3. The profile (Part 2: §15 of spec/virtualize.md)

### 3.1 Choosing the profile

[spec/virtualize.md §1.2](../../spec/virtualize.md#12-input)'s table gains a
row before the last: bytes 0–8 are `SIMPLE  =` and byte 29 is `T` → FITS
profile. `SIMPLE = F` (byte 29 `F`), bzip2 (`BZh`) and gzip (`1F 8B`) fall to
the last row and are rejected; the row's note names them.

### 3.2 Reading

The parser reads headers and main tables only, in rounds
(`Step::Read(ranges)`, as every IR parser):

1. a window of `min(size, 2^16)` bytes at offset 0;
2. for each HDU in turn: if the window at its offset holds no `END` within
   the header's blocks read so far, read on in 2^16-byte windows (a header
   MUST end within 2^24 bytes, else rejected); when the header is parsed and
   the HDU is a compressed image, read its main table (`NAXIS1 × NAXIS2`
   bytes, 98 KB for a DECam chip; at most 2^26 bytes, else rejected); then
   read the window at the next HDU's offset. The next header's window and
   the current table are requested in one round.

A DECam file takes about 63 rounds and 98 KB of table per chip; a plain
MEF one round per HDU. The budget rules of the core apply unchanged.

### 3.3 Checks

In this order, per HDU, every check applying whatever the other values are:

- **Blocks:** the file's size is at least 2880; every HDU starts at a block
  boundary (by construction) and its header ends with an `END` card within
  2^24 bytes of its start; a header card is 80 bytes of printable ASCII
  (bytes 32–126): a byte outside that range rejects (a NUL in the header,
  which happens in corrupted files).
- **Mandatory cards:** the primary begins `SIMPLE`, `BITPIX`, `NAXIS`,
  `NAXIS1..n`; an extension begins `XTENSION`, `BITPIX`, `NAXIS`,
  `NAXIS1..n`, `PCOUNT`, `GCOUNT`, with the fixed-format values the standard
  requires: `BITPIX` in {8, 16, 32, 64, −32, −64}, `NAXIS` in 0..999,
  `NAXISn ≥ 0`, `PCOUNT ≥ 0`, `GCOUNT ≥ 1`; `XTENSION` is `IMAGE`, `TABLE`
  or `BINTABLE` (any other is rejected and named); `GROUPS = T` is rejected
  and named.
- **Data size:** `D` computed exactly is at most 2^53 − 1 and
  `offset + D ≤ size`. The padding may be short (a truncated final block);
  the next HDU, if any, starts at the next block boundary, which MUST be
  within the file.
- **Tables:** `BITPIX = 8`, `NAXIS = 2`, `TFIELDS` in 0..999 with `TFORMn`
  for each, `THEAP` (default `NAXIS1 × NAXIS2`) at least that and
  `THEAP + H = NAXIS1 × NAXIS2 + PCOUNT`; `ZTABLE = T` rejected.
- **Compressed images:** `ZCMPTYPE` in {`RICE_1`, `RICE_ONE`, `GZIP_1`,
  `GZIP_2`, `NOCOMPRESS`} (`HCOMPRESS_1`, `PLIO_1` rejected and named),
  `ZBITPIX` and `ZNAXIS`/`ZNAXISn` as for an image, `ZTILEn` in 1..ZNAXISn,
  `ZQUANTIZ` in {`NO_DITHER`, `SUBTRACTIVE_DITHER_1`,
  `SUBTRACTIVE_DITHER_2`} with `ZDITHER0` in 1..10000 when dithered, the
  columns `COMPRESSED_DATA` (`1PB`, `1QB`), optional `GZIP_COMPRESSED_DATA`
  (`1PB`/`1QB`), `ZSCALE` (`1D`), `ZZERO` (`1D`), `ZBLANK` (`1J`), and no
  other (`UNCOMPRESSED_DATA`, `NULL_PIXEL_MASK` rejected and named); the row
  count equals the tile count; every descriptor's range lies within the
  heap; for each tile exactly one of its descriptors is non-null, or none
  (absent tile); the tiles of each chunk are in storage order in the heap
  (a tile starting before the previous one ends, or before the previous
  tile, rejects in version 0).
- **Limits:** at most 2^16 HDUs; a main table at most 2^26 bytes.

A rejection names the HDU and the card: `HDU 3 (SCI,1): BITPIX 12 is not
one of 8, 16, 32, 64, -32, -64`.

### 3.4 Chunks and the source table

Images: [conventions §7](../../spec/conventions.md#7-source-values-as-arrays)
with the 2^20 limit, one range of source 0 per chunk plus a literal padding
or a copy, as [mrc.md §14.2](../../spec/virtualize/mrc.md#142-chunks).
Compressed images: a literal prefix then the ranges of
[§2.4](#24-tile-compressed-images). Tables: ranges of whole rows and heap
blocks. The output has no data sources: the source table is source 0 alone.
`refkv_source` is the mirror.

### 3.5 Summary

Both implementations print `hdus` (the count), `kinds` (counts by kind),
`arrays` (the arrays' paths with shape, data type and chunk shape),
`compressed` (the compression types used), `chunks`, `cards` (the total),
and `tables`.

## 4. Modules

All parsing and projection is in the Rust core, as for MRC; the hosts add
detection and the codec.

### 4.1 Rust core (`rust/refkv-ir`)

| file | what |
|---|---|
| `src/fits.rs` | the sans-IO parser: `detect(head)`, `Fits::new(size)`, `step`, `feed`, `finish`; `Header` (cards from 80-byte records; `card(k)`, `int`, `float`, `logical`, `string`, long-string joining, `HIERARCH`), `Hdu` (offsets, kind, sizes), the table-row record type from `TFORMn`, and the tile grid |
| `schema/fits.json` | the source model: the elements of [§2.7](#27-source-model-the-ir), the `bitpix` and `compression` tables, and every check of [§3.3](#33-checks) as a role with its message, in order |
| `src/project/fits.rs` | the projection: root and HDU documents, image arrays through `grid_chunks` (limit 2^20, copy threshold), tile chunks (`tiles.rs` helpers: the grid cut, the prefix bytes, the adjacency merge), table arrays through `grid_chunks` and `blob_chunks`, the cards JSON, the summary |
| `src/coding.rs` or `src/fits_tiles.rs` | the `fits_tiles` decoder: RICE_1 (the `ricecomp` crate, 0.5.0, MIT, a port of cfitsio's `ricecomp.c` that is bit-compatible with `fits_rdecomp`; its wasm build to confirm, else a port of the same 250 lines), GZIP_1/2 (miniz_oxide, already a dependency; the byte unshuffle), the dequantizer with the dither table. One implementation serves Python, the browser and the Neuroglancer fork |
| `src/run.rs`, `src/lib.rs`, `src/rules.rs`, `src/project/mod.rs` | `Format::Fits`, `detect`, `parser!(fits::Fits)`, `FITS_SCHEMA`, dispatch: the MRC commit's lines, once more |
| `tests/parsers.rs` | one `reject!` per rejection fixture, with its message |
| `profiles.json` | regenerated by `just profiles` |

### 4.2 Hosts

- **Python** (`python/src/refkv`): `ir/__init__.py` names the format;
  `_core` gains `fits_tiles_decode(chunk: bytes, config: str) -> bytes` in
  `python/src/lib.rs` (the first decoder the core exposes to a host;
  `python/src/refkv/zarr/codecs.py` registers `fits_tiles` as an
  `ArrayBytesCodec` that calls it and reshapes). No astropy in the package.
- **Browser** (`js/src/virtualize`): `index.ts` detects `SIMPLE  =`,
  `IrFormat` gains `"fits"`, `ir/run.ts` passes it through; the wasm build
  exports `fits_tiles_decode`; `js/src/virtualize/codecs` (or wherever the
  demo's decoders live, cf. `demo/jpegxr_decoder.ts`) wraps it for the
  demo's reader.
- **Neuroglancer fork** (`~/dev/neuroglancer`, branch `vzip`): a
  `fits_tiles` codec that calls the same wasm, as JPEG XR was added
  (`src/sliceview/jpegxr/`). Without it `.fz` files virtualize but do not
  display; `.fits` files need nothing.
- **CLI** (`rust/refkv-cli`): the format name in `main.rs`;
  `tests/identical.py` picks up the fixtures by suffix.

### 4.3 Codecs

`fits_tiles` is specified in the convention (prefix layout, decoding,
dither) and implemented once, in Rust, tested against astropy's
`CompImageHDU` decoding of every compressed fixture, tile by tile, and
against cfitsio's reference values for the dither table (the 10,000th seed
is 1043618065).

## 5. Tests

`python/tests/ir/test_fits.py`, following the house rule (one test for the
accepted combinations, one per error), with astropy as the independent
reader (as mrcfile is for MRC):

- `test_virtualize_fits_outputs`: parametrized over the accepted fixtures.
  For each: the output's HDU table equals astropy's `HDUList` (index, type,
  name, version, offsets from `fileinfo()`), every image array read through
  the archive equals `hdu.data` with `do_not_scale_image_data=True` (stored
  values) and `CompImageHDU.data` for compressed HDUs, every `rows`
  array equals the table's raw bytes, every HDU's `cards` equal
  `hdu.header.cards` by keyword, value and comment (astropy's parsing is the
  oracle for `CONTINUE`, `HIERARCH`, `D` exponents), the mirror rebuilds the
  file byte for byte, and the browser twin's output is identical.
- One test per rejection fixture (`fits_reject_*`): `short`, `not_simple`,
  `simple_false`, `no_end`, `header_nul`, `bitpix`, `naxis_negative`,
  `naxis_999`, `pcount_negative`, `gcount_zero`, `xtension_unknown`,
  `groups`, `data_outside`, `data_overflow`, `next_hdu_outside`,
  `table_bitpix`, `table_naxis`, `tfields`, `tform_missing`,
  `theap_small`, `ztable`, `zcmptype_hcompress`, `zcmptype_plio`,
  `zcmptype_unknown`, `ztile_zero`, `ztile_large`, `zquantiz_unknown`,
  `zdither0_range`, `columns_extra`, `uncompressed_data`,
  `null_pixel_mask`, `row_count`, `descriptor_outside`, `descriptor_both`,
  `tiles_out_of_order`, `too_many_hdus`, `table_too_large`, `gzip_file`,
  `bzip2_file`.
- Unit tests of the card parser (each value type, long strings, `HIERARCH`,
  blank and commentary cards, a malformed value), of the tile grid cut
  (row tiles, square tiles, 3-D plane tiles, edge tiles), of the prefix
  bytes, and of `fits_tiles_decode` against astropy for each compression,
  `ZBITPIX`, quantization and tile shape, including edge tiles and absent
  tiles.
- `python/tests/test_virtualize_conventions.py` picks up the fixtures and
  schema once the profile is registered; `rust/refkv-cli/tests/identical.py`
  and `just ir-wasm` likewise.

## 6. Fixtures

`fixtures/generators/fits/write_fixtures.py` writes `fixtures/fits/*.fits`
with astropy, and by hand (`struct`, 80-byte cards) where a file must be
wrong. Pixels are seeded ramps, distinct per voxel, as MRC's. Every accepted
fixture is checked once, at generation, to open in astropy (and, for
compressed ones, to decompress).

Accepted:

| fixture | covers |
|---|---|
| `fits_primary_int16` | one primary image, `BZERO = 32768`, `BUNIT`, a TAN WCS |
| `fits_primary_float32_cube` | 3-D float32 with a degenerate 4th axis (radio) |
| `fits_bitpix_<t>` | `uint8`, `int32`, `int64`, `float64`; `BLANK`; `BSCALE`/`BZERO` non-trivial |
| `fits_mef_sci_err_dq` | empty primary + `SCI`/`ERR`/`DQ` × 2 with `EXTVER`, the HST shape |
| `fits_mef_naxis0` | an extension with `NAXIS = 0` and full WCS cards (STIS) |
| `fits_mef_naxisn_zero` | an image with `NAXIS2 = 0`: an empty array |
| `fits_chunk_rows`, `fits_chunk_runs`, `fits_chunk_edge_literal` | the §7 cut: row bands, runs of a row (a wide image), an edge chunk's literal and a copied one (copy threshold) |
| `fits_header_long` | 300 cards, `CONTINUE` strings, `HIERARCH`, `COMMENT`/`HISTORY`, blank cards, duplicates, `D` exponents, complex values, a header over 1024 bytes but under the budget |
| `fits_header_huge` | a header past the 65536-byte cards budget: `cards` null |
| `fits_trailing` | bytes after the last HDU (special records) |
| `fits_truncated_pad` | the last HDU's padding short |
| `fits_comp_rice_int16`, `_int32`, `_uint8` | RICE_1 row tiles, `BYTEPIX` 2 and 4, `BLOCKSIZE` 16 and 32 |
| `fits_comp_rice_float_dither1`, `_dither2`, `_nodither` | quantized float32 with each `ZQUANTIZ`, `ZSCALE`/`ZZERO` columns, a NaN pixel (`ZBLANK`) |
| `fits_comp_tiles_square` | 100 × 100 tiles on a 350 × 230 image: edge tiles, multi-tile chunks |
| `fits_comp_tiles_3d` | per-plane tiles of a cube |
| `fits_comp_gzip1`, `fits_comp_gzip2`, `fits_comp_nocompress` | the other compressions |
| `fits_comp_gzip_fallback` | a tile in `GZIP_COMPRESSED_DATA` with a null `COMPRESSED_DATA` descriptor |
| `fits_comp_absent_tile` | a row with both descriptors null |
| `fits_comp_heap_gaps` | tiles in order with unused heap bytes between them (per-tile recipe; gaps) |
| `fits_comp_q_descriptors` | `1QB` descriptors |
| `fits_comp_rice_one` | `ZCMPTYPE = 'RICE_ONE'` (Legacy Surveys) |
| `fits_comp_many` | 8 compressed HDUs with distinct `ZDITHER0` (the DECam shape, small) |
| `fits_bintable` | fixed columns of every `TFORM` letter, `TDIM`, `TSCAL`/`TZERO`, `TNULL` |
| `fits_bintable_heap` | `P` and `Q` columns, `THEAP` past the rows (a gap), the heap |
| `fits_bintable_asdf` | a one-row `NB` column holding YAML (the JWST shape) |
| `fits_asciitable` | a `TABLE` extension with `TBCOLn` |
| `fits_big_header_count` | 64 HDUs (the root's table) |

Rejected: one fixture per rejection test of [§5](#5-tests), named
`fits_reject_<case>`. `fits_reject_gzip_file` and `fits_reject_bzip2_file`
are compressed copies of `fits_primary_int16`.

A fixture past 16 MiB (a cube whose planes exceed the 2^20 limit and whose
rows exceed it too) is generated at test time into a temporary directory,
as CZI's `czi_big_plane` is.

## 7. Verifier

`js/test/fits/verify.py` serves the fixtures over local HTTP, runs the
browser virtualizer under Node, opens every archive through the reference
reader and zarr-python with `refkv.zarr` (the `fits_tiles` codec
registered), and checks:

1. **Pixels, per HDU**, against astropy: stored values for images
   (`do_not_scale_image_data=True`), decompressed values for compressed
   images (astropy's `CompImageHDU.data`, float32 compared exactly: the
   dequantization is deterministic), raw bytes for tables.
2. **Cards**, per HDU, against `Header.cards`.
3. **Reconstruction**: the mirror rebuilds the file byte for byte
   (`ir_mirror.mirror_problems`), fixtures with trailing bytes and short
   padding included.
4. **Rejections**: every `fits_reject_*` is rejected with the profile's
   message.

## 8. Corpus

`conformance/virtualize/corpus_fits.txt`, each `no-ref` (no frozen
reference), compared py with web, over HTTP ranges:

| file | URL | why |
|---|---|---|
| astropy `comp.fits`, `test0.fits`, `o4sp040b0_raw.fits`, `tb.fits`, `variable_length_table.fits`, `checksum.fits` | `https://raw.githubusercontent.com/astropy/astropy/<sha>/astropy/io/fits/tests/data/` (BSD-3-Clause; GitHub raw serves ranges, as the MRC corpus relies on) | RICE int16 row tiles; MEF with `EXTVER`; HST STIS with `NAXIS = 0` placeholders; tables; checksums |
| SDSS `spec-0266-51602-0001.fits` | `https://data.sdss.org/sas/dr17/sdss/spectro/redux/26/spectra/lite/0266/` | three binary tables |
| HiPS tile | `https://alasky.cds.unistra.fr/Pan-STARRS/DR1/g/Norder3/Dir0/Npix0.fits` | one float32 512×512 |
| radio cube | `https://astropy.stsci.edu/data/l1448/l1448_13co.fits` | 3-D cube |
| Pan-STARRS skycell | `https://ps1images.stsci.edu/rings.v3.skycell/1405/053/rings.v3.skycell.1405.053.stk.g.unconv.fits` (67 MB) | RICE int16 row tiles, `BZERO`, 6243 tiles |
| Legacy Surveys brick | `https://portal.nersc.gov/cfs/cosmo/data/legacysurvey/dr10/south/coadd/000/0001m002/legacysurvey-0001m002-image-g.fits.fz` (11 MB) | `RICE_ONE`, 100×100 tiles, `SUBTRACTIVE_DITHER_2` |
| ZTF `sciimg` | `https://irsa.ipac.caltech.edu/ibe/data/ztf/products/sci/2018/0411/467847/ztf_20180411467847_000535_zr_c11_o_q3_sciimg.fits` (38 MB) | plain float32, TPV |
| DECam `ooi` | NOIRLab archive API, `https://astroarchive.noirlab.edu/api/retrieve/88094a7fab884fbbcb440cd795e3fde0/` (331 MB; stability of the URL to confirm) | 61 RICE chips, dithered floats, 250k tiles |
| JWST `jw02736001001_02101_00001_nrca1_{uncal,cal,i2d}.fits` | `s3://stpubdata/jwst/public/jw02736/jw02736001001/` over HTTPS (public bucket; CORS and anonymous HTTPS to confirm) | uint16 4-D, `ASDF` table |
| HST `j8pu0y010_drc.fits` | `s3://stpubdata/hst/public/j8pu/j8pu0y010/` (224 MB) | `SCI`/`WHT`/`CTX`, `HDRTAB` |
| SDSS `frame-r-003918-3-0213.fits.bz2` | `https://data.sdss.org/sas/dr17/eboss/photoObj/frames/301/3918/3/` | the bzip2 rejection |

The 45 GB TESS cube and the 7.3 GB Euclid VIS frame (433 HDUs) are
stress cases for `--quick`-less runs, readable by header walk alone.

## 9. Shared changes

- `spec/virtualize/profiles.json`: the entry `fits` (section 15, title
  `FITS`, kind `file`, suffixes `[".fits", ".fz"]`, reference `null`,
  phrases); `just profiles` regenerates the hosts' tables, the core's copy
  and the profile tables of the spec, the docs and the demo.
- `spec/virtualize.md`: the detection row ([§3.1](#31-choosing-the-profile));
  `spec/virtualize/fits.md` and `fits/schema.json`; `spec/generate_schemas.py`:
  `SOURCE_METADATA["fits"]` (root HDU table and cards, HDU nodes' cards,
  image scaling members, table columns, compression); `just sections`.
- `spec/conventions.md`: nothing new; the convention cites §7, §8, §2.
- `pyproject.toml` dev dependencies: `astropy` (the generator and the
  verifier; the package itself never imports it).
- `docs/virtualize/fits/README.md` (who it is for, what you get, the `.fz`
  codec and the viewers that have it, what is not done: columns, WCS
  interpretation, bzip2) and the docs index via `just profiles`.
- `demo/index.html`: an example per form, from a host that allows
  cross-origin range requests: astropy's `comp.fits` on GitHub raw (tiny),
  the Pan-STARRS skycell or the HST `_drc` on S3 (CORS to confirm), with
  credits in the footer. A `.fz` example displays only once the Neuroglancer
  fork has the codec.
- `rust/refkv-ir/wasm/run.mjs`: the sample fixture (via `profiles.py
  sample`).

## 10. Order of work

1. **Parser, plain files:** `fits.rs` cards and HDU walk, `schema/fits.json`
   with the block, card, mandatory and data-size checks, IR elements for
   empty, image and table HDUs; the plain fixtures; `tests/parsers.rs`.
2. **Projection, plain files:** root and HDU documents, image and table
   arrays, cards JSON, budgets, summary; `test_fits.py` outputs test against
   astropy; the mirror's rebuild.
3. **Compressed images, parser:** the table read round, the column typing,
   the tile grid, the descriptor checks, `heap/<j>` elements; the compressed
   fixtures and rejections.
4. **Codec:** `fits_tiles_decode` in Rust (RICE, gzip, unshuffle,
   dequantize, dither), unit-tested against astropy per tile; the Python
   binding and codec class; the wasm export.
5. **Projection, compressed images:** the prefix, the chunk references,
   the decoded-size cut; the outputs test extended to compressed fixtures.
6. **Hosts:** Python detection, browser detection and codec, CLI;
   `just ir-wasm`, `identical.py`, the verifier.
7. **Registry, spec, schema, docs, demo**, `just sections`.
8. **Corpus run** (`just compare` on `corpus_fits.txt`), the Neuroglancer
   fork's codec, and the demo's `.fz` example.

Steps 1–2 are a working profile for every uncompressed archive (HST, JWST,
Euclid, ZTF, TESS, DESI, HiPS, radio); 3–5 add the ground-based `.fz` world.

## 11. Open questions and their decisions

Recommended answers; the convention states whatever is decided.

1. **Unsigned integers.** `BZERO = 32768` on `BITPIX = 16` (HST raw, JWST
   uncal) and `BZERO = 2^31` on 32 (DESI masks) are stored signed. Options:
   present the stored signed integers and record the scaling (NIfTI's
   precedent, needed for every other `BSCALE`/`BZERO` anyway); or recognize
   the four exact unsigned cases and present `uint16`/`uint32`/`uint64`
   through a one-line sign-flip codec that no Zarr implementation has.
   **Decision: record only, in version 0.** A `fixedscaleoffset` codec is
   wrong (it yields float64) and a custom codec would be required by every
   reader of the most common FITS files. A later `fits-dataset` layout can
   apply CF `add_offset` for xarray.
2. **HDU paths by index**, names in metadata. **Decision: yes.**
3. **Chunk limit 2^20 decoded bytes** for images and compressed images,
   so that both forms of a file share a grid. **Decision: yes.** (Sky images
   are read 2-D; 25-row bands across 10,000 columns are what `.section`
   readers already fetch, and the Neuroglancer fork handles them.)
4. **The prefix layout** (fixed 32-byte records, `r0` in the header) versus
   a side array of per-tile parameters. **Decision: the prefix.** A codec
   cannot read a side array, and the prefix is what makes concatenation
   across files codec-uniform.
5. **Tiles out of order in the heap: reject, or per-tile recipes.**
   **Decision: reject in version 0**, since no known writer produces them;
   gaps between in-order tiles are supported (per-tile parts).
6. **Where the decoder lives.** Rust in the core, exposed to Python and the
   browser, versus astropy's codec classes in Python and a TypeScript port
   for the browser. **Decision: Rust**, one implementation, with astropy as
   the oracle; this is the core's first host-facing decoder, so the binding
   shape (`fits_tiles_decode(bytes, config) -> bytes`) is new.
7. **Tables as bytes** in version 0; columns later. **Decision: yes.**
8. **Cards typing.** Typed values (lossy for a float's decimal text) with
   the raw header in the mirror, versus the raw 80-character text as a list
   of strings. **Decision: typed cards**, since the mirror keeps the text
   and consumers want values; astropy's parse is the oracle.
9. **Dataset layout** (`fits-dataset`: dimension names from `CTYPEn`,
   coordinates for linear axes such as a cube's spectral axis).
   **Decision: not in version 0**; a convention of its own later.
10. **Truncated final padding: tolerate.** **Decision: yes**, the `pad`
    element is the bytes present.
11. **`HCOMPRESS_1` and `PLIO_1`: reject.** **Decision: yes** in version 0;
    both are rare in archives (HCOMPRESS is lossy and used for quicklooks;
    PLIO for IRAF masks).
12. **Whole-file gzip and bzip2: reject.** **Decision: yes**; a deflate
    index (`indexed_gzip`-style) is a different project.
