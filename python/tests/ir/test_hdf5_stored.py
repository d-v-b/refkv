"""The stored arrays of HDF5 files (spec/stored-array.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>),
Imaris files among them, against h5py's reading of the same files: every dataset h5py reaches has
a description, found by its object header's address, with h5py's shape, item size, fill value,
filters and chunk shape; every chunk h5py finds stored has a row at its offset, of its size, with
its filter mask; and the rows' bytes, decoded by their chains, are the values h5py reads."""

import base64
import bz2
import contextlib
import json
import shutil
import struct
import zlib
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache, partial
from pathlib import Path

import h5py
import hdf5plugin  # importing it registers the third-party filters h5py reads the fixtures' data through
import imagecodecs
import jsonschema
import numpy as np
import pytest
from h5py import h5o  # pyrefly: ignore  (the stubs do not list the module)
from ir_helpers import FIXTURES, ROOT

from refkv.ir import Ir
from refkv.ir.kind import Kind
from refkv.ir.stored import (
    Codec,
    Each,
    ElementPlace,
    Given,
    Listed,
    Place,
    Source,
    Stored,
    StoredArray,
    Value,
)
from refkv.zarrify import _zarrify

pa = pytest.importorskip("pyarrow")

SCHEMA = json.loads((ROOT / "spec" / "stored-array.json").read_text())
FILES = sorted(
    p for d, suffixes in (("hdf5", (".h5", ".nwb")), ("ims", (".ims",)))
    for p in (FIXTURES / d).iterdir() if p.suffix in suffixes and "reject" not in p.name
)
# the codec of each registered filter id the IR names (spec/stored-array.md §3.2
# <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#32-the-list>)
NAMED = {1: "zlib", 2: "shuffle", 3: "h5_fletcher32", 4: "h5_szip", 5: "h5_nbit", 6: "h5_scaleoffset",
         307: "bzip2", 32000: "lzf", 32001: "blosc", 32004: "h5_lz4", 32008: "h5_bitshuffle", 32015: "zstd"}
# the client data values each named codec's parameters are places of: (first value, count), the
# count None for all of them
CLIENT = {
    "shuffle": {"element_size": (0, 1)},
    "h5_szip": {"options": (0, 1), "pixels_per_block": (1, 1), "bits_per_pixel": (2, 1), "pixels_per_scanline": (3, 1)},
    "h5_nbit": {"parameters": (0, None)},
    "h5_scaleoffset": {"parameters": (0, None)},
    "h5_bitshuffle": {"element_size": (2, 1), "block_size": (3, 1), "compression": (4, 1)},
}
# a grid of more positions than this is checked at its rows' positions alone
EVERY_POSITION = 200_000
# HDF5 2.0.0 gives another chunk than the one stored at a position of an extensible array whose
# unlimited axis is not its first. Of such a dataset's chunks only their number and their values
# are compared: every row's bytes still decode to the values h5py reads at its position
MISREAD_BY_HDF5 = {("ims_latest_indexes.ims", "Indexes/ea_last")}


@dataclass(frozen=True)
class Described:
    """A file's IR, its stored arrays, its chunk map's rows by array, and its objects' bytes by key
    (a file IR's one object under the key None)."""

    ir: Ir
    stored: Stored
    rows: dict[int, list[dict]]
    objects: dict[str | None, bytes]

    def key(self, element: int) -> str | None:
        """The key of the object `element` is in."""
        while element and self.ir.kind(element) != Kind.OBJECT:
            element = self.ir.parent(element)
        return self.ir.name(element) if element else None

    def at(self, element: int, within: tuple[int, int] | None = None) -> tuple[str | None, int, int]:
        """(object key, offset, length) of an element's extent, or of a range of it."""
        start, length = self.ir.extent(element)
        if within is not None:
            start, length = start + within[0], within[1]
        return self.key(element), start, length

    def place(self, p: Place) -> bytes:
        """The bytes at a place."""
        if isinstance(p, ElementPlace):
            key, start, length = self.at(p.element, p.range)
        else:
            key, start, length = p.object, p.offset, p.length
        return self.objects[key][start:start + length]

    def parts(self, row: dict) -> list[tuple[str | None, int, int]]:
        """A row's unit: the ranges of objects that, joined, are its stored bytes."""
        if row["element"] is None:
            return [(r["object"], r["offset"], r["length"]) for r in row["ranges"]]
        r = row["range"]
        return [self.at(row["element"], None if r is None else (r["offset"], r["length"]))]

    def unit(self, row: dict) -> bytes:
        return b"".join(self.objects[k][o:o + n] for k, o, n in self.parts(row))


