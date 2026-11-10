"""The HDF5 profile's arrays against the stored arrays' descriptions (spec/stored-array.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): what the profile
writes of a dataset's storage, read off the dataset's description by the profile's rules
(spec/zarrify/hdf5.md §3.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#32-chunks>),
is what the profile wrote. Every array the profile writes has a description, found by its object
header's address, of its shape, its HDF5 chunk shape, its filters and its fill value; and each of its
chunks holds the ranges of the source the description's rows give.

The profile reads some datasets' elements further than a description does, which says where the
stored elements are and not what they mean: a variable-length string's bytes are in a global heap,
an object reference is written as a path, and a compound with such a member is written anew. Of
those the shape and the source metadata are compared, and the chunks are not.

An Imaris file's arrays are the IMS profile's, of another layout, and are not compared here."""

import base64
import json
import math
from collections import Counter
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import h5py
import numpy as np
import pytest
from described import Described, described
from ir_helpers import FIXTURES, core_setting

from refkv import _core
from refkv._narrow import JSON, at, integer, mapping, sequence, string
from refkv.ir.stored import Each, Given, Source, StoredArray, Value
from refkv.zarrify.common import Output

FILES = sorted(p for p in (FIXTURES / "hdf5").iterdir() if p.suffix in (".h5", ".nwb") and "reject" not in p.name)
# the registered filter id each codec the IR names stands for (spec/stored-array.md §3.2
# <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#32-the-list>)
FILTER = {"zlib": 1, "shuffle": 2, "h5_fletcher32": 3, "h5_szip": 4, "h5_nbit": 5, "h5_scaleoffset": 6,
          "bzip2": 307, "lzf": 32000, "blosc": 32001, "h5_lz4": 32004, "h5_bitshuffle": 32008, "zstd": 32015}
# the Zarr codec the profile writes for each (spec/zarrify/hdf5.md §3
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#3-datasets>); a checksum has none
CODEC = {"zlib": "numcodecs.zlib", "shuffle": "numcodecs.shuffle", "lzf": "imagecodecs_lzf", "blosc": "blosc",
         "h5_lz4": "imagecodecs_lz4h5", "h5_bitshuffle": "refkv.h5_bitshuffle", "zstd": "zstd"}
CHECKSUM = 4

type Range = tuple[str | None, int, int]
"""(object key, offset, length): bytes of the source; the key None in a file's IR"""


@dataclass(frozen=True)
class Presented:
    """An array the profile wrote: its path, its document, its `hdf5` source metadata, the object
    its dataset's header is in with the header's offset there, and its chunks by coordinates, each
    a reference's parts or a bytes entry."""

    path: str
    doc: dict[str, JSON]
    own: dict[str, JSON]
    key: str | None
    header: int
    chunks: dict[tuple[int, ...], list | bytes]


def ranges(parts: list) -> list[Range]:
    """The ranges of the source among a reference's parts, in order, adjacent ones joined: neither
    its literals nor its ranges of a data source."""
    out: list[Range] = []
    for p in parts:
        if isinstance(p, bytes):
            continue
        if p[0] == "object":
            r = (p[1], p[2], p[3])
        elif len(p) == 2 or p[0] == 0:
            r = (None, p[-2], p[-1])
        else:
            continue
        if out and out[-1][0] == r[0] and out[-1][1] + out[-1][2] == r[1]:
            out[-1] = (r[0], out[-1][1], out[-1][2] + r[2])
        else:
            out.append(r)
    return out


def within(parts: list[Range], start: int, length: int) -> list[Range]:
    """The `length` bytes from `start` of the bytes `parts` join to, as ranges."""
    out = []
    for key, o, n in parts:
        if start < n and length > 0:
            take = min(n - start, length)
            out.append((key, o + start, take))
            length -= take
        start = max(start - n, 0)
    return out


def item_size(data_type: JSON) -> int | None:
    """The bytes of an element of a Zarr data type the profile writes; None for one of no fixed size."""
    if isinstance(data_type, str):
        return None if data_type == "string" else np.dtype(data_type).itemsize
    if at(data_type, "name") != "struct":
        return None
    fields = sequence(at(data_type, "configuration", "fields"), "fields")
    sizes = [item_size(at(f, "data_type")) for f in fields]
    return None if None in sizes else sum(s for s in sizes if s is not None)


