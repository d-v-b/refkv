"""The ND2 profile's arrays against the stored arrays' descriptions (spec/stored-array.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): what the profile
writes of a file's frames, read off their description by the profile's rules
(spec/zarrify/nd2.md §4.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nd2.md#43-output>
and §7.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nd2.md#73-frames>), is what the
profile wrote.

The frames' description is the one the facts' `declared_by` keys. Of it the test derives each
position's array document (its shape, its chunk shape, its codecs and the size of its data type)
and every chunk's parts: a frame's chunk is the frame's one unit, the rows together or the zlib
stream, and a row block's the units of its rows, one part a row, in order. A frame the description
has no row for has no chunk.

Four things are taken from elsewhere, since no description holds them:

- the loops (their kinds, their counts and whether the z loop is flipped), from the parser's
  facts: the description has their product, its first axis, and the test checks that it is; where
  a frame's number places it among the output's axes and positions is the profile's reading of
  the experiment;
- the rows of a row block, from the output: the profile chooses them, and the test checks that
  they divide the frame's rows;
- the byte order, which is the format's (little-endian, in every ND2 file), and the data type's
  name, of which the test compares the size;
- the zlib level the profile's codec names, which only an encoder needs."""

import json
import math
from collections import Counter
from functools import cache
from pathlib import Path

import numpy as np
import pytest
from described import Described, described
from ir_helpers import FIXTURES

from refkv._narrow import JSON, at, boolean, integer, mapping, sequence, string
from refkv.ir import parse
from refkv.ir.planner import FileTransport
from refkv.ir.stored import Each, StoredArray
from refkv.zarrify.common import Output

FILES = sorted(p for p in (FIXTURES / "nd2").glob("*.nd2") if "reject" not in p.name)

type Parts = list[tuple[int, int]]
"""a chunk's parts: (offset, length) of the file, in order"""


def file_place(d: Described, row: dict) -> tuple[int, int]:
    """(offset, length) in the file of a row's unit: its element's extent, a run's stride further
    for each member before its own, or its range of that."""
    start, length = d.ir.extent(row["element"])
    if row["member"] is not None:
        root = row["element"]
        while (run := d.ir._core.run(root)) is None:
            root = d.ir.parent(root)
        start += row["member"] * run[1]
    if row["range"] is not None:
        start, length = start + row["range"]["offset"], row["range"]["length"]
    return start, length


def lengths(a: StoredArray) -> tuple[int, ...]:
    assert a.grid is not None
    assert all(isinstance(x, Each) for x in a.grid.axes)
    return tuple(x.length for x in a.grid.axes if isinstance(x, Each))


def loops_of(path: Path) -> tuple[list[tuple[str, int, bool]], int]:
    """The loops the parser's facts give, each (kind, count, whether its index is flipped), and
    the element that keys the frames' description."""
    _, facts, _ = parse("nd2", FileTransport(str(path)), 4)
    loops = [(string(at(x, "kind")), integer(at(x, "count")), "flip" in mapping(x) and boolean(at(x, "flip")))
             for x in sequence(facts["loops"])]
    return loops, integer(facts["declared_by"])


