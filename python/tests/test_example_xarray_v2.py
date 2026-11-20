"""The example layout `refkv.example.xarray-v2` (python/src/refkv/zarrify/layouts/examples/xarray_v2.py):
HDF5 and NetCDF-4 files as Zarr v2 with consolidated metadata, opened by xarray and zarr and compared
with what h5netcdf reads of the file and with the base's own arrays."""

import json
import math
import os
import subprocess
import sys
from pathlib import Path

import h5netcdf
import h5py
import imagecodecs.numcodecs
import numpy as np
import pytest
import xarray as xr
import zarr

import refkv.zarr
from refkv.zarrify import zarrify
from refkv.zarrify.layouts import Layout, LayoutRejected, LayoutUsageError, Written, base
from refkv.zarrify.layouts.examples.xarray_v2 import XarrayV2
from refkv.zarrify.layouts.hierarchy import check

ROOT = Path(__file__).resolve().parents[2]
HDF5 = ROOT / "fixtures" / "hdf5"

imagecodecs.numcodecs.register_codecs(verbose=False)  # Zarr v2 reads imagecodecs_lzf only once registered


def netcdf(path: Path) -> Path:
    """A NetCDF-4 file with a subgroup, compressed variables with missing chunks of their
    `_FillValue`, strings, and a non-finite attribute and an integer beyond 2^53."""
    with h5netcdf.File(path, "w") as f:
        f.dimensions = {"time": 6, "lat": 3}
        f.attrs["title"] = "made by the test"
        f.attrs["version"] = np.int64(2**60 + 1)
        t = f.create_variable("time", ("time",), "f8")
        t[:] = np.arange(6.0)
        t.attrs["units"] = "hours since 2001-01-01"
        v = f.create_variable("temp", ("time", "lat"), "f4", chunks=(2, 3), compression="gzip", shuffle=True,
                              fillvalue=np.float32(np.nan))
        v[:4] = np.arange(12, dtype="f4").reshape(4, 3)  # the last chunk is never written
        v.attrs["scale_factor"] = 0.25
        v.attrs["valid_range"] = np.array([-50, 50], "f4")
        v.attrs["actual_max"] = np.inf
        c = f.create_variable("count", ("time",), "i4", chunks=(3,), compression="gzip", fillvalue=np.int32(-1))
        c[:3] = [1, 2, 3]
        s = f.create_variable("station", ("lat",), h5py.string_dtype())
        s[:] = np.array(["a", "bb", "ccc"], dtype=object)
        g = f.create_group("sub")
        g.dimensions = {"z": 2}
        g.create_variable("z", ("z",), "i2")[:] = [10, 20]
        d = g.create_variable("depth", ("time", "z"), "f8", chunks=(3, 2), compression="lzf")
        d[:] = np.arange(12.0).reshape(6, 2)
    return path


# the plugin's output for a file, as bytes to compare across processes
OUTPUT = """
import sys
from refkv.zarrify.layouts import base, encode_entries
from refkv.zarrify.layouts.examples.xarray_v2 import XarrayV2
w = XarrayV2().write(base(sys.argv[1]), XarrayV2().options({}))
sys.stdout.buffer.write(encode_entries(w.entries) + "|".join(sorted(w.read_at_open)).encode())
"""


def document(entries, key: str):
    """The JSON document at `key` of an output, parsed."""
    value = entries[key]
    assert isinstance(value, bytes), key
    return json.loads(value)


def groups_of(entries) -> list[str | None]:
    return [None, *sorted(k.removesuffix("/.zgroup") for k in entries if k.endswith("/.zgroup"))]


