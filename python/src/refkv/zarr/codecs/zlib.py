"""`zlib`: a zlib stream (RFC 1950), as Neuroglancer names it (conventions §3
<https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#3-arrays>,
spec/zarrify/n5.md §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/n5.md#3-datasets>,
spec/zarrify/zarr2.md §3.1
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#31-compressors>).
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from typing import Self, override

from zarr.abc.codec import BytesBytesCodec
from zarr.core.array_spec import ArraySpec
from zarr.core.buffer import Buffer
from zarr.core.common import JSON
from zarr.registry import register_codec

from refkv._narrow import integer, mapping


@dataclass(frozen=True)
class ZlibCodec(BytesBytesCodec):
    """`zlib`: each chunk is one zlib stream."""

    is_fixed_size = False
    level: int = 1

    @classmethod
    @override
    def from_dict(cls, data: dict[str, JSON]) -> Self:
        return cls(**{k: integer(v, k) for k, v in mapping(data.get("configuration", {}), "configuration").items()})

    @override
    def to_dict(self) -> dict[str, JSON]:
        return {"name": "zlib", "configuration": {"level": self.level}}

    @override
    async def _decode_single(self, chunk_data: Buffer, chunk_spec: ArraySpec) -> Buffer:
        return chunk_spec.prototype.buffer.from_bytes(zlib.decompress(chunk_data.to_bytes()))

    @override
    async def _encode_single(self, chunk_data: Buffer, chunk_spec: ArraySpec) -> Buffer:
        return chunk_spec.prototype.buffer.from_bytes(zlib.compress(chunk_data.to_bytes(), self.level))

    def compute_encoded_size(self, input_byte_length: int, chunk_spec: ArraySpec) -> int:
        raise NotImplementedError


register_codec("zlib", ZlibCodec)
