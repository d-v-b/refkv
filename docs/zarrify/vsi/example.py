# /// script
# requires-python = ">=3.12"
# dependencies = ["refkv[zarr,images]", "matplotlib"]
#
# [tool.uv.sources]
# refkv = { git = "https://github.com/d-v-b/refkv", subdirectory = "python" }
# ///
"""An Olympus/Evident cellSens VSI brightfield slide (its tile files read beside it), zarrified without copying it: refkv reads the file's structure and
presents it as Zarr, each chunk a reference to the file's own bytes, read only when plotted.

Run with `uv run docs/zarrify/vsi/example.py`; the figure is saved as vsi.png.
"""

import matplotlib.pyplot as plt
import zarr

import refkv.zarr
from refkv.zarrify import zarrify

URL = "https://downloads.openmicroscopy.org/images/CellSens/evident/Image_V4.1_BF.vsi"

root = zarr.open_group(refkv.zarr.store(zarrify(URL)), mode="r")
series = root["2"]  # the slide's series: 0 is its label, 1 its overview, 2 the 20x scan
levels = series.attrs["ome"]["multiscales"][0]["datasets"]
image = series[levels[-2]["path"]][...]  # [c, y, x], a coarse pyramid level
fig, ax = plt.subplots(figsize=(7, 7))
ax.imshow(image.transpose(1, 2, 0))
ax.set_title(f"Image_V4.1_BF.vsi, 20x, level {levels[-2]['path']}")
ax.axis("off")

fig.tight_layout()
fig.savefig("vsi.png", dpi=100)
plt.show()
