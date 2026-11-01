# CZI

The Zarr layout of a Zeiss CZI file (the ZISRAW container, file version 1),
and the translation of its segments into Zarr arrays and JSON. What all of
refkv's conventions share is in [spec/conventions.md](../conventions.md), cited
here as "conventions §n". How refkv produces this layout as a reference store
is the CZI profile, [Part 2](#part-2-the-profile) below, cited as
"the profile §n".

Convention version: 0 (until release, [conventions §1](../conventions.md#1-conventions)) · UUID: `7a0733c7-d4be-4482-a64f-6904d9354ea5` ·
Schema: [schema.json](czi/schema.json)

Changed 2026-10-26, with no change to any output: the layout is tabulated, its entries in
[Layout](#layout) and its types in type tables
([conventions §11.5](../conventions.md#115-layout-tables)), and the source's structures, the
values read from the metadata XML, the codecs and the cases of a coded size are tables; the
prose also states what refkv already wrote and it left unsaid: when the largest divisor band of
a large uncompressed tile holds less than 2^20 bytes, its bands are short bands of the most rows
within 2^24 bytes, and the level's or tile array's grid is rectilinear
([§4.3](#43-image-arrays), [§4.4](#44-tiles)); and it drops the per-element arrays under
`refkv_source` that the IR mirror replaced in
[revision 21](../history/virtualize/REVISIONS.md#revision-21-the-ir-mirror-is-the-source-metadata-of-tiff-nd2-and-czi)
([§7.5](#75-chunk-references)), and names the subblock segment `ZISRAWSUBBLOCK`
([§5.3](#53-elements)).

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

Every convention is held to reconstruction and efficiency (conventions,
introduction). For CZI this means: every segment the file holds (subblocks
with their data, metadata and attachments, the metadata XML, attachments,
deleted and unreferenced segments, and bytes after the last segment) can be
rebuilt from the hierarchy, byte for byte (its IR mirror, [§5.2](#52-the-ir-mirror));
and no document grows with the number of subblocks, segments or channels
beyond the stated limits: the documents that one subblock adds (a tile
array's, [§4.4](#44-tiles)) are of a bounded size, and every value that grows with the
file is in an array, referenced where the file holds it.

## Layout

The hierarchy's entries ([conventions §11.5](../conventions.md#115-layout-tables)), a
bioformats2raw collection ([conventions §4](../conventions.md#4-images), whose
[entries](../conventions.md#table-collection) these rows refine) of the series' images, with
the tile arrays beside it:

| prefix | type | structure | description |
|---|---|---|---|
| `zarr.json` | group | [`GroupDocument`](../conventions.md#type-groupdocument), attributes a [`CollectionRoot`](../conventions.md#type-collectionroot) | the root ([§4.1](#41-the-hierarchy)) |
| `zarr.json#/attributes/ome` | attribute | `{"version": "0.5", "bioformats2raw.layout": 3}` | when some series has an image ([§3.5](#35-regular-levels)); absent when none has (no subblock, or every placed subblock a tile, or none placed), and then there is no `OME` group |
| `zarr.json#/attributes/zarr_conventions` | attribute | array of [`ConventionMetadata`](../conventions.md#type-conventionmetadata) | the CMO of [§1](#1-declaration) |
| `zarr.json#/attributes/refkv_zarrified` | attribute | [`RootProperty`](../conventions.md#type-rootproperty) | `profile` `"czi"`, `version` `0`, `source.url` the file's URL |
| `zarr.json#/attributes/refkv_zarrified/czi` | attribute | [`CziRoot`](#type-cziroot) | the root's source metadata, the file header ([§5.1](#51-the-root)) |
| `OME/zarr.json` | group | [`GroupDocument`](../conventions.md#type-groupdocument), attributes a [`SeriesGroup`](../conventions.md#type-seriesgroup) | when some series has an image: `series` `"0"`, `"1"`, ..., one per image |
| `<k>/zarr.json` | group | [`ImageGroup`](../conventions.md#type-imagegroup) | image `k` ([§4.2](#42-images)) |
| `<k>/zarr.json#/attributes/ome` | attribute | [`OmeImage`](../conventions.md#type-omeimage) | `M`: one dataset per level, with `name`, axes, scales, translations and `omero` as [§4.2](#42-images) and [§4.3](#43-image-arrays) say |
| `<k>/zarr.json#/attributes/zarr_conventions` | attribute | array of [`ConventionMetadata`](../conventions.md#type-conventionmetadata) | the CMO of [§1](#1-declaration), when the image's series key has a dimension |
| `<k>/zarr.json#/attributes/refkv_zarrified` | attribute | [`NodeProperty`](../conventions.md#type-nodeproperty) | when the image's series key has a dimension |
| `<k>/zarr.json#/attributes/refkv_zarrified/czi` | attribute | [`CziImage`](#type-cziimage) | the image's source metadata ([§4.2](#42-images)) |
| `<k>/<d>/zarr.json` | array | [`CziLevelArray`](#type-czilevelarray) | level `d` of image `k` ([§4.3](#43-image-arrays)); attributes `{}` |
| `<k>/<d>/c/<coords>` | chunk | [`CziChunk`](#type-czichunk) | a placed subblock of the level, or a band of it ([§4.3](#43-image-arrays)); a cell without a subblock has no chunk |
| `tiles/zarr.json` | group | [`GroupDocument`](../conventions.md#type-groupdocument), attributes `{}` | when there is a tile ([§4.4](#44-tiles)) |
| `tiles/<n>/zarr.json` | array | [`CziTileArray`](#type-czitilearray) | tile array `n`: a copy of a tile position ([§4.4](#44-tiles)) |
| `tiles/<n>/zarr.json#/attributes/zarr_conventions` | attribute | array of [`ConventionMetadata`](../conventions.md#type-conventionmetadata) | the CMO of [§1](#1-declaration); every tile array declares the convention |
| `tiles/<n>/zarr.json#/attributes/refkv_zarrified` | attribute | [`NodeProperty`](../conventions.md#type-nodeproperty) | |
| `tiles/<n>/zarr.json#/attributes/refkv_zarrified/czi` | attribute | [`CziTile`](#type-czitile) | the tile array's source metadata ([§4.4](#44-tiles)) |
| `tiles/<n>/c/<coords>` | chunk | [`CziChunk`](#type-czichunk) | a tile, or a band of it ([§4.4](#44-tiles)); a plane without a tile has no chunk |
| `refkv_source/**` | subtree | [the mirror's entries](../conventions.md#table-mirror) | the IR mirror ([§5.2](#52-the-ir-mirror)): its elements are [§5.3](#53-elements)'s; the convention writes no arrays of its own under it |

| placeholder | value |
|---|---|
| `<k>` | an image's index, in decimal, from 0 in series order ([§3.3](#33-series)) |
| `<d>` | a level's index in its image, in decimal, from 0 in the order of [§3.5](#35-regular-levels) |
| `<n>` | a tile array's index, in decimal, from 0 in increasing order of the index of its first tile ([§4.4](#44-tiles)) |
| `<coords>` | the chunk's coordinates along the array's axes, in decimal, one key segment each: `<t>/<c>/<z>/<y>/<x>`, each of `t`, `c` and `z` only for the axes present (so it stands for two to five segments) |

## 1. Declaration

The root and `refkv_source` declare the convention
([conventions §11.2](../conventions.md#112-declaration)), the root with the source
metadata of [§5.1](#51-the-root) as the member `"czi"`. Its CMO is:

```json
{
  "uuid": "7a0733c7-d4be-4482-a64f-6904d9354ea5",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/zarrify/czi/schema.json",
  "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md",
  "name": "refkv_zarrified",
  "description": "The Zarr layout of a CZI source zarrified by refkv, and the source's metadata"
}
```

These other nodes declare it, each with `{"czi": S}`:

| node | `S` |
|---|---|
| every image group whose series key has a dimension ([§4.2](#42-images)) | [`CziImage`](#type-cziimage) |
| every tile array ([§4.4](#44-tiles)) | [`CziTile`](#type-czitile) |

No other node declares it: not an image group whose series key has no dimension, an image's
arrays, `OME` or `tiles`.

## 2. The source

All integers are little-endian; `i32`, `i64` are signed, `u8` unsigned.
Offsets are from the start of the file.

### 2.1 Segments

A CZI file is a sequence of **segments**. A segment at offset `o` starts with
a 32-byte header: 16 bytes of **id** (ASCII, NUL-padded), `i64`
**AllocatedSize** `A`, `i64` **UsedSize** `U`; its data is the `A` bytes
from `o + 32`, of which the first `U` are used (`U = 0` means `U = A`, as
early writers left it). The next segment starts at `o + 32 + A`. The ids are:

| id | segment |
|---|---|
| `ZISRAWFILE` | the file header, at offset 0 |
| `ZISRAWDIRECTORY` | the subblock directory |
| `ZISRAWSUBBLOCK` | a subblock: one tile of one plane |
| `ZISRAWMETADATA` | the metadata XML |
| `ZISRAWATTDIR` | the attachment directory |
| `ZISRAWATTACH` | an attachment |
| `DELETED` | a segment a writer has given up; its data is what it held |

A segment is **referenced** when the file header, the directory or the
attachment directory gives its offset ([§2.2](#22-the-file-header)–[§2.6](#26-attachments)); the rest are found by the
**walk** ([the profile §7.2](#72-structures-and-the-walk)), which
visits the segments in file order from offset 0 and stops at the first offset
where no segment header can be read; the bytes from there to the end of the
file are the **tail**.

### 2.2 The file header

The `ZISRAWFILE` segment's data starts with these 80 bytes (the rest of the 512 is spare):

| bytes | field |
|---|---|
| 0–3 | `i32` Major |
| 4–7 | `i32` Minor |
| 8–15 | `i32` × 2 reserved |
| 16–31 | 16 bytes PrimaryFileGuid |
| 32–47 | 16 bytes FileGuid |
| 48–51 | `i32` FilePart |
| 52–59 | `i64` SubBlockDirectoryPosition |
| 60–67 | `i64` MetadataPosition |
| 68–71 | `i32` UpdatePending |
| 72–79 | `i64` AttachmentDirectoryPosition |

Major MUST be 1. FilePart MUST be 0: a CZI whose image is
split over several files (`name.czi`, `name(1).czi`, ...) is rejected, both
here and wherever a directory entry or attachment entry names a FilePart
other than 0, since a reference reaches one file only.

A **GUID** is written as text in the 36-character form whose first three
groups are the `u32`, `u16` and `u16` at its bytes 0–3, 4–5 and 6–7 read
little-endian, and whose last two groups are its bytes 8–9 and 10–15 in
order, in lowercase hexadecimal (as Windows and libCZI write a GUID).

### 2.3 The subblock directory

At SubBlockDirectoryPosition, which MUST be the offset of a
`ZISRAWDIRECTORY` segment, the data is `i32` EntryCount `N` (0 ≤ `N` ≤
2^21), 124 spare bytes, then `N` **entries** back to back, all within the
segment's used bytes. Each entry has schema `DV` (an entry of schema `DE`,
or of any other schema, rejects the input):

| bytes | field |
|---|---|
| 0–1 | `DV` |
| 2–5 | `i32` PixelType ([§3.1](#31-pixel-types-and-codecs)) |
| 6–13 | `i64` FilePosition, the subblock segment's offset |
| 14–17 | `i32` FilePart (MUST be 0) |
| 18–21 | `i32` Compression ([§3.1](#31-pixel-types-and-codecs)) |
| 22–27 | 6 spare bytes; byte 22 is the **pyramid type** (0 none, 1 single subblock, 2 multi subblock) |
| 28–31 | `i32` DimensionCount `d` |
| 32 ... | `d` **dimension entries** of 20 bytes |

A dimension entry is 20 bytes:

| bytes | field |
|---|---|
| 0–3 | **dimension** id |
| 4–7 | `i32` Start |
| 8–11 | `i32` Size |
| 12–15 | `f32` StartCoordinate |
| 16–19 | `i32` StoredSize |

The id MUST be one of the letters
`X Y Z C T R S I H V B M` followed by three NUL bytes, no letter twice in
one entry, and `X` and `Y` MUST be present; `d` is therefore 2 to 12. The letters:

| letter | dimension | Size and StoredSize |
|---|---|---|
| `X`, `Y` | the subblock's **logical rectangle** (Start, Size: where it lies, in the pixels of the full-resolution image) and its **stored size** (StoredSize: the pixels it holds) | MUST be at least 1 |
| `Z` | depth | MUST be 1 |
| `C` | channel | MUST be 1 |
| `T` | time | MUST be 1 |
| `R` | rotation | MUST be 1 |
| `S` | scene | MUST be 1 |
| `I` | illumination | MUST be 1 |
| `H` | phase | MUST be 1 |
| `V` | view | MUST be 1 |
| `B` | block, deprecated | MUST be 1 |
| `M` | the mosaic tile index | any Size, which some writers set wrongly on pyramid tiles |

For every letter but `X`, `Y` and `M`, whose Size and StoredSize are 1, the subblock lies at
index Start of that dimension.

**Subblock `i`** is the subblock of the `i`-th entry (`0 ≤ i < N`), its
**index**.

### 2.4 Subblocks

At an entry's FilePosition, which MUST be the offset of a `ZISRAWSUBBLOCK`
segment, the data is, by bytes from its start (the parts from `L` on start at `o + 32 + L`):

| bytes | field |
|---|---|
| 0–3 | `i32` MetadataSize `m`, at least 0 |
| 4–7 | `i32` AttachmentSize `a`, at least 0 |
| 8–15 | `i64` DataSize `n`, at least 0 |
| 16 to `L − 1` | the subblock's own copy of its directory entry (schema `DV`, with its own DimensionCount `d'`, 0 ≤ `d'` ≤ 40), padded to the **header length** `L = max(256, 16 + 32 + 20 × d')` bytes from the start of the data |
| `m` bytes from `L` | the subblock's **metadata** (an XML fragment, `<METADATA>...`) |
| `n` bytes from `L + m` | its **data** (its pixels, coded as Compression says) |
| `a` bytes from `L + m + n` | its **attachment** (for example a valid-pixel mask, when the metadata says `CHUNKCONTAINER`) |

All of these MUST lie within the file.

The subblock's copy of its entry is the `32 + 20 × d'` bytes after its first
16; it **agrees** with the directory when it has the same bytes as the
directory entry. A subblock whose copy does not agree is **unplaced** ([§3.2](#32-conforming-and-unplaced-subblocks)).

### 2.5 The metadata segment

When MetadataPosition is not 0, it MUST be the offset of a `ZISRAWMETADATA`
segment, whose data is, within the file:

| bytes | field |
|---|---|
| 0–3 | `i32` XmlSize `x`, at least 0 |
| 4–7 | `i32` AttachmentSize `b`, at least 0 |
| 8–255 | 248 spare bytes |
| `x` bytes from 256 | the **metadata XML** |
| `b` bytes from `256 + x` | the **metadata attachment** (unused by current writers) |

### 2.6 Attachments

When AttachmentDirectoryPosition is not 0, it MUST be the offset of a
`ZISRAWATTDIR` segment, whose data is, within the file:

| bytes | field |
|---|---|
| 0–3 | `i32` EntryCount `K` (0 ≤ `K` ≤ 2^16) |
| 4–255 | 252 spare bytes |
| `128 × K` bytes from 256 | `K` **attachment entries** of 128 bytes |

An entry of schema `A1` (bytes 0–1) is:

| bytes | field |
|---|---|
| 0–1 | `A1` |
| 2–11 | 10 spare bytes |
| 12–19 | `i64` FilePosition |
| 20–23 | `i32` FilePart (MUST be 0) |
| 24–39 | 16 bytes ContentGuid |
| 40–47 | 8 bytes ContentFileType |
| 48–127 | 80 bytes Name |

Its FilePosition MUST be the offset of a `ZISRAWATTACH` segment, whose data is, within the
file:

| bytes | field |
|---|---|
| 0–7 | `i64` DataSize `s`, at least 0 |
| 8–15 | 8 spare bytes |
| 16–143 | its own copy of the 128-byte entry |
| 144–255 | spare bytes to 256 |
| `s` bytes from 256 | the **attachment data** |

Attachment `k` is the `k`-th entry. An entry of another schema is kept as it is
([§5.3](#53-elements)) and not followed, as libCZI does.

Writers use these ContentFileTypes:

| ContentFileType | content |
|---|---|
| `CZTIMS` | TimeStamps |
| `CZEVL` | EventList |
| `CZFOC` | FocusPositions |
| `CZLUT` | LookupTables |
| `JPG` | Thumbnail |
| `CZI`, `ZISRAW` | an embedded CZI file: Label, SlidePreview, Prescan |
| `Zip-Comp`, `ZIP` | gzip: Profile |
| `CZEXP`, `CZHWS`, `CZMVM`, `CZFBMX` | XML |
| `PNG`, `CZPML`, `BINARY` | |

### 2.7 The metadata XML

The layout reads a few values of the metadata XML ([§4](#4-output)); all of it is kept
as bytes ([§5.3](#53-elements)). When the XML's bytes (after a leading `EF BB BF`, which is
skipped) are valid UTF-8 and at most 2^26 bytes, its text `X` is read by the
tag scan of [the TIFF convention §3](tiff.md#3-ome-xml) (skipped
sections, tags, attribute values with references decoded). Otherwise no
value is read from it.

**Elements.** The scan's tags nest: a start tag that is not self-closing
opens an element; an end tag closes the innermost open element of the same
name and every element opened after it (an end tag that matches no open
element is ignored). An element's **children** are the elements opened while
it is the innermost open one. The **root** is the first element opened at
depth 0. A **path** `A/B/C` names, from the root named `ImageDocument`, the
first child named `A`, its first child named `B`, and so on; `A/B/C[k]`
names the `k`-th (from 0) child named `C`. An element's **text** is read as
the TIFF convention reads a `UUID` element's text (the characters up to the
next tag, skipped sections removed, references decoded, whitespace trimmed).
A **decimal** is a text matching
`[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?` whose value is finite;
an **integer** is a text of one or more digits, at most 2^53 − 1.

The values read, each absent when its element or attribute is:

| value | unit | read from | as |
|---|---|---|---|
| `PX`, `PY`, `PZ` | meters | the text of `Value`, the first child of the first child `Distance` of `Metadata/Scaling/Items` whose attribute `Id` is `X`, `Y`, `Z` | a decimal that is positive, else absent |
| `INC` | seconds | the text of `Metadata/Information/Image/Dimensions/T/Positions/Interval/Increment` | a positive decimal |
| `BITS` | | the text of `Metadata/Information/Image/ComponentBitCount` | an integer |
| channel `k`'s info channel | | `Metadata/Information/Image/Dimensions/Channels/Channel[k]`: its attribute `Name`, the text of its child `Color`, and its child `ComponentBitCount` | `ComponentBitCount` an integer text |
| channel `k`'s display channel | | `Metadata/DisplaySetting/Channels/Channel[k]`: its `Name`, the text of its child `Color`, and its children `Low` and `High` | `Low` and `High` decimal texts: the display's black and white points as fractions of the pixel type's range, as libCZI reads them |
| scene `s`'s name | | the attribute `Name` of the first child `Scene` of `Metadata/Information/Image/Dimensions/S/Scenes` whose attribute `Index` is an integer equal to `s` | absent when that `Scene` has no `Name`, even if a later one has |

A **color** is a text `#AARRGGBB` or `#RRGGBB` of hexadecimal digits (either
case); its value is `RRGGBB` in uppercase.

## 3. Placement

### 3.1 Pixel types and codecs

| PixelType | name | data type | samples `p` | bytes per pixel `q` |
|---|---|---|---|---|
| 0 | Gray8 | `uint8` | 1 | 1 |
| 1 | Gray16 | `uint16` | 1 | 2 |
| 2 | Gray32Float | `float32` | 1 | 4 |
| 3 | Bgr24 | `uint8` | 3 | 3 |
| 4 | Bgr48 | `uint16` | 3 | 6 |
| 8 | Bgr96Float | `float32` | 3 | 12 |
| 9 | Bgra32 | `uint8` | 4 | 4 |
| 10 | Gray64ComplexFloat | `complex64` | 1 | 8 |
| 11 | Bgr192ComplexFloat | `complex64` | 3 | 24 |
| 12 | Gray32 | `int32` | 1 | 4 |
| 13 | Gray64Float | `float64` | 1 | 8 |

Any other PixelType rejects the input. A subblock's pixels are its stored
height rows of its stored width pixels, each `p` samples, interleaved, of
the data type, little-endian.

| Compression | name | pixel types | codecs after `transpose` ([conventions §3](../conventions.md#3-arrays)) | sample order |
|---|---|---|---|---|
| 0 | uncompressed | all | `bytes` | B, G, R, A |
| 1 | JpgFile | Gray8, Bgr24 | `imagecodecs_jpeg` | R, G, B |
| 4 | JpgXr | Gray8, Gray16, Gray32Float, Bgr24, Bgr48, Bgr96Float, Bgra32 | `imagecodecs_jpegxr` | R, G, B, A |
| 5 | Zstd0 | all | `bytes`, `zstd` | B, G, R, A |
| 6 | Zstd1 | all | `bytes`, `zstd`; with hi-lo packing `bytes`, `numcodecs.shuffle`, `zstd` | B, G, R, A |

Any other Compression (2 LZW, 3 lossless JPEG, 7 chunked-extensible, 100 to
999 camera raw, 1000 and up system raw, and unknown values), and a listed
Compression with a pixel type its row does not list, rejects the input.

Each codec's JSON form, exactly:

| codec | JSON form | what it is |
|---|---|---|
| `bytes` | `{"name": "bytes", "configuration": {"endian": "little"}}`, and `{"name": "bytes"}` (no configuration) for the 1-byte types `uint8` ([conventions §3](../conventions.md#3-arrays)) | |
| `zstd` | `{"name": "zstd", "configuration": {"level": 0, "checksum": false}}` | the zarr-extensions `zstd` codec; level 0 is the encoder's default and does not affect decoding |
| `imagecodecs_jpeg` | that of [conventions §3](../conventions.md#3-arrays) | one complete JPEG stream |
| `imagecodecs_jpegxr` | `{"name": "imagecodecs_jpegxr"}`, with no configuration | an array-to-bytes codec, each chunk one JPEG XR file (ITU-T T.832 with its TIFF-like container, as the subblock holds it), decoding as `imagecodecs.jpegxr_decode` (jxrlib) does to `[y, x]`, or to `[y, x, c]` with the samples in the order jxrlib gives them (R, G, B, and A for `Bgra32`), after which `transpose` applies. Its encoded form is jxrlib's, so it needs nothing beyond the chunk's shape and data type. It is not in the zarr-extensions registry; imagecodecs-zarr provides it to zarr-python (refkv's `images` extra installs it), and its registration and a Neuroglancer decoder are a follow-up ([design/plans/czi.md §7](../../design/plans/czi.md#7-decisions-on-the-open-questions)) |
| `numcodecs.shuffle` | `{"name": "numcodecs.shuffle", "configuration": {"elementsize": 2}}` | a bytes-to-bytes codec: numcodecs' `Shuffle` with element size 2, as zarr-python 3 names it (`zarr.codecs.numcodecs.Shuffle`). Its encoded bytes are byte 0 of every 2-byte element, then byte 1 of every element |

A Zstd1 subblock with hi-lo packing holds exactly what `numcodecs.shuffle` encodes: its
decompressed bytes are the low bytes of all its 16-bit values, then their high bytes
(libCZI's `LoHiBytePackStrided`), over the whole subblock, color samples included. So the
chain of such a subblock is `bytes`, `numcodecs.shuffle`, `zstd`: the zstd frame decompresses
to shuffled bytes, which unshuffle to the little-endian pixels. (A registry `shuffle` codec
would replace the `numcodecs.` name; that is a follow-up,
[design/plans/czi.md §7](../../design/plans/czi.md#7-decisions-on-the-open-questions).)

**Sample order.** The `c` samples of a pixel are in the order the codec gives them: as
stored (B, G, R, A) for uncompressed and Zstd data, and R, G, B (A) for JPEG and JPEG XR,
which decode to RGB. Transpose reorders axes, not indices, so the order is kept, and named in
`omero` ([§4.3](#43-image-arrays)).

A Zstd1 subblock's data starts with a **Zstd1 header**: the byte 1 (header
length 1, no packing), or the bytes 3, 1, `f` (header length 3, hi-lo
packing when bit 0 of `f` is set); the zstd frame follows it. A Zstd0
subblock's data is the zstd frame.

### 3.2 Conforming and unplaced subblocks

A subblock's **coded size** is the width and height its data holds, and its
**codec form** is its PixelType, Compression and, for Zstd1, its hi-lo flag. Its coded size,
by its Compression (`W`, `H` its stored width and height):

| Compression | coded size | when |
|---|---|---|
| uncompressed | its stored size | `n ≥ W × H × q`; the first `W × H × q` bytes are its pixels and the rest, if any, its **trailing bytes** |
| Zstd0, Zstd1 | its stored size | its Zstd1 header (for Zstd1) is one of those of [§3.1](#31-pixel-types-and-codecs), hi-lo packing is only on Gray16 and Bgr48, and its zstd frame header declares a content size of `W × H × q` bytes and no dictionary ([the profile §7.4](#74-codec-headers)) |
| JpgFile | the width and height of its JPEG frame header | SOF0, SOF1 or SOF2, 8-bit samples, `p` components |
| JpgXr | the `ImageWidth` and `ImageHeight` of its JPEG XR container | its `PixelFormat` is one this subblock's pixel type admits (the profile [§7.4](#74-codec-headers)) |

A subblock is **unplaced** when its copy of its entry does not agree ([§2.4](#24-subblocks)),
or when it has no coded size by these rules (its data is too short, or its
codec header cannot be read or does not match). An unplaced subblock is in
no image and is no tile: its data is kept as bytes ([§5.3](#53-elements)). Every other
subblock is **placed**, and **conforming** when its coded size is its stored
size. (Some writers store a pyramid edge tile of `ceil(N/2)` pixels in the
directory but encode `floor(N/2)`; such a JPEG or JPEG XR subblock is placed
but not conforming, and becomes a tile of its coded size, [§4.4](#44-tiles).)

### 3.3 Series

A placed subblock's **series key** is, for each of `S`, `B`, `H`, `I`, `R`,
`V` in this order, `(0)` when its entry lacks the dimension and `(1, Start)`
when it has it. Subblocks with equal keys form a **series**; series are
ordered by their keys (lexicographically, numbers by value). A series is one
image at most, because OME-NGFF 0.5 has room for one channel axis, one time
axis and space only: the other dimensions select the image.

Within a series, a subblock's **plane** is `(t, c, z)`, its `T`, `C` and `Z`
Start (0 for a dimension its entry lacks).

### 3.4 Layers

A placed subblock's **layer** says how far its stored pixels are minified
from the logical ones, by libCZI's rule
(`CSbBlkStatisticsUpdater::TryToDeterminePyramidLayerInfo`): with logical
size `(Wl, Hl)` and stored size `(W, H)`,

- if `Wl = W` and `Hl = H`, layer `(1, 0)`;
- otherwise let `f = Wl / W` if `W > H`, else `f = Hl / H` (binary64). The
  layer is `(2, n)` for the first row of this table with
  `v − δ ≤ f ≤ v + δ`, searched in order:

  | v | 2 | 4 | 8 | 16 | 32 | 64 | 128 | 256 | 512 | 1024 |
  |---|---|---|---|---|---|---|---|---|---|---|
  | δ | 0.1 | 0.2 | 0.4 | 0.8 | 1 | 1 | 1 | 2 | 4 | 10 |
  | n | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |

  and failing that `(3, n)` by this one:

  | v | 3 | 9 | 27 | 81 | 243 | 729 | 2187 |
  |---|---|---|---|---|---|---|---|
  | δ | 0.1 | 0.2 | 0.8 | 1.5 | 2 | 5 | 15 |
  | n | 1 | 2 | 3 | 4 | 5 | 6 | 7 |

  and failing both, the subblock has no layer.

The pyramid type byte is not used (libCZI calls it legacy). A layer-0 image
stored subsampled (as Airyscan "fast" modes do) has a layer above 0 and no
layer `(1, 0)`; one stored supersampled (PALM) has none.

### 3.5 Regular levels

Let `B` be the conforming subblocks of a series that have one layer `λ`.
`B` is **regular** when all of these hold:

1. All of `B` have the same codec form ([§3.2](#32-conforming-and-unplaced-subblocks)).
2. Let `Wl`, `Hl` be the largest logical width and height, `W`, `H` the
   largest stored width and height, in `B`. Then `Wl = F × W` and
   `Hl = F × H` for one integer `F ≥ 1`, the level's **factor**.
3. Let `x0`, `y0` be the least X and Y Start in `B`. Every subblock's X
   Start is `x0 + i × Wl` and Y Start is `y0 + j × Hl` for integers `i`,
   `j ≥ 0`, its **column** and **row**. Let `m` and `r` be one more than the
   largest column and row.
4. No two subblocks of `B` have the same plane, column and row.
5. Every subblock of a column `i < m − 1` has logical width `Wl` and stored
   width `W`; the subblocks of column `m − 1` all have the same logical width
   and the same stored width `W'` (`1 ≤ W' ≤ W`). Likewise every subblock of
   a row `j < r − 1` has logical height `Hl` and stored height `H`, and those
   of row `r − 1` all the same logical height and stored height `H'`.

That is: equal tiles on a lattice, without overlap, of which only the last
column may be narrower and the last row shorter, as the edge of the scanned
region clips them. Missing cells are allowed (they read as the fill value).
A regular `B` is a **level** of the series' image, with tile size `(W, H)`,
edge sizes `(W', H')`, origin `(x0, y0)` and factor `F`; its stored extent is
`(m − 1) × W + W'` by `(r − 1) × H + H'` pixels. A level is **clipped** when
`W' < W` or `H' < H`.

The levels of a series are ordered by factor, and levels of equal factor
by layer (`(kind, n)`, lexicographically). The first level's pixel type is
the **image's pixel type**; a level of another pixel type is not a level
of the image (its subblocks are tiles), so that every dataset of an image
has the same data type and the same samples.

When `B` is not regular, every subblock of `B` is a tile ([§4.4](#44-tiles)); so is every
placed subblock without a layer, and every placed subblock that is not
conforming. A series has an image when it has at least one level.

(Typical results: a confocal stack, a line scan or a non-overlapping tile
scan is one level; a widefield or slide-scanner tile scan, whose tiles
overlap at stage positions, gives tiles at layer 0, and its pyramid layers,
which ZEN computes on a lattice, give levels, clipped at the region's far
edges. A pyramid of "minimal coverage", whose tiles are clipped on any side
around an irregular scan region, gives tiles.)

## 4. Output

### 4.1 The hierarchy

The nodes, whose entries are listed in [Layout](#layout):

| node | when | what |
|---|---|---|
| `zarr.json` | always | the root group, declaring the convention ([§1](#1-declaration)). When some series has an image, its attributes have `"ome": {"version": "0.5", "bioformats2raw.layout": 3}` |
| `OME/zarr.json` | some series has an image | a group with `{"ome": {"version": "0.5", "series": ["0", "1", ...]}}`, one per image |
| `<k>/zarr.json` and its arrays `<k>/<d>` | for each image | image `k` ([§4.2](#42-images), [§4.3](#43-image-arrays)), the images numbered from 0 in series order |
| `tiles/zarr.json` | there is a tile | a group with empty attributes, holding the tile arrays `tiles/<n>`, numbered from 0 ([§4.4](#44-tiles)) |
| `refkv_source` | always | the source metadata ([§5](#5-source-metadata)) |

A file in which no series has an image (no subblock is placed, or every placed subblock is a
tile) is accepted: its output is the root, without `ome`, the tiles if any, and
`refkv_source`.

### 4.2 Images

Image `k` is an image ([conventions §4](../conventions.md#4-images)) whose
datasets are its series' levels in the order of [§3.5](#35-regular-levels), at paths
`"0"`, `"1"`, .... Its `name` is the scene name of its `S` Start
([§2.7](#27-the-metadata-xml)) when the series has `S` and the name is present, not empty and
at most 256 bytes in UTF-8; otherwise its multiscale has no `name`.

Its source metadata is a `CziImage` when its series key has a dimension; when the key has
none, it has no source metadata (and does not declare the convention, [§1](#1-declaration)):

<a id="type-cziimage"></a>**`CziImage`**, an image group's `S`:

| member | type | presence | value |
|---|---|---|---|
| `dimensions` | [`CziDimensions`](#type-czidimensions) | required | the dimensions of the series key |

<a id="type-czidimensions"></a>**`CziDimensions`**: the Start of each of `S`, `B`, `H`, `I`,
`R`, `V` the series key has, in that order (at least one):

| member | type | presence | value |
|---|---|---|---|
| `S` | integer | optional | the scene's Start, when the key has `S` |
| `B` | integer | optional | the block's Start, when the key has `B` |
| `H` | integer | optional | the phase's Start, when the key has `H` |
| `I` | integer | optional | the illumination's Start, when the key has `I` |
| `R` | integer | optional | the rotation's Start, when the key has `R` |
| `V` | integer | optional | the view's Start, when the key has `V` |

**Axes.** Over the subblocks of all the image's levels, let `lo_t`, `hi_t`
be the least and largest `t` of their planes, and likewise for `c` and `z`.
The axes are `t` if `hi_t > lo_t`; `c` if `hi_c > lo_c` or `p > 1`; `z` if
`hi_z > lo_z`; then `y`, `x`. A plane's index along `t` is `t − lo_t`, along
`z` is `z − lo_z`; channel `c` occupies the indexes
`(c − lo_c) × p` to `(c − lo_c) × p + p − 1`, one per sample. (A level that
lacks some planes has those chunks absent.)

### 4.3 Image arrays

The array of a level, at `<k>/<d>` ([conventions §3](../conventions.md#3-arrays)):

<a id="type-czilevelarray"></a>**`CziLevelArray`**, a level's `zarr.json`: an
[`ArrayDocument`](../conventions.md#type-arraydocument) with these members:

| member | type | presence | value |
|---|---|---|---|
| `shape` | array of integer | required | `hi_t − lo_t + 1`, `(hi_c − lo_c + 1) × p`, `hi_z − lo_z + 1` (each for its axis), then the stored extent `(r − 1) × H + H'` and `(m − 1) × W + W'` |
| `data_type` | string | required | the image's pixel type's ([§3.1](#31-pixel-types-and-codecs)) |
| `chunk_grid` | object or [`CziRectilinearGrid`](#type-czirectilineargrid) | required | from the chunk lengths (**Chunk grid**, below): when every length is an integer, the regular grid of those lengths, `{"name": "regular", "configuration": {"chunk_shape": [...]}}`; otherwise (the level is **clipped**, or has short row bands) its chunk grid is a `CziRectilinearGrid` |
| `fill_value` | number or array of number | required | 0 (`[0.0, 0.0]` for `complex64`) |
| `codecs` | array of object | required | [§3.1](#31-pixel-types-and-codecs)'s, of the level's codec form, with `transpose` ([conventions §3](../conventions.md#3-arrays)) first when `p > 1` |
| `dimension_names` | array of string | required | the image's axes ([§4.2](#42-images)) |
| `attributes` | object | required | `{}` |

**Chunk grid.** The chunk lengths are 1 for `t` and `z`, `p` for `c`;
for `y` the integer `H` when `H' = H`, else the list `[[H, r − 1], H']`
(`r − 1` chunks of `H` rows, then one of `H'`), or with row bands (below)
the integer `b` of divisor bands or the list of short bands; for `x` the
integer `W` when `W' = W`, else `[[W, m − 1], W']`. (`r = 1` gives
`H' = H`, and `m = 1` gives `W' = W`, since `H` and `W` are the largest
stored sizes, so the lists of a clipped axis always have their run.) The
chunk grid of a level with a list among its lengths, clipped or of short
bands, is the zarr-extensions `rectilinear` grid
([chunk-grids/rectilinear](https://github.com/zarr-developers/zarr-extensions/tree/main/chunk-grids/rectilinear)),
exactly
`{"name": "rectilinear", "configuration": {"kind": "inline", "chunk_shapes": [L_0, ..., L_{d−1}]}}`:

<a id="type-czirectilineargrid"></a>**`CziRectilinearGrid`**, the chunk grid of a clipped level, or of a level or
tile array ([§4.4](#44-tiles)) of short row bands:

| member | type | presence | value |
|---|---|---|---|
| `name` | string | required | `"rectilinear"` |
| `configuration` | [`CziRectilinearConfiguration`](#type-czirectilinearconfiguration) | required | the chunk lengths |

<a id="type-czirectilinearconfiguration"></a>**`CziRectilinearConfiguration`**:

| member | type | presence | value |
|---|---|---|---|
| `kind` | string | required | `"inline"` |
| `chunk_shapes` | array of (integer or array of (integer or array of integer)) | required | `[L_0, ..., L_{d−1}]`, `L_j` the length of axis `j`, in array order: an integer for a regular axis, and otherwise the list of its chunk lengths, in which an element `[v, n]` stands for `n` chunks of `v` and an integer for one chunk: for a clipped axis `[[H, r − 1], H']` (or `[[W, m − 1], W']`), and for `y` with short row bands the list of **Row bands** (below). The chunk lengths of a listed axis sum to its shape exactly |

For example, 16 columns of 1024 pixels whose last
is 959 wide, and 10 full rows, give `"chunk_shapes": [1024, [[1024, 15], 959]]`
for `[y, x]`. zarr-python 3.4 reads such an array (as `read_chunk_sizes`
shows) once `zarr.config.set({"array.rectilinear_chunks": True})` is set;
without it, it refuses the array's metadata.

**Row bands.** When the codec form is uncompressed and `W × H × q`
exceeds 2^24 bytes, chunks are bands of `b` rows. Let `b'` be the largest
divisor of `gcd(H, H')` with `b' × W × q ≤ 2^24` (at least 1):

- When `b' × W × q` is at least 2^20 bytes, the bands are **divisor
  bands** of `b = b'` rows. The `y` chunk length is then `b` (a regular
  grid of `b`, or in a rectilinear grid the integer `b`), and a tile of row
  `j` is the chunks `j × H / b` to `j × H / b + h / b − 1`, `h` being its
  stored height.
- Otherwise (as when `H` is prime, whose divisor bands would be one row
  each) the bands are **short bands** of `b = max(1, ⌊2^24 / (W × q)⌋)`
  rows, the most within 2^24 bytes, and the last band of each tile row is
  shorter: a tile row of height `h` (`H`, or `H'` for row `r − 1`) is
  `⌊h / b⌋` bands of `b` rows, then one of `h mod b` rows when `b` does
  not divide `h`. The `y` chunk length is the list of these bands'
  heights, tile row by tile row, in which consecutive equal heights are one
  element `[v, n]` (`n ≥ 2`) and a height alone is its integer, so the
  level's chunk grid is a [`CziRectilinearGrid`](#type-czirectilineargrid),
  whether or not it is clipped. A tile of row `j` is the chunks
  `j × ⌈H / b⌉` to `j × ⌈H / b⌉ + ⌈h / b⌉ − 1`, its band `k` rows `k × b`
  to `min(k × b + b, h) − 1`. For example, uncompressed tiles 4096 pixels
  wide and 4099 high of `q = 4`, in 3 full rows, have `b' = 1` (4099 is
  prime) and `b = 1024`, and the `y` length
  `[[1024, 4], 3, [1024, 4], 3, [1024, 4], 3]`.

**Scale and translation.** Every level has both; those of a level of factor `F` and origin
`(x0, y0)` ([§3.5](#35-regular-levels)) are:

| axis | scale | unit | translation |
|---|---|---|---|
| `x` | `PX × F`, the meters converted by [conventions §5](../conventions.md#5-units) (`v × (1 / 1e-6)`); or `F` when `PX` is absent | `micrometer`; none when `PX` is absent | `(x0 + (F − 1) / 2) × PX'` |
| `y` | `PY × F`, converted likewise; or `F` when `PY` is absent | `micrometer`; none when `PY` is absent | `(y0 + (F − 1) / 2) × PY'` |
| `z` | `PZ`, converted likewise; or 1 | `micrometer`; none without `PZ` | 0 |
| `t` | `INC`; or 1 | `second`; none without `INC` | 0 |
| `c` | 1 | none | 0 |

`PX'` is `PX` in micrometers or 1 without it, and `PY'` likewise (computed in that order:
`F − 1`, the division, the sum, the product). No time step is derived from a TimeStamps
attachment when `INC` is absent: its times are kept ([§5.3](#53-elements)), and need not
be evenly spaced. Translations are in CZI's **global pixel
frame**, scaled by the pixel size: subblock positions are pixels of one
frame for the whole file (X Start may be negative), so images of
different scenes, levels and tiles keep their places relative to each
other. The stage frame (a scene's `CenterPosition`, a subblock's
`StageXPosition`) is not used. The `(F − 1) / 2` puts a minified pixel's
center at the center of the full-resolution pixels it covers.

**omero.** When the image has at most 64 channel indexes, its `M` has
`"omero": {"channels": [...]}`, one [`OmeroChannel`](../conventions.md#type-omerochannel) per
index: for channel `c` (its absolute Start, not `c − lo_c`) and sample `s`:

| member | with `p = 1` | with `p > 1` |
|---|---|---|
| `label` | the info channel `c`'s `Name` if present, not empty and at most 256 bytes, else the display channel's, else `C<c>` | that label followed by a space and the sample's letter in the sample order of [§3.1](#31-pixel-types-and-codecs) (`B`, `G`, `R`, `A` or `R`, `G`, `B`, `A`) |
| `color` | the display channel's `Color`, else the info channel's, else `FFFFFF` | `0000FF` for B, `00FF00` for G, `FF0000` for R and `FFFFFF` for A |
| `active` | `true` | `true` |
| `window` | for `uint8` and `uint16`, `{"min": 0, "max": V, "start": S, "end": E}` (below); no window for other types | every sample of channel `c` has channel `c`'s window |

In the window, `V = 2^b − 1`, `b` the info
channel's `ComponentBitCount` when it is from 1 to the type's bits, else
`BITS` when it is, else the type's bits. `S` is the display channel's
`Low` times `T`, and `E` its `High` times `T` (each a product in
binary64), `T = 2^bits − 1` with the type's bits (255 or 65535: libCZI
scales the black and white points by the type's range, not by `b`);
without `Low`, `S` is 0, and without `High`, `E` is `V`.

**Chunks:** subblock `i` of the level, of plane `(t, c, z)`, column `i'`
and row `j`, is the chunk at `t − lo_t`, `c − lo_c`, `z − lo_z` (each for
its axis), `j`, `i'` (or its bands), a [`CziChunk`](#type-czichunk). The chunk holds the subblock's pixels
as its codec form codes them: the first `W × H × q` bytes of its data for
uncompressed (or a band's rows of them), the zstd frame after its Zstd1
header, the JPEG stream, or the JPEG XR file, each being its whole data
otherwise. Cells without a subblock have no chunk.

### 4.4 Tiles

The placed subblocks that are in no level of an image ([§3.5](#35-regular-levels)) are **tiles**.
One Zarr array holds all the planes of one **tile position**, not one
subblock:

- Two tiles have the same **position** when they are in the same series and
  have the same X and Y Start, logical width and height, stored width and
  height, coded width and height, and codec form. Tiles at the same X and Y
  Start whose sizes or codec forms differ (as a JPEG pyramid tile coded one
  row short, beside a full one) are different positions, and so different
  arrays.
- Taking the tiles in index order, a tile is in **copy** `k` of its position,
  `k` being the number of tiles before it with the same position and the
  same plane. So all the planes (`T`, `C`, `Z`) of a position share copy 0,
  and a tile whose position and plane repeat an earlier tile's (the same
  field acquired twice, at another `M`) is in copy 1, and so on: no two
  tiles of one copy have the same plane.

Each copy of each position is a **tile array** `tiles/<n>`
([conventions §3](../conventions.md#3-arrays)), numbered from 0 in increasing
order of the index of its first tile:

<a id="type-czitilearray"></a>**`CziTileArray`**, a tile array's `zarr.json`: an
[`ArrayDocument`](../conventions.md#type-arraydocument) with these members, its axes, shape and
chunks as an image's level's ([§4.2](#42-images), [§4.3](#43-image-arrays)) over the array's
own tiles:

| member | type | presence | value |
|---|---|---|---|
| `shape` | array of integer | required | `lo` and `hi` of `t`, `c` and `z` over its tiles' planes give the extents of `t`, `c` (times `p`) and `z`, each for its axis; then its coded height and width |
| `data_type` | string | required | that of an image of its pixel type ([§4.3](#43-image-arrays)) |
| `chunk_grid` | object or [`CziRectilinearGrid`](#type-czirectilineargrid) | required | 1 for `t` and `z`, `p` for `c`, the coded height and the coded width, a regular grid; or, for a large uncompressed tile, the row bands of [§4.3](#43-image-arrays) in place of the coded height, with `W` the coded width, `H = H'` the coded height and `r = 1`: divisor bands give the regular grid of `b`, and short bands a `CziRectilinearGrid` whose `y` length is the list of the tile's bands (`[[b, n], H mod b]`, `n = ⌊H / b⌋`, or `[b, H mod b]` when `n` is 1) |
| `fill_value` | number or array of number | required | that of an image of its pixel type ([§4.3](#43-image-arrays)) |
| `codecs` | array of object | required | those of an image of its pixel type and codec form ([§4.3](#43-image-arrays)), `transpose` first when `p > 1` |
| `dimension_names` | array of string | required | its axes: `lo` and `hi` of `t`, `c` and `z` over its tiles' planes give them, as an image's ([§4.2](#42-images)) |
| `attributes` | object | required | a [`Declaration`](../conventions.md#type-declaration) whose source metadata is a [`CziTile`](#type-czitile) |

**Chunks:** the tile of plane `(t, c, z)` is the chunk at `t − lo_t`,
`c − lo_c`, `z − lo_z` (each for its axis), `0`, `0` (or its bands), a
[`CziChunk`](#type-czichunk), holding its pixels as [§4.3](#43-image-arrays) says. Planes
without a tile have no chunk.

**Source metadata:** where the array lies, and which planes it starts at; for example

```json
{"dimensions": {"S": 0}, "x": -104227, "y": 20275, "size": [1504, 2048],
 "stored_size": [1504, 2048], "planes": {"t": 0, "c": 0, "z": 0}, "copy": 0}
```

<a id="type-czitile"></a>**`CziTile`**, a tile array's `S`:

| member | type | presence | value |
|---|---|---|---|
| `dimensions` | [`CziDimensions`](#type-czidimensions) | optional | as an image's ([§4.2](#42-images)), absent when the series key has none |
| `x` | integer | required | the X Start |
| `y` | integer | required | the Y Start |
| `size` | array of integer | required | the logical width and height (the region of the full-resolution frame it covers), each at least 1 |
| `stored_size` | array of integer | required | the stored width and height, each at least 1 |
| `planes` | [`CziPlanes`](#type-cziplanes) | required | the planes at its index 0 |
| `copy` | integer | required | its copy `k`, at least 0 |

<a id="type-cziplanes"></a>**`CziPlanes`**: the `T`, `C` and `Z` Start of index 0 along each
of `t`, `c` and `z` (for an axis the array lacks, the one plane all its tiles have):

| member | type | presence | value |
|---|---|---|---|
| `t` | integer | required | the `T` Start of index 0 along `t` |
| `c` | integer | required | the `C` Start of index 0 along `c` |
| `z` | integer | required | the `Z` Start of index 0 along `z` |

A tile array's document has these members only ([`CziTileArray`](#type-czitilearray),
[`CziTile`](#type-czitile)), so its size is bounded
whatever the number of planes; the number of tile arrays is at most the
number of subblocks. Which subblock each chunk holds follows from the
directory entries ([§5.3](#53-elements)): subblock `i`, of series key, position and plane
as its entry's dimension entries give, is the chunk of its plane in the array of its position
and copy. Tiles carry no OME-NGFF metadata: their place in an image's frame
is `x`, `y`, `size`, and their pixel size the image's scaled by `size` over
the coded size. (With 1000 positions of 4 channels and 50 planes, a tile
scan has 1000 tile arrays, not 200000.)

## 5. Source metadata

### 5.1 The root

<a id="type-cziroot"></a>**`CziRoot`**, the root's `S`: the file header
([§2.2](#22-the-file-header)):

| member | type | presence | value |
|---|---|---|---|
| `version` | array of integer | required | `[Major, Minor]` |
| `primary_file_guid` | string | required | PrimaryFileGuid, as [§2.2](#22-the-file-header) writes a GUID |
| `file_guid` | string | required | FileGuid, likewise |
| `file_part` | integer | required | `0` |
| `update_pending` | integer | required | UpdatePending |

Everything else is on `refkv_source`.

### 5.2 The IR mirror

`refkv_source` is the file's **IR mirror**
([conventions §11.3](../conventions.md#113-the-ir-mirror)).
The IR's elements (the CZI **source model**) are below; nothing is left out, deleted
and unreferenced segments, spare bytes and dead space included.

### 5.3 Elements

Paths are relative to the IR's root, whose path is `""`
([conventions §8.1](../conventions.md#81-elements)); `<i>`, `<k>` are decimal name indexes.
Every segment struct's extent is its 32-byte header and its allocated
size (or the header alone when the allocation does not lie within the
file), and holds `header`, a value of type
`{id:cstr[16],allocated_size:<i8,used_size:<i8}`.

| path | kind | type | what |
|---|---|---|---|
| `""` | struct | | the root: the whole file, extent `(0, size)` |
| `file_header` | struct | | the `ZISRAWFILE` segment |
| `file_header/fields` | value | record | its fields (version, GUIDs, FilePart, the positions of the directory, metadata and attachment directory segments, UpdatePending) |
| `directory` | struct | | the `ZISRAWDIRECTORY` segment |
| `directory/entry_count` | value | `<i4` | `N` |
| `directory/entries/<i>` | value | record | entry `i`'s 32 bytes (schema, pixel type, FilePosition, FilePart, compression, pyramid type, spare, dimension count) |
| `directory/dimensions/<i>` | value | `{dimension:ascii[4],start:<i4,size:<i4,start_coordinate:<f4,stored_size:<i4}[d]` | entry `i`'s dimension entries |
| `subblocks/<i>` | struct | | the `ZISRAWSUBBLOCK` segment entry `i` names; an alias when an earlier entry names the same segment |
| `subblocks/<i>/sizes` | value | `{metadata_size:<i4,attachment_size:<i4,data_size:<i8}` | its sizes |
| `subblocks/<i>/entry`, `subblocks/<i>/dimensions` | value | as the directory's | its own copy of its entry |
| `subblocks/<i>/metadata` | value | `ascii[m]` | its metadata |
| `subblocks/<i>/data` | data | | a placed subblock's pixels ([§3.2](#32-conforming-and-unplaced-subblocks)): after a Zstd1 header, or the first `w × h × q` bytes of uncompressed data; form geometry `{"shape": [h, w(, samples)], "dtype", "pixel_type", "compression"}`, codec the chain of [§3.1](#31-pixel-types-and-codecs) |
| `subblocks/<i>/data` | value | `bytes[n]` | an unplaced subblock's data |
| `subblocks/<i>/zstd1_header` | value | `bytes[n]` | a Zstd1 subblock's header |
| `subblocks/<i>/trailing` | value | `bytes[n]` | uncompressed data after the pixels |
| `subblocks/<i>/attachment` | value | `bytes[a]` | its attachment |
| `metadata` | struct | | the `ZISRAWMETADATA` segment |
| `metadata/sizes` | value | `{xml_size:<i4,attachment_size:<i4}` | |
| `metadata/xml`, `metadata/attachment` | value | `ascii[n]`, `bytes[n]` | the metadata XML ([§2.7](#27-the-metadata-xml)) and its attachment |
| `attachment_directory` | struct | | the `ZISRAWATTDIR` segment |
| `attachment_directory/entry_count` | value | `<i4` | `K` |
| `attachment_directory/entries/<k>` | value | record (A1) or `bytes[128]` | entry `k` |
| `attachments/<k>` | struct | | the `ZISRAWATTACH` segment entry `k` names (an alias when an earlier entry names it) |
| `attachments/<k>/data_size`, `attachments/<k>/entry` | value | `<i8`, record | |
| `attachments/<k>/data` | struct, value or derived | | the attachment data: for TimeStamps and FocusPositions, a struct of `size` (`<i4`), `count` (`<i4`) and `values` (`<f8[n]` or `<f4[n]`); for an EventList, `size`, `count` and `events` (`{size:<i4,time:<f8,type:<i4,description_size:<i4}[n]`) or `events/<e>` and `descriptions/<e>`; for `Zip-Comp` and `ZIP`, a derived element (transform gzip, not inflated); otherwise `bytes[s]` |
| `segments/<k>` | struct | | the walk's other segments (deleted, or referenced by nothing), in file order, with `header` and `data` (`bytes[n]`) |
| `tail` | value | `bytes[n]` | the bytes after the last segment the walk reads |
| `gaps/<offset>` | gap | | spare bytes, unused allocations and any other bytes no element claims |

### 5.4 Equivalence

Hierarchies of one file are compared entry for entry, `refkv_source` included
([conventions §11.4](../conventions.md#114-equivalence)).

## 6. Example

The root of a slide scan at `https://example.org/a.czi` (one scene, Gray16
JPEG XR, overlapping layer-0 tiles and five pyramid levels; this is
`2023_11_30__RecognizedCode-27.czi` of the corpus):

```json
{
  "zarr_format": 3,
  "node_type": "group",
  "attributes": {
    "ome": {"version": "0.5", "bioformats2raw.layout": 3},
    "zarr_conventions": ["(the CMO of §1)"],
    "refkv_zarrified": {
      "profile": "czi",
      "version": 0,
      "source": {"url": "https://example.org/a.czi"},
      "czi": {
        "version": [1, 0],
        "primary_file_guid": "7d8bda89-6a0c-45d1-99a2-5ed155788eba",
        "file_guid": "7d8bda89-6a0c-45d1-99a2-5ed155788eba",
        "file_part": 0,
        "update_pending": 0
      }
    }
  }
}
```

Image `0`'s `ome` (levels of factor 2, 4, 8, 16 and 32; level 0 has
`x0 = -104227`, `y0 = 20275`; `PX = PY = 3.444225755520869E-07` m):

```json
{
  "version": "0.5",
  "multiscales": [{
    "name": "ScanRegion0",
    "axes": [
      {"name": "y", "type": "space", "unit": "micrometer"},
      {"name": "x", "type": "space", "unit": "micrometer"}
    ],
    "datasets": [
      {"path": "0", "coordinateTransformations": [
        {"type": "scale", "scale": [0.6888451511041738, 0.6888451511041738]},
        {"type": "translation", "translation": [6983.3399306063375, -35897.95957077958]}]},
      "..."
    ]
  }],
  "omero": {"channels": [{"label": "EGFP", "color": "00FF5B", "active": true,
    "window": {"min": 0, "max": 16383, "start": 0, "end": 16383.75}}]}
}
```

Its level `0` (16 columns, 10 rows of 1024 × 1024 tiles, the last column
959 wide) is `uint16` of shape `[10240, 16319]` with the chunk grid
`{"name": "rectilinear", "configuration": {"kind": "inline", "chunk_shapes":
[1024, [[1024, 15], 959]]}}` and codecs
`[{"name": "imagecodecs_jpegxr"}]`. Each of the 264 overlapping layer-0 tiles
is a position of its own, the array `tiles/<n>` of shape `[2048, 1504]`;
`refkv_source` is the IR mirror ([§5.2](#52-the-ir-mirror)): its elements include the metadata
XML (`metadata/xml`), the directory's entries and dimensions, each subblock's
segment, `attachments/0` to `attachments/5` (EventList, TimeStamps, the
embedded Label and SlidePreview CZIs as bytes, the gzip Profile as a derived
element, and the JPEG Thumbnail), and the two `DELETED` segments the walk
finds (`segments/<k>`). (The display channel's `High` is 0.25: the window ends at
0.25 × 65535.)

# Part 2. The profile

The CZI profile of [spec/zarrify.md](../zarrify.md) is [§7](#7-czi-profile) of this document. (Conformance is spec/zarrify.md's [Appendix A](../zarrify.md#appendix-a-conformance).) What every profile's Part 2 shares is in
[spec/zarrify.md, Profiles](../zarrify.md#profiles).

## 7. CZI profile

### 7.1 Detection

**Signature** ([spec/zarrify.md §1.2](../zarrify.md#12-input), tested at the precedence of
[profiles/czi.json](profiles/czi.json)): bytes 0–15 of `H` are
`5A 49 53 52 41 57 46 49 4C 45` (`ZISRAWFILE`) followed by six `00` bytes. No other profile's
signature matches these bytes.

### 7.2 Structures and the walk

Reading a segment header at `o` means reading its 32 bytes, which MUST lie
within the file; when the segment is referenced (steps 1–5), its id MUST
be the one expected, and its AllocatedSize and UsedSize MUST be from 0 to
2^53 − 1. Reading a structure means reading its bytes, which MUST lie
within the file; every offset and length read MUST be from 0 to
2^53 − 1 and every size field the convention says is at least 0 MUST be.
The zarrifier reads, in any order ([spec/zarrify.md §1.2](../zarrify.md#12-input), evaluation order):

1. the file header: the segment header at 0 and its 80 bytes of data (the
   convention [§2.2](#22-the-file-header));
2. the directory: the segment header at SubBlockDirectoryPosition, then the
   4-byte EntryCount and the entries, from `o + 160` to at most
   `o + 32 + U` (`U` its UsedSize, or AllocatedSize when `U` is 0). An entry
   that does not end within these bytes rejects the input. EntryCount is
   checked (0 to 2^21) before any entry is read;
3. for each entry `i`: the segment header at its FilePosition and the
   subblock's first `L` bytes (`L` from its `d'`, [the convention §2.4](#24-subblocks); the
   first 256 bytes MUST lie within the file before `d'` is read), and,
   when its copy of its entry agrees with the directory, the codec header of
   its data ([§7.4](#74-codec-headers));
4. when MetadataPosition is not 0, the metadata segment's header and first
   8 bytes of data, and the XML when [§2.7](#27-the-metadata-xml) of the convention reads it;
5. when AttachmentDirectoryPosition is not 0, the attachment directory's
   header, EntryCount (0 to 2^16, checked first) and entries; for each `A1`
   entry, the attachment segment's header and first 256 bytes of data; and
   the data of attachments whose form the convention decodes (`CZTIMS`,
   `CZFOC`: only bytes 0–7; `CZEVL`: all of it, when `s` is at most 2^26);
6. **the walk.** From `o = 0`: if `o` is the file's size, the walk ends with
   an empty tail. Otherwise the segment header at `o` is a **segment** when
   it lies within the file, its id is 1 to 16 bytes of `A`–`Z`, `0`–`9` and
   `_` followed by NUL bytes to 16, its AllocatedSize `A` and UsedSize `U`
   are at least 0, and `o + 32 + A` is at most the file's size; the walk
   then continues at `o + 32 + A`. When it is not a segment, or after
   2^23 segments, the walk ends and the tail is the bytes from `o` to the
   end of the file. Only segment headers are read; a zarrifier MAY take
   those of referenced segments from step 1–5's reads, and read only the
   others, in order.

### 7.3 Rejection

The input is rejected when a read of [§7.2](#72-structures-and-the-walk) steps 1–5 lies outside the file,
when a rule of this profile fails, and wherever the convention says the
input is rejected or that something MUST hold: Major and FilePart ([§2.2](#22-the-file-header)),
the directory and its entries ([§2.3](#23-the-subblock-directory): segment id, EntryCount, schema `DV`,
dimension ids, counts and sizes), the subblock segments ([§2.4](#24-subblocks): id, sizes,
schema `DV` and `d'` of the subblock's copy, parts within the file), the
metadata segment and attachment directory when present ([§2.5](#25-the-metadata-segment), [§2.6](#26-attachments): ids,
counts, parts within the file, FilePart), pixel types and compressions
([§3.1](#31-pixel-types-and-codecs)), and the limits below. What only [§3.2](#32-conforming-and-unplaced-subblocks) decides (a subblock's coded
size) never rejects: such a subblock is unplaced. The walk ([§7.2](#72-structures-and-the-walk) step 6)
never rejects. Neither does what only the source metadata reads (the
attachment forms, the budget), nor the metadata XML.

**Limits.** These bound memory, time and the documents; exceeding one
rejects the input:

| limit | value | bounds |
|---|---|---|
| directory entries `N` | 2^21 | requests (one subblock header each), the mirror's table ([conventions §8](../conventions.md#8-the-ir-mirror)) |
| attachment entries `K` | 2^16 | the mirror's table |
| series with an image | 2^16 | the `OME` series list (about 7 bytes per series), as for ND2 positions |
| levels per image | 64 | the image's `multiscales` (the layer tables give at most 18) |
| array extent | every dimension of an image's or tile array's shape (`t`, `c` with its samples, `z`, and the stored extent of `y` and `x`) at most 2^31 | shapes |

The walk stops at 2^23 segments, the XML is read for the layout only when
at most 2^26 bytes, codec headers are scanned within 2^16 bytes, and event
lists are decoded only up to 2^20 events and 2^26 bytes; past those the
convention keeps the bytes as they are, without rejecting. Every document
is then bounded: the root's and each image's by the limits above (`omero`
has at most 64 channels), each tile array's by its fixed members (the
convention [§4.4](#44-tiles)), whatever its number of planes, and `refkv_source`'s by
the mirror's budgets ([conventions §8.7](../conventions.md#87-the-view)). The tile arrays number at most `N`.

### 7.4 Codec headers

Each is read from the start of the subblock's data `D` (its `n` bytes); a
read past `n` bytes, or past 2^16 bytes into `D`, means the subblock has no
coded size ([the convention §3.2](#32-conforming-and-unplaced-subblocks)). They are coding parameters, which the
output may depend on ([spec/zarrify.md §1.2](../zarrify.md#12-input), structure only).

- **Zstd1 header:** `D[0]` is 1 (length 1, no packing), or `D[0]` is 3,
  `D[1]` is 1, and `D[2]` gives the hi-lo flag in bit 0 (length 3). Any
  other first bytes, or `n` not more than the header length, give no coded
  size.
- **Zstd frame header**, at the start of the frame (after the Zstd1 header
  for Zstd1): the magic `28 B5 2F FD`, then the frame header descriptor
  byte `h`, with `h` bit 3 (reserved) clear and dictionary flag `h & 3` equal
  to 0; a window descriptor byte when bit 5 (single segment) is clear; then
  the content size field, of 8, 4, 2 or (with single segment) 1 bytes for
  `h >> 6` of 3, 2, 1 and 0 (no field for 0 without single segment, which
  gives no coded size), little-endian, plus 256 for the 2-byte field. The
  subblock has its stored size as coded size when that content size is
  `W × H × q` ([the convention §3.2](#32-conforming-and-unplaced-subblocks)).
- **JPEG:** `D` starts with `FF D8`. Then markers are scanned: at each, one
  or more `FF` bytes and a marker byte `M`; for `M` in `D0`–`D7` or `01`
  nothing follows; otherwise a 2-byte big-endian length `l ≥ 2` and `l − 2`
  bytes. The first `M` in `C0`–`CF` other than `C4`, `C8` and `CC` is the
  frame header: precision (1 byte), height and width (2 bytes each,
  big-endian) and the component count (1 byte). There is a coded size when
  `M` is `C0`, `C1` or `C2`, the precision is 8, the height and width are at
  least 1 and the component count is the pixel type's `p`; a start of scan
  (`DA`) or end of image (`D9`) before the frame header gives none.
- **JPEG XR:** `D` starts with `49 49 BC 01`; the `u32` at byte 4 is the
  offset of the image directory: a `u16` count `e` and `e` entries of 12
  bytes (`u16` tag, `u16` type, `u32` count, `u32` value or offset). Of the
  first entries with tags `BC01` (PixelFormat: type 1, count 16, its offset
  giving the 16-byte GUID), `BC80` (ImageWidth) and `BC81` (ImageHeight,
  each of type 3 or 4 and count 1, the value in the first 2 or 4 bytes of the
  value field), all MUST be present. The coded size is
  (ImageWidth, ImageHeight), each at least 1, when the PixelFormat is one the subblock's
  pixel type admits. The pixel formats each pixel type admits are, by the GUID's bytes as
  stored (the common prefix `24 C3 DD 6F 03 4E FE 4B B1 85 3D 77 76 8D C9` and a last byte):

  | pixel type | PixelFormat |
  |---|---|
  | Gray8 | `08` |
  | Gray16 | `0B` |
  | Gray32Float | `11` |
  | Bgr24 | `0C` (24bppBGR) or `0D` (24bppRGB) |
  | Bgr48 | `15` |
  | Bgra32 | `0F` |
  | Bgr96Float | the GUID `8F D7 FE E3 DB E8 CF 4A 84 C1 E9 7F 61 36 B3 27` |

### 7.5 Chunk references

All references are ranges of source 0; this profile has no data sources.
Let `P = o + 32 + L + m` be subblock `i`'s data offset ([the convention §2.4](#24-subblocks)).

<a id="type-czichunk"></a>**`CziChunk`**, the chunk of a subblock of an image's level or a tile
array ([the convention §4.3](#43-image-arrays), [§4.4](#44-tiles)), whose data starts at `P`
and is `n` bytes, of stored width `W` and height `H`: one range, by its codec form:

| part | range | presence | value |
|---|---|---|---|
| the pixels, uncompressed | source range `(0, P, W × H × q)` | optional | when uncompressed without row bands: the single range |
| a band, uncompressed | source range `(0, P + k × b × W × q, min(b, H − k × b) × W × q)` | optional | with row bands of `b` rows ([the convention §4.3](#43-image-arrays)): band `k` of its rows, `b` rows but for the last band of short bands, which has `H − k × b` |
| the zstd frame, Zstd0 | source range `(0, P, n)` | optional | for a Zstd0 subblock |
| the zstd frame, Zstd1 | source range `(0, P + l, n − l)` | optional | for a Zstd1 subblock, `l` its header length |
| the JPEG stream or JPEG XR file | source range `(0, P, n)` | optional | for a JPEG or JPEG XR subblock |

The references by kind:

- **Image chunks and tiles** ([the convention §4.3](#43-image-arrays), [§4.4](#44-tiles)): each
  is a [`CziChunk`](#type-czichunk) (above), one range of the subblock's data by its codec
  form. Pixels are never copied.
- **`refkv_source`** is the IR mirror ([the convention §5.2](#52-the-ir-mirror)), with
  [the mirror's entries](../conventions.md#table-mirror) only: the elements of
  [the convention §5.3](#53-elements) are in its table, and the profile writes no
  arrays of its own under it.
- **Documents** of `tiles/<n>` are stored, like those of `refkv_source`, so
  that a reader fetches them only when it opens the node ([conventions §2](../conventions.md#2-attributes)):
  there is one per tile position and copy.

### 7.6 Equivalence notes

The hi-lo flag, Zstd frame content size, JPEG frame header and JPEG XR
directory decide whether a subblock is placed, so two producers read them
alike, and only them: a producer MUST NOT decompress a subblock to decide
anything (structure only). A zstd stream that, after a valid frame header,
holds more than one frame or trailing bytes is placed all the same, and
fails when a reader decodes it; a writer of such files is not known.

### 7.7 Reading the output

The layout uses two things that are not in every Zarr reader:

- **Clipped levels**, and the levels and tile arrays of short row bands,
  use the zarr-extensions `rectilinear` chunk grid (the
  convention [§4.3](#43-image-arrays), [§4.4](#44-tiles)). zarr-python 3.4 reads them after
  `zarr.config.set({"array.rectilinear_chunks": True})`. Where a reader has
  no rectilinear support, such an array can still be read chunk by chunk.
  Without row bands, the chunk at `y` index `j` and `x` index `i` holds the
  tile of that row and column, of the stored size `(W, H)`, or `W'` wide in
  the last column and `H'` high in the last row; with row bands, it holds the
  band of column `i` that [§4.3](#43-image-arrays) numbers `j`, of the band's height
  (refkv's verifier, `js/test/czi/verify.py`, checks both ways).
- **Codecs.** `imagecodecs_jpegxr` (JPEG XR subblocks) and
  `numcodecs.shuffle` (Zstd1 with hi-lo packing) are not in the
  zarr-extensions registry, nor in the Neuroglancer fork refkv's viewer uses.
  zarr-python 3 has `numcodecs.shuffle`; imagecodecs-zarr (refkv's `images`
  extra) has `imagecodecs_jpegxr`. Files with these codecs are accepted, not
  rejected: their chunks are the subblocks' bytes, so the archive is
  complete whatever a reader supports. Their registration, and decoders in
  the Neuroglancer fork, are a follow-up.

### 7.8 Measured cost

On the public corpus (`conformance/zarrify/corpus_czi.txt`, 24 files
of 1 MB to 2.1 GB), the Python reference makes about one range request per
subblock (2 KiB at its segment, which holds its header, and usually its
metadata and codec header; ranges less than 16 KiB apart are merged into
reads of up to 1 MiB), plus the metadata XML and the attachment headers:
498 requests and 1.7 MB read for a 505 MB slide of 481 subblocks, 31
requests for a 5.9 MB line scan of 3575 subblocks. The archives are 29 KB
to 1.7 MB; no document exceeds 1.9 KB. Details per file are in
`spec/history/virtualize/REVISIONS.md` (revision 18).
