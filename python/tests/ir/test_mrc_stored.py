"""The stored arrays of the MRC fixtures (spec/stored-array.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>), compared with
mrcfile, the reference reader. Every accepted file has one description, and of it:

- its key and its declarations are the header mrcfile reads, and the fields of it that say what a
  value is, byte for byte (a gzipped file's key is its stream, which inflates to that header, and
  it has no declarations, since no place names what a stream inflates to);
- its shape is mrcfile's `nz`, `ny` and `nx`, which mrcfile's own array, of two axes for an image
  and four for a volume stack, has the elements of;
- its sample size is that of mrcfile's data type, its grid one unit of the whole array, and its
  chain what mrcfile's opening of the file says: `skip` and `gzip` for a file mrcfile inflates,
  `skip`'s length the header's and the extended header's bytes;
- its one row is at mrcfile's offset, after the header and the extended header mrcfile reads, of
  as many bytes as mrcfile's array (a gzipped file's is the whole file);
- the row's bytes, decoded through the chain and read as elements of the description's size, in
  the byte order and of the kind the declarations give (the header's, of a gzipped file), are
  mrcfile's values.

mrcfile reads no values of mode 3 (complex int16) or mode 101 (4 bits): those arrays are compared
with the file's own bytes at mrcfile's offset, read as pairs of 16-bit integers, and as rows of
half bytes, the low half first (spec/zarrify/mrc.md §3
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mrc.md#3-data-types>). mrcfile also reads
every mode 0 file as signed: which sign a value has is not the description's to say, and the
declarations that decide it are compared as bytes.

A second test asserts which cases the fixtures reached, and a third compares files of cases no
fixture holds. No MRC array lacks a grid or falls back to `source`: the profile's checks
(spec/zarrify/mrc.md §8.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mrc.md#81-header>)
refuse a file whose data is not whole, so there is no test of a fallback."""

import gzip
import math
import struct
import warnings
import zlib
from collections import Counter
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import cast

import mrcfile
import numpy as np
import pytest
from described import Described, described
from ir_helpers import FIXTURES
from mrcfile.gzipmrcfile import GzipMrcFile

from refkv.ir.stored import Codec, Each, ElementPlace, StoredArray, Value

FILES = sorted(p for p in (FIXTURES / "mrc").iterdir() if "reject" not in p.name)
HEADER = 1024
# where the header's fields that say what a value is are in it: (offset, length)
MACHST, MODE, NVERSION, IMOD_STAMP, IMOD_FLAGS = (212, 4), (12, 4), (108, 4), (152, 4), (156, 4)
# `extra2`, which holds IMOD's stamp and flags, starts here
EXTRA2 = 112
# the kind and bytes of a value of each mode mrcfile reads, as numpy names them
KIND = {0: "i1", 1: "i2", 2: "f4", 4: "c8", 6: "u2", 12: "f2"}
# the bits of a value of each mode mrcfile does not read
UNREAD = {3: 32, 101: 4}


@dataclass(frozen=True)
class Read:
    """mrcfile's reading of a file: its header, the bytes of the extended header it read, its
    array (None for a mode it does not read), and whether it inflated the file."""

    header: np.recarray
    extended: int
    data: np.ndarray | None
    gzipped: bool


