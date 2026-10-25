# /// script
# requires-python = ">=3.12"
# dependencies = ["refkv[zarr,images]", "matplotlib"]
#
# [tool.uv.sources]
# refkv = { git = "https://github.com/d-v-b/refkv", subdirectory = "python" }
# ///
"""OpenSlide's Hamamatsu NDPI test slide CMU-1: its smallest pyramid level, zarrified without
copying it: refkv reads the file's structure and presents it as Zarr, each chunk a reference to the
file's own bytes, read only when plotted.

Run with `uv run docs/zarrify/ndpi/example.py`; the figure is saved as ndpi.png.
"""

import matplotlib.pyplot as plt
import numpy as np
import zarr

import refkv.zarr
from refkv.zarrify import zarrify

URL = "https://openslide.cs.cmu.edu/download/openslide-testdata/Hamamatsu/CMU-1.ndpi"

root = zarr.open_group(refkv.zarr.store(zarrify(URL)), mode="r")
levels = sorted(root.array_keys(), key=int)  # the pyramid's levels, 0 its full resolution
smallest = np.asarray(root.get_array(levels[-1])[...])  # [c, y, x], JPEG-coded tiles decoded by refkv.zarr's codecs
fig, ax = plt.subplots(figsize=(8, 6))
ax.imshow(smallest.transpose(1, 2, 0))
ax.set_title(f"CMU-1.ndpi, level {levels[-1]} of {len(levels)}")
ax.axis("off")

fig.tight_layout()
fig.savefig("ndpi.png", dpi=100)
plt.show()
