"""The safetensors profile's arrays against the stored arrays' descriptions (spec/stored-array.md
§7 <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): what the profile
writes of a tensor, read off the tensor's description by the profile's rules
(spec/zarrify/safetensors.md §4.2
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#42-the-arrays> and §8.3
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#83-chunks>), is what the
profile wrote, in a file's output and in a store's.

Of a description the test derives the whole array document and every chunk:

- the shape, from `shape`, and the dimension names, from its rank;
- the data type and the fill value, from the `dtype` the description's declaration holds, by the
  profile's table (spec/zarrify/safetensors.md §3
  <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#3-data-types>); the data
  type's bits are the description's `item_bits`;
- the codecs, from `item_bits` and the chain: `bytes` for an empty chain, little-endian above one
  byte, and `packbits` for `lsb_first`;
- the chunk shape and the chunks, from the shape, `item_bits` and the place of the one row, cut as
  conventions §7 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays>
  cuts contiguous values (written out here from that section), an edge chunk's padding zero bytes;
  a packed tensor cut by elements, or one chunk where a chunk would start within a byte.

Two things no description holds are taken from the output's source metadata: a tensor's path (its
name, or that the profile does not present it), found at the description's index in the header's
tensors, and, in a store, the path of its shard's group, found by the key of the object the
description's element is in. The fill value is the profile's, the zero of its data type: the
description's is `undefined`, since safetensors has none. No chunk of the fixtures is copied; one
that were would be reported as a difference."""

import json
import math
from collections import Counter
from functools import cache
from itertools import product
from pathlib import Path

import numpy as np
import pytest
from described import Described
from test_safetensors import TYPES
from test_safetensors_stored import FILES, STORES, reached, read, written

from refkv._narrow import JSON, at, integer, mapping, sequence, string
from refkv.ir.store import StoreIrOutput
from refkv.ir.stored import Value

# the chunk limit of spec/zarrify/safetensors.md §8.3
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#83-chunks>, and the paddings
# conventions §7 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays>
# allows: one it takes at once, the most of one edge chunk, and the most of all of an array's
LIMIT, SMALL, MOST, TOTAL = 1 << 20, 1 << 10, (1 << 16) - (1 << 10), 1 << 20
# the data types whose zero is not the number 0 (spec/zarrify/safetensors.md §3
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#3-data-types>)
ZERO: dict[str, JSON] = {"bool": False, "complex64": [0, 0], "float8_e8m0fnu": "NaN"}

type Piece = tuple[str | None, int, int] | bytes
"""a part of a chunk: (object key, offset, length) of the source, or bytes it does not hold there"""
type Run = tuple[tuple[int, ...], int, int, int]
"""a chunk of contiguous values: its coordinates, the first of its elements, how many it holds and
how many more its shape has"""


