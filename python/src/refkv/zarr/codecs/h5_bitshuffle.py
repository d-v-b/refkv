"""`refkv.h5_bitshuffle`: a chunk of HDF5's bitshuffle filter (32008), with or without its LZ4
(spec/zarrify/hdf5.md §3.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#34-the-codec-refkvh5_bitshuffle>),
decoded by the Rust core. It has no encoder.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self, override

from zarr.abc.codec import BytesBytesCodec
from zarr.core.array_spec import ArraySpec
from zarr.core.buffer import Buffer
from zarr.core.common import JSON
from zarr.registry import register_codec

from refkv import _core
from refkv._narrow import at, integer, string


@dataclass(frozen=True)
class H5BitshuffleCodec(BytesBytesCodec):
    """`refkv.h5_bitshuffle`, configuration `{"elementsize": s, "block_size": b, "compression":
    "lz4" or "none"}`, decoding only: `b` governs without LZ4, and with it the chunk's own header."""

    is_fixed_size = False
    elementsize: int = 1
    block_size: int = 0
    compression: str = "lz4"

    def __init__(self, *, elementsize: int, block_size: int, compression: str) -> None:
        if compression not in ("lz4", "none"):
            raise ValueError(f"refkv.h5_bitshuffle: unknown compression {compression!r}")
        object.__setattr__(self, "elementsize", elementsize)
        object.__setattr__(self, "block_size", block_size)
        object.__setattr__(self, "compression", compression)

    @classmethod
    @override
    def from_dict(cls, data: dict[str, JSON]) -> Self:
        return cls(
            elementsize=integer(at(data, "configuration", "elementsize"), "configuration.elementsize"),
            block_size=integer(at(data, "configuration", "block_size"), "configuration.block_size"),
            compression=string(at(data, "configuration", "compression"), "configuration.compression"),
        )

    @override
    def to_dict(self) -> dict[str, JSON]:
        return {
            "name": "refkv.h5_bitshuffle",
            "configuration": {
                "elementsize": self.elementsize,
                "block_size": self.block_size,
                "compression": self.compression,
            },
        }

    @override
    async def _decode_single(self, chunk_data: Buffer, chunk_spec: ArraySpec) -> Buffer:
        lz4 = self.compression == "lz4"
        raw = _core.h5_bitshuffle_decode(chunk_data.to_bytes(), self.elementsize, self.block_size, lz4)
        return chunk_spec.prototype.buffer.from_bytes(raw)

    @override
    async def _encode_single(self, chunk_data: Buffer, chunk_spec: ArraySpec) -> Buffer:
        raise NotImplementedError("refkv.h5_bitshuffle decodes only")

    @override
    def compute_encoded_size(self, input_byte_length: int, chunk_spec: ArraySpec) -> int:
        raise NotImplementedError


register_codec("refkv.h5_bitshuffle", H5BitshuffleCodec)
