"""Writes the netCDF3 fixtures (spec/zarrify/netcdf3.md) to fixtures/netcdf3/: files of the three
formats, CDF-1, CDF-2 and CDF-5, laid out as netCDF-C lays them out (the header, the fixed-size
variables in order, each from a multiple of 4, then the records), and the inputs the profile rejects
(§7.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#74-checks>), each the
smallest file that breaks one check. A writer of its own, from the format specification, needs no
netCDF library, so it runs everywhere and gives the same bytes; python/tests/ir/test_netcdf3.py
reads every accepted fixture with netCDF4-python and scipy, and also zarrifies files netCDF-C wrote.
conformance/zarrify/rejects/netcdf3.json lists each rejected input with its message.

Usage: uv run python fixtures/generators/netcdf3/write_fixtures.py
"""

from __future__ import annotations

import math
import shutil
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

OUT = Path(__file__).parents[2] / "netcdf3"

# nc_type codes and their big-endian NumPy types
BYTE, CHAR, SHORT, INT, FLOAT, DOUBLE, UBYTE, USHORT, UINT, INT64, UINT64 = range(1, 12)
TYPES = {BYTE: ">i1", CHAR: "S1", SHORT: ">i2", INT: ">i4", FLOAT: ">f4", DOUBLE: ">f8", UBYTE: ">u1",
         USHORT: ">u2", UINT: ">u4", INT64: ">i8", UINT64: ">u8"}
NC_DIMENSION, NC_VARIABLE, NC_ATTRIBUTE = 10, 11, 12


def pad4(n: int) -> int:
    return -n % 4


@dataclass
class Var:
    name: bytes | str
    dims: list[str]
    type: int
    data: Any = None
    attrs: list[tuple[bytes | str, int, Any]] = field(default_factory=list)


