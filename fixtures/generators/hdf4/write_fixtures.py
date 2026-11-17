"""Writes small HDF4 files to fixtures/hdf4/, covering the HDF4 convention and profile
(spec/zarrify/hdf4.md <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md>), with
pyhdf, the HDF4 library's Python binding, and the library itself through ctypes where pyhdf has
no wrapper (`SDsetchunk`, `SDwritechunk`, `SDsetblocksize`, `SDwritedata` of a little-endian
type):

- `hdf4_layouts.hdf`: a dataset of each storage (plain, deflated whole, chunked with plain and with
  deflated chunks, an unwritten chunk among them, each chunk table in linked blocks, one with an
  empty first block; records of an unlimited dimension in linked blocks, past the SDD's size, which
  is patched to lag them as an older library's did), of
  each number type, big- and little-endian, datasets never written (netCDF's fill values, and a
  `_FillValue`), a rank-3 dataset with named dimensions and a dimension scale, and attributes of
  every kind: text padded with NULs, with spaces and exact, an HDF-EOS `StructMetadata.0`,
  numbers one and several, a NaN;
- `hdf4_groups.hdf`: nested Vgroups, a Vgroup and a dataset reached twice, Vgroups that hold each
  other in a loop, a dataset in no Vgroup, Vgroups not presented (a name with `/`, a second
  `outer`) and what they hold, a Vgroup's attributes (its own and an `Attr0.0` member),
  tables, a dataset of text, and names the hierarchy cannot take;
- `hdf4_tables.hdf`: Vdatas of the writer's classes, MO36's `Calibration` table's fields among them
  (integers, a float of order 6, text padded with NULs, with spaces and exact), their attributes,
  records in linked blocks, fields not interlaced, no records, little-endian and mixed-order fields
  (patched, as pyhdf writes only big-endian ones), and a table two Vgroups hold;
- `hdf4_small.hdf`: one plain and two chunked datasets, which write_rejects.py patches into the
  inputs the profile rejects.

The files are committed, and this generator runs on demand: pyhdf's wheels bundle different HDF4
libraries (4.3.0 in the macOS wheel, 4.2.14 in the Linux one), whose files differ in their VERSION
element and their layout, so its output is not the same on every platform. It runs on macOS and
Linux, where ctypes finds the library's functions through pyhdf's extension module. Deflate's bytes are the
zlib the wheel links (the system's on macOS): the deflated datasets compress at level 0, stored
blocks.

python/tests/ir/test_hdf4.py checks the zarrified files against pyhdf.

Usage: uv run python fixtures/generators/hdf4/write_fixtures.py
"""

# fixtures: on demand (pyhdf's HDF4 differs by platform, so the files are committed; `just fixtures` skips it)

from __future__ import annotations

import ctypes
import importlib.util
import struct
from pathlib import Path

import numpy as np
import pyhdf.V  # (HDF.vgstart finds it as an attribute of the package)
import pyhdf.VS  # noqa: F401  (and HDF.vstart this one)
from pyhdf.HDF import HC, HDF
from pyhdf.SD import SD, SDC

OUT = Path(__file__).parents[2] / "hdf4"

# ---- the HDF4 library, through pyhdf's own copy of it

def _extension() -> str:
    """The path of pyhdf's extension module (`pyhdf._hdfext`), which links the HDF4 library."""
    spec = importlib.util.find_spec("pyhdf._hdfext")
    if spec is None or spec.origin is None:
        raise ImportError("pyhdf's extension module is not installed")
    return spec.origin


# the HDF4 library pyhdf's extension module links, through the module's own handle
LIB = ctypes.CDLL(_extension())
LITEND = 0x4000  # hntdefs.h: DFNT_LITEND, a type stored little-endian
HDF_CHUNK, HDF_COMP = 1, 3  # hproto.h: SDsetchunk's flags
DEFLATE = 4  # hcomp.h: COMP_CODE_DEFLATE
LEVEL = 0  # deflate's level: stored blocks, alike from every zlib


class _CompInfo(ctypes.Union):
    _fields_ = [("deflate_level", ctypes.c_int), ("szip", ctypes.c_int32 * 5)]


class _ModelInfo(ctypes.Structure):
    _fields_ = [("nt", ctypes.c_int32), ("ndim", ctypes.c_int), ("dims", ctypes.c_void_p)]


