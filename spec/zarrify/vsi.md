# VSI

The Zarr layout of an Olympus VSI slide (a cellSens `.vsi` file and the tile
files beside it), and the translation of its metadata tree into JSON. What
all of refkv's conventions share is in [spec/conventions.md](../conventions.md),
cited here as "conventions §n". How refkv produces this layout as a reference
store, which files it reads and which inputs it rejects, is the VSI profile,
[Part 2](#part-2-the-profile) below.

Convention version: 0 (until release, [conventions §1](../conventions.md#1-conventions)) · UUID: `c0a1d4e6-5b7f-4f3a-9c2d-8e1f6a7b3c45` ·
Schema: [schema.json](vsi/schema.json) · Added 2026-10-21, after
[revision 29](../history/virtualize/REVISIONS.md#revision-29-no-recorded-revision-the-profile-registry)
(a profile's changes are recorded here, dated, from then on) · Changed 2026-10-26, with no
change to any output: the layout is tabulated, its entries in [Layout](#layout) and its types
in type tables ([conventions §11.5](../conventions.md#115-layout-tables)), which also state what
refkv already wrote: a value field's empty data is opaque, `""` ([§6.1](#61-the-root)), and an
IFD image's summary has no `sizes`, `grids` or `tilesOutside` ([§9.5](#95-summary))

This document has two parts ([conventions §11.1](../conventions.md#111-parts)).

# Part 1. The convention

## Layout

The hierarchy's entries ([conventions §11.5](../conventions.md#115-layout-tables)), a
bioformats2raw collection ([conventions §4](../conventions.md#table-collection)) of the
images of [§4](#4-the-images):

| prefix | type | structure | description |
|---|---|---|---|
| `zarr.json` | group | [`GroupDocument`](../conventions.md#type-groupdocument), attributes a [`CollectionRoot`](../conventions.md#type-collectionroot) | the root, which declares the convention ([§1](#1-declaration)) |
| `zarr.json#/attributes/ome` | attribute | object | `{"version": "0.5", "bioformats2raw.layout": 3}`, present when there is at least one image |
| `zarr.json#/attributes/zarr_conventions` | attribute | array of [`ConventionMetadata`](../conventions.md#type-conventionmetadata) | the CMO of [§1](#1-declaration) |
| `zarr.json#/attributes/refkv_zarrified` | attribute | [`RootProperty`](../conventions.md#type-rootproperty) | `profile` `"vsi"`, `version` `0`, `source.url` the `.vsi` file's object URL ([§1](#1-declaration)) |
| `zarr.json#/attributes/refkv_zarrified/vsi` | attribute | [`VsiRoot`](#type-vsiroot) | the root's source metadata ([§6.1](#61-the-root)) |
| `OME/zarr.json` | group | [`GroupDocument`](../conventions.md#type-groupdocument), attributes a [`SeriesGroup`](../conventions.md#type-seriesgroup) | the images' paths, `"0"`, `"1"`, ...; present when there is at least one image |
| `<k>/zarr.json` | group | [`ImageGroup`](../conventions.md#type-imagegroup) | image `k` ([§4](#4-the-images)), which declares the convention ([§1](#1-declaration)) |
| `<k>/zarr.json#/attributes/ome` | attribute | [`OmeImage`](../conventions.md#type-omeimage) | `M`: named by its stack (`macro image` for the macro image), a level per array, with the scales, units and translations of [§5](#5-scales-units-and-position), and `omero` ([§4.2](#42-channels)) |
| `<k>/zarr.json#/attributes/zarr_conventions` | attribute | array of [`ConventionMetadata`](../conventions.md#type-conventionmetadata) | the CMO of [§1](#1-declaration) |
| `<k>/zarr.json#/attributes/refkv_zarrified` | attribute | [`NodeProperty`](../conventions.md#type-nodeproperty) | `{"vsi": ...}`, with no `profile`, `version` or `source` |
| `<k>/zarr.json#/attributes/refkv_zarrified/vsi` | attribute | [`VsiImage`](#type-vsiimage) or [`VsiMacro`](#type-vsimacro) | the image's source metadata ([§6.1](#61-the-root)): a stack's, or the macro image's |
| `<s>/<l>/zarr.json` | array | [`VsiLevelArray`](#type-vsilevelarray) | level `l` of a stack's pyramid ([§4.1](#41-the-pyramid)) |
| `<s>/<l>/c/<coords>` | chunk | [`VsiTileChunk`](#type-vsitilechunk) | each tile inside its level ([§4.1](#41-the-pyramid)); a tile the directory lacks, or outside its level, has none |
| `<f>/0/zarr.json` | array | [`VsiIfdArray`](#type-vsiifdarray) | an IFD image's one array ([§4.3](#43-the-macro-image)) |
| `<f>/0/c/<origin>` | chunk | [`VsiIfdChunk`](#type-vsiifdchunk) | its one chunk ([§4.3](#43-the-macro-image)) |
| `refkv_source/**` | subtree | [the mirror's entries](../conventions.md#table-mirror) | the store IR mirror ([§6.2](#62-the-ir-mirror)): its objects are the `.vsi` file and the tile files read, its elements [§6.3](#63-elements)'s; the convention has no arrays of its own there |

| placeholder | value |
|---|---|
| `<k>` | an image's index in decimal, from 0, in the order of [§4](#4-the-images) |
| `<s>` | the index `<k>` of an image that is a stack's pyramid (a stack with a tile file) |
| `<f>` | the index `<k>` of an IFD image: a stack's default sample IFD, or the macro image |
| `<l>` | a level, from 0 to `L − 1`, in decimal |
| `<coords>` | `<t>/<c>/<z>/<row>/<column>`, the tile's coordinates for the axes present, one key segment each (`c` is the channel's index, or 0 when `S > 1`; two to five segments) |
| `<origin>` | `0/0/0` for three samples (`c`, `y`, `x`), `0/0` for one |

## 1. Declaration

The root and `refkv_source` declare the convention
([conventions §11.2](../conventions.md#112-declaration)), the root with the `.vsi`
file's URL as `source.url` (the object URL of its key in the store,
[spec/zarrify.md §1.4](../zarrify.md#14-store-inputs)) and its source metadata
of [§6.1](#61-the-root) as the member `"vsi"`. Its CMO is:

```json
{
  "uuid": "c0a1d4e6-5b7f-4f3a-9c2d-8e1f6a7b3c45",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/zarrify/vsi/schema.json",
  "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md",
  "name": "refkv_zarrified",
  "description": "The Zarr layout of a VSI source zarrified by refkv, and the source's metadata"
}
```

Each image's group ([§4](#4-the-images)) declares it too, with the image's
source metadata ([§6.1](#61-the-root)) as `"vsi"` and no `profile`, `version`
or `source` (a node property, [conventions §2](../conventions.md#2-attributes),
a [`NodeProperty`](../conventions.md#type-nodeproperty)).
The arrays and the `OME` group have no attributes.

## 2. The .vsi file

A slide is a `.vsi` file and a directory beside it, `_<name>_` for the file
`<name>.vsi`, holding one directory per stack with a **tile file**
([§3](#3-the-tile-files)). The `.vsi` file is a little-endian classic TIFF
(`II`, 42, the first IFD's offset) whose byte 8 starts the **metadata tree**:
a container of fields that fills the rest of the file, and inside whose
regions the TIFF IFDs lie ([§2.3](#23-the-tiff-images)). All integers are
little-endian. The types:

| type | value |
|---|---|
| `i32`, `i64` | a two's complement integer of that width |
| `u32` | an unsigned integer of that width |
| `f64` | an IEEE 754 binary64 number |

### 2.1 Containers and fields

A **container** at offset `fp` with the **region** `[fp, end)` is a 24-byte
header followed by its fields, which tile the region in order:

| offset | type | field |
|---|---|---|
| 0 | `u16` | `header_size`: 24 |
| 2 | `u16` | `version`: 21321 |
| 4 | `i32` | `volume_version` |
| 8 | `i64` | `data_offset`: 24, or 0 for an empty container |
| 16 | `u32` | `flags`: the low 28 bits are the **field count** |
| 20 | `u32` | reserved |

The tree's root container is at offset 8, with the region `[8, size)`,
`size` the file's size. A **field** at offset `p` has a 16-byte header, `type`,
`tag`, `next` and `size` (four `u32`), followed by a fifth `u32`, the
**second tag** `tag2`, when bit 27 of `type` is set. The bits of `type`:

| bits | meaning |
|---|---|
| 27 | the field has a second tag |
| 28 | an **extended** field |
| 30 | an **inline** field |
| 0–23 (the low 24) | the **field type**: for a plain field, the type of its value (the table `field_types`, below); for an extended field, what its data holds (the table `extended_types`, below) |

The other bits of `type` are not interpreted. The table `field_types` of the schema:

| field type | name |
|---|---|
| 1 | `CHAR` |
| 2 | `UCHAR` |
| 3 | `SHORT` |
| 4 | `USHORT` |
| 5 | `INT` |
| 6 | `UINT` |
| 7 | `LONG` |
| 8 | `ULONG` |
| 9 | `FLOAT` |
| 10 | `DOUBLE` |
| 12 | `BOOLEAN` |
| 13 | `TCHAR` |
| 14 | `DWORD` |
| 17 | `TIMESTAMP` |
| 18 | `DATE` |
| 256 to 280 | the vector, rectangle, interval, matrix and array types (`INT_2`, `DOUBLE_2`, `INT_RECT`, `DOUBLE_INTERVAL`, `RGB` 269, `BGR` 270, ...) |
| 8192, 8194 | the Unicode texts |
| 8195 | `DIM_INDEX_1` |
| 8199 | `DIM_INDEX_2` |
| 8200 | `VOLUME_INDEX` |
| 8470 | `PIXEL_INFO_TYPE` |

The table `extended_types`:

| field type | an extended field's data holds |
|---|---|
| 0 | a **volume** |
| 1 | a **property set** |
| 2 | an **mdim volume** |
| 10 | a **TIFF IFD** |

The field's **region** runs from `p` to `fp + next`, or to the container's
`end` for the last field, whose `next` is 0 (and only the last field's is).
Its **data** starts at `q`, the end of its header (`p + 16`, or `p + 20` with a
second tag), and ends:

| field | its data ends at |
|---|---|
| an inline field | `q`: it has no data, and `size` is its value (an integer) |
| a field with a second tag whose `size` is not 0 | `fp + 20 + size` (the writer counts those bytes from its container's start, not from the field's) |
| any other | `q + size` |

A **plain** (not extended, not inline) field is a **value**: its data is one
value, or an array of values, of its field type. A volume's data is one
container, with the region `[q, data end)`. A property set's or an mdim
volume's data is one container filling the field's region, `[q, fp + next)`
(or to `end`). A TIFF IFD field's data region holds a TIFF IFD, its values
and its strips ([§2.3](#23-the-tiff-images)). Whatever a field's region holds
after its data (a volume's or a TIFF IFD field's, and any plain or inline
field's) holds TIFF structures too, or padding; the IFDs of the coarse
thumbnails lie there, after an inline `Coarse pyramid level` field (tag
2022). An empty container's region past its header holds the same.

Tags are reused in different containers with different meanings (2003 is
`Dimension size` in a stack's volume and `Display limits` in a property set;
2018 is `RWC frame origin` in the stack properties and the `External file
properties` set in a frame volume; 2019 `RWC frame scale` and `Coarse frame
IFD`). The table `tag_names` of the schema names the tags as Bio-Formats'
CellSensReader does; a tag it does not name is still a field.

### 2.2 Stacks

The root container's **collection volume** is its first volume field of tag
2000. Each volume field of tag 2001 (`Multidimensional image volume`) in
it is a **stack**, whose **id** is the field's second tag (the `N` of its
directory `stack<N>`, [§3](#3-the-tile-files)). In the stack's container, and the containers
within it:

| in | field | tag | kind | value |
|---|---|---|---|---|
| the stack's container | `Dimension size` | 2003 | a value | the stack's **dimension sizes**, an array of integers (empty for a plain image), recorded as they are |
| the stack's container | the **frame volume** | 2002 | a volume | |
| the frame volume | the **external file properties** | 2018 | a property set | |
| the external file properties | `Image boundary` | 2053 | four numbers `[x, y, w, h]` | whose third and fourth values are the stack's **width** `W` and **height** `H` in pixels |
| the external file properties | `Has external file` | 20005 | | the stack **has a tile file** when it is present and not 0 |
| the frame volume | `Default sample IFD` | 2016 | a TIFF IFD field | its second tag is the index in the TIFF chain ([§2.3](#23-the-tiff-images)) of the stack's full image (a focus map or a sample mask stored in the `.vsi` file itself) |
| the frame volume | `Coarse frame IFD` | 2019 | a TIFF IFD field | its second tag is the index in the TIFF chain of the stack's coarse thumbnail |
| the stack's container | the **stack properties** | 2005 | a property set | |
| the stack properties | `Stack name` | 2030 | a text | |
| the stack properties | `Stack type` | 2074 | an integer | the table `stack_types` (below) |
| the stack properties | `RWC frame scale` | 2019 | two numbers `[sx, sy]` | the pixel size |
| the stack properties | `RWC frame origin` | 2018 | two numbers `[ox, oy]` | the position of the image's first pixel in the slide's stage coordinates |
| the stack properties | `RWC frame unit` | 2020 | a text | `10^-6m^1` for micrometers; the table `units` |
| the stack properties | `Display limits` | 2003 | two numbers `[lo, hi]` | |
| the stack properties | `Creation time` | 2015 | an integer | |
| anywhere under the stack properties (depth first) | `Objective magnification` | 120060 | a number | |
| the stack's container | the **dimension descriptions** | 2007 | volumes | below |

The table `stack_types`:

| code | stack type |
|---|---|
| 0 | `Default image` |
| 1 | `Overview image` |
| 2 | `Sample mask` |
| 4 | `Focus image` |
| 8, 16, 32 and 64 | the EFI maps and stack |
| 256 | `Macro image` |

The **dimension descriptions** (2007, volumes) are one per dimension of the
stack beyond x and y, whose second tag `d` is the dimension's index: its
**coordinate** in a tile record is `2 + d` ([§3](#3-the-tile-files)). Each
holds property sets of tag 2008, the **channel properties**; the
dimension's **meaning** is the first `Dimension meaning` (2023) among
them, by the table `dimension_meanings` (below), or, when none has
one, `z` when one has `Z start` (2012), else `t` when one has `Time start`
(2100) or `Dimension value ID` (2027), else `c` when one has `Channel name`
(2419) or `Stack display LUT` (2004), else `unknown`. For the `z` dimension,
the last `Z increment` (2013, an mdim volume) among the sets gives the
**z step** and its unit. For the `c` dimension, each set is a **channel**
(below).

The table `dimension_meanings`:

| code | meaning |
|---|---|
| 1 | `z` |
| 2 | `t` |
| 3 | `lambda` |
| 4 | `c` |
| 9 | `phase` |
| another code | the text `meaning <code>` |

A **channel**'s fields, in its set:

| of the channel | field | tag | value |
|---|---|---|---|
| its name | `Dimension name`, or else `Channel name` | 2021, 2419 | the dye, or else the optical path; both are recorded |
| its display limits | `Display limits` | 2003 | |
| its **color** | `Stack display LUT` | 2004 | an mdim volume whose `Value` is an array of 3-byte entries, `RGB` or `BGR` by its field type: the last entry, as `RRGGBB` |
| its wavelength | `Channel wavelength` | 2417 | an mdim volume |

An **mdim volume**'s value is the `Value` field (tag 268435458) of its
container, its unit the text of the `Units` field (268435456). A value field
is read by its field type ([§6.1](#61-the-root) gives the translation): a
field of one number is that number, a text field its text; a field read as
several numbers where one is wanted, or of another type, is as if absent.

### 2.3 The TIFF images

The TIFF **chain** starts at the header's first IFD offset: each IFD is an
entry count (`u16`), 12-byte entries (tag, type, count, value or offset) and
the next IFD's offset (`u32`), as classic TIFF; IFD `k` of the chain has the
**index** `k`. An IFD whose entry of tag 34665 (`ExifIFD`, type `LONG`, `IFD`
or `SHORT`) is not 0 also has an **EXIF IFD** at that offset, which is not
in the chain. Every IFD, main or EXIF, lies inside a TIFF region of the tree
([§2.1](#21-containers-and-fields)); its elements go under that region's field
([§6.3](#63-elements)).

An IFD used as an **image** (the macro image, [§4.3](#43-the-macro-image), or a
stack's default sample IFD) is one of `ImageWidth` (256) × `ImageLength` (257)
pixels, with these tags; a tag that is absent has its TIFF default:

| tag | code | value | TIFF default |
|---|---|---|---|
| `SamplesPerPixel` | 277 | 1 or 3 | 1 |
| `BitsPerSample` | 258 | 8, or 16 when uncompressed | 1 |
| `Compression` | 259 | 1 (none) or 7 (JPEG) | 1 |
| `PlanarConfiguration` | 284 | 1 | 1 |
| `SampleFormat` | 339 | 1 | 1 |
| `StripOffsets`/`StripByteCounts` (else `TileOffsets`/`TileByteCounts`) | 273, 279 (324, 325) | one strip when JPEG, at least one otherwise | |
| `PhotometricInterpretation` | 262 | read: it gives the macro image's JPEG transform byte ([§4.3](#43-the-macro-image)) | 2 for three samples, else 1 |

A JPEG strip is an
abbreviated stream whose tables are in `JPEGTables` (347); the layout
supplies them ([§4.3](#43-the-macro-image)).

## 3. The tile files

A stack that has a tile file keeps its pixels in `_<name>_/stack<N>/frame_t.ets`
(or `frame_t_0.ets`, which some writers use for a single-frame stack),
relative to the `.vsi` file's directory. A tile file is a **SIS header**, an
**ETS header** it points at, the **tiles**, and a **tile directory**:

The SIS header, the file's first 64 bytes:

| offset | type | field |
|---|---|---|
| 0 | `char[4]` | `magic`: `SIS` and a NUL |
| 4 | `i32` | `header_size`: 64 |
| 8 | `i32` | `version` |
| 12 | `i32` | `coordinates`: the integers per tile record, `n` |
| 16 | `i64` | `ets_offset`: the ETS header's offset |
| 24 | `i32` | `ets_size`: its length, 228 or more |
| 28 | `i32` | reserved |
| 32 | `i64` | `tiles_offset`: the tile directory's offset |
| 40 | `i32` | `tiles`: the tile count |
| 44 | `i32` | reserved |
| 48 | `i64` | `size` |
| 56 | `bytes[8]` | reserved |

The ETS header, `ets_size` bytes at `ets_offset`, whose first 228 are:

| offset | type | field |
|---|---|---|
| 0 | `char[4]` | `magic`: `ETS` and a NUL |
| 4 | `i32` | `version` |
| 8 | `i32` | `pixel_type` (the table `pixel_types`: 1 `int8`, 2 `uint8`, 3 `int16`, 4 `uint16`, 5 `int32`, 6 `uint32`, 7 `int64`, 8 `uint64`, 9 `float32`, 10 `float64`) |
| 12 | `i32` | `channels`: the **samples per pixel**, 1, 3 or 4 |
| 16 | `i32` | `colorspace` |
| 20 | `i32` | `compression` (the table `compressions`: 0 none, 2 JPEG, 3 JPEG 2000, 5 lossless JPEG; 8 PNG and 9 BMP are named and not supported) |
| 24 | `i32` | `quality` |
| 28 | `i32` | `tile_width` `tw` |
| 32 | `i32` | `tile_height` `th` |
| 36 | `i32` | `tile_depth`: 1 |
| 40 | `i32[17]` | `pixel_info_hints` |
| 108 | `bytes[40]` | `background`: the background color, one pixel's samples in the pixel type |
| 148 | `i32` | `component_order`: 1 when uncompressed samples are stored blue, green, red |
| 152 | `i32` | `use_pyramid`: not 0 when the last coordinate is the level |
| 156 | `bytes[28]` | reserved |
| 184 | `i32` | `dimensions`: the sizes listed, at most 10; 0 in a header of version 0x30003, which lists none |
| 188 | `i32[10]` | `dimension_sizes`: the size of each coordinate, the first two the image's width and height |

The tile directory, `tiles` records of `20 + 4n` bytes at `tiles_offset`, each:

| offset | type | field |
|---|---|---|
| 0 | `i32` | `coordinates`: `n` |
| 4 | `i32[n]` | `coordinate` |
| 4 + 4n | `i64` | `offset` |
| 12 + 4n | `i32` | `size` |
| 16 + 4n | `i32` | `index` |

A tile's coordinates are its column
(`coordinate[0]`) and row (`coordinate[1]`) in the level's grid of `tw × th`
tiles, then the stack's dimensions in coordinate order ([§2.2](#22-stacks)),
and, when `use_pyramid` is set, the **level** last (`coordinate[n − 1]`).
The size of coordinate `c ≥ 2` that is not the level is `dimension_sizes[c]`
when `c < dimensions`, else 1. A tile's bytes, `size` at `offset`, are one
`tw × th` tile with `channels` samples per pixel, interleaved, row-major, in
the pixel type (uncompressed: `tw × th × channels × bytes` bytes,
little-endian; a tile at the right or bottom edge is a whole tile, padded by
the writer), or one JPEG or JPEG 2000 codestream of such a tile.

## 4. The images

The hierarchy is a **bioformats2raw layout** of images ([conventions §4](../conventions.md#4-images),
its [entries](../conventions.md#table-collection); this convention's are in [Layout](#layout)):
the root `zarr.json` ([`CollectionRoot`](../conventions.md#type-collectionroot)) has `"ome": {"version": "0.5", "bioformats2raw.layout": 3}`
beside `zarr_conventions` and `refkv_zarrified` ([§1](#1-declaration)), the
group `OME` has `{"version": "0.5", "series": ["0", "1", ...]}`, and image `k`
is the group `k`, one OME-Zarr 0.5 image named by its stack. The images are,
in this order: each stack of the tree ([§2.2](#22-stacks)) that has a tile
file ([§4.1](#41-the-pyramid)) or else a default sample IFD ([§4.3](#43-the-macro-image)),
in the tree's order; then the **macro image** ([§4.3](#43-the-macro-image)),
when the collection volume has a `Default sample IFD` field (2016). A stack
with neither is no image (it is listed in `stacks`, [§6.1](#61-the-root)).
Without any image, the root has no `ome` member and there is no `OME` group.

### 4.1 The pyramid

A stack with a tile file is an image whose **levels** are the tile file's:
`L` levels, `L` being 1 past the greatest level coordinate among the tiles
when `use_pyramid` is set, else 1. Level 0 is `W × H` ([§2.2](#22-stacks)).
Level `l > 0` is `w_l × h_l` from the level before, as Bio-Formats computes
it: with `mx = tw × (1 + max column coordinate at level l)` and `my` likewise
for the rows, `w_l = floor(w_{l−1} / 2)`, plus 1 when `w_{l−1}` is odd and
that quotient is less than `mx`, else capped at `mx` when it is greater;
`h_l` the same from `h_{l−1}` and `my`. A tile whose column is `ceil(w_l / tw)`
or more, or whose row is `ceil(h_l / th)` or more, is **outside** its level
and is no chunk (the count is recorded, [§6.1](#61-the-root)).

**Axes:** `t` when the `t` dimension's size `T` is over 1; `c` when the `c`
dimension's size `C` is over 1 or the samples per pixel `S` are over 1;
`z` when the `z` dimension's size `Z` is over 1; then `y`, `x`. (A
dimension is the stack's dimension of that meaning whose coordinate is a
tile record's and not the level's; without one, its size is 1. A
dimension of another meaning has the size 1, [§9.3](#93-tiles-and-levels).)
`t` is `time`, `c` `channel`, `z`, `y` and `x` `space`.

**Arrays:** one per level, at the path `l`.

<a id="type-vsilevelarray"></a>**`VsiLevelArray`**, level `l`'s `zarr.json`: an
[`ArrayDocument`](../conventions.md#type-arraydocument) with these members:

| member | type | presence | value |
|---|---|---|---|
| `shape` | array of integer | required | `[T, C or S, Z, h_l, w_l]` in the axes present (the `c` axis has `S` when `S > 1`, else `C`) |
| `data_type` | string | required | the data type of the pixel type ([§3](#3-the-tile-files)) |
| `chunk_grid` | object | required | regular, its chunk shape `[1, S or 1, 1, th, tw]` in the axes present (`S` on the `c` axis when `S > 1`, else 1) |
| `fill_value` | number | required | the background's first sample (`background`'s first `bytes` bytes as the pixel type; 0 for a float that is not finite). A tile the directory lacks reads as the background |
| `codecs` | array of object | required | for `S > 1`, `transpose` with the order that puts `c` last of the axes (`{"name": "transpose", "configuration": {"order": [1, 2, 0]}}` for `c`, `y`, `x`), since the samples are interleaved; then `imagecodecs_jpeg` for JPEG, `imagecodecs_jpeg2k` for JPEG 2000, `imagecodecs_ljpeg` for lossless JPEG, or `bytes` with `{"endian": "little"}` (plain `bytes` for a one-byte type). A JPEG tile of three samples is a plain codestream; no transform marker is added |
| `dimension_names` | array of string | required | the axes |

**Chunks:** each tile inside its level is the chunk at `l/c/<t>/<c>/<z>/<row>/<column>`
(the keys of the axes present; `c` is 0 when `S > 1`), its bytes the tile's
bytes ([§9.4](#94-chunks), a [`VsiTileChunk`](#type-vsitilechunk)).

### 4.2 Channels

`M`, the image's `ome` member, has `"omero": {"channels": [...]}` (an
[`Omero`](../conventions.md#type-omero) of [`OmeroChannel`](../conventions.md#type-omerochannel)s):

| case | channels | `label` and `color` |
|---|---|---|
| `S > 1` | one per sample | labels `R`, `G`, `B`, `A` with the colors `FF0000`, `00FF00`, `0000FF`, `FFFFFF`, or `B`, `G`, `R`, `A` with `0000FF`, `00FF00`, `FF0000`, `FFFFFF` for an uncompressed file whose `component_order` is 1 (its samples are stored blue first; the array keeps the stored order, and the labels say which is which) |
| else | one per channel `c` of the `c` dimension | the label is the channel's name ([§2.2](#22-stacks)), or `C<c>` when the stack has no channel `c` or it has no name; the color the channel's, or `FFFFFF` |

Each channel has `"active": true` and, for a data type of at most 32 bits, a
`window`: `min` 0 and `max` the type's largest value (for a signed type, its
least and largest), `start` and `end` the display limits, clamped to
`[min, max]`, when they are finite and `end > start`, else `min` and `max`.
The limits are the channel's own ([§2.2](#22-stacks)), else the stack's. A
64-bit or float type has no `window`.

### 4.3 The macro image

The **macro image** is the IFD the collection volume's `Default sample IFD`
names (its second tag, [§2.2](#22-stacks)): the slide's overview photograph,
a JPEG. A stack without a tile file but with a `Default sample IFD` (a focus
map, a sample mask) is likewise an **IFD image**. Such an image is one array
at the path `0`, the whole IFD.

<a id="type-vsiifdarray"></a>**`VsiIfdArray`**, an IFD image's `0/zarr.json`: an
[`ArrayDocument`](../conventions.md#type-arraydocument) with these members:

| member | type | presence | value |
|---|---|---|---|
| `shape` | array of integer | required | `[3, height, width]` for three samples, or `[height, width]` for one |
| `data_type` | string | required | `uint16` for 16 bits per sample, else `uint8` |
| `chunk_grid` | object | required | regular, its chunk shape the shape: one chunk, `0/c/0/0/0` or `0/c/0/0` |
| `fill_value` | number | required | 0 |
| `codecs` | array of object | required | `transpose` putting `c` last for three samples; then `imagecodecs_jpeg` for compression 7, else `bytes` (`{"endian": "little"}` for `uint16`) |
| `dimension_names` | array of string | required | **axes** `c`, `y`, `x` for three samples; `y`, `x` for one |

**Chunk:** for JPEG, the strip's first two bytes (SOI), then a **prefix**,
then the rest of the strip: the prefix is, for three samples, the Adobe
APP14 marker `FF EE 00 0E 'Adobe' 00 64 00 00 00 00` followed by the
transform byte, 0 for `PhotometricInterpretation` 2 (RGB) and 1 otherwise,
and then the body of `JPEGTables` (its bytes between its SOI and its EOI)
when the tag is present and well formed. When the prefix is empty, or the
strip is 2 bytes or fewer, the chunk is the strip. For an uncompressed
image, the chunk is the strips in order, one range per run of contiguous
strips ([§9.4](#94-chunks), a [`VsiIfdChunk`](#type-vsiifdchunk)).

**Channels:** as [§4.2](#42-channels), with the stack's display limits (the
macro image has none, so its windows are the type's range).

The macro image is named `macro image`, as Bio-Formats names it, has no
scale unit and no translation ([§5](#5-scales-units-and-position)); an IFD
image of a stack is named by its stack and placed by its properties.

## 5. Scales, units and position

A stack's **unit** is its `RWC frame unit` text's entry in the table `units`;
a text the table lacks is no unit:

| text | unit |
|---|---|
| `10^-9m^1` | nanometer |
| `10^-6m^1` | micrometer |
| `10^-3m^1` | millimeter |
| `10^-2m^1` | centimeter |
| `10^0m^1`, `m` | meter |

Each axis's scale and unit:

| axis | scale | unit |
|---|---|---|
| `x`, `y` | with a unit and a `RWC frame scale` `[sx, sy]`: at level `l`, `sx × W / w_l` and `sy × H / h_l` (so level 0 has `sx`, `sy`, and each coarser level the pixel size its subsampling gives; an IFD image has `sx`, `sy`); otherwise 1 | with a unit and a `RWC frame scale`, that unit; otherwise none |
| `z` | with the `z` axis present, a finite positive z step and a z unit the table names, the step; else 1 | that z unit; else none |
| `t`, `c` | 1 | none |

Each level's dataset has a `translation` when the stack has a unit and a
`RWC frame origin` `[ox, oy]` with both finite: `ox` for `x`, `oy` for `y`,
0 for the other axes, the same at every level (the origin is a position,
not a pixel index). Otherwise no dataset has one. The image's `name` is the
stack's name (`macro image` for the macro image). The multiscale metadata is
that of [conventions §4](../conventions.md#4-images) with these scales,
units and translations.

## 6. Source metadata

The root's source metadata `S` ([conventions §2](../conventions.md#2-attributes))
is an object with the members of [§6.1](#61-the-root), in that order, and each
image's is the object given there. Everything the files hold is on
`refkv_source`, the input's **IR mirror** ([§6.2](#62-the-ir-mirror)), from
which every file read is described byte for byte.

### 6.1 The root

<a id="type-vsiroot"></a>**`VsiRoot`**, the root's `S`:

| member | type | presence | value |
|---|---|---|---|
| `file` | [`VsiFile`](#type-vsifile) | required | the `.vsi` file |
| `stacks` | array of [`VsiStack`](#type-vsistack) | required | one object per stack of the tree, in its order |
| `tree` | [`VsiContainer`](#type-vsicontainer) | required | the metadata tree as JSON: its root container |

<a id="type-vsifile"></a>**`VsiFile`**, `{"key": k, "size": s, "ifds": n}`:

| member | type | presence | value |
|---|---|---|---|
| `key` | string | required | `k`, the `.vsi` file's key (its name) |
| `size` | integer | required | `s`, its size |
| `ifds` | integer | required | `n`, the IFDs of its chain (EXIF IFDs not counted) |

<a id="type-vsistack"></a>**`VsiStack`**, a stack of the tree:

| member | type | presence | value |
|---|---|---|---|
| `id` | integer | required | its id ([§2.2](#22-stacks)) |
| `name` | string | required | its name (`Stack name`) |
| `type` | integer or null | required | the stack type, or `null` |
| `external` | boolean | required | whether it has a tile file |
| `width` | integer or null | required | `W`; `null` without an image boundary |
| `height` | integer or null | required | `H`; `null` without an image boundary |
| `tile_file` | string or null | required | the key of its tile file, or `null` |

<a id="type-vsicontainer"></a>**`VsiContainer`**, a container, `{"fields": [...]}`:

| member | type | presence | value |
|---|---|---|---|
| `fields` | array of [`VsiField`](#type-vsifield) | required | its fields, in order |

<a id="type-vsifield"></a>**`VsiField`**, a field: `tag`, `tag2` when it has a second tag,
`type`, `name` when the table `tag_names` names the tag, and then one of: `"inline": true` and
`value` (the inline integer); `value`, a value field's data translated as below; `fields`, a
volume's, property set's or mdim volume's container; or `ifd`, for a TIFF IFD field:

| member | type | presence | value |
|---|---|---|---|
| `tag` | integer | required | its tag |
| `tag2` | integer | optional | its second tag, when it has one |
| `type` | integer | required | the field type |
| `name` | string | optional | the tag's name, when the table `tag_names` names the tag |
| `inline` | boolean | optional | `true`, for an inline field |
| `value` | integer or [`VsiValue`](#type-vsivalue) | optional | an inline field's: the inline integer; a value field's: its data translated as below |
| `fields` | array of [`VsiField`](#type-vsifield) | optional | a volume's, property set's or mdim volume's: its container's fields (as a [`VsiContainer`](#type-vsicontainer)'s) |
| `ifd` | [`VsiFieldIfd`](#type-vsifieldifd) | optional | a TIFF IFD field's |

<a id="type-vsifieldifd"></a>**`VsiFieldIfd`**, `{"index": i, "bytes": b}`:

| member | type | presence | value |
|---|---|---|---|
| `index` | integer | optional | `i`, its second tag, when it has one: the IFD's index in the chain |
| `bytes` | integer | required | `b`, its data region's length |

<a id="type-vsivalue"></a>**`VsiValue`**, a value field's data `d` of field type `T`
translated, one of:

| form | when |
|---|---|
| string | a Unicode text type (13, 8192, 8194): the string its 16-bit little-endian units encode, up to the first 0 unit, an unpaired surrogate decoded as U+FFFD (the field's data, when its length is odd, is opaque) |
| boolean | `BOOLEAN` (12): `true` or `false` when it is one byte (of another length, it is opaque) |
| a number, an [`Integer`](../conventions.md#type-integer) or a [`Float`](../conventions.md#type-float) | a type of width `w` bytes (the table's `type`): its one number when `d` is `w` bytes |
| array of number, [`Integer`](../conventions.md#type-integer) or [`Float`](../conventions.md#type-float) | a type of width `w`: an array of its `d / w` numbers when `d` is not empty, `d / w` is a whole number and `d` is at most 1024 bytes (the array of 3-byte `RGB` and `BGR` entries is an array of bytes) |
| [`Base64`](../conventions.md#type-base64) | **opaque** data, at most 1024 bytes: a type the table lacks, or of a type of width `w` empty data or a length that is not a multiple of `w`; the string of its base64 encoding (`""` for empty data) |
| [`VsiBytes`](#type-vsibytes) | data longer than 1024 bytes that would be an array or opaque |

An integer past 2^53 − 1 in magnitude, and a float that is not finite, are JSON as
[conventions §6](../conventions.md#6-source-metadata-as-json) writes them (`"NaN"`,
`"Infinity"`, `"-Infinity"`).

<a id="type-vsibytes"></a>**`VsiBytes`**, `{"bytes": n}`:

| member | type | presence | value |
|---|---|---|---|
| `bytes` | integer | required | `n`, the data's length: the bytes are in the mirror |

An **image's** source metadata is a [`VsiImage`](#type-vsiimage) for a stack (a pyramid or an
IFD image), and a [`VsiMacro`](#type-vsimacro) for the macro image.

<a id="type-vsiimage"></a>**`VsiImage`**, a stack's image's `S`: the members to `name`, and
then, for a pyramid, `tile_file`, `ets`, `levels` and `tiles_outside`, or, for an IFD image,
`ifd`:

| member | type | presence | value |
|---|---|---|---|
| `id` | integer | required | the stack's id |
| `type` | integer or null | required | the stack type, or `null` |
| `type_name` | string or null | required | the table's name (`stack_types`), or `null` |
| `width` | integer or null | required | `W`, or `null` without an image boundary |
| `height` | integer or null | required | `H`, or `null` without an image boundary |
| `dimension_sizes` | array of integer | required | the stack's dimension sizes, as recorded |
| `dimensions` | array of [`VsiDimension`](#type-vsidimension) | required | in coordinate order |
| `scale` | array of number or null | required | `[sx, sy]` or `null` |
| `unit` | string or null | required | the `RWC frame unit` text or `null` |
| `origin` | array of number or null | required | `[ox, oy]`, the `RWC frame origin`, or `null` |
| `z_step` | number or null | required | the z step, or `null` |
| `z_unit` | string or null | required | the z step's unit text, or `null` |
| `display` | array of number or null | required | `[lo, hi]` or `null` |
| `channels` | array of [`VsiChannel`](#type-vsichannel) | required | the channels of the `c` dimension |
| `creation_time` | integer or null | required | `Creation time`, or `null` |
| `magnification` | number or null | required | `Objective magnification`, or `null` |
| `default_ifd` | integer or null | required | the `Default sample IFD`'s index, or `null` |
| `coarse_ifd` | integer or null | required | the `Coarse frame IFD`'s index, or `null` |
| `name` | string | required | the stack's name |
| `tile_file` | string | optional | a pyramid's: its tile file's key |
| `ets` | [`VsiEts`](#type-vsiets) | optional | a pyramid's: the tile file's headers |
| `levels` | array of array of integer | optional | a pyramid's: `[[w_l, h_l], ...]`, one per level |
| `tiles_outside` | integer | optional | a pyramid's: the tiles that are no chunk ([§4.1](#41-the-pyramid)) |
| `ifd` | [`VsiIfd`](#type-vsiifd) | optional | an IFD image's |

<a id="type-vsidimension"></a>**`VsiDimension`**, `{"coordinate": c, "meaning": m}`:

| member | type | presence | value |
|---|---|---|---|
| `coordinate` | integer | required | `c`, its coordinate in a tile record ([§2.2](#22-stacks)) |
| `meaning` | string | required | `m`, its meaning ([§2.2](#22-stacks)) |

<a id="type-vsichannel"></a>**`VsiChannel`**, `{"name", "channel_name", "color", "display",
"wavelength"}`, each `null` when absent:

| member | type | presence | value |
|---|---|---|---|
| `name` | string or null | required | the channel's name ([§2.2](#22-stacks)) |
| `channel_name` | string or null | required | its `Channel name` |
| `color` | string or null | required | its color, `RRGGBB` |
| `display` | array of number or null | required | its display limits, `[lo, hi]` |
| `wavelength` | number or null | required | its wavelength |

<a id="type-vsiets"></a>**`VsiEts`**, the tile file's headers ([§3](#3-the-tile-files)):

| member | type | presence | value |
|---|---|---|---|
| `key` | string | required | the tile file's key |
| `size` | integer | required | its size |
| `version` | integer | required | the SIS `version` |
| `coordinates` | integer | required | the SIS `coordinates` |
| `tiles` | integer | required | the SIS `tiles` |
| `ets_version` | integer | required | the ETS header's `version` |
| `pixel_type` | integer | required | the ETS `pixel_type` |
| `data_type` | string | required | its data type (the table `pixel_types`) |
| `samples` | integer | required | the samples per pixel, the ETS `channels` |
| `colorspace` | integer | required | the ETS `colorspace` |
| `compression` | integer | required | the ETS `compression` |
| `codec` | string | required | `raw`, `jpeg`, `jpeg2000` or `ljpeg` |
| `quality` | integer | required | the ETS `quality` |
| `tile_width` | integer | required | `tw` |
| `tile_height` | integer | required | `th` |
| `tile_depth` | integer | required | the ETS `tile_depth` |
| `background` | [`Base64`](../conventions.md#type-base64) | required | base64 of the 40 bytes |
| `component_order` | integer | required | the ETS `component_order` |
| `use_pyramid` | boolean | required | whether `use_pyramid` is set |
| `dimensions` | integer | required | the ETS `dimensions` |
| `dimension_sizes` | array of integer | required | the sizes listed |

<a id="type-vsiifd"></a>**`VsiIfd`**, an IFD image's IFD, `{"index", "width", "height",
"samples", "bits", "compression", "photometric"}`:

| member | type | presence | value |
|---|---|---|---|
| `index` | integer | required | its index in the chain |
| `width` | integer | required | `ImageWidth` |
| `height` | integer | required | `ImageLength` |
| `samples` | integer | required | `SamplesPerPixel` |
| `bits` | integer | required | `BitsPerSample` |
| `compression` | integer | required | `Compression` |
| `photometric` | integer | required | `PhotometricInterpretation` |

The tags' values are those of [§2.3](#23-the-tiff-images), absent tags having their TIFF
defaults.

<a id="type-vsimacro"></a>**`VsiMacro`**, the macro image's `S`: `name` and `ifd` alone:

| member | type | presence | value |
|---|---|---|---|
| `name` | string | required | `"macro image"` |
| `ifd` | [`VsiIfd`](#type-vsiifd) | required | its IFD |

### 6.2 The IR mirror

`refkv_source` is the input's **IR mirror** ([conventions §8](../conventions.md#8-the-ir-mirror)),
a **store IR** ([conventions §8.9](../conventions.md#89-stores)) whose objects
are the files the profile read: the `.vsi` file, at its key, and each
stack's tile file, at its key under the directories `_<name>_` and
`stack<N>`. Every object is **described in place**: its leaves partition it,
and its bytes stay in the file (the table holds none of them). The view,
`refkv_source/tree`, shows the headers, the tree's fields and values, the
IFDs' entries and the tile directories; tiles, strips and gaps are not
shown. A tile file the profile did not read (a `frame_t_0.ets` beside a
`frame_t.ets`, [§9.2](#92-the-tile-files)) is not an object.

### 6.3 Elements

Paths are relative to the IR's root ([conventions §8.1](../conventions.md#81-elements)),
whose children are the objects and directories; the paths below are
relative to each object. Types are those of [conventions §8.6](../conventions.md#86-types).

In the `.vsi` file:

| path | kind | type | extent: what |
|---|---|---|---|
| `header` | value | `{byte_order:ascii[2],magic:<u2,first_ifd:<u4}` | `(0, 8)`: the TIFF header |
| `tree` | struct | | `(8, size − 8)`: the root container |
| `.../header` | value | `{header_size:<u2,version:<u2,volume_version:<i4,data_offset:<i8,flags:<u4,reserved:<u4}` | a container's 24-byte header |
| `.../fields/<k>` | struct | | field `k` of a container: its region |
| `.../fields/<k>/header` | value | `{type:<u4,tag:<u4,next:<u4,size:<u4}`, or with `tag2:<u4` last | the field's header |
| `.../fields/<k>/value` | value | the field type's (`<i4[n]` for `n` `INT`s, `utf16[n]` for a text, `bytes[n]` for a type the table lacks or a length that is not a multiple of the type's) | a plain field's data, when it has any |
| `.../fields/<k>/container` | struct | | the container a volume, property set or mdim volume holds, with its `header` and `fields/<k>` |
| `.../fields/<k>/ifds/<j>` | struct | | IFD `j` of the chain, in the field's data region or the rest of its region: `entry_count` (`<u2`), `tags/<tag>` (a struct per entry, `tags/<tag>~<d>` for the `d`-th repeat of a tag: `entry`, its 12 bytes, or 8 and the inline `value` and `padding`; `value` for an out-of-line value), `next_ifd` (`<u4`), and `strips/<s>` or `tiles/<s>`, data elements, the strips |
| `.../fields/<k>/exif/<j>` | struct | | the EXIF IFD of IFD `j`, likewise (no strips) |
| `.../gaps/<offset>` | gap | | bytes of a region no element claims (padding, an IFD's out-of-line values outside its region) |

A strip's form is `{"geometry": {"shape": [rows, cols], "samples": S, "bits": b}, "codec": {"compression": C}, "recipe": R}`,
`R` `[["src", 0, null]]`, or, for a JPEG strip of over 2 bytes with a
prefix ([§4.3](#43-the-macro-image)), `[["src", 0, 2], ["shared", x], ["src", 2, null]]`,
`x` the prefix as a shared data source.

In a tile file:

| path | kind | type | extent: what |
|---|---|---|---|
| `header` | value | `{magic:ascii[4],header_size:<i4,version:<i4,coordinates:<i4,ets_offset:<i8,ets_size:<i4,reserved:<i4,tiles_offset:<i8,tiles:<i4,reserved2:<i4,size:<i8,reserved3:bytes[8]}` | `(0, 64)`: the SIS header |
| `ets` | value | `{magic:ascii[4],version:<i4,pixel_type:<i4,channels:<i4,colorspace:<i4,compression:<i4,quality:<i4,tile_width:<i4,tile_height:<i4,tile_depth:<i4,pixel_info_hints:<i4[17],background:bytes[40],component_order:<i4,use_pyramid:<i4,reserved:bytes[28],dimensions:<i4,dimension_sizes:<i4[10]}` | `(ets_offset, 228)`: the ETS header |
| `ets_extra` | value | `bytes[ets_size − 228]` | the rest of the ETS header, when `ets_size > 228` |
| `directory` | value | `{coordinates:<i4,coordinate:<i4[n],offset:<i8,size:<i4,index:<i4}[tiles]` | `(tiles_offset, tiles × (20 + 4n))`: the tile directory, when there are tiles |
| `tiles/<k>` | data | | tile `k` of the directory, `(offset, size)`; its form `{"geometry": {"shape": [th, tw], "samples": S, "bits": 8 × bytes}, "codec": {"compression": "raw" \| "jpeg" \| "jpeg2000" \| "ljpeg"}, "recipe": [["src", 0, null]]}` |
| `gaps/<offset>` | gap | | bytes no other element claims |

### 6.4 Equivalence

The elements are determined by the files. Hierarchies of one
slide are compared entry for entry, `refkv_source` included
([conventions §11.4](../conventions.md#114-equivalence)).

## 7. Example

The fixture `fixtures/vsi/vsi_raw_tzc.vsi`, served at `https://example.org/slides/vsi_raw_tzc.vsi`
with `_vsi_raw_tzc_/stack1/frame_t_0.ets` beside it, holds one stack,
`488/640_50um_1x`: 40 × 30 pixels of uncompressed `uint16`, 2 time points, 2
channels (`Cy5`, red, and `GFP`, green) and 3 z planes at 0.2 µm, a pixel
size of 0.065 µm, the origin (−66211, −41421) µm, one tile per plane, and
the 12-bit background 4095; and the macro image, a 40 × 30 JPEG. Its root
`zarr.json` has these attributes (`tree` elided):

```json
{
  "ome": {"version": "0.5", "bioformats2raw.layout": 3},
  "zarr_conventions": [{
    "uuid": "c0a1d4e6-5b7f-4f3a-9c2d-8e1f6a7b3c45",
    "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/zarrify/vsi/schema.json",
    "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md",
    "name": "refkv_zarrified",
    "description": "The Zarr layout of a VSI source zarrified by refkv, and the source's metadata"
  }],
  "refkv_zarrified": {
    "profile": "vsi",
    "version": 0,
    "source": {"url": "https://example.org/slides/vsi_raw_tzc.vsi"},
    "vsi": {
      "file": {"key": "vsi_raw_tzc.vsi", "size": 6822, "ifds": 2},
      "stacks": [{"id": 1, "name": "488/640_50um_1x", "type": 0, "external": true,
                  "width": 40, "height": 30, "tile_file": "_vsi_raw_tzc_/stack1/frame_t_0.ets"}],
      "tree": {"fields": [
        {"tag": 2, "type": 1, "fields": [
          {"tag": 11, "type": 8192, "name": "Document Name", "value": "vsi_raw_tzc"},
          {"tag": 34, "type": 8192, "name": "Product Name", "value": "synthetic cellSens"}]},
        {"tag": 2000, "tag2": 0, "type": 0, "name": "Collection volume", "fields": ["..."]}]}
    }
  }
}
```

`OME/zarr.json` has `{"version": "0.5", "series": ["0", "1"]}`. The group `0`
has:

```json
{
  "ome": {
    "version": "0.5",
    "multiscales": [{
      "name": "488/640_50um_1x",
      "axes": [
        {"name": "t", "type": "time"},
        {"name": "c", "type": "channel"},
        {"name": "z", "type": "space", "unit": "micrometer"},
        {"name": "y", "type": "space", "unit": "micrometer"},
        {"name": "x", "type": "space", "unit": "micrometer"}
      ],
      "datasets": [{"path": "0", "coordinateTransformations": [
        {"type": "scale", "scale": [1, 1, 0.2, 0.065, 0.065]},
        {"type": "translation", "translation": [0, 0, 0, -41421.0, -66211.0]}
      ]}]
    }],
    "omero": {"channels": [
      {"label": "Cy5", "color": "FF0000", "active": true, "window": {"min": 0.0, "max": 65535.0, "start": 88.0, "end": 169.0}},
      {"label": "GFP", "color": "00FF00", "active": true, "window": {"min": 0.0, "max": 65535.0, "start": 86.0, "end": 170.0}}
    ]}
  },
  "zarr_conventions": ["..."],
  "refkv_zarrified": {"vsi": {
    "id": 1, "type": 0, "type_name": "Default image", "width": 40, "height": 30,
    "dimension_sizes": [2, 3, 2],
    "dimensions": [{"coordinate": 2, "meaning": "t"}, {"coordinate": 3, "meaning": "z"}, {"coordinate": 4, "meaning": "c"}],
    "scale": [0.065, 0.065], "unit": "10^-6m^1", "origin": [-66211.0, -41421.0],
    "z_step": 0.2, "z_unit": "10^-6m^1", "display": [91.0, 138.0],
    "channels": [
      {"name": "Cy5", "channel_name": "488/640_50um_1x", "color": "FF0000", "display": [88.0, 169.0], "wavelength": 670.0},
      {"name": "GFP", "channel_name": "488/640_50um_1x", "color": "00FF00", "display": [86.0, 170.0], "wavelength": 508.0}
    ],
    "creation_time": 1700000001, "magnification": 40.0, "default_ifd": null, "coarse_ifd": 1,
    "name": "488/640_50um_1x",
    "tile_file": "_vsi_raw_tzc_/stack1/frame_t_0.ets",
    "ets": {"key": "_vsi_raw_tzc_/stack1/frame_t_0.ets", "size": 29620, "version": 3, "coordinates": 6, "tiles": 12,
            "ets_version": 196614, "pixel_type": 4, "data_type": "uint16", "samples": 1, "colorspace": 1,
            "compression": 0, "codec": "raw", "quality": 90, "tile_width": 40, "tile_height": 30, "tile_depth": 1,
            "background": "/w8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==", "component_order": -1,
            "use_pyramid": false, "dimensions": 5, "dimension_sizes": [40, 30, 2, 3, 2]},
    "levels": [[40, 30]], "tiles_outside": 0
  }}
}
```

Its array `0/0` has the shape `[2, 2, 3, 30, 40]`, the chunk shape
`[1, 1, 1, 30, 40]`, `uint16`, the fill value 4095 and the codec `bytes`
little-endian; the chunk `0/0/c/0/0/0/0/0` is the 2400 bytes at offset 292
of the tile file, and `0/0/c/0/0/1/0/0` the 2400 at 2692. The group `1` is
the macro image: `c`, `y`, `x`, `[3, 30, 40]`, `uint8`, `transpose` then
`imagecodecs_jpeg`, one chunk of three parts (the SOI, the shared prefix
with the Adobe marker and the JPEG tables, and the rest of the strip).

## 8. What the layout does not express

This section is informative. The layout keeps what OME-NGFF 0.5 can place:
each stack's pixels with a pixel size, a position and channel colors. The
rest of what cellSens records is in `tree`, in each image's source metadata,
or in the mirror.

- **The relation between stacks.** An overview (type 1), the default image
  (type 0) it was the preview of, a focus map (type 4), a sample mask (2)
  and the EFI maps (8, 16, 32) are separate images, placed by their origins
  on one stage; the layout does not say which overview belongs to which
  scan. `type` and `type_name` say what each is.
- **The coordinate values** of a dimension: `Z start`, `Time start` and
  the per-plane values a dimension description holds are not applied (the
  `z` scale is the z step alone, `t` has none); the channel wavelengths are
  recorded, not expressed.
- **Dimensions the axes cannot hold.** A `lambda`, `phase` or unknown
  dimension is accepted only when its size is 1 ([§9.3](#93-tiles-and-levels)).
- **The thumbnails.** Each stack's coarse frame IFD (a small JPEG of the
  stack) is in the mirror but is no image: the pyramid's levels serve.
- **Display.** The stack's and channels' display limits become windows;
  the LUT itself (a 256-entry colormap, of which the layout keeps the last
  entry as the channel's color), the default background color (2034, the
  stack's) and the magnification are recorded only.
- **Document properties**: the name, author, creation time, the product
  and its version, the objective and camera settings, and every other tag:
  in `tree`, by number and, where Bio-Formats names them, by name.
- **The tile index** of each record and the file's `colorspace` and
  `quality`: in the mirror and `ets`.

# Part 2. The profile

The VSI profile of [spec/zarrify.md](../zarrify.md) is [§9](#9-vsi-profile) of this document. What every profile's Part 2 shares is in
[spec/zarrify.md, Profiles](../zarrify.md#profiles). The input is the slide's files,
and the profile also says which of them are read.

## 9. VSI profile

The input is a **file input** that names the `.vsi` file (a URL, or a local
path), which its **signature** ([spec/zarrify.md §1.2](../zarrify.md#12-input), tested at the precedence of
[profiles/vsi.json](profiles/vsi.json)) chose: bytes 0–3 of `H`
are `49 49 2A 00` (a little-endian classic TIFF) and bytes 8 to 11 `18 00 49 53` (a container
header's size, 24, and version, 21321; [the convention §2.1](#21-containers-and-fields)),
tested before the TIFF profile's. The profile reads it as a **store**
([§1.4](../zarrify.md#14-store-inputs)) whose URL is the `.vsi` file's
directory (the URL up to and including its last `/`; for a local path, the
URI reference of its directory, ending in `/`, or `--url`'s directory when
`--url` names the `.vsi` file; a bare file name with no `--url` records the
empty store URL, so that the keys name the files relative to the archive),
and whose objects are the `.vsi` file, at its key (its name, percent-decoded
from the URL), and the tile files the profile finds. The store is not listed
(a plain web server cannot list a directory): its objects are **probed** by
key ([§9.2](#92-the-tile-files)). The root's `source.url` is the `.vsi`
file's object URL. The `.vsi` file's key must end in `.vsi` (in any case)
and hold no `/`; its URL must have no query or fragment.

### 9.1 The .vsi file

The `.vsi` file is read **whole**, in one request. It is rejected when it is
shorter than 32 bytes or larger than 2^28 bytes (256 MiB; a slide's metadata
is a few megabytes), when its byte order mark is not `II` or its magic not
42, or when its first IFD offset plus 6 exceeds its size.

**The tree** ([the convention §2.1](#21-containers-and-fields)) is parsed
from offset 8 to the file's end, every container and field checked in order:

- a container is rejected when its 24-byte header does not fit its region,
  when `header_size` is not 24 or `version` not 21321, when containers nest
  more than 64 deep, when its field count is over 2^20, when `data_offset`
  is not 24 (or 0 with no fields), or when its fields' headers (16 bytes
  each) do not fit before its region's end;
- a field is rejected when its header runs past the container's region;
  when `next` is 0 and it is not the last field, or not 0 and it is; when
  `fp + next` is before the field's header's end or past the container's
  region; when its data ends before its header or past its own region; when
  it is extended and its type is not 0, 1, 2 or 10 (the schema's
  `extended_types`); or when it is a property set or an mdim volume whose
  `size` is not 0 (their containers fill their regions).

**The chain** ([the convention §2.3](#23-the-tiff-images)) is read from the
first IFD offset: an IFD whose entries and next-offset word do not fit in
the file is rejected, as are more than 65536 IFDs (EXIF IFDs counted) and a
chain that returns to an IFD already read (a next offset or an EXIF offset
seen before). An entry's out-of-line value that lies outside the file is not
an element (the entry's 12 bytes are). An IFD that lies in no TIFF region
of the tree is read but has no elements; the profile rejects the input only
when such an IFD is one it uses as an image.

**The stacks** are found as [the convention §2.2](#22-stacks) says. A stack
that has a tile file but no image boundary (a width and height of at least
1) is rejected. A stack of the collection volume that is not a volume field
of tag 2001 is not a stack; a stack whose dimension descriptions name
coordinates the tile records lack is accepted (those dimensions have the
size 1, [§9.3](#93-tiles-and-levels)).

### 9.2 The tile files

For each stack that has a tile file, in the tree's order, the profile
**probes** two keys, in this order: `_<base>_/stack<id>/frame_t.ets` and
`_<base>_/stack<id>/frame_t_0.ets`, where `<base>` is the `.vsi` file's key
without its last four characters and `<id>` the stack's id in decimal. All
probes of all stacks are one batch. A probe asks the host whether the object
exists and how large it is: a local file that is there, or an HTTP object
whose response to the first request is not a 404 (a `HEAD` or a ranged `GET`
under the reader policy; a 404 is **absent**; any other failure is a
failure, not a rejection). The first candidate found, in the order above,
is the stack's **tile file**, an object of the store at that key; a
candidate found after one that was found is neither read nor an object (the
mirror does not list it). A stack none of whose candidates exists is
rejected: `stack <id> (<name>) has no tile file: neither
_<base>_/stack<id>/frame_t.ets nor _<base>_/stack<id>/frame_t_0.ets exists`.

The hosts drive this through the core's store run: the run's `poll` returns
an empty read list while probes are pending, the host answers each key with
its size or none (`probes` and `found` of the Python, TypeScript and Rust
bindings), and the run goes on. Both implementations open each object
through a transport of its own (a mapped local file, or an http(s) object
under the reader policy), reading ranges from then on.

Each tile file is then read in two batches across every file: its first 64
bytes (the SIS header), then its ETS header (`ets_size` bytes at
`ets_offset`) and its tile directory (`tiles × (20 + 4n)` bytes at
`tiles_offset`), both in one batch. A tile file shorter than 64 bytes is
rejected, and so is one whose SIS magic is not `SIS`, whose `header_size`
is not 64, whose `coordinates` are fewer than 2 or more than 16, whose ETS
header is outside the file or shorter than 228 bytes, whose tile count is
negative or over 2^24, or whose directory is outside the file. Its ETS
header is rejected when its magic is not `ETS`, its pixel type is not in
`pixel_types`, its `channels` are not 1, 3 or 4, its compression is 8 or 9
(named in the rejection as PNG or BMP) or any other value
than 0, 2, 3 or 5, its tiles are not 1 to 65535 pixels on each side, its
`tile_depth` is not 1, or its `dimensions` are 1 or more than
`coordinates` (0, as a version 0x30003 header has, lists no sizes: every
coordinate but the column, the row and the level then has the size 1).

### 9.3 Tiles and levels

The coordinates of a tile record are read as [the convention §3](#3-the-tile-files)
says: the column, the row, the stack's dimensions by coordinate, and the
level last when `use_pyramid` is set. Each dimension of the stack
([the convention §2.2](#22-stacks)) is checked: its size (the ETS header's
for its coordinate; 1 for a coordinate the records lack or the level's) is
at least 1, and its meaning is `z`, `t` or `c`, or its size is 1 (a
`lambda`, `phase` or unknown dimension of several values is rejected: the
layout cannot place it). A stack with several samples per pixel and a `c`
dimension of several channels is rejected.

Each tile is checked: its `size` is at least 1 and it lies within the file;
each coordinate is 0 or more and, for a coordinate that is not the column,
the row or the level, less than its dimension's size; no earlier tile has
the same coordinates; and an uncompressed tile has exactly
`tw × th × channels × bytes` bytes. A compressed tile's codestream is not
read: the output does not depend on pixel data
([§1.2](../zarrify.md#12-input)).

The levels and their sizes are those of [the convention §4.1](#41-the-pyramid);
a tile outside its level is no chunk, and counted. A tile the directory
lacks is no chunk: the array's fill value stands for it.

### 9.4 Chunks

Every image's chunk ([the convention §4.1](#41-the-pyramid), [§4.3](#43-the-macro-image))
references the files; the output copies no pixels. A pyramid's chunk is one
range of its tile file's object: the tile's `size` bytes at its `offset`,
referenced as [conventions §7](../conventions.md#7-source-values-as-arrays)
references a chunk of a store's object. An IFD image's chunk is, for JPEG,
three parts (the strip's first 2 bytes, the prefix as a data source, the
rest of the strip), or the strip alone, and for an uncompressed image one
range per run of contiguous strips, of the `.vsi` file's object. A
reference that does not fit in a payload ([§1.2](../zarrify.md#12-input))
rejects the input (an uncompressed IFD image of thousands of strips). The
output's data sources are the distinct JPEG prefixes (one per IFD image
with a prefix, shared when equal). The output's source table is one `url` source per object a
reference uses (its object URL, with a `size` pin), in order of first use, then the data sources
in order of first use (the references taken in the order of their keys).

<a id="type-vsitilechunk"></a>**`VsiTileChunk`**, a pyramid's chunk, `i` the `url` source of its
tile file's object:

| part | range | presence | value |
|---|---|---|---|
| the tile | source range `(i, offset, size)` | required | the tile's `size` bytes at its `offset` in the tile file |

<a id="type-vsiifdchunk"></a>**`VsiIfdChunk`**, an IFD image's chunk, `i` the `url` source of the
`.vsi` file's object, a strip being `n` bytes at `o` in it; exactly one case applies:

| part | range | presence | value |
|---|---|---|---|
| the strip's first 2 bytes (SOI) | source range `(i, o, 2)` | optional | JPEG, with a prefix and a strip of more than 2 bytes |
| the prefix | data source `j`, whole: source range `(j, 0, p)` | optional | likewise: the prefix ([the convention §4.3](#43-the-macro-image)), `p` bytes, a data source shared by the IFD images whose prefixes are equal |
| the rest of the strip | source range `(i, o + 2, n − 2)` | optional | likewise |
| the strip | source range `(i, o, n)` | optional | JPEG, when the prefix is empty or the strip is 2 bytes or fewer |
| a run of contiguous strips | source range `(i, o, m)`, `o` the run's first strip's offset and `m` the run's length | optional | uncompressed: one range per run, in order |

`refkv_source` is the IR mirror ([the convention §6.2](#62-the-ir-mirror)).
The archive has no other entries than the root's, the `OME` group's, each
image's and each level's `zarr.json`, the chunks, and the mirror's.

### 9.5 Summary

This section is informative. Both implementations print a one-line JSON
summary, with these members:

| member | value |
|---|---|
| `images` | one object per image (below) |
| `chunks` | the number of all images' chunks |
| `dataSources` | the data sources |
| `ifds` | every IFD read, EXIF included |
| `tileFiles` | the tile files |
| `tiles` | every record |
| `batches` | the read batches, the probe batch included |
| `listingRequests` | the objects opened beyond the `.vsi` file |

Each image's object, by whether the image is a pyramid ([the convention §4.1](#41-the-pyramid)) or an
IFD image ([the convention §4.3](#43-the-macro-image)); an IFD image's has no `sizes`, `grids` or
`tilesOutside`:

| member | a pyramid's | an IFD image's |
|---|---|---|
| `name` | its name | its name |
| `kind` | `"stack"` | `"stack"`, or `"macro"` for the macro image |
| `source` | `"tiles"` | `"ifd"` |
| `id` | its stack's id | its stack's id; `null` for the macro image |
| `dataType` | its data type | its data type |
| `samples` | its samples per pixel | its samples per pixel |
| `codec` | its codec | `"jpeg"` for `Compression` 7, else `"raw"` |
| `axes` | its axes | its axes |
| `sizes` | `t`, `c`, `z` | absent |
| `levels` | `[w, h]` per level | `[[w, h]]`, its one array's |
| `grids` | `[columns, rows]` per level | absent |
| `chunks` | the number of its chunks | 1 |
| `tilesOutside` | its tiles outside their levels | absent |
