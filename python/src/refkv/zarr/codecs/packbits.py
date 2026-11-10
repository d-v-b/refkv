"""`packbits`: zarr-extensions' array-to-bytes codec of elements narrower than a byte
(https://github.com/zarr-developers/zarr-extensions/tree/main/codecs/packbits), which
zarr-python does not provide. It is bit packing, not TIFF's PackBits, a run-length code
(`imagecodecs_packbits`). Element `i` is the bits `[i k, (i + 1) k)` of the data, least
significant bit first, as a safetensors file holds its `F4`, `F6_E2M3` and `F6_E3M2` tensors
(spec/zarrify/safetensors.md §3.2
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#32-packed-data-types>),
a DICOM file its 1-bit images (spec/zarrify/dicom.md §3
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#3-the-image>) and an MRC file its
4-bit ones (spec/zarrify/mrc.md §3
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mrc.md#3-data-types>).

This implements the data types zarrified archives name, `bool` (1 bit), `uint8` (8),
`float4_e2m1fn` (4), `float6_e2m3fn` and `float6_e3m2fn` (6), with every `padding_encoding`, and
`first_bit` and `last_bit`, which limit an element to the bits between them, inclusive: a decoded
element is those bits shifted to `first_bit`, the others zero. Another data type, or a range
outside the data type's bits, is refused.
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

from refkv._narrow import at, integer, string
from refkv.zarr.dtypes import BITS

if TYPE_CHECKING:
    from zarr.core.buffer.core import NDArrayLike

PADDINGS = ("none", "first_byte", "last_byte")
WIDTHS: dict[str, int] = {**BITS, "bool": 1, "uint8": 8}
"""The bits of each data type's element in memory, one byte an element, that `packbits` reads."""


def _width(chunk_spec: ArraySpec) -> int:
    name = chunk_spec.dtype.to_json(zarr_format=3)
    if not isinstance(name, str) or name not in WIDTHS:
        raise ValueError(f"packbits: the data type {name!r} is not one refkv packs (one of {sorted(WIDTHS)})")
    return WIDTHS[name]


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
    """`packbits`, configuration `{"padding_encoding": "none" | "first_byte" | "last_byte",
    "first_bit": int | null, "last_bit": int | null}`."""

    is_fixed_size = True
    padding_encoding: str = "none"
    first_bit: int | None = None
    last_bit: int | None = None

    def __init__(self, *, padding_encoding: str = "none", first_bit: int | None = None,
                 last_bit: int | None = None) -> None:
        if padding_encoding not in PADDINGS:
            raise ValueError(f"packbits: padding_encoding {padding_encoding!r} is not one of {PADDINGS}")
        if first_bit is not None and first_bit < 0:
            raise ValueError(f"packbits: first_bit {first_bit} is negative")
        if last_bit is not None and last_bit < (first_bit or 0):
            raise ValueError(f"packbits: last_bit {last_bit} is less than first_bit {first_bit or 0}")
        object.__setattr__(self, "padding_encoding", padding_encoding)
        object.__setattr__(self, "first_bit", first_bit)
        object.__setattr__(self, "last_bit", last_bit)

    def _range(self, chunk_spec: ArraySpec) -> tuple[int, int]:
        """(the element's first bit, its bits `k`), which the data type's width bounds."""
        width = _width(chunk_spec)
        first = self.first_bit or 0
        last = width - 1 if self.last_bit is None else self.last_bit
        if first >= width or last >= width:
            raise ValueError(f"packbits: the bits {first} to {last} are not all among the {width} of the data type")
        return first, last - first + 1

    @classmethod
    @override
    def from_dict(cls, data: dict[str, JSON]) -> Self:
        config = data.get("configuration") or {}
        if not isinstance(config, dict):
            raise TypeError("packbits: configuration is not an object")
        bits: dict[str, int | None] = {"first_bit": None, "last_bit": None}
        for key in bits:
            if config.get(key) is not None:
                bits[key] = integer(at(data, "configuration", key), key)
        padding = "none"
        if "padding_encoding" in config:
            padding = string(at(data, "configuration", "padding_encoding"), "padding_encoding")
        return cls(padding_encoding=padding, first_bit=bits["first_bit"], last_bit=bits["last_bit"])

    @override
    def to_dict(self) -> dict[str, JSON]:
        config: dict[str, JSON] = {"padding_encoding": self.padding_encoding}
        if self.first_bit is not None:
            config["first_bit"] = self.first_bit
        if self.last_bit is not None:
            config["last_bit"] = self.last_bit
        return {"name": "packbits", "configuration": config}

    @override
    async def _decode_single(self, chunk_data: Buffer, chunk_spec: ArraySpec) -> NDBuffer:
        first, k = self._range(chunk_spec)
        data = chunk_data.to_bytes()
        if self.padding_encoding == "first_byte":
            data = data[1:]
        elif self.padding_encoding == "last_byte":
            data = data[:-1]
        count = int(np.prod(chunk_spec.shape))
        elements = unpack(data, count, k) << first
        values = elements.view(chunk_spec.dtype.to_native_dtype()).reshape(chunk_spec.shape)
        return chunk_spec.prototype.nd_buffer.from_ndarray_like(cast("NDArrayLike", values))

    @override
    async def _encode_single(self, chunk_data: NDBuffer, chunk_spec: ArraySpec) -> Buffer | None:
        first, k = self._range(chunk_spec)
        values = np.ascontiguousarray(chunk_data.as_numpy_array()).view(np.uint8).reshape(-1) >> first
        packed = pack(values, k)
        pad = len(packed) * 8 - values.size * k
        if self.padding_encoding == "first_byte":
            packed = bytes([pad]) + packed
        elif self.padding_encoding == "last_byte":
            packed = packed + bytes([pad])
        return chunk_spec.prototype.buffer.from_bytes(packed)

    @override
    def compute_encoded_size(self, input_byte_length: int, chunk_spec: ArraySpec) -> int:
        _, k = self._range(chunk_spec)
        return (input_byte_length * k + 7) // 8 + (self.padding_encoding != "none")


register_codec("packbits", PackBitsCodec)
