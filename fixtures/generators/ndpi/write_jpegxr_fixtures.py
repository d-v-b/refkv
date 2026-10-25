"""Writes NDPI files whose levels are JPEG XR to fixtures/ndpi/, for spec/zarrify/ndpi.md §3
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#3-levels> and §7
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#7-ndpi-profile>, laid out as a
Hamamatsu NanoZoomer (C13210) writes them (d-v-b/refkv#123). Their tags say every level is 8-bit
YCbCr with 3 samples, whatever its JPEG XR streams hold.

- `ndpi_jpegxr_gray16.ndpi`: a fluorescence slide. Its streams are 16-bit grayscale. It has two
  tiled levels with partial tiles at the right and bottom edges, and in level 1 one tile of byte
  count 0. Level 2 is a single strip. The tile tables have high bytes (0s), there is a macro
  image of negative magnification, and the levels have the acquisition tags Fluorescence,
  Wavelength and ExposureTime, tag 65433, and CaptureMode 17 (from which tifffile, like
  Bio-Formats, takes the levels to be 16-bit grayscale).
- `ndpi_jpegxr_rgb.ndpi`: a brightfield slide in 24bppRGB: one tiled level and one single strip.
  Its red samples are the brightest and its blue ones the darkest, so that their order shows.
- `ndpi_jpegxr_bgr.ndpi`: the same, its streams labeled 24bppBGR, which decodes in R, G, B
  order too. Level 0 is a single tile: its tile tables are one value in line, whose high bits
  are its entries' high words (0), so its high tables, of 1s, are not added to them.
- `edge_reject_ndpi_jpegxr_*.ndpi`, `edge_reject_ndpi_compressions.ndpi`: inputs §3 and §7
  reject.

Pixels are checked against tifffile by verify.py.

Usage: uv run python fixtures/generators/ndpi/write_jpegxr_fixtures.py
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import imagecodecs
import numpy as np

# the script's directory, on sys.path when it runs; not on pyrefly's search-path, since every
# generator directory has a write_fixtures module
from write_fixtures import Ndpi  # pyrefly: ignore[missing-import]

OUT = Path(__file__).parents[2] / "ndpi"
JPEGXR = 22610  # Hamamatsu's Compression for JPEG XR (tifffile's JPEGXR_NDPI)
ASCII, SHORT, LONG, RATIONAL, SLONG, FLOAT = 2, 3, 4, 5, 9, 11
rng = np.random.default_rng(123)


def jxr(pixels: np.ndarray) -> bytes:
    """A JPEG XR file of `pixels` ([y, x] or [y, x, 3]); its pixel format is jxrlib's for the
    data type and samples (Gray8, Gray16, Gray32Float, 24bppRGB, 48bppRGB)."""
    # imagecodecs' stubs: an encoder gives bytes, without out=
    return cast("bytes", imagecodecs.jpegxr_encode(pixels, level=0.9))


def image(dtype: str, height: int, width: int, samples: int = 1) -> np.ndarray:
    """A smooth gradient with noise, of `dtype`, as a slide's image is."""
    y, x = np.mgrid[0:height, 0:width]
    top = 255 if dtype == "uint8" else 20000
    base = (top * 0.2 + top * 0.6 * (x / max(width - 1, 1) + y / max(height - 1, 1)) / 2)
    planes = [base * (1 - 0.25 * s) + rng.normal(0, top * 0.02, base.shape) for s in range(samples)]
    stack = np.stack(planes, axis=-1) if samples > 1 else planes[0]
    return np.clip(stack, 1, top).astype(dtype)


def bgr(stream: bytes) -> bytes:
    """A 24bppRGB JPEG XR file relabeled 24bppBGR: its pixel format GUID's last byte 0C for 0D.
    The codestream's color is the codec's own, so it decodes to the same pixels."""
    guid = bytes.fromhex("24c3dd6f034efe4bb1853d77768dc90d")
    at = stream.index(guid)
    return stream[: at + 15] + b"\x0c" + stream[at + 16 :]