@dataclass
class File:
    """A netCDF3 file to write: its version, dimensions (`None` for the unlimited one's length),
    global attributes, variables and record count."""

    version: int
    dims: list[tuple[str, int | None]]
    attrs: list[tuple[bytes | str, int, Any]]
    vars: list[Var]
    records: int = 0
    streaming: bool = False
    # bytes of free space after the header, as netCDF-C's h_minfree leaves
    free: int = 0
    # where each field lands, for the rejects' patches: ("begin", k), ("type", k), ("numrecs",)
    at: dict = field(default_factory=dict)

    def nonneg(self, v: int) -> bytes:
        return struct.pack(">Q" if self.version == 5 else ">I", v)

    def offset(self, v: int) -> bytes:
        return struct.pack(">I" if self.version == 1 else ">Q", v)

    def name(self, s: bytes | str) -> bytes:
        b = s.encode() if isinstance(s, str) else s
        return self.nonneg(len(b)) + b + b"\0" * pad4(len(b))

    def values(self, code: int, v: Any) -> tuple[int, bytes]:
        if code == CHAR:
            raw = v.encode() if isinstance(v, str) else bytes(v)
            return len(raw), raw
        a = np.asarray(v, dtype=TYPES[code]).reshape(-1)
        return a.size, a.tobytes()

    def att_list(self, attrs) -> bytes:
        if not attrs:
            return struct.pack(">I", 0) + self.nonneg(0)
        out = struct.pack(">I", NC_ATTRIBUTE) + self.nonneg(len(attrs))
        for name, code, v in attrs:
            count, raw = self.values(code, v)
            out += self.name(name) + struct.pack(">I", code) + self.nonneg(count) + raw + b"\0" * pad4(len(raw))
        return out

    def lengths(self, v: Var) -> list[int]:
        lengths = dict(self.dims)
        return [self.records if (n := lengths[d]) is None else n for d in v.dims]

    def is_record(self, v: Var) -> bool:
        return bool(v.dims) and dict(self.dims)[v.dims[0]] is None

    def nbytes(self, v: Var) -> int:
        """A fixed-size variable's bytes, or a record variable's slice."""
        lens = self.lengths(v)[1:] if self.is_record(v) else self.lengths(v)
        return math.prod(lens) * np.dtype(TYPES[v.type]).itemsize

    def header(self, begins: list[int]) -> bytes:
        out = b"CDF" + bytes([self.version])
        self.at["numrecs"] = len(out)
        out += (b"\xff" * (8 if self.version == 5 else 4)) if self.streaming else self.nonneg(self.records)
        if self.dims:
            out += struct.pack(">I", NC_DIMENSION) + self.nonneg(len(self.dims))
            for n, length in self.dims:
                out += self.name(n) + self.nonneg(length or 0)
        else:
            out += struct.pack(">I", 0) + self.nonneg(0)
        out += self.att_list(self.attrs)
        if not self.vars:
            return out + struct.pack(">I", 0) + self.nonneg(0)
        index = {n: k for k, (n, _) in enumerate(self.dims)}
        out += struct.pack(">I", NC_VARIABLE) + self.nonneg(len(self.vars))
        for k, v in enumerate(self.vars):
            out += self.name(v.name) + self.nonneg(len(v.dims))
            out += b"".join(self.nonneg(index[d]) for d in v.dims)
            out += self.att_list(v.attrs)
            self.at["type", k] = len(out)
            n = self.nbytes(v)
            out += struct.pack(">I", v.type) + self.nonneg(min(n + pad4(n), 2**32 - 1))
            self.at["begin", k] = len(out)
            out += self.offset(begins[k])
        return out

    def bytes(self) -> bytes:
        size = len(self.header([0] * len(self.vars)))
        at = size + pad4(size) + self.free
        begins = [0] * len(self.vars)
        fixed = [k for k, v in enumerate(self.vars) if not self.is_record(v)]
        recs = [k for k, v in enumerate(self.vars) if self.is_record(v)]
        for k in fixed:
            begins[k] = at
            n = self.nbytes(self.vars[k])
            at += n + pad4(n)
        for k in recs:
            begins[k] = at
            n = self.nbytes(self.vars[k])
            at += n + pad4(n)
        head = self.header(begins)
        out = bytearray(head + b"\0" * (begins[fixed[0]] - len(head) if fixed else 0))
        for k in fixed:
            v = self.vars[k]
            raw = np.asarray(v.data, dtype=TYPES[v.type]).tobytes()
            out = out.ljust(begins[k], b"\0") + raw + b"\0" * pad4(len(raw))
        if recs:
            out = out.ljust(begins[recs[0]], b"\0")
        for r in range(self.records):
            for k in recs:
                v = self.vars[k]
                raw = np.asarray(v.data, dtype=TYPES[v.type])[r : r + 1].tobytes()
                # one record variable's slices follow one another unpadded
                out += raw + (b"" if len(recs) == 1 else b"\0" * pad4(len(raw)))
        return bytes(out)


def chars(rows: list[str], width: int) -> np.ndarray:
    return np.array([[c.encode() for c in r.ljust(width, "\0")] for r in rows], dtype="S1")


def classic() -> File:
    """CDF-1: coordinate variables, two record variables (padded slices), text, packing."""
    rng = np.random.default_rng(1)
    time = np.array([0.0, 6.0, 12.0])
    temp = rng.normal(280, 5, (3, 3, 4)).astype(">f4")
    temp[1, 1, 1] = -999.0
    return File(1, [("time", None), ("lat", 3), ("lon", 4), ("strlen", 5), ("unused", 2)],
                [("title", CHAR, "netCDF3 classic fixture"), ("history", CHAR, "written by write_fixtures.py\0\0"),
                 ("version", INT, 3), ("range", DOUBLE, [-1.5, 2.25]), ("empty", SHORT, []),
                 ("latin", CHAR, b"caf\xe9"), ("nan", FLOAT, [float("nan"), float("inf")])],
                [Var("time", ["time"], DOUBLE, time, [("units", CHAR, "hours since 2000-01-01"), ("axis", CHAR, "T")]),
                 Var("lat", ["lat"], FLOAT, [-10.0, 0.0, 10.0], [("units", CHAR, "degrees_north")]),
                 Var("lon", ["lon"], FLOAT, [0.0, 90.0, 180.0, 270.0], [("units", CHAR, "degrees_east")]),
                 Var("temp", ["time", "lat", "lon"], FLOAT, temp,
                     [("_FillValue", FLOAT, -999.0), ("units", CHAR, "K"), ("valid_range", FLOAT, [200, 350])]),
                 Var("flag", ["time"], SHORT, [1, 2, 3], [("_FillValue", SHORT, -1)]),
                 Var("mask", ["lat", "lon"], BYTE, np.arange(12).reshape(3, 4) - 6),
                 Var("station", ["lat", "strlen"], CHAR, chars(["abc", "de", "fghij"], 5)),
                 Var("packed", ["lat"], SHORT, [100, 200, 300],
                     [("scale_factor", FLOAT, 0.5), ("add_offset", FLOAT, 10.0)])],
                records=3, free=12)


