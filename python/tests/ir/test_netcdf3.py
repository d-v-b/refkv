"""The netCDF3 profile (spec/zarrify/netcdf3.md
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md>) against netCDF-C, through
netCDF4-python, and scipy's reader of CDF-1 and CDF-2: every variable of every accepted fixture
(fixtures/netcdf3), and of files netCDF-C writes here in each of the three formats, is presented
with the values, shape, data type, dimension names and attributes the readers read, and xarray
opens the zarrified file as it opens the file. The fixtures' chunks, unpresented variables and
source metadata are then checked against what the generator wrote. The rejected fixtures are
test_rejects.py's.

netCDF-C 4.10 cannot read a streaming record count (it loops on CDF-1 and CDF-2, and crashes on
CDF-5), so the streaming fixture is compared with the same file with its record count written out."""

import struct
from pathlib import Path
from typing import Literal

import netCDF4
import numpy as np
import pytest
import scipy.io
import xarray as xr
import zarr
from ir_helpers import FIXTURES
from netcdf3_oracle import array, attrs, own, problems, same

import refkv.zarr
from refkv.pb import parts
from refkv.zarrify import zarrify
from refkv.zarrify.layouts import Layout

NETCDF3 = FIXTURES / "netcdf3"
# the fixtures netCDF-C reads: the unpresented one's names are not UTF-8, and the streaming one's
# record count it cannot read
UNREAD = ("netcdf3_unpresented", "netcdf3_streaming")
READ = sorted(p for p in NETCDF3.glob("*.nc") if "reject" not in p.name and p.stem not in UNREAD)
FORMATS: list[Literal["NETCDF3_CLASSIC", "NETCDF3_64BIT_OFFSET", "NETCDF3_64BIT_DATA"]] = [
    "NETCDF3_CLASSIC", "NETCDF3_64BIT_OFFSET", "NETCDF3_64BIT_DATA"]


def opened(path: Path) -> tuple[zarr.Group, refkv.ReferenceStore]:
    store = zarrify(str(path))
    return zarr.open_group(refkv.zarr.store(store), mode="r"), store


def check(path: Path) -> None:
    root, _ = opened(path)
    with netCDF4.Dataset(path) as ds:
        ds.set_auto_maskandscale(False)
        ds.set_auto_chartostring(False)
        assert problems(path, root, ds) == []
        version = ds.data_model
    if version != "NETCDF3_64BIT_DATA":
        with scipy.io.netcdf_file(path, mmap=False, maskandscale=False) as f:
            for name, v in f.variables.items():
                assert same(np.asarray(array(root, name)[...]), np.asarray(v.data)), name
    # xarray reads the zarrified file as it reads the file: a JSON attribute has no float32, so a
    # variable packed with a float32 `scale_factor` decodes to float64 from Zarr and to float32 from
    # the file, values within float32's precision
    bare = zarrify(str(path), layout=Layout("refkv.example.bare"))
    with (xr.open_dataset(path, engine="netcdf4") as file,
          xr.open_zarr(refkv.zarr.store(bare), consolidated=False, zarr_format=3) as zarrified):
        got, want = zarrified.load(), file.load()
        xr.testing.assert_allclose(got, want, rtol=1e-6)
        assert {k: v.dims for k, v in got.variables.items()} == {k: v.dims for k, v in want.variables.items()}


@pytest.mark.parametrize("path", READ, ids=lambda p: p.name)
def test_fixtures_are_what_netcdf_c_reads(path):
    check(path)


@pytest.fixture(scope="module")
def written(tmp_path_factory) -> list[Path]:
    """Files netCDF-C lays out: each format, fixed-size and record variables of its types, text,
    a scalar, attributes of each type, and one record variable alone without fill."""
    out = []
    d = tmp_path_factory.mktemp("written")
    for fmt in FORMATS:
        p = d / f"{fmt}.nc"
        with netCDF4.Dataset(p, "w", format=fmt) as ds:
            ds.setncattr("title", "written by netCDF-C")
            ds.setncattr("numbers", np.array([1, 2, 3], dtype="i4"))
            ds.setncattr("ratio", np.float32(0.25))
            ds.createDimension("time", None)
            ds.createDimension("y", 3)
            ds.createDimension("x", 5)
            ds.createDimension("nchar", 4)
            t = ds.createVariable("time", "f8", ("time",))
            t.units = "days since 2000-01-01"
            t[:] = np.arange(4) * 0.5
            v = ds.createVariable("v", "i2", ("time", "y", "x"), fill_value=-99)
            v.scale_factor = np.float32(0.1)
            v[:] = np.arange(60).reshape(4, 3, 5) - 20
            v[2, 1, 1] = np.ma.masked
            ds.createVariable("y", "f4", ("y",))[:] = [1.0, 2.0, 3.0]
            ds.createVariable("grid", "f8", ("y", "x"), fill_value=np.nan)[:] = np.linspace(0, 1, 15).reshape(3, 5)
            names = netCDF4.stringtochar(np.array(["ab", "cdef", ""], dtype="S4"))
            ds.createVariable("name", "S1", ("y", "nchar"))[:] = names
            ds.createVariable("scalar", "i4", ())[...] = 42
            ds.createVariable("b", "i1", ("time",))[:] = [-1, 0, 1, 2]
            if fmt == "NETCDF3_64BIT_DATA":
                for name, dtype in [("ub", "u1"), ("us", "u2"), ("ui", "u4"), ("i64", "i8"), ("u64", "u8")]:
                    w = ds.createVariable(name, dtype, ("time", "y"))
                    w[:] = np.iinfo(dtype).max - np.arange(12, dtype=dtype).reshape(4, 3)
                ds.setncattr("big", np.int64(-(2**62)))
        out.append(p)
    p = d / "nofill.nc"
    with netCDF4.Dataset(p, "w", format="NETCDF3_CLASSIC") as ds:
        ds.set_fill_off()
        ds.createDimension("t", None)
        ds.createVariable("only", "i2", ("t",))[:] = [1, 2, 3, 4, 5]
    out.append(p)
    return out