def tiles(pixels: np.ndarray, tile_height: int, tile_width: int) -> list[bytes]:
    """The tiles of `pixels`, row-major, each a whole tile (zeros past the image's edges)."""
    height, width = pixels.shape[:2]
    out = []
    for y in range(0, height, tile_height):
        for x in range(0, width, tile_width):
            tile = np.zeros((tile_height, tile_width, *pixels.shape[2:]), pixels.dtype)
            part = pixels[y : y + tile_height, x : x + tile_width]
            tile[: part.shape[0], : part.shape[1]] = part
            out.append(jxr(tile))
    return out


def tags(width: int, height: int, mag: float, compression: int = JPEGXR) -> dict[int, tuple[int, object]]:
    """A level's tags as Hamamatsu writes them: 8-bit YCbCr with 3 samples, whatever the
    streams hold."""
    return {
        256: (LONG, [width]), 257: (LONG, [height]), 258: (SHORT, [8, 8, 8]), 259: (SHORT, [compression]),
        262: (SHORT, [6]), 271: (ASCII, b"Hamamatsu\0"), 272: (ASCII, b"C13210\0"), 277: (SHORT, [3]),
        282: (RATIONAL, [(22643, 1)]), 283: (RATIONAL, [(22643, 1)]), 296: (SHORT, [3]), 530: (SHORT, [1, 1]),
        65420: (SHORT, [1]), 65421: (FLOAT, [mag]),
        65422: (SLONG, [14568878]), 65423: (SLONG, [33761]),
    }


def directory(f: Ndpi, entries: dict[int, tuple[int, object]]) -> None:
    f.ifds.append(f.directory([(tag, *entries[tag]) for tag in sorted(entries)]))


def tiled(f: Ndpi, streams: list[bytes], width: int, height: int, tile: tuple[int, int], mag: float,
          extra: dict[int, tuple[int, object]] | None = None, absent: tuple[int, ...] = ()) -> None:
    """A tiled JPEG XR level: its tiles (those of `absent` with byte count 0), TileWidth and
    TileLength `tile`, and tile tables with high bytes of 0."""
    offsets = [0 if j in absent else f.blob(s) for j, s in enumerate(streams)]
    counts = [0 if j in absent else len(s) for j, s in enumerate(streams)]
    entries = tags(width, height, mag)
    entries |= {322: (LONG, [tile[1]]), 323: (LONG, [tile[0]]), 324: (LONG, offsets), 325: (LONG, counts),
                65324: (LONG, [0] * len(offsets)), 65325: (LONG, [0] * len(offsets))}
    entries |= extra or {}
    directory(f, entries)


def strip(f: Ndpi, stream: bytes, width: int, height: int, mag: float, compression: int = JPEGXR,
          extra: dict[int, tuple[int, object]] | None = None) -> None:
    """A JPEG XR level (or macro image) in one strip."""
    entries = tags(width, height, mag, compression)
    entries |= {273: (LONG, [f.blob(stream)]), 278: (LONG, [height]), 279: (LONG, [len(stream)])}
    entries |= extra or {}
    directory(f, entries)


ACQUISITION: dict[int, tuple[int, object]] = {
    65433: (LONG, [1500]), 65434: (ASCII, b"TxRed\0"), 65441: (LONG, [17]), 65451: (FLOAT, [650.0]),
    65454: (LONG, [1024000]),
}


