# /// script
# requires-python = ">=3.12"
# dependencies = ["refkv[zarr]", "matplotlib"]
#
# [tool.uv.sources]
# refkv = { git = "https://github.com/d-v-b/refkv", subdirectory = "python" }
# ///
"""A Leica LIF file (Zenodo 6606445, a 2 x 2 tile scan of a neuron and LAS X's merged image,
four channels, 12 z-planes), zarrified without copying it: refkv reads the file's XML header and
its blocks' headers and presents each image, and each tile of the scan, as Zarr, each chunk a
reference to the file's own bytes, read only when plotted.

The tiles overlap and are not stitched: each is drawn where its stage position (`PosX`, `PosY`,
in meters) puts it, beside the image LAS X merged from them.

Run with `uv run docs/zarrify/lif/example.py`; the figure is saved as lif.png.
"""

import matplotlib.pyplot as plt
import numpy as np
import zarr

import refkv.zarr
from refkv.zarrify import zarrify

URL = "https://downloads.openmicroscopy.org/images/Leica-LIF/zenodo-6606445/Project007.lif"
CHANNEL = 0

root = zarr.open_group(refkv.zarr.store(zarrify(URL)), mode="r")


def multiscale(series: str) -> dict:
    """A series' OME-NGFF multiscale: its name, axes and scale."""
    ome = root.get_group(series).attrs["ome"]
    assert isinstance(ome, dict)
    assert isinstance(ome["multiscales"], list)
    m = ome["multiscales"][0]
    assert isinstance(m, dict)
    return m


def stage(series: str) -> tuple[float, float]:
    """A tile's stage position (meters), from its source metadata."""
    own = root.get_group(series).attrs["refkv_zarrified"]
    assert isinstance(own, dict)
    assert isinstance(own["lif"], dict)
    tile = own["lif"]["tile"]
    assert isinstance(tile, dict)
    return float(str(tile["PosX"])), float(str(tile["PosY"]))


print([multiscale(s)["name"] for s in "23456"])  # "Cell 1 (tile 0)" to "(tile 3)", then "Cell 1 Merged"

fig, (tiles, merged) = plt.subplots(1, 2, figsize=(10, 5))
corners = []
for s in ("2", "3", "4", "5"):
    image = root.get_array(f"{s}/0")  # [c, z, y, x]
    plane = np.asarray(image[CHANNEL, image.shape[1] // 2])  # one chunk
    transform = multiscale(s)["datasets"][0]["coordinateTransformations"][0]
    pixel = float(transform["scale"][-1]) * 1e-6  # micrometers to meters
    x0, y0 = stage(s)
    x1, y1 = x0 + plane.shape[1] * pixel, y0 + plane.shape[0] * pixel
    corners += [(x0, y0), (x1, y1)]
    tiles.imshow(plane, cmap="gray", extent=(x0, x1, y1, y0), alpha=0.6, vmax=np.percentile(plane, 99.5))
    tiles.text(x0, y0, f" tile {int(s) - 2}", color="yellow", va="top")
xs, ys = zip(*corners, strict=True)
tiles.set_xlim(min(xs), max(xs))
tiles.set_ylim(max(ys), min(ys))
tiles.set_title("the tiles, at their stage positions")
whole = root.get_array("6/0")
plane = np.asarray(whole[CHANNEL, whole.shape[1] // 2])
merged.imshow(plane, cmap="gray", vmax=np.percentile(plane, 99.5))
merged.set_title("LAS X's merged image")
for ax in (tiles, merged):
    ax.axis("off")

fig.tight_layout()
fig.savefig("lif.png", dpi=100)
plt.show()