def test_files_netcdf_c_writes(written):
    for p in written:
        check(p)


def test_record_variables_are_a_chunk_a_record():
    root, store = opened(NETCDF3 / "netcdf3_classic.nc")
    temp, flag = array(root, "temp"), array(root, "flag")
    assert temp.chunks == (1, 3, 4)
    assert flag.chunks == (1,)
    meta = own(root)
    # three record variables, each slice padded to 4 bytes: 8, 48 and 4 bytes a record
    assert meta["recsize"] == 8 + 48 + 4
    for name, n in [("temp", 48), ("flag", 2)]:
        begin = own(array(root, name))["begin"]
        for r in range(3):
            key = f"{name}/c/{r}" + "/0/0" * (name == "temp")
            value = store.mapping[key]
            assert not isinstance(value, bytes), key
            (ref,) = parts(value)
            assert (ref.offset, ref.length) == (begin + r * meta["recsize"], n), key
    # one record variable alone: its slices unpadded, one after another
    root, store = opened(NETCDF3 / "netcdf3_64bit_offset.nc")
    meta = own(root)
    assert meta["recsize"] == 6
    assert array(root, "series").chunks == (1, 3)
    assert array(root, "scalar").shape == ()
    assert float(np.asarray(array(root, "scalar")[...])) == 3.5
    assert not isinstance(store.mapping["scalar/c"], bytes)
    # a fixed-size variable past the chunk limit is cut, its edge chunk padded
    root, store = opened(NETCDF3 / "netcdf3_chunked.nc")
    big = array(root, "big")
    assert big.chunks[1] == 1000
    assert big.chunks[0] < 1100
    assert np.array_equal(big[...], (np.arange(1100 * 1000) % 251).astype("i1").reshape(1100, 1000))


def test_streaming_record_count_is_the_records_the_file_holds(tmp_path):
    path = NETCDF3 / "netcdf3_streaming.nc"
    root, _ = opened(path)
    meta = own(root)
    assert (meta["numrecs"], meta["records"]) == ("streaming", 4)
    written_out = tmp_path / "counted.nc"
    b = bytearray(path.read_bytes())
    b[4:8] = struct.pack(">I", 4)
    written_out.write_bytes(bytes(b))
    with netCDF4.Dataset(written_out) as ds:
        ds.set_auto_maskandscale(False)
        assert problems(path, root, ds) == []


def test_source_metadata_and_what_is_not_presented():
    root, _ = opened(NETCDF3 / "netcdf3_unpresented.nc")
    assert sorted(root.array_keys()) == ["v", "w"]
    meta = own(root)
    assert meta["unpresented"] == [
        {"name": "__hidden", "index": 0, "reason": "name"},
        {"name": "refkv_source", "index": 1, "reason": "reserved"},
        {"name": "v", "index": 3, "reason": "duplicate"},
        {"name": {"latin1": "bad\xff"}, "index": 4, "reason": "name"},
        {"name": "a/b", "index": 5, "reason": "name"},
    ]
    assert np.array_equal(array(root, "v")[...], [5, 6])
    # a reserved attribute is recorded, a second of one name skipped
    assert meta["unpresented_attributes"] == {"zarr_conventions": "reserved"}
    assert attrs(root)["ok"] == 1
    assert meta["attributes"]["ok"] == {"type": "NC_INT", "count": 1}
    # a character variable's `_FillValue` of one character is its fill value, and no attribute,
    # since xarray's Zarr backend decodes none on it
    assert array(root, "w").metadata.fill_value == b"x"
    assert "_FillValue" not in attrs(array(root, "w"))
    assert own(array(root, "w"))["unpresented_attributes"] == {"_FillValue": "fill_value"}
    root, _ = opened(NETCDF3 / "netcdf3_classic.nc")
    meta = own(root)
    assert (meta["format"], meta["version"], meta["numrecs"], meta["records"]) == ("classic", 1, 3, 3)
    assert meta["dimensions"][0] == {"name": "time", "length": 0, "unlimited": True}
    assert {"name": "unused", "length": 2} in meta["dimensions"]
    assert attrs(root)["latin"] == {"latin1": "café"}
    assert attrs(root)["nan"] == ["NaN", "Infinity"]
    assert attrs(root)["empty"] == []
    assert attrs(root)["history"] == "written by write_fixtures.py"
    variable = own(array(root, "temp"))
    assert (variable["index"], variable["type"], variable["record"], variable["vsize"]) == (3, "NC_FLOAT", True, 48)
    # fill values: a `_FillValue` of the variable's type, else netCDF's default
    assert array(root, "temp").metadata.fill_value == np.float32(-999.0)
    assert array(root, "mask").metadata.fill_value == -127
    assert array(root, "lat").metadata.fill_value == np.float32(15 * 2.0**119)
    root, _ = opened(NETCDF3 / "netcdf3_64bit_offset.nc")
    # a `_FillValue` of another type than the variable's leaves the default
    assert array(root, "series").metadata.fill_value == -32767
    root, _ = opened(NETCDF3 / "netcdf3_empty.nc")
    assert array(root, "r").shape == (0, 2)
    assert array(root, "r").chunks == (1, 2)
