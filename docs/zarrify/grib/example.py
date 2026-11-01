# /// script
# requires-python = ">=3.12"
# dependencies = ["refkv[zarr]>=0.2.0", "xarray", "matplotlib"]
# ///
"""An HRRR forecast (NOAA, public domain), zarrified from NOAA's bucket without copying it: refkv
reads each message's header, through the file's index, and presents the fields as xarray
variables, each chunk a reference to one message of the file, read only when plotted.

Run with `uv run docs/zarrify/grib/example.py`; the archive is saved as hrrr.vzip, and the figure
as grib.png.
"""

import matplotlib.pyplot as plt
import xarray as xr

import refkv.zarr
from refkv.zarrify import zarrify

URL = "https://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.20261029/conus/hrrr.t00z.wrfsfcf06.grib2"

store = zarrify(URL)
store.save("hrrr.vzip")
ds = xr.open_zarr(refkv.zarr.store(store), group="grid0", consolidated=False, zarr_format=3)
t2m = ds["t_heightAboveGround"].sel(heightAboveGround=2.0).isel(time=0, step=0)
print(ds)

fig, ax = plt.subplots(figsize=(10, 6))
(t2m - 273.15).plot(ax=ax, cmap="RdYlBu_r", cbar_kwargs={"label": "°C"})
ax.set_title(f"HRRR 2 m temperature, valid {ds['valid_time'].values.ravel()[0]}")
ax.set_aspect("equal")
fig.tight_layout()
fig.savefig("grib.png", dpi=100)
plt.show()