def read(path: Path) -> Read:
    """mrcfile's reading, permissive: it then reads the header of a mode it has no data type for,
    and warns of a file longer than its data."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with mrcfile.open(path, permissive=True) as f:
            # mrcfile is unannotated: its header is None only before a read
            header = cast("np.recarray", f.header).copy()
            extended = cast("np.ndarray", f.extended_header).nbytes
            data = None if f.data is None else np.array(f.data)
            return Read(header, extended, data, isinstance(f, GzipMrcFile))


def field(header: np.recarray, at: tuple[int, int]) -> bytes:
    """The bytes of mrcfile's header at a field's place."""
    return header.tobytes()[at[0]:at[0] + at[1]]


def runs(a: StoredArray) -> tuple[int, int]:
    """How a unit of the whole array packs: its runs of elements that end on a byte, and the
    elements of each (spec/stored-array.md §2
    <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#2-the-description>)."""
    assert a.shape is not None
    cut = 0 if a.pad_from is None else a.pad_from
    return math.prod(a.shape[:cut]), math.prod(a.shape[cut:])


def msb_first(b: bytes, bits: int, count: int, run: int) -> bytes:
    """`lsb_first`'s decoding of `count` runs of `run` elements of `bits` bits (spec/stored-array.md
    §3.2 <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#32-the-list>): bit k of a
    run is bit k mod 8 of its byte k // 8, from the least significant, and an element's first bit
    is its least significant; they are packed again most significant bit first."""
    stride = math.ceil(run * bits / 8)
    assert len(b) == count * stride, "runs that each end on a byte"
    stored = np.unpackbits(np.frombuffer(b, np.uint8).reshape(count, stride), axis=1, bitorder="little")
    each = stored[:, :run * bits].reshape(count, run, bits)[:, :, ::-1]
    return np.packbits(each.reshape(count, run * bits), axis=1, bitorder="big").tobytes()


def decoded(d: Described, a: StoredArray, row: dict) -> bytes:
    """A row's bytes taken through its chain, from its last codec to its first."""
    (chain,) = a.chains
    b = d.unit(row)
    for codec in reversed(d.stored.chains[chain]):
        values = {k: v.value for k, v in codec.parameters.items() if isinstance(v, Value)}
        if codec.name == "gzip":
            # every member of the stream, joined
            b = gzip.decompress(b)
        elif codec.name == "skip":
            b = b[cast("int", values["length"]):]
        elif codec.name == "lsb_first":
            b = msb_first(b, cast("int", values["bits"]), *runs(a))
        else:
            raise AssertionError(f"a codec the test does not decode: {codec.name}")
    return b


def packed_values(b: bytes, a: StoredArray) -> np.ndarray:
    """The elements of fewer bits than a byte that `b` holds as a description says they pack: most
    significant bit first, each run ending on a byte."""
    assert a.item_bits is not None
    count, run = runs(a)
    held = np.unpackbits(np.frombuffer(b, np.uint8).reshape(count, -1), axis=1, bitorder="big")
    each = held[:, :run * a.item_bits].reshape(count * run, a.item_bits)
    return each @ (1 << np.arange(a.item_bits - 1, -1, -1))


def half_bytes(b: bytes, shape: tuple[int, ...]) -> np.ndarray:
    """The values of mode 101 in the file's bytes `b`: a row's first value is the low half of its
    first byte, its second the high half, and a row of an odd count ends on a half byte of
    padding."""
    rows = np.frombuffer(b, np.uint8).reshape(math.prod(shape[:-1]), -1)
    values = np.empty((rows.shape[0], 2 * rows.shape[1]), np.uint8)
    values[:, ::2], values[:, 1::2] = rows & 15, rows >> 4
    return values[:, :shape[-1]].reshape(-1)


def check_declarations(d: Described, a: StoredArray, r: Read, seen: Counter) -> list[str]:
    """The key and the declarations against mrcfile's header."""
    said = []
    whole = (None, 0, len(d.objects[None]))
    if r.gzipped:
        # the stream, which holds the header and is the file: no place names the header
        seen["gzipped files, keyed by their stream"] += 1
        if a.path != "data" or d.at(a.element) != whole:
            said.append(f"a gzipped file's key is {a.path} at {d.at(a.element)}, not its stream")
        if a.declarations != ():
            said.append(f"a gzipped file's declarations are {a.declarations}, not none")
        if gzip.decompress(d.place(ElementPlace(a.element, None, None)))[:HEADER] != r.header.tobytes():
            said.append("the stream does not inflate to the header mrcfile reads")
        return said
    if a.path != "header" or d.at(a.element) != (None, 0, HEADER) or d.place(
            ElementPlace(a.element, None, None)) != r.header.tobytes():
        said.append(f"the key is {a.path} at {d.at(a.element)}, not the header mrcfile reads")
    fields = [MACHST, MODE]
    if int(r.header.mode) == 0:
        seen["mode 0 files, with the declarations of their sign"] += 1
        fields += [NVERSION, IMOD_STAMP, IMOD_FLAGS]
    if a.declarations != tuple(ElementPlace(a.element, None, f) for f in fields):
        return [*said, f"the declarations are {a.declarations}, not the header's ranges {fields}"]
    # each is the field as mrcfile holds it: its own reading of the stamp and flags is of `extra2`
    extra = r.header.extra2.tobytes()
    theirs = [r.header.machst.tobytes(), r.header.mode.tobytes(), r.header.nversion.tobytes(),
              extra[IMOD_STAMP[0] - EXTRA2:][:4], extra[IMOD_FLAGS[0] - EXTRA2:][:4]]
    if [d.place(p) for p in a.declarations] != theirs[:len(fields)]:
        said.append("the declarations are not the fields mrcfile reads")
    return said


def check_values(d: Described, a: StoredArray, r: Read, row: dict, name: str, seen: Counter) -> list[str]:
    """The row's bytes, through the chain, against the values mrcfile reads; or, of a mode it does
    not read, against the file's bytes at mrcfile's offset."""
    assert a.shape is not None
    assert a.item_bits is not None
    mode = int(r.header.mode)
    # the byte order and the mode, as the declarations give them: a gzipped file's are in the
    # header its key, the stream, inflates to
    held = gzip.decompress(d.place(ElementPlace(a.element, None, None))) if r.gzipped else None
    stamp = held[MACHST[0]:][:4] if held is not None else d.place(a.declarations[0])
    order = {0x44: "<", 0x11: ">"}[stamp[0]]
    declared = held[MODE[0]:][:4] if held is not None else d.place(a.declarations[1])
    if struct.unpack(order + "i", declared) != (mode,):
        return [f"the declarations give mode {declared!r} in the order {order}; mrcfile reads {mode}"]
    seen["big-endian files"] += order == ">"
    count, run = runs(a)
    try:
        mine = decoded(d, a, row)
    except (OSError, EOFError, zlib.error, AssertionError) as e:
        return [f"the row does not decode through the chain: {e}"]
    if len(mine) != count * math.ceil(run * a.item_bits / 8):
        return [f"the row decodes to {len(mine)} bytes, not those of {a.shape} elements of {a.item_bits} bits"]
    if r.data is not None:
        seen["arrays mrcfile reads the values of"] += 1
        seen["images, which mrcfile reads as two axes"] += r.data.ndim == 2
        seen["volume stacks, which mrcfile reads as four axes"] += r.data.ndim == 4
        dtype = np.dtype(order + KIND[mode])
        if dtype != r.data.dtype or dtype.itemsize * 8 != a.item_bits or r.data.size != math.prod(a.shape):
            return [f"{a.shape} of {a.item_bits} bits, read as {dtype}; mrcfile reads {r.data.shape} of {r.data.dtype}"]
        if np.frombuffer(mine, dtype).reshape(a.shape).tobytes() != r.data.reshape(a.shape).tobytes():
            return ["the row decodes to other values than mrcfile reads"]
        return []
    # a mode mrcfile does not read: the file's own bytes after the header and the extended header
    source = gzip.decompress(d.objects[None]) if r.gzipped else d.objects[None]
    theirs = source[HEADER + r.extended:][:count * math.ceil(run * a.item_bits / 8)]
    if mode == 3:
        seen["arrays of mode 3, compared with the file's bytes"] += 1
        pairs = np.dtype([("real", order + "i2"), ("imag", order + "i2")])
        if not np.array_equal(np.frombuffer(mine, pairs), np.frombuffer(theirs, pairs)):
            return ["the row decodes to other pairs of integers than the file holds"]
        return []
    seen["arrays of mode 101, compared with the file's half bytes"] += 1
    seen["arrays of rows of an odd count of 4-bit values"] += a.shape[-1] % 2
    if not np.array_equal(packed_values(mine, a), half_bytes(theirs, a.shape)):
        return [f"the row decodes to other 4-bit values than {name} holds"]
    return []


def check_array(d: Described, a: StoredArray, r: Read, rows: list[dict], name: str, seen: Counter) -> list[str]:
    """The file's description against mrcfile's reading."""
    said = check_declarations(d, a, r, seen)
    if a.index is not None or a.fill != "undefined" or a.unit_parameters or a.axes != (2, 1, 0):
        said.append(f"an index, a fill value, unit parameters, or the axes {a.axes}")
    mode = int(r.header.mode)
    shape = (int(r.header.nz), int(r.header.ny), int(r.header.nx))
    bits = UNREAD[mode] if r.data is None else r.data.dtype.itemsize * 8
    packed = bits % 8 != 0
    if (a.shape, a.item_bits, a.pad_from) != (shape, bits, 2 if packed else None):
        return [*said, f"{a.shape} of {a.item_bits} bits, padded from {a.pad_from}; mrcfile reads {shape} of {bits}"]
    # one unit, the whole array
    if a.grid is None or a.grid.axes != tuple(Each(n) for n in shape) or a.grid.edge != "short":
        return [*said, f"grid {a.grid}, not one unit of {shape}"]
    skip = HEADER + r.extended
    seen["files with an extended header"] += r.extended > 0
    want = [Codec("lsb_first", {"bits": Value(bits)})] if packed else []
    if r.gzipped:
        want += [Codec("skip", {"length": Value(skip)}), Codec("gzip", {})]
    if len(a.chains) != 1 or list(d.stored.chains[a.chains[0]]) != want:
        return [*said, f"chains {[d.stored.chains[c] for c in a.chains]}, not {want}"]
    count, run = runs(a)
    size = len(d.objects[None])
    stored = count * math.ceil(run * bits / 8) if r.data is None else r.data.nbytes
    place = (None, 0, size) if r.gzipped else (None, skip, stored)
    seen["files with bytes after the data"] += not r.gzipped and skip + stored < size
    if len(rows) != 1:
        return [*said, f"{len(rows)} rows, not the one unit"]
    (row,) = rows
    if (tuple(row["coords"]), row["chain"], row["skips"], row["range"]) != ((0, 0, 0), None, None, None):
        said.append(f"a row at {row['coords']} with a chain, skips or a range")
    if row["element"] is None or d.ir.path(row["element"]) != "data" or d.parts(row) != [place]:
        return [*said, f"the row is at {d.parts(row)}; mrcfile reads {place}"]
    return said + check_values(d, a, r, row, name, seen)


@cache
def compared(path: Path) -> tuple[tuple[str, ...], Counter]:
    """What differs between the description of the file at `path` and mrcfile's reading of it, and
    what the comparison saw."""
    d = described(path)
    said: list[str] = []
    seen: Counter = Counter()
    if d.stored.undescribed:
        said.append("descriptions dropped for room")
    if len(d.stored.arrays) != 1:
        return (*said, f"{len(d.stored.arrays)} descriptions of one file's data"), seen
    seen["arrays"] += 1
    said += check_array(d, d.stored.arrays[0], read(path), d.rows.get(0, []), path.name, seen)
    return tuple(said), seen


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_mrc_data_is_described_as_mrcfile_reads_it(path):
    """The file's data is described as mrcfile reads it, and its unit decodes to mrcfile's values."""
    said, seen = compared(path)
    assert said == ()
    assert seen["arrays"] == 1


def test_the_fixtures_hold_every_case_the_comparison_reads():
    """The fixtures reach each case of the comparison, so that none of it passes for want of a
    file. The arrays mrcfile reads no values of are those of modes 3 and 101."""
    seen: Counter = Counter()
    for path in FILES:
        seen += compared(path)[1]
    assert {k: v for k, v in seen.items() if v} == {
        "arrays": 77,
        "arrays mrcfile reads the values of": 70,
        "arrays of mode 3, compared with the file's bytes": 3,
        "arrays of mode 101, compared with the file's half bytes": 4,
        "arrays of rows of an odd count of 4-bit values": 1,
        "images, which mrcfile reads as two axes": 6,
        "volume stacks, which mrcfile reads as four axes": 3,
        "mode 0 files, with the declarations of their sign": 12,
        "gzipped files, keyed by their stream": 4,
        "files with an extended header": 5,
        "files with bytes after the data": 1,
        "big-endian files": 8,
    }


def header(shape: tuple[int, int, int], mode: int, *, big: bool = False, stamp: bytes | None = None, ispg: int = 1,
           mz: int | None = None, extended: int = 0, imod: int | None = None, nversion: int = 20140) -> bytes:
    """An MRC header of `shape` (sections, rows, columns), with `extended` bytes of extended header
    to follow; `imod` is IMOD's flags, written with its stamp."""
    order = ">" if big else "<"
    nz, ny, nx = shape
    h = bytearray(HEADER)
    struct.pack_into(order + "4i", h, 0, nx, ny, nz, mode)
    struct.pack_into(order + "3i", h, 28, nx, ny, nz if mz is None else mz)
    struct.pack_into(order + "6f", h, 40, nx, ny, nz, 90, 90, 90)
    struct.pack_into(order + "3i", h, 64, 1, 2, 3)
    struct.pack_into(order + "2i", h, 88, ispg, extended)
    struct.pack_into(order + "i", h, NVERSION[0], nversion)
    if imod is not None:
        struct.pack_into(order + "2i", h, IMOD_STAMP[0], 1146047817, imod)
    h[208:212] = b"MAP "
    h[212:216] = stamp if stamp is not None else (b"\x11\x11\0\0" if big else b"\x44\x44\0\0")
    return bytes(h)


def gzip_member(stream: bytes, name: bytes, comment: bytes) -> bytes:
    """One gzip member of `stream`, with a file name and a comment in its header (RFC 1952)."""
    deflate = zlib.compressobj(6, zlib.DEFLATED, -15)
    body = deflate.compress(stream) + deflate.flush()
    # the magic number, deflate, FNAME and FCOMMENT, no time, no extra flags, an unknown system
    start = b"\x1f\x8b\x08\x18" + bytes(4) + b"\x00\xff" + name + b"\0" + comment + b"\0"
    return start + body + struct.pack("<II", zlib.crc32(stream), len(stream) % 2**32)


def test_files_no_fixture_holds_are_described_as_mrcfile_reads_them(tmp_path):
    """Cases the fixtures do not have, each a file written here: 4-bit values in a big-endian file,
    whose half bytes do not follow the byte order, and in rows of one value; the two machine stamps
    of other writers, whose second byte the profile does not read (mrcfile, permissive, finds the
    byte order of the one it does not know by the mode); big-endian floats of 16 bits, complex
    values and unsigned integers; a volume stack of bytes; and gzipped files whose member's header
    has a name and a comment, of a mode 0 file with IMOD's words and an extended header, and of
    complex values."""
    rng = np.random.default_rng(0)

    def values(shape: tuple[int, int, int], dtype: str) -> bytes:
        return rng.integers(0, 200, shape).astype(dtype).tobytes()

    def packed(shape: tuple[int, int, int]) -> bytes:
        return rng.integers(0, 256, (shape[0] * shape[1], (shape[2] + 1) // 2)).astype("u1").tobytes()

    extended = bytes(range(48))
    imod = header((2, 3, 4), 0, extended=len(extended), imod=0, nversion=0)
    cases = {
        "int4_big.mrc": header((2, 3, 5), 101, big=True) + packed((2, 3, 5)),
        "int4_one_column.mrc": header((3, 2, 1), 101) + packed((3, 2, 1)),
        "stamp_44_41.mrc": header((2, 3, 4), 1, stamp=b"\x44\x41\0\0") + values((2, 3, 4), "<i2"),
        "stamp_11_00.mrc": header((2, 3, 4), 2, big=True, stamp=b"\x11\0\0\0") + values((2, 3, 4), ">f4"),
        "float16_big.mrc": header((2, 3, 4), 12, big=True) + values((2, 3, 4), ">f2"),
        "complex64_big.mrc": header((2, 3, 4), 4, big=True) + values((2, 3, 4), ">c8"),
        "uint16_big.mrc": header((1, 3, 4), 6, big=True, ispg=0) + values((1, 3, 4), ">u2"),
        "stack_int8.mrc": header((6, 2, 3), 0, ispg=401, mz=2) + values((6, 2, 3), "i1"),
        "named.mrc.gz": gzip_member(header((2, 3, 4), 1) + values((2, 3, 4), "<i2"), b"named.mrc", b"a comment"),
        "imod_extended.mrc.gz": gzip.compress(imod + extended + values((2, 3, 4), "u1"), mtime=0),
        "complex64.map.gz": gzip.compress(header((2, 3, 4), 4) + values((2, 3, 4), "<c8"), mtime=0),
    }
    seen: Counter = Counter()
    for name, data in cases.items():
        path = tmp_path / name
        path.write_bytes(data)
        said, saw = compared(path)
        assert said == (), name
        seen += saw
    assert {k: v for k, v in seen.items() if v} == {
        "arrays": 11,
        "arrays mrcfile reads the values of": 9,
        "arrays of mode 101, compared with the file's half bytes": 2,
        "arrays of rows of an odd count of 4-bit values": 2,
        "images, which mrcfile reads as two axes": 1,
        "volume stacks, which mrcfile reads as four axes": 1,
        "mode 0 files, with the declarations of their sign": 1,
        "gzipped files, keyed by their stream": 3,
        "files with an extended header": 1,
        "big-endian files": 5,
    }
