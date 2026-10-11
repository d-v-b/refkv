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
