"""The stored array of the NRRD fixtures (spec/stored-array.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>), compared with
pynrrd, the reference reader. pynrrd reads each input from file objects that note where it reads
the data, and of each input

- the array is keyed by the header file's object, and its declarations are the lines pynrrd reads
  the `type` and the `endian` from;
- its shape, its sample size and its one unit are those of the array pynrrd reads, and `axes`
  turns pynrrd's array in the header's order into the one in storage order;
- its chain is the one the header's `encoding` and `byte skip`, as pynrrd reads them, give, the
  skip's length at the place the header writes it;
- its row is the bytes pynrrd reads the data from: the file, the offset and the length;
- those bytes, decoded through the chain, are the bytes of the array pynrrd reads.

pynrrd 1.1.3 departs from the NRRD definition in three ways, each counted where it is met. It
reads a file it is given by its path to the file's end, and refuses one with bytes after the data
(it reads the same file from a file object, as the test gives it). With the gzip encoding and a
byte skip it also seeks that far into the compressed stream, so it is given a copy with as many
bytes before the stream. And it inflates the first member of a gzip stream alone, where the
`gunzip` program the definition names reads every member: of a stream of several, which pynrrd
refuses, the test compares the bytes it wrote.

A second test asserts which cases the fixtures reached."""

import gzip
import io
import math
import os
import zlib
from collections import Counter
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Literal, override
from unittest import mock

import nrrd
import numpy as np
import pytest
from described import Described, described
from ir_helpers import FIXTURES

from refkv.ir.kind import Kind
from refkv.ir.stored import Each, ElementPlace, Source

FILES = sorted(p for p in (FIXTURES / "nrrd").iterdir() if p.suffix in (".nrrd", ".nhdr") and "reject" not in p.name)
# what pynrrd raises of data it does not read: its own error, zlib's, and NumPy's of bytes that
# are no whole elements
REFUSALS = (nrrd.NRRDError, zlib.error, ValueError)
pytestmark = pytest.mark.filterwarnings("ignore:Duplicate header field")

type Range = tuple[str, int, int]
"""(object key, offset, length): bytes of a file of the input"""


@dataclass(frozen=True)
class Input:
    """A NRRD input's descriptions, with its store's objects: each object's key by its element,
    and each file's bytes by its key."""

    d: Described
    keys: dict[int, str]
    files: dict[str, bytes]

    def where(self, element: int, within: tuple[int, int] | None = None) -> Range:
        """The bytes of an element's extent, or of a range of it."""
        start, length = self.d.ir.extent(element)
        if within is not None:
            start, length = start + within[0], within[1]
        while self.d.ir.kind(element) != Kind.OBJECT:
            element = self.d.ir.parent(element)
        return self.keys[element], start, length

    def held(self, at: Range) -> bytes:
        key, offset, length = at
        return self.files[key][offset:offset + length]


def read_input(path: Path) -> Input:
    """The input whose header file is at `path`, its data file beside it."""
    d = described(path)
    objects = [i for i in range(len(d.ir)) if d.ir.kind(i) == Kind.OBJECT]
    keys = {i: key for i, (key, _, _) in zip(objects, d.ir.object_bases(), strict=True)}
    return Input(d, keys, {key: (path.parent / key).read_bytes() for key in keys.values()})


@dataclass
class Log:
    """Where pynrrd read data, as (key, offset, length), and where it sought, as (key, the
    position it was at, the seek's offset, its whence)."""

    reads: list[Range] = field(default_factory=list)
    seeks: list[tuple[str, int, int, int]] = field(default_factory=list)


class Noted(io.BytesIO):
    """A file as pynrrd reads a file object, which notes each read of data and each seek. pynrrd
    reads the header's lines, and those a line skip passes over, a line at a time, which is no
    such read."""

    def __init__(self, data: bytes, key: str, log: Log) -> None:
        super().__init__(data)
        self.key, self.log = key, log

    @override
    def read(self, size: int | None = -1) -> bytes:
        at = self.tell()
        got = super().read(size)
        self.log.reads.append((self.key, at, len(got)))
        return got

    @override
    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        self.log.seeks.append((self.key, self.tell(), offset, whence))
        return super().seek(offset, whence)


def pynrrd_header(path: Path, files: dict[str, bytes]) -> dict:
    """The header pynrrd reads of the header file at `path`. A key given twice keeps its last
    value, as NRRD allows, which pynrrd reads only when told to."""
    with mock.patch.object(nrrd.reader, "ALLOW_DUPLICATE_FIELD", new=True):
        return nrrd.read_header(io.BytesIO(files[path.name]))


