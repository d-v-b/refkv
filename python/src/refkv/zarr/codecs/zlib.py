"""`zlib`: a zlib stream (RFC 1950), as Neuroglancer names it (conventions §3
<https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#3-arrays>,
spec/zarrify/n5.md §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/n5.md#3-datasets>,
spec/zarrify/zarr2.md §3.1
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#31-compressors>).
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from typing import Self

from zarr.abc.codec import BytesBytesCodec
from zarr.core.array_spec import ArraySpec
from zarr.core.buffer import Buffer
from zarr.registry import register_codec


@dataclass(frozen=True)
class ZlibCodec(BytesBytesCodec):
    """`zlib`: each chunk is one zlib stream."""

    is_fixed_size = False
    level: int = 1

    @classmethod
    def from_dict(cls, data: dict) -> Self:
        return cls(**data.get("configuration", {}))

    def to_dict(self) -> dict:
        return {"name": "zlib", "configuration": {"level": self.level}}

    async def _decode_single(self, chunk_bytes: Buffer, chunk_spec: ArraySpec) -> Buffer:
        return chunk_spec.prototype.buffer.from_bytes(zlib.decompress(chunk_bytes.to_bytes()))

    async def _encode_single(self, chunk_bytes: Buffer, chunk_spec: ArraySpec) -> Buffer:
        return chunk_spec.prototype.buffer.from_bytes(zlib.compress(chunk_bytes.to_bytes(), self.level))

    def compute_encoded_size(self, input_byte_length: int, chunk_spec: ArraySpec) -> int:
        raise NotImplementedError


register_codec("zlib", ZlibCodec)
