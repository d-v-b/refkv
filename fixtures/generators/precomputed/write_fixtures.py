"""Writes synthetic Neuroglancer precomputed volumes to fixtures/precomputed/<name>/, covering the
rules of the profile (spec/zarrify/precomputed.md §11
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/precomputed.md#11-neuroglancer-precomputed-profile>):
raw, JPEG, PNG and compressed_segmentation chunks; every content coding of a chunk object;
chunks clipped by the volume's upper bound, a voxel offset, and missing chunks; shards with raw
and gzip indexes and data; scales that are not presented, and the inputs the profile rejects
(`precomputed_reject_*`, one per rule).

Everything is written by hand from the format's description, so that the bytes are the same on
every platform: gzip and zstd as stored blocks, brotli as uncompressed meta-blocks, PNG with a
stored deflate stream. The JPEG streams are literals, made once with libjpeg-turbo, since a
JPEG encoder's bytes depend on its build. The values follow `value` below, which
python/tests/ir/test_precomputed.py states again on its own.

Usage: uv run python fixtures/generators/precomputed/write_fixtures.py
"""

from __future__ import annotations

import base64
import json
import shutil
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).parents[2] / "precomputed"
# made with imagecodecs.jpeg8_encode(level=90): gray 16 × 32 and 16 × 16 of (7x + 3y) % 256, and
# 16 × 32 of that, of it upside down and of its inverse, as R, G and B
JPEG = {k: base64.b64decode(v) for k, v in {
    "FULL": (
        "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQ"
        "ERMUFRUVDA8XGBYUGBIUFRT/wAALCAAgABABAREA/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgED"
        "AwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RF"
        "RkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJ"
        "ytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/9oACAEBAAA/APgf4f6J/q/l9K+lfh/on+r+X0ryr4f6J/q/l9K+lfh/"
        "on+r+X0ryr4f6J/q/l9K+lfh/on+r+X0ryr4f6J/q/l9K+lfh/on+r+X0r//2Q=="
    ),
    "CLIP": (
        "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQ"
        "ERMUFRUVDA8XGBYUGBIUFRT/wAALCAAQABABAREA/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgED"
        "AwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RF"
        "RkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJ"
        "ytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/9oACAEBAAA/APgf4f6J/q/l9K+lfh/on+r+X0ryr4f6J/q/l9K+lfh/"
        "on+r+X0r/9k="
    ),
    "RGB": (
        "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQ"
        "ERMUFRUVDA8XGBYUGBIUFRT/2wBDAQMEBAUEBQkFBQkUDQsNFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQU"
        "FBQUFBQUFBQUFBQUFBT/wAARCAAgABADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAA"
        "AgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6"
        "Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXG"
        "x8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREA"
        "AgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5"
        "OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPE"
        "xcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDzDwdd/c5r3Pwdd/c5r5y8HXf3Oa9z8HXf"
        "3Oa/Y/EDE/GfmWFyTbQ+c/B139zmvc/B139zmvnLwdd/c5r3Pwdd/c5r5HxAxPxn9AYXJNtD/9k="
    ),
}.items()}


def value(x: int, y: int, z: int, c: int = 0) -> int:
    """A voxel's value, in the volume's own coordinates (no offset)."""
    return (x + 3 * y + 5 * z + 7 * c) % 251


