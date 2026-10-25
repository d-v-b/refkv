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


def tile(series: str) -> dict | None:
    """A tile's record (its stage position), from its source metadata; None for an image that is
    no tile of a tile scan."""
    own = root.get_group(series).attrs["refkv_zarrified"]
    assert isinstance(own, dict)
    assert isinstance(own["lif"], dict)
    record = own["lif"].get("tile")
    assert record is None or isinstance(record, dict)
    return record


ome = root.get_group("OME").attrs["ome"]
assert isinstance(ome, dict)
assert isinstance(ome["series"], list)
names = {str(s): multiscale(str(s))["name"] for s in ome["series"]}
tiles_of_cell = [s for s, name in names.items() if name.startswith("Cell 1 (tile ") and tile(s) is not None]
(merged_image,) = (s for s, name in names.items() if name == "Cell 1 Merged")
print(names)

fig, (tiles, merged) = plt.subplots(1, 2, figsize=(10, 5))
corners = []
for s in tiles_of_cell:
    image = root.get_array(f"{s}/0")  # [c, z, y, x]
    plane = np.asarray(image[CHANNEL, image.shape[1] // 2])  # one plane
    transform = multiscale(s)["datasets"][0]["coordinateTransformations"][0]
    dy, dx = (float(v) * 1e-6 for v in transform["scale"][-2:])  # micrometers to meters
    record = tile(s)
    assert record is not None
    x0, y0 = float(record["PosX"]), float(record["PosY"])
    x1, y1 = x0 + plane.shape[1] * dx, y0 + plane.shape[0] * dy
    corners += [(x0, y0), (x1, y1)]
    tiles.imshow(plane, cmap="gray", extent=(x0, x1, y1, y0), alpha=0.6, vmax=np.percentile(plane, 99.5))
    tiles.text(x0, y0, f" {names[s].removeprefix('Cell 1 ')}", color="yellow", va="top")
xs, ys = zip(*corners, strict=True)
tiles.set_xlim(min(xs), max(xs))
tiles.set_ylim(max(ys), min(ys))
tiles.set_title("the tiles, at their stage positions")
whole = root.get_array(f"{merged_image}/0")
plane = np.asarray(whole[CHANNEL, whole.shape[1] // 2])
merged.imshow(plane, cmap="gray", vmax=np.percentile(plane, 99.5))
merged.set_title("LAS X's merged image")
for ax in (tiles, merged):
    ax.axis("off")

fig.tight_layout()
fig.savefig("lif.png", dpi=100)
plt.show()
