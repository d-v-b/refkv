# /// script
# requires-python = ">=3.12"
# dependencies = ["refkv[images]>=0.2.0", "matplotlib"]
# ///
"""A section of the BANC fruit fly nerve cord (Bates et al., bossdb-open-data), Neuroglancer
precomputed JPEG in shards, zarrified without copying it: refkv reads `info` and the listing and
presents 690 million chunks as Zarr, each shard a reference, read only when plotted.

Run with `uv run docs/zarrify/precomputed/example.py`; the figure is saved as precomputed.png.
"""

import matplotlib.pyplot as plt
import numpy as np
import zarr

import refkv.zarr
from refkv.zarrify import zarrify

URL = "https://bossdb-open-data.s3.amazonaws.com/bates_phelps_kim_yang2025/banc/em/"

root = zarr.open_group(refkv.zarr.store(zarrify(URL)), mode="r")
coarsest = root.get_array("6")  # [z, y, x], at 512 nm
section = np.asarray(coarsest[3500])
fig, ax = plt.subplots(figsize=(8, 8))
ax.imshow(section, cmap="gray")
ax.set_title("BANC at 512 nm, section 3500")
ax.axis("off")

fig.tight_layout()
fig.savefig("precomputed.png", dpi=100)
plt.show()
