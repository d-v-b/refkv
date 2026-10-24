"""`refkv.stream`: a run of coded audio or video frames (conventions §10
<https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#10-coded-streams>), as virtualized MP3
files' chunks and MP4 tracks' are: a header giving each frame's size and output position, then the
frames, decoded by ffmpeg's decoders through PyAV (the `media` extra). It has no encoder.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Self

import numpy as np
from zarr.abc.codec import ArrayBytesCodec
from zarr.core.array_spec import ArraySpec
from zarr.core.buffer import Buffer, NDBuffer
from zarr.registry import register_codec

from refkv._extras import missing


def _av():
    """PyAV, whose ffmpeg decoders the refkv.stream codec uses, imported when a chunk is first
    decoded."""
    try:
        import av
    except ImportError as e:
        raise missing("media", "Reading MP3, AAC and H.264 chunks", e) from e
    return av


@dataclass(frozen=True)
class StreamCodec(ArrayBytesCodec):
    """`refkv.stream`: a chunk is a run of coded frames of an audio or video stream whose
    frames depend on earlier ones (conventions §10
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#10-coded-streams>): a header
    that gives each frame's size and output position, then the frames. The frames are decoded
    in one pass by ffmpeg's decoder of `format` (through PyAV: MP3, AAC or H.264), each
    frame's output placed at its position in the chunk, a frame with position -1 (the chunk's
    lead-in) decoded and dropped; what no frame fills is zero. `setup` is the decoder's
    configuration (AAC's AudioSpecificConfig, H.264's avcC), `plane` the plane of a video
    frame the array holds ("y", "u" or "v")."""

    format: str = "mp3"
    setup: str | None = None
    plane: str | None = None

    is_fixed_size = False

    @classmethod
    def from_dict(cls, data: dict) -> Self:
        c = data.get("configuration", {})
        if c.get("format") not in STREAM_FORMATS:
            raise ValueError(f"refkv.stream: unknown format {c.get('format')!r}")
        return cls(format=c["format"], setup=c.get("setup"), plane=c.get("plane"))

    def to_dict(self) -> dict:
        c = {"format": self.format}
        if self.setup is not None:
            c["setup"] = self.setup
        if self.plane is not None:
            c["plane"] = self.plane
        return {"name": "refkv.stream", "configuration": c}

    async def _decode_single(self, chunk_bytes: Buffer, chunk_spec: ArraySpec) -> NDBuffer:
        units, positions = stream_units(chunk_bytes.to_bytes())
        out = np.zeros(chunk_spec.shape, dtype=chunk_spec.dtype.to_native_dtype())
        for k, values in STREAM_FORMATS[self.format](self, units):
            if not 0 <= k < len(units):
                raise ValueError(f"refkv.stream: the decoder gave a frame of no packet ({k})")
            position = positions[k]
            if position < 0:
                continue
            if values.shape[1:] != out.shape[1:] or position + len(values) > len(out):
                raise ValueError(f"refkv.stream: a frame of shape {values.shape} at {position} does not fit the "
                                 f"chunk's {out.shape}")
            out[position:position + len(values)] = values
        return chunk_spec.prototype.nd_buffer.from_ndarray_like(out)

    async def _encode_single(self, chunk_array: NDBuffer, chunk_spec: ArraySpec) -> Buffer:
        raise NotImplementedError("refkv.stream decodes only")

    def compute_encoded_size(self, input_byte_length: int, chunk_spec: ArraySpec) -> int:
        raise NotImplementedError


def _varint(b: bytes, at: int) -> tuple[int, int]:
    """The protobuf varint at `at` of `b`, and where the next value starts."""
    v, shift = 0, 0
    while True:
        if at >= len(b) or shift > 63:
            raise ValueError("refkv.stream: a chunk's header ends inside a varint")
        c = b[at]
        v |= (c & 0x7F) << shift
        at += 1
        if c < 0x80:
            return v, at
        shift += 7


def stream_units(b: bytes) -> tuple[list[bytes], list[int]]:
    """The frames of a `refkv.stream` chunk and their output positions: a varint count `n`,
    then for each frame a varint size and a zigzag varint of its position less the previous
    frame's (the first's less -1), then the frames."""
    n, at = _varint(b, 0)
    sizes, positions, prev = [], [], -1
    for _ in range(n):
        size, at = _varint(b, at)
        z, at = _varint(b, at)
        prev += (z >> 1) ^ -(z & 1)
        sizes.append(size)
        positions.append(prev)
    if at + sum(sizes) != len(b):
        raise ValueError(f"refkv.stream: the frames' sizes sum to {sum(sizes)}, not the chunk's {len(b) - at}")
    units = []
    for size in sizes:
        units.append(b[at:at + size])
        at += size
    return units, positions


def _av_frames(codec: StreamCodec, units: list[bytes], name: str):
    """(frame index, decoded frame) per frame PyAV's decoder `name` outputs: each packet is
    tagged with its index as its timestamp, which the decoder passes on to the frame it
    decodes to, in whatever order it outputs them."""
    av = _av()
    ctx = av.CodecContext.create(name, "r")
    if codec.setup is not None:
        ctx.extradata = base64.b64decode(codec.setup)
    out = []
    for k, unit in enumerate(units):
        packet = av.Packet(unit)
        packet.pts = packet.dts = k
        out += ctx.decode(packet)
    out += ctx.decode(None)
    return [(f.pts, f) for f in out]


def _audio_frames(name: str, dtype):
    """The decoder of an audio format: (frame index, samples) per frame PyAV's decoder `name`
    outputs, [samples, channels] of `dtype`: float32, as ffmpeg's float MP3 and AAC decoders
    give (its fixed-point MP3 decoder carries rounding error from frame to frame, so a chunk
    decoded from its lead-in would differ from the stream by a unit here and there)."""

    def decode(codec: StreamCodec, units: list[bytes]):
        out = []
        for k, f in _av_frames(codec, units, name):
            a = f.to_ndarray()
            out.append((k, (a.T if f.format.is_planar else a.reshape(f.samples, -1)).astype(dtype, copy=False)))
        return out

    return decode


PLANES = {"y": 0, "u": 1, "v": 2}


def _h264_frames(codec: StreamCodec, units: list[bytes]):
    """(frame index, plane) per H.264 frame: PyAV's decoder, the plane `codec.plane` of the
    8-bit 4:2:0 picture, [1, height, width] uint8."""
    out = []
    for k, f in _av_frames(codec, units, "h264"):
        if f.format.name not in ("yuv420p", "yuvj420p"):
            raise ValueError(f"refkv.stream: an H.264 frame decodes to {f.format.name}, not 8-bit 4:2:0")
        p = f.planes[PLANES[codec.plane or "y"]]
        rows = np.frombuffer(p, dtype=np.uint8).reshape(p.height, p.line_size)[:, :p.width]
        out.append((k, rows[None]))
    return out


STREAM_FORMATS = {"mp3": _audio_frames("mp3float", np.float32), "aac": _audio_frames("aac", np.float32), "h264": _h264_frames}


register_codec("refkv.stream", StreamCodec)
