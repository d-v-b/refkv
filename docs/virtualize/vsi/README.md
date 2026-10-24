# VSI

refkv turns an image file into an OME-Zarr dataset by writing a small archive
of references to the file's bytes, without copying the pixels
([overview](../README.md)). This page is about Olympus VSI slides.

## Who this is for

VSI is the format of Olympus (now Evident) cellSens and its slide scanners
(VS120, VS200): brightfield and fluorescence whole-slide images, z stacks
and time series, in pathology, neuroscience and core facilities. A slide
is a `.vsi` file and a directory beside it, `_<name>_`, with one `frame_t.ets`
tile file per stack. Bio-Formats, QuPath and OMERO read it; everyone else
converts it, which copies tens of gigabytes of JPEG tiles that are already
tiled and compressed the way an OME-Zarr reader wants them.

refkv reads the `.vsi` file (a megabyte or two of metadata) and each tile
file's headers and tile directory, and writes an archive in which every
Zarr chunk is one tile of one tile file. Nothing is decoded: the JPEG, JPEG
2000 or uncompressed tiles are served to the reader as they are.

## What you get

A bioformats2raw layout of OME-Zarr 0.5 images, one per stack of the slide:
the label, the overview, each scan (`20x_BF_01`, ...), each focus map, and
the macro image, numbered `0`, `1`, ... in the order the slide lists them,
with `OME/zarr.json` listing the series.

- **Pyramids:** every level of each scan, sized as Bio-Formats sizes them,
  with the stack's tiles (512 × 512 on a VS200) as chunks.
- **Axes:** `y`, `x`, with `c` for RGB tiles (the samples, as `transpose`
  puts them) or for a channel dimension, and `t` and `z` when a stack has
  more than one time point or plane.
- **Data types:** `uint8` and `uint16` in practice; every ETS pixel type
  from `int8` to `float64`.
- **Codecs:** `imagecodecs_jpeg`, `imagecodecs_jpeg2k` or `bytes`, after the
  `transpose` for interleaved samples. The chunks are the tiles' bytes.
- **Scale:** the pixel size in micrometers (or whatever unit the slide
  names) at every level; the z step for a z stack.
- **Translation:** the stack's origin in stage coordinates, so that the
  label, the overview and the scans of one slide are placed on one stage.
- **omero:** the channel names (`Cy5`, `DAPI`, ...), their LUT colors and
  display limits for fluorescence; `R`, `G`, `B` for brightfield.
- **Metadata:** the slide's whole metadata tree as JSON on the root, under
  `refkv_virtualized.vsi.tree`, tag by tag (named as Bio-Formats names
  them), with the list of stacks beside it; on each image, its stack's
  properties (scale, origin, magnification, creation time, channels) and
  its tile file's headers. The `.vsi` file and every tile file are
  described on `refkv_source` by the Rust core's IR mirror, each in place,
  so that each is rebuilt from the slide's own bytes.
- **The macro image and focus maps:** the JPEG photograph of the slide and
  the small `uint16` focus maps, stored as TIFF strips inside the `.vsi`
  file, as images of their own.

