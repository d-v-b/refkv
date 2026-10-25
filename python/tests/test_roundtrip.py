import itertools

import numpy as np
import xarray as xr
import zarr

import refkv.zarr
from refkv.convert import split_virtual_dataset, write_vzip
from refkv.vzip.store import VZipStore

from helpers import zarrify


def test_roundtrip_single_and_combined(netcdf_files, tmp_path):
    paths, expected = netcdf_files
    vdss = [zarrify(p) for p in paths]
    combined = xr.concat(vdss, dim="time", coords="minimal", compat="override")
    cases = [
        ("one.vzip", vdss[0], xr.open_dataset(paths[0], engine="h5netcdf")),
        ("all.vzip", combined, expected),
    ]
    for (name, vds, exp), page_size, consolidated in itertools.product(
        cases, [None, 256], [False, True]
    ):
        out = tmp_path / f"{page_size}{name}"
        write_vzip(vds, out, page_size=page_size)
        store = refkv.zarr.open(out)
        if consolidated:
            # the root zarr.json lists every array, so opening never lists the archive
            store.list_dir = store.list_prefix = store.list = _no_listing
        ds = xr.open_zarr(store, consolidated=consolidated, zarr_format=3)
        xr.testing.assert_identical(ds.load(), exp.load())


def _no_listing(*args):
    raise AssertionError("listed the archive")


def test_naive_view_is_a_plain_zip(netcdf_files, tmp_path):
    import zipfile

    from refkv.pb import Range

    paths, _ = netcdf_files
    out = tmp_path / "one.vzip"
    write_vzip(zarrify(paths[0]), out)
    with zipfile.ZipFile(out) as zf:
        assert zf.testzip() is None
        ref = Range.decode(zf.read("temp/c/0/0/0"))
    zarr.open_group(refkv.zarr.open(out, resolve=False), mode="r")  # metadata is plain bytes
    assert ref.length > 0


def test_what_xarray_reads_at_open_is_written_late(netcdf_files, tmp_path):
    from refkv.convert import read_at_open

    paths, _ = netcdf_files
    vds = xr.concat([zarrify(p) for p in paths], dim="time", coords="minimal",
                    compat="override")
    assert {k: read_at_open(k, vds) for k in [
        "zarr.json", "temp/zarr.json", "time/c/0", "lat/c/0", "temp/c/0/0/0", "mask/c/0/0",
    ]} == {"zarr.json": True, "temp/zarr.json": True, "time/c/0": True, "lat/c/0": True,
           "temp/c/0/0/0": False, "mask/c/0/0": False}
    for page_size in [None, 256]:
        out = tmp_path / f"{page_size}.vzip"
        write_vzip(vds, out, page_size=page_size)
        s = VZipStore(str(out), resolve=False)
        assert s.classify("__vz__/sources") == "bytes"
        sources = s.entry("__vz__/sources").data_offset
        for k in ["zarr.json", "time/zarr.json", "time/c/0", "lat/c/0", "temp/c/0/0/0"]:
            assert s.classify(k) == "bytes"
            assert (s.entry(k).data_offset > sources) == read_at_open(k, vds), (page_size, k)


def test_loadable_variables_are_written_as_a_dataset_of_their_own(netcdf_files):
    """refkv writes the loadable variables through the class of the dataset it is
    given, without importing xarray, and the bytes are those of
    `xr.Dataset(loadable).to_zarr(...)`: the variables alone, without the coordinate
    roles and attributes of the virtual dataset (a non-index coordinate here, and
    dataset attributes, which would otherwise add `"coordinates": "row"` to `mask`)."""
    paths, _ = netcdf_files
    vds = zarrify(paths[0], ["time", "lat", "lon", "mask"])
    vds = vds.assign_coords(row=("lat", np.arange(30, dtype="i4"))).assign_attrs(extra="x")
    loadable = {k: v for k, v in vds.variables.items() if k != "temp"}
    mem = zarr.storage.MemoryStore()
    xr.Dataset(loadable).to_zarr(mem, zarr_format=3, consolidated=False, mode="a")
    expected = {k: v.to_bytes() for k, v in mem._store_dict.items() if k != "zarr.json"}
    concrete, refs = split_virtual_dataset(vds)
    assert {k: concrete[k] for k in expected} == expected
    assert {k.split("/")[0] for k in refs} == {"temp"}
