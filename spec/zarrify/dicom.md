# DICOM

The Zarr layout of a DICOM Part 10 file (PS3.10) with native, or JPEG,
lossless JPEG or JPEG 2000 encapsulated pixel data, including whole-slide images, and the
translation of its File Meta Information and dataset into the DICOM JSON
Model. What all of refkv's conventions share is in
[spec/conventions.md](../conventions.md), cited here as "conventions §n". How
refkv produces this layout as a reference store is the DICOM profile,
[Part 2](#part-2-the-profile) below.

Convention version: 0 (until release, [conventions §1](../conventions.md#1-conventions)) · UUID: `acf17198-e5a5-48d3-8187-22ec4bb40ea5` ·
Schema: [schema.json](dicom/schema.json)

Changed 2026-10-30: the JPEG Lossless transfer syntaxes, Process 14 and its selection value 1,
are admitted ([§2.1](#21-transfer-syntax)), their frames chunks of `imagecodecs_ljpeg`
([§4](#4-output)), each frame's markers up to its first scan read and checked
([§7.5](#75-frames), **Lossless JPEG**).

Changed 2026-10-26, with no change to any output: the layout is tabulated, its entries in
[Layout](#layout) and its types in type tables
([conventions §11.5](../conventions.md#115-layout-tables)), among them the datasets as JSON
([`DicomDataset`](#type-dicomdataset), [`DicomAttribute`](#type-dicomattribute)) and a chunk's
ranges ([`DicomChunk`](#type-dicomchunk)); and the text says, as refkv already wrote it, that
`S` always has `meta` and `dataset` ([§5.1](#51-the-root)).

Changes before 2026-10-22 are recorded in
[the revision log of VIRTUALIZE.md](../history/virtualize/REVISIONS.md), this document's earlier
name, up to
[revision 29](../history/virtualize/REVISIONS.md#revision-29-no-recorded-revision-the-profile-registry).

This document has two parts ([conventions §11.1](../conventions.md#111-parts)).

# Part 1. The convention

This convention gives a layout only to the files that meet its
requirements. Where it says that "the input is rejected", or that
something MUST hold, a file that fails has no layout under this
convention, and the profile rejects it.

## Layout

The hierarchy's entries ([conventions §11.5](../conventions.md#115-layout-tables)):

| prefix | type | structure | description |
|---|---|---|---|
| `zarr.json` | group | [`ImageGroup`](../conventions.md#type-imagegroup) | the root: the image ([§4](#4-output)) |
| `zarr.json#/attributes/ome` | attribute | [`OmeImage`](../conventions.md#type-omeimage) | `M`: no `name`, one level, the array `0`, with the scales of [§4](#4-output) and no translation, and `omero`, whose channels are [`DicomChannel`](#type-dicomchannel)s |
| `zarr.json#/attributes/zarr_conventions` | attribute | array of [`ConventionMetadata`](../conventions.md#type-conventionmetadata) | the CMO of [§1](#1-declaration) |
| `zarr.json#/attributes/refkv_zarrified` | attribute | [`RootProperty`](../conventions.md#type-rootproperty) | `profile` `"dicom"`, `version` `0`, `source.url` the file's URL |
| `zarr.json#/attributes/refkv_zarrified/dicom` | attribute | [`DicomRoot`](#type-dicomroot) | the root's source metadata ([§5.1](#51-the-root)) |
| `0/zarr.json` | array | [`DicomArray`](#type-dicomarray) | the pixels ([§4](#4-output)) |
| `0/c/<coords>` | chunk | [`DicomChunk`](#type-dicomchunk) | a frame, or one sample of it when planar; every chunk is present ([§4](#4-output), [§7.6](#76-chunk-references)) |
| `refkv_source/**` | subtree | [the mirror's entries](../conventions.md#table-mirror) | the IR mirror ([§5.3](#53-the-ir-mirror)): its elements are [§5.4](#54-elements)'s |

| placeholder | value |
|---|---|
| `<coords>` | the chunk's coordinates along the array's axes, in decimal, one key segment each: `<c>/<f>/<row>/<column>`, each of `<c>` and `<f>` only for the axes present (so it stands for two to four segments), as [§4](#4-output) gives them |

## 1. Declaration

The root and `refkv_source` declare the convention
([conventions §11.2](../conventions.md#112-declaration)), the root with the source
metadata of [§5](#5-source-metadata) as the member `"dicom"`. Its CMO is:

```json
{
  "uuid": "acf17198-e5a5-48d3-8187-22ec4bb40ea5",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/zarrify/dicom/schema.json",
  "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md",
  "name": "refkv_zarrified",
  "description": "The Zarr layout of a DICOM source zarrified by refkv, and the source's metadata"
}
```

The image's array has no attributes.

## 2. The source

The input is one DICOM Part 10 file (PS3.10): a 128-byte preamble, `DICM`,
the File Meta Information, then a dataset whose last element read is Pixel
Data. One file is one image with one pyramid level; a whole-slide image's
other levels are other files.


### 2.1 Transfer syntax

The File Meta Information is the file's group `0002` elements, from offset
132; the top-level dataset follows it and runs until Pixel Data
`(7FE0,0010)`. ([§7.2](#72-file)
says how they are read.)

- **Transfer syntax:** the meta element `(0002,0010)` (Transfer Syntax UID)
  is required, and its VR MUST be `UI`. Its value, a string ([§2.2](#22-attributes)), selects
  the dataset's encoding and the pixel data's form, and MUST be one of:

  | UID | name | encoding | pixel data |
  |---|---|---|---|
  | `1.2.840.10008.1.2` | Implicit VR Little Endian | implicit, little | native |
  | `1.2.840.10008.1.2.1` | Explicit VR Little Endian | explicit, little | native |
  | `1.2.840.10008.1.2.2` | Explicit VR Big Endian (retired) | explicit, big | native |
  | `1.2.840.10008.1.2.4.50` | JPEG Baseline (Process 1) | explicit, little | encapsulated, `jpeg` |
  | `1.2.840.10008.1.2.4.57` | JPEG Lossless, Non-Hierarchical (Process 14) | explicit, little | encapsulated, `ljpeg` |
  | `1.2.840.10008.1.2.4.70` | JPEG Lossless, Non-Hierarchical, First-Order Prediction (Process 14, Selection Value 1) | explicit, little | encapsulated, `ljpeg` |
  | `1.2.840.10008.1.2.4.90` | JPEG 2000 (lossless only) | explicit, little | encapsulated, `jpeg2k` |
  | `1.2.840.10008.1.2.4.91` | JPEG 2000 | explicit, little | encapsulated, `jpeg2k` |

  Any other value, including Deflated Explicit VR Little Endian, RLE
  Lossless, the other JPEG processes (among them the lossy extended and
  hierarchical ones), JPEG-LS and HTJ2K, rejects the input:
  their frames are not a single stream that a codec of [conventions §3](../conventions.md#3-arrays) decodes.

### 2.2 Attributes

The attributes read are these. Those marked **(FG)** are read from two
places: the first item of `(0028,9110)` (Pixel Measures Sequence) in the
first item of `(5200,9229)` (Shared Functional Groups Sequence) of the
top-level dataset, and the top-level dataset itself. All others are read
only from the top-level dataset. An element with length 0 counts as absent.
Attributes elsewhere (in other items, or deeper) are not read.

| tag | name | VR | kind |
|---|---|---|---|
| `(0008,0016)` | SOP Class UID | UI | string |
| `(0018,0088)` | Spacing Between Slices (FG) | DS | decimals |
| `(0018,1063)` | Frame Time | DS | decimals |
| `(0028,0009)` | Frame Increment Pointer | AT | tag |
| `(0020,9311)` | Dimension Organization Type | CS | string |
| `(0028,0002)` | Samples per Pixel | US | integer |
| `(0028,0004)` | Photometric Interpretation | CS | string |
| `(0028,0006)` | Planar Configuration | US | integer |
| `(0028,0008)` | Number of Frames | IS | integer string |
| `(0028,0010)`, `(0028,0011)` | Rows, Columns | US | integer |
| `(0028,0030)` | Pixel Spacing (FG) | DS | decimals |
| `(0028,0100)` | Bits Allocated | US | integer |
| `(0028,0101)` | Bits Stored | US | integer |
| `(0028,0102)` | High Bit | US | integer |
| `(0028,0103)` | Pixel Representation | US | integer |
| `(0028,1050)`, `(0028,1051)` | Window Center, Window Width | DS | decimals |
| `(0028,1052)`, `(0028,1053)` | Rescale Intercept, Rescale Slope | DS | decimals |
| `(0048,0006)`, `(0048,0007)` | Total Pixel Matrix Columns, Rows | UL | integer |
| `(0048,0302)` | Number of Optical Paths | UL | integer |
| `(0048,0303)` | Total Pixel Matrix Focal Planes | UL | integer |
| `(7FE0,0001)` | Extended Offset Table | OV | 64-bit integers |
| `(7FE0,0002)` | Extended Offset Table Lengths | OV | 64-bit integers |
| `(7FE0,0010)` | Pixel Data | OB or OW | [§7.5](#75-frames) |

Every attribute of the table present where it is read is checked as below,
whether or not its value ends up in the output.

- **VR:** in explicit VR, the element's VR MUST be the table's, else the
  input is rejected. (In implicit VR there is none to check.)
- **Length:** an attribute of the table other than Pixel Data MUST NOT have
  an undefined length (in implicit VR, [§7.3](#73-datasets-and-sequences) walks such an element as a
  sequence; it is then rejected here).
- **Kind:** the value is read by the table's kind:

  | kind | value |
  |---|---|
  | tag | `u16` group and `u16` element pairs in the dataset's byte order; its length MUST be a multiple of 4, and the first pair is used, as the tag `(group,element)` |
  | integer | `u16` (US) or `u32` (UL) values in the dataset's byte order; its length MUST be a multiple of 2 (4), and the first value is used |
  | 64-bit integers | `u64` values in the dataset's byte order; the length MUST be a multiple of 8, and every value MUST be at most 2^53 − 1 |
  | string | the value's bytes, split at each backslash (byte `5C`) into string values, each with its leading and trailing bytes `20` (space) and `00` removed. The first is used, as bytes (compared byte for byte with the ASCII text given here) |
  | integer string | the first string value MUST be one or more digits ([spec/zarrify.md §1.3](../zarrify.md#13-arithmetic)), optionally preceded by `+` or `-`, and is read as a decimal integer, which for Number of Frames MUST be from 1 to 2^53 − 1. Otherwise the input is rejected |
  | decimals | the string values. A value is **valid** if it matches `[+-]?([0-9]+(\.[0-9]*)?\|\.[0-9]+)([eE][+-]?[0-9]+)?` and its binary64 value ([spec/zarrify.md §1.3](../zarrify.md#13-arithmetic)) is finite. Invalid values never reject the input; they make the attribute unusable where [§4](#4-output) says so |

An **(FG)** attribute is taken from the Pixel Measures item if it is present
there, else from the top-level dataset. (The choice is made by presence: an
unusable value in the item does not fall back to the top level.)

## 3. The image

- **Rows**, **Columns**, **Samples per Pixel**, **Photometric
  Interpretation**, **Bits Allocated**, **Bits Stored** and **Pixel
  Representation** are required. Rows and Columns MUST be at least 1.
- **Samples per Pixel** `S` MUST be 1 or 3. Planar Configuration is required
  when `S` is 3, and MUST then be 0 (interleaved) or 1 (planar); when `S` is
  1 it is not used (but is checked as [§2.2](#22-attributes) says).
- **Number of Frames** `N` defaults to 1.
- **Bits:** Bits Stored MUST be from 1 to Bits Allocated. High Bit, when
  present, MUST be Bits Stored − 1. Pixel Representation MUST be 0
  (unsigned) or 1 (signed).
- **Data type:** `uint` (Pixel Representation 0) or `int` (1), then Bits
  Allocated. Bits Allocated MUST be 8, 16 or 32 for native pixel data, 8
  for `jpeg`, and 8 or 16 for `ljpeg` and `jpeg2k`. For `jpeg`, Bits Stored MUST be 8 and
  Pixel Representation 0. (For `ljpeg`, each frame's precision is checked
  against Bits Allocated and Pixel Representation, [§7.5](#75-frames).)
- **Cells:** native pixels are read as whole cells of Bits Allocated bits.
  When Bits Stored is less, the cells are not masked or sign-extended: the
  output equals the stored values only when the unused high bits are 0
  (unsigned) or copies of the sign bit (signed), as writers produce them.
- **Photometric Interpretation** (a string) and `S` MUST be one of:

  | pixel data | `S` = 1 | `S` = 3 |
  |---|---|---|
  | native | `MONOCHROME1`, `MONOCHROME2` | `RGB` |
  | `jpeg` | `MONOCHROME1`, `MONOCHROME2` | `RGB`, `YBR_FULL`, `YBR_FULL_422` |
  | `ljpeg` | `MONOCHROME1`, `MONOCHROME2` | `RGB` |
  | `jpeg2k` | `MONOCHROME1`, `MONOCHROME2` | `RGB`, `YBR_ICT`, `YBR_RCT` |

  Anything else is rejected: `PALETTE COLOR` (its values are indices into a
  lookup table that no codec of [conventions §3](../conventions.md#3-arrays) applies), native `YBR_*` (YCbCr, or
  subsampled, as stored), and the combinations a JPEG or JPEG 2000 decoder
  does not turn into RGB, among them every one of lossless JPEG but `RGB`
  (a lossless JPEG decoder applies no color transform). Every accepted
  three-sample image decodes to RGB.

**Whole-slide images.** The file is a **whole-slide image** if its SOP Class
UID is `1.2.840.10008.5.1.4.1.1.77.1.6` (VL Whole Slide Microscopy Image
Storage). Then:

- Dimension Organization Type MUST be `TILED_FULL` (`TILED_SPARSE`, whose
  frames are placed by per-frame positions, and an absent value are
  rejected);
- Total Pixel Matrix Columns `W` and Rows `H` are required and MUST be at
  least 1;
- Number of Optical Paths and Total Pixel Matrix Focal Planes default to 1
  and MUST be 1;
- the frames are tiles of `Rows × Columns` in row-major order: with
  `TC = ceil(W / Columns)` and `TR = ceil(H / Rows)`, `N` MUST equal
  `TR × TC`, and frame `f` is tile row `f div TC`, column `f mod TC`.

Otherwise the image is `Rows × Columns`, and frame `f` is index `f` of the
**frame axis**: `t` (time) if the Frame Increment Pointer is `(0018,1063)`
(Frame Time) or `(0018,1065)` (Frame Time Vector), as in cine ultrasound,
angiography and fluoroscopy; otherwise `z` (space).

## 4. Output

One image at the archive root ([conventions §4](../conventions.md#4-images)), with no `name` and one level, the
array `"0"`.

<a id="type-dicomarray"></a>**`DicomArray`**, the array's `zarr.json`: an
[`ArrayDocument`](../conventions.md#type-arraydocument) with these members:

| member | type | presence | value |
|---|---|---|---|
| `dimension_names` | array of string | required | the axes: `c` if `S` = 3; the frame axis ([§3](#3-the-image)), `t` or `z`, if the image is not a whole-slide image and `N > 1`; then `y`, `x` |
| `shape` | array of integer | required | 3 for `c`, `N` for the frame axis; then `H`, `W` for a whole-slide image, else Rows, Columns |
| `data_type` | string | required | from [§3](#3-the-image) (**Data type**) |
| `chunk_grid` | object | required | regular, its chunk shape: for `c`, 1 if the pixel data is native with Planar Configuration 1, else 3; 1 for the frame axis; then Rows, Columns |
| `codecs` | array of object | required | ([conventions §3](../conventions.md#3-arrays)) `transpose` when `c` is present and its chunk size is 3 (interleaved, as every decoded JPEG, lossless JPEG and JPEG 2000 frame is); then `bytes` (with `endian` `little` or `big` by the encoding when Bits Allocated is above 8), `imagecodecs_jpeg`, `imagecodecs_ljpeg` or `imagecodecs_jpeg2k` |
| `attributes` | object | required | `{}` ([§1](#1-declaration)) |

**Scale and units.** Each axis's scale and unit:

| axis | scale | unit |
|---|---|---|
| `y`, `x` | if Pixel Spacing (FG, [§2.2](#22-attributes)) is **usable**, its first and second values (the spacing between rows, then between columns). It is usable if it has exactly two string values, both valid decimals and positive. Otherwise 1 | `millimeter` when Pixel Spacing is usable; otherwise none |
| `z` | if Spacing Between Slices (FG) has a first value that is a valid, positive decimal, that value; otherwise 1 | `millimeter` with that value; otherwise none |
| `t` | if the Frame Increment Pointer is Frame Time and Frame Time's first value is a valid, positive decimal `ms`, the scale `ms / 1000`; otherwise 1 (a Frame Time Vector gives each frame's own interval, which a scale cannot) | `second` with `ms / 1000`; otherwise none |
| `c` | 1 | none |

**Translation:** none. A whole-slide image's origin is given in the slide
coordinate system, whose axes are rotated or mirrored against the image's
by Image Orientation (Slide); a cross-sectional image's position is in the
patient's frame, oriented by Image Orientation (Patient). Neither is a
translation along the image's own axes in general.

**omero:** `M` has `"omero"`, an [`Omero`](../conventions.md#type-omero) whose `channels`
are one [`DicomChannel`](#type-dicomchannel) per channel (one when `S` = 1, even though
there is no `c` axis).

<a id="type-dicomchannel"></a>**`DicomChannel`**: an
[`OmeroChannel`](../conventions.md#type-omerochannel) with these members:

| member | type | presence | value |
|---|---|---|---|
| `label` | string | required | `S` = 1: `gray`; `S` = 3: `R`, `G`, `B` |
| `color` | string | required | `S` = 1: `FFFFFF`; `S` = 3: `FF0000`, `00FF00`, `0000FF` |
| `active` | boolean | required | `true` |
| `window` | [`Window`](../conventions.md#type-window) | required | `{"min": A, "max": Z, "start": a, "end": e}` (below) |
| `inverted` | boolean | optional | `true`, present for `MONOCHROME1` |

`A` and `Z` are the stored values' range for Bits Stored `b`:

| Pixel Representation | `A` | `Z` |
|---|---|---|
| 0 (unsigned) | 0 | `2^b − 1` |
| 1 (signed) | `−2^(b−1)` | `2^(b−1) − 1` |

`a` and `e` are `A` and `Z`, except when `S` = 1 and there is a **VOI
window**: Window Center and Window Width are present with valid first
values `c` and `w`, `w ≥ 1`, and Rescale Slope `m` and Rescale Intercept
`k` are each absent (taken as 1 and 0) or have a valid first value, with
`m ≠ 0`. The window, in rescaled units, is converted to stored values:
`a = (c − w / 2 − k) / m` and `e = (c + w / 2 − k) / m` (`w / 2` first,
then left to right), swapped if `m < 0`.

**Chunks:** frame `f` (or sample `s` of it) is the chunk at
`0/c/<coords>` ([Layout](#layout)). Coords are: for `c`, the sample `s` when planar, else 0;
for the frame axis, `f`; then the frame's tile row and column for a whole-slide
image, else 0 and 0. Every chunk is present. A chunk holds the frame's
pixel data: for native pixel data, its `Rows × Columns × S` cells (or one
sample's, when planar); for encapsulated pixel data, the frame's
fragments' data, concatenated, which is one JPEG, lossless JPEG or JPEG 2000 stream (a
JPEG stream with three samples made explicit about its color transform,
as [§7.5](#75-frames) says). Its ranges are a [`DicomChunk`](#type-dicomchunk).

## 5. Source metadata

The root's source metadata `S` is the File Meta Information and the dataset in
the DICOM JSON Model, without the members that would make the root large
([§5.1](#51-the-root), [§5.2](#52-the-datasets-as-json)). Everything the file
holds is on `refkv_source`, which is the file's **IR mirror**
([§5.3](#53-the-ir-mirror), [conventions §8](../conventions.md#8-the-ir-mirror)):
its table, from which the file is rebuilt byte for byte, and its view,
`refkv_source/tree`. The IR's elements ([§5.4](#54-elements)) are the file's data
elements themselves; nothing is left out, layout, padding and dead space
included.

### 5.1 The root

The root's `S` ([conventions §2](../conventions.md#2-attributes)) has these
members (unchanged from revision 25, whose `vzip_source` held the members
the root leaves out, and arrays at the paths it names; since revision 26 each
such path, `refkv_source/<p>`, names the IR's element at path `<p>`, [§5.4](#54-elements),
except where this section says otherwise).

<a id="type-dicomroot"></a>**`DicomRoot`**, the root's `S`:

| member | type | presence | value |
|---|---|---|---|
| `preamble` | [`Base64`](../conventions.md#type-base64) | optional | present when the 128 bytes before `DICM` are not all zero: their base64 encoding |
| `meta` | [`DicomDataset`](#type-dicomdataset) | required | the File Meta Information, without the members that are `refkv_source`'s (below) |
| `dataset` | [`DicomDataset`](#type-dicomdataset) | required | the dataset, at every depth, including private attributes and the elements after Pixel Data, without the members that are `refkv_source`'s (below) |
| `pixel_extra` | string | optional | present when native Pixel Data has extra bytes (**Pixel data that no frame holds**, [§5.2](#52-the-datasets-as-json)): `"refkv_source/pixel_extra"`, the IR's element that holds them |
| `pixel_unreferenced` | string | optional | present when an Extended Offset Table skips bytes (**Pixel data that no frame holds**): `"refkv_source/pixel_unreferenced"`, the IR's struct whose elements hold them |
| `pixel_unreferenced_headers` | boolean | optional | `true`, present when that family also holds the frames' item headers (**Pixel data that no frame holds**) |
| `pixel_fragments` | string | optional | present when a frame of encapsulated pixel data has more than one fragment (**Fragments**, [§5.2](#52-the-datasets-as-json)): `"refkv_source/pixel_fragments"`, which names no element: the fragments are the IR's elements `dataset/7FE00010/fragments/<j>` |
| `pixel_offset_table` | boolean | optional | `true`, present when the Basic Offset Table of encapsulated pixel data is not empty (**Fragments**) |
| `trailing` | string | optional | present when the bytes after the last element that parses (**After Pixel Data**, [§5.2](#52-the-datasets-as-json)) are not empty: `"refkv_source/trailing"`, the IR's element that holds them |

The members of the File Meta Information and of the top-level dataset that
would make the root large are left out of the root's `meta` and `dataset`;
they are in the mirror only (their elements, [§5.4](#54-elements)). A member's
**size** is the length in UTF-8 of what ECMAScript's `JSON.stringify` writes
for its value (no whitespace), and an object's size that of the object. They
are left out, in this order:
1. the top-level dataset's Per-frame Functional Groups Sequence
   `(5200,9230)`, gathered (**Gathered sequences**, below);
2. every other member of `meta` or `dataset` (an attribute, or
   `duplicates`) whose size is more than 16384 bytes;
3. then, while the sizes of the root's `meta` and `dataset` together are
   more than 65536 bytes, the largest member left in either; of members of
   equal size, the first by name (in code-unit order, so `duplicates` after
   the tags), and `meta`'s before `dataset`'s.

The root records nothing of the members it leaves out. `S` always has `meta` and `dataset`,
so the root always has its source metadata member.

### 5.2 The datasets as JSON

The datasets are in the DICOM JSON Model (PS3.18 §F.2), with one addition,
`duplicates`. A dataset is a JSON object with one member per attribute,
named by its tag as eight uppercase hexadecimal digits (`"00280010"`). The
order of the members is not significant
([conventions §6](../conventions.md#6-source-metadata-as-json)). The layout
elements (**Layout**, below) are omitted, as PS3.18 §F.2 asks of group
lengths; every other element is kept, a group length or an offset table
included. Of elements with the same
tag in a dataset, the first is the attribute; the later ones are the
dataset's member `duplicates`.

<a id="type-dicomdataset"></a>**`DicomDataset`**, a dataset:

| member | type | presence | value |
|---|---|---|---|
| `"<tag>"` | [`DicomAttribute`](#type-dicomattribute) | optional | one member per attribute, named by its tag as eight uppercase hexadecimal digits (`"00280010"`): of elements with the same tag, the first |
| `duplicates` | array of object of [`DicomAttribute`](#type-dicomattribute) | optional | present only when there are any: an array, in file order, of objects that each have one member, named by the tag, whose value is the attribute, the later elements with that tag |

An attribute is `{"vr": VR}` with, when it has a value, `"Value"`,
`"InlineBinary"` or `"BulkDataURI"` (or, in the items of a gathered sequence,
`"Gathered"`; and, for a gathered sequence, `"Structures"` in place of `"Value"`:
**Gathered sequences**, below), and `"LittleEndian"` where the next list says.

<a id="type-dicomattribute"></a>**`DicomAttribute`**, an attribute (at most one of `Value`,
`InlineBinary`, `BulkDataURI`, `Gathered` and `Structures` is present):

| member | type | presence | value |
|---|---|---|---|
| `vr` | string | required | `VR`, two uppercase letters, by the next list |
| `Value` | array of [`Text`](../conventions.md#type-text), `null`, number (a `DS` value that is one exactly), [`Integer`](../conventions.md#type-integer) (an `IS` value, and `SL SS UL US SV UV`), [`Float`](../conventions.md#type-float) (`FL FD`), string (an `AT` tag, eight uppercase hexadecimal digits), [`DicomPersonName`](#type-dicompersonname) or [`DicomDataset`](#type-dicomdataset) | optional | the value, when it has one translated so: its items by its VR (**Explicit VR**, below); for a sequence, its items' datasets (`[]` when it has none, a gathered sequence without items included) |
| `InlineBinary` | [`Base64`](../conventions.md#type-base64) | optional | the value's bytes, by its VR (**Explicit VR**, **Character sets**) or because it is a binary value of at most 64 bytes (**Large values**) |
| `BulkDataURI` | string | optional | a large value (**Large values**), a sequence's bytes (**Sequences that the profile does not walk**) or nested Pixel Data (**Pixel Data**): `refkv_source/<p>`, the IR's element at `<p>` |
| `Gathered` | boolean | optional | `true`: a value in the items of a gathered sequence, the IR's element `s/<i>/<p>` (**Gathered sequences**) |
| `Structures` | array of [`DicomDataset`](#type-dicomdataset) | optional | a gathered sequence with at least one item, in place of `Value`: its items' distinct structures (**Structures**) |
| `LittleEndian` | boolean | optional | `true`, present with `InlineBinary`, `BulkDataURI` or `Gathered` on an explicit `UN` element in explicit VR big endian (**Without a stated VR**) |

<a id="type-dicompersonname"></a>**`DicomPersonName`**, a `PN` value (`null` when none of its
groups is present):

| member | type | presence | value |
|---|---|---|---|
| `Alphabetic` | [`Text`](../conventions.md#type-text) | optional | the text before the first `=`, present when not empty |
| `Ideographic` | [`Text`](../conventions.md#type-text) | optional | the text between the first and second `=`, present when not empty |
| `Phonetic` | [`Text`](../conventions.md#type-text) | optional | the text after the second `=`, present when not empty |

The translation:

- **Explicit VR.** `VR` is the element's. Its value is translated by VR:

  | VR | member | its items |
  |---|---|---|
  | `AE AS CS DA DS DT IS LO PN SH TM UC UI` | `Value` | the value's bytes split at each backslash (byte `5C`), each a text value (below) |
  | `LT ST UT UR` | `Value` | one value, a text value (below) |
  | `AT` | `Value` | each 4 bytes, a `u16` group and a `u16` element in the dataset's byte order, as eight uppercase hexadecimal digits |
  | `FL FD SL SS UL US SV UV` | `Value` | the values, numbers by [conventions §6](../conventions.md#6-source-metadata-as-json) ([`Float`](../conventions.md#type-float) for `FL FD`, [`Integer`](../conventions.md#type-integer) for the others) |
  | `OB OD OF OL OV OW UN` | `InlineBinary` | the bytes in base64, as stored (in the dataset's byte order; an explicit `UN` value in little endian, below) |
  | `SQ` | `Value` | an array of its items' datasets ([`DicomDataset`](#type-dicomdataset)) |

  - **Text values.** Each value of the first two rows has
    its trailing bytes `20` and `00` removed and, except for `LT ST UT UC
    UR`, its leading bytes `20`; it is then text by
    [conventions §6](../conventions.md#6-source-metadata-as-json) (UTF-8 if
    valid, else ISO 8859-1, so that its bytes are kept: Specific Character
    Set is not applied, and is in the dataset for a reader who decodes the
    bytes). An empty value is `null`. An `IS` value of the form
    `[+-]?[0-9]+` is that integer ([conventions §6](../conventions.md#6-source-metadata-as-json)): with more than 16
    digits without its sign and leading zeros, it is above 2^53 − 1, and
    is the string of those digits (with a leading `-` if negative). A `DS` value that
    matches the pattern of [§2.2](#22-attributes) is a number when it is one exactly: its
    binary64 value `v` ([spec/zarrify.md §1.3](../zarrify.md#13-arithmetic))
    is finite, and the text's decimal value equals that of the shortest
    decimal string that rounds to `v` (which ECMAScript's
    `Number::toString` and Python's `repr` write). Both are compared as
    decimals, without leading and trailing zeros: `0.1`, `1.10` and
    `-0` are numbers; `9007199254740993` and
    `0.1000000000000000055511151231257827`, which no binary64 value is, are
    not. A value is at most 2^16 bytes (a longer one is an array, below),
    so one whose digits are not all zero and whose exponent has more than 5
    digits without its sign and leading zeros is out of binary64's range
    (above it, or not zero and below every subnormal), and is not a number:
    the exponent is not converted. Any other `DS` or `IS` value stays text. A `PN` value is split at
    its first two `=` into the `Alphabetic`, `Ideographic` and `Phonetic`
    groups, of which the non-empty ones are members of an object, a
    [`DicomPersonName`](#type-dicompersonname) (`null` if none is).
  - **Not whole values.** An `AT` or numeric value whose length is not a
    multiple of its value size (4 for `AT`, else that of its data type
    below) is `"InlineBinary"`, its bytes as stored, keeping its VR.
- **Without a stated VR.** An element in implicit VR, or one whose
  explicit VR is `UN`, takes the VR that the **data dictionary** gives its
  tag, and is translated as above with it; if the dictionary gives none, it
  is `UN`, with `"InlineBinary"`. The value of an explicit `UN` element is
  in little endian in every transfer syntax (PS3.5 §6.2.2): in explicit VR
  big endian too, its values (and an array of them) are read in little
  endian, as its items are read in implicit VR little endian (below). The dictionary is
  [rust/refkv-ir/schema/dicom_dictionary.json](../../rust/refkv-ir/schema/dicom_dictionary.json)
  (PS3.6 as pydicom 3.0.2 ships it, written by
  [generate_dictionary.py](dicom/generate_dictionary.py)); it is normative, and a
  new edition of it is a new version of this convention. A tag's VR is,
  by the first row that applies:

  | tag | VR |
  |---|---|
  | in an odd (private) group | `LO` for elements `0010` to `00FF` (the private creators), and none for the others |
  | in `tags` (each VR's tags, as 8 uppercase hexadecimal digits in one string) | its entry's |
  | matched by an entry of `masks`, where `x` matches any hexadecimal digit (the repeating groups, such as `60xx0010`) | the VR of the first entry that matches it |
  | any other | none |

  Tags whose registered VR is ambiguous (`US or SS`, `OB or OW`, ...) are
  not in the dictionary, so they stay `UN`: their VR would depend on other
  elements. An element whose dictionary VR is `SQ` and whose length is
  defined is read as a sequence (in implicit VR little endian when the
  element was explicit `UN`, PS3.5 §6.2.2); if that breaks a rule of the
  next paragraph, it is `UN`, with its bytes. An element of undefined
  length is a sequence, `SQ`.

  In explicit VR big endian, an attribute of an explicit `UN` element whose
  value is bytes for a reader to translate (`"InlineBinary"`,
  `"BulkDataURI"` or `"Gathered"`) also has the member `"LittleEndian":
  true`: those bytes are in little endian, not in the dataset's byte order
  (a `uint8` array keeps no byte order, and one gathered column can hold
  both).
- An element of length 0 has no value.
- A sequence of undefined length is `SQ` (also when its VR is `UN`, whose
  items are then in implicit VR little endian).
- **Pixel Data.** The top-level Pixel Data is `{"vr": VR}` (`"OW"` in
  implicit VR), without a value: its value is the image. Pixel Data of
  undefined length nested in an item (an encapsulated icon image) has, when
  its fragment sequence has an item, a `BulkDataURI` naming its element,
  whose `offset_table` and `fragments/<j>` hold its items' data
  ([§5.4](#54-elements)).

**Character sets.** A dataset's **character set** is given by its first
element `(0008,0005)` (Specific Character Set) of defined length, for the
elements after it; for those before it, and in a dataset without one, it is
the character set in effect for the item's sequence in the dataset that
holds it, which the item thus inherits. The top-level dataset starts with none, and the File
Meta Information has none. It is **multibyte** if one of its values (its
bytes split at `5C`, each without leading and trailing bytes `20` and `00`)
is `GB18030`, `GBK`, `ISO 2022 IR 58`, `ISO 2022 IR 87`, `ISO 2022 IR 149` or
`ISO 2022 IR 159`: the character sets in which the bytes `5C` (`\`) and `3D`
(`=`) occur inside a character. Under a multibyte character set, a value of
VR `LO`, `PN`, `SH` or `UC` (the VRs that are split at `5C`, `PN` also at
`3D`, and to which the character set applies) is `"InlineBinary"`: its
bytes as stored, not split, trimmed or decoded, which a reader decodes with
the character set. Other values are translated as above: the other split
VRs hold only ASCII, and `LT ST UT` are not split, so that their bytes are
kept by [conventions §6](../conventions.md#6-source-metadata-as-json).

**Large values.** A value that is too large for JSON is the attribute
`{"vr": VR, "BulkDataURI": U}`, where `U` is `refkv_source/<p>`, `<p>` the
attribute's path (**Paths**, below), which is the path of its element in the
IR ([§5.4](#54-elements)): the value is that element's `v`. In the items of a
gathered sequence, every value is gathered instead (below). The large values:

| value | large when |
|---|---|
| a binary VR (`OB OD OF OL OV OW UN`) | its value is more than 64 bytes |
| an `AT` or numeric value that is not whole values | it is more than 64 bytes |
| a numeric VR (`FL FD SL SS UL US SV UV`) | it has more than 64 values |
| a `DS` or `IS` value | it has more than 64 values (more than 63 bytes `5C`) |
| a text VR | its value is more than 2^16 bytes |

A binary value of at most 64 bytes is `"InlineBinary"`, in base64.

**Layout.** These elements are omitted, and every other element but Data Set
Trailing Padding is kept:
- a group length `(gggg,0000)` of defined length, of an even group `gggg`,
  that is the only element of its tag in its dataset, whose VR is `UL`
  (or that is in implicit VR) and whose value is 4 bytes, when its value is
  the length of its **run**: the bytes from its end to the start of the
  next element of its dataset whose group is not `gggg` (or to the end of
  the dataset's elements: the end of an item's dataset of defined length,
  the start of its item delimiter, the end of the File Meta Information,
  or, for the top-level dataset, the end of the elements after Pixel Data,
  before `trailing`). In the top-level dataset, Pixel Data is an element of
  the run that ends where Pixel Data ends (below). A group length of a
  private (odd) group, one of another VR or length, one whose value is not
  its run's length, and one of a tag that occurs twice in its dataset (all
  of that tag) are kept;
- the Extended Offset Table `(7FE0,0001)` and its lengths `(7FE0,0002)`,
  when the profile reads the frames from them (encapsulated pixel data,
  [§7.5](#75-frames)): the first element of each tag in the top-level
  dataset, before Pixel Data. A later element of such a tag is a member of
  `duplicates` (the omitted one is the first). An offset table in native
  pixel data's dataset, in an item, in the File Meta Information or after
  Pixel Data is kept.

Data Set Trailing Padding is not kept either (above).

**Gathered sequences.** A sequence is **gathered** when it is not in an
item of a gathered sequence and it is the top-level Per-frame Functional
Groups Sequence `(5200,9230)` (the sequence at path `dataset/52009230`,
whatever its items), or it has more than 64 items (at any depth of the File
Meta Information or the dataset, a duplicate or an element after Pixel Data
too). An outer sequence of more than 64 items is thus gathered, and the
sequences in its items are not. Let `s` be the sequence's path and `F` its
number of items.

Every value in its items, at any depth (every attribute of defined length
that has a value and is not a sequence, and the bytes of a sequence that
breaks a rule, below; not encapsulated pixel data, which is as above), is
gathered with the values of the other items, column-wise, so that the
items' JSON keeps only their structure. Each such attribute is
`{"vr": VR, "Gathered": true}`. Its value is that of the IR's element at
`s/<i>/<p>` ([§5.4](#54-elements)), where `i` is its item's index and `p` its
path within the item (its path, below, without `s/<i>/`); its **column** is
the values of every item at one `p`. An attribute without a value is
`{"vr": VR}`, and a sequence is `{"vr": "SQ", "Value": [...]}`, its items
written so in turn.

**Structures.** Written so, an item is its **structure**: its attributes'
VRs, its sequences' items, its `duplicates`, and the markers, without its
values; items that differ only in their values have equal structures (as
JSON values). When the sequence has at least one item, its attribute is
`{"vr": "SQ", "Structures": [...]}`, the distinct structures in order of
their first item. The JSON thus grows with the distinct structures, not
with the items. A reader rebuilds item `i` from the IR, whose `s/<i>` is the
item: its structure is one of `Structures`, and each attribute
`{"vr": VR, "Gathered": true}` at path `p` takes as its value the IR's
element `s/<i>/<p>`, read as above (in little endian when the attribute
has `"LittleEndian"`). A gathered sequence without items is
`{"vr": "SQ", "Value": []}`.

The bounds below weigh each column as revision 25 stored it, as an array.
Each value has **typed values**, of a data type:

| value | typed values |
|---|---|
| a numeric VR: `FL`, `FD`, `SL`, `SS`, `UL`, `US`, `SV`, `UV` | its values as `float32`, `float64`, `int32`, `int16`, `uint32`, `uint16`, `int64`, `uint64` respectively, in the byte order in which it is stored |
| a binary VR: `OB`, `OD`, `OF`, `OL`, `OV`, `OW`, `UN` | its values as `uint8`, `float64`, `float32`, `uint32`, `uint64`, `uint16`, `uint8` respectively, in the byte order in which it is stored |
| a `DS` value of at most 2^16 bytes whose every value (split and trimmed as above) is a number by the rule above | those numbers as `float64`, in little endian |
| an `IS` value of at most 2^16 bytes whose every value is of the form `[+-]?[0-9]+` and from −2^63 to 2^63 − 1 | those integers as `int64`, in little endian |
| a value of another VR (text, `AT`, the bytes of a sequence), or whose length is not a multiple of its value size | its bytes as stored, as `uint8` |

A
column's **rows** are of `b` bytes when the values' typed values all have
the same data type, length (`b`) and byte order, of at most 2^24 bytes, or
else when the values' bytes as stored all have the same length (`b`), of at
most 2^24 bytes; otherwise it is a **family**. Of its `F` items, `n` have
the value; a column of rows is **sparse** when `2n < F`.

**Bounds.** Let `L` be the sequence's length: its value's length, or, for
a sequence of undefined length, the bytes from its value's start to its
sequence delimiter. A path's **cost** is, for a family, `8 × (F + 1)` plus
its members' lengths; for a column of rows of `b` bytes, `n × b` when it
is sparse, else `F × b`. A sequence that would be gathered is not, and is
instead its bytes, as a sequence that breaks a rule is (below: `SQ`, or
`UN` for an implicit or `UN` element, by the binary rule; for undefined
length, the `L` bytes above), when:
- its items have more than 1024 distinct paths `p` with a gathered value
  (not counting group lengths that are layout);
- or the size of its `Structures` is more than 65536 bytes;
- or the sum of its paths' costs is more than `16 × L`.

So the gathered JSON stays bounded. (The IR has the sequence's items either
way: these bounds decide its JSON only.)

**Paths.** An attribute's path is that of its dataset, then its tag (the
path of its element in the IR, [§5.4](#54-elements)):

| what | path |
|---|---|
| the File Meta Information | `meta` |
| the dataset | `dataset` |
| item `i` of a sequence at path `p` | `p/i` |
| an attribute, for example `(0028,3006)` of the dataset | its dataset's path, then its tag: `dataset/00283006` |
| the `k`-th duplicate (from 0) of a dataset at path `d`, of tag `t` | `d/duplicates/<k>/<t>` |

**Sequences that the profile does not walk.** The profile checks the
structure of every element it walks:
- every element of the top-level dataset;
- the elements of sequences of undefined length;
- the elements of the two sequences of [§2.2](#22-attributes)
  ([§7.3](#73-datasets-and-sequences)).

The source metadata also walks the other sequences of defined length, by
the same rules. It never rejects the input.

When one of them breaks a rule (an element runs past its container, an
unknown VR, items nested more than 64 deep, a tag other than an item where
one is expected), it is `SQ` with its bytes by the binary rule above:
`{"vr": "SQ", "InlineBinary": B}` when they are at most 64 bytes, else
`{"vr": "SQ", "BulkDataURI": U}` (gathered, in the item of a gathered
sequence). A sequence that only the dictionary types (an implicit or `UN`
element) is `UN` instead, by the same rule. In the IR, such a sequence is one
value, its bytes ([§5.4](#54-elements)).

**After Pixel Data.** The elements after the top-level Pixel Data are
read, in the dataset's encoding, as long as each one's header lies within
the file and has a known VR, its tag is not of group `FFFE`, no earlier
element of the top-level dataset has its tag (a duplicate ends them), and,
if its length is defined, its value lies within the file. Such an element
of defined length is translated as in the rest of the dataset: a sequence
that breaks a rule is its bytes, as above, and the elements go on after
it. An element of undefined length is a sequence with its items (an `SQ`
or `UN` element in explicit VR; any other explicit VR ends them), read as
in the rest of the dataset; if it breaks a rule outside the sequences of
defined length that it holds (which keep their bytes instead), it ends
them, since where it ends is not known. The element that ends them is not
translated, and the bytes from its start to the end of the file, if any,
are `trailing`.

Pixel Data ends:

| pixel data | Pixel Data ends |
|---|---|
| native | after its value |
| encapsulated | after its sequence delimiter. With an Extended Offset Table, whose fragments the profile does not walk, that is after the frame's item that ends last, plus the delimiter if it follows there |

**Pixel data that no frame holds.**
- **Native.** With `B`, `F` and `N` as in
  [§7.5](#75-frames), the bytes of
  Pixel Data's value after its `N × F` bytes of frames, unless they are
  none or one byte `00`, are the IR's element `pixel_extra` (one byte `00`
  is `dataset/7FE00010/padding`).
- **Extended Offset Table.** The profile does not check the frames' item
  headers. When, for every frame `f`, the 8 bytes at `q + EOT[f]` are an
  item header for the frame's data: the tag `(FFFE,E000)` and the length
  `EOTL[f]` (in little endian), the frames' items are the ranges
  `(q + EOT[f], 8 + EOTL[f])` (item header and data). Otherwise, the
  frames' items are their data alone, the ranges `(q + EOT[f] + 8,
  EOTL[f])`, every frame's 8 bytes before its data are kept with the bytes
  between the items, and `pixel_unreferenced_headers` is `true`. The items
  are taken in order of offset (then of length). Member `k`, for `k` from 0
  to `N − 1`, is the bytes from the end of the items before the `k`-th (the
  largest end so far, or `q` for `k = 0`) to the start of the `k`-th, and
  is empty if that start is not after that end. When one member is not
  empty (always, with `pixel_unreferenced_headers`), the members that are
  not empty are the IR's elements `pixel_unreferenced/<k>`.

**Fragments.** Without an Extended Offset Table, a frame of encapsulated
pixel data is the data of its fragments without their item headers, and of
its empty fragments nothing ([§7.5](#75-frames)). When a frame has more
than one fragment (empty ones included), the root has `pixel_fragments`.
`pixel_offset_table` is `true` when the Basic Offset Table is not empty.
The fragments, their item headers and the offset table are the IR's
elements under `dataset/7FE00010` ([§5.4](#54-elements)).

### 5.3 The IR mirror

`refkv_source` is the file's **IR mirror**
([conventions §11.3](../conventions.md#113-the-ir-mirror)).
The IR's elements (the DICOM **source model**) are below; nothing is left out: the
preamble, the File Meta Information, every data element at every depth
(group lengths, offset tables, duplicates, private and padding elements
included), the items and delimiters of sequences and fragments, the pixel
data, the elements after it, the trailing bytes and any other bytes, as
gaps.

### 5.4 Elements

Paths are relative to the IR's root, whose path is `""`
([conventions §8.1](../conventions.md#81-elements)); `<i>`, `<j>`, `<k>`, `<f>`
are decimal name indexes; `<tag>` is a tag as 8 uppercase hexadecimal digits
(`00280010`); `E` is the byte order of the dataset (`<` little, `>` big),
and of the items of a fragment sequence. Each dataset's elements are walked
as [§7.3](#73-datasets-and-sequences) and **Sequences that the profile does not walk**
([§5.2](#52-the-datasets-as-json)) say, every sequence that parses a struct.

| path | kind | type | what |
|---|---|---|---|
| `""` | struct | | the root: the whole file, extent `(0, size)` |
| `preamble` | value | `bytes[128]` | the preamble |
| `magic` | value | `ascii[4]` | `DICM` |
| `meta` | struct | | the File Meta Information, as a dataset |
| `dataset` | struct | | the top-level dataset: its elements to Pixel Data, Pixel Data, and the elements after it that parse ([§5.2](#52-the-datasets-as-json), **After Pixel Data**) |
| `<d>/<tag>` | value | `{H,v:T}` | an element of a dataset `<d>` of defined length that is not a sequence (or a sequence that breaks a rule, `T` then `bytes[n]`): its header fields `H` and its value `v` |
| `<d>/<tag>` | struct | | a sequence: `header` (a value `{H}`), its items, and `end` (`{group:Eu2,element:Eu2,length:Eu4}`, the sequence delimiter) when its length is undefined |
| `<d>/duplicates/<k>/<tag>` | | | the `k`-th element of `<d>`, in file order, whose tag an earlier element of `<d>` has (an omitted offset table counting as the earlier one); `duplicates/<k>` is a struct holding it |
| `<d>/FFFCFFFC~<k>` | value | | the `k`-th later Data Set Trailing Padding of `<d>` (padding is never a duplicate) |
| `<s>/<i>` | struct | | item `i` of a sequence `<s>`: `header` (`{group:Eu2,element:Eu2,length:Eu4}`), its dataset's elements, and `end` (the item delimiter) when its length is undefined |
| `dataset/7FE00010` | struct | | the top-level Pixel Data: `header` (`{H}`), then its value (below) |
| `dataset/7FE00010/frames/<f>` | data | | native pixel data: frame `f`, its `F` bytes ([§7.5](#75-frames)) |
| `dataset/7FE00010/padding` | value | `bytes[1]` | native pixel data's one byte `00` past the frames |
| `…/offset_table` | struct | | encapsulated pixel data's first item: `header` and `offsets` (`Eu4[k]`, or `bytes[n]` when its length is not a multiple of 4) |
| `…/fragment_headers/<j>` | value | `{group:Eu2,element:Eu2,length:Eu4}` | the item header of fragment `j` (after the offset table), or, with an Extended Offset Table, of frame `j` when the frames' headers are item headers |
| `dataset/7FE00010/fragments/<j>` | data | | fragment `j`'s data, when not empty (with an Extended Offset Table, frame `j`'s); its form's geometry is `{"rows", "columns", "samples", "bits_allocated", "pixel_representation", "photometric"}`, its codec `{"name": "bytes", "endian"}`, `{"name": "jpeg"}`, `{"name": "ljpeg"}` or `{"name": "jpeg2k"}`, its recipe the whole range |
| `<d>/7FE00010/fragments/<j>` | value | `bytes[n]` | the fragments of Pixel Data nested in an item (an icon image) |
| `…/end` | value | `{group:Eu2,element:Eu2,length:Eu4}` | the fragment sequence's delimiter |
| `pixel_extra` | value | `bytes[n]` | native pixel data past the frames, when not one byte `00` |
| `pixel_unreferenced/<k>` | value | `bytes[n]` | with an Extended Offset Table, the bytes before the `k`-th frame's item in order of offset, when not empty ([§5.2](#52-the-datasets-as-json)) |
| `trailing` | value | `bytes[n]` | the bytes from where the elements after Pixel Data stop to the end of the file |
| `gaps/<offset>` | gap | | any other bytes no element claims |

The header fields `H`:

| element | `H` |
|---|---|
| explicit VR | `group:Eu2,element:Eu2,vr:ascii[2],length:Eu2` |
| explicit VR, the VRs with a 4-byte length | `group:Eu2,element:Eu2,vr:ascii[2],reserved:Eu2,length:Eu4` |
| implicit VR | `group:Eu2,element:Eu2,length:Eu4` |

A value's type `T` is by its VR (the element's, or for one in
implicit VR or of VR `UN` the data dictionary's, [§5.2](#52-the-datasets-as-json),
else `UN`), for its `n` bytes:

| VR | `T` |
|---|---|
| a text VR: `AE AS CS DA DS DT IS LO LT PN SH ST TM UC UI UR UT` | `cstr[n]` (its text to its padding NUL, and any byte after it kept) |
| `AT` | `Eu2[n/4,2]` |
| `FL`, `FD`, `SL`, `SS`, `UL`, `US`, `SV`, `UV` | `Ef4`, `Ef8`, `Ei4`, `Ei2`, `Eu4`, `Eu2`, `Ei8`, `Eu8` respectively, of `n` over the value size values |
| any other, and when the length is not a multiple of the value size | `bytes[n]` |

A value of explicit VR `UN` is in little endian (`E` is `<` for
its `v`), as [§5.2](#52-the-datasets-as-json) reads it. The view shows such a
record by its `v` ([conventions §8.7](../conventions.md#87-the-view)); the
header, its VR among it, is in the table.

Items of one sequence that are identical, byte for byte, at one stride, and
native frames, fold into runs. The VR of an element, its encoding and its
value are all in the table (types and extents) and the source: a reader
rebuilds the DICOM JSON of [§5.2](#52-the-datasets-as-json) from them, the members
the root leaves out included.

### 5.5 Equivalence

Hierarchies of one file are compared entry for entry, `refkv_source` included
([conventions §11.4](../conventions.md#114-equivalence)).

## 6. Example

The root of an explicit VR little endian secondary capture image with a
referenced image sequence and shared functional groups
(`https://example.org/a.dcm`):

```json
"refkv_zarrified": {
  "profile": "dicom",
  "version": 0,
  "source": {
    "url": "https://example.org/a.dcm"
  },
  "dicom": {
    "meta": {
      "00020001": {
        "vr": "OB",
        "InlineBinary": "AAE="
      },
      "00020002": {
        "vr": "UI",
        "Value": [
          "1.2.840.10008.5.1.4.1.1.7"
        ]
      },
      "00020003": {
        "vr": "UI",
        "Value": [
          "1.2.826.0.1.3680043.8.498.54895542129125825857839641277016018109"
        ]
      },
      "00020010": {
        "vr": "UI",
        "Value": [
          "1.2.840.10008.1.2.1"
        ]
      },
      "00020012": {
        "vr": "UI",
        "Value": [
          "1.2.826.0.1.3680043.8.498.1"
        ]
      },
      "00020013": {
        "vr": "SH",
        "Value": [
          "PYDICOM 3.0.2"
        ]
      }
    },
    "dataset": {
      "00080016": {
        "vr": "UI",
        "Value": [
          "1.2.840.10008.5.1.4.1.1.7"
        ]
      },
      "00080018": {
        "vr": "UI",
        "Value": [
          "1.2.826.0.1.3680043.8.498.54895542129125825857839641277016018109"
        ]
      },
      "00080060": {
        "vr": "CS",
        "Value": [
          "OT"
        ]
      },
      "00081110": {
        "vr": "SQ",
        "Value": [
          {}
        ]
      },
      "00081140": {
        "vr": "SQ",
        "Value": []
      },
      "00082112": {
        "vr": "SQ",
        "Value": [
          {
            "00081150": {
              "vr": "UI",
              "Value": [
                "1.2.840.10008.5.1.4.1.1.7"
              ]
            },
            "00081155": {
              "vr": "UI",
              "Value": [
                "1.2.826.0.1.3680043.8.498.72746325067103167969493458029025673857"
              ]
            },
            "0040A170": {
              "vr": "SQ",
              "Value": [
                {
                  "00080100": {
                    "vr": "SH",
                    "Value": [
                      "121327"
                    ]
                  },
                  "00080102": {
                    "vr": "SH",
                    "Value": [
                      "DCM"
                    ]
                  },
                  "00080104": {
                    "vr": "LO",
                    "Value": [
                      "Full fidelity image"
                    ]
                  }
                }
              ]
            }
          },
          {}
        ]
      },
      "00280002": {
        "vr": "US",
        "Value": [
          1
        ]
      },
      "00280004": {
        "vr": "CS",
        "Value": [
          "MONOCHROME2"
        ]
      },
      "00280010": {
        "vr": "US",
        "Value": [
          5
        ]
      },
      "00280011": {
        "vr": "US",
        "Value": [
          3
        ]
      },
      "00280030": {
        "vr": "DS",
        "Value": [
          9.0,
          9.0
        ]
      },
      "00280100": {
        "vr": "US",
        "Value": [
          8
        ]
      },
      "00280101": {
        "vr": "US",
        "Value": [
          8
        ]
      },
      "00280102": {
        "vr": "US",
        "Value": [
          7
        ]
      },
      "00280103": {
        "vr": "US",
        "Value": [
          0
        ]
      },
      "52009229": {
        "vr": "SQ",
        "Value": [
          {
            "00289110": {
              "vr": "SQ",
              "Value": [
                {
                  "00280030": {
                    "vr": "DS",
                    "Value": [
                      0.2,
                      0.3
                    ]
                  }
                }
              ]
            }
          }
        ]
      },
      "7FE00010": {
        "vr": "OB"
      }
    }
  }
}
```

# Part 2. The profile

The DICOM profile of [spec/zarrify.md](../zarrify.md) is [§7](#7-dicom-profile) of this document. What every profile's Part 2 shares is in
[spec/zarrify.md, Profiles](../zarrify.md#profiles).

## 7. DICOM profile

**Signature** ([spec/zarrify.md §1.2](../zarrify.md#12-input), tested at the precedence of
[profiles/dicom.json](profiles/dicom.json)): bytes 128–131 of `H` are `44 49 43 4D`
(`DICM`). It is tested first, so a file whose 128-byte preamble holds a TIFF header, as
dual-personality files and some writers' preambles do, is read by this profile.

The input is one DICOM Part 10 file (PS3.10): a 128-byte preamble, `DICM`,
the File Meta Information, then a dataset whose last element read is Pixel
Data. One file is one image with one pyramid level; a whole-slide image's
other levels are other files.


### 7.1 Elements

All integers below are unsigned. An **element** at offset `p` is read in an
**encoding**: explicit or implicit VR, little or big endian.

- **Tag:** a `u16` group and a `u16` element number, in the encoding's byte
  order; the tag is written `(gggg,eeee)`.
- **Explicit VR:** the tag of an element that is not an item or a delimiter is
  followed by a VR, two bytes that MUST be one of `AE AS AT CS DA DS DT FD FL IS
  LO LT OB OD OF OL OV OW PN SH SL SQ SS ST SV TM UC UI UL UN UR US UT UV`, else
  the input is rejected.
- **Header:** after the tag come, by the element:

  | element | after the tag | the value starts at |
  |---|---|---|
  | **items and delimiters:** a tag of group `FFFE`, in every encoding | a `u32` length in the encoding's byte order (without a VR) | `p + 8` |
  | **implicit VR** (the element has no VR) | a `u32` length | `p + 8` |
  | **explicit VR**, VR `OB OD OF OL OV OW SQ SV UC UN UR UT UV` | the VR, two bytes that are not read, then a `u32` length | `p + 12` |
  | **explicit VR**, the other VRs | the VR, then a `u16` length | `p + 8` |

A length of `FFFFFFFF` is **undefined**. An element's header (its 8 or 12
bytes) MUST lie within its **container** ([§7.3](#73-datasets-and-sequences)), and so MUST a value of
defined length: a value that starts at `w` with defined length `n` ends at
`w + n`, where the next element starts.

### 7.2 File

- **Preamble:** the file MUST be at least 132 bytes long and bytes 128–131
  MUST be `DICM` (which is how [§1.2](../zarrify.md#12-input) selected this profile).
- **File Meta Information** starts at offset 132, in explicit VR little
  endian. It is the run of elements, from there, whose group (the `u16`
  little-endian at the element's start) is `0002`; it ends at the first
  element whose group is not `0002`, or where fewer than 2 bytes remain in
  the file. Its container is the file. A meta element of undefined length
  rejects the input.
- **Transfer syntax:** the meta element `(0002,0010)` (Transfer Syntax UID)
  is required, and its VR MUST be `UI`. Its value, a string (the
  convention [§2.2](#22-attributes)), MUST be one of the table of
  [the convention §2.1](#21-transfer-syntax),
  which gives the dataset's encoding and the pixel data's form.
- **Dataset:** the **top-level dataset** starts where the File Meta
  Information ends and is read in the transfer syntax's encoding. Its
  container is the file. The other meta elements, including the group
  length `(0002,0000)`, are walked as elements but not otherwise read.

### 7.3 Datasets and sequences

A **dataset** is a run of elements. The top-level dataset runs until its
first element with tag `(7FE0,0010)` (Pixel Data): that element's header is
read, and the walk stops there. Elements after it are not read. Reaching the
end of the file first (no Pixel Data) rejects the input.

Each element of a dataset is one of these, by the first that applies:

1. **Group `FFFE`:** `(FFFE,E00D)` ends an item of undefined length (below).
   Any other group-`FFFE` tag, and `(FFFE,E00D)` anywhere else, rejects the
   input.
2. **Undefined length.** In explicit VR, the VR MUST be `SQ` or `UN`, or
   (for tag `(7FE0,0010)` only) `OB` or `OW`, else the input is rejected.
   - Tag `(7FE0,0010)` (pixel data nested in an item, for example an icon
     image) is **encapsulated**: its value is a fragment sequence ([§7.5](#75-frames)),
     whose items are read in the dataset's byte order and are not parsed.
   - Otherwise the element is a **sequence** of undefined length, read in
     the dataset's encoding, except that a VR of `UN` means its items are
     in implicit VR little endian (PS3.5 §6.2.2).
3. **Defined length, tag `(5200,9229)` or `(0028,9110)`** (the two sequences
   [the convention §2.2](#22-attributes) reads): a sequence of defined length, in the dataset's encoding. In
   explicit VR its VR MUST be `SQ`, else the input is rejected.
4. **Any other defined length:** the value is skipped, whatever its VR (a
   sequence of defined length is not walked).

A **sequence** is a run of **items**. Each item is an element of tag
`(FFFE,E000)`; any other tag where an item is expected rejects the input,
except that:

- a sequence of undefined length ends with the element `(FFFE,E0DD)`, whose
  length MUST be 0. Its items are within the container of the element that
  holds the sequence;
- a sequence of defined length `n` whose value starts at `w` ends exactly at
  `w + n`, which is its items' container.

An item's value is a dataset. With defined length `m` starting at `w`, that
dataset's elements run from `w` to exactly `w + m` (their container). With
undefined length, they run until an element `(FFFE,E00D)`, whose length MUST
be 0, within the item's own container.

**Depth.** The top-level dataset has depth 0, and an item's dataset has the
depth of the dataset holding its sequence, plus 1. An item at a depth above
64 rejects the input.

Of elements with the same tag in a dataset, the first is used (the
convention's source metadata keeps the others); tags are not otherwise
required to be in order. Every element walked is checked as this
section says, whether or not [the convention §2.2](#22-attributes) reads it.

### 7.4 Rejection

The input is rejected when a rule of this profile fails, and when the
convention gives it no layout: wherever its [§2](#2-the-source)–[§4](#4-output) say that the input is
rejected, or that something MUST hold and it does not. The source
metadata ([the convention §5](#5-source-metadata)) never rejects the input.

### 7.5 Frames

Let `B = Bits Allocated / 8` and `F = Rows × Columns × S × B`, a frame's
size. Let `v` be the start of the top-level Pixel Data value and `n` its
length.

**Native.** The Pixel Data length MUST be defined, its value MUST lie
within the file (`v + n` at most the file's size; this is checked before
anything is computed from `n` or `N`), and `n ≥ N × F` (extra bytes, such
as a padding byte, are not part of a frame: [the convention §5](#5-source-metadata) keeps them).
In explicit VR big endian,
Pixel Data with VR `OW` and Bits Allocated 8 is rejected (its bytes are
swapped in 16-bit words). Frame `f` is the range `(0, v + f × F, F)`; when
`S` is 3 and Planar Configuration is 1, sample `s` of the frame is the range
`(0, v + f × F + s × F / 3, F / 3)`.

**Encapsulated.** The Pixel Data length MUST be undefined. Its value is a
**fragment sequence**, read in little endian: items `(FFFE,E000)` of defined
length (undefined rejects the input), ended by `(FFFE,E0DD)` whose length
MUST be 0; any other tag rejects the input. Item headers are 8 bytes, as in
[§7.1](#71-elements), and an item's **data** is its value. The first item, at `v`, is the
**Basic Offset Table** (BOT): its length `L` MUST be a multiple of 4, and its
data is `L / 4` `u32` offsets. The **fragments** are the items after it,
starting at `q = v + 8 + L`. A fragment's **position** is the offset of its
item tag minus `q`.

The frames' fragments are found by the first of these that applies.

1. **Extended Offset Table.** If `(7FE0,0001)` or `(7FE0,0002)` is present,
   both MUST be, each with exactly `N` values, and the BOT MUST be empty
   (`L = 0`). Frame `f` is one fragment, whose data is the range
   `(0, q + EOT[f] + 8, EOTL[f])`. The frames' items, the ranges
   `(q + EOT[f], 8 + EOTL[f])` of each frame's 8 bytes before its data and
   its data, MUST NOT overlap (taken in order of offset, then of length,
   each MUST start at or after the end of the one before it); otherwise the
   input is rejected. The fragments' item headers do not
   decide the frames, nor does the end of the sequence: neither is checked,
   and the input is not rejected for them. [The convention §5](#5-source-metadata) reads each
   frame's 8 bytes at `q + EOT[f]`, and keeps them, with the bytes between
   the frames' items, when one is not an item header `(FFFE,E000)` of
   length `EOTL[f]`.
2. Otherwise every fragment's item header is read, up to and including
   the `(FFFE,E0DD)` that ends the sequence, all within the file. There
   MUST be at least one fragment.
   - **BOT not empty:** `L / 4` MUST equal `N`; the offsets MUST start at 0,
     be strictly increasing, and each MUST be the position of a fragment.
     Frame `f` is the fragments whose positions are from its offset to
     before the next frame's (for the last frame, to the end).
   - **Empty BOT:** if there are `N` fragments, frame `f` is fragment `f`;
     else if `N = 1`, the frame is every fragment; otherwise the input is
     rejected (fragments are not split into frames by their contents).

A frame's **ranges** are, for each of its fragments in order whose data is
not empty, the range `(0, start of data, length of data)`. A frame with no
ranges rejects the input. The ranges are the frame's stream, without item
headers.

**JPEG color.** For `jpeg` with `S` = 3, the frame's stream is made
explicit about its color transform, as the TIFF profile does for JPEG tiles
([tiff.md §7.3](tiff.md#73-chunks)): its ranges become `[P, (0, o + 2, m − 2), ...the rest]`, where
`(0, o, m)` is its first range, `m` MUST be more than 2 and the range's
first 2 bytes MUST be `FF D8`, the SOI marker (they are dropped, and `P`
starts with its own), and `P` is the literal range
`FF D8 FF EE 00 0E 41 64 6F 62 65 00 64 00 00 00 00 T`: SOI and an Adobe
APP14 marker with transform `T` = 0 for `RGB` and 1 for `YBR_FULL` and
`YBR_FULL_422`. (Markers of the stream's own, such as JFIF, still apply by
[conventions §3](../conventions.md#3-arrays)'s conventions.)

Every range MUST lie within the file, and every chunk's reference payload
MUST be at most 65519 bytes ([§1.2](../zarrify.md#12-input)); otherwise the input is rejected.

**Lossless JPEG.** For `ljpeg`, each frame's stream (its ranges, in order) is
read from its start up to its first SOS marker, by ISO/IEC 10918-1's
marker syntax: it MUST start with SOI (`FF D8`); after it, at each marker's place,
bytes `FF` before a marker are skipped, as are TEM (`FF 01`) and RSTn
(`FF D0`–`FF D7`), and any other marker is a segment of 2 bytes, a `u16` big
endian length `l` of at least 2, and `l − 2` bytes, the next marker following
it. A frame header (SOFn: `FF C0`–`FF CF` but `FF C4`, `FF C8`, `FF CC`) gives
the precision `P` (its first byte) and the components `Nf` (its sixth); a DRI
(`FF DD`) the restart interval `Ri` (its first `u16`), which is 0 without
one; and the first SOS (`FF DA`), which MUST follow a frame header, the
components of its scan `Ns` (its first byte), the predictor `Ss` (the byte
after its `Ns` component specifications) and the point transform `Al` (the
low 4 bits of the byte after `Ss` and `Se`). The input is rejected if a
stream ends, or is not a marker (`FF` and a byte other than `00`, `D8`, `D9`)
where one is due, before that SOS; or if, by the last frame header and DRI
before it:

- the frame header is not SOF3 (`FF C3`, lossless, Huffman coded);
- `Nf` or `Ns` is not `S` (the scan holds every component, interleaved);
- `P` is less than 2 or more than Bits Allocated, or, with Pixel
  Representation 1, not Bits Allocated (a decoder gives a sample's `P` bits
  as unsigned, which the array's signed data type reads as stored only at the
  full width);
- `Ss` is not from 1 to 7;
- `Al` is not 0 (a point transform, which loses the low bits), or `Ri` is
  not 0 (restart intervals): imagecodecs' lossless JPEG decoder, which
  `imagecodecs_ljpeg` calls, gives wrong samples for both. None of the 11
  JPEG Lossless files of pydicom's pylibjpeg-data (CT, MR, CR, DX, NM, US
  and secondary capture) has either.

Every frame's markers are read before any is checked: first each frame's
first 512 bytes (or its first range, when shorter), then, for a walk that
needs more, 512 bytes (or to its range's end) from where it stopped, until
every walk ends.

### 7.6 Chunk references

Frame `f` (or sample `s` of it) is the entry `0/c/<coords>` of
[the convention §4](#4-output), with its ranges ([§7.5](#75-frames)). Coords are: for `c`, the sample `s` when planar,
else 0; for the frame axis, `f`; then the frame's tile row and column for a
whole-slide image, else 0 and 0.

<a id="type-dicomchunk"></a>**`DicomChunk`**, the reference of frame `f` (or of sample `s` of
it), with `v`, `F`, `q`, `EOT` and `EOTL` as in [§7.5](#75-frames). Its ranges are exactly
one of: the frame; sample `s` of the frame; or the fragments' data, preceded by the Adobe
prefix when the prefix is present:

| part | range | presence | value |
|---|---|---|---|
| the frame | source range `(0, v + f × F, F)` | optional | present for native pixel data, unless `S` is 3 and Planar Configuration is 1 |
| sample `s` of the frame | source range `(0, v + f × F + s × F / 3, F / 3)` | optional | present for native pixel data with `S` 3 and Planar Configuration 1 |
| the Adobe prefix | literal range `P` | optional | present for `jpeg` with `S` = 3, before the fragments' data: the 18 bytes `FF D8 FF EE 00 0E 41 64 6F 62 65 00 64 00 00 00 00 T`, SOI and an Adobe APP14 marker with transform `T` = 0 for `RGB` and 1 for `YBR_FULL` and `YBR_FULL_422` (**JPEG color**, [§7.5](#75-frames)) |
| the fragments' data | source ranges `(0, start of data, length of data)` | optional | present for encapsulated pixel data: one range for each of the frame's fragments whose data is not empty, in order, at least one; with an Extended Offset Table, the one range `(0, q + EOT[f] + 8, EOTL[f])`. After the Adobe prefix, the first range `(0, o, m)` is instead `(0, o + 2, m − 2)`, without its SOI |

No chunk is copied: every chunk is a reference.


### 7.7 Limits

These are deliberate, and follow from the rules above.

- Only the transfer syntaxes of [the convention §2.1](#21-transfer-syntax). JPEG-LS, RLE, deflate
  and HTJ2K are rejected, as is any file whose frames are not single
  streams that a codec of [conventions §3](../conventions.md#3-arrays) decodes.
- A lossless JPEG frame with a point transform or restart intervals is
  rejected, though its stream is valid, since the codec's decoder reads it
  wrongly ([§7.5](#75-frames)); so is a signed one whose precision is less
  than Bits Allocated. Each lossless JPEG frame's markers cost a read of
  its first 512 bytes, which a Basic or Extended Offset Table lets the
  reads of all frames batch.
- A file is one level. A whole-slide pyramid spans several files (one per
  level); `TILED_SPARSE` images, and those with several focal planes or
  optical paths, are rejected.
- The frames of a multi-frame image that is not a whole-slide image are
  placed along `t` when the Frame Increment Pointer names Frame Time or
  Frame Time Vector, and along `z` otherwise, whatever they are. An
  enhanced image whose frames span several dimensions (slices, echoes,
  b-values) is still one axis.
- With an empty Basic Offset Table, no Extended Offset Table, and a number
  of fragments other than 1 or `N`, the input is rejected rather than split
  at JPEG markers, which would read pixel data.
- Without an Extended Offset Table, every fragment's item header is read:
  on a large encapsulated file, that is many small reads.
- No modality LUT, VOI LUT, palette or overlay is applied to the pixels;
  only the omero window uses the VOI attributes.

A file whose preamble holds a TIFF header, as dual-personality files and
some toolkits write it, is still read by this profile: [§1.2](../zarrify.md#12-input) tests for `DICM`
first.