@pytest.mark.parametrize(("name", "options", "codes", "netcdf4", "left_out"), [
    ("hdf5_netcdf4.h5", {}, set(), True, set()),
    ("made.nc", {}, {"json"}, True, set()),  # actual_max is +inf
    ("hdf5_filters.h5", {}, set(), False, set()),
    ("hdf5_masks.h5", {}, set(), False, set()),
    ("hdf5_third_party.h5", {"unsupported": "skip"}, set(), False,
     {"bitshuffle_bare", "bitshuffle_block", "bitshuffle_lz4"}),
    ("hdf5_layouts.h5", {}, set(), False, set()),
    ("hdf5_strings.h5", {}, set(), False, set()),
    ("hdf5_compounds.h5", {"unsupported": "skip"}, set(), False, {"nested"}),
    ("hdf5_numbers.h5", {}, set(), False, set()),
    ("hdf5_attributes.h5", {}, {"json"}, False, set()),
    ("hdf5_attributes.h5", {"nonfinite": "text"}, set(), False, set()),
    ("nwb_ecephys.nwb", {}, set(), False, set()),
    ("nwb_ophys.nwb", {}, {"json"}, False, set()),  # a NaN starting_time rate
])
def test_files_as_zarr_v2_for_xarray(tmp_path, name, options, codes, netcdf4, left_out):
    path = str(netcdf(tmp_path / name) if name == "made.nc" else HDF5 / name)
    b = base(path)
    plugin = XarrayV2()
    written = plugin.write(b, plugin.options(options))
    assert isinstance(written, Written)
    entries = written.entries
    assert {p.code for p in check(b, written)} == codes
    assert sorted(written.read_at_open or ()) == sorted(k for k in (".zattrs", ".zgroup", ".zmetadata") if k in entries)
    consolidated = document(entries, ".zmetadata")["metadata"]
    assert consolidated == {k: document(entries, k) for k in entries if k.rpartition("/")[2] in
                            (".zgroup", ".zarray", ".zattrs")}
    arrays = {k.removesuffix("/.zarray") for k in entries if k.endswith("/.zarray")}
    assert not arrays & left_out
    assert all("_ARRAY_DIMENSIONS" in consolidated[f"{a}/.zattrs"] for a in arrays)

    zarrified = zarrify(path, layout=Layout(XarrayV2(), options))
    assert zarrified.read_at_open == set(written.read_at_open or ())
    zarrified.save_vzip(tmp_path / "out.vzip")
    store = refkv.zarr.open(tmp_path / "out.vzip", policy=zarrified.policy)
    for g in groups_of(entries):  # each group opens in xarray, as the file does in h5netcdf
        got = xr.open_zarr(store, consolidated=True, group=g).load()
        if netcdf4:
            xr.testing.assert_identical(got, xr.open_dataset(path, engine="h5netcdf", group=g).load())

    base_store = refkv.zarr.store(zarrify(path))
    root = zarr.open_group(store, mode="r", zarr_format=2, use_consolidated=True)
    keys = set(b.keys())
    for a in sorted(arrays):  # every array holds the base's values
        parent, _, leaf = a.rpartition("/")
        source = a if f"{a}/zarr.json" in keys else f"{parent}/_nc4_non_coord_{leaf}".lstrip("/")
        array = root[a]
        assert isinstance(array, zarr.Array)
        got = np.asarray(array[...])
        want = np.asarray(zarr.open_array(base_store, path=source, mode="r")[...])
        if got.dtype.kind == "S":  # a fixed-length string: the base's trailing axis of bytes
            want = np.ascontiguousarray(want).view(got.dtype).reshape(got.shape)
        assert got.shape == want.shape, a
        assert np.array_equal(got, want, equal_nan=got.dtype.kind in "fc"), a

    # the encodings of spec/zarrify/hdf5.md §6
    # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#6-attributes>, undone
    if name == "hdf5_attributes.h5":
        attrs = document(entries, "attrs/.zattrs")
        assert attrs["uint64_big"] == 9223372036854775813
        assert attrs["fixed"] == "fixed"
        assert math.copysign(1.0, attrs["floats"][2]) == -1.0
        if options.get("nonfinite") == "text":
            assert attrs["nan"] == "NaN"
            assert attrs["floats"][0] == "0x7fc00001"
        else:
            assert math.isnan(attrs["nan"])
            assert attrs["floats"][1] == -math.inf
            assert math.isnan(attrs["half_nan"])
        assert attrs["nan_text"] == "NaN"  # a string attribute stays text


def test_the_output_does_not_depend_on_the_hash_seed():
    path = str(HDF5 / "hdf5_netcdf4.h5")
    runs = [subprocess.run([sys.executable, "-c", OUTPUT, path], capture_output=True, check=True,
                           env={**os.environ, "PYTHONHASHSEED": seed}).stdout for seed in ("1", "2")]
    assert runs[0] == runs[1]
    assert b"sst/.zarray" in runs[0]


# ---- the errors: one test each

def test_an_array_whose_codecs_have_no_v2_form_is_rejected():
    with pytest.raises(LayoutRejected, match=r"refkv\.h5_bitshuffle has no Zarr v2 form"):
        zarrify(str(HDF5 / "hdf5_third_party.h5"), layout=Layout(XarrayV2()))


def test_a_nested_struct_is_rejected():
    with pytest.raises(LayoutRejected, match="nested: a struct nested in a struct"):
        zarrify(str(HDF5 / "hdf5_compounds.h5"), layout=Layout(XarrayV2()))


def test_missing_chunks_of_a_fill_value_other_than_fillvalue_are_rejected(tmp_path):
    with h5py.File(tmp_path / "f.h5", "w") as f:
        d = f.create_dataset("x", shape=(4,), chunks=(2,), dtype="f4", fillvalue=0)
        d[:2] = 1
        d.attrs["_FillValue"] = np.float32(-1)
    with pytest.raises(LayoutRejected, match=r"not the _FillValue -1\.0"):
        zarrify(str(tmp_path / "f.h5"), layout=Layout(XarrayV2()))


def test_missing_chunks_of_a_nonzero_fill_value_that_is_not_a_number_are_rejected(tmp_path):
    record = np.dtype([("a", "<i4"), ("b", "<f8")])
    with h5py.File(tmp_path / "f.h5", "w") as f:
        d = f.create_dataset("x", shape=(4,), chunks=(2,), dtype=record, fillvalue=np.array((5, 0.5), record))
        d[:2] = np.array([(1, 1.0), (2, 2.0)], record)
    with pytest.raises(LayoutRejected, match="writes only for numbers"):
        zarrify(str(tmp_path / "f.h5"), layout=Layout(XarrayV2()))


def test_missing_chunks_of_a_nan_fill_value_with_payload_bits_are_rejected(tmp_path):
    with h5py.File(tmp_path / "f.h5", "w") as f:
        d = f.create_dataset("x", shape=(4,), chunks=(2,), dtype="f4",
                             fillvalue=np.array([0x7FC00001], "u4").view("f4")[0])
        d[:2] = 1
    with pytest.raises(LayoutRejected, match="a NaN fill value with other bits"):
        zarrify(str(tmp_path / "f.h5"), layout=Layout(XarrayV2()))


def test_an_unknown_option_is_a_usage_error():
    with pytest.raises(LayoutUsageError, match="unknown options"):
        zarrify(str(HDF5 / "hdf5_netcdf4.h5"), layout=Layout(XarrayV2(), {"consolidated": False}))


@pytest.mark.parametrize("options", [{"unsupported": "drop"}, {"nonfinite": None}])
def test_an_option_of_another_value_is_a_usage_error(options):
    with pytest.raises(LayoutUsageError, match=f"{next(iter(options))} is"):
        zarrify(str(HDF5 / "hdf5_netcdf4.h5"), layout=Layout(XarrayV2(), options))