class _Comp(ctypes.Structure):
    _fields_ = [("chunk_lengths", ctypes.c_int32 * 32), ("comp_type", ctypes.c_int32), ("model_type", ctypes.c_int32),
                ("cinfo", _CompInfo), ("minfo", _ModelInfo)]


class _ChunkDef(ctypes.Union):
    """hproto.h's HDF_CHUNK_DEF."""
    _fields_ = [("chunk_lengths", ctypes.c_int32 * 32), ("comp", _Comp)]


LIB.SDsetchunk.argtypes = [ctypes.c_int32, _ChunkDef, ctypes.c_int32]
LIB.SDwritechunk.argtypes = [ctypes.c_int32, ctypes.POINTER(ctypes.c_int32), ctypes.c_void_p]
LIB.SDsetblocksize.argtypes = [ctypes.c_int32, ctypes.c_int32]
LIB.SDwritedata.argtypes = [ctypes.c_int32, ctypes.POINTER(ctypes.c_int32), ctypes.c_void_p,
                            ctypes.POINTER(ctypes.c_int32), ctypes.c_void_p]


def _ok(status: int, what: str) -> None:
    if status != 0:
        raise RuntimeError(f"{what} failed")


def ints(v) -> ctypes.Array:
    return (ctypes.c_int32 * len(v))(*v)


def set_chunk(sds, chunk: list[int], deflate: bool) -> None:
    d = _ChunkDef()
    for k, v in enumerate(chunk):
        d.comp.chunk_lengths[k] = v
    if deflate:
        d.comp.comp_type = DEFLATE
        d.comp.cinfo.deflate_level = LEVEL
    _ok(LIB.SDsetchunk(sds._id, d, HDF_COMP if deflate else HDF_CHUNK), "SDsetchunk")


def write_chunk(sds, at: list[int], values: np.ndarray) -> None:
    v = np.ascontiguousarray(values)
    _ok(LIB.SDwritechunk(sds._id, ints(at), v.ctypes.data), "SDwritechunk")


def write(sds, start: list[int], values: np.ndarray) -> None:
    """Values in the machine's order, which the library converts to the dataset's type."""
    v = np.ascontiguousarray(values)
    _ok(LIB.SDwritedata(sds._id, ints(start), None, ints(list(v.shape)), v.ctypes.data), "SDwritedata")


def ramp(shape, dtype, start=0) -> np.ndarray:
    n = int(np.prod(shape))
    dt = np.dtype(dtype)
    if dt.kind == "f":
        return (np.arange(n, dtype="f8") * 0.25 - 3 + start).astype(dt).reshape(shape)
    info = np.iinfo(dt)
    low = max(int(info.min), -1000)
    return ((np.arange(n) * 7 + start) % (int(info.max) - low) + low).astype(dt).reshape(shape)


# ---- the files

