# /// script
# requires-python = ">=3.12"
# dependencies = ["refkv[zarr]", "matplotlib"]
#
# [tool.uv.sources]
# refkv = { git = "https://github.com/d-v-b/refkv", subdirectory = "python" }
# ///
"""HeLa cell 2 FIB-SEM (OpenOrganelle, jrc_hela-2), a Zarr v2 array: three slices, zarrified without
copying it: refkv reads the file's structure and presents it as Zarr, each chunk a reference to the
file's own bytes, read only when plotted.

Run with `uv run docs/zarrify/zarr2/example.py`; the figure is saved as zarr2.png.
"""

import matplotlib.pyplot as plt
import numpy as np
import zarr

import refkv.zarr
from refkv.zarrify import zarrify

URL = "https://janelia-cosem-datasets.s3.amazonaws.com/jrc_hela-2/jrc_hela-2.zarr/recon-1/em/fibsem-uint8/s5/"

array = zarr.open_array(refkv.zarr.store(zarrify(URL)), mode="r")  # the store is one array: [z, y, x]
volume = np.asarray(array[...])
cz, cy, cx = (n // 2 for n in volume.shape)
fig, axes = plt.subplots(1, 3, figsize=(13, 4))
planes = [("z", volume[cz]), ("y", volume[:, cy]), ("x", volume[:, :, cx])]
for ax, (name, plane) in zip(axes, planes, strict=True):
    ax.imshow(plane, cmap="gray")
    ax.set_title(f"central {name} slice")
    ax.axis("off")

fig.tight_layout()
fig.savefig("zarr2.png", dpi=100)
plt.show()
