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

Added 2026-10-23, after
[revision 29](../history/virtualize/REVISIONS.md#revision-29-no-recorded-revision-the-profile-registry)
(a profile's changes are recorded here, dated, from then on).

This document has two parts. [Part 1](#part-1-the-convention) is the MP4
convention: the Zarr layout, cited as "the convention §n".
[Part 2](#part-2-the-profile) is the MP4 profile: how refkv reads the source,
which inputs it rejects, and how each chunk references the source. The
profile keeps its numbering as a section of [spec/virtualize.md](../virtualize.md),
and is cited as spec/virtualize.md §n.

An MP4 file is not an image. The hierarchy holds each track it virtualizes
as a group: an H.264 video track as the Y, U and V planes of its pictures
and their presentation times, an AAC or MP3 audio track as its samples. The
pictures and samples stay in the file as coded frames; the arrays' codec,
`refkv.stream` ([conventions §10](../conventions.md#10-coded-streams)),
decodes them when a chunk is read.

# Part 1. The convention

## 1. Declaration

The root declares the convention by [conventions §2](../conventions.md#2-attributes),
with `"profile": "mp4"`, `"version": 0` ([conventions §1](../conventions.md#1-conventions)), the file's URL as `source.url`,
and the root's source metadata of [§5.1](#51-the-root) as the member `"mp4"`. Its CMO is:

```json
{
  "uuid": "bbe3f6b9-488d-4d8d-85ce-60a1649dd27e",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/virtualize/mp4/schema.json",
  "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp4.md",
  "name": "refkv_virtualized",
  "description": "The Zarr layout of a MP4 source virtualized by refkv, and the source's metadata"
}
```

The root's attributes are `zarr_conventions` and `refkv_virtualized` and
nothing else: there is no `ome` member. Each track's group declares the
convention too, with the track's source metadata of [§5.2](#52-the-tracks);
so does `refkv_source`, with the IR mirror's description
([§5.3](#53-elements), [conventions §8](../conventions.md#8-the-ir-mirror)),
and each group of the mirror's view that has a member. The arrays have no
attributes.

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
| `ctts` | the same | `{sample_count:>u4,sample_offset:>u4}[n]` (version 1: `>i4`) |
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
After its fields, a sample entry holds boxes (`avcC`, `esds`, `btrt`,
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
media timescale. The **sync samples** are the samples `stss` names (its
entries, less 1), or every sample when the track has no `stss` box.

## 4. The layout

The hierarchy is a group at the root, `{"zarr_format": 3, "node_type":
"group", "attributes": A}`, where `A` has `zarr_conventions` and
`refkv_virtualized` ([§1](#1-declaration)) and nothing else; a group
`tracks`, with no attributes; and, for each track the profile virtualizes
([spec/virtualize.md §18.2](#182-tracks)), a group `tracks/<id>`, `<id>`
the track's ID, which declares the convention with the track's source
metadata, and its arrays below. Every array's `zarr.json` is
[conventions §3](../conventions.md#3-arrays)'s, with the fill value 0.

### 4.1 Video tracks

An H.264 track of `N` samples, `W × H` pixels (its sample entry's `width`
and `height`), holds its pictures in **presentation order**: picture `p` is
the sample with the `p`-th smallest presentation time.

- `y`, `u` and `v`: the planes of the pictures, `uint8`, of shape
  `[N, H, W]` for `y` and `[N, ceil(H / 2), ceil(W / 2)]` for `u` and `v`,
  the dimension names `["time", "y", "x"]`; the codecs
  `[{"name": "refkv.stream", "configuration": {"format": "h264", "setup":
  S, "plane": P}}]`, `S` the base64 of the sample entry's `avcC` box's
  payload and `P` the array's name. The chunk shape is `[F, H, W]` (or the
  chroma planes' sizes), `F = min(256, max(1, floor(2^25 / (W × H))))`
  pictures: at most 32 MiB of luma a chunk (16 pictures at 1920 × 1080),
  since a chunk decodes whole. Chunk `q` holds the pictures `q × F` to
  `min(N, (q + 1) × F) − 1`, its key `<plane>/c/<q>/0/0`.
- `time`: the pictures' presentation times, in the media timescale, `int64`
  of shape `[N]`, the dimension names `["time"]`, the codecs
  `[{"name": "bytes", "configuration": {"endian": "little"}}, {"name":
  "gzip", "configuration": {"level": 6}}]`, the chunk shape
  `[min(N, 2^17)]`, its chunks bytes entries (the last padded with zeros to
  the chunk shape). These are the media timeline's times, as the tables give
  them; the edit list, which may shift or cut them on the movie's timeline,
  is recorded ([§5.2](#52-the-tracks)) and not applied.

The three planes of a chunk are the same `refkv.stream` chunk: each
decodes the same pictures and keeps its plane.

### 4.2 Audio tracks

An AAC or MP3 track of `N` samples (coded frames), each decoding to `S`
samples of each of its `C` channels (`S` is 1024 for AAC, 1152 for MP3 at
32 kHz and more and 576 below), holds its samples in one array:

- `samples`: `float32`, of shape `[N × S, C]`, the dimension names
  `["time", "channel"]`, the codecs `[{"name": "refkv.stream",
  "configuration": {"format": F, "setup": A}}]`, `F` `"aac"` or `"mp3"`, and
  for AAC `A` the base64 of the AudioSpecificConfig of the sample entry's
  `esds` box. The chunk shape is `[256 × S, C]`: 256 frames a chunk.

Every decoded sample is in the array, the encoder's priming and padding
included: AAC encoders start a stream with 1024 or 2112 samples of priming,
which the edit list records, and MP3 encoders with their encoder delay.
The samples of an AAC track coded with perceptual noise substitution are
those of [conventions §10](../conventions.md#10-coded-streams): in the
noise bands, a chunk's noise is its own.

## 5. Source metadata

The root's and each track group's source metadata are objects with the
members below, in that order, translated by
[conventions §6](../conventions.md#6-source-metadata-as-json). Everything
the file holds is on `refkv_source`, the file's **IR mirror**
([§5.3](#53-elements)), from which the file is rebuilt byte for byte.

### 5.1 The root

- `brand`: when the file has an `ftyp` box, `{"major": B, "minor": n,
  "compatible": [B, ...]}`, its major brand, minor version and compatible
  brands, each brand its 4 bytes as text. Absent otherwise.
- `timescale` and `duration`: the `mvhd` box's.
- `tracks`: one object per track, in the order of the `trak` boxes,
  `{"id": n, "handler": H, "codec": E, "path": P, "reason": R}`: its ID, its
  handler, its sample entry's type (`""` without one), and `P` its group's
  path and `R` null when the profile virtualizes it, else `P` null and `R`
  why not ([spec/virtualize.md §18.2](#182-tracks)).

### 5.2 The tracks

A track's group has: `id`, `handler`, `codec` and `format` (`"h264"`,
`"aac"` or `"mp3"`); `timescale` and `duration`, the media's; `edits`, an
array of `{"duration": n, "media_time": n, "rate": x}`, its `elst` box's
entries (`rate` the media rate and fraction as one number); `frames`, its
sample count `N`; then, for a video track, `width`, `height`, `profile` and
`level` (the SPS's `profile_idc` and `level_idc`) and `sync_samples` (their
count), and for an audio track `sample_rate`, `channels`,
`samples_per_frame` (`S`) and `samples` (`N × S`); and last `chunk_frames`,
the frames a chunk holds (`F`, or 256).

### 5.3 Elements

`refkv_source` is the file's **IR mirror**
([conventions §8](../conventions.md#8-the-ir-mirror)). Paths are relative to
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
| `samples/<T>/<j>` | data | | sample `j` of track `T`, `(offset, size)`; its form's geometry is `{"width", "height", "planes": 3, "data_type": "uint8"}` for a video track and `{"samples", "channels", "data_type": "float32"}` for an audio track (null for a track the profile does not virtualize), its codec `{"name": E, "format": F}` (`F` null for such a track), its recipe `[["src", 0, null]]` |
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

The elements are determined by the file, and the mirror by its elements
([conventions §8.8](../conventions.md#88-canonical-form)): hierarchies of one
file are compared entry for entry, `refkv_source` included
([spec/virtualize.md §1.1](../virtualize.md#11-output-and-equivalence)).

## 6. Example

A 3-second MP4 at `https://example.org/clip.mp4` (the fixture
`fixtures/mp4/mp4_h264_aac.mp4`) holds an H.264 track of 30 pictures of 64 ×
48, B-frames in groups of 10, and an AAC-LC mono track at 16 kHz. Its root's
`refkv_virtualized` is:

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
 "profile": 100, "level": 10, "sync_samples": 3, "chunk_frames": 256}
```

The edit list starts the movie at media time 2048, two pictures' worth:
the B-frames' composition delay. `tracks/1/y` has the shape `[30, 48, 64]`,
the chunk shape `[256, 48, 64]` and the codecs `[{"name": "refkv.stream",
"configuration": {"format": "h264", "setup": "AWQACv/hABhn...", "plane":
"y"}}]`; its one chunk, `tracks/1/y/c/0/0/0`, is a literal header and the
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
  box) are in the mirror; converting to RGB is the reader's.
- **Other tracks and codecs.** HEVC, AV1, 10-bit or 4:2:2 H.264, other
  audio codecs, timecode, text and chapter tracks are recorded in `tracks`
  with the reason the profile does not virtualize them, and kept whole in
  the mirror.
- **Metadata.** `udta` and `meta` boxes (titles, iTunes tags, GPS) are
  opaque in the mirror.

# Part 2. The profile

The MP4 profile of [spec/virtualize.md](../virtualize.md), numbered as its
[§18](#18-mp4-profile). [§1](../virtualize.md#1-introduction) and
[§2](../virtualize.md#2-the-zarr-layout) are in spec/virtualize.md and apply
here.

The output has the convention's layout for the input
([spec/virtualize.md §2](../virtualize.md#2-the-zarr-layout)); this profile
says which inputs are accepted, which tracks are virtualized, and how each
chunk references the file. "The convention §n" below is a section of
Part 1.

## 18. MP4 profile

### 18.1 Boxes

The input is an MP4 or QuickTime file, which [§1.2](../virtualize.md#12-input)
chose by a box at offset 0: a 32-bit size of at least 8, or 1, and the type
`ftyp`, `moov`, `mdat`, `wide`, `free` or `skip`. A virtualizer reads the
top-level boxes' headers, then the whole `moov` box (and the `ftyp` box); it
never reads the media data.

**Checks.** The input is rejected unless all of these hold. They are checked
as the boxes are met, the top-level boxes first, in the file's order, then
the `moov` box's tree, then each track's samples; a rejection gives the first
that fails.

- **Size:** the file is at least 8 bytes.
- **Boxes:** every box, top-level and in a container or a sample entry, is
  at least its header's length, and ends within its parent (the file, for a
  top-level box). Bytes after the last box of a container or a sample entry
  that are too few for a box header (fewer than 8) are its `rest`; at the
  top level they are a gap.
- **Not fragmented:** the file has no `moof` box at its top level and no
  `mvex` box in its `moov` box.
- **One movie:** the file has exactly one `moov` box, of at most 64 MiB.
- **Tables:** in every `stsd`, `stsz`, `elst`, `stts`, `ctts`, `stsc`,
  `stco`, `co64` and `stss` box, the fields fit in the payload, and the
  table's `n` entries in the bytes after them. (An `mvhd`, `tkhd`, `mdhd` or
  `hdlr` box too short for its fields is opaque instead, and an `ftyp` box
  of more than 4096 bytes is not read.)
- **Samples:** for every track with an `stsz` box: at most 2^24 samples; its
  `stts` box, its `ctts` box (when it has one) and its sample-to-chunk
  layout count as many samples as its `stsz` box; its `stsc` table is not
  empty (when it has samples), starts at chunk 1, its `first_chunk`s
  increase and are at most the chunk count, and no entry has 0 samples per
  chunk; and every sample ends within the file.
- **A track:** the profile virtualizes at least one track ([§18.2](#182-tracks)).
  Without one, the input is rejected with each track's reason: `no track can
  be virtualized: track 1 (hev1): hev1 video is not supported: only H.264
  (avc1) is`.

A box that is not well formed but within a box the profile does not parse
is not checked: its payload is opaque.

### 18.2 Tracks

A track is virtualized unless one of these holds, checked in this order;
the first that holds is its **reason** ([the convention §5.1](#51-the-root)),
and the track is kept in the mirror but has no group:

- its handler is neither `vide` nor `soun` (`a text track`);
- its sample sizes are in an `stz2` box;
- it has no samples;
- its `stsd` box has more than one sample entry, or an `stsc` entry names
  another;
- its media timescale is 0;
- **video:** its sample entry is not `avc1`; or its `avcC` box is absent,
  holds no SPS, or an SPS the profile cannot read; or the SPS is not 8-bit
  4:2:0 (`chroma_format_idc` 1 and both bit depths 8; profiles below High
  are); or its width or height is 0; or its first sample is not a sync
  sample; or two samples share a presentation time; or two sync samples
  (or the last one and the end) are more than 1024 samples apart; or a group
  of pictures is **open**: a sample after a sync sample in decode order is
  presented before it, or one before it after it;
- **audio:** its sample entry is a QuickTime sound description of version 2;
  or it is neither `mp4a` nor `.mp3`; or, for `mp4a`, its `esds` box (or the
  one in its `wave` box) is absent or cannot be read, or its object type
  indication is neither AAC (0x40, 0x66 to 0x68) nor MPEG audio (0x69, 0x6B);
  or, for AAC, its AudioSpecificConfig cannot be read, or its object type is
  not AAC-LC (2), or its frames are 960 samples; or its sample rate or
  channel count is 0.

The channel count and sample rate of an AAC track are its
AudioSpecificConfig's (the sample entry's channel count when its channel
configuration is 0); those of an MP3 track its sample entry's.

The open-group and spacing rules make every chunk decodable from a sync
sample before it, with a header of at most a few thousand frames, so its
reference fits in a payload ([§1.2](../virtualize.md#12-input)).

### 18.3 Chunks

Every chunk of every array of a virtualized track is present, and is the
`refkv.stream` chunk ([conventions §10](../conventions.md#10-coded-streams))
of these samples, in decode order, a range per run of samples that follow
each other in the file:

- **Video**, chunk `q` of the pictures `a = q × F` to `b − 1`: the samples
  from the last sync sample at or before the first of those pictures in
  decode order, to the last of them in decode order. A sample's position is
  its presentation index less `a` when that is from 0 to `b − a − 1`, else
  −1. So the chunk decodes the group of pictures it starts in from its sync
  sample, and every picture its own pictures are predicted from; the
  pictures it decodes and does not hold (before `a`, or after `b − 1` but
  decoded before one of its own) are dropped. The chunks of `u` and `v` are
  `y`'s.
- **Audio**, chunk `q` of the frames `a = q × 256` to `b − 1`: the frames
  `l` to `b − 1`, frame `j`'s position −1 when `j < a` and `(j − a) × S`
  otherwise. `l = a` for the first chunk; otherwise, for AAC, `l = a − 1`
  (the frame before the chunk, whose overlap its first frame needs), and for
  MP3, `l` is the greatest index at most `a − 2` such that frames `l` to
  `a − 3` hold at least 511 bytes of main data, counting each frame's size
  less 38 bytes (its header, CRC and side information are at most that), or
  0 when none does: the MP3 profile's rule
  ([virtualize/mp3.md §17.2](mp3.md#172-chunks)) with MPEG-1's reservoir and
  a bound in place of each frame's side information, which a virtualizer
  does not read.

The `time` array's chunks are bytes entries. The output has no data
sources: its source table is source 0 alone. `refkv_source` is the IR mirror
([the convention §5.3](#53-elements)). The archive has no other entries than
the root's, `tracks`' and each track group's `zarr.json`, the arrays' and
their chunks, and the mirror's.

### 18.4 Summary

This section is informative. Both implementations print a one-line JSON
summary: `brand` (the major brand, or `null`), `tracks` (for each track
virtualized, its `id`, `format`, `path`, `frames`, `chunkFrames` and
`chunks`) and `skipped` (for each other, its `id`, `codec` and `reason`).
