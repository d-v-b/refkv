"""`imagecodecs_jpeg`: each chunk is one complete JPEG stream (as zarrified JPEG-in-TIFF tiles
are, spec/zarrify/tiff.md §7.3
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#73-chunks>). It decodes only;
its encoder exists so zarr can create the array metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self

import numpy as np
from zarr.abc.codec import ArrayBytesCodec
from zarr.core.array_spec import ArraySpec
from zarr.core.buffer import Buffer, NDBuffer
from zarr.registry import register_codec

from refkv.zarr.codecs._imagecodecs import imagecodecs


@dataclass(frozen=True)
class JpegCodec(ArrayBytesCodec):
    is_fixed_size = False

    @classmethod
    def from_dict(cls, data: dict) -> Self:
        return cls()

    def to_dict(self) -> dict:
        return {"name": "imagecodecs_jpeg"}

    async def _decode_single(self, chunk_bytes: Buffer, chunk_spec: ArraySpec) -> NDBuffer:
        img = imagecodecs().jpeg8_decode(chunk_bytes.to_bytes())
        img = np.asarray(img, dtype=chunk_spec.dtype.to_native_dtype()).reshape(chunk_spec.shape)
        return chunk_spec.prototype.nd_buffer.from_ndarray_like(img)

    async def _encode_single(self, chunk_array: NDBuffer, chunk_spec: ArraySpec) -> Buffer:
        arr = np.squeeze(chunk_array.as_numpy_array())
        return chunk_spec.prototype.buffer.from_bytes(imagecodecs().jpeg8_encode(arr))

    def compute_encoded_size(self, input_byte_length: int, chunk_spec: ArraySpec) -> int:
        raise NotImplementedError


register_codec("imagecodecs_jpeg", JpegCodec)
