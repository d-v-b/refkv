# ENVI

refkv turns an image file into an OME-Zarr dataset by writing a small archive of references to
the file's bytes, without copying the pixels ([overview](../README.md)). This page is about ENVI
rasters.

## Who this is for

ENVI's format is the working format of remote sensing and hyperspectral imaging: airborne and
spaceborne imaging spectrometers (AVIRIS, EnMAP, PRISMA and many lab cameras) ship their cubes
as ENVI, and GDAL, spectral and VirtualiZarr's [`ENVIParser`](https://github.com/zarr-developers/VirtualiZarr/blob/30221eb05a3bb0040956bda1dcc68e6bded5e149/virtualizarr/parsers/envi.py) (newer than its
releases) read it. An "ENVI Standard" raster is two files: a
flat binary data file, and a short text header beside it, `<name>.hdr`, which says the sizes, the
data type, the byte order and how the bands are interleaved. refkv reads the header and writes an
archive whose chunks are ranges of the data file.

## What you get

One OME-Zarr 0.5 image at the archive root, with one array, `0`.

- **Axes:** `c` (the bands), `y` (the lines) and `x` (the samples), whatever the interleave.
- **Data types:** every ENVI type of a flat array: `uint8`, `int16`, `uint16`, `int32`, `uint32`,
  `int64`, `uint64`, `float32`, `float64`, `complex64` and `complex128`, in either byte order.
- **Chunks:** one band each for a band-sequential (BSQ) file, one line each for a
  band-interleaved file (BIL, BIP), at the byte ranges VirtualiZarr's `ENVIParser` gives them. A
  BIL or BIP file is put in `c`, `y`, `x` order by a `transpose` codec, so no byte is moved. A
  gzip file (`file compression = 1`) is **one chunk** of the whole file, as a gzip stream has no
  index to cut it by; a header offset is then counted in the inflated bytes, as GDAL counts it,
  and skipped by a `refkv.skip` codec.
- **Header:** every field, as written (wavelengths, FWHM, band names, map info, a braced value
  with its line ends), and every comment, under `refkv_zarrified.envi`. The header file is held
  in the archive, and the data file described on `refkv_source` by the Rust core's IR mirror.

The image has scale 1 and no translation: `map info` and `projection info` are recorded, not
applied, and the wavelengths are not made a coordinate yet.

## Try it

[example.py](example.py) zarrifies GDAL's small RGB test raster, stored band-interleaved by pixel,
and shows it with matplotlib. Its dependencies are in its header (PEP 723), so it runs on its own:

```bash
uv run docs/zarrify/envi/example.py
```

From the command line, name the header: the data file is found beside it.

```bash
uv run refkv zarrify https://raw.githubusercontent.com/OSGeo/gdal/8a556e7b81aab50ec6612b47499ffbe942aa3a71/autotest/gdrivers/data/envi/envi_rgbsmall_bip.hdr rgb.vzip
```

```
{"format": "envi", "sizes": {"c": 3, "y": 49, "x": 50}, "dataType": "uint8", "interleave": "bip", "compression": "none", "dataFile": "envi_rgbsmall_bip.img", "chunkShape": [3, 1, 50], "chunks": 49, ...}
```

Read it:

```python
import zarr
import refkv.zarr

g = zarr.open_group(refkv.zarr.open("rgb.vzip"), mode="r")
print(g.attrs["refkv_zarrified"]["envi"]["interleave"])
a = g["0"]
print(a.shape, a.dtype, a.chunks)
```

```
bip
(3, 49, 50) uint8 (3, 1, 50)
```

### Naming the input

The input is the **header file**: it has a signature (its first line is `ENVI`), and the data file
has none. refkv finds the data file beside the header by its name, `<name>.hdr` less `.hdr`
(`scene.img` for `scene.img.hdr`, or `scene` for `scene.hdr`, as ENVI itself writes it), then that
name followed by `.img`, `.dat`, `.raw`, `.bin`, `.bsq`, `.bil` or `.bip` (in capitals for
`.HDR`), the first that exists. Over HTTP each name is one request, and a name that is not there a
404. These are the two conventions VirtualiZarr's `ENVIParser` finds a header by, read back, so a
tool that has the data file's URL names the header beside it: `<data file>.hdr`, or the data
file's name with its extension replaced by `.hdr`.

## Supported and not supported

Supported:

- ENVI Standard files (and headers without a `file type`), BSQ, BIL and BIP, every data type of a
  flat array, both byte orders, a header offset, and whole-file gzip.

Rejected, each with a message that says why, as VirtualiZarr's `ENVIParser` rejects them:

- Other file types: spectral libraries, classification images, and files ENVI reads under a
  header of another format's (TIFF, HDF).
- Framed data (`major frame offsets`, `minor frame offsets`).
- A raw data file whose size is not the header offset and the array's bytes: one of another size
  usually means a field that changes the layout is not read. (GDAL reads a file longer than its
  array; a few of GDAL's own test files end with a stray byte.)
- A header not named `<name>.hdr`, or with no data file beside it.

## How it's verified

`js/test/envi/verify.py` zarrifies the synthetic inputs of [fixtures/envi/](../../../fixtures/envi/)
(including inputs to reject) with the browser code, reads them back through zarr-python, and
compares the values with GDAL's ENVI driver's reading (through rasterio) and NumPy's reading of the
data file, and every header field with GDAL's. `python/tests/ir/test_envi.py` does the same for the
Python host and checks that the chunks are the byte ranges [`ENVIParser`'s source](https://github.com/zarr-developers/VirtualiZarr/blob/30221eb05a3bb0040956bda1dcc68e6bded5e149/virtualizarr/parsers/envi.py)
gives, restated in the test, since the VirtualiZarr release refkv locks predates the parser;
`rust/refkv-ir/tests/envi.rs` checks the layout and each rejection. GDAL's own ENVI test rasters
are in the public corpus ([corpus_envi.txt](../../../conformance/zarrify/corpus_envi.txt)).

- Profile: [spec/zarrify/envi.md](../../../spec/zarrify/envi.md#part-2-the-profile)
- Core: [rust/refkv-ir/src/envi.rs](../../../rust/refkv-ir/src/envi.rs)
