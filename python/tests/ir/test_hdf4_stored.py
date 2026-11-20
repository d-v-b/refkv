"""The stored arrays of the HDF4 fixtures (spec/stored-array.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>), compared with
pyhdf, the HDF4 library's binding, the reference reader. Every dataset of the SD interface has a
description, keyed by its `Var0.0` Vgroup, whose number type and dimension record are its first
declarations; a description is matched with pyhdf's dataset by the NDG the Vgroup lists. Of each
with a grid, the array its rows, chains and fill value make, each unit decoded and placed by its
coordinates (a chunk at the grid's full size, then cut to the array), is the array the library
reads, in the source's byte order as the number type's class says. A unit of a method the IR names
only by `source` is counted and not decoded.

A second test asserts which cases the fixtures reached."""

import struct
import zlib
from collections import Counter
from functools import cache
from pathlib import Path

import numpy as np
import pytest
from described import described
from hdf4_oracle import NATIVE, values
from ir_helpers import FIXTURES
from pyhdf.error import HDF4Error
from pyhdf.SD import SD, SDC

from refkv.ir.stored import Each, ElementPlace, Given, Source, Value

FILES = sorted(p for p in (FIXTURES / "hdf4").glob("*.hdf") if "reject" not in p.name)
NDG = 720
# the SD interface's value where a dataset has no data and no `_FillValue`: netCDF's default of
# the number type's netCDF type, as `NC_arrayfill` writes it (spec/zarrify/hdf4.md §3
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#3-datasets>)
DEFAULT = {3: 0, 4: 0, 20: -127, 21: 129, 22: -32767, 23: 32769, 24: -2147483647, 25: 2147483649,
           5: 9.9692099683868690e36, 6: 9.9692099683868690e36}


def ndg_of(vgroup: bytes) -> int | None:
    """The NDG a `Var0.0` Vgroup lists among its members: its count, its tags, then its refs."""
    (n,) = struct.unpack_from(">H", vgroup)
    tags = struct.unpack_from(f">{n}H", vgroup, 2)
    refs = struct.unpack_from(f">{n}H", vgroup, 2 + 2 * n)
    return next((r for t, r in zip(tags, refs, strict=True) if t == NDG), None)


def fill_value(s) -> object:
    """The dataset's `_FillValue`, as pyhdf reads it, or None where it has none."""
    try:
        return s.getfillvalue()
    except HDF4Error:
        return None


def decoded(chain_names: list[str], unit: bytes) -> bytes:
    """A unit's bytes taken through its chain, from its last codec to its first."""
    for name in reversed(chain_names):
        if name == "zlib":
            unit = zlib.decompress(unit)
        else:
            raise AssertionError(name)
    return unit