def cut(shape: tuple[int, ...], size: int) -> tuple[list[int], list[Run]]:
    """The chunk shape and the chunks of contiguous values of `shape`, of `size` bytes an element,
    as conventions §7
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays> cuts
    them: along the first axis whose indices fit the limit and whose padding fits, the axes after
    it whole."""
    if 0 in shape:
        return [max(1, n) for n in shape], []
    if not shape:
        return [], [((), 0, 1, 0)]
    total = size * math.prod(shape)
    axis, chunk = len(shape) - 1, 0
    for j, n in enumerate(shape):
        slab = size * math.prod(shape[j + 1:])
        if slab > LIMIT:
            continue
        fewest = math.ceil(n / (LIMIT // slab))
        # the first count whose padding is small, else the one of least padding
        counts = [(math.ceil(n / q), q) for q in range(fewest, min(2 * fewest, n) + 1)]
        paddings = [((math.ceil(n / c) * c - n) * slab, c) for c, _ in counts]
        padding, c = next((p for p in paddings if p[0] <= SMALL), min(paddings, key=lambda p: p[0]))
        if padding <= MOST and padding * math.prod(shape[:j]) <= max(TOTAL, total // 64):
            axis, chunk = j, c
            break
        if j == len(shape) - 1:
            axis, chunk = j, math.ceil(n / fewest)
    n, within = shape[axis], math.prod(shape[axis + 1:])
    runs: list[Run] = []
    for k, outer in enumerate(product(*(range(m) for m in shape[:axis]))):
        for q in range(math.ceil(n / chunk)):
            held = min(chunk, n - q * chunk)
            coords = (*outer, q, *[0] * (len(shape) - axis - 1))
            runs.append((coords, (k * n + q * chunk) * within, held * within, (chunk - held) * within))
    return [*[1] * axis, chunk, *shape[axis + 1:]], runs


def derived(d: Described, k: int, seen: Counter) -> tuple[dict[str, JSON], dict[tuple[int, ...], list[Piece]]] | str:
    """The array document and the chunks the profile's rules give the description `k`; or why they
    give none."""
    a = d.stored.arrays[k]
    if a.grid is None or a.shape is None or a.item_bits is None or len(a.declarations) != 1:
        return "a description of no grid"
    dtype = json.loads(d.place(a.declarations[0]))
    data_type, _, bits = TYPES[dtype]
    if bits != a.item_bits:
        return f"item_bits {a.item_bits}; the profile's data type {data_type}, of {bits} bits"
    names = [(c.name, dict(c.parameters)) for c in d.stored.chains[a.chains[0]]]
    codec: dict[str, JSON]
    if not names:
        seen["arrays of one byte an element" if bits == 8 else "arrays of several bytes an element"] += 1
        codec = {"name": "bytes"} if bits == 8 else {"name": "bytes", "configuration": {"endian": "little"}}
    elif names == [("lsb_first", {"bits": Value(bits)})]:
        codec = {"name": "packbits", "configuration": {"padding_encoding": "none"}}
    else:
        return f"chain {names}, which the profile presents nothing of"
    rows = d.rows.get(k, [])
    if len(rows) > 1 or any(len(d.parts(r)) != 1 for r in rows):
        return f"{len(rows)} rows, not one unit in one range"
    # a packed tensor is cut by elements, each counted as a byte
    packed = bits < 8
    chunk_shape, runs = cut(a.shape, 1 if packed else bits // 8)
    chunks: dict[tuple[int, ...], list[Piece]] = {}
    for row in rows:
        ((key, start, length),) = d.parts(row)
        if packed and any(first * bits % 8 for _, first, _, _ in runs):
            # a chunk would start within a byte: one chunk, the whole data
            seen["packed arrays of one chunk, a chunk starting within a byte"] += 1
            chunk_shape, runs = [max(1, n) for n in a.shape], [(tuple([0] * len(a.shape)), 0, math.prod(a.shape), 0)]
        elif packed:
            seen["packed arrays cut by elements"] += 1
        for coords, first, held, more in runs:
            # the run's elements' bytes, and zero bytes up to the whole chunk's
            size = -(-held * bits // 8)
            padding = -(-(held + more) * bits // 8) - size
            chunks[coords] = [(key, start + first * bits // 8, size), *([bytes(padding)] if padding else [])]
            seen["chunks"] += 1
            seen["chunks padded with zero bytes"] += padding > 0
        if sum(p[2] for parts in chunks.values() for p in parts if isinstance(p, tuple)) != length:
            return f"chunks that do not hold the unit's {length} bytes"
    seen["arrays of no chunks"] += not chunks
    seen["arrays of rank 0"] += not a.shape
    doc: dict[str, JSON] = {
        "zarr_format": 3,
        "node_type": "array",
        "shape": list(a.shape),
        "data_type": data_type,
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": list(chunk_shape)}},
        "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
        "fill_value": ZERO.get(data_type, 0),
        "codecs": [codec],
        "dimension_names": [f"dim_{j}" for j in range(len(a.shape))],
        "attributes": {},
    }
    return doc, chunks


def entries(d: Described) -> dict[str, bytes]:
    """The output's entries of bytes."""
    return d.out.entries if isinstance(d.out, StoreIrOutput) else d.out.bytes_entries


def pieces(d: Described, key: str) -> list[Piece]:
    """The parts of the chunk the output wrote at `key`: ranges of the source, and bytes, those
    side by side joined."""
    out = d.out
    if key in entries(d):
        return [entries(d)[key]]
    got: list[Piece] = []
    for p in out.refs[key]:
        piece: Piece
        if isinstance(p, bytes):
            piece = p
        elif p[0] == "object":
            piece = (string(p[1]), integer(p[2]), integer(p[3]))
        elif len(p) == 2:
            piece = (None, p[0], p[1])
        elif isinstance(out, StoreIrOutput):
            piece = out.data[integer(p[0]) - 1][p[1]:p[1] + p[2]]
        elif p[0] == 0:
            piece = (None, p[1], p[2])
        else:
            piece = {i: b for b, i in out.data.items()}[integer(p[0])][p[1]:p[1] + p[2]]
        last = got[-1] if got else None
        if isinstance(piece, bytes) and isinstance(last, bytes):
            got[-1] = last + piece
        else:
            got.append(piece)
    return got


def metadata(d: Described, prefix: str) -> list[dict[str, JSON]]:
    """The tensors of the source metadata of the group at `prefix` of the output, in the header's
    order."""
    doc = json.loads(entries(d)[f"{prefix}zarr.json"])
    return [dict(mapping(t)) for t in sequence(at(doc, "attributes", "refkv_zarrified", "safetensors", "tensors"))]


def check_array(d: Described, k: int, path: str, seen: Counter) -> list[str]:
    """The array the output holds at `path`, and its chunks, against the description `k`."""
    want = derived(d, k, seen)
    if isinstance(want, str):
        return [want]
    doc, chunks = want
    said = []
    wrote = json.loads(entries(d).get(f"{path}/zarr.json", b"null"))
    if wrote is None:
        said.append("no array document")
    elif wrote != doc:
        differ = sorted(m for m in {*doc, *wrote} if wrote.get(m) != doc.get(m))
        said.append(f"the profile's {differ} {[wrote.get(m) for m in differ]}, not {[doc.get(m) for m in differ]}")
    keys = {f"{path}/c" + "".join(f"/{c}" for c in coords): parts for coords, parts in chunks.items()}
    written_keys = {key for key in [*d.out.refs, *entries(d)] if key == f"{path}/c" or key.startswith(f"{path}/c/")}
    if written_keys != set(keys):
        alone, missing = sorted(written_keys - set(keys)), sorted(set(keys) - written_keys)
        said.append(f"chunks at {alone[:4]} alone, and none at {missing[:4]}")

    def shown(parts: list[Piece]) -> list[object]:
        return [p if isinstance(p, tuple) else f"{len(p)} bytes" for p in parts]

    said += [f"chunk {key} is {shown(pieces(d, key))}, not {shown(keys[key])}"
             for key in sorted(written_keys & set(keys)) if pieces(d, key) != keys[key]]
    return said


@cache
def compared(path: Path) -> tuple[tuple[str, ...], Counter]:
    """What differs between the arrays the profile wrote of the file, or the store, at `path` and
    its descriptions, and what the comparison saw."""
    d = read(path)
    said: list[str] = []
    seen: Counter = Counter()
    if d.stored.undescribed:
        said.append(f"descriptions dropped for room: {d.stored.undescribed}")
    root = json.loads(entries(d)["zarr.json"])
    # a shard's group, by its key: the root, for a file
    groups: dict[str | None, str | None] = {None: ""}
    if isinstance(d.out, StoreIrOutput):
        files = [mapping(f) for f in sequence(at(root, "attributes", "refkv_zarrified", "safetensors", "files"))]
        groups = {string(f["key"]): f"{string(f['path'])}/" if "path" in f else None for f in files}
    presented: set[str] = set()
    for k, a in enumerate(d.stored.arrays):
        prefix = groups[d.key(a.element)]
        assert a.index is not None
        tensor = None if prefix is None else metadata(d, prefix)[a.index]
        if tensor is None or "path" not in tensor:
            seen["descriptions the profile writes no array of"] += 1
            seen["descriptions of a shard the profile writes no group of"] += tensor is None
            continue
        array = f"{prefix}{string(tensor['path'])}"
        seen["arrays"] += 1
        presented.add(f"{array}/zarr.json")
        said += [f"{array}, {a.path} {a.index}: {s}" for s in check_array(d, k, array, seen)]
    # every array the profile wrote is some description's
    arrays = {key for key, v in entries(d).items() if key.endswith("zarr.json") and not key.startswith("refkv_source/")
              and json.loads(v)["node_type"] == "array"}
    said += [f"{key}: an array of no description" for key in sorted(arrays - presented)]
    return tuple(said), seen


@pytest.mark.parametrize("path", [*FILES, *STORES], ids=[p.name for p in [*FILES, *STORES]])
def test_the_safetensors_profile_writes_what_the_descriptions_give(path):
    """Every array the profile writes of the file, or of the store, is what its tensor's
    description gives."""
    said, _ = compared(path)
    assert said == ()


def test_the_fixtures_hold_every_case_the_profile_is_compared_in():
    """The fixtures reach each case of the comparison, so that none of it passes for want of a
    file. The descriptions with no array are the tensors whose names are no paths, and those of a
    shard whose key is none."""
    seen: Counter = Counter()
    for path in [*FILES, *STORES]:
        seen += compared(path)[1]
    assert reached(seen) == {
        "arrays": 45,
        "arrays of one byte an element": 16,
        "arrays of several bytes an element": 24,
        "packed arrays cut by elements": 4,
        "packed arrays of one chunk, a chunk starting within a byte": 1,
        "arrays of rank 0": 1,
        "arrays of no chunks": 1,
        "chunks": 46,
        "chunks padded with zero bytes": 1,
        "descriptions the profile writes no array of": 7,
        "descriptions of a shard the profile writes no group of": 1,
    }


def test_the_profile_writes_what_the_descriptions_give_of_cuts_no_fixture_holds(tmp_path):
    """Cuts the fixtures do not have, in a file written here: along a second axis, with a padded
    chunk in each row; and a packed tensor cut by elements whose last chunk is padded."""
    rng = np.random.default_rng(0)
    a, b = 3 * 200001 * 8, 2097154 // 2
    header = json.dumps({
        "rows": {"dtype": "I64", "shape": [3, 200001], "data_offsets": [0, a]},
        "packed": {"dtype": "F4", "shape": [2097154], "data_offsets": [a, a + b]},
    })
    said, seen = compared(written(tmp_path / "cuts.safetensors", header, rng.bytes(a + b)))
    assert said == ()
    assert reached(seen) == {
        "arrays": 2,
        "arrays of several bytes an element": 1,
        "packed arrays cut by elements": 1,
        "chunks": 9,
        "chunks padded with zero bytes": 4,
    }
    # the chunk shapes the comparison derived are the ones this file was sized for
    wrote = entries(read(tmp_path / "cuts.safetensors"))
    docs = [json.loads(wrote[f"{p}/zarr.json"]) for p in ("rows", "packed")]
    assert [at(doc, "chunk_grid", "configuration", "chunk_shape") for doc in docs] == [[1, 100001], [699052]]
