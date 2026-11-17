"""pyhdf, the HDF4 library's Python binding, as the oracle of the HDF4 profile
(spec/zarrify/hdf4.md <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md>): the
problems of a zarrified HDF4 file, every dataset the SD interface lists compared with what the
hierarchy presents. python/tests/ir/test_hdf4.py runs it on the fixtures, and
js/test/hdf4/verify.py on the browser code's archives of the fixtures or of a corpus.

A dataset is found by its NDG's ref, which pyhdf's `ref()` gives and the array's source metadata
records (`ndg`); one the hierarchy does not present must be listed, with its reason, in the
`unpresented` of a group, by its name, or, in a file the DFSD interface wrote, as one of the NDGs
listed without a name. Each presented one must have pyhdf's shape, values, dimension names
and attributes. pyhdf gives a `char8` attribute's whole buffer, its NULs too; the convention
presents its text to the first NUL (spec/zarrify/hdf4.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#7-attributes>). The values are read
with the library's `SDreaddata` through pyhdf's binding of it, as `get` reads them, because `get`
refuses a little-endian type and a dataset with no elements."""

from __future__ import annotations

import base64
import math
import struct
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pyhdf.VS  # noqa: F401  (HDF.vstart finds it as an attribute of the package)
import zarr
from pyhdf import hdfext
from pyhdf.error import HDF4Error
from pyhdf.HDF import HC, HDF
from pyhdf.SD import SD, SDC
from zarr.core.metadata import ArrayV3Metadata

from refkv._narrow import integer, ndarray, string

# the machine's NumPy type of each HDF4 number type (hntdefs.h), its byte-order flag 0x4000 aside
NATIVE = {3: "u1", 4: "S1", 5: "f4", 6: "f8", 20: "i1", 21: "u1", 22: "i2", 23: "u2", 24: "i4", 25: "u4"}
OWN = "refkv_zarrified"
# the interfaces' own Vdata classes (the library's INTERNAL_HDF_VDS), whose Vdatas are not tables
INTERNAL_VDS = {"DimVal0.0", "DimVal0.1", "Attr0.0", "SDSVar", "CoordVar", "RIATTR0.0N", "RIATTR0.0C"}
# the datasets and tables each fixture (fixtures/hdf4) holds that the hierarchy does not present, by
# name, with the reason its generator plants
UNPRESENTED = {
    "hdf4_groups.hdf": {"a/b": "name", "__hidden": "name", "refkv_source": "reserved", "twin": "duplicate",
                        "pair": "duplicate"},
    "hdf4_layouts.hdf": {"text_cut": "chunks"},
    "hdf4_tables.hdf": {"mixed": "byte_order"},
}


def values(sds) -> np.ndarray:
    """The dataset's values, as the library reads them: in the machine's order, a little-endian
    type's too, which pyhdf's own call reads when it is given the type without its flag."""
    _, rank, dims, data_type, _ = sds.info()
    shape = [dims] if rank == 1 else list(dims)
    if 0 in shape:
        return np.empty(shape, NATIVE[data_type & 0xFF])
    return np.asarray(hdfext._SDreaddata_0(sds._id, data_type & 0xFF, [0] * rank, shape, [1] * rank))


def v3(a: zarr.Array) -> ArrayV3Metadata:
    """An array's metadata, which refkv writes as Zarr v3's."""
    assert isinstance(a.metadata, ArrayV3Metadata), a.path
    return a.metadata


def own(node) -> dict:
    return node.attrs.get(OWN, {}).get("hdf4", {})


def nodes(root: zarr.Group) -> Iterator[tuple[str, zarr.Group | zarr.Array]]:
    """Every node but the IR mirror's, by path, the root first."""
    stack: list[tuple[str, zarr.Group | zarr.Array]] = [("", root)]
    while stack:
        path, node = stack.pop()
        if path == "refkv_source":
            continue
        yield path, node
        if isinstance(node, zarr.Group):
            stack.extend((f"{path}/{name}".lstrip("/"), child) for name, child in node.members())


def text_of(v: str) -> str:
    return v.split("\0", 1)[0]


