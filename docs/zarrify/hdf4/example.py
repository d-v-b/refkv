# /// script
# requires-python = ">=3.12"
# dependencies = ["refkv[zarr]", "xarray", "matplotlib"]
#
# [tool.uv.sources]
# refkv = { git = "https://github.com/d-v-b/refkv", subdirectory = "python" }
# ///
"""A MODIS surface reflectance tile (MOD09A1, NASA LP DAAC, of GDAL's HDF4 samples), zarrified from
GDAL's server without copying it: refkv reads the file's descriptors, Vgroups and chunk tables, and
presents the HDF-EOS grid's fields as xarray variables, each chunk a reference to one deflated chunk
of the file, read only when plotted.

Run with `uv run docs/zarrify/hdf4/example.py`; the archive is saved as mod09a1.vzip, and the
figure as hdf4.png.
"""

import matplotlib.pyplot as plt
import xarray as xr

import refkv.zarr
from refkv.zarrify import zarrify

URL = "https://download.osgeo.org/gdal/data/hdf4/MOD09A1.A2010041.h06v03.005.2010051001103.hdf"

store = zarrify(URL)
store.save_vzip("mod09a1.vzip")
fields = xr.open_zarr(refkv.zarr.store(store), group="MOD_Grid_500m_Surface_Reflectance/Data Fields",
                      consolidated=False, zarr_format=3, mask_and_scale=True)
print(fields)
red = fields["sur_refl_b01"]

fig, ax = plt.subplots(figsize=(7, 6))
red.plot(ax=ax, cmap="gray", vmin=0, vmax=0.4, cbar_kwargs={"label": red.attrs.get("long_name", "")})
ax.set_title("MOD09A1 band 1 (red), 2010-02-10, tile h06v03")
ax.set_aspect("equal")
fig.tight_layout()
fig.savefig("hdf4.png", dpi=100)
plt.show()
