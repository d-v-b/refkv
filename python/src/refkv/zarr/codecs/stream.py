"""`refkv.stream`: a run of coded audio or video frames (conventions §10
<https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#10-coded-streams>), as zarrified MP3
files' chunks, MP4 tracks' and FLAC files' inner chunks are: a header giving each frame's size and
output position, then the frames, decoded by ffmpeg's decoders through PyAV (the `media` extra). It
has no encoder.
"""

from __future__ import annotations

import base64
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from types import ModuleType
from typing import TYPE_CHECKING, Self, cast, override

import numpy as np
import numpy.typing as npt
from zarr.abc.codec import ArrayBytesCodec
from zarr.core.array_spec import ArraySpec
from zarr.core.buffer import Buffer, NDBuffer
from zarr.core.buffer.core import NDArrayLike
from zarr.core.common import JSON
from zarr.registry import register_codec

from refkv._extras import missing
from refkv._narrow import mapping, string

if TYPE_CHECKING:
    from av.audio.frame import AudioFrame
    from av.frame import Frame
    from av.video.frame import VideoFrame


def _av() -> ModuleType:
    """PyAV, whose ffmpeg decoders the refkv.stream codec uses, imported when a chunk is first
    decoded."""
    try:
        import av  # noqa: PLC0415 (the media extra)
    except ImportError as e:
        raise missing("media", "Reading MP3, AAC, FLAC and H.264 chunks", e) from e
    return av


@dataclass(frozen=True)
class StreamCodec(ArrayBytesCodec):
    """`refkv.stream`: a chunk is a run of coded frames of an audio or video stream whose
    frames depend on earlier ones (conventions §10
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#10-coded-streams>): a header
    that gives each frame's size and output position, then the frames. The frames are decoded
    in one pass by ffmpeg's decoder of `format` (through PyAV: MP3, AAC, FLAC or H.264), each
    frame's output placed at its position in the chunk as the decoder gives it, a frame with
    position -1 (the chunk's lead-in) decoded and dropped at once, so that a chunk holds no
    more than its own values and the decoder's reference frames; what no frame fills is zero.
    `setup` is the decoder's configuration (AAC's AudioSpecificConfig, FLAC's STREAMINFO block,
    H.264's avcC), `plane`
    the plane of a video frame the array holds ("y", "u" or "v"). A video chunk's three planes
    are one chunk's bytes: decoding one keeps the other two (`SHARED`), so that reading a
    picture's Y, U and V decodes it once."""

    format: str = "mp3"
    setup: str | None = None
    plane: str | None = None

    is_fixed_size = False

    @classmethod
    @override
    def from_dict(cls, data: dict[str, JSON]) -> Self:
        c = mapping(data.get("configuration", {}), "configuration")
        if c.get("format") not in STREAM_FORMATS:
            raise ValueError(f"refkv.stream: unknown format {c.get('format')!r}")
        setup, plane = c.get("setup"), c.get("plane")
        return cls(format=string(c["format"]), setup=None if setup is None else string(setup, "setup"),
                   plane=None if plane is None else string(plane, "plane"))

    @override
    def to_dict(self) -> dict[str, JSON]:
        c = {"format": self.format}
        if self.setup is not None:
            c["setup"] = self.setup
        if self.plane is not None:
            c["plane"] = self.plane
        return {"name": "refkv.stream", "configuration": c}

    @override
    async def _decode_single(self, chunk_data: Buffer, chunk_spec: ArraySpec) -> NDBuffer:
        data = chunk_data.to_bytes()
        shape, dtype = tuple(chunk_spec.shape), chunk_spec.dtype.to_native_dtype()
        if self.format == "h264":
            plane = self.plane or "y"
            out = SHARED.take((self.setup, data, shape[0]), plane,
                              lambda: decode_chunk(self, data, plane, shape, dtype))
        else:
            out = decode_chunk(self, data, None, shape, dtype)[None]
        # an ndarray is NDArrayLike, though numpy's stubs do not match zarr's protocol
        return chunk_spec.prototype.nd_buffer.from_ndarray_like(cast("NDArrayLike", out))

    @override
    async def _encode_single(self, chunk_data: NDBuffer, chunk_spec: ArraySpec) -> Buffer:
        raise NotImplementedError("refkv.stream decodes only")

    def compute_encoded_size(self, input_byte_length: int, chunk_spec: ArraySpec) -> int:
        raise NotImplementedError


