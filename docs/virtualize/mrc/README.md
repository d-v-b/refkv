# MRC

refkv turns an image file into an OME-Zarr dataset by writing a small archive
of references to the file's bytes, without copying the pixels
([overview](../README.md)). This page is about MRC files.

## Who this is for

MRC (`.mrc`, `.mrcs`, `.map`, `.rec`, `.st`) is the working format of
electron microscopy: cryo-EM density maps from the EMDB, particle stacks and
micrographs from RELION and cryoSPARC, tilt series and tomograms from IMOD,
and crystallographic maps from CCP4. Someone who wants to look at a map in
an OME-Zarr viewer, read one section of a tomogram lazily over HTTP, or
put a particle stack next to other arrays in a Zarr store has to convert it
today.

An MRC file is a 1024-byte header, an optional extended header, and one
block of values, so refkv reads the header and writes an archive in which
every Zarr chunk is a run of at most 1 MiB of that block: whole volumes or
blocks of sections when they are small, else blocks of rows.

## What you get

One OME-Zarr 0.5 image at the archive root, with one array, `0`.

- **Axes:** `z`, `y`, `x` for a volume or a stack of images (sections,
  rows, columns), `y`, `x` for a single image, and `t`, `z`, `y`, `x` for a
  stack of volumes (`ispg` 401 and up), `t` indexing the volumes. The axes
  are named by the header's axis map (`mapc`, `mapr`, `maps`) when it is a
  permutation of x, y and z; the values are never reordered.
- **Data types:** `int8`, `int16`, `uint16`, `float16`, `float32` and
  `complex64` (modes 0, 1, 6, 12, 2 and 4); `uint8` for a mode 0 file that
  IMOD marks as unsigned.
- **Chunks:** the values as the file stores them, cut into equal runs of at
  most 1 MiB along the outermost axis that allows it.
- **Codecs:** `bytes` in the file's byte order. No compression: the file
  has none.
- **Scale:** the voxel size, `cella / mx` and so on, in angstroms, where the
  cell and the grid give a positive finite one.
- **Translation:** the header's `origin` (angstroms), for a volume, when it
  is finite and not all zero. An image's origin is a phase origin in pixels
  and is not applied.
- **Header:** every field of the MRC header, under
  `refkv_virtualized.mrc.header`, with the kind of file, the data type,
  whether the axis map was applied and the voxel size beside it. The whole
  file (the header, the extended header, the values and any bytes after
  them) is described on `refkv_source` by the Rust core's IR mirror, from
  which it is rebuilt byte for byte.
