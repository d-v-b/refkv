"""Writes NDPI files with focal planes to fixtures/ndpi/, for spec/zarrify/ndpi.md §3
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#3-levels> and §4
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#4-the-image>, laid out as a
NanoZoomer writes them (SR1274-908A.ndpi of OME's sample images, 3 planes of 7 levels): every level
of a plane, then every level of the next, each IFD with its focal offset in tag 65424, an SLONG.

The JPEG streams are written here, with no JPEG library, so that they are the same bytes everywhere:
flat 8 × 8 blocks with a color by plane and place, and restart intervals. Each stream is a whole,
valid JPEG stream that tifffile and imagecodecs decode.

- `ndpi_focal_planes.ndpi`: 3 evenly spaced planes, in the chain out of order, of two levels with
  McuStarts, level 0 cut into 2 × 2 chunks whose bottom and right edges repeat intervals. A coarser
  level has only the middle plane, so it is left out; a macro image of negative magnification too.
- `ndpi_focal_uneven.ndpi`: 3 unevenly spaced planes of two levels in single strips, with offsets of
  the field types SHORT, SSHORT, LONG and SLONG. Level 0 also has an image for each way of being no
  plane: another size, McuStarts, an offset used already, no offset, and an offset that is a FLOAT.
- `ndpi_focal_jpegxr.ndpi`: 2 planes of a fluorescence slide in JPEG XR: 16-bit gray tiles in level 0
  and one strip in level 1.
- `edge_ndpi_focal_coarse.ndpi`: one plane in level 0 and 3 in level 1, which hold more pixels, so that
  level 1 is the hierarchy, at its scale from level 0's.
- `edge_ndpi_focal_sizes.ndpi`: two images of one magnification and of different sizes, without
  focal offsets: no `z` axis, and the second is left out.

Pixels are checked against tifffile by verify.py.

Usage: uv run python fixtures/generators/ndpi/write_focal_fixtures.py
"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

# the script's directory, on sys.path when it runs; not on pyrefly's search-path, since every
# generator directory has a write_fixtures module
from write_fixtures import Ndpi  # pyrefly: ignore[missing-import]
from write_jpegxr_fixtures import ACQUISITION, jxr, strip, tiled, tiles  # pyrefly: ignore[missing-import]

OUT = Path(__file__).parents[2] / "ndpi"
SHORT, LONG, SSHORT, SLONG, FLOAT = 3, 4, 8, 9, 11
Z = 65424  # ZOffsetFromSlideCenter


def segment(marker: int, body: bytes) -> bytes:
    return bytes([0xFF, marker]) + struct.pack(">H", len(body) + 2) + body


def jpeg(width: int, height: int, plane: int, restart: int | None = None) -> tuple[bytes, list[int]]:
    """A baseline JPEG stream of `width` × `height` pixels, of 8 × 8 MCUs of 3 components, each
    block one color, by `plane` and its place: with `restart`, a DRI of that many MCUs and the
    offsets in the stream of its restart intervals (McuStarts)."""
    tables = segment(0xDB, b"\x00" + b"\x01" * 64)  # a quantization table of 1s
    # 12 DC categories of 4 bits each, the code the category; the one AC symbol, EOB, of 1 bit, 0
    tables += segment(0xC4, b"\x00" + bytes([0, 0, 0, 12] + [0] * 12) + bytes(range(12)))
    tables += segment(0xC4, b"\x10" + bytes([1] + [0] * 15) + b"\x00")
    components = b"".join(bytes([c, 0x11, 0]) for c in (1, 2, 3))
    frame = segment(0xC0, struct.pack(">BHHB", 8, height, width, 3) + components)
    dri = segment(0xDD, struct.pack(">H", restart)) if restart else b""
    scan = segment(0xDA, b"\x03" + b"".join(bytes([c, 0]) for c in (1, 2, 3)) + bytes([0, 63, 0]))
    out = bytearray(b"\xff\xd8" + frame + tables + dri + scan)
    across, down = width // 8 + (width % 8 > 0), height // 8 + (height % 8 > 0)
    per = restart or across * down
    starts = []
    for first in range(0, across * down, per):
        if first:
            out += bytes([0xFF, 0xD0 + (first // per - 1) % 8])
        starts.append(len(out))
        out += interval(first, min(first + per, across * down), across, plane)
    return bytes(out) + b"\xff\xd9", starts


def interval(first: int, end: int, across: int, plane: int) -> bytes:
    """The entropy-coded MCUs `first` to `end`, the DC predictions starting from 0."""
    bits: list[int] = []
    predictions = [0, 0, 0]
    for mcu in range(first, end):
        down, along = divmod(mcu, across)
        y = 40 + (along * 7 + down * 5 + plane * 31) % 170
        colors = (y, 98 + (down * 3 + plane * 13) % 60, 98 + (along * 3 + plane * 17) % 60)
        for k, color in enumerate(colors):
            dc = 8 * (color - 128)  # a flat block's DC coefficient, its table being 1s
            diff = dc - predictions[k]
            predictions[k] = dc
            category = abs(diff).bit_length()
            bits += [(category >> i) & 1 for i in (3, 2, 1, 0)]
            extra = diff if diff > 0 else diff + (1 << category) - 1
            bits += [(extra >> i) & 1 for i in range(category - 1, -1, -1)]
            bits.append(0)  # EOB
    bits += [1] * (-len(bits) % 8)
    out = bytearray()
    for i in range(0, len(bits), 8):
        byte = sum(b << (7 - j) for j, b in enumerate(bits[i : i + 8]))
        out.append(byte)
        if byte == 0xFF:
            out.append(0)
    return bytes(out)


def add(f: Ndpi, width: int, height: int, mag: float, plane: int, offset: tuple[int, list] | None = None,
        restart: int | None = None) -> None:
    """A JPEG image of magnification `mag`, of the colors of `plane`, with tag 65424 of the field
    type and values `offset`; with `restart`, McuStarts."""
    stream, starts = jpeg(width, height, plane, restart)
    f.image(stream, width, height, mag, starts if restart else None, extra={Z: offset} if offset else None)


def planes() -> None:
    f = Ndpi()
    for plane, offset in enumerate((0, -2000, 2000)):
        add(f, 1152, 1040, 20.0, plane, (SLONG, [offset]), restart=16)
        add(f, 256, 192, 5.0, plane, (SLONG, [offset]), restart=8)
        if offset == 0:
            add(f, 64, 48, 1.25, plane, (SLONG, [offset]))
    add(f, 48, 16, -1.0, 9)
    f.write("ndpi_focal_planes.ndpi")


def uneven() -> None:
    f = Ndpi()
    add(f, 64, 48, 20.0, 0, (SLONG, [0]))
    add(f, 32, 24, 5.0, 0, (SHORT, [0]))
    add(f, 64, 48, 20.0, 1, (SLONG, [3000]))
    add(f, 32, 24, 5.0, 1, (LONG, [3000]))
    add(f, 64, 48, 20.0, 2, (SLONG, [-1000]))
    add(f, 32, 24, 5.0, 2, (SSHORT, [-1000]))
    # no planes, each for its own reason
    add(f, 32, 24, 20.0, 3, (SLONG, [5000]))  # another size
    add(f, 64, 48, 20.0, 4, (SLONG, [6000]), restart=8)  # McuStarts, so other chunks
    add(f, 64, 48, 20.0, 5, (SLONG, [3000]))  # an offset used already
    add(f, 64, 48, 20.0, 6)  # no offset
    add(f, 64, 48, 20.0, 7, (FLOAT, [8000.0]))  # an offset of a field type that is not one
    f.write("ndpi_focal_uneven.ndpi")


def jpegxr() -> None:
    f = Ndpi()
    y, x = np.mgrid[0:60, 0:80]
    for plane, offset in enumerate((-500, 500)):
        gray = (1000 + 100 * x + 37 * y + 5000 * plane).astype(np.uint16)
        tiled(f, tiles(gray, 32, 32), 80, 60, (32, 32), 20.0, extra={**ACQUISITION, Z: (SLONG, [offset])})
        strip(f, jxr(gray[::2, ::2]), 40, 30, 10.0, extra={**ACQUISITION, Z: (SLONG, [offset])})
    f.write("ndpi_focal_jpegxr.ndpi")


def edges() -> None:
    f = Ndpi()
    add(f, 64, 48, 20.0, 0, (SLONG, [0]))
    for plane, offset in enumerate((-100, 0, 100)):
        add(f, 48, 40, 5.0, plane, (SLONG, [offset]))
    f.write("edge_ndpi_focal_coarse.ndpi")
    f = Ndpi()
    add(f, 256, 192, 20.0, 0, restart=16)
    add(f, 128, 96, 20.0, 1, restart=16)
    f.write("edge_ndpi_focal_sizes.ndpi")


def main() -> None:
    OUT.mkdir(exist_ok=True)
    planes()
    uneven()
    jpegxr()
    edges()
    for p in sorted(OUT.glob("*focal*")):
        print(p.name, p.stat().st_size)


if __name__ == "__main__":
    main()
