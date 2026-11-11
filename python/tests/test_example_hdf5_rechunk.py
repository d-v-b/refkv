"""The example layout `refkv.example.hdf5-rechunk` (refkv.zarrify.layouts.examples.hdf5_rechunk):
contiguous HDF5 datasets re-tiled into the chunk shapes the options give, read back equal to
h5py's values, and each way it refuses options or an input."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import h5py
import numpy as np
import pytest
import zarr

import refkv.zarr
from refkv.zarrify import zarrify
from refkv.zarrify.layouts import Layout, LayoutRejected, LayoutUsageError, base, encode_entries
from refkv.zarrify.layouts.examples.hdf5_rechunk import Hdf5Rechunk
from refkv.zarrify.layouts.hierarchy import check

ROOT = Path(__file__).resolve().parents[2]
HDF5 = ROOT / "fixtures" / "hdf5"
MIRROR = "refkv_source/"
GENERATED = {  # name: (data, chunk shape asked for), each stored contiguous
    "i16": (np.arange(37 * 29, dtype="<i2").reshape(37, 29) - 500, [8, 8]),  # not a multiple
    "f64be": (np.linspace(-1, 1, 5 * 17 * 13, dtype=">f8").reshape(5, 17, 13), [2, 4, 5]),
    "u8": (np.arange(64 * 64, dtype="u1").reshape(64, 64), [16, 16]),  # a multiple
    "u32_3d": (np.arange(3 * 40 * 50, dtype="<u4").reshape(3, 40, 50), [None, 16, 16]),  # null: the whole axis
    "big": (np.arange(700 * 600, dtype="<f4").reshape(700, 600) / 7, [128, 128]),  # 1.6 MiB: the base slabs it
}


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    """A directory of HDF5 files: `generated.h5`, contiguous datasets of several data types and
    one chunked; `square.h5`, one dataset whose base needs no zero data source."""
    folder = tmp_path_factory.mktemp("rechunk")
    with h5py.File(folder / "generated.h5", "w") as f:
        for name, (data, _) in GENERATED.items():
            f.create_dataset(name, data=data)
        f.create_dataset("group/chunked", data=np.ones((20, 20), "<i4"), chunks=(7, 7), compression="gzip")
        f["group"].attrs["note"] = "kept"
    with h5py.File(folder / "square.h5", "w") as f:
        f.create_dataset("a", data=np.arange(300 * 300, dtype="<f4").reshape(300, 300))
    return folder


def grid(shape, tile, edge):
    """The chunk grid a re-tiled array of `shape` takes for `tile` (null: the whole axis)."""
    tile = [max(1, min(n, n if c is None else c)) for n, c in zip(shape, tile, strict=True)]
    if edge == "pad" or all(n % c == 0 for n, c in zip(shape, tile, strict=True)):
        return {"name": "regular", "configuration": {"chunk_shape": tile}}
    lengths = [c if n % c == 0 else [c] * (n // c) + [n % c] for n, c in zip(shape, tile, strict=True)]
    return {"name": "rectilinear", "configuration": {"kind": "inline", "chunk_shapes": lengths}}


def arrays(group, prefix=""):
    """Every array of a zarr group, by path, but the IR mirror's."""
    out = {}
    for name, node in group.members():
        path = f"{prefix}{name}"
        if path == MIRROR.rstrip("/"):
            continue
        out |= {path: node} if isinstance(node, zarr.Array) else arrays(node, f"{path}/")
    return out


def h5_values(path, names):
    """h5py's values of the datasets `names`, read before zarr opens anything."""
    with h5py.File(path, "r") as f:
        return {n: f[n][...] for n in names}


def document(entries, key):
    """A document of a plugin's entries, parsed."""
    value = entries[key]
    assert isinstance(value, bytes)
    return json.loads(value)


def same(z, h):
    """Whether a re-tiled array's values are h5py's (a fixed-length string's bytes as uint8)."""
    if h.dtype.kind == "S":
        h = np.frombuffer(h.tobytes(), "u1").reshape(z.shape)
    if h.dtype.names:
        return z.tolist() == h.tolist()
    return z.shape == h.shape and np.array_equal(z, h, equal_nan=h.dtype.kind == "f")