- **omero:** a display window from `dmin` and `dmax`.
- **A dataset for xarray:** the same values again under `dataset/`, as a
  group xarray opens directly: the data variable `data` with the
  dimensions `z`, `y`, `x` (`section` or `volume` for a stack's index) and
  coordinates in angstroms along each axis with a voxel size. Its chunks
  are the same references; only the coordinates are stored, as a
  compressed index (a few bytes per thousand positions) that xarray's CF
  decoding turns into `float64` positions.

**What is not expressed.** OME-Zarr places an image by a scale and a
translation and has no crystallography, so the cell angles (a skewed grid
is shown orthogonal), the space group and symmetry operators, the
`nxstart` placement of a sub-volume, the per-section records of FEI and
SerialEM extended headers (tilt angles, doses), and the meaning of a
stack's index (micrographs or particles, not z-slices; volumes, not time
points) are recorded in the header or the mirror but not in the layout.
[spec/virtualize/mrc.md §7](../../../spec/virtualize/mrc.md#7-what-the-layout-does-not-express)
lists them.

## Try it

An EMDB map from mrcfile's test data (EMD-3197, 20 × 20 × 20 voxels, CC0),
from [corpus_mrc.txt](../../../conformance/virtualize/corpus_mrc.txt):

```bash
uv run python -m refkv.virtualize https://raw.githubusercontent.com/ccpem/mrcfile/64b0fb9ce0d921b902a06fe94d43e8d642b33d7d/tests/test_data/EMD-3197.map emd_3197.vzip
```

```
{"format": "mrc", "kind": "volume", "byteOrder": "little", "mode": 2, "dataType": "float32", "sizes": {"z": 20, "y": 20, "x": 20}, "chunkShape": [20, 20, 20], "chunks": 1, "voxelSize": [11.4, 11.4, 11.4], "translation": false, "axisMap": true, "extendedHeader": 0, "extendedType": "", ...}
```

The archive is 12 KB, both layouts included. Read the image:

```python
import zarr
import refkv.zarr

g = zarr.open_group(refkv.zarr.open("emd_3197.vzip"), mode="r")
print(g.attrs["ome"]["multiscales"][0]["axes"])
print(g.attrs["ome"]["multiscales"][0]["datasets"])
print(g.attrs["refkv_virtualized"]["mrc"]["voxel_size"])
a = g["0"]
print(a.shape, a.dtype, a.chunks)
print(a[10, 10, 8:12])
```

```
[{'name': 'z', 'type': 'space', 'unit': 'angstrom'}, {'name': 'y', 'type': 'space', 'unit': 'angstrom'}, {'name': 'x', 'type': 'space', 'unit': 'angstrom'}]
[{'path': '0', 'coordinateTransformations': [{'type': 'scale', 'scale': [11.4, 11.4, 11.4]}]}]
{'x': 11.4, 'y': 11.4, 'z': 11.4}
(20, 20, 20) float32 (20, 20, 20)
[ 0.1251585  -0.87975615  0.12517013  2.0731938 ]
```

These are mrcfile's values (`mrcfile.open(path).data[10, 10, 8:12]`), in the
same axis order (sections, rows, columns). The same archive as a dataset:

```python
import xarray as xr

ds = xr.open_zarr(refkv.zarr.open("emd_3197.vzip"), group="dataset", consolidated=False)
print(ds)
print(ds["data"].sel(z=114.0, y=114.0, x=91.2).values)
```

```
<xarray.Dataset> Size: 32kB
Dimensions:  (z: 20, y: 20, x: 20)
Coordinates:
  * z        (z) float64 160B 0.0 11.4 22.8 34.2 ... 182.4 193.8 205.2 216.6
  * y        (y) float64 160B 0.0 11.4 22.8 34.2 ... 182.4 193.8 205.2 216.6
  * x        (x) float64 160B 0.0 11.4 22.8 34.2 ... 182.4 193.8 205.2 216.6
Data variables:
    data     (z, y, x) float32 32kB dask.array<chunksize=(20, 20, 20), meta=np.ndarray>
Attributes:
    zarr_conventions:   [{'uuid': 'f139bf4e-7bab-4c3c-9b2d-66ba747f9b93', 'sc...
    refkv_virtualized:  {'profile': 'mrc-dataset', 'version': 0, 'revision': ...
0.1251585
```

The positions are angstroms, so `sel` takes them; the value is
`data[10, 10, 8]` above.

**In the browser.** Not on the live demo yet (it virtualizes TIFF, NDPI and
ND2 only). A local build of the demo (see the [overview](../README.md))
virtualizes a map and shows it in Neuroglancer; raw.githubusercontent.com
allows cross-origin reads.

## Viewing

The Neuroglancer fork (<https://github.com/d-v-b/neuroglancer>, branch `vzip`)
displays `int8`, `int16`, `uint16` and `float32` maps with angstrom scales.
It has no `float16` or `complex64` data type; zarr-python reads them.

## Supported and not supported

Supported:

- MRC2014 files, either byte order: single images, image stacks, volumes
  and volume stacks.
- Extended headers of any type (CCP4 symmetry records, FEI, SerialEM, ...)
  are kept in the source metadata; they do not change where the values
  start.

Rejected:

- Gzipped maps (`.map.gz`), the way the EMDB distributes them: `not a
  TIFF, NDPI, ND2, DICOM, NIfTI, MRC, IMS, CZI or SAFE zip file`. A gzip
  stream has no byte ranges to reference; decompress the map first.
- Files without `MAP` at byte 208 (the pre-2000 format), and a machine
  stamp that is neither little- nor big-endian.
- Complex int16 (mode 3) and 4-bit (mode 101) data.
- A volume stack whose `mz` does not divide `nz`, and data past the end of
  the file.

## Performance

- Virtualizing reads the first 1024 bytes of the file: one range request,
  after one request for the file's size. The extended header is never
  read.
- Reading a chunk is one range request of at most 1 MiB.
- The archive has one entry per chunk.

## How it's verified

`js/test/mrc/verify.py` virtualizes the synthetic MRC files (including
inputs to reject) with the browser code, reads them back through
`python/src/refkv` and zarr-python, and compares the values and every
header field with mrcfile's. `python/tests/ir/test_mrc.py` does the same
through Python. `compare.py` checks that the Python and browser outputs are
equivalent on those files and on the public files of
[corpus_mrc.txt](../../../conformance/virtualize/corpus_mrc.txt).

- Profile: [spec/virtualize/mrc.md](../../../spec/virtualize/mrc.md#part-2-the-profile)
- Rust core: [rust/refkv-ir/src/mrc.rs](../../../rust/refkv-ir/src/mrc.rs)
- Fixtures: [fixtures/mrc/](../../../fixtures/mrc/)
