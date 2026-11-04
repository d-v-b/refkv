"""Writes the TIFF fixtures of LERC, WebP, JPEG XL and LZMA in fixtures/tiff/ (spec/zarrify/tiff.md §4.3
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#43-data-types-and-codecs>), apart from
write_fixtures.py, so that its random numbers, and the pixels of its fixtures, do not depend on these.

Usage: uv run python fixtures/generators/tiff/write_codec_fixtures.py
"""

from __future__ import annotations

import numpy as np
import tifffile

# the script's directory, on sys.path when it runs; not on pyrefly's search-path, since every
# generator directory has a write_fixtures module
from write_fixtures import OUT, image, patch_short  # pyrefly: ignore[missing-import]


def main() -> None:
    OUT.mkdir(exist_ok=True)
    small = image((64, 64), np.uint8)
    wide = image((64, 64), np.uint16) * 257
    # Compressions imagecodecs decodes (spec/zarrify/tiff.md §4.3
    # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#43-data-types-and-codecs>), as tifffile
    # writes them: LERC, its streams alone and in Deflate and zstd, of integers and floats, one and
    # three samples; WebP of RGB and of RGBA whose alpha is opaque, which a WebP stream may leave out;
    # JPEG XL of uint8, uint16 and float32; LZMA, with Predictor 2. Rejected: what those decoders
    # do not hold, and a compression refkv does not name.
    lerc_floats = image((64, 48), np.float32) / 7
    tifffile.imwrite(OUT / "lerc_uint16.tif", image((64, 48), np.uint16), tile=(32, 32), compression="lerc",
                     photometric="minisblack")
    tifffile.imwrite(OUT / "lerc_float32_strips.tif", lerc_floats, rowsperstrip=16, compression="lerc",
                     photometric="minisblack")
    tifffile.imwrite(OUT / "lerc_deflate_float32.tif", lerc_floats, tile=(32, 32), compression="lerc",
                     compressionargs={"compression": "deflate"}, photometric="minisblack")
    tifffile.imwrite(OUT / "lerc_zstd_float64.tif", image((64, 48), np.float64) / 7, tile=(32, 32),
                     compression="lerc", compressionargs={"compression": "zstd"}, photometric="minisblack")
    # blocks of one value, which LERC keeps in its tiled mode: of finer uint8 pixels its bytes differ between
    # macOS and Linux, and `just fixtures-check` compares them with these
    lerc_blocks = image((3, 64, 48), np.uint8)[:, ::8, ::8].repeat(8, axis=1).repeat(8, axis=2)
    tifffile.imwrite(OUT / "lerc_rgb_contig_uint8.tif", np.moveaxis(lerc_blocks, 0, -1).copy(),
                     tile=(32, 32), compression="lerc", photometric="rgb", planarconfig="contig")
    webp_rgb = np.moveaxis(image((3, 64, 48), np.uint8), 0, -1).copy()
    tifffile.imwrite(OUT / "webp_rgb.tif", webp_rgb, tile=(32, 32), compression="webp", photometric="rgb",
                     planarconfig="contig", compressionargs={"lossless": True})
    tifffile.imwrite(OUT / "webp_rgb_strips.tif", webp_rgb, rowsperstrip=16, compression="webp", photometric="rgb",
                     planarconfig="contig", compressionargs={"lossless": True})
    webp_alpha = np.dstack([webp_rgb, np.where(np.arange(64 * 48).reshape(64, 48) < 32 * 48, 255, 90).astype(np.uint8)])
    tifffile.imwrite(OUT / "webp_rgba_opaque_tile.tif", webp_alpha, tile=(32, 32), compression="webp",
                     photometric="rgb", planarconfig="contig", extrasamples=["unassalpha"],
                     compressionargs={"lossless": True})
    for name, data, photometric, planar in (
        ("jpegxl_uint8", image((64, 48), np.uint8), "minisblack", None),
        ("jpegxl_uint16", image((64, 48), np.uint16), "minisblack", None),
        ("jpegxl_float32", lerc_floats, "minisblack", None),
        ("jpegxl_rgb_uint8", webp_rgb, "rgb", "contig"),
    ):
        tifffile.imwrite(OUT / f"{name}.tif", data, tile=(32, 32), compression="jpegxl", photometric=photometric,
                         planarconfig=planar, compressionargs={"lossless": True})
    tifffile.imwrite(OUT / "lzma_uint16.tif", image((64, 48), np.uint16), tile=(32, 32), compression="lzma",
                     photometric="minisblack")
    tifffile.imwrite(OUT / "lzma_predictor_uint16_be.tif", image((64, 48), np.uint16), byteorder=">", rowsperstrip=16,
                     compression="lzma", predictor=True, photometric="minisblack")
    # A short last strip of each, which cannot be padded: the `rectilinear` grid.
    tifffile.imwrite(OUT / "lerc_float32_strips_short.tif", lerc_floats[:56], rowsperstrip=16, compression="lerc",
                     photometric="minisblack")
    tifffile.imwrite(OUT / "webp_rgb_strips_short.tif", webp_rgb[:56], rowsperstrip=16, compression="webp",
                     photometric="rgb", planarconfig="contig", compressionargs={"lossless": True})
    tifffile.imwrite(OUT / "jpegxl_uint16_strips_short.tif", wide[:56, :48], rowsperstrip=16, compression="jpegxl",
                     photometric="minisblack", compressionargs={"lossless": True})
    tifffile.imwrite(OUT / "lzma_uint16_strips_short.tif", wide[:56, :48], rowsperstrip=16, compression="lzma",
                     photometric="minisblack")
    for name, data, compression in (
        ("edge_reject_webp_gray", small, 50001),
        ("edge_reject_webp_uint16", wide, 50001),
        ("edge_reject_jpegxl_int16", image((64, 64), np.int16), 50002),
        ("edge_reject_lerc_uint64", image((64, 64), np.uint64), 34887),
        ("edge_reject_compression_unknown", small, 34661),
    ):
        tifffile.imwrite(OUT / f"{name}.tif", data, tile=(32, 32), compression="zlib", photometric="minisblack")
        patch_short(OUT / f"{name}.tif", "Compression", compression)
    tifffile.imwrite(OUT / "edge_reject_predictor_lerc.tif", small, tile=(32, 32), compression="zlib", predictor=True,
                     photometric="minisblack")
    patch_short(OUT / "edge_reject_predictor_lerc.tif", "Compression", 34887)


if __name__ == "__main__":
    main()