def pynrrd_data(path: Path, files: dict[str, bytes], order: Literal["C", "F"], log: Log) -> np.ndarray:
    """The array pynrrd reads of the header file at `path`, the input's files given by key, as
    `nrrd.read` reads it but from file objects that note in `log` where the data is read:
    `order` "C" for storage order (the header's last axis first), "F" for the header's."""

    def opened(name: str, _mode: str) -> Noted:
        key = Path(os.path.relpath(name, path.parent)).as_posix()
        return Noted(files[key], key, log)

    handle = Noted(files[path.name], path.name, log)
    with (mock.patch.object(nrrd.reader, "ALLOW_DUPLICATE_FIELD", new=True),
          mock.patch.object(nrrd.reader, "open", opened, create=True)):
        return nrrd.read_data(nrrd.read_header(handle), handle, str(path), index_order=order)


def by_path(path: Path) -> np.ndarray | None:
    """The array `nrrd.read` reads of the file at `path`, in storage order; None where it refuses."""
    try:
        with mock.patch.object(nrrd.reader, "ALLOW_DUPLICATE_FIELD", new=True):
            return nrrd.read(str(path), index_order="C")[0]
    except REFUSALS:
        return None


def referenced(path: Path, files: dict[str, bytes], skip: int, seen: Counter) -> tuple[
        np.ndarray | None, np.ndarray | None, list[Range]]:
    """pynrrd's reading of an input whose header gives the byte skip `skip`: its array in storage
    order and in the header's order (None where it reads none), and where it read the data."""
    log = Log()
    try:
        stored = pynrrd_data(path, files, "C", log)
    except REFUSALS:
        moved = [(key, at) for key, at, offset, whence in log.seeks if (offset, whence) == (skip, os.SEEK_CUR)]
        if skip <= 0 or len(moved) != 1:
            seen["inputs pynrrd refuses"] += 1
            return None, None, log.reads
        # pynrrd seeks the byte skip into the gzip stream before it inflates it, and then passes
        # over as many inflated bytes: it reads a copy with that many bytes where it seeks
        seen["inputs pynrrd reads only with the byte skip's bytes before the stream"] += 1
        ((key, at),) = moved
        copy = {**files, key: files[key][:at] + bytes(skip) + files[key][at:]}
        log = Log()
        stored = pynrrd_data(path, copy, "C", log)
        # where the copy's reads are in the input: those after the bytes put in, as many earlier
        reads = [(k, o - skip if k == key and o >= at + skip else o, n) for k, o, n in log.reads]
        return stored, pynrrd_data(path, copy, "F", Log()), reads
    read = by_path(path)
    if read is None:
        # of a file with bytes after its raw data, which pynrrd reads to the file's end by its path
        seen["inputs pynrrd refuses by their path alone"] += 1
    elif read.dtype != stored.dtype or read.tobytes() != stored.tobytes():
        raise AssertionError("pynrrd reads another array by the file's path than from file objects")
    return stored, pynrrd_data(path, files, "F", Log()), log.reads


def field_of(line: bytes) -> tuple[str, str]:
    """A header line's field: its identifier in lower case, and its descriptor."""
    identifier, _, descriptor = line.decode("ascii").partition(":")
    return identifier.lower(), descriptor.strip()


def decoded(inp: Input, unit: bytes) -> bytes:
    """A unit's bytes taken through the array's chain, from its last codec to its first."""
    (a,) = inp.d.stored.arrays
    for codec in reversed(inp.d.stored.chains[a.chains[0]]):
        if codec.name == "gzip":
            # every member's bytes, joined
            unit = gzip.decompress(unit)
        elif codec.name == "skip":
            length = codec.parameters["length"]
            assert isinstance(length, Source)
            assert isinstance(length.place, ElementPlace)
            unit = unit[int(inp.held(inp.where(length.place.element, length.place.range))):]
        else:
            raise AssertionError(f"the codec {codec.name}")
    return unit