def offset64() -> File:
    """CDF-2: a scalar, one record variable (its slices unpadded), a `_FillValue` of another type."""
    return File(2, [("x", 3), ("t", None)],
                [("Conventions", CHAR, "CF-1.8")],
                [Var("scalar", [], DOUBLE, 3.5, [("long_name", CHAR, "a scalar")]),
                 Var("x", ["x"], INT, [10, 20, 30]),
                 Var("series", ["t", "x"], SHORT, np.arange(15).reshape(5, 3) - 7,
                     [("_FillValue", INT, -1), ("missing_value", SHORT, -7)]),
                 Var("odd", ["x"], BYTE, [1, -2, 3])],
                records=5)


def data64() -> File:
    """CDF-5: its types and attributes of them."""
    return File(5, [("n", 2), ("rec", None)],
                [("big", INT64, [2**62 + 1, -(2**63)]), ("ubig", UINT64, 2**64 - 1), ("u8", UBYTE, [0, 255]),
                 ("u16", USHORT, 65535), ("u32", UINT, [1, 4294967295])],
                [Var("ub", ["n"], UBYTE, [1, 255], [("_FillValue", UBYTE, 7)]),
                 Var("us", ["n"], USHORT, [1, 65535]),
                 Var("ui", ["n"], UINT, [1, 4294967295]),
                 Var("i64", ["n"], INT64, [-(2**63), 2**63 - 1], [("_FillValue", INT64, -5)]),
                 Var("u64", ["n"], UINT64, [0, 2**64 - 1]),
                 Var("r", ["rec", "n"], DOUBLE, [[1.0, 2.0], [3.0, 4.0]]),
                 Var("rb", ["rec"], UBYTE, [9, 10])],
                records=2)


def streaming(stream: bool = True) -> File:
    """CDF-1: a streaming record count, which the records the file holds replace. netCDF-C 4.10
    loops on it (and crashes on it in CDF-5), so the tests read the same file with the count
    written out (`stream` false) as the reference."""
    return File(1, [("t", None), ("k", 3)], [], [Var("a", ["t", "k"], SHORT, np.arange(12).reshape(4, 3)),
                                                 Var("b", ["t"], DOUBLE, [0.5, 1.5, 2.5, 3.5])],
                records=4, streaming=stream)


def unpresented() -> File:
    """CDF-1: variables whose names no node may take, and a reserved attribute."""
    return File(1, [("d", 2)],
                [("zarr_conventions", CHAR, "mine"), ("ok", INT, 1), ("ok", INT, 2)],
                [Var("__hidden", ["d"], INT, [1, 2]), Var("refkv_source", ["d"], INT, [3, 4]),
                 Var("v", ["d"], INT, [5, 6]), Var("v", ["d"], INT, [7, 8]), Var(b"bad\xff", ["d"], INT, [9, 10]),
                 Var("a/b", ["d"], INT, [11, 12]), Var("zarr.json", ["d"], INT, [13, 14]), Var("w", ["d"], CHAR, [b"x", b"\0"], [("_FillValue", CHAR, "x")])])


def empty() -> File:
    """CDF-1: a record variable with no records, and no attributes."""
    return File(1, [("t", None), ("k", 2)], [],
                [Var("r", ["t", "k"], FLOAT, np.zeros((0, 2))), Var("k", ["k"], INT, [1, 2])])


