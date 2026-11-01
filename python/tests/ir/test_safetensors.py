"""The safetensors IR: the Rust parser and its schema, the projection
(spec/zarrify/safetensors.md) against the safetensors library's reading
(`safetensors.deserialize`) of every synthetic file, read back through zarr-python with
refkv's ml_dtypes data types and `packbits` codec, the mirror's rebuild, the tensors it does
not present, and the chunks. safetensors has no frozen Python reference: the library is the
independent reader."""

import json

import ml_dtypes
import numpy as np
import pytest
import safetensors
from ir_helpers import FIXTURES
from ir_helpers import zarrified as shared_zarrified

from refkv._narrow import mapping, ndarray, sequence, string

ACCEPTED = sorted(p for p in (FIXTURES / "safetensors").iterdir() if "reject" not in p.name)

# a dtype's Zarr data type, its NumPy type and its bits (spec/zarrify/safetensors.md §3
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#3-data-types>)
TYPES = {
    "BOOL": ("bool", np.bool_, 8), "U8": ("uint8", np.uint8, 8), "I8": ("int8", np.int8, 8),
    "U16": ("uint16", np.uint16, 16), "I16": ("int16", np.int16, 16), "U32": ("uint32", np.uint32, 32),
    "I32": ("int32", np.int32, 32), "U64": ("uint64", np.uint64, 64), "I64": ("int64", np.int64, 64),
    "F16": ("float16", np.float16, 16), "F32": ("float32", np.float32, 32), "F64": ("float64", np.float64, 64),
    "C64": ("complex64", np.complex64, 64), "BF16": ("bfloat16", ml_dtypes.bfloat16, 16),
    "F8_E5M2": ("float8_e5m2", ml_dtypes.float8_e5m2, 8),
    "F8_E4M3": ("refkv.float8_e4m3fn", ml_dtypes.float8_e4m3fn, 8),
    "F8_E4M3FNUZ": ("float8_e4m3fnuz", ml_dtypes.float8_e4m3fnuz, 8),
    "F8_E5M2FNUZ": ("float8_e5m2fnuz", ml_dtypes.float8_e5m2fnuz, 8),
    "F8_E8M0": ("float8_e8m0fnu", ml_dtypes.float8_e8m0fnu, 8), "F4": ("float4_e2m1fn", ml_dtypes.float4_e2m1fn, 4),
    "F6_E2M3": ("float6_e2m3fn", ml_dtypes.float6_e2m3fn, 6), "F6_E3M2": ("float6_e3m2fn", ml_dtypes.float6_e3m2fn, 6),
}


def zarrified(path, tmp_path):
    return shared_zarrified(path, tmp_path, "safetensors")


def expected(dtype: str, shape: list[int], data: bytes) -> np.ndarray:
    """The library's tensor as an array: its bytes little-endian, or a packed type's elements,
    `k` bits each from the least significant (spec/zarrify/safetensors.md §3.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#32-packed-data-types>),
    unpacked here by shifts and masks: two 4-bit elements a byte, low first, or four 6-bit
    elements in each 3 bytes, a 24-bit little-endian integer."""
    _, t, k = TYPES[dtype]
    b = np.frombuffer(data, dtype=np.uint8)
    if k >= 8:
        return b.view(np.dtype(t).newbyteorder("<")).reshape(shape)
    if k == 4:
        elements = np.stack([b & 0xF, b >> 4], axis=-1).reshape(-1)
    else:
        word = b.reshape(-1, 3).astype(np.uint32) @ np.array([1, 1 << 8, 1 << 16], dtype=np.uint32)
        elements = np.stack([(word >> (6 * i)) & 0x3F for i in range(4)], axis=-1).reshape(-1)
    return elements.astype(np.uint8)[: int(np.prod(shape))].view(t).reshape(shape)


def same_bits(got: np.ndarray, want: np.ndarray, where: str) -> None:
    """Equal shapes, types and bits (so that NaNs compare)."""
    assert got.shape == want.shape, where
    assert got.dtype.newbyteorder("=") == want.dtype.newbyteorder("="), where
    got_bits, want_bits = (np.ascontiguousarray(a).view(np.uint8) for a in (got, want.astype(got.dtype)))
    np.testing.assert_array_equal(got_bits, want_bits, err_msg=where)