def layouts(path: Path) -> None:
    sd = SD(str(path), SDC.WRITE | SDC.CREATE | SDC.TRUNC)
    # file attributes: text padded each way, HDF-EOS's structure text, numbers
    sd.attr("title_nul").set(SDC.CHAR8, "padded\0\0\0")
    sd.attr("title_spaces").set(SDC.CHAR8, "padded   ")
    sd.attr("title_exact").set(SDC.CHAR8, "exact")
    sd.attr("StructMetadata.0").set(SDC.CHAR8, "GROUP=SwathStructure\nEND_GROUP=SwathStructure\nEND\n" + "\0" * 64)
    sd.attr("versions").set(SDC.INT32, [1, 2, 3])
    sd.attr("ratio").set(SDC.FLOAT64, 0.5)

    plain = sd.create("plain", SDC.INT16, [4, 5])
    plain[:] = ramp([4, 5], "i2")
    plain.attr("units").set(SDC.CHAR8, "kelvin")
    plain.attr("valid_range").set(SDC.INT16, [-100, 100])
    plain.attr("scale_factor").set(SDC.FLOAT64, 0.01)
    plain.attr("missing").set(SDC.FLOAT32, float("nan"))
    plain.endaccess()

    # a float dataset whose _FillValue is text: an attribute of its text, not a fill value
    textfill = sd.create("text_fill", SDC.FLOAT32, [2])
    textfill[:] = np.array([1.5, 2.5], "f4")
    textfill.attr("_FillValue").set(SDC.CHAR8, "-9999")
    textfill.endaccess()

    deflated = sd.create("deflated", SDC.FLOAT32, [6, 7])
    deflated.setcompress(SDC.COMP_DEFLATE, LEVEL)
    deflated[:] = ramp([6, 7], "f4")
    deflated.setfillvalue(-1.5)
    deflated.endaccess()

    # two chunked datasets, their chunks written in turn, so that each chunk table's records are
    # linked blocks (the second's first block empty, as in MODIS files); chunk [1, 1] of each is
    # never written
    for name, deflate in (("chunked", False), ("chunked_deflate", True)):
        s = sd.create(name, SDC.INT16, [5, 6])
        set_chunk(s, [2, 4], deflate)
        s.endaccess()
    chunked = [sd.select(name) for name in ("chunked", "chunked_deflate")]
    for at in ([0, 0], [0, 1], [1, 0], [2, 0], [2, 1]):
        for k, s in enumerate(chunked):
            write_chunk(s, at, ramp([2, 4], "i2", 100 * k + 10 * at[0] + at[1]))
    for s in chunked:
        s.endaccess()

    # records of an unlimited dimension, written in two calls with other data between: linked
    # blocks of 48 bytes
    records = sd.create("records", SDC.INT32, [SDC.UNLIMITED, 3])
    _ok(LIB.SDsetblocksize(records._id, 48), "SDsetblocksize")
    records.dim(0).setname("time")
    records.dim(1).setname("station")
    write(records, [0, 0], ramp([3, 3], "i4"))
    between = sd.create("between", SDC.UINT8, [16])
    between[:] = ramp([16], "u1")
    between.endaccess()
    write(records, [3, 0], ramp([4, 3], "i4", 50))
    records.endaccess()

    for name, t, dtype in (("little_int16", SDC.INT16, "<i2"), ("little_float64", SDC.FLOAT64, "<f8"),
                           ("little_uint32", SDC.UINT32, "<u4")):
        s = sd.create(name, t | LITEND, [3, 4])
        write(s, [0, 0], ramp([3, 4], dtype).astype(np.dtype(dtype).newbyteorder("=")))
        s.endaccess()
    for name, t, dtype in (("int8", SDC.INT8, "i1"), ("uint16", SDC.UINT16, "u2"), ("uint32", SDC.UINT32, "u4"),
                           ("float64", SDC.FLOAT64, "f8"), ("uchar8", SDC.UCHAR8, "u1")):
        s = sd.create(name, t, [2, 3])
        s[:] = ramp([2, 3], dtype)
        s.endaccess()

    # datasets never written: netCDF's fill values, or the _FillValue
    for name, t in (("empty_int8", SDC.INT8), ("empty_uint8", SDC.UINT8), ("empty_int16", SDC.INT16),
                    ("empty_uint16", SDC.UINT16), ("empty_int32", SDC.INT32), ("empty_uint32", SDC.UINT32),
                    ("empty_float32", SDC.FLOAT32), ("empty_float64", SDC.FLOAT64), ("empty_uchar8", SDC.UCHAR8)):
        sd.create(name, t, [2, 3]).endaccess()
    filled = sd.create("empty_filled", SDC.INT16, [2, 3])
    filled.setfillvalue(7)
    filled.endaccess()
    sd.create("empty_records", SDC.FLOAT32, [SDC.UNLIMITED, 2]).endaccess()

    # datasets of text: of rank 1 (a scalar of one string), 2 and 3, padded with NULs, with spaces,
    # and exact; chunked whole along the strings, and across them (not presented); never written
    for name, shape, raw in (("text1", [8], b"hello\0\0\0"), ("text2", [3, 4], b"ab\0\0cd  efgh"),
                             ("text3", [2, 2, 3], b"abcde\0fg\0hij")):
        s = sd.create(name, SDC.CHAR8, shape)
        s[:] = np.frombuffer(raw, "S1").reshape(shape)
        s.endaccess()
    for name, shape, chunk in (("text_chunked", [4, 5], [2, 5]), ("text_cut", [4, 6], [2, 3])):
        s = sd.create(name, SDC.CHAR8, shape)
        set_chunk(s, chunk, False)
        for at in ([0, 0], [1, 0], [0, 1], [1, 1])[:4 if chunk[1] < shape[1] else 2]:
            first = 97 + 4 * at[0] + at[1]
            write_chunk(s, at, np.frombuffer(bytes(range(first, first + chunk[0] * chunk[1])), "S1").reshape(chunk))
        s.endaccess()
    sd.create("text_empty", SDC.CHAR8, [2, 3]).endaccess()

    # rank 3, named dimensions, and a scale on one: the coordinate variable `lat`
    cube = sd.create("cube", SDC.FLOAT32, [2, 3, 4])
    cube[:] = ramp([2, 3, 4], "f4")
    for k, name in enumerate(("level", "lat", "lon")):
        cube.dim(k).setname(name)
    cube.dim(1).setscale(SDC.FLOAT32, [-10.0, 0.0, 10.0])
    cube.endaccess()
    sd.end()


