"""Writes the TIFF fixtures in fixtures/tiff/ with tifffile.

Usage: uv run python fixtures/generators/tiff/write_fixtures.py
"""

from __future__ import annotations

import uuid
from pathlib import Path

import numpy as np
import tifffile

OUT = Path(__file__).parents[2] / "tiff"
rng = np.random.default_rng(0)


def image(shape, dtype):
    y, x = np.mgrid[0 : shape[-2], 0 : shape[-1]]
    base = (x * 3 + y * 5) % 251
    out = np.broadcast_to(base, shape).astype(np.float64)
    out = out + np.arange(np.prod(shape[:-2]) or 1).reshape((*shape[:-2], 1, 1)) * 17
    out = out + rng.integers(0, 4, shape)
    return out.astype(dtype)


def pyramid(path, data, levels, *, axes, downsample_axes=2, **kw):
    """An OME-TIFF with `levels` SubIFD levels, each half the size."""
    # A UUID from the file's name, not tifffile's (time-based) one: reproducible files.
    ome_meta = {"axes": axes, "PhysicalSizeX": 0.5, "PhysicalSizeY": 0.5,
                "UUID": str(uuid.uuid5(uuid.NAMESPACE_URL, f"vzip-fixture:{Path(path).name}"))}
    if "Z" in axes:
        ome_meta["PhysicalSizeZ"] = 2.0
    bigtiff = kw.pop("bigtiff", False)
    byteorder = kw.pop("byteorder", "<")
    with tifffile.TiffWriter(path, bigtiff=bigtiff, byteorder=byteorder, ome=True) as tif:
        tif.write(data, subifds=levels, metadata=ome_meta, **kw)
        for i in range(1, levels + 1):
            s = 2**i
            sl = (slice(None),) * (data.ndim - downsample_axes) + (slice(None, None, s),) * 2
            if axes.endswith("S"):
                sl = (slice(None),) * (data.ndim - 3) + (slice(None, None, s),) * 2 + (slice(None),)
            tif.write(data[sl], subfiletype=1, **kw)