@pytest.mark.parametrize("path", ACCEPTED, ids=[p.name for p in ACCEPTED])
def test_matches_the_library_and_rebuilds(path, tmp_path):
    """Every presented tensor is the library's, in its shape, data type and bits; every tensor
    is in the source metadata, in the header's order; and the mirror rebuilds the file
    (zarrified checks it)."""
    out, root, _ = zarrified(path, tmp_path)
    meta = mapping(mapping(root.attrs["refkv_zarrified"])["safetensors"])
    library = dict(safetensors.deserialize(path.read_bytes()))
    tensors = [mapping(t) for t in sequence(meta["tensors"])]
    assert sorted(string(t["name"]) for t in tensors) == sorted(library), path.name
    for t in tensors:
        name = string(t["name"])
        lib = library[name]
        assert (t["dtype"], t["shape"]) == (lib["dtype"], lib["shape"]), name
        if "path" not in t:
            continue
        array = root.get_array(string(t["path"]))
        assert json.loads(out.bytes_entries[f"{t['path']}/zarr.json"])["data_type"] == TYPES[lib["dtype"]][0], name
        same_bits(ndarray(array[...]), expected(lib["dtype"], lib["shape"], lib["data"]), f"{path.name}:{name}")


def test_writes_the_root_and_array_documents(tmp_path):
    """The example of spec/zarrify/safetensors.md §6
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#6-example>."""
    out, _, _ = zarrified(FIXTURES / "safetensors" / "st_two.safetensors", tmp_path)
    attributes = json.loads(out.bytes_entries["zarr.json"])["attributes"]
    assert list(attributes) == ["zarr_conventions", "refkv_zarrified"]
    assert attributes["refkv_zarrified"]["safetensors"] == {
        "header_length": 152,
        "metadata": {"format": "pt"},
        "tensors": [
            {"name": "weight", "dtype": "F32", "shape": [2, 3], "data_offsets": [0, 24], "path": "weight"},
            {"name": "bias", "dtype": "BF16", "shape": [3], "data_offsets": [24, 30], "path": "bias"},
        ],
    }
    assert json.loads(out.bytes_entries["bias/zarr.json"]) == {
        "zarr_format": 3,
        "node_type": "array",
        "shape": [3],
        "data_type": "bfloat16",
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [3]}},
        "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
        "fill_value": 0,
        "codecs": [{"name": "bytes", "configuration": {"endian": "little"}}],
        "dimension_names": ["dim_0"],
        "attributes": {},
    }
    assert out.refs["bias/c/0"] == [(184, 6)]
    assert out.refs["weight/c/0/0"] == [(160, 24)]


def test_writes_each_data_type_and_codec(tmp_path):
    """Each dtype's Zarr data type, codecs and fill value (spec/zarrify/safetensors.md §4.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#42-the-arrays>)."""
    docs = {}
    for name in ("st_dtypes", "st_f6"):
        out, _, _ = zarrified(FIXTURES / "safetensors" / f"{name}.safetensors", tmp_path)
        docs |= {k.removesuffix("/zarr.json"): json.loads(v) for k, v in out.bytes_entries.items()
                 if k.endswith("/zarr.json") and not k.startswith("refkv_source")}
    got = {p: (d["data_type"], d["codecs"], d["fill_value"]) for p, d in docs.items()}
    little = [{"name": "bytes", "configuration": {"endian": "little"}}]
    one = [{"name": "bytes"}]
    packed = [{"name": "packbits", "configuration": {"padding_encoding": "none"}}]
    assert got == {
        "bool": ("bool", one, False), "u8": ("uint8", one, 0), "i8": ("int8", one, 0),
        "u16": ("uint16", little, 0), "i16": ("int16", little, 0), "u32": ("uint32", little, 0),
        "i32": ("int32", little, 0), "u64": ("uint64", little, 0), "i64": ("int64", little, 0),
        "f16": ("float16", little, 0), "f32": ("float32", little, 0), "f64": ("float64", little, 0),
        "c64": ("complex64", little, [0, 0]), "bf16": ("bfloat16", little, 0),
        "f8_e5m2": ("float8_e5m2", one, 0), "f8_e4m3": ("refkv.float8_e4m3fn", one, 0),
        "f8_e4m3fnuz": ("float8_e4m3fnuz", one, 0), "f8_e5m2fnuz": ("float8_e5m2fnuz", one, 0),
        "f8_e8m0": ("float8_e8m0fnu", one, "NaN"), "f4": ("float4_e2m1fn", packed, 0),
        "e2m3": ("float6_e2m3fn", packed, 0), "e3m2": ("float6_e3m2fn", packed, 0),
    }


