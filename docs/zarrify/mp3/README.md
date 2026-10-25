# MP3

refkv turns a file into a Zarr dataset by writing a small archive of
references to the file's bytes, without copying its content
([overview](../README.md)). This page is about MP3 files: the one format
refkv zarrifies that is not an image.

## Who this is for

MP3 (`.mp3`: MPEG-1, MPEG-2 and MPEG-2.5 Layer III audio) is how a great
deal of recorded sound is stored and shared: speech corpora, field
recordings, podcasts, audiobooks, music. Someone who wants to read a
minute of a long recording lazily over HTTP as a NumPy array, put a
collection of recordings next to other arrays in a Zarr store, or run an
array pipeline over audio without first decoding every file to PCM on disk
has to convert it today.

An MP3 file is a sequence of frames, each decoding to 1152 (MPEG-1) or 576
samples per channel, with tags before and after them. refkv reads the file
once, finds every frame, and writes an archive in which every Zarr chunk is
a run of 256 frames, referenced together with the few frames before it that
a decoder needs (MP3 frames borrow bits from their predecessors). The
archive's `refkv.stream` codec decodes a chunk when it is read, so the
samples are never stored: they stay in the file as MPEG frames.

## What you get

A group at the archive root with one array, `samples`.

- **Shape:** `[samples, channels]`, with the dimension names `time` and
  `channel`: one row per sample, one column per channel (1 or 2).
- **Data type:** `float32`, the decoder's own output. The array holds
  every sample the frames decode to, the encoder's delay and padding
  included (their counts are in the metadata, for a reader that wants to
  trim them, as players do).
- **Chunks:** 256 frames each (294912 samples for MPEG-1, 147456 for
  MPEG-2 and 2.5; 6.7 seconds at 44.1 kHz, 6.7 at 22.05), the last one
  shorter. Reading a chunk reads its frames and a few before them (the
  bit reservoir) in one request, and decodes them in one pass.
- **Codecs:** `refkv.stream`, which refkv's `zarr` module registers and
  which decodes with ffmpeg's float MP3 decoder through PyAV (the `media`
  extra); MP4 tracks use it too. No other codec.
- **Metadata:** under `refkv_zarrified.mp3` on the root: the stream's
  version, sample rate, channel count and mode, frame and sample counts,
  duration and bitrate (constant or variable), whether frames carry CRCs;
  the Xing or Info frame's frame and byte counts, encoder (`LAME3.99r`),
  delay and padding; the ID3v2 tags' versions and sizes; the ID3v1 tag's
  title, artist, album, year, comment, track and genre; an APE tag's
  version and item count. The whole file (the tags, the info frame, every
  frame and any bytes between them) is described on `refkv_source` by the
  Rust core's IR mirror, from which it is rebuilt byte for byte.
- **No `ome` metadata:** the hierarchy is not an image, so OME-Zarr
  viewers do not open it; zarr-python, xarray and any Zarr v3 reader with
  the codec do.

