"""The HDF5 profile (spec/zarrify/hdf5.md
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md>) against h5py, the independent
reader: every dataset of every accepted fixture (fixtures/hdf5) is presented at its path with
the values h5py reads, or listed in its group's `unpresented` with the reason the fixture plants;
each NWB fixture, opened by hdmf-zarr's NWBZarrIO through refkv.zarr, is the NWBFile pynwb's
NWBHDF5IO reads from the source. The rejected fixtures are test_rejects.py's."""

import warnings

import h5py
import numpy as np
import pytest
import zarr
from hdmf_zarr.nwb import NWBZarrIO
from ir_helpers import FIXTURES
from pynwb import NWBHDF5IO

import refkv.zarr
from refkv.zarrify import zarrify

ACCEPTED = sorted(p for p in (FIXTURES / "hdf5").iterdir() if p.suffix in (".h5", ".nwb") and "reject" not in p.name)

# the objects each fixture holds that the hierarchy does not present, by path, with the reason
UNPRESENTED = {
    "hdf5_attributes.h5": {"__reserved": "name", "bad\u00ffname": "name", "named_type": "named_datatype",
                           "refkv_source": "reserved"},
    "hdf5_compounds.h5": {"gap": "datatype", "mixed_order": "datatype"},
    "hdf5_layouts.h5": {"external": "external_storage", "null": "null_dataspace", "virtual": "layout"},
    "hdf5_filters.h5": {"fletcher32": "filter", "lzf": "filter"},
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
def test_datasets_are_what_h5py_reads(path):
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
    names = ("int", "uint64_big", "float", "nan", "array", "text", "texts", "fixed", "latin1", "bool", "empty",
             "compound")
    assert {k: a[k] for k in names} == {
        "int": -3, "uint64_big": str(2**63 + 5), "float": 0.1, "nan": "NaN", "array": [[0, 1, 2], [3, 4, 5]],
        "text": "vlen text", "texts": ["a", "é", ""], "fixed": "fixed", "latin1": {"latin1": "café"}, "bool": True,
        "empty": None, "compound": {"i": 1, "f": 2.5}}
    assert own(opened(FIXTURES / "hdf5" / "hdf5_attributes.h5")[0]["attrs"])["unpresented_attributes"] == {
        "_LINKS": "reserved", "refkv_zarrified": "reserved", "sequence": "datatype"}
    big = opened(FIXTURES / "hdf5" / "hdf5_attributes.h5")[0]
    assert "big" not in big["big"].attrs
    assert own(big["big_and_small"])["unpresented_attributes"] == {"big": "budget"}
    assert big["big_and_small"].attrs["small"] == 1


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
