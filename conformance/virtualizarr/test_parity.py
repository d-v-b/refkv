"""refkv's VirtualiZarr parser against VirtualiZarr's own, on generated fixtures.

Each fixture is parsed twice: by the parser VirtualiZarr has for its format (HDFParser,
NetCDF3Parser, FITSParser, ZarrParser, ZippedZarrParser, ENVIParser) and by
`refkv.virtualizarr.RefkvParser`, with its default layout. The two ManifestStores are compared
aspect by aspect: which arrays they hold, and for each array both hold its shape, data type,
fill value, dimension names, attributes, chunk byte ranges and decoded values, and the root
group's attributes. Each difference that https://github.com/d-v-b/refkv/issues/271 tracks is a
strict xfail in `KNOWN_GAPS`, its reason naming the gap, so that the test fails once the gap
closes and the entry has to go. `just virtualizarr-parity` runs it, and CI's `virtualizarr-parity`
job.
"""

from __future__ import annotations

import functools
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import requires
from pathlib import Path
from typing import Any

import h5py
import numpy as np
import pytest
import xarray as xr
import zarr
import zarr.storage
from astropy.io import fits
from obspec_utils.registry import ObjectStoreRegistry
from obstore.store import LocalStore
from packaging.requirements import Requirement
from packaging.version import Version
from virtualizarr.manifests import ManifestStore
from virtualizarr.parsers import (
    ENVIParser,
    FITSParser,
    HDFParser,
    NetCDF3Parser,
    ZarrParser,
    ZippedZarrParser,
)

from refkv.virtualizarr import RefkvParser
from refkv.zarrify import zarrify
from refkv.zarrify.common import Rejected

ROOT = Path(__file__).resolve().parents[2]



@dataclass(frozen=True)
class Gap:
    """A gap of https://github.com/d-v-b/refkv/issues/271, by the heading of its item, and the
    exception a comparison fails with while it is open."""

    reason: str
    raises: type[BaseException] = AssertionError


SOURCE_METADATA = Gap("#271 'Separate source metadata from dataset attributes'")
ZARR2_DIMENSIONS = Gap("#271 'Separate source metadata from dataset attributes': refkv keeps a Zarr v2 "
                       "array's _ARRAY_DIMENSIONS as source metadata, ZarrParser makes them its dimension names")
FITS_NAMES = Gap("#271 'Decide array names for FITS': refkv names the primary HDU '0', FITSParser 'PRIMARY'")
NETCDF3 = Gap("#271 'Support the missing formats': netCDF3 is not a file of any supported format", Rejected)
ENVI = Gap("#271 'Support the missing formats': ENVI is not a file of any supported format", Rejected)

ASPECTS = ("arrays", "shape", "data type", "fill value", "dimension names", "attributes",
           "byte ranges", "values", "root attributes")
"""What is compared. `arrays` is the arrays each store holds, `root attributes` the root
group's attributes, and the rest each array both stores hold."""


@dataclass(frozen=True)
class Case:
    """A fixture: the function that writes it into a directory and returns its path, and the
    VirtualiZarr parser of its format."""

    write: Callable[[Path], Path]
    parser: Callable[[], Any]
    group: str | None = None


def dataset() -> xr.Dataset:
    return xr.Dataset(
        {"t": (("time", "lat", "lon"), np.arange(60, dtype="f4").reshape(3, 4, 5), {"units": "K"}),
         "p": (("lat", "lon"), np.arange(20.0).reshape(4, 5))},
        coords={"time": ("time", np.arange(3), {"units": "days since 2000-01-01"}),
                "lat": ("lat", np.arange(4.0)), "lon": ("lon", np.arange(5.0))},
        attrs={"title": "parity"},
    )


def netcdf4(d: Path) -> Path:
    p = d / "nc4.nc"
    dataset().to_netcdf(p, engine="h5netcdf", encoding={"t": {"chunksizes": (1, 2, 5), "zlib": True}})
    return p


def netcdf4_packed(d: Path) -> Path:
    p = d / "nc4_packed.nc"
    xr.Dataset({"s": (("x",), np.arange(6.0) * 0.5)}, coords={"x": np.arange(6)}).to_netcdf(
        p, engine="h5netcdf", encoding={"s": {"dtype": "i2", "scale_factor": 0.5, "_FillValue": -99}})
    return p


def netcdf4_strings(d: Path) -> Path:
    p = d / "nc4_str.nc"
    xr.Dataset({"name": (("x",), np.array(["ab", "cde"], dtype=object))}).to_netcdf(p, engine="h5netcdf")
    return p


def netcdf4_group(d: Path) -> Path:
    p = d / "nc4_groups.nc"
    dataset().to_netcdf(p, engine="h5netcdf", group="g")
    return p


