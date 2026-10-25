"""Five years of NOAA OISST v2.1 as one virtual dataset, saved as a vzip archive.

The daily netCDF files stay on AWS: the archive records where each chunk lives,
and xarray reads the chunks from those files. This takes the 1st of each month,
60 files: 1 to 1.5 minutes in all, about 40 s of it to zarrify them.
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
from obstore.store import S3Store  # refkv[obstore], which refkv[virtualizarr] includes
from virtualizarr.parsers import HDFParser

import refkv

bucket = "noaa-cdr-sea-surface-temp-optimum-interpolation-pds"
days = pd.date_range("2015-01-01", "2019-12-31", freq="MS")  # freq="D" for every day
urls = [f"s3://{bucket}/data/v2.1/avhrr/{d:%Y%m}/oisst-avhrr-v02r01.{d:%Y%m%d}.nc" for d in days]


class EightThreads(ThreadPoolExecutor):  # parallel= takes a class, which VirtualiZarr instantiates
    def __init__(self):
        super().__init__(max_workers=8)


# 1. Zarrify the files, 8 at a time, reading only their metadata, and concatenate along time.
registry = ObjectStoreRegistry({f"s3://{bucket}": S3Store(bucket, region="us-east-1", skip_signature=True)})
parser = HDFParser(reader_factory=partial(BlockStoreReader, block_size=2**16))
vds = vz.open_virtual_mfdataset(
    urls, registry=registry, parser=parser, parallel=EightThreads, combine_attrs="drop_conflicts"
)

# 2. Save it as a vzip archive, naming the files by their public HTTPS URLs.
store = refkv.ReferenceStore.from_virtualizarr(vds, rewrite_urls=refkv.public_s3_url)
print(store.save("oisst.vzip"))

# 3. Reopen it with xarray: the yearly mean SST over the Nino 3.4 region, weighted by area.
stats = refkv.Stats()
ds = xr.open_zarr(refkv.zarr.open("oisst.vzip", stats=stats))
nino34 = ds.sst.sel(lat=slice(-5, 5), lon=slice(190, 240)).load()  # read each chunk once
mean = nino34.weighted(np.cos(np.deg2rad(nino34.lat))).mean(["lat", "lon", "zlev"])
print(mean.groupby("time.year").mean().to_series().round(2))
print(f"read {stats.external_bytes / 1e6:.0f} MB in {stats.external_requests} requests to the netCDF files")

# 4. Check the last day against the netCDF file itself (float32: see troubleshooting).
with fsspec.open(refkv.public_s3_url(urls[-1])) as f:
    direct = xr.open_dataset(f, engine="h5netcdf").sst.sel(lat=slice(-5, 5), lon=slice(190, 240))
    np.testing.assert_array_equal(nino34.sel(time=direct.time).astype("float32"), direct)
print("the archive's values equal the netCDF file's on", days[-1].date())