def check(path: Path, inp: Input, wrote: bytes | None, seen: Counter) -> list[str]:
    """What differs between the description of the input at `path` and pynrrd's reading of it.
    `wrote` is the array's bytes, for an input the test wrote that pynrrd refuses."""
    d = inp.d
    if d.stored.undescribed or len(d.stored.arrays) != 1:
        return [f"{len(d.stored.arrays)} descriptions, undescribed {d.stored.undescribed}"]
    (a,) = d.stored.arrays
    rows = d.rows.get(0, [])
    header = pynrrd_header(path, inp.files)
    sizes = [int(n) for n in header["sizes"]]
    skip = int(header.get("byte skip", header.get("byteskip", 0)))
    gz = header["encoding"] in ("gzip", "gz")
    said = []
    seen["arrays"] += 1
    seen[f"arrays of {len(sizes)} axes"] += 1

    # the key: the header file's object
    if inp.keys.get(a.element) != path.name or a.index is not None:
        said.append(f"keyed by {a.path} with the index {a.index}, not by the header file's object")

    # the declarations: the lines of the type and of the endian, as pynrrd reads them
    want_lines = [("type", header["type"])] + ([("endian", header["endian"])] if "endian" in header else [])
    lines = []
    for p in a.declarations:
        if not isinstance(p, ElementPlace) or p.range is not None or p.member is not None:
            said.append(f"a declaration {p} that is no whole element")
            continue
        key, start, length = inp.where(p.element)
        whole, before = inp.files[key][start:start + length], inp.files[key][:start]
        # a line of the header file: it starts the file or follows a line end, and ends with its own
        if key != path.name or before[-1:] not in (b"", b"\n") or whole.count(b"\n") != 1 or whole[-1:] != b"\n":
            said.append(f"a declaration at {(key, start, length)} that is no line of the header")
        lines.append(field_of(whole))
    if lines != want_lines:
        said.append(f"declarations {lines}, pynrrd's {want_lines}")
    seen["arrays of one byte, with no endian"] += "endian" not in header

    # the shape, the axes' order and the sample size
    stored, listed, reads = referenced(path, inp.files, skip if gz else 0, seen)
    if stored is None or listed is None:
        if wrote is None:
            return [*said, "pynrrd reads no array"]
        want, item = wrote, len(wrote) // math.prod(sizes)
    else:
        want, item = stored.tobytes(), stored.dtype.itemsize
        if a.shape != stored.shape:
            said.append(f"shape {a.shape}, pynrrd's {stored.shape}")
        # storage axis i is the header's axis axes[i]
        order = a.axes if a.axes is not None else tuple(range(len(sizes)))
        if sorted(order) != list(range(listed.ndim)) or np.transpose(listed, order).tobytes() != want:
            said.append(f"axes {a.axes} do not turn pynrrd's array in the header's order into the stored one")
    if a.shape != tuple(reversed(sizes)):
        said.append(f"shape {a.shape}, the header's sizes {sizes}")
    if (a.axes is None) != (len(sizes) < 2):
        said.append(f"axes {a.axes} of {len(sizes)} axes")
    if a.item_bits != 8 * item:
        said.append(f"item_bits {a.item_bits}, pynrrd's elements of {item} bytes")
    if a.pad_from is not None or a.fill != "undefined" or a.unit_parameters:
        said.append("a padding, a fill value or unit parameters")

    # one unit of the whole array
    if a.grid is None or a.shape is None or a.grid.axes != tuple(Each(n) for n in a.shape) or a.grid.edge != "short":
        return [*said, f"grid {a.grid}, not one unit of {a.shape}"]
    if len(rows) != 1:
        return [*said, f"{len(rows)} rows"]
    (row,) = rows
    unset = ("member", "range", "ranges", "chain", "skips")
    if row["coords"] != [0] * len(sizes) or row["element"] is None or any(row[c] is not None for c in unset):
        return [*said, f"the row {row}"]

    # the chain: what the encoding and the byte skip give
    chain = d.stored.chains[a.chains[0]] if len(a.chains) == 1 else None
    names = [(c.name, sorted(c.parameters)) for c in chain or ()]
    want_names = ([("skip", ["length"])] if gz and skip > 0 else []) + ([("gzip", [])] if gz else [])
    if chain is None or names != want_names:
        return [*said, f"chain {names}, not {want_names} of the encoding {header['encoding']} and the byte skip {skip}"]
    seen["arrays of the gzip encoding" if gz else "arrays of the raw encoding"] += 1
    if names[:1] == [("skip", ["length"])]:
        seen["arrays of a skip in the stream"] += 1
        length = chain[0].parameters["length"]
        if not isinstance(length, Source) or not isinstance(length.place, ElementPlace) or length.place.range is None:
            return [*said, f"a skip of the length {length}"]
        # the descriptor of the byte skip's line, as pynrrd reads it
        line = inp.held(inp.where(length.place.element))
        at = inp.where(length.place.element, length.place.range)
        offset, count = length.place.range
        if (at[0] != path.name or field_of(line)[0] not in ("byte skip", "byteskip")
                or line[offset:offset + count].decode() != field_of(line)[1] or int(inp.held(at)) != skip):
            said.append(f"a skip of the length {inp.held(at)!r} of the line {line!r}, pynrrd's byte skip {skip}")

    # the row: the bytes pynrrd reads the data from
    at = inp.where(row["element"])
    if reads != [at]:
        said.append(f"the unit at {at}, pynrrd reads {reads}")
    seen["arrays of a data file"] += at[0] != path.name
    seen["units with bytes of the file after them"] += at[1] + at[2] < len(inp.files[at[0]])

    # the unit, decoded through the chain: the array's bytes
    if decoded(inp, inp.held(at)) != want:
        said.append("the unit decodes to other bytes than the array pynrrd reads")
    seen["units decoded"] += 1
    seen["units compared with the bytes written"] += stored is None
    return said


