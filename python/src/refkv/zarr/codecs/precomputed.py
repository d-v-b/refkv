"""`refkv.precomputed`: a chunk or a whole shard of a Neuroglancer precomputed volume, after a
header that says where it lies (spec/zarrify/precomputed.md §6
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/precomputed.md#6-the-codec-refkvprecomputed>).

A shard is one Zarr chunk, read as Neuroglancer reads it: the shard index, then the minishard
indexes of the chunks asked for, then those chunks, so a reader of one chunk of a shard of
thousands makes at most three range reads. Lossy and packed encodings need imagecodecs (the
`images` extra), named when a chunk is decoded.
"""

from __future__ import annotations

import asyncio
import gzip
import struct
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from types import ModuleType
from typing import TYPE_CHECKING, Self, cast, override

import numpy as np
from zarr.abc.codec import ArrayBytesCodec, ArrayBytesCodecPartialDecodeMixin
from zarr.abc.store import OffsetByteRequest, RangeByteRequest
from zarr.core.array_spec import ArraySpec
from zarr.core.buffer import Buffer, NDBuffer
from zarr.core.buffer.core import NDArrayLike
from zarr.core.chunk_grids import ChunkGrid
from zarr.core.common import JSON
from zarr.core.indexing import get_indexer
from zarr.registry import register_codec

from refkv._extras import missing
from refkv._narrow import at, integer, mapping, sequence, string

if TYPE_CHECKING:
    from zarr.abc.store import ByteGetter
    from zarr.core.dtype.wrapper import TBaseDType, TBaseScalar, ZDType
    from zarr.core.indexing import SelectorTuple
    from zarr.core.metadata.v3 import ChunkGridMetadata

# the position header: x, y and z on the chunk grid, each a little-endian uint32
HEADER = 12
# a minishard index entry's three uint64 columns
ENTRY = 24
ENCODINGS = ("raw", "jpeg", "png", "compressed_segmentation")
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
GZIP_MAGIC = b"\x1f\x8b"
JPEG_MAGIC = b"\xff\xd8"
PNG_MAGIC = b"\x89PNG"
# ranges of a shard this near are read as one, and at most this long
COALESCE_GAP = 1 << 20
COALESCE_MAX = 1 << 26


class PrecomputedChunkError(ValueError):
    """A chunk or a shard that the refkv.precomputed codec cannot decode."""


def _imagecodecs() -> ModuleType:
    """imagecodecs, which decodes JPEG, PNG, brotli and zstd, imported when a chunk first needs it."""
    try:
        import imagecodecs  # noqa: PLC0415 (the images extra)
    except ImportError as e:
        raise missing("images", "Reading precomputed chunks", e) from e
    return imagecodecs


def _ints3(value: object, name: str) -> tuple[int, int, int]:
    items = sequence(value, name)
    if len(items) != 3:
        raise ValueError(f"refkv.precomputed: {name} is not 3 integers")
    return integer(items[0], name), integer(items[1], name), integer(items[2], name)


def _bits_of(n: int) -> int:
    """`ceil(log2(n))`, the bits of an id along an axis of `n` chunks."""
    return (n - 1).bit_length() if n > 1 else 0


def chunk_id(position: tuple[int, int, int], grid: tuple[int, int, int]) -> int:
    """The compressed Morton id of a chunk: the bits of x, y and z in turn from the lowest, an
    axis left out once its bits are used up."""
    bits = [_bits_of(n) for n in grid]
    out, j = 0, 0
    for i in range(max(bits, default=0)):
        for a in range(3):
            if i < bits[a]:
                out |= ((position[a] >> i) & 1) << j
                j += 1
    return out


def _decode_gzip(data: bytes, what: str) -> bytes:
    try:
        return gzip.decompress(data)
    except (OSError, EOFError) as e:
        raise PrecomputedChunkError(f"{what}: bad gzip stream: {e}") from e


