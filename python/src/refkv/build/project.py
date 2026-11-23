"""`project`: the built-in output of an input `read` read, as values (the design of
d-v-b/refkv#278, "The built-in output"). `read` made no output: `project` has the kept run make
it, reading what that output alone needs, the first time it is asked for."""

from __future__ import annotations

import asyncio
import bisect
import json
from typing import Any

from refkv import Concat, Range, _sync
from refkv.entries import MIRROR, chunk_coords, dump, undeclared
from refkv.errors import Rejected
from refkv.ir.store import StoreIrOutput
from refkv.refstore import ReferenceStore

from .parsed import Parsed
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

    def __init__(self, parsed: Parsed, store: ReferenceStore, objects: list[str], *, checksums: bool) -> None:
        self.parsed = parsed
        self._store_sources = tuple(store._sources)  # noqa: SLF001 (the output's table)
        self._objects = objects  # a store output's objects, in its table's order
        self._sources = self._source_map()
        self._checksums = checksums
        self._holders: dict[Value, list[str]] | None = None
        self._sorted = sorted(store.mapping)  # for an array's keys, found by prefix
        root = json.loads(store.mapping["zarr.json"])  # type: ignore[arg-type]
        self._root: dict[str, Any] = root["attributes"]["refkv_zarrified"]
        self.written = Written()
        for k in self._sorted:
            v = store.mapping[k]
            if isinstance(v, bytes):
                self.written[k] = v
                self.written.at_open[k] = k in store.read_at_open
            else:  # checksums are the seal's, not the output's
                self.written[k] = Value(tuple(self._part(r) for r in _ranges(v)))

    def _source_map(self) -> list[int | None]:
        """Each of the output's sources as the input's source of that index, or None for a data
        source."""
        parsed = self.parsed
        out: list[int | None] = []
        objects = self._objects
        for j, s in enumerate(self._store_sources):
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
        data = self._store_sources[r.source].data or b""
        if not data.strip(b"\0"):
            return Zeros(r.length)
        if (r.offset, r.length) == (0, len(data)):
            return Shared(data)
        return Shared(data[r.offset:r.offset + r.length])

    # ---- what the output is of

    @property
    def profile(self) -> str:
        """The profile the output declares, the input's."""
        return str(self._root["profile"])

    @property
    def version(self) -> int:
        """The version of the profile's convention the output follows."""
        return int(self._root["version"])

    @property
    def checksums(self) -> bool:
        """Whether the input was read for checksums, which changes how a profile joins a shard's
        ranges (spec/zarrify.md §2.1
        <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#21-virtual-shards>)."""
        return self._checksums

    @property
    def datasets(self) -> dict[str, str]:
        """The dataset layouts the output holds (spec/conventions.md §9
        <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#9-dataset-layouts>): each one's
        name and the path of its group; `{}` for a format that has none."""
        named = self._root.get("layouts")
        return {str(k): str(v) for k, v in named.items()} if isinstance(named, dict) else {}

    @property
    def mirror(self) -> str | None:
        """The path of the IR mirror's group, `refkv_source`, or None when the output has none."""
        return MIRROR if f"{MIRROR}/zarr.json" in self.written else None

    # ---- its hierarchy

    def keys(self) -> list[str]:
        """Every key of the output, sorted."""
        return list(self._sorted)

    def __contains__(self, key: object) -> bool:
        return key in self.written

    def _left_out(self, mirror: bool, datasets: bool) -> list[str]:
        out = [] if mirror or self.mirror is None else [MIRROR]
        return out if datasets else out + list(self.datasets.values())

    def nodes(self, *, mirror: bool = False, datasets: bool = False) -> list[str]:
        """The paths of the output's Zarr nodes, the root's `""` first; those of the IR mirror and
        of the dataset layouts only when asked for."""
        left = self._left_out(mirror, datasets)
        out = []
        for k in self._sorted:
            if not _is_document(k):
                continue
            path = k[: -len("zarr.json")].rstrip("/")
            if not any(path == p or path.startswith(f"{p}/") for p in left):
                out.append(path)
        return sorted(out)

    def node_type(self, path: str) -> str | None:
        """`"group"` or `"array"` for the node at `path`, or None where there is none."""
        value = self.written.get(f"{_prefix(path)}zarr.json")
        return json.loads(value).get("node_type") if isinstance(value, bytes) else None

    def document(self, key: str) -> dict[str, Any]:
        """The Zarr document at `key`, parsed: a copy the caller may change. KeyError for a key
        that holds none."""
        value = self.written.get(key)
        if not _is_document(key) or not isinstance(value, bytes):
            raise KeyError(f"no document {key!r} in the output (a document's key ends in zarr.json)")
        return json.loads(value)  # type: ignore[no-any-return]

    def _node(self, path: str) -> dict[str, Any]:
        try:
            return self.document(f"{_prefix(path)}zarr.json")
        except KeyError:
            raise KeyError(f"no node {path!r} in the output") from None

    def attributes(self, path: str) -> dict[str, Any]:
        """The attributes of the node at `path`, without refkv's declarations (`refkv_zarrified`,
        `refkv_provenance` and the declarations of refkv's conventions)."""
        return undeclared(self._node(path).get("attributes", {}))

    def source_metadata(self, path: str) -> Any:  # noqa: ANN401 (JSON the format's convention specifies)
        """The source metadata the node at `path` declares: the member of its `refkv_zarrified`
        named after the profile it names (the output's, or a dataset layout's own) (spec/conventions.md §2
        <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#2-attributes>), or None."""
        prop = self._node(path).get("attributes", {}).get("refkv_zarrified")
        if not isinstance(prop, dict):
            return None
        # a node of a dataset layout declares its own profile (`mrc-dataset`), and so names its member
        return prop.get(prop.get("profile", self.profile))

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
        doc = self._node(path)
        if doc.get("node_type") != "array":
            raise KeyError(f"{path!r} is a group of the output, not an array")
        prefix = _prefix(path)
        return doc, {k[len(prefix):]: self.written[k] for k in self._under(prefix) if not _is_document(k[len(prefix):])}

    def _under(self, prefix: str) -> list[str]:
        """The output's keys that start with `prefix`, sorted."""
        if not prefix:
            return list(self._sorted)
        out = []
        for k in self._sorted[bisect.bisect_left(self._sorted, prefix):]:
            if not k.startswith(prefix):
                break
            out.append(k)
        return out

    def entries(self, prefix: str = "", *, declarations: bool = True, mirror: bool = True,
                datasets: bool = True) -> Written:
        """The output's entries whose keys start with `prefix`, keys as the output holds them, as a
        `Written`: `out |= projected.entries()` keeps the whole of refkv's output with its
        placements, `projected.entries("refkv_source/")` the IR mirror. Without `declarations`,
        each `zarr.json` is written with its node's `attributes`, refkv's declarations left out;
        without `mirror` or `datasets`, the IR mirror's or the dataset layouts' entries are left
        out. An entry kept as it is keeps its placement; one rewritten follows refkv's default."""
        out = Written()
        left = tuple(f"{p}/" for p in self._left_out(mirror, datasets))
        for k in self._under(prefix):
            if left and k.startswith(left):
                continue
            v = self.written[k]
            if not isinstance(v, bytes):
                out[k] = v
                continue
            if not declarations and _is_document(k):
                doc = json.loads(v)
                doc["attributes"] = self.attributes(k[: -len("zarr.json")].rstrip("/"))
                v = dump(doc)
            out[k] = v
            if v == self.written[k]:
                out.at_open[k] = self.written.at_open[k]
        return out

    # ---- its values, and the IR

    def _held(self) -> dict[Value, list[str]]:
        """Each reference value of the output, with the paths of the arrays that hold it."""
        if self._holders is None:
            arrays = {p for p in self.nodes(mirror=True, datasets=True) if self.node_type(p) == "array"}
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
        parsed = self.parsed
        out: dict[str, tuple[tuple[int, int | None, int, int] | Ref | None, ...]] = {}
        for key, value in self.array_entries(path)[1].items():
            items: list[tuple[int, int | None, int, int] | Ref | None] = []
            for p in (() if isinstance(value, bytes) else value.parts):
                if not isinstance(p, Ref):  # a literal, or bytes of a data source
                    items.append(None)
                    continue
                at, end = p.offset, p.offset + p.length
                while True:
                    held = parsed.element_at(p.source, at)
                    if held is None:  # bytes no data element holds, up to the next one that does
                        starts, _ = parsed._space_index(p.source)  # noqa: SLF001
                        k = bisect.bisect_right(starts, at)
                        stop = min(end, starts[k]) if k < len(starts) else end
                        items.append(Ref(parsed, p.source, at, max(stop - at, 0)))
                    else:
                        i, member = held
                        span = parsed.span(i, member)
                        stop = min(end, span.offset + span.length)
                        items.append((i, member, at - span.offset, stop - span.offset))
                    if stop >= end:
                        break
                    at = stop
            out[key] = tuple(items)
        return out


def _ranges(value: Range | Concat) -> tuple[Range, ...]:
    return (value,) if isinstance(value, Range) else value.parts


def _is_document(key: str) -> bool:
    return key.rpartition("/")[2] == "zarr.json"


def _prefix(path: str) -> str:
    return f"{path}/" if path else ""


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
        parsed._own_error = e  # noqa: SLF001
        raise

    def projected() -> Projected:
        store = out.to_store(policy=parsed.policy, registry=parsed.registry,
                             info={"format": parsed._fmt, **out.summary})  # noqa: SLF001
        objects = out._numbered()[0] if isinstance(out, StoreIrOutput) else []  # noqa: SLF001
        return Projected(parsed, store, objects, checksums=parsed.checksums)

    # the store and the values, on a thread: both grow with the chunks
    return await asyncio.to_thread(projected)


def project(parsed: Parsed) -> Projected:
    """`project_async`, the caller's thread waiting for it: call it from a thread, not from
    refkv's own event loop."""
    return _sync.run(project_async(parsed))