def derived(d: Described, a: StoredArray, rows: list[dict], loops: list[tuple[str, int, bool]], block: int,
            seen: Counter) -> tuple[dict[str, JSON], dict[str, Parts]] | str:
    """What the profile writes of the frames' array `a`, whose rows are `rows`, with row blocks of
    `block` rows: an array document's members, and every chunk's parts by its key; or why the
    description gives none."""
    if a.grid is None or a.shape is None or a.item_bits is None:
        return "a description of no grid"
    count, height, width = a.shape[:3]
    components = a.shape[3] if len(a.shape) == 4 else 1
    counts = {kind: n for kind, n, _ in loops}
    if count != math.prod(counts.values()) or len(counts) != len(loops):
        return f"{count} frames, loops of {loops}"
    names = [c.name for c in d.stored.chains[a.chains[0]]]
    # a frame a unit, or a row a unit, which the profile joins into blocks
    apart = lengths(a)[1] == 1 and height > 1
    units = (1, 1 if apart else height, width, *[components] * (components > 1))
    if lengths(a) != units or names not in ([], ["zlib"]):
        return f"units of {lengths(a)} in the chain {names}"
    if not apart:
        block = height
    if block < 1 or height % block:
        return f"row blocks of {block} rows of {height}"
    # the axes: the time loop, the components where there are several, the z loop, the rows and
    # the columns; the components come last in a frame, and first of these in the output
    axes: list[str] = [x for x in "tczyx" if x in "yx" or (x == "c" and components > 1) or x in counts]
    size = {"t": counts.get("t", 1), "c": components, "z": counts.get("z", 1), "y": height, "x": width}
    chunk = {"t": 1, "c": components, "z": 1, "y": block, "x": width}
    stored = [x for x in axes if x != "c"] + ["c"] * (components > 1)
    codecs: list[JSON] = [{"name": "transpose", "configuration": {"order": [axes.index(x) for x in stored]}}]
    codecs = codecs[:components > 1]
    codecs.append({"name": "bytes", "configuration": {"endian": "little"}} if a.item_bits > 8 else {"name": "bytes"})
    if names == ["zlib"]:
        codecs.append({"name": "numcodecs.zlib", "configuration": {"level": 1}})
    doc: dict[str, JSON] = {"shape": [size[x] for x in axes], "chunk_shape": [chunk[x] for x in axes], "codecs": codecs,
                            "dimension_names": list(axes), "item_bits": a.item_bits}
    by_frame: dict[int, list[dict]] = {}
    for row in rows:
        by_frame.setdefault(row["coords"][0], []).append(row)
    chunks: dict[str, Parts] = {}
    for f, units in sorted(by_frame.items()):
        # the frame's coordinates: its number over the loops, the last fastest
        where, rest = {}, f
        for kind, n, flip in reversed(loops):
            where[kind] = n - 1 - rest % n if flip else rest % n
            rest //= n
            seen["frames at a flipped index"] += flip
        if len(units) != (height if apart else 1) or [u["coords"][1] for u in units] != list(range(len(units))):
            return f"frame {f} has the units {[u['coords'] for u in units][:4]}"
        for j in range(height // block):
            index = {"t": where.get("t", 0), "c": 0, "z": where.get("z", 0), "y": j, "x": 0}
            key = f"{where.get('p', 0)}/0/c/" + "/".join(str(index[x]) for x in axes)
            chunks[key] = [file_place(d, u) for u in (units[j * block:(j + 1) * block] if apart else units)]
            seen["chunks of rows apart" if apart else "chunks of a zlib stream" if names else "chunks of a frame"] += 1
    seen["files of row blocks shorter than a frame"] += apart and block < height
    return doc, chunks


def written(out: Output, prefix: str) -> tuple[dict[str, JSON], dict[str, Parts]]:
    """What the profile wrote of the array at `prefix`: the members of its document that `derived`
    gives, and its chunks' parts by their keys."""
    doc = dict(mapping(json.loads(out.bytes_entries[f"{prefix}zarr.json"])))
    members: dict[str, JSON] = {
        "shape": doc["shape"],
        "chunk_shape": at(doc["chunk_grid"], "configuration", "chunk_shape"),
        "codecs": doc["codecs"],
        "dimension_names": doc["dimension_names"],
        "item_bits": np.dtype(string(doc["data_type"])).itemsize * 8,
    }
    chunks: dict[str, Parts] = {}
    for key, parts in out.refs.items():
        if key.startswith(f"{prefix}c/"):
            assert all(isinstance(p, tuple) and len(p) == 2 for p in parts), "ranges of the file alone"
            chunks[key] = [(p[0], p[1]) for p in parts]
    return members, chunks


@cache
def compared(path: Path) -> tuple[tuple[str, ...], Counter]:
    """What differs between the arrays the profile wrote of the file at `path` and what its
    frames' description gives, and what the comparison saw."""
    d = described(path)
    out = d.out
    assert isinstance(out, Output), "an ND2 is one file"
    seen: Counter = Counter()
    if d.stored.undescribed:
        return ("descriptions dropped for room",), seen
    loops, key = loops_of(path)
    found = [k for k, a in enumerate(d.stored.arrays) if a.element == key]
    if len(found) != 1:
        return (f"{len(found)} descriptions keyed by the facts' declared_by",), seen
    (k,) = found
    seen["files"] += 1
    seen["descriptions the profile writes no array of"] += len(d.stored.arrays) - 1
    # the arrays the profile wrote: one a position, whichever frames the file holds
    nodes = {key: v for key, v in out.bytes_entries.items()
             if key.endswith("zarr.json") and not key.startswith("refkv_source/")}
    docs = {key for key, v in nodes.items() if json.loads(v)["node_type"] == "array"}
    positions = math.prod(n for kind, n, _ in loops if kind == "p")
    said: list[str] = []
    if docs != {f"{p}/0/zarr.json" for p in range(positions)}:
        said.append(f"arrays at {sorted(docs)[:4]}, of {positions} positions")
        return tuple(said), seen
    seen["files of several positions"] += positions > 1
    theirs = {p: written(out, f"{p}/0/") for p in range(positions)}
    # the rows of a row block are the profile's choice: the output's chunks say them
    block = integer(sequence(theirs[0][0]["chunk_shape"])[-2])
    mine = derived(d, d.stored.arrays[k], d.rows.get(k, []), loops, block, seen)
    if isinstance(mine, str):
        return (mine,), seen
    doc, chunks = mine
    for p, (members, parts) in theirs.items():
        seen["arrays"] += 1
        seen["arrays of no chunk"] += not parts
        said += [f"{p}/0: {name} {members[name]}, the description gives {doc[name]}"
                 for name in doc if members[name] != doc[name]]
        want = {key: v for key, v in chunks.items() if key.startswith(f"{p}/0/c/")}
        if set(want) != set(parts):
            said.append(f"{p}/0: chunks at {sorted(set(parts) - set(want))[:4]} alone, and none at "
                        f"{sorted(set(want) - set(parts))[:4]}")
        said += [f"{key} is {parts[key][:4]}, not {v[:4]}" for key, v in sorted(want.items())
                 if key in parts and parts[key] != v]
    if {key for key in chunks if int(key.split("/")[0]) >= positions}:
        said.append("frames at positions the profile writes no array of")
    return tuple(said), seen


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_the_nd2_profile_writes_what_the_description_gives(path):
    """Every array the profile writes of the file, and every chunk of it, is what the frames'
    description gives."""
    said, seen = compared(path)
    assert said == ()
    assert seen["arrays"] >= 1


def test_the_fixtures_hold_every_case_the_profile_is_compared_in():
    """The fixtures reach each case of the comparison, so that none of it passes for want of a
    file. The descriptions with no array are the frame chunks past the loops' count, which the
    image leaves out; the chunks of rows apart are the row blocks of two files, one of them of
    blocks of half a frame's rows."""
    seen: Counter = Counter()
    for path in FILES:
        seen += compared(path)[1]
    assert {k: v for k, v in seen.items() if v} == {
        "files": 33,
        "files of several positions": 5,
        "files of row blocks shorter than a frame": 1,
        "arrays": 39,
        "chunks of a frame": 2102,
        "chunks of a zlib stream": 8,
        "chunks of rows apart": 4,
        "frames at a flipped index": 18,
        "descriptions the profile writes no array of": 5,
    }
