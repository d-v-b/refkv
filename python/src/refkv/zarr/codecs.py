"""Zarr v3 codecs needed to read virtualized image files and stores, which
zarr-python does not provide.

`imagecodecs_jpeg2k`: each chunk is one JPEG 2000 codestream (as in TIFF
compression 33003/33005/34712 with planar-separate tiles).
`imagecodecs_jpeg`: each chunk is one complete JPEG stream (as virtualized
JPEG-in-TIFF tiles are, spec/virtualize.md §3.3
<https://github.com/d-v-b/refkv/blob/main/spec/virtualize/tiff.md#33-chunks>).
`imagecodecs_jpegxr`: each chunk is one JPEG XR file (ITU-T T.832, with its
container), as CZI JpgXr subblocks are (spec/virtualize/czi.md §3.1
<https://github.com/d-v-b/refkv/blob/main/spec/virtualize/czi.md#31-pixel-types-and-codecs>).
`imagecodecs_lzw`: a TIFF LZW stream (TIFF 6.0 §13, MSB-first with early change), as
QPTIFF tiles and strips are (spec/virtualize/qptiff.md §4.1
<https://github.com/d-v-b/refkv/blob/main/spec/virtualize/qptiff.md#41-data-types-and-codecs>),
decoded into the chunk's size, as TIFF readers decode a tile: a stream that runs on past it
(some scanners write the final code one bit too narrow) still gives the chunk.
`zlib`: a zlib stream (RFC 1950), as Neuroglancer names it (conventions §3
<https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#3-arrays>,
spec/virtualize/n5.md §3 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/n5.md#3-datasets>,
spec/virtualize/zarr2.md §3.1
<https://github.com/d-v-b/refkv/blob/main/spec/virtualize/zarr2.md#31-compressors>).
`n5_default`: an N5 default-mode block, header included (zarr-extensions
`codecs/n5_default`; spec/virtualize/n5.md).
`mp3`: a chunk of a virtualized MP3 file (spec/virtualize/mp3.md §4.1
<https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp3.md#41-the-array>): the samples to
keep, then MPEG Layer III frames, decoded by miniaudio.
`refkv.fits_tiles`: a chunk of a tile-compressed FITS image, a prefix of per-tile records then
the tiles' RICE_1, GZIP_1, GZIP_2 or NOCOMPRESS streams, dequantized as cfitsio does
(spec/virtualize/fits.md §3.3
<https://github.com/d-v-b/refkv/blob/main/spec/virtualize/fits.md#33-tile-compressed-images>),
decoded by the Rust core.

The image codecs decode only; their encoders exist so zarr can create the
array metadata. The mp3 codec has no encoder.
"""

from __future__ import annotations

import json
import struct
import zlib
from dataclasses import dataclass
from typing import Self

import numpy as np
import zarr
from zarr.abc.codec import ArrayBytesCodec, BytesBytesCodec, Codec
from zarr.codecs import BytesCodec, TransposeCodec
from zarr.core.array_spec import ArraySpec
from zarr.core.buffer import Buffer, NDBuffer
from zarr.registry import get_pipeline_class, register_codec

from refkv import _core
from refkv._extras import missing


def _miniaudio():
    """miniaudio, which the mp3 codec needs, imported when a chunk is first decoded."""
    try:
        import miniaudio
    except ImportError as e:
        raise missing("audio", "Reading MP3 chunks", e) from e
    return miniaudio


def _imagecodecs():
    """imagecodecs, which the image codecs need, imported when a chunk is first coded."""
    try:
        import imagecodecs
    except ImportError as e:
        raise missing("images", "Reading JPEG, JPEG 2000, JPEG XR and LZW chunks", e) from e
    return imagecodecs


@dataclass(frozen=True)
class Jpeg2kCodec(ArrayBytesCodec):
    is_fixed_size = False

    @classmethod
    def from_dict(cls, data: dict) -> Self:
        return cls()

    def to_dict(self) -> dict:
        return {"name": "imagecodecs_jpeg2k"}

    async def _decode_single(self, chunk_bytes: Buffer, chunk_spec: ArraySpec) -> NDBuffer:
        img = _imagecodecs().jpeg2k_decode(chunk_bytes.to_bytes())
        img = np.asarray(img, dtype=chunk_spec.dtype.to_native_dtype()).reshape(chunk_spec.shape)
        return chunk_spec.prototype.nd_buffer.from_ndarray_like(img)

    async def _encode_single(self, chunk_array: NDBuffer, chunk_spec: ArraySpec) -> Buffer:
        arr = np.squeeze(chunk_array.as_numpy_array())
        return chunk_spec.prototype.buffer.from_bytes(_imagecodecs().jpeg2k_encode(arr, level=0))

    def compute_encoded_size(self, input_byte_length: int, chunk_spec: ArraySpec) -> int:
        raise NotImplementedError