def uncode(data: bytes, encoding: str, raw_lengths: tuple[int, ...], channels: int) -> bytes:
    """A chunk object's bytes without the content coding its object may have
    (spec/zarrify/precomputed.md §6.3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/precomputed.md#63-content-codings>),
    which the listing does not say and the bytes do: a raw chunk has one of its lengths, JPEG,
    PNG and compressed_segmentation begin as they do, gzip and zstd have their magic numbers, and
    what is left can only be brotli, which has none."""
    if encoding == "raw" and len(data) in raw_lengths:
        return data
    if encoding == "jpeg" and data.startswith(JPEG_MAGIC):
        return data
    if encoding == "png" and data.startswith(PNG_MAGIC):
        return data
    if encoding == "compressed_segmentation" and len(data) >= 4 and struct.unpack_from("<I", data)[0] == channels:
        return data
    if data.startswith(GZIP_MAGIC):
        return _decode_gzip(data, "a chunk object")
    if data.startswith(ZSTD_MAGIC):
        return _imagecodecs().zstd_decode(data)
    try:
        return _imagecodecs().brotli_decode(data)
    except ImportError:
        raise
    except Exception as e:
        raise PrecomputedChunkError(f"a {encoding} chunk object of {len(data)} bytes is not {encoding}, gzip, "
                                    f"zstd or brotli: {e}") from e


