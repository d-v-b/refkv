"""`n5_default`: an N5 default-mode block, header included (zarr-extensions `codecs/n5_default`;
spec/zarrify/n5.md).
"""

from __future__ import annotations

import struct
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, cast, override

import numpy as np
import zarr
from zarr.abc.codec import ArrayBytesCodec, BytesBytesCodec, Codec, CodecPipeline
from zarr.codecs import BytesCodec, TransposeCodec
from zarr.core.array_spec import ArraySpec
from zarr.core.buffer import Buffer, NDBuffer
from zarr.core.buffer.core import NDArrayLike
from zarr.core.common import JSON
from zarr.core.metadata.v3 import parse_codecs
from zarr.registry import get_pipeline_class, register_codec

from refkv._narrow import at, sequence

if TYPE_CHECKING:
    from zarr.core.dtype.wrapper import TBaseDType, TBaseScalar, ZDType
    from zarr.core.metadata.v3 import ChunkGridMetadata


class N5BlockError(ValueError):
    """An N5 block that the n5_default codec cannot decode."""


@dataclass(frozen=True)
class N5DefaultCodec(ArrayBytesCodec):
    """`n5_default`: one N5 block, header included.

    Configuration `{"codecs": [transpose (full reversal), bytes (big-endian, or
    no endian for 1-byte types), optional one bytes-to-bytes codec]}`. Decoding
    parses the header (mode, number of dimensions, block size in N5 dimension
    order, which is the array's), requires default mode (0) and the array's
    number of dimensions, decodes the body with the inner codecs at the
    block's own size, and pads (with the fill value) or truncates it to the
    chunk's shape, so truncated edge blocks read correctly.
    """

    is_fixed_size = False
    codecs: tuple[Codec, ...] = ()

    def __init__(self, *, codecs: Iterable[Codec | JSON]) -> None:
        cs = parse_codecs(codecs)
        if not 2 <= len(cs) <= 3:
            raise ValueError(f"n5_default needs 2 or 3 inner codecs, got {len(cs)}")
        t, b = cs[0], cs[1]
        if not isinstance(t, TransposeCodec) or tuple(t.order) != tuple(range(len(t.order) - 1, -1, -1)):
            raise ValueError("n5_default's first codec must be a full-reversal transpose")
        if not isinstance(b, BytesCodec):
            raise ValueError("n5_default's second codec must be a bytes codec")  # noqa: TRY004 (a bad configuration, like the checks around it)
        if len(cs) == 3 and not isinstance(cs[2], BytesBytesCodec):
            raise ValueError("n5_default's third codec must be bytes-to-bytes")
        object.__setattr__(self, "codecs", tuple(cs))

    @classmethod
    @override
    def from_dict(cls, data: dict[str, JSON]) -> Self:
        return cls(codecs=sequence(at(data, "configuration", "codecs"), "configuration.codecs"))

    @override
    def to_dict(self) -> dict[str, JSON]:
        return {"name": "n5_default", "configuration": {"codecs": [c.to_dict() for c in self.codecs]}}

    @override
    def evolve_from_array_spec(self, array_spec: ArraySpec) -> Self:
        return self

    @override
    def validate(
        self,
        *,
        shape: tuple[int, ...],
        dtype: ZDType[TBaseDType, TBaseScalar],
        chunk_grid: ChunkGridMetadata,
    ) -> None:
        # a transpose and a bytes codec, as __init__ checked
        transpose, bytes_codec = cast("TransposeCodec", self.codecs[0]), cast("BytesCodec", self.codecs[1])
        if len(shape) != len(transpose.order):
            raise ValueError(f"n5_default: the array has {len(shape)} dimensions, the codec "
                             f"{len(transpose.order)}")
        # zarr-python gives a bytes codec without configuration the native endianness;
        # it only matters, and must be big, for types of more than one byte.
        endian = getattr(bytes_codec.endian, "value", bytes_codec.endian)
        if dtype.to_native_dtype().itemsize > 1 and endian != "big":
            raise ValueError("n5_default's bytes codec must be big-endian")

    def _pipeline(self) -> CodecPipeline:
        return get_pipeline_class().from_codecs(self.codecs)

    @override
    async def _decode_single(self, chunk_data: Buffer, chunk_spec: ArraySpec) -> NDBuffer:
        raw = chunk_data.to_bytes()
        if len(raw) < 4:
            raise N5BlockError("N5 block shorter than its header")
        mode, ndim = struct.unpack_from(">HH", raw)
        if mode != 0:
            raise N5BlockError(f"N5 block mode {mode} is not default (0)")
        if ndim != len(chunk_spec.shape):
            raise N5BlockError(f"N5 block has {ndim} dimensions, the array {len(chunk_spec.shape)}")
        if len(raw) < 4 + 4 * ndim:
            raise N5BlockError("N5 block shorter than its header")
        shape = struct.unpack_from(f">{ndim}I", raw, 4)
        body = chunk_spec.prototype.buffer.from_bytes(raw[4 + 4 * ndim :])
        spec = ArraySpec(tuple(shape), chunk_spec.dtype, chunk_spec.fill_value, chunk_spec.config,
                         chunk_spec.prototype)
        (decoded,) = await self._pipeline().decode([(body, spec)])
        if decoded is None:
            raise AssertionError("the inner codecs decoded a block's body to nothing")
        block = decoded.as_numpy_array()
        if tuple(shape) == tuple(chunk_spec.shape):
            return decoded
        out = np.full(chunk_spec.shape, chunk_spec.fill_value, dtype=block.dtype)
        common = tuple(slice(0, min(a, b)) for a, b in zip(shape, chunk_spec.shape, strict=True))
        out[common] = block[common]
        # an ndarray is NDArrayLike, though numpy's stubs do not match zarr's protocol
        return chunk_spec.prototype.nd_buffer.from_ndarray_like(cast("NDArrayLike", out))

    @override
    async def _encode_single(self, chunk_data: NDBuffer, chunk_spec: ArraySpec) -> Buffer:
        (body,) = await self._pipeline().encode([(chunk_data, chunk_spec)])
        if body is None:
            raise AssertionError("the inner codecs encoded a chunk to nothing")
        header = struct.pack(f">HH{len(chunk_spec.shape)}I", 0, len(chunk_spec.shape), *chunk_spec.shape)
        return chunk_spec.prototype.buffer.from_bytes(header + body.to_bytes())

    def compute_encoded_size(self, input_byte_length: int, chunk_spec: ArraySpec) -> int:
        raise NotImplementedError


register_codec("n5_default", N5DefaultCodec)

# Other packages (zarr-n5) register an `n5_default` too; zarr-python then picks
# one arbitrarily unless the config names it. Name this one, unless the caller
# already chose.
if zarr.config.get("codecs.n5_default", None) is None:
    zarr.config.set({"codecs.n5_default": "refkv.zarr.codecs.n5_default.N5DefaultCodec"})
