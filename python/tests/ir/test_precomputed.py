"""Neuroglancer precomputed volumes through the IR (refkv.zarrify → refkv.ir.store →
rust/refkv-ir/src/precomputed.rs; spec/zarrify/precomputed.md
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/precomputed.md>), read back through
zarr-python and the codec `refkv.precomputed`. One test over the accepted fixture stores, every
array equal to the values their generator states, and to tensorstore's reading of the same store
wherever tensorstore can read it; one each of what a reader of a shard reads and of the layout's
documents; and one per way the codec refuses a chunk."""

import asyncio
import json
import struct
from pathlib import Path
from typing import TYPE_CHECKING, cast

import numpy as np
import pytest
import tensorstore
import zarr
from obstore.store import LocalStore
from zarr.abc.store import RangeByteRequest
from zarr.core.array_spec import ArrayConfig, ArraySpec
from zarr.core.buffer import default_buffer_prototype
from zarr.core.metadata import ArrayV3Metadata

import refkv
from refkv._narrow import at, ndarray, sequence
from refkv.zarr import store as zarr_store
from refkv.zarr.codecs.precomputed import PrecomputedChunkError, PrecomputedCodec, chunk_id, uncode
from refkv.zarrify import zarrify_store

if TYPE_CHECKING:
    from zarr.abc.store import ByteGetter

FIXTURES = Path(__file__).parents[3] / "fixtures" / "precomputed"
URL = "https://example.test/precomputed/"


def value(x, y, z, c=0):
    """A voxel's value, as fixtures/generators/precomputed/write_fixtures.py writes it."""
    return (x + 3 * y + 5 * z + 7 * c) % 251


def metadata_v3(array) -> ArrayV3Metadata:
    assert isinstance(array.metadata, ArrayV3Metadata)
    return array.metadata


def values(size, channels=None, dtype="u1", shift=(0, 0, 0)):
    """The volume of `size` (x, y, z) as (c, z, y, x), or (z, y, x) without `channels`."""
    z, y, x = np.mgrid[0:size[2], 0:size[1], 0:size[0]]
    x, y, z = x + shift[0], y + shift[1], z + shift[2]
    if channels is None:
        return value(x, y, z).astype(dtype)
    return np.stack([value(x, y, z, c) for c in range(channels)]).astype(dtype)


def codec_of(array) -> PrecomputedCodec:
    codec = metadata_v3(array).codecs[0]
    assert isinstance(codec, PrecomputedCodec)
    return codec


def opened(name, tmp_path):
    """The fixture zarrified, its objects read from the fixtures through a registry."""
    zarrify_store(str(FIXTURES / name), f"{URL}{name}/").save(str(tmp_path / f"{name}.vzip"))
    reader = refkv.open(str(tmp_path / f"{name}.vzip"), registry={"https://example.test": LocalStore(FIXTURES.parent)})
    return reader, zarr.open_group(zarr_store(reader), mode="r")


def raw_clipped():
    a = values((10, 7, 5))
    a[2:4, 4:7, 4:8] = 0  # the missing chunk
    b = values((5, 4, 3), shift=(100, 0, 0))
    return {"0": a, "1": b}


def sharded_raw():
    a = values((10, 9, 5))
    a[0:2, 0:2, 2:4] = 0  # a missing chunk
    a[:, 8:, 8:] = 0  # a missing shard
    return {"0": a, "1": values((4, 3, 2), shift=(200, 0, 0))}


