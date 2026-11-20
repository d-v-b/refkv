"""Writes the ImageJ hyperstack fixtures in fixtures/tiff/ (spec/zarrify/tiff.md §4.1
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#41-planes>): stacks of one IFD a
plane, and stacks in one IFD, whose planes after the first follow IFD 0's strips with no IFD of
their own (ImageJ writes those over 4 GB; tifffile's `truncate`), with the description's lines that
give their frames, slices and channels. The rejected ones have a line of the description changed in
place, to a value of the same length.

Usage: uv run python fixtures/generators/tiff/write_imagej_fixtures.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import tifffile

OUT = Path(__file__).parents[2] / "tiff"


def image(shape, dtype):
    y, x = np.mgrid[0 : shape[-2], 0 : shape[-1]]
    base = (x * 3 + y * 5) % 251
    out = np.broadcast_to(base, shape).astype(np.float64)
    out = out + np.arange(np.prod(shape[:-2]) or 1).reshape((*shape[:-2], 1, 1)) * 17
    return out.astype(dtype)


def patch_description(path: Path, old: str, new: str) -> None:
    """IFD 0's ImageDescription with `old` replaced by `new`, of the same length, in place."""
    assert len(old) == len(new)
    with tifffile.TiffFile(path) as tf:
        tag = tf.pages.first.tags["ImageDescription"]
        offset, value = tag.valueoffset, tag.value
    assert old in value, (old, value)
    b = bytearray(path.read_bytes())
    b[offset : offset + len(value)] = value.replace(old, new, 1).encode()
    path.write_bytes(bytes(b))


def main() -> None:
    OUT.mkdir(exist_ok=True)
    zyx = image((5, 40, 32), np.uint8)
    tzcyx = image((2, 3, 2, 25, 20), np.uint16)
    # one IFD a plane: a stack of slices, and a hyperstack of frames, slices and channels, with
    # strips of 10 rows whose last is short
    tifffile.imwrite(OUT / "imagej_pages_zyx.tif", zyx, imagej=True, metadata={"axes": "ZYX"})
    tifffile.imwrite(OUT / "imagej_pages_tzcyx_uint16.tif", tzcyx, imagej=True, rowsperstrip=10,
                     metadata={"axes": "TZCYX"})
    # in one IFD: the same two, in both byte orders, and an RGB stack of slices (ImageJ's own
    # description of one: no channels line)
    tifffile.imwrite(OUT / "imagej_contiguous_zyx.tif", zyx, imagej=True, truncate=True, metadata={"axes": "ZYX"})
    tifffile.imwrite(OUT / "imagej_contiguous_tzcyx_uint16_be.tif", tzcyx, imagej=True, truncate=True, byteorder=">",
                     rowsperstrip=10, metadata={"axes": "TZCYX"})
    rgb = np.moveaxis(image((3, 4, 30, 20), np.uint8), 0, -1).copy()
    tifffile.imwrite(OUT / "imagej_contiguous_rgb.tif", rgb, imagej=True, truncate=True, photometric="rgb",
                     metadata={"axes": "ZYXS"})
    # Rejected: a stack in one IFD whose images reach past the file's end; frames, slices and
    # channels that do not make images; a stack of pages whose images are not its IFDs; and
    # channels of several samples each (tifffile's default description of an RGB stack)
    tifffile.imwrite(OUT / "edge_reject_imagej_short.tif", zyx, imagej=True, truncate=True, metadata={"axes": "ZYX"})
    patch_description(OUT / "edge_reject_imagej_short.tif", "images=5\nslices=5", "images=9\nslices=9")
    tifffile.imwrite(OUT / "edge_reject_imagej_product.tif", tzcyx, imagej=True, rowsperstrip=10,
                     metadata={"axes": "TZCYX"})
    patch_description(OUT / "edge_reject_imagej_product.tif", "slices=3", "slices=4")
    tifffile.imwrite(OUT / "edge_reject_imagej_ifds.tif", zyx, imagej=True, metadata={"axes": "ZYX"})
    patch_description(OUT / "edge_reject_imagej_ifds.tif", "images=5\nslices=5", "images=7\nslices=7")
    tifffile.imwrite(OUT / "edge_reject_imagej_rgb_channels.tif", rgb, imagej=True, truncate=True, photometric="rgb")


if __name__ == "__main__":
    main()
