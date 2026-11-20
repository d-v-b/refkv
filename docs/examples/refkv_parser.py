"""A netCDF file opened as a VirtualiZarr virtual dataset through refkv's parser.

RefkvParser zarrifies the file with refkv and hands VirtualiZarr the result as a ManifestStore,
so open_virtual_dataset takes any file refkv can zarrify. This writes a small netCDF file into
the current directory, so it runs offline.
"""

from pathlib import Path

import numpy as np
import virtualizarr as vz
import xarray as xr
from obspec_utils.registry import ObjectStoreRegistry
from obstore.store import LocalStore

from refkv.virtualizarr import RefkvParser

path = Path("temperature.nc").resolve()
expected = xr.Dataset(
    {"t": (("time", "x"), np.arange(12.0).reshape(3, 4), {"units": "K"})},
    coords={"time": [0, 1, 2]},
)
expected.to_netcdf(path, engine="h5netcdf", encoding={"t": {"chunksizes": (1, 4)}})

registry = ObjectStoreRegistry({"file://": LocalStore()})
with vz.open_virtual_dataset(path.as_uri(), registry=registry, parser=RefkvParser(),
                             loadable_variables=["time"]) as vds:
    print(vds["t"].data.manifest.dict()["0.0"])  # the first chunk: a byte range of the file

# The parser's ManifestStore is also a Zarr store, which xarray reads directly.
ds = xr.open_zarr(RefkvParser()(path.as_uri(), registry), zarr_format=3, consolidated=False)
np.testing.assert_array_equal(ds["t"], expected["t"])
print("the virtual dataset's values equal the file's")