def _decode_segmentation(data: bytes, shape: tuple[int, int, int, int], block: tuple[int, int, int],
                         dtype: np.dtype) -> np.ndarray:
    """A compressed_segmentation stream (Neuroglancer's `sliceview/compressed_segmentation`)
    as an array of `shape`, (c, z, y, x)."""
    channels, sz, sy, sx = shape
    bx, by, bz = block
    width = dtype.itemsize // 4  # the words per label
    words = np.frombuffer(data, dtype="<u4")
    if len(words) < channels:
        raise PrecomputedChunkError("compressed_segmentation: shorter than its channel offsets")
    out = np.zeros(shape, dtype=dtype)
    grid = ((sx + bx - 1) // bx, (sy + by - 1) // by, (sz + bz - 1) // bz)
    for c in range(channels):
        base = int(words[c])
        for gz in range(grid[2]):
            for gy in range(grid[1]):
                for gx in range(grid[0]):
                    h = base + 2 * (gx + grid[0] * (gy + grid[1] * gz))
                    if h + 2 > len(words):
                        raise PrecomputedChunkError("compressed_segmentation: a block header past the stream's end")
                    w0, w1 = int(words[h]), int(words[h + 1])
                    table = base + (w0 & 0xFFFFFF)
                    bits = w0 >> 24
                    values = base + w1
                    if bits not in (0, 1, 2, 4, 8, 16, 32):
                        raise PrecomputedChunkError(f"compressed_segmentation: {bits} bits a voxel")
                    n = bx * by * bz
                    if bits == 0:
                        index = np.zeros(n, dtype=np.int64)
                    else:
                        nwords = (n * bits + 31) // 32
                        if values + nwords > len(words):
                            raise PrecomputedChunkError("compressed_segmentation: block values past the stream's end")
                        packed = words[values:values + nwords].astype(np.uint64)
                        pos = np.arange(n, dtype=np.uint64) * np.uint64(bits)
                        words_at = (pos // 32).astype(np.int64)
                        index = ((packed[words_at] >> (pos % 32)) & np.uint64((1 << bits) - 1)).astype(np.int64)
                    count = int(index.max()) + 1
                    if table + count * width > len(words):
                        raise PrecomputedChunkError("compressed_segmentation: a block's table past the stream's end")
                    labels = words[table:table + count * width]
                    if width == 2:
                        labels = labels.astype(np.uint64)
                        labels = labels[0::2] | (labels[1::2] << np.uint64(32))
                    block_values = labels[index].reshape(bz, by, bx)
                    z0, y0, x0 = gz * bz, gy * by, gx * bx
                    z1, y1, x1 = min(z0 + bz, sz), min(y0 + by, sy), min(x0 + bx, sx)
                    out[c, z0:z1, y0:y1, x0:x1] = block_values[:z1 - z0, :y1 - y0, :x1 - x0].astype(dtype)
    return out


@dataclass(frozen=True)
class PrecomputedCodec(ArrayBytesCodecPartialDecodeMixin, ArrayBytesCodec):
    """`refkv.precomputed`: a Zarr chunk's 12-byte header (its first chunk's x, y and z on the
    chunk grid) and one object of the volume: a chunk, or a shard whose box of chunks the Zarr
    chunk is. Configuration, in `info`'s own terms and x, y, z order: `encoding`, `size`,
    `chunk_size`, `compressed_segmentation_block_size` for that encoding, and `sharding`
    (`preshift_bits`, `minishard_bits`, `minishard_index_encoding`, `data_encoding`) for a shard.
    A chunk clipped by the volume's upper bound reads with the fill value past the bound."""

    is_fixed_size = False
    encoding: str = "raw"
    size: tuple[int, int, int] = (1, 1, 1)
    chunk_size: tuple[int, int, int] = (1, 1, 1)
    block: tuple[int, int, int] | None = None
    preshift_bits: int | None = None
    minishard_bits: int = 0
    minishard_index_encoding: str = "raw"
    data_encoding: str = "raw"

    @classmethod
    @override
    def from_dict(cls, data: dict[str, JSON]) -> Self:
        config = mapping(at(data, "configuration"), "configuration")
        encoding = string(config.get("encoding"), "encoding")
        if encoding not in ENCODINGS:
            raise ValueError(f"refkv.precomputed: the encoding {encoding!r} has no decoder")
        block = config.get("compressed_segmentation_block_size")
        sharding = config.get("sharding")
        kwargs: dict[str, object] = {}
        if sharding is not None:
            sh = mapping(sharding, "sharding")
            kwargs = {"preshift_bits": integer(sh["preshift_bits"], "preshift_bits"),
                      "minishard_bits": integer(sh["minishard_bits"], "minishard_bits"),
                      "minishard_index_encoding": string(sh["minishard_index_encoding"], "minishard_index_encoding"),
                      "data_encoding": string(sh["data_encoding"], "data_encoding")}
        return cls(encoding=encoding, size=_ints3(config.get("size"), "size"),
                   chunk_size=_ints3(config.get("chunk_size"), "chunk_size"),
                   block=None if block is None else _ints3(block, "compressed_segmentation_block_size"),
                   **kwargs)  # type: ignore[arg-type]

    @override
    def to_dict(self) -> dict[str, JSON]:
        config: dict[str, JSON] = {"encoding": self.encoding, "size": list(self.size),
                                   "chunk_size": list(self.chunk_size)}
        if self.block is not None:
            config["compressed_segmentation_block_size"] = list(self.block)
        if self.preshift_bits is not None:
            config["sharding"] = {"preshift_bits": self.preshift_bits, "minishard_bits": self.minishard_bits,
                                  "minishard_index_encoding": self.minishard_index_encoding,
                                  "data_encoding": self.data_encoding}
        return {"name": "refkv.precomputed", "configuration": config}

    @override
    def evolve_from_array_spec(self, array_spec: ArraySpec) -> Self:
        return self

    @override
    def validate(
        self,
        *,
        shape: tuple[int, ...],
        dtype: ZDType[TBaseDType, TBaseScalar],
        chunk_grid: ChunkGridMetadata,
    ) -> None:
        if len(shape) not in (3, 4):
            raise ValueError(f"refkv.precomputed: the array has {len(shape)} dimensions, not 3 or 4")

    @property
    def _grid(self) -> tuple[int, int, int]:
        size, chunk = self.size, self.chunk_size
        return cast("tuple[int, int, int]", tuple((s + c - 1) // c for s, c in zip(size, chunk, strict=True)))

    def _clipped(self, position: tuple[int, int, int]) -> tuple[int, int, int]:
        """A chunk's x, y and z sizes: its chunk size, less what is past the volume's end."""
        return cast("tuple[int, int, int]", tuple(
            min(c, s - p * c) for c, s, p in zip(self.chunk_size, self.size, position, strict=True)))

    def _decode_chunk(self, data: bytes, position: tuple[int, int, int], channels: int, dtype: np.dtype) -> np.ndarray:
        """One chunk as an array (c, z, y, x) of its clipped shape."""
        x, y, z = self._clipped(position)
        shape = (channels, z, y, x)
        cx, cy, cz = self.chunk_size
        item = dtype.itemsize
        lengths = (channels * z * y * x * item, channels * cz * cy * cx * item)
        data = uncode(data, self.encoding, lengths, channels)
        if self.encoding == "raw":
            if len(data) == lengths[0]:
                return np.frombuffer(data, dtype=dtype.newbyteorder("<")).reshape(shape).astype(dtype)
            if len(data) == lengths[1]:
                # a writer that pads its edge chunks: the chunk's own size, cut back
                full = np.frombuffer(data, dtype=dtype.newbyteorder("<")).reshape(channels, cz, cy, cx)
                return full[:, :z, :y, :x].astype(dtype)
            raise PrecomputedChunkError(f"a raw chunk at {position} is {len(data)} bytes, not {lengths[0]}")
        if self.encoding == "compressed_segmentation":
            if self.block is None:
                raise PrecomputedChunkError("compressed_segmentation without a block size")
            return _decode_segmentation(data, shape, self.block, dtype)
        codecs = _imagecodecs()
        image = codecs.jpeg8_decode(data) if self.encoding == "jpeg" else codecs.png_decode(data)
        # an image of width x and height y × z, slices one under the other; samples last
        pixels = image.reshape(*image.shape[:2], -1)
        if pixels.shape != (y * z, x, channels):
            raise PrecomputedChunkError(f"a {self.encoding} chunk at {position} is an image of {image.shape}, not "
                                        f"({y * z}, {x}, {channels})")
        return pixels.reshape(z, y, x, channels).transpose(3, 0, 1, 2).astype(dtype)

    async def _read_shard(self, read: Callable[[int, int], Awaitable[bytes]],
                          wanted: Iterable[tuple[int, int, int]]) -> dict[tuple[int, int, int], bytes]:
        """The bytes of the chunks `wanted` that the shard holds. `read(offset, length)` reads
        the shard's bytes, its header left out: `read` errors at the end of the shard are the
        store's, and a range the indexes give that does not fit is this codec's."""
        preshift, m = cast("int", self.preshift_bits), self.minishard_bits
        grid = self._grid
        ids = {chunk_id(p, grid): p for p in wanted}
        by_minishard: dict[int, dict[int, tuple[int, int, int]]] = {}
        for i, p in ids.items():
            by_minishard.setdefault((i >> preshift) & ((1 << m) - 1), {})[i] = p
        index_size = 16 << m
        table = await read(0, index_size)
        if len(table) != index_size:
            raise PrecomputedChunkError(f"a shard of {len(table)} bytes has no shard index of {index_size}")

        async def minishard(number: int, want: dict[int, tuple[int, int, int]]) -> dict[tuple[int, int, int], bytes]:
            start, end = struct.unpack_from("<QQ", table, number * 16)
            if start == end:
                return {}
            if end < start or end - start > 1 << 40:
                raise PrecomputedChunkError(f"minishard {number}'s index is at [{start}, {end})")
            raw = await read(index_size + start, end - start)
            if self.minishard_index_encoding == "gzip":
                raw = _decode_gzip(raw, f"minishard {number}'s index")
            if len(raw) % ENTRY:
                raise PrecomputedChunkError(f"minishard {number}'s index is {len(raw)} bytes, not a multiple of 24")
            n = len(raw) // ENTRY
            columns = np.frombuffer(raw, dtype="<u8").reshape(3, n)
            found_ids = np.cumsum(columns[0], dtype=np.uint64)
            # a chunk's offset is the end of the chunk before it plus its delta
            sizes = columns[2]
            offsets = np.cumsum(columns[1] + np.concatenate(([0], sizes[:-1])), dtype=np.uint64)
            hits = sorted((int(offsets[k]), int(sizes[k]), i)
                          for k, i in enumerate(found_ids.tolist()) if i in want)
            # one read for the chunks that lie near each other
            runs: list[list[tuple[int, int, int]]] = []
            for hit in hits:
                last = runs[-1] if runs else None
                if last and hit[0] - (last[-1][0] + last[-1][1]) <= COALESCE_GAP \
                        and hit[0] + hit[1] - last[0][0] <= COALESCE_MAX:
                    last.append(hit)
                else:
                    runs.append([hit])

            async def run(hits: list[tuple[int, int, int]]) -> list[tuple[tuple[int, int, int], bytes]]:
                start, stop = hits[0][0], hits[-1][0] + hits[-1][1]
                b = await read(index_size + start, stop - start)
                got = []
                for offset, length, i in hits:
                    c = b[offset - start:offset - start + length]
                    got.append((want[i], _decode_gzip(c, f"chunk {i}") if self.data_encoding == "gzip" else c))
                return got

            return {k: v for got in await asyncio.gather(*(run(r) for r in runs)) for k, v in got}

        parts = await asyncio.gather(*(minishard(n, w) for n, w in by_minishard.items()))
        return {k: v for part in parts for k, v in part.items()}

    @staticmethod
    def _origin(header: bytes) -> tuple[int, int, int]:
        if len(header) != HEADER:
            raise PrecomputedChunkError(f"a chunk of {len(header)} bytes has no header of {HEADER}")
        return cast("tuple[int, int, int]", struct.unpack("<III", header))

    def _inner_grid(self, shape: tuple[int, ...]) -> ChunkGrid:
        cx, cy, cz = self.chunk_size
        inner = (shape[0], cz, cy, cx) if len(shape) == 4 else (cz, cy, cx)
        return ChunkGrid.from_sizes(shape, inner)

    async def _decode_selection(self, origin: tuple[int, int, int], selection: SelectorTuple, spec: ArraySpec,
                                whole: Callable[[], Awaitable[bytes]],
                                read: Callable[[int, int], Awaitable[bytes]]) -> NDBuffer:
        """The array of `selection`, in a buffer of the selection's own shape: a shard's box can
        be gigabytes, of which a reader asks for one chunk."""
        dtype = spec.dtype.to_native_dtype()
        shape = tuple(spec.shape)
        channels = shape[0] if len(shape) == 4 else 1
        cx, cy, cz = self.chunk_size
        grid = self._grid
        indexer = get_indexer(selection, shape=shape, chunk_grid=self._inner_grid(shape))
        parts = list(indexer)
        position = {}
        for coords, *_ in parts:
            z, y, x = coords[-3:]
            p = (origin[0] + x, origin[1] + y, origin[2] + z)
            if p[0] < grid[0] and p[1] < grid[1] and p[2] < grid[2]:
                position[tuple(coords)] = p
        if self.preshift_bits is None:
            chunks = {origin: await whole()} if position else {}
        else:
            chunks = await self._read_shard(read, set(position.values()))
        out = np.full(indexer.shape, spec.fill_value, dtype=dtype)
        for coords, chunk_selection, out_selection, *_ in parts:
            p = position.get(tuple(coords))
            if p is None or p not in chunks:
                continue
            data = chunks[p]
            block = self._decode_chunk(data, p, channels, dtype)
            # the chunk's own shape, the fill value where the volume's end clips it
            full = np.full((channels, cz, cy, cx), spec.fill_value, dtype=dtype)
            _, bz, by, bx = block.shape
            full[:, :bz, :by, :bx] = block
            out[out_selection] = (full if len(shape) == 4 else full[0])[chunk_selection]
        if hasattr(indexer, "sel_shape"):
            out = out.reshape(indexer.sel_shape)
        return spec.prototype.nd_buffer.from_ndarray_like(cast("NDArrayLike", out))

    @override
    async def _decode_single(self, chunk_data: Buffer, chunk_spec: ArraySpec) -> NDBuffer:
        raw = chunk_data.to_bytes()
        origin = self._origin(raw[:HEADER])

        async def whole() -> bytes:
            return raw[HEADER:]

        async def read(offset: int, length: int) -> bytes:
            if HEADER + offset + length > len(raw):
                raise PrecomputedChunkError(f"a range [{offset}, {offset + length}) past a shard of "
                                            f"{len(raw) - HEADER} bytes")
            return raw[HEADER + offset:HEADER + offset + length]

        selection = tuple(slice(0, n) for n in chunk_spec.shape)
        return await self._decode_selection(origin, selection, chunk_spec, whole, read)

    @override
    async def _decode_partial_single(self, byte_getter: ByteGetter, selection: SelectorTuple,
                                     chunk_spec: ArraySpec) -> NDBuffer | None:
        prototype = chunk_spec.prototype
        header = await byte_getter.get(prototype, RangeByteRequest(0, HEADER))
        if header is None:
            return None

        async def whole() -> bytes:
            got = await byte_getter.get(prototype, OffsetByteRequest(HEADER))
            if got is None:
                raise PrecomputedChunkError("a chunk's object went missing while it was read")
            return got.to_bytes()

        async def read(offset: int, length: int) -> bytes:
            got = await byte_getter.get(prototype, RangeByteRequest(HEADER + offset, HEADER + offset + length))
            if got is None or len(got) != length:
                raise PrecomputedChunkError(f"a read of [{offset}, {offset + length}) of a shard came back short")
            return got.to_bytes()

        return await self._decode_selection(self._origin(header.to_bytes()), selection, chunk_spec, whole, read)

    @override
    async def _encode_single(self, chunk_data: NDBuffer, chunk_spec: ArraySpec) -> Buffer:
        raise NotImplementedError("refkv.precomputed is read-only")

    def compute_encoded_size(self, input_byte_length: int, chunk_spec: ArraySpec) -> int:
        raise NotImplementedError


register_codec("refkv.precomputed", PrecomputedCodec)
