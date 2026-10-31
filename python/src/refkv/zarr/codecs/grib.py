"""`refkv.grib`: a chunk that is one GRIB edition 2 message, of which one field (`field`, from 0)
is decoded into its grid's values, binary64 then the array's float64 or float32, NaN where its
bitmap or its complex packing's missing values leave a point out (spec/conventions.md §12
<https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#12-grib-messages>), decoded by the
Rust core. It has no encoder.
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
class GribCodec(ArrayBytesCodec):
    """The `refkv.grib` codec, decoding only: the Rust core decodes each chunk."""

    is_fixed_size = False
    config: str = "{}"  # the configuration as JSON (a dataclass field must be hashable)

    @classmethod
    @override
    def from_dict(cls, data: dict[str, JSON]) -> Self:
        return cls(config=json.dumps(data.get("configuration", {})))

    @override
    def to_dict(self) -> dict[str, JSON]:
        return {"name": "refkv.grib", "configuration": json.loads(self.config)}

    @override
    async def _decode_single(self, chunk_data: Buffer, chunk_spec: ArraySpec) -> NDBuffer:
        dtype = chunk_spec.dtype.to_native_dtype()
        if dtype.kind != "f" or dtype.itemsize not in (4, 8):
            raise ValueError(f"refkv.grib: an array of {dtype} is not decoded (float64 or float32)")
        raw = _core.grib_decode(chunk_data.to_bytes(), self.config, [int(n) for n in chunk_spec.shape])
        # binary64, rounded to nearest for a float32 array
        arr = np.frombuffer(raw, dtype="<f8").astype(dtype).reshape(chunk_spec.shape)
        # an ndarray is NDArrayLike, though numpy's stubs do not match zarr's protocol
        return chunk_spec.prototype.nd_buffer.from_ndarray_like(cast("NDArrayLike", arr))

    @override
    async def _encode_single(self, chunk_data: NDBuffer, chunk_spec: ArraySpec) -> Buffer:
        raise NotImplementedError("refkv.grib decodes only")

    def compute_encoded_size(self, input_byte_length: int, chunk_spec: ArraySpec) -> int:
        raise NotImplementedError


register_codec("refkv.grib", GribCodec)