@cache
def presented(path: Path) -> tuple[Presented, ...]:
    """The arrays the profile wrote of the file at `path`, but the source metadata node's."""
    d = described(path)
    out = d.out
    entries = out.bytes_entries if isinstance(out, Output) else out.entries
    docs = {k[:-len("zarr.json")]: mapping(json.loads(v), k) for k, v in entries.items()
            if k.endswith("zarr.json") and not k.startswith("refkv_source/")}
    named = None if None in d.objects else path.name
    arrays = []
    for prefix, doc in sorted(docs.items()):
        if doc["node_type"] != "array":
            continue
        own = dict(mapping(at(doc, "attributes", "refkv_zarrified", "hdf5"), prefix))
        # the file a node is in: the one the nearest external link above it names (spec/zarrify/hdf5.md
        # §8.6 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#86-external-files>)
        key, above = named, prefix
        while above:
            declared = mapping(mapping(docs[above].get("attributes", {}), above).get("refkv_zarrified", {}), above)
            link = mapping(declared.get("hdf5", {}), above)
            if "external" in link:
                key = string(at(link, "external", "file"), above)
                break
            above = above[:above.rstrip("/").rfind("/") + 1]
        chunks: dict[tuple[int, ...], list | bytes] = {}
        for k, v in [*out.refs.items(), *entries.items()]:
            if k == f"{prefix}c" or k.startswith(f"{prefix}c/"):
                chunks[tuple(int(x) for x in k[len(prefix) + 2:].split("/") if x)] = v
        arrays.append(Presented(prefix.rstrip("/"), dict(doc), own, key, integer(own["address"], prefix) + base(d, key),
                                chunks))
    return tuple(arrays)


def base(d: Described, key: str | None) -> int:
    """Where the HDF5 file in the object `key` begins: its superblock's offset, past a user block."""
    top = 0 if key is None else next(c for c in d.ir.children(0) if d.ir.name(c) == key)
    superblock = d.ir.child(top, "superblock")
    assert superblock is not None, key
    return d.ir.extent(superblock)[0]


def check_metadata(d: Described, a: StoredArray, names: list[str], p: Presented) -> list[str]:
    """What the description says other than the array's source metadata: the layout, the HDF5
    chunk shape, the filters and the fill value's bytes."""
    said = []
    assert a.grid is not None
    lengths = [x.length for x in a.grid.axes if isinstance(x, Each)]
    chunked = p.own["layout"] == "chunked"
    if chunked != (a.grid.edge == "full") or (chunked and p.own["chunks"] != lengths):
        said.append(f"grid {a.grid}; the profile's layout {p.own['layout']}, chunks {p.own.get('chunks')}")
    filters = []
    for c in d.stored.chains[a.chains[0]] if a.chains else ():
        given = c.parameters.get("id")
        filters.append(given.value if c.name == "source" and isinstance(given, Value) else FILTER[c.name])
    if filters != p.own.get("filters", []):
        said.append(f"chain {names}; the profile's filters {p.own.get('filters')}")
    fill = None
    if isinstance(a.fill, Given):
        param = a.fill.param
        fill = d.place(param.place) if isinstance(param, Source) else None
    stated = p.own.get("fill_value")
    if fill != (None if stated is None else base64.b64decode(string(stated, "fill_value"))):
        said.append(f"fill {a.fill}; the profile's {stated}")
    return said


