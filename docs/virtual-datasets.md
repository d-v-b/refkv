# Virtual datasets with xarray

Many netCDF or HDF5 files become one virtual dataset, which
[VirtualiZarr](https://virtualizarr.readthedocs.io/) builds and vzip saves as
one small archive that records where each chunk lives in the original files.
xarray opens the archive as an ordinary lazy dataset, and reads each chunk
from the file it came from: no data is copied.

## Install

<!-- This installs main, which has the virtualizarr extra once this page is merged; do not pin a branch. -->
```bash
pip install "vzip[virtualizarr] @ git+https://github.com/d-v-b/vzip"
```

The `virtualizarr` extra installs VirtualiZarr, xarray, h5py, h5netcdf,
fsspec and aiohttp, and with them obstore, obspec_utils and pandas, which the
example also imports. Without it, vzip reads and writes archives, and holds a
`ReferenceStore` built from a mapping. (In a clone of this repository,
`uv sync` installs every extra.)

## An example: five years of sea surface temperature

[NOAA OISST v2.1](https://www.ncei.noaa.gov/products/optimum-interpolation-sst) is one
netCDF file per day on a public S3 bucket. The example virtualizes the 1st of
each month from 2015 to 2019, 60 files, and saves them as `oisst.vzip`. It
then reopens the archive with xarray, computes the yearly mean temperature
over the Niño 3.4 region, and checks one day against the netCDF file. It
runs in 1 to 1.5 minutes, about 40 s of it to virtualize the files, and reads
about 7 MB of metadata and 40 MB of data. The file is
[examples/oisst.py](examples/oisst.py):

<!-- include: examples/oisst.py -->
```python
"""Five years of NOAA OISST v2.1 as one virtual dataset, saved as a vzip archive.

The daily netCDF files stay on AWS: the archive records where each chunk lives,
and xarray reads the chunks from those files. This takes the 1st of each month,
60 files: 1 to 1.5 minutes in all, about 40 s of it to virtualize them.
"""

from concurrent.futures import ThreadPoolExecutor
from functools import partial

import fsspec
import numpy as np
import pandas as pd
import virtualizarr as vz
import xarray as xr
from obspec_utils.readers import BlockStoreReader
from obspec_utils.registry import ObjectStoreRegistry
from obstore.store import S3Store
from virtualizarr.parsers import HDFParser

import vzip

bucket = "noaa-cdr-sea-surface-temp-optimum-interpolation-pds"
days = pd.date_range("2015-01-01", "2019-12-31", freq="MS")  # freq="D" for every day
urls = [f"s3://{bucket}/data/v2.1/avhrr/{d:%Y%m}/oisst-avhrr-v02r01.{d:%Y%m%d}.nc" for d in days]


class EightThreads(ThreadPoolExecutor):  # parallel= takes a class, which VirtualiZarr instantiates
    def __init__(self):
        super().__init__(max_workers=8)


# 1. Virtualize the files, 8 at a time, reading only their metadata, and concatenate along time.
registry = ObjectStoreRegistry({f"s3://{bucket}": S3Store(bucket, region="us-east-1", skip_signature=True)})
parser = HDFParser(reader_factory=partial(BlockStoreReader, block_size=2**16))
vds = vz.open_virtual_mfdataset(
    urls, registry=registry, parser=parser, parallel=EightThreads, combine_attrs="drop_conflicts"
)

# 2. Save it as a vzip archive, naming the files by their public HTTPS URLs.
store = vzip.ReferenceStore.from_virtualizarr(vds, rewrite_urls=vzip.public_s3_url)
print(store.save("oisst.vzip"))

# 3. Reopen it with xarray: the yearly mean SST over the Nino 3.4 region, weighted by area.
stats = vzip.Stats()
ds = xr.open_zarr(vzip.open("oisst.vzip", stats=stats))
nino34 = ds.sst.sel(lat=slice(-5, 5), lon=slice(190, 240))
mean = nino34.weighted(np.cos(np.deg2rad(nino34.lat))).mean(["lat", "lon", "zlev"])
print(mean.groupby("time.year").mean().to_series().round(2))
print(f"read {stats.external_bytes / 1e6:.0f} MB in {stats.external_requests} requests to the netCDF files")

# 4. Check the last day against the netCDF file itself (float32: see troubleshooting).
with fsspec.open(vzip.public_s3_url(urls[-1])) as f:
    direct = xr.open_dataset(f, engine="h5netcdf").sst.sel(lat=slice(-5, 5), lon=slice(190, 240))
    np.testing.assert_array_equal(nino34.sel(time=direct.time).astype("float32"), direct)
print("the archive's values equal the netCDF file's on", days[-1].date())
```

It prints:

```text
oisst.vzip: 312 keys, 240 of them references to 60 sources; 63,376 bytes
year
2015    28.34
2016    27.49
2017    26.90
2018    27.04
2019    27.61
Name: sst, dtype: float64
read 40 MB in 60 requests to the netCDF files
the archive's values equal the netCDF file's on 2019-12-01
```

What each step does:

1. **Virtualize.** `open_virtual_mfdataset` reads each file's HDF5 metadata,
   records each chunk as a byte range, and concatenates the files along
   time. The `S3Store` reads the public bucket without credentials, the
   `HDFParser` reads in 64 kB blocks rather than whole files
   ([troubleshooting](#troubleshooting)), and `EightThreads` reads 8 files
   at a time ([scaling up](#scaling-up)). `combine_attrs="drop_conflicts"`
   drops the attributes that differ between files (`id`, `date_created`,
   `date_modified`, `time_coverage_*`), rather than keeping the first file's.
2. **Save.** `from_virtualizarr` turns the virtual dataset into a store:
   Zarr metadata, the coordinates as bytes, and each chunk as a reference.
   `rewrite_urls=vzip.public_s3_url` names each file by its public HTTPS URL
   ([publishing](#publishing)). `save` writes the archive and summarizes it.
3. **Compute.** `vzip.open` opens the archive as a read-only Zarr store for
   `xr.open_zarr`, which reads chunks from the netCDF files, at most 8
   requests at a time. The files' chunking stays: a chunk of `sst` is a whole day's global
   field, 720×1440, about 0.65 MB, so even one grid cell costs that per day
   (a year of one cell read 239 MB). The mean is weighted by cos(latitude),
   each cell's relative area; within ±5° that moves it by at most 0.01 °C.
4. **Check.** The netCDF file gives the same values, once the archive's
   float64 is cast to float32, which is exact
   ([troubleshooting](#troubleshooting)).

## Scaling up

- **Every day.** `freq="D"` takes every day. A year of daily files took
  156 s to virtualize with 8 threads, about 0.4 s per file, against about
  1.2 s per file one at a time. `parallel=` takes an executor class and
  instantiates it with no arguments, hence the `EightThreads` subclass,
  which keeps the host to 8 connections.
- **Reuse the archive.** That is the point of saving it: steps 3 and 4 rerun
  from `vzip.open("oisst.vzip")` without virtualizing again, so guard steps
  1 and 2 with `if not Path("oisst.vzip").exists():` while you iterate.
- **The page index.** Without one, a reader loads the whole central
  directory at open, about 125 bytes per key. A year of daily OISST is
  1,837 keys in a 263 kB archive: no index needed. At 259,200 keys, the
  directory was 18.8 MB, and a page index cut opening to 2 requests and
  115 kB ([design/COMPARISON.md](../design/COMPARISON.md#opening)). Once the
  directory passes a few MB, tens of thousands of keys, save with
  `store.save("oisst.vzip", page_size=65536)`
  ([spec §12, Page index](../spec/archive.md#12-page-index-optional)).
- **Requests.** `vzip.open(..., max_concurrency=16)` allows more requests in
  flight, and `max_concurrency=None` removes the cap. Respect the host's
  limits.

## Publishing

`rewrite_urls=vzip.public_s3_url` gives HTTPS URLs that any reader can read
without credentials: vzip in Python, the [JavaScript reader](../js/README.md),
and a browser (if the bucket allows cross-origin requests). Serve the archive
over HTTPS, and anyone can open it with
`xr.open_zarr(vzip.open("https://example.org/oisst.vzip"))`.
`save(..., relative_to="https://example.org/data/")` writes URLs under that
prefix relative to the archive, so the two can move together.

For private data, every vzip store takes `registry=`: the
`ObjectStoreRegistry` you gave VirtualiZarr, or a mapping of URL prefixes to
stores. Readers need the same stores, so tell `save` their policy:

```python
from obstore.store import S3Store

registry = {"s3://my-bucket": S3Store("my-bucket", region="us-west-2")}
store = vzip.ReferenceStore.from_virtualizarr(vds, registry=registry)
store.save("private.vzip", policy=vzip.Policy(schemes={"s3"}))
ds = xr.open_zarr(vzip.open("private.vzip", registry=registry))
```

By default, vzip reads `http(s)` URLs (and local files, from a local
archive), and never builds an `s3://`, `gs://` or `az://` store itself, so it
cannot pick up your cloud credentials by accident. For a private bucket,
register its store, as above; that allows its URLs. The rules are in
[spec §6.7, Reader policy](../spec/archive.md#67-reader-policy).

## Troubleshooting

- **The values are float64, the netCDF file's float32.** Zarr attributes
  are JSON, so `scale_factor` and `add_offset` lose their float32 dtype, and
  xarray decodes a packed int16 variable as float64 (CF's rule;
  [pydata/xarray#8713](https://github.com/pydata/xarray/issues/8713)). Every
  Zarr store does this, `to_zarr`'s and VirtualiZarr's included.
  Cast with `ds["sst"].astype("float32")`; for OISST this is exact, since
  an int16 times a float32 scale factor is exact in float64. Or compare
  packed values with `xr.open_zarr(..., mask_and_scale=False)`.
- **Virtualizing downloads whole files.** `HDFParser()` reads in 1 MiB
  blocks by default, the whole of a 1.7 MB OISST file. Give it smaller
  blocks, as the example does, with `BlockStoreReader` from
  `obspec_utils.readers`.
- **Virtualizing is slow, or opens too many connections.** Pass
  `parallel=` a capped executor class ([scaling up](#scaling-up)).
- **The attributes describe only the first file** (its `id`,
  `time_coverage_start`). Pass `combine_attrs="drop_conflicts"`.
- **`the scheme 's3' is not allowed`** (reading) or **`not saving ...: the
  default reader policy refuses ...`** (saving). The references name
  `s3://` URLs, which the default policy refuses. For a public
  bucket, pass `rewrite_urls=vzip.public_s3_url` to `from_virtualizarr`. For
  a private one, pass `registry=` to read, and
  `policy=vzip.Policy(schemes={"s3"})` to `save` ([publishing](#publishing)).
- **`no store is registered for s3://...`.** The policy allows the scheme,
  but no store serves the URL; pass `registry={"s3://bucket": S3Store(...)}`.
- **`the request would go through the proxy ...`.** vzip checks the
  address each request goes to, and cannot check one a proxy connects to.
  Exempt the host with `no_proxy`, or pass
  `policy=vzip.Policy(allow_unchecked_proxy=True)`.

The docstrings say more: `help(vzip.ReferenceStore)`, `help(vzip.open)`
(its `registry=` and `max_concurrency=`), and `help(vzip.Policy)`.
