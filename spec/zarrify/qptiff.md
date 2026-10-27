# QPTIFF

The Zarr layout of an Akoya Biosciences (formerly PerkinElmer) QPTIFF file:
the whole-slide scans of the Vectra and Vectra Polaris scanners (`.qptiff`),
and the unmixed component data inForm writes (`.tif`). QPTIFF is a TIFF
whose images each carry a `PerkinElmer-QPI-ImageDescription` XML; this
convention builds on the [TIFF convention](tiff.md), and its source model is
the TIFF source model. What all of refkv's conventions share is in
[spec/conventions.md](../conventions.md), cited here as "conventions §n".
How refkv produces this layout as a reference store is the QPTIFF profile,
[Part 2](#part-2-the-profile) below.

Convention version: 0 (until release, [conventions §1](../conventions.md#1-conventions)) · UUID: `329ea136-2f55-4c27-b6f4-8e60ca2109b8` ·
Schema: [schema.json](qptiff/schema.json) ·
Changed 2026-10-27, with no change to the output of the inputs accepted before but the unusual ones below:
the strip tags, LZW and the padding of a short last strip are now the TIFF convention's
([tiff.md §4.3](tiff.md#43-data-types-and-codecs), [tiff.md §4.5](tiff.md#45-strips)), which
[§2](#2-the-source), [§4.1](#41-data-types-and-codecs) and [§4.2](#42-tiles-and-strips) cite;
a level whose short last strip cannot be padded is still left out here; a strip padded
with more than 2^22 zero bytes now rejects the input ([§4.2](#42-tiles-and-strips)), and a
short last LZW strip is read up to `3 × rows × B + 64` bytes, as [tiff.md §7.1](tiff.md#71-reading)
says, where the whole strip was read (a stream that spends more than a Clear code and a literal
on each byte it decodes no longer reaches its size) ·
Changed 2026-10-27: an LZW tile's or strip's codec is `refkv.tiff_lzw`, refkv's own, in place of `imagecodecs_lzw` (imagecodecs-zarr's codec of that name reads the whole stream), and a Deflate one's `numcodecs.zlib`, in place of `zlib` ([conventions §3](../conventions.md#3-arrays), d-v-b/refkv#90) ·
Changed 2026-10-26, with no change to any output:
the layout is tabulated, its entries in [Layout](#layout) and its types in type tables
([conventions §11.5](../conventions.md#115-layout-tables)), the root's source metadata and a
JPEG tile's reference those of the TIFF convention ([`TiffRoot`](tiff.md#type-tiffroot),
[`TiffChunk`](tiff.md#type-tiffchunk)) · Changed 2026-10-23: each `omero` channel's
window is sampled from the coarsest level's data, as in the TIFF convention, when refkv decodes
its compression (none, LZW, Deflate) and the image is not RGB, so that a `float32` channel has a
window too, and an integer channel without a sampled window has the type's range
([§4.3](#43-arrays)); the profile reads the sampled chunks ([§7](#7-qptiff-profile)), after
[revision 29](../history/virtualize/REVISIONS.md#revision-29-no-recorded-revision-the-profile-registry)
(a profile's changes are recorded here, dated, from then on)

This document has two parts ([conventions §11.1](../conventions.md#111-parts)).

The format is described by Akoya's "PerkinElmer image format" note
(`PKI_Image Format.docx`, 2015, with its public sample files at
<https://downloads.openmicroscopy.org/images/Vectra-QPTIFF/>), which this
convention follows where the samples do.

# Part 1. The convention

This convention gives a layout only to the files that meet its
requirements. Where it says that "the input is rejected", or that
something MUST hold, a file that fails has no layout under this
convention, and the profile rejects it.

## Layout

The hierarchy's entries ([conventions §11.5](../conventions.md#115-layout-tables)):

| prefix | type | structure | description |
|---|---|---|---|
| `zarr.json` | group | [`ImageGroup`](../conventions.md#type-imagegroup) | the root: the image ([§4.3](#43-arrays)) |
| `zarr.json#/attributes/ome` | attribute | [`OmeImage`](../conventions.md#type-omeimage) | `M`: one multiscale, with the name, axes, scales and translation of [§4.3](#43-arrays), one level per array; `omero` with one [`QptiffOmeroChannel`](#type-qptiffomerochannel) per channel ([§4.3](#43-arrays)) |
| `zarr.json#/attributes/zarr_conventions` | attribute | array of [`ConventionMetadata`](../conventions.md#type-conventionmetadata) | the CMO of [§1](#1-declaration) |
| `zarr.json#/attributes/refkv_zarrified` | attribute | [`RootProperty`](../conventions.md#type-rootproperty) | `profile` `"qptiff"`, `version` `0`, `source.url` the file's URL |
| `zarr.json#/attributes/refkv_zarrified/qptiff` | attribute | [`TiffRoot`](tiff.md#type-tiffroot) | the root's source metadata ([§5](#5-source-metadata)) |
| `<L>/zarr.json` | array | [`QptiffArray`](#type-qptiffarray) | level `L` ([§3](#3-images-and-channels), [§4.3](#43-arrays)) |
| `<L>/c/<coords>` | chunk | [`QptiffChunk`](#type-qptiffchunk) | a tile or strip of an image of level `L`, present when its byte count is more than 0 ([§4.3](#43-arrays), **Chunks**; [§7](#7-qptiff-profile)) |
| `refkv_source/**` | subtree | [the mirror's entries](../conventions.md#table-mirror) | the IR mirror ([§5](#5-source-metadata)): its elements are the [TIFF source model's](tiff.md#51-elements) |

| placeholder | value |
|---|---|
| `<L>` | the level's index, from 0, in decimal |
| `<coords>` | the chunk's coordinates, in decimal, one key segment each: `<c>/<row>/<column>`, `c` only when the axis is present, then the tile row and column, or the strip index and 0 (so it stands for two or three segments) |

## 1. Declaration

The root and `refkv_source` declare the convention
([conventions §11.2](../conventions.md#112-declaration)), the root with the source
metadata of [§5](#5-source-metadata) as the member `"qptiff"`. Its CMO is:

```json
{
  "uuid": "329ea136-2f55-4c27-b6f4-8e60ca2109b8",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/zarrify/qptiff/schema.json",
  "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md",
  "name": "refkv_zarrified",
  "description": "The Zarr layout of a QPTIFF source zarrified by refkv, and the source's metadata"
}
```

## 2. The source

The file is a TIFF (magic 42) or BigTIFF (magic 43), in either byte order,
whose first IFD's Software tag (305) is ASCII text starting with
`PerkinElmer-QPI` ([§7](#7-qptiff-profile) says how
that is tested). Its **IFDs** are the image file directories of its **main
chain**, which starts at the header's first-IFD offset and ends at a
next-IFD offset of 0, and MUST have at least one IFD; **IFD 0** is its
first. The SubIFDs of an IFD (tag 330) are read as the
[TIFF convention §2](tiff.md#2-the-source) says, for the source metadata
only: the layout uses the main chain alone. Of duplicate tags in an IFD,
the first is used.

The layout uses the tags of the [TIFF convention's table](tiff.md#2-the-source)
(with their defaults, the strip tags included; field types and counts as the profile checks
them), and these:

| tag | name | kind | default |
|---|---|---|---|
| 286, 287 | XPosition, YPosition | scalar (RATIONAL) | none |

An IFD's **format** is the tuple of [the TIFF convention §2](tiff.md#2-the-source)
(BitsPerSample, SamplesPerPixel, SampleFormat, PlanarConfiguration,
Compression, Predictor, and PhotometricInterpretation when Compression is
7), computed as it says, for IFD 0 and for every IFD that becomes part of a
level ([§3](#3-images-and-channels)); a failure rejects the input.

**The QPI description.** An IFD's **QPI description** is read from its
ImageDescription (270) when that has field type ASCII (2): let `D` be its
bytes up to the first NUL; if `D` is valid UTF-8, its text `X` is read with
the tag scan of [the TIFF convention §3](tiff.md#3-ome-xml) (skipped
sections, tags, attribute values and the decoding of references as there),
tracking depth: a start tag that is not self-closing opens an element, and
an end tag closes one. The description is the first start tag named
`PerkinElmer-QPI-ImageDescription` found at depth 0 (outside every
element); without one, or when `D` is not UTF-8 or the tag is not ASCII,
the IFD has **no QPI description**. Within it, the layout reads the
**direct children** (the start tags at depth 1, inside the root element and
no other) named `ImageType`, `Name`, `Color` and `SlideID`: of each name,
the first that is not self-closing. A child's **text** is the characters of
`X` from the end of its start tag to the start of the next tag, with skipped
sections removed, references decoded as attribute values are, and leading
and trailing whitespace removed. Elements nested deeper (the `Name` inside
a `ScanProfile`, a `Responsivity` filter's) are not read. The description's
XML declaration says `encoding="utf-16"`, but the bytes are ASCII; the
declaration is a skipped section.

An IFD's **kind** is its `ImageType` text: `FullResolution`,
`ReducedResolution`, `Thumbnail`, `Overview`, `Label`, another string, or
none (no QPI description, or no `ImageType` child). Only the first two
shape the layout; the others, and any IFD whose kind is none or unknown,
are kept in the source metadata and otherwise ignored.

## 3. Images and channels

- **Channels.** IFD 0 MUST have a QPI description, and its kind MUST be
  `FullResolution`. The **channels** are the main-chain IFDs from IFD 0 to
  the last of the FullResolution IFDs that start the chain; their count `N`
  is at least 1. Channel `c` (from 0) is the IFD at index `c`. A
  FullResolution IFD later in the chain, after an IFD of another kind,
  rejects the input. When `N > 1`, IFD 0's SamplesPerPixel MUST be 1
  (several RGB channels are not supported).
- **Levels.** Level 0 is the channels. The ReducedResolution IFDs of the
  chain, in chain order, are taken `N` at a time: each run of `N` is the
  next level, its `c`-th IFD the level's image of channel `c`. Their count
  MUST be a multiple of `N`. IFDs of other kinds between them (the Thumbnail
  between level 0 and level 1) are skipped. The levels are numbered 0, 1,
  ... in that order.
- **Within a level**, every IFD MUST have ImageWidth and ImageLength, both
  at least 1, and all the IFDs MUST have the same width, length, format and
  tiling ([§4.2](#42-tiles-and-strips): tiled with the same TileWidth and
  TileLength, or in strips with the same RowsPerStrip). Every IFD of every
  level MUST have IFD 0's format. A level after the first MUST be strictly
  smaller than the level before in both width and length.
- A channel's **name** is the `Name` text of its FullResolution IFD, and
  its **color** the `Color` text; the image's **name** is IFD 0's `SlideID`
  text. The ReducedResolution IFDs' names are not used (the scanners'
  brightfield levels have none).

## 4. The image

### 4.1 Data types and codecs

The data type and codecs are those of [the TIFF convention §4.3](tiff.md#43-data-types-and-codecs),
including its requirements on JPEG, FillOrder and YCbCr subsampling, its LZW row
(`bytes`, `refkv.tiff_lzw`: the scanners' default) and its rejection of a predictor.

### 4.2 Tiles and strips

An IFD is tiled or in strips as [the TIFF convention §2](tiff.md#2-the-source) says (a tiled
IFD MUST have TileLength and TileByteCounts, with TileWidth and TileLength at least 1), and
a level in strips has the rows per strip `R`, the chunks of `R` rows and the padding of a
short last strip of [the TIFF convention §4.5](tiff.md#45-strips) (its limits on the
padding and on the bytes an LZW tail reads included, which reject the input whatever the
level), with one difference: a level whose short last strip cannot be padded (a
compression other than uncompressed and LZW) is not a level, and its images stay in the
source metadata. If level 0 would be left out, the input is rejected. (The scanners write 512 × 512 tiles for images larger than
about 2K × 2K, and strips of 16 rows for smaller ones, so the smallest levels of a pyramid,
and inForm's component data, are in strips.)

### 4.3 Arrays

The hierarchy is one image at the root ([conventions §4](../conventions.md#4-images)),
with one array per level at path `"<level>"`. Let `spp` be IFD 0's
SamplesPerPixel. The channel count is `N` when `spp` is 1, else `spp` (one RGB channel,
whose samples are the channel axis). The axes, in this order, and along each, a level's
shape and chunk shape and the image's scale (level `L` has width `WL` and length `HL`,
level 0 width `W0` and length `H0`):

| axis | present when | shape | chunk shape | scale of level `L` | unit |
|---|---|---|---|---|---|
| `c` | `N > 1` or `spp > 1` | the channel count | `spp` if interleaved (`spp > 1` and PlanarConfiguration 1), else 1 | 1 | none |
| `y` | always | the level's ImageLength `HL` | for a tiled level, its TileLength; for a level in strips, `R` | `PY × (H0 / HL)` | `y`'s (**Pixel size**) |
| `x` | always | the level's ImageWidth `WL` | for a tiled level, its TileWidth; for a level in strips, the level's ImageWidth | `PX × (W0 / WL)` | `x`'s (**Pixel size**) |

The chunk grid is always the regular grid of that chunk shape: a short last strip is
padded ([§4.2](#42-tiles-and-strips)). (In a scale, the division is computed first.)

<a id="type-qptiffarray"></a>**`QptiffArray`**, level `L`'s `zarr.json`: an
[`ArrayDocument`](../conventions.md#type-arraydocument) with these members:

| member | type | presence | value |
|---|---|---|---|
| `shape` | array of integer | required | each present axis's shape, above |
| `data_type` | string | required | from [§4.1](#41-data-types-and-codecs) |
| `chunk_grid` | object | required | regular, its chunk shape each present axis's chunk shape, above |
| `codecs` | array of object | required | from [§4.1](#41-data-types-and-codecs) |
| `dimension_names` | array of string | required | the axes present, above |
| `attributes` | object | required | `{}` |

- **Pixel size.** `PX`, `PY` and the units of `x`, `y` are those of the
  [TIFF convention §4.4](tiff.md#44-arrays)'s resolution-tag rule (its third
  rule, from IFD 0's XResolution, YResolution and ResolutionUnit, 2 or 3,
  when the pixel is under 25.4 µm): the scanners write ResolutionUnit 3 and
  the pixels per centimeter, and 96 dots per inch when the resolution is
  unknown. Otherwise `PX`, `PY` are 1 with no unit. `c` has no unit.
- **Position.** The image has a translation ([conventions §5](../conventions.md#5-units)),
  the same at every level, when `x` and `y` have units and IFD 0 has
  XPosition `n / d` and YPosition `n' / d'` with `d` and `d'` not 0: the
  translation is `x = (n / d) × u`, `y = (n' / d') × u`, where `u` is
  25400 for ResolutionUnit 2 and 10000 for 3 (the positions are in
  ResolutionUnits, and name the image's upper-left corner), and 0 for `c`.
  The translation MUST be finite.
- **Name:** IFD 0's `SlideID` text, if present and not empty.
- **Chunks:** for channel `c` of a level, its IFD's tiles (or strips) are
  numbered `k = s × T + j`, where `T` is the number of tiles (strips) per
  sample plane (`ceil(H / TileLength) × ceil(W / TileWidth)`, or the strip
  count), `j` runs row-major over the tile grid (down the strips), and `s`
  is the sample when `spp > 1` and planar (else 0). Both offset and byte
  count tables MUST have `T` times the number of sample planes values. The
  chunk with coords `c` (the channel, or the sample `s`, or 0 when
  interleaved; only when `c` is present), then the tile row and column (the
  strip index and 0), is tile `k`. It is present when its byte count is
  more than 0, and absent (the fill value) otherwise. Its bytes are the
  tile's or strip's, encoded as the codecs say (a short last strip's, with
  its padding, [§4.2](#42-tiles-and-strips)); for JPEG, as
  [the TIFF convention §4.4](tiff.md#44-arrays) says of a tile: a complete
  stream with the IFD's JPEGTables and an explicit color transform, with
  the same requirements on JPEGTables and on the tile's length. Its
  reference is a [`QptiffChunk`](#type-qptiffchunk) ([§7](#7-qptiff-profile)).

**omero.** `M` has `"omero"`, an [`Omero`](../conventions.md#type-omero), next to
`multiscales`, with one [`QptiffOmeroChannel`](#type-qptiffomerochannel) per channel of the
`c` axis (one when there is no `c` axis). Channel `c`'s name qualifies when it is present,
not empty and at most 256 bytes.

<a id="type-qptiffomerochannel"></a>**`QptiffOmeroChannel`**, an
[`OmeroChannel`](../conventions.md#type-omerochannel):

| member | type | presence | value |
|---|---|---|---|
| `label` | string | required | with `spp = 1`, channel `c`'s name when it qualifies, else `C<c>`; with `spp > 1`, channel 0's name (when it qualifies) followed by a space and the sample's letter (below), or that letter alone |
| `color` | string | required | with `spp = 1`, its `Color` text when that is three decimal integers from 0 to 255 separated by commas (whitespace around each allowed), as six uppercase hex digits `RRGGBB`, else `FFFFFF`; with `spp > 1`, the sample's color (below) |
| `active` | boolean | required | `true` |
| `window` | [`Window`](../conventions.md#type-window) | optional | the channel's **window** (below); absent for a `float32` channel without a sampled window |

With `spp > 1`, the samples' letters and colors:

| `spp` | channels | letter of sample `s` | color |
|---|---|---|---|
| 3 | three | `R`, `G` or `B` | `FF0000`, `00FF00`, `0000FF` |
| any other | `spp` | `S<s>` | `FFFFFF` |

Every channel has `"active": true` and, but for a `float32` channel
without a sampled window, a **window**, as in [the TIFF convention
§4.4](tiff.md#44-arrays): a QPTIFF records no display range
(SMinSampleValue and SMaxSampleValue, when present, are the data's
extremes, not a display setting), so the window is sampled from the
middle chunk of each channel of the last level, there in steps 1 to 3,
when `spp` is not 3 (an RGB image, whose samples keep the type's range)
and Compression is 1, 5 (LZW, decoded as [§4.1](#41-data-types-and-codecs)
says, into the chunk's size) or 8 or 32946 (Deflate); a level in strips
has the chunk shape above, and a padded last strip's rows past the image
are not values. A channel without a sampled window has, with an integer
data type, the window of the type's whole range (0 to 255 for `uint8`),
and with `float32` none, which has no such range.

The members are written in the order label, color, active, window, and
the window's in the order min, max, start, end.

## 5. Source metadata

The root's source metadata `S` is a [`TiffRoot`](tiff.md#type-tiffroot)
(`{"byte_order": "little" | "big", "bigtiff": true | false}`), as the TIFF convention's. Everything else the
file holds is on `refkv_source`, which is the file's **IR mirror**
([conventions §11.3](../conventions.md#113-the-ir-mirror)).
The QPTIFF **source model** is the [TIFF source model](tiff.md#5-source-metadata)
(its elements, which IFDs, its values), without `ome/planes/<p>` (a QPTIFF
has no OME-XML planes), and with the TIFF convention's named array rule
("layout tables", [conventions §8.8](../conventions.md#88-canonical-form)),
so that a level's thousands of tiles fold into one row of the table, whose
extents are read from the TIFF's own TileOffsets and TileByteCounts (or
StripOffsets and StripByteCounts). Every QPI description, the ScanProfile
included, is a value of its IFD's ImageDescription, held in the mirror; the
channel names and colors the layout reads are also in `omero`.

Hierarchies of one file are compared entry for entry, `refkv_source` included
([conventions §11.4](../conventions.md#114-equivalence)).

## 6. Example

A fluorescence scan at `https://example.org/slide.qptiff`, the 65,154-byte
`qptiff_fl_uint8_lzw.qptiff` of refkv's test fixtures: three `uint8`
channels (DAPI, Cy3 & TRITC, Opal 520) of 96 × 128 pixels in 32 × 32 LZW
tiles, a thumbnail, two reduced levels (48 × 64 in tiles; 24 × 32 in two
strips of 16 and 8 rows), an overview and a label. The root's property and
OME metadata:

```json
{
  "profile": "qptiff",
  "version": 0,
  "source": {"url": "https://example.org/slide.qptiff"},
  "qptiff": {"byte_order": "little", "bigtiff": false}
}
```

```json
{
  "version": "0.5",
  "multiscales": [{
    "name": "Fixture-Slide",
    "axes": [{"name": "c", "type": "channel"}, {"name": "y", "type": "space", "unit": "micrometer"},
             {"name": "x", "type": "space", "unit": "micrometer"}],
    "datasets": [
      {"path": "0", "coordinateTransformations": [{"type": "scale", "scale": [1, 0.49800796812749004, 0.49800796812749004]},
                                                  {"type": "translation", "translation": [0, 484400.0, 88600.0]}]},
      {"path": "1", "coordinateTransformations": [{"type": "scale", "scale": [1, 0.9960159362549801, 0.9960159362549801]},
                                                  {"type": "translation", "translation": [0, 484400.0, 88600.0]}]},
      {"path": "2", "coordinateTransformations": [{"type": "scale", "scale": [1, 1.9920318725099602, 1.9920318725099602]},
                                                  {"type": "translation", "translation": [0, 484400.0, 88600.0]}]}
    ]
  }],
  "omero": {"channels": [
    {"label": "DAPI", "color": "0000FF", "active": true, "window": {"min": 0, "max": 255, "start": 0, "end": 255}},
    {"label": "Cy3 & TRITC", "color": "FF3C9D", "active": true, "window": {"min": 0, "max": 255, "start": 0, "end": 255}},
    {"label": "Opal 520", "color": "00FF00", "active": true, "window": {"min": 0, "max": 255, "start": 0, "end": 255}}
  ]}
}
```

Level `0` is `uint8` of shape `[3, 96, 128]` with chunk shape `[1, 32, 32]`
and codecs `[{"name": "bytes"}, {"name": "refkv.tiff_lzw"}]`; its chunk
`0/c/1/2/3` is tile 11 of IFD 1 (channel 1, tile row 2, column 3). Level
`2`, in strips, has shape `[3, 24, 32]` and chunk shape `[1, 16, 32]`; its
chunk `2/c/0/1/0` is the second strip of the level's DAPI image, 8 rows
high, padded to 16 by a tail spliced into its LZW stream. `refkv_source` is the IR mirror ([§5](#5-source-metadata)): the TIFF
source model's elements, among them each IFD's ImageDescription (IFD 0's
holds the ScanProfile) and the tiles and strips of the thumbnail, overview
and label, which the layout leaves out.

# Part 2. The profile

The QPTIFF profile of [spec/zarrify.md](../zarrify.md) is [§7](#7-qptiff-profile) of this document. It builds on the TIFF profile
([tiff.md §7](tiff.md#7-tiff-profile), [tiff.md](tiff.md#part-2-the-profile)). What every profile's Part 2 shares is in
[spec/zarrify.md, Profiles](../zarrify.md#profiles).

## 7. QPTIFF profile

**No signature.** A file's first bytes never choose this profile ([spec/zarrify.md
§1.2](../zarrify.md#12-input)): a file the TIFF profile's signature chose is read by it when it passes the
detection test below.

Akoya QPTIFF files are TIFFs or BigTIFFs, in either byte order, whose
images each carry a QPI description. They are read by [tiff.md §7.1](tiff.md#71-reading)
([tiff.md](tiff.md#71-reading)) with the changes below, with the tags of
[the convention §2](#2-the-source) (the TIFF table and the two it adds) as
the checked tags.

**Detection.** After the header is read (and the NDPI test,
[ndpi.md §7](ndpi.md#7-ndpi-profile), has failed), let `O` be the header's
first-IFD offset. The file is a QPTIFF if the entry count at `O` and the
entries it counts lie within the file, an entry among them has tag 305
(Software; of several, the first), with field type ASCII (2) and a count of
at least 15, its value lies within the file, and the value's first 15 bytes
are `PerkinElmer-QPI`. Otherwise it is a TIFF ([tiff.md §7](tiff.md#7-tiff-profile)).
This test never rejects; the IFD at `O` is then read as the first IFD of
the main chain, with every check of [tiff.md §7.1](tiff.md#71-reading).

**Reading.** The IFDs, limits and checks of [tiff.md §7.1](tiff.md#71-reading)
apply (the main chain, its SubIFDs to depth 4, at most 100000 IFDs; the
field type, count and place of every tag of the table in every IFD read,
XPosition and YPosition being RATIONAL tags). These values are read
([tiff.md §7.1](tiff.md#71-reading), **Values read**):

| of | values read |
|---|---|
| every main-chain IFD | every tag of the table but the tile and strip tables (TileOffsets, TileByteCounts, StripOffsets, StripByteCounts), and its ImageDescription when its field type is ASCII (2), for the QPI description ([the convention §2](#2-the-source)) |
| every IFD of every level | its tile tables, or its strip tables |
| every IFD of a level in LZW strips whose last strip is short | the bytes of its last strip (of each sample plane), for the tail of [the convention §4.2](#42-tiles-and-strips) |
| after them, each sampled chunk of the `omero` windows ([the convention §4.3](#43-arrays)) | its bytes, the source ranges its reference names. These reads and their decoding never reject the input: a chunk that does not decode is not sampled |

Each value read of an integer type MUST be at most 2^53 − 1. Nothing else
is read for the layout.

**Rejection.** The input is rejected when a check fails, and when the
convention gives it no layout: wherever the convention says that the input
is rejected, or that something MUST hold and it does not
([the convention §2–§4](#2-the-source)). The input is rejected with the
reason the profile meets first, in this order: the baseline ([the
convention §3](#3-images-and-channels): IFD 0's description and kind, a
late FullResolution image, the samples per pixel of several channels), IFD
0's predictor, the count of ReducedResolution images, then level by level
(each IFD's size, format, samples and tiling, the level's uniformity and
size), then the data type and codecs, then each level's tiles or strips
(their tables' counts, each tile within the file), then the padding of each
level in strips (a last strip that does not decode to its size; level 0 of
a compression that cannot be padded), then the translation.

**Chunks.** Every chunk that the convention says is present ([the
convention §4.3](#43-arrays)) is the entry `<level>/c/<coords>`, whose reference is a
[`QptiffChunk`](#type-qptiffchunk):

<a id="type-qptiffchunk"></a>**`QptiffChunk`**, the reference of tile (or strip) `k`, with
byte count `n > 0`, `offset` TileOffsets (StripOffsets) and `n`
TileByteCounts`[k]` (StripByteCounts`[k]`):

| part | range | presence | value |
|---|---|---|---|
| the tile or strip | source range `(0, offset[k], n)` | optional | but for JPEG and an LZW short last strip: the tile's or strip's bytes |
| the zero rows | `Z`, source range `(i, 0, z)` | optional | for an uncompressed short last strip that lacks `z > 0` bytes of the chunk's size ([the convention §4.2](#42-tiles-and-strips)), after the strip: the data source `i` ([§1.2](../zarrify.md#12-input)) that holds those `z` zero bytes (one data source per distinct count, in order of first use: `(R − rows) × B` when every such strip holds its rows) |
| the strip before the cut | source range `(0, offset[k], p)` | optional | for an LZW short last strip: `p` the bytes before the cut's byte |
| the tail | `T`, a literal range | optional | for an LZW short last strip: the literal tail |
| a JPEG tile or strip | the ranges of a [`TiffChunk`](tiff.md#type-tiffchunk) | optional | for JPEG, in place of the others: the ranges of [tiff.md §7.3](tiff.md#73-chunks) (its first 2 bytes, the shared prefix `P` of its IFD, the rest), with the same requirements |

A short last strip thus has, when uncompressed, the ranges `[(0, offset[k], n), Z]`, and
when LZW, the ranges `[(0, offset[k], p), T]`; any other tile or strip but a JPEG one has the
single range `(0, offset[k], n)`. The references are
listed (for [§1.2](../zarrify.md#12-input)'s order of first use) level
by level, within a level by channel, and within an image by `k`. Besides
these and the root's and the arrays' `zarr.json`, the archive's entries are
those of the source metadata node `refkv_source`, the file's IR mirror
([the convention §5](#5-source-metadata), [conventions §8](../conventions.md#8-the-ir-mirror)).