type Planes = dict[str | None, np.ndarray]
"""A chunk's or a frame's arrays, by plane (None for audio's one array)."""

type Decoder = Callable[[StreamCodec, list[bytes], list[int]], Iterator[tuple[int | None, Planes]]]
"""A format's decoder: (frame index, its arrays) per frame decoded from a chunk's frames."""


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
    sizes: list[int] = []
    positions: list[int] = []
    prev = -1
    for _ in range(n):
        size, at = _varint(b, at)
        z, at = _varint(b, at)
        prev += (z >> 1) ^ -(z & 1)
        sizes.append(size)
        positions.append(prev)
    if at + sum(sizes) != len(b):
        raise ValueError(f"refkv.stream: the frames' sizes sum to {sum(sizes)}, not the chunk's {len(b) - at}")
    units: list[bytes] = []
    for size in sizes:
        units.append(b[at:at + size])
        at += size
    return units, positions


def decode_chunk(codec: StreamCodec, data: bytes, plane: str | None, shape: tuple[int, ...],
                 dtype: np.dtype) -> Planes:
    """The arrays a chunk decodes to, by plane (None for audio's one array): the plane `plane`
    of shape `shape`, and for video the other planes of their pictures' sizes. Each frame is
    placed as the decoder gives it, and a lead-in frame's dropped."""
    units, positions = stream_units(data)
    out: Planes = {}
    for k, values in STREAM_FORMATS[codec.format](codec, units, positions):
        if k is None or not 0 <= k < len(units):
            raise ValueError(f"refkv.stream: the decoder gave a frame of no packet ({k})")
        position = positions[k]
        if position < 0:
            continue
        for name, v in values.items():
            o = out.get(name)
            if o is None:
                o = out[name] = np.zeros(shape if name == plane else (shape[0], *v.shape[1:]), dtype)
            if v.shape[1:] != o.shape[1:] or position + len(v) > len(o):
                raise ValueError(f"refkv.stream: a frame of shape {v.shape} at {position} does not fit the "
                                 f"chunk's {o.shape}")
            o[position:position + len(v)] = v
    if plane not in out:
        out[plane] = np.zeros(shape, dtype)
    return out


def _av_frames(codec: StreamCodec, units: list[bytes], positions: list[int],
               name: str) -> Iterator[tuple[int | None, Frame | None]]:
    """(frame index, decoded frame) per frame PyAV's decoder `name` outputs, as it outputs it:
    each packet is tagged with its index as its timestamp, which the decoder passes on to the
    frame it decodes to, in whatever order it outputs them. A frame of a packet at position -1
    (the lead-in) is given as None, its pictures not converted."""
    av = _av()
    ctx = av.CodecContext.create(name, "r")
    if codec.setup is not None:
        ctx.extradata = base64.b64decode(codec.setup)
    for k, unit in enumerate([*units, None]):
        if unit is None:
            frames = ctx.decode(None)
        else:
            packet = av.Packet(unit)
            packet.pts = packet.dts = k
            frames = ctx.decode(packet)
        for f in frames:
            lead = f.pts is not None and 0 <= f.pts < len(positions) and positions[f.pts] < 0
            yield f.pts, None if lead else f


def _audio_frames(name: str, dtype: npt.DTypeLike) -> Decoder:
    """The decoder of an audio format: (frame index, {None: samples}) per frame PyAV's decoder
    `name` outputs, [samples, channels] of `dtype`: float32, as ffmpeg's float MP3 and AAC
    decoders give (its fixed-point MP3 decoder carries rounding error from frame to frame, so a
    chunk decoded from its lead-in would differ from the stream by a unit here and there)."""

    def decode(codec: StreamCodec, units: list[bytes], positions: list[int]) -> Iterator[tuple[int | None, Planes]]:
        for k, frame in _av_frames(codec, units, positions, name):
            if frame is None:
                yield k, {}
                continue
            f = cast("AudioFrame", frame)  # an audio decoder's
            a = f.to_ndarray()
            yield k, {None: (a.T if f.format.is_planar else a.reshape(f.samples, -1)).astype(dtype, copy=False)}

    return decode