@cache
def described(path: Path) -> Described:
    """The IR of the file at `path`, read with the files beside it that it names (an external
    file, an external link's target), its descriptions' document checked against the schema."""
    _, out = _zarrify(str(path), scope=str(path.parent))
    core = out.ir
    assert core is not None
    jsonschema.validate(json.loads(core.stored_arrays()), SCHEMA)
    ir = Ir(core)
    rows: dict[int, list[dict]] = {}
    for r in pa.record_batch(core.chunk_map()).to_pylist():
        rows.setdefault(r["array"], []).append(r)
    keys = [ir.name(c) for c in ir.children(0) if ir.kind(c) == Kind.OBJECT]
    objects: dict[str | None, bytes] = {k: (path.parent / k).read_bytes() for k in keys} or {None: path.read_bytes()}
    return Described(ir, ir.stored_arrays(), rows, objects)


def u32s(b: bytes) -> list[int]:
    return list(struct.unpack(f"<{len(b) // 4}I", b))


def unshuffled(b: bytes, size: int) -> bytes:
    """HDF5's shuffle filter undone: the bytes of `size`-byte elements, stored byte plane by byte
    plane, with the bytes past the last whole element as they are."""
    n = len(b) // size * size
    return np.frombuffer(b[:n], "u1").reshape(size, -1).T.tobytes() + b[n:]


def lz4_blocks(b: bytes) -> bytes:
    """The HDF5 LZ4 filter's stream decoded: the decoded size and the block size, big-endian, then
    each block's stored size and bytes, a block of its decoded size stored as it is."""
    total, block = struct.unpack_from(">QI", b)
    out, at = [], 12
    while (left := total - sum(map(len, out))) > 0:
        (n,) = struct.unpack_from(">I", b, at)
        want = min(block, left)
        out.append(b[at + 4:at + 4 + n] if n == want else imagecodecs.lz4_decode(b[at + 4:at + 4 + n], out=want))
        at += 4 + n
    return b"".join(out)


# each codec the test decodes a unit through: (the unit's bytes, the codec's parameters as bytes)
DECODERS: dict[str, Callable[[bytes, dict[str, bytes]], bytes]] = {
    "zlib": lambda b, _: zlib.decompress(b),
    "shuffle": lambda b, p: unshuffled(b, u32s(p["element_size"])[0]),
    "h5_fletcher32": lambda b, _: b[:-4],
    "bzip2": lambda b, _: bz2.decompress(b),
    "lzf": lambda b, _: bytes(imagecodecs.lzf_decode(b)),
    "blosc": lambda b, _: bytes(imagecodecs.blosc_decode(b)),
    "zstd": lambda b, _: bytes(imagecodecs.zstd_decode(b)),
    "h5_lz4": lambda b, _: lz4_blocks(b),
}


def decoded(d: Described, chain: tuple[Codec, ...], row: dict) -> bytes | None:
    """A row's unit decoded by its chain, last codec first, the codecs its `skips` name left out;
    None where the chain has a codec the test has no decoder for."""
    b = d.unit(row)
    skips = row["skips"] or 0
    for i in reversed(range(len(chain))):
        if skips >> i & 1:
            continue
        decode = DECODERS.get(chain[i].name)
        if decode is None:
            return None
        params = {k: d.place(v.place) for k, v in chain[i].parameters.items() if isinstance(v, Source)}
        b = decode(b, params)
    return b