CASES = [  # (input, options, checksums, the arrays re-tiled with their chunk shapes)
    ("generated.h5", {"chunks": {f"/{n}" if n == "i16" else n: t for n, (_, t) in GENERATED.items()},
                   "default": [10, 10]}, False, {n: t for n, (_, t) in GENERATED.items()}),
    ("generated.h5", {"chunks": {n: t for n, (_, t) in GENERATED.items()}, "edge": "rectilinear"}, False,
     {n: t for n, (_, t) in GENERATED.items()}),
    ("generated.h5", {"chunks": {"big": [100, 64], "i16": [5, 29]}}, True, {"big": [100, 64], "i16": [5, 29]}),
    ("square.h5", {"chunks": {"a": [256, 256]}}, False, {"a": [256, 256]}),  # pads with a zero source refkv adds
    ("square.h5", {"chunks": {"a": [256, 256]}}, True, {"a": [256, 256]}),
    (HDF5 / "hdf5_numbers.h5", {"default": [64, 64], "chunks": {"d3": [1, 2, 3], "d1/le_i8": [3]}}, False,
     {"bands": [64, 64], "d3": [1, 2, 3], "d1/le_i8": [3]}),
    (HDF5 / "hdf5_numbers.h5", {"chunks": {"bands": [100, 300]}, "edge": "rectilinear"}, False,
     {"bands": [100, 300]}),
    (HDF5 / "hdf5_compounds.h5", {"default": [2]}, False,  # packed structs; records of strings stay copies
     {"bool": [2], "enum": [2], "pixel_mask": [2]}),
    (HDF5 / "hdf5_strings.h5", {"chunks": {"fixed": [2, 2]}, "default": [3]}, False, {"fixed": [2, 2]}),
    (HDF5 / "hdf5_layouts.h5", {"chunks": {"unallocated": [4]}}, False, {"unallocated": [4]}),
    (HDF5 / "hdf5_netcdf4.h5", {"default": [3, 1]}, False,  # sst is chunked: left as it is
     {"_nc4_non_coord_depth": [3, 1], "inner/anomaly": [3, 1], "label": [3, 1]}),
    (HDF5 / "hdf5_external.h5", {"chunks": {"own": [2], "linked_dataset": [2, 2]}}, False,  # a file read as a store,
     {"own": [2], "linked_dataset": [2, 2]}),  # and an external link's target in it
    (HDF5 / "nwb_ophys.nwb", {"chunks": {"stimulus/templates/movie/data": [1, 2, 2],
                                         "processing/ophys/Fluorescence/dff/data": [16, 2]}}, False,
     {"stimulus/templates/movie/data": [1, 2, 2], "processing/ophys/Fluorescence/dff/data": [16, 2]}),
    (HDF5 / "nwb_ecephys.nwb", {"default": [64, 3]}, False, {"acquisition/raw/data": [64, 3]}),
]


@pytest.mark.parametrize(("source", "given", "checksums", "retiled"), CASES)
def test_hdf5_rechunk_re_tiles_contiguous_datasets(generated, source, given, checksums, retiled):
    """The output has no `check` problem; it is the base's hierarchy with each array named in
    `retiled` in chunks of the shape asked for, read back equal to h5py's values, and every other
    array read back equal to the base's."""
    path = generated / source if isinstance(source, str) else source
    plugin = Hdf5Rechunk()
    options = plugin.options(given)
    b = base(str(path), checksums=checksums)
    out = plugin.write(b, options)
    assert check(b, out) == []
    mirror = {k for k in b.keys() if k.startswith(MIRROR)}  # noqa: SIM118 (a Base is no mapping)
    assert mirror == {k for k in out if k.startswith(MIRROR)}

    store = zarrify(str(path), layout=Layout(plugin, given), checksums=checksums)
    assert set(store.mapping) == set(out)
    plain = arrays(zarr.open_group(refkv.zarr.store(zarrify(str(path), checksums=checksums)), mode="r"))
    mine = arrays(zarr.open_group(refkv.zarr.store(store), mode="r"))
    assert set(mine) == set(plain)
    expected = h5_values(path, retiled)
    for name, a in mine.items():
        doc = document(out, f"{name}/zarr.json")
        if name in retiled:
            assert doc["chunk_grid"] == grid(a.shape, retiled[name], options["edge"]), name
            assert same(a[...], expected[name]), name
        else:
            assert doc["chunk_grid"] == b.document(f"{name}/zarr.json")["chunk_grid"]
        want, got = np.asarray(plain[name][...]), np.asarray(a[...])
        assert want.dtype == got.dtype, name
        assert want.shape == got.shape, name
        assert np.array_equal(want, got, equal_nan=True) if want.dtype.kind in "fc" else want.tolist() == got.tolist()
    if source == "generated.h5" and options["edge"] == "rectilinear":  # the grid, spelled out once
        assert document(out, "i16/zarr.json")["chunk_grid"]["configuration"]["chunk_shapes"] == \
            [[8, 8, 8, 8, 5], [8, 8, 8, 5]]


