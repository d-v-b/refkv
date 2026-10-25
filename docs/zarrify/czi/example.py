# /// script
# requires-python = ">=3.12"
# dependencies = ["refkv[zarr,images]", "matplotlib"]
#
# [tool.uv.sources]
# refkv = { git = "https://github.com/d-v-b/refkv", subdirectory = "python" }
# ///
"""A Zeiss CZI plate scan (IDR idr0011, three channels, 21 z-planes), zarrified without copying it:
refkv reads the file's structure and presents it as Zarr, each chunk a reference to the file's own
bytes, read only when plotted.

Run with `uv run docs/zarrify/czi/example.py`; the figure is saved as czi.png.
"""

import matplotlib.pyplot as plt
import numpy as np
import zarr

import refkv.zarr
from refkv.zarrify import zarrify

URL = "https://downloads.openmicroscopy.org/images/Zeiss-CZI/idr0011/Plate1-Blue-A_TS-Stinger/Plate1-Blue-A-02-Scene-1-P2-E1-01.czi"

root = zarr.open_group(refkv.zarr.store(zarrify(URL)), mode="r")
image = root.get_array("0/0")  # [c, z, y, x], the first series' full resolution
z = image.shape[1] // 2
planes = np.asarray(image[:, z])  # one plane per channel: three chunks
fig, axes = plt.subplots(1, len(planes), figsize=(4 * len(planes), 4))
for c, (ax, plane) in enumerate(zip(axes, planes, strict=True)):
    ax.imshow(plane, cmap="gray", vmin=np.percentile(plane, 1), vmax=np.percentile(plane, 99.5))
    ax.set_title(f"channel {c}, z = {z}")
    ax.axis("off")

fig.tight_layout()
fig.savefig("czi.png", dpi=100)
plt.show()