def hdf5_plain(d: Path) -> Path:
    p = d / "plain.h5"
    with h5py.File(p, "w") as f:
        f.create_dataset("a", data=np.arange(16.0).reshape(4, 4), chunks=(2, 2))
    return p


def netcdf3(d: Path) -> Path:
    p = d / "nc3.nc"
    dataset()[["p"]].to_netcdf(p, engine="scipy", format="NETCDF3_64BIT")
    return p


def fits_image(d: Path) -> Path:
    p = d / "a.fits"
    fits.PrimaryHDU(np.arange(12, dtype="i4").reshape(3, 4)).writeto(p)
    return p


def zarr_v2(d: Path) -> Path:
    p = d / "v2.zarr"
    dataset().to_zarr(p, zarr_format=2, consolidated=False)
    return p


def zarr_v3(d: Path) -> Path:
    p = d / "v3.zarr"
    dataset().to_zarr(p, zarr_format=3, consolidated=False)
    return p


def zipped_zarr_v3(d: Path) -> Path:
    p = d / "z.zarr.zip"
    store = zarr.storage.ZipStore(p, mode="w")
    dataset().to_zarr(store, zarr_format=3, consolidated=False)
    store.close()
    return p


def envi(d: Path) -> Path:
    p = d / "e.img"
    p.write_bytes(np.arange(12, dtype="<i2").tobytes())
    (d / "e.hdr").write_text(
        "ENVI\nsamples = 4\nlines = 3\nbands = 1\nheader offset = 0\nfile type = ENVI Standard\n"
        "data type = 2\ninterleave = bsq\nbyte order = 0\n")
    return p


CASES = {
    "netCDF4 chunked and gzip": Case(netcdf4, HDFParser),
    "netCDF4 packed with fill": Case(netcdf4_packed, HDFParser),
    "netCDF4 vlen strings": Case(netcdf4_strings, HDFParser),
    "netCDF4 in a group": Case(netcdf4_group, lambda: HDFParser(group="g"), group="g"),
    "HDF5 without dimension scales": Case(hdf5_plain, HDFParser),
    "netCDF3": Case(netcdf3, NetCDF3Parser),
    "FITS image": Case(fits_image, FITSParser),
    "Zarr v2 directory": Case(zarr_v2, ZarrParser),
    "Zarr v3 directory": Case(zarr_v3, ZarrParser),
    "zipped Zarr v3": Case(zipped_zarr_v3, ZippedZarrParser),
    "ENVI": Case(envi, ENVIParser),
}

ALL = "*"
KNOWN_GAPS: dict[tuple[str, str], Gap] = {
    ("netCDF4 chunked and gzip", "attributes"): SOURCE_METADATA,
    ("netCDF4 chunked and gzip", "root attributes"): SOURCE_METADATA,
    ("netCDF4 packed with fill", "attributes"): SOURCE_METADATA,
    ("netCDF4 packed with fill", "root attributes"): SOURCE_METADATA,
    ("netCDF4 vlen strings", "attributes"): SOURCE_METADATA,
    ("netCDF4 vlen strings", "root attributes"): SOURCE_METADATA,
    ("netCDF4 in a group", "attributes"): SOURCE_METADATA,
    ("netCDF4 in a group", "root attributes"): SOURCE_METADATA,
    ("HDF5 without dimension scales", "attributes"): SOURCE_METADATA,
    ("HDF5 without dimension scales", "root attributes"): SOURCE_METADATA,
    ("zipped Zarr v3", "root attributes"): SOURCE_METADATA,
    ("netCDF3", ALL): NETCDF3,
    ("ENVI", ALL): ENVI,
    ("FITS image", ALL): FITS_NAMES,
    ("Zarr v2 directory", "dimension names"): ZARR2_DIMENSIONS,
    ("Zarr v2 directory", "attributes"): SOURCE_METADATA,
    ("Zarr v2 directory", "root attributes"): SOURCE_METADATA,
    ("Zarr v3 directory", "root attributes"): SOURCE_METADATA,
}
"""The differences #271 tracks, by (case, aspect), `*` for every aspect of a case."""


def gap(case: str, aspect: str) -> Gap | None:
    return KNOWN_GAPS.get((case, aspect)) or KNOWN_GAPS.get((case, ALL))


def params() -> list[Any]:
    out = []
    for case in CASES:
        for aspect in ASPECTS:
            g = gap(case, aspect)
            marks = [pytest.mark.xfail(strict=True, reason=g.reason, raises=g.raises)] if g else []
            out.append(pytest.param(case, aspect, id=f"{case}: {aspect}", marks=marks))
    return out


