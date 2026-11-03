"""`packbits`: zarr-extensions' array-to-bytes codec of elements narrower than a byte
(https://github.com/zarr-developers/zarr-extensions/tree/main/codecs/packbits), which
zarr-python does not provide. A safetensors file's `F4`, `F6_E2M3` and `F6_E3M2` tensors hold
their elements packed so, element `i` the bits `[i k, (i + 1) k)` of the data, least significant
bit first (spec/zarrify/safetensors.md §3.2
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#32-packed-data-types>).

This implements the data types zarrified archives name, `float4_e2m1fn` (4 bits),
`float6_e2m3fn` and `float6_e3m2fn` (6 bits), with every `padding_encoding`, and without
`first_bit` and `last_bit`: a configuration that sets them, or another data type, is refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, cast, override

import numpy as np
from zarr.abc.codec import ArrayBytesCodec
from zarr.core.array_spec import ArraySpec
from zarr.core.buffer import Buffer, NDBuffer
from zarr.core.common import JSON
from zarr.registry import register_codec

from refkv._narrow import at, string
from refkv.zarr.dtypes import BITS

if TYPE_CHECKING:
    from zarr.core.buffer.core import NDArrayLike

PADDINGS = ("none", "first_byte", "last_byte")


def _bits(chunk_spec: ArraySpec) -> int:
    name = chunk_spec.dtype.to_json(zarr_format=3)
    if not isinstance(name, str) or name not in BITS:
        raise ValueError(f"packbits: the data type {name!r} is not one refkv packs (one of {sorted(BITS)})")
    return BITS[name]


def unpack(data: bytes, count: int, k: int) -> np.ndarray:
    """The first `count` elements of `k` bits each of `data`, least significant bit first, one
    `uint8` each."""
    bits = np.unpackbits(np.frombuffer(data, dtype=np.uint8), bitorder="little")
    if bits.size < count * k:
        raise ValueError(f"packbits: a chunk of {len(data)} bytes, fewer than {count} elements of {k} bits")
    elements = bits[: count * k].reshape(count, k)
    return np.packbits(elements, axis=1, bitorder="little")[:, 0]


def pack(values: np.ndarray, k: int) -> bytes:
    """`values`, `uint8` elements, as their low `k` bits each, least significant bit first, padded
    with zero bits to a whole byte."""
    bits = np.unpackbits(values.reshape(-1, 1), axis=1, bitorder="little")[:, :k]
    return np.packbits(bits.reshape(-1), bitorder="little").tobytes()


@dataclass(frozen=True)
class PackBitsCodec(ArrayBytesCodec):
    """`packbits`, configuration `{"padding_encoding": "none" | "first_byte" | "last_byte"}`."""

    is_fixed_size = True
    padding_encoding: str = "none"

    def __init__(self, *, padding_encoding: str = "none") -> None:
        if padding_encoding not in PADDINGS:
            raise ValueError(f"packbits: padding_encoding {padding_encoding!r} is not one of {PADDINGS}")
        object.__setattr__(self, "padding_encoding", padding_encoding)

    @classmethod
    @override
    def from_dict(cls, data: dict[str, JSON]) -> Self:
        config = data.get("configuration") or {}
        if not isinstance(config, dict):
            raise TypeError("packbits: configuration is not an object")
        for key in ("first_bit", "last_bit"):
            if config.get(key) is not None:
                raise ValueError(f"packbits: {key} is not supported")
        padding = "none"
        if "padding_encoding" in config:
            padding = string(at(data, "configuration", "padding_encoding"), "padding_encoding")
        return cls(padding_encoding=padding)

    @override
    def to_dict(self) -> dict[str, JSON]:
        return {"name": "packbits", "configuration": {"padding_encoding": self.padding_encoding}}

    @override
    async def _decode_single(self, chunk_data: Buffer, chunk_spec: ArraySpec) -> NDBuffer:
        k = _bits(chunk_spec)
        data = chunk_data.to_bytes()
        if self.padding_encoding == "first_byte":
            data = data[1:]
        elif self.padding_encoding == "last_byte":
            data = data[:-1]
        count = int(np.prod(chunk_spec.shape))
        values = unpack(data, count, k).view(chunk_spec.dtype.to_native_dtype()).reshape(chunk_spec.shape)
        return chunk_spec.prototype.nd_buffer.from_ndarray_like(cast("NDArrayLike", values))

    @override
    async def _encode_single(self, chunk_data: NDBuffer, chunk_spec: ArraySpec) -> Buffer | None:
        k = _bits(chunk_spec)
        values = np.ascontiguousarray(chunk_data.as_numpy_array()).view(np.uint8).reshape(-1)
        packed = pack(values, k)
        pad = len(packed) * 8 - values.size * k
        if self.padding_encoding == "first_byte":
            packed = bytes([pad]) + packed
        elif self.padding_encoding == "last_byte":
            packed = packed + bytes([pad])
        return chunk_spec.prototype.buffer.from_bytes(packed)

    @override
    def compute_encoded_size(self, input_byte_length: int, chunk_spec: ArraySpec) -> int:
        k = _bits(chunk_spec)
        return (input_byte_length * k + 7) // 8 + (self.padding_encoding != "none")


register_codec("packbits", PackBitsCodec)
