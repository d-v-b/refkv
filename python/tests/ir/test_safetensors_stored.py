"""The stored arrays of the safetensors fixtures (spec/stored-array.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>), files and stores,
compared with the reference readers: the safetensors library (`safetensors.deserialize`, which
gives each tensor's data type, shape and bytes) and the file's own header, read here apart from the
parser (the length, then JSON through Python's scanner, for the tensors' order, each one's
`data_offsets` and where its `dtype` is written). Every tensor the library reads has a description,
keyed by its file's `header` element (or, of a shard the store form does not present, by the
shard's object), at its position in the header, and

- its shape is the library's;
- its sample size is the library's: the bits of its bytes over its elements, and, of a tensor of
  no elements, the size of its data type in the library's table;
- its one declaration is the bytes of the header that write its `dtype`, which read as the
  library's data type;
- its grid is one unit of the whole tensor, and its chain is empty for whole bytes and `lsb_first`
  for a sample of 4 or 6 bits;
- its one row is at the bytes the header's `data_offsets` give, and holds the library's bytes; a
  tensor of no bytes has no row;
- a packed unit, decoded through its chain, holds the elements the library's bytes hold in the
  order spec/zarrify/safetensors.md §3.2
  <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#32-packed-data-types>
  gives. The library reads a tensor's bytes and none of its elements, so that order is the
  profile's document's, not a reader's.

A file the library refuses has no description. A second test asserts which cases the fixtures
reached; a third reads tensors no fixture holds, written here; and one test for each way a file's
descriptions fall back, at the descriptions' limit: no tensor of a checked header has no grid
otherwise."""

import json
import math
import struct
from collections import Counter
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import override

import jsonschema
import numpy as np
import pytest
import safetensors
from described import SCHEMA, Described, described
from ir_helpers import FIXTURES
from test_safetensors import TYPES, expected

from refkv.ir import Ir, parse
from refkv.ir.kind import Kind
from refkv.ir.planner import open_transport
from refkv.ir.store import StoreIrOutput, zarrify_store
from refkv.ir.stored import Codec, Each, ElementPlace, Grid, Listed, Stored, Value

pa = pytest.importorskip("pyarrow")

FILES = sorted(p for p in (FIXTURES / "safetensors").iterdir() if p.is_file() and "reject" not in p.name)
STORES = sorted(p for p in (FIXTURES / "safetensors").iterdir() if p.is_dir())
SPACE = " \t\n\r"


@dataclass(frozen=True)
class StoreDescribed(Described):
    """A store's IR with its stored arrays: an object's key is its element's path."""

    @override
    def key(self, element: int) -> str | None:
        while element and self.ir.kind(element) != Kind.OBJECT:
            element = self.ir.parent(element)
        return self.ir.path(element) if element else None


@cache
def described_store(root: Path) -> Described:
    """The IR of the store at the directory `root`, zarrified in the profile's store form, its
    descriptions' document checked against the schema."""
    out, core, _ = zarrify_store(str(root), f"https://data.test/{root.name}/", fmt="safetensors")
    jsonschema.validate(json.loads(core.stored_arrays()), SCHEMA)
    ir = Ir(core)
    rows: dict[int, list[dict]] = {}
    for r in pa.record_batch(core.chunk_map()).to_pylist():
        rows.setdefault(r["array"], []).append(r)
    objects: dict[str | None, bytes] = {p.relative_to(root).as_posix(): p.read_bytes()
                                        for p in root.rglob("*") if p.is_file()}
    return StoreDescribed(ir, ir.stored_arrays(), rows, objects, out)


def read(path: Path) -> Described:
    return described_store(path) if path.is_dir() else described(path)


def members(text: str, start: int) -> list[tuple[str, int, int]]:
    """The members of the JSON object that starts at `text[start]`, in the order written: each
    one's name and the extent of its value, by Python's JSON scanner."""
    assert text[start] == "{"
    decoder = json.JSONDecoder()
    out = []
    i = start + 1
    while True:
        while text[i] in SPACE + ",":
            i += 1
        if text[i] == "}":
            return out
        name, i = decoder.raw_decode(text, i)
        assert isinstance(name, str)
        while text[i] in SPACE + ":":
            i += 1
        _, end = decoder.raw_decode(text, i)
        out.append((name, i, end))
        i = end


