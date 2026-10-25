# MP4

refkv turns a file into a Zarr dataset by writing a small archive of
references to the file's bytes, without copying its content
([overview](../README.md)). This page is about MP4 and QuickTime files
(`.mp4`, `.m4a`, `.mov`): video and audio, which, like MP3, are not images.

## Who this is for

H.264 in MP4 is how most video is recorded and shared, scientific video
included: behavior recordings from lab cameras (the input of pose
estimation), microscope and field recordings, the external video files an
NWB file points to. Someone who wants to read a few seconds of a long
recording as a NumPy array, over HTTP, without decoding the whole file or
converting it to images first, has to write that machinery today.

An MP4 file indexes every coded frame (its offset, size, timestamp and
whether it is a keyframe) in its `moov` box, so refkv reads that box alone,
never the media, and writes an archive in which every Zarr chunk is a run
of frames, referenced together with the frames before it that a decoder
needs: back to the keyframe before it, for video. The archive's
`refkv.stream` codec decodes a chunk when it is read.

## What you get

A group at the archive root, and a group per track refkv zarrifies, at
`tracks/<id>`.

- **Video (H.264, 8-bit 4:2:0):** three arrays, `y`, `u` and `v`, the
  decoder's planes, `uint8`, `[time, y, x]` (the chroma planes half the
  size along each axis), in presentation order; and `time`, each
  picture's presentation time in the track's timescale (`int64`). Each
  chunk is whole groups of pictures from a keyframe, up to 256 pictures
  and at most 32 MiB of luma (16 pictures at 1080p); only a group longer
  than that is split. Chunks of different lengths use the zarr-extensions
  `rectilinear` chunk grid, which `refkv.zarr` turns on in zarr-python.
  No color conversion: RGB depends on the stream's color matrix, which is
  the reader's to apply.
- **Audio (AAC-LC, MP3):** `samples`, `float32`, `[time, channel]`, the
  decoder's output, in chunks of 256 frames.
