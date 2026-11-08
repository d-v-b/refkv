"""`refkv.skip`: a fixed number of bytes before a chunk's own, and any after them, dropped in decoding
(spec/conventions.md §3 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#3-arrays>).
A deflated member of a `.npz` archive inflates to its NPY header and then its data, and `gzip`
gives both: this codec, after it in decoding, drops the header
(spec/zarrify/npy.md §8.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#84-chunks>).
A compressed FITS file's array is one chunk, the whole stream, which decodes to the other HDUs too:
`keep` stops at the array's end
(spec/zarrify/fits.md §2.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#21-a-compressed-file>).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self, override

from zarr.abc.codec import BytesBytesCodec
from zarr.core.array_spec import ArraySpec
from zarr.core.buffer import Buffer
from zarr.core.common import JSON
from zarr.registry import register_codec

from refkv._narrow import at, integer, mapping


@dataclass(frozen=True)
class SkipCodec(BytesBytesCodec):
    """`refkv.skip`, configuration `{"length": k}` or `{"length": k, "keep": n}`: decoding drops
    a chunk's first `k` bytes and, with `keep`, any after the `n` that follow them, and refuses
    a chunk of fewer than those; encoding puts `k` zero bytes before it."""

    is_fixed_size = True
    length: int = 0
    keep: int | None = None

    def __init__(self, *, length: int, keep: int | None = None) -> None:
        if length < 0:
            raise ValueError(f"refkv.skip's length must not be negative, got {length}")
        if keep is not None and keep < 0:
            raise ValueError(f"refkv.skip's keep must not be negative, got {keep}")
        object.__setattr__(self, "length", length)
        object.__setattr__(self, "keep", keep)

    @classmethod
    @override
    def from_dict(cls, data: dict[str, JSON]) -> Self:
        keep = mapping(data["configuration"]).get("keep")
        return cls(length=integer(at(data, "configuration", "length"), "configuration.length"),
                   keep=None if keep is None else integer(keep, "configuration.keep"))

    @override
    def to_dict(self) -> dict[str, JSON]:
        configuration: dict[str, JSON] = {"length": self.length}
        if self.keep is not None:
            configuration["keep"] = self.keep
        return {"name": "refkv.skip", "configuration": configuration}

    @override
    async def _decode_single(self, chunk_data: Buffer, chunk_spec: ArraySpec) -> Buffer:
        end = len(chunk_data) if self.keep is None else self.length + self.keep
        if len(chunk_data) < self.length:
            raise ValueError(f"refkv.skip: a chunk of {len(chunk_data)} bytes, fewer than the {self.length} it skips")
        if len(chunk_data) < end:
            raise ValueError(f"refkv.skip: a chunk of {len(chunk_data)} bytes, fewer than the {end} it keeps up to")
        return chunk_data[self.length : end]

    @override
    async def _encode_single(self, chunk_data: Buffer, chunk_spec: ArraySpec) -> Buffer:
        return chunk_spec.prototype.buffer.from_bytes(bytes(self.length) + chunk_data.to_bytes())

    @override
    def compute_encoded_size(self, input_byte_length: int, chunk_spec: ArraySpec) -> int:
        return input_byte_length + self.length


register_codec("refkv.skip", SkipCodec)
