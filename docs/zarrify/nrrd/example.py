# /// script
# requires-python = ">=3.12"
# dependencies = ["refkv[zarr]>=0.2.0", "matplotlib"]
# ///
"""pynrrd's test ball (BallBinary30x30x30), twice: a `.nrrd` file whose data is one gzip stream, and
a detached `.nhdr` header whose raw data lies in a file beside it. Each is zarrified without copying
it: refkv reads the header and presents the data as Zarr, each chunk a reference to the file's own
bytes (the gzip stream is one chunk, which the `gzip` codec decompresses), read only when plotted.

Run with `uv run docs/zarrify/nrrd/example.py`; the figure is saved as nrrd.png.
"""

import matplotlib.pyplot as plt
import numpy as np
import zarr

import refkv.zarr
from refkv.zarrify import zarrify

BASE = "https://raw.githubusercontent.com/mhe/pynrrd/96bf87b78bde03987c09346ceffe0bb85a8f4214/nrrd/tests/data/"
INPUTS = ["BallBinary30x30x30_gz.nrrd", "BallBinary30x30x30.nhdr"]

fig, axes = plt.subplots(len(INPUTS), 3, figsize=(9, 3 * len(INPUTS)))
for row, name in zip(axes, INPUTS, strict=True):
    root = zarr.open_group(refkv.zarr.store(zarrify(BASE + name)), mode="r")
    volume = np.asarray(root.get_array("0")[...])  # [z, y, x]
    own = root.attrs.get("refkv_zarrified")
    nrrd = own.get("nrrd") if isinstance(own, dict) else None
    encoding = nrrd.get("encoding") if isinstance(nrrd, dict) else None
    cz, cy, cx = (n // 2 for n in volume.shape)
    planes = [("z", volume[cz]), ("y", volume[:, cy]), ("x", volume[:, :, cx])]
    for ax, (axis, plane) in zip(row, planes, strict=True):
        ax.imshow(plane, cmap="gray")
        ax.set_title(f"{name} ({encoding}): central {axis} slice", fontsize=8)
        ax.set_xticks([])  # a frame around the slice: the ball is white, as the page is
        ax.set_yticks([])

fig.tight_layout()
fig.savefig("nrrd.png", dpi=100)
plt.show()
