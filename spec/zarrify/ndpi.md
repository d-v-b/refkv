# NDPI

The Zarr layout of a Hamamatsu NDPI slide, and its source model: the
elements of its IR, which `refkv_source` holds as the IR mirror. NDPI is a variant of TIFF; this convention builds on the
[TIFF convention](tiff.md). What all of refkv's conventions share is
in [spec/conventions.md](../conventions.md), cited here as "conventions §n".
How refkv produces this layout as a reference store is the NDPI profile,
[Part 2](#part-2-the-profile) below.

Convention version: 0 (until release, [conventions §1](../conventions.md#1-conventions)) · UUID: `6cac71ef-dbb2-4acd-b60c-00389aa4238a` ·
Schema: [schema.json](ndpi/schema.json) · Changed 2026-10-26, with no change to any output:
the layout is tabulated, its entries in [Layout](#layout) and its types in type tables
([conventions §11.5](../conventions.md#115-layout-tables)) · Changed 2026-10-25: a level may be JPEG XR
(Compression 22610), tiled or in one strip, each tile or strip a chunk, its data type and
samples from the JPEG XR stream header and not the tags ([§3](#3-levels),
[§4](#4-the-image)); the tile tables' high bytes (65324, 65325) extend them
([§7](#7-ndpi-profile)); the root has the source metadata of
[§5](#5-source-metadata), the fluorescence filter, wavelength and exposure time, and a
single channel's `omero` label is its filter's name (d-v-b/refkv#123) · Changed 2026-10-24: a level without McuStarts whose
strip's frame header is not the level's size has the level's size spliced into the frame header
when it needs the same MCU rows and columns, and is otherwise not a level (rejected when it is
level 0) ([§3](#3-levels), [§4](#4-the-image)); the profile reads such a strip's header
([§7](#7-ndpi-profile)) · Changed 2026-10-22: the image has an `omero` member
([§4](#4-the-image)), after
[revision 29](../history/virtualize/REVISIONS.md#revision-29-no-recorded-revision-the-profile-registry)
(a profile's changes are recorded here, dated, from then on)

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
| `zarr.json` | group | [`ImageGroup`](../conventions.md#type-imagegroup) | the root: the image ([§4](#4-the-image)) |
| `zarr.json#/attributes/ome` | attribute | [`OmeImage`](../conventions.md#type-omeimage) | `M`: one multiscale, without a name, with the axes `c`, `y`, `x` ([§3](#3-levels)), one level per array with the scale and translation of [§4](#4-the-image); `omero` one [`OmeroChannel`](../conventions.md#type-omerochannel) per sample, as [§4](#4-the-image) says |
| `zarr.json#/attributes/zarr_conventions` | attribute | array of [`ConventionMetadata`](../conventions.md#type-conventionmetadata) | the CMO of [§1](#1-declaration) |
| `zarr.json#/attributes/refkv_zarrified` | attribute | [`RootProperty`](../conventions.md#type-rootproperty) | `profile` `"ndpi"`, `version` `0`, `source.url` the file's URL |
| `zarr.json#/attributes/refkv_zarrified/ndpi` | attribute | [`NdpiRoot`](#type-ndpiroot) | the root's source metadata ([§5](#5-source-metadata)), when `S` is not empty: level 0 has one of the acquisition tags, with a value |
| `<L>/zarr.json` | array | [`NdpiArray`](#type-ndpiarray) | level `L` ([§3](#3-levels), [§4](#4-the-image)) |
| `<L>/c/0/<u>/<v>` | chunk | [`NdpiStripChunk`](#type-ndpistripchunk), [`NdpiMcuChunk`](#type-ndpimcuchunk) or [`NdpiJpegXrChunk`](#type-ndpijpegxrchunk) | the chunks of level `L` ([§4](#4-the-image)): of a JPEG level without McuStarts, its one chunk (`u` and `v` 0), an `NdpiStripChunk`; of a JPEG level with McuStarts, chunk `(u, v)`, an `NdpiMcuChunk`; of a JPEG XR level, each tile whose byte count is above 0, or its one strip, an `NdpiJpegXrChunk` ([§7](#7-ndpi-profile)) |
| `refkv_source/**` | subtree | [the mirror's entries](../conventions.md#table-mirror) | the IR mirror ([§5](#5-source-metadata)): its elements are [§5.1](#51-elements)'s |

| placeholder | value |
|---|---|
| `<L>` | the level's index, from 0, in decimal |
| `<u>`, `<v>` | the chunk's row and column, in decimal |

## 1. Declaration

The root and `refkv_source` declare the convention
([conventions §11.2](../conventions.md#112-declaration)); the root has a source
metadata member when level 0 has one of the acquisition tags of
[§5](#5-source-metadata). Its CMO is:

```json
{
  "uuid": "6cac71ef-dbb2-4acd-b60c-00389aa4238a",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/zarrify/ndpi/schema.json",
  "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md",
  "name": "refkv_zarrified",
  "description": "The Zarr layout of a NDPI source zarrified by refkv, and the source's metadata"
}
```

## 2. The source

An NDPI file is a little-endian classic TIFF (magic 42) whose IFDs have
64-bit offsets ([§7](#7-ndpi-profile)
says how they are read). Its **IFDs** are those of its main chain, in
order. Of duplicate tags in an IFD, the first is used.

The layout uses these tags, besides ImageWidth (256), ImageLength (257), BitsPerSample (258), Compression (259), PhotometricInterpretation (262) and SamplesPerPixel (277), whose defaults are the [TIFF convention's](tiff.md#2-the-source); field types and counts are as the profile checks them:

| tag | name | kind | type | default |
|---|---|---|---|---|
| 273, 279 | StripOffsets, StripByteCounts | scalar | integer | absent |
| 322, 323 | TileWidth, TileLength | scalar | integer | absent |
| 324, 325 | TileOffsets, TileByteCounts | array | integer | absent |
| 282, 283 | XResolution, YResolution | scalar | RATIONAL (5) | absent |
| 296 | ResolutionUnit | scalar | integer | none |
| 65420 | NDPI format flag | scalar | integer | required |
| 65421 | Magnification | scalar | FLOAT (11) or DOUBLE (12) | required |
| 65422, 65423 | X, YOffsetFromSlideCenter | scalar | SHORT, LONG, SSHORT, SLONG (3, 4, 8, 9) | absent |
| 65426 | McuStarts | array | integer | absent |
| 65432 | McuStartsHighBytes | array | integer | absent |
| 65324, 65325 | OffsetHighBytes, ByteCountHighBytes | array | integer | absent |

The **acquisition tags** are read too, but are not checked like the table's: each is read
only when its field type is the one given, its value lies within the file, and (for
Fluorescence) its count is at most 1024. Otherwise it is treated as absent. They never
reject the input. The names are tifffile's; the meanings are Bio-Formats'.

| tag | name | field type | what |
|---|---|---|---|
| 65434 | Fluorescence | ASCII (2) | the fluorescence filter set's name (`TxRed`) |
| 65451 | Wavelength | FLOAT (11) or DOUBLE (12) | the emission wavelength, in nanometres |
| 65454 | ExposureTime | SHORT or LONG (3, 4) | the exposure time, in microseconds |

(Tag 65433 is in fluorescence and brightfield files alike, with no known meaning. It is kept
in the source model, like every other tag.)


## 3. Levels

**Levels.** The levels are the main-chain IFDs whose Magnification is
positive, in chain order. Each MUST be one of two kinds, and every level
MUST have the Compression of the first:
- a **JPEG level**: Compression 7, PhotometricInterpretation 6,
  SamplesPerPixel 3, BitsPerSample 8 and one strip (StripOffsets and
  StripByteCounts of one value each), which MUST lie within the file and be
  at least 4 bytes long (a JPEG stream's SOI and EOI). This is the
  brightfield slide.
- a **JPEG XR level**: Compression 22610, which Hamamatsu uses for JPEG XR
  (ITU-T T.832 with its container) and tifffile names `JPEGXR_NDPI`. Its
  image is **tiled** (TileWidth and TileLength of at least 1, and
  TileOffsets and TileByteCounts) or in **one strip**, as a JPEG level's,
  and every tile or the strip is one JPEG XR stream.

Each level MUST be strictly smaller in both width and length than the
one before. If two levels have the same Magnification (focal planes), the
input is rejected. Other IFDs (the macro image, the slide map) are ignored.
A JPEG level without McuStarts whose strip's frame cannot be given the
level's size ([§4](#4-the-image), **Chunks of a JPEG level**) is not a level after all: it
is left out, and its IFD stays in the source metadata like the ignored
ones; when it is the first level, the input is rejected.

**Pixels.** The array has axes `c`, `y`, `x`.
- A JPEG level has data type `uint8`, 3 samples, and codecs `transpose`
  and `imagecodecs_jpeg` ([conventions §3](../conventions.md#3-arrays)).
- A JPEG XR level's data type and samples are its streams', which the
  stream header's **pixel format** gives (the GUID of its `PixelFormat`
  entry, [§7](#7-ndpi-profile)): Gray8 (`…08`) is `uint8` with 1 sample,
  Gray16 (`…0B`) `uint16` with 1, 24bppBGR (`…0C`) and 24bppRGB (`…0D`)
  `uint8` with 3, and 48bppRGB (`…15`) `uint16` with 3. Any other pixel
  format is rejected. Its codecs are `transpose` (with 3 samples only) and
  `imagecodecs_jpegxr` (as the
  [CZI convention §3.1](czi.md#31-pixel-types-and-codecs) defines it),
  which decodes the samples in R, G, B order.

**The tags are not the pixels.** A JPEG XR level's PhotometricInterpretation,
SamplesPerPixel and BitsPerSample are not read for the layout. Hamamatsu
writes 6, 3 and 8, 8, 8 whatever the streams hold: in a fluorescence slide,
every level says 8-bit YCbCr with 3 samples, and every stream is 16-bit
grayscale. Where the stream header and these tags disagree, the stream
header decides, because a decoder gives what the stream holds. The tags are
kept, as they are, in the source model ([§5](#5-source-metadata)), and their
disagreement never rejects the input. (tifffile and Bio-Formats take a CaptureMode,
tag 65441, above 6 to mean 16-bit grayscale. The stream header says that
too, and it also says what every other pixel format is.)

The pixel format is read from one stream of each level, its **first
stream**: the strip, or the first tile whose byte count is above 0. Every
level MUST have one, every level's MUST have the pixel format of level 0's,
and its width and height MUST be the level's chunk's
([§4](#4-the-image)). Otherwise the input is rejected. The level's other
streams are not read, so a stream that differs from the first is not found
here: what its chunk decodes to is then the decoder's.


## 4. The image

The hierarchy is one image at the root ([conventions §4](../conventions.md#4-images)),
without a name, with one array per level at path `"<level>"`.

<a id="type-ndpiarray"></a>**`NdpiArray`**, a level's `zarr.json`: an
[`ArrayDocument`](../conventions.md#type-arraydocument) with these members:

| member | type | presence | value |
|---|---|---|---|
| `shape` | array of integer | required | `[s, H, W]`: `s` the level's samples (3 for a JPEG level, the pixel format's for a JPEG XR level), the level's ImageLength `H` and ImageWidth `W` |
| `data_type` | string | required | `"uint8"` for a JPEG level; the pixel format's, `"uint8"` or `"uint16"`, for a JPEG XR level ([§3](#3-levels)) |
| `chunk_grid` | object | required | regular, its chunk shape, for a JPEG level, `[3, H, W]` without McuStarts and `[3, b × mh, a × R × mw]` with McuStarts (**Chunks of a JPEG level**, below); for a JPEG XR level, `[s, TileLength, TileWidth]` when tiled and `[s, H, W]` in one strip (**Chunks of a JPEG XR level**, below) |
| `codecs` | array of object | required | `transpose` with `order` `[1, 2, 0]` (with 3 samples only, for a JPEG XR level), then `imagecodecs_jpeg` for a JPEG level or `imagecodecs_jpegxr` for a JPEG XR level ([§3](#3-levels)) |
| `dimension_names` | array of string | required | `["c", "y", "x"]` ([§3](#3-levels)) |
| `attributes` | object | required | `{}` |

**Scale.** With XResolution `rx` and a ResolutionUnit tag of 3
(centimeter), let `p = 10000 / rx`; of 2 (inch), `p = 25400 / rx`. If `p`
is less than 25.4, `x` has unit `micrometer` and level 0 scale `p`;
otherwise, and without a ResolutionUnit tag, no unit and scale 1, as in the
TIFF convention [§4.4](tiff.md#44-arrays). `y` likewise from YResolution.
Level `L`'s scale is level 0's times `(W0 / WL)` (x) or `(H0 / HL)` (y), as
in [the TIFF convention §4.4](tiff.md#44-arrays).

**Position.** When `x` and `y` have units and level 0's IFD has
XOffsetFromSlideCenter (65422) and YOffsetFromSlideCenter (65423), in
nanometres, the image's centre is at `(X / 1000, Y / 1000)` micrometres,
which gives its translation ([conventions §5](../conventions.md#5-units)).

**omero.** `M` has `"omero": {"channels": [...]}` next to `multiscales`,
one object per sample, as in [the TIFF convention §4.4](tiff.md#44-arrays),
whose member order this follows. Each is `"active": true` with the window of
its data type's whole range (`{"min": 0, "max": 255, "start": 0, "end": 255}`
for `uint8`, 65535 for `uint16`): refkv does not decode JPEG or JPEG XR to
sample one.
- With 3 samples (JPEG streams, and JPEG XR streams of an RGB pixel format,
  which decode to RGB), the samples are labeled `R`, `G` and `B`, with
  colors `FF0000`, `00FF00` and `0000FF`.
- With 1 sample, the channel is labeled with level 0's Fluorescence
  ([§2](#2-the-source)) when it is UTF-8, not empty and at most 256 bytes
  (up to its first NUL), and `C0` otherwise, with color `FFFFFF`.

**Chunks of a JPEG XR level.** Each tile is a chunk, of chunk shape
`[s, TileLength, TileWidth]` (`s` the samples), and the strip of a level in
one strip is the one chunk, the whole image (chunk shape `[s, H, W]`). The
chunk holds the stream as it is. Tile `j`, in row-major order with
`ceil(W / TileWidth)` tiles a row, is chunk `<level>/c/0/<row>/<column>`.
TileOffsets and TileByteCounts MUST each have `ceil(H / TileLength) ×
ceil(W / TileWidth)` values, and a tile whose byte count is 0 has no chunk
(its pixels are the fill value 0). Every other tile MUST lie within the
file. As in TIFF, the tiles at the bottom and right edges are whole tiles,
whose pixels outside the array are not read.

**Chunks of a JPEG level.** A level's strip is a JPEG stream. Without McuStarts, the
level is one chunk, the whole image (chunk shape `[3, H, W]`), which holds
the strip. Its frame header (its first SOF0 segment) should give the size
`W × H`, but some scanners write a frame a row short of the level's
ImageLength: a decoder then gives an image of the frame's size, which is
not the chunk's. So, when the frame's height `h` or width `w` differs from
`H` or `W`, with an MCU of `mw × mh` pixels (8 times the frame's largest
horizontal and vertical sampling factors):
- if `ceil(H / mh) = ceil(h / mh)`, `ceil(W / mw) = ceil(w / mw)`, and `H`
  and `W` are at most 65535, the chunk holds the strip with the frame's
  height and width set to `H` and `W`. The stream encodes whole MCUs, so it
  holds every pixel of that size: rows or columns the frame left out are
  those the encoder padded its last MCU row or column with, and those it
  adds are decoded and dropped;
- otherwise the level is not a level ([§3](#3-levels)).

A strip that has no frame header to read this way (the profile's
[§7](#7-ndpi-profile)) is held as it is. With McuStarts, the strip is a sequence of restart
**intervals**, each `R` MCUs of `mw × mh` pixels wide and one MCU tall, in
row-major order, `q` per row and `r` rows (the profile defines them from
the strip's JPEG header); a chunk is `a × b` intervals, `a` =
`min(q, max(1, floor(1024 / (R × mw))))` across and `b` down, so the chunk
shape is `[3, b × mh, a × R × mw]`. (`b` is chosen by the profile, as the
largest that keeps every chunk's reference within refkv's limits.) Chunk
`(u, v)` (row `u`, column `v`), at `<level>/c/0/<u>/<v>`, holds a complete
JPEG stream of those intervals, which decodes to the chunk's pixels; at the
image's bottom and right edges, the pixels outside the array are
unspecified. Every chunk is present.

A chunk's stream holds the strip's header with a frame header (SOF0) of its
own size, its intervals, and restart markers and an EOI of its own, so:
- the chunk's width `a × R × mw` and height `b × mh` MUST each be at most
  65535, the largest a frame header holds, else the input is rejected;
- the strip's SOF0 segment, which every chunk replaces, is kept where it
  is: the strip is an element of the source model ([§5](#5-source-metadata)), so the mirror
  rebuilds it byte for byte;
- the strip's own restart markers and final EOI are referenced by no chunk:
  each chunk puts the standard sequence that every valid JPEG stream has.
  The mirror keeps the strip's own, whatever they are.

## 5. Source metadata

The root's source metadata `S` ([conventions §2](../conventions.md#2-attributes))
has, of level 0's IFD, the acquisition tags of [§2](#2-the-source) it has, in
this order, each as [conventions §6](../conventions.md#6-source-metadata-as-json)
translates it.

<a id="type-ndpiroot"></a>**`NdpiRoot`**, the root's source metadata `S`:

| member | type | presence | value |
|---|---|---|---|
| `Fluorescence` | [`Text`](../conventions.md#type-text) | optional | tag 65434: its text value |
| `Wavelength` | [`Float`](../conventions.md#type-float) or `{"bits": H}` | optional | tag 65451: its first value, a float; `{"bits": H}`, its IEEE 754 bits as 8 (FLOAT) or 16 (DOUBLE) lowercase hexadecimal digits, most significant first, for a negative zero or a NaN other than the canonical one, whose bits a JSON number or `"NaN"` does not keep (as [NIfTI's `NiftiBits`](nifti.md#type-niftibits)) |
| `ExposureTime` | [`Integer`](../conventions.md#type-integer) | optional | tag 65454: its first value, an integer (of an in-line LONG, with its high word, [§7](#7-ndpi-profile)) |

A tag that [§2](#2-the-source) treats as absent, or that has no value, is left out. With none of these, `S` is empty, and the root has no source
metadata member (as for a brightfield slide). They are also in the source model, the
values of their tags.

Everything else the file holds is on
`refkv_source`, which is the file's **IR mirror**
([conventions §11.3](../conventions.md#113-the-ir-mirror)).
This section is the NDPI **source model**: the elements of an NDPI's IR. It is the
[TIFF source model](tiff.md#5-source-metadata) read in NDPI's layout. Nothing is
left out, layout and dead space included (they are elements too, the dead
space as gaps): a level's strip, its JPEG header, restart markers and EOI
included, is one element, which the chunks of [§4](#4-the-image) cut along McuStarts.

### 5.1 Elements

Paths are relative to the IR's root, whose path is `""`; `<i>`, `<j>`,
`<tag>` are decimal name indexes ([conventions §8.1](../conventions.md#81-elements)). Types are those of [conventions §8.6](../conventions.md#86-types);
an NDPI is little-endian.

| path | kind | type | extent: what |
|---|---|---|---|
| `""` | struct | | the root: the whole file, `(0, size)` |
| `header` | struct | | the header, bytes 0 to 12 |
| `header/byte_order` | value | `ascii[2]` | `II` |
| `header/magic` | value | `<u2` | 42 |
| `header/first_ifd` | value | `<u8` | the first IFD's offset `V` ([§7](#7-ndpi-profile)) |
| `ifds/<i>` | struct | | main-chain IFD `i` ([§2](#2-the-source)): its entry count, entries, next-IFD offset and high words, `2 + 16 n + 8` bytes for `n` entries |
| `…/entry_count` | value | `<u2` | the entry count `n` |
| `…/tags/<tag>` | struct | | the first entry of tag `<tag>` (12 bytes); a later one of the same tag is `…/tags/<tag>~<k>`, `k` = 1, 2, … |
| `…/tags/<tag>/entry` | value | record | the entry's tag, field type and count (`{tag:<u2,type:<u2,count:<u4}`), with `offset:<u4` (the low 32 bits of the value's offset) when the value is out of line, or `field:bytes[4]` when the field type is unknown |
| `…/tags/<tag>/value` | value | by field type | the value: in the entry's field (the bytes it uses; the rest of the field is a gap), or at its offset `low + high × 2^32` when it lies within the file; its type as in the [TIFF source model](tiff.md#51-elements) (`<u4[n]`, `<u4[n,2]`, `<f4[n]`, …) |
| `…/tags/<tag>/high` | value | `<u4` | the entry's **high word**, after the next-IFD offset: the high 32 bits of an out-of-line value's offset, and of an in-line LONG or IFD of count 1 |
| `…/next_ifd` | value | `<u8` | the next-IFD offset |
| `…/strips` | struct | | an IFD's strips (by StripOffsets, with high words, and StripByteCounts), when it has both tables |
| `…/strips/<j>` | data | | strip `j`, when its byte count is not 0 and it lies within the file; its form's geometry is `{"shape": [rows, columns], "samples": s, "bits": b}`, its codec `{"compression": c}`, and its recipe its bytes: a level's strip is a whole JPEG or JPEG XR stream |
| `…/tiles` | struct | | an IFD's tiles (by TileOffsets and TileByteCounts, with OffsetHighBytes and ByteCountHighBytes, [§7](#7-ndpi-profile)), when it has both tables |
| `…/tiles/<j>` | data | | tile `j`, when its byte count is not 0 and it lies within the file; its form as a strip's, the shape `[TileLength, TileWidth]`; `s` and `b` are the tags', which a JPEG XR stream need not hold ([§3](#3-levels)) |
| `…/subifds/<j>`, `…/exif`, `…/gps`, `…/interoperability`, `…/ifd_<tag>` | struct | | the IFD a pointer tag ([§5.2](#52-which-ifds)) leads to (`…/<name>/<j>` when the tag has several values), as an IFD |
| `gaps/<offset>` | gap | | bytes no other element claims (padding, unused space, bytes 12 to 16 when no element claims them) |

An IFD whose strip (tile) tables, and their high bytes, are at the offsets
of an earlier IFD's, with the same counts and codec, names that IFD's
`strips` (`tiles`) by an alias. An element
whose bytes an earlier element claims (an IFD read twice through a pointer,
a strip at another's offset) is an alias of that element.

### 5.2 Which IFDs

The IFDs of [§2](#2-the-source), the main chain, at depth 0 (`ifds/<i>`); then the IFDs that
pointer tags lead to, as the [TIFF source model §5.2](tiff.md#52-which-ifds)
says, with these differences:
- the main-chain IFDs are the only IFDs of the tree: an NDPI's SubIFDs tag
  (330) is followed like the other pointer tags;
- the other pointer tags of type IFD or IFD8 are those that are neither in
  the table of [§2](#2-the-source) nor layout tags, McuStarts (65426) and McuStartsHighBytes
  (65432) among the latter;
- every IFD is read in NDPI's layout ([§7](#7-ndpi-profile)): one a pointer leads to is read
  when its offset is at least 16 and its entry count, entries, next-IFD
  offset and high words lie within the file, has at least one entry, and
  overlaps no IFD already read; an in-line LONG or IFD of count 1 leads to
  `low + high × 2^32`, and an out-of-line value is at its 64-bit offset.

### 5.3 Values

An entry's value is an element when it lies within the file. Its bytes are
in the source; the view shows it when it is a number, a record or text
(ASCII) of at most 1024 bytes ([conventions §8.7](../conventions.md#87-the-view)), and a producer reads
every such value. A value element holds the bytes of its extent: an in-line
LONG or IFD of count 1 shows its low 32 bits, and its `high` sibling the
rest. Every high word is shown.

### 5.4 Equivalence

Hierarchies of one file are compared entry for entry, `refkv_source` included
([conventions §11.4](../conventions.md#114-equivalence); NDPI has no named array rule).

## 6. Example

A slide at `https://example.org/slide.ndpi`, the 207,434-byte
`ndpi_levels.ndpi` of refkv's test fixtures: three levels, of which the first
two have McuStarts. The root's property:

```json
{
  "profile": "ndpi",
  "version": 0,
  "source": {
    "url": "https://example.org/slide.ndpi"
  }
}
```

`refkv_source` is the IR mirror ([§5](#5-source-metadata)). Its property:

```json
{
 "ndpi": {
  "ir": {
   "version": 2,
   "size": 207434,
   "elements": 292,
   "rows": 146,
   "names": ["", "byte_order", "entry", "entry_count", "first_ifd", "gaps/", "header", "high", "ifds/", "magic",
             "next_ifd", "strips", "tags/", "value"],
   "types": ["", "<f4[1]", "<i4[1]", "<u2", "<u2[1]", "<u2[3]", "<u4", "<u4[1,2]", "<u4[1500]", "<u4[1]",
             "<u4[60]", "<u8", "ascii[2]", "{tag:<u2,type:<u2,count:<u4,offset:<u4}", "{tag:<u2,type:<u2,count:<u4}"],
   "forms": ["{\"geometry\":{\"shape\":[1200,1280],\"samples\":3,\"bits\":8},\"codec\":{\"compression\":7},\"recipe\":[[\"src\",0,null]]}",
             "…"]
  }
 }
}
```

(abridged: four forms, one per strip shape). The view, `refkv_source/tree`,
is the root's document, and begins:

```json
{
 "ndpi": {
  "header": {"byte_order": "II", "magic": 42, "first_ifd": 198686},
  "ifds/0": {
   "strips": {},
   "entry_count": 16,
   "tags/256": {"entry": {"tag": 256, "type": 4, "count": 1}, "value": [1280], "high": 0},
   "tags/273": {"entry": {"tag": 273, "type": 4, "count": 1}, "value": [12], "high": 0},
   "tags/282": {"value": [[5477, 1]], "entry": {"tag": 282, "type": 5, "count": 1, "offset": 192670}, "high": 0},
   "tags/65421": {"entry": {"tag": 65421, "type": 11, "count": 1}, "value": [20.0], "high": 0},
   "tags/65426": {"value": {"$vz": "element", "id": 89},
                  "entry": {"tag": 65426, "type": 4, "count": 1500, "offset": 192686}, "high": 0},
   "next_ifd": 202450
  }
 }
}
```

(abridged: the other tags are like these). Level 0's strip, `ifds/0/strips/0`,
starts at byte 12, before its IFD, so `strips` comes first; its 1,500
McuStarts values (6,000 bytes) are more than the view shows, so the view
names the element by its row, and a reader reads them from the source.

# Part 2. The profile

The NDPI profile of [spec/zarrify.md](../zarrify.md) (revision 16) is [§7](#7-ndpi-profile) of this document. It builds on the TIFF profile ([tiff.md §7](tiff.md#7-tiff-profile), [tiff.md](tiff.md#part-2-the-profile)). What every profile's Part 2 shares is in
[spec/zarrify.md, Profiles](../zarrify.md#profiles).

## 7. NDPI profile

**No signature.** A file's first bytes never choose this profile ([spec/zarrify.md
§1.2](../zarrify.md#12-input)): a file the TIFF profile's signature chose is read by it when it passes the
detection test below.

Hamamatsu NDPI files are little-endian classic TIFFs (magic 42) with a 64-bit
extension and pyramids stored as single JPEG strips (brightfield) or as JPEG XR
tiles or strips (fluorescence, and some brightfield scanners). They are read by [tiff.md §7.1](tiff.md#71-reading)
([tiff.md](tiff.md#71-reading)) with the changes below, with the tags of
[the convention §2](#2-the-source) as the
checked tags.

**Detection.** Let `V` be the little-endian `u64` at bytes 4–11 of the
header. The file is NDPI if it is little-endian with magic 42, `16 ≤ V`,
`V + 2 ≤` the file's size, and the IFD at `V`, read as below, has tag 65420
among its first `n` entries (its entry count `n` and the `12 n` bytes of
entries MUST lie within the file for this test; if they do not, the file is
not NDPI). Otherwise it is a TIFF ([tiff.md §7](tiff.md#7-tiff-profile)). This test never rejects.

**IFDs.** The main chain starts at `V`. An IFD at `o` is a `u16` entry count
`n`, `n` entries of 12 bytes as in TIFF, then a `u64` next-IFD offset, then
`n` `u32` **high words**, one per entry. An entry's value field is its 4
bytes plus its high word `h`: an out-of-line value is at offset
`low + h × 2^32`, and an inline value of count 1 and type LONG (4) or IFD
(13) is `low + h × 2^32`, but for OffsetHighBytes and ByteCountHighBytes
(**Tiles** below). Other inline values ignore `h`. Only the main
chain is read for the layout: an NDPI's SubIFDs, like the IFDs of its other
pointer tags, are read in this layout for the IR only, and
never reject the input ([the convention
§5](#5-source-metadata)). The limits and checks
of [tiff.md §7.1](tiff.md#71-reading) apply, with these values read ([tiff.md §7.1](tiff.md#71-reading), **Values read**): the
Magnification of every main-chain IFD, and every tag of the table of each
level ([the convention §3](#3-levels)); of a
scalar tag, its first value, of McuStarts, McuStartsHighBytes,
BitsPerSample, TileOffsets, TileByteCounts, OffsetHighBytes and
ByteCountHighBytes, every value. (A LONG or IFD value of count 1, with its high
word, can exceed 2^53 − 1 too.) The strip header of each JPEG level, and the
first stream's header of each JPEG XR level, are read too, as **Strips** and
**JPEG XR streams** say; so are level 0's acquisition tags ([the convention
§2](#2-the-source)), when of their field types. No other value is read for the layout, so
the other IFDs' tags have only their field types, counts and places
checked.

**Rejection.** The input is rejected when a check fails, and when the
convention gives it no layout: wherever the convention says that the input
is rejected, or that something MUST hold and it does not.

**Strips.** A JPEG level's strip is the JPEG stream `S` = bytes
`[StripOffsets, StripOffsets + StripByteCounts)` of the file, which MUST
lie within the file. StripByteCounts MUST be at least 4, the SOI and EOI
markers of the shortest JPEG stream.

- **Without McuStarts,** the level is one chunk, the whole image (chunk
  shape `[3, H, W]`). Its **frame header** is read from the first bytes of
  `S`: `min(len(S), 1024)` of them, then, while the header continues past
  the `n` bytes read and the `k` bytes it needs are at most
  `c = min(len(S), 2^20)`, `min(c, max(k, 2 × n))` of them. The frame
  header is the first SOF0 (`C0`) segment when `S` starts with SOI
  (`FF D8`), then, after any `FF` fill bytes, each marker `FF xx` has a
  2-byte length of at least 2, every segment before the SOF0 is one of
  DQT, DHT, DAC, JPG, DRI, DNL, DHP, EXP, APPn, JPGn or COM (`xx` = `C4`,
  `C8`, `CC`, `DB` to `DF`, `E0` to `FD`, `FE`), and the SOF0 segment is
  `10 + 3 × Nf` bytes long with `Nf ≥ 1` components and sampling factors
  whose largest horizontal and vertical are above 0. Otherwise (another
  marker first, such as SOS or another SOFn; a header past `c` bytes; a
  malformed segment) the strip has no frame header to read, and the chunk
  is the single range of `S`: this reading never rejects the input. With a frame header of the level's
  size, the chunk is the single range of `S` too. With one of another size
  ([the convention §4](#4-the-image), **Chunks of a JPEG level**), when the level's size
  needs the same MCU rows and columns, the chunk is three parts: the range
  of `S` up to the frame's height field (its SOF0 segment's bytes 5 and
  6), a literal of 4 bytes, `H` and `W` as big-endian `u16`, and the range
  of `S` after the frame's width field. Otherwise the level is left out
  ([the convention §3](#3-levels)), and when it is level 0 the input is
  rejected.
- **With McuStarts,** let `M[i]` be its values, plus `McuStartsHighBytes[i]
  × 2^32` when that tag is present (it MUST then have as many values); they
  are offsets in `S`. `S`'s **header** is its first `M[0]` bytes: a JPEG SOI,
  then marker segments up to and including SOS, which MUST end exactly at
  `M[0]`. The header MUST have exactly one SOF0 (`FF C0`) segment and a DRI
  (`FF DD`) segment with restart interval `R > 0` (in MCUs). The SOF0
  segment's sampling factors give the MCU size `mw × mh`: 8 times the largest
  horizontal and vertical factors. The strip is then a sequence of
  **intervals**, each `R` MCUs wide and one MCU tall, in row-major order:
  `q = ceil(W / (R × mw))` per row and `r = ceil(H / mh)` rows, and McuStarts
  MUST have `q × r` values, increasing, with `M[q × r − 1] < len(S)`.
  Interval `i` is the bytes `[M[i], M[i + 1] − 2)` of `S` (`[M[i], len(S) −
  2)` for the last), dropping the 2-byte restart marker or EOI after it; each
  MUST be non-empty.

<a id="type-ndpistripchunk"></a>**`NdpiStripChunk`**, the chunk `<level>/c/0/0/0` of a level
without McuStarts, with `o` its StripOffsets, `m` its StripByteCounts and `f` the offset in
`S` of its frame's height field:

| part | range | presence | value |
|---|---|---|---|
| the strip | source range `(0, o, m)` | optional | with no frame header to read, or one of the level's size: all of `S`, the chunk's one range |
| the strip up to the height | source range `(0, o, f)` | optional | with a frame header of another size whose MCU rows and columns the level's size needs: `S` up to the frame's height field (its SOF0 segment's bytes 5 and 6) |
| the level's size | literal range | optional | likewise: 4 bytes, `H` and `W` as big-endian `u16` |
| the rest of the strip | source range `(0, o + f + 4, m − f − 4)` | optional | likewise: `S` after the frame's width field |

**Chunks of a strip with McuStarts.** A chunk is `a × b` intervals: `a` =
`min(q, max(1, floor(1024 / (R × mw))))` intervals across and `b` intervals
down, so the chunk shape is `[3, b × mh, a × R × mw]`. `a × R × mw` MUST be
at most 65535. The 2 bytes of `S` before `M[i]` (for `i ≥ 1`) and its last
2 bytes, its restart markers and EOI, are not referenced: the chunks put
their own, the standard sequence `FF D0 + (t mod 8)` and `FF D9`, and the
mirror keeps the strip's ([the convention §5](#5-source-metadata)).
`b` is the largest
number from 1 to `min(r, floor(1024 / mh))` (at least 1) for which every
chunk's payload is at most 65519 bytes ([§1.2](../zarrify.md#12-input)); `b × mh` MUST be at most
65535 (it always is, since `mh ≤ 120`). Chunk `(u, v)` (row `u`,
column `v`) is the JPEG stream [`NdpiMcuChunk`](#type-ndpimcuchunk), whose pieces its table
numbers.

<a id="type-ndpimcuchunk"></a>**`NdpiMcuChunk`**, chunk `(u, v)` of a level with McuStarts, `o`
its StripOffsets:

| part | range | presence | value |
|---|---|---|---|
| 1. the header up to its SOF0 segment | source range `(d1, 0, n1)`, the whole data source `d1` holding piece 1, of `n1` bytes | required | a shared byte string ([§1.2](../zarrify.md#12-input)): a range of the data source that holds it, the whole source (**Data sources** below) |
| 2. the SOF0 segment | literal range | required | the header's, with its height set to `b × mh` and its width to `a × R × mw` |
| 3. the rest of the header after the SOF0 segment | source range `(d3, 0, n3)`, the whole data source `d3` holding piece 3, of `n3` bytes | required | a shared byte string: a range of the data source that holds it, the whole source |
| 4. the intervals | source ranges `(0, o + M[i], l)` and literal ranges | required | for `y` from 0 to `b − 1` and, within it, `x` from 0 to `a − 1`: the interval at row `min(u × b + y, r − 1)` and column `min(v × a + x, q − 1)` (a range of the file: interval `i`'s `l` bytes), followed, except after the last, by the literal restart marker `FF D0 + (t mod 8)`, where `t` counts the intervals placed so far from 0 |
| 5. the EOI | literal range | required | `FF D9` |

Clamping repeats the last row or column of intervals in the chunks at the
image's bottom and right edges; those pixels lie outside the array and are
not read. (Every interval decodes on its own after a restart marker.)
Chunk keys are `<level>/c/0/<u>/<v>`. The references are listed (for
[§1.2](../zarrify.md#12-input)'s order of first use) level by level, and within a level by `u`, then
`v`.

**Data sources.** Pieces 1 and 3 are the same in every chunk of a level,
and are held once, in data sources, so that reading a chunk reads only its
intervals from the file. They sit at the start of the strip, far from most
intervals, so as ranges of the file they would cost a reader a separate
request per chunk. By [§1.2](../zarrify.md#12-input)'s order of first use, the first McuStarts level's
pieces 1 and 3 are sources 1 and 2; a later level adds a source only for a
piece whose bytes differ from every earlier one. (In CMU-1.ndpi every level
shares piece 1, its quantization and Huffman tables, and piece 3, DRI and
SOS, differs by its restart interval.) Both are JPEG markers and tables, not
pixel data ([§1.2](../zarrify.md#12-input), **Structure only**).

**Tiles.** A JPEG XR level's tile `j` is at offset `TileOffsets[j] +
OffsetHighBytes[j] × 2^32` and is `TileByteCounts[j] + ByteCountHighBytes[j]
× 2^32` bytes long, each high table counting as 0s when absent. A high table's values are the
32-bit values it stores: one value in line is its 4 bytes, without its entry's high word.
A high table that is present MUST have as many values as TileOffsets. A table of one value
in line, of type LONG, already has its high bits from its entry's high word
(**IFDs** above): its high table, when present, MUST hold that word, and is
not added to it. (TileOffsets, a LONG
array, holds only offsets below 2^32, so Hamamatsu writes the high tables
with it; in a file of less than 4 GiB they are 0s.) The sums are
what the layout and the source model's `tiles` use ([the convention
§5.1](#51-elements)), for every IFD whose tile tables and high tables are
read.

**JPEG XR streams.** A JPEG XR level's first stream ([the convention
§3](#3-levels)) is the bytes `S` of its strip, or of its first tile whose
byte count is above 0. Its **header** is read from the first bytes of `S`:
`min(len(S), 1024)` of them, then, while the header continues past the `n`
bytes read and the `k` bytes it needs are at most `c = min(len(S), 2^16)`,
`min(c, max(k, 2 × n))` of them. The header is the container of
[the CZI profile §7.4](czi.md#74-codec-headers), **JPEG XR**: `S` starts
with `49 49 BC 01`, the `u32` at byte 4 is the offset of the image
directory, and of its first entries with tags `BC01` (PixelFormat),
`BC80` (ImageWidth) and `BC81` (ImageHeight) all are present, PixelFormat
of type 1 and count 16 (its offset giving the 16-byte GUID), and the sizes
of type 3 or 4, count 1 and at least 1. The pixel format is the GUID: the
common prefix `24 C3 DD 6F 03 4E FE 4B B1 85 3D 77 76 8D C9` and the last
byte of [the convention §3](#3-levels). The input is rejected when a level
has no first stream, when the header is not there to read (another start, a
missing or malformed entry, or bytes past `c`), when its pixel format is
another, or is not level 0's, and when its ImageWidth and ImageHeight are not
TileWidth and TileLength (tiled) or `W` and `H` (one strip).

**Chunks of a JPEG XR level.** Chunk `<level>/c/0/<row>/<column>` is the
single range of tile `j = row × ceil(W / TileWidth) + column`, or, for a level
in one strip, `<level>/c/0/0/0` the single range of `S`. A tile of byte count
0 has no reference. The references are listed level by level, and
within a level by `j`.

<a id="type-ndpijpegxrchunk"></a>**`NdpiJpegXrChunk`**, a chunk of a JPEG XR level, its stream
as it is:

| part | range | presence | value |
|---|---|---|---|
| the stream | source range `(0, t, n)` | required | of a tiled level, tile `j`: `t` and `n` its offset and length (**Tiles**, above), `n` above 0; of a level in one strip, the strip: `S`, `t` its offset and `n` its length |
