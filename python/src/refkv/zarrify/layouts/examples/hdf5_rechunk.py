"""`refkv.example.hdf5-rechunk`: an HDF5 file's hierarchy with its contiguous datasets re-tiled
into N-D chunks of a shape the options give (spec/layout.md
<https://github.com/d-v-b/refkv/blob/main/spec/layout.md>).

    from refkv.zarrify import zarrify
    from refkv.zarrify.layouts import Layout

    zarrify("scan.h5", layout=Layout("refkv.example.hdf5-rechunk", {
        "chunks": {"entry/data": [1, 256, 256]}, "default": [64, 64], "edge": "pad"}))

The HDF5 profile presents a contiguous dataset in slabs of at most 1 MiB, cut along one axis
(spec/zarrify/hdf5.md §3.2
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#32-chunks>), which suits a
reader of whole rows and not one of small N-D regions. This layout finds the dataset's data
element from the base's chunks (`base.elements`), reads it whole (`base.reference`), and makes
each tile a `concat` of `cut`s of it, one a run of bytes the tile holds contiguously, in C order.
The base array's data type, codecs (`bytes` alone: contiguous storage has no filters), fill value
and dimension names are kept.

Options:

- `chunks`: a dataset's path (canonical, with or without a leading `/`) to its chunk shape, one
  length an axis, `null` for the axis's whole length. A dataset named here that cannot be
  re-tiled is `Rejected`, naming why.
- `default`: the chunk shape of every other contiguous dataset of its rank, or `null` (the
  default): none. A dataset it cannot re-tile is left as the base has it.
- `edge`: how a short last chunk on an axis is written: `pad` (the default), a `regular` grid
  whose edge chunks are padded with ranges of zero bytes (`base.zeros`); or `rectilinear`, a
  `rectilinear` grid along the axes whose length the chunk does not divide, which reads no
  padding.

An axis's chunk length is at most the axis's length. A dataset cannot be re-tiled when it is not
contiguous (chunked or compact), its codecs are more than `bytes` (a variable-length string),
its bytes are not one data element of the IR (external storage), or a tile would break a rule
every reference keeps (`base.fits`: a payload of at most 65519 bytes, at most 1024 reads). A
chunk shape equal to the base's copies the array.

Everything else is the base's: each group's document (its attributes without refkv's
declarations), each other array a `copy` with its attributes, and the IR mirror,
`refkv_source/`, entry for entry. So `check` finds nothing: no `copy` (a re-tiled array's
references are not the base's), no `size` (each tile reads exactly its chunk's bytes, padding
included), no `refkv` (refkv's declarations are only the mirror's, whose subtree is the base's).
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, ClassVar

from refkv import Concat, Range
from refkv.zarrify import Rejected
from refkv.zarrify.layouts import MIRROR, Base, EntryValue, concat, cut
from refkv.zarrify.layouts.hierarchy import array, copy, group

Value = Range | Concat
Out = dict[str, EntryValue]  # entries being built
EDGES = ("pad", "rectilinear")
OPTIONS = frozenset(["chunks", "default", "edge"])


def _shape(given: object, where: str) -> list[int | None]:
    """A chunk shape of the options: a non-empty list of positive integers or nulls."""
    if not isinstance(given, list) or not given or not all(
            c is None or (type(c) is int and c > 0) for c in given):
        raise ValueError(f"{where}: a chunk shape is a non-empty list of positive integers or nulls, not {given!r}")
    return list(given)


class Hdf5Rechunk:
    """Contiguous HDF5 datasets re-tiled into chunks of a shape the options give."""

    name = "refkv.example.hdf5-rechunk"
    formats: ClassVar[dict[str, set[int]]] = {"hdf5": {0}}  # IMS refuses contiguous datasets

    def options(self, given: Mapping[str, Any]) -> dict[str, Any]:
        """The options in canonical form: paths without a leading `/`, every member present."""
        unknown = set(given) - OPTIONS
        if unknown:
            raise ValueError(f"unknown options {sorted(unknown)}: they are {', '.join(sorted(OPTIONS))}")
        chunks = given.get("chunks", {})
        if not isinstance(chunks, Mapping):  # a ValueError, as refkv makes it a LayoutUsageError
            raise ValueError(f"chunks maps a dataset's path to its chunk shape, not {chunks!r}")  # noqa: TRY004
        named: dict[str, list[int | None]] = {}
        for path, shape in chunks.items():
            canonical = path.removeprefix("/")
            if not canonical or canonical in named or canonical.split("/")[0] == MIRROR:
                raise ValueError(f"chunks: {path!r} is empty, repeated, or in the IR mirror")
            named[canonical] = _shape(shape, path)
        default = given.get("default")
        edge = given.get("edge", "pad")
        if edge not in EDGES:
            raise ValueError(f"edge is {' or '.join(EDGES)}, not {edge!r}")
        return {"chunks": dict(sorted(named.items())),
                "default": None if default is None else _shape(default, "default"), "edge": edge}

    def write(self, base: Base, options: Mapping[str, Any]) -> Out:
        named: dict[str, list[int | None]] = options["chunks"]
        for path in named:
            try:
                base.array(path)
            except KeyError as e:
                raise Rejected(f"chunks names {path!r}, which is no array of the base") from e
        out: Out = base.entries(f"{MIRROR}/")
        for path in base.nodes():
            key = f"{path}/zarr.json" if path else "zarr.json"
            if base.document(key)["node_type"] == "group":
                out[key] = group(base.attributes(path))
                continue
            want = named.get(path, options["default"])
            entries = None if want is None else _retile(base, path, want, options["edge"])
            if isinstance(entries, str):
                if path in named:
                    raise Rejected(f"{path!r} cannot be re-tiled: {entries}")
                entries = None
            out |= copy(base, path, attributes=base.attributes(path), to=path) if entries is None else entries
        return out


def _retile(base: Base, path: str, want: Sequence[int | None], edge: str) -> Out | str | None:
    """The array at `path` re-tiled into chunks of `want`, keys from the output's root; None when
    that is the base's chunk shape (copy it); or why it cannot be re-tiled."""
    doc, chunks = base.array(path)
    shape: list[int] = doc["shape"]
    if len(want) != len(shape):
        return f"a chunk shape of {len(want)} axes for an array of {len(shape)}"
    if [c.get("name") for c in doc["codecs"]] != ["bytes"]:
        return f"its codecs are {[c.get('name') for c in doc['codecs']]}, not bytes alone"
    meta = base.source_metadata(path)
    storage = meta.get("layout") if isinstance(meta, dict) else None
    if storage != "contiguous":
        return f"its storage is {storage}, not contiguous"
    tile = [max(1, min(n, n if c is None else c)) for n, c in zip(shape, want, strict=True)]
    grid = doc["chunk_grid"]
    if grid["name"] == "regular" and grid["configuration"]["chunk_shape"] == tile:
        return None
    whole = _data(base, path, chunks)
    if isinstance(whole, str):
        return whole
    counts = [-(-n // c) for n, c in zip(shape, tile, strict=True)]
    tiles: dict[tuple[int, ...], Value] = {}
    if whole is not None:  # else unallocated or empty: no chunks, the array reads as its fill value
        item, rest = divmod(whole.size, math.prod(shape))
        if rest or not item:
            return f"its data element's {whole.size} bytes are not its {math.prod(shape)} elements'"
        for coords in itertools.product(*(range(k) for k in counts)):
            value = _tile(base, whole, item, shape, tile, coords, pad=edge == "pad")
            why = base.fits(value)
            if why is not None:
                return f"chunk {list(coords)}: {why}"
            tiles[coords] = value
    lengths: list[int | list[int]] = [
        c if edge == "pad" or n % c == 0 else [c] * (k - 1) + [n - c * (k - 1)]
        for n, c, k in zip(shape, tile, counts, strict=True)]
    return array(shape, doc["data_type"], lengths, doc["codecs"], tiles, fill_value=doc["fill_value"],
                 dimension_names=doc.get("dimension_names"), attributes=base.attributes(path), to=path)


def _data(base: Base, path: str, chunks: Mapping[str, EntryValue]) -> Value | str | None:
    """The reference that reads the array's whole data element, found from its base chunks (None
    when it has none), or why there is not one: every part of a chunk reads that one element, or
    is the base's padding."""
    if not chunks:
        return None
    found: set[tuple[int, int | None]] = set()
    for key, items in base.elements(path).items():
        for item in items:
            if isinstance(item, Range):
                return f"chunk {key} reads bytes of a source no data element holds"
            if item is not None:
                found.add((item[0], item[1]))
    if len(found) != 1:
        return f"its chunks read {len(found)} data elements of the IR, not one"
    return base.reference(*found.pop())


def _runs(shape: Sequence[int], ext: Sequence[int], lo: Sequence[int], hi: Sequence[int],
          item: int) -> Iterator[tuple[int | None, int]]:
    """A tile's bytes in C order as runs `(offset, n)`: `offset` the start of `n` bytes of the
    data element, or None for `n` bytes of padding. The tile holds `lo` to `hi` of the array along
    each axis, in a chunk of `ext` (the tile's own shape, or the grid's for a padded one)."""
    rank = len(shape)
    stride = [item * math.prod(shape[j + 1:]) for j in range(rank)]
    block = [item * math.prod(ext[j + 1:]) for j in range(rank)]
    # whole[j]: the axes after j are held whole and unpadded, so one index along j is one run
    whole = [all(lo[k] == 0 and hi[k] == shape[k] == ext[k] for k in range(j + 1, rank)) for j in range(rank)]

    def walk(j: int, at: int) -> Iterator[tuple[int | None, int]]:
        if whole[j]:
            yield at + lo[j] * stride[j], (hi[j] - lo[j]) * stride[j]
        else:
            for i in range(lo[j], hi[j]):
                yield from walk(j + 1, at + i * stride[j])
        if ext[j] > hi[j] - lo[j]:
            yield None, (ext[j] - (hi[j] - lo[j])) * block[j]

    return walk(0, 0)


def _tile(base: Base, whole: Value, item: int, shape: Sequence[int], tile: Sequence[int], coords: Sequence[int],
          *, pad: bool) -> Value:
    """The value of the chunk at `coords`: a `cut` of `whole` for each run of the data element,
    and zero bytes for each run of padding (runs of it in a row as one), joined."""
    lo = [c * t for c, t in zip(coords, tile, strict=True)]
    hi = [min(a + t, n) for a, t, n in zip(lo, tile, shape, strict=True)]
    ext = list(tile) if pad else [b - a for a, b in zip(lo, hi, strict=True)]
    parts: list[Value] = []
    zeros = 0
    for at, n in _runs(shape, ext, lo, hi, item):
        if at is None:
            zeros += n
            continue
        if zeros:
            parts.append(base.zeros(zeros))
            zeros = 0
        parts.append(cut(whole, at, at + n))
    if zeros:
        parts.append(base.zeros(zeros))
    return concat(*parts, merge=True)