def _flac_frames(codec: StreamCodec, units: list[bytes], positions: list[int]) -> Iterator[tuple[int | None, Planes]]:
    """(frame index, {None: samples}) per FLAC frame PyAV's decoder outputs, [samples, channels]:
    the stream's integer samples. ffmpeg's decoder gives them in the high bits of 16 or 32 (an
    8-bit sample as a 16-bit one, a 24-bit one as a 32-bit one), so they are shifted back by
    the bits per sample of the setup's STREAMINFO block (`fLaC`, its 4-byte header, its body:
    RFC 9639 §8.2 <https://www.rfc-editor.org/rfc/rfc9639#section-8.2>)."""
    setup = base64.b64decode(codec.setup or "")
    if len(setup) != 42 or setup[:4] != b"fLaC":
        raise ValueError("refkv.stream: a FLAC setup is fLaC and a STREAMINFO block, 42 bytes")
    bits = ((setup[20] & 1) << 4 | setup[21] >> 4) + 1
    for k, frame in _av_frames(codec, units, positions, "flac"):
        if frame is None:
            yield k, {}
            continue
        f = cast("AudioFrame", frame)  # an audio decoder's
        a = f.to_ndarray()
        a = a.T if f.format.is_planar else a.reshape(f.samples, -1)
        yield k, {None: np.right_shift(a, 8 * a.dtype.itemsize - bits)}


PLANES = {"y": 0, "u": 1, "v": 2}


def _h264_frames(codec: StreamCodec, units: list[bytes], positions: list[int]) -> Iterator[tuple[int | None, Planes]]:
    """(frame index, {plane: [1, height, width] uint8}) per H.264 frame PyAV's decoder outputs:
    the Y, U and V planes of the 8-bit 4:2:0 picture."""
    for k, frame in _av_frames(codec, units, positions, "h264"):
        if frame is None:
            yield k, {}
            continue
        f = cast("VideoFrame", frame)  # a video decoder's
        if f.format.name not in ("yuv420p", "yuvj420p"):
            raise ValueError(f"refkv.stream: an H.264 frame decodes to {f.format.name}, not 8-bit 4:2:0")
        planes: Planes = {}
        for name, i in PLANES.items():
            p = f.planes[i]
            planes[name] = np.frombuffer(p, dtype=np.uint8).reshape(p.height, p.line_size)[None, :, :p.width]
        yield k, planes


class Shared:
    """The planes a video chunk's decoding gave that were not asked for yet, kept for when they
    are: by (setup, chunk bytes, pictures), at most `limit` bytes in all, the oldest dropped
    first. A plane is given once, then dropped. While one reader decodes a chunk, another
    asking for one of its planes waits for it."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.kept: OrderedDict[tuple[str | None, bytes, int], Planes] = OrderedDict()
        self.decoding: dict[tuple[str | None, bytes, int], threading.Event] = {}
        self.lock = threading.Lock()

    def held(self) -> int:
        return sum(a.nbytes for planes in self.kept.values() for a in planes.values())

    def take(self, key: tuple[str | None, bytes, int], plane: str, decode: Callable[[], Planes]) -> np.ndarray:
        """The plane `plane` of the chunk `key`: kept, or decoded by `decode` (which gives
        every plane)."""
        while True:
            with self.lock:
                planes = self.kept.get(key)
                if planes is not None and plane in planes:
                    out = planes.pop(plane)
                    if not planes:
                        del self.kept[key]
                    return out
                busy = self.decoding.get(key)
                if busy is None:
                    busy = self.decoding[key] = threading.Event()
                    break
            busy.wait()
        try:
            planes = decode()
        finally:
            with self.lock:
                self.decoding.pop(key).set()
        out = planes.pop(plane)
        with self.lock:
            if planes and sum(a.nbytes for a in planes.values()) <= self.limit:
                self.kept[key] = planes
                while self.held() > self.limit:
                    self.kept.popitem(last=False)
        return out


# 64 MiB: the two planes not asked for of at least one chunk of 32 MiB of luma (its Y and V when
# its U is read first, 40 MiB)
SHARED = Shared(1 << 26)


STREAM_FORMATS: dict[str, Decoder] = {
    "mp3": _audio_frames("mp3float", np.float32), "aac": _audio_frames("aac", np.float32), "flac": _flac_frames,
    "h264": _h264_frames}


register_codec("refkv.stream", StreamCodec)
