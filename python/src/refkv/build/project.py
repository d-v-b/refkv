"""`project`: the built-in output of an input `read` read, as values (the design of
d-v-b/refkv#278, "The built-in output"). `read` made no output: `project` has the kept run make
it, reading what that output alone needs, the first time it is asked for. It gives the output as
`refkv.zarrify.layouts._Base` holds it."""

from __future__ import annotations

import asyncio
from typing import Any

from refkv import Range, _sync
from refkv.ir.store import StoreIrOutput
from refkv.zarrify.common import Rejected
from refkv.zarrify.layouts import _Base, _base_of, chunk_coords

from .parsed import Parsed
from .plugin import mark_own
from .values import MODELED, Array, Literal, Part, Ref, Shared, Value, Written, Zeros

__all__ = ["Projected", "project", "project_async"]


class Projected:
    """The built-in output of an input, as values: what refkv's own convention writes for it, for
    a caller or a plugin layout that wants to keep, copy or change some of it.

    - `written`: every entry, with its placement, its references as values of the input's sources;
    - `keys()`, `nodes()`, `node_type(path)`, `document(key)`, `attributes(path)` and
      `source_metadata(path)`: its hierarchy;
    - `array(path)`, an array of it as an `Array`, and `array_entries(path)`, its document and its
      chunk entries;
    - `entries(prefix)`: the entries below a prefix, as a `Written` that keeps their placements;
    - `elements(path)`: for each chunk of an array, the IR elements its bytes lie in.

    It is one projection of the input among others: nothing in `refkv.build` needs it but the
    callers that want refkv's own output."""

    def __init__(self, parsed: Parsed, base: _Base, read_at_open: frozenset[str],
                 objects: list[str]) -> None:
        self.parsed = parsed
        self._base = base
        self._objects = objects  # a store output's objects, in its table's order
        self._sources = self._source_map()
        self._holders: dict[Value, list[str]] | None = None
        self.written = Written()
        for k in base.keys():  # noqa: SIM118 (a _Base has keys(), not iteration)
            v = base._values[k]  # noqa: SLF001 (the base's own values, checksums dropped)
            if isinstance(v, bytes):
                self.written[k] = v
                self.written.at_open[k] = k in read_at_open
            else:
                self.written[k] = Value(tuple(self._part(r) for r in ((v,) if isinstance(v, Range) else v.parts)))

    def _source_map(self) -> list[int | None]:
        """Each of the output's sources as the input's source of that index, or None for a data
        source."""
        parsed = self.parsed
        out: list[int | None] = []
        objects = self._objects
        for j, s in enumerate(self._base.sources):
            if s.url is None:
                out.append(None)
            elif objects:
                out.append(parsed._objects.index(objects[j]))  # noqa: SLF001
            else:
                out.append(0)
        return out

    def _part(self, r: Range) -> Part:
        if r.data is not None:
            return Literal(r.data)
        j = self._sources[r.source]
        if j is not None:
            return Ref(self.parsed, j, r.offset, r.length)
        data = self._base.sources[r.source].data or b""
        if not data.strip(b"\0"):
            return Zeros(r.length)
        if (r.offset, r.length) == (0, len(data)):
            return Shared(data)
        return Shared(data[r.offset:r.offset + r.length])

    # ---- what the output is of

    @property
    def profile(self) -> str:
        """The profile the output declares, the input's."""
        return self._base.profile

    @property
    def version(self) -> int:
        """The version of the profile's convention the output follows."""
        return self._base.version

    @property
    def checksums(self) -> bool:
        """Whether the input was read for checksums, which changes how a profile joins a shard's
        ranges (spec/zarrify.md §2.1
        <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#21-virtual-shards>)."""
        return self._base.checksums

    @property
    def datasets(self) -> dict[str, str]:
        """The dataset layouts the output holds: each one's name and the path of its group."""
        return self._base.datasets

    @property
    def mirror(self) -> str | None:
        """The path of the IR mirror's group, `refkv_source`, or None when the output has none."""
        return self._base.mirror

    # ---- its hierarchy

    def keys(self) -> list[str]:
        """Every key of the output, sorted."""
        return self._base.keys()

    def __contains__(self, key: object) -> bool:
        return key in self._base

    def nodes(self, *, mirror: bool = False, datasets: bool = False) -> list[str]:
        """The paths of the output's Zarr nodes, the root's `""` first; those of the IR mirror and
        of the dataset layouts only when asked for."""
        return self._base.nodes(mirror=mirror, datasets=datasets)

    def node_type(self, path: str) -> str | None:
        """`"group"` or `"array"` for the node at `path`, or None where there is none."""
        return self._base.node_type(path)

    def document(self, key: str) -> dict[str, Any]:
        """The Zarr document at `key`, parsed: a copy the caller may change."""
        return self._base.document(key)

    def attributes(self, path: str) -> dict[str, Any]:
        """The attributes of the node at `path`, without refkv's declarations (`refkv_zarrified`,
        `refkv_provenance` and the declarations of refkv's conventions)."""
        return self._base.attributes(path)

    def source_metadata(self, path: str) -> Any:  # noqa: ANN401 (JSON the format's convention specifies)
        """The source metadata the node at `path` declares: the member of its `refkv_zarrified`
        named after the profile (spec/conventions.md §2
        <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#2-attributes>), or None."""
        return self._base.source_metadata(path)

    def array(self, path: str) -> Array:
        """The array at `path`, its chunks the output's values, with its attributes (refkv's declarations among
        them, as the output's document holds them) and chunk key encoding."""
        doc, chunks = self.array_entries(path)
        grid = doc["chunk_grid"]["configuration"]
        return Array(list(doc["shape"]), doc["data_type"], list(grid.get("chunk_shape", grid.get("chunk_shapes"))),
                     list(doc["codecs"]),
                     {coords: v for k, v in chunks.items()  # type: ignore[misc]
                      if (coords := _coords(k, doc)) is not None},
                     doc.get("fill_value"), doc.get("dimension_names"), dict(doc.get("attributes") or {}),
                     doc.get("chunk_key_encoding"), {k: v for k, v in doc.items() if k not in MODELED})

    def array_entries(self, path: str) -> tuple[dict[str, Any], dict[str, bytes | Value]]:
        """The array at `path`: its document, parsed (a copy), and its chunk entries by key relative
        to the array, each bytes or a value. KeyError for a path that is no array of the output."""
        doc, chunks = self._base.array(path)
        prefix = f"{path}/" if path else ""
        return doc, {k: self.written[f"{prefix}{k}"] for k in chunks}

    def entries(self, prefix: str = "", *, declarations: bool = True, mirror: bool = True,
                datasets: bool = True) -> Written:
        """The output's entries whose keys start with `prefix`, keys as the output holds them, as a
        `Written`: `out |= projected.entries()` keeps the whole of refkv's output with its
        placements, `projected.entries("refkv_source/")` the IR mirror. Without `declarations`,
        each `zarr.json` is written with its node's `attributes`, refkv's declarations left out;
        without `mirror` or `datasets`, the IR mirror's or the dataset layouts' entries are left
        out. An entry kept as it is keeps its placement; one rewritten follows refkv's default."""
        out = Written()
        for k, v in self._base.entries(prefix, declarations=declarations, mirror=mirror, datasets=datasets).items():
            mine = self.written[k]
            if not isinstance(v, bytes):
                out[k] = mine
                continue
            out[k] = v
            if v == mine:
                out.at_open[k] = self.written.at_open[k]
        return out

    # ---- its values, and the IR

    def _held(self) -> dict[Value, list[str]]:
        """Each reference value of the output, with the paths of the arrays that hold it."""
        if self._holders is None:
            arrays = {p for p in self._base.nodes(mirror=True, datasets=True) if self._base.node_type(p) == "array"}
            holders: dict[Value, list[str]] = {}
            for k, v in self.written.items():
                if isinstance(v, bytes):
                    continue
                paths = holders.setdefault(v, [])
                holder = k.rpartition("/")[0]
                while holder and holder not in arrays:
                    holder = holder.rpartition("/")[0]
                if holder in arrays and holder not in paths:  # a reference outside every array is no chunk
                    paths.append(holder)
            self._holders = holders
        return self._holders

    def holds(self, value: object) -> bool:
        """Whether `value` is one of the output's reference values."""
        try:
            return isinstance(value, Value) and value in self._held()
        except TypeError:  # a value of unhashable parts
            return False

    def arrays_holding(self, value: Value) -> list[str]:
        """The paths of the output's arrays that hold the reference `value` as a chunk."""
        return self._held().get(value, [])

    def elements(self, path: str) -> dict[str, tuple[tuple[int, int | None, int, int] | Ref | None, ...]]:
        """For every chunk of the output's array at `path` (keys relative to the array), the bytes its
        value reads, in order, by the IR element that holds them: (element, member, start, stop), the
        bytes of it read, half open, offsets within the element's (or member's) own bytes, its
        `span` (not its `reference`, which a recipe may change), one item for each
        element a part runs across (a chunk of strips the profile joined is several); None for a
        literal part or a part of a data source; a `Ref`, of the part, for bytes of a source
        that no data element holds (an HDF5 dataset's external storage)."""
        return {k: tuple(self._part(x) if isinstance(x, Range) else x for x in items)  # type: ignore[misc]
                for k, items in self._base.elements(path).items()}


