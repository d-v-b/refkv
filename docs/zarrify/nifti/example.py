# /// script
# requires-python = ">=3.12"
# dependencies = ["refkv[zarr]", "matplotlib"]
#
# [tool.uv.sources]
# refkv = { git = "https://github.com/d-v-b/refkv", subdirectory = "python" }
# ///
"""SPM's average T1 brain template (avg152T1.nii): three slices, zarrified without copying it: refkv reads the file's structure and
presents it as Zarr, each chunk a reference to the file's own bytes, read only when plotted.

Run with `uv run docs/zarrify/nifti/example.py`; the figure is saved as nifti.png.
"""

import matplotlib.pyplot as plt
import zarr

import refkv.zarr
from refkv.zarrify import zarrify

URL = "https://raw.githubusercontent.com/spm/spm/main/canonical/avg152T1.nii"

root = zarr.open_group(refkv.zarr.store(zarrify(URL)), mode="r")
volume = root["0"][...]  # [z, y, x]
cz, cy, cx = (n // 2 for n in volume.shape)
fig, axes = plt.subplots(1, 3, figsize=(12, 4))
for ax, (name, plane) in zip(axes, [("axial", volume[cz]), ("coronal", volume[:, cy]), ("sagittal", volume[:, :, cx])]):
    ax.imshow(plane, cmap="gray", origin="lower")
    ax.set_title(name)
    ax.axis("off")

fig.tight_layout()
fig.savefig("nifti.png", dpi=100)
plt.show()
