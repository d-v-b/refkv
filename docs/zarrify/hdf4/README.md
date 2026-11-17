# HDF4 and HDF-EOS2

refkv turns a file into a Zarr hierarchy by writing a small archive of
references to the file's bytes, without copying the data
([overview](../README.md)). This page is about HDF4 files, HDF-EOS2 files
among them.

## Who this is for

HDF4 is the format of NASA's EOS-era archives: MODIS, SeaWiFS, AIRS, CALIPSO,
CERES, MISR and AMSR-E products, petabytes of them, many as HDF-EOS2 (HDF4
with ODL text that defines grids and swaths). The HDF4 library has no reader
in the browser and none over HTTP, so a user downloads each granule whole;
NASA's Hyrax serves some through DMR++ sidecars, and kerchunk and VirtualiZarr
have experimental readers with known bugs (floats read with the wrong byte
order, datasets outside Vgroups missed, data in linked blocks not referenced).

refkv reads the HDF4 file layer itself (descriptors, Vgroups, Vdatas,
special elements) and writes an archive in which every scientific dataset is
a Zarr array, its chunks byte ranges of the file, and every Vgroup the writer
made a group, so that xarray opens a MODIS grid's fields as variables with
their dimension names and attributes.

## What you get

A Zarr v3 hierarchy of the file's scientific datasets (the SD interface's,
which most products use):

- **Groups** are the writer's Vgroups (MODIS's `GRID` Vgroups, SeaWiFS's
  `Per Scan Data`), nested as in the file, with their attributes. The SD
  interface's own Vgroups are bookkeeping, not groups; a dataset in no Vgroup
  is at the root. A Vgroup or dataset reached twice is presented once and
  linked from the other place in the source metadata.
- **Datasets** are arrays of `int8` to `uint32`, `float32` and `float64`, big-
  or little-endian by the file's number type. Their chunks are:
  - plain data cut into chunks of at most 1 MiB along the slowest axis;
  - data deflated whole, one chunk with `numcodecs.zlib`;
  - chunked data, HDF4's own chunks, plain or deflated (MODIS land products);
  - data in linked blocks, each chunk the ranges of its blocks.

  An unlimited dimension's length is its record count, and a dataset or chunk
  never written reads as its fill value: the chunked header's, the
  `_FillValue`, or netCDF's default, as the HDF4 library reads it.
- **Dimension names** are the SD interface's dimensions (`fakeDim<n>` where the
  writer named none), and a dimension scale is its coordinate variable, an
  array of the dimension's name.
- **Attributes** of the file, the groups and the datasets are Zarr attributes,
  as xarray reads netCDF attributes: text to its first NUL, numbers exactly.
  HDF-EOS2's `StructMetadata.0`, `CoreMetadata.0` and `ArchiveMetadata.0` are
  kept whole and verbatim.
- **Source metadata:** each node's `refkv_zarrified.hdf4` holds its Vgroup's
  ref and class, or a dataset's number type, storage, chunk shape and
  compression, and each attribute's type and count. A group lists what it
  does not present, each with the reason; the whole file is described on
  `refkv_source` by the IR mirror, from which it is rebuilt byte for byte.