def attribute_problems(where: str, want: dict, got, float_dataset: bool) -> list[str]:
    """pyhdf's attributes against a node's."""
    problems = []
    extra = set(got) - set(want) - {"zarr_conventions", OWN}
    if extra:
        problems.append(f"{where}: attributes pyhdf does not read: {sorted(extra)}")
    for name, w in want.items():
        if name not in got:
            problems.append(f"{where}: attribute {name!r} absent")
            continue
        g = got[name]
        if name == "_FillValue" and float_dataset and isinstance(g, str):
            g = struct.unpack("<d", base64.b64decode(g))[0]
        if isinstance(w, str):
            ok = text_of(w) == g
        else:
            ws = list(w) if isinstance(w, (list, tuple)) else [w]
            gs = g if isinstance(g, list) else [g]
            ok = len(ws) == len(gs) and all(same_number(a, b) for a, b in zip(ws, gs, strict=True))
        if not ok:
            problems.append(f"{where}: attribute {name!r} {g!r}, pyhdf {w!r}")
    return problems


def same_number(want, got) -> bool:
    if isinstance(got, str):
        got = {"NaN": math.nan, "Infinity": math.inf, "-Infinity": -math.inf}.get(got, got)
    if isinstance(want, float) and isinstance(got, (int, float)):
        return (math.isnan(want) and math.isnan(got)) or want == got or np.float32(want) == np.float32(got)
    return want == got


def problems(path: Path, root: zarr.Group, unpresented: dict[str, str] | None = None) -> list[str]:
    """What the hierarchy `root` of the HDF4 file `path` gets wrong against pyhdf; `unpresented`,
    the datasets the hierarchy may leave out, each with its reason, by name (None: any, with any
    reason, as for a corpus file)."""
    out: list[str] = []
    arrays = {}
    tables: dict[int, tuple[str, zarr.Group | zarr.Array]] = {}
    listed: dict[str, list[str]] = {}
    for p, node in nodes(root):
        meta = own(node)
        if "records" in meta:
            tables[meta["ref"]] = (p, node)
        elif isinstance(node, zarr.Array) and "ndg" in meta:
            arrays[meta["ndg"]] = (p, node)
        for u in meta.get("unpresented", []):
            listed.setdefault(u.get("name", ""), []).append(u["reason"])
    sd = SD(str(path), SDC.READ)
    out += attribute_problems("/", sd.attributes(), root.attrs, False)
    for i in range(sd.info()[0]):
        s = sd.select(i)
        name, rank, _, _, _ = s.info()
        if s.ref() not in arrays:
            # a file without a CDF0.0 Vgroup lists its NDGs, unnamed; the library makes datasets of
            # them and of their scales
            reasons = listed.get(name, []) or listed.get("", [])
            if not reasons:
                out.append(f"{name}: neither presented nor listed")
            elif unpresented is not None and unpresented.get(name) not in reasons:
                out.append(f"{name}: not presented for {reasons}, not {unpresented.get(name)!r}")
            s.endaccess()
            continue
        at, a = arrays.pop(s.ref())
        if unpresented is not None and name in unpresented and unpresented[name] != "duplicate":
            out.append(f"{at}: presented, but expected not to be ({unpresented[name]})")
        if at.rsplit("/", 1)[-1] != name:
            out.append(f"{at}: named {at.rsplit('/', 1)[-1]!r}, pyhdf {name!r}")
        want = values(s)
        names = [s.dim(k).info()[0] for k in range(rank)]
        if want.dtype.kind == "S":
            # text: the strings of the last axis's length, that axis left out (spec/zarrify/hdf4.md
            # §3.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#33-text>)
            want = np.frombuffer(want.tobytes(), f"S{want.shape[-1]}").reshape(want.shape[:-1])
            if own(a).get("text_axis") != names[-1]:
                out.append(f"{at}: text axis {own(a).get('text_axis')!r}, pyhdf {names[-1]!r}")
            names = names[:-1]
        got = ndarray(a[...])
        if got.shape != want.shape or not np.array_equal(got, want, equal_nan=want.dtype.kind == "f"):
            out.append(f"{at}: values differ (shape {got.shape}, pyhdf {want.shape})")
        dimension_names = v3(a).dimension_names
        if list(dimension_names or []) != names:
            out.append(f"{at}: dimension names {dimension_names}, pyhdf {names}")
        out += attribute_problems(at, s.attributes(), a.attrs, want.dtype.kind == "f")
        s.endaccess()
    sd.end()
    out += [f"{p}: an array pyhdf does not list" for p, _ in arrays.values()]
    return out + table_problems(path, tables, listed, unpresented)


