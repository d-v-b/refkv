# netCDF3

refkv turns a file into a Zarr hierarchy by writing a small archive of
references to the file's bytes, without copying the data
([overview](../README.md)). This page is about netCDF3 files: the netCDF
classic format and its 64-bit offset and 64-bit data variants.

## Who this is for

netCDF3 is the format of decades of climate, ocean and atmosphere data: model
output, reanalyses, station and buoy records, and the files many tools still
write by default (scipy, older netCDF libraries, CDO and NCO's classic modes).
It has no chunks and no compression, so a reader that wants one variable over
HTTP must know where the file put it. refkv reads the header, which says
exactly that, and writes an archive in which every variable is a Zarr array,
its chunks byte ranges of the file, so that xarray opens it as it opens the
file. It covers what VirtualiZarr's `NetCDF3Parser` reads.

## What you get

A Zarr v3 hierarchy, flat as the file is:

- **An array per variable**, at its name, with its dimension names, its
  attributes and a fill value: the `_FillValue`, or netCDF's default for the
  type.
- **Every type** of the three formats: `int8`, `int16`, `int32`, `float32` and
  `float64`, and in CDF-5 also `uint8` to `uint64` and `int64`, all big-endian.
  A `char` variable is an array of single characters, as netCDF-C reads it;
  xarray joins its last axis into strings when it decodes it.
- **Chunks:** a fixed-size variable's values are cut into chunks of at most
  1 MiB along its slowest axes; a record variable is one chunk per record, so
  that each chunk is one range of the file (the file interleaves the record
  variables a record at a time).
- **Attributes** of the file and of each variable, as xarray reads netCDF
  attributes: text without its trailing NULs, numbers exactly (64-bit integers
  too), NaN and infinities as Zarr writes them, and `_FillValue` in the form
  xarray's Zarr backend decodes.
- **Source metadata:** the root's `refkv_zarrified.netcdf3` holds the format
  and version, the record count, the record size and every dimension, the
  unused ones too; each array's holds its variable's position, type, `begin`
  and `vsize`. The whole header is on `refkv_source`, the IR mirror, from which
  the file is rebuilt byte for byte.

**What is not presented**, and recorded instead: a variable whose name no Zarr
node may take (empty, `.`, `..`, `zarr.json`, starting with `__`, holding `/`,
not UTF-8, or `refkv_source`), or that repeats a name; an attribute named
`zarr_conventions` or `refkv_zarrified`; and a `_FillValue` that xarray's Zarr
backend cannot decode (on a `char` variable, a NaN on an integer variable).
[spec/zarrify/netcdf3.md](../../../spec/zarrify/netcdf3.md) gives each reason.

## Try it

[example.py](example.py) zarrifies xarray's `air_temperature.nc` tutorial file
(NCEP reanalysis, a CDF-1 file of 7.8 MB) from GitHub, opens it with xarray,
and plots one time step. Its dependencies are in its header (PEP 723), so it
runs on its own, with refkv from PyPI:

```bash
uv run docs/zarrify/netcdf3/example.py
```

The same file with the CLI:

```bash
uv run refkv zarrify https://raw.githubusercontent.com/pydata/xarray-data/a35297e9da2cc99c811014f0c8a4297345a5c28d/air_temperature.nc air.vzip
```

```
{"format": "netcdf3", "dimensions": 3, "variables": 4, "unpresented": {}, "records": 0, "header": 1336, "rounds": 1, "chunk_entries": 11, ...}
```

It read the first 64 KiB in one round; the archive is 11 KB.

```python
import xarray as xr
import refkv.zarr

ds = xr.open_zarr(refkv.zarr.open("air.vzip"), consolidated=False, zarr_format=3)
print(ds["air"])
```

With `layout=Layout("refkv.example.bare")` the hierarchy has no refkv
declarations and no IR mirror: the variables and their attributes alone, as
xarray writes a dataset.

## Viewing

The demo does not show netCDF3 files: their variables are not images (no
OME-NGFF metadata). Open them with xarray or zarr-python.

## Supported and not supported

Supported:

- CDF-1, CDF-2 and CDF-5 files, of any number of dimensions, attributes and
  variables, with or without an unlimited dimension, with fixed-size and record
  variables of every type, one record variable alone (its records unpadded)
  among them, and scalars.
- A streaming record count (all ones), which the records the file holds
  replace, as netCDF-C's documentation says.
- Names stored with a trailing NUL, read to the NUL as netCDF-C reads them.

Rejected:

- A header that runs past the file or past 64 MiB; a list of the wrong tag; an
  unknown type, or one of CDF-5 in a CDF-1 or CDF-2 file; a dimension that is
  not the file's, or an unlimited one after a variable's first axis.
- A variable of more than 2^64 bytes; data that runs past the file or begins
  inside the header; variables, record slices or records that overlap.

netCDF-C opens some of the rejected files, reading bytes past the file's end as
zeros; refkv refuses to reference bytes the file does not hold.
[spec/zarrify/netcdf3.md §7.4](../../../spec/zarrify/netcdf3.md#74-checks) has
the messages.

## Performance

- Zarrifying reads the header only: the first 64 KiB, and a prefix twice as long
  while the header runs past it. A header of thousands of variables takes a few
  rounds; the data is never read.
- Reading a fixed-size variable's chunk is one range request of at most 1 MiB,
  and a record variable's one request a record. A record variable of rank 1,
  such as a time coordinate, is one chunk per value: netCDF-C's
  `ref_nccopy3_subset.nc`, 367 variables of 10 records, makes 1141 chunks.
- The archive has one entry per chunk: 11 for the file above.

## How it's verified

`python/tests/ir/test_netcdf3.py` zarrifies the files of
[fixtures/netcdf3/](../../../fixtures/netcdf3/) (written byte by byte by
[the generator](../../../fixtures/generators/netcdf3/write_fixtures.py), from
the format specification), and files netCDF-C writes through netCDF4-python in
each of the three formats, and compares every variable with what netCDF-C reads,
and with scipy for CDF-1 and CDF-2: values, shape, data type, dimension names and
attributes. It then opens each with xarray, from the file and from the archive,
and compares the datasets. `python/tests/ir/test_netcdf3_stored.py` compares the
stored-array descriptions with netCDF-C's values. `just verify-of netcdf3` does
the same through the browser code, and checks that the mirror rebuilds each
file; given a directory, it checks the files of
[corpus_netcdf3.txt](../../../conformance/zarrify/corpus_netcdf3.txt): netCDF-C's
own test files, scipy's, and xarray's tutorial datasets, every variable of which
matches. The Rust tests rebuild every accepted fixture from its IR, and cut and
corrupt two of them byte by byte without a panic.

netCDF-C 4.10 cannot read a streaming record count (it loops on CDF-1 and CDF-2
files, and crashes on CDF-5), so the streaming fixture is compared with the same
file with its count written out.
