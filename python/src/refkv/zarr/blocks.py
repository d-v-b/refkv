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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
from zarr.abc.store import Store
from zarr.core.buffer import default_buffer_prototype

from refkv import _sync
from refkv._narrow import JSON, at, integer, mapping, sequence, string
from refkv.pb import Range, parts
from refkv.reader import ReferenceReader
from refkv.zarr import ZarrStore

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


class BlocksStore(Store):
    """`inner`, with each array of `views` served on the chunk grid given with it."""

    def __init__(self, inner: ZarrStore, views: dict[str, tuple[Contiguous, tuple[int, ...]]],
                 row_bytes_threshold: int) -> None:
        raise NotImplementedError


def blocks(store: ZarrStore, shapes: Mapping[str, Sequence[int]], *,
           row_bytes_threshold: int = 4096) -> BlocksStore:
    """`store`, with each array of `shapes` (by path) served in chunks of its shape. Raises
    ValueError for an array that is not contiguous values (`contiguous`) or a shape of
    another rank than the array's or an axis below 1."""
    for path, shape in shapes.items():
        c = _sync.run(_at(store, path))  # that array alone, not the whole store
        if c is None:
            raise ValueError(f"{path!r} is not an array of contiguous values (refkv.zarr.contiguous)")
        if len(shape) != len(c.shape):
            raise ValueError(f"{path!r}: a block shape of rank {len(shape)} for an array of rank {len(c.shape)}")
        if min(shape, default=1) < 1:
            raise ValueError(f"{path!r}: a block shape {tuple(shape)} with an axis below 1")
    raise NotImplementedError(f"BlocksStore, at row_bytes_threshold={row_bytes_threshold}")