def stale_sdd(path: Path, name: bytes, size: int) -> None:
    """Sets the first size of the dataset `name`'s SDD to `size`, as older libraries left the SDD of
    an unlimited dimension behind its records (the library's test.hdf: 7 in the SDD, 9 records)."""
    b = bytearray(path.read_bytes())
    elements, at = {}, 4
    while at:
        ndd, nxt = struct.unpack_from(">hI", b, at)
        for k in range(ndd):
            t, r, o, n = struct.unpack_from(">HHII", b, at + 6 + 12 * k)
            elements[(t, r)] = (o, n)
        at = nxt
    for (t, _), (o, _) in elements.items():
        if t != 1965:
            continue
        count = struct.unpack_from(">H", b, o)[0]
        tags = struct.unpack_from(f">{count}H", b, o + 2)
        refs = struct.unpack_from(f">{count}H", b, o + 2 + 2 * count)
        p = o + 2 + 4 * count
        vg_name = bytes(b[p + 2:p + 2 + struct.unpack_from(">H", b, p)[0]])
        if vg_name == name and 701 in tags:
            sdd, _ = elements[(701, refs[tags.index(701)])]
            struct.pack_into(">I", b, sdd + 2, size)
    path.write_bytes(bytes(b))


def groups(path: Path) -> None:
    sd = SD(str(path), SDC.WRITE | SDC.CREATE | SDC.TRUNC)
    refs = {}
    for name in ("alone", "inner_data", "shared", "a/b", "__hidden", "twin", "refkv_source", "orphan", "under_data",
                 "dup_member"):
        s = sd.create(name, SDC.INT32, [3])
        s[:] = ramp([3], "i4")
        refs[name] = s.ref()
        s.endaccess()
    twin = sd.create("twin", SDC.INT16, [2])
    twin[:] = ramp([2], "i2")
    twin.endaccess()
    # three datasets of one name: two in one Vgroup, the third and the second in its subgroup, the
    # second also in another Vgroup, where it is presented
    for k in range(3):
        s = sd.create("pair", SDC.INT16, [2 + k])
        s[:] = ramp([2 + k], "i2")
        refs[f"pair{k}"] = s.ref()
        s.endaccess()
    text = sd.create("text", SDC.CHAR8, [2, 4])
    text[:] = np.frombuffer(b"abcdefgh", "S1").reshape(2, 4)
    text.endaccess()
    sd.end()

    hdf = HDF(str(path), HC.WRITE)
    v, vs = hdf.vgstart(), hdf.vstart()
    vgs = {}
    for key, name, cls in (("outer", "outer", "Grid"), ("inner", "inner", ""), ("second", "second", "Grid"),
                           ("loop_a", "loop_a", "Loop"), ("loop_b", "loop_b", "Loop"), ("bad", "bad/name", "Grid"),
                           ("under", "under", ""), ("outer2", "outer", "Grid"), ("pairs", "pairs", ""),
                           ("pair_child", "pair_child", ""), ("pair_again", "pair_again", "")):
        vgs[key] = v.create(name)
        vgs[key]._class = cls
    vgs["outer"].insert(vgs["inner"])
    vgs["second"].insert(vgs["inner"])
    vgs["loop_a"].insert(vgs["loop_b"])
    vgs["loop_b"].insert(vgs["loop_a"])
    vgs["inner"].add(HC.DFTAG_NDG, refs["inner_data"])
    # a Vgroup whose name is no node name, and a second of one name: neither is presented, and what
    # they hold is the root's
    vgs["bad"].add(HC.DFTAG_NDG, refs["orphan"])
    vgs["bad"].insert(vgs["under"])
    vgs["under"].add(HC.DFTAG_NDG, refs["under_data"])
    vgs["outer2"].add(HC.DFTAG_NDG, refs["dup_member"])
    # `pairs` holds pair0, pair1, a subgroup that holds pair2 and pair1, then pair1 again: pair1 is
    # recorded once in each of the two groups, and presented in `pair_again`
    vgs["pairs"].add(HC.DFTAG_NDG, refs["pair0"])
    vgs["pairs"].add(HC.DFTAG_NDG, refs["pair1"])
    vgs["pairs"].insert(vgs["pair_child"])
    vgs["pair_child"].add(HC.DFTAG_NDG, refs["pair2"])
    vgs["pair_child"].add(HC.DFTAG_NDG, refs["pair1"])
    vgs["pairs"].add(HC.DFTAG_NDG, refs["pair1"])
    vgs["pair_again"].add(HC.DFTAG_NDG, refs["pair1"])
    vgs["outer"].add(HC.DFTAG_NDG, refs["shared"])
    vgs["second"].add(HC.DFTAG_NDG, refs["shared"])
    vgs["second"].add(HC.DFTAG_NDG, refs["a/b"])
    vgs["second"].add(HC.DFTAG_NDG, refs["__hidden"])
    # the Vgroup's own attribute (a version 4 Vgroup), and an Attr0.0 Vdata member, as HDF-EOS writes
    vgs["outer"].attr("note").set(HC.CHAR8, "grid")
    fv = vs.create("_FV_inner_data", (("AttrValues", HC.INT16, 1),))
    fv._class = "Attr0.0"
    fv.write([[-1]])
    vgs["outer"].insert(fv)
    table = vs.create("table", (("x", HC.INT32, 1), ("y", HC.FLOAT32, 1)))
    table._class = "Table"
    table.write([[1, 0.5], [2, 1.5]])
    vgs["second"].insert(table)
    vgs["outer"].insert(table)  # a table two Vgroups hold, presented once
    slashed = vs.create("x/y", (("v", HC.INT16, 1),))
    slashed._class = "Table"
    slashed.write([[5]])
    vgs["outer"].insert(slashed)  # a table whose name is no node name, two Vgroups hold: recorded once
    vgs["second"].insert(slashed)
    hidden = vs.create("hidden_table", (("v", HC.INT16, 1),))
    hidden._class = "Table"
    hidden.write([[4]])
    vgs["bad"].insert(hidden)  # a table only a Vgroup not presented holds: the root's
    lone = vs.create("lone", (("v", HC.INT16, 1),))
    lone._class = "Lone"
    lone.write([[3]])
    for x in (fv, table, lone, hidden, slashed):
        x.detach()
    for g in vgs.values():
        g.detach()
    v.end()
    vs.end()
    hdf.close()


