# /// script
# requires-python = ">=3.12"
# dependencies = ["refkv[zarr,images]", "matplotlib"]
#
# [tool.uv.sources]
# refkv = { git = "https://github.com/d-v-b/refkv", subdirectory = "python" }
# ///
"""An Akoya (PerkinElmer) Vectra QPTIFF of lung cancer, seven-color panel: a composite, zarrified
without copying it: refkv reads the file's structure and presents it as Zarr, each chunk a reference
to the file's own bytes, read only when plotted.

Run with `uv run docs/zarrify/qptiff/example.py`; the figure is saved as qptiff.png.
"""

import matplotlib.pyplot as plt
import numpy as np
import zarr

import refkv.zarr
from refkv.zarrify import zarrify

URL = "https://downloads.openmicroscopy.org/images/Vectra-QPTIFF/perkinelmer/PKI_scans/LuCa-7color_Scan1.qptiff"

root = zarr.open_group(refkv.zarr.store(zarrify(URL)), mode="r")
ome = root.attrs["ome"]  # attributes are JSON: check the parts read
assert isinstance(ome, dict)
assert isinstance(ome["omero"], dict)
channels = ome["omero"]["channels"]
assert isinstance(channels, list)
level = root.get_array("4")  # [c, y, x], 1/16 of full resolution
composite = np.zeros((*level.shape[1:], 3), np.float32)
labels = []
for c, channel in enumerate(channels):
    assert isinstance(channel, dict)
    assert isinstance(channel["label"], str)
    assert isinstance(channel["color"], str)
    labels.append(channel["label"])
    plane = np.asarray(level[c], dtype=np.float32)
    plane = np.clip(plane / np.percentile(plane, 99.5), 0, 1)
    color = np.array([int(channel["color"][i:i + 2], 16) / 255 for i in (0, 2, 4)], np.float32)
    composite += plane[..., None] * color  # each channel in its own color
fig, ax = plt.subplots(figsize=(7, 9))
ax.imshow(np.clip(composite, 0, 1))
ax.set_title(", ".join(labels))
ax.axis("off")

fig.tight_layout()
fig.savefig("qptiff.png", dpi=100)
plt.show()