def nothing() -> File:
    """CDF-2: no dimensions, attributes or variables."""
    return File(2, [], [], [])


def chunked() -> File:
    """CDF-1: a fixed-size variable past the chunk limit, cut with a padded edge chunk."""
    data = (np.arange(1100 * 1000) % 251).astype(">i1").reshape(1100, 1000)
    return File(1, [("y", 1100), ("x", 1000)], [], [Var("big", ["y", "x"], BYTE, data)])


def patched(f: File, *patches: tuple[Any, str, int]) -> bytes:
    """The file `f` with each (where, format, value) packed: `where` a byte offset, or a field of
    `f.at`."""
    b = bytearray(f.bytes())
    for where, fmt, v in patches:
        struct.pack_into(fmt, b, where if isinstance(where, int) else f.at[where], v)
    return bytes(b)


def moved(f: File, k: int, to: int) -> bytes:
    """The file `f` with variable `k`'s `begin` that of variable `to`, plus 4 bytes for one that is
    fixed-size and `to` itself (so that it overlaps its neighbor, not equals it)."""
    b = bytearray(f.bytes())
    begin = struct.unpack_from(">I", b, f.at["begin", to])[0]
    struct.pack_into(">I", b, f.at["begin", k], begin + (4 if k == to + 1 and not f.is_record(f.vars[k]) else 0))
    return bytes(b)


def rejects() -> dict[str, bytes]:
    def small(v: int = 1) -> File:
        return File(v, [("d", 2)], [], [Var("a", ["d"], INT, [1, 2])])

    two = File(1, [("d", 2)], [], [Var("a", ["d"], INT, [1, 2]), Var("b", ["d"], INT, [3, 4])])
    recs = File(1, [("t", None)], [], [Var("a", ["t"], INT, [1, 2]), Var("b", ["t"], INT, [3, 4])], records=2)
    mixed = File(1, [("t", None), ("d", 2)], [], [Var("f", ["d"], INT, [1, 2]), Var("r", ["t"], INT, [3, 4])],
                 records=2)
    one = small()
    one.bytes()
    return {
        "header_truncated": small().bytes()[:30],
        # the dimension list's tag, after the magic number and the record count
        "dimension_tag": patched(small(), (8, ">I", 13)),
        # the attribute's type: after the magic number, the record count, the absent dimension
        # list, the attribute list's tag and count, and the name `x` (its length and 4 bytes)
        "attribute_type": patched(File(1, [], [("x", INT, 1)], []), (32, ">I", 12)),
        "cdf5_type": patched(small(), (("type", 0), ">I", UBYTE)),
        # the variable's dimension id, before its absent attribute list (8 bytes)
        "dimension_id": patched(small(), (one.at["type", 0] - 12, ">I", 7)),
        "unlimited_axis": File(1, [("t", None), ("d", 2)], [], [Var("a", ["d", "t"], INT, np.zeros((2, 0)))]).bytes(),
        "variable_past_end": small().bytes()[:-4],
        "variable_in_header": patched(small(), (("begin", 0), ">I", 8)),
        "too_big": File(5, [("a", 2**33), ("b", 2**33)], [], [Var("v", ["a", "b"], DOUBLE)]).header([0]),
        "variables_overlap": moved(two, 1, 0),
        "slices_overlap": moved(recs, 1, 0),
        "records_past_end": patched(recs, (("numrecs"), ">I", 3)),
        "records_overlap": moved(mixed, 0, 1),
    }


def main() -> None:
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)
    accepted = {"classic": classic(), "64bit_offset": offset64(), "64bit_data": data64(),
                "unpresented": unpresented(), "streaming": streaming(),
                "empty": empty(), "nothing": nothing(), "chunked": chunked()}
    for name, f in accepted.items():
        (OUT / f"netcdf3_{name}.nc").write_bytes(f.bytes())
    for name, b in rejects().items():
        (OUT / f"netcdf3_reject_{name}.nc").write_bytes(b)


if __name__ == "__main__":
    main()
