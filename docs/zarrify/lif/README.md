# LIF

refkv turns an image file into an OME-Zarr dataset by writing a small archive
of references to the file's bytes, without copying the pixels
([overview](../README.md)). This page is about Leica LIF files.

## Who this is for

LIF (`.lif`) is what Leica's LAS X and LAS AF software saves a confocal or
widefield session as: one file holding every image of the session, in a tree
of folders, with an XML description of each image and of the instrument's
settings. A file often holds tens of images: z-stacks, time series, FRAP and
FRET series, tile scans and the merged images LAS X stitches from them.
Someone who wants to open one image of such a file in an OME-Zarr viewer, or
read a plane of it lazily over HTTP, has to convert the whole file today, or
read it with a LIF reader (liffile, readlif, Bio-Formats).

A LIF file is an XML header and one uncompressed block of pixels per image, so
refkv reads the header and the blocks' headers and writes an archive in which
every Zarr chunk is a plane of an image, or a band of its rows, where the file
holds it.

## What you get

A bioformats2raw collection of OME-Zarr 0.5 images, one per image of the file
in the order the XML lists them (`0`, `1`, ...), each with one array, `0`.

- **Axes:** `t`, `c`, `z`, `y`, `x`, each when the image has it (`y` and `x`
  always), whatever order the file stores them in: LAS X usually stores
  channels between Y and Z.
- **Tile scans:** each tile is an image of its own, named `<image> (tile <n>)`,
  with its stage position (`FieldX`, `FieldY`, `PosX`, `PosY`, `PosZ`) under
  `refkv_zarrified.lif.tile`. Tiles overlap and are not stitched; LAS X's
  merged image, when the file has one, is another image. Other dimensions that
  OME-NGFF has no axis for (a lambda scan's wavelengths, rotations) select
  images the same way, named `<image> (field <n>)`.
- **Data types:** `uint8`, `uint16`, `uint32` and `uint64` (12-bit data in
  `uint16`, with a display window of 0 to 4095), `float16`, `float32` and
  `float64`.
- **Chunks:** one plane of one channel, or of all channels when they are
  interleaved (RGB, with the `transpose` codec), or bands of its rows when a
  plane is over 16 MiB. No compression: the file has none.
- **Scale:** each dimension's `Length` over its elements, in micrometers (from
  meters) and seconds. No translation.
- **omero:** a channel per channel, colored by its LUT (`Red`, `Green`, ...).
- **Metadata:** each image's group holds its element of the XML (its name,
  path in the file's tree, `UniqueID`), its memory block, and every attribute of
  its dimensions and channels, as text, under `refkv_zarrified.lif`. The whole
  file (the XML header, every block and any bytes after them) is described on
  `refkv_source` by the Rust core's IR mirror, from which it is rebuilt byte for
  byte.

**What is not read.** LOF files (one image of an XLEF project) and LIFEXT files
are rejected, and XLEF, XLIF and XLCF projects (XML naming image files beside
them) are not LIF files. FLIM raw data (photon histograms) has no image; its
block is kept in the mirror.
[spec/zarrify/lif.md §7.5](../../../spec/zarrify/lif.md#75-what-the-profile-does-not-read)
says so.

## Try it

A tile scan of four tiles, two channels, three planes and two time points
(227 KB, CC BY 4.0, Michael Goelzer), from
[corpus_lif.txt](../../../conformance/zarrify/corpus_lif.txt):

```bash
uv run refkv zarrify https://downloads.openmicroscopy.org/images/Leica-LIF/michael/PR2729_frameOrderCombinedScanTypes.lif pr2729.vzip
```

```
{"format": "lif", "version": 2, "images": 1, "series": 4, "blocks": 2, "chunks": 48, ...}
```

The archive is 23 KB. Read tile 1:

```python
import zarr
import refkv.zarr

g = zarr.open_group(refkv.zarr.open("pr2729.vzip"), mode="r")
print(g["OME"].attrs["ome"]["series"])
tile = g["1"]
print(tile.attrs["ome"]["multiscales"][0]["name"])
print(tile.attrs["refkv_zarrified"]["lif"]["tile"])
a = tile["0"]
print(a.shape, a.dtype, a.chunks)
print(a[1, 0, 2, 40, 30:36])
```

```
['0', '1', '2', '3']
TileScan_001 (tile 1)
{'FieldX': '0', 'FieldY': '1', 'PosX': '0.00005359375', 'PosY': '0.00028640625', 'PosZ': '-2.03516769781777E-08'}
(2, 2, 3, 64, 64) uint8 (1, 1, 1, 64, 64)
[56 55 54 53 52 51]
```

These are liffile's values (`LifFile(path).images[0].asarray()[1, 1, 2, 0, 40,
30:36]`, whose axes are T, M, Z, C, Y, X as the file stores them).

## Checked against

liffile, the reference Python reader (`js/test/lif/verify.py` and
`python/tests/ir/test_lif.py`): every image of every synthetic file
(`fixtures/lif`, written byte for byte by
`fixtures/generators/lif/write_fixtures.py`) reads as liffile reads it, and the
mirror rebuilds each file. The five public files of the corpus, from 227 KB to
3 GB, read as liffile reads them.
