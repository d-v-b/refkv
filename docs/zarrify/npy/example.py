# /// script
# requires-python = ">=3.12"
# dependencies = ["refkv[zarr]", "matplotlib"]
#
# [tool.uv.sources]
# refkv = { git = "https://github.com/d-v-b/refkv", subdirectory = "python" }
# ///
"""An archive of two arrays, saved by `np.savez` and zarrified without copying it: refkv reads the
archive's structure and presents each member as a Zarr array, each chunk a reference to the file's
own bytes, read only when plotted.

Run with `uv run docs/zarrify/npy/example.py`; the archive is saved as arrays.npz and arrays.vzip,
and the figure as npy.png.
"""

import matplotlib.pyplot as plt
import numpy as np
import zarr

import refkv.zarr
from refkv.zarrify import zarrify

y, x = np.mgrid[-2:2:400j, -3:3:600j]
wave = np.sin(x * y * 3) * np.exp(-(x**2 + y**2) / 4)
np.savez("arrays.npz", image=wave.astype(np.float32), profile=wave[200])

store = zarrify("arrays.npz")
store.save("arrays.vzip")
root = zarr.open_group(refkv.zarr.store(store), mode="r")
image, profile = root.get_array("image"), root.get_array("profile")
print(sorted(root.array_keys()), image.shape, image.chunks)

fig, (left, right) = plt.subplots(1, 2, figsize=(12, 4))
left.imshow(np.asarray(image[...]), cmap="RdBu")
left.set_title("image")
left.axis("off")
right.plot(np.asarray(profile[...]))
right.set_title("profile")
fig.tight_layout()
fig.savefig("npy.png", dpi=100)
plt.show()