- **Codecs:** `refkv.stream` (refkv's `zarr` module registers it), which
  decodes with ffmpeg's decoders through PyAV (the `media` extra).
- **Metadata:** under `refkv_zarrified.mp4` on the root, the brands, the
  movie's timescale and duration, and every track: its codec, its path,
  or why it is not zarrified (`hev1 video is not supported: only H.264
  (avc1) is`). Each track's group holds its timescale, edit list,
  dimensions or sample rate and channel count, and the H.264 profile and
  level. The whole file (every box, field by field, every sample, and the
  media data in no sample) is described on `refkv_source` by the Rust
  core's IR mirror, from which it is rebuilt byte for byte.

**What is not expressed.** The edit list, which places each track on the
movie's timeline (delaying it, or cutting the encoder's priming), is
recorded, not applied: the arrays hold every decoded picture and sample.
HEVC, AV1, 10-bit H.264 and other codecs' tracks are listed with the reason
and kept in the mirror, as are `udta` and `meta` boxes (titles, tags).
[spec/zarrify/mp4.md §7](../../../spec/zarrify/mp4.md#7-what-the-layout-does-not-express)
lists them.

## Try it

[example.py](example.py) zarrifies a public file and plots it with matplotlib. Its
dependencies are in its header (PEP 723), so it runs on its own (uv builds refkv from GitHub,
which needs a Rust toolchain, until a release on PyPI has `refkv.zarrify`):

```bash
uv run docs/zarrify/mp4/example.py
```

*About Bananas* (1935), from the Prelinger Archives on the Internet Archive
(public domain, 46 MB), from
[corpus_mp4.txt](../../../conformance/zarrify/corpus_mp4.txt):

```bash
uv run refkv zarrify https://archive.org/download/AboutBan1935/AboutBan1935_512kb.mp4 bananas.vzip
```

```
{"format": "mp4", "brand": "isom", "tracks": [{"id": 1, "format": "h264", "path": "tracks/1", "frames": 19901, "chunkFrames": 256, "chunks": 80}, {"id": 2, "format": "aac", "path": "tracks/2", "frames": 28567, "chunkFrames": 256, "chunks": 112}], "skipped": [], ...}
```

It takes under 4 seconds, reading the `moov` box alone; the archive is
2.4 MB. Read a picture and its time:

```python
import zarr
import refkv.zarr

g = zarr.open_group(refkv.zarr.open("bananas.vzip"), mode="r")
video = g["tracks/1"]
m = video.attrs["refkv_zarrified"]["mp4"]
print({k: m[k] for k in ("width", "height", "frames", "timescale", "sync_samples")})
y = video["y"][5000]
print(y.shape, y.dtype, video["time"][5000] / m["timescale"])
print(g["tracks/2/samples"].shape)
```

```
{'width': 320, 'height': 240, 'frames': 19901, 'timescale': 2997, 'sync_samples': 1660}
(240, 320) uint8 166.8335001668335
(29252608, 2)
```

Picture 5000 is ffmpeg's decoding of it, value for value. Reading it
fetches and decodes its chunk, 21 groups of 12 pictures from a keyframe, in
about 2 seconds over HTTP.

**In the browser.** The service worker serves a zarrified MP4 as a Zarr
hierarchy like any other, and the demo page opens each H.264 track's luma
(its Y plane) in Neuroglancer, scrolled over time: the Neuroglancer fork
decodes `refkv.stream` chunks with the browser's WebCodecs `VideoDecoder`.
The demo's MP4 example is a SLEAP test video of two flies.

## Supported and not supported

Supported:

- MP4 and QuickTime files with the `moov` box before or after the media,
  64-bit box sizes and a media box that runs to the end of the file.
- H.264 (`avc1`) video in any profile that is 8-bit 4:2:0, with or without
  B-frames, at a constant or variable frame rate, any number of tracks.
- AAC-LC and MP3 audio tracks, mono or stereo, in either container.

Not zarrified (listed with the reason, and kept in the mirror): HEVC,
AV1 and other video codecs, `avc3` (parameter sets in the stream), 10-bit,
4:2:2 and 4:4:4 H.264, open groups of pictures, groups of more than 1024
pictures, HE-AAC and other audio codecs, timecode and text tracks.

Rejected: fragmented MP4 (`moof` boxes), files without a `moov` box, files
with none of the tracks above, and malformed boxes or sample tables.

## Performance

- Zarrifying reads the first 64 KiB, each top-level box header after it,
  and the `moov` box: for a file whose `moov` box is at the end, three or
  four requests.
- Reading a video chunk is one request per run of its pictures in the file
  (the audio interleaved between them splits them) and decoding its
  pictures. A chunk starts at a keyframe, unless it splits a group longer
  than a chunk: then it also decodes the pictures back to the group's
  keyframe. A smaller read costs the same: Zarr decodes whole chunks.
- Decoding holds the chunk and the decoder's reference pictures, never the
  lead-in's pictures, and decodes Y, U and V once: reading U and V after Y
  takes the planes the decoding of Y kept.
- The archive holds a reference per chunk, with a range per picture when
  the audio is interleaved, for each of the three planes, and about 4
  bytes per frame of header: 2.4 MB for the 46 MB file above, two thirds of
  it the planes' references, the same for `y`, `u` and `v`.

## How it's verified

`js/test/mp4/verify.py` zarrifies the synthetic MP4 and MOV files
(including inputs to reject) with the browser code, reads every track
through `python/src/refkv`, zarr-python and the `refkv.stream` codec, and
compares it with ffmpeg's decoding of the file (through PyAV, whose demuxer
reads the tables itself): every plane of every picture, the presentation
times and every audio sample, exactly. `python/tests/ir/test_mp4.py` does
the same through Python, also with chunks of 7 frames, so that every track
has several chunks and lead-ins. AAC coded with noise substitution is
compared by its level, since a chunk decodes its noise bands from a
generator of its own.

- Profile: [spec/zarrify/mp4.md](../../../spec/zarrify/mp4.md#part-2-the-profile)
- Codec: [conventions §10](../../../spec/conventions.md#10-coded-streams),
  [python/src/refkv/zarr/codecs/stream.py](../../../python/src/refkv/zarr/codecs/stream.py)
- Rust core: [rust/refkv-ir/src/mp4.rs](../../../rust/refkv-ir/src/mp4.rs)
- Fixtures: [fixtures/mp4/](../../../fixtures/mp4/)