register_codec("imagecodecs_jpeg2k", Jpeg2kCodec)


@dataclass(frozen=True)
class JpegCodec(ArrayBytesCodec):
    is_fixed_size = False

    @classmethod
    def from_dict(cls, data: dict) -> Self:
        return cls()

    def to_dict(self) -> dict:
        return {"name": "imagecodecs_jpeg"}

    async def _decode_single(self, chunk_bytes: Buffer, chunk_spec: ArraySpec) -> NDBuffer:
        img = _imagecodecs().jpeg8_decode(chunk_bytes.to_bytes())
        img = np.asarray(img, dtype=chunk_spec.dtype.to_native_dtype()).reshape(chunk_spec.shape)
        return chunk_spec.prototype.nd_buffer.from_ndarray_like(img)

    async def _encode_single(self, chunk_array: NDBuffer, chunk_spec: ArraySpec) -> Buffer:
        arr = np.squeeze(chunk_array.as_numpy_array())
        return chunk_spec.prototype.buffer.from_bytes(_imagecodecs().jpeg8_encode(arr))

    def compute_encoded_size(self, input_byte_length: int, chunk_spec: ArraySpec) -> int:
        raise NotImplementedError


register_codec("imagecodecs_jpeg", JpegCodec)


@dataclass(frozen=True)
class JpegXrCodec(ArrayBytesCodec):
    """`imagecodecs_jpegxr`: one JPEG XR file per chunk, decoding to [y, x] or
    [y, x, c] (color samples in R, G, B (A) order), before `transpose`."""

    is_fixed_size = False

    @classmethod
    def from_dict(cls, data: dict) -> Self:
        return cls()

    def to_dict(self) -> dict:
        return {"name": "imagecodecs_jpegxr"}

    async def _decode_single(self, chunk_bytes: Buffer, chunk_spec: ArraySpec) -> NDBuffer:
        img = _imagecodecs().jpegxr_decode(chunk_bytes.to_bytes())
        img = np.asarray(img, dtype=chunk_spec.dtype.to_native_dtype()).reshape(chunk_spec.shape)
        return chunk_spec.prototype.nd_buffer.from_ndarray_like(img)

    async def _encode_single(self, chunk_array: NDBuffer, chunk_spec: ArraySpec) -> Buffer:
        arr = np.squeeze(chunk_array.as_numpy_array())
        return chunk_spec.prototype.buffer.from_bytes(_imagecodecs().jpegxr_encode(arr, level=1.0))

    def compute_encoded_size(self, input_byte_length: int, chunk_spec: ArraySpec) -> int:
        raise NotImplementedError


register_codec("imagecodecs_jpegxr", JpegXrCodec)


