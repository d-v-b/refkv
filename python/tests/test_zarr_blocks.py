"""refkv.zarr.blocks: the arrays of contiguous values a zarr store holds, and those arrays on
another chunk grid."""

import itertools
import json
import math
from collections.abc import AsyncIterator
from pathlib import Path

import numpy as np
import pytest
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
