# TIFF

The Zarr layout of a TIFF or BigTIFF file, including OME-TIFF and JPEG-tiled
slides such as Aperio SVS, and the translation of its tags into JSON. What
all of refkv's conventions share is in [spec/conventions.md](../conventions.md),
cited here as "conventions §n". How refkv produces this layout as a reference
store is the TIFF profile, [Part 2](#part-2-the-profile) below.

Convention version: 0 (until release, [conventions §1](../conventions.md#1-conventions)) · UUID: `48e9ac4e-1156-4a62-955e-20467d9c2700` ·
Schema: [schema.json](tiff/schema.json) ·
Changed 2026-10-27: an image of an integer type with Predictor 2 (horizontal differencing) and
LZW, Deflate or zstd has a layout, its codecs `imagecodecs_delta` before `bytes`
([§4.3](#43-data-types-and-codecs)), and its `omero` windows sampled through the predictor
([§4.4](#44-arrays)); any other predictor, and Predictor 2 with a `float` type, with no
compression or with JPEG, is still rejected; no output of a file accepted before changed ·
Changed 2026-10-27: images in strips and LZW
(Compression 5) have a layout, which they had in the QPTIFF convention only: a level in strips
has chunks of its rows per strip, a short last strip padded to them when uncompressed (with a
range of the output's zero data source, [§7.3](#73-chunks)) or LZW ([§4.5](#45-strips)), and
LZW is `refkv.tiff_lzw` ([§4.3](#43-data-types-and-codecs)); the
table of [§2](#2-the-source) holds the strip tags, which the profile now checks in every IFD it
reads ([§7.1](#71-reading)); no output of a file accepted before changed ·
Changed 2026-10-27: a zlib stream's codec is `numcodecs.zlib`, zarr-python's name for it, in place of `zlib` ([conventions §3](../conventions.md#3-arrays), d-v-b/refkv#90) ·
Changed 2026-10-26, with no change to any output:
the layout is tabulated, its entries in [Layout](#layout) and its types in type tables
([conventions §11.5](../conventions.md#115-layout-tables)); the QPTIFF convention shares
[`TiffRoot`](#type-tiffroot) and [`TiffChunk`](#type-tiffchunk); and a tile's or strip's
recipe frames it with the IFD's JPEG prefix whenever that prefix is not empty (the Adobe marker
of 3 samples included, with or without JPEGTables) and the tile is more than 2 bytes, as
refkv already wrote ([§5.1](#51-elements)) · Changed 2026-10-23: each `omero` channel's window
is sampled from the coarsest level's data when refkv decodes its compression and the samples are
not RGB, and an image of a float type has an `omero` when every channel's window is sampled
([§4.4](#44-arrays)); the profile reads the sampled chunks
([§7.1](#71-reading)) · Changed 2026-10-22: an image of an integer type has
an `omero` member ([§4.4](#44-arrays)), with the OME-XML's channel names and colors
([§3](#3-ome-xml)) and the type's range as each channel's window, after
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
| `zarr.json` | group | [`ImageGroup`](../conventions.md#type-imagegroup) | the root: the image ([§4.4](#44-arrays)) |
| `zarr.json#/attributes/ome` | attribute | [`OmeImage`](../conventions.md#type-omeimage) | `M`: one multiscale, with the name, axes, scales and translation of [§4.4](#44-arrays), one level per array; `omero` as [§4.4](#44-arrays) says, its channels [`TiffOmeroChannel`](#type-tiffomerochannel)s |
| `zarr.json#/attributes/zarr_conventions` | attribute | array of [`ConventionMetadata`](../conventions.md#type-conventionmetadata) | the CMO of [§1](#1-declaration) |
| `zarr.json#/attributes/refkv_zarrified` | attribute | [`RootProperty`](../conventions.md#type-rootproperty) | `profile` `"tiff"`, `version` `0`, `source.url` the file's URL |
| `zarr.json#/attributes/refkv_zarrified/tiff` | attribute | [`TiffRoot`](#type-tiffroot) | the root's source metadata ([§5](#5-source-metadata)) |
| `<L>/zarr.json` | array | [`TiffArray`](#type-tiffarray) | level `L` ([§4.2](#42-pyramid-levels), [§4.4](#44-arrays)) |
| `<L>/c/<coords>` | chunk | [`TiffChunk`](#type-tiffchunk) | a tile or strip of a plane of level `L`, present when its byte count is more than 0 ([§4.4](#44-arrays), **Chunks**; [§7.3](#73-chunks)) |
| `refkv_source/**` | subtree | [the mirror's entries](../conventions.md#table-mirror) | the IR mirror ([§5](#5-source-metadata)): its elements are [§5.1](#51-elements)'s |

| placeholder | value |
|---|---|
| `<L>` | the level's index, from 0, in decimal |
| `<coords>` | the chunk's coordinates, in decimal, one key segment each: `<t>/<c>/<z>/<row>/<column>`, `t`, `c` and `z` each only for the axes present, then the tile row and column, or the strip index and 0 (so it stands for two to five segments) |

## 1. Declaration

The root and `refkv_source` declare the convention
([conventions §11.2](../conventions.md#112-declaration)), the root with the source
metadata of [§5](#5-source-metadata) as the member `"tiff"`. Its CMO is:

```json
{
  "uuid": "48e9ac4e-1156-4a62-955e-20467d9c2700",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/zarrify/tiff/schema.json",
  "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md",
  "name": "refkv_zarrified",
  "description": "The Zarr layout of a TIFF source zarrified by refkv, and the source's metadata"
}
```

## 2. The source

The file is a TIFF (magic 42) or BigTIFF (magic 43), in either byte order.
Its **IFDs** are the image file directories of its **main chain**, which
starts at the header's first-IFD offset and ends at a next-IFD offset of 0,
and, for each of them, its **SubIFDs**: the IFDs whose offsets its
`SubIFDs` tag (330) lists, in order, and theirs in turn. Main-chain IFDs
are at **depth** 0 and the SubIFDs of an IFD at depth `d` at depth `d + 1`;
the SubIFDs of an IFD at depth 4 are not read. (A SubIFD's next IFD is not
part of the source.) The main chain MUST have at least one IFD. **IFD 0**
is the first IFD of the main chain. Of duplicate tags in an IFD, the first
is used.

The layout uses these tags (absent tags take the defaults shown); field
types and counts are as the profile checks them
([§7.1](#71-reading)):

| tag | name | kind | default |
|---|---|---|---|
| 256, 257 | ImageWidth, ImageLength | scalar | required |
| 258 | BitsPerSample | array | required |
| 259 | Compression | scalar | 1 |
| 262 | PhotometricInterpretation | scalar | none (required for JPEG with 3 samples, [§4.3](#43-data-types-and-codecs)) |
| 266 | FillOrder | scalar | 1 |
| 270 | ImageDescription | text | none |
| 273, 279 | StripOffsets, StripByteCounts | array | required for images in strips |
| 277 | SamplesPerPixel | scalar | 1 |
| 278 | RowsPerStrip | scalar | 2^32 − 1 |
| 284 | PlanarConfiguration | scalar | 1 |
| 317 | Predictor | scalar | 1 |
| 322, 323 | TileWidth, TileLength | scalar | required for tiled images |
| 324, 325 | TileOffsets, TileByteCounts | array | required for tiled images |
| 330 | SubIFDs | array | none |
| 282, 283 | XResolution, YResolution | scalar | none |
| 296 | ResolutionUnit | scalar | none (2 for TIFF, but see [§4.4](#44-arrays)) |
| 339 | SampleFormat | array | 1 |
| 347 | JPEGTables | bytes | none |
| 530 | YCbCrSubsampling | array | `[2, 2]` (TIFF 6.0) |

**Format.** An IFD's **format** is the tuple (BitsPerSample, SamplesPerPixel,
SampleFormat, PlanarConfiguration, Compression, Predictor). Computing it
requires:

- BitsPerSample, with at least one value, all equal and at least 1;
- SamplesPerPixel at least 1;
- SampleFormat values all equal (when the tag is present it MUST have at
  least one value).

When SamplesPerPixel is 1, PlanarConfiguration is taken as 1. Otherwise it
MUST be 1 or 2. When Compression is 7, the format also includes
PhotometricInterpretation (absent counts as a value of its own). Counts of BitsPerSample and SampleFormat are not otherwise
checked. If any of these fails, the input is rejected. Formats are computed
for IFD 0 (always, even when it is not a plane), for every IFD that becomes
a plane or a level ([§4.1](#41-planes), [§4.2](#42-pyramid-levels)), and for every candidate of the level scan
([§4.2](#42-pyramid-levels)).

**Size.** An IFD that becomes a plane or a level, or is a candidate of the
level scan, MUST have ImageWidth and ImageLength, and both MUST be at least
1. A tiled one ([§4.2](#42-pyramid-levels)) MUST have TileWidth, TileLength, TileOffsets and
TileByteCounts, with TileWidth and TileLength at least 1. An IFD is
**tiled** if it has TileWidth and TileOffsets; one that is not is **in strips**, and MUST
have StripOffsets and StripByteCounts and a RowsPerStrip of at least 1 when it becomes a plane
or a level ([§4.5](#45-strips)).

## 3. OME-XML

If IFD 0's ImageDescription has field type ASCII (2), let `D` be its bytes up
to the first NUL. If `D` is valid UTF-8 and the tag scan below finds a start
tag named `OME` in it, `D` is the **OME-XML**, and `X` is its text.

**Tag scan.** `X` is read as a sequence of tags, not as a validated XML
document, in one left-to-right scan: at each `<`, a skipped section (1) if
one starts there, else a tag (2) if the grammar matches, else text; the
scan continues after whichever was found. So a `<!--` inside a tag's
attribute value is part of the value, not a comment.

1. **Skipped sections:** scanning from the start, the first of these to
   begin at each point is skipped, up to and including its end:

   | section | from | to |
   |---|---|---|
   | comment | `<!--` | the next `-->` |
   | CDATA section | `<![CDATA[` | the next `]]>` |
   | processing instruction | `<?` | the next `?>` |
   | other declaration | `<!` | the next `>` |

   The search for the end starts after the whole start marker (so `<!-->`
   does not end itself). A section without an end runs to the end of `X`.
2. **Tags:** outside skipped sections, a tag is a match of this grammar
   starting at a `<` (whitespace and digits as in [spec/zarrify.md §1.3](../zarrify.md#13-arithmetic)):

   ```
   tag    = "<" ["/"] qname *(ws1 attr) [ws] ["/"] ">"
   qname  = [name ":"] name
   name   = 1*(ASCII letter / digit / "_" / "." / "-")
   attr   = aname [ws] "=" [ws] ( DQUOTE *(not DQUOTE) DQUOTE / "'" *(not "'") "'" )
   aname  = 1*(any character except whitespace, "=", "/", ">", DQUOTE, "'", "<")
   ws     = 1*whitespace ; ws1 is the same
   ```

   A `<` where the grammar does not match is text. Scanning continues after
   the end of each tag.
3. A tag is a **start tag** if it does not begin with `</`, and an **end
   tag** if it does. It is **self-closing** if it ends with `/>`. Its
   **name** is the last `name` of its `qname`, without the namespace prefix.
   Attribute names are compared exactly. Of duplicate attribute names in a
   tag, the first is used.
4. **Attribute values** have references decoded: the five XML predefined
   entities (`&lt;` `&gt;` `&amp;` `&quot;` `&apos;`), and numeric character
   references (`&#` digits `;` or `&#x` hex digits `;`) that denote a Unicode
   scalar value other than U+0000. Anything else, including other entity
   references and references to U+0000, surrogates or values above
   U+10FFFF, is left as it is. Whitespace is not normalized.

From `X` the zarrifier uses:

| tag | which | what it gives |
|---|---|---|
| `Image` | the first start tag | its `Name` attribute (the image name) |
| `Pixels` | the first start tag | its attributes `SizeZ`, `SizeC`, `SizeT` and `DimensionOrder`; `PhysicalSizeX/Y/Z` and `PhysicalSizeX/Y/ZUnit` |
| `Plane` | the first start tag after that `Pixels` start tag and before the first `Pixels` end tag after it whose `TheZ`, `TheC` and `TheT` are each absent or `0` (as integer attributes) | its `PositionX`, `PositionY`, `PositionXUnit` and `PositionYUnit` (the stage position of the plane) |
| `Channel` | the start tags after that `Pixels` start tag and before the first `Pixels` end tag after it (or the end of `X`), in order | the `Name` and `Color` attributes of the first 64 (the channel names and colors of `omero`, [§4.4](#44-arrays)) |
| `TiffData` | the start tags after that `Pixels` start tag and before the first `Pixels` end tag after it (or the end of `X`), in order; if the `Pixels` start tag is self-closing, there are none | its attributes `IFD`, `FirstZ`, `FirstC`, `FirstT` and `PlaneCount` |
| `UUID` | for each `TiffData` that is not self-closing, the first `UUID` start tag after it and before the next `TiffData` end tag, `TiffData` start tag or `Pixels` end tag | its `FileName` attribute, and its **text**: empty if it is self-closing; otherwise the characters of `X` from the end of the `UUID` tag to the start of the next tag, with skipped sections (including the content of CDATA sections) removed, references decoded as in attribute values, and leading and trailing whitespace removed |

Without a `Pixels` start tag, every `Pixels` attribute is absent and there
are no `TiffData` tags.

**Values.**

| attributes | value |
|---|---|
| **Integer attributes** (`SizeZ`, `SizeC`, `SizeT`, `First*`, `IFD`, `PlaneCount`; `SizeX` and `SizeY` are not used) | the whole value MUST be one or more digits, optionally preceded and followed by whitespace, and at most 2^53 − 1. `SizeZ`, `SizeC`, `SizeT` and `PlaneCount` MUST be at least 1. Otherwise the input is rejected |
| **`PositionX`, `PositionY`** | like `PhysicalSize*` below, but any finite value counts (zero and negative too); the unit has no default (OME's default, "reference frame", is not a length) |
| **`PhysicalSize*`** | a value counts as present only if the whole value (with no whitespace) matches `[+-]?([0-9]+(\.[0-9]*)?\|\.[0-9]+)([eE][+-]?[0-9]+)?` and its value is finite and positive. Otherwise it is treated as absent |
| **`DimensionOrder`** | MUST be `XY` followed by a permutation of `ZCT`, or the input is rejected |
| **Unit attributes** | matched exactly (by code points) against [conventions §5](../conventions.md#5-units) |

The OME-XML's other content (for example `Interleaved`) is not used: the
TIFF tags decide.

## 4. The image

### 4.1 Planes

Let `spp` be IFD 0's SamplesPerPixel. Without OME-XML there is one plane,
IFD 0. With OME-XML:

- `SizeZ` and `SizeT` default to 1; `SizeC` defaults to `spp`.
- If `spp > 1`: `SizeC` 1 is read as `spp`; any other `SizeC ≠ spp` is
  rejected (several RGB channels are not supported). Channels are then the
  samples, and there is one plane per (z, t).
- If `spp = 1`, there is one plane per (z, c, t).

Let `Cp` be 1 if `spp > 1`, else `SizeC`. Planes have positions (z, c, t)
with `z < SizeZ`, `c < Cp`, `t < SizeT`; the plane count is
`SizeZ × Cp × SizeT`, which MUST be at most 100000. Planes are mapped to main-chain IFDs (by their index
in the main chain, IFD 0 being index 0) by the `TiffData` elements, in
document order (with one implicit `TiffData` without attributes if there
are none):

- **Multi-file datasets:** a `TiffData` with a `UUID` names the file
  identified by the `UUID`'s `FileName` if it has one, else by its text (two
  identifiers are the same file only if they are equal strings). If the
  `TiffData` elements name more than one distinct file, the input is
  rejected. Otherwise `UUID`s are ignored, so a file that names itself is
  accepted, and a `TiffData` without a `UUID` names no file.
- **Coverage:** a `TiffData` starts at position (`FirstZ`, `FirstC`,
  `FirstT`) (each default 0, and each MUST be less than `SizeZ`, `Cp`,
  `SizeT` respectively, else the input is rejected) and IFD index `IFD`
  (default 0). It covers `PlaneCount` planes. `PlaneCount` defaults to the
  plane count when it is the only `TiffData` and has no `IFD` attribute, and
  to 1 otherwise.
- **Stepping:** successive planes step through positions in
  `DimensionOrder` (default `XYZCT`; the letters after `XY` are fastest
  first, with sizes `SizeZ`, `Cp`, `SizeT`) and through consecutive IFD
  indices. Planes past the last position are ignored: with `P` the plane
  count and `L` the start position's index in that order (`L = a + A × (b
  + B × c)` for the letters after `XY` with positions `a`, `b`, `c` and
  sizes `A`, `B`), a `TiffData` covers `min(PlaneCount, P − L)` planes.
- **Cost:** the planes covered, summed over all `TiffData` elements, MUST
  be at most `4 × P + 1000`, else the input is rejected (so that a short
  document cannot step through many more planes than the image has).
- A plane mapped twice takes the later mapping.

After all `TiffData` elements are applied, every plane MUST be mapped to an
existing main-chain IFD, else the input is rejected. (An index past the
chain that a later mapping replaces is not an error.)

### 4.2 Pyramid levels

- If IFD 0 has SubIFDs, there are `1 + s` levels, where `s` is IFD 0's
  SubIFD count: level 0 is the planes' IFDs, and level `k ≥ 1` is, for every
  plane, its IFD's `k`-th SubIFD. A plane IFD with fewer than `s` SubIFDs
  rejects the input.
- Otherwise level 0 is the planes. Without OME-XML, the later main-chain
  IFDs are scanned in order. An IFD that is tiled and has BitsPerSample is a
  **candidate**: its format and size are checked as [§2](#2-the-source) says (a failure
  rejects the input). A candidate becomes the next level if it has IFD 0's
  format and is strictly smaller in both width and length than the last
  level. Other IFDs, and other candidates, are skipped. This finds the
  pyramids of Aperio SVS files, whose stripped thumbnails, labels and macros
  are skipped. (A tiled image of the right format that is not part of the
  pyramid would be taken as a level.)

Within a level, all planes MUST have the same width, length, tiling (tiled with the same
tile size, or in strips with the same rows per strip, [§4.5](#45-strips)) and format. Every
image of every level MUST have IFD 0's format. (The level scan above takes tiled images only;
planes and SubIFD levels may be in strips.) Without OME-XML and SubIFDs, when IFD 0 is in
strips, a later main-chain IFD that is not a level and has IFD 0's width, length and format
(a stack of pages, as ImageJ and tifffile write them) rejects the input: its planes have no
mapping, and the image would show only the first. (A later IFD whose format cannot be computed,
or whose values do not load, does not have IFD 0's. A tiled IFD 0 keeps the rule it had before
images in strips had a layout: its later pages of its size are not read as planes.)

### 4.3 Data types and codecs

The data type is `uint`, `int` or `float` (SampleFormat 1, 2, 3) followed by
BitsPerSample, which MUST be 8, 16, 32 or 64 (32 or 64 for `float`).

| Compression | Predictor | codecs ([conventions §3](../conventions.md#3-arrays)) |
|---|---|---|
| 1 | 1 | bytes |
| 5 (LZW) | 1 or 2 | bytes, `refkv.tiff_lzw` |
| 8, 32946 (Deflate) | 1 or 2 | bytes, `numcodecs.zlib` |
| 50000 (zstd) | 1 or 2 | bytes, zstd |
| 33003, 33004, 33005, 34712 (JPEG 2000) | any | imagecodecs_jpeg2k |
| 7 (JPEG) | 1 | imagecodecs_jpeg |

Anything else is rejected. JPEG (Compression 7) additionally requires
BitsPerSample 8, SampleFormat 1, and either SamplesPerPixel 1, or
SamplesPerPixel 3 with PlanarConfiguration 1 and PhotometricInterpretation
2 (RGB) or 6 (YCbCr); otherwise the input is rejected. (Old-style JPEG,
Compression 6, is rejected.)

`{"name": "refkv.tiff_lzw"}`, refkv's own codec (imagecodecs-zarr's `imagecodecs_lzw` reads the
whole stream, which the next sentences rule out), has no configuration: each chunk is one TIFF
LZW stream (TIFF 6.0 §13: codes packed most significant bit first, with the
"early change" every TIFF writer uses), which decodes to the chunk's bytes
as `bytes` reads them. A decoder reads the stream until its output reaches
the chunk's size and no further, as TIFF readers decode a tile into a buffer
of its size: some scanners write a tile's end-of-information code one bit
too narrow (a reader at the right width sees code 514, one more table entry,
or an invalid code), so a decoder that reads the whole stream overruns the
tile by a few bytes or fails; a chunk whose stream ends before the chunk's
size is malformed.

**Predictor.** Predictor 2, horizontal differencing (TIFF 6.0 §14), with an integer data type
(SampleFormat 1 or 2) puts `{"name": "imagecodecs_delta", "configuration": {"axis": a, "dist":
1}}` ([conventions §3](../conventions.md#3-arrays)) before `bytes`, `a` being the index of `x`
in stored order: `n − 1` for an array of `n` axes, or `n − 2` when `transpose` comes first. Within
each row of a tile or strip, the writer stored each sample less the same sample of the pixel
before it, in the data type's arithmetic (modulo 2^BitsPerSample), so that the decompressed
bytes are differences, and the codec sums them back along `x`, each sample apart. GDAL, libtiff
and tifffile write it with LZW, Deflate and zstd.

Any other Predictor rejects the input, but for JPEG 2000, whose streams do not use it, and so
does Predictor 2:
- with a `float` data type, whose values a writer differences as integers of their bits
  (libtiff does), which no codec of [conventions §3](../conventions.md#3-arrays) sums back;
- with Compression 1, which libtiff stores without differencing (it applies a predictor only
  inside a compression's coder) and tifffile does not write, so that readers disagree on what
  the bytes hold;
- with JPEG (Compression 7), whose stream does not use it.

Predictor 3, the floating-point predictor, differences the bytes of a row after reordering them
by significance, which no codec of [conventions §3](../conventions.md#3-arrays) undoes.

The codecs read the bytes of a tile as they are, filled from each byte's
most significant bit (FillOrder 1), and decode samples at full resolution.
So, for every IFD that is a plane or a level:
- when Compression is 1, 5, 8, 32946 or 50000, FillOrder MUST be 1;
- when PhotometricInterpretation is 6 (YCbCr) and Compression is neither 7
  nor JPEG 2000 (whose streams hold their own sampling), YCbCrSubsampling
  MUST be present and exactly `[1, 1]` (absent, it is TIFF 6.0's default
  `[2, 2]`).

Otherwise the input is rejected. `bytes` has `endian` from the TIFF's byte order
(when the data type is larger than 1 byte). When `spp > 1` and
PlanarConfiguration is 1 (interleaved), `transpose` comes first, for every
compression including JPEG 2000.

### 4.4 Arrays

The hierarchy is one image at the root ([conventions §4](../conventions.md#4-images)), with one array per level at
path `"<level>"`. The **channel count** is `SizeC` after [§4.1](#41-planes), or `spp`
without OME-XML. The axes, in this order, and along each, a level's shape and chunk shape and
the image's scale (level `L` has width `WL` and length `HL`, level 0 width `W0` and length
`H0`):

| axis | present when | shape | chunk shape | scale of level `L` | unit |
|---|---|---|---|---|---|
| `t` | `SizeT > 1` | `SizeT` | 1 | 1 | none |
| `c` | the channel count is more than 1 | the channel count | `spp` if interleaved, else 1 | 1 | none |
| `z` | `SizeZ > 1` | `SizeZ` | 1 | `PZ` | `z`'s (**Pixel size**) |
| `y` | always | the level's length `HL` | the level's TileLength; for a level in strips, its rows per strip `R` ([§4.5](#45-strips)) | `PY × (H0 / HL)` | `y`'s (**Pixel size**) |
| `x` | always | the level's width `WL` | the level's TileWidth; for a level in strips, `WL` | `PX × (W0 / WL)` | `x`'s (**Pixel size**) |

Levels may differ in tile size, and one may be tiled and another in strips. The chunk grid is
always the regular grid of that chunk shape: a short last strip is padded
([§4.5](#45-strips)). (In a scale, the division is computed first.)

<a id="type-tiffarray"></a>**`TiffArray`**, level `L`'s `zarr.json`: an
[`ArrayDocument`](../conventions.md#type-arraydocument) with these members:

| member | type | presence | value |
|---|---|---|---|
| `shape` | array of integer | required | each present axis's shape, above |
| `data_type` | string | required | from [§4.3](#43-data-types-and-codecs) |
| `chunk_grid` | object | required | regular, its chunk shape each present axis's chunk shape, above |
| `codecs` | array of object | required | from [§4.3](#43-data-types-and-codecs): `transpose` first when `spp > 1` and PlanarConfiguration is 1, then `imagecodecs_delta` with Predictor 2, then the codecs of the Compression's row |
| `dimension_names` | array of string | required | the axes present, above |
| `attributes` | object | required | `{}` |

- **Pixel size.** `PX`, `PY`, `PZ` and the units of `x`, `y`, `z` come from
  the first of these that applies:
  1. **OME-XML:** `PX`, `PY`, `PZ` are `PhysicalSizeX/Y/Z`, and `x`, `y`, `z`
     have the unit ([conventions §5](../conventions.md#5-units)) of `PhysicalSizeXUnit` etc. (default `µm`), each
     only when that `PhysicalSize` is present.
  2. **Aperio:** IFD 0 has an ImageDescription of type ASCII whose bytes up
     to the first NUL start with `Aperio` and are valid UTF-8. Its fields are
     the parts of that text between `|` characters; a field `name = value`
     is split at its first `=`, with whitespace around each part removed, and
     of fields with the same name the first is used. A field named `MPP`
     whose value is a
     present decimal (as `PhysicalSize*`, [§3](#3-ome-xml)) gives `PX = PY` = that
     value, and `x`, `y` the unit `micrometer`.
  3. **Resolution tags:** IFD 0 has XResolution `n / d` with `n` and `d`
     positive and a ResolutionUnit tag of 2 (inch) or 3 (centimeter):
     `PX` is `25400 / (n / d)` or `10000 / (n / d)`, with unit
     `micrometer`, if that is less than 25.4. `PY` likewise from
     YResolution. (The ResolutionUnit default is not used, and neither is a
     pixel of 25.4 µm or more, 1000 dpi or less: a resolution of 72, 96 or
     300 dpi is the one photo and document software writes, not a pixel
     size.)

  Otherwise `PX`, `PY`, `PZ` are 1 with no unit. `t` and `c` have no unit.
- **Position.** The image has a translation ([conventions §5](../conventions.md#5-units)), the same at every level,
  when `x` and `y` have units and either of these gives one:

  | from | when | translation |
  |---|---|---|
  | the OME-XML's `Plane` | it has `PositionX` and `PositionY` with units that are lengths ([conventions §5](../conventions.md#5-units)) | their values converted to the units of `x` and `y` are the center of the image |
  | the Aperio description, without OME-XML | it has fields `Left` and `Top` (millimeters, the scanned area's top-left on the slide) whose values are present decimals or zero | `x = Left × 1000`, `y = Top × 1000` (micrometers) |
- **Name:** the OME `Image` name, if present and not empty.

**omero.** When the channel count is at most 64 and every channel has a window
(below), `M` has `"omero"`, an [`Omero`](../conventions.md#type-omero), next to
`multiscales`, with one [`TiffOmeroChannel`](#type-tiffomerochannel) per channel (one when
there is no `c` axis). Otherwise `M` has no `"omero"` member: with more channels, so that
the root does not grow with the channel count; with a channel of a `float` data type
([§4.3](#43-data-types-and-codecs)) whose window is not sampled, because a channel's
`window` is required and a float type has no range to give it. Let channel `c`'s **name**
be the `Name` of the `c`-th `Channel` ([§3](#3-ome-xml)) when present, not empty and at
most 256 bytes.

<a id="type-tiffomerochannel"></a>**`TiffOmeroChannel`**, an
[`OmeroChannel`](../conventions.md#type-omerochannel):

| member | type | presence | value |
|---|---|---|---|
| `label` | string | required | with `spp > 1` (the channels are the samples, `spp` of them), sample `s`'s label (below), following channel 0's name and a space, when it has one; with `spp = 1`, channel `c`'s name, else `C<c>` |
| `color` | string | required | with `spp > 1`, sample `s`'s color (below); with `spp = 1`, the `c`-th `Channel`'s `Color` when that is an integer from −2^31 to 2^31 − 1 (an optional `+` or `-` sign and digits, whitespace around allowed), OME's signed 32-bit `RRGGBBAA`: its top 24 bits as six uppercase hex digits `RRGGBB`. Otherwise `FFFFFF` |
| `active` | boolean | required | `true` |
| `window` | [`Window`](../conventions.md#type-window) | required | the channel's **window** (below) |

With `spp > 1`, the samples' labels and colors:

| samples | sample `s` | label | color |
|---|---|---|---|
| decode to RGB (PhotometricInterpretation 2, or 6 with Compression 7, whose stream the codec decodes to RGB) | 0, 1 and 2 | `R`, `G` and `B` | `FF0000`, `00FF00`, `0000FF` |
| decode to RGB | every other | `S<s>` | `FFFFFF` |
| do not decode to RGB | every | `S<s>` | `FFFFFF` |

Every channel has `"active": true` and a **window**. A TIFF records
no display range (SMinSampleValue and SMaxSampleValue, when present,
are the data's extremes, not a display setting), so the window is
sampled from the data, as refkv's demo samples a contrast range, when
the samples do not decode to RGB (above) and Compression is 1, 5, 8 or
32946, whose chunks refkv decodes (LZW as [§4.3](#43-data-types-and-codecs) says, into
the chunk's size, and Predictor 2's differences summed back as `imagecodecs_delta` does; it
does not decode JPEG, JPEG 2000 or zstd):
1. Channel `c`'s **sampled chunk** is the chunk of the last level (the
   coarsest) with coords: along `t`, 0; along `c`, the chunk holding
   channel `c` (`c`, or 0 when interleaved); along `z`, `y` and `x`, the
   chunk holding index `floor(n / 2)`, `n` the axis' length. A chunk
   that is absent, or whose bytes, encoded or decoded, are more than
   2^26, is not sampled.
2. Its **values** are the chunk's bytes decoded by the codecs, which
   MUST give exactly the chunk's size (else the chunk is not sampled),
   as values of the data type: channel `c`'s (of each pixel's `spp`
   values, the `c`-th, when interleaved), at the rows and columns inside
   the array (an edge tile's padding past the image, and a padded last strip's rows, are left out),
   leaving out those equal to the fill value 0 (in a coarse level they
   are mostly the area outside the acquired region) and, of a float
   type, those that are not finite.
3. Of the `m` values in ascending order, `start` is the one at index
   `floor(m × 5 / 100)` and `end` the one at
   `min(m − 1, floor(m × 999 / 1000))`, the 5th and 99.9th percentiles;
   `min` and `max` are the type's least and greatest values for `uint`
   and `int`, and the least and greatest of the values for `float`.
   When `m` is 0 or `start` equals `end`, there is no sampled window.

A channel without a sampled window (RGB samples, which a window per
sample would tint; another compression; no sampled chunk or window)
has, for `uint` and `int`, the window of the type's whole range,
`{"min": lo, "max": hi, "start": lo, "end": hi}` with `lo` and `hi` the
type's least and greatest values (0 and 255 for `uint8`, −32768 and
32767 for `int16`), and for `float` none. An integer type's values are
JSON integers; a float type's are JSON numbers, a `float32` value
converted exactly to a double.

The members are written in the order label, color, active, window, and
the window's in the order min, max, start, end.

**Chunks:** for each plane (t, c, z) of a level, its IFD's tiles (or strips) are numbered
`k = s × T + j`, where `T` is the number of tiles (strips) per sample plane
(`ceil(H/TileLength) × ceil(W/TileWidth)`, or `ceil(H / R)` strips, [§4.5](#45-strips)),
`j` runs row-major over the tile grid (down the strips), and `s` is the sample when `spp > 1`
and planar (else 0). Both TileOffsets and TileByteCounts (StripOffsets and StripByteCounts)
MUST have `T` times the number of sample planes values. The chunk with coords `t`, `c` (the
plane's channel, or the sample `s`, or 0 when interleaved), `z` (each only when present),
then the tile row and column (the strip index and 0), is tile (strip) `k`. It is present when
its byte count is more than 0, and absent (the fill value) otherwise. Its bytes are the
tile's or strip's, encoded as the codecs say (a short last strip's, with its padding,
[§4.5](#45-strips)); for JPEG, a JPEG tile is made a complete
stream with the IFD's JPEGTables and an explicit color transform for its
PhotometricInterpretation (2: RGB as stored; 6: YCbCr). For JPEG,
JPEGTables, when present, MUST be at least 4 bytes long, start with
`FF D8` and end with `FF D9`, and every present tile or strip MUST be more than 2
bytes long. Its reference is a [`TiffChunk`](#type-tiffchunk) ([§7.3](#73-chunks)).

### 4.5 Strips

An IFD in strips ([§2](#2-the-source)) that becomes a plane or a level has its **rows per
strip** `R = min(RowsPerStrip, ImageLength)`, and `ceil(ImageLength / R)` strips per sample
plane, the last holding the remaining rows. A level in strips has chunks of `R` rows by the
level's width ([§4.4](#44-arrays)), one strip each.

When its last strip is **short** (the level has more than one strip and ImageLength is not a
multiple of `R`), the last strip's chunk is **padded** to `R` rows, so that every chunk
decodes to the chunk shape (a Zarr reader then needs no irregular chunk grid): the strip's
bytes are followed by the bytes that make the rows missing zero, which depend on the
compression (`rows` being the strip's rows and `B` the bytes of one row of the strip:
ImageWidth × BitsPerSample / 8, times SamplesPerPixel when the samples are interleaved):

| Compression | padding |
|---|---|
| **1 (uncompressed)** | `R × B − n` zero bytes, `n` the strip's byte count: `(R − rows) × B` when the strip holds its rows, and none when it holds `R × B` bytes or more (as a strip written with all the chunk's rows does; like any uncompressed tile or strip, one of more bytes than its chunk does not decode) |
| **5 (LZW)** | the strip's stream is read until its output reaches the strip's size (`rows × B` bytes; a stream that does not reach it, or whose code crosses it, rejects the input), and what follows (its end-of-information code, or the malformed final code of [§4.3](#43-data-types-and-codecs)) is replaced by a **tail** that continues the stream: a Clear code at the width the decoder has there, then the `(R − rows) × B` zeros as the codes 0, 258, 259, ... (each code one zero longer than the last, the entry the decoder defines on reading the previous one) and one more code for the remainder shorter than the next entry, then the end-of-information code, every code at the width the decoder has when it reads it. The chunk's bytes are the strip's bytes before the byte holding the cut, then the tail, which starts with that byte's bits before the cut |
| **another compression** (JPEG, Deflate, zstd, JPEG 2000) | cannot be padded: the input is rejected |

With Predictor 2, the padding is the same: each padded row is differences of 0, which sum back
to zeros, since the differencing starts again at each row. (A short strip of Deflate or zstd cannot be extended without decompressing and compressing it
again, and a JPEG or JPEG 2000 stream holds its own height, so these have no padding that
references the strip's bytes.) A strip padded with more than 2^22 (4194304) zero bytes
rejects the input, so that no archive holds more zeros than that, and an LZW tail stays within
one code table; so does a short last LZW strip whose tail would read more than 2^24
(16777216) of its bytes (its byte count, read up to the bound of [§7.1](#71-reading)), and so do
short last LZW strips whose distinct ranges read for their tails hold more bytes, together, than
the file (which strips that do not overlap never do).

## 5. Source metadata

The root's source metadata `S` is a [`TiffRoot`](#type-tiffroot):

<a id="type-tiffroot"></a>**`TiffRoot`**, the root's `S` (a QPTIFF's too,
[qptiff.md §5](qptiff.md#5-source-metadata)):

| member | type | presence | value |
|---|---|---|---|
| `byte_order` | string | required | `"little"` or `"big"`: the file's byte order |
| `bigtiff` | boolean | required | `true` for a BigTIFF (magic 43), `false` for a TIFF (magic 42) |

Everything else the file holds is on
`refkv_source`, which is the file's **IR mirror**
([conventions §11.3](../conventions.md#113-the-ir-mirror)).
This section is the TIFF **source model**: the elements of a TIFF's IR. Nothing is
left out, layout and dead space included (they are elements too, the dead
space as gaps).

### 5.1 Elements

Paths are relative to the IR's root, whose path is `""`; `<i>`, `<j>`,
`<tag>` are decimal name indexes ([conventions §8.1](../conventions.md#81-elements)). Types are those of [conventions §8.6](../conventions.md#86-types), with `E` the
file's byte order (`<` little, `>` big), `W` the offset type (`u4`, or `u8`
for BigTIFF) and `C` the entry-count type (`u2`, or `u8`).

| path | kind | type | extent: what |
|---|---|---|---|
| `""` | struct | | the root: the whole file, `(0, size)` |
| `header` | struct | | the header (8 or 16 bytes) |
| `header/byte_order` | value | `ascii[2]` | `II` or `MM` |
| `header/magic` | value | `Eu2` | 42 or 43 |
| `header/offset_size`, `header/reserved` | value | `Eu2` | BigTIFF only |
| `header/first_ifd` | value | `EW` | the first IFD's offset |
| `ifds/<i>` | struct | | main-chain IFD `i` ([§2](#2-the-source)): its entry count, entries and next-IFD offset |
| `…/entry_count` | value | `EC` | the entry count `n` |
| `…/tags/<tag>` | struct | | the first entry of tag `<tag>` (12 or 20 bytes); a later one of the same tag is `…/tags/<tag>~<k>`, `k` = 1, 2, … |
| `…/tags/<tag>/entry` | value | record | the entry's tag, field type and count (`{tag:Eu2,type:Eu2,count:EW}`), with `offset:EW` when the value is out of line, or `field:bytes[4\|8]` when the field type is unknown |
| `…/tags/<tag>/value` | value | by field type | the value: in the entry's field (the bytes it uses; the rest of the field is a gap), or at its offset when it lies within the file. Its type is `ascii[n]` (ASCII), `Eu1[n]`, `Eu2[n]`, `Eu4[n]`, `Eu8[n]`, `Ei1[n]` … (integers), `Eu4[n,2]`, `Ei4[n,2]` (rationals), `Ef4[n]`, `Ef8[n]`, or `bytes[n]` (UNDEFINED) |
| `…/next_ifd` | value | `EW` | the next-IFD offset |
| `…/tiles`, `…/strips` | struct | | an IFD's tiles or strips (by its TileOffsets and TileByteCounts, or StripOffsets and StripByteCounts), when it has both tables |
| `…/tiles/<j>`, `…/strips/<j>` | data | | tile or strip `j`, when its byte count is not 0 and it lies within the file; its form's geometry is `{"shape": [rows, columns], "samples": s, "bits": b}`, its codec `{"compression": c}`, and its recipe the bytes (`[["src", 0, null]]`), or, when the IFD's JPEG prefix `P` (a [`TiffJpegPrefix`](#type-tiffjpegprefix): for JPEG (7), the Adobe marker for 3 samples, then the JPEGTables, when the IFD has them, without their SOI and EOI; empty for every other compression) is not empty and the tile or strip is more than 2 bytes, the first 2 bytes, `P` (shared data source `x`, [conventions §8.2](../conventions.md#82-the-table)), and the rest: `[["src", 0, 2], ["shared", x], ["src", 2, null]]` |
| `…/subifds/<j>` | struct | | SubIFD `j` of the IFD (to depth 4), as an IFD |
| `…/exif`, `…/gps`, `…/interoperability`, `…/ifd_<tag>` | struct | | the IFD a pointer tag ([§5.2](#52-which-ifds)) leads to (`…/<name>/<j>` when the tag has several values), as an IFD |
| `ome/planes/<p>` | alias | | plane `p` of the OME-XML ([§4.1](#41-planes)): the main-chain IFD its `TiffData` names |
| `gaps/<offset>` | gap | | bytes no other element claims (padding, unused space, image data of tables that do not lie within the file) |

An IFD whose tile (or strip) tables are at the offsets of an earlier IFD's,
with the same counts and codec, names that IFD's `tiles` by an alias. An
element whose bytes an earlier element claims (an IFD read twice through a
pointer, a tile at another's offset) is an alias of that element.

### 5.2 Which IFDs

The IFDs of [§2](#2-the-source), at depth 0 (`ifds/<i>`) and their SubIFDs (to depth 4); then
the IFDs that these pointer tags lead to, each named in [§5.1](#51-elements) as shown (a name
without `<j>` becomes `…/<name>/<j>` when the tag has several values):

| tag | name | followed when | element |
|---|---|---|---|
| 330 | SubIFDs | its field type is an unsigned integer type | `…/subifds/<j>` |
| 34665 | ExifIFD | its field type is an unsigned integer type | `…/exif` |
| 34853 | GPSIFD | its field type is an unsigned integer type | `…/gps` |
| 40965 | InteroperabilityIFD | its field type is an unsigned integer type | `…/interoperability` |
| 400 | GlobalParametersIFD | its field type is LONG | `…/ifd_400` |
| any other | | its field type is IFD (13) or IFD8 (18), and it is neither a tag the layout uses nor a layout table | `…/ifd_<tag>` |

The IFDs of [§2](#2-the-source) are visited in order (each
IFD, then its SubIFDs, depth first), and each pointer value in order; an IFD
that a value leads to is read when it lies within the file, has at least one
entry and does not overlap an IFD already read, at depth at most 4, while at
most 100000 IFDs are read and 10000 offsets tried. An IFD already read is an
alias.

### 5.3 Values

An entry's value is an element when it lies within the file. Its bytes are
in the source; the view shows it when it is a number, a record, a GUID or
text (ASCII) of at most 1024 bytes ([conventions §8.7](../conventions.md#87-the-view)), and a producer reads
every such value.

### 5.4 Equivalence

Hierarchies of one file are compared entry for entry, `refkv_source` included
([conventions §11.4](../conventions.md#114-equivalence)).

## 6. Example

A one-plane RGB OME-TIFF at `https://example.org/image.ome.tif`, with zstd
tiles and one SubIFD level (the OME-XML is shortened here). The root's
property:

```json
{
  "profile": "tiff",
  "version": 0,
  "source": {
    "url": "https://example.org/image.ome.tif"
  },
  "tiff": {
    "byte_order": "little",
    "bigtiff": false
  }
}
```

`refkv_source` is the IR mirror ([§5](#5-source-metadata)). For the 5,319-byte JPEG-tiled file
`jpeg_gray.tif` of refkv's test fixtures, its property is:

```json
{
 "tiff": {
  "ir": {
   "version": 2,
   "size": 5319,
   "elements": 73,
   "rows": 14,
   "names": ["", "byte_order", "entry", "entry_count", "first_ifd", "gaps/", "header", "ifds/", "magic",
             "next_ifd", "tags/", "tiles", "value"],
   "types": ["", "<u2", "<u2[12]", "<u2[1]", "<u4", "<u4[1,2]", "<u4[12]", "<u4[1]", "ascii[12]",
             "ascii[21]", "ascii[2]", "{tag:<u2,type:<u2,count:<u4,offset:<u4}", "{tag:<u2,type:<u2,count:<u4}"],
   "forms": ["{\"geometry\":{\"shape\":[32,32],\"samples\":1,\"bits\":8},\"codec\":{\"compression\":7},\"recipe\":[[\"src\",0,null]]}"]
  }
 }
}
```

Its 73 elements are 14 rows: IFD 0's 15 tags fold into one column run, its
12 tiles into another, whose starts and lengths are stored as differences
([conventions §8.8](../conventions.md#88-canonical-form): encoding 2, which reads them from TileOffsets and
TileByteCounts, needs 16 tiles or more), and the 7 gaps into a third, whose
name indexes are their starts (encoding 3). The names and types are sorted,
so `names[0]` is `""`, the root's name; the root is row 0, with the extent
`(0, 5319)`. The view, `refkv_source/tree`, is the root's document, and
begins:

```json
{
 "tiff": {
  "header": {"byte_order": "II", "magic": 42, "first_ifd": 8},
  "ifds/0": {
   "entry_count": 15,
   "tags/256": {"entry": {"tag": 256, "type": 4, "count": 1}, "value": [128]},
   "tags/257": {"entry": {"tag": 257, "type": 4, "count": 1}, "value": [96]},
   "tags/305": {"entry": {"tag": 305, "type": 2, "count": 12, "offset": 246}, "value": "tifffile.py"},
   "tags/324": {"entry": {"tag": 324, "type": 4, "count": 12, "offset": 258},
                "value": [336, 752, 1166, 1581, 1997, 2413, 2827, 3242, 3658, 4074, 4488, 4903]},
   "next_ifd": 0,
   "tiles": {}
  }
 }
}
```

(abridged: the other tags are like 256's; a text value is shown up to its first
NUL, so tag 305's 12 bytes are `tifffile.py`).

# Part 2. The profile

The TIFF profile of [spec/zarrify.md](../zarrify.md) (revision 16) is [§7](#7-tiff-profile) of this document. What every profile's Part 2 shares is in
[spec/zarrify.md, Profiles](../zarrify.md#profiles).

## 7. TIFF profile

**Signature** ([spec/zarrify.md §1.2](../zarrify.md#12-input), tested at the precedence of
[profiles/tiff.json](profiles/tiff.json)): bytes 0–3 of `H` are `49 49 2A 00`,
`4D 4D 00 2A`, `49 49 2B 00` or `4D 4D 00 2B`. A file this signature chose is read by the NDPI
profile instead when it passes that profile's detection test ([ndpi.md §7](ndpi.md#7-ndpi-profile)), and by the
QPTIFF profile when it passes that one's ([qptiff.md §7](qptiff.md#7-qptiff-profile)); a VSI file, whose signature
is tested first, is read by the VSI profile ([vsi.md §9](vsi.md#9-vsi-profile)).

### 7.1 Reading

The input is a TIFF (magic 42) or BigTIFF (magic 43, offset size 8 and
reserved word 0), in either byte order. An NDPI file is read with the changes given in the NDPI profile ([ndpi.md §7](ndpi.md#7-ndpi-profile),
[ndpi.md](ndpi.md#part-2-the-profile)).

- **IFDs:** the zarrifier reads the chain of image file directories (IFDs)
  from the header (the **main chain**, which ends at a next-IFD offset of 0;
  it MUST have at least one IFD).
  Then, for every main-chain IFD in order, it reads the IFDs whose offsets
  that IFD's `SubIFDs` tag (330) lists, in order, and after them, each of
  their SubIFDs in turn (depth first), down to depth 4: the SubIFDs of a
  SubIFD at depth 4 (main-chain IFDs being at depth 0) are not read. A
  SubIFD's next-IFD offset field MUST lie within the file but its value is
  not used or checked. (A main-chain next-IFD offset is an offset, so it
  MUST be at most 2^53 − 1.) A `SubIFDs` tag with no values means no
  SubIFDs.
- **Limits:** every IFD offset read (main chain or SubIFD) MUST be at least
  8 (16 for BigTIFF) and MUST differ from every other IFD offset read, so a
  cycle, or a SubIFD shared by two IFDs, rejects the input. At most 100000
  IFDs are read; reading the 100001st rejects the input. (The IFDs that
  other pointer tags lead to are read for the source metadata only, and
  never reject the input:
  [the convention §5](#5-source-metadata).)
- **Tags:** the tags that the convention's layout uses (the table of
  [the convention §2](#2-the-source)) are
  checked below. Every other tag is read only for the source metadata
  ([the convention §5](#5-source-metadata)),
  which never rejects the input: its field type and value are not checked.
  Of duplicate tags in an IFD, the first is used and the others are not
  checked (the source metadata keeps them).

**Checks on every IFD read.** For every tag of the convention's table
present in an IFD read (whether or not that IFD is used), without reading
its value:

- **Field type:** each tag's field type is one of these. Any other type rejects.

  | tag | field types |
  |---|---|
  | ImageDescription | may be any field type that TIFF 6.0 or BigTIFF defines (1–12, 16–18), or 13 (IFD) |
  | JPEGTables | MUST be BYTE (1) or UNDEFINED (7); its value is its bytes |
  | XResolution, YResolution | MUST be RATIONAL (5) |
  | every other tag | MUST be an unsigned integer type: BYTE (1), SHORT (3), LONG (4), IFD (13), LONG8 (16) or IFD8 (18), in either TIFF variant |
- **Count:** a scalar tag MUST have at least one value. An array tag may
  have any count; BitsPerSample and SampleFormat with no values reject
  when they are used.
- **Place:** its value MUST lie within the file (a value of more than 4
  bytes, 8 for BigTIFF: its offset plus its size is at most the file's
  size).

**Values read.** A tag's values are read only where the layout uses them
(an IFD's tables are often shared by many IFDs, and most IFDs are not
used): of a scalar tag, its first value (the only one used); of an array
tag, every value; of ImageDescription and JPEGTables, their bytes. They are
read for:

| IFDs | values read |
|---|---|
| each IFD whose SubIFDs are read (above) | its SubIFDs tag |
| IFD 0 | its ImageDescription, when its field type is ASCII (2) |
| IFD 0 and every IFD that becomes a plane or a level, or is a candidate of the level scan ([the convention §4.1, §4.2](#41-planes)) | every other tag of the table but the tile and strip tables (TileOffsets, TileByteCounts, StripOffsets, StripByteCounts), which are read only for planes and levels |
| without OME-XML and SubIFDs, when IFD 0 is in strips, each later main-chain IFD that is not a level, has BitsPerSample, and whose ImageWidth and ImageLength (in their value fields) are IFD 0's ([the convention §4.2](#42-pyramid-levels)), in chain order, until one has IFD 0's format | every tag of the table but the tile and strip tables, for its format |
| every IFD of a level in LZW strips whose last strip is short | the bytes of its last strip (of each sample plane), for the tail of [the convention §4.5](#45-strips): at most `3 × rows × B + 64` of them, which hold the strip's output for any stream that does not spend more than a Clear code and a literal on each byte it decodes; a stream that does not reach the strip's size within them is read as one that ends before it |

Each value read of an integer type MUST be at most 2^53 − 1 (only a LONG8
or IFD8 value can exceed it). The values of the other IFDs are not read:
for an IFD that is not used, only the field type, count and place of each
tag of the table are checked.

**Sampled chunks.** After the IFDs, the zarrifier reads the bytes of
each sampled chunk of the `omero` windows ([the convention §4.4](#44-arrays)),
the source ranges its reference names, and decodes them. These reads and
their decoding never reject the input: a chunk that does not decode is not
sampled.

### 7.2 Rejection

The input is rejected when a check of [§7.1](#71-reading) fails, and when the convention
gives it no layout: wherever the convention says that the input is
rejected, or that something MUST hold and it does not
([the convention §2–§4](#2-the-source)).

### 7.3 Chunks

Every chunk that the convention says is present
([the convention §4.4](#44-arrays)) is the
entry `<level>/c/<coords>`. Tile (strip) `k`, with TileByteCounts (StripByteCounts) `n > 0`,
has the single range `(0, TileOffsets[k], n)` (`StripOffsets[k]`; for JPEG, and for a short
last strip, see below). The references
are listed (for [§1.2](../zarrify.md#12-input)'s order of first use) level by level, within a level
by `t`, then `c`, then `z`, and within a plane by `k`. Besides these and the
root's and the arrays' `zarr.json`, the archive's entries are those of the
source metadata node `refkv_source`, the file's IR mirror
([the convention §5](#5-source-metadata),
[conventions §8](../conventions.md#8-the-ir-mirror)): its table's
arrays, whose chunks hold copied bytes, and its view's groups.

**JPEG tiles.** A JPEG-in-TIFF tile is a JPEG stream that may omit its
tables (which are in the IFD's JPEGTables) and the color transform (which
is given by PhotometricInterpretation). Each tile's reference makes it the
complete stream that the convention's chunk holds by inserting, after the
tile's first 2 bytes (its SOI marker), the shared byte string `P`, a
[`TiffJpegPrefix`](#type-tiffjpegprefix):

<a id="type-tiffjpegprefix"></a>**`TiffJpegPrefix`**, `P`, the bytes of a data source
([§1.2](../zarrify.md#12-input)), in this order (empty but for JPEG):

| part | encoding | value |
|---|---|---|
| the Adobe marker | bytes | present for 3 samples: the 16 bytes `FF EE 00 0E 41 64 6F 62 65 00 64 00 00 00 00 T`, with transform `T` = 0 for PhotometricInterpretation 2 (the samples are RGB as stored) and 1 for 6 (YCbCr) |
| the tables | bytes | present if the IFD has JPEGTables: its bytes without the first 2 and the last 2. JPEGTables MUST then be at least 4 bytes long, start with `FF D8` and end with `FF D9` |

`n` MUST be more than 2. When `P` is empty (one sample, no JPEGTables) the
tile has the single range above; otherwise it has three ranges, the middle one `R`, a
range of the data source ([§1.2](../zarrify.md#12-input)) that holds `P`:

<a id="type-tiffchunk"></a>**`TiffChunk`**, the reference of tile (or strip) `k`, with `o =
TileOffsets[k]` and `n = TileByteCounts[k] > 0` (`StripOffsets[k]`, `StripByteCounts[k]`):

| part | range | presence | value |
|---|---|---|---|
| the tile | source range `(0, o, n)` | optional | when `P` is empty (every compression but JPEG, and JPEG with one sample and no JPEGTables) and the tile is not a short last strip of LZW: the whole tile or strip |
| the zero rows | `Z`, source range `(i, 0, z)` | optional | for an uncompressed short last strip that lacks `z > 0` bytes of the chunk's size ([the convention §4.5](#45-strips)), after the strip: the first `z` bytes of the output's zero data source `i` ([§1.2](../zarrify.md#12-input)) |
| the strip before the cut | source range `(0, o, p)` | optional | for an LZW short last strip: `p` the bytes before the cut's byte |
| the tail | `T`, a literal range | optional | for an LZW short last strip: the literal tail of [the convention §4.5](#45-strips) |
| the tile's SOI | source range `(0, o, 2)` | optional | when `P` is not empty: the tile's first 2 bytes |
| `P` | `R`, source range `(i, 0, len(P))` | optional | when `P` is not empty: the whole data source `i` that holds `P`, a [`TiffJpegPrefix`](#type-tiffjpegprefix) |
| the rest of the tile | source range `(0, o + 2, n − 2)` | optional | when `P` is not empty: the tile's bytes after its first 2 |

A short last strip thus has, when uncompressed and lacking bytes, the ranges `[(0, o, n), Z]`, and when LZW,
the ranges `[(0, o, p), T]` (a JPEG strip is never short: [the convention §4.5](#45-strips)
rejects it). Of a JPEG tile, every byte of the tile is
referenced, none replaced, so the chunk holds the tile whatever its first 2
bytes are. `P` depends only on the IFD, so every tile of an IFD has the
same `P`, and IFDs with the same bytes (as several levels of a slide often
have) share one data source.