@pytest.fixture(scope="session")
def registry() -> ObjectStoreRegistry:
    return ObjectStoreRegistry({"file://": LocalStore()})


@pytest.fixture(scope="session")
def parsed(tmp_path_factory: pytest.TempPathFactory, registry: ObjectStoreRegistry) -> Callable[[str], Any]:
    """The two parsers' ManifestStores for a case, each parsed once a session; an exception a
    parser raised stands in for its store, and is raised again by each comparison."""

    @functools.cache
    def parse(case: str) -> tuple[ManifestStore | Exception, ManifestStore | Exception]:
        c = CASES[case]
        url = c.write(tmp_path_factory.mktemp("fixture")).as_uri()
        results: list[ManifestStore | Exception] = []
        for parser in (c.parser(), RefkvParser(group=c.group)):
            try:
                results.append(parser(url, registry))
            except Exception as e:  # noqa: BLE001 (raised again by every comparison)
                results.append(e)
        return results[0], results[1]

    return parse


def manifest_ranges(manifest: Any) -> dict[str, tuple[int, int]]:
    return {k: (v["offset"], v["length"]) for k, v in manifest.dict().items()}


def read(store: ManifestStore, name: str) -> np.ndarray:
    return np.asarray(zarr.open_array(store, path=name, mode="r", zarr_format=3)[...])


def differences(theirs: ManifestStore, ours: ManifestStore, aspect: str) -> list[str]:
    a, b = theirs._group.arrays, ours._group.arrays
    if aspect == "arrays":
        return [] if set(a) == set(b) else [(f"VirtualiZarr's only {sorted(set(a) - set(b))}, "
                                             f"refkv's only {sorted(set(b) - set(a))}")]
    if aspect == "root attributes":
        x = theirs._group.metadata.attributes
        y = ours._group.metadata.attributes
        return [] if x == y else [f"VirtualiZarr's {x!r}, refkv's {y!r}"]
    common = sorted(set(a) & set(b))
    if not common:
        return ["no array in common"]
    out = []
    for name in common:
        x, y = a[name], b[name]
        if aspect == "shape":
            same, shown = x.metadata.shape == y.metadata.shape, (x.metadata.shape, y.metadata.shape)
        elif aspect == "data type":
            same, shown = str(x.metadata.data_type) == str(y.metadata.data_type), (
                x.metadata.data_type, y.metadata.data_type)
        elif aspect == "fill value":
            same, shown = str(x.metadata.fill_value) == str(y.metadata.fill_value), (
                x.metadata.fill_value, y.metadata.fill_value)
        elif aspect == "dimension names":
            same, shown = x.metadata.dimension_names == y.metadata.dimension_names, (
                x.metadata.dimension_names, y.metadata.dimension_names)
        elif aspect == "attributes":
            same, shown = x.metadata.attributes == y.metadata.attributes, (
                x.metadata.attributes, y.metadata.attributes)
        elif aspect == "byte ranges":
            rx, ry = manifest_ranges(x.manifest), manifest_ranges(y.manifest)
            same, shown = rx == ry, (rx, ry)
        else:
            vx, vy = read(theirs, name), read(ours, name)
            same = vx.dtype == vy.dtype and np.array_equal(vx, vy, equal_nan=vx.dtype.kind in "fc")
            shown = (vx, vy)
        if not same:
            out.append(f"{name}: VirtualiZarr's {shown[0]!r}, refkv's {shown[1]!r}")
    return out


@pytest.mark.parametrize(("case", "aspect"), params())
def test_parity(parsed: Callable[[str], Any], case: str, aspect: str) -> None:
    theirs, ours = parsed(case)
    if isinstance(theirs, Exception):
        pytest.fail(f"VirtualiZarr's parser failed, so there is nothing to compare: {theirs!r}")
    if isinstance(ours, Exception):
        raise ours
    assert differences(theirs, ours, aspect) == []


def test_zarrify_takes_file_url(tmp_path: Path, registry: ObjectStoreRegistry) -> None:
    """zarrify takes a local file's file: URL as given."""
    zarrify(hdf5_plain(tmp_path).as_uri(), registry=registry)


def test_numpy_floor() -> None:
    """refkv's lowest numpy is no higher than VirtualiZarr's, so replacing its parsers does not
    raise VirtualiZarr's."""

    def floor(requirements: list[str]) -> str:
        numpy = next(r for r in map(Requirement, requirements) if r.name == "numpy" and not r.marker)
        return next(s.version for s in numpy.specifier if s.operator == ">=")

    ours = tomllib.loads((ROOT / "python" / "pyproject.toml").read_text())["project"]["dependencies"]
    theirs = requires("virtualizarr") or []
    assert Version(floor(ours)) <= Version(floor(theirs))
