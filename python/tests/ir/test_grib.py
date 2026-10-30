"""The GRIB IR: the Rust parser and its schema, and the projection (spec/zarrify/grib.md) against
ecCodes' reading of every accepted fixture: every field is a chunk at the coordinates ecCodes gives
its message (its reference time, step, level and member), and holds ecCodes' values; the mirror
rebuilds the file; an index beside the file changes the reads, not the output; and xarray opens a
grid's group as a dataset. GRIB has no frozen Python reference; rust/refkv-ir/tests/grib.rs checks
the IR and each rejection's message, and python/tests/test_grib_codec.py the codec's values."""

from __future__ import annotations

import calendar
from pathlib import Path

import numpy as np
import pytest
import xarray as xr
import zarr
from ir_helpers import FIXTURES

import refkv.zarr
from refkv import _core
from refkv._narrow import at, mapping, ndarray, sequence, string
from refkv.ir.store import StoreIrOutput
from refkv.zarrify import _zarrify as zarrify
from refkv.zarrify.store import object_url

try:
    import eccodes
except (ImportError, RuntimeError):  # no ecCodes library for this platform
    eccodes = None

GRIB = FIXTURES / "grib"
# every fixture a field of which is presented (grib_jpeg2000's first field is; its 5.40 field is not)
INPUTS = sorted(p for p in GRIB.glob("*.grib2") if "reject" not in p.name)
# the fixtures ecCodes 2.50 cannot decode (python/tests/test_grib_codec.py): their values are the
# codec's, which that test compares with the generator's
UNDECODED = {"grib_png_1bit", "grib_png_2bit", "grib_png_4bit"}
needs_eccodes = pytest.mark.skipif(eccodes is None, reason="no ecCodes library")


def zarrified(path: Path) -> tuple[StoreIrOutput, zarr.Group]:
    fmt, out = zarrify(str(path))
    assert fmt == "grib"
    assert isinstance(out, StoreIrOutput)
    return out, zarr.open_group(refkv.zarr.store(out.to_store()), mode="r", zarr_format=3)


def chunk_bytes(parts: list, directory: Path) -> bytes:
    """A chunk's bytes, from its parts: ranges of the input's files, and literal bytes."""
    out = b""
    for p in parts:
        if isinstance(p, bytes):
            out += p
        else:
            _, key, offset, length = p
            with (directory / key).open("rb") as f:
                f.seek(offset)
                out += f.read(length)
    return out


def ordered(values: np.ndarray, ni: int, nj: int, scan: int) -> np.ndarray:
    """ecCodes' values, in the file's order, in the codec's (spec/conventions.md §12
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#12-grib-messages>)."""
    lines = values.reshape((ni, nj) if scan & 0x20 else (nj, ni)).copy()
    if scan & 0x10:
        lines[1::2] = lines[1::2, ::-1]
    return lines.T.copy() if scan & 0x20 else lines


