# /// script
# requires-python = ">=3.12"
# dependencies = ["refkv[zarr]>=0.2.0", "matplotlib"]
# ///
"""GDAL's small RGB test raster (MIT), three times: stored band-sequential, band-interleaved by line
and band-interleaved by pixel. Each is zarrified by its `.hdr` header without copying it: refkv
finds the data file beside the header and presents its bytes as Zarr, a band or a line a chunk,
read only when plotted. All three give the same image.

Run with `uv run docs/zarrify/envi/example.py`; the figure is saved as envi.png.
"""

import matplotlib.pyplot as plt
import numpy as np
import zarr

import refkv.zarr
from refkv.zarrify import zarrify

BASE = "https://raw.githubusercontent.com/OSGeo/gdal/8a556e7b81aab50ec6612b47499ffbe942aa3a71/autotest/gdrivers/data/envi/"
INPUTS = ["envi_rgbsmall_bsq.hdr", "envi_rgbsmall_bil.hdr", "envi_rgbsmall_bip.hdr"]

fig, axes = plt.subplots(1, len(INPUTS), figsize=(3 * len(INPUTS), 3))
for ax, name in zip(axes, INPUTS, strict=True):
    root = zarr.open_group(refkv.zarr.store(zarrify(BASE + name)), mode="r")
    image = np.asarray(root.get_array("0")[...])  # [c, y, x]
    ax.imshow(np.moveaxis(image, 0, -1))
    ax.set_title(name, fontsize=8)
    ax.set_xticks([])
    ax.set_yticks([])

fig.tight_layout()
fig.savefig("envi.png", dpi=100)
plt.show()
