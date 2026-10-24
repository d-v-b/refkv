"""`mp3`: a chunk of a virtualized MP3 file (spec/virtualize/mp3.md §4.1
<https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp3.md#41-the-array>): the samples to
keep, then MPEG Layer III frames, decoded by miniaudio (the `audio` extra). It has no encoder.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self

import numpy as np
from zarr.abc.codec import ArrayBytesCodec
from zarr.core.array_spec import ArraySpec
from zarr.core.buffer import Buffer, NDBuffer
from zarr.registry import register_codec

from refkv._extras import missing


def _miniaudio():
    """miniaudio, which the mp3 codec needs, imported when a chunk is first decoded."""
    try:
        import miniaudio
    except ImportError as e:
        raise missing("audio", "Reading MP3 chunks", e) from e
    return miniaudio


@dataclass(frozen=True)
class Mp3Codec(ArrayBytesCodec):
    """`mp3`: a chunk is a little-endian uint32, the samples per channel to keep, then MPEG
    Layer III frames: the frames before the chunk's that a decoder needs (its lead-in), then
    the chunk's own (spec/virtualize/mp3.md §4.1
    <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp3.md#41-the-array>, §8.2
    <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp3.md#82-chunks>). The frames
    decode, in one pass, to int16 of shape [samples, channels]; the chunk's samples are the
    last `keep` of them, placed first and followed by zeros up to the chunk's shape. When the
    decoder gives fewer (the stream's first frames, which have no lead-in, may not decode),
    they end at `keep` and zeros precede them."""

    is_fixed_size = False

    @classmethod
    def from_dict(cls, data: dict) -> Self:
        return cls()

    def to_dict(self) -> dict:
        return {"name": "mp3"}

    async def _decode_single(self, chunk_bytes: Buffer, chunk_spec: ArraySpec) -> NDBuffer:
        b = chunk_bytes.to_bytes()
        if len(b) < 4:
            raise ValueError("an mp3 chunk starts with the 4-byte count of its samples")
        keep = int.from_bytes(b[:4], "little")
        audio = b[4:]
        ma = _miniaudio()
        info = ma.mp3_get_info(audio)
        decoded = ma.decode(audio, ma.SampleFormat.SIGNED16, nchannels=info.nchannels, sample_rate=info.sample_rate)
        pcm = np.frombuffer(decoded.samples, dtype=np.int16).reshape(-1, info.nchannels)
        n, c = chunk_spec.shape
        if c != info.nchannels or keep > n:
            raise ValueError(f"the frames hold {info.nchannels} channels and {keep} samples; the chunk is {n} x {c}")
        out = np.zeros((n, c), dtype=np.int16)
        tail = pcm[max(len(pcm) - keep, 0):]
        out[keep - len(tail):keep] = tail
        return chunk_spec.prototype.nd_buffer.from_ndarray_like(out.astype(chunk_spec.dtype.to_native_dtype(), copy=False))

    async def _encode_single(self, chunk_array: NDBuffer, chunk_spec: ArraySpec) -> Buffer:
        raise NotImplementedError("the mp3 codec decodes only")

    def compute_encoded_size(self, input_byte_length: int, chunk_spec: ArraySpec) -> int:
        raise NotImplementedError


register_codec("mp3", Mp3Codec)
