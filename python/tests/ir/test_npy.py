"""The NumPy IR: the Rust parser and its schema, the projection (spec/zarrify/npy.md) against
`np.load`'s reading of every synthetic `.npy` file and `.npz` archive, the mirror's rebuild,
the members an archive does not present, and the edge chunks it copies. NumPy has no frozen
Python reference: `np.load` is the independent reader."""

import json
import struct
import zipfile

import numpy as np
import pytest
from ir_helpers import FIXTURES
from ir_helpers import zarrified as shared_zarrified

from refkv import _core
from refkv._narrow import mapping, ndarray, sequence, string

ACCEPTED = sorted(p for p in (FIXTURES / "npy").iterdir() if "reject" not in p.name)


def zarrified(path, tmp_path):
    return shared_zarrified(path, tmp_path, "npy")


def as_stored(a: np.ndarray) -> np.ndarray:
    """`np.load`'s array as the layout presents it (spec/zarrify/npy.md §3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#3-data-types>): fixed-length
    bytes and voids as `uint8` with a trailing axis of their size."""
    if a.dtype.kind in "SV" and not a.dtype.names:
        n = a.dtype.itemsize
        return np.frombuffer(np.ascontiguousarray(a).tobytes(), dtype=np.uint8).reshape((*a.shape, n))
    return a


def assert_same(got: np.ndarray, want: np.ndarray, where: str) -> None:
    """Equal values, shape and type (a structure's field by field), whatever their byte order."""
    assert got.shape == want.shape, where
    if want.dtype.names:
        assert got.dtype.names == want.dtype.names, where
        for name in want.dtype.names:
            assert_same(got[name], want[name], f"{where}.{name}")
        return
    assert got.dtype.newbyteorder("=") == want.dtype.newbyteorder("="), where
    np.testing.assert_array_equal(got, want, err_msg=where)


# np.load warns of a header Python 2 wrote (npy_python2_long.npy), which it reads all the same
@pytest.mark.filterwarnings("ignore:Reading `.npy` or `.npz` file required additional header parsing")
@pytest.mark.parametrize("path", ACCEPTED, ids=[p.name for p in ACCEPTED])
def test_matches_numpy_and_rebuilds(path, tmp_path):
    """Every presented array is `np.load`'s, in its shape, type and values, and the mirror
    rebuilds the file (zarrified checks it)."""
    _, root, _ = zarrified(path, tmp_path)
    meta = mapping(mapping(root.attrs["refkv_zarrified"])["npy"])
    if path.suffix == ".npy":
        assert meta["kind"] == "npy"
        assert_same(ndarray(root.get_array("array")[...]), as_stored(np.load(path)), path.name)
        return
    assert meta["kind"] == "npz"
    members = [mapping(m) for m in sequence(meta["members"])]
    paths = [string(m["path"]) for m in members if "path" in m]
    assert paths, path.name
    with np.load(path, allow_pickle=False) as loaded:
        for p in paths:
            assert_same(ndarray(root.get_array(p)[...]), as_stored(loaded[p]), f"{path.name}:{p}")
    assert sorted(root.array_keys()) == sorted(paths), path.name


def test_writes_the_root_and_array_documents(tmp_path):
    """The example of spec/zarrify/npy.md §6
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#6-example>, and its Fortran
    order twin's codecs and chunk."""
    out, _, _ = zarrified(FIXTURES / "npy" / "npy_c_float64.npy", tmp_path)
    attributes = json.loads(out.bytes_entries["zarr.json"])["attributes"]
    assert list(attributes) == ["zarr_conventions", "refkv_zarrified"]
    assert attributes["refkv_zarrified"]["npy"] == {
        "kind": "npy",
        "array": {"version": [1, 0], "descr": "<f8", "fortran_order": False, "shape": [3, 4], "offset": 128},
    }
    array = json.loads(out.bytes_entries["array/zarr.json"])
    assert array == {
        "zarr_format": 3,
        "node_type": "array",
        "shape": [3, 4],
        "data_type": "float64",
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [3, 4]}},
        "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
        "fill_value": 0,
        "codecs": [{"name": "bytes", "configuration": {"endian": "little"}}],
        "dimension_names": ["dim_0", "dim_1"],
        "attributes": {},
    }
    assert out.refs["array/c/0/0"] == [(128, 96)]
    out, _, _ = zarrified(FIXTURES / "npy" / "npy_f_float64.npy", tmp_path)
    codecs = json.loads(out.bytes_entries["array/zarr.json"])["codecs"]
    assert codecs[0] == {"name": "transpose", "configuration": {"order": [1, 0]}}


