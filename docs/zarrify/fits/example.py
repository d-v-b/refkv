# /// script
# requires-python = ">=3.12"
# dependencies = ["refkv[zarr]", "matplotlib"]
#
# [tool.uv.sources]
# refkv = { git = "https://github.com/d-v-b/refkv", subdirectory = "python" }
# ///
"""A Legacy Surveys DR10 g-band brick, RICE tile-compressed (100 x 100 float32 tiles on 3600 x
3600), zarrified without copying it: refkv reads the file's structure and presents it as Zarr, each
chunk a reference to the file's own bytes, read only when plotted.

Run with `uv run docs/zarrify/fits/example.py`; the figure is saved as fits.png.
"""

import matplotlib.pyplot as plt
import numpy as np
import zarr

import refkv.zarr
from refkv.zarrify import zarrify

URL = "https://portal.nersc.gov/cfs/cosmo/data/legacysurvey/dr10/south/coadd/000/0001m002/legacysurvey-0001m002-image-g.fits.fz"

root = zarr.open_group(refkv.zarr.store(zarrify(URL)), mode="r")
image = root.get_array("1")  # HDU 1, [axis2, axis1]: the compressed image
cut = np.asarray(image[1500:2100, 1200:1800])  # a 600 x 600 cutout: the six 100 x 1800 chunks it spans are decoded
lo, hi = np.percentile(cut, [1, 99.5])
fig, ax = plt.subplots(figsize=(6, 6))
ax.imshow(np.arcsinh((cut - lo) / (hi - lo) * 10), cmap="gray", origin="lower")
ax.set_title("legacysurvey-0001m002, g band")
ax.axis("off")

fig.tight_layout()
fig.savefig("fits.png", dpi=100)
plt.show()