def segmentation():
    z, y, x = np.mgrid[0:4, 0:4, 0:6]
    return {"0": (((x // 2) + 2 * (y // 2) + (z // 3)) % 5).astype("u8") * (1 << 40) + (x // 3).astype("u8")}


def segmentation_sharded():
    z, y, x = np.mgrid[0:4, 0:4, 0:8]
    return {"0": ((x + y + z) % 3 + 7).astype("u4")}


def png():
    return {"0": values((6, 4, 3), dtype="<u2")}


def not_presented():
    a = values((4, 2, 2))
    a[..., 2:4] = 0  # the empty object's chunk
    return {"0": a}


EXPECTED = {
    "precomputed_raw_clipped": raw_clipped,
    "precomputed_codings": lambda: {"0": values((16, 4, 2), 2, "<u2")},
    "precomputed_codings_bare": lambda: {"0": values((16, 4, 2), 2, "<u2")},
    "precomputed_sharded_raw": sharded_raw,
    "precomputed_png": png,
    "precomputed_float32": lambda: {"0": (values((4, 2, 2), dtype="f4") / 2).astype("f4")},
    "precomputed_segmentation": segmentation,
    "precomputed_segmentation_sharded": segmentation_sharded,
    "precomputed_not_presented": not_presented,
}
# what tensorstore cannot read as a local store: a chunk's content coding is the HTTP response's
# there, so a directory of coded objects, and a name with a suffix, are not its format
NOT_TENSORSTORE = {"precomputed_codings", "precomputed_codings_bare", "precomputed_not_presented"}


def tensorstore_scale(name, index):
    v = tensorstore.open({"driver": "neuroglancer_precomputed",
                          "kvstore": {"driver": "file", "path": f"{FIXTURES / name}/"},
                          "scale_index": index}).result()
    # x, y, z, channel, from voxel_offset
    a = np.asarray(v.translate_to[0, 0, 0, 0].read().result())
    return np.squeeze(a.transpose(3, 2, 1, 0), axis=0) if a.shape[3] == 1 else a.transpose(3, 2, 1, 0)


def test_every_array_reads_as_its_generator_wrote_it_and_as_tensorstore_reads_it(tmp_path):
    for name, expect in EXPECTED.items():
        _, group = opened(name, tmp_path)
        for path, want in expect().items():
            got = ndarray(group.get_array(path)[...])
            assert got.dtype == want.dtype.newbyteorder("="), name
            assert np.array_equal(got, want), f"{name}/{path}"
            if name not in NOT_TENSORSTORE:
                assert np.array_equal(got, tensorstore_scale(name, int(path))), f"{name}/{path}: tensorstore"


def test_jpeg_chunks_read_as_tensorstore_reads_them(tmp_path):
    for name, path in (("precomputed_sharded_jpeg", "0"), ("precomputed_jpeg_rgb", "0")):
        _, group = opened(name, tmp_path)
        got = ndarray(group.get_array(path)[...])
        assert got.max() > 0, name
        assert np.array_equal(got, tensorstore_scale(name, 0)), name
    # the clipped slice is the first of a 16 × 16 image, the full chunk's first too
    _, group = opened("precomputed_sharded_jpeg", tmp_path)
    array = group.get_array("0")
    assert array.shape == (3, 16, 32)
    assert np.array_equal(ndarray(array[2, :, :16]), ndarray(array[0, :, :16]))


def test_the_layout_presents_scales_full_resolution_first_and_keeps_info(tmp_path):
    reader, group = opened("precomputed_raw_clipped", tmp_path)
    attrs = group.attrs.asdict()
    info = json.loads((FIXTURES / "precomputed_raw_clipped" / "info").read_text())
    assert at(attrs, "refkv_zarrified", "precomputed") == {"info": info}
    assert at(attrs, "ome", "omero", "channels", 0, "window") == {"min": 0, "max": 255, "start": 0, "end": 255}
    m = at(attrs, "ome", "multiscales", 0)
    assert [at(a, "name") for a in sequence(at(m, "axes"))] == ["z", "y", "x"]
    assert at(m, "axes", 0, "unit") == "nanometer"
    assert at(m, "datasets", 0) == {"path": "0", "coordinateTransformations": [
        {"type": "scale", "scale": [40, 4, 4]}, {"type": "translation", "translation": [20, -2, 10]}]}
    # a voxel's center: the second level's first voxel covers two of the first's
    assert at(m, "datasets", 1, "coordinateTransformations", 1, "translation") == [40, -4, 12]
    array = group.get_array("0")
    assert array.shape == (5, 7, 10)
    assert array.chunks == (2, 4, 4)
    assert metadata_v3(array).dimension_names == ("z", "y", "x")
    assert at(array.attrs.asdict(), "refkv_zarrified", "precomputed") == {"index": 0, "scale": info["scales"][0]}
    codec = codec_of(array)
    assert codec.to_dict() == {"name": "refkv.precomputed", "configuration": {
        "encoding": "raw", "size": [10, 7, 5], "chunk_size": [4, 4, 2]}}
    # the provenance stays reachable, whole
    assert reader.get("refkv_source/objects/provenance") == b'{"description": "a fixture"}'
    # a chunk's value: its position, then the object
    assert (reader.get("0/c/2/1/0") or b"")[:12] == struct.pack("<III", 0, 1, 2)


def test_a_shard_is_one_key_and_a_chunk_of_it_costs_three_reads(tmp_path):
    reader, group = opened("precomputed_sharded_raw", tmp_path)
    array = group.get_array("0")
    # boxes of 2 × 2 × 1 chunks of 2 × 2 × 2 voxels
    assert array.chunks == (2, 4, 4)
    keys = reader.list("0/c/")
    assert len(keys) == len(list((FIXTURES / "precomputed_sharded_raw" / "a").iterdir()))
    codec = codec_of(array)
    assert at(codec.to_dict(), "configuration", "sharding") == {
        "preshift_bits": 1, "minishard_bits": 1, "minishard_index_encoding": "gzip", "data_encoding": "gzip"}

    reads = []
    value_of = reader.get("0/c/0/0/0") or b""

    class Getter:
        async def get(self, prototype, byte_range=None):
            assert isinstance(byte_range, RangeByteRequest)
            reads.append(byte_range)
            return prototype.buffer.from_bytes(value_of[byte_range.start:byte_range.end])

    spec = ArraySpec(shape=array.chunks, dtype=metadata_v3(array).data_type, fill_value=0,
                     config=ArrayConfig.from_dict({}), prototype=default_buffer_prototype())
    selection = (slice(0, 2), slice(2, 4), slice(0, 2))
    got = asyncio.run(codec._decode_partial_single(cast("ByteGetter", Getter()), selection, spec))
    assert got is not None
    assert np.array_equal(got.as_numpy_array(), values((10, 9, 5))[0:2, 2:4, 0:2])
    # the header (a literal, which costs no request), the shard index, the minishard index, the chunk
    assert len(reads) == 4


def test_chunk_ids_are_compressed_morton():
    # BANC's 8 nm scale: inspected, its shard 0's first minishard holds ids 0 to 2047
    grid = (1024, 1152, 439)
    assert chunk_id((0, 0, 0), grid) == 0
    assert chunk_id((1, 0, 0), grid) == 1
    assert chunk_id((0, 1, 0), grid) == 2
    assert chunk_id((0, 0, 1), grid) == 4
    assert chunk_id((2, 0, 0), grid) == 8
    # an axis of 1 chunk has no bits, and the others close up
    assert chunk_id((1, 0, 1), (2, 1, 2)) == 3
    assert chunk_id((0, 0, 3), (2, 1, 8)) == 0b110


def test_a_raw_chunk_of_the_wrong_length_is_refused():
    with pytest.raises(PrecomputedChunkError, match="is not raw, gzip, zstd or brotli"):
        uncode(b"\x00" * 5, "raw", (8, 16), 1)


def test_a_bad_gzip_chunk_is_refused():
    with pytest.raises(PrecomputedChunkError, match="bad gzip stream"):
        uncode(b"\x1f\x8bbroken", "raw", (4, 16), 1)


def test_a_chunk_of_the_wrong_image_size_is_refused():
    small = PrecomputedCodec(encoding="jpeg", size=(4, 4, 2), chunk_size=(4, 4, 2))
    data = (FIXTURES / "precomputed_jpeg_rgb" / "rgb" / "0-16_0-16_0-2").read_bytes()
    with pytest.raises(PrecomputedChunkError, match="is an image of"):
        small._decode_chunk(data, (0, 0, 0), 3, np.dtype("u1"))


def test_a_short_shard_is_refused():
    codec = PrecomputedCodec(encoding="raw", size=(2, 2, 2), chunk_size=(2, 2, 2), preshift_bits=0, minishard_bits=1)

    async def read(_offset, _length):
        return b"short"

    with pytest.raises(PrecomputedChunkError, match="has no shard index"):
        asyncio.run(codec._read_shard(read, [(0, 0, 0)]))


def test_a_minishard_index_not_of_whole_entries_is_refused():
    codec = PrecomputedCodec(encoding="raw", size=(2, 2, 2), chunk_size=(2, 2, 2), preshift_bits=0, minishard_bits=0)
    shard = struct.pack("<QQ", 0, 5) + b"12345"

    async def read(offset, length):
        return shard[offset:offset + length]

    with pytest.raises(PrecomputedChunkError, match="not a multiple of 24"):
        asyncio.run(codec._read_shard(read, [(0, 0, 0)]))


def test_a_chunk_without_its_header_is_refused():
    with pytest.raises(PrecomputedChunkError, match="has no header"):
        PrecomputedCodec._origin(b"abc")


def test_an_unknown_encoding_is_refused():
    with pytest.raises(ValueError, match="has no decoder"):
        PrecomputedCodec.from_dict({"name": "refkv.precomputed", "configuration": {
            "encoding": "fpzip", "size": [1, 1, 1], "chunk_size": [1, 1, 1]}})
