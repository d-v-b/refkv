# /// script
# requires-python = ">=3.12"
# dependencies = ["refkv[zarr,images]", "matplotlib"]
#
# [tool.uv.sources]
# refkv = { git = "https://github.com/d-v-b/refkv", subdirectory = "python" }
# ///
"""OpenSlide's Aperio SVS test slide CMU-1-Small-Region, zarrified without copying it: refkv reads
the file's structure and presents it as Zarr, each chunk a reference to the file's own bytes, read
only when plotted.

Run with `uv run docs/zarrify/tiff/example.py`; the figure is saved as tiff.png.
"""

import matplotlib.pyplot as plt
import numpy as np
import zarr

import refkv.zarr
from refkv.zarrify import zarrify

URL = "https://openslide.cs.cmu.edu/download/openslide-testdata/Aperio/CMU-1-Small-Region.svs"

root = zarr.open_group(refkv.zarr.store(zarrify(URL)), mode="r")
image = np.asarray(root.get_array("0")[...])  # [c, y, x], JPEG-coded tiles decoded by refkv.zarr's codecs
fig, ax = plt.subplots(figsize=(7, 9))
ax.imshow(image.transpose(1, 2, 0))
ax.set_title("CMU-1-Small-Region.svs")
ax.axis("off")

fig.tight_layout()
fig.savefig("tiff.png", dpi=100)
plt.show()