def read_message(message: bytes, values: bool) -> dict[str, object]:
    """What ecCodes reads of a one-field message: its values (NaN where missing, in the codec's
    order; None unless `values`), reference time and step in seconds, level and member."""
    assert eccodes is not None
    h = eccodes.codes_new_from_message(message)
    try:
        date, time = eccodes.codes_get_long(h, "dataDate"), eccodes.codes_get_long(h, "dataTime")
        reference = calendar.timegm((date // 10000, date // 100 % 100, date % 100, time // 100, time % 100, 0))
        eccodes.codes_set_string(h, "stepUnits", "s")
        level = None
        surface = eccodes.codes_get_long(h, "typeOfFirstFixedSurface")
        if surface != 255 and not eccodes.codes_is_missing(h, "scaledValueOfFirstFixedSurface"):
            scaled = eccodes.codes_get_long(h, "scaledValueOfFirstFixedSurface")
            level = scaled / 10.0 ** eccodes.codes_get_long(h, "scaleFactorOfFirstFixedSurface")
        member = None
        if eccodes.codes_is_defined(h, "perturbationNumber"):
            member = eccodes.codes_get_long(h, "perturbationNumber")
        got = None
        if values:
            eccodes.codes_set(h, "missingValue", 1e300)  # no field holds it
            v = np.array(eccodes.codes_get_values(h), dtype=np.float64)
            v[v == 1e300] = np.nan
            ni, nj, scan = (eccodes.codes_get_long(h, k) for k in ("Ni", "Nj", "scanningMode"))
            got = ordered(v, ni, nj, scan)
        return {"values": got, "reference": reference,
                "step": eccodes.codes_get_long(h, "endStep"), "level": level, "member": member}
    finally:
        eccodes.codes_release(h)


@needs_eccodes
@pytest.mark.parametrize("path", INPUTS, ids=lambda p: p.name)
def test_fields_are_eccodes_at_their_coordinates_and_the_file_rebuilds(path):
    """Every chunk is a message of one field (the field's own, or one made of its sections), which
    ecCodes reads as the variable's values at the chunk's coordinates: the group's `time`, `step`,
    level and `number` there are the message's; every presented field is a chunk; and the mirror
    rebuilds the file, the only source, described in place."""
    out, root = zarrified(path)
    meta = mapping(at(root.attrs, "refkv_zarrified", "grib"))
    unpresented = len(sequence(meta["unpresented"]))
    assert len(out.refs) == int(meta["fields"]) - unpresented
    for key, parts in sorted(out.refs.items()):
        group, variable, _, *coords = key.split("/")
        array = root.get_array(f"{group}/{variable}")
        dims = [string(d) for d in sequence(array.metadata.dimension_names)]
        index = tuple(int(c) for c in coords)
        got = ndarray(array[index[:-2]])
        message = chunk_bytes(parts, path.parent)
        want = read_message(message, path.stem not in UNDECODED)
        coordinate = {d: ndarray(root.get_array(f"{group}/{d}")[...]) for d in dims[:-2]}
        at_index = dict(zip(dims[:-2], index[:-2], strict=True))
        assert coordinate["time"][at_index["time"]] == want["reference"], key
        assert coordinate["step"][at_index["step"]] == want["step"], key
        level = [d for d in dims[2:-2] if d != "number"]
        assert (want["level"] is None) == (not level), key
        if level:
            assert coordinate[level[0]][at_index[level[0]]] == pytest.approx(want["level"]), key
        if "number" in dims:
            assert coordinate["number"][at_index["number"]] == want["member"], key
        if path.stem not in UNDECODED:
            np.testing.assert_array_equal(got, want["values"], err_msg=key)
    rebuilt = _core.rebuild_store(out.entries.get)
    assert {k for k, _, _ in rebuilt} == {path.name}
    for k, size, held in rebuilt:
        assert size == path.stat().st_size, k
        assert held is None, k
        assert object_url(out.url, k) in out.table()[0], k


def test_an_index_changes_the_reads_not_the_output(tmp_path):
    """A NOAA index beside the file is read and held in the mirror, and named in the root's source
    metadata; the hierarchy is the one without it, and the run reads each listed message's head at
    once rather than walking the file."""
    alone = tmp_path / "alone"
    indexed = tmp_path / "indexed"
    for d in (alone, indexed):
        d.mkdir()
    # three messages: grib_simple's three times over, each a whole message
    one = (GRIB / "grib_simple.grib2").read_bytes()
    for d in (alone, indexed):
        (d / "f.grib2").write_bytes(one * 3)
    lines = [f"{k + 1}:{k * len(one)}:d=2026102900:X:surface:anl:\n" for k in range(3)]
    (indexed / "f.grib2.idx").write_text("".join(lines))
    out_a, root_a = zarrified(alone / "f.grib2")
    out_i, root_i = zarrified(indexed / "f.grib2")
    assert at(root_a.attrs, "refkv_zarrified", "grib", "index") is None
    assert at(root_i.attrs, "refkv_zarrified", "grib", "index") == "f.grib2.idx"
    keep = lambda k: not k.startswith("refkv_source") and k != "zarr.json"  # noqa: E731
    assert {k: v for k, v in out_a.entries.items() if keep(k)} == {k: v for k, v in out_i.entries.items() if keep(k)}
    assert out_a.refs == out_i.refs
    # the same field three times: the first in the variable, the others its copies
    assert sorted(k.split("/")[1] for k in out_a.refs) == ["t_surface", "t_surface_2", "t_surface_3"]
    rebuilt = {k: held for k, _, held in _core.rebuild_store(out_i.entries.get)}
    assert rebuilt["f.grib2.idx"] == (indexed / "f.grib2.idx").read_bytes()


def test_xarray_opens_a_grid_as_a_dataset():
    """A grid's group opens in xarray, its time decoded as datetimes and its grid's coordinates
    as degrees."""
    _, root = zarrified(GRIB / "grib_gfs_land.grib2")
    out = zarrify(str(GRIB / "grib_gfs_land.grib2"))[1]
    ds = xr.open_zarr(refkv.zarr.store(out.to_store()), group="grid0", consolidated=False, zarr_format=3)
    assert ds["time"].dtype.kind == "M"
    assert float(ds["latitude"][0]) == 90.0
    assert float(ds["latitude"][-1]) == -90.0
    assert float(ds["longitude"][1]) == 0.25
    (name,) = [v for v in ds.data_vars if v != "valid_time"]
    assert ds[name].dims[-2:] == ("latitude", "longitude")
    assert root is not None
