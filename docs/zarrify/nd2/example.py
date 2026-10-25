# /// script
# requires-python = ">=3.12"
# dependencies = ["refkv[zarr]", "matplotlib"]
#
# [tool.uv.sources]
# refkv = { git = "https://github.com/d-v-b/refkv", subdirectory = "python" }
# ///
"""A Nikon ND2 confocal stack (BioImage Archive S-BIAD2077): one z-plane of each channel, zarrified
without copying it: refkv reads the file's structure and presents it as Zarr, each chunk a reference
to the file's own bytes, read only when plotted.

Run with `uv run docs/zarrify/nd2/example.py`; the figure is saved as nd2.png.
"""

import matplotlib.pyplot as plt
import numpy as np
import zarr

import refkv.zarr
from refkv.zarrify import zarrify

URL = "https://ftp.ebi.ac.uk/biostudies/fire/S-BIAD/077/S-BIAD2077/Files/373_230614_A1_Blk_Reg2_40x.nd2"

root = zarr.open_group(refkv.zarr.store(zarrify(URL)), mode="r")
image = root.get_array("0/0")  # [c, z, y, x]: a chunk holds every channel of one plane
z = image.shape[1] // 2
planes = np.asarray(image[:, z])
fig, axes = plt.subplots(1, len(planes), figsize=(3.5 * len(planes), 3.5))
for c, (ax, plane) in enumerate(zip(axes, planes, strict=True)):
    ax.imshow(plane, cmap="gray", vmin=np.percentile(plane, 1), vmax=np.percentile(plane, 99.8))
    ax.set_title(f"channel {c}")
    ax.axis("off")
fig.suptitle(f"z = {z}")

fig.tight_layout()
fig.savefig("nd2.png", dpi=100)
plt.show()
