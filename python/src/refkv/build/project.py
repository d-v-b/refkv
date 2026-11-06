"""`project`: the built-in output of an input `read` read, as checked values (the design of
d-v-b/refkv#278, "The built-in output"). In this first step it is the output the run already
made, which `refkv.zarrify.layouts.Base` holds."""

from __future__ import annotations

import json
from typing import Any

from refkv import Concat, Range
from refkv.ir.store import StoreIrOutput
from refkv.zarrify.layouts import Base, base_of, chunk_coords

from .parsed import Parsed
from .values import Array, Binding, Literal, Part, Ref, Shared, Value, Written, Zeros, _issue

__all__ = ["Projected", "project"]


class Projected:
    """The built-in output of an input: `written`, its entries with their placement, the chunks of
    its arrays as values checked under the rule `"builtin"`; `array(path)`, an array of it; and
    `base`, the `refkv.zarrify.layouts.Base` it was made from (for `hierarchy.copy`, a node's
    attributes and source metadata)."""

    def __init__(self, parsed: Parsed, base: Base, read_at_open: frozenset[str]) -> None:
        self.parsed = parsed
        self.base = base
        self._sources = self._source_map()
        self._docs: dict[str, dict[str, Any]] = {}
        self.written = Written()
        for k in base.keys():  # noqa: SIM118 (a Base has keys(), not iteration)
            v = base._values[k]  # noqa: SLF001 (the base's own values, checksums dropped)
            if isinstance(v, bytes):
                self.written[k] = v
                self.written.at_open[k] = k in read_at_open
            else:
                self.written[k] = self._value(k, v)

    def _source_map(self) -> list[int | None]:
        """Each base source's index in the input's sources, or None for a data source."""
        parsed = self.parsed
        out: list[int | None] = []
        objects: list[str] = []
        if isinstance(parsed._out, StoreIrOutput):  # noqa: SLF001 (the run read made)
            objects, _, _ = parsed._out._numbered()  # noqa: SLF001
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

    def _doc(self, path: str) -> dict[str, Any] | None:
        if path not in self._docs:
            raw = self.base._values.get(f"{path}/zarr.json" if path else "zarr.json")  # noqa: SLF001
            self._docs[path] = json.loads(raw) if isinstance(raw, bytes) else {}
        doc = self._docs[path]
        return doc if doc.get("node_type") == "array" else None

    def _value(self, key: str, v: Range | Concat) -> Value:
        parts = tuple(self._part(r) for r in ((v,) if isinstance(v, Range) else v.parts))
        segments = key.split("/")
        for cut in range(len(segments) - 1, -1, -1):
            path = "/".join(segments[:cut])
            doc = self._doc(path)
            if doc is None:
                continue
            coords = chunk_coords(key[len(path) + 1:] if path else key, doc)
            return Value(parts, _issue("builtin", Binding.of(doc, coords)))
        return Value(parts)

    def array(self, path: str) -> Array:
        """The array at `path`, its chunks checked."""
        doc, chunks = self.base.array(path)
        grid = doc["chunk_grid"]["configuration"]
        prefix = f"{path}/" if path else ""
        return Array(list(doc["shape"]), doc["data_type"], list(grid.get("chunk_shape", grid.get("chunk_shapes"))),
                     list(doc["codecs"]),
                     {tuple(chunk_coords(k, doc)): self.written[f"{prefix}{k}"] for k in chunks},  # type: ignore[misc]
                     doc.get("fill_value"), doc.get("dimension_names"))


def project(parsed: Parsed) -> Projected:
    """The built-in projection of `parsed`'s input: what `zarrify` writes for it, as checked values."""
    base, store = base_of(parsed._out, parsed._fmt, parsed.policy, parsed.registry)  # noqa: SLF001
    return Projected(parsed, base, frozenset(store.read_at_open))
