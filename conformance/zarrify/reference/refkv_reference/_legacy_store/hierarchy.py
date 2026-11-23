"""A store's hierarchy, as the Zarr v2 and N5 store layouts find it (spec/zarrify/zarr2.md,
spec/zarrify/n5.md): its directories, nodes and chunk objects. The Rust core zarrifies those
stores (rust/refkv-ir/src/zarr2.rs, n5.rs); the frozen reference profiles
(conformance/zarrify/reference) use these.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence


def directories_of(keys: Iterable[str]) -> set[str]:
    """Every directory of the store (§1.4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>): the root and each
    proper key prefix before a `/`."""
    out = {""}
    for k in keys:
        i = k.find("/")
        while i >= 0:
            out.add(k[:i])
            i = k.find("/", i + 1)
    return out


def depth(path: str) -> int:
    return 0 if path == "" else path.count("/") + 1


def join(parent: str, name: str) -> str:
    return name if parent == "" else f"{parent}/{name}"


def parent_of(path: str) -> str:
    return path.rpartition("/")[0]


class PathTrie[V]:
    """Paths (the root `""` included), each with a value, as a tree of their
    segments: finding a key's nearest ancestor among them takes time linear in
    the key's length, without rebuilding any prefix."""

    __slots__ = ("children", "value")

    def __init__(self) -> None:
        self.children: dict[str, PathTrie[V]] = {}
        self.value: V | None = None

    def add(self, path: str, value: V) -> None:
        node = self
        if path:
            for s in path.split("/"):
                child = node.children.get(s)
                if child is None:
                    child = node.children[s] = PathTrie()
                node = child
        node.value = value

    def nearest(self, segs: list[str]) -> tuple[V, int] | None:
        """(value, j) of the nearest proper ancestor of the key with segments
        `segs` that has a value, at the path `segs[:j]`; or None."""
        node = self
        best: tuple[V, int] | None = None
        if self.value is not None and segs != [""]:
            best = (self.value, 0)
        for j in range(len(segs) - 1):
            child = node.children.get(segs[j])
            if child is None:
                break
            node = child
            if node.value is not None:
                best = (node.value, j + 1)
        return best


def implicit_groups(nodes: Iterable[str]) -> set[str]:
    """The proper ancestors of `nodes` that are not nodes. Each walk stops at a
    path already seen, so the time is that of the paths it adds."""
    nodes = set(nodes)
    implicit: set[str] = set()
    for path in nodes:
        i = len(path)
        while i > 0:
            i = path.rfind("/", 0, i)
            p = path[:i] if i > 0 else ""
            if p in nodes or p in implicit:
                break
            implicit.add(p)
            i = max(i, 0)
    return implicit


def classify(candidates: dict[str, str]) -> tuple[dict[str, str], set[str]]:
    """Nodes from candidates {path: kind}, kind 'array' or 'group', classified
    from the root down (spec/zarrify/n5.md §2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/n5.md#2-nodes>,
    spec/zarrify/zarr2.md §2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#2-nodes>): candidates inside
    an array are dropped. Returns ({path: kind}, implicit groups)."""
    nodes: dict[str, str] = {}
    arrays = PathTrie[bool]()
    for path in sorted(candidates, key=lambda p: (depth(p), p)):
        if arrays.nearest(path.split("/")) is not None:
            continue
        nodes[path] = candidates[path]
        if candidates[path] == "array":
            arrays.add(path, True)
    return nodes, implicit_groups(nodes)


def canonical_index(s: str, limit: int) -> bool:
    """Is `s` a decimal integer without leading zeros below `limit` (spec/zarrify/n5.md §3.1
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/n5.md#31-chunks>,
    spec/zarrify/zarr2.md §3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#3-arrays>)?"""
    return (s.isascii() and s.isdecimal() and (s == "0" or s[0] != "0")
            and len(s) <= 17 and int(s) < limit)


def grid(shape: Sequence[int], chunks: Sequence[int]) -> list[int]:
    return [-(-s // c) for s, c in zip(shape, chunks, strict=True)]


def find_chunks(objects: dict[str, int], arrays: dict[str, Callable[[str], bool]]) -> list[tuple[str, int]]:
    """The chunk objects (§1.4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>): for each object,
    the array that is its nearest ancestor decides with `is_chunk(rest)` whether the rest of the key
    is one of its chunk keys. Returns [(key, size)] in key order."""
    trie = PathTrie[Callable[[str], bool]]()
    for path, test in arrays.items():
        trie.add(path, test)
    out = []
    for key, size in objects.items():
        segs = key.split("/")
        found = trie.nearest(segs)
        if found is not None and found[0]("/".join(segs[found[1]:])):
            out.append((key, size))
    out.sort()
    return out
