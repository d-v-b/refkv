# /// script
# requires-python = ">=3.12"
# dependencies = ["refkv[zarr]", "matplotlib"]
#
# [tool.uv.sources]
# refkv = { git = "https://github.com/d-v-b/refkv", subdirectory = "python" }
# ///
"""EMDB map EMD-3197 (from mrcfile's test data): three central slices, zarrified without copying it:
refkv reads the file's structure and presents it as Zarr, each chunk a reference to the file's own
bytes, read only when plotted.

Run with `uv run docs/zarrify/mrc/example.py`; the figure is saved as mrc.png.
"""

import matplotlib.pyplot as plt
import numpy as np
import zarr

import refkv.zarr
from refkv.zarrify import zarrify

URL = "https://raw.githubusercontent.com/ccpem/mrcfile/64b0fb9ce0d921b902a06fe94d43e8d642b33d7d/tests/test_data/EMD-3197.map"

root = zarr.open_group(refkv.zarr.store(zarrify(URL)), mode="r")
volume = np.asarray(root.get_array("0")[...])  # [z, y, x]
cz, cy, cx = (n // 2 for n in volume.shape)
fig, axes = plt.subplots(1, 3, figsize=(12, 4))
planes = [("z", volume[cz]), ("y", volume[:, cy]), ("x", volume[:, :, cx])]
for ax, (name, plane) in zip(axes, planes, strict=True):
    ax.imshow(plane, cmap="gray")
    ax.set_title(f"central {name} slice")
    ax.axis("off")

fig.tight_layout()
fig.savefig("mrc.png", dpi=100)
plt.show()
