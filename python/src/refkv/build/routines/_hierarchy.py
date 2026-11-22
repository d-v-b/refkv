"""What the routines of a convention that presents a hierarchy share: `_Hierarchy`, a hierarchy
made from what a `hierarchy()` routine of `_core` gives."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from refkv.build.values import Copy, Literal, Part, Ref, Shared, Value, Zeros

if TYPE_CHECKING:
    from refkv.build.parsed import Parsed

__all__: list[str] = []


class _Hierarchy:
    """A convention's hierarchy of an input, as refkv's own output presents it but for its
    declarations and its IR mirror: each node the convention presents, its attributes and source
    metadata, and each array's document and chunks, as values of the input; made from what a
    `hierarchy()` routine of `_core` gives (its nodes, its documents and chunks, and the edge chunks
    it copies)."""

    def __init__(self, parsed: Parsed, given: tuple[str, Any, list[tuple[str, int, int, int]]]) -> None:
        nodes, (_, entries, refs, data, _, _), copies = given
        self._nodes: dict[str, dict[str, Any]] = {n["path"]: n for n in json.loads(nodes)}
        self._documents = {k: v for k, v in entries if k.rpartition("/")[2] == "zarr.json"}
        objects = getattr(parsed, "_objects", None) or []

        def part(p: Any) -> Part:  # noqa: ANN401 (a part in `out_py`'s form)
            if isinstance(p, (bytes, bytearray)):
                return Literal(bytes(p))
            if p[0] == "object":
                return Ref(parsed, objects.index(p[1]), p[2], p[3])
            if len(p) == 3:
                held = data[p[0] - 1][p[1]:p[1] + p[2]]
                return Zeros(p[2]) if not held.strip(b"\0") else Shared(held)
            return Ref(parsed, 0, p[0], p[1])

        chunks: dict[str, bytes | Value] = {k: v for k, v in entries if k.rpartition("/")[2] != "zarr.json"}
        chunks.update({k: Value(tuple(part(p) for p in parts)) for k, parts in refs})
        for key, offset, length, pad in copies:
            copied = Copy(Ref(parsed, 0, offset, length))
            chunks[key] = Value((copied, Zeros(pad)) if pad else (copied,))
        # each array's chunks, by key relative to it, filed once: a chunk's key is its array's path,
        # then `c` (a node may be named `c` too, so the path is the one that names an array)
        arrays = {path for path, node in self._nodes.items() if "array" in node}
        self._chunks: dict[str, dict[str, bytes | Value]] = {path: {} for path in arrays}
        for key, value in chunks.items():
            segments = key.split("/")
            for i, segment in enumerate(segments):
                path = "/".join(segments[:i])
                if segment == "c" and path in arrays:
                    self._chunks[path]["/".join(segments[i:])] = value
                    break

    def nodes(self) -> list[str]:
        """The paths of the nodes the convention presents, the root's `""` first."""
        return list(self._nodes)

    def node_type(self, path: str) -> str | None:
        """`"group"` or `"array"` for the node at `path`, or None where there is none."""
        node = self._nodes.get(path)
        return None if node is None else "array" if "array" in node else "group"

    def attributes(self, path: str) -> dict[str, Any]:
        """The attributes of the node at `path` as the convention presents them, without refkv's
        declarations: a copy."""
        return json.loads(json.dumps(self._nodes[path]["attributes"]))

    def source_metadata(self, path: str) -> Any:  # noqa: ANN401 (JSON the convention specifies)
        """The source metadata of the node at `path`, as the convention specifies it: a copy, or
        None where there is no node."""
        node = self._nodes.get(path)
        return None if node is None else json.loads(json.dumps(node["own"]))

    def document(self, key: str) -> dict[str, Any]:
        """The Zarr document at `key` (`<path>/zarr.json`), parsed, its attributes the node's
        without refkv's declarations: a copy. KeyError for a key of no node."""
        path = key.removesuffix("zarr.json").removesuffix("/")
        doc: dict[str, Any] = json.loads(self._documents[key])
        doc["attributes"] = self.attributes(path)
        return doc

    def array_entries(self, path: str) -> tuple[dict[str, Any], dict[str, bytes | Value]]:
        """The array at `path`: its document (`document`), and its chunk entries by key relative
        to the array, each bytes or a value. An edge chunk refkv's own output copies, because its
        padded reference would not fit in a payload, is a value of a `Copy`, which the caller writes
        as bytes (`refkv.build.filled`). KeyError for a path that is no array."""
        if self.node_type(path) != "array":
            raise KeyError(path)
        prefix = f"{path}/" if path else ""
        return self.document(f"{prefix}zarr.json"), dict(self._chunks[path])
