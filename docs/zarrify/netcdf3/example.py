# /// script
# requires-python = ">=3.12"
# dependencies = ["refkv[zarr]", "xarray", "matplotlib"]
#
# [tool.uv.sources]
# refkv = { git = "https://github.com/d-v-b/refkv", subdirectory = "python" }
# ///
"""xarray's `air_temperature.nc` tutorial file (NCEP reanalysis, 4x daily, 2013-2014; a netCDF3
classic file of 7.8 MB), zarrified from GitHub without copying it: refkv reads the file's header,
and presents each variable as an xarray variable, each chunk a reference to its bytes in the file,
read only when plotted.

Run with `uv run docs/zarrify/netcdf3/example.py`; the archive is saved as air.vzip, and the
figure as netcdf3.png.
"""

import matplotlib.pyplot as plt
import xarray as xr

import refkv.zarr
from refkv.zarrify import zarrify

URL = "https://raw.githubusercontent.com/pydata/xarray-data/a35297e9da2cc99c811014f0c8a4297345a5c28d/air_temperature.nc"

store = zarrify(URL)
store.save_vzip("air.vzip")
ds = xr.open_zarr(refkv.zarr.store(store), consolidated=False, zarr_format=3)
print(ds)
air = ds["air"].isel(time=0)

fig, ax = plt.subplots(figsize=(8, 4))
air.plot(ax=ax, cmap="RdBu_r", cbar_kwargs={"label": ds["air"].attrs.get("units", "")})
ax.set_title(f"Air temperature, {str(air.time.values)[:16]}")
fig.tight_layout()
fig.savefig("netcdf3.png", dpi=100)
plt.show()
