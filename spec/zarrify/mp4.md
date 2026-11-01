# MP4

The Zarr layout of an MP4 or QuickTime file (`.mp4`, `.m4a`, `.m4v`,
`.mov`: ISO/IEC 14496-12, 14496-14 and Apple's QuickTime file format), and
the translation of its boxes and tracks into JSON. What all of refkv's
conventions share is in [spec/conventions.md](../conventions.md), cited here
as "conventions §n". How refkv produces this layout as a reference store,
and which files it accepts, is the MP4 profile, [Part 2](#part-2-the-profile)
below.

Convention version: 0 (until release, [conventions §1](../conventions.md#1-conventions)) · UUID: `bbe3f6b9-488d-4d8d-85ce-60a1649dd27e` ·
Schema: [schema.json](mp4/schema.json)

Changed 2026-11-01, with no change to any output: the checks say that a zarrifier MUST reject
an input that fails one, in RFC 2119's words; and the registry lists the file names this
document does, `.mp4`, `.m4a`, `.m4v` and `.mov`, which the profile reads alike, as its
signature chooses it by the file's first bytes.

Changed 2026-10-29: a version 0 `ctts` box's offsets are signed, as version 1's are, where they
were unsigned: Apple's encoder writes negative ones, which made each picture they belong to seem
presented after every later sync sample, and the track's video was not zarrified
([§2](#2-boxes), [§3](#3-tracks-and-samples)).

Changed 2026-10-28: HEVC video tracks (`hvc1` and `hev1` sample entries) are zarrified as H.264
tracks are: 4:2:0 of 8 bits (Main) or 10 (Main 10, an iPhone's HDR video), the planes `uint8` or
`uint16`, the `refkv.stream` format `"hevc"` with the `hvcC` box as its setup
([§4.1](#41-video-tracks), [§8.2](#82-tracks)); their IRAP pictures are the sync samples, and
their RASL and RADL pictures leading pictures, under the rules H.264's open groups follow
([§8.3](#83-chunks)). A video chunk holds at most 32 MiB of luma counted in bytes, so half as
many 10-bit pictures as 8-bit ones; a video track's source metadata has its `bit_depth`
([§5.2](#52-the-tracks)).

Changed 2026-10-26: a duration or an edit's `media_time` past 2^53 − 1 in magnitude is the
string of its digits, as [conventions §6](../conventions.md#6-source-metadata-as-json) writes an
integer, where it was a JSON number ([§5](#5-source-metadata)); a track whose ID an earlier
zarrified track has is not zarrified, its reason saying so, where both were written to one group
([§8.2](#82-tracks)). The layout is tabulated, its entries in [Layout](#layout) and its types in
type tables ([conventions §11.5](../conventions.md#115-layout-tables)); the summary is a table
too. And the text says what refkv already wrote: a four-character code is a plain string of its
bytes' code points ([§5](#5-source-metadata)), and a chunk of `time` is not a `refkv.stream`
chunk ([§8.3](#83-chunks)).

Changed 2026-10-25: a track whose sync samples are followed in decode order by pictures
presented before them (leading pictures: a camera's B-frames after each IDR picture, or an open
group's) is zarrified, not refused, while each leading picture is presented after the sync
sample before; a chunk that holds a leading picture of the sync sample it would decode from
decodes from the sync sample before that ([§8.2](#82-tracks), [§8.3](#83-chunks)). A track whose
sync sample table does not increase, or names a sample past the last, is refused.

Changed 2026-10-24: a video chunk holds whole groups of pictures, from a sync sample, in place
of `F` pictures from a multiple of `F`; a group longer than `F` pictures is split. Where the
chunks' lengths differ, the planes' chunk grid is the zarr-extensions `rectilinear` grid
([§4.1](#41-video-tracks), [§8.3](#83-chunks)).

Added 2026-10-23.

This document has two parts ([conventions §11.1](../conventions.md#111-parts)).

An MP4 file is not an image. The hierarchy holds each track it zarrifies
as a group: an H.264 or HEVC video track as the Y, U and V planes of its pictures
and their presentation times, an AAC or MP3 audio track as its samples. The
pictures and samples stay in the file as coded frames; the arrays' codec,
`refkv.stream` ([conventions §10](../conventions.md#10-coded-streams)),
decodes them when a chunk is read.

# Part 1. The convention

## Layout

The hierarchy's entries ([conventions §11.5](../conventions.md#115-layout-tables)):

| prefix | type | structure | description |
|---|---|---|---|
| `zarr.json` | group | [`GroupDocument`](../conventions.md#type-groupdocument), its attributes a [`Declaration`](../conventions.md#type-declaration) and nothing else | the root ([§4](#4-the-layout)): no `ome` member |
| `zarr.json#/attributes/zarr_conventions` | attribute | array of [`ConventionMetadata`](../conventions.md#type-conventionmetadata) | the CMO of [§1](#1-declaration) |
| `zarr.json#/attributes/refkv_zarrified` | attribute | [`RootProperty`](../conventions.md#type-rootproperty) | `profile` `"mp4"`, `version` `0`, `source.url` the file's URL |
| `zarr.json#/attributes/refkv_zarrified/mp4` | attribute | [`Mp4Root`](#type-mp4root) | the root's source metadata ([§5.1](#51-the-root)) |
| `tracks/zarr.json` | group | [`GroupDocument`](../conventions.md#type-groupdocument), attributes `{}` | the group of the tracks ([§4](#4-the-layout)) |
| `tracks/<id>/zarr.json` | group | [`GroupDocument`](../conventions.md#type-groupdocument), its attributes a [`Declaration`](../conventions.md#type-declaration) and nothing else | a track the profile zarrifies ([§4](#4-the-layout), [§8.2](#82-tracks)) |
| `tracks/<id>/zarr.json#/attributes/zarr_conventions` | attribute | array of [`ConventionMetadata`](../conventions.md#type-conventionmetadata) | the CMO of [§1](#1-declaration) |
| `tracks/<id>/zarr.json#/attributes/refkv_zarrified` | attribute | [`NodeProperty`](../conventions.md#type-nodeproperty) | the track's property |
| `tracks/<id>/zarr.json#/attributes/refkv_zarrified/mp4` | attribute | [`Mp4VideoTrack`](#type-mp4videotrack) or [`Mp4AudioTrack`](#type-mp4audiotrack) | the track's source metadata ([§5.2](#52-the-tracks)) |
| `tracks/<id>/<plane>/zarr.json` | array | [`Mp4PlaneArray`](#type-mp4planearray) | a video track's plane ([§4.1](#41-video-tracks)) |
| `tracks/<id>/<plane>/c/<q>/0/0` | chunk | [`Mp4VideoChunk`](#type-mp4videochunk) | every chunk of a plane ([§8.3](#83-chunks)) |
| `tracks/<id>/time/zarr.json` | array | [`Mp4TimeArray`](#type-mp4timearray) | a video track's presentation times ([§4.1](#41-video-tracks)) |
| `tracks/<id>/time/c/<k>` | chunk | [`Mp4TimeChunk`](#type-mp4timechunk) | every chunk of `time`, a bytes entry ([§8.3](#83-chunks)) |
| `tracks/<id>/samples/zarr.json` | array | [`Mp4AudioArray`](#type-mp4audioarray) | an audio track's samples ([§4.2](#42-audio-tracks)) |
| `tracks/<id>/samples/c/<q>/0` | chunk | [`Mp4AudioChunk`](#type-mp4audiochunk) | every chunk of the samples ([§8.3](#83-chunks)) |
| `refkv_source/**` | subtree | [the mirror's entries](../conventions.md#table-mirror) | the IR mirror ([conventions §11.3](../conventions.md#113-the-ir-mirror)): its elements are [§5.3](#53-elements)'s |

| placeholder | value |
|---|---|
| `<id>` | the track's ID, its `tkhd` box's `track_id`, in decimal |
| `<plane>` | `y`, `u` or `v` |
| `<q>` | the chunk's index along `time`, in decimal, from 0 |
| `<k>` | the chunk's index along `time`, in decimal, from 0 to `ceil(N / 2^17) − 1` |

## 1. Declaration

The root and `refkv_source` declare the convention
([conventions §11.2](../conventions.md#112-declaration)), the root with its source
metadata of [§5.1](#51-the-root) as the member `"mp4"`. Its CMO is:

```json
{
  "uuid": "bbe3f6b9-488d-4d8d-85ce-60a1649dd27e",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/zarrify/mp4/schema.json",
  "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp4.md",
  "name": "refkv_zarrified",
  "description": "The Zarr layout of a MP4 source zarrified by refkv, and the source's metadata"
}
```

The root's attributes are `zarr_conventions` and `refkv_zarrified` and
nothing else: there is no `ome` member. Each track's group declares the
convention too, with the track's source metadata of [§5.2](#52-the-tracks).
The arrays have no attributes.

## 2. Boxes

An MP4 file is a sequence of **boxes**. A box is a header, a 32-bit
big-endian `size` and a 4-byte `type`, then its payload: `size` bytes in
all, header included. A `size` of 1 is followed by a 64-bit `largesize`,
the box's size, so the header is 16 bytes; a `size` of 0 makes the box run
to the end of its parent (of the file, for a top-level box). The boxes
whose payload is boxes, the **containers**, are `moov`, `trak`, `mdia`,
`minf`, `stbl`, `edts`, `dinf` and `tref`. A full box's payload starts with
a byte of version and 3 bytes of flags. All numbers are big-endian.

The convention reads these boxes. Types are those of
[conventions §8.6](../conventions.md#86-types); `n` is a table's entry
count, from the box's fields.

| box | fields | table |
|---|---|---|
| `ftyp` | `{major_brand:ascii[4],minor_version:>u4}` | `{brand:ascii[4]}[k]`, the compatible brands |
| `mvhd` | version 0: `{version:u1,flags:u1[3],creation_time:>u4,modification_time:>u4,timescale:>u4,duration:>u4,rate:>i4,volume:>i2,reserved:u1[10],matrix:>i4[9],pre_defined:u1[24],next_track_id:>u4}`; version 1: the times and duration `>u8` | |
| `tkhd` | version 0: `{version:u1,flags:u1[3],creation_time:>u4,modification_time:>u4,track_id:>u4,reserved:>u4,duration:>u4,reserved2:u1[8],layer:>i2,alternate_group:>i2,volume:>i2,reserved3:>u2,matrix:>i4[9],width:>u4,height:>u4}`; version 1: the times and duration `>u8` | |
| `mdhd` | version 0: `{version:u1,flags:u1[3],creation_time:>u4,modification_time:>u4,timescale:>u4,duration:>u4,language:>u2,pre_defined:>u2}`; version 1: the times and duration `>u8` | |
| `hdlr` | `{version:u1,flags:u1[3],pre_defined:>u4,handler_type:ascii[4],reserved:>u4[3]}`, then the name, `cstr` | |
| `elst` | `{version:u1,flags:u1[3],entry_count:>u4}` | version 0: `{segment_duration:>u4,media_time:>i4,media_rate:>i2,media_rate_fraction:>i2}[n]`; version 1: the duration and time `>u8`, `>i8` |
| `stts` | the same | `{sample_count:>u4,sample_delta:>u4}[n]` |
| `ctts` | the same | `{sample_count:>u4,sample_offset:>i4}[n]` |
| `stsc` | the same | `{first_chunk:>u4,samples_per_chunk:>u4,sample_description_index:>u4}[n]` |
| `stsz` | `{version:u1,flags:u1[3],sample_size:>u4,sample_count:>u4}` | `>u4[n]` when `sample_size` is 0, `n` the sample count |
| `stco`, `co64`, `stss` | `{version:u1,flags:u1[3],entry_count:>u4}` | `>u4[n]`, `>u8[n]`, `>u4[n]` |
| `stsd` | the same | the sample entries, boxes |

A **sample entry** of a video track (its handler is `vide`) has the fields
`{reserved:u1[6],data_reference_index:>u2,pre_defined:>u2,reserved2:>u2,pre_defined2:>u4[3],width:>u2,height:>u2,horizresolution:>u4,vertresolution:>u4,reserved3:>u4,frame_count:>u2,compressorname:bytes[32],depth:>u2,pre_defined3:>i2}`,
78 bytes; one of an audio track (`soun`) has
`{reserved:u1[6],data_reference_index:>u2,version:>u2,revision:>u2,vendor:>u4,channel_count:>u2,sample_size:>u2,compression_id:>i2,packet_size:>u2,sample_rate:>u4}`,
28 bytes, followed for a QuickTime sound description of version 1 by
`{samples_per_packet:>u4,bytes_per_packet:>u4,bytes_per_frame:>u4,bytes_per_sample:>u4}`.
After its fields, a sample entry holds boxes (`avcC`, `hvcC`, `esds`, `btrt`,
`pasp`, `colr`, and QuickTime's `wave`, which holds an `esds` box of its own).
The payload of every other box, and of a sample entry of another handler or
of a sound description of version 2, is opaque.

## 3. Tracks and samples

Each `trak` box of the `moov` box is a **track**, numbered by its order
among them from 0 (its index `T`); its ID is its `tkhd` box's `track_id`.
Its **handler** is the `handler_type` of its first `hdlr` box, the `mdia`
box's (a QuickTime `minf` box holds another, the data handler's). Its
**media timescale** and duration are its `mdhd` box's; its **edit list**
its `elst` box's entries.

A track's **samples**, numbered from 0 in decode order, are given by its
sample table (the `stbl` box): sample `j` is `size(j)` bytes, its `stsz`
box's `sample_size`, or the table's entry `j` when that is 0. The samples
are in chunks: chunk `c` (from 1) starts at entry `c − 1` of `stco` or
`co64`, and holds the `samples_per_chunk` of the `stsc` entry whose
`first_chunk` is the greatest at most `c`, one after the other. Sample `j`'s
**decode time** is the sum of the `sample_delta`s of the samples before it
(`stts` gives each run of samples its delta), and its **presentation time**
its decode time plus its `ctts` offset (0 without a `ctts` box), both in the
media timescale. The offset is signed in either version of the box, as
QuickTime's are and as Apple's encoder writes them, negative ones in a
version 0 box, where ISO/IEC 14496-12 §8.6.1.3 has version 0's unsigned.
The **sync samples** are the samples `stss` names (its entries, less 1), or
every sample when the track has no `stss` box.

## 4. The layout

The hierarchy is a group at the root, `{"zarr_format": 3, "node_type":
"group", "attributes": A}`, where `A` has `zarr_conventions` and
`refkv_zarrified` ([§1](#1-declaration)) and nothing else; a group
`tracks`, with no attributes; and, for each track the profile zarrifies
([spec/zarrify.md §8.2](#82-tracks)), a group `tracks/<id>`, `<id>`
the track's ID, which declares the convention with the track's source
metadata, and its arrays below. Every array's `zarr.json` is
[conventions §3](../conventions.md#3-arrays)'s, with the fill value 0.

### 4.1 Video tracks

An H.264 or HEVC track of `N` samples, `W × H` pixels (its sample entry's
`width` and `height`), of `D` bits a sample (its SPS's bit depth, [§8.2](#82-tracks)), holds
its pictures in **presentation order**: picture `p` is
the sample with the `p`-th smallest presentation time. Its arrays are `y`, `u` and `v`, the
planes of the pictures, and `time`, the pictures' presentation times.

**Chunks.** A chunk holds at most
`F = min(256, max(1, floor(2^25 / (W × H × B))))` pictures, `B` the bytes of a sample (1 for
`D` = 8, 2 for 10): at most 32 MiB of luma (16 pictures at 1920 × 1080 of 8 bits, 8 of 10),
since a chunk decodes whole. The
chunks are cut at the sync samples' presentation indexes: chunk `q` holds
the pictures `a_q` to `a_{q+1} − 1`, from `a_0 = 0`, where `a_{q+1}` is
`N` when `N − a_q ≤ F`; else the greatest presentation index of a sync
sample that is greater than `a_q` and at most `a_q + F`; else `a_q + F`.
So a chunk holds whole groups of pictures and the next starts with a
sync sample, but where a group longer than `F` is split. A sync sample's
leading pictures (those after it in decode order and presented before
it) are in the chunk before it. Its key is `<plane>/c/<q>/0/0`.

<a id="type-mp4planearray"></a>**`Mp4PlaneArray`**, the `zarr.json` of `y`, `u` and `v`: an
[`ArrayDocument`](../conventions.md#type-arraydocument) with these members, `L_q` the length
of chunk `q`:

| member | type | presence | value |
|---|---|---|---|
| `shape` | array of integer | required | `[N, H, W]` for `y` and `[N, ceil(H / 2), ceil(W / 2)]` for `u` and `v` |
| `data_type` | string | required | `"uint8"` when `D` is 8, `"uint16"` when it is 10 (the decoder's samples, from 0 to 2^D − 1) |
| `chunk_grid` | object | required | when every `L_q` but the last is `L_0` and the last at most `L_0`, the regular grid of chunk shape `[L_0, H, W]` (or the chroma planes' sizes); otherwise the zarr-extensions `rectilinear` grid ([chunk-grids/rectilinear](https://github.com/zarr-developers/zarr-extensions/tree/main/chunk-grids/rectilinear)), exactly `{"name": "rectilinear", "configuration": {"kind": "inline", "chunk_shapes": [T, H, W]}}` (or the chroma planes' sizes), `T` the lengths in order, each run of `r > 1` equal lengths `L` written `[L, r]`, as the CZI convention writes a clipped axis ([zarrify/czi.md §4.3](czi.md#43-image-arrays)) |
| `codecs` | array of object | required | `[{"name": "refkv.stream", "configuration": {"format": V, "setup": S, "plane": P}}]`, a [`StreamConfiguration`](../conventions.md#type-streamconfiguration), `V` `"h264"` for H.264 and `"hevc"` for HEVC, `S` the base64 of the payload of the sample entry's `avcC` box (H.264) or `hvcC` box (HEVC), and `P` the array's name |
| `dimension_names` | array of string | required | `["time", "y", "x"]` |
| `attributes` | object | required | `{}` ([§1](#1-declaration)) |

For example, groups
starting at pictures 0, 7, 9, 23 and 40 of 60, with `F = 7`, give
`T = [7, 2, [7, 4], 3, [7, 2], 6]`. zarr-python 3.4 reads such an array
once `zarr.config.set({"array.rectilinear_chunks": True})` is set.

<a id="type-mp4timearray"></a>**`Mp4TimeArray`**, the `zarr.json` of `time`, the pictures'
presentation times, in the media timescale: an
[`ArrayDocument`](../conventions.md#type-arraydocument) with these members:

| member | type | presence | value |
|---|---|---|---|
| `shape` | array of integer | required | `[N]` |
| `data_type` | string | required | `"int64"` |
| `chunk_grid` | object | required | regular, its chunk shape `[min(N, 2^17)]` |
| `codecs` | array of object | required | `[{"name": "bytes", "configuration": {"endian": "little"}}, {"name": "gzip", "configuration": {"level": 6}}]` |
| `dimension_names` | array of string | required | `["time"]` |
| `attributes` | object | required | `{}` ([§1](#1-declaration)) |

Its chunks are bytes entries (the last padded with zeros to
the chunk shape). These are the media timeline's times, as the tables give
them; the edit list, which may shift or cut them on the movie's timeline,
is recorded ([§5.2](#52-the-tracks)) and not applied.

The three planes of a chunk are the same `refkv.stream` chunk: each
decodes the same pictures and keeps its plane.

### 4.2 Audio tracks

An AAC or MP3 track of `N` samples (coded frames), each decoding to `S`
samples of each of its `C` channels (`S` is 1024 for AAC, 1152 for MP3 at
32 kHz and more and 576 below), holds its samples in one array, `samples`:

<a id="type-mp4audioarray"></a>**`Mp4AudioArray`**, the `zarr.json` of `samples`: an
[`ArrayDocument`](../conventions.md#type-arraydocument) with these members:

| member | type | presence | value |
|---|---|---|---|
| `shape` | array of integer | required | `[N × S, C]` |
| `data_type` | string | required | `"float32"` |
| `chunk_grid` | object | required | regular, its chunk shape `[256 × S, C]`: 256 frames a chunk |
| `codecs` | array of object | required | `[{"name": "refkv.stream", "configuration": {"format": F, "setup": A}}]`, a [`StreamConfiguration`](../conventions.md#type-streamconfiguration), `F` `"aac"` or `"mp3"`, and for AAC `A` the base64 of the AudioSpecificConfig of the sample entry's `esds` box (an MP3 track's configuration has no `setup`) |
| `dimension_names` | array of string | required | `["time", "channel"]` |
| `attributes` | object | required | `{}` ([§1](#1-declaration)) |

Every decoded sample is in the array, the encoder's priming and padding
included: AAC encoders start a stream with 1024 or 2112 samples of priming,
which the edit list records, and MP3 encoders with their encoder delay.
The samples of an AAC track coded with perceptual noise substitution are
those of [conventions §10](../conventions.md#10-coded-streams): in the
noise bands, a chunk's noise is its own.

## 5. Source metadata

The root's and each track group's source metadata are objects with the
members below ([`Mp4Root`](#type-mp4root), [`Mp4VideoTrack`](#type-mp4videotrack),
[`Mp4AudioTrack`](#type-mp4audiotrack)), in that order, translated by
[conventions §6](../conventions.md#6-source-metadata-as-json). Everything
the file holds is on `refkv_source`, the file's **IR mirror**
([§5.3](#53-elements)), from which the file is rebuilt byte for byte. A **four-character
code** (a brand, a handler, a sample entry's type) is written as text: a string of its 4 bytes,
each the character whose code point is its value (ISO 8859-1), whatever the bytes are, rather
than a text value of [conventions §6](../conventions.md#6-source-metadata-as-json), which would tag bytes that are not UTF-8.

### 5.1 The root

<a id="type-mp4root"></a>**`Mp4Root`**, the root's source metadata:

| member | type | presence | value |
|---|---|---|---|
| `brand` | [`Mp4Brand`](#type-mp4brand) | optional | when the file has an `ftyp` box of at most 4096 bytes (the first, [§8.1](#81-boxes)); absent otherwise |
| `timescale` | integer | required | the `mvhd` box's |
| `duration` | [`Integer`](../conventions.md#type-integer) | required | the `mvhd` box's |
| `tracks` | array of [`Mp4TrackListing`](#type-mp4tracklisting) | required | one object per track, in the order of the `trak` boxes |

<a id="type-mp4brand"></a>**`Mp4Brand`**, the `ftyp` box's brands, each brand a
four-character code:

| member | type | presence | value |
|---|---|---|---|
| `major` | string | required | `B`, its major brand |
| `minor` | integer | required | its minor version |
| `compatible` | array of string | required | its compatible brands, in order |

<a id="type-mp4tracklisting"></a>**`Mp4TrackListing`**, a track:

| member | type | presence | value |
|---|---|---|---|
| `id` | integer | required | its ID |
| `handler` | string | required | `H`, its handler |
| `codec` | string | required | `E`, its sample entry's type (`""` without one) |
| `path` | string or null | required | `P`, its group's path when the profile zarrifies it; else null |
| `reason` | string or null | required | `R`, null when the profile zarrifies it; else why not ([spec/zarrify.md §8.2](#82-tracks)) |

### 5.2 The tracks

A track's group has, as its source metadata, an [`Mp4VideoTrack`](#type-mp4videotrack) for a
video track and an [`Mp4AudioTrack`](#type-mp4audiotrack) for an audio track. Their members
are the same up to `frames` and at the end, `chunk_frames`.

<a id="type-mp4videotrack"></a>**`Mp4VideoTrack`**:

| member | type | presence | value |
|---|---|---|---|
| `id` | integer | required | its ID |
| `handler` | string | required | its handler |
| `codec` | string | required | its sample entry's type |
| `format` | string | required | `"h264"` or `"hevc"` |
| `timescale` | integer | required | the media's timescale |
| `duration` | [`Integer`](../conventions.md#type-integer) | required | the media's duration |
| `edits` | array of [`Mp4Edit`](#type-mp4edit) | required | its `elst` box's entries |
| `frames` | integer | required | its sample count `N` |
| `width` | integer | required | `W` ([§4.1](#41-video-tracks)) |
| `height` | integer | required | `H` |
| `bit_depth` | integer | required | `D`: 8 or 10 |
| `profile` | integer | required | the SPS's `profile_idc` (H.264) or `general_profile_idc` (HEVC: 1 Main, 2 Main 10) |
| `level` | integer | required | the SPS's `level_idc` (H.264) or `general_level_idc` (HEVC: 30 times the level) |
| `sync_samples` | integer | required | the count of its sync samples |
| `chunk_frames` | integer | required | the most frames a chunk holds: `F` |

<a id="type-mp4audiotrack"></a>**`Mp4AudioTrack`**:

| member | type | presence | value |
|---|---|---|---|
| `id` | integer | required | its ID |
| `handler` | string | required | its handler |
| `codec` | string | required | its sample entry's type |
| `format` | string | required | `"aac"` or `"mp3"` |
| `timescale` | integer | required | the media's timescale |
| `duration` | [`Integer`](../conventions.md#type-integer) | required | the media's duration |
| `edits` | array of [`Mp4Edit`](#type-mp4edit) | required | its `elst` box's entries |
| `frames` | integer | required | its sample count `N` |
| `sample_rate` | integer | required | its sample rate ([§8.2](#82-tracks)) |
| `channels` | integer | required | its channel count `C` |
| `samples_per_frame` | integer | required | `S` |
| `samples` | integer | required | `N × S` |
| `chunk_frames` | integer | required | the most frames a chunk holds: 256 |

<a id="type-mp4edit"></a>**`Mp4Edit`**, an `elst` box's entry:

| member | type | presence | value |
|---|---|---|---|
| `duration` | [`Integer`](../conventions.md#type-integer) | required | its segment duration |
| `media_time` | [`Integer`](../conventions.md#type-integer) | required | its media time |
| `rate` | number | required | the media rate and fraction as one number |

### 5.3 Elements

`refkv_source` is the file's **IR mirror**
([conventions §11.3](../conventions.md#113-the-ir-mirror)). Paths are relative to
the IR's root, whose path is `""`
([conventions §8.1](../conventions.md#81-elements)).

| path | kind | type | extent: what |
|---|---|---|---|
| `""` | struct | | the root: the whole file |
| `<box>` | struct | | a box: the whole box; a top-level box at the root, a child box under its parent box |
| `<box>/header` | value | `{size:>u4,type:ascii[4]}` or `{size:>u4,type:ascii[4],largesize:>u8}` | the box's header |
| `<box>/fields`, `<box>/fields_v1` | value | the box's fields ([§2](#2-boxes)) | after the header |
| `<box>/entries` | value | the box's table ([§2](#2-boxes)) | after the fields |
| `<box>/name` | value | `cstr[k]` | an `hdlr` box's name |
| `<box>/data` | value | `bytes[k]` | the payload of a box the convention does not parse, opaque |
| `<box>/rest` | value | `bytes[k]` | bytes of a box that are none of its fields, table or boxes |
| `samples/<T>/<j>` | data | | sample `j` of track `T`, `(offset, size)`; its form's geometry is `{"width", "height", "planes": 3, "data_type"}` for a video track, its data type the planes' (`"uint8"` or `"uint16"`), and `{"samples", "channels", "data_type": "float32"}` for an audio track (null for a track the profile does not zarrify), its codec `{"name": E, "format": F}` (`F` null for such a track), its recipe `[["src", 0, null]]` |
| `gaps/<offset>` | gap | | bytes no other element claims: the media data in no sample, and fewer than 8 bytes after the last box |

A box is named by its type: printable ASCII as it is but for `/`, `~`, `%`
and the space, every other byte as `%XX` (so `©nam` is `%A9nam`); a second
box of one type among its siblings, or one whose name is one of a box's own
values (`header`, `fields`, `fields_v1`, `entries`, `name`, `data`, `rest`,
`gaps`), is named as [conventions §8.1](../conventions.md#81-elements) names
a repeated source name (`trak~` with the index 1 for the second `trak`). A
top-level box named `samples` would be so too. The `mdat` box has its header
alone: its payload is the samples and gaps. The view
([conventions §8.7](../conventions.md#87-the-view)) shows every box's header,
fields and table, its `hdlr` name and its sample entries' fields, each that
is at most 1024 bytes; opaque payloads are not shown.

### 5.4 Equivalence

Hierarchies of one file are compared entry for entry, `refkv_source` included
([conventions §11.4](../conventions.md#114-equivalence)).

## 6. Example

A 3-second MP4 at `https://example.org/clip.mp4` (the fixture
`fixtures/mp4/mp4_h264_aac.mp4`) holds an H.264 track of 30 pictures of 64 ×
48, B-frames in groups of 10, and an AAC-LC mono track at 16 kHz. Its root's
`refkv_zarrified` is:

```json
{
  "profile": "mp4",
  "version": 0,
  "source": {"url": "https://example.org/clip.mp4"},
  "mp4": {
    "brand": {"major": "isom", "minor": 512, "compatible": ["isom", "iso2", "avc1", "mp41"]},
    "timescale": 16000,
    "duration": 48000,
    "tracks": [
      {"id": 1, "handler": "vide", "codec": "avc1", "path": "tracks/1", "reason": null},
      {"id": 2, "handler": "soun", "codec": "mp4a", "path": "tracks/2", "reason": null}
    ]
  }
}
```

and `tracks/1`'s `{"mp4": V}`, `V`:

```json
{"id": 1, "handler": "vide", "codec": "avc1", "format": "h264", "timescale": 10240, "duration": 30720,
 "edits": [{"duration": 48000, "media_time": 2048, "rate": 1.0}], "frames": 30, "width": 64, "height": 48,
 "bit_depth": 8, "profile": 100, "level": 10, "sync_samples": 3, "chunk_frames": 256}
```

The edit list starts the movie at media time 2048, two pictures' worth:
the B-frames' composition delay. `tracks/1/y` has the shape `[30, 48, 64]`,
the chunk shape `[30, 48, 64]` (every picture in one chunk, since 30 is at
most `F`, 256) and the codecs `[{"name": "refkv.stream", "configuration":
{"format": "h264", "setup": "AWQACv/hABhn...", "plane": "y"}}]`; its one chunk, `tracks/1/y/c/0/0/0`, is a literal header and the
30 samples' ranges, each sample's position its presentation index.
`tracks/2/samples` has the shape `[49152, 1]` (48 frames of 1024 samples),
`float32`, the codecs `[{"name": "refkv.stream", "configuration": {"format":
"aac", "setup": "FAhW5QA="}}]`.

## 7. What the layout does not express

This section is informative.

- **The movie's timeline.** The edit list maps the media timeline onto the
  movie's: it delays a track, cuts its priming, or plays part of it again.
  The layout holds the media timeline and records the edit list, so a
  reader aligning two tracks applies it.
- **Color.** The planes are the decoder's Y, U and V samples. Their color
  primaries, transfer function, matrix and range (the SPS's VUI, a `colr`
  box) are in the mirror; converting to RGB is the reader's. So is an HDR
  stream's transfer function: an iPhone's HDR video is 10-bit HEVC with
  HLG's, and its Dolby Vision metadata (a `dvcC` or `dvvC` box, and RPU NAL
  units in the samples, which an HEVC decoder ignores) is in the mirror,
  not applied: the planes are the HLG base layer's.
- **Orientation.** The `tkhd` box's matrix, which turns a phone's portrait
  video upright on the movie's display, is in the mirror, not applied: the
  planes are as coded.
- **Other tracks and codecs.** AV1, Dolby Vision's own sample entries
  (`dvh1`, `dvhe`), 10-bit or 4:2:2 H.264, 4:2:2, 4:4:4 or 12-bit HEVC,
  other audio codecs, timecode, text, chapter and metadata tracks are
  recorded in `tracks` with the reason the profile does not zarrify them,
  and kept whole in the mirror.
- **Metadata.** `udta` and `meta` boxes (titles, iTunes tags, GPS) are
  opaque in the mirror.

# Part 2. The profile

The MP4 profile of [spec/zarrify.md](../zarrify.md) is [§8](#8-mp4-profile) of this document. What every profile's Part 2 shares is in
[spec/zarrify.md, Profiles](../zarrify.md#profiles). The profile also says which
tracks are zarrified.

## 8. MP4 profile

### 8.1 Boxes

The input is an MP4 or QuickTime file, which its **signature** ([spec/zarrify.md §1.2](../zarrify.md#12-input), tested at the precedence of
[profiles/mp4.json](profiles/mp4.json), before MRC's) chose: bytes 0–3 of `H` are a
32-bit big-endian size of at least 8, or 1, and bytes 4–7 are `ftyp`, `moov`,
`mdat`, `wide`, `free` or `skip`. A zarrifier reads the
top-level boxes' headers, then the whole `moov` box (and the `ftyp` box); it
never reads the media data.

**Checks.** A zarrifier MUST reject the input unless all of these hold. They are checked
as the boxes are met, the top-level boxes first, in the file's order, then
the `moov` box's tree, then each track's samples; a rejection gives the first
that fails.

- **Size:** the file is at least 8 bytes.
- **Boxes:** every box, top-level and in a container or a sample entry, is
  at least its header's length, and ends within its parent (the file, for a
  top-level box). Bytes after the last box of a container or a sample entry
  that are too few for a box header (fewer than 8) are its `rest`; at the
  top level they are a gap.
- **Brands:** every top-level `ftyp` box's payload, after its header of 8
  or 16 bytes, holds its 8 bytes of fields ([§2](#2-boxes)): a 20-byte
  `ftyp` box with a 64-bit size is rejected with `the ftyp box at 0 has 4
  bytes, too few for its 8 bytes of fields`. The first `ftyp` box of at
  most 4096 bytes is read, for its fields, its brands and `brand`
  ([§5.1](#51-the-root)); any other is opaque.
- **Not fragmented:** the file has no `moof` box at its top level and no
  `mvex` box in its `moov` box.
- **One movie:** the file has exactly one `moov` box, of at most 64 MiB.
- **Tables:** in every `stsd`, `stsz`, `elst`, `stts`, `ctts`, `stsc`,
  `stco`, `co64` and `stss` box, the fields fit in the payload, and the
  table's `n` entries in the bytes after them. (An `mvhd`, `tkhd`, `mdhd` or
  `hdlr` box too short for its fields is opaque instead.)
- **Samples:** for every track with an `stsz` box: at most 2^24 samples; its
  `stts` box, its `ctts` box (when it has one) and its sample-to-chunk
  layout count as many samples as its `stsz` box; its `stsc` table is not
  empty (when it has samples), starts at chunk 1, its `first_chunk`s
  increase and are at most the chunk count, and no entry has 0 samples per
  chunk; and every sample ends within the file.
- **A track:** the profile zarrifies at least one track ([§8.2](#82-tracks)).
  Without one, the input is rejected with each track's reason: `no track can
  be zarrified: track 1 (hvc1): HEVC with chroma format 2 and bit depth 8 is
  not supported: only 4:2:0 of 8 or 10 bits is`.

A box that is not well formed but within a box the profile does not parse
is not checked: its payload is opaque.

### 8.2 Tracks

A track is zarrified unless one of these holds, checked in this order;
the first that holds is its **reason** ([the convention §5.1](#51-the-root)),
and the track is kept in the mirror but has no group:

- its handler is neither `vide` nor `soun` (`a text track`);
- its sample sizes are in an `stz2` box;
- it has no samples;
- its `stsd` box has more than one sample entry, or an `stsc` entry names
  another;
- its media timescale is 0;
- **video:** its sample entry is none of `avc1`, `hvc1` and `hev1`; or, for
  `avc1`, its `avcC` box is absent, holds no SPS, or an SPS the profile
  cannot read, or the SPS is not 8-bit 4:2:0 (`chroma_format_idc` 1 and both
  bit depths 8; profiles below High are); or, for `hvc1` and `hev1`, its
  `hvcC` box is absent, does not hold a VPS, an SPS and a PPS, gives NAL unit
  lengths of 3 bytes (`lengthSizeMinusOne` 2, which ISO/IEC 14496-15 does not
  allow), or its first SPS cannot be read to its bit depths (ITU-T H.265
  §7.3.2.2), or the SPS is not 4:2:0 of 8 or 10 bits (`chroma_format_idc` 1
  and both bit depths 8, or both 10: Main and Main 10); or its width or
  height is 0; or its first sample is not a sync
  sample; or its sync samples do not increase, or one is past its last
  sample; or two samples share a presentation time; or two sync samples
  (or the last one and the end) are more than 1024 samples apart; or a
  sample before a sync sample in decode order is presented after it; or a
  sample after a sync sample in decode order is presented before the sync
  sample before it. So a sample after a sync sample in decode order may be
  presented before it (a **leading picture**), but after the sync sample
  before: a camera's B-frames after each IDR picture are, and an open
  group's, which may reference the group before ([§8.3](#83-chunks));
- **audio:** its sample entry is a QuickTime sound description of version 2;
  or it is neither `mp4a` nor `.mp3`; or, for `mp4a`, its `esds` box (or the
  one in its `wave` box) is absent or cannot be read, or its object type
  indication is neither AAC (0x40, 0x66 to 0x68) nor MPEG audio (0x69, 0x6B);
  or, for AAC, its AudioSpecificConfig cannot be read, or its object type is
  not AAC-LC (2), or its frames are 960 samples; or its sample rate or
  channel count is 0;
- its track ID is an earlier zarrified track's (`its track ID n is an earlier zarrified
  track's`): a zarrified track's group is named by its ID, so of the tracks that share one,
  only the first that no other reason keeps from being zarrified is zarrified.

**HEVC's random access pictures.** ISO/IEC 14496-15 marks an HEVC sample as a
sync sample when its picture is an IRAP picture: an IDR, a CRA or a BLA
picture. An IRAP picture may be followed in decode order by leading pictures:
RADL pictures, which are predicted from it alone, and RASL pictures, which
may also reference the group before (a CRA picture's in an open group, as
x265 codes by default). The profile reads no sample, so it does not see which
kind an IRAP or a leading picture is; the presentation-order rules above
place them as they place H.264's, and a chunk that holds a leading picture
decodes from the sync sample before its own ([§8.3](#83-chunks)), which
decodes a RASL picture as a decoder of the whole stream does, and a RADL
picture too (its reference pictures, from the IRAP picture on, are all
decoded). A chunk that starts with a CRA picture decodes from it, and the
decoder skips that picture's RASL pictures, which are the chunk before's. A
BLA picture's RASL pictures are output by no decoder, in a chunk or in the
whole stream, so their rows are the fill value. An `hev1` track may carry
parameter sets in its samples as well as in its `hvcC` box: a chunk decodes
with the box's and with those of the samples it decodes, so a sync sample
that needs a parameter set only an earlier sample carries is decoded
wrongly, which the profile, reading no sample, does not check.

The channel count and sample rate of an AAC track are its
AudioSpecificConfig's (the sample entry's channel count when its channel
configuration is 0); those of an MP3 track its sample entry's.

The presentation-order rules bound the samples a chunk decodes
([§8.3](#83-chunks)). Its pictures are decoded at or after the last sync
sample presented at or before its first picture, since a sample decoded
before a sync sample is presented before it. And it decodes from the last
sync sample at or before its first picture in decode order or from the sync
sample before that, never further back, since the earlier sync sample's
leading pictures are decoded before the later one and so are not in the
chunk. With the spacing rule, a chunk decodes fewer than 2048 samples before
its first picture in decode order, so its reference fits in a payload
([§1.2](../zarrify.md#12-input)).

### 8.3 Chunks

Every chunk of every array of a zarrified track is present. A chunk of a plane or of
`samples` is the `refkv.stream` chunk ([conventions §10](../conventions.md#10-coded-streams))
of these samples, in decode order, a range per run of samples that follow
each other in the file:

- **Video**, chunk `q` of the pictures `a = a_q` to `b − 1`, `b = a_{q+1}`
  ([the convention §4.1](#41-video-tracks)): the samples from a sync sample
  `s` to the last of those pictures in decode order, where `s` is the last
  sync sample at or before the first of them in decode order, or, while
  one of them is a leading picture of `s` ([§8.2](#82-tracks)), the sync
  sample before `s`. A sample's position is its presentation index less `a`
  when that is from 0 to `b − a − 1`, else −1. So the chunk decodes the
  group of pictures it starts in from its sync sample, and every picture
  its own pictures are predicted from, an open group's leading pictures
  included; the pictures it decodes and does not hold (before `a`, or after
  `b − 1` but decoded before one of its own) are dropped. A chunk that
  starts with a sync sample decodes from it. The chunks of `u` and `v` are
  `y`'s.
- **Audio**, chunk `q` of the frames `a = q × 256` to `b − 1`: the frames
  `l` to `b − 1`, frame `j`'s position −1 when `j < a` and `(j − a) × S`
  otherwise. `l = a` for the first chunk; otherwise, for AAC, `l = a − 1`
  (the frame before the chunk, whose overlap its first frame needs), and for
  MP3, `l` is the greatest index at most `a − 2` such that frames `l` to
  `a − 3` hold at least 511 bytes of main data, counting each frame's size
  less 38 bytes (its header, CRC and side information are at most that), or
  0 when none does: the MP3 profile's rule
  ([zarrify/mp3.md §8.2](mp3.md#82-chunks)) with MPEG-1's reservoir and
  a bound in place of each frame's side information, which a zarrifier
  does not read.

<a id="type-mp4videochunk"></a>**`Mp4VideoChunk`**, chunk `q` of `y`, `u` and `v`, of the
pictures `a` to `b − 1`:

| part | range | presence | value |
|---|---|---|---|
| the header | literal | required | the [`StreamChunk`](../conventions.md#type-streamchunk) header of the samples from `s` to the last of the pictures in decode order: their count, then each one's size and its position, its presentation index less `a` when that is from 0 to `b − a − 1`, else −1 |
| the samples | source ranges `(0, offset, length)` | required | those samples, in decode order, a range per run of samples that follow each other in the file |

<a id="type-mp4audiochunk"></a>**`Mp4AudioChunk`**, chunk `q` of `samples`, of the frames
`a = q × 256` to `b − 1`:

| part | range | presence | value |
|---|---|---|---|
| the header | literal | required | the [`StreamChunk`](../conventions.md#type-streamchunk) header of the frames `l` to `b − 1`: their count, then each one's size and its position, −1 when `j < a` and `(j − a) × S` otherwise |
| the frames | source ranges `(0, offset, length)` | required | frames `l` to `b − 1`, in order, a range per run of frames that follow each other in the file |

<a id="type-mp4timechunk"></a>**`Mp4TimeChunk`**, chunk `k` of `time`:

| part | range | presence | value |
|---|---|---|---|
| the times | `bytes` | required | the presentation times of the pictures `k × c` to `min(N, (k + 1) × c) − 1`, `c` the chunk shape's, as the array's codecs encode them: `int64` little-endian, the last chunk padded with zeros to the chunk shape, then gzip |

The `time` array's chunks are bytes entries. The output has no data
sources: its source table is source 0 alone. `refkv_source` is the IR mirror
([the convention §5.3](#53-elements)). The archive has no other entries than
the root's, `tracks`' and each track group's `zarr.json`, the arrays' and
their chunks, and the mirror's.

### 8.4 Summary

This section is informative. Both implementations print a one-line JSON
summary, with these members:

| member | value |
|---|---|
| `brand` | the major brand, or `null` |
| `tracks` | for each track zarrified, its `id`, `format`, `path`, `frames`, `chunkFrames` and `chunks` |
| `skipped` | for each other, its `id`, `codec` and `reason` |
