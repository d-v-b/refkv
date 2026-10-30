# Akoya QPTIFF

refkv turns an image file into an OME-Zarr dataset by writing a small archive
of references to the file's bytes, without copying the pixels
([overview](../README.md)). This page is about Akoya Biosciences (formerly
PerkinElmer) QPTIFF files.

## Who this is for

Akoya's Vectra and Vectra Polaris scanners write multiplex fluorescence and
brightfield whole-slide scans as `.qptiff` files, one image per channel, and
inForm writes unmixed component data as `.tif` files in the same format. The
files are TIFFs, so most tools open the first image; what they miss is the
format's own arrangement, where the channels are consecutive images, the
pyramid levels follow in groups, and the tiles are LZW-compressed, which
Zarr tooling does not usually read. People with spatial-omics data want to
view a whole multiplexed slide in an OME-Zarr viewer, with the channels
named, or tile it for analysis in Python, without converting every slide.

refkv reads the file's image file directories and the
`PerkinElmer-QPI-ImageDescription` XML each one carries, groups the images
into channels and levels, and writes an archive in which every Zarr chunk is
one tile (or one strip) of the file, decoded by an LZW, JPEG or plain
`bytes` codec. Akoya's 2.0 GB LuCa-7color sample scan (5 channels, 6 levels,
33 images) became a 2.9 MB archive in about 3 s, from 16 range requests. The
format's specification and public samples are at
<https://downloads.openmicroscopy.org/images/Vectra-QPTIFF/>.

## What you get

One OME-Zarr 0.5 image at the archive root, with one array per pyramid
level, `0` the full resolution.

- **Axes:** `c`, `y`, `x` (`y`, `x` alone for a single grayscale channel).
  A fluorescence scan's channels are its FullResolution images; a
  brightfield scan is one RGB image, whose samples are the channel axis.
- **Levels:** the FullResolution images, then each run of ReducedResolution
  images, one per channel. The thumbnail, overview and label images are
  not levels; they stay in the source metadata.
- **Chunks:** one TIFF tile per chunk; the smallest levels, which the
  scanners write in strips of 16 rows, get one strip per chunk, on a
  regular grid: a short last strip is padded to 16 rows (LZW: a few literal
  bytes spliced into its stream; uncompressed: a zero data source), so
  that dask and napari read the level as they read any other. A JPEG level
  in strips whose last strip is short cannot be padded and is left out.