@dataclass(frozen=True)
class Tensor:
    """A tensor as the reference readers give it: the library's data type, shape and bytes, and
    the header's places, (offset, length) in the file, of its data and of its `dtype` value."""

    name: str
    dtype: str
    shape: tuple[int, ...]
    data: bytes
    at: tuple[int, int]
    declared: tuple[int, int]


def reference(data: bytes) -> list[Tensor] | None:
    """A file's tensors, in the header's order, or None where the library refuses the file."""
    try:
        library = dict(safetensors.deserialize(data))
    except safetensors.SafetensorError:
        return None
    (n,) = struct.unpack_from("<Q", data)
    text = data[8:8 + n].decode()

    def byte(i: int) -> int:
        return 8 + len(text[:i].encode())

    out = []
    for name, start, end in members(text, text.index("{")):
        if name == "__metadata__":
            continue
        ((a, b),) = [(byte(a), byte(b)) for member, a, b in members(text, start) if member == "dtype"]
        begin, stop = json.loads(text[start:end])["data_offsets"]
        lib = library[name]
        at = (8 + n + begin, stop - begin)
        out.append(Tensor(name, lib["dtype"], tuple(lib["shape"]), lib["data"], at, (a, b - a)))
    assert len(out) == len(library)
    return out


def lsb_first(unit: bytes, bits: int) -> bytes:
    """The decoding of the codec `lsb_first` (spec/stored-array.md §3.2
    <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#32-the-list>): elements of `bits`
    bits, packed from each byte's least significant bit, packed again most significant bit first."""
    stream = np.unpackbits(np.frombuffer(unit, dtype=np.uint8), bitorder="little")
    items = stream[:len(stream) // bits * bits].reshape(-1, bits)[:, ::-1]
    return np.packbits(items.reshape(-1), bitorder="big").tobytes()


def elements(decoded: bytes, bits: int, count: int) -> np.ndarray:
    """The first `count` elements of `bits` bits of bytes that pack them most significant bit
    first (spec/stored-array.md §3.1
    <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#31-chains>), as integers."""
    stream = np.unpackbits(np.frombuffer(decoded, dtype=np.uint8), bitorder="big")[:count * bits]
    return stream.reshape(count, bits).astype(np.uint8) @ (1 << np.arange(bits, dtype=np.uint8)[::-1])


def check_tensor(d: Described, k: int, t: Tensor, key: str | None, seen: Counter) -> list[str]:
    """The description `k` against the tensor `t` of the object `key`, as the references read it."""
    a = d.stored.arrays[k]
    count = math.prod(t.shape)
    # the sample size: the library's bytes over its elements, or its table's, of a tensor of none
    bits = len(t.data) * 8 // count if count else TYPES[t.dtype][2]
    assert len(t.data) * 8 == count * bits, "the library's tensor is of whole elements"
    assert bits == TYPES[t.dtype][2], "the library's table"
    seen["tensors"] += 1
    seen["tensors of no elements, sized by the library's table"] += not count
    said = []
    if a.shape != t.shape:
        said.append(f"shape {a.shape}, the library's {t.shape}")
    if a.item_bits != bits:
        said.append(f"item_bits {a.item_bits}, the library's {bits}")
    if (a.axes, a.pad_from, a.fill, a.unit_parameters) != (None, None, "undefined", ()):
        said.append(f"axes {a.axes}, pad_from {a.pad_from}, fill {a.fill}, unit parameters {a.unit_parameters}")
    # one unit of the whole tensor, an axis of length 0 listing none
    if a.grid != Grid(tuple(Each(n) if n else Listed(()) for n in t.shape), "short"):
        said.append(f"grid {a.grid} of shape {t.shape}")
    chain = (Codec("lsb_first", {"bits": Value(bits)}),) if bits < 8 else ()
    if tuple(d.stored.chains[c] for c in a.chains) != (chain,):
        said.append(f"chains {[d.stored.chains[c] for c in a.chains]} of samples of {bits} bits")
    seen[f"tensors of {bits} bits, packed" if bits < 8 else "tensors of whole bytes"] += 1
    # the one declaration: where the header writes the dtype, which reads as the library's
    places = [d.at(p.element, p.range) if isinstance(p, ElementPlace) else None for p in a.declarations]
    if places != [(key, *t.declared)]:
        said.append(f"declarations at {places}, the header's dtype at {t.declared}")
    elif [json.loads(d.place(p)) for p in a.declarations] != [t.dtype]:
        said.append(f"a declaration of {[d.place(p) for p in a.declarations]}, the library's {t.dtype}")
    rows = d.rows.get(k, [])
    if not t.data:
        seen["tensors of no bytes"] += 1
        return [*said, *([f"{len(rows)} rows of a tensor of no bytes"] if rows else [])]
    if len(rows) != 1:
        return [*said, f"{len(rows)} rows of a tensor of {len(t.data)} bytes"]
    (row,) = rows
    seen["rows"] += 1
    seen["scalars"] += not t.shape
    if (row["coords"], row["member"], row["ranges"], row["chain"], row["skips"]) != ([0] * len(t.shape), *[None] * 4):
        said.append(f"a row {row}")
    # the unit is its data's element, whole, or, keyed by its object, a range of the object
    in_object = d.ir.kind(a.element) == Kind.OBJECT
    seen["rows of a range of their object"] += in_object
    if in_object != (row["range"] is not None) or (row["element"] == a.element) != in_object:
        said.append(f"a row of element {row['element']} and range {row['range']}, keyed by {a.path}")
    if d.parts(row) != [(key, *t.at)]:
        said.append(f"a unit at {d.parts(row)}, the header's data_offsets {t.at}")
    elif d.unit(row) != t.data:
        said.append("a unit of other bytes than the library's")
    elif bits < 8:
        mine = elements(lsb_first(d.unit(row), bits), bits, count)
        theirs = expected(t.dtype, list(t.shape), t.data).view(np.uint8).reshape(-1)
        seen["packed elements decoded"] += count
        if not np.array_equal(mine, theirs):
            said.append("a unit whose chain decodes to other elements")
    return said


@cache
def compared(path: Path) -> tuple[tuple[str, ...], Counter]:
    """What differs between the descriptions of the file, or the store, at `path` and the
    reference readers, and what the comparison saw."""
    d = read(path)
    said: list[str] = []
    seen: Counter = Counter()
    if d.stored.undescribed:
        said.append(f"descriptions dropped for room: {d.stored.undescribed}")
    by_object: dict[str | None, list[int]] = {}
    for k, a in enumerate(d.stored.arrays):
        by_object.setdefault(d.key(a.element), []).append(k)
    for key, data in d.objects.items():
        if key is not None and not key.endswith(".safetensors"):
            continue
        mine = by_object.pop(key, [])
        tensors = reference(data)
        name = key or path.name
        if tensors is None:
            seen["files the library refuses"] += 1
            said += [f"{name}: {len(mine)} descriptions of a file the library refuses"] if mine else []
            continue
        seen["files the library reads"] += 1
        arrays = [d.stored.arrays[k] for k in mine]
        if len(arrays) != len(tensors) or [a.index for a in arrays] != list(range(len(tensors))):
            said.append(f"{name}: descriptions {[a.index for a in arrays]} of {len(tensors)} tensors")
            continue
        if not arrays:
            seen["files of no tensors"] += 1
            continue
        # one key: the header, or, where the profile wrote no group of the shard, its object
        header = "header" if key is None else f"{key}/header"
        if len({a.element for a in arrays}) != 1 or arrays[0].path not in (header, key):
            said.append(f"{name}: descriptions keyed by {sorted({str(a.path) for a in arrays})}")
            continue
        in_object = d.ir.kind(arrays[0].element) == Kind.OBJECT
        seen["shards keyed by their object"] += in_object
        if isinstance(d.out, StoreIrOutput) and in_object != (f"{key}/zarr.json" not in d.out.entries):
            said.append(f"{name}: keyed by {arrays[0].path}, with{'out' * in_object} a group in the output")
        for k, t in zip(mine, tensors, strict=True):
            said += [f"{name}:{t.name}: {s}" for s in check_tensor(d, k, t, key, seen)]
    said += [f"{key}: {len(ks)} descriptions of an object that is no safetensors file" for key, ks in by_object.items()]
    return tuple(said), seen


def reached(seen: Counter) -> dict[str, int]:
    """The cases a comparison saw, with how many of each."""
    return {k: v for k, v in seen.items() if v}


@pytest.mark.parametrize("path", [*FILES, *STORES], ids=[p.name for p in [*FILES, *STORES]])
def test_safetensors_tensors_are_described_as_the_library_reads_them(path):
    """Every tensor of the file, or of the store's shards, is described as the library reads it."""
    said, seen = compared(path)
    assert said == ()
    assert seen["files the library reads"] >= 1


def test_the_fixtures_hold_every_case_the_comparison_reads():
    """The fixtures reach each case of the comparison, so that none of it passes for want of a
    file. The file the library refuses is the shard of `st_store_mixed` whose data is shorter than
    its shape, which the profile does not present either."""
    seen: Counter = Counter()
    for path in [*FILES, *STORES]:
        seen += compared(path)[1]
    assert reached(seen) == {
        "files the library reads": 18,
        "files the library refuses": 1,
        "files of no tensors": 2,
        "shards keyed by their object": 1,
        "tensors": 52,
        "tensors of whole bytes": 47,
        "tensors of 4 bits, packed": 3,
        "tensors of 6 bits, packed": 2,
        "tensors of no elements, sized by the library's table": 1,
        "tensors of no bytes": 1,
        "scalars": 1,
        "rows": 51,
        "rows of a range of their object": 1,
        "packed elements decoded": 2200918,
    }


# ---- tensors no fixture holds, in files written here


def written(path: Path, header: str, buffer: bytes) -> Path:
    """A safetensors file of the header's text and the buffer."""
    raw = header.encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<Q", len(raw)) + raw + buffer)
    return path


def test_tensors_no_fixture_holds_are_described_as_the_library_reads_them(tmp_path):
    """Headers the fixtures do not have: a `dtype` written with escapes, under a name written with
    escapes, after other members, one of which holds a `dtype` of its own; a tensor name of several
    bytes a character before it; `__metadata__` between the tensors, with a member `dtype`; a
    scalar of one byte and one of a complex number; a tensor of five axes; tensors of no elements,
    of whole bytes and of 4 bits; a packed tensor that is not the buffer's first; and, in a store, a
    shard keyed by its object (its key is reserved) that holds packed tensors and one of no bytes."""
    rng = np.random.default_rng(0)
    header = (
        '{ "π/w" : {"note":{"dtype":"I64","shape":[9]},"shape":[2,1,3,1,1],'
        '"d\\u0074ype" : "\\u0046\\u0031\\u0036" ,"data_offsets":[11,23]},\n'
        ' "__metadata__":{"dtype":"U8"},'
        '"c":{"dtype":"C64","shape":[],"data_offsets":[3,11]},'
        '"f4":{"dtype":"F4","shape":[6],"data_offsets":[0,3]},'
        '"e":{"shape":[2,2],"dtype":"F6_E3M2","data_offsets":[23,26]},'
        '"z":{"dtype":"BOOL","shape":[3,0,2],"data_offsets":[26,26]},'
        '"z4":{"dtype":"F4","shape":[0],"data_offsets":[26,26]},'
        '"b":{"dtype":"BOOL","shape":[],"data_offsets":[26,27]} }  '
    )
    said, seen = compared(written(tmp_path / "hand.safetensors", header, rng.bytes(26) + b"\x01"))
    assert said == ()
    assert reached(seen) == {
        "files the library reads": 1,
        "tensors": 7,
        "tensors of whole bytes": 4,
        "tensors of 4 bits, packed": 2,
        "tensors of 6 bits, packed": 1,
        "tensors of no elements, sized by the library's table": 2,
        "tensors of no bytes": 2,
        "scalars": 2,
        "rows": 5,
        "packed elements decoded": 10,
    }
    shard = (
        '{"f4":{"dtype":"F4","shape":[3,2],"data_offsets":[0,3]},'
        '"none":{"dtype":"F6_E2M3","shape":[0,4],"data_offsets":[3,3]},'
        '"e":{"dtype":"F6_E2M3","shape":[4],"data_offsets":[3,6]}}'
    )
    store = tmp_path / "store"
    written(store / "refkv_source" / "x.safetensors", shard, rng.bytes(6))
    written(store / "a" / "model.safetensors", '{"w":{"dtype":"U16","shape":[2],"data_offsets":[0,4]}}', rng.bytes(4))
    said, seen = compared(store)
    assert said == ()
    assert reached(seen) == {
        "files the library reads": 2,
        "shards keyed by their object": 1,
        "tensors": 4,
        "tensors of whole bytes": 1,
        "tensors of 4 bits, packed": 1,
        "tensors of 6 bits, packed": 2,
        "tensors of no elements, sized by the library's table": 1,
        "tensors of no bytes": 1,
        "rows": 3,
        "rows of a range of their object": 2,
        "packed elements decoded": 10,
    }


# ---- the descriptions' limit (spec/stored-array.md §6
# <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#6-in-the-ir>), the one fallback a
# safetensors file reaches: every tensor of a checked header is one unit, and the chunk map's limit
# has room for a row of any tensor that holds a byte

RANK = (1 << 23) + 200


def wide(tmp_path: Path, room: int | None) -> tuple[Path, list[Tensor]]:
    """A file of a tensor `u` of two bytes and, after it in the header, a tensor `t` of 4-bit
    elements along RANK axes of length 0, with the library's reading of them. A description costs
    an entry for each axis, and a header 2 bytes for each, of which the limit counts a half: it is
    2^22 entries and one for every 4 bytes of the file. The header is padded with spaces so that the
    limit is `room` entries, or not at all for None."""
    header = ('{"u":{"dtype":"U8","shape":[2],"data_offsets":[0,2]},'
              '"t":{"dtype":"F4","shape":[' + ",".join("0" * RANK) + '],"data_offsets":[2,2]}}')
    if room is not None:
        padding = 4 * (room - (1 << 22)) - (8 + len(header) + 2)
        assert padding >= 0
        header += " " * padding
    path = written(tmp_path / "wide.safetensors", header, b"\x01\x02")
    tensors = reference(path.read_bytes())
    assert tensors is not None, "the library reads the file"
    assert [(t.name, t.dtype, len(t.shape), len(t.data)) for t in tensors] == [("u", "U8", 1, 2), ("t", "F4", RANK, 0)]
    return path, tensors


def parsed(path: Path) -> tuple[Stored, list[dict]]:
    """The stored arrays and the chunk map's rows of the file at `path`, which the parser reads
    and its profile does not project. (The document, of a shape of millions of axes, is not checked
    against the schema here: the fixtures' are.)"""
    core, _, _ = parse("safetensors", open_transport(str(path)))
    return Ir(core).stored_arrays(), pa.record_batch(core.chunk_map()).to_pylist()


def test_a_tensor_of_more_axes_than_the_limit_has_room_for_falls_back_alone(tmp_path):
    """Fallen back, the two descriptions cost RANK + 5 entries: each one itself, its axes and its
    declaration. `u`'s full description costs 2 more (its chain, and the chain in the table) and
    `t`'s 3 more (its chain, and in the table the chain and its codec). A limit of RANK + 8 has room
    for the first and not for the second, which keeps its shape, its sample size and its declaration,
    and has no grid, no chain and no row; the file is read all the same."""
    path, (u, t) = wide(tmp_path, RANK + 8)
    stored, rows = parsed(path)
    assert stored.undescribed is False
    first, second = stored.arrays
    assert (first.shape, first.item_bits, first.grid) == (u.shape, 8, Grid((Each(2),), "short"))
    assert (second.shape, second.item_bits, second.grid, second.chains) == (t.shape, 4, None, ())
    (place,) = second.declarations
    assert isinstance(place, ElementPlace)
    assert place.range == (t.declared[0] - 8, t.declared[1])
    assert (second.path, second.index, second.fill) == ("header", 1, "undefined")
    assert stored.chains == ((),)
    assert [r["array"] for r in rows] == [0]


def test_a_file_whose_descriptions_pass_the_limit_fallen_back_keeps_none(tmp_path):
    """Without the padding, the limit is below RANK + 5 entries, what the two descriptions cost
    fallen back: the file keeps no description, chain or row, and is marked `undescribed`, which a
    profile reads as a file to take as its bytes; it is not rejected."""
    path, _ = wide(tmp_path, None)
    assert (1 << 22) + path.stat().st_size // 4 < RANK + 5
    stored, rows = parsed(path)
    assert (stored.undescribed, stored.arrays, stored.chains, rows) == (True, (), (), [])
