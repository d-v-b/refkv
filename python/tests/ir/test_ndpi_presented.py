"""The NDPI profile's arrays against the stored arrays' descriptions (spec/stored-array.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): what the profile
writes of an image's storage, read off the image's description by the profile's rules
(spec/zarrify/ndpi.md §7 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#7-ndpi-profile>),
is what the profile wrote.

A plane's description is found by its chunks' bytes: the one whose chunk map's rows hold the first
range of the source its chunks name. Of that description the test derives the level's shape,
its chunk shape, its codecs and every chunk:

- of a strip McuStarts cuts, a whole JPEG stream the profile builds: the strip's header, which
  the description's codec gives a place for, with the chunk's size in its frame header; the
  chunk's restart intervals, each a row's bytes, the last row and column repeated past the
  image's edge; a restart marker between them; and an EOI;
- of a strip read whole, the strip, with the image's size written over the frame's where the
  two differ;
- of a JPEG XR image, each tile or the strip, as it is.

Two things are the profile's reading, and taken from its output: which image is which plane and
level, and how many rows of intervals a chunk holds."""

import json
import math
import struct
from collections import Counter
from functools import cache
from pathlib import Path

import numpy as np
import pytest
from described import Described, described
from ir_helpers import FIXTURES

from refkv._narrow import JSON, at, integer, mapping, sequence, string
from refkv.ir.stored import Each, Source, StoredArray
from refkv.zarrify.common import Output

FILES = sorted(p for p in (FIXTURES / "ndpi").iterdir() if p.suffix == ".ndpi" and "reject" not in p.name)
SOF0, EOI = b"\xff\xc0", b"\xff\xd9"
# the widest a chunk of intervals is, in pixels, where an interval is no wider
TARGET = 1024

type Piece = tuple[int, int] | bytes
"""a part of a reference: (offset, length) of the file, or bytes the file does not hold there (a
literal, or a range of a data source)"""
type Chunks = dict[tuple[int, int], list[Piece]]
"""a plane's chunks, by the row and column of each"""


def pieces(out: Output, parts: list) -> list[Piece]:
    """A reference's parts, those that are no range of the file joined where they meet."""
    data = {i: b for b, i in out.data.items()}
    got: list[Piece] = []
    for p in parts:
        if isinstance(p, bytes):
            piece: Piece = p
        elif len(p) == 2 or p[0] == 0:
            piece = (p[-2], p[-1])
        else:
            piece = data[p[0]][p[1]:p[1] + p[2]]
        last = got[-1] if got else None
        if isinstance(piece, bytes) and isinstance(last, bytes):
            got[-1] = last + piece
        else:
            got.append(piece)
    return got


def frame_at(stream: bytes) -> int:
    """Where the height of a JPEG stream's frame header (its first SOF0 segment) is, the width
    after it: past the segment's marker, its length and the precision."""
    position = 2
    while stream[position:position + 2] != SOF0:
        assert stream[position] == 0xFF, "a marker"
        position += 2 + struct.unpack_from(">H", stream, position + 2)[0]
    return position + 5


def lengths(a: StoredArray) -> tuple[int, ...]:
    assert a.grid is not None
    assert all(isinstance(x, Each) for x in a.grid.axes)
    return tuple(x.length for x in a.grid.axes if isinstance(x, Each))