@cache
def compared(path: Path, wrote: bytes | None = None) -> tuple[tuple[str, ...], Counter]:
    """What differs between the description of the input at `path` and pynrrd, and what the
    comparison saw."""
    seen: Counter = Counter()
    return tuple(check(path, read_input(path), wrote, seen)), seen


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_a_nrrd_array_is_described_as_pynrrd_reads_it(path):
    """The input's array is described: its shape, its sample size, its one unit where pynrrd
    reads the data, in the chain that decodes it to the values pynrrd reads."""
    said, seen = compared(path)
    assert said == ()
    assert seen["units decoded"] == 1


def test_the_fixtures_hold_every_case_the_comparison_reads():
    """The fixtures reach each case of the comparison, so that none of it passes for want of a
    file. The input pynrrd reads only from a copy is `nrrd_gzip_byte_skip.nrrd`: pynrrd seeks the
    byte skip into the compressed stream, which the NRRD definition counts within the inflated
    bytes alone."""
    seen: Counter = Counter()
    for path in FILES:
        seen += compared(path)[1]
    assert {k: v for k, v in seen.items() if v} == {
        "arrays": 41,
        "arrays of 2 axes": 8,
        "arrays of 3 axes": 31,
        "arrays of 4 axes": 2,
        "arrays of one byte, with no endian": 11,
        "arrays of the raw encoding": 36,
        "arrays of the gzip encoding": 5,
        "arrays of a skip in the stream": 1,
        "arrays of a data file": 3,
        "inputs pynrrd reads only with the byte skip's bytes before the stream": 1,
        "units decoded": 41,
    }


def header(*fields: str, end: str = "\n", blank: bool = True) -> bytes:
    """A header of the magic and `fields`, a line each, with the blank line that ends it."""
    return "".join(f"{line}{end}" for line in ("NRRD0004", *fields, *[""] * blank)).encode()