DIGEST = """
import hashlib, sys
from refkv.zarrify.layouts import base, encode_entries
from refkv.zarrify.layouts.examples.hdf5_rechunk import Hdf5Rechunk
p = Hdf5Rechunk()
out = p.write(base(sys.argv[1]), p.options({"default": [10, 10], "chunks": {"big": [128, 128]}}))
print(hashlib.sha256(encode_entries(out)).hexdigest())
"""


def test_hdf5_rechunk_is_deterministic(generated):
    """Two runs with other hash seeds write the same entries as this one."""
    path = generated / "generated.h5"
    runs = {subprocess.run([sys.executable, "-c", DIGEST, str(path)], check=True, capture_output=True,
                           text=True, env={**os.environ, "PYTHONHASHSEED": seed}).stdout for seed in ("1", "2")}
    p = Hdf5Rechunk()
    out = p.write(base(path), p.options({"default": [10, 10], "chunks": {"big": [128, 128]}}))
    assert runs == {hashlib.sha256(encode_entries(out)).hexdigest() + "\n"}


# ---- options refused

def test_options_refuse_an_unknown_member():
    with pytest.raises(ValueError, match="unknown options"):
        Hdf5Rechunk().options({"chunk": {"a": [2]}})


@pytest.mark.parametrize("given", [
    {"chunks": {"a": []}}, {"chunks": {"a": [0, 2]}}, {"chunks": {"a": [-1]}}, {"chunks": {"a": [True]}},
    {"chunks": {"a": [2.0]}}, {"chunks": {"a": 4}}, {"default": [4, "4"]}, {"chunks": [["a", [2]]]},
])
def test_options_refuse_a_chunk_shape_of_other_than_positive_lengths(given):
    with pytest.raises(ValueError, match=r"chunk shape|chunks maps"):
        Hdf5Rechunk().options(given)


def test_options_refuse_an_edge_other_than_pad_or_rectilinear():
    with pytest.raises(ValueError, match="edge"):
        Hdf5Rechunk().options({"edge": "trim"})


@pytest.mark.parametrize("chunks", [{"/a": [2], "a": [3]}, {"": [2]}, {"refkv_source/x": [2]}])
def test_options_refuse_a_path_twice_empty_or_in_the_mirror(chunks):
    with pytest.raises(ValueError, match="empty, repeated"):
        Hdf5Rechunk().options({"chunks": chunks})
    with pytest.raises(LayoutUsageError):  # before the input is opened
        zarrify(str(HDF5 / "hdf5_numbers.h5"), layout=Layout(Hdf5Rechunk(), {"chunks": chunks}))


# ---- inputs rejected

def rejected(path, chunks, match):
    with pytest.raises(LayoutRejected, match=match):
        zarrify(str(path), layout=Layout(Hdf5Rechunk(), {"chunks": chunks}))


@pytest.mark.parametrize("name", ["missing", "d1"])  # no node; a group
def test_a_named_path_that_is_no_array_is_rejected(name):
    rejected(HDF5 / "hdf5_numbers.h5", {name: [2]}, "no array of the base")


@pytest.mark.parametrize(("name", "storage"), [("fill7", "chunked"), ("compact_i2", "compact")])
def test_a_named_dataset_that_is_not_contiguous_is_rejected(name, storage):
    rejected(HDF5 / "hdf5_layouts.h5", {name: [2, 2]}, f"storage is {storage}")


def test_a_named_dataset_of_variable_length_strings_is_rejected():
    rejected(HDF5 / "hdf5_strings.h5", {"many": [100]}, "not bytes alone")


def test_a_named_dataset_of_another_rank_is_rejected():
    rejected(HDF5 / "hdf5_numbers.h5", {"d2": [2]}, "1 axes for an array of 2")


def test_a_named_dataset_in_external_storage_is_rejected():
    rejected(HDF5 / "hdf5_external.h5", {"spread": [2, 2]}, "no data element holds")


def test_a_tile_past_the_payload_limit_is_rejected(tmp_path):
    """A tile of 16384 one-byte runs, each a range of its own, is past 65519 bytes of payload."""
    path = tmp_path / "narrow.h5"
    with h5py.File(path, "w") as f:
        f.create_dataset("narrow", data=np.zeros((16384, 2), "u1"))
    rejected(path, {"narrow": [16384, 1]}, "payload")


def test_an_input_of_another_profile_is_rejected():
    with pytest.raises(LayoutRejected, match="does not read the tiff"):
        zarrify(str(ROOT / "fixtures" / "tiff" / "strips_uint8.tif"), layout=Layout(Hdf5Rechunk()))
