# Implementation plan: the HDF4 profile

Status: plan, 2026-11-16. Nothing implemented. This note follows
[docs/adding-a-format.md](../../docs/adding-a-format.md): a research summary, the convention, the
profile, then the modules, tests, fixtures and corpus. Claims marked **[inspected]** were checked
against the bytes of real files with the scripts described in [§8](#8-corpus); the rest come from
the HDF4 library's source and documentation and are to be checked during implementation.

## 1. Research summary

### 1.1 The format in one page

An HDF4 file is a flat set of **data elements**, each named by a (tag, ref) pair, with a linked
list of **data descriptor blocks** that give each element's offset and length **[inspected]**:

- 4 bytes of magic, `0e 03 13 01`, then descriptor blocks. A block is `ndd` (int16), the offset of
  the next block (uint32, 0 for the last), then `ndd` descriptors of 12 bytes: tag (uint16), ref
  (uint16), offset (uint32), length (uint32). Everything is big-endian. Tag 1 (`DFTAG_NULL`) is an
  empty slot. Files of the corpus have 1 to 146 descriptor blocks.
- Offsets are 32-bit, so a file is at most 4 GiB.
- A tag with bit `0x4000` set is a **special element**: its bytes are a header that says where the
  data really is ([§1.4](#14-findings-that-shaped-the-design)).

Objects are groups of elements, built by several interfaces over that flat layer:

| interface | object | elements |
|---|---|---|
| SD (multifile, netCDF-like) | a scientific dataset (SDS): an n-dimensional array | an NDG (720) listing the data (SD, 702), its dimension record (SDD, 701), its number type (NT, 106), and older DFSD tags; a Vgroup of class `Var0.0` with its attributes and dimensions |
| SD | a dimension | a Vgroup of class `Dim0.0` (or `UDim0.0` for the unlimited one), with a Vdata of class `DimVal0.0` holding its scale's values, or `DimVal0.1` when it has none |
| SD | an attribute | a one-record Vdata of class `Attr0.0` with one field named `VALUES` |
| Vdata | a table of records | a header (VH, 1962) giving fields, types, orders and interlace, and the records (VS, 1963) |
| Vgroup | a group | a VG element (1965) listing (tag, ref) members, with a name and a class |
| GR, and the older DFR8 and DF24 | raster images, palettes | RI (302), RIG (306), LUT (301), Vgroups of class `RIG0.0` and `RI0.0`, attributes as Vdatas of class `RIATTR0.0C` |
| AN | annotations | file and object labels and descriptions (FID 100, FD 101, DIL 104, DIA 105) |
| (none) | version | VERSION (30): three uint32 and a text |

The SD interface writes the file's own Vgroup of class `CDF0.0` and the per-dataset Vgroups above,
which are bookkeeping, not a hierarchy the writer chose. A writer's own Vgroups (MODIS's HDF-EOS
`GRID` Vgroups, SeaWiFS's `Per Scan Data`) are the hierarchy **[inspected]**. A dataset may also be
in no Vgroup at all: CALIPSO keeps all of its datasets that way (bug 5 of
[the kerchunk HDF4 reader's list](https://hackmd.io/@maxrjones/ByZo8kY0Gx)).

**Number types.** The NT element is 4 bytes: version, type, width in bits, class. Types: 3 and 4
`uchar8` and `char8`, 5 and 6 `float32` and `float64`, 20 to 27 the 8- to 64-bit integers. The
class byte gives the byte order (1 big-endian, 4 little-endian, 2 VAX); a file written with a
"native" or "little-endian" type has it there. Every number type in the corpus is class 1,
big-endian **[inspected]**, including the library's `swf32`/`swi16` files, so the little-endian and
VAX cases have no file yet ([§8](#8-corpus)).

### 1.2 How archives use it

HDF4 is NASA's archive format of the EOS era. MODIS, SeaWiFS, AIRS, CALIPSO, CERES, MISR, TRMM
and AMSR-E products are HDF4, many as **HDF-EOS2**: HDF4 plus ODL text attributes
(`StructMetadata.0`, `CoreMetadata.0`, `ArchiveMetadata.0`) that define swaths and grids. The
StructMetadata gives a grid's projection, its corner coordinates and its dimension names, which
place a MODIS tile on Earth **[inspected]**. NASA's Hyrax serves HDF4 through DMR++ sidecars,
which list each variable's byte ranges, and VirtualiZarr and kerchunk each have an experimental
HDF4 reader.

### 1.3 Single file or collection

One input is one file. External elements (special code 2) name another file; the library's
`Tables.hdf` has a Vdata whose records are in `Tables_External_File` **[inspected]**. Such a file
is read as a store ([spec/zarrify.md §1.8](../../spec/zarrify.md#18-files-read-as-stores)), on
demand, as HDF5's external files are.

### 1.4 Findings that shaped the design

From 70 files: the 7 of GDAL's sample set and the 63 test files of the HDF4 library
([§8](#8-corpus)).

1. **Every dataset's values are byte ranges plus zlib** **[inspected]**. All 103 SDS of the GDAL
   set, and every SDS of the library set that has data and an available codec, decode from the
   ranges the descriptors give to the values pyhdf reads, exactly.
2. **Special elements decide the layout** **[inspected]**:
   - **plain** (no special bit): the data element is the array, contiguous, in C order;
   - **compressed** (code 3): a header (version, uncompressed length, the ref of a `COMPRESSED`
     (40) element, model type, compression type, and the method's parameters) and the compressed
     stream in that element. The whole array is one stream;
   - **chunked** (code 5): a header (chunk sizes per axis, the fill value, and the ref of a chunk
     table) and a Vdata of rows `(origin[rank] int32, chk_tag uint16, chk_ref uint16)`. Each chunk
     is a `CHUNK` (61) element, plain or itself compressed. MOD09A1 has 75 chunks of 32 × 2400 per
     dataset; MOD09Q1G_EVI one chunk of 4800 × 4800 (46 MB inflated);
   - **linked blocks** (code 1): the element is split into blocks, listed by link tables of
     `blocks per table` refs and a next-table ref. The header's length is the element's;
     the blocks' total may be larger (allocated space). A block may be empty: two MODIS chunk
     tables have a first block of 0 bytes;
   - **external** (code 2): an offset and length in a named file.
   Kinds compose: a chunk table is a Vdata whose records may be linked blocks (all 19 MODIS chunk
   tables are), and AIRS Level 3 compresses data stored as linked blocks (bug 6 of the list above).
   A value whose bytes lie in several blocks is a `Concat` of ranges, which a reference already is.
3. **Compression methods** **[inspected]**: deflate (4) in MODIS and the library; skipping Huffman
   (3) and SZIP (5) in the library's `sds_compressed.hdf`; RLE (1), NBIT (2) and JPEG (7, GR
   images only) by the library's documentation. Deflate is zlib (`numcodecs.zlib`); SZIP has
   `imagecodecs.szip`, to check against HDF4's framing of it. RLE (HDF4's own, not PackBits), NBIT
   and skipping Huffman have no codec. The pyhdf wheel is built without SZIP (`-DNOSZIP`), so it
   cannot be the oracle for SZIP ([§7](#7-verifier)).
4. **An unlimited dimension's length is not in its SDD** **[inspected]**. The library's `test.hdf`
   has `Byte`, whose SDD says 7 × 7 and which pyhdf reads as 9 × 7: the first dimension is unlimited
   and its current length is the record count, from the `UDim0.0` Vgroup's dimension or the data's
   length. The slots of a record never written read as the fill value (−127 there).
5. **A dataset with no data element is all fill value** **[inspected]**: created and never written
   (the library's `fakeDim*` scales in `ntcheck.hdf`, the examples' `Test SD`). Such datasets are
   reached through their `Var0.0` Vgroup, not an NDG.
6. **Text is one string per value, in one range** **[inspected]**. Every `char8` field in the corpus,
   in attributes (`Attr0.0`, `RIATTR0.0C`) and in tables, holds one text of ASCII, padded with NULs
   to its order, padded with spaces, or filling the field exactly. None holds two texts, and none
   lies in linked blocks. HDF4 does not record which padding a field uses.
7. **The big texts are HDF-EOS metadata** **[inspected]**: `StructMetadata.0` is a 32 000-byte
   buffer padded with NULs, `CoreMetadata.0` reaches 37 285 bytes, `ArchiveMetadata.0` 9393. A
   SeaWiFS `Input Parameters` attribute is 2518 bytes.
8. **Tables are records, fully interlaced** **[inspected]**. Every Vdata in the corpus has interlace
   0: records contiguous, fields packed in order, big-endian, no padding. The MODIS chlorophyll
   file's `Calibration` table is one 804-byte record: three `int32`, six `float32`, and three
   `char8[256]` (`name`, `units`, `bandname`, each NUL-padded). This is the layout of a FITS binary
   table's rows, which [fits §4.1](../../spec/zarrify/fits.md#41-rows-as-a-struct) presents as a
   `struct` that references the file's bytes.
9. **No SDS in the corpus is a character array.** HDF4 allows one (an SDS of `char8`, by netCDF's
   convention the last axis being the string's length), and some products have them; the corpus
   has none, so fixtures must make some ([§6](#6-fixtures)).
10. **The kerchunk and VirtualiZarr readers' bugs are this design's checklist**
    ([the list](https://hackmd.io/@maxrjones/ByZo8kY0Gx)): floats read little-endian (here, the NT
    class byte decides); plain datasets with no location; every compressed chunk taken for deflate;
    the version text read 2 bytes long; datasets outside Vgroups missed; linked compressed data not
    referenced; every variable marked zlib; dimension names for rank 3 or more; nested Vgroups
    refused. Each becomes a fixture or a corpus check.

## 2. The convention (Part 1 of `spec/zarrify/hdf4.md`)

### 2.1 Hierarchy

- The root group is the file. Each Vgroup a writer made (any class but the interfaces' own:
  `CDF0.0`, `Var0.0`, `Dim0.0`, `UDim0.0`, `RIG0.0`, `RI0.0`, `_HDF_CHK_TBL_0`, `Attr0.0`) is a group,
  named by its Vgroup name. A Vgroup reached twice is presented once, at its first path in a walk
  of the Vgroups in ref order, with a link in source metadata, as HDF5's hard links are
  ([hdf5 §2.3](../../spec/zarrify/hdf5.md#23-links)).
- An object in no Vgroup is a member of the root, after the root's Vgroups.
- Names are made unique and valid as HDF5's are ([hdf5 §2.2](../../spec/zarrify/hdf5.md#22-paths)).

### 2.2 Datasets

- **Shape:** the SDD's sizes, but for an unlimited dimension, whose size is its record count
  ([§1.4](#14-findings-that-shaped-the-design) 4).
- **Data type:** by the NT element, its byte order from the class byte: `int8` … `uint64`,
  `float32`, `float64`, `bytes` with that `endian`. `uchar8` is `uint8`.
- **A `char8` SDS** — decision D1 ([§9](#9-decisions)): `null_terminated_bytes` of the last axis's
  length, the other axes the array's, as FITS presents a text column; the last axis's name in
  source metadata.
- **Chunks and codecs** by the special element ([§1.4](#14-findings-that-shaped-the-design) 2):
  a plain dataset as contiguous values cut with the limit 2^20 bytes
  ([conventions §7](../../spec/conventions.md#7-source-values-as-arrays)); a compressed one as one
  chunk with its codec; a chunked one with its chunk sizes, each chunk a reference to its
  element (with the chunk's codec when compressed). Codecs: deflate `numcodecs.zlib`; SZIP
  `imagecodecs_szip` with its parameters; RLE, NBIT and skipping Huffman not presented in version
  0, with the reason (decision D3).
- **Fill value:** the chunked header's fill value, else the `_FillValue` attribute, else 0. An
  unwritten dataset and unwritten chunks read as it.
- **Dimension names:** each SDS dimension's name (`fakeDim<n>` when the writer gave none, as the
  library names it). A dimension with a scale (`DimVal0.0`) is a coordinate array of its name in the
  dataset's group, as netCDF-3 presents a coordinate variable, so xarray reads it as one.
- **Attributes:** the dataset's `Attr0.0` Vdatas, in their order, as JSON: a `char8` value as its
  text to the first NUL, keeping trailing spaces (HDF4 records no padding); one number as a number;
  several as a list; integers exact.

### 2.3 Tables (Vdatas)

A Vdata of a writer's class (not the interfaces' own) is an array of one `struct` element per
record, as [fits §4.1](../../spec/zarrify/fits.md#41-rows-as-a-struct) presents a binary table's
rows: fields in order, big-endian, a `char8` field of order `n` a `null_terminated_bytes` of `n`, a
numeric field of order `n > 1` a nested `struct` of fields `0` … `n − 1`. Chunks are whole records,
referenced, nothing copied; a table in linked blocks has `Concat` chunks. A Vdata with interlace 1
(each field's values contiguous, the corpus has none) is one array per field. The `Calibration`
table of [§1.4](#14-findings-that-shaped-the-design) 8 is the model fixture.

### 2.4 Text attributes and HDF-EOS

File attributes (the `CDF0.0` Vgroup's `Attr0.0` Vdatas) are the root's attributes, as [§2.2](#22-datasets) writes
a dataset's. The HDF-EOS texts are kept whole, verbatim, as strings: nothing of the science is in
the values, but the grid's geolocation is in the text. Turning `StructMetadata.0` into coordinates
(projection, `XDim`/`YDim` from the corner points) is an HDF-EOS2 layout, decision D2.

### 2.5 Raster images, palettes, annotations

GR images (8-bit and 24-bit, interlaced by pixel, line or plane, plain or compressed with RLE,
JPEG or deflate) are arrays with their palettes; annotations are source metadata text. Both are
in the library's test files and in old products, and are version 0 work only if decision D3 says so.

### 2.6 Source metadata and the IR mirror

The descriptors, the NT, SDD and VH headers, the Vgroup records, the special headers and link
tables, and every attribute's bytes are the mirror's held elements; the data elements are
referenced. A field's padding, an attribute's type and order, and a Vgroup's class go in the source
metadata, so the file can be rebuilt ([conventions §8](../../spec/conventions.md#8-the-ir-mirror)).

### 2.7 Stored arrays

Each SDS (written or not), each chunk table's dataset, each GR image and each presented Vdata is a
stored array ([spec/stored-array.md](../../spec/stored-array.md)): keyed by its NDG, RI or VH
element; declarations the NT and SDD (and the special header); a grid from the chunk sizes or the
whole array; the chain from the special elements (`zlib`, `szip`, or `source` with HDF4's
compression id for the others). The paragraph in [stored-array §7](../../spec/stored-array.md#7-the-formats) comes with the describe-only PR
([§4](#4-order-of-work)), checked against pyhdf.

## 3. The profile (Part 2)

- **Choosing the profile:** the magic `0e 03 13 01` at offset 0.
- **Reading:** the descriptor blocks first (a chain of up to 146 small reads in the corpus, each
  block 6 + 12 × `ndd` bytes; batch the reads of one block's successors where the offsets are
  known), then every header element (NT, SDD, NDG, VH, VG, special headers, link tables), then the
  attribute Vdatas' records. Data elements are never read, but the chunk tables and the bytes of a
  Vdata that is presented as text.
- **Checks, in this order:** the magic; a descriptor block past the end of the file or a cycle in
  the chain; an element past the end; a special header of an unknown code; a chunk table whose rows
  do not tile the dataset; link tables that loop; a number type of an unknown type or class.
- **Bounds:** at most 2^20 descriptors, 2^16 descriptor blocks, 2^20 chunks per dataset, link tables
  followed at most once each.
- **Summary line:** datasets, tables, groups, and the datasets not presented with their reasons.

## 4. Order of work

Each step is a PR to main.

1. **Plain and deflate SDS, attributes, Vgroups.** Covers all seven GDAL files: plain, compressed,
   chunked and linked-block datasets, unlimited dimensions, unwritten datasets, the hierarchy,
   text attributes kept verbatim, dimension scales as coordinates. Verifier against pyhdf.
2. **Tables** ([§2.3](#23-tables-vdatas)), with `char8` fields as `null_terminated_bytes`, and `char8` SDS
   ([§2.2](#22-datasets), [D1](#9-decisions)).
3. **SZIP**, once an oracle with SZIP reads the library's file ([§7](#7-verifier)).
4. **Stored arrays** ([§2.7](#27-stored-arrays)), describe-only.
5. **External elements** ([§1.3](#13-single-file-or-collection)), read as a store on demand.
6. By decision D3: GR images and palettes; RLE, NBIT and skipping Huffman codecs; annotations.
7. By decision D2: the HDF-EOS2 layout.

## 5. Tests

As the house style says: one test over the valid fixtures comparing every value with pyhdf, and one
test per rejection with its message. Test the referenced chunks' decoded bytes, not only the
parser's facts. Each bug of the kerchunk list ([§1.4](#14-findings-that-shaped-the-design), 10) has a fixture that would have caught it.

## 6. Fixtures

Written by a generator with pyhdf, byte-identical on macOS and Linux (zlib's output is the
library's; check that pyhdf's bundled zlib gives the same bytes on both, or keep deflate fixtures
as committed binaries from the library's test set):

- one of each special kind: plain, compressed (deflate), chunked with plain and compressed chunks,
  linked blocks with an empty first block, a chunk table in linked blocks;
- an unlimited dimension with records written past the SDD's size; an unwritten dataset; an
  unwritten chunk;
- `char8` SDS of rank 1, 2 and 3; a table with `char8` fields padded with NULs, with spaces, and
  exactly; text attributes of each padding;
- a dataset in no Vgroup, nested Vgroups, a Vgroup reached twice;
- little-endian and native number types (pyhdf's `SDC.*` types with the `DFNT_LITEND` flag);
- a rank-3 dataset with named dimensions and a scale;
- rejects: a truncated descriptor block, a descriptor past the end, a looping link table, an unknown
  special code.

Plus the library's 63 test files where their license allows, which cover GR images, annotations,
DFSD-era datasets and SZIP and skipping-Huffman data.

## 7. Verifier

pyhdf, the HDF4 library's Python binding, is the oracle for values, shapes, dimension names and
attributes. It lacks SZIP in its wheel, so SZIP is checked with GDAL built with libaec, or with
HDF4's `hdp`/`hrepack` built with it. NASA's DMR++ sidecars, where a product has them, are a second
check of byte ranges: Hyrax's are what the list's author compared against.

## 8. Corpus

Inspected so far:

- **GDAL's sample set** (`https://download.osgeo.org/gdal/data/hdf4/`, 7 files): SeaWiFS and
  MODIS-Aqua Level 2 SST, a MODIS Level 3 chlorophyll grid, two MODIS land grids (chunked,
  deflate), an NCEP reanalysis, a SeaWiFS Level 1A extract.
- **The HDF4 library's test files** (63 files at `HDFGroup/hdf4` master): GR images, palettes,
  Vdatas, Vgroups, compressed, empty and long-named datasets, external elements, DFSD-era files.

The survey scripts (`survey.py` with pyhdf, `layout.py` over the descriptors, `check.py` comparing
the two, `strings.py` on text) are to be committed under `design/experiments/hdf4/` with the
first PR. To add, by range reads where the archive allows (most NASA DAACs need an Earthdata
login): AIRS Level 2 and 3 (plain datasets; compressed linked blocks), CALIPSO Level 1B and 2
(datasets outside Vgroups; the version text), MOD11_L2 (floats), a MISR or CERES product (SZIP,
`char8` SDS), and any product with little-endian number types.

## 9. Decisions

- **D1. A `char8` SDS.** Recommended: `null_terminated_bytes` of the last axis's length, as FITS
  presents a text column, referencing the file's bytes and read by numpy as `S<n>`. The alternative
  is HDF5's choice for a fixed-length string, `uint8` with a trailing axis named `string<n>`
  ([hdf5 §3](../../spec/zarrify/hdf5.md#3-datasets)), which xarray reads as bytes, not text. Either
  way the two profiles should agree; if D1 takes the first, HDF5's fixed-length strings could
  follow it in a change of their own.
- **D2. HDF-EOS2.** Version 0 keeps the ODL texts verbatim. A later layout (`hdf4-eos`) would parse
  `StructMetadata.0` into grid coordinates and swath geolocation links, which is what users of MODIS
  grids need and what GDAL does. Its oracle would be GDAL's geotransform.
- **D3. Version 0's scope.** Recommended: SD, Vdata, Vgroup, attributes, deflate and SZIP, external
  elements. Not presented, with a reason: GR images, RLE, NBIT, skipping Huffman, JPEG. Every NASA
  file in the corpus is covered by that; the rest is the library's older interfaces.
