# /// script
# requires-python = ">=3.12"
# dependencies = ["refkv[zarr,images]", "matplotlib"]
#
# [tool.uv.sources]
# refkv = { git = "https://github.com/d-v-b/refkv", subdirectory = "python" }
# ///
"""OpenSlide's Hamamatsu NDPI test slide CMU-1: its smallest pyramid level, zarrified without copying it: refkv reads the file's structure and
presents it as Zarr, each chunk a reference to the file's own bytes, read only when plotted.

Run with `uv run docs/zarrify/ndpi/example.py`; the figure is saved as ndpi.png.
"""

import matplotlib.pyplot as plt
import zarr

import refkv.zarr
from refkv.zarrify import zarrify

URL = "https://openslide.cs.cmu.edu/download/openslide-testdata/Hamamatsu/CMU-1.ndpi"

root = zarr.open_group(refkv.zarr.store(zarrify(URL)), mode="r")
levels = root.attrs["ome"]["multiscales"][0]["datasets"]
smallest = root[levels[-1]["path"]][...]  # [c, y, x], JPEG-coded tiles decoded by refkv.zarr's codecs
fig, ax = plt.subplots(figsize=(8, 6))
ax.imshow(smallest.transpose(1, 2, 0))
ax.set_title(f"CMU-1.ndpi, level {levels[-1]['path']} of {len(levels)}")
ax.axis("off")

fig.tight_layout()
fig.savefig("ndpi.png", dpi=100)
plt.show()