def variable(dtype: np.dtype) -> bool:
    """Whether h5py reads elements of `dtype` through references the file's bytes do not hold as
    they are (variable-length data, object and region references)."""
    return dtype.hasobject or h5py.check_vlen_dtype(dtype) is not None


def check_filters(d: Described, a: StoredArray, plist, said: list[str], seen: Counter) -> None:
    """The array's one chain is the dataset's filter pipeline: each filter's codec, by its id, in
    order, with its client data values where the codec's parameters are places of them."""
    (c,) = a.chains
    chain = d.stored.chains[c]
    filters = [plist.get_filter(i) for i in range(plist.get_nfilters())]
    if len(chain) != len(filters):
        said.append(f"{len(chain)} codecs, h5py {len(filters)} filters")
        return
    for codec, (code, _, client, name) in zip(chain, filters, strict=True):
        seen[f"codec {codec.name}"] += 1
        values = list(client)
        params = dict(codec.parameters)
        if codec.name == "source":
            if params.pop("format") != Value("hdf5") or params.pop("id") != Value(code):
                said.append(f"a source codec of {codec.parameters}, h5py filter {code}")
            if "name" in params and params.pop("name") != Value(name.decode()):
                said.append(f"a source codec named {codec.parameters['name']}, h5py {name!r}")
            given = params.pop("parameters", None)
            if (u32s(d.place(given.place)) if isinstance(given, Source) else []) != values:
                said.append(f"filter {code}'s parameters are not h5py's {values}")
        elif NAMED.get(code) != codec.name:
            said.append(f"codec {codec.name}, h5py filter {code}")
        else:
            for k, (first, count) in CLIENT.get(codec.name, {}).items():
                want = values[first:] if count is None else values[first:first + count]
                p = params.pop(k, None)
                if (u32s(d.place(p.place)) if isinstance(p, Source) else []) != want:
                    said.append(f"{codec.name}'s {k} is not h5py's {want}")
        if params:
            said.append(f"{codec.name} with parameters h5py does not give: {sorted(params)}")


