# /// script
# requires-python = ">=3.12"
# dependencies = ["refkv[zarr,images]>=0.2.0", "matplotlib"]
# ///
"""A CT slice from pydicom's test data, zarrified without copying it: refkv reads the file's structure and
presents it as Zarr, each chunk a reference to the file's own bytes, read only when plotted.

Run with `uv run docs/zarrify/dicom/example.py`; the figure is saved as dicom.png.
"""

import matplotlib.pyplot as plt
import numpy as np
import zarr

import refkv.zarr
from refkv.zarrify import zarrify

URL = "https://raw.githubusercontent.com/pydicom/pydicom/v3.0.1/src/pydicom/data/test_files/CT_small.dcm"

root = zarr.open_group(refkv.zarr.store(zarrify(URL)), mode="r")
image = np.asarray(root.get_array("0")[...])  # [y, x], int16 as stored
fig, ax = plt.subplots(figsize=(5, 5))
ax.imshow(image, cmap="gray")
ax.set_title("CT_small.dcm")
ax.axis("off")

fig.tight_layout()
fig.savefig("dicom.png", dpi=100)
plt.show()