def check_chunked(d: Described, names: list[str], rows: list[dict], p: Presented, extra: int,
                  seen: Counter) -> list[str]:
    """What a chunked dataset's rows say other than the array's chunks: one chunk a row, at the
    row's coordinates, of the row's bytes without a checksum; in a skipped filter's literal form
    where the row skips one, and the range alone where it skips none."""
    said = []
    by = {(*r["coords"], *[0] * extra): r for r in rows}
    if set(by) != set(p.chunks):
        said.append(f"rows at {sorted(set(by) - set(p.chunks))} and chunks at {sorted(set(p.chunks) - set(by))} alone")
    checksum = names.index("h5_fletcher32") if "h5_fletcher32" in names else None
    for coords, row in sorted(by.items()):
        parts = p.chunks.get(coords)
        if not isinstance(parts, list):
            continue
        skips = row["skips"] or 0
        stored = d.parts(row)
        length = sum(n for _, _, n in stored)
        if checksum is not None and not skips >> checksum & 1:
            length -= CHECKSUM
            seen["chunks without their checksum"] += 1
        skipped = skips & ~(0 if checksum is None else 1 << checksum)
        framed = any(isinstance(x, bytes) for x in parts)
        if ranges(parts) != within(stored, 0, length) or framed != bool(skipped):
            said.append(f"chunk {coords}: {parts}; the row's bytes {stored}, skips {skips}")
        seen["chunks"] += 1
        seen["chunks in a skipped filter's form"] += framed
    return said


def check_whole(d: Described, rows: list[dict], p: Presented, item: int, seen: Counter) -> list[str]:
    """What a row of data stored whole says other than the array's chunks: the profile cuts the
    data into chunks, each the range of the elements C order puts in it."""
    shape = [integer(n, "shape") for n in sequence(p.doc["shape"], "shape")]
    chunk = [integer(n, "chunk_shape") for n in sequence(at(p.doc, "chunk_grid", "configuration", "chunk_shape"), "c")]
    if not rows:
        seen["arrays of no stored data"] += 1
        return [f"chunks {sorted(p.chunks)} of no stored data"] if p.chunks else []
    said = []
    (row,) = rows
    stored = d.parts(row)
    grid = [range(math.ceil(n / c)) for n, c in zip(shape, chunk, strict=True)]
    if set(p.chunks) != set(np.ndindex(*map(len, grid))):
        said.append(f"chunks at {sorted(p.chunks)} of a grid of {[len(g) for g in grid]}")
    for coords, parts in sorted(p.chunks.items()):
        first = [x * c for x, c in zip(coords, chunk, strict=True)]
        held = [min(c, n - o) for c, n, o in zip(chunk, shape, first, strict=True)]
        # the chunk's elements are one run of the data where every axis past the first it cuts is whole
        cut = next((k for k, (h, n) in enumerate(zip(held, shape, strict=True)) if h != n), len(shape))
        if held[cut + 1:] != shape[cut + 1:]:
            said.append(f"chunk {coords} of {chunk} is no one run of the elements of {shape}")
            continue
        start = int(np.ravel_multi_index(first, shape)) * item if shape else 0
        want = within(stored, start, math.prod(held) * item)
        if isinstance(parts, bytes):
            # an edge chunk the profile copied with its padding
            data = b"".join(d.objects[k][o:o + n] for k, o, n in want)
            same = parts[:len(data)] == data and not any(parts[len(data):])
            seen["chunks copied"] += 1
        else:
            same = ranges(parts) == want and not any(isinstance(x, bytes) and any(x) for x in parts)
        if not same:
            said.append(f"chunk {coords}: {parts if isinstance(parts, list) else '(copied)'}; C order gives {want}")
        seen["chunks cut from data stored whole"] += 1
    seen["arrays stored whole"] += 1
    seen["arrays stored whole in several chunks"] += len(p.chunks) > 1
    seen["arrays stored in other files"] += any(k not in (None, p.key) for k, _, _ in stored)
    return said


