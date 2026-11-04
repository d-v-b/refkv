"""The HDF5 profile (spec/zarrify/hdf5.md
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md>) against h5py, the independent
reader: every dataset of every accepted fixture (fixtures/hdf5) is presented at its path with
the values h5py reads, or listed in its group's `unpresented` with the reason the fixture plants;
each NWB fixture, opened by hdmf-zarr's NWBZarrIO through refkv.zarr, is the NWBFile pynwb's
NWBHDF5IO reads from the source. The rejected fixtures are test_rejects.py's."""

import json
import math
import warnings

import h5py
import numpy as np
import pytest
import xarray as xr
import zarr
from hdmf_zarr.nwb import NWBZarrIO
from ir_helpers import FIXTURES
from pynwb import NWBHDF5IO
from zarr.core.metadata import ArrayV3Metadata

import refkv.zarr
from refkv._narrow import mapping, number, sequence
from refkv.ir import sniff
from refkv.zarrify import zarrify

USER_BLOCKS = {"hdf5_user_block_512.h5": 512, "hdf5_user_block_1024.h5": 1024, "hdf5_user_block_4096.h5": 4096}
ACCEPTED = sorted(p for p in (FIXTURES / "hdf5").iterdir() if p.suffix in (".h5", ".nwb") and "reject" not in p.name)

# the objects each fixture holds that the hierarchy does not present, by path, with the reason
UNPRESENTED = {
    "hdf5_attributes.h5": {"__reserved": "name", "bad\u00ffname": "name", "named_type": "named_datatype",
                           "refkv_source": "reserved"},
    "hdf5_compounds.h5": {"gap": "datatype", "mixed_order": "datatype"},
    "hdf5_layouts.h5": {"external": "external_storage", "null": "null_dataspace", "virtual": "layout"},
    "hdf5_external.h5": {"absent_file": "external_storage", "past_the_end": "external_storage",
                         "linked_references": "external_reference",
                         "linked_group/references": "external_reference"},
    "hdf5_filters.h5": {"fletcher32": "filter", "lzf": "filter"},
    "hdf5_netcdf4.h5": {"depth": "dimension_only", "x": "dimension_only"},
    "hdf5_links_earliest.h5": {"region": "datatype"},
    "hdf5_links_latest.h5": {"region": "datatype"},
}


def opened(path) -> tuple[zarr.Group, dict]:
    store = zarrify(str(path))
    return zarr.open_group(refkv.zarr.store(store), mode="r"), store.info


def own(node) -> dict:
    return node.attrs.get("refkv_zarrified", {}).get("hdf5", {})


def unpresented(root: zarr.Group) -> dict[str, str]:
    """Every group's `unpresented`, by path."""
    out = {}
    groups = [("", root), *((p, g) for p, g in root.groups())]
    stack = list(groups)
    seen = set()
    while stack:
        prefix, g = stack.pop()
        if prefix in seen:
            continue
        seen.add(prefix)
        for name, why in own(g).get("unpresented", {}).items():
            out[f"{prefix}/{name}".lstrip("/")] = why
        stack.extend((f"{prefix}/{n}".lstrip("/"), c) for n, c in g.groups())
    return out


def h5_value(d: h5py.Dataset):
    """A dataset's values as the hierarchy presents them: strings decoded, references as
    objects, fixed-length strings as their bytes with a trailing axis."""
    s = h5py.check_string_dtype(d.dtype)
    if s is not None and s.length is None:
        return np.asarray(d.asstr()[()], dtype=object)
    if d.dtype.kind == "S":
        return np.frombuffer(np.ascontiguousarray(d[()]).tobytes(), "u1").reshape(*d.shape, d.dtype.itemsize)
    return d[()]


def same_records(f: h5py.File, want: np.ndarray, got: np.ndarray) -> bool:
    for name in want.dtype.names or ():
        w, g = np.ravel(want[name]), np.ravel(got[name])
        if w.dtype.kind == "O":
            def text(v):
                if isinstance(v, h5py.Reference):
                    return f[v].name if v else ""
                return v.decode() if isinstance(v, bytes) else v
            w = np.asarray([text(v) for v in w], dtype=object)
            g = np.asarray(g, dtype=object)
        if not np.array_equal(w, g):
            return False
    return True