def check_chunks(d: Described, a: StoredArray, rows: list[dict], dset: h5py.Dataset, misread: bool,
                 said: list[str], seen: Counter) -> None:
    """The grid is the dataset's chunk shape, whole at the edges, and the rows are the chunks h5py
    finds stored: each at its offset, of its size, skipping the filters its mask names, and
    decoding to the values h5py reads in its region."""
    chunk = dset.chunks
    if a.grid is None or a.grid.edge != "full" or a.grid.axes != tuple(Each(c) for c in chunk):
        said.append(f"grid {a.grid}, h5py chunks {chunk}")
        return
    got = {tuple(r["coords"]): r for r in rows}
    if len(got) != len(rows):
        said.append("two rows at one position")
    if len(rows) != dset.id.get_num_chunks():
        said.append(f"{len(rows)} rows, h5py {dset.id.get_num_chunks()} chunks")
    counts = [-(-s // c) for s, c in zip(dset.shape, chunk, strict=True)]
    every = int(np.prod(counts, dtype=object)) <= EVERY_POSITION
    chain = d.stored.chains[a.chains[0]]
    for coords in np.ndindex(*counts) if every else sorted(got):
        info = dset.id.get_chunk_info_by_coord(tuple(c * k for c, k in zip(coords, chunk, strict=True)))
        row = got.get(coords)
        if info.byte_offset is None or row is None:
            if (info.byte_offset is None) != (row is None) and not misread:
                ours, theirs = ("no row", "stores") if row is None else ("a row", "does not store")
                said.append(f"chunk {coords}: {ours}, h5py {theirs} it")
            continue
        seen["chunks"] += 1
        region = tuple(slice(c * k, min((c + 1) * k, n)) for c, k, n in zip(coords, chunk, dset.shape, strict=True))
        # a dataset may say that a chunk the array's edge cuts is stored unfiltered, with no bit of
        # its mask set (h5py does not give that option): such a row skips every codec, and its
        # bytes, as they are, are compared with the values h5py reads
        codecs = (1 << len(chain)) - 1
        cut = any(r.stop - r.start < k for r, k in zip(region, chunk, strict=True))
        raw_edge = bool(cut and codecs and row["skips"] == codecs and not info.filter_mask)
        mask = codecs if raw_edge else info.filter_mask & codecs
        seen["edge chunks stored unfiltered"] += raw_edge
        seen[f"rows of {'ranges' if row['element'] is None else 'a range' if row['range'] else 'an element'}"] += 1
        parts = d.parts(row)
        if not misread:
            if len(parts) != 1 or parts[0][1:] != (info.byte_offset, info.size):
                said.append(f"chunk {coords} at {parts}, h5py {info.byte_offset} of {info.size}")
            if (row["skips"] or 0) != mask:
                said.append(f"chunk {coords} skips {row['skips']}, h5py's mask {info.filter_mask:#x}")
        seen["chunks skipping a filter"] += bool(row["skips"])
        if variable(dset.dtype):
            continue
        try:
            theirs = np.ascontiguousarray(dset[region])
        except OSError:
            # h5py reads no values to compare (a chunk whose mask leaves on a filter its bytes are
            # not the output of); the chunk's place and mask were compared above
            seen["chunks h5py cannot read"] += 1
            continue
        b = decoded(d, chain, row)
        if b is None:
            seen["chunks not decoded"] += 1
            continue
        size = int(np.prod(chunk)) * dset.dtype.itemsize
        if len(b) > size:
            said.append(f"chunk {coords} decodes to {len(b)} bytes, more than a chunk's {size}")
            continue
        # a chunk stored unfiltered may be short of a chunk's size: the elements it holds are
        # compared, and not the rest, which the library reads as what its buffer held (zeros on
        # one platform, other values on another)
        if len(b) < size and (row["skips"] or 0) != codecs:
            said.append(f"chunk {coords} decodes to {len(b)} bytes, fewer than a chunk's {size}")
            continue
        seen["chunks stored short"] += len(b) < size
        within = tuple(slice(0, r.stop - r.start) for r in region)
        held = (np.arange(int(np.prod(chunk))) < len(b) // dset.dtype.itemsize).reshape(chunk)[within]
        mine = np.frombuffer(b.ljust(size, b"\0"), dtype=dset.dtype).reshape(chunk)[within]
        if mine[held].tobytes() != theirs[held].tobytes():
            said.append(f"chunk {coords} decodes to other values than h5py reads")
        seen["chunks decoded"] += 1


def external_parts(plist, stored: int) -> list[tuple[str, int, int]]:
    """(file name, offset, length) of each external file's part of a dataset of `stored` bytes: its
    slot, in order, as much of it as the data needs."""
    parts, left = [], stored
    for i in range(plist.get_external_count()):
        name, offset, size = plist.get_external(i)
        n = min(left, size)
        if n:
            parts.append((Path(name.decode()).name, offset, n))
        left -= n
    return parts


def check_whole(d: Described, a: StoredArray, rows: list[dict], dset: h5py.Dataset, plist, size: int,
                said: list[str], seen: Counter) -> None:
    """Contiguous, external and compact data are one unit, the whole array, stored as it is: a row
    of the bytes h5py reads (an external dataset's, the ranges of its files, in order), and none
    where the file allocates no space for them."""
    axes = tuple(Each(n) if n else Listed(()) for n in dset.shape)
    if a.grid is None or a.grid.edge != "short" or a.grid.axes != axes or d.stored.chains[a.chains[0]] != ():
        said.append(f"grid {a.grid} of chain {a.chains} for data of shape {dset.shape} stored whole")
    stored = (dset.size or 0) * size
    external = external_parts(plist, stored)
    layout = ("external" if plist.get_external_count() else
              "compact" if plist.get_layout() == h5py.h5d.COMPACT else "contiguous")
    if layout == "contiguous" and dset.id.get_offset() is None:
        seen["unallocated"] += 1
        if rows:
            said.append("rows for data with no address")
        return
    if stored == 0:
        seen[f"empty {layout}"] += 1
        if rows:
            said.append("rows for data of no bytes")
        return
    if len(rows) != 1 or rows[0]["coords"] != [0] * len(dset.shape) or rows[0]["skips"]:
        said.append(f"{layout} data's rows {rows}")
        return
    seen[layout] += 1
    parts = d.parts(rows[0])
    if layout == "external":
        if parts != external:
            said.append(f"external data at {parts}, h5py {external}")
    # h5py gives the data's offset, like a chunk's, from the file's start, a user block counted
    elif layout == "contiguous" and (len(parts) != 1 or parts[0][1:] != (dset.id.get_offset(), stored)):
        said.append(f"contiguous data at {parts}, h5py {dset.id.get_offset()} of {stored}")
    if not variable(dset.dtype) and d.unit(rows[0]) != np.ascontiguousarray(dset[()]).tobytes():
        said.append(f"{layout} data's bytes are not the values h5py reads")


def check_dataset(d: Described, k: int, dset: h5py.Dataset, misread: bool, seen: Counter) -> list[str]:
    """What the description of `dset`, array `k`, says other than h5py."""
    a = d.stored.arrays[k]
    rows = d.rows.get(k, [])
    said: list[str] = []
    plist = dset.id.get_create_plist()
    null = dset.id.get_space().get_simple_extent_type() == h5py.h5s.NULL
    if a.shape != (None if null else dset.shape) or a.axes is not None or a.index is not None:
        said.append(f"shape {a.shape}, axes {a.axes}, index {a.index}; h5py {dset.shape}")
    # the first declaration is the datatype message, which begins with its class and version and
    # holds the element's size in the file (h5py gives a variable-length type's size in memory)
    message = d.place(a.declarations[0]) if a.declarations else b""
    if len(message) < 8 or not 1 <= message[0] >> 4 <= 5:
        said.append("the first declaration is not a datatype message")
        return said
    size = dset.id.get_type().get_size() if not variable(dset.dtype) else u32s(message[4:8])[0]
    if a.item_bits != size * 8 or u32s(message[4:8])[0] != size or a.pad_from is not None:
        said.append(f"item_bits {a.item_bits}, the datatype message's size {u32s(message[4:8])[0]}; h5py {size} bytes")
    state = {h5py.h5d.FILL_VALUE_UNDEFINED: "undefined", h5py.h5d.FILL_VALUE_DEFAULT: "format_default",
             h5py.h5d.FILL_VALUE_USER_DEFINED: "given"}[plist.fill_value_defined()]
    seen[f"fill {state}"] += 1
    if (a.fill if isinstance(a.fill, str) else "given") != state:
        said.append(f"fill {a.fill}, h5py {state}")
    elif isinstance(a.fill, Given):
        p = a.fill.param
        value = d.place(p.place) if isinstance(p, Source) else base64.b64decode(str(p.value))
        if len(value) != size:
            said.append(f"a fill value of {len(value)} bytes for elements of {size}")
        # NumPy does not hold a string's or a compound's fill value as the file stores it: of those
        # only the length is compared
        elif not variable(dset.dtype) and dset.dtype.kind not in "SV":
            theirs = np.zeros((1,), dset.dtype)
            plist.get_fill_value(theirs)
            if value != theirs.tobytes():
                said.append(f"fill value {value.hex()}, h5py {theirs.tobytes().hex()}")
            seen["fill values compared"] += 1
    # every declaration past the first is a fill value message the description does not give
    if len(a.declarations) > 1 and state == "given" and isinstance(a.fill, Given):
        said.append(f"{len(a.declarations)} declarations beside a fill value given")
    layout = plist.get_layout()
    if null or layout == h5py.h5d.VIRTUAL:
        seen["null" if null else "virtual"] += 1
        if a.grid is not None or a.chains or rows:
            said.append(f"a grid, chain or rows for {'a null dataspace' if null else 'a virtual dataset'}")
        return said
    # an external file that is not beside the fixture, or ends before its part of the data does,
    # leaves its dataset's units unstated
    beside = Path(dset.file.filename).parent
    short = any(not (beside / name).exists() or (beside / name).stat().st_size < offset + n
                for name, offset, n in external_parts(plist, (dset.size or 0) * size))
    if a.grid is None or short:
        seen["external, a file absent or short"] += short
        if a.grid is not None or rows or not short:
            said.append("a grid or rows for data an external file does not hold" if short else "no grid")
        return said
    if a.unit_parameters:
        said.append(f"unit parameters {a.unit_parameters}")
    check_filters(d, a, plist, said, seen)
    if layout == h5py.h5d.CHUNKED:
        check_chunks(d, a, rows, dset, misread, said, seen)
    else:
        check_whole(d, a, rows, dset, plist, size, said, seen)
    return said


@cache
def compared(path: Path) -> tuple[tuple[str, ...], Counter]:
    """(what the descriptions of the file at `path`, and of each HDF5 file it names, say other than
    h5py; how many of each thing were compared)."""
    d = described(path)
    said: list[str] = []
    seen: Counter = Counter()
    with contextlib.chdir(path.parent):  # where the library looks for a dataset's external files
        compare(d, path, said, seen)
    return tuple(said), seen


def reach(reached: dict[int, tuple[str, h5py.Dataset]], name: str, o: h5py.HLObject) -> None:
    """Record a visited dataset under its object header's address, once."""
    if isinstance(o, h5py.Dataset):
        reached.setdefault(h5o.get_info(o.id).addr, (name, o))


def compare(d: Described, path: Path, said: list[str], seen: Counter) -> None:
    # a description's key is its object's struct, whose `prefix` child starts the object header
    by_header: dict[tuple[str | None, int], int] = {}
    for k, a in enumerate(d.stored.arrays):
        prefix = d.ir.child(a.element, "prefix")
        assert prefix is not None, a.path
        by_header[d.at(prefix)[:2]] = k
    assert len(by_header) == len(d.stored.arrays)
    matched = set()
    for key in d.objects:
        at = path if key is None else path.parent / key
        if not h5py.is_hdf5(at):
            continue
        with h5py.File(at, "r") as f:
            reached: dict[int, tuple[str, h5py.Dataset]] = {}
            f.visititems(partial(reach, reached))
            for address, (name, dset) in sorted(reached.items()):
                k = by_header.get((key, address + f.userblock_size))
                if k is None:
                    said.append(f"{at.name}:/{name}: no description of the object at {address}")
                    continue
                matched.add(k)
                seen["datasets"] += 1
                misread = (at.name, name) in MISREAD_BY_HDF5
                said += [f"{at.name}:/{name}: {s}" for s in check_dataset(d, k, dset, misread, seen)]
    # a description h5py reaches no dataset of would be of an object no link leads to, which h5py
    # reads nothing of to compare it with: no file here has one
    said += [f"{d.stored.arrays[k].path}: a description h5py reaches no dataset of"
             for k in sorted(set(range(len(d.stored.arrays))) - matched)]


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_hdf5_arrays_are_described_as_h5py_reads_them(path):
    """Every dataset h5py reaches in the file, and in each HDF5 file it names, is described as
    h5py reads it, and no object's descriptions were dropped for room."""
    said, seen = compared(path)
    assert said == ()
    assert seen["datasets"] > 0
    assert described(path).stored.undescribed in ((), False)


def test_the_fixtures_hold_every_case_the_comparison_reads():
    """The comparison above compares something of each kind: no branch of it passes for want of a
    fixture, but a row of ranges of the source, which only an IR out of room has."""
    seen: Counter = Counter()
    for path in FILES:
        seen += compared(path)[1]
    for case in ("datasets", "chunks", "chunks decoded", "chunks skipping a filter", "contiguous", "compact",
                 "external", "external, a file absent or short", "unallocated", "null", "virtual",
                 "fill format_default",
                 "fill given", "fill values compared", "rows of an element", "rows of a range",
                 "edge chunks stored unfiltered", "chunks stored short", "chunks h5py cannot read", "codec zlib",
                 "codec shuffle", "codec h5_fletcher32", "codec source", "codec lzf", "codec blosc", "codec zstd",
                 "codec h5_lz4", "codec h5_bitshuffle", "codec h5_scaleoffset"):
        assert seen[case] > 0, (case, dict(seen))


DATASPACE, FILL_VALUE, LAYOUT = 1, 5, 8  # the types of the dataspace, fill value and layout messages


def message(d: Described, a: StoredArray, kind: int) -> tuple[int, int]:
    """Where the header and the data of the one message of type `kind` start, in the file of `d`,
    of the dataset `a` describes, whose object header is of version 1 (no checksum covers it)."""
    messages = d.ir.child(a.element, "messages")
    assert messages is not None
    found = []
    for m in d.ir.children(messages):
        header, body = d.ir.child(m, "header"), d.ir.child(m, "data")
        assert header is not None
        assert body is not None
        if struct.unpack_from("<H", d.objects[None], d.ir.extent(header)[0])[0] == kind:
            found.append((d.ir.extent(header)[0], d.ir.extent(body)[0]))
    (at,) = found
    return at


def with_message(tmp_path: Path, name: str, kind: int, change: Callable[[bytearray, int, int], None]) -> Path:
    """A file of one dataset, `d`, which h5py wrote contiguous, with the format's default fill
    value, in a version 1 object header, after `change(file, header, data)` changed the bytes of
    its message of type `kind`, whose header and data start at those offsets."""
    path = tmp_path / "written.h5"
    with h5py.File(path, "w", libver="earliest") as f:
        f.create_dataset("d", data=np.arange(6, dtype="<i4"))
    d = described(path)
    (a,) = d.stored.arrays
    assert a.fill == "format_default"
    data = bytearray(path.read_bytes())
    change(data, *message(d, a, kind))
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_a_fill_value_message_that_defines_none_is_undefined(tmp_path):
    """A fill value message of version 2 whose `defined` byte is 0 defines no fill value, as the
    library reads it."""

    def undefine(data: bytearray, _: int, at: int) -> None:
        assert data[at] == 2
        assert data[at + 3] == 1
        data[at + 3] = 0

    path = with_message(tmp_path, "undefined.h5", FILL_VALUE, undefine)
    said, seen = compared(path)
    assert said == ()
    assert seen["fill undefined"] == 1


def test_a_dataset_with_no_fill_value_message_has_an_undefined_fill_value(tmp_path):
    """A dataset whose header has no fill value message of either type has no fill value, as the
    library reads it (the message here made a NIL message, which a reader passes over)."""

    def remove(data: bytearray, header: int, _: int) -> None:
        data[header:header + 2] = bytes(2)

    path = with_message(tmp_path, "none.h5", FILL_VALUE, remove)
    said, seen = compared(path)
    assert said == ()
    assert seen["fill undefined"] == 1


def test_data_stored_whole_that_is_not_of_the_arrays_size_has_no_grid(tmp_path):
    """A contiguous dataset whose layout message gives its data another length than its elements'
    bytes has no grid and no row: its bytes are not the array's."""

    def shorten(data: bytearray, _: int, at: int) -> None:
        assert bytes(data[at:at + 2]) == b"\x03\x01"  # version 3, contiguous: an address, then a size
        assert struct.unpack_from("<Q", data, at + 10)[0] == 24
        struct.pack_into("<Q", data, at + 10, 20)

    d = described(with_message(tmp_path, "short.h5", LAYOUT, shorten))
    (a,) = d.stored.arrays
    assert (a.shape, a.grid, a.chains, d.rows) == ((6,), None, (), {})


def at_address(d: Described, address: int) -> tuple[int, StoredArray]:
    """The description, with its position, of the dataset whose object header is at `address`."""
    found = []
    for k, a in enumerate(d.stored.arrays):
        prefix = d.ir.child(a.element, "prefix")
        assert prefix is not None
        if d.ir.extent(prefix)[0] == address:
            found.append((k, a))
    (one,) = found
    return one


def test_a_dataset_the_parser_did_not_read_has_no_shape_and_no_grid(tmp_path):
    """A dataset outside an IMS file's image whose dataspace does not read (the IMS parser rejects
    no file for one) has no shape, no grid and no row. It keeps the element size its own datatype
    message gives; where that message is shared, it has no element size and declares the shared
    message, not the committed datatype's, which the parser may not have read."""
    path = tmp_path / "written.ims"
    shutil.copy(FIXTURES / "ims" / "ims_earliest_review2.ims", path)
    with h5py.File(path, "r+", libver="earliest") as f:
        f["X/committed"] = np.dtype("<i4")
        f["X"].create_dataset("typed", data=np.arange(6), dtype=f["X/committed"])
        own, shared = (h5o.get_info(f[name].id).addr for name in ("X/target", "X/typed"))
    d = described(path)
    data = bytearray(path.read_bytes())
    declared = {}
    for address, shape, bits in ((own, (6, 10), 64), (shared, (6,), 32)):
        k, a = at_address(d, address)
        assert (a.shape, a.item_bits, a.grid is None, len(d.rows[k])) == (shape, bits, False, 1)
        declared[address] = a.declarations
        _, at = message(d, a, DATASPACE)
        assert data[at] == 1
        data[at] = 9
    path = tmp_path / "unread.ims"
    path.write_bytes(data)
    d = described(path)
    for address, bits in ((own, 64), (shared, None)):
        k, a = at_address(d, address)
        assert (a.shape, a.item_bits, a.grid, a.chains, d.rows.get(k)) == (None, bits, None, (), None)
        (place,) = a.declarations
        assert isinstance(place, ElementPlace)
        messages = d.ir.child(a.element, "messages")
        assert messages is not None
        # its own message, which a dataset the parser read declares only where it is not shared
        assert place.element in {d.ir.child(m, "data") for m in d.ir.children(messages)}
        assert (d.place(place) == d.place(declared[address][0])) == (address == own)


def test_filters_no_fixture_holds_are_described_as_h5py_reads_them(tmp_path):
    """The codecs of two filters no fixture's pipeline has, bzip2 and N-bit, are described with
    h5py's client data, and a bzip2 chunk decodes to the values h5py reads. (The library h5py
    ships has no SZIP filter to write a third with.)"""
    path = tmp_path / "filters.h5"
    values = np.arange(4096, dtype="<u2").reshape(64, 64)
    with h5py.File(path, "w") as f:
        f.create_dataset("bzip2", data=values, chunks=(32, 64), **hdf5plugin.BZip2())
        plist = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
        plist.set_chunk((32, 64))
        plist.set_filter(h5py.h5z.FILTER_NBIT)
        kind = h5py.h5t.STD_U16LE.copy()
        kind.set_precision(12)
        space = h5py.h5s.create_simple(values.shape)
        h5py.h5d.create(f.id, b"nbit", kind, space, plist).write(h5py.h5s.ALL, h5py.h5s.ALL, values)
    said, seen = compared(path)
    assert said == ()
    assert (seen["codec bzip2"], seen["codec h5_nbit"]) == (1, 1)
    assert seen["chunks decoded"] == 2
