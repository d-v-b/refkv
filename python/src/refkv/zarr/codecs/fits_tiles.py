"""`refkv.fits_tiles`: a chunk of a tile-compressed FITS image, a prefix of per-tile records then
the tiles' RICE_1, GZIP_1, GZIP_2 or NOCOMPRESS streams, dequantized as cfitsio does
(spec/zarrify/fits.md §3.3
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#33-tile-compressed-images>),
decoded by the Rust core. It has no encoder.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Self, cast, override

import numpy as np
from zarr.abc.codec import ArrayBytesCodec
from zarr.core.array_spec import ArraySpec
from zarr.core.buffer import Buffer, NDBuffer
from zarr.core.buffer.core import NDArrayLike
from zarr.core.common import JSON
from zarr.registry import register_codec

from refkv import _core


@dataclass(frozen=True)
class FitsTilesCodec(ArrayBytesCodec):
    """The `refkv.fits_tiles` codec, decoding only: the Rust core decodes each chunk."""

    is_fixed_size = False
    config: str = "{}"  # the configuration as JSON (a dataclass field must be hashable)

    @classmethod
    @override
    def from_dict(cls, data: dict[str, JSON]) -> Self:
        return cls(config=json.dumps(data.get("configuration", {})))

    @override
    def to_dict(self) -> dict[str, JSON]:
        return {"name": "refkv.fits_tiles", "configuration": json.loads(self.config)}

    @override
    async def _decode_single(self, chunk_data: Buffer, chunk_spec: ArraySpec) -> NDBuffer:
        raw = _core.fits_tiles_decode(chunk_data.to_bytes(), self.config, [int(n) for n in chunk_spec.shape])
        dtype = chunk_spec.dtype.to_native_dtype().newbyteorder("<")
        arr = np.frombuffer(raw, dtype=dtype).reshape(chunk_spec.shape)
        # an ndarray is NDArrayLike, though numpy's stubs do not match zarr's protocol
        return chunk_spec.prototype.nd_buffer.from_ndarray_like(cast("NDArrayLike", arr))

    @override
    async def _encode_single(self, chunk_data: NDBuffer, chunk_spec: ArraySpec) -> Buffer:
        raise NotImplementedError("refkv.fits_tiles decodes only")

    def compute_encoded_size(self, input_byte_length: int, chunk_spec: ArraySpec) -> int:
        raise NotImplementedError


register_codec("refkv.fits_tiles", FitsTilesCodec)
