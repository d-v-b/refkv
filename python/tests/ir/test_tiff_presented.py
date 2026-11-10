"""The TIFF and QPTIFF profiles' arrays against the stored arrays' descriptions (spec/stored-array.md
§7 <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): what the profile
writes of an image's storage, read off the image's description by the profile's rules
(spec/zarrify/tiff.md §4.3
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#43-data-types-and-codecs> to §4.5
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips> and §7.3
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#73-chunks>), is what the profile wrote.

A level's array has no address of an IFD, so a plane's description is found by its chunks' bytes:
the one whose chunk map's rows hold every range of the source the plane's chunks name. Of that
description the test derives the level's shape along `y`, `x` and the samples, its chunk shape, its
codecs, and each chunk: the key a row's coordinates give, and the bytes (a tile's or a strip's
range; the strips a chunk joins; a short last chunk's zeros, or its LZW stream cut and continued; a
JPEG tile with its tables spliced in). A sharded level is compared by its inner chunks, each cut from
its shard by the shard's index.

Three things are the profile's reading, and taken from its output: which image is which plane and
level (OME-XML, the IFD chain and SubIFDs say, which no one description holds), how many strips a
chunk joins, and how many a shard gathers."""

import json
import math
import struct
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import imagecodecs
import numpy as np
import pytest
import tifffile
from described import Described, described
from ir_helpers import FIXTURES

from refkv._narrow import JSON, at, integer, mapping, sequence, string
from refkv.ir.stored import Codec, Each, ElementPlace, Source, StoredArray, Value
from refkv.zarrify.common import Output

# files the TIFF profile rejects, whose names do not say so
REJECTED = {"mm_split_MMStack_Pos0_1.ome.tif", "unsupported_predictor_2_float.tif"}
FILES = sorted(
    p for d in ("tiff", "qptiff") for p in (FIXTURES / d).iterdir()
    if p.is_file() and "reject" not in p.name and p.name not in REJECTED
)
# the Zarr codec the profile writes for each codec the IR names (spec/zarrify/tiff.md §4.3
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#43-data-types-and-codecs>): those that
# restore the stored bytes, and those that make the elements, which stand in the place of `bytes`
RESTORING: dict[str, JSON] = {
    "tiff_lzw": {"name": "refkv.tiff_lzw"},
    "zlib": {"name": "numcodecs.zlib", "configuration": {"level": 1}},
    "zstd": {"name": "zstd", "configuration": {"level": 0, "checksum": False}},
    "xz": {"name": "imagecodecs_lzma"},
    "packbits": {"name": "imagecodecs_packbits"},
}
MAKING = {"jpeg": "imagecodecs_jpeg", "jpeg2k": "imagecodecs_jpeg2k", "jpegxl": "imagecodecs_jpegxl",
          "lerc": "imagecodecs_lerc", "ccitt_t6": "imagecodecs_ccittfax4"}
# the chains whose short last strip the profile pads to the chunk: uncompressed, and LZW
PADDED = ((), ("tiff_lzw",), ("tiff_predictor", "tiff_lzw"))
ABSENT = 2**64 - 1

type Range = tuple[str | None, int, int]
"""(object key, offset, length): bytes of the source; the key None in a file's IR"""
type Piece = Range | bytes
"""a part of a reference: a range of the source, or bytes no source holds (a literal, or a range of
a data source)"""


def pieces(d: Described, parts: list) -> list[Piece]:
    """A reference's parts: its ranges of the source, and the bytes of each other part."""
    out = d.out
    data = {i: b for b, i in out.data.items()} if isinstance(out, Output) else dict(enumerate(out.data, 1))
    got: list[Piece] = []
    for p in parts:
        if isinstance(p, bytes):
            got.append(p)
        elif p[0] == "object":
            got.append((p[1], p[2], p[3]))
        elif len(p) == 2 or p[0] == 0:
            got.append((None, p[-2], p[-1]))
        else:
            got.append(data[p[0]][p[1]:p[1] + p[2]])
    return got


def size(p: Piece) -> int:
    return len(p) if isinstance(p, bytes) else p[2]