@cache
def check(path: Path) -> tuple[list[str], Counter]:
    """What differs between a file's descriptions and pyhdf, and the cases they met."""
    d = described(path)
    seen: Counter = Counter()
    said: list[str] = []
    sd = SD(str(path), SDC.READ)
    by_ndg = {}
    for k in range(sd.info()[0]):
        s = sd.select(k)
        by_ndg[s.ref()] = s
    for n, a in enumerate(d.stored.arrays):
        at = a.path or ""
        if not at.startswith("elements/1965/") or a.index is not None:
            said.append(f"{at}: not keyed by a Vgroup")
            continue
        ndg = ndg_of(d.ir.raw(a.element) or b"")
        s = by_ndg.get(ndg)
        if s is None:
            said.append(f"{at}: no dataset of the NDG {ndg}")
            continue
        names = [d.ir.path(p.element) for p in a.declarations if isinstance(p, ElementPlace)]
        if len(names) < 2 or not names[0].startswith("elements/106/") or not names[1].startswith("elements/701/"):
            said.append(f"{at}: declarations {names}")
            continue
        nt = d.place(a.declarations[0])
        code, cls = nt[1], nt[3]
        dtype = np.dtype(NATIVE[code])
        dtype = dtype.newbyteorder(">" if dtype.itemsize == 1 or cls in (0, 1) else "<")
        want = values(s)
        seen["datasets"] += 1
        if a.shape != tuple(want.shape) or a.item_bits != 8 * dtype.itemsize:
            said.append(f"{at}: shape {a.shape} of {a.item_bits} bits, pyhdf {want.shape} of {8 * dtype.itemsize}")
            continue
        if a.grid is None:
            seen["datasets with no grid"] += 1
            continue
        shape = tuple(want.shape)
        # the fill value
        if isinstance(a.fill, Given) and isinstance(a.fill.param, Source):
            fill = np.frombuffer(d.place(a.fill.param.place), dtype)[0]
            seen["fill values at a place"] += 1
        elif a.fill == "format_default":
            fill = np.array(DEFAULT[code]).astype(dtype) if code != 4 else np.bytes_(b"\0")
            seen["default fill values"] += 1
        else:
            # a `_FillValue` in another byte order: its Vdata header the last declaration; the
            # dataset must be written whole for the comparison
            if len(names) != 3 or not names[2].startswith("elements/1962/"):
                said.append(f"{at}: fill {a.fill} with the declarations {names}")
            seen["undefined fill values"] += 1
            fill = np.zeros((), dtype)
        # a `_FillValue`'s is the one the library reads (a chunk header's fills unwritten chunks)
        attribute = fill_value(s) if isinstance(a.fill, Given) else None
        if attribute is not None and np.frombuffer(np.asarray(fill, dtype).tobytes(), dtype)[0] != attribute:
            said.append(f"{at}: the fill value {fill}, pyhdf {attribute}")
        unit = tuple(x.length if isinstance(x, Each) else 0 for x in a.grid.axes)
        full = a.grid.edge == "full"
        seen["chunked datasets" if full else "datasets of one unit"] += 1
        grid = [-(-s_ // u_) if u_ else 0 for s_, u_ in zip(shape, unit, strict=True)]
        padded = np.full([g * u for g, u in zip(grid, unit, strict=True)] if full else shape, fill, dtype)
        skipped = False
        for r in d.rows.get(n, []):
            chain = d.stored.chains[a.chains[r["chain"] or 0]]
            names_ = [c.name for c in chain]
            if names_[:1] == ["source"]:
                params = chain[0].parameters
                if params.get("format") != Value("hdf4") or params.get("id") != Value(s.getcompress()[0]):
                    said.append(f"{at}: the codec {chain[0]}, pyhdf's method {s.getcompress()[0]}")
                seen["units of another method"] += 1
                skipped = True
                continue
            seen[f"units in {names_}"] += 1
            if r["element"] is None:
                seen["units in ranges"] += 1
            raw = decoded(names_, d.unit(r))
            within = unit if full else shape
            count = int(np.prod(within))
            block = np.frombuffer(raw[:count * dtype.itemsize], dtype).reshape(within)
            where = tuple(slice(c * u, (c + 1) * u) for c, u in zip(r["coords"], unit, strict=True))
            padded[where] = block
        if not d.rows.get(n):
            seen["datasets with no rows"] += 1
        got = padded[tuple(slice(0, x) for x in shape)]
        if skipped:
            continue
        if got.dtype.kind == "S":
            same = got.tobytes() == want.tobytes()
        else:
            same = np.array_equal(got, want, equal_nan=got.dtype.kind == "f")
        if not same:
            said.append(f"{at}: the rows make another array than pyhdf reads")
    sd.end()
    return said, seen


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_the_descriptions_make_the_datasets_pyhdf_reads(path):
    said, _ = check(path)
    assert not said, "\n".join(said)


def test_the_fixtures_reach_every_case():
    seen: Counter = Counter()
    for path in FILES:
        seen += check(path)[1]
    for case in ("chunked datasets", "datasets of one unit", "fill values at a place", "default fill values",
                 "units in ['zlib']", "units in []", "units in ranges", "datasets with no rows",
                 "units of another method", "undefined fill values"):
        assert seen[case], case