def elements(b: bytes) -> dict[tuple[int, int], tuple[int, int]]:
    """Each element's (offset, length), by (tag, ref)."""
    out, at = {}, 4
    while at:
        ndd, nxt = struct.unpack_from(">hI", b, at)
        for k in range(ndd):
            t, r, o, n = struct.unpack_from(">HHII", b, at + 6 + 12 * k)
            out[(t, r)] = (o, n)
        at = nxt
    return out


def little_fields(path: Path, name: bytes, fields: list[int]) -> None:
    """Makes the fields `fields` (by index) of the Vdata `name`, all `int16` or `int32`, of the type's
    little-endian form (the flag 0x4000), their values swapped to match: pyhdf writes only big-endian
    fields."""
    b = bytearray(path.read_bytes())
    els = elements(bytes(b))
    for (t, r), (o, _) in els.items():
        if t != 1962:
            continue
        nvert, ivsize, n = struct.unpack_from(">IHH", b, o + 2)
        types = struct.unpack_from(f">{n}H", b, o + 10)
        sizes = struct.unpack_from(f">{n}H", b, o + 10 + 2 * n)
        offs = struct.unpack_from(f">{n}H", b, o + 10 + 4 * n)
        p = o + 10 + 8 * n
        for _ in range(n):
            p += 2 + struct.unpack_from(">H", b, p)[0]
        if bytes(b[p + 2:p + 2 + struct.unpack_from(">H", b, p)[0]]) != name:
            continue
        data, _ = els[(1963, r)]
        for k in fields:
            struct.pack_into(">H", b, o + 10 + 2 * k, types[k] | 0x4000)
            for rec in range(nvert):
                at = data + rec * ivsize + offs[k]
                b[at:at + sizes[k]] = b[at:at + sizes[k]][::-1]
    path.write_bytes(bytes(b))


