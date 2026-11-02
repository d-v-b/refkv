"""Bounds: the keys a reference store answers for.

Inside its bounds, a key the store does not hold is *absent*: for a Zarr chunk, its fill
value. Outside them, the store has nothing to say about the key, and a combination of
stores (`refkv.compose`) asks another one. A store on its own reads the same with any
bounds; they matter only when stores are combined.

A bound is one of:

- `Key(k)`: the one key `k`;
- `KeyRange(start, stop)`: every key `k` with `start <= k < stop` (`stop=None`: no upper
  limit). Python compares strings by code point, which is the order of their UTF-8 bytes;
- `Prefix(path)`: every key under `path/`, so `Prefix("0/c/1")` does not take in
  `0/c/10/0`. `Prefix("")` is every key;
- `ChunkBox(array, start, stop)`: the chunks of the Zarr array at `array` whose
  coordinates `c` satisfy `start <= c < stop` on every axis, by the array's chunk key
  encoding (`encoding`, `separator`). Chunk keys do not sort in index order (`c/10`
  before `c/2`), so no key range can say "chunks 2 to 10"; a box can.

`derive(mapping)` gives the bounds a store has when none are stated: each Zarr array's
subtree, each group's own metadata keys, and every other key. See
design/proposals/store-operators.md
<https://github.com/d-v-b/refkv/blob/main/design/proposals/store-operators.md>.
"""

from __future__ import annotations

import json
import operator
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal, SupportsIndex, cast

from refkv.vzip.names import RESERVED_PREFIX

__all__ = ["Bound", "ChunkBox", "Key", "KeyRange", "Prefix", "chunk_key", "derive", "parse_chunk_key"]

_INDEX = re.compile(r"0|[1-9][0-9]*")


def _check_path(path: str, what: str) -> None:
    if not isinstance(path, str):
        raise TypeError(f"{what} is a string, not {type(path).__name__}")
    if path and (path.startswith("/") or path.endswith("/") or "//" in path):
        raise ValueError(f"{what} {path!r}: a node path has no leading, trailing or doubled '/'")


@dataclass(frozen=True, slots=True)
class Key:
    """The one key `key`."""

    key: str

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key:
            raise ValueError(f"a Key bound is a non-empty string, not {self.key!r}")

    def contains(self, key: str) -> bool:
        return key == self.key


@dataclass(frozen=True, slots=True)
class KeyRange:
    """Every key `k` with `start <= k < stop`; `stop=None` sets no upper limit."""

    start: str
    stop: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.start, str) or not (self.stop is None or isinstance(self.stop, str)):
            raise TypeError(f"a KeyRange's start and stop are strings, not {self.start!r}, {self.stop!r}")
        if self.stop is not None and self.stop < self.start:
            raise ValueError(f"KeyRange({self.start!r}, {self.stop!r}): stop is before start")

    def contains(self, key: str) -> bool:
        return self.start <= key and (self.stop is None or key < self.stop)


@dataclass(frozen=True, slots=True)
class Prefix:
    """Every key under `path/`; `Prefix("")` is every key."""

    path: str

    def __post_init__(self) -> None:
        _check_path(self.path, "Prefix path")

    def as_range(self) -> KeyRange:
        """The same keys as a `KeyRange`: `[path/, path0)`, since `0` follows `/`."""
        return KeyRange("") if not self.path else KeyRange(self.path + "/", self.path + "0")

    def contains(self, key: str) -> bool:
        return not self.path or key.startswith(self.path + "/")


@dataclass(frozen=True, slots=True)
class ChunkBox:
    """The chunks of the array at `array` whose coordinates `c` satisfy `start <= c < stop`.

    `encoding` and `separator` are the array's chunk key encoding: `"default"` (Zarr v3's,
    keys `c/0/1` with separator `/`, `c.0.1` with `.`) or `"v2"` (keys `0.1`, or `0/1`).
    The separator defaults to the encoding's own: `/` for `"default"`, `.` for `"v2"`.
    An array of no axes has one chunk, which `start=stop=()` takes in.
    """

    array: str
    start: tuple[int, ...]
    stop: tuple[int, ...]
    encoding: Literal["default", "v2"] = "default"
    separator: Literal["/", "."] = None  # type: ignore[assignment]  # the encoding's own

    def __post_init__(self) -> None:
        _check_path(self.array, "ChunkBox array")
        if self.separator is None:
            object.__setattr__(self, "separator", "." if self.encoding == "v2" else "/")
        object.__setattr__(self, "start", _indexes(self.start, "start"))
        object.__setattr__(self, "stop", _indexes(self.stop, "stop"))
        if self.encoding not in ("default", "v2"):
            raise ValueError(f"ChunkBox encoding is 'default' or 'v2', not {self.encoding!r}")
        if self.separator not in ("/", "."):
            raise ValueError(f"ChunkBox separator is '/' or '.', not {self.separator!r}")
        if len(self.start) != len(self.stop):
            raise ValueError(f"ChunkBox start {self.start} and stop {self.stop} have different lengths")
        for a, b in zip(self.start, self.stop, strict=True):
            if not 0 <= a <= b:
                raise ValueError(f"ChunkBox needs 0 <= start <= stop on every axis, not {self.start}, {self.stop}")

    def coordinates(self, key: str) -> tuple[int, ...] | None:
        """The coordinates of the chunk `key` names in this box's array and encoding, or None
        if it names none (whether or not they are inside the box)."""
        return parse_chunk_key(key, self.array, len(self.start), self.encoding, self.separator)

    def contains(self, key: str) -> bool:
        c = self.coordinates(key)
        return c is not None and all(a <= x < b for x, a, b in zip(c, self.start, self.stop, strict=True))


