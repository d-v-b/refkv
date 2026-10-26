# FLAC

The Zarr layout of a FLAC file (Free Lossless Audio Codec, `.flac`,
[RFC 9639](https://www.rfc-editor.org/rfc/rfc9639)), and the translation of
its metadata blocks and tags into JSON. What all of refkv's conventions share
is in [spec/conventions.md](../conventions.md), cited here as "conventions
§n". How refkv produces this layout as a reference store, and which files it
accepts, is the FLAC profile, [Part 2](#part-2-the-profile) below.

Convention version: 0 (until release, [conventions §1](../conventions.md#1-conventions)) · UUID: `e9108d07-b24b-432f-a8ce-09d80c8073b4` ·
Schema: [schema.json](flac/schema.json)

Added 2026-10-25 (a profile's changes are recorded here, dated, from then on).

**2026-10-26.** Changed with no change to any output: the layout is tabulated, its entries in
[Layout](#layout) and its types in type tables
([conventions §11.5](../conventions.md#115-layout-tables)); the metadata blocks' and the frame
header's fields and the summary are tables too.

This document has two parts ([conventions §11.1](../conventions.md#111-parts)).

Like MP3's, the hierarchy holds decoded audio, with no OME-NGFF metadata.
The samples stay in the file as FLAC frames; the array's codecs decode them
when a chunk is read. FLAC is lossless: the array holds the recording's own
integer samples, exactly.

# Part 1. The convention

## Layout

The hierarchy's entries ([conventions §11.5](../conventions.md#115-layout-tables)):

| prefix | type | structure | description |
|---|---|---|---|
| `zarr.json` | group | [`GroupDocument`](../conventions.md#type-groupdocument), its attributes a [`Declaration`](../conventions.md#type-declaration) and nothing else | the root ([§4](#4-the-layout)) |
| `zarr.json#/attributes/zarr_conventions` | attribute | array of [`ConventionMetadata`](../conventions.md#type-conventionmetadata) | the CMO of [§1](#1-declaration) |
| `zarr.json#/attributes/refkv_zarrified` | attribute | [`RootProperty`](../conventions.md#type-rootproperty) | `profile` `"flac"`, `version` `0`, `source.url` the file's URL |
| `zarr.json#/attributes/refkv_zarrified/flac` | attribute | [`FlacRoot`](#type-flacroot) | the root's source metadata ([§5.1](#51-the-root)) |
| `samples/zarr.json` | array | [`FlacArray`](#type-flacarray) | the decoded samples, a sharded array ([§4.1](#41-the-array)) |
| `samples/c/<q>/0` | chunk | a [`Shard`](../zarrify.md#type-shard) of [`FlacInnerChunk`](#type-flacinnerchunk)s | every shard of the array, a run of `F` frames ([§8.2](#82-shards)) |
| `pictures/zarr.json` | group | [`GroupDocument`](../conventions.md#type-groupdocument), attributes `{}` | present only when some PICTURE block's data is not empty ([§4.2](#42-the-pictures)) |
| `pictures/<i>/zarr.json` | array | [`SourceArray`](../conventions.md#type-sourcearray), of the [bytes](../conventions.md#type-bytes) form | the data of the `i`-th PICTURE block, when not empty ([§4.2](#42-the-pictures)) |
| `pictures/<i>/c/<k>` | chunk | [`FlacPictureChunk`](#type-flacpicturechunk) | every chunk of a picture's array ([§8.2](#82-shards)) |
| `refkv_source/**` | subtree | [the mirror's entries](../conventions.md#table-mirror) | the IR mirror ([§5.2](#52-the-ir-mirror)): its elements are [§5.3](#53-elements)'s |

| placeholder | value |
|---|---|
| `<q>` | the shard's index along `time`, in decimal, from 0 to `ceil(N / F) − 1` |
| `<i>` | the index of a PICTURE block among the file's PICTURE blocks, counted from 0, in decimal |
| `<k>` | the chunk's index along `byte`, in decimal |

## 1. Declaration

The root and `refkv_source` declare the convention
([conventions §11.2](../conventions.md#112-declaration)), the root with its source
metadata of [§5.1](#51-the-root) as the member `"flac"`. Its CMO is:

```json
{
  "uuid": "e9108d07-b24b-432f-a8ce-09d80c8073b4",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/zarrify/flac/schema.json",
  "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/flac.md",
  "name": "refkv_zarrified",
  "description": "The Zarr layout of a FLAC source zarrified by refkv, and the source's metadata"
}
```

The root's attributes are `zarr_conventions` and `refkv_zarrified` and
nothing else. The arrays and the group `pictures` have no attributes.

## 2. The stream

A FLAC stream ([RFC 9639 §6](https://www.rfc-editor.org/rfc/rfc9639#section-6))
is the 4-byte **marker** `fLaC`, then **metadata blocks**, then **frames**.
All numbers are big-endian but the lengths of a Vorbis comment block, which
are little-endian.

**Metadata blocks** ([RFC 9639 §8](https://www.rfc-editor.org/rfc/rfc9639#section-8)).
Each is a 4-byte header, then its **body**: bit 7 of the header's first byte is
set on the last block, its other 7 bits are the block's **type**, and the next
3 bytes are the body's length `n`. The types:

| type | block |
|---|---|
| 0 | STREAMINFO |
| 1 | PADDING |
| 2 | APPLICATION |
| 3 | SEEKTABLE |
| 4 | VORBIS_COMMENT |
| 5 | CUESHEET |
| 6 | PICTURE |
| 7 to 126 | reserved |
| 127 | forbidden |

The first block is STREAMINFO, whose body is 34 bytes, these fields in order:

| field | size | value |
|---|---|---|
| the minimum block size | 16 bits | |
| the maximum block size | 16 bits | |
| the minimum frame size | 24 bits | 0: unknown |
| the maximum frame size | 24 bits | 0: unknown |
| the sample rate | 20 bits | |
| the channel count less 1 | 3 bits | |
| the bits per sample less 1 | 5 bits | |
| the total samples per channel | 36 bits | 0: unknown |
| the MD5 of the samples | 16 bytes | all 0: unknown |

A SEEKTABLE body is seek points of 18 bytes, each these fields:

| field | size | value |
|---|---|---|
| a sample number | 64 bits | `FFFFFFFFFFFFFFFF` for a placeholder |
| an offset | 64 bits | the offset of the frame holding it from the first frame |
| a sample count | 16 bits | that frame's sample count |

A VORBIS_COMMENT body is a vendor string and a list of fields (`NAME=value`),
each preceded by its 32-bit length, the list by its count. A CUESHEET body, a track and an
index point are these fields, in order:

| part | field | size | value |
|---|---|---|---|
| the body | a media catalog number | 128 bytes | |
| the body | the lead-in samples | 64 bits | |
| the body | a flags byte | 1 byte | bit 7: a CD |
| the body | reserved | 258 bytes | |
| the body | the track count | 1 byte | |
| the body | the tracks | | each a track |
| a track | an offset | 64 bits | |
| a track | a number | 1 byte | |
| a track | an ISRC | 12 bytes | |
| a track | a flags byte | 1 byte | bit 7: not audio; bit 6: pre-emphasis |
| a track | reserved | 13 bytes | |
| a track | the index point count | 1 byte | |
| a track | the index points | | each an index point |
| an index point | an offset | 64 bits | |
| an index point | a number | 1 byte | |
| an index point | reserved | 3 bytes | |

A PICTURE body is these fields, in order:

| field | size |
|---|---|
| the picture type | 32 bits |
| the media type's length, then the media type | 32 bits, then that many bytes |
| the description's length, then the description | 32 bits, then that many bytes |
| the width | 32 bits |
| the height | 32 bits |
| the color depth | 32 bits |
| the indexed colors | 32 bits |
| the picture data's length, then the picture data | 32 bits, then that many bytes |

An APPLICATION body is a 4-byte ID and its data.

**Frames** ([RFC 9639 §9](https://www.rfc-editor.org/rfc/rfc9639#section-9)).
A frame is a header, one subframe per channel, zero bits up to a byte
boundary, and a 16-bit CRC. The header's fields, in order:

| field | size | value |
|---|---|---|
| the sync code and the **blocking strategy** | 16 bits | `FF F8`, with the blocking strategy in bit 0 of its second byte (`F9`: a variable block size) |
| the block size code | 4 bits | below |
| the sample rate code | 4 bits | below |
| the channel assignment | 4 bits | 0 to 7 for 1 to 8 independent channels, 8 to 10 for two coded as left-side, side-right or mid-side; 11 to 15 reserved |
| the bit depth code | 3 bits | below; 3 reserved |
| a reserved bit | 1 bit | 0 |
| the **coded number** | up to 7 bytes | a frame number in a fixed block size stream (the frames before it) and a sample number in a variable one, coded as UTF-8 is but up to 7 bytes |
| the block size | 8 or 16 bits | when its code says so |
| the sample rate | 8 or 16 bits | when its code says so |
| the **CRC-8** | 8 bits | of the header's bytes before it (polynomial `x^8 + x^2 + x + 1`, initial 0) |

The codes give:

| code | gives |
|---|---|
| block size 0 | reserved |
| block size 1 | 192 |
| block size `v`, 2 to 5 | `144 × 2^v` |
| block size 6 or 7 | the 8-bit (6) or 16-bit (7) value after the number, plus 1 |
| block size `v`, 8 to 15 | `2^v` |
| sample rate 0 | STREAMINFO's |
| sample rate 12, 13 or 14 | the value after the number, in kHz (12), Hz (13) or tens of Hz (14) |
| another sample rate code | one of eleven rates |
| bit depth 0 | STREAMINFO's |
| another bit depth code | 8, 12, 16, 20, 24 or 32 |

The footer's **CRC-16** (polynomial
`x^16 + x^15 + x^2 + 1`, initial 0) covers the frame's bytes before it, so
that the CRC-16 of the whole frame is 0. A frame's header gives no length:
its subframes end where their bits do.

A frame decodes alone: unlike MP3's or AAC's, its samples depend on no other
frame. Its **block size** `B` is the samples per channel it decodes to. In a
**fixed block size stream** every frame but the last has the same block
size, and the last may have fewer.

## 3. Tags

FLAC has no tags of its own beyond its Vorbis comments, but other tools add
MP3's: **ID3v2 tags** at the start of the file, before `fLaC`, and an **ID3v1
tag**, the last 128 bytes of the file when they start with `TAG`, measured
as [mp3.md §3](mp3.md#3-tags) says. The convention does not decode an ID3v2
tag's frames.

## 4. The layout

The hierarchy is a group at the root with an array at path `samples` and,
when the file has PICTURE blocks with data, a group `pictures` of arrays:
the root `zarr.json` is `{"zarr_format": 3, "node_type": "group",
"attributes": A}`, where `A` has `zarr_conventions` and `refkv_zarrified`
([§1](#1-declaration)) and nothing else.

### 4.1 The array

The stream's frames are numbered from 0 in file order; `N` is their count,
`B` the first frame's block size, `C` the channel count, `b` the bits per
sample and `T` the samples per channel, the frames' block sizes summed. The
array is a **sharded** array ([spec/zarrify.md §2.1](../zarrify.md#21-virtual-shards)):
each frame is an **inner chunk**, and each run of `F` frames a **shard**,
one key of the store. The **inner chunk shape** is `[B, C]`: inner chunk `j` is frame `j`.
The **shard shape** is `[F, 1]` inner chunks, with **`F = min(512, N)` frames per shard**:
shard `q`, at `samples/c/<q>/0`, holds the frames `q × F` to `min(N, (q + 1) × F) − 1`. The
**inner codecs** are `[{"name": "refkv.stream", "configuration": {"format": "flac", "setup":
S}}]` (a [`StreamConfiguration`](../conventions.md#type-streamconfiguration)), where `S` is
the base64 of the **setup**: `fLaC`, then the 4-byte header `80 00 00 22` (the last block,
STREAMINFO, 34 bytes), then the file's STREAMINFO body
([conventions §10](../conventions.md#10-coded-streams)).

<a id="type-flacarray"></a>**`FlacArray`**, the array's `zarr.json`: a
[`ShardedArray`](../zarrify.md#type-shardedarray), sharded as
[spec/zarrify.md §2.1](../zarrify.md#21-virtual-shards) says, of an unsharded array that is
otherwise [conventions §3](../conventions.md#3-arrays)'s
([`ArrayDocument`](../conventions.md#type-arraydocument), its fill value 0); these are its
members that differ from an `ArrayDocument`'s:

| member | type | presence | value |
|---|---|---|---|
| `shape` | array of integer | required | `[T, C]` |
| `data_type` | string | required | `"int8"` when `b ≤ 8`, `"int16"` when `b ≤ 16`, else `"int32"`: the smallest signed integer that holds a sample |
| `chunk_grid` | object | required | regular, its chunk shape the shard's, `[F × B, C]` |
| `codecs` | array of object | required | `[{"name": "sharding_indexed", "configuration": {"chunk_shape": [B, C], "codecs": [the inner codecs], "index_codecs": [{"name": "bytes", "configuration": {"endian": "little"}}, {"name": "crc32c"}], "index_location": "end"}}]`, its configuration a [`ShardingConfiguration`](../zarrify.md#type-shardingconfiguration) |
| `dimension_names` | array of string | required | `["time", "channel"]` |
| `attributes` | object | required | `{}` ([§1](#1-declaration)) |

Inner chunk `j` of the array, `[j × B, (j + 1) × B)` along `time`, is
frame `j` as a `refkv.stream` chunk of one frame
([conventions §10](../conventions.md#10-coded-streams), a
[`FlacInnerChunk`](#type-flacinnerchunk)): the header `01`,
the frame's length as a varint and `02` (its position 0, less −1, as a
zigzag varint), then the frame's bytes. Its samples fill its first rows: the
last frame's, when it holds fewer than `B`, are followed by the fill value,
past the array's end. A shard is its frames' inner chunks, one after the
other, then the shard index, whose entries past the last frame (in the last
shard only) are empty ([spec/zarrify.md §2.1](../zarrify.md#21-virtual-shards), a
[`Shard`](../zarrify.md#type-shard)).

So a reader that reads a few samples reads the shard index and the frames
that hold them, and nothing else; the store holds one key per `F` frames.
Since frames decode alone, no inner chunk needs another's frames.

### 4.2 The pictures

The `pictures` group, `{"zarr_format": 3, "node_type": "group",
"attributes": {}}`, present only when some PICTURE block's data is not
empty, holds for the `i`-th PICTURE block of the file, counted from 0, whose
picture data is not empty, the array `pictures/<i>`: the data, as **bytes**
([conventions §7](../conventions.md#7-source-values-as-arrays): `uint8`,
dimension `byte`; a [`SourceArray`](../conventions.md#type-sourcearray) of the
[bytes](../conventions.md#type-bytes) form). Its fields are in `pictures` of [§5.1](#51-the-root). A
picture whose media type is `-->` holds a URI, as text, in place of an image;
its array holds the URI's bytes.

### 4.3 The samples

Sample `i` of channel `c` is the `i`th sample of channel `c` that a FLAC
decoder ([RFC 9639](https://www.rfc-editor.org/rfc/rfc9639)) gives decoding the
frames one after the other: an integer of `b` bits, the recording's own, after
the stereo decorrelation is undone, as a signed integer of the array's data
type. A decoder that gives the samples in the high bits of a wider integer
(ffmpeg's: a 24-bit sample in the top 24 bits of 32) is shifted back. FLAC is
lossless, and decoding is exact: every conforming decoder gives these values,
and STREAMINFO's MD5, when it is not 0, is the MD5 of the array's samples,
interleaved, each little-endian in `ceil(b / 8)` bytes, sign-extended
([RFC 9639 §8.2](https://www.rfc-editor.org/rfc/rfc9639#section-8.2)).

The sample rate is `sample_rate` of [§5.1](#51-the-root); sample `i` is at
`i / sample_rate` seconds from the stream's start.

## 5. Source metadata

The root's source metadata `S` ([conventions §2](../conventions.md#2-attributes))
is a [`FlacRoot`](#type-flacroot), an object with the members of [§5.1](#51-the-root), in that order,
translated by [conventions §6](../conventions.md#6-source-metadata-as-json).
Everything the file holds is on `refkv_source`, the file's **IR mirror**
([§5.2](#52-the-ir-mirror)), from which the file is rebuilt byte for byte.

### 5.1 The root

<a id="type-flacroot"></a>**`FlacRoot`**, the root's `S`:

| member | type | presence | value |
|---|---|---|---|
| `sample_rate` | integer | required | STREAMINFO's, which every frame has ([§8.1](#81-the-stream)) |
| `channels` | integer | required | STREAMINFO's, likewise |
| `bits_per_sample` | integer | required | STREAMINFO's, likewise |
| `block_size` | integer | required | `B` |
| `frames` | integer | required | `N` |
| `samples` | integer | required | `T`, the samples per channel |
| `duration` | number or null | required | `T / sample_rate`, in seconds, as a number; `null` when the sample rate is 0 (allowed for data that is not audio) |
| `streaminfo` | [`FlacStreaminfo`](#type-flacstreaminfo) | required | the STREAMINFO block's fields |
| `blocks` | array of [`FlacBlock`](#type-flacblock) | required | one object per metadata block, in file order |
| `seek_table` | [`FlacSeekTable`](#type-flacseektable) | optional | when the file has a SEEKTABLE block |
| `vorbis_comment` | [`FlacVorbisComment`](#type-flacvorbiscomment) | optional | when the file has a VORBIS_COMMENT block |
| `cuesheets` | array of [`FlacCuesheet`](#type-flaccuesheet) | optional | when the file has CUESHEET blocks: one object per block, in file order (RFC 9639 does not limit them to one) |
| `pictures` | array of [`FlacPicture`](#type-flacpicture) | optional | when the file has PICTURE blocks: one object per block, in file order |
| `applications` | array of [`FlacApplication`](#type-flacapplication) | optional | when the file has APPLICATION blocks: one object per block |
| `id3v2` | array of [`Mp3Id3v2`](mp3.md#type-mp3id3v2) | optional | one object per ID3v2 tag at the start, as [mp3.md §5.1](mp3.md#51-the-root) writes it; absent when there is none |
| `id3v1` | [`Mp3Id3v1`](mp3.md#type-mp3id3v1) | optional | the ID3v1 tag's fields, as [mp3.md §5.1](mp3.md#51-the-root) writes them; absent when there is none |

<a id="type-flacstreaminfo"></a>**`FlacStreaminfo`**, the STREAMINFO block's fields
([§2](#2-the-stream)):

| member | type | presence | value |
|---|---|---|---|
| `min_block_size` | integer | required | the minimum block size |
| `max_block_size` | integer | required | the maximum block size |
| `min_frame_size` | integer | required | the minimum frame size |
| `max_frame_size` | integer | required | the maximum frame size |
| `sample_rate` | integer | required | the sample rate |
| `channels` | integer | required | the channel count |
| `bits_per_sample` | integer | required | the bits per sample |
| `total_samples` | integer | required | the total samples per channel |
| `md5` | string | required | `h`, the MD5 as 32 lowercase hexadecimal digits, as `md5sum` and `metaflac --show-md5sum` print it (not base64: [conventions §6](../conventions.md#6-source-metadata-as-json) allows a convention its own form) |

<a id="type-flacblock"></a>**`FlacBlock`**, a metadata block:

| member | type | presence | value |
|---|---|---|---|
| `type` | string or integer | required | its type's name (`"STREAMINFO"`, `"PADDING"`, `"APPLICATION"`, `"SEEKTABLE"`, `"VORBIS_COMMENT"`, `"CUESHEET"`, `"PICTURE"`), or its number for a reserved type |
| `size` | integer | required | its body's length |

<a id="type-flacseektable"></a>**`FlacSeekTable`**, the SEEKTABLE block's:

| member | type | presence | value |
|---|---|---|---|
| `points` | integer | required | its seek points, the placeholders not counted |
| `placeholders` | integer | required | its placeholders |

The points themselves are where the frames are, which the array's shard indexes
hold too: they are in the mirror (conventions, Reconstruction: the
layout may be left out).

<a id="type-flacvorbiscomment"></a>**`FlacVorbisComment`**, the VORBIS_COMMENT block's:

| member | type | presence | value |
|---|---|---|---|
| `vendor` | [`Text`](../conventions.md#type-text) | required | `V`, the vendor string, as a text value (UTF-8, or `{"latin1": ...}`) |
| `comments` | array of [`Text`](../conventions.md#type-text) | required | each field, in order and whole (`"TITLE=A tone"`), as text values. Names repeat, and their case differs from file to file, so the fields stay a list |

<a id="type-flaccuesheet"></a>**`FlacCuesheet`**, a CUESHEET block's:

| member | type | presence | value |
|---|---|---|---|
| `catalog` | [`Text`](../conventions.md#type-text) | required | the catalog number up to the first NUL, as a text value |
| `lead_in` | [`Integer`](../conventions.md#type-integer) | required | the lead-in samples |
| `cd` | boolean | required | the flags' bit 7: a CD |
| `tracks` | array of [`FlacCuesheetTrack`](#type-flaccuesheettrack) | required | the tracks, in order |

<a id="type-flaccuesheettrack"></a>**`FlacCuesheetTrack`**:

| member | type | presence | value |
|---|---|---|---|
| `offset` | [`Integer`](../conventions.md#type-integer) | required | its offset |
| `number` | integer | required | its number |
| `isrc` | [`Text`](../conventions.md#type-text) | required | its ISRC up to the first NUL, as a text value |
| `audio` | boolean | required | whether it is audio: its flags' bit 7 clear |
| `pre_emphasis` | boolean | required | its flags' bit 6 |
| `indexes` | array of [`FlacCuesheetIndex`](#type-flaccuesheetindex) | required | its index points, in order |

<a id="type-flaccuesheetindex"></a>**`FlacCuesheetIndex`**, an index point:

| member | type | presence | value |
|---|---|---|---|
| `offset` | [`Integer`](../conventions.md#type-integer) | required | its offset |
| `number` | integer | required | its number |

<a id="type-flacpicture"></a>**`FlacPicture`**, a PICTURE block's fields:

| member | type | presence | value |
|---|---|---|---|
| `type` | integer | required | the picture type |
| `media_type` | [`Text`](../conventions.md#type-text) | required | the media type |
| `description` | [`Text`](../conventions.md#type-text) | required | the description |
| `width` | integer | required | the width |
| `height` | integer | required | the height |
| `depth` | integer | required | the color depth |
| `colors` | integer | required | the indexed colors |
| `size` | integer | required | the data's length; the data is the array `pictures/<i>` ([§4.2](#42-the-pictures)), which a picture of size 0 does not have |

<a id="type-flacapplication"></a>**`FlacApplication`**, an APPLICATION block's:

| member | type | presence | value |
|---|---|---|---|
| `id` | [`Text`](../conventions.md#type-text) | required | its ID as text |
| `size` | integer | required | the length of its data, which the mirror holds |

`sample_rate` to `duration` describe the array, so that a reader need not
read STREAMINFO; the blocks' fields are there so that a reader finds the
recording's description, labels, cue points and pictures without decoding
the blocks.

### 5.2 The IR mirror

`refkv_source` is the file's **IR mirror**
([conventions §11.3](../conventions.md#113-the-ir-mirror)).
The IR's elements (the FLAC **source model**) are below; nothing is left
out: the tags, the marker, every metadata block and every frame.

### 5.3 Elements

Paths are relative to the IR's root, whose path is `""`
([conventions §8.1](../conventions.md#81-elements)). Types are those of
[conventions §8.6](../conventions.md#86-types); `n` is a tag's size or a
block's body's, `f` the length of a picture's fields and `d` of its data, `L`
a frame's length.

| path | kind | type | extent: what |
|---|---|---|---|
| `""` | struct | | the root: the whole file, `(0, size)` |
| `id3v2/<k>` | struct | | the `k`-th ID3v2 tag at the start, `k` from 0: the whole tag |
| `id3v2/<k>/header` | value | `{id:ascii[3],version:u1[2],flags:u1,size:u1[4]}` | its 10-byte header |
| `id3v2/<k>/data` | value | `bytes[n]` | its `n` bytes of frames and padding, when `n > 0`, opaque |
| `id3v2/<k>/footer` | value | `bytes[10]` | its footer, when it has one |
| `marker` | value | `ascii[4]` | `fLaC` |
| `metadata/<k>` | struct | | the `k`-th metadata block, `k` from 0: the whole block |
| `metadata/<k>/header` | value | `{type:u1,length:u1[3]}` | its 4-byte header |
| `metadata/<k>/streaminfo` | value | `{min_block_size:>u2,max_block_size:>u2,min_frame_size:u1[3],max_frame_size:u1[3],rate_channels_bits_samples:>u8,md5:bytes[16]}` | a STREAMINFO body |
| `metadata/<k>/padding` | value | `bytes[n]` | a PADDING body, when `n > 0` |
| `metadata/<k>/id` | value | `ascii[4]` | an APPLICATION block's ID |
| `metadata/<k>/data` | value | `bytes[n]` | an APPLICATION block's data, a PICTURE block's picture (`d` bytes), or a reserved type's body, when not empty, opaque |
| `metadata/<k>/seek_points` | value | `{sample:>u8,offset:>u8,samples:>u2}[n / 18]` | a SEEKTABLE body, when `n > 0` |
| `metadata/<k>/vorbis_comment` | value | `bytes[n]` | a VORBIS_COMMENT body, opaque ([§5.1](#51-the-root) decodes it) |
| `metadata/<k>/cuesheet` | value | `bytes[n]` | a CUESHEET body, opaque ([§5.1](#51-the-root) decodes it) |
| `metadata/<k>/fields` | value | `bytes[f]` | a PICTURE block's fields before its data, opaque ([§5.1](#51-the-root) decodes them) |
| `frames` | struct | | the frames: from the first's start to the last's end |
| `frames/<j>` | data | | frame `j`, `j` from 0, `(offset, L)`, header and footer included; its form's geometry is `{"samples": B, "channels": C, "data_type": D}` (`B` its block size, `D` the array's data type), its codec `{"name": "flac", "sample_rate": r, "bits_per_sample": b}`, and its recipe `[["src", 0, null]]` |
| `id3v1` | value | `{tag:ascii[3],title:cstr[30],artist:cstr[30],album:cstr[30],year:ascii[4],comment:cstr[30],genre:u1}` | the ID3v1 tag, `(size − 128, 128)`, when there is one |

The view ([conventions §8.7](../conventions.md#87-the-view)) shows each ID3v2
header, the marker, each block's header, STREAMINFO, an application's ID, a
seek table of at most 56 points, and the ID3v1 tag; opaque bytes are not
shown.

### 5.4 Equivalence

Hierarchies of one file are compared entry for entry, `refkv_source` included
([conventions §11.4](../conventions.md#114-equivalence)).

## 6. Example

A mono 16-bit stream at 16 kHz of 8 frames of 1024 samples (the last 832),
with no tags and no blocks but STREAMINFO, at `https://example.org/tone.flac`
(the fixture `fixtures/flac/flac_mono_16.flac`, 10209 bytes), has this root
`zarr.json`:

```json
{
  "zarr_format": 3,
  "node_type": "group",
  "attributes": {
    "zarr_conventions": [{
      "uuid": "e9108d07-b24b-432f-a8ce-09d80c8073b4",
      "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/zarrify/flac/schema.json",
      "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/flac.md",
      "name": "refkv_zarrified",
      "description": "The Zarr layout of a FLAC source zarrified by refkv, and the source's metadata"
    }],
    "refkv_zarrified": {
      "profile": "flac",
      "version": 0,
      "source": {"url": "https://example.org/tone.flac"},
      "flac": {
        "sample_rate": 16000,
        "channels": 1,
        "bits_per_sample": 16,
        "block_size": 1024,
        "frames": 8,
        "samples": 8000,
        "duration": 0.5,
        "streaminfo": {
          "min_block_size": 1024,
          "max_block_size": 1024,
          "min_frame_size": 1074,
          "max_frame_size": 1305,
          "sample_rate": 16000,
          "channels": 1,
          "bits_per_sample": 16,
          "total_samples": 8000,
          "md5": "603c633f2f9016a27b9f3a74bdf814d9"
        },
        "blocks": [{"type": "STREAMINFO", "size": 34}]
      }
    }
  }
}
```

Its array `samples/zarr.json` has the shape `[8000, 1]`, the data type
`int16`, the chunk shape `[8192, 1]` (one shard of 8 frames), and the codecs
`[{"name": "sharding_indexed", "configuration": {"chunk_shape": [1024, 1],
"codecs": [{"name": "refkv.stream", "configuration": {"format": "flac",
"setup": "ZkxhQ4AAACIEAAQAAAQyAAUZA+gA8AAAH0BgPGM/L5AWonufOnS9+BTZ"}}],
"index_codecs": [{"name": "bytes", "configuration": {"endian": "little"}},
{"name": "crc32c"}], "index_location": "end"}}]`. The one shard,
`samples/c/0/0`, is 17 ranges: for each frame a 4-byte literal header (the
first's `01 92 0A 02`: one frame, of 1298 bytes, at position 0) and the
frame's range of source 0 (the first's `(42, 1298)`, right after the
STREAMINFO block), then a 132-byte literal, the index (8 entries, the first
`(0, 1302)`) and its CRC-32C.

## 7. What the layout does not express

This section is informative.

- **Variable block sizes.** A stream whose frames hold different sample
  counts has no regular grid of frames; the profile rejects it
  ([§8.1](#81-the-stream)).
- **The channel layout.** The channels are in the order of the frames'
  channel assignment ([RFC 9639 §9.1.3](https://www.rfc-editor.org/rfc/rfc9639#section-9.1.3)),
  or as a `WAVEFORMATEXTENSIBLE_CHANNEL_MASK` Vorbis comment says; the
  convention keeps the comment and does not apply it.
- **The seek table's points**, the reserved bytes of a cue sheet and the
  frames' per-frame coding (channel decorrelation, predictors) are in the
  mirror, not in the root.
- **Pictures as images.** A picture is kept as its bytes, not decoded.

# Part 2. The profile

The FLAC profile of [spec/zarrify.md](../zarrify.md) is [§8](#8-flac-profile) of this document. What every profile's Part 2 shares is in
[spec/zarrify.md, Profiles](../zarrify.md#profiles).

## 8. FLAC profile

### 8.1 The stream

The input is a FLAC file, which its **signature** ([spec/zarrify.md §1.2](../zarrify.md#12-input), tested at the precedence of
[profiles/flac.json](profiles/flac.json)) chose: `H` holds the bytes `66 4C 61 43`
(`fLaC`) at the offset `A` after the ID3v2 tags at its start (0 without one),
each tag `ID3`, its size bytes each below `80`, measured as
[the convention §3](#3-tags) says. Its precedence, 95, is before MP3's, whose
signature also takes a file that starts with `ID3`. A FLAC file whose ID3v2
tags end past `H` (552 bytes) does not match: the MP3 profile's signature
then takes it, and that profile rejects it; naming the FLAC profile
(`FlacSource`, [spec/zarrify.md §1.7](../zarrify.md#17-named-inputs-and-layouts)) reads it.

**Reads.** A frame's header gives no length, and no index records every
frame's offset: a SEEKTABLE holds a few points, a frame in each run of
seconds, and a file need not have one. So the profile reads every frame's
bytes, as the MP3 profile does, and finds each frame's end by its CRC-16
(below). It reads in rounds, so that it holds at most 16 MiB of frames at a
time: the file's first 64 KiB and its last 128 bytes; then, while the
metadata blocks run past what it holds, the 64 KiB from each block header
it does not hold, and of each block's body what the profile decodes
(STREAMINFO, SEEKTABLE, VORBIS_COMMENT and CUESHEET whole, an APPLICATION
block's ID, a PICTURE block's fields up to its data); then the frames, 16 MiB
at a time. Bytes read per file: every frame, the metadata blocks but the
data of pictures, padding and applications past the 64 KiB windows, the
ID3v2 headers, and the last 128 bytes; in `1 + k + ceil(M / 16 MiB)` rounds
at most, `k` the block headers past the 64 KiB windows read before them and
`M` the frames' bytes. A 3-minute stereo track at 44.1 kHz, about 20 MiB of
frames after a 1 MiB cover picture, is read in 4 rounds, its picture's data
unread but the first 64 KiB.

**Checks.** The input is rejected unless all of these hold, checked in this
order:

- **ID3v2 tags:** for each tag at the start, as
  [mp3.md §8.1](mp3.md#81-the-stream) checks them (its header within the
  file, syncsafe size bytes, the tag within the file), with its messages.
- **Marker:** `fLaC` at `A`, before `E`, where `E` is the file's size, or
  its size less 128 when the last 128 bytes, at or after `A`, start with
  `TAG` (the ID3v1 tag): `no fLaC marker at <A>`.
- **Metadata blocks:** from `A + 4`, each block, up to the one whose header
  has bit 7 set: its header is before `E`; its type is not 127; the first
  block is STREAMINFO, and no STREAMINFO, SEEKTABLE or VORBIS_COMMENT block
  follows one of its type; a STREAMINFO body is 34 bytes, a SEEKTABLE body a
  multiple of 18, an APPLICATION body at least 4; the block ends at or before
  `E`; STREAMINFO's bits per sample are at least 4; and a VORBIS_COMMENT,
  CUESHEET or PICTURE body is its fields exactly, no length past its end and
  no byte after its last field (`the PICTURE block at 42 is malformed: its
  fields and data do not fit its body`).
- **The first frame:** the frames start right after the last block, at
  `P0`: `P0 < E` (`no audio frames: ...`), and a valid frame header starts
  there (`the frame header at 42 is not valid: no frame sync code`; `...: a
  CRC-8 that does not match`): the sync code, no reserved or forbidden code
  ([the convention §2](#2-the-stream)), a coded number of the extended UTF-8
  form, and its CRC-8.
- **Each frame:** its blocking strategy is fixed (`a variable block size
  (the frame at 42) is not supported: only a fixed block size stream is`);
  its sample rate, channel count and bit depth, each its own or, for code
  0, STREAMINFO's, are STREAMINFO's (`the frame at 180 changes the sample
  rate from 8000 to 16000`); its coded number is its index; it holds no more
  samples than the first frame; and a frame that another follows holds as
  many as the first (`only the last frame may hold fewer`).
- **Each frame's end** (below) is found within 16 MiB of its start (`the
  frame at 3939 (frame 3) has no end: ...`).
- **Total:** when STREAMINFO's total samples are not 0, the frames hold
  that many samples per channel (`the frames hold 5120 samples per channel,
  not STREAMINFO's 8000`: a stream cut at a frame's end).

**Frames.** Frame 0 starts at `P0`. Frame `j`, starting at `p`, ends at the
first `q > p + h + 2` (`h` its header's length) at which the CRC-16 of the
bytes `[p, q)` is 0 and either `q = E` or a valid frame header starts at `q`;
frame `j + 1` starts at `q`, and its checks (above) require its number to be
`j + 1`. The last frame is the one that ends at `E`. A frame that has no such
`q` (a byte of it changed, the stream cut inside it, the next header's CRC-8
broken) rejects the input.

So a frame is found by two checks at once: a CRC-16 of 0 over the frame (the
chance that a wrong end passes it is 2^−16) and a valid header after it, its
sync code and its CRC-8 (2^−8): a sync code inside a frame's audio data,
which FLAC does not prevent, is not taken for a frame. Since the CRC-16 of a
run of whole frames is 0 at the end of each, a header numbered wrongly ends
the frame before it (and rejects the input) rather than making it run on to
the next header that matches. There are no bytes between
frames, nor after the last but the ID3v1 tag: a stream with junk there has
no last frame. The frames' bytes are read to find the frames and check their
CRCs, not decoded: the output is the frames' offsets and lengths, which are
the stream's structure ([spec/zarrify.md §1.2](../zarrify.md#12-input)), and
a stream whose CRCs fail is rejected, as a decoder would stop.

**Why fixed block sizes only.** Zarr's chunk grids are regular, and
sharding's inner chunks are of one shape: a frame is an inner chunk only
when every frame holds `B` samples. Chunks of whole frames (the MP3
profile's layout) need that too: a chunk of `k` frames covers `k × B`
samples only when the frames hold `B` each, and a frame across a chunk
boundary would be decoded by two chunks and placed by neither. So a variable
block size stream, rare (libFLAC and ffmpeg write fixed block sizes), is
rejected by name rather than given another layout.

### 8.2 Shards

Inner chunk `j` ([the convention §4.1](#41-the-array)), frame `j` of length
`L` at `o`, is the reference of two ranges: a literal range of its
`refkv.stream` header (`01`, `varint(L)`, `02`) and the frame's range of
source 0, `(o, L)`:

<a id="type-flacinnerchunk"></a>**`FlacInnerChunk`**, inner chunk `j`, frame `j` of length `L`
at `o`:

| part | range | presence | value |
|---|---|---|---|
| the header | literal | required | the [`StreamChunk`](../conventions.md#type-streamchunk) header of one frame: `01`, `varint(L)`, `02` (its position 0, less −1, as a zigzag varint) |
| the frame | source range `(0, o, L)` | required | the frame's bytes |

The array is sharded as
[spec/zarrify.md §2.1](../zarrify.md#21-virtual-shards) says, `F = min(512, N)`
inner chunks a shard: the array has `ceil(N / F)` shards, each present, and
shard `q`'s reference, a [`Shard`](../zarrify.md#type-shard) of the shard shape `[F, 1]`, is
the headers and ranges of its frames in order, then
the literal index, `16 × F + 4` bytes. No ranges are joined, with or
without checksums: a header lies between each two frames.

A shard's payload is at most `41 × F + 12` bytes for frames of less than
2^28 bytes at offsets below 2^49 (each frame's literal header part at most 10
bytes, its source range part at most 15 and its index entry 16, and the
index's part at most 12 bytes besides its entries): 21 004 bytes for
`F = 512`, and with checksums, each source range part up to 6 bytes longer,
24 076, within the 32 KiB that shards are sized to and the 65 519 that a
reference may hold ([spec/zarrify.md §1.2](../zarrify.md#12-input)). A
reader of a few frames reads the shard's index, 8 KiB, and those frames.

The pictures' arrays ([the convention §4.2](#42-the-pictures)) are cut as
bytes are ([conventions §7](../conventions.md#7-source-values-as-arrays)), each
chunk a range of source 0 (the last chunk's zero padding a literal range):

<a id="type-flacpicturechunk"></a>**`FlacPictureChunk`**, chunk `k` of the array of a picture
whose data, `d` bytes, starts at `s`, in chunks of `c` bytes, with `m = min(c, d − k × c)`:

| part | range | presence | value |
|---|---|---|---|
| the data | source range `(0, s + k × c, m)` | required | the chunk's bytes of the picture's data |
| the padding | literal | optional | when `m < c` (the last chunk): `c − m` zero bytes |

The ranges lie within the file. The output has no data sources: its source
table is source 0 alone. `refkv_source` is the IR mirror
([the convention §5.2](#52-the-ir-mirror)). The archive has no other entries
than the root's, the array's and the pictures' `zarr.json`, the shards, the
pictures' chunks, and the mirror's.

### 8.3 Summary

This section is informative. Both implementations print a one-line JSON
summary, with these members:

| member | value |
|---|---|
| `sampleRate` | the sample rate |
| `channels` | the channel count |
| `bitsPerSample` | the bits per sample |
| `blockSize` | `B` |
| `frames` | `N` |
| `samples` | `T` |
| `duration` | in seconds |
| `shardFrames` | `F` |
| `shards` | the array's shards |
| `pictures` | the PICTURE blocks |
| `tags` | those of `"id3v2"` and `"id3v1"` the file has, in that order |
| `joinRanges` | whether ranges adjacent across inner chunks would be joined: not when the zarrifier records checksums, [spec/zarrify.md §2.1](../zarrify.md#21-virtual-shards) |
