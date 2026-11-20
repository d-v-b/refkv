"""The stored arrays of the netCDF3 fixtures (spec/stored-array.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>), compared with
netCDF-C through netCDF4-python, the reference reader. Every variable of an accepted file has one
description, keyed by the `header` element at its position in the header, and of it:

- its first declaration is the 4 bytes of the header that hold its `nc_type`, which names the type
  netCDF-C reads;
- its shape is netCDF-C's, its element size the type's, its grid a unit of the whole array, or of a
  record, along the record axis, and dense, with no fill value: the file stores every unit;
- its rows' bytes, read big-endian as elements of the declared type, are netCDF-C's values.

The streaming fixture is compared with its record count written out, which netCDF-C reads
(test_netcdf3.py says why)."""

import struct
from collections import Counter
from pathlib import Path

import netCDF4
import numpy as np
import pytest
from described import described
from ir_helpers import FIXTURES

from refkv.ir.stored import Each

NETCDF3 = FIXTURES / "netcdf3"
FILES = sorted(p for p in NETCDF3.glob("*.nc") if "reject" not in p.name and p.stem != "netcdf3_unpresented")
TYPES = {1: ">i1", 2: "S1", 3: ">i2", 4: ">i4", 5: ">f4", 6: ">f8", 7: ">u1", 8: ">u2", 9: ">u4", 10: ">i8", 11: ">u8"}


def reference(path: Path, tmp: Path) -> Path:
    """The file netCDF-C reads in place of `path`: the streaming fixture with its count written out."""
    if path.stem != "netcdf3_streaming":
        return path
    b = bytearray(path.read_bytes())
    b[4:8] = struct.pack(">I", 4)
    out = tmp / path.name
    out.write_bytes(bytes(b))
    return out


def check(path: Path, tmp: Path) -> tuple[list[str], Counter]:
    d = described(path)
    said: list[str] = []
    seen: Counter = Counter()
    with netCDF4.Dataset(reference(path, tmp)) as ds:
        ds.set_auto_maskandscale(False)
        ds.set_auto_chartostring(False)
        variables = list(ds.variables.values())
        if len(d.stored.arrays) != len(variables):
            return [f"{path.name}: {len(d.stored.arrays)} descriptions of {len(variables)} variables"], seen
        for k, (a, v) in enumerate(zip(d.stored.arrays, variables, strict=True)):
            at = f"{path.name}: {v.name}"
            if d.ir.path(a.element) != "header" or a.index != k:
                said.append(f"{at}: keyed by {d.ir.path(a.element)} {a.index}")
                continue
            code = struct.unpack(">I", d.place(a.declarations[0]))[0]
            dtype = np.dtype(TYPES[code])
            want = np.asarray(v[...])
            kind = dtype.newbyteorder("=") == want.dtype.newbyteorder("=")
            if not kind or a.shape != want.shape or a.item_bits != 8 * dtype.itemsize:
                said.append(f"{at}: {dtype} {a.shape} of {a.item_bits} bits, netCDF-C {want.dtype} {want.shape}")
                continue
            record = v.dimensions[:1] and ds.dimensions[v.dimensions[0]].isunlimited()
            seen["record variables" if record else "fixed-size variables"] += 1
            unit = tuple(x.length if isinstance(x, Each) else 0 for x in a.grid.axes) if a.grid else None
            whole = tuple((1 if record and i == 0 else n) for i, n in enumerate(want.shape))
            if unit != tuple(max(n, 1) for n in whole):
                said.append(f"{at}: the unit {unit}")
            if a.grid is None or a.grid.sparse is not None or len(a.declarations) != 1:
                said.append(f"{at}: a sparse grid, or {len(a.declarations)} declarations")
            if "_FillValue" in v.ncattrs():
                seen["variables with a _FillValue"] += 1
            got = np.zeros(want.shape, dtype)
            rows = d.rows.get(k, [])
            for r in rows:
                within = tuple(min(u, n) for u, n in zip(whole, want.shape, strict=True))
                block = np.frombuffer(d.unit(r), dtype).reshape(within)
                where = tuple(slice(c * u, (c + 1) * u) for c, u in zip(r["coords"], whole, strict=True))
                got[where] = block
            if not rows:
                seen["variables with no rows"] += 1
            if got.tobytes() != np.asarray(want, dtype).tobytes():
                said.append(f"{at}: the rows make another array than netCDF-C reads")
    return said, seen


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_the_descriptions_make_the_variables_netcdf_c_reads(path, tmp_path):
    said, _ = check(path, tmp_path)
    assert not said, "\n".join(said)


def test_the_fixtures_reach_every_case(tmp_path):
    seen: Counter = Counter()
    for path in FILES:
        seen += check(path, tmp_path)[1]
    for case in ("record variables", "fixed-size variables", "variables with a _FillValue", "variables with no rows"):
        assert seen[case], case