def cut(parts: list[Piece], start: int, length: int) -> list[Piece]:
    """The `length` bytes from `start` of the bytes `parts` join to."""
    out: list[Piece] = []
    for p in parts:
        n = size(p)
        if start < n and length > 0:
            take = min(n - start, length)
            out.append(p[start:start + take] if isinstance(p, bytes) else (p[0], p[1] + start, take))
            length -= take
        start = max(start - n, 0)
    return out


def joined(parts: Sequence[Piece]) -> list[Piece]:
    """`parts` with each range that starts where the range before it ends made one with it."""
    out: list[Piece] = []
    for p in parts:
        last = out[-1] if out else None
        if isinstance(p, tuple) and isinstance(last, tuple) and last[0] == p[0] and last[1] + last[2] == p[1]:
            out[-1] = (p[0], last[1], last[2] + p[2])
        else:
            out.append(p)
    return out


def lengths(along: JSON) -> list[int] | int:
    """A rectilinear grid's chunk lengths along one axis: one length for every chunk, or each
    chunk's, a run `[length, count]` written out."""
    if isinstance(along, int):
        return along
    out: list[int] = []
    for x in sequence(along):
        out += [integer(x)] if isinstance(x, int) else [integer(at(x, 0))] * integer(at(x, 1))
    return out


@dataclass(frozen=True)
class Level:
    """An array the profile wrote: its path, its axes with its shape and chunk shape along them
    (a sharded array's inner chunk's), the chunk lengths along `y` where they differ (a
    `rectilinear` grid), its codecs (a sharded array's inner ones), how many inner chunks a shard
    holds along each axis (None where it has no shards), and its chunks by coordinates (a sharded
    array's inner ones)."""

    path: str
    data_type: str
    axes: tuple[str, ...]
    shape: tuple[int, ...]
    chunk: tuple[int, ...]
    ys: tuple[int, ...] | None
    codecs: tuple[JSON, ...]
    shard: tuple[int, ...] | None
    chunks: dict[tuple[int, ...], list[Piece]]

    def along(self, axis: str, of: tuple[int, ...]) -> int:
        return of[self.axes.index(axis)] if axis in self.axes else 1


def inner_chunks(at_shard: tuple[int, ...], held: tuple[int, ...],
                 parts: list[Piece]) -> dict[tuple[int, ...], list[Piece]]:
    """The inner chunks of the shard at `at_shard`, of `held` inner chunks along each axis, by their
    coordinates in the array (spec/zarrify.md §2.1
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#21-virtual-shards>): each the bytes the
    shard's index gives it, the index being the shard's last part, and the inner chunks its bytes
    before that, one after another in the index's order."""
    index, body = parts[-1], parts[:-1]
    count = math.prod(held)
    assert isinstance(index, bytes), "a shard's last part is its index"
    assert len(index) == 16 * count + 4
    entries = struct.unpack(f"<{2 * count}Q", index[:-4])
    out, end = {}, 0
    for i, within in enumerate(np.ndindex(*held)):
        offset, length = entries[2 * i:2 * i + 2]
        if (offset, length) == (ABSENT, ABSENT):
            continue
        assert offset == end, "a shard's inner chunks follow one another"
        end = offset + length
        out[tuple(s * k + w for s, k, w in zip(at_shard, held, within, strict=True))] = cut(body, offset, length)
    assert end == sum(size(p) for p in body), "a shard holds its inner chunks and its index, nothing else"
    return out


