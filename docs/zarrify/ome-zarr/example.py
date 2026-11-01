# /// script
# requires-python = ">=3.12"
# dependencies = ["refkv[zarr]>=0.2.0", "matplotlib"]
# ///
"""An OME-Zarr 0.4 image from the IDR (idr0062A): one z-plane of each channel, zarrified without
copying it: refkv reads the file's structure and presents it as Zarr, each chunk a reference to the
file's own bytes, read only when plotted.

Run with `uv run docs/zarrify/ome-zarr/example.py`; the figure is saved as ome-zarr.png.
"""

import matplotlib.pyplot as plt
import numpy as np
import zarr

import refkv.zarr
from refkv.zarrify import zarrify

URL = "https://uk1s3.embassy.ebi.ac.uk/idr/zarr/v0.4/idr0062A/6001240.zarr/"

root = zarr.open_group(refkv.zarr.store(zarrify(URL)), mode="r")
image = root.get_array("0")  # [c, z, y, x], presented as OME-Zarr 0.5
z = image.shape[1] // 2
ome = root.attrs["ome"]  # attributes are JSON: check the parts read
assert isinstance(ome, dict)
assert isinstance(ome["omero"], dict)
channels = ome["omero"]["channels"]
assert isinstance(channels, list)
fig, axes = plt.subplots(1, image.shape[0], figsize=(5 * image.shape[0], 5))
for c, (ax, channel) in enumerate(zip(np.atleast_1d(axes), channels, strict=True)):
    assert isinstance(channel, dict)
    plane = np.asarray(image[c, z])
    ax.imshow(plane, cmap="gray", vmin=np.percentile(plane, 1), vmax=np.percentile(plane, 99.5))
    ax.set_title(f"{channel.get('label', c)}, z = {z}")
    ax.axis("off")

fig.tight_layout()
fig.savefig("ome-zarr.png", dpi=100)
plt.show()
