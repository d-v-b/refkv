# LIF

The Zarr layout of a Leica LIF file (the Leica Image File that LAS X and LAS AF write,
versions 1 and 2), and the translation of its XML header into JSON. What all of refkv's
conventions share is in [spec/conventions.md](../conventions.md), cited here as "conventions
§n". How refkv produces this layout as a reference store is the LIF profile,
[Part 2](#part-2-the-profile) below, cited as "the profile §n".

Convention version: 0 (until release, [conventions §1](../conventions.md#1-conventions)) · UUID: `b099ab1f-f9d4-4276-9006-9ff1193a34b2` ·
Schema: [schema.json](lif/schema.json) · Changed 2026-10-26, with no change to any output: the
layout is tabulated, its entries in [Layout](#layout) and its types in type tables
([conventions §11.5](../conventions.md#115-layout-tables)), and the blocks' fields, the
dimensions' and channels' attributes, the data types and the channels' colors are tables;
the prose also gives an image's source metadata members in the order refkv already wrote
them, `tile_scan` before `field` and `tile` ([§5.2](#52-the-images))

This document has two parts ([conventions §11.1](../conventions.md#111-parts)).

# Part 1. The convention

This convention gives a layout only to the files that meet its requirements. Where it says
that "the input is rejected", or that something MUST hold, a file that fails has no layout
under this convention, and the profile rejects it.

Every convention is held to reconstruction and efficiency (conventions, introduction). For
LIF this means: every block the file holds (the XML header, every memory block with its ID and
data, those of no image included) and the bytes after the last block can be rebuilt from the
hierarchy, byte for byte (its IR mirror, [§5.3](#53-the-ir-mirror)); each image's elements of
the XML, its dimensions and channels, are on its group as JSON; and no document grows with the
number of planes: an image's documents grow with its dimensions and channels only, which the
profile bounds ([the profile §7.3](#73-rejection)).

## Layout

The hierarchy's entries ([conventions §11.5](../conventions.md#115-layout-tables)), a
bioformats2raw collection ([conventions §4](../conventions.md#4-images), whose
[entries](../conventions.md#table-collection) these rows refine) of the images' fields, one
series each:

| prefix | type | structure | description |
|---|---|---|---|
| `zarr.json` | group | [`GroupDocument`](../conventions.md#type-groupdocument), attributes a [`CollectionRoot`](../conventions.md#type-collectionroot) | the root ([§4.1](#41-the-hierarchy)) |
| `zarr.json#/attributes/ome` | attribute | `{"version": "0.5", "bioformats2raw.layout": 3}` | when the file has a series; absent for a file without images (only folders, or FLIM data), which then has no `OME` group |
| `zarr.json#/attributes/zarr_conventions` | attribute | array of [`ConventionMetadata`](../conventions.md#type-conventionmetadata) | the CMO of [§1](#1-declaration) |
| `zarr.json#/attributes/refkv_zarrified` | attribute | [`RootProperty`](../conventions.md#type-rootproperty) | `profile` `"lif"`, `version` `0`, `source.url` the file's URL |
| `zarr.json#/attributes/refkv_zarrified/lif` | attribute | [`LifRoot`](#type-lifroot) | the root's source metadata ([§5.1](#51-the-root)) |
| `OME/zarr.json` | group | [`GroupDocument`](../conventions.md#type-groupdocument), attributes a [`SeriesGroup`](../conventions.md#type-seriesgroup) | when the file has a series: `series` `"0"`, `"1"`, ..., one per series |
| `<s>/zarr.json` | group | [`ImageGroup`](../conventions.md#type-imagegroup) | series `s` ([§4.2](#42-images)) |
| `<s>/zarr.json#/attributes/ome` | attribute | [`OmeImage`](../conventions.md#type-omeimage) | `M`: one level, the array `0`, with `name`, scales and `omero` as [§4.2](#42-images) says, and no translation |
| `<s>/zarr.json#/attributes/zarr_conventions` | attribute | array of [`ConventionMetadata`](../conventions.md#type-conventionmetadata) | the CMO of [§1](#1-declaration): every image group declares the convention |
| `<s>/zarr.json#/attributes/refkv_zarrified` | attribute | [`NodeProperty`](../conventions.md#type-nodeproperty) | |
| `<s>/zarr.json#/attributes/refkv_zarrified/lif` | attribute | [`LifImage`](#type-lifimage) | the series' source metadata ([§5.2](#52-the-images)) |
| `<s>/0/zarr.json` | array | [`LifArray`](#type-lifarray) | the series' array ([§4.3](#43-image-arrays)); attributes `{}` |
| `<s>/0/c/<coords>` | chunk | [`LifChunk`](#type-lifchunk) | a band of a plane ([§4.3](#43-image-arrays)); every chunk is present |
| `refkv_source/**` | subtree | [the mirror's entries](../conventions.md#table-mirror) | the IR mirror ([§5.3](#53-the-ir-mirror)): its elements are [§5.4](#54-elements)'s; the convention writes no arrays of its own under it |

| placeholder | value |
|---|---|
| `<s>` | a series' index, in decimal, from 0: image 0's fields in order, then image 1's, and so on ([§4.1](#41-the-hierarchy)) |
| `<coords>` | the chunk's coordinates along the array's axes, in decimal, one key segment each: `<t>/<c>/<z>/<j>/0`, each of `t`, `c` and `z` only for the axes present (so it stands for two to five segments); `j` the band |

## 1. Declaration

The root and `refkv_source` declare the convention
([conventions §11.2](../conventions.md#112-declaration)), the root with the source metadata of
[§5.1](#51-the-root) as the member `"lif"`. Its CMO is:

```json
{
  "uuid": "b099ab1f-f9d4-4276-9006-9ff1193a34b2",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/zarrify/lif/schema.json",
  "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md",
  "name": "refkv_zarrified",
  "description": "The Zarr layout of a LIF source zarrified by refkv, and the source's metadata"
}
```

Every image group ([§4.2](#42-images)) declares it too, with `{"lif": S}`, `S` its source
metadata ([§5.2](#52-the-images)), a [`LifImage`](#type-lifimage). No other node declares it:
not the image arrays, nor `OME`.

## 2. The source

All integers are little-endian, `u8`, `u32` and `u64` unsigned. Offsets are from the start of
the file.

### 2.1 Blocks

A LIF file is a sequence of **blocks**. The first, at offset 0, is the **header block**:

| bytes | field |
|---|---|
| 0–3 | `u32` test value `0x70` |
| 4–7 | `u32` length (not read; writers set it to `2c + 5`) |
| 8 | `u8` magic byte `0x2A` |
| 9–12 | `u32` `c`, the XML header's characters |
| `2c` bytes from 13 | the XML header ([§2.2](#22-the-xml-header)), UTF-16LE |

The test value and the magic byte MUST be these, and the XML header MUST lie within the file.

After it come the **memory blocks**, from offset `13 + 2c`. A memory block is its **fixed
part** of `H` bytes, then its **ID** and its **data**. The data size `D` is a `u32` in a file of
version 1 and a `u64` in one of version 2 ([§2.2](#22-the-xml-header)), so `H` is 18 or 22:

| bytes, version 1 | bytes, version 2 | field |
|---|---|---|
| 0–3 | 0–3 | `u32` test value `0x70` |
| 4–7 | 4–7 | `u32` length (not read) |
| 8 | 8 | `u8` magic byte `0x2A` |
| 9–12 | 9–16 | the data size `D` |
| 13 | 17 | `u8` magic byte `0x2A` |
| 14–17 | 18–21 | `u32` `m`, the ID's characters |
| `2m` bytes from 18 | `2m` bytes from 22 | the **ID**, UTF-16LE |
| `D` bytes from `18 + 2m` | `D` bytes from `22 + 2m` | the **data** |

Memory block `i` is the `i`-th the **walk**
finds ([the profile §7.2](#72-reading)), from 0, in file order. Bytes after the last block are
the **tail**.

### 2.2 The XML header

The XML header's bytes MUST be well-formed UTF-16LE (a code unit of an unpaired surrogate
rejects the input); its text is read by the tag scan of [the TIFF convention §3](tiff.md#3-ome-xml)
(skipped sections, tags, and attribute values with their references decoded), and its tags
nest into elements as [the CZI convention §2.7](czi.md#27-the-metadata-xml) says: an element's
**children** are those opened while it is the innermost open one, and the **root** is the first
element at depth 0, self-closing or not. Elements are named by their local name. A path `A/B` names an element's
first child `A` and that child's first child `B`.

The root MUST be named `LMSDataContainerHeader` (a LIFEXT file's root,
`LMSDataContainerEnhancedHeader`, is rejected), and its attribute `Version` MUST be an integer,
1 or 2: the file's **version**, which gives the width of the memory blocks' data sizes. An XML
header whose text is `LMS_Object_File` is a Leica object file's (LOF), which is rejected.

An attribute's **integer** is its value with whitespace removed from both ends, one or more
digits of a value at most 2^53 − 1; its **decimal** is that value when it matches the decimal of
[the CZI convention §2.7](czi.md#27-the-metadata-xml) and is finite.

### 2.3 Images

The **elements** are the elements named `Element` in the root's subtree, in document order:
the file's own, its folders, and its images. An **image** is an element with a path
`Data/Image`; the images are numbered `k` from 0 in document order. An element with no image,
such as a folder, or FLIM data (`Data/SingleMoleculeDetection`), has no layout: its memory
block is kept as bytes ([§5.4](#54-elements)).

An image's **path** is the attribute `Name` of each element from the file's own element to the
image, in that order (an element without one: `null`). Its memory block is named by the
attribute `MemoryBlockID` of its first child `Memory`, which MUST be present: the memory block
whose ID's code units are the attribute value's ([§2.1](#21-blocks)). Exactly one memory block
MUST have that ID, and two images MUST NOT name one block. (LAS X gives elements without data,
such as folders, IDs it writes no block for, or the ID of one block of size 0; they name no
image's block.)

### 2.4 Dimensions and channels

An image's **dimensions** are the children `DimensionDescription` of its
`Data/Image/ImageDescription/Dimensions`, 1 to 16 of them, in document order; its **channels**
the children `ChannelDescription` of `Data/Image/ImageDescription/Channels`, 1 to 1024.

A dimension's attributes:

| attribute | value |
|---|---|
| `DimID` | MUST be an integer (below) |
| `NumberOfElements` | MUST be an integer, at least 1 |
| `BytesInc` | MUST be an integer: the bytes from one element to the next |
| `BitInc` | when present, MUST be the integer 0 (bit-packed data cannot be referenced as bytes) |
| `Origin` | its first element's position |
| `Length` | the distance from its first element to the last |
| `Unit` | their unit |

A dimension of one element is **unused**; the others are **used**, and no two used dimensions
MUST have one `DimID`.

A channel's attributes:

| attribute | value |
|---|---|
| `DataType` | MUST be an integer: 0, unsigned integer; 1, floating point |
| `Resolution` | MUST be an integer: bits, 1 to 64; more than 8 for a float |
| `BytesInc` | MUST be an integer: where its samples start |
| `BitInc` | when present, MUST be 0 |
| `ChannelTag` | names its color: 0 gray, 1 red, 2 green, 3 blue |
| `LUTName` | names its color |

A dimension's `DimID` gives its axis:

| `DimID` | dimension | axis |
|---|---|---|
| 1 | X | `x` |
| 2 | Y | `y` |
| 3 | Z | `z` |
| 4 | T | `t` |
| 5, 9 | emission, excitation wavelength | a field ([§3.3](#33-axes-and-fields)) |
| 6, 7, 8, 11 | rotation, XT slices, T slices, loop | a field |
| 10 | mosaic tile (LAS X's stage position) | a field: a tile ([§4.4](#44-mosaics)) |
| other | | a field |

## 3. Placement

### 3.1 Data types

A channel's **item size** `e` and data type, by its `Resolution` and `DataType`:

| `Resolution` | item size `e` | data type, `DataType` 0 | data type, `DataType` 1 |
|---|---|---|---|
| 1–8 bits | 1 byte | `uint8` | rejected |
| 9–16 bits | 2 bytes | `uint16` | `float16` |
| 17–32 bits | 4 bytes | `uint32` | `float32` |
| 33–64 bits | 8 bytes | `uint64` | `float64` |

(So 12 bits are `uint16`, and a float of 8 bits is rejected.) Every channel of an image MUST
have one data type, the image's. Samples are little-endian.

### 3.2 Channels

An image's channels are taken in ascending order of their `BytesInc` (two channels of one
`BytesInc` reject the input): channel `c`, from 0, has the `c`-th, `o_c`. The image MUST have a
dimension of `DimID` 1: its **X** is the used one, else the first, of `BytesInc` `s_x`, and its
channels are:

| channels | when | samples |
|---|---|---|
| **planar** | `s_x = e` | each channel's samples are an image of their own, from `o_c` |
| **interleaved** | there are `n > 1` channels, `s_x = n × e` and `o_c = c × e` | a pixel holds one sample of each channel, in order (RGB data, as LAS X writes it) |
| (none) | otherwise | the input is rejected |

The sample of channel `c` at the index `i_d` along each dimension `d` is at `o_c + Σ i_d × s_d`
bytes into the data of the image's memory block, `s_d` the dimension's `BytesInc`. The image's
**extent** is `O + Σ (N_d − 1) × s_d + e` over its dimensions (`N_d` its `NumberOfElements`,
`O` the largest `o_c`), computed exactly, which MUST be at most its block's data size `D`.

### 3.3 Axes and fields

The image's axes ([conventions §3](../conventions.md#3-arrays)) are `t` when its dimension of
`DimID` 4 is used, `c` when it has more than one channel, `z` when its dimension of `DimID` 3 is
used, then `y` and `x`. Its shape is `N_T`, the number of channels, `N_Z`, `N_Y` and `N_X` (each
for its axis present; `N_Y` is 1 when the image has no used dimension of `DimID` 2, as a line
scan over time, `x` and `t`, has none). Each number of the shape MUST be at most 2^31.

Its **field dimensions** are its used dimensions of any other `DimID`, in document order,
`d_1, …, d_q`, and its **fields** the combinations of their indexes: field `f = i_1 + N_1 × (i_2 +
N_2 × (…))` (the first fastest) has the **offset** `Σ i_j × s_j`; there are `F = N_1 × … × N_q`
of them, one when `q` is 0, at most 65536. The image's **planes** are `N_T × n' × N_Z × F`
(`n'` the number of channels, or 1 when they are interleaved: a band of each is a chunk), at
most the references a run makes, `2^22 + 4 ×` the file's size: a dimension whose `BytesInc`
is 0 repeats a plane without growing the extent. Each field is an image of the output
([§4.2](#42-images)): OME-NGFF 0.5 has no axis for a mosaic's tile, a wavelength or a rotation.

### 3.4 Rows and bands

A row is `R = N_X × s_x` bytes. When Y is used, its `BytesInc` `s_y` MUST be at least `R`; the
rows are **packed** when it is `R`, or Y unused, and **padded** otherwise (stride-aligned RGB
rows), the bytes between them in no chunk.

A chunk holds a **band** of `b` rows of one plane. `b` is the largest divisor of `N_Y` such that
`b × R ≤ 2^24` and, for padded rows, a band of `b` rows (`b` ranges of `R` bytes, `s_y` apart)
starting at the farthest band's offset ([the profile §7.4](#74-chunk-references): the last band
of the plane of the last time, channel, depth and field) has a reference payload of at most
65519 bytes ([spec/zarrify.md §1.2](../zarrify.md#12-input)); at least 1. So every chunk is one
range of its block, or for padded rows the ranges of its rows, and holds at most 16 MiB
(or one row).

## 4. Output

### 4.1 The hierarchy

The nodes, whose entries are listed in [Layout](#layout):

| node | when | what |
|---|---|---|
| `zarr.json` | always | the root group, declaring the convention ([§1](#1-declaration)). When the file has a series (below), its attributes have `"ome": {"version": "0.5", "bioformats2raw.layout": 3}` |
| `OME/zarr.json` | the file has a series | a group with `{"ome": {"version": "0.5", "series": ["0", "1", ...]}}`, one per series |
| `<s>/zarr.json` and its array `<s>/0` | for each series | series `s`, an image ([§4.2](#42-images), [§4.3](#43-image-arrays)) |
| `refkv_source` | always | the source metadata ([§5](#5-source-metadata)) |

The **series** are the fields of the images, numbered from 0: image 0's fields in order, then
image 1's, and so on. A file without images (only folders, or FLIM data) is accepted: its
output is the root and `refkv_source`.

### 4.2 Images

Series `s`, field `f` of image `k`, is an image ([conventions §4](../conventions.md#4-images))
of one dataset, `"0"`, and no translation. Its source metadata is [§5.2](#52-the-images)'s, a
[`LifImage`](#type-lifimage). Its `name`, by whether the image's `Name` is present, not empty
and at most 256 bytes in UTF-8 (a **valid** `Name`), and by its field dimensions, `<f>` in
decimal:

| the image's `Name` | field dimensions | `name` |
|---|---|---|
| valid | none | the `Name` |
| valid | its only field dimension is of `DimID` 10 | the `Name` followed by ` (tile <f>)` |
| valid | some, other than a single one of `DimID` 10 | the `Name` followed by ` (field <f>)` |
| not valid | its only field dimension is of `DimID` 10 | `tile <f>` |
| not valid | some, other than a single one of `DimID` 10 | `field <f>` |
| not valid | none | no `name` |

**Scale.** `c` is 1. Along `x`, `y`, `z` and `t`, the dimension of its `DimID` (1, 2, 3, 4)
that is used gives the step `|L / (N − 1)|` (binary64: the division, then the magnitude), `L`
its `Length` (a decimal) and `N` its `NumberOfElements`; the scale is 1, without a unit, when
the dimension is unused, `Length` is not a decimal, or the step is not finite and positive.
Otherwise:

| axis | `Unit` | scale | unit |
|---|---|---|---|
| `x`, `y`, `z` | a length symbol of [conventions §5](../conventions.md#5-units) | the step converted to micrometers, `step × (size(Unit) / 1e-6)` (`m`, as LAS X writes, gives `step × (1 / 1e-6)`), when finite and positive; else 1 | `micrometer`; none when the converted step is not finite and positive (scale 1) |
| `t` | a time symbol of [conventions §5](../conventions.md#5-units) (`s`, `ms`, `min`, `h`) | the step | that unit |
| any | otherwise | the step | none |

Origins are not translations: each image keeps its own (its dimensions'), and a mosaic's tiles
their stage positions, in its source metadata ([§4.4](#44-mosaics)).

**omero.** When the image has at most 64 channels, `M` has `"omero": {"channels": [...]}`, one
[`OmeroChannel`](../conventions.md#type-omerochannel) per channel `c`:

| member | value |
|---|---|
| `label` | `C<c>` |
| `color` | by its `ChannelTag`, else by its `LUTName` (below) |
| `active` | `true` |
| `window` | for an unsigned type, `{"min": 0, "max": V, "start": 0, "end": V}`, `V = 2^b − 1`, `b` the channel's `Resolution` (4095 for 12-bit data in `uint16`); no window for a float |

The color is `FF0000`, `00FF00` or `0000FF` for a `ChannelTag` of 1, 2 or 3, else by its
`LUTName`, compared in ASCII lower case:

| `LUTName` | color |
|---|---|
| `red` | `FF0000` |
| `green` | `00FF00` |
| `blue` | `0000FF` |
| `cyan` | `00FFFF` |
| `magenta` | `FF00FF` |
| `yellow` | `FFFF00` |
| anything else | `FFFFFF` |

### 4.3 Image arrays

The array of series `s`, at `<s>/0` ([conventions §3](../conventions.md#3-arrays)):

<a id="type-lifarray"></a>**`LifArray`**, the array's `zarr.json`: an
[`ArrayDocument`](../conventions.md#type-arraydocument) with these members:

| member | type | presence | value |
|---|---|---|---|
| `shape` | array of integer | required | the image's ([§3.3](#33-axes-and-fields)) |
| `data_type` | string | required | the image's ([§3.1](#31-data-types)) |
| `chunk_grid` | object | required | regular, its chunk shape 1 for `t` and `z`, the number of channels for `c` when they are interleaved and 1 when they are planar, the band `b` for `y` ([§3.4](#34-rows-and-bands)) and `N_X` for `x` |
| `fill_value` | number | required | 0 |
| `codecs` | array of object | required | `transpose` ([conventions §3](../conventions.md#3-arrays)) when the channels are interleaved (a chunk holds `[y, x, c]`), then `bytes`, little-endian for an item of more than one byte |
| `dimension_names` | array of string | required | the image's axes ([§3.3](#33-axes-and-fields)) |
| `attributes` | object | required | `{}` |

**Chunks:** the chunk at `t`, `c`, `z` (each for its axis present), `j`, 0 holds band `j` (rows
`j × b` to `j × b + b − 1`) of the plane of that time, channel (all channels when they are
interleaved, at `c` = 0), depth and the series' field, a [`LifChunk`](#type-lifchunk), as
[the profile §7.4](#74-chunk-references) references it. Every chunk is present.

### 4.4 Mosaics

A tile scan is an image with a used dimension of `DimID` 10, one element per tile, whose
`Data/Image` has an `Attachment` named `TileScanInfo` with a child `Tile` per tile (its
`FieldX`, `FieldY` and stage position `PosX`, `PosY`, `PosZ`). Tiles overlap: the stage moves
less than a field of view between them, and LAS X stitches them into a merged image, which it
writes as an image of its own. Each tile is a series of the output, as a CZI tile is an array of
its own ([the CZI convention §4.4](czi.md#44-tiles)): its pixels as the file holds them, its
place in [§5.2](#52-the-images)'s `tile` and `tile_scan`, and nothing stitched. The tiles have no
translation: the stage frame's direction and origin (`FlipX`, `FlipY`, `SwapXY` and the
objective's) are not in the image's own frame, so a viewer places them from the source metadata,
or shows the merged image.

## 5. Source metadata

### 5.1 The root

<a id="type-lifroot"></a>**`LifRoot`**, the root's `S`:

| member | type | presence | value |
|---|---|---|---|
| `version` | integer | required | `V`, the file's version ([§2.2](#22-the-xml-header)): 1 or 2 |

Everything else is on the images and on `refkv_source`.

### 5.2 The images

The `S` of series `s`, field `f` of image `k`, has these members, in this order:

<a id="type-lifimage"></a>**`LifImage`**, an image group's `S`:

| member | type | presence | value |
|---|---|---|---|
| `image` | integer | required | `k` |
| `path` | array of (string or `null`) | required | the image's path ([§2.3](#23-images)), an array of strings and `null`s |
| `attributes` | [`LifAttributes`](#type-lifattributes) | required | the image element's attributes (`Name`, `UniqueID`, ...) |
| `memory` | [`LifAttributes`](#type-lifattributes) | required | its `Memory` element's attributes (`Size`, `MemoryBlockID`) |
| `dimensions` | array of [`LifAttributes`](#type-lifattributes) | required | each dimension's attributes, in document order |
| `channels` | array of [`LifAttributes`](#type-lifattributes) | required | each channel's attributes, in document order (not the channel order of [§3.2](#32-channels)) |
| `tile_scan` | [`LifAttributes`](#type-lifattributes) | optional | when the image's only field dimension is of `DimID` 10 and its `TileScanInfo` attachment ([§4.4](#44-mosaics)) has a child `Tile`, the attachment's attributes |
| `field` | [`LifField`](#type-liffield) | optional | when the image has field dimensions |
| `tile` | [`LifAttributes`](#type-lifattributes) | optional | when `tile_scan` is present and the attachment has an `f`-th child `Tile` (from 0), that child's attributes |

<a id="type-lifattributes"></a>**`LifAttributes`**, an element's **attributes**: a JSON object of
its attributes' values as strings, references decoded, in document order (of a repeated name,
the first):

| member | type | presence | value |
|---|---|---|---|
| each attribute of the element, by its name | string | optional | the attribute's value, when the element has it |

<a id="type-liffield"></a>**`LifField`**: an object whose members are the image's field
dimensions' `DimID`s, as text, in the order of the field dimensions `d_1, …, d_q`
([§3.3](#33-axes-and-fields)), and the field's index along each:

| member | type | presence | value |
|---|---|---|---|
| each field dimension's `DimID`, in decimal | integer | required | the field's index along that dimension, from 0 |

The rest of the XML header (the experiment, the hardware settings, time stamps, LUTs and every
other attachment) is in the IR mirror, with every byte of the file.

### 5.3 The IR mirror

`refkv_source` is the file's **IR mirror**
([conventions §11.3](../conventions.md#113-the-ir-mirror)). The IR's elements (the LIF **source
model**) are below; nothing is left out: the XML header, every memory block (an image's or not,
of size 0 or not) and the tail.

### 5.4 Elements

Paths are relative to the IR's root, whose path is `""`
([conventions §8.1](../conventions.md#81-elements)); `<i>` is a decimal name index.

| path | kind | type | what |
|---|---|---|---|
| `""` | struct | | the root: the whole file, extent `(0, size)` |
| `header` | struct | | the header block |
| `header/fields` | value | `{test:<u4,length:<u4,magic:u1,characters:<u4}` | its fixed part |
| `header/xml` | value | `utf16[2c]` | the XML header, when `c > 0` |
| `blocks/<i>` | struct | | memory block `i` ([§2.1](#21-blocks)) |
| `blocks/<i>/fields` | value | `{test:<u4,length:<u4,magic:u1,size:<u4,magic2:u1,characters:<u4}` (version 1), or with `size:<u8` (version 2) | its fixed part |
| `blocks/<i>/id` | value | `utf16[2m]` | its ID, when `m > 0` |
| `blocks/<i>/data` | data | | an image's block's data, when `D > 0`: form `{"geometry": {"image": k}, "codec": {"endian": "little"}, "recipe": [["src", 0, null]]}` |
| `blocks/<i>/data` | value | `bytes[D]` | any other block's data, when `D > 0` |
| `tail` | value | `bytes[n]` | the tail, when not empty |

### 5.5 Equivalence

Hierarchies of one file are compared entry for entry, `refkv_source` included
([conventions §11.4](../conventions.md#114-equivalence)).

## 6. Example

A tile scan at `https://example.org/g.lif` (`lif_mosaic_overlap.lif` of the fixtures): one
image, `mosaic`, of two planar `uint16` channels and four overlapping tiles of 8 × 6 pixels
(dimensions X, Y and the mosaic, `DimID` 10, with the channels between Y and the mosaic). The
root:

```json
{
  "zarr_format": 3,
  "node_type": "group",
  "attributes": {
    "ome": {"version": "0.5", "bioformats2raw.layout": 3},
    "zarr_conventions": ["(the CMO of §1)"],
    "refkv_zarrified": {"profile": "lif", "version": 0, "source": {"url": "https://example.org/g.lif"}, "lif": {"version": 2}}
  }
}
```

`OME/zarr.json` lists the series `"0"` to `"3"`, one per tile. Series `1`'s `ome` and source
metadata:

```json
{
  "version": "0.5",
  "multiscales": [{
    "name": "mosaic (tile 1)",
    "axes": [
      {"name": "c", "type": "channel"},
      {"name": "y", "type": "space", "unit": "micrometer"},
      {"name": "x", "type": "space", "unit": "micrometer"}
    ],
    "datasets": [{"path": "0", "coordinateTransformations": [{"type": "scale", "scale": [1, 2.2, 2.142857142857143]}]}]
  }],
  "omero": {"channels": [
    {"label": "C0", "color": "FFFFFF", "active": true, "window": {"min": 0, "max": 65535, "start": 0, "end": 65535}},
    {"label": "C1", "color": "FFFFFF", "active": true, "window": {"min": 0, "max": 65535, "start": 0, "end": 65535}}
  ]}
}
```

```json
{
  "image": 0,
  "path": ["g.lif", "mosaic"],
  "attributes": {"Name": "mosaic", "Visibility": "1", "CopyOption": "1", "UniqueID": "..."},
  "memory": {"Size": "768", "MemoryBlockID": "MemBlock_1"},
  "dimensions": [
    {"DimID": "1", "NumberOfElements": "8", "Origin": "0.000000e+000", "Length": "1.500000e-05", "Unit": "m", "BitInc": "0", "BytesInc": "2"},
    {"DimID": "2", "NumberOfElements": "6", "Origin": "0.000000e+000", "Length": "1.100000e-05", "Unit": "m", "BitInc": "0", "BytesInc": "16"},
    {"DimID": "10", "NumberOfElements": "4", "Origin": "0.000000e+000", "Length": "0.000000e+00", "Unit": "", "BitInc": "0", "BytesInc": "192"}
  ],
  "channels": ["(two ChannelDescriptions, BytesInc 0 and 96)"],
  "tile_scan": {"Name": "TileScanInfo", "Application": "LAS AF", "FlipX": "0", "FlipY": "0", "SwapXY": "0"},
  "field": {"10": 1},
  "tile": {"FieldX": "1", "FieldY": "0", "PosX": "0.000106500", "PosY": "0.000200000", "PosZ": "0"}
}
```

Its array `1/0` is `uint16` of shape `[2, 6, 8]` and chunk shape `[1, 6, 8]`; its chunk
`1/0/c/1/0/0` is the 96 bytes of channel 1 of tile 1, at 192 + 96 bytes into the block's data.

# Part 2. The profile

The LIF profile of [spec/zarrify.md](../zarrify.md) is [§7](#7-lif-profile) of this document.
(Conformance is spec/zarrify.md's [Appendix A](../zarrify.md#appendix-a-conformance).) What every
profile's Part 2 shares is in [spec/zarrify.md, Profiles](../zarrify.md#profiles).

## 7. LIF profile

### 7.1 Detection

**Signature** ([spec/zarrify.md §1.2](../zarrify.md#12-input), tested at the precedence of
[profiles/lif.json](profiles/lif.json)): bytes 0–3 of `H` are `70 00 00 00` (the test value) and
byte 8 is `2A` (the magic byte). No other profile's signature matches these bytes: a NIfTI file
starts with a header size of 348 or 540, the others with a magic number of their own. A LOF file
starts with the same bytes and is rejected by [§7.3](#73-rejection); XLIF, XLEF, XLCF and XLLF
files are XML and match no signature.

### 7.2 Reading

The zarrifier reads, in any order ([spec/zarrify.md §1.2](../zarrify.md#12-input), evaluation
order):

1. the header block's fixed part, its first 13 bytes, which MUST lie within the file, and the
   XML header;
2. **the walk.** From `o = 13 + 2c`: if `o` is the file's size, the walk ends with an empty tail.
   Otherwise the `H` bytes at `o` are a memory block's fixed part when they lie within the file,
   their test value is `0x70` and both magic bytes `0x2A`, its ID ends within the file, its `D`
   is at most 2^53 − 1 and its data ends within the file; the walk then continues at the end of
   its data. When they are not, or after 2^20 blocks, the walk ends and the tail is the bytes from
   `o` to the end of the file. The walk never rejects: a block that cannot be read ends it, and
   an image whose block lies after it names a block the file does not hold
   ([§7.3](#73-rejection)).

Nothing else is read: no byte of a block's data. (The implementations read the first 64 KiB,
then the rest of the XML header when it is longer, then the blocks' fixed parts and IDs in
batches at the offsets the XML's `Memory` elements predict: the first one's (the file's own
element's), then those with a `Size` above 0, each ID once, in document order, since LAS X
writes no block for its folders' empty and repeated IDs. A file whose blocks follow its elements
is walked in one round of requests, and each block the prediction misses costs another. The prediction decides only what is read ahead, never
the output.)

### 7.3 Rejection

The input is rejected when a read of [§7.2](#72-reading) step 1 lies outside the file, when a
rule of this profile fails, and wherever the convention says the input is rejected or that
something MUST hold: the header block ([§2.1](#21-blocks): test value, magic byte, the XML
within the file), the XML header ([§2.2](#22-the-xml-header): UTF-16, a LOF, the root, the
version), each image ([§2.3](#23-images): its `Memory`, its memory block found once and named by
it alone; [§2.4](#24-dimensions-and-channels): its dimensions and channels and their
attributes; [§3](#3-placement): its data type, channels, X, rows, extent, shape and planes), the images in
document order. The walk ([§7.2](#72-reading) step 2) never
rejects, and neither does what only the source metadata reads.

**Limits.** These bound memory, time and the documents; exceeding one rejects the input:

| limit | value | bounds |
|---|---|---|
| the XML header | 2^27 bytes | the parse, which reads it whole |
| dimensions of an image | 16 | an image's documents |
| channels of an image | 1024 | an image's documents (`omero` lists at most 64) |
| array extent | each number of an image's shape at most 2^31 | shapes |
| fields of an image, and series | 65536 | the `OME` series list, as for CZI and ND2 |
| planes of an image | `2^22 + 4 ×` the file's size | its chunks, before they are listed |

The walk stops at 2^20 blocks without rejecting. Every document is then bounded: the root's by
its one member, each image's by its element's attributes, at most 16 dimensions and 1024
channels, and `refkv_source`'s by the mirror's budgets ([conventions §8.7](../conventions.md#87-the-view)).

### 7.4 Chunk references

All references are ranges of source 0; this profile has no data sources. Let `B` be the offset
of the data of the image's memory block (its start plus `H + 2m`). Series `s`, field `f` of an
image, has, for each plane of time `t`, channel `c` (only `c` = 0 when interleaved) and depth
`z` (each from 0; 0 for an axis the image lacks), the plane offset

`P = B + o + t × s_T + z × s_Z + (the offset of field f)`

where `o` is `o_c`, or 0 when the channels are interleaved, and `s_T`, `s_Z` the `BytesInc` of
the T and Z dimensions ([§3.3](#33-axes-and-fields)). Band `j` of the plane is the chunk of
[§4.3](#43-image-arrays), by its rows ([§3.4](#34-rows-and-bands)). Pixels are never copied.

<a id="type-lifchunk"></a>**`LifChunk`**, band `j` of the plane at `P`:

| part | range | presence | value |
|---|---|---|---|
| the band, packed rows | source range `(0, P + j × b × R, b × R)` | optional | for packed rows: the single range |
| row `r` of the band, padded rows | source range `(0, P + (j × b + r) × s_y, R)` | optional | for padded rows: `b` ranges, `r` = 0 to `b − 1`, in order |

### 7.5 What the profile does not read

| input | what it is | outcome |
|---|---|---|
| **LOF** | a Leica object file: one image, an `LMS_Object_File` header block | rejected ([§2.2](#22-the-xml-header)) |
| **LIFEXT** | `LMSDataContainerEnhancedHeader` | rejected too |
| **XLIF, XLEF, XLCF and XLLF** | XML that names image files beside it: TIFF, JPEG, PNG, BMP or LOF | match no signature. A profile of them would read a store of the files a document names, as VSI reads its tile files |
| **FLIM** raw data | `SingleMoleculeDetection` elements, photon arrival histograms | has no image: its blocks are kept as bytes in the mirror |

LIF stores pixels uncompressed and unpacked, so every image the convention gives a layout is
referenced, not decoded.

### 7.6 Decisions

- **Hierarchy.** A file holds a tree of elements (folders and images), each image with its own
  memory block. The output is a bioformats2raw collection of the images in document order, as
  the CZI and ND2 profiles collect their scenes and positions, with each image's path in the
  tree on its group ([§5.2](#52-the-images)). A field dimension (a mosaic's tiles, a lambda
  scan's wavelengths) selects an image, as ND2's positions and CZI's scene, illumination and
  view dimensions do: OME-NGFF 0.5 has room for one time axis, one channel axis and space.
- **Axes and chunks.** The axes come from `DimID`, not from the order of the dimensions: LAS X
  usually stores channels between Y and Z (or outside T), and the `BytesInc` of each dimension
  and channel places every plane, so a chunk is a band of one plane whatever the order of the
  others. Bands follow the CZI profile's row bands, bounded at 2^24 bytes.
- **Channels.** Planar channels are chunks of their own; interleaved channels (RGB) are one
  chunk with `c` inside it and the `transpose` codec, as the TIFF profile reads interleaved
  samples. The channel order is the bytes' order (`BytesInc`): BGR data stays BGR, labeled by
  `ChannelTag`'s colors, as the CZI convention keeps its subblocks' B, G, R order.
- **Mosaics.** Not stitched; each tile is an image with its stage position in its source
  metadata ([§4.4](#44-mosaics)), as CZI keeps each overlapping tile.
- **Metadata.** Nothing is lost: the XML header and every block are in the mirror, byte for
  byte, and each image's element, memory, dimensions and channels are on its group as text, as
  the XML writes them, so that no number changes in a translation.
- **Signature.** Precedence 55, after CZI's: the test value and magic byte are five fixed bytes
  that no other signature reads.
- **Data types.** Every bit depth the XML can describe in whole bytes: 1 to 64 bits, as
  `uint8` to `uint64`, and `float16` to `float64`; the `Resolution` is kept as the window's
  range. Bit-packed data (`BitInc` other than 0), other data types, channels of two types, and
  channels that are neither planar nor interleaved cannot be referenced as chunks, and are
  rejected.