@cache
def levels(path: Path) -> tuple[Level, ...]:
    """The arrays the profile wrote of the file at `path`, but the source metadata node's."""
    d = described(path)
    out = d.out
    entries = out.bytes_entries if isinstance(out, Output) else out.entries
    found = []
    for key, v in sorted(entries.items()):
        if not key.endswith("zarr.json") or key.startswith("refkv_source/"):
            continue
        doc = mapping(json.loads(v), key)
        if doc["node_type"] != "array":
            continue
        prefix = key[:-len("zarr.json")]
        grid, codecs = doc["chunk_grid"], sequence(doc["codecs"], key)
        ys, shard = None, None
        if at(grid, "name") == "regular":
            chunk = tuple(integer(x) for x in sequence(at(grid, "configuration", "chunk_shape")))
        else:
            assert at(grid, "name") == "rectilinear", key
            assert at(grid, "configuration", "kind") == "inline", key
            given = [lengths(x) for x in sequence(at(grid, "configuration", "chunk_shapes"))]
            chunk = tuple(x if isinstance(x, int) else x[0] for x in given)
            axes = [string(x) for x in sequence(doc["dimension_names"])]
            (irregular,) = [i for i, x in enumerate(given) if not isinstance(x, int)]
            assert axes[irregular] == "y", "the short last strip is along y"
            along_y = given[irregular]
            assert isinstance(along_y, list)
            ys = tuple(along_y)
        if at(codecs[0], "name") == "sharding_indexed":
            assert len(codecs) == 1, key
            inner = tuple(integer(x) for x in sequence(at(codecs[0], "configuration", "chunk_shape")))
            assert all(c % i == 0 for c, i in zip(chunk, inner, strict=True)), key
            shard, chunk = tuple(c // i for c, i in zip(chunk, inner, strict=True)), inner
            codecs = sequence(at(codecs[0], "configuration", "codecs"))
        chunks: dict[tuple[int, ...], list[Piece]] = {}
        for k, parts in out.refs.items():
            if not k.startswith(f"{prefix}c/"):
                continue
            coords = tuple(int(x) for x in k[len(prefix) + 2:].split("/"))
            chunks |= {coords: pieces(d, parts)} if shard is None else inner_chunks(coords, shard, pieces(d, parts))
        found.append(Level(
            prefix.rstrip("/"), string(doc["data_type"]), tuple(string(x) for x in sequence(doc["dimension_names"])),
            tuple(integer(x) for x in sequence(doc["shape"])), chunk, ys, tuple(codecs), shard, chunks))
    return tuple(found)


def field(d: Described, a: StoredArray, tag: int) -> bytes | None:
    """The bytes of the value of the field `tag` among an image's declarations."""
    for p in a.declarations:
        if isinstance(p, ElementPlace) and d.ir.name(d.ir.parent(p.element)) == f"tags/{tag}":
            return d.place(p)
    return None


def chain_of(d: Described, a: StoredArray) -> tuple[Codec, ...]:
    (k,) = a.chains
    return tuple(d.stored.chains[k])


def codecs_of(d: Described, a: StoredArray, lv: Level, interleaved: bool) -> list[JSON]:
    """The codecs spec/zarrify/tiff.md §4.3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#43-data-types-and-codecs> gives
    an image, from its description: `transpose` for interleaved samples; what stores the elements
    (`bytes`, or `imagecodecs_packints` for samples that are not whole bytes), which a predictor
    precedes or replaces and a codec that makes the elements replaces; then the codecs that restore
    the stored bytes."""
    order = {b"II": "<", b"MM": ">"}[d.place(a.declarations[0])]
    chain = chain_of(d, a)
    assert a.item_bits is not None
    assert a.shape is not None
    samples = a.shape[-1] if interleaved else 1
    rows, columns = lv.along("y", lv.chunk), lv.along("x", lv.chunk)
    out: list[JSON] = []
    if interleaved:
        c = lv.axes.index("c")
        out.append({"name": "transpose", "configuration": {"order": [i for i in range(len(lv.axes)) if i != c] + [c]}})
    # `x` in stored order: the last axis, or the one before the samples
    x = len(lv.axes) - (2 if interleaved else 1)
    if a.item_bits % 8:
        stored: JSON = {"name": "imagecodecs_packints",
                        "configuration": {"bitspersample": a.item_bits, "runlen": columns * samples}}
    elif a.item_bits == 8:
        stored = {"name": "bytes"}
    else:
        stored = {"name": "bytes", "configuration": {"endian": "little" if order == "<" else "big"}}
    first = chain[0] if chain else None
    rest = chain[1:]
    if first is None:
        out.append(stored)
    elif first.name == "tiff_predictor":
        value = first.parameters["predictor"]
        assert isinstance(value, Source)
        (predictor,) = struct.unpack(order + "H", d.place(value.place))
        differenced: JSON = {"axis": x, "dist": 1}
        if predictor == 2:
            out += [{"name": "imagecodecs_delta", "configuration": differenced}, stored]
        else:
            out.append({"name": "imagecodecs_floatpred", "configuration": differenced})
    elif first.name in MAKING:
        out.append({"name": MAKING[first.name]})
        # a LERC stream in Deflate or zstd is one the codec finds without being told
        rest = () if first.name == "lerc" else rest
    elif first.name == "webp":
        out.append({"name": "imagecodecs_webp", "configuration": {"hasalpha": samples == 4}})
    elif first.name == "eer":
        # a value, or a field's one SHORT; EER's defaults where the file has no field
        bits = {"skip_bits": 7, "horizontal_bits": 2, "vertical_bits": 2}
        bits |= {k: integer(v.value) for k, v in first.parameters.items() if isinstance(v, Value)}
        bits |= {k: struct.unpack(order + "H", d.place(v.place))[0] for k, v in first.parameters.items()
                 if isinstance(v, Source)}
        out.append({"name": "imagecodecs_eer", "configuration": {
            "shape": [rows, columns], "skipbits": bits["skip_bits"], "horzbits": bits["horizontal_bits"],
            "vertbits": bits["vertical_bits"]}})
    else:
        out.append(stored)
        rest = chain
    return out + [RESTORING.get(c.name, {"name": c.name}) for c in rest]


def jpeg_prefix(d: Described, a: StoredArray, samples: int) -> bytes:
    """The bytes the profile splices into a JPEG tile after its first 2 (spec/zarrify/tiff.md §7.3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#73-chunks>): for 3 samples, the
    Adobe marker of the color transform PhotometricInterpretation says; then the image's
    JPEGTables without their first and last 2 bytes."""
    order = {b"II": "<", b"MM": ">"}[d.place(a.declarations[0])]
    out = b""
    if samples == 3:
        photometric = field(d, a, 262)
        assert photometric is not None
        ycbcr = struct.unpack(order + "H", photometric)[0] == 6
        out += bytes.fromhex("ffee000e41646f6265006400000000") + bytes([ycbcr])
    tables = chain_of(d, a)[0].parameters.get("tables")
    if tables is not None:
        assert isinstance(tables, Source)
        out += d.place(tables.place)[2:-2]
    return out


def check_plane(d: Described, lv: Level, k: int, chunks: dict[tuple[int, int, int], list[Piece]],
                seen: Counter) -> list[str]:
    """One plane's chunks, by the sample plane, the row and the column of each, against the
    description `k`."""
    a = d.stored.arrays[k]
    if a.grid is None or a.shape is None or a.item_bits is None or not all(isinstance(x, Each) for x in a.grid.axes):
        return ["a description of no grid"]
    grid = tuple(x.length for x in a.grid.axes if isinstance(x, Each))
    h, w, channels = (lv.along(x, lv.shape) for x in "yxc")
    interleaved = at(lv.codecs[0], "name") == "transpose"
    if len(a.shape) == 2:
        shape, per_chunk, (rows, columns), planes = (h, w), 1, grid, 1
    elif interleaved:
        shape, per_chunk, (rows, columns), planes = (h, w, channels), channels, grid[:2], 1
        if grid[2] != channels:
            return [f"grid {a.grid}: a unit does not hold a pixel's {channels} samples"]
    else:
        shape, per_chunk, (rows, columns), planes = (channels, h, w), 1, grid[1:], channels
        if grid[0] != 1:
            return [f"grid {a.grid}: a unit holds more than one sample plane"]
    if a.shape != shape:
        return [f"shape {a.shape}; the profile's {lv.shape} along {lv.axes}"]
    said = []
    # packed samples of 1 and 12 bits are read into elements of 1 and 2 bytes
    if np.dtype(lv.data_type).itemsize != math.ceil(a.item_bits / 8):
        said.append(f"item_bits {a.item_bits}; the profile's data type {lv.data_type}")
    names = tuple(c.name for c in chain_of(d, a))
    strips = math.ceil(h / rows)
    chunk_rows, chunk_columns = lv.along("y", lv.chunk), lv.along("x", lv.chunk)
    # how many strips a chunk joins, which is the profile's choice
    join = math.ceil(chunk_rows / rows)
    short = a.grid.edge == "short" and strips > 1 and h % rows != 0
    if a.grid.edge == "full":
        fits = lv.ys is None and (chunk_rows, chunk_columns) == (rows, columns)
    elif lv.ys is not None:
        fits = short and names not in PADDED and lv.ys == (*[rows] * (strips - 1), h - (strips - 1) * rows)
        fits = fits and chunk_columns == columns == w
    else:
        fits = chunk_columns == columns == w and chunk_rows == min(join * rows, h) and (join == 1 or names == ())
        fits = fits and not (short and names not in PADDED)
    want_chunk = tuple({"y": chunk_rows, "x": chunk_columns, "c": per_chunk}.get(x, 1) for x in lv.axes)
    if not fits or lv.chunk != want_chunk:
        return [*said, f"grid {a.grid} of {names}; the profile's chunks {lv.chunk} along {lv.axes}, {lv.ys} along y"]
    if list(lv.codecs) != codecs_of(d, a, lv, interleaved):
        said.append(f"chain {names}; the profile's codecs {list(lv.codecs)}")
    seen["planes in tiles" if a.grid.edge == "full" else "planes in strips"] += 1
    seen["planes of several sample planes"] += planes > 1
    # each chunk's rows: a tile's or a strip's, or those of the strips it joins
    units: dict[tuple[int, int, int], list[tuple[int, dict]]] = {}
    for r in sorted(d.rows.get(k, []), key=lambda r: r["coords"]):
        # a row's sample plane, and its unit's row and column
        sample, row, column = (0, *r["coords"][:2]) if planes == 1 else r["coords"]
        units.setdefault((sample, row // join, column), []).append((row, r))
    if set(units) != set(chunks):
        alone = sorted(set(units) - set(chunks)), sorted(set(chunks) - set(units))
        said.append(f"rows at {alone[0]} and chunks at {alone[1]} alone")
    # the bytes of a chunk's row, which starts on a whole byte, and of a chunk
    row_bytes = math.ceil(chunk_columns * per_chunk * a.item_bits / 8)
    last = math.ceil(h / chunk_rows) - 1
    prefix = jpeg_prefix(d, a, per_chunk) if names[:1] == ("jpeg",) else b""
    for at_chunk, held in sorted(units.items()):
        got = chunks.get(at_chunk)
        if got is None:
            continue
        seen["chunks"] += 1
        stored = [p for _, r in held for p in d.parts(r)]
        y = at_chunk[1]
        if join > 1:
            seen["chunks of joined strips"] += 1
            if [row for row, _ in held] != list(range(y * join, min((y + 1) * join, strips))):
                said.append(f"chunk {at_chunk} does not hold each of its strips")
        elif len(held) != 1:
            said.append(f"chunk {at_chunk} holds {len(held)} units")
            continue
        total = sum(n for _, _, n in stored)
        full = chunk_rows * row_bytes
        padded = a.grid.edge == "short" and lv.ys is None and y == last and last > 0 and h % chunk_rows != 0
        if prefix:
            # every byte of the tile is referenced, none replaced, the prefix after its first 2
            seen["chunks with a JPEG prefix"] += 1
            ((key, o, n),) = stored
            want: list[Piece] = [(key, o, 2), prefix, (key, o + 2, n - 2)]
            if got != want:
                said.append(f"chunk {at_chunk} is {got}, not its tile at {stored} with {prefix!r} spliced in")
        elif padded and names == () and total < full:
            seen["chunks padded with zeros"] += 1
            if joined(got) != [*joined(stored), bytes(full - total)]:
                said.append(f"chunk {at_chunk} is not {stored} and {full - total} zeros")
        elif padded and names[-1:] == ("tiff_lzw",):
            # the strip's stream up to a cut, then a tail that continues it with the rows missing as zeros
            seen["chunks of a cut LZW stream"] += 1
            ((key, o, n),) = stored
            data = d.objects[key]
            cut_at, tail = (got[0], got[1]) if len(got) == 2 else (None, None)
            if not (isinstance(cut_at, tuple) and isinstance(tail, bytes)) or cut_at[:2] != (key, o) or cut_at[2] > n:
                said.append(f"chunk {at_chunk} is {got}, not a prefix of its strip at {stored} and a literal tail")
                continue
            rows_held = (h - y * chunk_rows) * row_bytes
            theirs = bytes(imagecodecs.lzw_decode(data[o:o + n]))[:rows_held] + bytes(full - rows_held)
            if bytes(imagecodecs.lzw_decode(data[o:o + cut_at[2]] + tail)) != theirs:
                said.append(f"chunk {at_chunk}'s stream does not decode to its strip's rows and zeros")
        elif joined(got) != joined(stored):
            said.append(f"chunk {at_chunk} is {got}, not {stored}")
    return said


def check_level(d: Described, lv: Level, by_start: dict[tuple[str | None, int], set[int]], presented: set[int],
                seen: Counter) -> list[str]:
    """One array against the descriptions: each of its planes against the description whose rows
    hold its chunks' bytes."""
    seen["arrays"] += 1
    seen["arrays sharded"] += lv.shard is not None
    seen["arrays of a rectilinear grid"] += lv.ys is not None
    seen["arrays across files"] += None not in d.objects
    if lv.shard is not None:
        seen["inner chunks of shards"] += len(lv.chunks)
    if not lv.chunks:
        seen["arrays of no chunks"] += 1
        return []

    def candidates(parts: list[Piece]) -> set[int]:
        """The descriptions with a row at the first range of the source among `parts`."""
        first = next((p for p in parts if isinstance(p, tuple)), None)
        return set() if first is None else by_start.get(first[:2], set())

    # a level's images have one format, so any of them says whether a pixel has several samples,
    # which are then the level's channels: a plane's chunks differ along `c` by the sample plane
    some = sorted(k for parts in lv.chunks.values() for k in candidates(parts))
    if not some:
        # an image the descriptions give no grid, which the profile presents all the same: one with a
        # strip of no bytes at an offset, which readers disagree on and the profile takes as absent
        h, w = lv.along("y", lv.shape), lv.along("x", lv.shape)
        if any(a.grid is None and a.shape == (h, w) for a in d.stored.arrays):
            seen["arrays of an image the descriptions give no grid"] += 1
            return []
        return [f"{lv.path}: no chunk starts where a description's row does"]
    shape = d.stored.arrays[some[0]].shape
    c = lv.axes.index("c") if shape is not None and len(shape) == 3 else None
    planes: dict[tuple[int, ...], dict[tuple[int, int, int], list[Piece]]] = {}
    for coords, parts in lv.chunks.items():
        lead = coords[:-2]
        plane = lead if c is None else (*lead[:c], *lead[c + 1:])
        planes.setdefault(plane, {})[(0 if c is None else lead[c], coords[-2], coords[-1])] = parts
    said = []
    for plane, chunks in sorted(planes.items()):
        seen["planes"] += 1
        found = sorted(set.intersection(*(candidates(parts) for parts in chunks.values())))
        if not found:
            said.append(f"{lv.path} plane {plane}: no one description's rows hold its chunks")
            continue
        # images that share their bytes are told apart by nothing a chunk holds
        seen["planes of bytes several descriptions share"] += len(found) > 1
        complaints = []
        for k in found:
            counted: Counter = Counter()
            complaints = check_plane(d, lv, k, chunks, counted)
            if not complaints:
                seen.update(counted)
                presented.add(k)
                break
        said += [f"{lv.path} plane {plane}, {d.stored.arrays[found[-1]].path}: {s}" for s in complaints]
    return said


@cache
def compared(path: Path) -> tuple[tuple[str, ...], Counter]:
    """What differs between the arrays the profile wrote of the file at `path` and the file's
    descriptions, and what the comparison saw."""
    d = described(path)
    said: list[str] = []
    seen: Counter = Counter()
    if d.stored.undescribed:
        said.append(f"descriptions dropped for room: {d.stored.undescribed}")
    by_start: dict[tuple[str | None, int], set[int]] = {}
    for k, rows in d.rows.items():
        for r in rows:
            for key, offset, _ in d.parts(r):
                by_start.setdefault((key, offset), set()).add(k)
    presented: set[int] = set()
    for lv in levels(path):
        said += check_level(d, lv, by_start, presented, seen)
    seen["descriptions the profile writes no array of"] += len(d.stored.arrays) - len(presented)
    return tuple(said), seen


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_the_tiff_profile_writes_what_the_descriptions_give(path):
    """Every array the profile writes of the file is what its planes' descriptions give."""
    said, seen = compared(path)
    assert said == ()
    assert seen["arrays"] >= 1


def test_the_fixtures_hold_every_case_the_profile_is_compared_in():
    """The fixtures reach each case of the comparison, so that none of it passes for want of a
    file. The one image the descriptions give no grid is edge_strips_absent.tif's, whose strip of
    no bytes at an offset the profile takes as absent."""
    seen: Counter = Counter()
    for path in FILES:
        seen += compared(path)[1]
    assert {k: v for k, v in seen.items() if v} == {
        "arrays": 158,
        "arrays sharded": 18,
        "arrays of a rectilinear grid": 16,
        "arrays across files": 5,
        "arrays of an image the descriptions give no grid": 1,
        "planes": 281,
        "planes in tiles": 139,
        "planes in strips": 142,
        "planes of several sample planes": 7,
        "chunks": 1596,
        "inner chunks of shards": 646,
        "chunks of joined strips": 13,
        "chunks padded with zeros": 1,
        "chunks of a cut LZW stream": 27,
        "chunks with a JPEG prefix": 45,
        "descriptions the profile writes no array of": 24,
    }


def test_images_no_fixture_holds_are_written_as_their_descriptions_give(tmp_path):
    """Layouts the fixtures do not have, each a file written here: uncompressed strips whose short
    last chunk is padded with zeros, joined (a row a strip, two strips a chunk) and not (a strip
    too large to join); more LZW strips than one shard gathers, so that an inner chunk's place
    depends on its shard's; and two images of the same tiles, either of whose descriptions the one
    plane's chunks are."""
    rng = np.random.default_rng(0)

    def image(shape: tuple[int, ...]) -> np.ndarray:
        return rng.integers(0, 200, shape).astype("u1")

    joined_strips = tmp_path / "joined.tif"
    tifffile.imwrite(joined_strips, image((5, 2**19)), rowsperstrip=1, metadata=None)
    apart = tmp_path / "apart.tif"
    tifffile.imwrite(apart, image((5, 2**19 + 8)), rowsperstrip=2, metadata=None)
    shards = tmp_path / "shards.tif"
    tifffile.imwrite(shards, image((2050, 8)), rowsperstrip=1, compression="lzw", metadata=None)
    shared = tmp_path / "shared.tif"
    with tifffile.TiffWriter(shared) as w:
        for _ in range(2):
            w.write(np.arange(64 * 64, dtype="u1").reshape(64, 64), tile=(32, 32), metadata=None)
    data = bytearray(shared.read_bytes())
    with tifffile.TiffFile(shared) as tif:
        pages = [p for p in tif.pages if isinstance(p, tifffile.TiffPage)]
        first, second = (p.tags[324] for p in pages)
        assert (first.count, first.dtype, second.count, second.dtype) == (4, 4, 4, 4)
        data[second.valueoffset:second.valueoffset + 16] = data[first.valueoffset:first.valueoffset + 16]
    shared.write_bytes(data)
    cases = {
        joined_strips: {"planes in strips": 1, "chunks": 3, "chunks of joined strips": 3,
                        "chunks padded with zeros": 1},
        apart: {"planes in strips": 1, "chunks": 3, "chunks padded with zeros": 1},
        shards: {"planes in strips": 1, "chunks": 2050, "arrays sharded": 1, "inner chunks of shards": 2050},
        shared: {"planes in tiles": 1, "chunks": 4, "planes of bytes several descriptions share": 1,
                 "descriptions the profile writes no array of": 1},
    }
    for path, want in cases.items():
        said, seen = compared(path)
        assert said == (), path.name
        assert {k: v for k, v in seen.items() if v} == {"arrays": 1, "planes": 1, **want}, path.name
