"""refkv.zarr.blocks: the arrays of contiguous values a zarr store holds, and those arrays on
another chunk grid."""

import itertools
import json
import math
from collections.abc import AsyncIterator
from pathlib import Path

import numpy as np
import pytest
import zarr
from zarr.core.buffer import default_buffer_prototype
from zarr.core.sync import sync

import refkv
import refkv.zarr
from refkv.pb import Concat, Range, Source
from refkv.zarrify import zarrify

FIXTURES = Path(__file__).parents[2] / "fixtures"
PROTO = default_buffer_prototype()
BYTES = {"name": "bytes", "configuration": {"endian": "little"}}
TRANSPOSE = {"name": "transpose", "configuration": {"order": [1, 0]}}


def ARRAY_JSON(shape, dtype, chunk_shape, codecs=(BYTES,)):  # noqa: N802 (the brief's name)
    """A minimal Zarr v3 array document."""
    return json.dumps({
        "zarr_format": 3, "node_type": "array", "shape": list(shape), "data_type": dtype,
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": list(chunk_shape)}},
        "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
        "fill_value": 0, "codecs": list(codecs), "attributes": {}}).encode()


def _one_source_store(tmp_path, *, shape=(4, 4), chunks=(4, 4), dtype="uint8", codecs=(BYTES,),
                      chunk=None, offsets=None, policy=None):
    """A store of one array `a` over `src.bin`, `np.arange` of `shape`: one range per chunk at
    the offset of the slab cut of contiguous values, but for those `offsets` gives (by chunk
    coordinates), or `chunk` in place of the single chunk."""
    e = np.dtype(dtype).itemsize
    (tmp_path / "src.bin").write_bytes(np.arange(math.prod(shape), dtype=dtype).tobytes())
    mapping: dict[str, bytes | Range] = {"a/zarr.json": ARRAY_JSON(shape, dtype, chunks, codecs)}
    for coords in itertools.product(*(range(-(-n // c)) for n, c in zip(shape, chunks, strict=True))):
        start = [i * c for i, c in zip(coords, chunks, strict=True)]
        index = int(np.ravel_multi_index(start, shape))
        n = math.prod(min(c, s - x) for c, s, x in zip(chunks, shape, start, strict=True))
        offset = (offsets or {}).get(coords, index * e)
        mapping["a/c/" + "/".join(map(str, coords))] = Range(source=0, offset=offset, length=n * e)
    if chunk is not None:
        (key,) = (k for k in mapping if k.startswith("a/c/"))
        mapping[key] = chunk
    return refkv.ReferenceStore(mapping, [Source(url="src.bin")], url=tmp_path.as_uri() + "/x.vzip",
                                policy=policy)


async def _collect(ait: AsyncIterator[str]) -> list[str]:
    return [k async for k in ait]


def _get(store, key: str) -> bytes:
    """`key`'s value in the zarr store `store`, which holds it."""
    value = sync(store.get(key, PROTO))
    assert value is not None
    return value.to_bytes()


def test_contiguous_finds_a_flat_mrc_volume():
    zs = refkv.zarr.store(zarrify(str(FIXTURES / "mrc" / "mrc_chunk_sections.mrc")))
    found = refkv.zarr.contiguous(zs)
    c = found["0"]
    doc = sync(zs.get("0/zarr.json", PROTO))
    assert doc is not None
    assert (c.offset, c.itemsize) == (1024, 2)
    assert c.shape == tuple(json.loads(doc.to_bytes())["shape"])
    assert c.codecs == [{"name": "bytes", "configuration": {"endian": "little"}}]


def test_contiguous_reads_an_archive_whose_references_have_no_body(tmp_path):
    rs = zarrify(str(FIXTURES / "mrc" / "mrc_chunk_sections.mrc"))
    rs.save_vzip(tmp_path / "x.vzip", mirror_refs=False)
    found = refkv.zarr.contiguous(refkv.zarr.store(refkv.open(tmp_path / "x.vzip")))
    assert found["0"] == refkv.zarr.contiguous(refkv.zarr.store(rs))["0"]


def test_contiguous_leaves_out_arrays_that_are_not_one_block(tmp_path):
    # a literal chunk
    rs = refkv.ReferenceStore({"a/zarr.json": ARRAY_JSON((4, 4), "uint8", [4, 4]), "a/c/0/0": Range(data=bytes(16))},
                              [], url=tmp_path.as_uri() + "/x.vzip")
    assert refkv.zarr.contiguous(refkv.zarr.store(rs)) == {}
    # a crc32c on the range
    rs = _one_source_store(tmp_path, chunk=Range(source=0, offset=0, length=16, crc32c=0))
    assert refkv.zarr.contiguous(refkv.zarr.store(rs)) == {}
    # a transpose codec
    rs = _one_source_store(tmp_path, codecs=[TRANSPOSE, BYTES])
    assert refkv.zarr.contiguous(refkv.zarr.store(rs)) == {}
    # two chunks whose second offset is off the formula
    rs = _one_source_store(tmp_path, shape=(2, 4, 4), chunks=(1, 4, 4), offsets={(0, 0, 0): 0, (1, 0, 0): 17})
    assert refkv.zarr.contiguous(refkv.zarr.store(rs)) == {}
    # tiles, not the slab cut: each on the formula, but no tile is one run of the source
    rs = _one_source_store(tmp_path, shape=(4, 4), chunks=(2, 2))
    assert refkv.zarr.contiguous(refkv.zarr.store(rs)) == {}
    # a single codec that is not `bytes`
    assert refkv.zarr.contiguous(refkv.zarr.store(_one_source_store(tmp_path, codecs=[TRANSPOSE]))) == {}
    # chunks on two sources, each on the formula
    doc = ARRAY_JSON((2, 4), "uint8", [1, 4])
    two = {"a/zarr.json": doc, "a/c/0/0": Range(0, 0, 4), "a/c/1/0": Range(1, 4, 4)}
    rs = refkv.ReferenceStore(two, [Source(url="src.bin")] * 2, url=tmp_path.as_uri() + "/x.vzip")
    assert refkv.zarr.contiguous(refkv.zarr.store(rs)) == {}
    # an edge chunk padded from a data source that is not zeros
    doc = ARRAY_JSON((1, 4), "uint8", [2, 4])
    padded = {"a/zarr.json": doc, "a/c/0/0": Concat((Range(0, 0, 4), Range(1, 0, 4)))}
    ones = Source(data=b"\x01" * 4)
    rs = refkv.ReferenceStore(padded, [Source(url="src.bin"), ones], url=tmp_path.as_uri() + "/x.vzip")
    assert refkv.zarr.contiguous(refkv.zarr.store(rs)) == {}
    # chunk keys of another encoding
    doc = json.loads(ARRAY_JSON((4, 4), "uint8", [4, 4]))
    doc["chunk_key_encoding"] = {"name": "v2", "configuration": {"separator": "."}}
    v2 = {"a/zarr.json": json.dumps(doc).encode(), "a/0.0": Range(0, 0, 16)}
    rs = refkv.ReferenceStore(v2, [Source(url="src.bin")], url=tmp_path.as_uri() + "/x.vzip")
    assert refkv.zarr.contiguous(refkv.zarr.store(rs)) == {}


def test_contiguous_accepts_one_source_store_on_the_formula(tmp_path):
    rs = _one_source_store(tmp_path, shape=(3, 4, 4), chunks=(1, 4, 4), dtype="uint16")
    found = refkv.zarr.contiguous(refkv.zarr.store(rs))
    assert set(found) == {"a"}
    assert (found["a"].source, found["a"].offset, found["a"].shape) == (0, 0, (3, 4, 4))


def test_contiguous_accepts_zero_source_padding_and_a_prefix():
    zs = refkv.zarr.store(zarrify(str(FIXTURES / "mrc" / "mrc_chunk_edge_literal.mrc")))
    assert set(refkv.zarr.contiguous(zs)) == {"0", "dataset/data"}
    assert set(refkv.zarr.contiguous(zs, prefix="dataset")) == {"dataset/data"}
    assert refkv.zarr.contiguous(zs, prefix="refkv_source") == {}


def test_contiguous_accepts_zero_literal_padding(tmp_path):
    doc = ARRAY_JSON((1, 4), "uint8", [2, 4])
    for pad, want in ((bytes(4), {"a"}), (b"\x01" * 4, set())):
        padded = {"a/zarr.json": doc, "a/c/0/0": Concat((Range(0, 0, 4), Range(data=pad)))}
        rs = refkv.ReferenceStore(padded, [Source(url="src.bin")], url=tmp_path.as_uri() + "/x.vzip")
        assert set(refkv.zarr.contiguous(refkv.zarr.store(rs))) == want


def test_blocks_names_an_array_that_is_not_contiguous_or_a_wrong_rank():
    zs = refkv.zarr.store(zarrify(str(FIXTURES / "mrc" / "mrc_chunk_sections.mrc")))
    with pytest.raises(ValueError, match="dataset"):
        refkv.zarr.blocks(zs, {"dataset": (1, 1, 1)})
    with pytest.raises(ValueError, match="rank"):
        refkv.zarr.blocks(zs, {"0": (1, 1)})


@pytest.mark.parametrize("shape", [(0, 4, 4), (4, -1, 4)])
def test_blocks_names_a_block_shape_with_an_axis_below_one(shape):
    zs = refkv.zarr.store(zarrify(str(FIXTURES / "mrc" / "mrc_chunk_sections.mrc")))
    with pytest.raises(ValueError, match=r"'0'.*below 1"):
        refkv.zarr.blocks(zs, {"0": shape})


def test_blocks_reads_only_the_requested_arrays_references(monkeypatch):
    zs = refkv.zarr.store(zarrify(str(FIXTURES / "mrc" / "mrc_chunk_edge_literal.mrc")))
    asked: list[str] = []
    reference_async = zs.reader.reference_async

    async def counting(key):
        asked.append(key)
        return await reference_async(key)

    monkeypatch.setattr(zs.reader, "reference_async", counting)
    for path in ("0", "dataset/data"):
        asked.clear()
        rank = len(json.loads(_get(zs, f"{path}/zarr.json"))["shape"])
        refkv.zarr.blocks(zs, {path: (1,) * rank})
        assert asked
        assert all(k.startswith(f"{path}/c/") for k in asked)


@pytest.mark.parametrize("row_bytes_threshold", [1, 4096])  # row-wise, span-wise
@pytest.mark.parametrize("shape", [(3, 3, 3), (2, 5, 4)])
def test_blocks_reads_the_same_voxels_as_the_native_grid(shape, row_bytes_threshold):
    zs = refkv.zarr.store(zarrify(str(FIXTURES / "mrc" / "mrc_chunk_sections.mrc")))
    # a corner across the native chunks' boundary (z = 10) to the array's end on every axis, of
    # edge blocks on each: the whole array is ~26,000 blocks, seconds of reads
    region = np.s_[8:, 97:, 199:]
    native = zarr.open_array(zs, path="0", mode="r")[region]
    bs = refkv.zarr.blocks(zs, {"0": shape}, row_bytes_threshold=row_bytes_threshold)
    arr = zarr.open_array(bs, path="0", mode="r")
    assert arr.chunks == shape
    np.testing.assert_array_equal(arr[region], native)


def test_blocks_pads_edge_blocks_and_reads_only_existing_rows(tmp_path):
    rs = _one_source_store(tmp_path, shape=(3, 5, 7), chunks=(1, 5, 7), dtype="uint16")
    stats = refkv.Stats()
    rs.stats = stats
    bs = refkv.zarr.blocks(refkv.zarr.store(rs), {"a": (2, 4, 4)})
    raw = _get(bs, "a/c/1/1/1")
    assert len(raw) == 2 * 4 * 4 * 2
    block = np.frombuffer(raw, "<u2").reshape(2, 4, 4)
    # the one row inside the array: element (2, 4, 4) on, 3 of them
    assert block[0, 0, :3].tolist() == [102, 103, 104]
    assert (block[1] == 0).all()
    assert (block[0, 1:] == 0).all()
    assert (block[0, 0, 3:] == 0).all()
    assert stats.log
    assert all(start + n <= 3 * 5 * 7 * 2 for _, start, n in stats.log)
    assert sync(bs.get("a/c/2/0/0", PROTO)) is None


def test_blocks_batches_reads_under_the_policy_cap(tmp_path):
    # planes of 129 * 128 * 4 bytes, more than the 64 KiB reads merge across: a column of
    # 8 elements is 8 reads, which no single value may need under max_reads=3
    shape = (8, 129, 128)
    rs = _one_source_store(tmp_path, shape=shape, chunks=(1, 129, 128), dtype="uint32",
                           policy=refkv.Policy(max_reads=3))
    bs = refkv.zarr.blocks(refkv.zarr.store(rs), {"a": (8, 1, 1)})
    want = np.arange(math.prod(shape), dtype="uint32").reshape(shape)[:, :1, :1].tobytes()
    assert _get(bs, "a/c/0/0/0") == want
    assert rs.stats.external_requests == 8


def test_blocks_chooses_rows_for_wide_rows_and_spans_for_narrow_ones(tmp_path):
    # rows 80 KiB apart, farther than reads merge across: a read per row, or one span; a width
    # of 81921 starts each row's values one further on, so no two rows are alike
    width = 81921
    rs = _one_source_store(tmp_path, shape=(1, 8, width), chunks=(1, 8, width), dtype="uint8")
    stats = refkv.Stats()
    rs.stats = stats
    bs = refkv.zarr.blocks(refkv.zarr.store(rs), {"a": (1, 8, 8192)}, row_bytes_threshold=4096)
    _get(bs, "a/c/0/0/0")
    assert stats.external_requests == 8
    assert stats.external_bytes == 8 * 8192
    stats.reset()
    bs = refkv.zarr.blocks(refkv.zarr.store(rs), {"a": (1, 8, 64)})
    want = np.arange(8 * width, dtype=np.uint8).reshape(8, width)[:, 64:128].tobytes()
    assert _get(bs, "a/c/0/0/1") == want
    assert stats.external_requests == 1
    assert stats.external_bytes == 7 * width + 64


def test_blocks_lists_the_block_grid_and_keeps_other_keys():
    zs = refkv.zarr.store(zarrify(str(FIXTURES / "mrc" / "mrc_chunk_sections.mrc")))
    bs = refkv.zarr.blocks(zs, {"0": (3, 3, 3)})
    shape = json.loads(_get(zs, "0/zarr.json"))["shape"]
    grid = {"0/c/" + "/".join(map(str, c)) for c in itertools.product(*(range(math.ceil(n / 3)) for n in shape))}
    keys = set(sync(_collect(bs.list())))
    assert {"zarr.json", "0/zarr.json", "dataset/data/c/0/0/0"} <= keys
    assert {k for k in keys if k.startswith("0/c/")} == grid
    assert sorted(sync(_collect(bs.list_dir("0")))) == ["c", "zarr.json"]
    assert sorted(sync(_collect(bs.list_dir("0/c/6"))), key=int) == [str(i) for i in range(43)]
    assert set(sync(_collect(bs.list_prefix("0/c/6/42/")))) == {f"0/c/6/42/{i}" for i in range(86)}
    assert sync(bs.exists("0/c/6/42/85"))
    assert not sync(bs.exists("0/c/7/0/0"))
    assert not sync(bs.exists("0/c/1/0"))
    for key in ("0/c/0_0/1/1", "0/c/ 1/0/0", "0/c/+1/0/0"):
        assert not sync(bs.exists(key))
    doc = json.loads(_get(bs, "0/zarr.json"))
    assert doc["chunk_grid"]["configuration"]["chunk_shape"] == [3, 3, 3]