- **Codecs:** `bytes` and `refkv.tiff_lzw` for LZW (the scanners' default),
  `imagecodecs_jpeg` for JPEG brightfield scans, `bytes` alone for
  uncompressed data; interleaved RGB behind a `transpose` codec. Integer and
  float32 samples (inForm's component data).
- **Channels:** `omero.channels` carries each channel's name and color from
  its description (`DAPI`, `0,0,255`). The file records no display range,
  so each channel's window is sampled from the middle chunk of the coarsest
  level (the 5th to 99.9th percentile of its values, leaving out zeros) for
  LZW, Deflate and uncompressed data, float32 component data included; an
  RGB scan gets `R`, `G` and `B` channels with the data type's whole range,
  as does a channel whose chunk is not sampled.
- **Scale:** from the TIFF resolution tags (pixels per centimeter), in
  micrometers; each level's scale grows with its downsampling.
- **Translation:** the stage position of the image's upper-left corner,
  from the XPosition and YPosition tags.
- **Name:** the slide's `SlideID`.
- **Metadata:** the root's `refkv_zarrified.qptiff` holds the byte order
  and whether the file is BigTIFF. Everything else is on `refkv_source`,
  the file's IR mirror ([conventions §8](../../../spec/conventions.md#8-the-ir-mirror)),
  as for [TIFF](../tiff/README.md): every IFD, every tag (each image's
  whole XML description, the scan profile included), every tile and strip
  of every image, from which the file is rebuilt byte for byte.

## Try it

[example.py](example.py) zarrifies a public file and plots it with matplotlib. Its
dependencies are in its header (PEP 723), so it runs on its own (uv builds refkv from GitHub,
which needs a Rust toolchain, until a release on PyPI has `refkv.zarrify`):

```bash
uv run docs/zarrify/qptiff/example.py
```

Akoya's LuCa-7color scan (2.0 GB, CC BY 4.0, 5 channels):

```bash
uv run refkv zarrify https://downloads.openmicroscopy.org/images/Vectra-QPTIFF/perkinelmer/PKI_scans/LuCa-7color_Scan1.qptiff LuCa-7color.vzip
```

```
{"format": "qptiff", "axes": ["c", "y", "x"], "levels": [[5, 34560, 24960], [5, 17280, 12480], [5, 8640, 6240], [5, 4320, 3120], [5, 2160, 1560], [5, 1080, 780]], "channels": 5, "references": 22770, "planner": {…}, "elements": 25928, "folded": {…}}
```

This took 3 s and wrote a 2.9 MB archive (at spec/zarrify.md revision 28).
The brightfield H&E sample (`HandEcompressed_Scan1.qptiff`, 422 MB, JPEG) gave
5 levels of `[3, H, W]` in 0.8 s, and the inForm component field
(`PKI_fields/HnE_3_1x1component_data.tif`, 24 MB) one `float32` level of two
channels in strips.

Reading it with zarr-python needs the LZW codec `refkv.tiff_lzw`, which zarr finds
through refkv's entry points (it uses `imagecodecs`, the `images` extra):

```python
import zarr
import refkv.zarr

g = zarr.open_group(refkv.zarr.open("LuCa-7color.vzip"), mode="r")
ome = g.attrs["ome"]
print([c["label"] for c in ome["omero"]["channels"]])
for d in ome["multiscales"][0]["datasets"]:
    a = g[d["path"]]
    print(d["path"], a.shape, a.chunks, d["coordinateTransformations"][0]["scale"])
dapi = g["5"][0]  # the smallest level's first channel
print(dapi.shape, dapi.dtype, dapi.mean())
```

The same file and the inForm component fields are listed with their
structure in [spec/zarrify/qptiff.md](../../../spec/zarrify/qptiff.md).
The command line also takes a local path, and `vzip convert` ([rust/refkv-cli](../../../rust/refkv-cli/))
writes the same archive without Python.

**In the browser.** downloads.openmicroscopy.org sends no CORS headers, so
the browser cannot read Akoya's samples there. The live demo's QPTIFF
example is a Vectra Polaris fluorescence slide on Zenodo (record 16569043,
CDLA-Sharing-1.0; 3 channels, 71472 × 61440 pixels, 9 levels of JPEG tiles,
362 MB):
<https://d-v-b.github.io/refkv-demo/image-to-zarr/?url=https%3A%2F%2Fzenodo.org%2Fapi%2Frecords%2F16569043%2Ffiles%2F2025-07-15_13_13_34.qptiff%2Fcontent>.
It was ready in about 7 s, from 29 range requests. An LZW slide on a host
that allows cross-origin reads (Zenodo record 5545864's inForm component
fields, for example) zarrifies the same way.

## Viewing

The Neuroglancer fork refkv's demo uses decodes `imagecodecs_jpeg`, so JPEG
scans render there. It decodes LZW under the name `imagecodecs_lzw`
([d-v-b/neuroglancer#6](https://github.com/d-v-b/neuroglancer/pull/6)), which
archives named before d-v-b/refkv#90, and not yet `refkv.tiff_lzw`, so LZW scans
render there once the fork registers that name too; zarr-python reads both
(above).

## Supported and not supported

Supported:

- TIFF and BigTIFF QPTIFF files, either byte order, whose first image's
  Software tag starts with `PerkinElmer-QPI`.
- Any number of grayscale channels (`uint8`, `uint16`, `float32`, ... as the
  TIFF page lists them), or one RGB image.
- Pyramids of tiled levels, with the smallest levels in strips, or images
  in strips only (inForm fields); a short last strip padded when the level
  is uncompressed or LZW.
- LZW tiles whose stream runs a few bytes past the tile (some Vectra
  scanners write the end code one bit too narrow): the codec decodes into
  the tile's size, as tifffile does.
- Compression: none, LZW, DEFLATE, zstd, JPEG 2000, and JPEG (8-bit gray,
  RGB or YCbCr) with or without `JPEGTables`.
- The horizontal predictor (Predictor 2) of integer samples with LZW,
  DEFLATE or zstd, as in the TIFF profile (`imagecodecs_delta`).

Rejected:

- A first image that is not a FullResolution image with a description, a
  FullResolution image after the thumbnail, or several RGB channels.
- ReducedResolution images that do not come in groups of one per channel,
  levels whose images differ in size, tiling or format, or that do not get
  strictly smaller.
- The floating-point predictor (Predictor 3), as in the TIFF profile, which
  reads Predictor 2 (horizontal differencing) with `imagecodecs_delta`.
- A level in JPEG (or Deflate, zstd, JPEG 2000) strips whose last strip is
  short is left out of the pyramid, since such a strip cannot be padded;
  the scanners' JPEG brightfield scans have one such level, the smallest.
- Everything the TIFF profile rejects of an image: old-style JPEG, unknown
  compressions, FillOrder 2, subsampled YCbCr that is not JPEG.

A plain TIFF (no `PerkinElmer-QPI` Software tag) is read by the
[TIFF profile](../tiff/README.md), which still rejects LZW; LZW support is
specific to QPTIFF for now.

### In the browser

The browser code ([js/](../../../js/README.md)) reads Akoya QPTIFF (Vectra scans, inForm component data; found by the Software tag):

- The FullResolution images as channels and the runs of ReducedResolution
  images as levels, named and colored in `omero` from each image's XML
  description; tiles, or strips for the smallest levels and inForm fields
  (a rectilinear chunk grid when the last strip is short).
- Compression: none, LZW (`refkv.tiff_lzw`), JPEG, DEFLATE, zstd, JPEG 2000, and Predictor 2
  (`imagecodecs_delta`).

Not supported, and refused with HTTP 422: the floating-point predictor, several RGB channels,
and images that do not follow the format's arrangement.

## How it's verified

`python/tests/ir/test_qptiff.py` and `js/test/qptiff/verify.py` zarrify
the 15 synthetic QPTIFF files of [fixtures/qptiff/](../../../fixtures/qptiff/)
(fluorescence scans in LZW tiles with stripped bottom levels, a JPEG and an
uncompressed brightfield scan, float32 component fields in strips, a BigTIFF,
a tile whose LZW stream runs past it, and 9 inputs to reject) with the Python and browser hosts of the Rust core,
read every level back through zarr-python and compare the pixels with
tifffile, and rebuild each file from its mirror. QPTIFF has no frozen
reference implementation, so `compare.py` does not run it; tifffile is the
independent reader. Akoya's public samples (the LuCa-7color and H&E scans
and the HnE_3 component field, above) were zarrified over HTTP, and their
smaller levels read back through zarr-python equal what tifffile decodes from
the same pages; their structure is what the fixtures reproduce.

- Convention and profile: [spec/zarrify/qptiff.md](../../../spec/zarrify/qptiff.md), which builds on
  [spec/zarrify/tiff.md](../../../spec/zarrify/tiff.md#part-2-the-profile)
- Rust core (Python and the browser): [rust/refkv-ir/src/tiff.rs](../../../rust/refkv-ir/src/tiff.rs) (its QPTIFF
  variant), [rust/refkv-ir/src/project/qptiff.rs](../../../rust/refkv-ir/src/project/qptiff.rs),
  [rust/refkv-ir/schema/qptiff.json](../../../rust/refkv-ir/schema/qptiff.json)
- Fixtures: [fixtures/qptiff/](../../../fixtures/qptiff/), written by
  [fixtures/generators/qptiff/write_fixtures.py](../../../fixtures/generators/qptiff/write_fixtures.py)
