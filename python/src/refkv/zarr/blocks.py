"""Arrays of contiguous values, served to zarr on a chunk grid of the reader's choosing.

A file whose pixels are one uncompressed block in row-major order (MRC, NIfTI, NRRD, NPY,
FITS, DICOM's native encoding) is zarrified as contiguous values, cut into slabs
(spec/conventions.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays>):
a chunk is whole planes, or whole rows, so that each is one range of the file. A viewer that
reads a small region of a large volume wants cubes instead. Since every element's place in the
file follows from its index, any grid can be read from the file without rewriting it.

- `contiguous(store)` finds the arrays of a `refkv.zarr.ZarrStore` whose chunks are that cut:
  each chunk one range of one source, at the offset that cut gives it, with a `bytes` codec
  alone.
- `blocks(store, {path: shape})` serves such arrays on a grid of `shape` chunks, as a
  read-only zarr store (`BlocksStore`); every other key is the store's own.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import math
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import override

import numpy as np
from zarr.abc.store import ByteRequest
from zarr.core.buffer import Buffer, BufferPrototype, default_buffer_prototype

from refkv import _sync
from refkv._narrow import JSON, at, integer, mapping, sequence, string
from refkv.pb import Concat, Range, parts
from refkv.reader import ReferenceReader
from refkv.zarr import ZarrStore, _slice

# Zarr v3's default chunk key encoding, written out or not: keys `<path>/c/<i>/<j>/...`
_DEFAULT_KEYS: tuple[JSON, ...] = ({"name": "default"}, {"name": "default", "configuration": {"separator": "/"}})


@dataclass(frozen=True)
class Contiguous:
    """An array whose elements a source holds contiguously in row-major order.

    - `source`: the index of that source in the reader's source table.
    - `offset`: the byte offset of element (0, ..., 0) in that source.
    - `shape`: the array's shape.
    - `itemsize`: the bytes of one element.
    - `codecs`: the array's codec chain, one `bytes` codec.
    - `metadata`: the array's zarr.json, as parsed.
    """

    source: int
    offset: int
    shape: tuple[int, ...]
    itemsize: int
    codecs: list[dict[str, JSON]]
    metadata: dict[str, JSON]


async def _ranges(reader: ReferenceReader, key: str) -> tuple[Range, ...] | None:
    """The ranges of `key`'s reference; None for a bytes value or a missing key."""
    ref = await reader.reference_async(key)
    return None if ref is None else parts(ref)


