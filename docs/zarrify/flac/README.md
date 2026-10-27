# FLAC

refkv turns a file into a Zarr dataset by writing a small archive of
references to the file's bytes, without copying its content
([overview](../README.md)). This page is about FLAC files: lossless audio,
which refkv presents as an array of the recording's own integer samples.

## Who this is for

FLAC (`.flac`, [RFC 9639](https://www.rfc-editor.org/rfc/rfc9639)) is how
sound is archived when no information may be lost: music masters and CD
rips, field recordings, bioacoustics and speech corpora. Someone who wants
to read a few seconds of a long recording lazily over HTTP as a NumPy array,
exactly as recorded, put recordings next to other arrays in a Zarr store, or
run an array pipeline over audio without decoding every file to WAV first
has to convert it today.

A FLAC file is a few metadata blocks (STREAMINFO, with the stream's MD5;
Vorbis comments; pictures; a cue sheet; a seek table), then frames, each a
block of samples coded on its own. refkv reads the file once, finds every
frame by its CRCs, and writes an archive in which the array is **sharded**:
each frame is an inner chunk of a Zarr shard of up to 512 frames, and the
shard's index, which refkv computes, tells a reader where each frame is. A
reader of a few seconds reads the index and those frames, nothing else; the
`refkv.stream` codec decodes a frame when it is read.

## What you get

A group at the archive root with the array `samples` and, when the file has
pictures, the group `pictures`.

- **Shape:** `[samples, channels]`, with the dimension names `time` and
  `channel`.
- **Data type:** `int8`, `int16` or `int32`, the smallest that holds the
  stream's bits per sample (8, 16, 24, or any from 4 to 32): the samples
  exactly as recorded. Their MD5 is STREAMINFO's, as `metaflac
  --show-md5sum` prints it.
- **Chunks:** one frame each (4096 samples for most encoders), in shards of
  up to 512 frames (`sharding_indexed`, with a CRC-32C'd index), the last
  frame usually shorter.
- **Codecs:** `refkv.stream` inside `sharding_indexed`; zarr finds it
  through refkv's entry points, and it decodes with ffmpeg's FLAC decoder through
  PyAV (the `media` extra).
- **Pictures:** `pictures/<i>`, the `i`-th PICTURE block's data as bytes
  (`uint8`): a cover art PNG or JPEG, referenced, not copied.
- **Metadata:** under `refkv_zarrified.flac` on the root: the sample rate,
  channels, bits per sample, block size, frame and sample counts and
  duration; STREAMINFO's every field (its MD5 as hex); each metadata
  block's type and size; the Vorbis comments, whole and in order
  (`TITLE=...`); the cue sheet's tracks and index points; each picture's
  type, media type, description and size; the seek table's counts; the
  ID3v2 and ID3v1 tags some tools add. The whole file is described on
  `refkv_source` by the Rust core's IR mirror, from which it is rebuilt
  byte for byte.
- **No `ome` metadata:** the hierarchy is not an image; zarr-python, xarray
  and any Zarr v3 reader with the codec read it.

**What is not decoded.** The channel layout a
`WAVEFORMATEXTENSIBLE_CHANNEL_MASK` comment gives is kept, not applied; a
picture is kept as its bytes; an ID3v2 tag's frames are not translated.
[spec/zarrify/flac.md §7](../../../spec/zarrify/flac.md#7-what-the-layout-does-not-express)
lists them.

## Try it

[example.py](example.py) zarrifies a public file and plots it, and its cover,
with matplotlib. Its dependencies are in its header (PEP 723), so it runs on its
own (uv builds refkv from GitHub, which needs a Rust toolchain, until a release
on PyPI has `refkv.zarrify`):

```bash
uv run docs/zarrify/flac/example.py
```

A file of the IETF CELLAR working group's FLAC decoder testbench (CC0), 24-bit
stereo at 96 kHz, from
[corpus_flac.txt](../../../conformance/zarrify/corpus_flac.txt):

```bash
uv run refkv zarrify "https://raw.githubusercontent.com/ietf-wg-cellar/flac-test-files/main/subset/28%20-%20high%20resolution%20audio%2C%20default%20settings.flac" hires.vzip
```

```
{"format": "flac", "sampleRate": 96000, "channels": 2, "bitsPerSample": 24, "blockSize": 4096, "frames": 205, "samples": 838036, "duration": 8.729541666666666, "shardFrames": 205, "shards": 1, "pictures": 0, "tags": [], ...}
```

The archive is 21 KB: one shard's reference (a 4-byte header and a range
per frame, and the 3.3 KB index) and the mirror's table of 205 frames. Read
the recording:

```python
import zarr
import refkv.zarr

g = zarr.open_group(refkv.zarr.open("hires.vzip"), mode="r")
m = g.attrs["refkv_zarrified"]["flac"]
print({k: m[k] for k in ("sample_rate", "channels", "bits_per_sample", "duration")})
print(m["streaminfo"]["md5"], m["vorbis_comment"])
a = g["samples"]
print(a.shape, a.dtype, a.chunks, a.shards)
print(a[96000 * 5:96000 * 5 + 6, 0])
```

```
{'sample_rate': 96000, 'channels': 2, 'bits_per_sample': 24, 'duration': 8.729541666666666}
8b646375d4ccf5c08e659d15098d75b8 {'vendor': 'reference libFLAC 1.3.2 20170101', 'comments': ['Comment=Processed by SoX']}
(838036, 2) int32 (4096, 2) (839680, 2)
[828537 868888 861791 840096 852868 866102]
```

The last line reads six samples five seconds in: one request of the one
frame that holds them (17 KB of the 3.4 MB file), decoded on the fly.

**In the browser.** The service worker serves a zarrified FLAC as a Zarr
hierarchy like any other; the demo page does not display audio.

## Supported and not supported

Supported:

- Fixed block size streams (what libFLAC and ffmpeg write), 1 to 8
  channels, 4 to 32 bits per sample, any sample rate, every channel
  decorrelation, frames whose headers defer to STREAMINFO, uncommon block
  sizes and sample rates; all 59 fixed block size files of the IETF
  decoder testbench decode to their MD5.
- Every metadata block type, reserved ones kept opaque; ID3v2 tags before
  `fLaC` and an ID3v1 tag at the end.

Rejected:

- A variable block size stream (`a variable block size (the frame at 8264)
  is not supported: only a fixed block size stream is`): Zarr's grids are
  regular, and its frames do not fall on one.
- A stream whose frames change the sample rate, channel count or bit depth,
  are numbered out of order, or hold fewer samples than the first before the
  last.
- A corrupt or truncated stream: a frame whose CRC-16 fails, a header whose
  CRC-8 fails, a stream cut inside a frame or at a frame's end (when
  STREAMINFO gives the total), junk after the last frame.
- Malformed metadata: no `fLaC`, no STREAMINFO first, a block past the end of
  the file, a Vorbis comment, cue sheet or picture whose lengths do not add
  up.
- A file whose ID3v2 tags end past its first 552 bytes is taken for an MP3
  by its first bytes, and rejected; name its profile (`FlacSource`) to read it.

## Performance

- Zarrifying reads every frame (no index records where each one is), in
  windows of 16 MiB, but not the data of pictures, padding or application
  blocks past the first 64 KiB windows. The 3.4 MB file above takes two
  requests and under a second over HTTPS.
- Reading samples is one request of the frames that hold them, after the
  shard's index (at most 8 KB) is read once.
- The archive has one entry per 512 frames, each at most about 21 KB (a
  4-byte header and a range per frame, and 16 bytes of index per frame), and
  the mirror's table has a row per frame.
- zarr-python's sharding codec merges the byte ranges of a read whose gaps
  are under 1 MiB (`array.sharding_coalesce_max_gap_bytes`); frames are
  contiguous, so a read of a run of samples asks for no bytes it does not
  use.

## How it's verified

`js/test/flac/verify.py` zarrifies the synthetic FLAC files (including
inputs to reject) with the browser code, reads them back through
`python/src/refkv`, zarr-python's sharding codec and the `refkv.stream`
codec, and checks the samples against STREAMINFO's MD5, libsndfile's
decoding of the whole file (through soundfile, for 8, 16 and 24 bits) and
ffmpeg's. `python/tests/ir/test_flac.py` does the same through Python, with
shards of 3 frames as well, and checks that a read of a few samples reads
only their frames.

- Profile: [spec/zarrify/flac.md](../../../spec/zarrify/flac.md#part-2-the-profile)
- Shards: [spec/zarrify.md §2.1](../../../spec/zarrify.md#21-virtual-shards)
- Rust core: [rust/refkv-ir/src/flac.rs](../../../rust/refkv-ir/src/flac.rs)
- Codec: [python/src/refkv/zarr/codecs/stream.py](../../../python/src/refkv/zarr/codecs/stream.py)
- Fixtures: [fixtures/flac/](../../../fixtures/flac/)
