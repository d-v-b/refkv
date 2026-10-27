"""`imagecodecs_lzw`: a TIFF LZW stream (TIFF 6.0 §13, MSB-first with early change), as TIFF
and QPTIFF tiles and strips are (spec/zarrify/tiff.md §4.3
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#43-data-types-and-codecs>),
decoded into the chunk's size, as TIFF readers decode a tile: a stream that runs on past it
(some scanners write the final code one bit too narrow) still gives the chunk.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self, cast, override

import numpy as np
from zarr.abc.codec import BytesBytesCodec
from zarr.core.array_spec import ArraySpec
from zarr.core.buffer import Buffer
from zarr.core.common import JSON
from zarr.core.dtype.common import HasItemSize
from zarr.registry import register_codec

from refkv.zarr.codecs._imagecodecs import imagecodecs


@dataclass(frozen=True)
class LzwCodec(BytesBytesCodec):
    """`imagecodecs_lzw`: each chunk is one TIFF LZW stream (compression 5)."""

    is_fixed_size = False

    @classmethod
    @override
    def from_dict(cls, data: dict[str, JSON]) -> Self:
        return cls()

    @override
    def to_dict(self) -> dict[str, JSON]:
        return {"name": "imagecodecs_lzw"}

    @override
    async def _decode_single(self, chunk_data: Buffer, chunk_spec: ArraySpec) -> Buffer:
        # Into a buffer of the chunk's size: the decoder stops when it is full, so a stream whose
        # final code is malformed (and reads as one more entry) still decodes to the chunk; a
        # stream that is short decodes to fewer bytes, which the bytes codec then refuses.
        size = cast("HasItemSize", chunk_spec.dtype).item_size  # a TIFF's data types are of fixed size
        for d in chunk_spec.shape:
            size *= d
        out = np.empty(size, dtype=np.uint8)
        decoded = imagecodecs().lzw_decode(chunk_data.to_bytes(), out=out)
        return chunk_spec.prototype.buffer.from_bytes(np.ascontiguousarray(decoded).tobytes())

    @override
    async def _encode_single(self, chunk_data: Buffer, chunk_spec: ArraySpec) -> Buffer:
        return chunk_spec.prototype.buffer.from_bytes(imagecodecs().lzw_encode(chunk_data.to_bytes()))

    def compute_encoded_size(self, input_byte_length: int, chunk_spec: ArraySpec) -> int:
        raise NotImplementedError


register_codec("imagecodecs_lzw", LzwCodec)