**What is not expressed.** A sample mask and a focus points stack that
have no pixels are listed but are no image; the thumbnails of each stack
(its coarse IFDs) are in the mirror only; the z and time start values, the
wavelengths and the full LUTs stay in the metadata.
[spec/virtualize/vsi.md §8](../../../spec/virtualize/vsi.md#8-what-the-layout-does-not-express)
lists them.

## Try it

Evident's brightfield sample from the OME sample images, from
[corpus_vsi.txt](../../../conformance/virtualize/corpus_vsi.txt): a label,
an overview and two 20× scans of 16589 × 14663 pixels, JPEG tiles, 7 levels
each. The `.vsi` URL names the slide; the tile files are read from
`_Image_V4.1_BF_/` beside it, by name:

```bash
uv run python -m refkv.virtualize https://downloads.openmicroscopy.org/images/CellSens/evident/Image_V4.1_BF.vsi image_bf.vzip
```

```
{"format": "vsi", "images": [{"name": "Label", "kind": "stack", "source": "tiles", "id": 1, "dataType": "uint8", "samples": 3, "codec": "jpeg", "axes": ["c", "y", "x"], "sizes": {"t": 1, "c": 1, "z": 1}, "levels": [[8042, 9403], [4021, 4702], ...], "grids": [[16, 19], ...], "chunks": 413, "tilesOutside": 0}, {"name": "Overview", ...}, {"name": "20x_BF_01", ..., "levels": [[16589, 14663], [8295, 7332], [4148, 3666], [2074, 1833], [1037, 917], [519, 459], [260, 230]], ..., "chunks": 1313, ...}, {"name": "20x FocusMap", "kind": "stack", "source": "ifd", ...}, {"name": "20x_BF_02", ...}, {"name": "20x FocusMap", ...}, {"name": "macro image", "kind": "macro", "source": "ifd", ...}], "chunks": 3921, "dataSources": 1, "ifds": 8, "tileFiles": 4, "tiles": 3918, "batches": 4, "reads": {"objects": 5, ..., "requests": 9, "bytes": 1412682, ...}, ...}
```

The run makes 9 requests (the 1.3 MB `.vsi` file whole, then each tile
file's headers and directory) and reads 1.4 MB, in about 2.5 seconds; the
archive is 760 KB for 3921 chunks, the metadata tree included. Read a scan:

```python
import zarr
import refkv.zarr

g = zarr.open_group(refkv.zarr.open("image_bf.vzip"), mode="r")
print(g["OME"].attrs["ome"]["series"])
im = g["2"]
ms = im.attrs["ome"]["multiscales"][0]
print(ms["name"], [a["name"] for a in ms["axes"]])
print(ms["datasets"][0]["coordinateTransformations"])
print(im["0"].shape, im["0"].dtype, im["0"].chunks)
print(im["6"][:, 100, 100:104])
```

```
['0', '1', '2', '3', '4', '5', '6']
20x_BF_01 ['c', 'y', 'x']
[{'type': 'scale', 'scale': [1, 0.27359517497709746, 0.2735849634879867]}, {'type': 'translation', 'translation': [0, 4614.215260264726, 37099.84264072623]}]
(3, 14663, 16589) uint8 (3, 512, 512)
[[238 234 231 236]
 [185 181 175 180]
 [231 227 222 227]]
```

The pixel size is 0.2736 µm, the origin the stack's position on the stage
in micrometers, and the values are what Bio-Formats reads for the same
pixels (level 6 of series 2). Reading the chunk fetched one JPEG tile of
the tile file.

**In the browser.** Not on the live demo yet (it virtualizes TIFF, NDPI and
ND2 only). A local build of the demo (see the [overview](../README.md))
virtualizes a slide whose host allows cross-origin reads of the `.vsi`
file and of the tile files beside it, and shows it in Neuroglancer;
downloads.openmicroscopy.org does not allow them.

## Viewing

The Neuroglancer fork (<https://github.com/d-v-b/neuroglancer>, branch `vzip`)
decodes the JPEG tiles and shows each scan with its micrometer scale; the
stacks of one slide share the stage's coordinates.

## Supported and not supported

Supported:

- cellSens slides whose tile files are `frame_t.ets` or `frame_t_0.ets`
  (both names are tried, in that order), with uncompressed, JPEG or JPEG
  2000 tiles, in pyramids or not, with time, channel and z dimensions.
- The macro image and the focus maps stored inside the `.vsi` file (JPEG
  or uncompressed strips, 8 or 16 bits).
- A `--url` naming the `.vsi` file's URL for a local copy that will be
  served from there, with its directory.

Rejected:

- Tile files with lossless JPEG, PNG or BMP tiles (the compression is named
  in the message), or an unknown compression or pixel type.
- A stack whose tile file is missing (`stack 10002 (20x_BF_01) has no tile
  file: neither _<name>_/stack10002/frame_t.ets nor ..._0.ets exists`): copy
  the `_<name>_` directory with the `.vsi` file.
- A stack with several samples per pixel and several channels, or a
  dimension the layout cannot place (lambda, phase) with more than one
  value.
- A `.vsi` file over 256 MB, and any file whose containers, fields, IFDs or
  tile directories are inconsistent (the profile lists every check).

### In the browser

The browser code ([js/](../../../js/README.md)) reads Olympus VSI (cellSens slides):

- The `.vsi` file's URL names the slide; the tile files beside it
  (`_<name>_/stack<N>/frame_t.ets`) are probed by URL, not listed, so a
  plain web server that allows cross-origin reads serves a slide as it is.
- One OME-Zarr image per stack in a bioformats2raw layout: every pyramid
  level, with time, channel and z dimensions; JPEG, JPEG 2000 and
  uncompressed tiles as chunks; the macro image and focus maps from the
  `.vsi` file; pixel sizes in micrometers, stage origins as translations,
  channel names and colors.
- The whole metadata tree under `refkv_virtualized` on the root.

Not supported, and refused with HTTP 422: lossless JPEG, PNG and BMP
tiles, dimensions other than t, c and z with more than one value, and a
stack whose tile file is missing.

## Performance

- Virtualizing reads the `.vsi` file whole (one request; a few megabytes),
  then one request per tile file to open it, two to read its headers and
  its tile directory: a slide of `n` scans takes about `3n + 4` requests.
- Reading a chunk is one range request of one tile, 20 to 200 KB for a
  512 × 512 JPEG tile.
- The archive has one entry per tile, about 100 bytes each after the
  mirror's folding, and the metadata tree on the root.

## How it's verified

`js/test/vsi/verify.py` serves the synthetic slides (`fixtures/vsi/`,
including inputs to reject) with their tile files, virtualizes them with
the browser code, reads them back through `python/src/refkv` and
zarr-python, and compares every stack with the pixels the generator wrote,
and, when a JVM can be started, every level of every stack and the macro
image with Bio-Formats (bioio-bioformats). `python/tests/ir/test_vsi.py`
does the same through Python, and against Bio-Formats on the public samples
when they are at hand. `compare.py` checks that the Python and browser
outputs are equivalent on the synthetic slides and on the public slides of
[corpus_vsi.txt](../../../conformance/virtualize/corpus_vsi.txt). Every file
of each slide is rebuilt from the archive's mirror and compared with the
original.

- Profile: [spec/virtualize/vsi.md](../../../spec/virtualize/vsi.md#part-2-the-profile)
- Rust core: [rust/refkv-ir/src/vsi/](../../../rust/refkv-ir/src/vsi/)
- Fixtures: [fixtures/vsi/](../../../fixtures/vsi/)
