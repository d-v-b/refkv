"""`refkv.skip`: a fixed number of bytes before a chunk's own, dropped in decoding
(spec/conventions.md §3 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#3-arrays>).
A deflated member of a `.npz` archive inflates to its NPY header and then its data, and `gzip`
gives both: this codec, after it in decoding, drops the header
(spec/zarrify/npy.md §8.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#84-chunks>).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self, override

from zarr.abc.codec import BytesBytesCodec
from zarr.core.array_spec import ArraySpec
from zarr.core.buffer import Buffer
from zarr.core.common import JSON
from zarr.registry import register_codec

from refkv._narrow import at, integer


@dataclass(frozen=True)
class SkipCodec(BytesBytesCodec):
    """`refkv.skip`, configuration `{"length": k}`: decoding drops a chunk's first `k` bytes,
    and refuses a chunk of fewer; encoding puts `k` zero bytes before it."""

    is_fixed_size = True
    length: int = 0

    def __init__(self, *, length: int) -> None:
        if length < 0:
            raise ValueError(f"refkv.skip's length must not be negative, got {length}")
        object.__setattr__(self, "length", length)

    @classmethod
    @override
    def from_dict(cls, data: dict[str, JSON]) -> Self:
        return cls(length=integer(at(data, "configuration", "length"), "configuration.length"))

    @override
    def to_dict(self) -> dict[str, JSON]:
        return {"name": "refkv.skip", "configuration": {"length": self.length}}

    @override
    async def _decode_single(self, chunk_data: Buffer, chunk_spec: ArraySpec) -> Buffer:
        if len(chunk_data) < self.length:
            raise ValueError(f"refkv.skip: a chunk of {len(chunk_data)} bytes, fewer than the {self.length} it skips")
        return chunk_data[self.length :]

    @override
    async def _encode_single(self, chunk_data: Buffer, chunk_spec: ArraySpec) -> Buffer:
        return chunk_spec.prototype.buffer.from_bytes(bytes(self.length) + chunk_data.to_bytes())

    @override
    def compute_encoded_size(self, input_byte_length: int, chunk_spec: ArraySpec) -> int:
        return input_byte_length + self.length


register_codec("refkv.skip", SkipCodec)
