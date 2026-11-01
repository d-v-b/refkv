# /// script
# requires-python = ">=3.12"
# dependencies = ["refkv[zarr]>=0.2.0", "matplotlib"]
# ///
"""An Imaris file: Convallaria, three confocal channels, zarrified without copying it: refkv reads
the file's structure and presents it as Zarr, each chunk a reference to the file's own bytes, read
only when plotted.

Run with `uv run docs/zarrify/ims/example.py`; the figure is saved as ims.png.
"""

import matplotlib.pyplot as plt
import numpy as np
import zarr

import refkv.zarr
from refkv.zarrify import zarrify

URL = "https://downloads.openmicroscopy.org/images/Imaris-IMS/davemason/Convallaria_3C_1T_confocal.ims"

root = zarr.open_group(refkv.zarr.store(zarrify(URL)), mode="r")
image = np.asarray(root.get_array("0")[...], dtype=np.float32)  # [c, y, x]
lo = np.percentile(image, 1, axis=(1, 2), keepdims=True)
hi = np.percentile(image, 99.5, axis=(1, 2), keepdims=True)
rgb = np.clip((image - lo) / (hi - lo), 0, 1).transpose(1, 2, 0)  # channels as red, green, blue
fig, ax = plt.subplots(figsize=(6, 6))
ax.imshow(rgb)
ax.set_title("Convallaria, three channels")
ax.axis("off")

fig.tight_layout()
fig.savefig("ims.png", dpi=100)
plt.show()
