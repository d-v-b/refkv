# /// script
# requires-python = ">=3.12"
# dependencies = ["refkv[zarr,images]", "matplotlib"]
#
# [tool.uv.sources]
# refkv = { git = "https://github.com/d-v-b/refkv", subdirectory = "python" }
# ///
"""A Sentinel-2 Level-1C product (tile 32TNS, the Alps, 2024-01-03): a true-color crop, zarrified without copying it: refkv reads the file's structure and
presents it as Zarr, each chunk a reference to the file's own bytes, read only when plotted.

Run with `uv run docs/zarrify/safe/example.py`; the figure is saved as safe.png.
"""

import matplotlib.pyplot as plt
import zarr

import refkv.zarr
from refkv.zarrify import zarrify

URL = "https://storage.googleapis.com/gcp-public-data-sentinel-2/tiles/32/T/NS/S2B_MSIL1C_20240103T101329_N0510_R022_T32TNS_20240103T110304.SAFE/"

# refkv lists the product and reads its XML documents: about half a minute
root = zarr.open_group(refkv.zarr.store(zarrify(URL)), mode="r")
tci = root["r10m/TCI"]  # [c, y, x]: the true-color image, JPEG 2000 coded, 10 m pixels
# the orbit covers the tile's eastern fifth (the rest is no data): a 10 km square there,
# its 16 chunks decoded
crop = tci[:, 4600:5624, 9300:10324]
fig, ax = plt.subplots(figsize=(7, 7))
ax.imshow(crop.transpose(1, 2, 0))
ax.set_title("T32TNS true color, 10 km")
ax.axis("off")

fig.tight_layout()
fig.savefig("safe.png", dpi=100)
plt.show()