def gzip_stored(data: bytes) -> bytes:
    """A gzip member (RFC 1952) of stored deflate blocks, with no time, and the OS byte 255."""
    out = bytearray(b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff")
    n = len(data)
    out += zlib_stored(data)
    out += struct.pack("<II", zlib.crc32(data), n & 0xFFFFFFFF)
    return bytes(out)


def zlib_stored(data: bytes) -> bytes:
    """A raw deflate stream (RFC 1951) of stored blocks."""
    out = bytearray()
    at = 0
    while True:
        block = data[at:at + 65535]
        at += len(block)
        last = at >= len(data)
        out += struct.pack("<BHH", int(last), len(block), len(block) ^ 0xFFFF) + block
        if last:
            return bytes(out)


def brotli_stored(data: bytes) -> bytes:
    """A brotli stream (RFC 7932) of one uncompressed meta-block and an empty last one: WBITS 16
    (the bit 0), ISLAST 0, MNIBBLES 4 (00), MLEN - 1 in 16 bits, ISUNCOMPRESSED 1, padded to a byte."""
    assert 0 < len(data) <= 65536
    bits = ((len(data) - 1) << 4) | (1 << 20)
    return struct.pack("<I", bits)[:3] + data + b"\x03"


def zstd_stored(data: bytes) -> bytes:
    """A Zstandard frame (RFC 8878) of one raw block, its one-byte content size in the header."""
    assert len(data) <= 255
    block = struct.pack("<I", (len(data) << 3) | 1)[:3]
    return b"\x28\xb5\x2f\xfd\x20" + bytes([len(data)]) + block + data


def png_stored(rows: bytes, width: int, height: int, channels: int, depth: int) -> bytes:
    """A PNG (ISO/IEC 15948) of `rows`, big-endian for 16 bits, with a stored zlib stream."""
    color = {1: 0, 2: 4, 3: 2, 4: 6}[channels]
    stride = width * channels * depth // 8
    raw = b"".join(b"\0" + rows[y * stride:(y + 1) * stride] for y in range(height))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    head = struct.pack(">IIBBBBB", width, height, depth, color, 0, 0, 0)
    stream = b"\x78\x01" + zlib_stored(raw) + struct.pack(">I", zlib.adler32(raw))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", head) + chunk(b"IDAT", stream) + chunk(b"IEND", b"")


def write(root: Path, key: str, data: bytes) -> None:
    path = root / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def info(root: Path, doc) -> None:
    write(root, "info", json.dumps(doc, indent=1).encode())


def volume(data_type: str, channels: int, scales: list, kind: str = "image", **extra) -> dict:
    return {"@type": "neuroglancer_multiscale_volume", "type": kind, "data_type": data_type,
            "num_channels": channels, "scales": scales, **extra}


def scale(key: str, size, chunk, resolution, encoding="raw", offset=(0, 0, 0), **extra) -> dict:
    return {"key": key, "size": list(size), "chunk_sizes": [list(chunk)], "resolution": list(resolution),
            "voxel_offset": list(offset), "encoding": encoding, **extra}


def raw_chunk(lo, hi, channels: int = 1, item: int = 1, shift: tuple = (0, 0, 0), mod: int = 1 << 64) -> bytes:
    """A chunk's bytes: x fastest, then y, z and the channel, little-endian."""
    out = bytearray()
    for c in range(channels):
        for z in range(lo[2], hi[2]):
            for y in range(lo[1], hi[1]):
                for x in range(lo[0], hi[0]):
                    out += (value(x + shift[0], y + shift[1], z + shift[2], c) % mod).to_bytes(item, "little")
    return bytes(out)


def chunk_name(lo, hi, offset=(0, 0, 0)) -> str:
    return "_".join(f"{lo[a] + offset[a]}-{hi[a] + offset[a]}" for a in range(3))


def boxes(size, chunk):
    """Each chunk of the grid: (lo, hi, position), z slowest."""
    grid = [(s + c - 1) // c for s, c in zip(size, chunk, strict=True)]
    for k in range(grid[2]):
        for j in range(grid[1]):
            for i in range(grid[0]):
                p = (i, j, k)
                lo = tuple(p[a] * chunk[a] for a in range(3))
                hi = tuple(min(lo[a] + chunk[a], size[a]) for a in range(3))
                yield lo, hi, p


def bits_of(n: int) -> int:
    return (n - 1).bit_length() if n > 1 else 0


def morton(p, grid) -> int:
    """The compressed Morton id of a chunk."""
    bits = [bits_of(n) for n in grid]
    out, j = 0, 0
    for i in range(max(bits)):
        for a in range(3):
            if i < bits[a]:
                out |= ((p[a] >> i) & 1) << j
                j += 1
    return out


def shards(chunks: dict, preshift: int, minishard: int, shard_bits: int, data_gzip: bool, index_gzip: bool) -> dict:
    """The shard files of `chunks` (chunk id to its bytes), by shard number."""
    grouped: dict = {}
    for i in sorted(chunks):
        h = i >> preshift
        number = (h >> minishard) & ((1 << shard_bits) - 1)
        grouped.setdefault(number, {}).setdefault(h & ((1 << minishard) - 1), []).append(i)
    out = {}
    for number, minis in grouped.items():
        body = bytearray()
        tails = {}
        for m in sorted(minis):
            ids, offsets, sizes = [], [], []
            prev_id, prev_end = 0, 0
            for i in minis[m]:
                data = gzip_stored(chunks[i]) if data_gzip else chunks[i]
                ids.append(i - prev_id)
                offsets.append(len(body) - prev_end)
                sizes.append(len(data))
                prev_id, prev_end = i, len(body) + len(data)
                body += data
            n = len(ids)
            table = struct.pack(f"<{3 * n}Q", *ids, *offsets, *sizes)
            tails[m] = gzip_stored(table) if index_gzip else table
        index = bytearray()
        at = len(body)
        tail = bytearray()
        for m in range(1 << minishard):
            t = tails.get(m, b"")
            index += struct.pack("<QQ", at, at + len(t))
            at += len(t)
            tail += t
        out[number] = bytes(index) + bytes(body) + bytes(tail)
    return out


def shard_name(number: int, shard_bits: int) -> str:
    return f"{number:0{max(1, (shard_bits + 3) // 4)}x}.shard"


def compressed_segmentation(labels: dict, size, block, words: int) -> bytes:
    """One channel's compressed_segmentation stream: a channel offset, block headers, then each
    block's values and lookup table, labels[(x, y, z)] the label of a voxel."""
    grid = [(s + b - 1) // b for s, b in zip(size, block, strict=True)]
    count = grid[0] * grid[1] * grid[2]
    headers = []
    data = bytearray()
    base = 2 * count  # the words of the headers, from the channel's start
    for gz in range(grid[2]):
        for gy in range(grid[1]):
            for gx in range(grid[0]):
                cells = [labels.get((gx * block[0] + x, gy * block[1] + y, gz * block[2] + z), 0)
                         for z in range(block[2]) for y in range(block[1]) for x in range(block[0])]
                table = sorted(set(cells))
                bits = next(b for b in (0, 1, 2, 4, 8, 16, 32) if len(table) <= 1 << b)
                values_at = base + len(data) // 4
                if bits:
                    packed = 0
                    for n, v in enumerate(cells):
                        packed |= table.index(v) << (n * bits)
                    nwords = (len(cells) * bits + 31) // 32
                    data += packed.to_bytes(4 * nwords, "little")
                table_at = base + len(data) // 4
                for v in table:
                    data += v.to_bytes(4 * words, "little")
                headers += [table_at | (bits << 24), values_at]
    return struct.pack("<I", 1) + struct.pack(f"<{len(headers)}I", *headers) + bytes(data)


def raw_clipped() -> None:
    """uint8, chunks clipped along every axis, a voxel offset with a negative member, a missing
    chunk, and a second scale."""
    root = OUT / "precomputed_raw_clipped"
    size, chunk, offset = (10, 7, 5), (4, 4, 2), (2, -1, 0)
    small = (5, 4, 3)
    info(root, volume("uint8", 1, [scale("s0", size, chunk, (4, 4, 40), offset=offset),
                                   scale("s1", small, chunk, (8, 8, 80), offset=(1, -1, 0))]))
    write(root, "provenance", b'{"description": "a fixture"}')
    for lo, hi, p in boxes(size, chunk):
        if p == (1, 1, 1):  # missing: it reads as 0
            continue
        write(root, f"s0/{chunk_name(lo, hi, offset)}", raw_chunk(lo, hi))
    for lo, hi, _ in boxes(small, chunk):
        write(root, f"s1/{chunk_name(lo, hi, (1, -1, 0))}", raw_chunk(lo, hi, shift=(100, 0, 0)))


def codings() -> None:
    """uint16 in 2 channels, every content coding, chosen per object: by suffix or by nothing,
    as a server's Content-Encoding would leave the name bare."""
    root = OUT / "precomputed_codings"
    size, chunk = (16, 4, 2), (4, 4, 2)
    info(root, volume("uint16", 2, [scale("0", size, chunk, (1.5, 1.5, 3.25))], kind="image"))
    forms = [(lambda b: b, ""), (gzip_stored, ".gz"), (brotli_stored, ".br"), (zstd_stored, ".zstd")]
    # the same four again, the names bare, which is how a Content-Encoding header leaves them
    size2 = (16, 4, 2)
    for (lo, hi, _), (code, suffix) in zip(boxes(size, chunk), forms, strict=True):
        write(root, f"0/{chunk_name(lo, hi)}{suffix}", code(raw_chunk(lo, hi, 2, 2)))
    root = OUT / "precomputed_codings_bare"
    info(root, volume("uint16", 2, [scale("0", size2, chunk, (1.5, 1.5, 3.25))]))
    for (lo, hi, _), (code, _) in zip(boxes(size2, chunk), forms, strict=True):
        write(root, f"0/{chunk_name(lo, hi)}", code(raw_chunk(lo, hi, 2, 2)))


def sharded_raw() -> None:
    """Shards of gzip chunks and gzip indexes, boxes of 2 × 2 × 1 chunks, with a chunk and a whole
    shard missing, and a scale of one shard (no shard bits) with raw indexes and raw chunks."""
    root = OUT / "precomputed_sharded_raw"
    size, chunk = (10, 9, 5), (2, 2, 2)
    grid = (5, 5, 3)  # 3, 3 and 2 bits: x y z x y z x y from the lowest
    sharding = {"@type": "neuroglancer_uint64_sharded_v1", "hash": "identity", "preshift_bits": 1, "minishard_bits": 1,
                "shard_bits": 6, "minishard_index_encoding": "gzip", "data_encoding": "gzip"}
    small, small_chunk = (4, 3, 2), (2, 2, 2)
    one = {"@type": "neuroglancer_uint64_sharded_v1", "hash": "identity", "preshift_bits": 2, "minishard_bits": 1,
           "shard_bits": 0}
    info(root, volume("uint8", 1, [scale("a", size, chunk, (8, 8, 8), offset=(0, 0, 4), sharding=sharding),
                                   scale("b", small, small_chunk, (16, 16, 16), sharding=one)]))
    chunks = {}
    for lo, hi, p in boxes(size, chunk):
        if p == (1, 0, 0) or (p[0] >= 4 and p[1] >= 4):  # missing: a chunk of a shard, and a whole shard
            continue
        chunks[morton(p, grid)] = raw_chunk(lo, hi)
    for number, data in shards(chunks, 1, 1, 6, True, True).items():
        write(root, f"a/{shard_name(number, 6)}", data)
    chunks = {morton(p, (2, 2, 1)): raw_chunk(lo, hi, shift=(200, 0, 0)) for lo, hi, p in boxes(small, small_chunk)}
    for number, data in shards(chunks, 2, 1, 0, False, False).items():
        write(root, f"b/{shard_name(number, 0)}", data)


def sharded_jpeg() -> None:
    """JPEG chunks in a shard: one slice clipped along z, as BANC's last are, and 3 channels."""
    root = OUT / "precomputed_sharded_jpeg"
    sharding = {"@type": "neuroglancer_uint64_sharded_v1", "hash": "identity", "preshift_bits": 1, "minishard_bits": 1,
                "shard_bits": 0, "minishard_index_encoding": "gzip", "data_encoding": "raw"}
    size, chunk = (32, 16, 3), (16, 16, 2)
    info(root, volume("uint8", 1, [scale("gray", size, chunk, (8, 8, 45), "jpeg", jpeg_quality=90, sharding=sharding)]))
    grid = (2, 1, 2)
    chunks = {morton(p, grid): (JPEG["CLIP"] if p[2] == 1 else JPEG["FULL"]) for _, _, p in boxes(size, chunk)}
    for number, data in shards(chunks, 1, 1, 0, False, True).items():
        write(root, f"gray/{shard_name(number, 0)}", data)
    root = OUT / "precomputed_jpeg_rgb"
    info(root, volume("uint8", 3, [scale("rgb", (16, 16, 2), (16, 16, 2), (8, 8, 45), "jpeg")]))
    write(root, "rgb/0-16_0-16_0-2", JPEG["RGB"])


def png() -> None:
    """uint16 and 1 channel, and clipped: the height is y × z."""
    root = OUT / "precomputed_png"
    size, chunk = (6, 4, 3), (4, 4, 2)
    info(root, volume("uint16", 1, [scale("0", size, chunk, (1, 1, 1), "png")]))
    for lo, hi, _ in boxes(size, chunk):
        w, h, d = hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]
        le = raw_chunk(lo, hi, 1, 2)
        be = b"".join(le[i + 1:i + 2] + le[i:i + 1] for i in range(0, len(le), 2))
        write(root, f"0/{chunk_name(lo, hi)}", png_stored(be, w, h * d, 1, 16))


def segmentation() -> None:
    """compressed_segmentation in uint64, labels past 2^32 and clipped, and in uint32, in a
    shard, with a mesh directory, which stays an other object."""
    root = OUT / "precomputed_segmentation"
    size, chunk, block = (6, 4, 4), (4, 4, 4), (2, 2, 2)
    label = lambda x, y, z: (((x // 2) + 2 * (y // 2) + (z // 3)) % 5) * (1 << 40) + (x // 3)  # noqa: E731
    info(root, volume("uint64", 1, [scale("0", size, chunk, (8, 8, 8), "compressed_segmentation",
                                          compressed_segmentation_block_size=list(block))],
                      kind="segmentation", mesh="mesh"))
    write(root, "mesh/info", b'{"@type": "neuroglancer_multilod_draco"}')
    write(root, "mesh/1.index", b"a mesh")
    for lo, hi, _ in boxes(size, chunk):
        labels = {(x - lo[0], y - lo[1], z - lo[2]): label(x, y, z)
                  for z in range(lo[2], hi[2]) for y in range(lo[1], hi[1]) for x in range(lo[0], hi[0])}
        shape = [hi[a] - lo[a] for a in range(3)]
        write(root, f"0/{chunk_name(lo, hi)}", compressed_segmentation(labels, shape, block, 2))
    root = OUT / "precomputed_segmentation_sharded"
    sharding = {"@type": "neuroglancer_uint64_sharded_v1", "hash": "identity", "preshift_bits": 0, "minishard_bits": 1,
                "shard_bits": 1, "minishard_index_encoding": "raw", "data_encoding": "gzip"}
    size = (8, 4, 4)
    info(root, volume("uint32", 1, [scale("0", size, chunk, (8, 8, 8), "compressed_segmentation",
                                          compressed_segmentation_block_size=list(block), sharding=sharding)],
                      kind="segmentation"))
    chunks = {}
    for lo, hi, p in boxes(size, chunk):
        labels = {(x - lo[0], y - lo[1], z - lo[2]): (x + y + z) % 3 + 7
                  for z in range(lo[2], hi[2]) for y in range(lo[1], hi[1]) for x in range(lo[0], hi[0])}
        chunks[morton(p, (2, 1, 1))] = compressed_segmentation(labels, [hi[a] - lo[a] for a in range(3)], block, 1)
    for number, data in shards(chunks, 0, 1, 1, True, False).items():
        write(root, f"0/{shard_name(number, 1)}", data)


def float32() -> None:
    """float32, whose fill value is the number 0, with no window: a type with no range."""
    root = OUT / "precomputed_float32"
    size, chunk = (4, 2, 2), (2, 2, 2)
    info(root, volume("float32", 1, [scale("0", size, chunk, (1, 1, 1))]))
    for lo, hi, _ in boxes(size, chunk):
        body = b"".join(struct.pack("<f", value(x, y, z) / 2)
                        for z in range(lo[2], hi[2]) for y in range(lo[1], hi[1]) for x in range(lo[0], hi[0]))
        write(root, f"0/{chunk_name(lo, hi)}", body)


def not_presented() -> None:
    """Scales the layout cannot present, each with an object, and other objects beside a scale
    that it can: names that are not chunks, a chunk of another chunk size, an empty object, a
    chunk given twice."""
    root = OUT / "precomputed_not_presented"
    chunk = (2, 2, 2)
    size = (4, 2, 2)
    base = {"@type": "neuroglancer_uint64_sharded_v1", "preshift_bits": 0, "minishard_bits": 0, "shard_bits": 1}
    good = scale("good", size, chunk, (2, 2, 2))
    good["chunk_sizes"].append([1, 1, 1])
    scales = [
        good,
        scale("fp", size, chunk, (4, 4, 4), "fpzip"),
        scale("murmur", size, chunk, (8, 8, 8), sharding={**base, "hash": "murmurhash3_x86_128"}),
        scale("wrap", (8, 2, 2), chunk, (16, 16, 16), sharding={**base, "shard_bits": 0, "hash": "identity"}),
        scale("../up", size, chunk, (32, 32, 32)),
        scale("good", size, chunk, (64, 64, 64)),
        {"key": "broken"},
        "not an object",
    ]
    info(root, volume("uint8", 1, scales))
    for lo, hi, _ in boxes(size, chunk):
        write(root, f"good/{chunk_name(lo, hi)}", raw_chunk(lo, hi))
    write(root, "good/0-2_0-2_0-2.gz", gzip_stored(raw_chunk((0, 0, 0), (2, 2, 2))))
    write(root, "good/0-1_0-1_0-1", b"\0")  # of the other chunk size: other
    write(root, "good/1-3_0-2_0-2", b"\0" * 8)  # off the grid: other
    write(root, "good/0-3_2-4_0-2", b"\0" * 8)  # not the chunk's bounds: other
    write(root, "good/00-2_0-2_0-2", b"\0" * 8)  # not canonical: other
    write(root, "good/README", b"not a chunk")
    (root / "good" / "2-4_0-2_0-2").write_bytes(b"")  # empty: reads as the fill value
    write(root, "fp/0-2_0-2_0-2", b"fpzip")
    write(root, "murmur/0.shard", b"a shard")
    write(root, "wrap/0.shard", b"a shard")
    write(root, "broken/x", b"x")


def rejected() -> None:
    ok = volume("uint8", 1, [scale("0", (2, 2, 2), (2, 2, 2), (1, 1, 1))])
    cases = {
        "precomputed_reject_not_object": b"[1, 2]",
        "precomputed_reject_not_json": b"{not json",
        "precomputed_reject_empty": b"",
        "precomputed_reject_skeletons": json.dumps({"@type": "neuroglancer_skeletons"}).encode(),
        "precomputed_reject_data_type": json.dumps({**ok, "data_type": "bool"}).encode(),
        "precomputed_reject_num_channels": json.dumps({**ok, "num_channels": 0}).encode(),
        "precomputed_reject_type": json.dumps({**ok, "type": 3}).encode(),
        "precomputed_reject_no_scales": json.dumps({k: v for k, v in ok.items() if k != "scales"}).encode(),
        "precomputed_reject_empty_scales": json.dumps({**ok, "scales": []}).encode(),
    }
    for name, data in cases.items():
        write(OUT / name, "info", data)


def main() -> None:
    shutil.rmtree(OUT, ignore_errors=True)
    raw_clipped()
    codings()
    sharded_raw()
    sharded_jpeg()
    png()
    segmentation()
    float32()
    not_presented()
    rejected()


if __name__ == "__main__":
    main()
