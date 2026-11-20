# /// script
# requires-python = ">=3.12"
# dependencies = ["refkv[zarr,media]>=0.2.0", "matplotlib"]
# ///
"""A FlyLight confocal stack of a fly's brain (Janelia's Split-GAL4 Omnibus Broad release, H5J),
zarrified from Janelia's S3 bucket without copying it: refkv reads the HDF5 structure and each
channel's MP4 file, and presents each channel as an OME-NGFF image whose chunks are references to
the file's HEVC pictures, decoded only when read. The figure is each channel's maximum intensity
projection along z.

Run with `uv run docs/zarrify/h5j/example.py`; the archive is saved as flylight.vzip, and the
figure as h5j.png.
"""

import matplotlib.pyplot as plt
import numpy as np
import zarr

import refkv.zarr
from refkv.zarrify import zarrify

URL = ("https://s3.amazonaws.com/janelia-flylight-imagery/Split-GAL4+Omnibus+Broad/SS21347/"
       "SS21347-20151117_32_D4-m-20x-brain-Split_GAL4-JRC2018_Unisex_20x_HR-aligned_stack.h5j")

store = zarrify(URL)
store.save_vzip("flylight.vzip")
root = zarr.open_group(refkv.zarr.store(store), mode="r", zarr_format=3)
meta = root.attrs["refkv_zarrified"]  # attributes are JSON: check the parts read
assert isinstance(meta, dict)
assert isinstance(meta["h5j"], dict)
assert isinstance(meta["h5j"]["channels"], list)
channels = [str(c["path"]) for c in meta["h5j"]["channels"] if isinstance(c, dict) and c["path"]]

fig, axes = plt.subplots(1, len(channels), figsize=(6 * len(channels), 3.5))
for ax, name in zip(axes, channels, strict=True):
    image = root.get_group(name)
    ome = image.attrs["ome"]
    assert isinstance(ome, dict)
    assert isinstance(ome["omero"], dict)
    assert isinstance(ome["omero"]["channels"], list)
    channel = ome["omero"]["channels"][0]
    assert isinstance(channel, dict)
    label = channel["label"]
    stack = np.asarray(image.get_array("0")[...])  # (z, y, x), 12-bit samples
    print(name, label, stack.shape, stack.dtype)
    ax.imshow(stack.max(axis=0), cmap="gray", vmin=0, vmax=4095)
    ax.set_title(f"{name} ({label}), max over {stack.shape[0]} slices")
    ax.axis("off")
fig.tight_layout()
fig.savefig("h5j.png", dpi=100)
plt.show()
