"""Writes the TIFF fixtures of short last strips that cannot be padded in fixtures/tiff/ (spec/zarrify/tiff.md
§4.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>), apart from write_fixtures.py,
so that its random numbers, and the pixels of its fixtures, do not depend on these.

Usage: uv run python fixtures/generators/tiff/write_short_strip_fixtures.py
"""

from __future__ import annotations

import numpy as np
import tifffile

# the script's directory, on sys.path when it runs; not on pyrefly's search-path, since every
# generator directory has a write_fixtures module
from write_fixtures import OUT, image, pyramid  # pyrefly: ignore[missing-import]


def main() -> None:
    OUT.mkdir(exist_ok=True)
    tall = image((70, 50), np.uint16)
    # More short last strips that cannot be padded (spec/zarrify/tiff.md §4.5
    # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>): zstd, big-endian with
    # Predictor 2, JPEG and JPEG 2000, interleaved and planar samples, two strips, an OME-TIFF of
    # several planes and a SubIFD level, and one whose level of one strip keeps a regular grid.
    tifffile.imwrite(OUT / "strips_uint16_zstd_short.tif", tall, rowsperstrip=16, compression="zstd",
                     photometric="minisblack")
    tifffile.imwrite(OUT / "strips_uint16_deflate_be_predictor_short.tif", tall, byteorder=">", rowsperstrip=16,
                     compression="zlib", predictor=True, photometric="minisblack")
    tifffile.imwrite(OUT / "strips_rgb_contig_jpeg_short.tif", np.moveaxis(image((3, 70, 48), np.uint8), 0, -1).copy(),
                     rowsperstrip=16, compression="jpeg", photometric="rgb", planarconfig="contig")
    tifffile.imwrite(OUT / "strips_rgb_contig_deflate_short.tif",
                     np.moveaxis(image((3, 24, 50), np.uint8), 0, -1).copy(), rowsperstrip=16, compression="zlib",
                     photometric="rgb", planarconfig="contig")
    tifffile.imwrite(OUT / "strips_rgb_planar_jpeg2000_short.tif", image((3, 70, 50), np.uint8), rowsperstrip=16,
                     compression="jpeg2000", photometric="rgb", planarconfig="separate")
    pyramid(OUT / "strips_tcyx_uint16_deflate_short.ome.tif", image((2, 3, 70, 50), np.uint16), 1, axes="TCYX",
            rowsperstrip=16, compression="zlib", photometric="minisblack")
    pyramid(OUT / "strips_uint8_deflate_short_level.ome.tif", image((48, 50), np.uint8), 1, axes="YX",
            rowsperstrip=32, compression="zlib", photometric="minisblack")


if __name__ == "__main__":
    main()