async def _array(store: ZarrStore, path: str, meta: Mapping[str, JSON]) -> Contiguous | None:
    """The array at `path`, of zarr.json `meta`, as contiguous values; None if it is not."""
    try:
        shape = tuple(integer(n) for n in sequence(at(meta, "shape")))
        chunks = tuple(integer(n) for n in sequence(at(meta, "chunk_grid", "configuration", "chunk_shape")))
        codecs = [dict(mapping(c)) for c in sequence(at(meta, "codecs"))]
        regular = at(meta, "chunk_grid", "name") == "regular"
        keys = at(meta, "chunk_key_encoding")
        itemsize = np.dtype(string(at(meta, "data_type"))).itemsize
    except (KeyError, IndexError, TypeError):
        return None
    r = len(shape)
    # the shape of the slab cut, 1 along the axes before one and whole along those after; the
    # formula below says where a chunk starts only for those
    cut = any(all(c == 1 for c in chunks[:a]) and chunks[a + 1:] == shape[a + 1:] for a in range(r))
    if (not regular or keys not in _DEFAULT_KEYS or len(codecs) != 1 or codecs[0].get("name") != "bytes"
            or r == 0 or len(chunks) != r or min(shape) < 1 or min(chunks) < 1 or not cut):
        return None
    grid = [-(-n // c) for n, c in zip(shape, chunks, strict=True)]
    strides = [math.prod(shape[j + 1:]) for j in range(r)]
    base = f"{path}/c/" if path else "c/"
    coords: list[tuple[int, ...]] = []
    names = [k async for k in store.list_prefix(base)]
    for name in names:
        try:
            coords.append(tuple(int(i) for i in name.removeprefix(base).split("/")))
        except ValueError:
            return None
    if (len(set(coords)) != math.prod(grid)
            or any(len(c) != r or not all(0 <= i < g for i, g in zip(c, grid, strict=True)) for c in coords)):
        return None  # a chunk missing (its fill value, which no source holds), or a stray key
    reader = store.reader
    sources = reader.sources
    zeros = {i for i, s in enumerate(sources) if s.data is not None and not any(s.data)}
    # ponytail: one reference lookup per chunk, O(chunks) at open; the IR
    # mirror's contiguous values form could say it in O(1). In batches, so as not to hold a
    # Task per chunk at once.
    found = [r for batch in itertools.batched(names, 4096)
             for r in await asyncio.gather(*(_ranges(reader, n) for n in batch))]
    whole = math.prod(chunks) * itemsize
    source = offset = None
    for c, ranges in zip(coords, found, strict=True):
        if not ranges:
            return None
        rest = list(ranges)
        pad = 0
        # the edge padding: zero literal bytes, or a range of a zero data source
        while len(rest) > 1 and (not any(rest[-1].data) if rest[-1].data is not None
                                 else rest[-1].source in zeros):
            pad += rest.pop().size
        start = [i * n for i, n in zip(c, chunks, strict=True)]
        inside = math.prod(min(n, s - x) for n, s, x in zip(chunks, shape, start, strict=True)) * itemsize
        index = sum(x * st for x, st in zip(start, strides, strict=True))
        (one, *more) = rest
        if (more or one.data is not None or one.crc32c is not None or one.length != inside
                or one.length + pad != whole or not 0 <= one.source < len(sources)
                or sources[one.source].data is not None):
            return None
        at_zero = one.offset - index * itemsize  # where element (0, ..., 0) is, by this chunk
        if source is None:
            source, offset = one.source, at_zero
        elif (one.source, at_zero) != (source, offset):
            return None
    if source is None or offset is None or offset < 0:
        return None
    return Contiguous(source, offset, shape, itemsize, codecs, dict(meta))


async def _at(store: ZarrStore, path: str) -> Contiguous | None:
    """The array at `path` as contiguous values; None if it is not, or no array is there."""
    doc = await store.get(f"{path}/zarr.json" if path else "zarr.json", default_buffer_prototype())
    try:
        meta = mapping(json.loads(doc.to_bytes())) if doc is not None else None
    except (ValueError, TypeError):
        return None
    if meta is None or meta.get("node_type") != "array":
        return None
    return await _array(store, path, meta)


async def contiguous_async(store: ZarrStore, prefix: str = "") -> dict[str, Contiguous]:
    """`contiguous`, as a coroutine."""
    found: dict[str, Contiguous] = {}
    for key in [k async for k in store.list_prefix(prefix)]:
        if key != "zarr.json" and not key.endswith("/zarr.json"):
            continue
        path = key.removesuffix("zarr.json").removesuffix("/")
        if (c := await _at(store, path)) is not None:
            found[path] = c
    return found


def contiguous(store: ZarrStore, prefix: str = "") -> dict[str, Contiguous]:
    """The arrays under `prefix` whose every chunk is one range of one source, at the offset of
    the slab cut of contiguous values (spec/conventions.md §7
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays>), by path.

    `prefix` is a string prefix of keys, as in `list_prefix`: `"0"` also takes `"01"`, and
    `"0/"` takes the arrays under `0` alone."""
    return _sync.run(contiguous_async(store, prefix))


class BlocksStore(ZarrStore):
    """`inner`, with each array of `views` (by path: the array, and its block shape) served on
    the regular chunk grid of that shape; a block past the array's end is zero-padded. Every
    other key is `inner`'s. `blocks(store, shapes)` makes one.

    A block is read from the array's source by the ranges of its rows: one per row when a row
    of the block has `row_bytes_threshold` bytes or more, and otherwise one per plane (the
    block's rows of one index on the other axes), from its first row to the end of its last.
    A plane's range holds the whole rows of the array between, so it reads up to `S[-1] / cx`
    times the bytes it needs, for a block `cx` wide of an array `S[-1]` wide, in one request
    instead of one per row. The reference store merges nearby ranges into one request, and
    reads the ranges of a block in values of at most its policy's `max_reads` ranges.
    """

    def __init__(self, inner: ZarrStore, views: dict[str, tuple[Contiguous, tuple[int, ...]]],
                 row_bytes_threshold: int) -> None:
        super().__init__(inner.reader)
        self.inner = inner
        self.views = views
        self.row_bytes_threshold = row_bytes_threshold

    @override
    def __eq__(self, value: object) -> bool:
        return (isinstance(value, BlocksStore)
                and (value.inner, value.views, value.row_bytes_threshold)
                == (self.inner, self.views, self.row_bytes_threshold))

    __hash__ = object.__hash__

    @override
    def __repr__(self) -> str:
        shapes = {path: shape for path, (_, shape) in self.views.items()}
        return f"refkv.zarr.blocks({self.inner!r}, {shapes!r}, row_bytes_threshold={self.row_bytes_threshold})"

    def _view(self, key: str) -> tuple[str, str] | None:
        """The viewed array `key` is under, and the rest of the key after the array's path."""
        for path in self.views:
            base = f"{path}/" if path else ""
            if key.startswith(base) and (key[len(base):] == "zarr.json" or key[len(base):].startswith("c/")):
                return path, key[len(base):]
        return None

    def _grid(self, path: str) -> tuple[int, ...]:
        c, shape = self.views[path]
        return tuple(-(-n // b) for n, b in zip(c.shape, shape, strict=True))

    def _coords(self, path: str, rest: str) -> tuple[int, ...] | None:
        """The block `rest` (`c/<i>/<j>/...`) names, None if it names none of the grid."""
        names = rest.removeprefix("c/").split("/")
        if not all(n.isdecimal() for n in names):
            return None
        coords = tuple(int(n) for n in names)
        grid = self._grid(path)
        ok = len(coords) == len(grid) and all(0 <= i < g for i, g in zip(coords, grid, strict=True))
        return coords if ok else None

    def _metadata(self, path: str) -> bytes:
        """The array's zarr.json, its chunk shape the block shape, written as refkv writes JSON."""
        c, shape = self.views[path]
        grid = {"name": "regular", "configuration": {"chunk_shape": list(shape)}}
        return json.dumps({**c.metadata, "chunk_grid": grid}, separators=(",", ":"), ensure_ascii=False).encode()

    async def _block(self, path: str, coords: tuple[int, ...]) -> bytes:
        """The bytes of block `coords` of the array at `path`, in C order, zero-padded."""
        c, shape = self.views[path]
        e = c.itemsize
        lead = (1,) * max(0, 2 - len(c.shape))  # a 1-D array as one row
        size, block, at_ = lead + c.shape, lead + shape, (0,) * len(lead) + coords
        lo = [i * b for i, b in zip(at_, block, strict=True)]
        hi = [min(x + b, n) for x, b, n in zip(lo, block, size, strict=True)]
        strides = [math.prod(size[j + 1:]) for j in range(len(size))]
        (y0, x0), (y1, x1) = lo[-2:], hi[-2:]
        rows, row, pitch = y1 - y0, (x1 - x0) * e, size[-1] * e
        wide = row >= self.row_bytes_threshold
        ranges: list[Range] = []
        for plane in itertools.product(*(range(a, b) for a, b in zip(lo[:-2], hi[:-2], strict=True))):
            first = c.offset + (sum(i * st for i, st in zip(plane, strides[:-2], strict=True)) + y0 * size[-1] + x0) * e
            if wide:
                ranges += [Range(c.source, first + y * pitch, row) for y in range(rows)]
            else:
                ranges.append(Range(c.source, first, (rows - 1) * pitch + row))
        await self._ensure_open()
        cap = self.reader.policy.max_reads
        batches = [Concat(tuple(ranges[i:i + cap])) for i in range(0, len(ranges), cap)]
        # pins, merging, the request cap and Stats, as for a reference the store holds
        resolve = self.reader._ref_bytes  # noqa: SLF001 (the reference store's resolution)
        read = await asyncio.gather(*(resolve(b, 0, b.size) for b in batches))
        data = np.frombuffer(read[0] if len(read) == 1 else b"".join(read), np.uint8)
        # each plane's rows, `row_stride` apart in what was read: a row, or the array's row
        planes = math.prod(b - a for a, b in zip(lo[:-2], hi[:-2], strict=True))
        plane_bytes, row_stride = (rows * row, row) if wide else (ranges[0].length, pitch)
        found = np.lib.stride_tricks.as_strided(data, (planes, rows, row), (plane_bytes, row_stride, 1),
                                                writeable=False)
        out = np.zeros((*block[:-1], block[-1] * e), np.uint8)
        out[(*(slice(0, b - a) for a, b in zip(lo[:-1], hi[:-1], strict=True)), slice(0, row))] = (
            found.reshape(*(b - a for a, b in zip(lo[:-1], hi[:-1], strict=True)), row))
        return out.tobytes()

    @override
    async def get(
        self, key: str, prototype: BufferPrototype, byte_range: ByteRequest | None = None
    ) -> Buffer | None:
        view = self._view(key)
        if view is None:
            return await self.inner.get(key, prototype, byte_range)
        path, rest = view
        if rest == "zarr.json":
            data = self._metadata(path)
        elif (coords := self._coords(path, rest)) is not None:
            data = await self._block(path, coords)
        else:
            return None
        return prototype.buffer.from_bytes(_slice(data, byte_range))

    @override
    async def exists(self, key: str) -> bool:
        view = self._view(key)
        if view is None:
            return await self.inner.exists(key)
        return view[1] == "zarr.json" or self._coords(*view) is not None

    @override
    async def getsize(self, key: str) -> int:
        view = self._view(key)
        if view is None:
            return await self.inner.getsize(key)
        path, rest = view
        if rest == "zarr.json":
            return len(self._metadata(path))
        if self._coords(path, rest) is None:
            raise FileNotFoundError(key)
        c, shape = self.views[path]
        return math.prod(shape) * c.itemsize

    def _keys(self, path: str) -> Iterator[str]:
        base = f"{path}/c/" if path else "c/"
        for coords in itertools.product(*(range(g) for g in self._grid(path))):
            yield base + "/".join(map(str, coords))

    @override
    async def list(self) -> AsyncIterator[str]:
        async for k in self.list_prefix(""):
            yield k

    @override
    async def list_prefix(self, prefix: str) -> AsyncIterator[str]:
        async for k in self.inner.list_prefix(prefix):
            view = self._view(k)
            if view is None or view[1] == "zarr.json":
                yield k
        for path in self.views:
            base = f"{path}/c/" if path else "c/"
            # ponytail: walks the array's whole grid for any prefix under `c/`; a sub-grid if that matters
            if base.startswith(prefix) or prefix.startswith(base):
                for k in self._keys(path):
                    if k.startswith(prefix):
                        yield k

    @override
    async def list_dir(self, prefix: str) -> AsyncIterator[str]:
        prefix = prefix.rstrip("/")
        for path in self.views:
            top = f"{path}/c" if path else "c"
            if prefix == top or prefix.startswith(top + "/"):
                grid = self._grid(path)
                given = prefix[len(top) + 1:].split("/") if prefix != top else []
                inside = all(g.isdecimal() and int(g) < n for g, n in zip(given, grid, strict=False))
                if inside and len(given) < len(grid):
                    for i in range(grid[len(given)]):
                        yield str(i)
                return
        async for k in self.inner.list_dir(prefix):
            yield k


def blocks(store: ZarrStore, shapes: Mapping[str, Sequence[int]], *,
           row_bytes_threshold: int = 4096) -> BlocksStore:
    """`store`, with each array of `shapes` (by path) served in chunks of its shape. Raises
    ValueError for an array that is not contiguous values (`contiguous`) or a shape of
    another rank than the array's or an axis below 1."""
    views: dict[str, tuple[Contiguous, tuple[int, ...]]] = {}
    for path, shape in shapes.items():
        c = _sync.run(_at(store, path))  # that array alone, not the whole store
        if c is None:
            raise ValueError(f"{path!r} is not an array of contiguous values (refkv.zarr.contiguous)")
        if len(shape) != len(c.shape):
            raise ValueError(f"{path!r}: a block shape of rank {len(shape)} for an array of rank {len(c.shape)}")
        if min(shape, default=1) < 1:
            raise ValueError(f"{path!r}: a block shape {tuple(shape)} with an axis below 1")
        views[path] = (c, tuple(shape))
    return BlocksStore(store, views, row_bytes_threshold)