def _coords(key: str, doc: dict[str, Any]) -> tuple[int, ...] | None:
    """The chunk coordinates of `key` in the array `doc`, or None for a key under the array that is
    no chunk of it (a root array's IR mirror)."""
    try:
        return tuple(chunk_coords(key, doc))
    except (ValueError, KeyError, TypeError):
        return None


async def project_async(parsed: Parsed) -> Projected:
    """The built-in projection of `parsed`'s input: what `zarrify` writes for it, as values. The
    first call makes the output, reading what it alone needs through what `read` opened, so
    `parsed` must not be closed yet; later calls build on the output it made."""
    try:
        out = await parsed._output_async()  # noqa: SLF001 (the run read kept)
    except Rejected:  # the projection's refusal of the input
        raise
    except Exception as e:  # a read the built-in output makes, or its failure: refkv's own, whoever asked
        mark_own(e)
        raise

    def projected() -> Projected:
        base, store = _base_of(out, parsed._fmt, parsed.policy, parsed.registry,  # noqa: SLF001
                              checksums=parsed.checksums)
        objects = out._numbered()[0] if isinstance(out, StoreIrOutput) else []  # noqa: SLF001
        return Projected(parsed, base, frozenset(store.read_at_open), objects)

    # the store and the values, on a thread: both grow with the chunks
    return await asyncio.to_thread(projected)


def project(parsed: Parsed) -> Projected:
    """`project_async`, the caller's thread waiting for it: call it from a thread, not from
    refkv's own event loop."""
    return _sync.run(project_async(parsed))