**What is not decoded.** The ID3v2 tag's frames (title, cover art, lyrics)
and an APE tag's items are kept whole in the mirror, not translated; the
samples a decoder gives are not bit-exact across decoders (the standards
allow a difference of a few units); a stream whose frames borrow bits from
frames that are not there (two files joined) decodes as the reader's
decoder has it. [spec/zarrify/mp3.md §7](../../../spec/zarrify/mp3.md#7-what-the-layout-does-not-express)
lists them.

## Try it

[example.py](example.py) zarrifies a public file and plots it with matplotlib. Its
dependencies are in its header (PEP 723), so it runs on its own (uv builds refkv from GitHub,
which needs a Rust toolchain, until a release on PyPI has `refkv.zarrify`):

```bash
uv run docs/zarrify/mp3/example.py
```

A LibriVox recording from the Internet Archive (chapter 20 of Walter
Lippmann's *Public Opinion*, public domain, 3.9 MB), from
[corpus_mp3.txt](../../../conformance/zarrify/corpus_mp3.txt):

```bash
uv run refkv zarrify https://archive.org/download/public_opinion_1312_librivox/publicopinion_20_lippmann_64kb.mp3 lippmann_20.vzip
```

```
{"format": "mp3", "version": "MPEG-2", "sampleRate": 22050, "channels": 1, "frames": 18576, "samples": 10699776, "duration": 485.250612244898, "bitrate": 64, "chunkFrames": 256, "chunks": 73, "info": "Info", "tags": ["id3v2", "id3v1"], "strayBytes": 0, ...}
```

The archive is 167 KB: 73 chunk references, each with a header of about 4
bytes a frame that gives the codec each frame's size and place (the
archive stores it twice, in the reference and in its entry), and the
mirror's table of 18576 frames. Read the recording:

```python
import zarr
import refkv.zarr

g = zarr.open_group(refkv.zarr.open("lippmann_20.vzip"), mode="r")
m = g.attrs["refkv_zarrified"]["mp3"]
print({k: m[k] for k in ("version", "sample_rate", "channels", "duration")})
print(m["info"])
print(m["id3v1"])
a = g["samples"]
print(a.shape, a.dtype, a.chunks)
print(a[22050 * 60:22050 * 60 + 8, 0])
```

```
{'version': 'MPEG-2', 'sample_rate': 22050, 'channels': 1, 'duration': 485.250612244898}
{'tag': 'Info', 'frames': 18576, 'bytes': 3882212, 'quality': 57, 'encoder': 'LAME3.99r', 'delay': 576, 'padding': 1128}
{'title': '20 - A New Image', 'artist': 'Walter Lippmann', 'album': 'Public Opinion', 'year': '', 'comment': 'http://archive.org/details/p', 'track': 20, 'genre': 101}
(10699776, 1) float32 (147456, 1)
[ 0.000338 -0.000075  0.000176  0.000071  0.000034  0.000458  ...]
```

The last line reads the samples one minute in: one request for the chunk's
frames (about 54 KB), decoded on the fly. The same samples, as
`ffmpeg -i publicopinion_20_lippmann_64kb.mp3 -f s16le -` gives them after
its gapless trimming, start `delay + 529` samples later in its output.

**In the browser.** The service worker serves a zarrified MP3 as a Zarr
hierarchy like any other; the demo page does not display audio.

## Supported and not supported

Supported:

- MPEG-1, MPEG-2 and MPEG-2.5 Layer III streams, mono and stereo (joint,
  dual channel), constant and variable bitrates, with or without CRCs,
  with or without the bit reservoir.
- Xing, Info and VBRI frames (read, not counted as audio), LAME tags,
  ID3v2 tags (several, with footers), ID3v1 and APE tags.
- Junk between frames, a truncated last frame and bytes after the last
  frame: skipped, kept in the mirror, counted in `stray_bytes`.

Rejected:

- Layer I and Layer II streams (`MPEG-2 Layer II (the frame at 0) is not
  supported: only Layer III is`), and free-format bitrates.
- A stream whose frames change version, sample rate or channel count
  (`the frame at 2016 changes the sample rate from 16000 to 22050`): two
  files concatenated, for instance.
- A file with no audio frames, and an ID3v2 tag that runs past the file
  or whose size is not syncsafe.

### In the browser

The browser code ([js/](../../../js/README.md)) reads MP3 (MPEG-1, MPEG-2 and MPEG-2.5 Layer III audio):

- The one format that is not an image: one array `samples` of int16,
  `[time, channel]`, in chunks of 256 frames, each referencing the frames a
  decoder needs before it (the bit reservoir); the `mp3` codec decodes a
  chunk when it is read (in Python, with miniaudio).
- The stream's parameters, the Xing or Info frame's fields and the ID3v1
  tag under `refkv_zarrified`; the ID3v2 and APE tags in the mirror.

Not supported, and refused with HTTP 422: Layer I and II streams, free-format
bitrates, and files whose frames change sample rate, version or channel count.

## Performance

- Zarrifying reads the whole file (an MP3 has no index), in one or a few
  large range requests after one request for the size. The 3.9 MB file
  above takes under a second over HTTP.
- Reading a chunk is one request of the chunk's frames and their lead-in
  (two frames, plus the fewest frames holding 511 bytes of main data for
  MPEG-1 or 255 for MPEG-2: about 3 to 8 frames, a few percent of the
  chunk), then decoding them.
- The archive has one entry per chunk, and the mirror's table has a row
  per frame, folded into one run when the frames are all of one length
  (the file above: 167 KB in all, most of it the chunks' headers, about 4
  bytes a frame), a few bytes per frame more otherwise.

## How it's verified

`js/test/mp3/verify.py` zarrifies the synthetic MP3 files (including
inputs to reject) with the browser code, reads them back through
`python/src/refkv`, zarr-python and the `refkv.stream` codec, and compares
every sample with its decoder's decoding of the whole stream (exactly) and
with the ffmpeg command line's 16-bit output (within one unit, where the
generator stored it).
`python/tests/ir/test_mp3.py` does the same through Python, with chunks of
4 frames so that every file has several chunks and lead-ins. `compare.py`
checks that the Python and browser outputs are equivalent on those files
and on the public file of
[corpus_mp3.txt](../../../conformance/zarrify/corpus_mp3.txt).

- Profile: [spec/zarrify/mp3.md](../../../spec/zarrify/mp3.md#part-2-the-profile)
- Rust core: [rust/refkv-ir/src/mp3.rs](../../../rust/refkv-ir/src/mp3.rs)
- Codec: [python/src/refkv/zarr/codecs/mp3.py](../../../python/src/refkv/zarr/codecs/mp3.py)
- Fixtures: [fixtures/mp3/](../../../fixtures/mp3/)
