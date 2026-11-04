"""The `refkv.skip` codec (python/src/refkv/zarr/codecs/skip.py): it drops a fixed number of
leading bytes, and with `keep` any after the bytes it keeps, in decoding and puts as many zero
bytes back in encoding, and it refuses a chunk shorter than what it skips or keeps and a negative
length or `keep`."""

import asyncio

import numpy as np
import pytest
from zarr.core.array_spec import ArrayConfig, ArraySpec
from zarr.core.buffer import default_buffer_prototype
from zarr.core.dtype import UInt8

from refkv.zarr.codecs.skip import SkipCodec

SPEC = ArraySpec(
    shape=(3,),
    dtype=UInt8(),
    fill_value=0,
    config=ArrayConfig.from_dict({}),
    prototype=default_buffer_prototype(),
)


def buffer(b: bytes):
    return default_buffer_prototype().buffer.from_bytes(b)


def test_skips_and_restores_the_prefix():
    for length, chunk, decoded in [(0, b"abc", b"abc"), (2, b"..abc", b"abc"), (3, b"abc", b"")]:
        codec = SkipCodec.from_dict({"name": "refkv.skip", "configuration": {"length": length}})
        assert codec.to_dict() == {"name": "refkv.skip", "configuration": {"length": length}}
        got = asyncio.run(codec._decode_single(buffer(chunk), SPEC))
        assert got.to_bytes() == decoded
        encoded = asyncio.run(codec._encode_single(buffer(decoded), SPEC))
        assert encoded is not None
        assert encoded.to_bytes() == bytes(length) + decoded
        assert codec.compute_encoded_size(len(decoded), SPEC) == len(chunk)
    assert np.array_equal(np.frombuffer(b"abc", np.uint8), np.array([97, 98, 99], np.uint8))
    for length, keep, chunk, decoded in [(2, 3, b"..abc..", b"abc"), (0, 3, b"abc..", b"abc"), (2, 3, b"..abc", b"abc"),
                                         (2, 0, b"..abc", b"")]:
        configuration = {"length": length, "keep": keep}
        codec = SkipCodec.from_dict({"name": "refkv.skip", "configuration": configuration})
        assert codec.to_dict() == {"name": "refkv.skip", "configuration": configuration}
        assert asyncio.run(codec._decode_single(buffer(chunk), SPEC)).to_bytes() == decoded


def test_rejects_a_chunk_shorter_than_the_length():
    with pytest.raises(ValueError, match="fewer than the 4 it skips"):
        asyncio.run(SkipCodec(length=4)._decode_single(buffer(b"abc"), SPEC))


def test_rejects_a_negative_length():
    with pytest.raises(ValueError, match="must not be negative"):
        SkipCodec(length=-1)


def test_refuses_a_chunk_shorter_than_it_keeps():
    with pytest.raises(ValueError, match="fewer than the 5 it keeps up to"):
        asyncio.run(SkipCodec(length=2, keep=3)._decode_single(buffer(b"..ab"), SPEC))


def test_refuses_a_negative_keep():
    with pytest.raises(ValueError, match="must not be negative"):
        SkipCodec(length=0, keep=-1)
