"""Writes the TIFF fixtures of packed samples and bilevel images in fixtures/tiff/ (spec/zarrify/tiff.md
§4.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#43-data-types-and-codecs>): 1- and
12-bit samples, PackBits, CCITT Group 4 and EER, which tifffile writes only some of: the rest are
written here, tag by tag, with imagecodecs' and Pillow's encoders. Each is written with an image the file's
reader gets back, which python/tests/ir/test_tiff.py compares.

Usage: uv run python fixtures/generators/tiff/write_packed_fixtures.py
"""

from __future__ import annotations

import io
import struct
import zlib
from pathlib import Path

import imagecodecs
import numpy as np
import tifffile
from PIL import Image

OUT = Path(__file__).parents[2] / "tiff"
SHORT, LONG, UNDEFINED = 3, 4, 7


def bilevel(height: int, width: int) -> np.ndarray:
    """An image of 1 bit a sample with runs and lone bits, as a scan's."""
    y, x = np.mgrid[0:height, 0:width]
    return ((x // 3 + y // 5) % 2 == 0) | ((x * 7 + y * 11) % 23 == 0)


def twelve(height: int, width: int, samples: int = 1) -> np.ndarray:
    y, x = np.mgrid[0:height, 0:width]
    a = (x * 37 + y * 101) % 4096
    return (
        a.astype(np.uint16)
        if samples == 1
        else np.dstack([(a + s * 9) % 4096 for s in range(samples)]).astype(np.uint16)
    )


def write(name: str, width: int, height: int, strips: list[bytes], rows: int, tags: list[tuple[int, int, list[int]]]):
    """A little-endian TIFF of `strips`, without tags but those given and the size, strip and row tags."""
    buf = bytearray(b"II*\0\0\0\0\0")
    offsets = []
    for s in strips:
        offsets.append(len(buf))
        buf += s
        if len(buf) % 2:
            buf += b"\0"
    entries = sorted(
        [
            (256, SHORT, [width]),
            (257, SHORT, [height]),
            (278, SHORT, [rows]),
            (273, LONG, offsets),
            (279, LONG, [len(s) for s in strips]),
            *tags,
        ]
    )
    out_of_line = bytearray()
    start = len(buf) + 2 + 12 * len(entries) + 4
    packed = []
    for tag, typ, values in entries:
        raw = struct.pack(f"<{len(values)}{'H' if typ == SHORT else 'I'}", *values)
        if len(raw) <= 4:
            field = raw.ljust(4, b"\0")
        else:
            field = struct.pack("<I", start + len(out_of_line))
            out_of_line += raw
        packed.append(struct.pack("<HHI", tag, typ, len(values)) + field)
    struct.pack_into("<I", buf, 4, len(buf))
    buf += struct.pack("<H", len(entries)) + b"".join(packed) + b"\0\0\0\0" + out_of_line
    (OUT / name).write_bytes(bytes(buf))


def strips_of(a: np.ndarray, rows: int) -> list[np.ndarray]:
    return [a[i : i + rows] for i in range(0, len(a), rows)]


def packed(a: np.ndarray, bits: int) -> bytes:
    """Samples packed most significant bit first, each row on a whole byte."""
    return bytes(
        imagecodecs.packints_encode(
            a.astype(np.uint16 if bits > 8 else np.uint8), bits, runlen=a.shape[1] * (a.shape[2] if a.ndim == 3 else 1)
        )
    )


def g4(a: np.ndarray) -> bytes:
    """A CCITT Group 4 stream of `a`, as Pillow's libtiff writes it (imagecodecs has no encoder), its
    samples as `imagecodecs.ccittfax4_decode` gives them back."""
    with io.BytesIO() as f:
        Image.fromarray(a).save(f, format="TIFF", compression="group4")
        f.seek(0)
        with tifffile.TiffFile(f) as tf:
            page = tf.pages.first
            assert len(page.dataoffsets) == 1
            f.seek(page.dataoffsets[0])
            stream = f.read(page.databytecounts[0])
    assert np.array_equal(imagecodecs.ccittfax4_decode(stream, height=a.shape[0], width=a.shape[1]) != 0, a)
    return stream


def eer(mask: np.ndarray, skipbits: int, horzbits: int, vertbits: int) -> bytes:
    """One frame of events: `skipbits` bits of pixels skipped, least significant bit first, a skip of
    all ones going on to the next, and then the event's sub-pixel bits. Checked with the decoder,
    which imagecodecs alone holds (it has no encoder)."""
    bits: list[int] = []

    def put(value: int, n: int) -> None:
        bits.extend((value >> i) & 1 for i in range(n))

    run, most = 0, (1 << skipbits) - 1
    for v in mask.ravel():
        if v:
            while run >= most:
                put(most, skipbits)
                run -= most
            put(run, skipbits)
            put(0, horzbits + vertbits)
            run = 0
        else:
            run += 1
    # the decoder reads past the last event only to see a skip that passes the frame's end
    while run >= most:
        put(most, skipbits)
        run -= most
    put(run, skipbits)
    put(most, skipbits)
    stream = bytearray((len(bits) + 7) // 8)
    for i, b in enumerate(bits):
        stream[i // 8] |= b << (i % 8)
    decoded = imagecodecs.eer_decode(bytes(stream), mask.shape, skipbits, horzbits, vertbits)
    assert np.array_equal(decoded, mask)
    return bytes(stream)


def events(height: int, width: int, frame: int) -> np.ndarray:
    """A frame of sparse events, none in its last row, so that its stream ends with a skip."""
    mask = np.zeros((height, width), bool)
    y, x = np.mgrid[0 : height - 1, 0:width]
    mask[:-1] = (x * 13 + y * 7 + frame * 5) % 31 == 0
    return mask


def main() -> None:
    OUT.mkdir(exist_ok=True)
    white_is_zero = (262, SHORT, [0])
    black_is_zero = (262, SHORT, [1])

    # 1 bit, as tifffile writes it, without tag 258 (TIFF 6.0's default): uncompressed, in tiles, in
    # Deflate and in PackBits.
    b = bilevel(40, 45)
    tifffile.imwrite(OUT / "bilevel.tif", b, metadata=None)
    tifffile.imwrite(OUT / "bilevel_tiled.tif", b, tile=(16, 16), metadata=None)
    tifffile.imwrite(OUT / "bilevel_deflate.tif", b, compression="zlib", metadata=None)
    tifffile.imwrite(OUT / "bilevel_packbits.tif", b, compression="packbits", metadata=None)
    # Strips, which tifffile does not write of 1 bit: joined, the last short and padded.
    write("bilevel_strips_short.tif", 45, 40, [packed(s, 1) for s in strips_of(b, 16)], 16, [white_is_zero])
    write(
        "bilevel_lzw_strips_short.tif",
        45,
        40,
        [bytes(imagecodecs.lzw_encode(packed(s, 1))) for s in strips_of(b, 16)],
        16,
        [(259, SHORT, [5]), white_is_zero],
    )
    # A short last strip of PackBits, which cannot be padded: the `rectilinear` grid.
    write(
        "bilevel_packbits_strips_short.tif",
        45,
        40,
        [bytes(imagecodecs.packbits_encode(packed(s, 1))) for s in strips_of(b, 16)],
        16,
        [(259, SHORT, [32773]), white_is_zero],
    )
    # CCITT Group 4 (T.6), each strip its own, with and without tag 258, and whole strips; a short last
    # strip has the `rectilinear` grid.
    write("bilevel_g4.tif", 45, 40, [g4(b)], 40, [(258, SHORT, [1]), (259, SHORT, [4]), white_is_zero])
    write(
        "bilevel_g4_strips_short.tif",
        45,
        40,
        [g4(s) for s in strips_of(b, 16)],
        16,
        [(259, SHORT, [4]), white_is_zero],
    )
    b48 = bilevel(48, 45)
    write(
        "bilevel_g4_strips_no_bits.tif",
        45,
        48,
        [g4(s) for s in strips_of(b48, 16)],
        16,
        [(259, SHORT, [4]), black_is_zero],
    )

    # 12 bits: tifffile's, uncompressed; interleaved, planar and big-endian; and compressed, which it
    # does not write.
    t = twelve(70, 51)
    tifffile.imwrite(
        OUT / "uint12_strips.tif", t, bitspersample=12, rowsperstrip=70, metadata=None, photometric="minisblack"
    )
    tifffile.imwrite(
        OUT / "uint12_strips_short.tif", t, bitspersample=12, rowsperstrip=16, metadata=None, photometric="minisblack"
    )
    tifffile.imwrite(
        OUT / "uint12_be.tif",
        t,
        bitspersample=12,
        rowsperstrip=70,
        byteorder=">",
        metadata=None,
        photometric="minisblack",
    )
    tifffile.imwrite(
        OUT / "uint12_rgb_contig.tif",
        twelve(70, 51, 3),
        bitspersample=12,
        rowsperstrip=70,
        metadata=None,
        photometric="rgb",
        planarconfig="contig",
    )
    write(
        "uint12_deflate_strips.tif",
        51,
        64,
        [zlib.compress(packed(s, 12)) for s in strips_of(t[:64], 16)],
        16,
        [(258, SHORT, [12]), (259, SHORT, [8]), black_is_zero],
    )
    write(
        "uint12_lzw.tif",
        51,
        16,
        [bytes(imagecodecs.lzw_encode(packed(t[:16], 12)))],
        16,
        [(258, SHORT, [12]), (259, SHORT, [5]), black_is_zero],
    )

    # PackBits of whole samples.
    y, x = np.mgrid[0:64, 0:48]
    runs = (x // 4 * 16 + y // 8).astype(np.uint8)
    tifffile.imwrite(
        OUT / "packbits_uint8.tif",
        runs,
        compression="packbits",
        rowsperstrip=16,
        metadata=None,
        photometric="minisblack",
    )
    tifffile.imwrite(
        OUT / "packbits_uint16_be.tif",
        runs.astype(np.uint16) * 257,
        compression="packbits",
        rowsperstrip=16,
        byteorder=">",
        metadata=None,
        photometric="minisblack",
    )
    tifffile.imwrite(
        OUT / "packbits_rgb_contig.tif",
        np.dstack([runs, runs // 2, runs // 3]),
        compression="packbits",
        tile=(32, 32),
        metadata=None,
        photometric="rgb",
        planarconfig="contig",
    )

    # EER: one frame, whose codec's bits are the compression's, and frames whose bits are in tags.
    for name, compression, bits, frames in (
        ("eer_v0_frame.tif", 65000, (8, 2, 2), 1),
        ("eer_v1_frames.tif", 65001, (7, 2, 2), 3),
        ("eer_v2_frames.tif", 65002, (8, 3, 1), 2),
    ):
        ifds = [eer(events(32, 48, f), *bits) for f in range(frames)]
        chain_eer(name, ifds, 48, 32, compression, bits)

    # Rejected: a width refkv holds no codec of; signed; EER's and CCITT's other sample sizes; FillOrder 2;
    # 1 bit in a compression other than these; Predictor 2 of 12 bits; a stack of pages; CCITT Group 3.
    write("edge_reject_bits_10.tif", 8, 8, [bytes(10)], 8, [(258, SHORT, [10]), black_is_zero])
    write("edge_reject_bits_12_int.tif", 8, 8, [bytes(96)], 8, [(258, SHORT, [12]), black_is_zero, (339, SHORT, [2])])
    write("edge_reject_ccitt_uint8.tif", 8, 8, [bytes(8)], 8, [(258, SHORT, [8]), (259, SHORT, [4]), black_is_zero])
    write(
        "edge_reject_ccitt_fill_order.tif",
        8,
        8,
        [bytes(8)],
        8,
        [(258, SHORT, [1]), (259, SHORT, [4]), white_is_zero, (266, SHORT, [2])],
    )
    write("edge_reject_ccitt_group3.tif", 8, 8, [bytes(8)], 8, [(258, SHORT, [1]), (259, SHORT, [3]), white_is_zero])
    write(
        "edge_reject_bits_1_jpeg2000.tif",
        8,
        8,
        [bytes(8)],
        8,
        [(258, SHORT, [1]), (259, SHORT, [34712]), black_is_zero],
    )
    write(
        "edge_reject_bits_12_predictor.tif",
        8,
        8,
        [zlib.compress(bytes(96))],
        8,
        [(258, SHORT, [12]), (259, SHORT, [8]), black_is_zero, (317, SHORT, [2])],
    )
    write("edge_reject_eer_uint8.tif", 8, 8, [bytes(8)], 8, [(258, SHORT, [8]), (259, SHORT, [65001]), black_is_zero])
    write(
        "edge_reject_eer_short.tif",
        8,
        12,
        [bytes(8), bytes(4)],
        8,
        [(258, SHORT, [1]), (259, SHORT, [65001]), black_is_zero],
    )
    tifffile.imwrite(OUT / "edge_reject_bilevel_pages.tif", np.stack([b, ~b]), metadata=None)
    chain_eer("edge_reject_eer_bits_differ.tif", [bytes(8), bytes(8)], 8, 8, 65002, (7, 2, 2), other=(8, 2, 2))
    for p in sorted(OUT.glob("*.tif")):
        print(p.name, p.stat().st_size)


def chain_eer(
    name: str,
    frames: list[bytes],
    width: int,
    height: int,
    compression: int,
    bits: tuple[int, int, int],
    other: tuple[int, int, int] | None = None,
) -> None:
    """A little-endian TIFF of one IFD a frame, each frame one strip, with an EER file's tags: for
    `65002`, the bits, or `other`'s from the second frame on."""
    buf = bytearray(b"II*\0\0\0\0\0")
    next_at = 4
    for i, frame in enumerate(frames):
        offset = len(buf)
        buf += frame
        if len(buf) % 2:
            buf += b"\0"
        entries = [
            (256, SHORT, width),
            (257, SHORT, height),
            (258, SHORT, 1),
            (259, SHORT, compression),
            (262, SHORT, 1),
            (273, LONG, offset),
            (277, SHORT, 1),
            (278, SHORT, height),
            (279, LONG, len(frame)),
        ]
        if compression == 65002:
            skip, horz, vert = other if other is not None and i > 0 else bits
            entries += [(65007, SHORT, skip), (65008, SHORT, horz), (65009, SHORT, vert)]
        struct.pack_into("<I", buf, next_at, len(buf))
        buf += struct.pack("<H", len(entries))
        for tag, typ, value in entries:
            field = struct.pack("<H", value).ljust(4, b"\0") if typ == SHORT else struct.pack("<I", value)
            buf += struct.pack("<HHI", tag, typ, 1) + field
        next_at = len(buf)
        buf += b"\0\0\0\0"
    (OUT / name).write_bytes(bytes(buf))


if __name__ == "__main__":
    main()