@pytest.mark.parametrize("path", ACCEPTED, ids=lambda p: p.name)
def test_datasets_are_what_h5py_reads(path, monkeypatch):
    monkeypatch.chdir(path.parent)  # HDF5 finds external storage from the working directory
    root, info = opened(path)
    assert info["format"] == "hdf5"
    assert unpresented(root) == UNPRESENTED.get(path.name, {})
    with h5py.File(path, "r") as f:
        def visit(name, obj):
            if not isinstance(obj, h5py.Dataset) or isinstance(name, bytes) or name in UNPRESENTED.get(path.name, {}):
                return
            z = root[name]
            assert isinstance(z, zarr.Array), name
            assert z.shape == (*obj.shape, obj.dtype.itemsize) if obj.dtype.kind == "S" else z.shape == obj.shape, name
            got = z[()]
            if h5py.check_ref_dtype(obj.dtype) is not None:
                # the same objects as h5py's references, by the canonical paths
                for r, p in zip(np.ravel(obj[()]), np.ravel(np.asarray(got, dtype=object)), strict=True):
                    assert (f[p] == f[r]) if r else p == "", name
                assert z.attrs["_DTYPE"] == "object_reference", name
                return
            want = h5_value(obj)
            if obj.dtype.names:
                assert same_records(f, want, np.asarray(got)), name
                return
            got = np.asarray(got, dtype=object) if want.dtype.kind == "O" else np.asarray(got)
            assert np.array_equal(want, got, equal_nan=obj.dtype.kind == "f"), name
        f.visititems(visit)


def test_attributes_and_links_are_hdmf_zarr_members():
    root, _ = opened(FIXTURES / "hdf5" / "hdf5_links_earliest.h5")
    assert root.attrs["main"] == {"_REFERENCE": {"source": ".", "path": "/acquisition/alias"}}
    assert root.attrs["pair"] == [{"_REFERENCE": {"source": ".", "path": "/general/devices/probe"}},
                                  {"_REFERENCE": {"source": ".", "path": "/acquisition/alias"}}]
    assert own(root)["unpresented_attributes"] == {"region_attribute": "datatype"}
    es = root["acquisition/es"]
    assert es.attrs["_LINKS"] == [{"name": "dangling", "source": ".", "path": "/nowhere"},
                                  {"name": "device", "source": ".", "path": "/general/devices/probe"},
                                  {"name": "relative", "source": ".", "path": "data"}]
    assert own(es)["external_links"] == [{"name": "external", "type": 64, "file": "other.h5", "path": "/a/b"}]
    # the second hard link to /acquisition/data: its canonical path is /acquisition/alias
    assert root["acquisition"].attrs["_LINKS"] == [{"name": "data", "source": ".", "path": "/acquisition/alias"}]
    a = opened(FIXTURES / "hdf5" / "hdf5_attributes.h5")[0]["attrs"].attrs
    names = ("int", "uint64_big", "float", "array", "text", "texts", "fixed", "latin1", "bool", "empty",
             "compound", "nan_text")
    assert {k: a[k] for k in names} == {
        "int": -3, "uint64_big": str(2**63 + 5), "float": 0.1, "array": [[0, 1, 2], [3, 4, 5]],
        "text": "vlen text", "texts": ["a", "é", ""], "fixed": "fixed", "latin1": {"latin1": "café"}, "bool": True,
        "empty": None, "compound": {"i": 1, "f": 2.5}, "nan_text": "NaN"}
    assert own(opened(FIXTURES / "hdf5" / "hdf5_attributes.h5")[0]["attrs"])["unpresented_attributes"] == {
        "_LINKS": "reserved", "refkv_zarrified": "reserved", "sequence": "datatype"}
    big = opened(FIXTURES / "hdf5" / "hdf5_attributes.h5")[0]
    assert "big" not in big["big"].attrs
    assert own(big["big_and_small"])["unpresented_attributes"] == {"big": "budget"}
    assert big["big_and_small"].attrs["small"] == 1


def test_float_attributes_read_as_h5py_reads_them():
    """The archive holds a float attribute that JSON has no number for as a string; refkv.zarr
    decodes it where the node's datatypes record a float (a string "NaN" stays a string)."""
    path = FIXTURES / "hdf5" / "hdf5_attributes.h5"
    store = zarrify(str(path))
    held = mapping(mapping(json.loads(store.get("attrs/zarr.json") or b"{}"))["attributes"])
    assert [held[k] for k in ("nan", "floats", "half_nan", "compound_inf", "nan_text")] == [
        "NaN", ["0x7fc00001", "-Infinity", "0x80000000", 1.0], "0xfe00", {"f": "Infinity", "s": "NaN"}, "NaN"]
    a = zarr.open_group(refkv.zarr.store(store), mode="r")["attrs"].attrs

    def same(got: float, want: float) -> bool:  # NaN as NaN, -0 as -0
        if math.isnan(got) or math.isnan(want):
            return math.isnan(got) and math.isnan(want)
        return got == want and math.copysign(1, got) == math.copysign(1, want)

    with h5py.File(path, "r") as f:
        want = f["attrs"].attrs
        for name in ("nan", "floats", "half_nan"):
            got = [number(v) for v in sequence(a[name])] if name == "floats" else [number(a[name])]
            assert all(same(g, w) for g, w in zip(got, np.ravel(want[name]).astype(float).tolist(), strict=True)), name
        assert a["compound_inf"] == {"f": math.inf, "s": "NaN"} == {"f": float(want["compound_inf"]["f"]), "s": "NaN"}
    assert a["nan_text"] == "NaN"


