"""`imagecodecs_jpeg`: each chunk is one complete JPEG stream (as zarrified JPEG-in-TIFF tiles
are, spec/zarrify/tiff.md §7.3
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#73-chunks>). It decodes only;
its encoder exists so zarr can create the array metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self, cast, override

import numpy as np
from zarr.abc.codec import ArrayBytesCodec
from zarr.core.array_spec import ArraySpec
from zarr.core.buffer import Buffer, NDBuffer
from zarr.core.buffer.core import NDArrayLike
from zarr.core.common import JSON
from zarr.registry import register_codec

from refkv.zarr.codecs._imagecodecs import imagecodecs


@dataclass(frozen=True)
class JpegCodec(ArrayBytesCodec):
    is_fixed_size = False

    @classmethod
    @override
    def from_dict(cls, data: dict[str, JSON]) -> Self:
        return cls()

    @override
    def to_dict(self) -> dict[str, JSON]:
        return {"name": "imagecodecs_jpeg"}

    @override
    async def _decode_single(self, chunk_data: Buffer, chunk_spec: ArraySpec) -> NDBuffer:
        img = imagecodecs().jpeg8_decode(chunk_data.to_bytes())
        img = np.asarray(img, dtype=chunk_spec.dtype.to_native_dtype()).reshape(chunk_spec.shape)
        # an ndarray is NDArrayLike, though numpy's stubs do not match zarr's protocol
        return chunk_spec.prototype.nd_buffer.from_ndarray_like(cast("NDArrayLike", img))

    @override
    async def _encode_single(self, chunk_data: NDBuffer, chunk_spec: ArraySpec) -> Buffer:
        arr = np.squeeze(chunk_data.as_numpy_array())
        return chunk_spec.prototype.buffer.from_bytes(imagecodecs().jpeg8_encode(arr))

    def compute_encoded_size(self, input_byte_length: int, chunk_spec: ArraySpec) -> int:
        raise NotImplementedError


register_codec("imagecodecs_jpeg", JpegCodec)
