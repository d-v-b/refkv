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
Schema: [schema.json](qptiff/schema.json) · Changed 2026-10-23: each `omero` channel of an
integer type has the type's range as its window ([§4.3](#43-arrays)), after
[revision 29](../history/virtualize/REVISIONS.md#revision-29-no-recorded-revision-the-profile-registry)
(a profile's changes are recorded here, dated, from then on)

This document has two parts. [Part 1](#part-1-the-convention) is the QPTIFF
convention: the Zarr layout, cited as "the convention §n". [Part 2](#part-2-the-profile)
is the QPTIFF profile: how refkv reads the source, which inputs it rejects, and how
each chunk references the source. The profile keeps its numbering as a section
of [spec/virtualize.md](../virtualize.md), and is cited as spec/virtualize.md §n.

The format is described by Akoya's "PerkinElmer image format" note
(`PKI_Image Format.docx`, 2015, with its public sample files at
<https://downloads.openmicroscopy.org/images/Vectra-QPTIFF/>), which this
convention follows where the samples do.

# Part 1. The convention

This convention gives a layout only to the files that meet its
requirements. Where it says that "the input is rejected", or that
something MUST hold, a file that fails has no layout under this
convention, and the profile rejects it.

## 1. Declaration

The root declares the convention by [conventions §2](../conventions.md#2-attributes),
with `"profile": "qptiff"`, `"version": 0` ([conventions §1](../conventions.md#1-conventions)), the file's URL as `source.url`,
and the source metadata of [§5](#5-source-metadata) as the member `"qptiff"`. Its CMO is:

```json
{
  "uuid": "329ea136-2f55-4c27-b6f4-8e60ca2109b8",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/virtualize/qptiff/schema.json",
  "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/virtualize/qptiff.md",
  "name": "refkv_virtualized",
  "description": "The Zarr layout of a QPTIFF source virtualized by refkv, and the source's metadata"
}
```

`refkv_source` declares it too, with the IR mirror's description ([§5](#5-source-metadata),
[conventions §8](../conventions.md#8-the-ir-mirror)), and so does each group of
the mirror's view that has a member; the mirror's arrays have no
attributes.

## 2. The source

The file is a TIFF (magic 42) or BigTIFF (magic 43), in either byte order,
whose first IFD's Software tag (305) is ASCII text starting with
`PerkinElmer-QPI` ([spec/virtualize.md §16](#16-qptiff-profile) says how
that is tested). Its **IFDs** are the image file directories of its **main
chain**, which starts at the header's first-IFD offset and ends at a
next-IFD offset of 0, and MUST have at least one IFD; **IFD 0** is its
first. The SubIFDs of an IFD (tag 330) are read as the
[TIFF convention §2](tiff.md#2-the-source) says, for the source metadata
only: the layout uses the main chain alone. Of duplicate tags in an IFD,
the first is used.

The layout uses the tags of the [TIFF convention's table](tiff.md#2-the-source)
(with their defaults; field types and counts as the profile checks them),
and these:

| tag | name | kind | default |
|---|---|---|---|
| 273, 279 | StripOffsets, StripByteCounts | array | required for images in strips |
| 278 | RowsPerStrip | scalar | 2^32 − 1 |
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
including its requirements on JPEG, FillOrder and YCbCr subsampling, with
one more row in its table:

| Compression | Predictor | codecs ([conventions §3](../conventions.md#3-arrays)) |
|---|---|---|
| 5 (LZW) | 1 | bytes, `imagecodecs_lzw` |

`{"name": "imagecodecs_lzw"}` has no configuration: each chunk is one TIFF
LZW stream (TIFF 6.0 §13: codes packed most significant bit first, with the
"early change" every TIFF writer uses), which decodes to the chunk's bytes
as `bytes` reads them. A decoder reads the stream until its output reaches
the chunk's size and no further, as TIFF readers decode a tile into a buffer
of its size: some scanners write a tile's end-of-information code one bit
too narrow (a reader at the right width sees code 514, one more table entry,
or an invalid code), so a decoder that reads the whole stream overruns the
tile by a few bytes or fails; a chunk whose stream ends before the chunk's
size is malformed. Like the TIFF convention's uncompressed, Deflate and
zstd rows, LZW requires FillOrder 1 and Predictor 1: a horizontal
predictor (2) or a floating-point predictor (3) rejects the input.

### 4.2 Tiles and strips

An IFD is **tiled** if it has TileWidth and TileOffsets; a tiled IFD MUST
have TileLength and TileByteCounts, with TileWidth and TileLength at least
1. An IFD that is not tiled is **in strips**: it MUST have StripOffsets
and StripByteCounts, and RowsPerStrip (default 2^32 − 1) MUST be at least 1.
Its **rows per strip** `R` is `min(RowsPerStrip, ImageLength)`, and it has
`ceil(ImageLength / R)` strips per sample plane, the last holding the
remaining rows. (The scanners write 512 × 512 tiles for images larger than
about 2K × 2K, and strips of 16 rows for smaller ones, so the smallest
levels of a pyramid, and inForm's component data, are in strips.)

A level in strips has chunks of `R` rows ([§4.3](#43-arrays)). When its
last strip is **short** (the level has more than one strip and ImageLength
is not a multiple of `R`), the last strip's chunk is **padded** to `R` rows
so that every chunk decodes to the chunk shape (a Zarr reader then needs no
irregular chunk grid): the strip's bytes are followed by the bytes that
make the rows missing zero, which depend on the compression:

- **1 (uncompressed):** `(R − rows) × B` zero bytes, `rows` being the
  strip's rows and `B` the bytes of one row of the strip (ImageWidth ×
  BitsPerSample / 8, times SamplesPerPixel when the samples are
  interleaved);
- **5 (LZW):** the strip's stream is read until its output reaches the
  strip's size (`rows × B` bytes; a stream that does not reach it, or
  whose code crosses it, rejects the input), and what follows (its
  end-of-information code, or the malformed final code of
  [§4.1](#41-data-types-and-codecs)) is replaced by a **tail** that
  continues the stream: a Clear code at the width the decoder has there,
  then the `(R − rows) × B` zeros as the codes 0, 258, 259, ... (each code
  one zero longer than the last, the entry the decoder defines on reading
  the previous one) and one more code for the remainder shorter than the
  next entry, then the end-of-information code, every code at the width
  the decoder has when it reads it. The chunk's bytes are the strip's
  bytes before the byte holding the cut, then the tail, which starts with
  that byte's bits before the cut;
- **another compression** (JPEG, Deflate, zstd, JPEG 2000) cannot be
  padded: the level is not a level, and its images stay in the source
  metadata. If level 0 would be left out, the input is rejected.

### 4.3 Arrays

The hierarchy is one image at the root ([conventions §4](../conventions.md#4-images)),
with one array per level at path `"<level>"`. Let `spp` be IFD 0's
SamplesPerPixel.

- **Axes:** `c` if `N > 1` or `spp > 1`; then `y`, `x`. The channel count
  is `N` when `spp` is 1, else `spp` (one RGB channel, whose samples are the
  channel axis).
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
- **Shape:** the channel count (when `c` is present), then the level's
  ImageLength and ImageWidth.
- **Chunk shape:** for `c`, `spp` if interleaved (`spp > 1` and
  PlanarConfiguration 1), else 1; then, for a tiled level, its TileLength
  and TileWidth; for a level in strips, `R` and the level's ImageWidth. The
  chunk grid is always the regular grid of that shape: a short last strip
  is padded ([§4.2](#42-tiles-and-strips)).
- **Scale** of level `L` (level 0 has width `W0`, length `H0`):
  `y = PY × (H0 / HL)`, `x = PX × (W0 / WL)`, `c = 1`. (The division is
  computed first.)
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
  the same requirements on JPEGTables and on the tile's length.
- **omero.** `M` has `"omero": {"channels": [...]}` next to `multiscales`,
  one object per channel of the `c` axis (one object when there is no `c`
  axis):
  - with `spp = 1`, channel `c`'s **label** is its name when present, not
    empty and at most 256 bytes, else `C<c>`; its **color** is its `Color`
    text when that is three decimal integers from 0 to 255 separated by
    commas (whitespace around each allowed), as six uppercase hex digits
    `RRGGBB`, else `FFFFFF`;
  - with `spp = 3`, three channels: the label is channel 0's name (as
    above, when it qualifies) followed by a space and `R`, `G` or `B`, or
    that letter alone, with colors `FF0000`, `00FF00`, `0000FF`; with any
    other `spp > 1`, `spp` channels labeled likewise with `S<s>` and
    colored `FFFFFF`;
  - every channel has `"active": true`; with an integer data type, the
    **window** of the type's whole range, as in [the TIFF convention
    §4.4](tiff.md#44-arrays) (0 to 255 for `uint8`), and with `float32` no
    window, which has no such range: a QPTIFF records no display range
    (SMinSampleValue and SMaxSampleValue, when present, are the data's
    extremes, not a display setting), and a reader that needs contrast
    samples it from the data, as refkv's demo does.

  The members are written in the order label, color, active, window.

## 5. Source metadata

The root's source metadata `S` is `{"byte_order": "little" | "big",
"bigtiff": true | false}`, as the TIFF convention's. Everything else the
file holds is on `refkv_source`, which is the file's **IR mirror**
([conventions §8](../conventions.md#8-the-ir-mirror)): its table, from which the
file is rebuilt byte for byte, and its view, `refkv_source/tree`. The QPTIFF
**source model** is the [TIFF source model](tiff.md#5-source-metadata)
(its elements, which IFDs, its values), without `ome/planes/<p>` (a QPTIFF
has no OME-XML planes), and with the TIFF convention's named array rule
("layout tables", [conventions §8.8](../conventions.md#88-canonical-form)),
so that a level's thousands of tiles fold into one row of the table, whose
extents are read from the TIFF's own TileOffsets and TileByteCounts (or
StripOffsets and StripByteCounts). Every QPI description, the ScanProfile
included, is a value of its IFD's ImageDescription, held in the mirror; the
channel names and colors the layout reads are also in `omero`.

The elements are determined by the file, and the mirror by its elements
([conventions §8.8](../conventions.md#88-canonical-form)): hierarchies of one
file are compared entry for entry, `refkv_source` included
([spec/virtualize.md §1.1](../virtualize.md#11-output-and-equivalence)).

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
and codecs `[{"name": "bytes"}, {"name": "imagecodecs_lzw"}]`; its chunk
`0/c/1/2/3` is tile 11 of IFD 1 (channel 1, tile row 2, column 3). Level
`2`, in strips, has shape `[3, 24, 32]` and chunk shape `[1, 16, 32]`; its
chunk `2/c/0/1/0` is the second strip of the level's DAPI image, 8 rows
high, padded to 16 by a tail spliced into its LZW stream. `refkv_source` is the IR mirror ([§5](#5-source-metadata)): the TIFF
source model's elements, among them each IFD's ImageDescription (IFD 0's
holds the ScanProfile) and the tiles and strips of the thumbnail, overview
and label, which the layout leaves out.

# Part 2. The profile

The QPTIFF profile of [spec/virtualize.md](../virtualize.md), numbered as
its [§16](#16-qptiff-profile) (the number is the registry's,
[profiles.json](profiles.json)). It builds on the TIFF profile
([§3](tiff.md#3-tiff-profile), [tiff.md](tiff.md#part-2-the-profile)).
[§1](../virtualize.md#1-introduction) and [§2](../virtualize.md#2-the-zarr-layout)
are in spec/virtualize.md and apply here.

The output has the convention's layout for the input
([spec/virtualize.md §2](../virtualize.md#2-the-zarr-layout)); this profile says
how the file is read, which inputs are rejected, and how each chunk
references the file. "The convention §n" below is a section of Part 1.

## 16. QPTIFF profile

Akoya QPTIFF files are TIFFs or BigTIFFs, in either byte order, whose
images each carry a QPI description. They are read by [§3.1](tiff.md#31-reading)
([tiff.md](tiff.md#31-reading)) with the changes below, with the tags of
[the convention §2](#2-the-source) (the TIFF table and the five it adds) as
the checked tags.

**Detection.** After the header is read (and the NDPI test,
[§4](ndpi.md#4-ndpi-profile), has failed), let `O` be the header's
first-IFD offset. The file is a QPTIFF if the entry count at `O` and the
entries it counts lie within the file, an entry among them has tag 305
(Software; of several, the first), with field type ASCII (2) and a count of
at least 15, its value lies within the file, and the value's first 15 bytes
are `PerkinElmer-QPI`. Otherwise it is a TIFF ([§3](tiff.md#3-tiff-profile)).
This test never rejects; the IFD at `O` is then read as the first IFD of
the main chain, with every check of [§3.1](tiff.md#31-reading).

**Reading.** The IFDs, limits and checks of [§3.1](tiff.md#31-reading)
apply (the main chain, its SubIFDs to depth 4, at most 100000 IFDs; the
field type, count and place of every tag of the table in every IFD read,
RowsPerStrip, StripOffsets and StripByteCounts being integer tags and
XPosition and YPosition RATIONAL ones). These values are read
([§3.1](tiff.md#31-reading), **Values read**):

- of every main-chain IFD: every tag of the table but the tile and strip
  tables (TileOffsets, TileByteCounts, StripOffsets, StripByteCounts), and
  its ImageDescription when its field type is ASCII (2), for the QPI
  description ([the convention §2](#2-the-source));
- of every IFD of every level: its tile tables, or its strip tables;
- of every IFD of a level in LZW strips whose last strip is short: the
  bytes of its last strip (of each sample plane), for the tail of
  [the convention §4.2](#42-tiles-and-strips).

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
convention §4.3](#43-arrays)) is the entry `<level>/c/<coords>`. Tile (or
strip) `k`, with byte count `n > 0`, has the single range
`(0, offset[k], n)`, where `offset` is TileOffsets (StripOffsets) and `n`
TileByteCounts`[k]` (StripByteCounts`[k]`); a JPEG tile or strip has the
ranges of [§3.3](tiff.md#33-chunks) (its first 2 bytes, the shared prefix
`P` of its IFD, the rest), with the same requirements. A short last strip
([the convention §4.2](#42-tiles-and-strips)) has, when uncompressed, the
ranges `[(0, offset[k], n), Z]`, `Z` a range of the data source
([§1.2](../virtualize.md#12-input)) that holds the `(R − rows) × B` zero
bytes (one data source per distinct count, in order of first use); when
LZW, the ranges `[(0, offset[k], p), T]`, `p` the bytes before the cut's
byte and `T` the literal tail. The references are
listed (for [§1.2](../virtualize.md#12-input)'s order of first use) level
by level, within a level by channel, and within an image by `k`. Besides
these and the root's and the arrays' `zarr.json`, the archive's entries are
those of the source metadata node `refkv_source`, the file's IR mirror
([the convention §5](#5-source-metadata), [conventions §8](../conventions.md#8-the-ir-mirror)).