**What is not presented yet**, and recorded instead: datasets of text
(`char8`) and Vdata tables, the next step; SZIP and the other codecs (RLE,
N-bit, skipping Huffman, JPEG); external elements; raster images and
annotations; and files the older DFSD interface wrote, which have no SD
dataset. [spec/zarrify/hdf4.md §7.4](../../../spec/zarrify/hdf4.md#74-what-is-not-presented)
lists them, and [design/plans/hdf4.md](../../../design/plans/hdf4.md) the
order of the work.

## Try it

[example.py](example.py) zarrifies a MODIS surface reflectance tile from
GDAL's server, opens its grid's fields with xarray, and plots the red band.
Its dependencies are in its header (PEP 723), so it runs on its own, with
refkv from PyPI:

```bash
uv run docs/zarrify/hdf4/example.py
```

The same tile with the CLI:

```bash
uv run refkv zarrify https://download.osgeo.org/gdal/data/hdf4/MOD09A1.A2010041.h06v03.005.2010051001103.hdf mod09a1.vzip
```

```
{"format": "hdf4", "groups": 3, "arrays": 13, "unpresented": {}, "chunks": 975, "descriptors": 2333, "rounds": 32, "chunk_entries": 975, ...}
```

It read 1.9 MB of the 4.7 MB file in 32 rounds; the archive is 320 KB. Its
13 fields are in the group of the grid's `Data Fields` Vgroup:

```python
import xarray as xr
import refkv.zarr

ds = xr.open_zarr(refkv.zarr.open("mod09a1.vzip"), group="MOD_Grid_500m_Surface_Reflectance/Data Fields",
                  consolidated=False, zarr_format=3)
print(ds["sur_refl_b01"])
```

## Viewing

The demo does not show HDF4 files yet. The arrays are not images
(no OME-NGFF metadata): open them with xarray or zarr-python.

## Supported and not supported

Supported:

- Files of any number of descriptor blocks; special elements of linked blocks,
  compression and chunking, nested as files nest them (a chunk table in linked
  blocks, a compressed stream in linked blocks, a linked element whose first
  block is empty).
- The SD interface's datasets, written or not, with an unlimited dimension or
  not, of the numeric number types, big- and little-endian.

Rejected:

- Files whose structure does not read: descriptor blocks past the end of the
  file or in a loop, elements past the end, two descriptors of one tag and
  ref, a special element of an unknown code, link tables in a loop, a chunk
  outside its dataset's grid, a compressed stream held in a special element other than linked
  blocks.
- More than 2^16 descriptor blocks, 2^20 descriptors, 2^20 chunks a dataset (or
  64 MiB of chunk table) or 2^20 blocks of a linked element.

[spec/zarrify/hdf4.md §8.3](../../../spec/zarrify/hdf4.md#83-checks) has
the messages.

## Performance

- Zarrifying reads the descriptor blocks first, 64 KiB at a time, which in
  MODIS files holds several blocks; then every header in one round; then the
  link tables, attribute and chunk table records and chunk headers they name.
  A file of 100 to 700 descriptors takes 2 to 4 rounds; MOD09A1, of 146
  descriptor blocks spread through the file, 32.
- The data is never read. Reading a plain dataset's chunk is one range request
  of at most 1 MiB; a chunked dataset's, HDF4's chunk as stored.
- The archive has one entry per chunk: 975 for the tile above.

## How it's verified

`python/tests/ir/test_hdf4.py` zarrifies the synthetic files of
[fixtures/hdf4/](../../../fixtures/hdf4/) (written with pyhdf and, where pyhdf
has no wrapper, the HDF4 library itself, by
[the generator](../../../fixtures/generators/hdf4/write_fixtures.py)) and
compares every dataset with what pyhdf reads: values, shape, dimension names
and attributes. `just verify-of hdf4` does the same through the browser code,
and checks that the mirror rebuilds each file; given a directory, it checks the
files of [corpus_hdf4.txt](../../../conformance/zarrify/corpus_hdf4.txt): all
103 datasets of GDAL's 7 HDF4 samples, and every SD dataset of the HDF4
library's test files that pyhdf reads, match. The Rust tests rebuild every
fixture byte for byte from its mirror and parse thousands of corrupted copies
without a panic.

- Convention and profile: [spec/zarrify/hdf4.md](../../../spec/zarrify/hdf4.md)
- Rust core: [rust/refkv-ir/src/hdf4/](../../../rust/refkv-ir/src/hdf4/), the projection [rust/refkv-ir/src/project/hdf4.rs](../../../rust/refkv-ir/src/project/hdf4.rs)
- Fixtures: [fixtures/hdf4/](../../../fixtures/hdf4/)
- Design: [design/plans/hdf4.md](../../../design/plans/hdf4.md)