def main() -> None:
    OUT.mkdir(exist_ok=True)
    gray = image("uint16", 100, 160)
    level1 = gray[::2, ::2]
    level2 = level1[::2, ::2]
    macro = jxr(image("uint8", 16, 48, 3))
    l0, l1 = tiles(gray, 64, 64), tiles(level1, 32, 32)

    f = Ndpi()
    tiled(f, l0, 160, 100, (64, 64), 20.0, extra=ACQUISITION)
    tiled(f, l1, 80, 50, (32, 32), 10.0, extra=ACQUISITION, absent=(4,))
    strip(f, jxr(level2), 40, 25, 5.0, extra=ACQUISITION)
    strip(f, macro, 48, 16, -1.0)
    f.write("ndpi_jpegxr_gray16.ndpi")

    rgb = image("uint8", 60, 90, 3)
    f = Ndpi()
    tiled(f, tiles(rgb, 32, 48), 90, 60, (32, 48), 20.0)
    strip(f, jxr(rgb[::2, ::2]), 45, 30, 10.0)
    f.write("ndpi_jpegxr_rgb.ndpi")
    f = Ndpi()
    tiled(f, [bgr(jxr(rgb[:32, :48]))], 48, 32, (32, 48), 20.0, extra={65324: (LONG, [1]), 65325: (LONG, [1])})
    strip(f, bgr(jxr(rgb[:16:2, :24:2])), 12, 8, 10.0)
    f.write("ndpi_jpegxr_bgr.ndpi")

    # Rejected.
    f = Ndpi()  # a pixel format the profile does not read (Gray32Float)
    strip(f, jxr(image("uint16", 16, 24).astype(np.float32)), 24, 16, 20.0)
    f.write("edge_reject_ndpi_jpegxr_format.ndpi")
    f = Ndpi()  # level 1's streams are Gray8, level 0's Gray16
    tiled(f, l0, 160, 100, (64, 64), 20.0)
    strip(f, jxr((level1 // 256).astype(np.uint8)), 80, 50, 10.0)
    f.write("edge_reject_ndpi_jpegxr_formats.ndpi")
    f = Ndpi()  # the streams are 32 × 32 tiles, TileWidth and TileLength say 64
    tiled(f, tiles(gray, 32, 32)[:6], 160, 100, (64, 64), 20.0)
    f.write("edge_reject_ndpi_jpegxr_size.ndpi")
    f = Ndpi()  # one tile too few
    tiled(f, l0[:-1], 160, 100, (64, 64), 20.0)
    f.write("edge_reject_ndpi_jpegxr_tiles.ndpi")
    f = Ndpi()  # OffsetHighBytes of 1 puts the last tile 4 GiB further, outside the file
    tiled(f, l0, 160, 100, (64, 64), 20.0, extra={65324: (LONG, [0] * (len(l0) - 1) + [1])})
    f.write("edge_reject_ndpi_jpegxr_high_bytes.ndpi")
    f = Ndpi()  # OffsetHighBytes has one value too few
    tiled(f, l0, 160, 100, (64, 64), 20.0, extra={65324: (LONG, [0] * (len(l0) - 1))})
    f.write("edge_reject_ndpi_jpegxr_high_length.ndpi")
    f = Ndpi()  # a stream that is not a JPEG XR file (a JPEG stream)
    jpeg = cast("bytes", imagecodecs.jpeg8_encode(image("uint8", 25, 40, 3), level=85))
    strip(f, jpeg, 40, 25, 20.0)
    f.write("edge_reject_ndpi_jpegxr_header.ndpi")
    f = Ndpi()  # two strips, no tiles
    half = len(macro) // 2 & ~1
    strip(f, macro, 48, 16, 20.0, extra={273: (LONG, [f.blob(macro[:half]), f.blob(macro[half:])]), 278: (LONG, [8]),
                                          279: (LONG, [half, len(macro) - half])})
    f.write("edge_reject_ndpi_jpegxr_strips.ndpi")
    f = Ndpi()  # a JPEG XR level 0 and a JPEG level 1
    strip(f, jxr(level1), 80, 50, 20.0)
    strip(f, jpeg, 40, 25, 10.0, compression=7)
    f.write("edge_reject_ndpi_compressions.ndpi")
    for p in [*sorted(OUT.glob("*jpegxr*")), OUT / "edge_reject_ndpi_compressions.ndpi"]:
        print(p.name, p.stat().st_size)


if __name__ == "__main__":
    main()