@pytest.mark.parametrize("path", [p for p in ACCEPTED if p.suffix == ".nwb"], ids=lambda p: p.name)
def test_nwb_files_read_as_pynwb_reads_the_source(path):
    store = zarrify(str(path))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with NWBZarrIO(refkv.zarr.store(store), "r") as zio, NWBHDF5IO(path, "r") as hio:
            a, b = zio.read(), hio.read()
            for name in ("identifier", "session_description", "session_start_time"):
                assert getattr(a, name) == getattr(b, name), name
            assert sorted(a.acquisition) == sorted(b.acquisition)
            assert sorted(a.stimulus_template) == sorted(b.stimulus_template)
            for k, x in a.stimulus_template.items():
                got, want = number(x.rate), number(b.stimulus_template[k].rate)
                assert got == want or (math.isnan(got) and math.isnan(want)), k
            for k, x in a.acquisition.items():
                assert np.array_equal(np.asarray(x.data[:]), np.asarray(b.acquisition[k].data[:])), k
            assert sorted(a.processing) == sorted(b.processing)
            for table in ("electrodes", "units", "trials"):
                x, y = getattr(a, table), getattr(b, table)
                assert (x is None) == (y is None), table
                if x is not None:
                    assert list(x.colnames) == list(y.colnames), table
                    assert len(x) == len(y), table
            if b.electrodes is not None:
                assert list(a.electrodes["location"][:]) == list(b.electrodes["location"][:])
                assert [g.name for g in a.electrodes["group"][:]] == [g.name for g in b.electrodes["group"][:]]
                es = a.acquisition["raw"]
                assert es.electrodes.table is a.electrodes
            if b.trials is not None:
                # a compound with a reference member: hdmf-zarr's records
                # (each row a list of its references, as both readers give it)
                def rows(t):
                    return [[(r.idx_start, r.count, r.timeseries.name) for r in row] for row in t["timeseries"][:]]
                assert rows(a.trials) == rows(b.trials)
            if b.units is not None:
                assert [list(s) for s in a.units["spike_times"][:]] == [list(s) for s in b.units["spike_times"][:]]
            for name, mod in b.processing.items():
                assert sorted(a.processing[name].data_interfaces) == sorted(mod.data_interfaces)
            if "ophys" in b.processing:
                ps = a.processing["ophys"]["ImageSegmentation"]["PlaneSegmentation"]
                want = b.processing["ophys"]["ImageSegmentation"]["PlaneSegmentation"]
                def masks(t):
                    return [list(map(tuple, m)) for m in t["pixel_mask"][:]]
                assert masks(ps) == masks(want)


def test_the_summary_names_an_nwb_file():
    _, info = opened(FIXTURES / "hdf5" / "nwb_ophys.nwb")
    assert info["nwb"]["version"] == "2.11.0"
    assert set(info["nwb"]["namespaces"]) == {"core", "hdmf-common", "hdmf-experimental"}
    assert info["unpresented"] == {}


def test_an_imaris_file_is_the_ims_profiles():
    _, info = opened(FIXTURES / "ims" / "ims_earliest_uint16_deflate.ims")
    assert info["format"] == "ims"


def test_external_links_and_storage_read_as_h5py_reads_them(monkeypatch):
    path = FIXTURES / "hdf5" / "hdf5_external.h5"
    monkeypatch.chdir(path.parent)  # HDF5 finds external storage from the working directory
    root, _ = opened(path)
    assert own(root)["siblings"] == ["hdf5_external_a.bin", "hdf5_external_b.bin", "hdf5_external_target.h5"]
    with h5py.File(path, "r") as f:
        for name in ("own", "spread", "linked_dataset", "linked_group/chunked"):
            assert np.array_equal(root[name][()], f[name][()]), name  # pyrefly: ignore
        assert [root["linked_group/strings"][i] for i in range(2)] == ["one", "two"]  # pyrefly: ignore
    assert own(root["linked_group"])["external"] == {"file": "hdf5_external_target.h5", "path": "/g"}
    assert own(root["spread"])["external_storage"][1][0] == "hdf5_external_b.bin"


