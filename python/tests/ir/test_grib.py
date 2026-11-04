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
from refkv._narrow import at, integer, mapping, ndarray, sequence, string
from refkv.ir.store import StoreIrOutput
from refkv.zarrify import _zarrify as zarrify
from refkv.zarrify.store import object_url

try:
    import eccodes
except (ImportError, RuntimeError):  # no ecCodes library for this platform
    eccodes = None

GRIB = FIXTURES / "grib"
# every fixture a field of which is presented
INPUTS = sorted(p for p in GRIB.glob("*.grib2") if "reject" not in p.name)
# the fixtures ecCodes 2.50 cannot decode (python/tests/test_grib_codec.py): their values are the
# codec's, which that test compares with the generator's
UNDECODED = {"grib_png_1bit", "grib_png_2bit", "grib_png_4bit"}
needs_eccodes = pytest.mark.skipif(eccodes is None, reason="no ecCodes library")
ROWLESS = -1  # `shape_and_scan`'s Ni and Nj of a reduced grid and 3.101's
# the fixtures of each grid (spec/zarrify/grib.md §2.4
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#24-grids>), and the group's dimensions
GRIDS = {
    "grib_simple": ("latitude", "longitude"),
    "grib_gaussian": ("latitude", "longitude"),
    "grib_gaussian_south": ("latitude", "longitude"),
    "grib_gaussian_regional": ("latitude", "longitude"),
    "grib_cfs_flux_land": ("latitude", "longitude"),
    "grib_gfs_flux_land": ("latitude", "longitude"),
    "grib_rotated": ("rlat", "rlon"),
    "grib_rotated_angle": ("rlat", "rlon"),
    "grib_lambert": ("y", "x"),
    "grib_polar": ("y", "x"),
    "grib_polar_south": ("y", "x"),
    "grib_polar_pole": ("y", "x"),
    "grib_polar_wgs84": ("y", "x"),
    "grib_polar_south_wgs84": ("y", "x"),
    "grib_polar_pole_wgs84": ("y", "x"),
    "grib_seaice_south": ("y", "x"),
    "grib_hrrr_ak_massden": ("y", "x"),
    "grib_nbm_ak_frzspr": ("y", "x"),
    "grib_mercator": ("y", "x"),
    "grib_nbm_hi_vis": ("y", "x"),
    "grib_reduced_gg": ("point",),
    "grib_reduced_bitmap": ("point",),
    "grib_reduced_ll": ("point",),
    "grib_unstructured": ("point",),
}


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
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#12-grib-messages>): a grid with
    no rows' as they are."""
    if ni == ROWLESS:
        return values
    lines = values.reshape((ni, nj) if scan & 0x20 else (nj, ni)).copy()
    if scan & 0x10:
        lines[1::2] = lines[1::2, ::-1]
    return lines.T.copy() if scan & 0x20 else lines


def shape_and_scan(h: int) -> tuple[int, int, int]:
    """A handle's Ni, Nj and scanning mode."""
    assert eccodes is not None
    if eccodes.codes_get_string(h, "gridType") in ("reduced_gg", "reduced_ll", "unstructured_grid"):
        return ROWLESS, ROWLESS, 0
    ni, nj, scan = (eccodes.codes_get_long(h, k) for k in ("Ni", "Nj", "scanningMode"))
    return ni, nj, scan


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
            got = ordered(v, *shape_and_scan(h))
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
    assert len(out.refs) == integer(meta["fields"]) - unpresented
    for key, parts in sorted(out.refs.items()):
        group, variable, _, *coords = key.split("/")
        array = root.get_array(f"{group}/{variable}")
        dims = [string(d) for d in sequence(at(array.metadata.to_dict(), "dimension_names"))]
        index = tuple(int(c) for c in coords)
        rowless = dims[-1] == "point"
        last = -1 if rowless else -2
        got = ndarray(array[index[:last]])
        message = chunk_bytes(parts, path.parent)
        want = read_message(message, path.stem not in UNDECODED)
        coordinate = {d: ndarray(root.get_array(f"{group}/{d}")[...]) for d in dims[:last]}
        at_index = dict(zip(dims[:last], index[:last], strict=True))
        assert coordinate["time"][at_index["time"]] == want["reference"], key
        assert coordinate["step"][at_index["step"]] == want["step"], key
        level = [d for d in dims[2:last] if d != "number"]
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