def patch_short(path: Path, tag: str, value: int) -> None:
    """The first page's `tag`, a SHORT held in its entry, set to `value`."""
    with tifffile.TiffFile(path) as tf:
        page = tf.pages[0]
        assert isinstance(page, tifffile.TiffPage)
        t = page.tags[tag]
        at, order = t.valueoffset, tf.byteorder
    b = bytearray(path.read_bytes())
    b[at:at + 2] = value.to_bytes(2, "little" if order == "<" else "big")
    path.write_bytes(b)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    rgb = image((3, 96, 128), np.uint8)
    pyramid(OUT / "rgb_planar_jpeg2000_bigtiff_be.ome.tif", rgb, 2, axes="CYX",
            bigtiff=True, byteorder=">", tile=(32, 32), compression="jpeg2000",
            photometric="rgb", planarconfig="separate")
    pyramid(OUT / "tczyx_uint16_deflate.ome.tif", image((2, 2, 3, 96, 128), np.uint16), 1,
            axes="TCZYX", tile=(32, 32), compression="zlib", photometric="minisblack")
    pyramid(OUT / "rgb_contig_zstd.ome.tif", np.moveaxis(rgb, 0, -1).copy(), 1, axes="YXS",
            tile=(32, 48), compression="zstd", photometric="rgb", planarconfig="contig")
    pyramid(OUT / "float32_uncompressed_be.ome.tif", image((80, 112), np.float32), 0,
            axes="YX", byteorder=">", tile=(32, 32), photometric="minisblack")

    # Not OME: levels as later main-chain IFDs, with a stripped thumbnail
    # between them (as in SVS).
    with tifffile.TiffWriter(OUT / "svs_like_int16.tif") as tif:
        data = image((96, 128), np.int16) - 100
        tif.write(data, tile=(32, 32), compression="zlib", photometric="minisblack")
        tif.write(data[::8, ::8], photometric="minisblack")  # thumbnail, not tiled
        tif.write(data[::2, ::2], tile=(32, 32), compression="zlib", photometric="minisblack")

    # LZW tiles, and images in strips (spec/zarrify/tiff.md §4.5
    # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>): one strip; strips whose
    # last is short, padded with zeros (uncompressed) or an LZW tail; interleaved and planar
    # RGB; an OME-TIFF of several planes and a SubIFD level in strips.
    small = image((64, 64), np.uint8)
    tifffile.imwrite(OUT / "lzw_uint8.tif", small, tile=(32, 32), compression="lzw")
    tifffile.imwrite(OUT / "strips_uint8.tif", small)
    tall = image((70, 50), np.uint16)
    tifffile.imwrite(OUT / "strips_uint16_short.tif", tall, rowsperstrip=16, photometric="minisblack")
    tifffile.imwrite(OUT / "strips_uint16_lzw_short.tif", tall, rowsperstrip=16, compression="lzw",
                     photometric="minisblack")
    tifffile.imwrite(OUT / "strips_rgb_contig_short.tif", np.moveaxis(image((3, 70, 50), np.uint8), 0, -1).copy(),
                     rowsperstrip=16, photometric="rgb", planarconfig="contig")
    pyramid(OUT / "strips_rgb_planar_lzw.ome.tif", image((3, 70, 50), np.uint8), 1, axes="CYX",
            rowsperstrip=16, compression="lzw", photometric="rgb", planarconfig="separate")
    pyramid(OUT / "strips_tcyx_uint16_lzw.ome.tif", image((2, 3, 70, 50), np.uint16), 1, axes="TCYX",
            rowsperstrip=16, compression="lzw", photometric="minisblack")
    # two uncompressed levels whose last strips lack different rows: one zero data source
    pyramid(OUT / "strips_uint16_pyramid_short.ome.tif", image((70, 50), np.uint16), 1, axes="YX",
            rowsperstrip=16, photometric="minisblack")

    # Predictor 2, horizontal differencing (spec/zarrify/tiff.md §4.3
    # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#43-data-types-and-codecs>),
    # which imagecodecs_delta sums back.
    tifffile.imwrite(OUT / "predictor_deflate_uint8.tif", small, tile=(32, 32),
                     compression="zlib", predictor=True)
    # Rejected: Deflate strips whose last is short, which cannot be padded.
    tifffile.imwrite(OUT / "edge_reject_strips_deflate_short.tif", tall, rowsperstrip=16, compression="zlib",
                     photometric="minisblack")

    # More of Predictor 2 (written last, so that the fixtures above keep their pixels): uint16
    # whose differences wrap around, in LZW, Deflate and zstd, little- and big-endian; int16;
    # interleaved RGB of uint8 and uint16; LZW strips whose last is short, padded with a tail;
    # and an OME-TIFF of several planes and a SubIFD level. Rejected: the floating-point
    # predictor 3; Predictor 2 with a float type, or with no compression.
    wide = image((64, 64), np.uint16) * 257
    tifffile.imwrite(OUT / "predictor_lzw_uint16.tif", wide, tile=(32, 32), compression="lzw",
                     predictor=True, photometric="minisblack")
    tifffile.imwrite(OUT / "predictor_deflate_uint16_be.tif", wide, byteorder=">", tile=(32, 32),
                     compression="zlib", predictor=True, photometric="minisblack")
    tifffile.imwrite(OUT / "predictor_zstd_int16.tif", (wide.astype(np.int32) - 32768).astype(np.int16),
                     tile=(32, 48), compression="zstd", predictor=True, photometric="minisblack")
    tifffile.imwrite(OUT / "predictor_rgb_contig_lzw.tif", np.moveaxis(image((3, 70, 50), np.uint8), 0, -1).copy(),
                     tile=(32, 32), compression="lzw", predictor=True, photometric="rgb", planarconfig="contig")
    tifffile.imwrite(OUT / "predictor_rgb_contig_uint16_deflate.tif",
                     np.moveaxis(image((3, 64, 50), np.uint16) * 257, 0, -1).copy(), rowsperstrip=16,
                     compression="zlib", predictor=True, photometric="rgb", planarconfig="contig")
    tifffile.imwrite(OUT / "predictor_strips_uint16_lzw_short.tif", image((70, 50), np.uint16) * 257,
                     rowsperstrip=16, compression="lzw", predictor=True, photometric="minisblack")
    pyramid(OUT / "predictor_tcyx_uint16_lzw.ome.tif", image((2, 3, 64, 64), np.uint16) * 257, 1, axes="TCYX",
            tile=(32, 32), compression="lzw", predictor=True, photometric="minisblack")
    floats = image((64, 64), np.float32) / 7
    tifffile.imwrite(OUT / "unsupported_predictor_float.tif", floats, tile=(32, 32), compression="zlib",
                     predictor=True, photometric="minisblack")  # tifffile's predictor of floats is 3
    tifffile.imwrite(OUT / "unsupported_predictor_2_float.tif", floats, tile=(32, 32), compression="zlib",
                     predictor=True, photometric="minisblack")
    patch_short(OUT / "unsupported_predictor_2_float.tif", "Predictor", 2)
    tifffile.imwrite(OUT / "edge_reject_predictor_uncompressed.tif", small, tile=(32, 32), compression="zlib",
                     predictor=True)
    patch_short(OUT / "edge_reject_predictor_uncompressed.tif", "Compression", 1)
    for p in sorted(OUT.glob("*.tif")):
        print(p.name, p.stat().st_size)


if __name__ == "__main__":
    main()