def test_cuts_the_tensors_into_chunks(tmp_path):
    """The chunk shapes of conventions §7
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays>,
    a packed tensor's cut on bytes, or one chunk when a chunk would start mid-byte
    (spec/zarrify/safetensors.md §8.3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#83-chunks>), and the
    chunk key of rank 0."""
    cases = {
        "st_chunks": {"w": ([301, 1000], [151, 1000], 2)},
        "st_packed_chunks": {"w": ([1100, 1000], [550, 1000], 2)},
        "st_packed_one": {"w": ([1102, 999], [1102, 999], 1)},
        "st_scalar_empty": {"count": ([], [], 1), "none": ([0, 3], [1, 3], 0), "two": ([2], [2], 1)},
        "st_no_tensors": {},
    }
    for name, want in cases.items():
        out, _, _ = zarrified(FIXTURES / "safetensors" / f"{name}.safetensors", tmp_path)
        summary = map(mapping, sequence(out.summary["tensors"]))
        got = {string(a["path"]): (a["shape"], a["chunkShape"], a["chunks"]) for a in summary}
        assert got == want, name
    out, _, _ = zarrified(FIXTURES / "safetensors" / "st_packed_chunks.safetensors", tmp_path)
    n = 8 + json.loads(out.bytes_entries["zarr.json"])["attributes"]["refkv_zarrified"]["safetensors"]["header_length"]
    assert out.refs["w/c/0/0"] == [(n, 275000)]
    assert out.refs["w/c/1/0"] == [(n + 275000, 275000)]
    out, _, _ = zarrified(FIXTURES / "safetensors" / "st_scalar_empty.safetensors", tmp_path)
    assert "count/c" in out.refs


def test_records_the_tensors_it_does_not_present(tmp_path):
    """Names that are not paths, the reserved node, a tensor that is another's group, and the
    groups of names with `/` (spec/zarrify/safetensors.md §4.1
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#41-paths>)."""
    out, root, _ = zarrified(FIXTURES / "safetensors" / "st_names.safetensors", tmp_path)
    tensors = json.loads(out.bytes_entries["zarr.json"])["attributes"]["refkv_zarrified"]["safetensors"]["tensors"]
    assert {t["name"]: t.get("path", t.get("reason")) for t in tensors} == {
        "a": "path", "a/b": "a/b", "enc/conv/kernel": "enc/conv/kernel", "enc/conv/bias": "enc/conv/bias",
        "x//y": "name", "__x": "name", "refkv_source/w": "reserved", "zarr.json": "name",
        "model.layers.0.weight": "model.layers.0.weight", "dir/..": "name",
    }
    assert out.summary["unpresented"] == {"name": 4, "path": 1, "reserved": 1}
    for g in ("a", "enc", "enc/conv"):
        group = {"zarr_format": 3, "node_type": "group", "attributes": {}}
        assert json.loads(out.bytes_entries[f"{g}/zarr.json"]) == group
    arrays = {k for k, _ in root.members(max_depth=None)}
    assert {"a/b", "enc/conv/kernel"} <= arrays


def test_keeps_the_header_as_written(tmp_path):
    """A header the library would not write: its tensors in the header's order, not their
    data's, both metadata members, and an entry's other member (spec/zarrify/safetensors.md §5.1
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#51-the-root>)."""
    out, _, _ = zarrified(FIXTURES / "safetensors" / "st_hand.safetensors", tmp_path)
    meta = json.loads(out.bytes_entries["zarr.json"])["attributes"]["refkv_zarrified"]["safetensors"]
    assert meta == {
        "header_length": 194,
        "metadata": {"format": "np", "source": "hand"},
        "tensors": [
            {"name": "a", "dtype": "F64", "shape": [3], "data_offsets": [8, 32], "path": "a"},
            {"name": "b", "dtype": "I16", "shape": [2, 2], "data_offsets": [0, 8], "extra": {"note": "kept"},
             "path": "b"},
        ],
    }