def eccodes_positions(path: Path, *, unrotate: bool = True) -> tuple[np.ndarray, np.ndarray]:
    """ecCodes' latitude and longitude of each point of the file's one field, in the rotated frame
    unless `unrotate`: [Nj, Ni], or its points. Its positions do not follow alternate rows' reversed
    direction (scanning mode bit 0x10), which is the codec's order anyway."""
    assert eccodes is not None
    h = eccodes.codes_new_from_message(path.read_bytes())
    try:
        if not unrotate:
            eccodes.codes_set(h, "iteratorDisableUnrotate", 1)
        lat = np.array(eccodes.codes_get_array(h, "latitudes"), dtype=np.float64)
        lon = np.array(eccodes.codes_get_array(h, "longitudes"), dtype=np.float64)
        ni, nj, scan = shape_and_scan(h)
        return ordered(lat, ni, nj, scan & ~0x10), ordered(lon, ni, nj, scan & ~0x10)
    finally:
        eccodes.codes_release(h)


def degrees_apart(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """The difference of two longitudes, whichever way round the Earth."""
    return np.abs((a - b + 180.0) % 360.0 - 180.0)


def grid_coordinate(group: zarr.Group, name: str) -> np.ndarray:
    """A grid coordinate's positions: `add_offset + scale_factor × index`, or its values."""
    array = group.get_array(name)
    values = ndarray(array[...]).astype(np.float64)
    attrs = mapping(array.attrs)
    if "scale_factor" in attrs:
        return float(attrs["add_offset"]) + float(attrs["scale_factor"]) * values  # type: ignore[arg-type]
    return values


@needs_eccodes
@pytest.mark.parametrize("name", sorted(k for k, v in GRIDS.items() if v == ("latitude", "longitude")))
def test_latitudes_and_longitudes_are_eccodes(name):
    """A latitude/longitude or Gaussian grid's positions are ecCodes', which takes its longitudes
    from the corners as they are written: CFS's last, 359.062°, is 0.0005° short of the 360 / Ni
    steps the profile gives."""
    path = GRIB / f"{name}.grib2"
    _, root = zarrified(path)
    group = root["grid0"]
    assert isinstance(group, zarr.Group)
    lat, lon = grid_coordinate(group, "latitude"), grid_coordinate(group, "longitude")
    want_lat, want_lon = eccodes_positions(path)
    np.testing.assert_allclose(lat, want_lat[:, 0], atol=1e-5)
    assert np.max(degrees_apart(lon, want_lon[0])) < (1e-3 if name == "grib_cfs_flux_land" else 1e-5)


@needs_eccodes
@pytest.mark.parametrize("name", sorted(k for k, v in GRIDS.items() if v == ("rlat", "rlon")))
def test_rotated_grids_are_eccodes(name):
    """A rotated grid's coordinates are ecCodes' in the rotated frame, and with no angle of rotation
    its CRS takes them to ecCodes' geographic positions. With one, PROJ turns the rotated frame about
    its pole, as FM 92 says, and ecCodes 2.50 subtracts the angle from the geographic longitude: the
    CRS holds the angle as the file does."""
    pyproj = pytest.importorskip("pyproj")
    path = GRIB / f"{name}.grib2"
    _, root = zarrified(path)
    group = root["grid0"]
    assert isinstance(group, zarr.Group)
    rlat, rlon = grid_coordinate(group, "rlat"), grid_coordinate(group, "rlon")
    want_rlat, want_rlon = eccodes_positions(path, unrotate=False)
    np.testing.assert_allclose(rlat, want_rlat[:, 0], atol=1e-6)
    assert np.max(degrees_apart(rlon, want_rlon[0])) < 1e-6
    crs = pyproj.CRS.from_wkt(string(group.attrs["proj:wkt2"]))
    if name == "grib_rotated_angle":
        assert 'PARAMETER["Axis rotation (GRIB convention)",15,' in string(group.attrs["proj:wkt2"])
        return
    to_geographic = pyproj.Transformer.from_crs(crs, crs.source_crs, always_xy=True)
    lon, lat = to_geographic.transform(*np.meshgrid(rlon, rlat))
    want_lat, want_lon = eccodes_positions(path)
    assert np.max(np.abs(lat - want_lat)) < 1e-5
    assert np.max(degrees_apart(lon, want_lon)) < 1e-5


@needs_eccodes
@pytest.mark.parametrize("name", sorted(k for k, v in GRIDS.items()
                                        if v == ("y", "x") and "wgs84" not in k and "south" not in k))
def test_projected_grids_are_eccodes(name):
    """A projected grid's `y` and `x`, through the CRS of its `proj:wkt2`, are ecCodes' latitude
    and longitude of each point. ecCodes computes a polar stereographic grid's on a sphere alone,
    and runs a south pole's away from it: the next two tests cover those."""
    pyproj = pytest.importorskip("pyproj")
    path = GRIB / f"{name}.grib2"
    _, root = zarrified(path)
    group = root["grid0"]
    assert isinstance(group, zarr.Group)
    y, x = grid_coordinate(group, "y"), grid_coordinate(group, "x")
    crs = pyproj.CRS.from_wkt(string(group.attrs["proj:wkt2"]))
    assert crs.is_projected
    to_geographic = pyproj.Transformer.from_crs(crs, crs.geodetic_crs, always_xy=True)
    lon, lat = to_geographic.transform(*np.meshgrid(x, y))
    want_lat, want_lon = eccodes_positions(path)
    assert np.max(np.abs(lat - want_lat)) < 1e-4
    assert np.max(degrees_apart(lon, want_lon)) < 1e-4


def test_a_south_polar_grid_holds_antarctica_and_its_sea_ice():
    """NCEP's sea ice analysis, through the CRS of its `proj:wkt2`: the pole is inside the grid, ice
    lies in the Weddell, Ross and Amundsen Seas in late spring, and none on the open ocean or on
    land, which the analysis leaves at 0. It holds only with `x` and `y` run as spec/zarrify/grib.md
    §4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#4-georeferencing> says."""
    pyproj = pytest.importorskip("pyproj")
    _, root = zarrified(GRIB / "grib_seaice_south.grib2")
    group = root["grid0"]
    assert isinstance(group, zarr.Group)
    y, x = grid_coordinate(group, "y"), grid_coordinate(group, "x")
    crs = pyproj.CRS.from_wkt(string(group.attrs["proj:wkt2"]))
    to_projected = pyproj.Transformer.from_crs(crs.geodetic_crs, crs, always_xy=True)
    ice = ndarray(group.get_array("ci_meanSea")[0, 0, 0])

    def at(lat: float, lon: float) -> float:
        px, py = to_projected.transform(lon, lat)
        return float(ice[round((py - y[0]) / (y[1] - y[0])), round((px - x[0]) / (x[1] - x[0]))])

    assert (round((0.0 - y[0]) / (y[1] - y[0])), round((0.0 - x[0]) / (x[1] - x[0]))) == (360, 300)
    assert at(-68.0, -40.0) > 0.5  # the Weddell Sea
    assert at(-74.0, 178.0) > 0.5  # the Ross Sea
    assert at(-70.0, -115.0) > 0.5  # the Amundsen Sea
    assert at(-50.0, 90.0) == 0.0  # the southern Indian Ocean
    assert at(-50.0, -30.0) == 0.0  # the South Atlantic
    assert at(-78.5, 107.0) == 0.0  # Vostok


@needs_eccodes
@pytest.mark.parametrize("name", sorted(k for k, v in GRIDS.items() if v == ("y", "x")))
def test_a_projected_grids_first_point_is_where_its_crs_puts_it(name):
    """PROJ, through the CRS of `proj:wkt2`, takes the message's first point to the first `x` and
    `y`, on a sphere and on an ellipsoid, to a millimeter."""
    pyproj = pytest.importorskip("pyproj")
    path = GRIB / f"{name}.grib2"
    _, root = zarrified(path)
    group = root["grid0"]
    assert isinstance(group, zarr.Group)
    y, x = grid_coordinate(group, "y"), grid_coordinate(group, "x")
    crs = pyproj.CRS.from_wkt(string(group.attrs["proj:wkt2"]))
    assert eccodes is not None
    h = eccodes.codes_new_from_message(path.read_bytes())
    lat0 = eccodes.codes_get_double(h, "latitudeOfFirstGridPointInDegrees")
    lon0 = eccodes.codes_get_double(h, "longitudeOfFirstGridPointInDegrees")
    eccodes.codes_release(h)
    to_projected = pyproj.Transformer.from_crs(crs.geodetic_crs, crs, always_xy=True)
    px, py = to_projected.transform(lon0, lat0)
    assert abs(px - x[0]) < 1e-3
    assert abs(py - y[0]) < 1e-3


@needs_eccodes
@pytest.mark.parametrize("name", ["grib_reduced_gg", "grib_reduced_bitmap", "grib_reduced_ll"])
def test_reduced_grids_are_points_at_eccodes_positions(name):
    """A reduced grid's variable has one dimension of points, and its `latitude` and `longitude`,
    each over `point`, are ecCodes' positions; xarray reads them as its coordinates."""
    path = GRIB / f"{name}.grib2"
    out, root = zarrified(path)
    group = root["grid0"]
    assert isinstance(group, zarr.Group)
    want_lat, want_lon = eccodes_positions(path)
    np.testing.assert_allclose(ndarray(group.get_array("latitude")[...]), want_lat, atol=1e-5)
    assert np.max(degrees_apart(ndarray(group.get_array("longitude")[...]), want_lon)) < 1e-5
    ds = xr.open_zarr(refkv.zarr.store(out.to_store()), group="grid0", consolidated=False, zarr_format=3)
    (var,) = [v for v in ds.data_vars if v != "valid_time"]
    assert ds[var].dims[-1] == "point"
    assert {"latitude", "longitude"} <= set(ds[var].coords)
    assert "proj:wkt2" in group.attrs
    assert "spatial:dimensions" not in group.attrs


def test_an_unstructured_grid_is_points_and_names_its_grid():
    """A 3.101 field's variable has one dimension of points and no positions; its group names the
    grid that holds them."""
    path = GRIB / "grib_unstructured.grib2"
    _, root = zarrified(path)
    group = root["grid0"]
    assert isinstance(group, zarr.Group)
    assert dict(group.attrs) == {"GRIB_numberOfGridUsed": 26, "GRIB_numberOfGridInReference": 1,
                                 "GRIB_uuidOfHGrid": "a27b8de618c411e4820ab5b098c6a5c0"}
    arrays = dict(group.arrays())
    assert "latitude" not in arrays
    assert "point" not in arrays
    (var,) = [a for k, a in arrays.items()
              if sequence(at(a.metadata.to_dict(), "dimension_names"))[-1] == "point" and k != "valid_time"]
    assert var.shape[-1] == 192


def test_a_gaussian_grid_has_no_affine_transform():
    """A Gaussian grid's latitudes are not evenly spaced: its group gives their values, and its
    transform is left out."""
    _, root = zarrified(GRIB / "grib_gfs_flux_land.grib2")
    group = root["grid0"]
    assert isinstance(group, zarr.Group)
    assert "spatial:transform" not in group.attrs
    assert group.attrs["spatial:dimensions"] == ["latitude", "longitude"]
    assert group.attrs["spatial:shape"] == [1536, 3072]
    assert group.get_array("latitude").dtype == np.float64


def test_an_index_changes_the_reads_not_the_output(tmp_path):
    """A NOAA index beside the file is read and held in the mirror, and named in the root's source
    metadata; the hierarchy is the one without it, and the run reads each listed message's head at
    once rather than walking the file. With two indexes, the first of which lists no offsets, both
    are held and the second, whose offsets the run used, is named."""
    alone = tmp_path / "alone"
    indexed = tmp_path / "indexed"
    both = tmp_path / "both"
    for d in (alone, indexed, both):
        d.mkdir()
    # three messages: grib_simple's three times over, each a whole message
    one = (GRIB / "grib_simple.grib2").read_bytes()
    for d in (alone, indexed, both):
        (d / "f.grib2").write_bytes(one * 3)
    lines = [f"{k + 1}:{k * len(one)}:d=2026102900:X:surface:anl:\n" for k in range(3)]
    (indexed / "f.grib2.idx").write_text("".join(lines))
    (both / "f.grib2.idx").write_bytes(b"not an index\n")
    ecmwf = "".join(f'{{"_offset": {k * len(one)}, "_length": {len(one)}}}\n' for k in range(3))
    (both / "f.index").write_bytes(ecmwf.encode())
    out_a, root_a = zarrified(alone / "f.grib2")
    out_i, root_i = zarrified(indexed / "f.grib2")
    out_b, root_b = zarrified(both / "f.grib2")
    assert at(root_a.attrs, "refkv_zarrified", "grib", "index") is None
    assert at(root_i.attrs, "refkv_zarrified", "grib", "index") == "f.grib2.idx"
    assert at(root_b.attrs, "refkv_zarrified", "grib", "index") == "f.index"
    keep = lambda k: not k.startswith("refkv_source") and k != "zarr.json"  # noqa: E731
    assert {k: v for k, v in out_a.entries.items() if keep(k)} == {k: v for k, v in out_i.entries.items() if keep(k)}
    assert {k: v for k, v in out_a.entries.items() if keep(k)} == {k: v for k, v in out_b.entries.items() if keep(k)}
    assert out_a.refs == out_i.refs == out_b.refs
    held = {k: h for k, _, h in _core.rebuild_store(out_b.entries.get)}
    assert held["f.grib2.idx"] == b"not an index\n"
    assert held["f.index"] == (both / "f.index").read_bytes()
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