def test_cuts_the_stored_shape(tmp_path):
    """The chunk shapes of conventions §7
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays> on
    the stored shape (a Fortran order array's reversed, a trailing axis last), and the chunk key
    of rank 0."""
    cases = {
        "npy_chunk_rows": ([1024, 1100], [512, 1100], 2),
        "npy_chunk_edge": ([523, 2049], [262, 2049], 2),
        "npy_chunk_fortran": ([1000, 1050], [1000, 525], 2),
        "npy_f_int16_3d": ([2, 3, 4], [2, 3, 4], 1),
        "npy_f_bytes": ([3, 2, 2], [3, 2, 2], 1),
        "npy_scalar_int32": ([], [], 1),
        "npy_empty": ([0, 3], [1, 3], 0),
    }
    for name, (shape, chunk_shape, chunks) in cases.items():
        out, _, _ = zarrified(FIXTURES / "npy" / f"{name}.npy", tmp_path)
        (a,) = [mapping(x) for x in sequence(out.summary["arrays"])]
        assert (a["shape"], a["chunkShape"], a["chunks"]) == (shape, chunk_shape, chunks), name
    out, _, _ = zarrified(FIXTURES / "npy" / "npy_scalar_int32.npy", tmp_path)
    assert "array/c" in out.refs
    out, _, _ = zarrified(FIXTURES / "npy" / "npy_f_bytes.npy", tmp_path)
    codecs = json.loads(out.bytes_entries["array/zarr.json"])["codecs"]
    assert codecs[0] == {"name": "transpose", "configuration": {"order": [1, 0, 2]}}


def test_records_the_members_it_does_not_present(tmp_path):
    """An archive's members, presented or not, each with its reason (spec/zarrify/npy.md §4.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#42-an-npz-archive>)."""
    out, _, _ = zarrified(FIXTURES / "npy" / "npz_mixed.npz", tmp_path)
    members = json.loads(out.bytes_entries["zarr.json"])["attributes"]["refkv_zarrified"]["npy"]["members"]
    got = {m["name"]: m.get("path", m.get("reason")) for m in members}
    assert got == {
        "kept.npy": "kept",
        "deflated.npy": "deflated",
        "bzip2.npy": "method",
        "deflated_trailing.npy": "size",
        "deflated_broken.npy": "header",
        "deflated_long_header.npy": "header",
        "notes.txt": "not_npy",
        "dir/nested.npy": "name",
        "refkv_source.npy": "reserved",
        "objects.npy": "datatype",
        "long_double.npy": "datatype",
        "short.npy": "size",
        "broken.npy": "header",
        "scalar.npy": "scalar",
        "empty.npy": "empty",
    }
    headed = {m["name"] for m in members if "header" in m}
    assert headed == {
        "kept.npy",
        "deflated.npy",
        "deflated_trailing.npy",
        "objects.npy",
        "long_double.npy",
        "short.npy",
        "scalar.npy",
        "empty.npy",
    }
    assert out.summary["unpresented"] == {
        "datatype": 2,
        "header": 3,
        "method": 1,
        "name": 1,
        "not_npy": 1,
        "reserved": 1,
        "size": 2,
    }


def test_writes_a_deflated_member_as_one_gzip_chunk(tmp_path):
    """A deflated member is one chunk, its deflate stream between a gzip header and a trailer
    of its CRC-32 and size, decoded by `gzip` and then `refkv.skip`, which drops the NPY
    preamble and header (spec/zarrify/npy.md §8.4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#84-chunks>)."""
    path = FIXTURES / "npy" / "npz_compressed.npz"
    out, _, _ = zarrified(path, tmp_path)
    array = json.loads(out.bytes_entries["fortran/zarr.json"])
    assert array["chunk_grid"]["configuration"]["chunk_shape"] == [3, 4, 5]
    assert array["codecs"] == [
        {"name": "transpose", "configuration": {"order": [2, 1, 0]}},
        {"name": "bytes", "configuration": {"endian": "big"}},
        {"name": "refkv.skip", "configuration": {"length": 128}},
        {"name": "gzip", "configuration": {"level": 6}},
    ]
    with zipfile.ZipFile(path) as z:
        info = z.getinfo("fortran.npy")
    o = info.header_offset
    nn, ne = struct.unpack("<HH", path.read_bytes()[o + 26 : o + 30])
    stream = (o + 30 + nn + ne, info.compress_size)
    trailer = struct.pack("<II", info.CRC, info.file_size)
    assert out.refs["fortran/c/0/0/0"] == [bytes.fromhex("1f8b08000000000000ff"), stream, trailer]
    assert [k for k in out.refs if k.startswith("fortran/")] == ["fortran/c/0/0/0"]
    assert "scalar/c" in out.refs
    assert not [k for k in out.refs if k.startswith("empty/")]


def test_edge_chunks_past_the_payload_are_copied(tmp_path):
    """An edge chunk whose reference, with its zero padding, does not fit in a payload is
    copied, padding included (spec/zarrify/npy.md §8.4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#84-chunks>): with a payload
    limit of 8 bytes, and the array is still `np.load`'s."""
    path = FIXTURES / "npy" / "npy_chunk_edge.npy"
    _core._set_copy_threshold(8)
    try:
        out, root, _ = zarrified(path, tmp_path)
    finally:
        _core._set_copy_threshold(None)
    assert "array/c/1/0" in out.bytes_entries
    assert "array/c/1/0" not in out.refs
    assert len(out.bytes_entries["array/c/1/0"]) == 262 * 2049
    assert np.array_equal(ndarray(root.get_array("array")[...]), np.load(path))