Bound = Key | KeyRange | Prefix | ChunkBox


def _indexes(values: Iterable[object], what: str) -> tuple[int, ...]:
    """`values` as a tuple of Python ints: integers of any integer type, not bools."""
    out = []
    for v in values:
        if isinstance(v, bool):
            raise TypeError(f"ChunkBox {what} coordinates are integers, not {v!r}")
        try:
            out.append(operator.index(cast("SupportsIndex", v)))
        except TypeError:
            raise TypeError(f"ChunkBox {what} coordinates are integers, not {v!r}") from None
    return tuple(out)


def valid_path(path: str) -> bool:
    """Whether `path` is a node path: "" (the root), or segments joined by `/`, none empty."""
    return not path or all(path.split("/"))


def chunk_key(array: str, coords: tuple[int, ...], encoding: str = "default", separator: str = "/") -> str:
    """The key of the chunk at `coords` of the array at `array`."""
    base = f"{array}/" if array else ""
    if encoding == "default":
        return base + "c" + "".join(separator + str(i) for i in coords)
    return base + (separator.join(map(str, coords)) if coords else "0")


def parse_chunk_key(key: str, array: str, rank: int, encoding: str = "default",
                    separator: str = "/") -> tuple[int, ...] | None:
    """The coordinates of the chunk `key` names, for an array at `array` with `rank` axes
    and the given chunk key encoding; None if `key` is no such chunk key. Indexes are
    canonical decimals (`0`, `12`; not `012`), as `chunk_key` writes them."""
    base = f"{array}/" if array else ""
    if not key.startswith(base):
        return None
    rest = key[len(base):]
    if encoding == "default":
        if not rest.startswith("c"):
            return None
        rest = rest[1:]
        if rank == 0:
            return () if rest == "" else None
        if not rest.startswith(separator):
            return None
        parts = rest[1:].split(separator)
    else:
        if rank == 0:
            return () if rest == "0" else None
        parts = rest.split(separator)
    if len(parts) != rank or not all(_INDEX.fullmatch(p) for p in parts):
        return None
    return tuple(int(p) for p in parts)


# The keys that mark a Zarr node, by name: v3's document, v2's array and group documents.
MARKERS = {"zarr.json": 3, ".zarray": 2, ".zgroup": 2}


def nodes(mapping: Mapping[str, object]) -> dict[str, str]:
    """{path: "array" | "group"} for every Zarr node `mapping` marks; the root's path is
    "". A v3 node's kind is its `zarr.json`'s `node_type`; a document that is not a bytes
    JSON object with a known `node_type` marks no node. Where v2 and v3 documents mark one
    path, the v3 document decides; a v2 `.zarray` wins over a `.zgroup`."""
    v3: dict[str, str] = {}
    v2: dict[str, str] = {}
    for key, value in mapping.items():
        if key.startswith(RESERVED_PREFIX):
            continue
        path, _, name = key.rpartition("/")
        if not valid_path(path) or key.startswith("/"):
            continue  # `a//zarr.json` and `/zarr.json` mark no node
        if name == "zarr.json":
            kind = node_type(value)
            if kind is not None:
                v3[path] = kind
        elif name == ".zarray":
            v2[path] = "array"
        elif name == ".zgroup":
            v2.setdefault(path, "group")
    # whatever the keys' order: a v3 document decides a path it marks, then a v2 array's
    return {**v2, **v3}


def node_type(value: object) -> str | None:
    """The `node_type` of a v3 `zarr.json` value, "array" or "group"; None for a value that
    is no such document."""
    if not isinstance(value, bytes):
        return None
    try:
        doc = json.loads(value)
    except (ValueError, RecursionError):
        return None
    kind = doc.get("node_type") if isinstance(doc, dict) else None
    return kind if kind in ("array", "group") else None


def derive(mapping: Mapping[str, object]) -> tuple[Bound, ...]:
    """The bounds of a store that states none: a `Prefix` over each array's subtree (every
    key, for an array at the root), each group's own metadata keys (`zarr.json`, or v2's
    `.zgroup` and `.zattrs`), and a `Key` for every other key it holds."""
    found = nodes(mapping)
    out: list[Bound] = []
    arrays = {path for path, kind in found.items() if kind == "array"}
    for path, kind in sorted(found.items()):
        base = f"{path}/" if path else ""
        if kind == "array":
            out.append(Prefix(path))
        else:
            out += [Key(base + name) for name in ("zarr.json", ".zgroup", ".zattrs")
                    if base + name in mapping]
    covered = {b.key for b in out if isinstance(b, Key)}
    out += [Key(k) for k in mapping
            if not k.startswith(RESERVED_PREFIX) and k not in covered and not _under(k, arrays)]
    return tuple(out)


def ancestors(key: str) -> list[str]:
    """The node paths above `key`, nearest first: `a/b/c` gives `a/b`, `a`, then ``."""
    out = []
    while key:
        key = key.rpartition("/")[0]
        out.append(key)
    return out


def _under(key: str, paths: set[str]) -> bool:
    return any(p in paths for p in ancestors(key))


def check(bounds: Iterable[object]) -> tuple[Bound, ...]:
    """`bounds` as a tuple, each checked to be a bound."""
    if isinstance(bounds, (str, bytes, Key, KeyRange, Prefix, ChunkBox)):
        raise TypeError("bounds are an iterable of bounds, not a single "
                        f"{type(bounds).__name__}")
    out: list[Bound] = []
    for b in bounds:
        if not isinstance(b, (Key, KeyRange, Prefix, ChunkBox)):
            raise TypeError(f"a bound is a Key, KeyRange, Prefix or ChunkBox, not {type(b).__name__}")
        out.append(b)
    return tuple(out)