def check_array(d: Described, k: int, p: Presented, seen: Counter) -> list[str]:
    """What the description of the dataset the profile wrote as `p`, array `k`, says other than it."""
    a = d.stored.arrays[k]
    rows = d.rows.get(k, [])
    if a.shape is None or a.grid is None or a.item_bits is None:
        return ["the profile writes an array of a dataset with no shape or no grid"]
    names = [c.name for c in d.stored.chains[a.chains[0]]] if a.chains else []
    said = check_metadata(d, a, names, p)
    shape = sequence(p.doc["shape"], "shape")
    rank = len(a.shape)
    if tuple(shape[:rank]) != a.shape:
        said.append(f"shape {a.shape}; the profile's {shape}")
    # the elements as the file stores them: the description's size, in the trailing axis of a
    # fixed-length string; else the profile read them further
    size = item_size(p.doc["data_type"])
    codecs = [string(at(c, "name"), "codec") for c in sequence(p.doc["codecs"], "codecs")]
    extra = [integer(n, "shape") for n in shape[rank:]]
    if codecs[0] != "bytes" or size is None or size * math.prod(extra) * 8 != a.item_bits:
        seen["arrays read further"] += 1
        return said
    if codecs[1:] != [CODEC.get(n, n) for n in names if n != "h5_fletcher32"]:
        said.append(f"chain {names}; the profile's codecs {codecs}")
    chunk = sequence(at(p.doc, "chunk_grid", "configuration", "chunk_shape"), "chunk_shape")
    seen["arrays"] += 1
    seen["arrays of a trailing axis"] += bool(extra)
    if a.grid.edge == "full":
        if chunk != [x.length for x in a.grid.axes if isinstance(x, Each)] + extra:
            said.append(f"grid {a.grid}; the profile's chunk shape {chunk}")
        seen["arrays chunked"] += 1
        return said + check_chunked(d, names, rows, p, len(extra), seen)
    return said + check_whole(d, rows, p, size, seen)


@cache
def compared(path: Path) -> tuple[tuple[str, ...], Counter]:
    """(what the descriptions of the file at `path` say other than the profile's arrays; how many
    of each thing were compared)."""
    d = described(path)
    by_header: dict[tuple[str | None, int], int] = {}
    for k, a in enumerate(d.stored.arrays):
        prefix = d.ir.child(a.element, "prefix")
        assert prefix is not None, a.path
        by_header[d.at(prefix)[:2]] = k
    said: list[str] = []
    seen: Counter = Counter()
    matched = set()
    for p in presented(path):
        k = by_header.get((p.key, p.header))
        if k is None:
            said.append(f"/{p.path}: no description of the object at {p.header} of {p.key}")
            continue
        matched.add(k)
        said += [f"/{p.path}: {s}" for s in check_array(d, k, p, seen)]
    # a dataset the profile does not present (spec/zarrify/hdf5.md §7.4
    # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#74-what-is-not-presented>) is
    # described all the same
    seen["descriptions the profile writes no array of"] = len(d.stored.arrays) - len(matched)
    return tuple(said), seen


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_the_hdf5_profile_writes_what_the_descriptions_give(path):
    said, _ = compared(path)
    assert said == ()


def test_the_fixtures_hold_every_case_the_profile_is_compared_in():
    seen: Counter = Counter()
    for path in FILES:
        seen += compared(path)[1]
    assert dict(seen) == {
        "arrays": 164,
        "arrays chunked": 64,
        "arrays stored whole": 98,
        "arrays of no stored data": 2,
        "arrays of a trailing axis": 4,
        "arrays stored whole in several chunks": 1,
        "arrays stored in other files": 1,
        "arrays read further": 92,
        "chunks": 155,
        "chunks without their checksum": 10,
        "chunks in a skipped filter's form": 19,
        "chunks cut from data stored whole": 99,
        "descriptions the profile writes no array of": 19,
    }


def test_data_no_fixture_stores_is_written_as_its_descriptions_give(tmp_path):
    """Data stored whole that the profile cuts on two axes' worth of elements, its edge chunk padded
    from the zero data source and then copied with its padding; and fixed-length strings in chunks,
    whose bytes are a trailing axis."""
    for name, threshold in (("padded.h5", None), ("copied.h5", 8)):
        with h5py.File(tmp_path / name, "w") as f:
            f["whole"] = np.arange(701 * 500, dtype="<i4").reshape(701, 500)
            f.create_dataset("strings", data=np.array([b"ab", b"cde", b"f", b"ghijk", b""], "S5"), chunks=(2,))
        with core_setting(_core._set_copy_threshold, threshold):
            said, seen = compared(tmp_path / name)
        assert said == ()
        assert dict(seen) == {
            "arrays": 2,
            "arrays chunked": 1,
            "arrays stored whole": 1,
            "arrays of a trailing axis": 1,
            "arrays stored whole in several chunks": 1,
            "arrays stored in other files": 0,
            "chunks": 3,
            "chunks in a skipped filter's form": 0,
            "chunks cut from data stored whole": 2,
            "descriptions the profile writes no array of": 0,
            **({} if threshold is None else {"chunks copied": 1}),
        }
