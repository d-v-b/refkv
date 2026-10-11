# Virtual datasets with xarray

Many netCDF or HDF5 files become one virtual dataset, which
[VirtualiZarr](https://virtualizarr.readthedocs.io/) builds and vzip saves as
one small archive that records where each chunk lives in the original files.
xarray opens the archive as an ordinary lazy dataset, and reads each chunk
from the file it came from: no data is copied.

## Install

```bash
pip install "vzip[virtualizarr] @ git+https://github.com/d-v-b/vzip"
```

The `virtualizarr` extra installs VirtualiZarr, xarray, h5py, and what xarray
needs to read netCDF files over HTTPS (h5netcdf, fsspec, aiohttp). Without
it, vzip reads and writes archives, and holds a `ReferenceStore` built from a
mapping. (In a clone of this repository, `uv sync` installs every extra.)

## An example: five years of sea surface temperature

[NOAA OISST v2.1](https://www.ncei.noaa.gov/products/optimum-interpolation-sst) is one
netCDF file per day on a public S3 bucket. The example virtualizes the 1st of
each month from 2015 to 2019, 60 files, and saves them as `oisst.vzip`. It
then reopens the archive with xarray, computes the yearly mean temperature
over the Niño 3.4 region, and checks one day against the netCDF file. It runs
in about 2 minutes, and reads about 7 MB of metadata and 40 MB of data.
The file is [examples/oisst.py](examples/oisst.py):

<!-- include: examples/oisst.py -->
```python
"""Five years of NOAA OISST v2.1 as one virtual dataset, saved as a vzip archive.

The daily netCDF files stay on AWS: the archive records where each chunk lives,
and xarray reads the chunks from those files. This takes the 1st of each month,
60 files: about 1.5 minutes to virtualize, and 40 MB of SST to read.
"""

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

# 1. Virtualize the files, reading only their metadata in 64 kB blocks, and concatenate along time.
s3 = S3Store(bucket, region="us-east-1", skip_signature=True)
registry = ObjectStoreRegistry({f"s3://{bucket}": s3})
parser = HDFParser(reader_factory=partial(BlockStoreReader, block_size=2**16))
vds = vz.open_virtual_mfdataset(urls, registry=registry, parser=parser)

# 2. Save it as a vzip archive, naming the files by their public HTTPS URLs.
store = vzip.ReferenceStore.from_virtualizarr(vds, rewrite_urls=vzip.public_s3_url)
print(store.save("oisst.vzip"))

# 3. Reopen it with xarray: the yearly mean SST over the Nino 3.4 region.
stats = vzip.Stats()
ds = xr.open_zarr(vzip.open("oisst.vzip", stats=stats))
nino34 = ds.sst.sel(lat=slice(-5, 5), lon=slice(190, 240))
print(nino34.mean(["lat", "lon", "zlev"]).groupby("time.year").mean().to_series().round(2))
print(f"read {stats.external_bytes / 1e6:.0f} MB in {stats.external_requests} requests to the netCDF files")

# 4. Check the last day against the netCDF file itself. xarray decodes the archive's
#    sst as float64 and the file's as float32 (see the troubleshooting notes), so cast it.
with fsspec.open(vzip.public_s3_url(urls[-1])) as f:
    direct = xr.open_dataset(f, engine="h5netcdf").sst.sel(lat=slice(-5, 5), lon=slice(190, 240))
    np.testing.assert_array_equal(nino34.sel(time=direct.time).astype("float32"), direct)
print("the archive's values equal the netCDF file's on", days[-1].date())
```

It prints:

```text
oisst.vzip: 312 keys, 240 of them references to 60 sources; 63,609 bytes
year
2015    28.35
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
   and records each chunk as a byte range of the file. The `S3Store` reads
   the public bucket without credentials. The `HDFParser` reads the files in
   64 kB blocks; with its default, 1 MiB blocks, it downloads the whole of
   each 1.7 MB file (see [troubleshooting](#troubleshooting)).
2. **Save.** `ReferenceStore.from_virtualizarr` turns the virtual dataset into
   a store: Zarr metadata, the coordinates as bytes, and each chunk as a
   reference. `rewrite_urls=vzip.public_s3_url` writes each file's URL as
   `https://<bucket>.s3.amazonaws.com/<key>`, which any reader can read
   ([publishing](#publishing)). `save` writes the archive, and returns a
   summary of it.
3. **Compute.** `vzip.open` opens the archive as a read-only Zarr store, and
   `xr.open_zarr` reads it like any Zarr dataset. The mean reads one chunk
   per day, from the netCDF files themselves, with at most 8 requests in
   flight (`max_concurrency`, 8 by default). `vzip.Stats` counts them.
4. **Check.** The direct read of one file gives the same values. The archive
   decodes `sst` as float64 and the file as float32, so the check casts the
   archive's values to float32. The cast is exact: `sst` is stored as int16
   with a float32 `scale_factor`, and the product of the two is exact in
   float64, so rounding it to float32 gives the value netCDF computes.
   [Troubleshooting](#troubleshooting) says where the float64 comes from.

## Scaling up

- **Every day.** `freq="D"` takes all 1,826 days of the five years. Each
  file costs about 1.4 s to virtualize, one at a time, so that takes about
  40 minutes. VirtualiZarr's `parallel=` takes an executor class and starts
  its own number of workers, which can be more than a host would like; read
  its documentation before you use it.
- **The page index.** Without one, a reader loads the whole central
  directory when it opens the archive, about 125 bytes per key. Once that
  passes a few MB, save with `store.save("oisst.vzip", page_size=65536)`.
  A reader then loads only the pages that hold the keys it reads
  ([spec/archive.md §12](../spec/archive.md#12-page-index-optional)). This
  matters most for an archive read over the network.
- **Requests.** `vzip.open(..., max_concurrency=16)` allows more requests in
  flight, and `max_concurrency=None` removes the cap. Respect the host's
  limits.

## Publishing

`rewrite_urls=vzip.public_s3_url` gives HTTPS URLs that any reader can
read with no credentials: vzip in Python, the
[JavaScript reader](../js/README.md), and a web page in the browser (if the
bucket allows cross-origin requests). Serve the archive over HTTPS, and anyone can
open it with `xr.open_zarr(vzip.open("https://example.org/oisst.vzip"))`.
`save(..., relative_to="https://example.org/data/")` writes the URLs under
that prefix relative to the archive, so the archive and its files can move
together.

For private data, read the files with the stores you already have. Every
vzip store takes `registry=`: the `ObjectStoreRegistry` you gave VirtualiZarr,
or a mapping of URL prefixes to stores. The archive's readers need the same
stores, so tell `save` which policy they will read it with:

```python
from obstore.store import S3Store

registry = {"s3://my-bucket": S3Store("my-bucket", region="us-west-2")}
store = vzip.ReferenceStore.from_virtualizarr(vds, registry=registry)
store.save("private.vzip", policy=vzip.Policy(schemes={"s3"}))
ds = xr.open_zarr(vzip.open("private.vzip", registry=registry))
```

A store you register is you allowing its URLs: a source under a registered
prefix passes the reader policy's scheme rule (rule 1 of
[spec/archive.md §6.7](../spec/archive.md#67-reader-policy)) without
`Policy(schemes=...)`. The rest of the policy still applies. A
`Policy(prefixes=...)` list still decides which URLs are allowed, registered
or not. An archive opened from a URL still may not read local files. vzip
still refuses an `http(s)` URL whose host is written as a private, loopback
or link-local address. It cannot check the addresses a registered store
connects to, or the redirects it follows, since the store makes its own
connections. vzip makes no store for `s3://`, `gs://` or `az://` URLs itself:
obstore would look for credentials in the environment, in configuration
files and at the cloud metadata service (169.254.169.254), out of the
policy's reach.

## Troubleshooting

- **The values are float64, the netCDF file's float32.** This is upstream:
  Zarr attributes are JSON, so `scale_factor` and `add_offset` lose their
  float32 dtype, and xarray then decodes a packed int16 variable as float64
  (CF's rule; [pydata/xarray#8713](https://github.com/pydata/xarray/issues/8713),
  [discussion #9254](https://github.com/pydata/xarray/discussions/9254)).
  Every Zarr store does this, `to_zarr`'s and VirtualiZarr's own included.
  Cast with `ds["sst"].astype("float32")`, or compare packed values with
  `xr.open_zarr(..., mask_and_scale=False)`.
- **Virtualizing downloads whole files.** VirtualiZarr's `HDFParser()` reads
  in 1 MiB blocks by default, which is the whole of a small file. Give it
  smaller blocks:
  `HDFParser(reader_factory=partial(BlockStoreReader, block_size=2**16))`,
  with `BlockStoreReader` from `obspec_utils.readers`.
- **`the scheme 's3' is not allowed`** (reading) or **`not saving ...: the
  default reader policy refuses ...`** (saving). The references name
  `s3://` URLs, which the default reader policy refuses. For a public
  bucket, pass `rewrite_urls=vzip.public_s3_url` to `from_virtualizarr`. For
  a private one, pass `registry=` to read, and
  `policy=vzip.Policy(schemes={"s3"})` to `save` ([publishing](#publishing)).
- **`no store is registered for s3://...`.** The policy allows the scheme,
  but no store serves the URL; pass `registry={"s3://bucket": S3Store(...)}`.
- **`the request would go through the proxy ...`.** vzip checks the
  address each request goes to, and cannot check one a proxy connects to.
  Exempt the host with `no_proxy`, or pass
  `policy=vzip.Policy(allow_unchecked_proxy=True)`.

The docstrings say more: `help(vzip.ReferenceStore)`, `help(vzip.open)`,
`help(vzip.Policy)` and `help(vzip.store.Resolver)`.
