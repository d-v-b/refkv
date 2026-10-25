# /// script
# requires-python = ">=3.12"
# dependencies = ["refkv[zarr,media]", "matplotlib"]
#
# [tool.uv.sources]
# refkv = { git = "https://github.com/d-v-b/refkv", subdirectory = "python" }
# ///
"""SLEAP's test video of two flies (H.264): four pictures, in color from their Y, U and V planes,
zarrified without copying it: refkv reads the file's structure and presents it as Zarr, each chunk a
reference to the file's own bytes, read only when plotted.

Run with `uv run docs/zarrify/mp4/example.py`; the figure is saved as mp4.png.
"""

import matplotlib.pyplot as plt
import numpy as np
import zarr

import refkv.zarr
from refkv.zarrify import zarrify

URL = "https://raw.githubusercontent.com/talmolab/sleap/126715fc443ef8eb495fe1d3cb4190b5aa58539e/tests/data/videos/centered_pair_small.mp4"


def rgb(y, u, v):
    """BT.601 limited-range Y, U and V (U and V at half size) as RGB."""
    u = u.repeat(2, 0).repeat(2, 1)[: y.shape[0], : y.shape[1]].astype(np.float32) - 128
    v = v.repeat(2, 0).repeat(2, 1)[: y.shape[0], : y.shape[1]].astype(np.float32) - 128
    y = (y.astype(np.float32) - 16) * 1.164
    out = np.stack([y + 1.596 * v, y - 0.392 * u - 0.813 * v, y + 2.017 * u], axis=-1)
    return np.clip(out / 255, 0, 1)


track = zarr.open_group(refkv.zarr.store(zarrify(URL)), mode="r").get_group("tracks/1")
meta = track.attrs["refkv_zarrified"]  # attributes are JSON: check the parts read
assert isinstance(meta, dict)
assert isinstance(meta["mp4"], dict)
timescale = meta["mp4"]["timescale"]
assert isinstance(timescale, int)
times = np.asarray(track.get_array("time")[...]) / timescale
pictures = [0, 300, 600, 900]
fig, axes = plt.subplots(1, len(pictures), figsize=(4 * len(pictures), 4))
for ax, p in zip(axes, pictures, strict=True):
    # a chunk decodes once for its three planes
    ax.imshow(rgb(track.get_array("y")[p], track.get_array("u")[p], track.get_array("v")[p]))
    ax.set_title(f"picture {p}, {times[p]:.1f} s")
    ax.axis("off")

fig.tight_layout()
fig.savefig("mp4.png", dpi=100)
plt.show()