def test_inputs_the_fixtures_lack_are_described_as_pynrrd_reads_them(tmp_path):
    """Cases no fixture holds, each compared as the fixtures are: raw data with bytes of the file
    after it, which the NRRD definition leaves unread; data at the file's end (`byte skip: -1`),
    where a line skip is moot; a detached header that ends with its file, its data after a line
    and bytes of its data file; a skip whose line is written `byteskip`, with a tab and a sign; a
    gzip stream of two members, which pynrrd refuses; five axes; a header of CRLF line ends with
    no `endian`; and a detached gzip stream after a line, with a skip whose line ends with spaces
    and a CRLF."""
    rng = np.random.default_rng(7)

    def values(dtype: str, *shape: int) -> bytes:
        return rng.integers(0, 250, size=shape).astype(dtype).tobytes()

    cases: dict[str, bytes | None] = {}
    after = values(">i2", 4, 3)
    (tmp_path / "raw_bytes_after.nrrd").write_bytes(
        header("type: short", "dimension: 2", "sizes: 3 4", "endian: big", "encoding: raw") + after + b"not the data")
    cases["raw_bytes_after.nrrd"] = None
    (tmp_path / "raw_at_the_end.nrrd").write_bytes(
        header("type: uint16", "dimension: 2", "sizes: 5 2", "endian: little", "encoding: raw", "line skip: 2",
               "byte skip: -1") + b"one line\nand bytes" + values("<u2", 2, 5))
    cases["raw_at_the_end.nrrd"] = None
    (tmp_path / "parts").mkdir()
    (tmp_path / "parts" / "data.bin").write_bytes(b"a line\nxyz" + values(">u8", 4, 3, 2))
    (tmp_path / "detached.nhdr").write_bytes(
        header("type: unsigned long long int", "dimension: 3", "sizes: 2 3 4", "endian: big", "encoding: raw",
               "line skip: 1", "byte skip: 3", "data file: ./parts/data.bin", blank=False))
    cases["detached.nhdr"] = None
    (tmp_path / "gzip_skip_written_otherwise.nrrd").write_bytes(
        header("type: float", "dimension: 2", "sizes: 4 4", "endian: little", "byteskip: \t+03", "encoding: gz")
        + gzip.compress(b"\x01\x02\x03" + values("<f4", 4, 4)))
    cases["gzip_skip_written_otherwise.nrrd"] = None
    members = values("<i4", 2, 3, 4)
    (tmp_path / "gzip_two_members.nrrd").write_bytes(
        header("type: int", "dimension: 3", "sizes: 4 3 2", "endian: little", "encoding: gzip")
        + gzip.compress(members[:40]) + gzip.compress(members[40:]))
    cases["gzip_two_members.nrrd"] = members
    (tmp_path / "five_axes.nrrd").write_bytes(
        header("type: double", "dimension: 5", "sizes: 3 4 5 2 2", "endian: big", "encoding: raw",
               "kinds: RGB-color domain domain domain time") + values(">f8", 2, 2, 5, 4, 3))
    cases["five_axes.nrrd"] = None
    (tmp_path / "crlf_one_byte.nrrd").write_bytes(
        header("type: uint8", "dimension: 2", "sizes: 4 3", "encoding: raw", end="\r\n") + values("u1", 3, 4))
    cases["crlf_one_byte.nrrd"] = None
    (tmp_path / "z.gz").write_bytes(b"a line\n" + gzip.compress(b"12345" + values(">i2", 3, 2, 2)))
    (tmp_path / "detached_gzip_skip.nhdr").write_bytes(
        header("type: short", "dimension: 3", "sizes: 2 2 3", "endian: big", "encoding: gzip", "line skip: 1",
               "byte skip: 5  ", "data file: z.gz", end="\r\n"))
    cases["detached_gzip_skip.nhdr"] = None

    seen: Counter = Counter()
    for name, wrote in cases.items():
        said, saw = compared(tmp_path / name, wrote)
        assert said == (), name
        seen += saw
    assert {k: v for k, v in seen.items() if v} == {
        "arrays": 8,
        "arrays of 2 axes": 4,
        "arrays of 3 axes": 3,
        "arrays of 5 axes": 1,
        "arrays of one byte, with no endian": 1,
        "arrays of the raw encoding": 5,
        "arrays of the gzip encoding": 3,
        "arrays of a skip in the stream": 2,
        "arrays of a data file": 2,
        "units with bytes of the file after them": 1,
        "inputs pynrrd refuses by their path alone": 1,
        "inputs pynrrd reads only with the byte skip's bytes before the stream": 2,
        "inputs pynrrd refuses": 1,
        "units compared with the bytes written": 1,
        "units decoded": 8,
    }


def test_a_stream_of_more_bytes_than_the_arrays_is_described_as_one_of_as_many(tmp_path):
    """The parser does not inflate a gzip stream, so the description of one that inflates to more
    bytes than the array's is the description of one that inflates to as many: the array's bytes
    are the first the chain makes. pynrrd refuses such a stream."""
    data = np.arange(12, dtype="<i2").tobytes()
    top = header("type: short", "dimension: 2", "sizes: 4 3", "endian: little", "encoding: gzip")
    for name, more in (("as_many", b""), ("more", b"and more")):
        (tmp_path / name).mkdir()
        (tmp_path / name / "a.nrrd").write_bytes(top + gzip.compress(data + more))
    exact, longer = read_input(tmp_path / "as_many" / "a.nrrd"), read_input(tmp_path / "more" / "a.nrrd")
    assert compared(tmp_path / "as_many" / "a.nrrd")[0] == ()
    assert longer.d.stored == exact.d.stored
    assert longer.d.rows == exact.d.rows
    (row,) = longer.d.rows[0]
    assert decoded(longer, longer.held(longer.where(row["element"]))) == data + b"and more"
    with pytest.raises(REFUSALS):
        pynrrd_data(tmp_path / "more" / "a.nrrd", longer.files, "C", Log())
