# NRRD

refkv turns an image file into an OME-Zarr dataset by writing a small archive of references to
the file's bytes, without copying the pixels ([overview](../README.md)). This page is about NRRD
volumes.

## Who this is for

NRRD (`.nrrd`, or a detached header `.nhdr` beside its data file) is Teem's format, and the
working format of 3D Slicer, ITK-SNAP and diffusion MRI tools; the IMPC's embryo scans are
shared as NRRD. A NRRD file is a short text header followed by one block of data, raw or
compressed with gzip, so refkv reads the header and writes an archive whose chunks are ranges of
that block.

## What you get

One OME-Zarr 0.5 image at the archive root, with one array, `0`.

- **Axes:** placed by the header's `kinds` (and, without them, its `space directions`): two or
  three spatial axes are `x`, `y` and `z` in the header's order (its first, fastest axis is `x`),
  a `time` axis is `t`, and one other axis (a color, a vector, a diffusion list) is `c`.
- **Data types:** every NRRD scalar type (`int8` to `uint64`, `float`, `double`), by any of its
  names.
- **Chunks:** raw data as the file stores it, cut into equal runs of at most 128 KiB, as for
  NIfTI. Gzip data is **one chunk** of the whole array: a gzip stream has no index to cut it by,
  so the chunk references the whole stream and the `gzip` codec decompresses it. Nothing is
  copied, but reading any value decompresses the whole volume.
- **Codecs:** `transpose` when the axes are stored in another order (a color axis fastest),
  `bytes` in the header's byte order, and `gzip` for gzip data.
- **Scale and translation:** from `space directions` and `space origin` when the directions are
  axis-aligned with a positive step; otherwise the directions' lengths (a flipped or oblique
  volume is not rotated, and has no translation), or `spacings`. Units from `space units` or
  `units` (`mm`, `um`, `s`, ...).
- **Header:** every field, as written, every key/value pair (such as a diffusion scan's
  gradients) and every comment, under `refkv_zarrified.nrrd`. The header file (and a detached
  header's data file) is described on `refkv_source` by the Rust core's IR mirror; its view
  `refkv_source/tree` shows the header line by line.
- **omero:** R, G, B (A) channels for an `RGB-color` (`RGBA-color`) axis of `uint8`, or a
  display window from the `min` and `max` fields.

## Try it

pynrrd's test volume (MIT), from [corpus_nrrd.txt](../../../conformance/zarrify/corpus_nrrd.txt):

```bash
uv run refkv zarrify https://raw.githubusercontent.com/mhe/pynrrd/96bf87b78bde03987c09346ceffe0bb85a8f4214/nrrd/tests/data/BallBinary30x30x30_gz.nrrd ball.vzip
```

```
{"format": "nrrd", "sizes": {"z": 30, "y": 30, "x": 30}, "dataType": "int16", "encoding": "gzip", "detached": false, "dataFile": "BallBinary30x30x30_gz.nrrd", "chunkShape": [30, 30, 30], "chunks": 1, "orientation": true, "translation": true, ...}
```

The archive is 5 KB. Read it:

```python
import zarr
import refkv.zarr

g = zarr.open_group(refkv.zarr.open("ball.vzip"), mode="r")
print(g.attrs["refkv_zarrified"]["nrrd"]["fields"]["space directions"])
a = g["0"]
print(a.shape, a.dtype, a.chunks)
```

```
(1,0,0) (0,1,0) (0,0,1)
(30, 30, 30) int16 (30, 30, 30)
```

A detached header is named by its `.nhdr` file; its data file is found beside it, by the name its
`data file` field gives, over HTTP or on disk.

## Supported and not supported

Supported:

- Attached headers (`.nrrd`) and detached headers with one data file (`.nhdr`), magics
  `NRRD0001` to `NRRD0005`.
- The raw and gzip encodings, either byte order, `line skip`, and `byte skip` (including −1 for
  raw data at the end of its file).

Rejected, each with a message that says why:

- The ascii (`text`, `txt`) and hex encodings: their values are text, which no chunk can
  reference as binary.
- The bzip2 encoding: no Zarr codec that both of refkv's readers decode.
- `byte skip` with gzip data (it skips decompressed bytes, which no Zarr codec does).
- Detached headers with several data files (`LIST`, or a `%d` pattern), a data file outside the
  header's directory, or one that is missing.
- The `block` type, and axes OME-NGFF cannot place: one spatial axis or more than three, or two
  time or channel axes.

## How it's verified

`js/test/nrrd/verify.py` zarrifies the synthetic inputs of [fixtures/nrrd/](../../../fixtures/nrrd/)
(including inputs to reject) with the browser code, reads them back through zarr-python, and
compares the values, the axes, the scales and every header field with pynrrd's reading.
`python/tests/ir/test_nrrd.py` does the same for the Python host, and
`rust/refkv-ir/tests/nrrd.rs` checks the layout and each rejection.

- Profile: [spec/zarrify/nrrd.md](../../../spec/zarrify/nrrd.md#part-2-the-profile)
- Core: [rust/refkv-ir/src/nrrd.rs](../../../rust/refkv-ir/src/nrrd.rs)
