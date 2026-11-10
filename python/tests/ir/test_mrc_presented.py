"""The MRC profile's arrays against the stored arrays' descriptions (spec/stored-array.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): what the profile
writes of a file's data, read off the file's description by the profile's rules
(spec/zarrify/mrc.md §4.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mrc.md#41-the-array>
and §8 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mrc.md#8-mrc-profile>), is what the
profile wrote. Of the file's one description the test derives the document of the image's array
`0`, and of the dataset layout's `dataset/data`, which is the same array, and every chunk of
each:

- the data type and the `bytes` codec's byte order, from the description's sample size and the
  fields its declarations name: the machine stamp, the mode and, for mode 0, the three words that
  give its sign (a gzipped file has no declarations, and they are read from the header its key,
  the stream, inflates to);
- `packbits` for a chain of `lsb_first`, of the description's bits;
- the chunk shape and each chunk's range, cutting the description's one unit as contiguous values
  (conventions §7 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays>),
  of whole bytes or, where the description's rows pack values of fewer bits, of those values or of
  its rows, with the zero padding of an edge chunk, a range of the zero data source or, past the
  payload a reference may have, a copy;
- of a chain that ends with `skip` and `gzip`, the `refkv.skip` and `gzip` codecs and one chunk,
  the unit.

Two things are read from outside the description. The kind of file, which says how the profile
arranges the sections (an image has none, and a volume stack has `mz` to a volume), is `ispg` and
`mz`: fields of the header, which the description's key holds and no declaration lists, since they
do not say what a value is. And the payload past which an edge chunk is copied is the run's
setting. Nothing is taken from the profile's output. The names of the dimensions and the dataset's
coordinate arrays are not storage, and are not compared."""

import gzip
import json
import math
import struct
from collections import Counter
from dataclasses import dataclass
from functools import cache
from itertools import product
from pathlib import Path
from shutil import copyfile

import numpy as np
import pytest
from described import Described, described
from ir_helpers import FIXTURES, core_setting
from test_mrc_stored import header

from refkv import _core
from refkv._narrow import JSON, at, mapping
from refkv.ir.stored import ElementPlace, StoredArray, Value
from refkv.zarrify.common import Output

FILES = sorted(p for p in (FIXTURES / "mrc").iterdir() if "reject" not in p.name)
HEADER = 1024
# where the header's fields the profile reads of the data are in it: (offset, length)
MZ, ISPG = (36, 4), (88, 4)
MACHST, MODE, NVERSION, IMOD_STAMP, IMOD_FLAGS = (212, 4), (12, 4), (108, 4), (152, 4), (156, 4)
IMOD = 1146047817
# the space group from which a file is a stack of volumes
VOLUME_STACK = 401
# the Zarr data type of each mode of whole bytes (spec/zarrify/mrc.md §3
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mrc.md#3-data-types>), with its bytes
PAIR: JSON = {"name": "struct", "configuration": {"fields": [{"name": "real", "data_type": "int16"},
                                                             {"name": "imag", "data_type": "int16"}]}}
DATA_TYPE: dict[int, tuple[JSON, int]] = {0: ("int8", 1), 1: ("int16", 2), 2: ("float32", 4), 3: (PAIR, 4),
                                          4: ("complex64", 8), 6: ("uint16", 2), 12: ("float16", 2)}
# the most bytes of a chunk, the padding of an edge chunk that is taken at once, the most padding
# of one, and the most of all of an array's (conventions §7
# <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays>)
LIMIT, SMALL, MOST, MOST_IN_ALL = 2**20, 2**10, 2**16 - 2**10, 2**20
# the most bytes of a reference's payload (spec/zarrify.md §1.2
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>)
PAYLOAD = 65519
# the dataset layout's coordinate arrays (spec/zarrify/mrc-dataset.md §3
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mrc-dataset.md#3-coordinates>)
COORDINATES = {"dataset/x/", "dataset/y/", "dataset/z/"}
# what the last four axes of an array hold
AXES = ("volumes", "sections", "rows", "columns")

type Cut = tuple[int, int, int]
"""a chunk's bytes: their offset in the file, their length, and the zero bytes that pad them"""


@dataclass(frozen=True)
class Want:
    """An array as the profile writes it of a description: the members of its document that say
    how it is stored, and its chunks by their coordinates."""

    shape: list[int]
    chunk_shape: list[int]
    data_type: JSON
    codecs: list[JSON]
    fill_value: JSON
    chunks: dict[tuple[int, ...], Cut]