def tables(path: Path) -> None:
    """Vdatas of the writer's classes: MO36's `Calibration` table's fields (integers, a float of order
    6, text padded with NULs, with spaces and exact) and its attributes; records in linked blocks;
    fields not interlaced; no records; little-endian and mixed-order fields; a table two Vgroups hold."""
    hdf = HDF(str(path), HC.WRITE | HC.CREATE | HC.TRUNC)
    vs, v = hdf.vstart(), hdf.vgstart()
    cal = vs.create("Calibration", (("band", HC.INT32, 1), ("coeff", HC.FLOAT32, 6), ("name", HC.CHAR8, 16),
                                    ("units", HC.CHAR8, 8), ("code", HC.CHAR8, 4)))
    cal._class = "Calibration"
    cal.write([[1, [0.002, -5.0, 0.0, 0.0, 0.0, 0.0], "Chlorophyll", "mg/m^3  ", "ABCD"],
               [2, [1.5, 0.25, -1.0, 2.0, 3.0, 4.0], "sst", "deg_C   ", "EFGH"]])
    cal.attr("note").set(HC.CHAR8, "calibration")
    cal.field("band").attr("units").set(HC.CHAR8, "1")
    # records written in two calls with another table's between: linked blocks
    grown = vs.create("grown", (("x", HC.INT16, 1), ("y", HC.FLOAT64, 1)))
    grown._class = "Series"
    grown.write([[k, k * 0.5] for k in range(3)])
    grown.detach()
    between = vs.create("between", (("v", HC.UINT8, 2),))
    between._class = "Series"
    between.write([[[1, 2]], [[3, 4]]])
    grown = vs.attach("grown", 1)
    grown.seekend()
    grown.write([[k, k * 0.5] for k in range(3, 7)])
    columns = vs.create("columns", (("a", HC.INT32, 1), ("b", HC.CHAR8, 3)))
    columns._class = "Columns"
    columns._interlace = HC.NO_INTERLACE
    columns.write([[10, "abc"], [20, "de"], [30, "f"]])
    bad = vs.create("bad_columns", (("__x", HC.INT16, 1), ("y", HC.INT16, 1)))
    bad._class = "Columns"
    bad._interlace = HC.NO_INTERLACE
    bad.write([[1, 2]])
    bad.detach()
    empty = vs.create("empty", (("v", HC.INT16, 1),))
    empty._class = "Series"
    for name in ("little", "mixed"):
        t = vs.create(name, (("a", HC.INT16, 1), ("b", HC.INT32, 1)))
        t._class = "Series"
        t.write([[1, 2], [-3, 400000]])
        t.detach()
    group, other = v.create("Per File Data"), v.create("Other")
    group._class = other._class = "Grid"
    group.insert(cal)
    other.insert(cal)
    for x in (cal, grown, between, columns, empty, group, other):
        x.detach()
    v.end()
    vs.end()
    hdf.close()
    little_fields(path, b"little", [0, 1])
    little_fields(path, b"mixed", [0])


def small(path: Path) -> None:
    """One plain dataset and two chunked ones whose chunk tables are linked blocks: what the
    rejected inputs are patched from."""
    sd = SD(str(path), SDC.WRITE | SDC.CREATE | SDC.TRUNC)
    plain = sd.create("plain", SDC.INT16, [2, 3])
    plain[:] = ramp([2, 3], "i2")
    plain.endaccess()
    for name in ("a", "b"):
        s = sd.create(name, SDC.INT16, [4, 4])
        set_chunk(s, [2, 2], False)
        s.endaccess()
    chunked = [sd.select(name) for name in ("a", "b")]
    for at in ([0, 0], [0, 1], [1, 0], [1, 1]):
        for s in chunked:
            write_chunk(s, at, ramp([2, 2], "i2", at[0]))
    for s in chunked:
        s.endaccess()
    sd.end()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for p in OUT.glob("*.hdf"):
        if "reject" not in p.name:
            p.unlink()
    layouts(OUT / "hdf4_layouts.hdf")
    stale_sdd(OUT / "hdf4_layouts.hdf", b"records", 2)
    groups(OUT / "hdf4_groups.hdf")
    tables(OUT / "hdf4_tables.hdf")
    small(OUT / "hdf4_small.hdf")
    for p in sorted(OUT.glob("*.hdf")):
        if "reject" not in p.name:
            print(p.name, p.stat().st_size)


if __name__ == "__main__":
    main()