def test_what_a_linked_file_cannot_give_stays_recorded():
    root, _ = opened(FIXTURES / "hdf5" / "hdf5_external.h5")
    linked = {link["name"] for link in own(root)["external_links"]}
    assert {"dangling", "absolute", "not_hdf5", "no_target"} <= linked
    for name in ("dangling", "absolute", "not_hdf5", "no_target"):
        assert name not in root


def test_a_linked_file_follows_no_further():
    root, _ = opened(FIXTURES / "hdf5" / "hdf5_external.h5")
    group = root["linked_group"]
    assert "far" not in group
    assert [link["name"] for link in own(group)["external_links"]] == ["far"]
    assert own(group)["unpresented_attributes"]["self"] == "external_reference"


def test_a_file_that_names_no_sibling_stays_a_file_input():
    assert "siblings" not in own(opened(FIXTURES / "hdf5" / "hdf5_numbers.h5")[0])


@pytest.mark.parametrize(("name", "size"), USER_BLOCKS.items())
def test_a_user_block_is_recorded_and_its_file_sniffed(name, size):
    path = FIXTURES / "hdf5" / name
    root, _ = opened(path)
    assert own(root)["user_block"] == size
    head = path.read_bytes()
    # the host's two reads: a superblock past the first takes the second
    assert sniff(head[:552]) == ("hdf5" if size == 512 else None)
    assert sniff(head[:65600]) == "hdf5"


def test_a_file_without_a_user_block_records_none():
    root, _ = opened(FIXTURES / "hdf5" / "hdf5_numbers.h5")
    assert "user_block" not in own(root)


def test_a_netcdf4_file_reads_as_its_dimensions_and_coordinates():
    path = FIXTURES / "hdf5" / "hdf5_netcdf4.h5"
    store = refkv.zarr.store(zarrify(str(path)))
    for group in (None, "inner"):
        want = xr.open_dataset(path, engine="h5netcdf", group=group, decode_cf=False)
        got = xr.open_zarr(store, group=group, consolidated=False, zarr_format=3, decode_cf=False)
        renamed = {str(n): str(n).removeprefix("_nc4_non_coord_") for n in got.variables}
        got = got.rename_vars(renamed)
        assert set(got.variables) == set(want.variables)
        assert set(got.indexes) == set(want.indexes)
        for name, variable in want.variables.items():
            assert got[name].dims[:variable.ndim] == variable.dims, name
            if variable.dtype.kind != "S":
                assert np.array_equal(got[name].values, variable.values), name
                assert got[name].attrs.get("_FillValue") == variable.attrs.get("_FillValue"), name


def test_a_netcdf4_file_decodes_as_xarray_decodes_it():
    path = FIXTURES / "hdf5" / "hdf5_netcdf4.h5"
    want = xr.open_dataset(path, engine="h5netcdf")
    got = xr.open_zarr(refkv.zarr.store(zarrify(str(path))), consolidated=False, zarr_format=3)
    for name in ("sst", "counts", "time"):
        if name == "time":
            assert np.array_equal(got[name].values, want[name].values), name
        else:  # JSON keeps no float32, so that a scaled variable may decode as float64
            assert np.allclose(got[name].values, want[name].values, equal_nan=True), name


def test_a_netcdf4_dimension_without_a_variable_is_not_presented():
    root, _ = opened(FIXTURES / "hdf5" / "hdf5_netcdf4.h5")
    assert own(root)["unpresented"]["x"] == "dimension_only"
    assert "x" not in root


@pytest.mark.parametrize(("name", "want"), [
    ("sst", ["time", "x"]),
    ("time", ["time"]),
    ("_nc4_non_coord_depth", ["time", "depth"]),
    ("label", ["x", None]),
    ("inner/anomaly", ["time", "y"]),
    ("inner/y", ["y"]),
])
def test_dimension_names_come_from_the_scales(name, want):
    root, _ = opened(FIXTURES / "hdf5" / "hdf5_netcdf4.h5")
    array = root[name]
    assert isinstance(array, zarr.Array)
    assert isinstance(array.metadata, ArrayV3Metadata)
    assert list(array.metadata.dimension_names or []) == want


def test_a_dataset_without_scales_has_no_dimension_names():
    root, _ = opened(FIXTURES / "hdf5" / "hdf5_numbers.h5")
    for _, array in root.arrays():
        assert isinstance(array.metadata, ArrayV3Metadata)
        assert array.metadata.dimension_names is None