def contiguous(shape: list[int], size: int) -> tuple[list[int], dict[tuple[int, ...], Cut]]:
    """Contiguous values of `shape`, of `size` bytes each, cut into chunks of at most LIMIT bytes
    (conventions §7 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays>):
    the chunk shape, and each chunk's offset in the values, its length and its padding."""
    total = size * math.prod(shape)
    last = len(shape) - 1
    axis, length = last, 0
    for j, n in enumerate(shape):
        slab = size * math.prod(shape[j + 1:])
        if slab > LIMIT:
            continue
        least = math.ceil(n / (LIMIT // slab))
        best: tuple[int, int] | None = None
        for count in range(least, min(2 * least, n) + 1):
            c = math.ceil(n / count)
            padding = (math.ceil(n / c) * c - n) * slab
            if best is None or padding < best[0]:
                best = (padding, c)
            if padding <= SMALL:
                break
        assert best is not None
        fits = best[0] <= MOST and best[0] * math.prod(shape[:j]) <= max(MOST_IN_ALL, total // 64)
        if fits or j == last:
            axis, length = j, best[1] if fits else math.ceil(n / least)
            break
    n, slab = shape[axis], size * math.prod(shape[axis + 1:])
    chunks: dict[tuple[int, ...], Cut] = {}
    for base, outer in enumerate(product(*(range(m) for m in shape[:axis]))):
        for q in range(math.ceil(n / length)):
            m = min(length, n - q * length)
            coords = (*outer, q, *[0] * (last - axis))
            chunks[coords] = ((base * n + q * length) * slab, m * slab, (length - m) * slab)
    return [*[1] * axis, length, *shape[axis + 1:]], chunks


def packed(shape: list[int], bits: int) -> tuple[list[int], dict[tuple[int, ...], Cut], bool]:
    """Rows of values of `bits` bits, each row ending on a byte, cut into chunks
    (spec/zarrify/mrc.md §4.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mrc.md#41-the-array>):
    as values of one byte where the rows are one run of bits and every chunk starts on a byte,
    each chunk then the bytes of its values; else a row a chunk, which the third value says."""
    columns = shape[-1]
    chunk_shape, by_value = contiguous(shape, 1)
    if columns * bits % 8 == 0 and all(start * bits % 8 == 0 for start, _, _ in by_value.values()):
        whole = math.ceil(math.prod(chunk_shape) * bits / 8)
        return chunk_shape, {c: (start * bits // 8, math.ceil(n * bits / 8), whole - math.ceil(n * bits / 8))
                             for c, (start, n, _) in by_value.items()}, False
    row = math.ceil(columns * bits / 8)
    rows = product(*(range(m) for m in shape[:-1]))
    return [*[1] * (len(shape) - 1), columns], {(*c, 0): (k * row, row, 0) for k, c in enumerate(rows)}, True


def varint(n: int) -> int:
    """The bytes of a protobuf varint."""
    return max(1, math.ceil(n.bit_length() / 7))


def payload(offset: int, length: int, padding: int) -> int:
    """The bytes of the payload of a reference to a range of the file and a literal of `padding`
    zero bytes (spec/zarrify.md §1.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>):
    a `Concat` of the two `Range` messages."""
    source = sum(1 + varint(v) for v in (offset, length) if v)
    literal = 1 + varint(padding) + padding
    return sum(1 + varint(r) + r for r in (source, literal))


def held_header(d: Described, a: StoredArray) -> bytes:
    """The file's header: the description's key, or, of a gzipped file, whose key is the stream,
    the first bytes the stream inflates to."""
    (chain,) = a.chains
    key = d.place(ElementPlace(a.element, None, None))
    if [c.name for c in d.stored.chains[chain]][-1:] == ["gzip"]:
        return gzip.decompress(key)[:HEADER]
    return key


def derived(d: Described, k: int, seen: Counter) -> Want | str:
    """The array the profile's rules give of description `k`, or why they give none."""
    a = d.stored.arrays[k]
    rows = d.rows.get(k, [])
    if a.grid is None or a.shape is None or a.item_bits is None or len(a.chains) != 1 or len(rows) != 1:
        return "a description of no grid, or not of one unit"
    head = held_header(d, a)
    codecs_of = list(d.stored.chains[a.chains[0]])
    gzipped = [c.name for c in codecs_of[-2:]] == ["skip", "gzip"]
    stored = codecs_of[:-2] if gzipped else codecs_of
    # the fields that say what a value is: the declarations, or, of a gzipped file, which has
    # none, the header its stream holds
    places = [MACHST, MODE, NVERSION, IMOD_STAMP, IMOD_FLAGS]
    if gzipped:
        if a.declarations:
            return f"declarations {a.declarations} of a gzipped file"
        declared = [head[o:o + n] for o, n in places]
    else:
        declared = [d.place(p) for p in a.declarations]
        if [p.range for p in a.declarations if isinstance(p, ElementPlace)] != places[:len(declared)]:
            return f"declarations {a.declarations}, not the fields the profile reads"
    order = {0x44: "<", 0x11: ">"}[declared[0][0]]
    (mode,) = struct.unpack(order + "i", declared[1])
    (mz,), (ispg,) = (struct.unpack(order + "i", head[o:o + n]) for o, n in (MZ, ISPG))
    nz, ny, nx = a.shape
    # the kind of file: a stack of volumes, an image, or sections
    if ispg >= VOLUME_STACK:
        seen["volume stacks, of four axes"] += 1
        shape = [nz // mz, mz, ny, nx]
    elif ispg == 0 and nz == 1:
        seen["images, of two axes"] += 1
        shape = [ny, nx]
    else:
        shape = [nz, ny, nx]
    packed_bits = a.item_bits if a.item_bits % 8 else None
    if packed_bits is not None:
        # values of fewer bits than a byte, packed from the least significant bit: `packbits`
        if [(c.name, c.parameters) for c in stored] != [("lsb_first", {"bits": Value(packed_bits)})] or (
                a.pad_from != len(a.shape) - 1):
            return f"{packed_bits} bits in the chain {stored}, padded from {a.pad_from}"
        data_type: JSON = "uint8"
        size = 1
        configuration: JSON = {"padding_encoding": "none", "last_bit": packed_bits - 1}
        codecs: list[JSON] = [{"name": "packbits", "configuration": configuration}]
    else:
        data_type, size = DATA_TYPE[mode]
        if stored or size * 8 != a.item_bits:
            return f"mode {mode} of {a.item_bits} bits in the chain {stored}"
        if mode == 0 and len(declared) == len(places):
            # IMOD's bytes are unsigned unless its flags or a standard version say they are signed
            (version,), (stamp,), (flags,) = (struct.unpack(order + "i", b) for b in declared[2:])
            if stamp == IMOD and not flags & 1 and not 20140 <= version < 21000:
                seen["files of IMOD's unsigned bytes"] += 1
                data_type = "uint8"
        endian = {"<": "little", ">": "big"}[order]
        codecs = [{"name": "bytes"} if size == 1 else {"name": "bytes", "configuration": {"endian": endian}}]
        seen["big-endian files of values of several bytes"] += order == ">" and size > 1
    fill: JSON = [0, 0] if data_type == "complex64" else {"real": 0, "imag": 0} if data_type == PAIR else 0
    ((_, start, length),) = d.parts(rows[0])
    if gzipped:
        # one chunk of the whole array, the stream
        seen["gzipped files, one chunk the stream"] += 1
        skip = codecs_of[-2].parameters.get("length")
        if not isinstance(skip, Value) or codecs_of[-2].parameters.keys() != {"length"} or codecs_of[-1].parameters:
            return f"the chain {codecs_of}"
        skipped: JSON = {"name": "refkv.skip", "configuration": {"length": skip.value}}
        codecs = [*codecs, skipped, {"name": "gzip", "configuration": {"level": 6}}]
        return Want(shape, shape, data_type, codecs, fill, {(0,) * len(shape): (start, length, 0)})
    # the unit's bytes, cut
    by_row = False
    if packed_bits is None:
        chunk_shape, cut = contiguous(shape, size)
    else:
        chunk_shape, cut, by_row = packed(shape, packed_bits)
        seen["files of packed values"] += 1
    if length != sum(n for _, n, _ in cut.values()):
        return f"a unit of {length} bytes, and chunks of {sum(n for _, n, _ in cut.values())}"
    # the cut axis: the last a chunk is shorter along
    cut_at = max((j for j, (c, n) in enumerate(zip(chunk_shape, shape, strict=True)) if c != n), default=None)
    if by_row:
        seen["files of packed values, a row a chunk"] += 1
    else:
        along = "in one chunk" if cut_at is None else f"cut along their {AXES[cut_at - len(shape)]}"
        seen[f"files {along}"] += 1
    return Want(shape, chunk_shape, data_type, codecs, fill, {c: (start + o, n, p) for c, (o, n, p) in cut.items()})


def check_array(d: Described, out: Output, prefix: str, want: Want, threshold: int, seen: Counter) -> list[str]:
    """The array at `prefix` of the output against what the description gives."""
    doc = mapping(json.loads(out.bytes_entries[f"{prefix}zarr.json"]))
    said = []
    wrote = {"shape": doc["shape"], "chunk_shape": at(doc["chunk_grid"], "configuration", "chunk_shape"),
             "data_type": doc["data_type"], "codecs": doc["codecs"], "fill_value": doc["fill_value"]}
    gave = {"shape": want.shape, "chunk_shape": want.chunk_shape, "data_type": want.data_type, "codecs": want.codecs,
            "fill_value": want.fill_value}
    said += [f"{name} {wrote[name]}; the description gives {gave[name]}" for name in gave if wrote[name] != gave[name]]
    if at(doc["chunk_grid"], "name") != "regular":
        said.append(f"a chunk grid {doc['chunk_grid']}")
    keys = {key for key in (*out.refs, *out.bytes_entries) if key.startswith(f"{prefix}c/")}
    wanted = {f"{prefix}c/{'/'.join(map(str, c))}": cut for c, cut in want.chunks.items()}
    if keys != set(wanted):
        said.append(f"chunks at {sorted(keys - set(wanted))[:4]} alone, and none at {sorted(set(wanted) - keys)[:4]}")
    data = d.objects[None]
    for key, (offset, length, padding) in sorted(wanted.items()):
        if key not in keys:
            continue
        seen["chunks"] += 1
        if padding and payload(offset, length, padding) > threshold:
            # copied, with its padding
            seen["chunks copied with their padding"] += 1
            if out.bytes_entries.get(key) != data[offset:offset + length] + bytes(padding):
                said.append(f"chunk {key} is not a copy of ({offset}, {length}) and {padding} zero bytes")
            continue
        seen["chunks padded from the zero data source"] += padding > 0
        parts = [(offset, length), (1, 0, padding)] if padding else [(offset, length)]
        if out.refs.get(key) != parts:
            said.append(f"chunk {key} is {out.refs.get(key)}, not {parts}")
    return said


@cache
def compared(path: Path, threshold: int = PAYLOAD) -> tuple[tuple[str, ...], Counter]:
    """What differs between the arrays the profile wrote of the file at `path` and the file's
    description, and what the comparison saw; `threshold` is the run's payload limit."""
    d = described(path)
    out = d.out
    assert isinstance(out, Output), "an MRC file is one file"
    said: list[str] = []
    seen: Counter = Counter()
    if d.stored.undescribed:
        said.append("descriptions dropped for room")
    if len(d.stored.arrays) != 1:
        return (*said, f"{len(d.stored.arrays)} descriptions of one file's data"), seen
    want = derived(d, 0, seen)
    arrays = sorted(key[:-len("zarr.json")] for key, v in out.bytes_entries.items()
                    if key.endswith("zarr.json") and not key.startswith("refkv_source/")
                    and json.loads(v)["node_type"] == "array")
    # the image's array and the dataset layout's data variable are the description's array; the
    # dataset's coordinates are the index along an axis, which no source bytes hold
    presented = ["0/", "dataset/data/"]
    coordinates = [p for p in arrays if p not in presented]
    seen["coordinate arrays, of no description"] += len(coordinates)
    if [p for p in arrays if p in presented] != presented or not set(coordinates) <= COORDINATES:
        said.append(f"arrays at {arrays}")
    if isinstance(want, str):
        return (*said, want), seen
    for prefix in presented:
        seen["arrays"] += 1
        said += [f"{prefix}: {s}" for s in check_array(d, out, prefix, want, threshold, seen)]
    # the zero data source holds as many bytes as the most padding a reference has
    padded = max((p for o, n, p in want.chunks.values() if p and payload(o, n, p) <= threshold), default=0)
    if out.data != ({bytes(padded): 1} if padded else {}):
        said.append(f"data sources of {sorted(len(b) for b in out.data)} bytes, and a padding of at most {padded}")
    return tuple(said), seen


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_the_mrc_profile_writes_what_the_description_gives(path):
    """The image's array and the dataset's data variable are what the file's description gives."""
    said, seen = compared(path)
    assert said == ()
    assert seen["arrays"] == 2


def test_the_fixtures_hold_every_case_the_profile_is_compared_in():
    """The fixtures reach each case of the comparison, so that none of it passes for want of a
    file. Each file's array is compared twice, as the image's and as the dataset's."""
    seen: Counter = Counter()
    for path in FILES:
        seen += compared(path)[1]
    assert {k: v for k, v in seen.items() if v} == {
        "arrays": 154,
        "coordinate arrays, of no description": 214,
        "images, of two axes": 7,
        "volume stacks, of four axes": 3,
        "big-endian files of values of several bytes": 6,
        "files of IMOD's unsigned bytes": 3,
        "files of packed values": 3,
        "files of packed values, a row a chunk": 1,
        "gzipped files, one chunk the stream": 4,
        "files in one chunk": 69,
        "files cut along their sections": 1,
        "files cut along their rows": 1,
        "files cut along their columns": 1,
        "chunks": 170,
        "chunks padded from the zero data source": 2,
    }


def test_arrays_no_fixture_holds_are_written_as_their_descriptions_give(tmp_path):
    """Layouts the fixtures do not have, each a file written here: two sections of 4-bit values,
    each cut by its values into chunks of rows, the last padded; 4-bit values in a row whose even
    cut would split a byte, which is one chunk; a row cut in two with a byte of padding, where
    three chunks would have none (a padding of at most 1024 bytes is taken at once); rows cut in
    three with no padding, where two chunks would have a row of it, of more than 1024 bytes; rows
    whose last chunk's padding, more than 1/64 of the array, is within the 2^20 bytes an array may
    have in all; and a gzipped file of IMOD's unsigned bytes with an extended header, whose data
    type is read from the stream."""
    rng = np.random.default_rng(0)

    def values(shape: tuple[int, int, int]) -> bytes:
        return rng.integers(0, 256, shape).astype("u1").tobytes()

    def half_bytes(shape: tuple[int, int, int]) -> bytes:
        return rng.integers(0, 256, (shape[0] * shape[1], (shape[2] + 1) // 2)).astype("u1").tobytes()

    extended = bytes(range(48))
    cases = {
        "int4_rows.mrc": header((2, 1021, 2048), 101) + half_bytes((2, 1021, 2048)),
        "int4_split.mrc": header((1, 1, 2**20 + 2), 101) + half_bytes((1, 1, 2**20 + 2)),
        "padding_taken_at_once.mrc": header((1, 1, 2**20 + 5), 0) + values((1, 1, 2**20 + 5)),
        "padding_avoided.mrc": header((1, 1023, 1040), 0) + values((1, 1023, 1040)),
        "padding_of_a_row.mrc": header((1, 17, 64000), 0) + values((1, 17, 64000)),
        "imod_extended.mrc.gz": gzip.compress(
            header((2, 3, 4), 0, extended=len(extended), imod=0, nversion=0) + extended + values((2, 3, 4)), mtime=0),
    }
    seen: Counter = Counter()
    for name, data in cases.items():
        path = tmp_path / name
        path.write_bytes(data)
        said, saw = compared(path)
        assert said == (), name
        seen += saw
    assert {k: v for k, v in seen.items() if v} == {
        "arrays": 12,
        "coordinate arrays, of no description": 18,
        "files of IMOD's unsigned bytes": 1,
        "files of packed values": 2,
        "files of packed values, a row a chunk": 1,
        "gzipped files, one chunk the stream": 1,
        "files cut along their rows": 3,
        "files cut along their columns": 1,
        "chunks": 26,
        "chunks padded from the zero data source": 8,
    }


def test_an_edge_chunk_past_the_payload_is_a_copy_of_what_the_description_gives(tmp_path):
    """An edge chunk whose reference, with its padding, would pass the payload a run allows is
    copied with its padding (spec/zarrify/mrc.md §8.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mrc.md#82-chunks>): the bytes the
    description's unit gives, and the zero bytes. No file reaches the limit, so the run is given
    one of 8 bytes."""
    path = tmp_path / "copied.mrc"
    copyfile(FIXTURES / "mrc" / "mrc_chunk_edge_literal.mrc", path)
    with core_setting(_core._set_copy_threshold, 8):
        said, seen = compared(path, 8)
    assert said == ()
    assert {k: v for k, v in seen.items() if v} == {
        "arrays": 2,
        "coordinate arrays, of no description": 2,
        "images, of two axes": 1,
        "files cut along their columns": 1,
        "chunks": 4,
        "chunks copied with their padding": 2,
    }