@dataclass(frozen=True)
class Mp3Codec(ArrayBytesCodec):
    """`mp3`: a chunk is a little-endian uint32, the samples per channel to keep, then MPEG
    Layer III frames: the frames before the chunk's that a decoder needs (its lead-in), then
    the chunk's own (spec/virtualize/mp3.md §4.1
    <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp3.md#41-the-array>, §17.2
    <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp3.md#172-chunks>). The frames
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


@dataclass(frozen=True)
class ZlibCodec(BytesBytesCodec):
    """`zlib`: each chunk is one zlib stream."""

    is_fixed_size = False
    level: int = 1

    @classmethod
    def from_dict(cls, data: dict) -> Self:
        return cls(**data.get("configuration", {}))

    def to_dict(self) -> dict:
        return {"name": "zlib", "configuration": {"level": self.level}}

    async def _decode_single(self, chunk_bytes: Buffer, chunk_spec: ArraySpec) -> Buffer:
        return chunk_spec.prototype.buffer.from_bytes(zlib.decompress(chunk_bytes.to_bytes()))

    async def _encode_single(self, chunk_bytes: Buffer, chunk_spec: ArraySpec) -> Buffer:
        return chunk_spec.prototype.buffer.from_bytes(zlib.compress(chunk_bytes.to_bytes(), self.level))

    def compute_encoded_size(self, input_byte_length: int, chunk_spec: ArraySpec) -> int:
        raise NotImplementedError


register_codec("zlib", ZlibCodec)


@dataclass(frozen=True)
class LzwCodec(BytesBytesCodec):
    """`imagecodecs_lzw`: each chunk is one TIFF LZW stream (compression 5)."""

    is_fixed_size = False

    @classmethod
    def from_dict(cls, data: dict) -> Self:
        return cls()

    def to_dict(self) -> dict:
        return {"name": "imagecodecs_lzw"}

    async def _decode_single(self, chunk_bytes: Buffer, chunk_spec: ArraySpec) -> Buffer:
        # Into a buffer of the chunk's size: the decoder stops when it is full, so a stream whose
        # final code is malformed (and reads as one more entry) still decodes to the chunk; a
        # stream that is short decodes to fewer bytes, which the bytes codec then refuses.
        size = chunk_spec.dtype.item_size
        for d in chunk_spec.shape:
            size *= d
        out = np.empty(size, dtype=np.uint8)
        decoded = _imagecodecs().lzw_decode(chunk_bytes.to_bytes(), out=out)
        return chunk_spec.prototype.buffer.from_bytes(np.ascontiguousarray(decoded).tobytes())

    async def _encode_single(self, chunk_bytes: Buffer, chunk_spec: ArraySpec) -> Buffer:
        return chunk_spec.prototype.buffer.from_bytes(_imagecodecs().lzw_encode(chunk_bytes.to_bytes()))

    def compute_encoded_size(self, input_byte_length: int, chunk_spec: ArraySpec) -> int:
        raise NotImplementedError


register_codec("imagecodecs_lzw", LzwCodec)


class N5BlockError(ValueError):
    """An N5 block that the n5_default codec cannot decode."""


@dataclass(frozen=True)
class N5DefaultCodec(ArrayBytesCodec):
    """`n5_default`: one N5 block, header included.

    Configuration `{"codecs": [transpose (full reversal), bytes (big-endian, or
    no endian for 1-byte types), optional one bytes-to-bytes codec]}`. Decoding
    parses the header (mode, number of dimensions, block size in N5 dimension
    order, which is the array's), requires default mode (0) and the array's
    number of dimensions, decodes the body with the inner codecs at the
    block's own size, and pads (with the fill value) or truncates it to the
    chunk's shape, so truncated edge blocks read correctly.
    """

    is_fixed_size = False
    codecs: tuple[Codec, ...] = ()

    def __init__(self, *, codecs) -> None:
        from zarr.core.metadata.v3 import parse_codecs

        cs = parse_codecs(codecs)
        if not 2 <= len(cs) <= 3:
            raise ValueError(f"n5_default needs 2 or 3 inner codecs, got {len(cs)}")
        t, b = cs[0], cs[1]
        if not isinstance(t, TransposeCodec) or tuple(t.order) != tuple(range(len(t.order) - 1, -1, -1)):
            raise ValueError("n5_default's first codec must be a full-reversal transpose")
        if not isinstance(b, BytesCodec):
            raise ValueError("n5_default's second codec must be a bytes codec")
        if len(cs) == 3 and not isinstance(cs[2], BytesBytesCodec):
            raise ValueError("n5_default's third codec must be bytes-to-bytes")
        object.__setattr__(self, "codecs", tuple(cs))

    @classmethod
    def from_dict(cls, data: dict) -> Self:
        return cls(codecs=data["configuration"]["codecs"])

    def to_dict(self) -> dict:
        return {"name": "n5_default", "configuration": {"codecs": [c.to_dict() for c in self.codecs]}}

    def evolve_from_array_spec(self, array_spec: ArraySpec) -> Self:
        return self

    def validate(self, *, shape, dtype, chunk_grid) -> None:
        if len(shape) != len(self.codecs[0].order):
            raise ValueError(f"n5_default: the array has {len(shape)} dimensions, the codec "
                             f"{len(self.codecs[0].order)}")
        # zarr-python gives a bytes codec without configuration the native endianness;
        # it only matters, and must be big, for types of more than one byte.
        endian = getattr(self.codecs[1].endian, "value", self.codecs[1].endian)
        if dtype.to_native_dtype().itemsize > 1 and endian != "big":
            raise ValueError("n5_default's bytes codec must be big-endian")

    def _pipeline(self):
        return get_pipeline_class().from_codecs(self.codecs)

    async def _decode_single(self, chunk_bytes: Buffer, chunk_spec: ArraySpec) -> NDBuffer:
        raw = chunk_bytes.to_bytes()
        if len(raw) < 4:
            raise N5BlockError("N5 block shorter than its header")
        mode, ndim = struct.unpack_from(">HH", raw)
        if mode != 0:
            raise N5BlockError(f"N5 block mode {mode} is not default (0)")
        if ndim != len(chunk_spec.shape):
            raise N5BlockError(f"N5 block has {ndim} dimensions, the array {len(chunk_spec.shape)}")
        if len(raw) < 4 + 4 * ndim:
            raise N5BlockError("N5 block shorter than its header")
        shape = struct.unpack_from(f">{ndim}I", raw, 4)
        body = chunk_spec.prototype.buffer.from_bytes(raw[4 + 4 * ndim :])
        spec = ArraySpec(tuple(shape), chunk_spec.dtype, chunk_spec.fill_value, chunk_spec.config,
                         chunk_spec.prototype)
        (decoded,) = await self._pipeline().decode([(body, spec)])
        block = decoded.as_numpy_array()
        if tuple(shape) == tuple(chunk_spec.shape):
            return decoded
        out = np.full(chunk_spec.shape, chunk_spec.fill_value, dtype=block.dtype)
        common = tuple(slice(0, min(a, b)) for a, b in zip(shape, chunk_spec.shape))
        out[common] = block[common]
        return chunk_spec.prototype.nd_buffer.from_ndarray_like(out)

    async def _encode_single(self, chunk_array: NDBuffer, chunk_spec: ArraySpec) -> Buffer:
        (body,) = await self._pipeline().encode([(chunk_array, chunk_spec)])
        header = struct.pack(f">HH{len(chunk_spec.shape)}I", 0, len(chunk_spec.shape), *chunk_spec.shape)
        return chunk_spec.prototype.buffer.from_bytes(header + body.to_bytes())

    def compute_encoded_size(self, input_byte_length: int, chunk_spec: ArraySpec) -> int:
        raise NotImplementedError


register_codec("n5_default", N5DefaultCodec)

# Other packages (zarr-n5) register an `n5_default` too; zarr-python then picks
# one arbitrarily unless the config names it. Name this one, unless the caller
# already chose.
if zarr.config.get("codecs.n5_default", None) is None:
    zarr.config.set({"codecs.n5_default": "refkv.zarr.codecs.N5DefaultCodec"})


@dataclass(frozen=True)
class FitsTilesCodec(ArrayBytesCodec):
    """The `refkv.fits_tiles` codec, decoding only: the Rust core decodes each chunk."""

    is_fixed_size = False
    config: str = "{}"  # the configuration as JSON (a dataclass field must be hashable)

    @classmethod
    def from_dict(cls, data: dict) -> Self:
        return cls(config=json.dumps(data.get("configuration", {})))

    def to_dict(self) -> dict:
        return {"name": "refkv.fits_tiles", "configuration": json.loads(self.config)}

    async def _decode_single(self, chunk_bytes: Buffer, chunk_spec: ArraySpec) -> NDBuffer:
        raw = _core.fits_tiles_decode(chunk_bytes.to_bytes(), self.config, [int(n) for n in chunk_spec.shape])
        dtype = chunk_spec.dtype.to_native_dtype().newbyteorder("<")
        arr = np.frombuffer(raw, dtype=dtype).reshape(chunk_spec.shape)
        return chunk_spec.prototype.nd_buffer.from_ndarray_like(arr)

    async def _encode_single(self, chunk_array: NDBuffer, chunk_spec: ArraySpec) -> Buffer:
        raise NotImplementedError("refkv.fits_tiles decodes only")

    def compute_encoded_size(self, input_byte_length: int, chunk_spec: ArraySpec) -> int:
        raise NotImplementedError


register_codec("refkv.fits_tiles", FitsTilesCodec)
