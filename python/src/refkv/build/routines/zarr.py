"""The Zarr convention's routine: `zarr_hierarchy`, the convention's hierarchy of a Zarr v2 or v3
store (`ZarrHierarchy`)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from refkv import _core
from refkv.build.values import Part, Ref, Value
from refkv.zarrify.common import Rejected

if TYPE_CHECKING:
    from refkv.build.parsed import Parsed

__all__ = ["ZarrHierarchy", "zarr_hierarchy"]


class ZarrHierarchy:
    """The Zarr v2 or v3 convention's hierarchy of a Zarr store, listed or zipped (spec/zarrify/zarr2.md
    §2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#2-nodes>, spec/zarrify/zarr3.md
    §2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#2-nodes>), as refkv's own output
    presents it but for its declarations and the objects under `refkv_source/`: each node presented,
    its source metadata, and each array's document and chunks, as values of the input.
    `zarr_hierarchy` makes it."""

    def __init__(self, parsed: Parsed, zarr: _core.Zarr) -> None:
        try:
            nodes, (_, entries, refs, _, _, _) = zarr.hierarchy()
        except _core.Rejected as e:
            raise Rejected(str(e)) from None
        self._nodes: dict[str, dict[str, Any]] = {n["path"]: n for n in json.loads(nodes)}
        self._documents: dict[str, bytes] = {k: v for k, v in entries if k.rpartition("/")[2] == "zarr.json"}
        objects = getattr(parsed, "_objects", None) or []

        def part(p: Any) -> Part:  # noqa: ANN401 (a part in `out_py`'s form)
            if p[0] == "object":  # a listed store's object
                return Ref(parsed, objects.index(p[1]), p[2], p[3])
            return Ref(parsed, 0, p[0], p[1])  # a range of the zip file

        # each array's chunks, by key relative to it: the nearest array the key is below (no node
        # is inside an array)
        arrays = sorted((p for p, n in self._nodes.items() if n["kind"] == "array"), key=len, reverse=True)
        self._chunks: dict[str, dict[str, bytes | Value]] = {p: {} for p in arrays}
        for key, parts in refs:
            for path in arrays:
                if not path or key.startswith(f"{path}/"):
                    relative = key.removeprefix(f"{path}/") if path else key
                    self._chunks[path][relative] = Value(tuple(part(p) for p in parts))
                    break

    def nodes(self) -> list[str]:
        """The paths of the nodes the convention presents, explicit ones in ascending order of their
        paths, then the implicit groups."""
        return list(self._nodes)

    def node_type(self, path: str) -> str | None:
        """`"group"` or `"array"` for the node at `path`, or None where there is none."""
        node = self._nodes.get(path)
        return None if node is None else node["kind"]

    def source_metadata(self, path: str) -> Any:  # noqa: ANN401 (JSON the convention specifies)
        """The source metadata of the node at `path` (spec/zarrify/zarr2.md §4
        <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#4-source-metadata>,
        spec/zarrify/zarr3.md §4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#4-source-metadata>):
        what refkv's declaration on the node holds, or None for a node with none."""
        node = self._nodes.get(path)
        return None if node is None or node["s"] is None else json.loads(node["s"])

    def document_bytes(self, key: str) -> bytes:
        """The Zarr document at `key` (`<path>/zarr.json`) as refkv writes it but for its
        declarations: a Zarr v2 node's with no attributes but an OME group's `ome`, a Zarr v3
        node's with the attributes it keeps in place. KeyError for a key of no node."""
        return self._documents[key]

    def document(self, key: str) -> dict[str, Any]:
        """`document_bytes`, parsed."""
        return json.loads(self._documents[key])  # type: ignore[no-any-return]

    def array_entries(self, path: str) -> tuple[dict[str, Any], dict[str, bytes | Value]]:
        """The array at `path`: its document (`document`), and its chunk entries by key relative to
        the array, each a value of the input. KeyError for a path that is no array."""
        if self.node_type(path) != "array":
            raise KeyError(path)
        prefix = f"{path}/" if path else ""
        return self.document(f"{prefix}zarr.json"), dict(self._chunks[path])


def zarr_hierarchy(parsed: Parsed) -> ZarrHierarchy:
    """The Zarr convention's hierarchy of a Zarr v2 store or of a Zarr v3 store, listed or zipped
    (spec/zarrify/zarr2.md §2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#2-nodes>,
    spec/zarrify/zarr3.md §2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#2-nodes>):
    the convention's decisions, which nodes are presented, each array's document and chunks and each
    node's source metadata, made from the parser's description (zarr2.md §7.3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#73-the-description>, zarr3.md
    §7.6 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#76-the-description>) by the
    function refkv's own projection calls. It reads nothing, and asks for no output of refkv's.
    ValueError for an input that is not a Zarr store; `refkv.zarrify.Rejected` where the convention
    rejects the input, as `zarrify` does."""
    zarr = parsed._zarr  # noqa: SLF001
    if zarr is None:
        raise ValueError(f"a Zarr routine reads a Zarr v2 or v3 store, not a {parsed.profile} input")
    return ZarrHierarchy(parsed, zarr)
