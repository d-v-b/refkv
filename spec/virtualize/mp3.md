# MP3

The Zarr layout of an MP3 file (MPEG-1, MPEG-2 or MPEG-2.5 Layer III audio,
`.mp3`), and the translation of its stream parameters and tags into JSON.
What all of refkv's conventions share is in
[spec/conventions.md](../conventions.md), cited here as "conventions §n". How
refkv produces this layout as a reference store, and which files it accepts,
is the MP3 profile, [Part 2](#part-2-the-profile) below.

Convention version: 0 (until release, [conventions §1](../conventions.md#1-conventions)) · UUID: `b6cb0047-9448-4fdc-b03e-10dd3d6aa829` ·
Schema: [schema.json](mp3/schema.json)

Added 2026-10-21, after
[revision 29](../history/virtualize/REVISIONS.md#revision-29-no-recorded-revision-the-profile-registry)
(a profile's changes are recorded here, dated, from then on).

This document has two parts. [Part 1](#part-1-the-convention) is the MP3
convention: the Zarr layout, cited as "the convention §n".
[Part 2](#part-2-the-profile) is the MP3 profile: how refkv reads the source,
which inputs it rejects, and how each chunk references the source. The profile
keeps its numbering as a section of [spec/virtualize.md](../virtualize.md),
and is cited as spec/virtualize.md §n.

MP3 is the one format among refkv's conventions whose source is not an
image: the hierarchy holds the decoded audio as one array of samples, with no
OME-NGFF metadata. The samples stay in the file as MPEG frames; the array's
codec decodes them when a chunk is read.

# Part 1. The convention

## 1. Declaration

The root declares the convention by [conventions §2](../conventions.md#2-attributes),
with `"profile": "mp3"`, `"version": 0` ([conventions §1](../conventions.md#1-conventions)), the file's URL as `source.url`,
and the root's source metadata of [§5.1](#51-the-root) as the member `"mp3"`. Its CMO is:

```json
{
  "uuid": "b6cb0047-9448-4fdc-b03e-10dd3d6aa829",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/virtualize/mp3/schema.json",
  "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp3.md",
  "name": "refkv_virtualized",
  "description": "The Zarr layout of a MP3 source virtualized by refkv, and the source's metadata"
}
```

The root's attributes are `zarr_conventions` and `refkv_virtualized` and
nothing else: there is no `ome` member, since the hierarchy is not an image.
`refkv_source` declares the convention too, with the IR mirror's description
([§5.2](#52-the-ir-mirror), [conventions §8](../conventions.md#8-the-ir-mirror)), and so does each group of
the mirror's view that has a member; the array and the mirror's arrays have no
attributes.

## 2. Frames

An MP3 file is a sequence of **frames**, each a 4-byte **header** followed by
the frame's data, with tags ([§3](#3-tags)) before the first frame and after
the last. The header's bits, most significant first (byte 0 first):

| bits | field | values |
|---|---|---|
| 31–21 | sync | all ones (`FF Ex` to `FF Fx`) |
| 20–19 | version | `00` MPEG-2.5, `01` reserved, `10` MPEG-2, `11` MPEG-1 |
| 18–17 | layer | `00` reserved, `01` Layer III, `10` Layer II, `11` Layer I |
| 16 | protection | `0`: a 16-bit CRC follows the header; `1`: none |
| 15–12 | bitrate index | 0 free format, 15 invalid; else a table entry |
| 11–10 | sample rate index | 3 invalid; else a table entry |
| 9 | padding | the frame is one byte longer |
| 8 | private | |
| 7–6 | channel mode | `00` stereo, `01` joint stereo, `10` dual channel, `11` mono |
| 5–4 | mode extension | |
| 3 | copyright | |
| 2 | original | |
| 1–0 | emphasis | |

A header is **valid** when its sync is all ones, its version and layer are
not reserved, its bitrate index is from 1 to 14 and its sample rate index is
from 0 to 2. (A free-format header is one that is valid but for a bitrate
index of 0.) The header gives:

- the **sample rate**, by version and index: MPEG-1 44100, 48000, 32000;
  MPEG-2 22050, 24000, 16000; MPEG-2.5 11025, 12000, 8000;
- the **bitrate** in kbps, by version, layer and index 1 to 14: for Layer
  III, MPEG-1 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320,
  and MPEG-2 and 2.5 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144,
  160 (Layer I and II frames, which the profile rejects, have their own
  tables: Layer I 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384,
  416, 448 for MPEG-1 and 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176,
  192, 224, 256 for MPEG-2 and 2.5; Layer II 32, 48, 56, 64, 80, 96, 112,
  128, 160, 192, 224, 256, 320, 384 for MPEG-1, and Layer III's for MPEG-2
  and 2.5);
- the **channel count** `C`: 1 for mono, 2 otherwise;
- the frame's **length** `L` in bytes, header included, with `b` the bitrate
  in bits per second, `r` the sample rate and `p` the padding bit, integer
  division: Layer III `144 × b / r + p` for MPEG-1 and `72 × b / r + p` for
  MPEG-2 and 2.5 (Layer II `144 × b / r + p`; Layer I `(12 × b / r + p) × 4`);
- the **samples per channel** `S` a Layer III frame decodes to: 1152 for
  MPEG-1, 576 for MPEG-2 and 2.5.

A Layer III frame is its header, the 2-byte CRC when the protection bit is
0, the **side information** (MPEG-1: 17 bytes for mono, 32 otherwise;
MPEG-2 and 2.5: 9 for mono, 17 otherwise), and the **main data** that fills
the rest of the frame, `M = L − 4 − 2 × crc − side` bytes. A frame's own
audio may begin before its main data, in the main data of earlier frames (the
**bit reservoir**): the side information's `main_data_begin` says how many
bytes back, at most 511 for MPEG-1 and 255 for MPEG-2 and 2.5. So a frame
cannot be decoded alone; [§4.1](#41-the-array) says what a chunk holds so
that it can be.

Two frames are of **one stream** when they agree on the version, the layer,
the sample rate and the channel count. The bitrate, the padding, the
protection and the channel mode may differ from frame to frame (a variable
bitrate stream; joint stereo).

**The info frame.** An encoder may start the stream with a frame that holds
no audio but a tag describing the stream: a **Xing** or **Info** tag (the
ASCII `Xing` or `Info` right after the side information, that is at offset
`4 + 2 × crc + side` of the frame), followed by a 32-bit big-endian flags
word and, for each flag bit set, the frame count (bit 0), the byte count (bit
1), a 100-byte seek table (bit 2) and a quality indicator (bit 3), each
32-bit big-endian; a **LAME tag** may follow: a 9-byte encoder version
(`LAME3.100`, `Lavc61.19`), then its fields, among them at offset 21 from
the version's start the encoder **delay** (12 bits) and **padding** (12
bits) in samples. A **VBRI** tag is the ASCII `VBRI` at offset 36 of the
frame, followed by a 16-bit version, a 16-bit delay, a 16-bit quality, the
byte count and the frame count (32-bit, all big-endian). The convention does
not count an info frame as audio ([§4.1](#41-the-array)) and records its
fields ([§5.1](#51-the-root)).

## 3. Tags

An **ID3v2 tag** at the start of the file: `ID3`, a version (2 bytes: the
major version and the revision), a flags byte, and a 4-byte **syncsafe**
size (each byte below `80`; the size is their 7-bit values, most significant
first), the 10-byte header; then `size` bytes of frames and padding; then,
when bit 4 (`10`) of the flags is set, a 10-byte footer. A file may hold
several, one after the other. The convention does not decode the tag's
frames.

An **ID3v1 tag** is the last 128 bytes of the file when they start with
`TAG`: the title, artist and album (30 bytes each), the year (4), the
comment (30; in ID3v1.1, when its byte 28 is NUL, byte 29 is the track
number) and the genre (1 byte).

An **APE tag** ends with a 32-byte footer, right before the ID3v1 tag or at
the end of the file: `APETAGEX`, the version, the size (the items and the
footer, not the header), the item count and the flags (32-bit little-endian
each; bit 31 of the flags: the tag also has a 32-byte header before its
items), then 8 reserved bytes. The tag is `size` bytes, plus 32 when it has
a header, ending at the footer's end; a footer whose tag would start before
the first byte after the ID3v2 tags, or whose size is below 32, is not an
APE tag. The convention does not decode the items.

## 4. The layout

The hierarchy is a group at the root with one array at path `samples`: the
root `zarr.json` is `{"zarr_format": 3, "node_type": "group", "attributes":
A}`, where `A` has `zarr_conventions` and `refkv_virtualized` ([§1](#1-declaration)) and
nothing else.

### 4.1 The array

The **audio frames** are the stream's frames in file order, numbered from 0,
without the info frame ([§2](#2-frames)); `N` is their count ([spec/virtualize.md §16.1](#161-the-stream)
says which frames those are). Every audio frame has the stream's `S`
and `C`.

- **Shape:** `[N × S, C]`: the samples per channel, then the channels.
- **Data type:** `int16`.
- **Dimension names:** `["time", "channel"]`.
- **Chunk shape:** `[F × S, C]` with **`F = 256` frames per chunk**: chunk `q`
  holds the audio frames `q × F` to `min(N, (q + 1) × F) − 1`, which are
  **its frames**; the last chunk may hold fewer than `F`, and is padded
  to the chunk shape by the codec. The chunk grid is regular; the chunk
  keys are `samples/c/<q>/0`.
- **Codecs:** `[{"name": "mp3"}]`, the array-to-bytes codec this convention
  defines below.
- **Fill value:** 0.

The array's `zarr.json` is otherwise [conventions §3](../conventions.md#3-arrays)'s.

**The `mp3` codec.** The bytes of a chunk are:

1. a 32-bit little-endian unsigned integer **`keep`**, the samples per
   channel of the chunk's frames: `F × S`, or fewer for the last chunk;
2. then MPEG audio frames, one after the other in file order (whatever
   the file holds between two frames is left out): the chunk's
   **lead-in**, the frames before its first frame that a decoder needs to
   have decoded so that it decodes the chunk's frames as a decoder of the
   whole stream does ([spec/virtualize.md §16.2](#162-chunks) says which);
   then the chunk's frames.

Decoding a chunk is decoding the frames of 2 in one pass as a Layer III
decoder decodes a stream (the frames of the lead-in supply the bit
reservoir and the overlap state of the filter bank for the chunk's frames;
their own output is discarded), to 16-bit PCM of the stream's `C` channels,
interleaved by sample, and taking the **last `keep` samples** of the output:
they are the chunk's samples, from the chunk's first sample on, followed by
zeros up to the chunk shape. A decoder that cannot decode a frame whose
reservoir it does not hold (the stream's first frames, which have no
lead-in) gives fewer than `keep` samples for the first chunk; they are the
last of the `keep`, preceded by zeros. The channel count of the frames is
the array's; a chunk whose frames differ from it, or whose `keep` exceeds
the chunk shape, is an error.

### 4.2 The samples

Sample `i` of channel `c` is the output of a Layer III decoder, as ISO/IEC
11172-3 and 13818-3 define it, decoding the audio frames one after the
other from the first (the frames alone: not the bytes between them), as a
signed 16-bit integer (rounded from the decoder's output), the
`i`th sample it produces for channel `c`; the decoder starts with an empty
bit reservoir and a zero filter-bank state, and its first frame's output is
what it is (the stream's encoder delay and the decoder's own delay, about
529 samples, are in the array; neither is trimmed). The samples of a frame
are its `S` outputs; a frame the decoder cannot decode (its reservoir
reaches before the first audio frame) gives zeros. Two conforming decoders
may differ by a few units in a sample (the standards allow it); the
convention does not say which decoder a reader uses, and
[spec/virtualize.md §1.1](../virtualize.md#11-output-and-equivalence) compares
references, not samples.

The sample rate is `sample_rate` of [§5.1](#51-the-root); sample `i` is at
`i / sample_rate` seconds from the stream's start.

## 5. Source metadata

The root's source metadata `S` ([conventions §2](../conventions.md#2-attributes))
is an object with the members of [§5.1](#51-the-root), in that order,
translated by [conventions §6](../conventions.md#6-source-metadata-as-json).
Everything the file holds is on `refkv_source`, the file's **IR mirror**
([§5.2](#52-the-ir-mirror)), from which the file is rebuilt byte for byte.

### 5.1 The root

- `version`: `"MPEG-1"`, `"MPEG-2"` or `"MPEG-2.5"`, the stream's.
- `layer`: 3.
- `sample_rate`: the stream's, in Hz.
- `channels`: `C`.
- `channel_mode`: the first audio frame's: `"stereo"`, `"joint_stereo"`,
  `"dual_channel"` or `"mono"`.
- `samples_per_frame`: `S`.
- `frames`: `N`, the audio frames.
- `samples`: `N × S`.
- `duration`: `N × S / sample_rate`, in seconds, as a number.
- `bitrate_mode`: `"constant"` when every audio frame has the same bitrate
  index, else `"variable"`.
- `bitrate_kbps`: that bitrate in kbps, or `null` when variable.
- `crc_frames`: how many audio frames have the protection bit 0 (a CRC).
- `info`: when the stream starts with an info frame ([§2](#2-frames)), an
  object `{"tag": T, "frames": n, "bytes": n, "quality": n, "encoder": e,
  "delay": n, "padding": n}`: `T` is `"Xing"`, `"Info"` or `"VBRI"`; the
  counts and the quality are those the tag holds, `null` for each it does
  not (a Xing or Info flag bit clear; a value past the frame's end);
  `encoder` is the LAME tag's 9-byte version as text: its bytes up to the
  first NUL, when there is at least one, they are printable ASCII (`20` to
  `7E`) and the first is not a space, trailing spaces removed (`LAME3.99r`,
  `LAME4.0`, `Lavc63.1.`), else `null`; `delay` and `padding` are its
  encoder delay and padding, `null` without an encoder or when the frame
  ends before them; for a VBRI tag, `delay` is the tag's and `encoder` and
  `padding` are `null`. Absent when there is no info frame.
- `id3v2`: an array with one object per ID3v2 tag at the start, in order,
  `{"version": [major, revision], "flags": f, "size": n, "footer": b}`
  (the header's bytes 3 and 4, its flags byte, its size field, and whether
  it has a footer). Absent when there is none.
- `id3v1`: when the file ends with an ID3v1 tag, `{"title", "artist",
  "album", "year", "comment"}`, each field's bytes up to its first NUL as a
  text value, `"track"`, the track number of an ID3v1.1 tag (byte 28 of the
  comment NUL and byte 29 not) or `null`, and `"genre"`, the genre byte.
  Absent otherwise.
- `ape`: when the file has an APE tag, `{"version": v, "size": n, "items":
  n}`: the footer's version and item count, and the tag's whole size
  (header included). Absent otherwise.
- `stray_bytes`: the bytes between the ID3v2 tags and the trailing tags
  that are in no frame (junk between frames, a truncated last frame,
  bytes after the last frame).

`version` to `crc_frames` are derived from the frames, so that a reader
need not read a header; the tags' fields are there so that a reader finds
the stream's description and the file's labels without decoding the tags.
The ID3v2 and APE tags' items are in the mirror, not decoded.

### 5.2 The IR mirror

`refkv_source` is the file's **IR mirror**
([conventions §8](../conventions.md#8-the-ir-mirror)): its table, from which the
file is rebuilt byte for byte, and its view, `refkv_source/tree`. The IR's
elements (the MP3 **source model**) are below; nothing is left out: the
tags, the info frame, every audio frame, and every byte between or after
them.

### 5.3 Elements

Paths are relative to the IR's root, whose path is `""`
([conventions §8.1](../conventions.md#81-elements)). Types are those of
[conventions §8.6](../conventions.md#86-types); `n` is a tag's size, `L` a
frame's length.

| path | kind | type | extent: what |
|---|---|---|---|
| `""` | struct | | the root: the whole file, `(0, size)` |
| `id3v2/<k>` | struct | | the `k`-th ID3v2 tag at the start, `k` from 0: the whole tag |
| `id3v2/<k>/header` | value | `{id:ascii[3],version:u1[2],flags:u1,size:u1[4]}` | its 10-byte header |
| `id3v2/<k>/data` | value | `bytes[n]` | its `n` bytes of frames and padding, when `n > 0`, opaque |
| `id3v2/<k>/footer` | value | `bytes[10]` | its footer, when it has one |
| `info` | value | `bytes[L]` | the info frame ([§2](#2-frames)), when the stream starts with one |
| `frames` | struct | | the audio frames: from the first's start to the last's end |
| `frames/<j>` | data | | audio frame `j`, `j` from 0, `(offset, L)`, header included; its form's geometry is `{"samples": S, "channels": C, "data_type": "int16"}`, its codec `{"name": "mp3", "version": V, "sample_rate": r}`, and its recipe `[["src", 0, null]]` |
| `id3v1` | value | `{tag:ascii[3],title:cstr[30],artist:cstr[30],album:cstr[30],year:ascii[4],comment:cstr[30],genre:u1}` | the ID3v1 tag, `(size − 128, 128)`, when there is one |
| `ape` | value | `bytes[n]` | the APE tag, header and footer included, when there is one, opaque |
| `gaps/<offset>` | gap | | bytes no other element claims: junk between frames, a truncated last frame, bytes after the last frame |

The view ([conventions §8.7](../conventions.md#87-the-view)) shows each ID3v2
header and the ID3v1 tag; the tags' bodies, the info frame and the APE tag,
opaque bytes, are not shown.

### 5.4 Equivalence

The elements are determined by the file, and the mirror by its elements
([conventions §8.8](../conventions.md#88-canonical-form): one order, one folding, one encoding): hierarchies of one
file are compared entry for entry, `refkv_source` included
([spec/virtualize.md §1.1](../virtualize.md#11-output-and-equivalence)).

## 6. Example

A mono MPEG-2 stream at 16 kHz and 32 kbps of 22 frames and no tags, at
`https://example.org/clip.mp3` (the fixture
`fixtures/mp3/mp3_mpeg2_mono_16k.mp3`, 3168 bytes of frames of 144 bytes),
has this root `zarr.json`:

```json
{
  "zarr_format": 3,
  "node_type": "group",
  "attributes": {
    "zarr_conventions": [{
      "uuid": "b6cb0047-9448-4fdc-b03e-10dd3d6aa829",
      "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/virtualize/mp3/schema.json",
      "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp3.md",
      "name": "refkv_virtualized",
      "description": "The Zarr layout of a MP3 source virtualized by refkv, and the source's metadata"
    }],
    "refkv_virtualized": {
      "profile": "mp3",
      "version": 0,
      "source": {"url": "https://example.org/clip.mp3"},
      "mp3": {
        "version": "MPEG-2",
        "layer": 3,
        "sample_rate": 16000,
        "channels": 1,
        "channel_mode": "mono",
        "samples_per_frame": 576,
        "frames": 22,
        "samples": 12672,
        "duration": 0.792,
        "bitrate_mode": "constant",
        "bitrate_kbps": 32,
        "crc_frames": 0,
        "stray_bytes": 0
      }
    }
  }
}
```

Its array `samples/zarr.json` has the shape `[12672, 1]`, the chunk shape
`[147456, 1]` (256 frames of 576 samples), the data type `int16`, the codecs
`[{"name": "mp3"}]` and the dimension names `["time", "channel"]`. The one
chunk, `samples/c/0/0`, is the literal `80 31 00 00` (`keep = 12672`) then
the range `(0, 3168)` of source 0: every frame, no lead-in. Read with 4
frames a chunk instead (as the tests do), chunk 3 holds frames 12 to 15,
`keep = 2304`, and its range is `(1152, 1152)`: it starts at frame 8, since
the lead-in is frames 10 and 11, the two before the chunk, and before them
frames 8 and 9, whose main data (131 bytes each: `144 − 4 − 9`) holds 262
bytes, at least the 255-byte reservoir of MPEG-2.

## 7. What the layout does not express

This section is informative.

- **Gapless playback.** The encoder delay and padding of a LAME tag say
  which samples at the start and the end are the encoder's, not the
  recording's; players trim them. The array holds every decoded sample and
  records the two counts in `info`, so a reader that wants the recording
  alone drops `delay + 529` samples at the start (529 being a decoder's own
  delay) and `padding − 529` at the end, as players do.
- **The tags' items.** An ID3v2 tag's frames (title, artist, cover art,
  lyrics) and an APE tag's items are kept whole in the mirror and not
  decoded; `id3v1` is, as it is fixed-width.
- **Per-frame values.** The bitrate, padding and channel mode of each
  frame, and the seek table of a Xing tag, are in the frames and the info
  frame in the mirror, not in the root.
- **The decoder.** Which decoder, and whether it uses the gapless
  information or the seek table, is the reader's choice; the convention
  says what the samples are, to the accuracy the standards allow.
- **A broken reservoir.** A frame whose bit reservoir reaches into frames
  the stream does not hold (two streams cut and joined, a frame lost)
  decodes as the reader's decoder has it: some decoders skip it, which
  shifts the samples of its chunk by a frame; others give noise or
  silence for it. Only the stream's first frames, which have no earlier
  frames, are such frames in a well-formed file.

# Part 2. The profile

The MP3 profile of [spec/virtualize.md](../virtualize.md), numbered as its
[§16](#16-mp3-profile). [§1](../virtualize.md#1-introduction) and
[§2](../virtualize.md#2-the-zarr-layout) are in spec/virtualize.md and apply
here.

The output has the convention's layout for the input
([spec/virtualize.md §2](../virtualize.md#2-the-zarr-layout)); this profile says
which inputs are accepted and how each chunk references the file. "The
convention §n" below is a section of Part 1.

## 16. MP3 profile

### 16.1 The stream

The input is an MP3 file, which [§1.2](../virtualize.md#12-input) chose by
`ID3` at offset 0, or a frame header at offset 0 whose sync, version, layer
and sample rate index are valid, of any layer and bitrate index
([the convention §2](#2-frames)). The whole file is read: an MP3 file has
no index, and each frame's header gives the next.

**Checks.** The input is rejected unless all of these hold, checked in this
order:

- **Size:** the file is at least 4 bytes.
- **ID3v2 tags:** for each tag at the start (`ID3` at offset 0, and again
  at the end of each tag): the file has its 10-byte header; its four size
  bytes are each below `80`; and the tag, footer included, ends within the
  file.
- **The stream:** the file has a first confirmed frame (below), and it is
  Layer III. A Layer I or Layer II first frame rejects by name
  (`MPEG-2 Layer II (the frame at 0) is not supported: only Layer III is`).
- **One stream:** no confirmed frame changes the version, the layer, the
  sample rate or the channel count of the stream (the first confirmed
  frame's): `the frame at 2016 changes the sample rate from 16000 to 22050`.
- **Audio:** at least one audio frame. Without one, the input is rejected
  as `free-format bitrate (the frame header at <offset>) is not supported`
  when the scan met a free-format header (the first one), else as `no MPEG
  audio frames`.

**Tags.** The ID3v2 tags are those at the start, one after the other, each
as [the convention §3](#3-tags) measures it; `A` is the offset after the last
(0 without one). The ID3v1 tag is the last 128 bytes when they start with
`TAG` and lie at or after `A`; `E` is its offset, else the file's size. An
APE footer ending at `E`, whose tag ([the convention §3](#3-tags)) is at
least 32 bytes and starts at or after `A`, makes the tag end at `E` and
moves `E` to the tag's start. The frames are scanned in `[A, E)`.

**Frames.** The scan keeps a position `p`, from `A`, and the **stream**
(the first confirmed frame's version, layer, sample rate and channel
count; none at first). At `p`, when `p + 4 ≤ E` and the header at `p` is
valid ([the convention §2](#2-frames)), with its frame length `L`, the
frame **fits** when `p + L ≤ E`; it is **consistent** when there is no
stream yet or it is of the stream; the **next header** is valid and of the
same stream as this header when `p + L + 4 ≤ E` and the 4 bytes at `p + L`
are a valid header that agrees with this one on the version, layer, sample
rate and channel count. The frame is **confirmed** when it fits and one of
these holds:

- it is **chained** (the previous confirmed frame ended at `p`) and
  consistent;
- there is no stream yet and `p + L = E` (a file of one frame);
- the next header is valid and of the same stream as this header.

A confirmed frame that is not consistent rejects the input (the one-stream
check); the first confirmed frame sets the stream (the Layer III check). A
confirmed frame is a frame: `p` moves to `p + L`, and the next frame is
chained. Otherwise, or when the header at `p` is not valid, `p` moves to
`p + 1` and the next frame is not chained. The scan ends when `p + 4 > E`.
A valid header that is not confirmed, a frame that does not fit (the last
frame, truncated), and every byte between confirmed frames are not frames.

**The info frame.** The first confirmed frame is the **info frame** when it
holds a Xing, Info or VBRI tag ([the convention §2](#2-frames)); it is not
an audio frame. Every other confirmed frame is an audio frame, numbered
from 0 in order.

So the scan accepts a frame reached by the chain without looking ahead,
as decoders do; a frame found after junk (or the first) needs the next
frame to agree with it, so that a sync in junk is not a frame; a parameter
change is only taken for one when two frames of the new parameters follow
each other; and trailing junk, a truncated last frame and a tag the
convention does not know are bytes between or after frames, kept as gaps
([the convention §5.3](#53-elements)).

### 16.2 Chunks

The array has `ceil(N / F)` chunks, `F = 256`, chunk `q` holding the audio
frames `a = q × F` to `b − 1`, `b = min(N, a + F)`
([the convention §4.1](#41-the-array)). Every chunk is present. Chunk `q`, at
`samples/c/<q>/0`, is the reference of two ranges:

1. the literal range of 4 bytes `keep = (b − a) × S` as a 32-bit
   little-endian unsigned integer;
2. the audio frames `l` to `b − 1` as ranges of source 0, in order, one
   range per run of frames that follow each other in the file: a frame that
   starts where the frame before it ends extends that frame's range, and
   any other frame starts a new one (so the bytes between frames, which are
   no frame, are in no range). `l` is the first frame of the **lead-in**:
   `l = a` for the first chunk (`a = 0`); otherwise `l` is the greatest
   index at most `a − 2` such that the main data of frames `l` to `a − 3`
   ([the convention §2](#2-frames), `M` of each) sums to at least `R`,
   where `R = 511` for MPEG-1 and `255` for MPEG-2 and 2.5, or `l = 0` when
   no index does.

So the lead-in is the two frames before the chunk, and before them the
fewest frames whose main data holds the largest bit reservoir the version
allows: a decoder that decodes the lead-in holds the reservoir the two
frames need, the two frames give it the filter bank's overlap and the
synthesis state the chunk's first frame needs (the frame before it in
MPEG-1, where a frame has two granules; the two frames before it in MPEG-2
and 2.5, where a frame has one), and the chunk's frames then decode as
they do in the stream. Decoding the lead-in costs a few frames' work per
chunk and no extra reads. Leaving the bytes between frames out keeps a
decoder from resyncing in the middle of a chunk: a decoder that looks for a
run of frames to lock onto would drop the frames before junk, and one that
resets its state on junk would decode the frame after it from nothing.

The ranges lie within the file (frames are found within `[A, E)`). The
output has no data sources: its source table is source 0 alone.
`refkv_source` is the IR mirror ([the convention §5.2](#52-the-ir-mirror)). The archive
has no other entries than the root's and the array's `zarr.json`, the
array's chunks, and the mirror's.

### 16.3 Summary

This section is informative. Both implementations print a one-line JSON
summary: `version`, `sampleRate`, `channels`, `frames` (the audio frames),
`samples`, `duration` (seconds), `bitrate` (kbps, or `"variable"`),
`chunkFrames` (`F`), `chunks`, `info` (the info frame's tag, or `null`),
`tags` (those of `"id3v2"`, `"id3v1"` and `"ape"` the file has, in that
order) and `strayBytes`.
