"""`project`: the built-in output of an input `read` read, as values (the design of
d-v-b/refkv#278, "The built-in output"). `read` made no output: `project` has the kept run make
it, reading what that output alone needs, the first time it is asked for. It gives the output as
`refkv.zarrify.layouts.Base` holds it."""

from __future__ import annotations

from typing import Any

from refkv import Range, _sync
from refkv.ir.store import StoreIrOutput
from refkv.zarrify.layouts import Base, base_of, chunk_coords

from .parsed import Parsed
from .values import MODELED, Array, Literal, Part, Ref, Shared, Value, Written, Zeros

__all__ = ["Projected", "project", "project_async"]


class Projected:
    """The built-in output of an input: `written`, its entries with their placement, its
    references as values; `array(path)`, an array of it; and
    `base`, the `refkv.zarrify.layouts.Base` it was made from (for `hierarchy.copy`, a node's
    attributes and source metadata)."""

    def __init__(self, parsed: Parsed, base: Base, read_at_open: frozenset[str],
                 objects: list[str]) -> None:
        self.parsed = parsed
        self.base = base
        self._objects = objects  # a store output's objects, in its table's order
        self._sources = self._source_map()
        self.written = Written()
        for k in base.keys():  # noqa: SIM118 (a Base has keys(), not iteration)
            v = base._values[k]  # noqa: SLF001 (the base's own values, checksums dropped)
            if isinstance(v, bytes):
                self.written[k] = v
                self.written.at_open[k] = k in read_at_open
            else:
                self.written[k] = Value(tuple(self._part(r) for r in ((v,) if isinstance(v, Range) else v.parts)))

    def _source_map(self) -> list[int | None]:
        """Each base source's index in the input's sources, or None for a data source."""
        parsed = self.parsed
        out: list[int | None] = []
        objects = self._objects
        for j, s in enumerate(self.base.sources):
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
        data = self.base.sources[r.source].data or b""
        if not data.strip(b"\0"):
            return Zeros(r.length)
        if (r.offset, r.length) == (0, len(data)):
            return Shared(data)
        return Shared(data[r.offset:r.offset + r.length])

    def array(self, path: str) -> Array:
        """The array at `path`, its chunks the output's values, with its attributes (refkv's declarations among
        them, as the base's document holds them) and chunk key encoding."""
        doc, chunks = self.base.array(path)
        grid = doc["chunk_grid"]["configuration"]
        prefix = f"{path}/" if path else ""
        return Array(list(doc["shape"]), doc["data_type"], list(grid.get("chunk_shape", grid.get("chunk_shapes"))),
                     list(doc["codecs"]),
                     {coords: self.written[f"{prefix}{k}"] for k in chunks  # type: ignore[misc]
                      if (coords := _coords(k, doc)) is not None},
                     doc.get("fill_value"), doc.get("dimension_names"), dict(doc.get("attributes") or {}),
                     doc.get("chunk_key_encoding"), {k: v for k, v in doc.items() if k not in MODELED})


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
    out = await parsed._output_async()  # noqa: SLF001 (the run read kept)
    base, store = base_of(out, parsed._fmt, parsed.policy, parsed.registry,  # noqa: SLF001
                          checksums=parsed.checksums)
    objects = out._numbered()[0] if isinstance(out, StoreIrOutput) else []  # noqa: SLF001
    return Projected(parsed, base, frozenset(store.read_at_open), objects)


def project(parsed: Parsed) -> Projected:
    """`project_async`, the caller's thread waiting for it: call it from a thread, not from
    refkv's own event loop."""
    return _sync.run(project_async(parsed))
