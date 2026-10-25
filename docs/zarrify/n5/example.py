# /// script
# requires-python = ">=3.12"
# dependencies = ["refkv[zarr]", "matplotlib"]
#
# [tool.uv.sources]
# refkv = { git = "https://github.com/d-v-b/refkv", subdirectory = "python" }
# ///
"""Mitochondria labels of HeLa cell 2 (OpenOrganelle, jrc_hela-2), an N5 multiscale, zarrified
without copying it: refkv reads the file's structure and presents it as Zarr, each chunk a reference
to the file's own bytes, read only when plotted.

Run with `uv run docs/zarrify/n5/example.py`; the figure is saved as n5.png.
"""

import matplotlib.pyplot as plt
import numpy as np
import zarr

import refkv.zarr
from refkv.zarrify import zarrify

URL = "https://janelia-cosem-datasets.s3.amazonaws.com/jrc_hela-2/jrc_hela-2.n5/labels/mito_seg/"

root = zarr.open_group(refkv.zarr.store(zarrify(URL)), mode="r")
level = root.get_array("s4")  # dims ('x', 'y', 'z'), as N5 orders them
labels = np.asarray(level[...]).max(axis=1)  # every label's largest value across y: [x, z]
shuffle = np.random.default_rng(0).permutation(int(labels.max()) + 1)
colored = np.ma.masked_where(labels == 0, shuffle[labels])  # each label a color, background clear
fig, ax = plt.subplots(figsize=(8, 8))
ax.imshow(colored.T, cmap="tab20", interpolation="nearest")
ax.set_title("mito_seg s4, maximum across y")
ax.axis("off")

fig.tight_layout()
fig.savefig("n5.png", dpi=100)
plt.show()