def records(vd, n: int) -> np.ndarray:
    """A Vdata's `n` records as the library reads them (`VSread`, its fields interlaced, in the
    machine's order), as a NumPy structured array: pyhdf's `read` refuses a little-endian field."""
    fields = [vd.field(k) for k in range(len(vd._fields))]
    dtype = np.dtype([(f._name, f"S{f._order}" if f._type & 0xFF == 4 else (NATIVE[f._type & 0xFF], (f._order,)))
                      for f in fields])
    if n == 0:
        return np.empty(0, dtype)
    hdfext.VSsetfields(vd._id, ",".join(f._name for f in fields))
    buf = hdfext.array_byte(n * dtype.itemsize)
    if hdfext.VSread(vd._id, buf, n, 0) != n:
        raise RuntimeError("VSread failed")
    return np.frombuffer(bytes(buf[k] for k in range(n * dtype.itemsize)), dtype)


def field_values(got: np.ndarray, order: int) -> np.ndarray:
    """A field's values as a structured array gives them: a nested struct of `0` … `order − 1` as a
    trailing axis."""
    if got.dtype.names:
        return np.stack([got[str(k)] for k in range(order)], axis=-1)
    return got.reshape(*got.shape, 1) if order == 1 and got.dtype.kind != "S" else got


def vdata_refs(vs) -> list[int]:
    """Every Vdata's ref, by the library's `VSgetid`: pyhdf's `vdatainfo` fails on a Vdata whose
    records were never written."""
    refs, ref = [], -1
    while True:
        try:
            ref = integer(vs.next(ref))
        except HDF4Error:
            return refs
        refs.append(ref)


def table_problems(path: Path, tables: dict, listed: dict[str, list[str]],
                   unpresented: dict[str, str] | None) -> list[str]:
    """Every Vdata of a writer's class pyhdf lists against the table that presents it
    (spec/zarrify/hdf4.md §4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#4-tables>):
    its records, field by field, and its attributes."""
    out: list[str] = []
    hdf = HDF(str(path), HC.READ)
    vs = hdf.vstart()
    for ref in vdata_refs(vs):
        vd = vs.attach(ref)
        name, cls, n = string(vd._name), string(vd._class), integer(vd._nrecs)
        if cls in INTERNAL_VDS or cls.startswith("_HDF_CHK_TBL_") or vd._isattr:
            vd.detach()
            continue
        if ref not in tables:
            reasons = listed.get(name, [])
            if not reasons:
                out.append(f"table {name}: neither presented nor listed")
            elif unpresented is not None and unpresented.get(name) not in reasons:
                out.append(f"table {name}: not presented for {reasons}, not {unpresented.get(name)!r}")
            vd.detach()
            continue
        at, node = tables.pop(ref)
        want = records(vd, n)
        for field in want.dtype.names or ():
            column = node if isinstance(node, zarr.Array) else node[field]
            assert isinstance(column, zarr.Array), at
            got = ndarray(column[...])
            got = got[field] if isinstance(node, zarr.Array) else got
            order = integer(vd.field(field)._order)
            w = want[field] if want[field].dtype.kind == "S" else want[field].reshape(n, order)
            if not np.array_equal(field_values(got, order), w):
                out.append(f"{at}: field {field!r} differs")
        attrs = {k: v[2] for k, v in vd.attrinfo().items()}
        out += attribute_problems(at, attrs, node.attrs, False)
        vd.detach()
    vs.end()
    hdf.close()
    return out + [f"{p}: a table pyhdf does not list" for p, _ in tables.values()]
