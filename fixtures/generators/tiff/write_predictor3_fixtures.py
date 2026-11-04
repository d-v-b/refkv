"""Writes the TIFF fixtures of Predictor 3, the floating-point predictor, in fixtures/tiff/ (spec/zarrify/tiff.md
§4.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#43-data-types-and-codecs>), apart from
write_fixtures.py, so that its random numbers, and the pixels of its fixtures, do not depend on these.

Usage: uv run python fixtures/generators/tiff/write_predictor3_fixtures.py
"""

from __future__ import annotations

import numpy as np
import tifffile

# the script's directory, on sys.path when it runs; not on pyrefly's search-path, since every
# generator directory has a write_fixtures module
from write_fixtures import OUT, image, patch_short, pyramid  # pyrefly: ignore[missing-import]


def main() -> None:
    OUT.mkdir(exist_ok=True)
    small = image((64, 64), np.uint8)
    floats = image((64, 64), np.float32) / 7
    # More of Predictor 3, the floating-point predictor, which imagecodecs_floatpred undoes: LZW,
    # Deflate and zstd, either byte order, float32 and float64, interleaved and planar samples, tiles
    # and strips, a short last LZW strip padded with a tail, and an OME-TIFF of several planes and a
    # SubIFD level. Rejected: Predictor 3 of an integer type, or with no compression, and a
    # predictor TIFF does not define.
    tifffile.imwrite(OUT / "predictor3_lzw_float32_be.tif", floats, byteorder=">", tile=(32, 32), compression="lzw",
                     predictor=True, photometric="minisblack")
    tifffile.imwrite(OUT / "predictor3_zstd_float64.tif", image((64, 48), np.float64) / 7, rowsperstrip=16,
                     compression="zstd", predictor=True, photometric="minisblack")
    tifffile.imwrite(OUT / "predictor3_rgb_contig_deflate_float32.tif",
                     np.moveaxis(image((3, 64, 48), np.float32) / 7, 0, -1).copy(), tile=(32, 32),
                     compression="zlib", predictor=True, photometric="rgb", planarconfig="contig")
    tifffile.imwrite(OUT / "predictor3_contig_be_lzw_float64.tif",
                     np.moveaxis(image((4, 64, 48), np.float64) / 7, 0, -1).copy(), byteorder=">", rowsperstrip=16,
                     compression="lzw", predictor=True, photometric="minisblack", planarconfig="contig",
                     extrasamples=["unspecified"] * 3)
    tifffile.imwrite(OUT / "predictor3_planar_deflate_float32.tif", image((3, 64, 48), np.float32) / 7, tile=(32, 32),
                     compression="zlib", predictor=True, photometric="rgb", planarconfig="separate")
    tifffile.imwrite(OUT / "predictor3_strips_float32_lzw_short.tif", image((70, 50), np.float32) / 7, rowsperstrip=16,
                     compression="lzw", predictor=True, photometric="minisblack")
    pyramid(OUT / "predictor3_tcyx_float32_lzw.ome.tif", image((2, 3, 64, 64), np.float32) / 7, 1, axes="TCYX",
            tile=(32, 32), compression="lzw", predictor=True, photometric="minisblack")
    # A short last strip of Deflate, which cannot be padded: the `rectilinear` grid.
    tifffile.imwrite(OUT / "predictor3_strips_float32_deflate_short.tif", image((70, 50), np.float32) / 7,
                     rowsperstrip=16, compression="zlib", predictor=True, photometric="minisblack")
    tifffile.imwrite(OUT / "edge_reject_predictor3_integer.tif", small, tile=(32, 32), compression="zlib",
                     predictor=True)
    patch_short(OUT / "edge_reject_predictor3_integer.tif", "Predictor", 3)
    tifffile.imwrite(OUT / "edge_reject_predictor3_uncompressed.tif", floats, tile=(32, 32), compression="zlib",
                     predictor=True, photometric="minisblack")
    patch_short(OUT / "edge_reject_predictor3_uncompressed.tif", "Compression", 1)
    tifffile.imwrite(OUT / "edge_reject_predictor_unknown.tif", small, tile=(32, 32), compression="zlib",
                     predictor=True)
    patch_short(OUT / "edge_reject_predictor_unknown.tif", "Predictor", 4)


if __name__ == "__main__":
    main()