def interval_chunks(d: Described, k: int, chunk: tuple[int, int], seen: Counter) -> Chunks | str:
    """The chunks of a strip McuStarts cuts (the profile's `NdpiMcuChunk`), of `chunk` rows and
    columns of pixels, from its description; or why the description gives none of that size."""
    a = d.stored.arrays[k]
    assert a.shape is not None
    (codec,) = d.stored.chains[a.chains[0]]
    header = codec.parameters["header"]
    assert isinstance(header, Source)
    stream = d.place(header.place)
    down, across = lengths(a)[:2]
    rows, columns = math.ceil(a.shape[0] / down), math.ceil(a.shape[1] / across)
    # intervals down a chunk, which the profile chooses, and across it, which the convention gives
    b, wide = chunk[0] // down, min(columns, max(1, TARGET // across))
    if chunk != (b * down, wide * across) or b < 1:
        return f"chunks of {chunk} are not whole intervals of {down} by {across}, {wide} across"
    units = {tuple(r["coords"][:2]): r for r in d.rows.get(k, [])}
    height = frame_at(stream)
    sized = stream[:height] + struct.pack(">HH", *chunk) + stream[height + 4:]
    out: Chunks = {}
    for u in range(math.ceil(rows / b)):
        for v in range(math.ceil(columns / wide)):
            parts: list[Piece] = [sized]
            for t in range(b * wide):
                if t:
                    parts.append(bytes([0xFF, 0xD0 + (t - 1) % 8]))
                # past the image's edge, the last row and column of intervals again
                unit = units[(min(u * b + t // wide, rows - 1), min(v * wide + t % wide, columns - 1))]
                ((_, offset, length),) = d.parts(unit)
                parts.append((offset, length))
            out[(u, v)] = [*parts, EOI]
            seen["chunks built of intervals"] += 1
            seen["chunks that repeat an interval"] += (u + 1) * b > rows or (v + 1) * wide > columns
    return out


def unit_chunks(d: Described, k: int, chunk: tuple[int, int], name: str, seen: Counter) -> Chunks | str:
    """The chunks of an image in tiles or strips, one a unit, from its description; or why the
    description gives none of that size."""
    a = d.stored.arrays[k]
    assert a.shape is not None
    if chunk != lengths(a)[:2]:
        return f"chunks of {chunk}, units of {lengths(a)[:2]}"
    out: Chunks = {}
    for row in d.rows.get(k, []):
        ((_, offset, length),) = d.parts(row)
        parts: list[Piece] = [(offset, length)]
        if name == "jpeg":
            # a frame of another size than the image's is given the image's
            # (spec/zarrify/ndpi.md §4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#4-the-image>)
            strip = d.objects[None][offset:offset + length]
            height = frame_at(strip)
            if struct.unpack_from(">HH", strip, height) != a.shape[:2]:
                seen["chunks of a frame given the image's size"] += 1
                size = struct.pack(">HH", *a.shape[:2])
                parts = [(offset, height), size, (offset + height + 4, length - height - 4)]
        out[tuple(row["coords"][:2])] = parts
        seen["chunks of a tile or a strip"] += 1
    return out


def check_plane(d: Described, k: int, doc: dict[str, JSON], chunks: Chunks, seen: Counter) -> list[str]:
    """One plane's chunks, by the row and column of each, and its array's document against the
    description `k`."""
    a = d.stored.arrays[k]
    if a.grid is None or a.shape is None or a.item_bits is None:
        return ["a description of no grid"]
    names = [c.name for chain in a.chains for c in d.stored.chains[chain]]
    axes = [string(x) for x in sequence(doc["dimension_names"])]
    shape = [integer(x) for x in sequence(doc["shape"])]
    chunk = [integer(x) for x in sequence(at(doc["chunk_grid"], "configuration", "chunk_shape"))]
    samples = a.shape[2] if len(a.shape) == 3 else 1
    stacked = axes == ["c", "z", "y", "x"]
    said = []
    # the samples first, then the planes where the level has several, each chunk one plane
    if axes[:1] != ["c"] or axes[-2:] != ["y", "x"] or shape != [samples, *shape[1:-2], *a.shape[:2]]:
        said.append(f"shape {a.shape}; the profile's {shape} along {axes}")
    if chunk[:-2] != [samples, *[1] * stacked]:
        said.append(f"chunks of {chunk} along {axes}, of {samples} samples")
    codec = {"jpeg": "imagecodecs_jpeg", "jpeg_restart_interval": "imagecodecs_jpeg", "jpegxr": "imagecodecs_jpegxr"}
    if names[0] not in codec:
        return [*said, f"chain {names}, which the profile presents nothing of"]
    order: list[JSON] = [*range(1, len(axes)), 0]
    transposed: list[JSON] = [{"name": "transpose", "configuration": {"order": order}}] if samples == 3 else []
    if doc["codecs"] != [*transposed, {"name": codec[names[0]]}]:
        said.append(f"chain {names} of {samples} samples; the profile's codecs {doc['codecs']}")
    if np.dtype(string(doc["data_type"])).itemsize * 8 != a.item_bits:
        said.append(f"item_bits {a.item_bits}; the profile's data type {doc['data_type']}")
    size = (chunk[-2], chunk[-1])
    if names == ["jpeg_restart_interval"]:
        seen["planes cut into intervals"] += 1
        want = interval_chunks(d, k, size, seen)
    else:
        seen["planes of JPEG XR" if names == ["jpegxr"] else "planes of a strip read whole"] += 1
        want = unit_chunks(d, k, size, names[0], seen)
    if isinstance(want, str):
        return [*said, want]
    if set(want) != set(chunks):
        said.append(f"chunks at {sorted(set(chunks) - set(want))} alone, and none at {sorted(set(want) - set(chunks))}")
    said += [f"chunk {c} is {chunks[c][:4]}, not {parts[:4]}" for c, parts in sorted(want.items())
             if c in chunks and chunks[c] != parts]
    return said


@cache
def compared(path: Path) -> tuple[tuple[str, ...], Counter]:
    """What differs between the arrays the profile wrote of the file at `path` and the file's
    descriptions, and what the comparison saw."""
    d = described(path)
    out = d.out
    assert isinstance(out, Output), "an NDPI is one file"
    said: list[str] = []
    seen: Counter = Counter()
    if d.stored.undescribed:
        said.append("descriptions dropped for room")
    # each row's bytes in the file, with its description
    units = sorted((offset, length, k) for k, rows in d.rows.items() for r in rows for _, offset, length in d.parts(r))
    presented: set[int] = set()
    for key, v in sorted(out.bytes_entries.items()):
        if not key.endswith("zarr.json") or key.startswith("refkv_source/"):
            continue
        doc = dict(mapping(json.loads(v), key))
        if doc["node_type"] != "array":
            continue
        prefix = key[:-len("zarr.json")]
        seen["arrays"] += 1
        # a level's planes: its chunks by their coordinates between the samples' and the rows'
        planes: dict[tuple[int, ...], Chunks] = {}
        for chunk_key, parts in out.refs.items():
            if chunk_key.startswith(f"{prefix}c/"):
                coords = tuple(int(x) for x in chunk_key[len(prefix) + 2:].split("/"))
                planes.setdefault(coords[1:-2], {})[(coords[-2], coords[-1])] = pieces(out, parts)
        for plane, chunks in sorted(planes.items()):
            seen["planes"] += 1
            first = next(p for parts in chunks.values() for p in parts if isinstance(p, tuple))
            found = [k for offset, length, k in units if offset <= first[0] < offset + length]
            if len(found) != 1:
                said.append(f"{prefix}plane {plane}: its chunks' bytes are in {len(found)} descriptions' rows")
                continue
            presented.add(found[0])
            path_of = d.stored.arrays[found[0]].path
            said += [f"{prefix}plane {plane}, {path_of}: {s}" for s in check_plane(d, found[0], doc, chunks, seen)]
    seen["descriptions the profile writes no array of"] += len(d.stored.arrays) - len(presented)
    return tuple(said), seen


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_the_ndpi_profile_writes_what_the_descriptions_give(path):
    """Every array the profile writes of the file is what its planes' descriptions give."""
    said, seen = compared(path)
    assert said == ()
    assert seen["arrays"] >= 1


def test_the_fixtures_hold_every_case_the_profile_is_compared_in():
    """The fixtures reach each case of the comparison, so that none of it passes for want of a
    file. The descriptions with no array are the images the profile leaves out of its levels, the
    macro images and the slide maps."""
    seen: Counter = Counter()
    for path in FILES:
        seen += compared(path)[1]
    assert {k: v for k, v in seen.items() if v} == {
        "arrays": 24,
        "planes": 36,
        "planes cut into intervals": 10,
        "planes of a strip read whole": 14,
        "planes of JPEG XR": 12,
        "chunks built of intervals": 22,
        "chunks that repeat an interval": 12,
        "chunks of a tile or a strip": 48,
        "chunks of a frame given the image's size": 2,
        "descriptions the profile writes no array of": 14,
    }
