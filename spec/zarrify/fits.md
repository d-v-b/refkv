# FITS

The Zarr layout of a FITS file (`.fits`): its images, its tables and its
headers, and the translation of its headers into JSON. What all of refkv's
conventions share is in [spec/conventions.md](../conventions.md), cited here
as "conventions §n". How refkv produces this layout as a reference store, and
which files it accepts, is the FITS profile, [Part 2](#part-2-the-profile)
below.

Convention version: 0 (until release, [conventions §1](../conventions.md#1-conventions)) · UUID: `4b0d3a7e-1c52-4f6a-9b8e-2d7f0c6a1e93` ·
Schema: [schema.json](fits/schema.json) · Changed 2026-10-25: an edge chunk's zero padding
is a range of the output's one zero data source, not a literal range of each reference, so that
the archive holds the zeros once ([§9.4](#94-chunks)); no chunk's values change, nor which
chunks are copied

This document has two parts ([conventions §11.1](../conventions.md#111-parts)).

**Draft.** This revision covers uncompressed images, multi-extension files,
tables and tile-compressed images (`ZIMAGE = T`, the `.fz` files `fpack`
writes) with the `RICE_1`, `GZIP_1`, `GZIP_2` and `NOCOMPRESS` algorithms;
`HCOMPRESS_1` and `PLIO_1` are rejected ([§9.3](#93-checks)). The design is
[design/plans/fits.md](../../design/plans/fits.md).

# Part 1. The convention

A FITS file is a sequence of **HDUs** (header-data units), each an integral
number of 2880-byte **blocks**: a **header** of 80-byte **cards**, 36 per
block, ending with `END` and padded with spaces to a block boundary, then the
**data**, padded with zero bytes to a block boundary. The first HDU is the
**primary**; each other is an **extension** named by its `XTENSION` card:
`IMAGE`, `TABLE` (an ASCII table) or `BINTABLE` (a binary table). FITS is the
FITS Standard 4.0 (IAU FITS Working Group, 2016/2018); card and keyword
names below are the standard's.

Unlike most of refkv's conventions, the hierarchy has no OME-NGFF metadata:
FITS axes are sky coordinates under projections, spectral axes and table
rows, not microscopy axes, and the world coordinate system is a header
convention that viewers and libraries interpret for themselves. This section
extends [conventions §2](../conventions.md#2-attributes) ("Nothing else"): a
node's attributes are `zarr_conventions` and `refkv_zarrified` alone.

## 1. Declaration

The root and `refkv_source` declare the convention
([conventions §11.2](../conventions.md#112-declaration)), the root with its source
metadata of [§5.1](#51-the-root) as the member `"fits"`. Every HDU's node declares
it too, with the HDU's source metadata ([§5.2](#52-an-hdus-node)). A table's `rows`
and `heap` arrays have no attributes. Its CMO is:

```json
{
  "uuid": "4b0d3a7e-1c52-4f6a-9b8e-2d7f0c6a1e93",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/zarrify/fits/schema.json",
  "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md",
  "name": "refkv_zarrified",
  "description": "The Zarr layout of a FITS source zarrified by refkv, and the source's metadata"
}
```

## 2. HDUs and nodes

The file's HDUs are numbered from 0 in file order. HDU `i` is the node at
the path **`<i>`** (`0`, `1`, …), a child of the root. `EXTNAME` and
`EXTVER` are metadata, not paths: `EXTNAME` is optional and repeats (`SCI`
four times in a WFPC2 file, 144 times in a Euclid frame, with `EXTVER` or
a dotted name telling them apart), so a reader finds `SCI,2` in the root's
table of HDUs ([§5.1](#51-the-root)).

An HDU's **kind**, by its mandatory cards ([§9.3](#93-checks)):

| kind | when | the node |
|---|---|---|
| **empty** | `NAXIS = 0` | a group with no children: the HDU's metadata alone (the empty primary of a multi-extension file; placeholder extensions with a full header, as HST's STIS writes `ERR` and `DQ`) |
| **image** | the primary with `NAXIS ≥ 1`, or `XTENSION = 'IMAGE'`, or `XTENSION = 'BINTABLE'` with `ZIMAGE = T` (a tile-compressed image, [§3.3](#33-tile-compressed-images)) | an array ([§3](#3-images)); an array with no chunks when some `NAXISn` (`ZNAXISn`) is 0 |
| **table** | `XTENSION = 'BINTABLE'` or `'TABLE'` otherwise | a group with the arrays `rows` and, when the heap is not empty, `heap` ([§4](#4-tables)) |

Random groups (`GROUPS = T`), tile-compressed tables (`ZTABLE = T`) and
extensions of any other `XTENSION` are rejected by the profile.

## 3. Images

### 3.1 The array

- **Shape:** `[NAXISn, …, NAXIS1]`: the data are stored with `NAXIS1`
  varying fastest, so the Zarr shape is the FITS axes reversed.
- **Data type:** by `BITPIX`: 8 `uint8`, 16 `int16`, 32 `int32`, 64 `int64`,
  −32 `float32`, −64 `float64`.
- **Chunks** ([conventions §7](../conventions.md#7-source-values-as-arrays),
  contiguous values): the data is the row-major array `V` of the array's
  shape, with elements of `|BITPIX| / 8` bytes, at the HDU's data offset. It
  is cut as contiguous values with the **chunk limit `L = 2^20` bytes**
  (1 MiB): whole planes when they are small, else bands of rows, else runs
  of a row; an edge chunk along the cut axis is followed by zero bytes up to
  the whole chunk.
- **Codecs:** `[{"name": "bytes", "configuration": {"endian": "big"}}]`, or
  `[{"name": "bytes"}]` for `uint8`. FITS data are big-endian.
- **Fill value:** 0.
- **Dimension names:** `axis<n>` for FITS axis `n`, in the array's order
  (`["axis2", "axis1"]` for a 2-D image): `CTYPEn` is optional, repeats and
  names a coordinate type, not a dimension.
- **Attributes:** the declaration ([§1](#1-declaration)) with the HDU's
  source metadata ([§5.2](#52-an-hdus-node)).

For example, a `10272 × 4755` float32 plane (a JWST mosaic's `SCI`) has rows
of 41088 bytes, so `floor(2^20 / 41088) = 25` rows per chunk and `k =
ceil(4755 / 25) = 191` chunks of shape `[25, 10272]`, the last of 5 rows
padded with zero bytes. A `1100 × 1000` uint8 plane of 1100000 bytes, over
`L`, is cut along its rows into 2 chunks of 550 rows, `[550, 1000]`; a
20-plane stack of `128 × 256` int16 (64 KiB planes) into 2 chunks of 10
planes, `[10, 128, 256]`.

### 3.2 Value scaling

The array holds the stored values. FITS's value scaling, `physical = BZERO +
BSCALE × stored`, is not applied; `BSCALE`, `BZERO`, `BLANK` (the stored
value of an undefined integer pixel) and `BUNIT` are recorded in the node's
source metadata ([§5.2](#52-an-hdus-node)). That includes the unsigned
convention: a `BITPIX = 16` image with `BZERO = 32768` and `BSCALE = 1` is
`uint16` data stored as `int16`, and its array is `int16`.

### 3.3 Tile-compressed images

A tile-compressed image (the FITS tiled image compression convention, FITS
Standard 4.0 section 10) is a `BINTABLE` with `ZIMAGE = T` whose rows are the
**tiles** of an image of shape `[ZNAXISn, …, ZNAXIS1]` and `ZBITPIX`, each
tile of shape `[ZTILEn, …, ZTILE1]` (`ZTILE1 = ZNAXIS1` and the others 1 when
absent: a row per tile), numbered in storage order (the tile index along
axis 1 fastest). A row's `COMPRESSED_DATA` descriptor gives the tile's
stream in the heap, coded with `ZCMPTYPE`, or its `GZIP_COMPRESSED_DATA`
descriptor a gzip member of the tile's raw values instead (the writer's
fallback); a row with both null has no tile. A float image (`ZBITPIX < 0`)
whose table has `ZSCALE` and `ZZERO` columns is **quantized**: each tile's
stream codes 32-bit integers `I`, and its values are `(I − R + 0.5) × ZSCALE
+ ZZERO` for the subtractive dithers (`ZQUANTIZ`), `I × ZSCALE + ZZERO`
without one, in binary64 with each operation rounded
([spec/zarrify.md §1.3](../zarrify.md#13-arithmetic)), then the
array's type; `R` is the dither's random value, from cfitsio's table of
10,000 values (the Park–Miller generator from seed 1, each value as a
binary32), which tile `t` enters at `((t + ZDITHER0 − 1) mod 10000)` and
walks as cfitsio does (`unquantize.c`: the start `int(R_0 × 500)`, one
value per pixel, NaN pixels included, the index wrapping at 10,000); an
integer equal to `ZBLANK` (the row's, or the header's) is NaN, and
`−2147483646` is 0 under `SUBTRACTIVE_DITHER_2`. The tiles of an integer
image, and a float image's fallback tiles, are the values as stored.

The HDU's array has the shape `[ZNAXISn, …, ZNAXIS1]`, the data type of
`ZBITPIX` ([§3.1](#31-the-array)), the fill value 0, the dimension names of
[§3.1](#31-the-array), and:

- **Chunk grid.** The tile grid `[g_n, …, g_1]`, `g_j = ceil(ZNAXIS_j /
  ZTILE_j)`, is cut into chunks of consecutive tiles: with `b` the decoded
  bytes of a tile (`ZTILE1 × … × ZTILEn × |ZBITPIX| / 8`), `s_j = b × g_{j−1}
  × … × g_1` the bytes of one index along axis `j`, and the limit `L =
  2^20`, the cut axis `a` is the slowest axis with `s_a ≤ L`, or axis 1 if
  none; a chunk holds `c` indexes along `a`, `c = ceil(g_a / k)` with `k =
  ceil(g_a / max(1, floor(L / s_a)))`, one tile along each slower axis, and
  every tile along each faster axis. The chunk shape is `ZTILE_j` along the
  slower axes, `c × ZTILE_a` along `a`, and `ZNAXIS_j` along the faster
  axes; the grid is `g_j`, `ceil(g_a / c)` and 1. A chunk's tiles are then
  consecutive in storage order. A DECam chip (4094 row tiles of 8184 bytes)
  has chunks of 128 rows, 32 of them; a Legacy Surveys brick (`100 × 100`
  float32 tiles on `3600 × 3600`, a tile row of 1.44 MB) has chunks of 18
  tiles, `[100, 1800]`, 72 of them.
- **Codecs:** `[{"name": "refkv.fits_tiles", "configuration": C}]`, `C` the object
  `{"compression": ZCMPTYPE (`RICE_ONE` written `RICE_1`), "parameters":
  {"BLOCKSIZE": b, "BYTEPIX": p} (the `ZNAMEi`/`ZVALi` pairs; 32 and 4 when
  absent), "zbitpix": ZBITPIX, "shape": [ZNAXISn, …, ZNAXIS1], "tile_shape":
  [ZTILEn, …, ZTILE1], "quantize": ZQUANTIZ (`NO_DITHER` when absent),
  "dither_seed": ZDITHER0 (0 when absent)}`.
- **Chunks.** A chunk with at least one tile is present; its bytes are a
  **prefix** followed by its tiles' streams in storage order, and its
  reference is the prefix as a literal range followed by the ranges of the
  streams ([§9.4](#94-chunks)). The prefix is little-endian: `u32` the
  chunk's tile count `N`, `u32` the storage index of its first tile, then
  `N` records of 32 bytes: `u32` the stream's length (0: no tile), `u32`
  its kind (0: `COMPRESSED_DATA`; 1: `GZIP_COMPRESSED_DATA`), `i32` the
  tile's `ZBLANK` (its row's, else the header's, else 0), `u32` flags (bit
  0: the table has `ZSCALE` and `ZZERO`; bit 1: a `ZBLANK` was given),
  `f64` `ZSCALE`, `f64` `ZZERO` (the row's; 0 without the columns). A chunk
  whose tiles are all absent has no entry and reads as the fill value.
- **Decoding** (the codec): each tile's stream by its kind and
  `compression` (`RICE_1` as cfitsio's `fits_rdecomp`, `_short` and `_byte`
  for `BYTEPIX` 4, 2 and 1; `GZIP_1` a gzip member of big-endian values;
  `GZIP_2` the same with the values' bytes shuffled, the first bytes of
  every value first; `NOCOMPRESS` the big-endian values), dequantized as
  above when quantized, then placed at the tile's position in the chunk
  (the tile's coords less the first tile's, times the tile shape); an edge
  tile is smaller than the tile shape; the chunk's other pixels are 0.
- **Attributes:** as an image's ([§5.2](#52-an-hdus-node)); the table's
  cards (the image's, copied by the writer, among them).

The main table (the descriptors) is in the mirror ([§6.1](#61-elements)) and
is not an array; the columns are in the root's table of HDUs
([§5.1](#51-the-root)).

## 4. Tables

A binary table is rows of `NAXIS1` bytes, `NAXIS2` of them, each a
concatenation of its `TFIELDS` columns' fields with no padding, then, after
`THEAP` bytes from the start of the data (`NAXIS1 × NAXIS2` when the card is
absent), the **heap** of variable-length arrays, the last of the `PCOUNT`
supplemental bytes. An ASCII table is rows of text and has no heap. A
column is a strided read, and a Zarr v3 struct data type cannot record
big-endian fields, so this version keeps a table as bytes that any FITS
table reader decodes:

- **`<i>/rows`:** `uint8`, shape `[NAXIS2, NAXIS1]`, dimension names
  `["row", "byte"]`, contiguous values cut with the limit `2^20` (whole
  rows), codecs `[{"name": "bytes"}]`, fill value 0; absent when `NAXIS1 ×
  NAXIS2` is 0;
- **`<i>/heap`:** `uint8`, shape `[H]`, dimension names `["byte"]`, where
  `H = PCOUNT − (THEAP − NAXIS1 × NAXIS2)` is the heap's length, present when
  `H ≥ 1`, cut as bytes (chunks of at most `2^24` bytes,
  [conventions §7](../conventions.md#7-source-values-as-arrays)), the last
  chunk padded with zero bytes;
- the column definitions in the group's source metadata
  ([§5.2](#52-an-hdus-node)).

The bytes between the rows and the heap, when `THEAP` leaves any, are in the
mirror ([§6.1](#61-elements)), not in an array.

## 5. Source metadata

### 5.1 The root

The root's source metadata `fits` is an object with the members `hdus` and
`cards`, in that order:

- `hdus`: an array with one object per HDU, in file order, each with, in
  this order: `index` (the HDU's number), `type` (`"primary"`, `"image"`,
  `"table"` or `"bintable"`, the primary or the `XTENSION` value in lower
  case), `kind` (`"empty"`, `"image"` or `"table"`, [§2](#2-hdus-and-nodes)),
  `name` (`EXTNAME`'s string, trailing spaces trimmed, or `null`), `version`
  (`EXTVER`'s integer, or `null`), `bitpix`, `naxis` (`[NAXIS1, …, NAXISn]`,
  in FITS order), `pcount` and `gcount` (0 and 1 for the primary), `offset`
  (the HDU's first byte), `header_bytes` (its header's blocks), `data_offset`
  and `data_bytes` (`D` of [§9.3](#93-checks)), and `compression`: `null`,
  or for a tile-compressed image ([§3.3](#33-tile-compressed-images)) the
  object `{"type", "parameters", "zbitpix", "tile", "quantize",
  "dither_seed", "zblank", "tiles"}` (the codec's values, `tile` in the
  array's order, `zblank` the header's or `null`, `tiles` the tile count);
- `cards`: the primary header's cards ([§5.3](#53-cards)).

### 5.2 An HDU's node

Each HDU's node (the array of an image, the group of a table or of an empty
HDU) has the source metadata `fits` with, in this order: `index`, `cards`
([§5.3](#53-cards)), then for an image `bscale`, `bzero`, `blank` (each the
card's number, or `null` when absent or not a number) and `bunit` (the
card's string, or `null`); for a table `columns`, an array with one object
per column `n` of `1 … TFIELDS`: `name` (`TTYPEn`), `form` (`TFORMn`, trailing
spaces trimmed), `unit` (`TUNITn`), `dim` (`TDIMn`), `scale` (`TSCALn`), `zero`
(`TZEROn`), `null` (`TNULLn`), `disp` (`TDISPn`) and `bcol` (`TBCOLn`), each
the card's string or number, or `null` when absent.

### 5.3 Cards

A header's cards are an array of `[keyword, value, comment]` triples, one
per card in header order, the `END` card not included, duplicates kept:

- `keyword`: bytes 0–7 of the card as text, trailing spaces trimmed; for an
  ESO `HIERARCH` card (keyword `HIERARCH` without a value indicator at bytes
  8–9), the text between `HIERARCH ` and the `=`, trimmed, as written
  (`"ESO INS OPTI-3 ID"`);
- `value`: for a card with a value indicator (`= ` at bytes 8–9), its value
  field (bytes 10–79) read as the standard says: a string in single quotes,
  with doubled quotes undoubled and trailing spaces trimmed, as a JSON
  string; `T` or `F` as `true` or `false`; an integer (an optional sign and
  digits) exactly, as a JSON number up to 2^53 − 1 in magnitude and as a
  string of its digits past that; a real as its binary64 value (a `D`
  exponent read as `E`); a complex `(r, i)` as `[r, i]`; nothing as `null`;
  any other value text as `{"text": t}`. A string ending in `&` followed by
  `CONTINUE` cards (bytes 8–9 spaces, a string value) is one card whose
  string is the pieces joined without their `&`, the comments of the pieces
  joined with a space; an orphan `CONTINUE`, `COMMENT`, `HISTORY` and blank
  cards, and any card without a value indicator, have the value `null`;
- `comment`: the text after the `/` that follows the value, trimmed, or
  `null` when there is none; for a card without a value indicator, bytes
  8–79 trimmed (a `COMMENT` or `HISTORY` card's text).

**Budget.** `cards` is the array when its JSON text is at most 65536 bytes,
and `null` otherwise (a header of several hundred cards); the header's
bytes are in the mirror in any case.

## 6. The IR mirror

`refkv_source` is the file's IR mirror ([conventions §11.3](../conventions.md#113-the-ir-mirror)).
The view shows each HDU's
header as a spill (a header is at least 2880 bytes, over the 1024 bytes a
shown value may have); its cards are in the nodes' source metadata.

### 6.1 Elements

Paths are relative to the IR's root, whose path is `""`
([conventions §8.1](../conventions.md#81-elements)). Types are those of
[conventions §8.6](../conventions.md#86-types). For HDU `i`, with `k` its
header's blocks, `D` its data bytes ([§9.3](#93-checks)), `p` its padding
to the block boundary (shorter at a truncated end of file), `N1` and `N2` a
table's `NAXIS1` and `NAXIS2`, `g = THEAP − N1 × N2` and `H` its heap's
length ([§4](#4-tables)):

| path | kind | type | extent: what |
|---|---|---|---|
| `""` | struct | | the root: the whole file, `(0, size)` |
| `hdus/<i>` | struct | | HDU `i`: from its first header byte to the end of its padding |
| `hdus/<i>/header` | value | `{card:ascii[80]}[36k]` | the header's blocks, the `END` card and the blank padding cards included |
| `hdus/<i>/data` | data | | an image's `D` bytes, when `D > 0`: its form's geometry is `{"data_type": T, "shape": S}` ([§3.1](#31-the-array)), its codec `{"endian": "big"}`, its recipe `[["src", 0, null]]` |
| `hdus/<i>/rows` | data | | a table's `N1 × N2` bytes, when not 0: geometry `{"rows": N2, "row_bytes": N1}`, recipe `[["src", 0, null]]` |
| `hdus/<i>/gap` | value | `bytes[g]` | the `g` bytes between the rows and the heap, when `g > 0` |
| `hdus/<i>/heap` | data | | a table's heap, when `H > 0`: geometry `{"bytes": H}`, recipe `[["src", 0, null]]` |
| `hdus/<i>/table` | value | record `[N2]` | a tile-compressed image's main table, one record per tile typed from its columns in row order (`COMPRESSED_DATA:{n:>i4,o:>i4}` for a `P` descriptor, `>i8` for `Q`; `ZSCALE:>f8`, `ZZERO:>f8`, `ZBLANK:>i4`), held by the parser |
| `hdus/<i>/heap/<j>` | data | | chunk `j` of a tile-compressed image ([§3.3](#33-tile-compressed-images)): from its first present tile's start to its last's end, geometry `{"tiles": [first, N]}`, recipe `[["src", 0, null]]` when the tiles are adjacent, else one `["src", o, n]` per tile; a chunk without a tile has none |
| `hdus/<i>/pad` | value | `bytes[p]` | the data's padding, when `p > 0` |
| `gaps/<offset>` | gap | | bytes after the last HDU (the standard's special records); heap bytes no tile uses |

### 6.2 Equivalence

Hierarchies of one file are compared entry for entry, `refkv_source` included
([conventions §11.4](../conventions.md#114-equivalence)).

## 7. Example

The fixture `fixtures/fits/fits_mef_sci_err_dq.fits` has an HST-shaped
layout: an empty primary, then `SCI`, `ERR` and `DQ` for each of two chips
(`EXTVER` 1 and 2), each a `6 × 5` image, float32 but for `DQ` (int16),
seven HDUs of one header block and one data block each. Its hierarchy is the
root group, whose `refkv_zarrified.fits.hdus` begins

```json
[{"index": 0, "type": "primary", "kind": "empty", "name": null, "version": null, "bitpix": 8, "naxis": [],
  "pcount": 0, "gcount": 1, "offset": 0, "header_bytes": 2880, "data_offset": 2880, "data_bytes": 0},
 {"index": 1, "type": "image", "kind": "image", "name": "SCI", "version": 1, "bitpix": -32, "naxis": [6, 5],
  "pcount": 0, "gcount": 1, "offset": 2880, "header_bytes": 2880, "data_offset": 5760, "data_bytes": 120}, ...]
```

and whose `cards` are the primary's seven; the group `0` (empty, with those
cards again); the arrays `1` to `6`, each of shape `[5, 6]`, dimension names
`["axis2", "axis1"]`, one chunk `[5, 6]`, with `1/c/0/0` the range `(5760,
120)` of source 0 and `3/c/0/0` (`DQ,1`, int16) the range `(17280, 60)`;
and `refkv_source`, 27 elements. The array `1` declares the convention with
`{"index": 1, "cards": [...], "bscale": null, "bzero": null, "blank": null,
"bunit": "ELECTRONS"}`.

The fixture `fits_bintable_heap.fits` holds a binary table `VLA` of 3 rows
of 12 bytes (a `PI()` descriptor column and a `2I` column) and an 8-byte
heap. Its node `1` is a group with the arrays `1/rows` (`[3, 12]`, the chunk
`1/rows/c/0/0` the range `(5760, 36)`) and `1/heap` (`[8]`, the chunk
`1/heap/c/0` the range `(5796, 8)`), and the group's `columns` are
`[{"name": "var", "form": "PI(3)", ...}, {"name": "xyz", "form": "2I", ...}]`.

## 8. What the layout does not express

This section is informative. The layout keeps everything in the file but
gives some of it no Zarr expression:

- **World coordinates.** The WCS cards (`CTYPEn`, `CRPIXn`, `CRVALn`, `CDi_j`
  or `PCi_j` and `CDELTn`, `CUNITn`, alternates `A`–`Z`, `SIP`, `TPV` and
  lookup distortions) are kept as cards, not interpreted: placing a pixel on
  the sky is a library's job (astropy.wcs, wcslib), and the hierarchy has no
  OME-NGFF coordinate metadata.
- **Value scaling.** `BSCALE`, `BZERO` and `BLANK` are recorded, not applied
  ([§3.2](#32-value-scaling)).
- **Table columns.** A table is rows and heap bytes ([§4](#4-tables)); its
  columns are described, not presented as arrays.
- **Checksums.** `CHECKSUM` and `DATASUM` are cards; nothing verifies them.
- **ASDF and JSON trees** (JWST's `ASDF` extension, Rubin's JSON HDU) are
  the bytes of a one-row table.
- **Random groups** and **tile-compressed tables** are rejected; so are
  **tile-compressed images** in this revision.

# Part 2. The profile

## 9. FITS profile

### 9.1 Choosing the profile

The input is a FITS file, which its **signature** ([spec/zarrify.md §1.2](../zarrify.md#12-input), tested at the
precedence of [profiles/fits.json](profiles/fits.json)) chose: bytes 0–8 of `H` are `SIMPLE  =` and byte 29 is `T` (the
fixed-format logical of the standard's first card). A file whose first card
is `SIMPLE = F` (a non-conforming file), a file whose first card is
`XTENSION` (an extension written alone), and a gzip (`1F 8B`) or bzip2
(`BZh`) compressed file (`.fits.gz`, `.fits.bz2`), into which no byte range
reaches, match no profile's signature and are rejected.

### 9.2 Reading

The profile reads headers and the main tables of tile-compressed images,
never data. For each HDU in turn, from its first byte: a window of at most
2^16 bytes, extended by such windows until the header's `END` card is in
it; the bytes of the window past the header are the next HDU's first bytes
when they reach them. A header MUST end within 2^24 bytes of its first
byte, else the input is rejected ([§9.3](#93-checks)). A tile-compressed
image's main table (`NAXIS1 × NAXIS2` bytes at its data offset, at most 2^26
bytes) is read in one range, requested with the next HDU's window when the
current window does not reach it.

### 9.3 Checks

The input is rejected unless all of these hold, checked in this order, HDU by
HDU, every check applying whatever the other cards hold. A rejection names
the HDU by its number.

- **File:** the file's size is at least 2880.
- **Header:** every card scanned, up to and including `END`, is 80 bytes of
  printable ASCII (bytes 32–126); an `END` card is found within the lesser
  of the file's remaining bytes and 2^24.
- **Mandatory cards**, at these positions, each with the keyword the
  standard requires there and an integer value where one is required
  (`'sixteen'` as a `BITPIX` is rejected): card 0 `SIMPLE` (the primary) or
  `XTENSION`; 1 `BITPIX`, in {8, 16, 32, 64, −32, −64}; 2 `NAXIS`, in
  0 … 999; 3 to `2 + NAXIS`: `NAXIS1` … `NAXISn`, each at least 0; for an
  extension, then `PCOUNT` (at least 0) and `GCOUNT` (at least 1). An
  extension's `XTENSION` is `IMAGE`, `TABLE` or `BINTABLE` (trailing spaces
  trimmed); any other is rejected and named. A card `GROUPS = T` (random
  groups) is rejected.
- **Data size:** `D = |BITPIX| / 8 × GCOUNT × (PCOUNT + NAXIS1 × … ×
  NAXISn)` computed exactly (0 when `NAXIS = 0`, with `PCOUNT = 0` and
  `GCOUNT = 1` for the primary) is at most 2^53 − 1, and `data_offset + D`
  is at most the file's size, where `data_offset` is the HDU's first byte
  plus its header's blocks. The data's padding MAY be short (a truncated
  final block); the next HDU, if any, starts at the next block boundary.
- **Tables** (`TABLE` and `BINTABLE`): after `GCOUNT`, the card `TFIELDS`,
  an integer in 0 … 999; `BITPIX` is 8 and `NAXIS` is 2 (named in the
  rejection with the extension's type); a card `TFORMn` exists for each `n`
  in `1 … TFIELDS`; `THEAP` (an integer when present; `NAXIS1 × NAXIS2`
  when absent) is at least `NAXIS1 × NAXIS2` and at most `NAXIS1 × NAXIS2 +
  PCOUNT`; a card `ZTABLE = T` (a tile-compressed table) is rejected.
- **Tile-compressed images** (`BINTABLE` with `ZIMAGE = T`), in this order:
  the table is at most 2^26 bytes; `ZCMPTYPE` is `RICE_1`, `RICE_ONE`,
  `GZIP_1`, `GZIP_2` or `NOCOMPRESS` (`HCOMPRESS_1` and `PLIO_1` are
  rejected and named; any other is rejected); `ZBITPIX` is a `BITPIX` value;
  `ZNAXIS` in 1 … 999; each `ZNAXISn` at least 0 and `ZTILEn` (defaulted)
  in `1 … ZNAXISn` (1 when `ZNAXISn` is 0); for `RICE_1`, `BLOCKSIZE` is 16
  or 32 and `BYTEPIX` 1, 2 or 4; `ZQUANTIZ` (when present) is `NO_DITHER`,
  `SUBTRACTIVE_DITHER_1` or `SUBTRACTIVE_DITHER_2`, and a dither has
  `ZDITHER0` in 1 … 10000; every column is `COMPRESSED_DATA` (`1PB` or
  `1QB`), `GZIP_COMPRESSED_DATA` (`1PB` or `1QB`), `ZSCALE` (`1D`), `ZZERO`
  (`1D`) or `ZBLANK` (`1J`) with that `TFORM` (`UNCOMPRESSED_DATA` and
  `NULL_PIXEL_MASK` are rejected and named; any other column is rejected);
  `COMPRESSED_DATA` is a column, and `ZSCALE` and `ZZERO` both or neither;
  the row count is the tile count. Then, from the table: no tile has both
  descriptors non-null; each tile's stream lies within the heap; the
  present tiles are in storage order in the heap (a tile starting before
  the previous present tile ends is rejected). Every integer card named
  here is an integer.
- **Count:** at most 65536 HDUs.

After an HDU, if the next block boundary is within the file and the file
has at least 80 bytes from it that begin `XTENSION=`, they start the next
HDU; otherwise the remaining bytes are special records
([§6.1](#61-elements)), described and never read, and the walk ends. No
other card is checked: the values the layout uses change the output only as
the convention says, and never reject the input.

### 9.4 Chunks

An image's data is `D` contiguous bytes at `data_offset`, `NAXIS1` varying
fastest: the array `V` of [the convention §3.1](#31-the-array), row-major
at that offset. Every chunk of the array is present. The chunk with coords
`c`, at `<i>/c/<coords>`, is the chunk of `V` with those coords, referenced
as [conventions §7](../conventions.md#7-source-values-as-arrays) references
a chunk of contiguous values with the chunk limit `2^20`: one range of
source 0, followed for an edge chunk by its zero padding. Where
[conventions §7](../conventions.md#7-source-values-as-arrays) makes that padding a literal range, it is instead the range
`(1, 0, p)` of the output's **zero data source**
([spec/zarrify.md §1.2](../zarrify.md#12-input)), `p` the padding's
bytes. Where [conventions §7](../conventions.md#7-source-values-as-arrays) copies the chunk (when its reference, with the
padding as a literal range, would not fit in a payload), the chunk is
copied, with its padding. A table's `rows` are cut the same way, with an
element of one byte and rows as the array's rows; its `heap` as bytes,
the last chunk's padding a range of the zero data source too. The zero data
source holds as many zero bytes as the most padding any reference of the
output holds, over all its arrays.

A tile-compressed image's chunk ([the convention §3.3](#33-tile-compressed-images))
is referenced as its prefix, a literal range, followed by its tiles'
streams: one range of source 0 from the first present tile's start to the
last's end when every present tile starts where the one before it ends
(as `fpack` and astropy write them), else one range per present tile, in
storage order. The prefix is built from the main table's rows. The ranges
lie within the file (by the data size check). The output's source table
is source 0, followed by the zero data source when a reference is padded;
it has no other data source. `refkv_source` is the IR
mirror ([the convention §6](#6-the-ir-mirror)). The archive has no other
entries than the root's and the nodes' `zarr.json`, the arrays' chunks and
the mirror's.

### 9.5 Summary

This section is informative. Both implementations print a one-line JSON
summary: `hdus` (the count), `kinds` (the count of each kind), `arrays`
(each array's `path`, `shape`, `dataType` and `chunkShape`), `chunks` (the
arrays' chunks in all), `cards` (the headers' cards in all) and `tables`
(the table HDUs).
