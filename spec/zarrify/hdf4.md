# HDF4

The Zarr layout of an HDF4 file, HDF-EOS2 files among them: a group per Vgroup the writer made
and an array per scientific dataset of a numeric type, with the file's and the datasets'
attributes, so that xarray reads a zarrified MODIS or SeaWiFS product as it reads the netCDF
variables the SD interface models. What all of refkv's conventions share is in
[spec/conventions.md](../conventions.md), cited here as "conventions §n". How refkv produces this
layout as a reference store, which files it reads and which inputs it rejects, is the HDF4
profile, [Part 2](#part-2-the-profile) below. The format is the HDF Group's HDF4, as its library
writes and reads it (the `hdf4` repository: `hdf/src` for the file layer and the Vdata and Vgroup
interfaces, `mfhdf/src` for the SD interface), cited here by its source files. The design note is
[design/plans/hdf4.md](../../design/plans/hdf4.md).

Convention version: 0 (until release, [conventions §1](../conventions.md#1-conventions)) · UUID: `af66c29e-fc1a-44a4-bb71-1055ad5fdb0a` ·
Schema: [schema.json](hdf4/schema.json)

Changes: [changes/hdf4/](changes/hdf4/), one file per change.

Added 2026-11-17.

This document has two parts ([conventions §11.1](../conventions.md#111-parts)).

This version covers the first two steps of the plan's order of work
([design/plans/hdf4.md](../../design/plans/hdf4.md)): the SD interface's datasets of numbers and of
text, their attributes, the writer's Vdata tables, and the writer's Vgroups. Raster images, external
elements and the codecs other than deflate are listed with a reason
([§8.4](#84-what-is-not-presented)), and their bytes stay in the IR mirror.

Like HDF5's, HDF4's datasets are not images: the hierarchy holds no OME-NGFF metadata.

# Part 1. The convention

## Layout

| prefix | type | description |
|---|---|---|
| `zarr.json` | group | the file ([§2](#2-the-hierarchy)): its attributes and the declaration with the root's source metadata ([§8.1](#81-groups)) |
| `<path>/zarr.json` | group | a Vgroup the writer made, at its canonical path ([§2.2](#22-paths)) |
| `<path>/zarr.json` | array | a scientific dataset at its canonical path ([§3](#3-datasets)), with its source metadata ([§8.2](#82-datasets)) |
| `<path>/zarr.json` | array or group | a table the writer made, at its canonical path ([§4](#4-tables)): an array of its records, or a group of an array per field |
| `<path>/c/<coords>`, `<path>/c` | chunk | a chunk of a dataset's or table's array ([§3.2](#32-chunks), [§4](#4-tables)): `<path>/c` for a dataset of text of rank 1 |
| `refkv_source/**` | subtree | the IR mirror ([§8.3](#83-the-ir-mirror)) |

| placeholder | value |
|---|---|
| `<path>` | the object's canonical path ([§2.2](#22-paths)), its names joined by `/` |
| `<coords>` | the chunk's grid coordinates, in decimal, one key segment each |

## 1. Declaration

The root, every dataset's array and every group declare the convention
([conventions §11.2](../conventions.md#112-declaration)), and so does `refkv_source`. Its CMO is:

```json
{
  "uuid": "af66c29e-fc1a-44a4-bb71-1055ad5fdb0a",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/zarrify/hdf4/schema.json",
  "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md",
  "name": "refkv_zarrified",
  "description": "The Zarr layout of a HDF4 source zarrified by refkv, and the source's metadata"
}
```

## 2. The hierarchy

An HDF4 file is a flat set of **elements**, each named by a tag and a reference number (its
**ref**) and described by a **data descriptor** ([§9.2](#92-reading)). Its interfaces build objects
of them: a **Vgroup** (a VG element, tag 1965) lists members by tag and ref and has a name and a
class; a **Vdata** (a VH header, tag 1962, and its records, a VS element, tag 1963) is a table of
records; the **SD interface** (the netCDF-like multifile interface, `mfhdf/src`) keeps a dataset as a
Vgroup of class `Var0.0`, whose members are its dimensions (Vgroups of class `Dim0.0`, or `UDim0.0`
for the unlimited one), its attributes (Vdatas of class `Attr0.0`), its number type (NT, tag 106),
its dimension record (SDD, tag 701), its data (SD, tag 702) and its NDG (tag 720), all members of
the file's Vgroup of class `CDF0.0`, whose own `Attr0.0` members are the file's attributes.

The hierarchy's root is the file. Its attributes are the file attributes ([§7](#7-attributes)).

### 2.1 Groups

A Vgroup the writer made is a group: any Vgroup whose class is not one of the interfaces' own,
`Var0.0`, `Dim0.0`, `UDim0.0`, `CDF0.0`, `RIG0.0` and `RI0.0` (the library's `INTERNAL_HDF_VGS`,
`hdf/src/hlimits.h`). MODIS's HDF-EOS `GRID` Vgroups and SeaWiFS's `Per Scan Data` ones are
groups; the SD interface's bookkeeping is not.

A group's members, in the Vgroup's member order:

- a member Vgroup that is a group: a subgroup;
- a dataset ([§3](#3-datasets)), named by the member `(720, r)`, `r` its NDG's ref, or by the member
  `(1965, r)`, `r` its `Var0.0` Vgroup's ref;
- a Vdata of class `Attr0.0`: an attribute of the group ([§7](#7-attributes)), as are the Vgroup's
  own attributes, which a version 4 Vgroup lists after its class;
- a Vdata of a writer's class: a table ([§4](#4-tables));
- anything else is not presented ([§8.4](#84-what-is-not-presented)) (`object`): a Vdata of an
  interface's class, a raster image, a palette, an annotation, an interface's Vgroup.

The root's members are, in this order:

1. the **top Vgroups**: the groups no group holds, in ascending order of their refs, and then, while
   any group is left that the walk ([§2.2](#22-paths)) did not reach (Vgroups that hold one another
   in a loop, or that only a Vgroup not presented holds), the one of them with the least ref;
2. every dataset the walk did not reach, in the order of the `CDF0.0` Vgroup's members: a dataset
   that no group holds (CALIPSO keeps all of its datasets so), or that only Vgroups not presented
   hold;
3. every table the walk did not reach, in ascending order of their refs.

### 2.2 Paths

The **walk** visits the root's members in order, each group depth first, its members in order: an
object's **canonical path** is the path of the first visit, its names joined by `/` (the root's is
empty). A group or a dataset is presented once, at its canonical path; a later visit is a link of
the group that holds it ([§8.1](#81-groups)). A group is presented at most 64 levels below the root
(reason `depth` below that).

A group's name is its Vgroup's name, and a dataset's its `Var0.0` Vgroup's. An object is presented
only when its name is a **node name**: valid UTF-8, not empty, not `.` or `..`, not starting with
`__` (which Zarr v3 reserves), without `/`, and, at the root, not `refkv_source` (reason
`reserved`); otherwise it is not presented (reason `name`). HDF4 lets two members of a group share
a name; the first in the walk keeps it, and a later one is not presented (reason `duplicate`). The
walk does not enter a Vgroup it does not present, so what that Vgroup holds is the root's, unless
the walk reaches it elsewhere ([§2.1](#21-groups)). A dataset or table not presented is recorded once,
where the walk first reaches it, but for a dataset whose name another member of its group holds
(`duplicate`), which the walk tries again where it reaches it next in another group.

## 3. Datasets

A dataset is a `Var0.0` member of the file's `CDF0.0` Vgroup, the first in ascending ref order when
there are more, as the SD interface reads them (`hdf_read_vars`, `mfhdf/src/cdf.c`). A file with no
`CDF0.0` Vgroup was written by the older DFSD interface, whose datasets this version does not
present (reason `dfsd`). A dataset is an array when its number type, dimensions and storage are
among those below; otherwise it is not presented, with the reason [§8.4](#84-what-is-not-presented)
gives.

- **Shape:** the sizes of its SDD, in order, but for an unlimited dimension: its first dimension is
  unlimited when the `Var0.0` Vgroup's first dimension Vgroup is of class `UDim0.0`, and its size
  is then the dataset's **record count**, `floor(L / r)`, where `L` is the length of its data
  ([§3.2](#32-chunks); 0 without data) and `r` the bytes of one record, the element size times the
  other sizes (0 records when `r` is 0), as the SD interface counts them. The SDD's size of that
  dimension is not its length. The rank is the number of the `Var0.0` Vgroup's dimension Vgroups,
  which MUST be the SDD's rank, at least 1 (else `dimensions`).
- **Data type** by the number type ([§3.1](#31-data-types)).
- **Fill value**, in this order: the fill value of a chunked dataset's header, when it is one
  element long; else the dataset's `_FillValue` attribute, when it is one value of the dataset's
  data type; else netCDF's default fill for the netCDF type the SD interface maps the number type
  to, as `NC_arrayfill` (`mfhdf/src/array.c`) fills: `uchar8` 0; `int8` −127 and `uint8` 129 (each
  byte `0x81`); `int16` −32767 and `uint16` 32769; `int32` −2147483647 and `uint32` 2147483649;
  `float32` and `float64` 9.9692099683868690 × 10^36, which is 15 × 2^119 exactly in both.
  A dataset with no data, an unwritten chunk and a linked element with no element read as it. This
  departs from [conventions §3](../conventions.md#3-arrays)'s fill value 0, as HDF5's does. An
  integer is the JSON integer of its value; a float is the encoding of [§7](#7-attributes).
- **Dimension names:** the names of the `Var0.0` Vgroup's dimension Vgroups, in order
  ([§5](#5-dimensions-and-coordinates)).
- **Attributes:** [§7](#7-attributes).

### 3.1 Data types

The NT element is 4 bytes: a version, the type, its width in bits, and its class, which gives the
byte order (`hdf/src/hntdefs.h`):

| type (code) | Zarr `data_type` |
|---|---|
| `int8` (20), `uint8` (21), `int16` (22), `uint16` (23), `int32` (24), `uint32` (25) | `int8`, `uint8`, `int16`, `uint16`, `int32`, `uint32` |
| `float32` (5), `float64` (6) | `float32`, `float64` |
| `uchar8` (3) | `uint8` |

A type of more than one byte is big-endian when the class is 0 or 1 (IEEE, Motorola order) and
little-endian when it is 4 (a PC's), and its codecs start with `bytes` of that `endian`; a
one-byte type has `bytes` without one. Any other class (VAX, Cray and other native formats) is
not presented (`number_type`), nor is any other type, `int64` and `uint64` among them, which the
SD interface does not read (`hdf_unmap_type`). A dataset of `char8` (4) is a dataset of text
([§3.3](#33-text)).

### 3.2 Chunks

An element's tag with 0x4000 added is a **special element**: its bytes are a header that says where
the data is (`hdf/src/hfile.h`). The dataset's data is its `(702, r)` element, `r` the ref the
`Var0.0` Vgroup's SD member gives, or the special element `(0x4000 + 702, r)`; with neither, the
dataset has no data. The data's **length** `L` is the element's, or the header's for a special one:

- **plain:** the element is the array, contiguous, in C order;
- **linked blocks** (special code 1): its header gives the length, the block length `B`, the blocks
  of a link table and the first link table's ref. A link table (tag 20) is the next link table's
  ref, 0 for none, then that many block refs. The element's bytes are its blocks' in order, the
  first block as long as its descriptor says (0 bytes in some MODIS chunk tables) and each other `B`
  bytes, to the header's length, as `HLPread` (`hdf/src/hblocks.c`) reads them; a block of ref 0,
  never written, reads as zeros;
- **compressed** (code 3): its header gives the uncompressed length `L`, the ref of the
  compressed stream, a `COMPRESSED` element (tag 40), plain or in linked blocks, the model and the
  method. A length of 0 is a dataset with no data. With the standard model (0), method 4 (deflate)
  is a zlib stream (RFC 1950) and method 0 the data as it is; any other is not presented
  (`compression`);
- **chunked** (code 5): its header gives, for each axis, the dimension's length and the chunk's,
  then the fill value, and the ref of the **chunk table**, a Vdata whose records are each a chunk's
  grid coordinates (an `int32` per axis) and its element's tag and ref. Each chunk is that element:
  plain (a `CHUNK` element, tag 61), or compressed as above. Every chunk, the grid's edge ones
  included, holds a whole chunk;
- **external** (code 2) and the others are not presented (`external`, `special`).

The array's chunks:

- **contiguous** (plain, linked, or compressed with method 0): the first `nbytes` bytes of the data
  (`nbytes` the array's), which MUST be there (else `length`), are cut as contiguous values
  ([conventions §7](../conventions.md#7-source-values-as-arrays)) with the chunk limit 2^20 bytes;
- **compressed** with deflate: one chunk, the whole array, whose uncompressed length MUST be the
  array's `nbytes` (else `length`), its codecs `bytes` and `numcodecs.zlib`
  (`{"name": "numcodecs.zlib", "configuration": {"level": 1}}`);
- **chunked:** the chunk shape is the header's chunk lengths, and each chunk of the table is the
  chunk at its coordinates; a chunk that the table does not list is not written, and reads as the
  fill value. A chunk wholly past the record count of an unlimited axis is not written either. The
  chunks are plain, or all compressed with deflate, the codecs then `bytes` and `numcodecs.zlib`;
  a dataset whose chunks are of another method, or mixed, is not presented (`compression`,
  `chunks`);
- **none** (no data, or a size of 0): no chunk is written, and the chunk shape is the contiguous
  cut's.

A chunk's bytes are ranges of the file, by [§9.4](#94-chunk-references).

### 3.3 Text

A dataset of `char8` is an array of strings, as netCDF reads a `char` variable and as FITS presents a
text column ([fits.md §4.1](fits.md#41-rows-as-a-struct)): its last axis is the strings' length `L`,
1 to 2^20, and the array's shape is the others, its data type
`{"name": "null_terminated_bytes", "configuration": {"length_bytes": L}}`, each element the `L`
bytes as stored, the trailing spaces kept. A dataset of rank 1 is a scalar of one string, its chunk
`<path>/c`. Its storage is read as for a `uint8` dataset of its shape ([§3.2](#32-chunks)), its
chunks the same bytes; a chunked dataset is presented only when its chunks hold whole strings, the
chunk length of the last axis `L` (else `chunks`), its chunk shape the others'. Its codecs are
`bytes` (and `numcodecs.zlib`); its fill value is `""` when the fill byte (the chunked header's,
else the first byte of a `char8` `_FillValue`, else 0) is 0, and otherwise the base64 of `L` of it,
the encoding of a `null_terminated_bytes` value. Its `dimension_names` are the other axes', and the
last axis's name is in its source metadata (`text_axis`).

## 4. Tables

A Vdata of a writer's class (not one of the interfaces' own of [§8.1](#81-groups)) that no
interface's Vgroup holds is a **table**: an array of its records, as FITS presents a binary table's
rows ([fits.md §4.1](fits.md#41-rows-as-a-struct)), named by the Vdata's name, at the place the walk
first reaches it ([§2.2](#22-paths)).

A field of type `t`, order `n` and size `s` (`vunpackvs`; [§9.2](#92-reading)) is:

| field | data type | fill value |
|---|---|---|
| `char8`, `s = n` | `{"name": "null_terminated_bytes", "configuration": {"length_bytes": n}}`, the bytes as stored, trailing spaces kept | `""` |
| a number type of [§3.1](#31-data-types) of `b` bytes, `n = 1`, `s = b` | that type | `0`, or `0.0` for a float |
| the same, `n > 1`, `s = n × b` | `{"name": "struct", "configuration": {"fields": F}}`, `F` the fields `"0"` … `"n − 1"` of that type | the object of those fields' fills |

The type's flag 0x4000 makes it little-endian; any other flag, any other type, and an order of 0 are
not presented (`datatype`, `fields`).

**Interlace 0** (each record its fields in order): the table is an array of shape `[records]`, its
data type a `struct` of its fields in order, each `{"name": its name, "data_type": D}`, its fill value
the object of the fields' fills, its `dimension_names` `["record"]`, and its codecs `bytes`, of the
byte order of its fields of more than one byte, which MUST be one (else `byte_order`), big-endian when
it has none; without a field of more than one byte, `bytes` alone. Its records are contiguous values
([conventions §7](../conventions.md#7-source-values-as-arrays)), cut with the chunk limit 2^20 bytes,
whole records, referenced in the Vdata's records element (VS, plain or in linked blocks), nothing
copied. The fields MUST tile a record: each at the offset the header gives, in order, their sizes
summing to the record's (else `fields`).

**Interlace 1** (each field's values together, in field order): the table is a group of an array per
field, named by the field's name, each of shape `[records]`, of the field's data type, fill value and
byte order, its values the field's `records × s` bytes.

A table is not presented when its header is of version 2 or older (`version`: the older types), two
fields have one name or the fields number more than 4096 leaves (`fields`), a record is more than
2^20 bytes (`record_size`), or its records element holds fewer than `records × size` bytes
(`length`). A table with no records is an array of shape `[0]`, with no chunk.

**Attributes.** The Vdata's own attributes (a version 4 header's of field index −1) are the table's
attributes ([§7](#7-attributes)); a field's are in its source metadata.

## 5. Dimensions and coordinates

A dataset's `dimension_names` are its dimension Vgroups' names, each as text (UTF-8 if valid, else
ISO 8859-1), in order: the names the writer gave, or the SD interface's `fakeDim<n>`. A dimension of
one name is one dimension across the file, as netCDF's are.

A **dimension scale** is, in an SD file, a dataset of its own: a **coordinate variable**, of rank 1
and named as its dimension, which the SD interface marks with a Vdata of class `CoordVar` (or,
before HDF4.2r2, by its name alone, `SDiscoordvar`). It is an array like any dataset, whose
`dimension_names` is its own name, so that xarray reads it as the coordinate of every array in its
group that has that dimension. A `Dim0.0` Vgroup's own Vdata (`DimVal0.0`, `DimVal0.1`) holds no
scale the SD interface reads as one, and is not presented.

## 6. HDF-EOS2

HDF-EOS2 files (MODIS, AIRS, MISR and other EOS products) are HDF4 files whose file attributes
`StructMetadata.0`, `CoreMetadata.0` and `ArchiveMetadata.0` hold ODL text that defines their grids
and swaths. These are presented as every text attribute is ([§7](#7-attributes)): whole, with no
bound on their length, and verbatim. Their grids' and swaths' structure (a grid's projection and
corners, a swath's geolocation) is not turned into coordinates in this version.

## 7. Attributes

An attribute is a Vdata of class `Attr0.0` of one field: its name is the Vdata's name, and its value
the field's values, its order times its record count of them. A node's attributes are, in order:

- the file's: the `CDF0.0` Vgroup's `Attr0.0` members;
- a group's: [§2.1](#21-groups);
- a dataset's: its `Var0.0` Vgroup's `Attr0.0` members;

each a member of the node's attributes, by name, its value as JSON:

| field type | JSON |
|---|---|
| `char8` | its text to its first NUL, trailing spaces kept (HDF4 records no padding), as a text value ([conventions §6](../conventions.md#6-source-metadata-as-json)) |
| an integer type, `uchar8` included | a JSON integer, exact |
| `float32`, `float64` | a **float value**: [hdf5.md §6](hdf5.md#6-attributes)'s encoding, the Zarr v3 fill value of the value in the float data type of its size |

One value is the value; several are an array of them. A field's byte order is big-endian, or
little-endian when its type has the flag 0x4000 (`DFNT_LITEND`). As in HDF5's convention
([hdf5.md §2.1](hdf5.md#21-declaration-and-attributes)), the attributes are the node's own, beside
`refkv_zarrified`, because they are the netCDF attributes xarray reads.

**`_FillValue`.** On a dataset of a float type, an attribute `_FillValue` of one number is the
Base64 of the 8 little-endian bytes of its value as a binary64, the form xarray's Zarr backend
reads, as in [hdf5.md §6](hdf5.md#6-attributes); on an integer dataset it is the integer.

When two attributes of a node have one name, the first is presented. An attribute is not presented
(its reason in the node's source metadata, [§8](#8-source-metadata)) when it is named
`zarr_conventions` or `refkv_zarrified` (`reserved`), when its records are past 2^24 bytes
(`budget`), when its type is another (`datatype`), and when its Vdata is not one field of
interlace 0 whose records the file holds (`attribute`).

## 8. Source metadata

### 8.1 Groups

A group's `hdf4` member:

| member | presence | value |
|---|---|---|
| `ref`, `class` | a Vgroup's | its Vgroup's ref and class (a text value) |
| `version` | the root's, when the file has a VERSION element | `{"major", "minor", "release", "text"}`, the library version that wrote the file |
| `cdf` | the root's | the `CDF0.0` Vgroup's ref, or `null` |
| `attributes` | when it has attributes | each attribute's `{"type", "count"}`, with `"endian": "little"` when its type says so, by name |
| `unpresented_attributes` | when some are not presented | each one's reason, by name |
| `links` | when the walk visits a member again ([§2.2](#22-paths)) | `[{"name", "tag", "ref", "path"}]` in the walk's order: the member's name, its tag and ref as the Vgroup lists it, and its canonical path |
| `unpresented` | when some members are not presented | `[{"name", "tag", "ref", "reason"}]` in the walk's order ([§8.4](#84-what-is-not-presented)): the object's name, absent for one without a name, and its tag and ref, those of a dataset's `Var0.0` Vgroup, of a group's Vgroup, of a table's VH, or of any other member as the Vgroup lists it |

The interfaces' own Vdata classes are the library's `INTERNAL_HDF_VDS`: `DimVal0.0`, `DimVal0.1`,
`Attr0.0`, `SDSVar`, `CoordVar`, `RIATTR0.0N`, `RIATTR0.0C`, and the chunk tables' `_HDF_CHK_TBL_`
prefix. In a file without a `CDF0.0` Vgroup, the root's `unpresented` also lists each NDG, as
`dfsd`.

### 8.2 Datasets

A dataset's array's `hdf4` member: `ref`, its `Var0.0` Vgroup's ref; `ndg`, its NDG's ref, when it
has one; `number_type`, `{"type", "width", "class"}` of its NT element; `layout` (`none`,
`contiguous`, `linked`, `compressed`, `chunked`); `compression`, the method's name (`none`, `rle`,
`nbit`, `skipping_huffman`, `deflate`, `szip`, `jpeg`, `imcomp`, or `code <n>` for another), when
compressed; `chunks`, a
chunked dataset's chunk lengths; `chunk_fill_value`, the base64 of a chunked header's fill value;
`unlimited` (`true`) and `sdd`, the SDD's sizes, for a dataset with an unlimited dimension;
`coordinate`, `true` or `false`, when a `CoordVar` or `SDSVar` Vdata marks it; `text_axis`, a
dataset of text's last axis's name ([§3.3](#33-text)); and `attributes` and `unpresented_attributes`
as a group's.

A table's array's, or its group's, `hdf4` member: `ref`, its VH's ref; `class`, its class (a text
value); `interlace`; `records`, its record count; `fields`, each field's `{"name", "type", "order",
"offset", "size"}`, with `"endian": "little"` when its type says so; `field_attributes`, each
field's attributes as JSON, by the field's name, when it has any; and `attributes` and
`unpresented_attributes` as a group's.

### 8.3 The IR mirror

`refkv_source` is the file's IR mirror ([conventions §11.3](../conventions.md#113-the-ir-mirror)).
Its elements:

| path | kind | what |
|---|---|---|
| `magic` | value, `bytes[4]` | the magic number |
| `descriptors/<k>` | value | the `k`-th descriptor block of the chain, from 0: `{count:>i2,next:>u4,descriptors:{tag:>u2,ref:>u2,offset:>u4,length:>u4}[count]}` |
| `elements/<tag>/<ref>` | value or data | every element a descriptor describes with a length of at least 1: a value (`bytes[n]`; a number type `{version:u1,type:u1,width:u1,class:u1}`) when the profile reads it ([§9.2](#92-reading)), else data whose form is `{"recipe": [["src", 0, null]]}` |
| `gaps/<offset>` | gap | bytes no element claims |

Two descriptors whose elements overlap are an element and an alias of it. Every byte no element
claims is a gap: the mirror rebuilds the file.

### 8.4 What is not presented

| reason | what |
|---|---|
| `name`, `reserved`, `duplicate`, `depth` | [§2.2](#22-paths) |
| `number_type`, `dimensions`, `size` | a number type outside [§3.1](#31-data-types) or absent; an SDD absent or not of the dimension Vgroups' rank; an array of more than 2^64 bytes |
| `compression`, `chunks`, `length` | [§3.2](#32-chunks): a method other than deflate (SZIP, RLE, NBIT, skipping Huffman, JPEG), a model other than the standard one; chunks of mixed methods; data shorter than the array |
| `external`, `special` | data in an external file (special code 2), or in a special element of another code |
| `dfsd` | the datasets of a file without a `CDF0.0` Vgroup ([§3](#3-datasets)) |
| `version`, `datatype`, `fields`, `byte_order`, `record_size` | a table's ([§4](#4-tables)) |
| `object` | any other member of a group: a raster image, a palette, an annotation, an interface's Vgroup |

Every byte of an object not presented is in the mirror, so the hierarchy loses none of the file:
what it does not present it records, and the mirror rebuilds.

### 8.5 Equivalence

Hierarchies of one file are compared entry for entry, `refkv_source` included
([conventions §11.4](../conventions.md#114-equivalence)).

### 8.6 What the layout does not express

This section is informative.

- **Coordinates across groups.** A coordinate variable is presented where the walk finds it. One
  that no group holds is at the root, and an array in a group that has its dimension does not see
  it as a coordinate there; the dimension's name is all they share. The corpus has no file with
  both coordinate variables and groups.
- **HDF-EOS2 geolocation** ([§6](#6-hdf-eos2)), which a later layout may turn into coordinates.
- **The SD interface's dimension metadata**: a dimension Vgroup's own attributes and Vdatas, and the
  record count the `UDim0.0` Vgroup keeps for a netCDF file, are in the mirror only.

# Part 2. The profile

The HDF4 profile of [spec/zarrify.md](../zarrify.md) is [§9](#9-hdf4-profile) of this document. What
every profile's Part 2 shares is in [spec/zarrify.md, Profiles](../zarrify.md#profiles).

## 9. HDF4 profile

**Signature** ([spec/zarrify.md §1.2](../zarrify.md#12-input), tested at the precedence of
[profiles/hdf4.json](profiles/hdf4.json)): bytes 0–3 of `H` are the HDF4 magic number
`0E 03 13 01`.

### 9.1 Choosing the profile

The signature chooses the profile; no other profile reads an HDF4 file.

### 9.2 Reading

Every number is big-endian. The file is the magic number, then a chain of **descriptor blocks**,
the first at byte 4: a block is its descriptor count (`int16`), the offset of the next block
(`uint32`, 0 for the last), and that many descriptors of 12 bytes, a tag (`uint16`), a ref
(`uint16`), an offset and a length (`uint32` each). A descriptor of tag 1 (`DFTAG_NULL`) is an
empty slot.

The profile reads in rounds, each the reads that what was read so far calls for, all at once:

1. the first 2^16 bytes;
2. each descriptor block, the 2^16 bytes from its offset (to the file's end at most), which in
   MODIS files hold the blocks the writer placed after it: one round per block not read yet;
3. every **header element**: each NT, SDD, NDG, VH, VG and VERSION element, and each special
   element's header, in one round;
4. what the headers name, in the profile's order, each independent read of a round at once: the
   link tables of each linked element the profile follows, along their chain until their blocks
   cover the element's length (one round per table), the records of each attribute Vdata and chunk
   table, with the blocks that hold them, and each chunk's header.

An element is read as its descriptor gives it, its offset and length exactly, and held in the IR
mirror ([§8.3](#83-the-ir-mirror)). The data of a dataset is never read.

Structures:

- **A Vgroup** (VG, `vunpackvg` in `hdf/src/vgp.c`): its member count `n` (`uint16`), `n` tags and `n`
  refs, its name and its class (each a `uint16` length and the bytes), the expansion tag and ref,
  and its version, the `uint16` 5 bytes before the element's end; a version 4 Vgroup has, after the
  expansion ref, flags (`uint32`) and, when bit 0 is set, its attributes: a count (`uint32`) and that
  many tags and refs.
- **A Vdata header** (VH, `vunpackvs` in `hdf/src/vio.c`): its interlace (`uint16`), record count
  (`uint32`), record size (`uint16`), field count `n` (`int16`), the fields' `n` types, sizes, offsets
  and orders (`uint16` each), their names, its name and its class (each a `uint16` length and the
  bytes). A Vdata's records are its VS element of its ref, plain or in linked blocks.
- **An SDD**: the rank (`uint16`), then a size (`uint32`) per axis.
- **Special headers** ([§3.2](#32-chunks)): a code (`uint16`), then for linked blocks the length,
  the block length and the blocks per link table (`uint32` each) and the first link table's ref
  (`uint16`); for a compressed element a version (`uint16`), the uncompressed length (`uint32`), the
  stream's ref, the model and the method (`uint16` each), and the method's parameters; for a chunked
  element (`hdf/src/hchunks.c`) the header's length (`uint32`), a version (`uint8`), a flag, the
  logical length, the chunk size and the number type's size (`uint32` each), the chunk table's tag
  and ref and two more (`uint16` each), the rank (`uint32`), each axis's flag, length and chunk
  length (`uint32` each), and the fill value's length (`uint32`) and bytes.

### 9.3 Checks

A zarrifier MUST reject the input, with these messages, in this order:

- **Magic:** a file of fewer than 4 bytes, or not starting with the magic number (`not an HDF4
  file`).
- **Descriptor blocks:** a block whose 6-byte header or descriptors run past the end of the file
  (`the descriptor block at byte <at> runs past the end of the file`); a negative count (`the
  descriptor block at byte <at> has <n> descriptors`); a chain that returns to a block (`the
  descriptor blocks loop at byte <at>`); more than 65536 blocks or 2^20 descriptors (`budget: more
  than 65536 descriptor blocks`, `budget: more than 1048576 descriptors`).
- **Descriptors:** two descriptors of one tag and ref (`two descriptors describe tag <t> ref <r>`);
  an element of a length of at least 1 that runs past the end of the file (`tag <t> ref <r> (<n>
  bytes at byte <o>) runs past the end of the file`).
- **Headers:** a header element of more than 2^20 bytes (`budget: tag <t> ref <r> is <n> bytes,
  more than 1048576 of a header`); a special element's header too short for its code (`the special
  element tag <t> ref <r> is too short for its header`) or of a code outside 1 to 7 (`the special
  element tag <t> ref <r> has the unknown code <c>`); a linked header with no blocks a table or more
  than 65535; a VG or VH element too short for what it lists (`the Vgroup ref <r> runs past its
  element`, `the Vdata header ref <r> runs past its element`).
- **Compressed elements:** a stream in a special element other than linked blocks (`the compressed
  element tag <t> ref <r> holds its data in a special element other than linked blocks`).
- **Linked blocks**, of each linked element the profile follows, along the chain of link tables
  its length needs: a link table that no descriptor describes, or too short for its blocks; link
  tables that loop (`the link tables of tag <t> ref <r> loop`); more than 2^20 blocks
  (`budget: tag <t> ref <r> needs more than 1048576 linked blocks`); a block that no descriptor
  describes, or shorter than the bytes it holds; blocks that hold less than the element's length.
- **Chunks**, of each chunked dataset presented: a chunk length of 0; a chunk table whose Vdata
  header is absent, whose records are not of `4 × rank + 4` bytes, which holds fewer records than
  its header counts, or of more than 2^20 chunks or 2^26 bytes; a chunk placed outside the grid of the header's
  lengths or placed twice (`the chunk table of tag <t> ref <r> places a chunk at <coords>, outside
  its grid <grid>`, `… places two chunks at <coords>`); a chunk element that no descriptor describes,
  shorter than a chunk, that decodes to another length, or whose stream is empty, or shorter than a
  chunk for method 0 (`the chunk at <coords> of tag <t> ref <r> holds <n> bytes of its stream`).
- **Rounds:** more than 4096 rounds of dependent reads (`more than 4096 rounds of dependent
  reads`).

No number type, method, special code from 1 to 7, Vdata or object that the profile does not present
rejects the input: [§8.4](#84-what-is-not-presented) records it.

### 9.4 Chunk references

<a id="type-hdf4chunk"></a>**`Hdf4Chunk`**, a chunk of a dataset's array: the ranges of source 0
that hold its bytes, in order, as [§3.2](#32-chunks) cuts them: one range for plain data, and one
per block for data in linked blocks, adjacent ranges joined; a block never written, and the zero
padding of a contiguous array's edge chunk, are ranges of the zero data source
([spec/zarrify.md §1.2](../zarrify.md#12-input)), at most 2^16 bytes each.

### 9.5 Summary

Both implementations print `groups` and `arrays` (presented), `unpresented` (a count by reason),
`chunks` (the chunks of the presented chunked datasets that the chunk tables list), `descriptors`,
`chunk_entries` (the chunks written) and `rounds`.
