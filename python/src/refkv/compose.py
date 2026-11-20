"""Operators that combine reference stores into new ones, by their bounds.

Each store answers for the keys of its bounds (`refkv.bounds`, `ReferenceStore.bounds`):
inside them a key it does not hold is absent, outside them it has nothing to say. The
operators build new stores and change none:

- `overlay(*stores)`: each key from the last store whose bounds contain it, a value or
  absence, as a later layer wins in TensorStore's `stack` and `kvstack`. Associative and
  idempotent in what it reads, with the empty store as identity; not commutative. A
  nested call checks its intermediate result, so it may refuse what one flat call, in
  which a later store hides the offending value, accepts.
- `union(*stores)`: `overlay`, where stores whose bounds share a key must answer it the
  same (equal values, or both absent). On disjoint bounds it is also commutative: the
  operator for a stack or a mosaic.
- `map_keys(store, f)`: each key renamed to `f(key)`, or dropped where it gives None.
- `mount(store, path)`: the store under `path/`, its bounds moved with it.
- `shift_chunks(store, array, offset)`: the chunks of one array alone, moved by `offset`
  (after `new_axes` new leading axes), with their bounds.
- `normalize(store)`: keys no valid Zarr node owns dropped, missing ancestor groups added.

A combination's sources are each store's, relative URLs resolved against that store's
base URL, equal ones merged, and the unused dropped; references are renumbered to match,
the most used source first. A store's keys outside its own bounds are never carried into
a combination. Its hidden keys (under the binding's reserved prefix) are carried only when
a carried value reads one through a key source, renamed by its content if another carried
one of that name differs. Consolidated metadata (a `zarr.json`'s `consolidated_metadata`,
a v2 `.zmetadata`) lists the nodes of one store, so `overlay`, `union`, `map_keys` and
`normalize` (of a store with Zarr nodes) drop it, keeping every other number and string
of the document as written. Every result's `info` is empty unless `info=` is given.

A stack of same-layout arrays along a new axis, say the planes of a z-stack, is a header
holding the stack's metadata, bounded to its own keys, and the planes' chunks:

    header = ReferenceStore({"zarr.json": root, "0/zarr.json": stacked},
                            bounds=[Key("zarr.json"), Key("0/zarr.json")])
    planes = [shift_chunks(s, "0", (i, 0, 0), new_axes=1) for i, s in enumerate(stores)]
    stack = union(header, *planes)

design/proposals/store-operators.md
<https://github.com/d-v-b/refkv/blob/main/design/proposals/store-operators.md> is the
design, with its reasons.
"""

from __future__ import annotations

import bisect
import dataclasses
import hashlib
import heapq
import json
import operator
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from typing import TYPE_CHECKING, SupportsIndex, TypedDict, Unpack, cast

from refkv.bounds import (
    Bound,
    ChunkBox,
    Key,
    KeyRange,
    Prefix,
    ancestors,
    ancestors_from_root,
    chunk_key,
    load_json,
    node_type,
    nodes,
    parse_chunk_key,
)
from refkv.pb import Concat, Range, Reference, Source
from refkv.policy import Policy, crc32c
from refkv.refstore import ReferenceStore
from refkv.uri import resolve as resolve_reference
from refkv.vzip.names import INDEX_KEY, RESERVED_PREFIX, SOURCES_KEY

if TYPE_CHECKING:
    from refkv.reader import Registry, Resolver, Stats

__all__ = [
    "ChunkBox", "Key", "KeyRange", "Prefix",
    "map_keys", "mount", "normalize", "overlay", "shift_chunks", "union",
]

Value = bytes | Reference

# The chunk boxes of an array are indexed in a bulk-loaded box tree (an R-tree): one entry
# per box whatever its size or rank, each node the bounding box of what is below it and the
# greatest number there, so a lookup descends only into nodes that contain the chunk (and,
# for `owner`, could beat the best number found so far).
_LEAF = 16

_DOCUMENTS = frozenset({"zarr.json", ".zarray", ".zgroup", ".zattrs"})
GROUP_V3 = b'{"zarr_format":3,"node_type":"group","attributes":{}}'
GROUP_V2 = b'{"zarr_format":2}'


class Options(TypedDict, total=False):
    """The options of the store an operator returns (see `ReferenceStore`). A `url` or
    `policy` of None asks for the default the operator decides from its stores."""

    url: str | None
    page_size: int | None
    info: dict[str, object]
    registry: Registry | None
    stats: Stats | None
    policy: Policy | None
    max_concurrency: int | None


# ---------------------------------------------------------------- the bounds index


_Interval = tuple[str, str | None, int]


@dataclasses.dataclass(slots=True)
class _Node:
    """A node of an interval tree: its center, the intervals holding it by start and by
    stop, and the subtrees of those wholly below and wholly above it."""

    center: str
    by_start: list[_Interval]
    by_stop: list[_Interval]
    left: _Node | None
    right: _Node | None


class _Intervals:
    """Key ranges `[start, stop)` (`stop` None: unbounded), each with a number; `which(key)`
    gives the numbers of those containing `key`. A centered interval tree: O(n) memory,
    O(log n + found) a query."""

    def __init__(self, items: list[tuple[str, str | None, int]]) -> None:
        self.root = self._build(items)
        # for `owner`: the key line split at every end, with the greatest number per piece
        self.points = sorted({s for s, _, _ in items} | {e for _, e, _ in items if e is not None})
        starts: dict[str, list[tuple[str | None, int]]] = {}
        for s, e, i in items:
            starts.setdefault(s, []).append((e, i))
        self.top: list[int] = []
        active: Counter[int] = Counter()
        ends: list[tuple[str, int]] = []  # (end, number), a heap
        best: list[int] = []  # numbers active, a max-heap (negated), pruned lazily
        for point in self.points:
            while ends and ends[0][0] <= point:
                _, i = heapq.heappop(ends)
                active[i] -= 1
            for e, i in starts.get(point, ()):
                active[i] += 1
                heapq.heappush(best, -i)
                if e is not None:
                    heapq.heappush(ends, (e, i))
            while best and active[-best[0]] <= 0:
                heapq.heappop(best)
            self.top.append(-best[0] if best else -1)

    def owner(self, key: str) -> int:
        """The greatest number of an interval containing `key`, or -1."""
        k = bisect.bisect_right(self.points, key) - 1
        return self.top[k] if k >= 0 else -1

    @classmethod
    def _build(cls, items: list[tuple[str, str | None, int]]) -> _Node | None:
        if not items:
            return None
        starts = sorted(s for s, _, _ in items)
        center = starts[len(starts) // 2]  # a start: its interval is held here, so each level shrinks
        here = [it for it in items if it[0] <= center and (it[1] is None or center < it[1])]
        left = [it for it in items if it[1] is not None and it[1] <= center]
        right = [it for it in items if it[0] > center]
        by_start = sorted(here, key=lambda it: it[0])
        by_stop = sorted(here, key=lambda it: (it[1] is None, it[1] or ""), reverse=True)  # unbounded first
        return _Node(center, by_start, by_stop, cls._build(left), cls._build(right))

    def which(self, key: str) -> set[int]:
        out: set[int] = set()
        node = self.root
        while node is not None:
            if key < node.center:
                for s, _, i in node.by_start:  # all contain center > key: those starting by key contain it
                    if s > key:
                        break
                    out.add(i)
                node = node.left
            else:
                for _, e, i in node.by_stop:  # all start by center <= key: those ending after it contain it
                    if e is not None and e <= key:
                        break
                    out.add(i)
                node = node.right if key > node.center else None
        return out


@dataclasses.dataclass(slots=True)
class _BoxNode:
    """A node of a box tree: the bounding box of the boxes below it, their greatest number,
    and either its children (greatest number first) or, at a leaf, its boxes."""

    start: tuple[int, ...]
    stop: tuple[int, ...]
    top: int
    children: list[_BoxNode]
    boxes: list[tuple[ChunkBox, int]]

    def holds(self, c: tuple[int, ...]) -> bool:
        return all(a <= x < z for x, a, z in zip(c, self.start, self.stop, strict=True))


class _Boxes:
    """The chunk boxes of one array and chunk key encoding, in a box tree (see `_LEAF`)."""

    def __init__(self, boxes: list[tuple[ChunkBox, int]]) -> None:
        boxes = [(b, i) for b, i in boxes if all(a < z for a, z in zip(b.start, b.stop, strict=True))]
        self.root = self._build(boxes) if boxes else None

    @classmethod
    def _build(cls, boxes: list[tuple[ChunkBox, int]]) -> _BoxNode:
        rank = len(boxes[0][0].start)
        start = tuple(min(b.start[d] for b, _ in boxes) for d in range(rank))
        stop = tuple(max(b.stop[d] for b, _ in boxes) for d in range(rank))
        top = max(i for _, i in boxes)
        if len(boxes) <= _LEAF or rank == 0:
            return _BoxNode(start, stop, top, [], sorted(boxes, key=lambda bi: -bi[1]))
        # split at the median of the boxes' centers on the axis where the centers spread most
        axis = max(range(rank), key=lambda d: max(b.start[d] + b.stop[d] for b, _ in boxes)
                   - min(b.start[d] + b.stop[d] for b, _ in boxes))
        boxes = sorted(boxes, key=lambda bi: bi[0].start[axis] + bi[0].stop[axis])
        half = len(boxes) // 2
        children = sorted((cls._build(boxes[:half]), cls._build(boxes[half:])), key=lambda n: -n.top)
        return _BoxNode(start, stop, top, children, [])

    def which(self, c: tuple[int, ...], out: set[int]) -> None:
        work = [self.root] if self.root is not None and self.root.holds(c) else []
        while work:
            node = work.pop()
            for b, i in node.boxes:
                if i not in out and all(a <= x < z for x, a, z in zip(c, b.start, b.stop, strict=True)):
                    out.add(i)
            work.extend(child for child in node.children if child.holds(c))

    def owner(self, c: tuple[int, ...], best: int) -> int:
        work = [self.root] if self.root is not None else []
        while work:
            node = work.pop()
            if node.top <= best or not node.holds(c):
                continue
            for b, i in node.boxes:  # greatest number first: the first that holds c wins here
                if i <= best:
                    break
                if all(a <= x < z for x, a, z in zip(c, b.start, b.stop, strict=True)):
                    best = i
                    break
            work.extend(reversed(node.children))  # the child with the greater number is taken first
        return best


class _Cover:
    """Which of several numbered bound sets contain a key: `which(key)`, as a set."""

    def __init__(self, sets: Iterable[tuple[int, Iterable[Bound]]]) -> None:
        self.keys: dict[str, set[int]] = {}
        ranges: list[tuple[str, str | None, int]] = []
        boxes: dict[str, dict[tuple[int, str, str], list[tuple[ChunkBox, int]]]] = {}
        for i, bounds in sets:
            for b in bounds:
                if isinstance(b, Key):
                    self.keys.setdefault(b.key, set()).add(i)
                elif isinstance(b, (KeyRange, Prefix)):
                    r = b.as_range() if isinstance(b, Prefix) else b
                    if r.stop is None or r.start < r.stop:
                        ranges.append((r.start, r.stop, i))
                else:
                    group = (len(b.start), b.encoding, b.separator)
                    boxes.setdefault(b.array, {}).setdefault(group, []).append((b, i))
        self.ranges = _Intervals(ranges)
        self.boxes = {array: {g: _Boxes(items) for g, items in groups.items()}
                      for array, groups in boxes.items()}
        self.top = {key: max(found) for key, found in self.keys.items()}

    def which(self, key: str) -> set[int]:
        out = set(self.keys.get(key, ()))
        if self.ranges.root is not None:
            out |= self.ranges.which(key)
        for array in self._arrays(key):
            for (rank, encoding, sep), group in self.boxes.get(array, {}).items():
                c = parse_chunk_key(key, array, rank, encoding, sep)
                if c is not None:
                    group.which(c, out)
        return out

    def _arrays(self, key: str) -> Iterable[str]:
        if len(self.boxes) <= 8:  # a few arrays: test each, rather than walk the key's ancestors
            return [a for a in self.boxes if not a or key.startswith(a + "/")]
        return ancestors(key)

    def owner(self, key: str) -> int:
        """The greatest number of a set containing `key`, or -1: `max(which(key))`, without
        finding every set."""
        best = max(self.top.get(key, -1), self.ranges.owner(key))
        for array in self._arrays(key):
            for (rank, encoding, sep), group in self.boxes.get(array, {}).items():
                c = parse_chunk_key(key, array, rank, encoding, sep)
                if c is not None:
                    best = group.owner(c, best)
        return best

    def contains(self, key: str) -> bool:
        return self.owner(key) >= 0


# ---------------------------------------------------------------- values and sources


def _absolute(store: ReferenceStore) -> list[Source]:
    return [dataclasses.replace(s, url=resolve_reference(store.url, s.url)) if s.url is not None else s
            for s in store.sources]


def _renumber(value: Value, table: Sequence[int]) -> Value:
    if isinstance(value, Concat):
        return Concat(tuple(_renumber_range(r, table) for r in value.parts))
    if isinstance(value, Range):
        return _renumber_range(value, table)
    return value


def _renumber_range(r: Range, table: Sequence[int]) -> Range:
    return r if r.data is not None else dataclasses.replace(r, source=table[r.source])


def _used(value: Value) -> list[int]:
    if isinstance(value, bytes):
        return []
    parts = value.parts if isinstance(value, Concat) else (value,)
    return [r.source for r in parts if r.data is None]


class _Number(str):
    """A JSON number as written, which no parsed JSON value is."""

    __slots__ = ()


class _Token(str):
    """Text `_canonical` writes as it is, which no parsed JSON value is."""

    __slots__ = ()


def _canonical(doc: object) -> str:
    """`doc`, a parsed JSON value, as one canonical text: object keys sorted, strings
    quoted, numbers as written and marked `n` (so `true`, `1` and `1.0` differ, and no
    string or object spells a number). It walks with a stack, not recursion, so a deep
    document neither stops it nor makes comparing two results recurse."""
    parts: list[str] = []
    work: list[object] = [doc]
    while work:
        x = work.pop()
        if isinstance(x, _Token):
            parts.append(x)
        elif isinstance(x, dict):
            items: list[object] = [_Token("{")]
            for n, k in enumerate(sorted(x)):
                items += [_Token("," if n else ""), _Token(json.dumps(k, ensure_ascii=False) + ":"), x[k]]
            work.extend(reversed([*items, _Token("}")]))
        elif isinstance(x, list):
            items = [_Token("[")]
            for n, v in enumerate(x):
                items += [_Token("," if n else ""), v]
            work.extend(reversed([*items, _Token("]")]))
        elif isinstance(x, _Number):
            parts.append("n" + x)
        else:  # a string, true, false or null
            parts.append(json.dumps(x, ensure_ascii=False))
    return "".join(parts)


def _load(value: bytes) -> object:
    """A JSON document with its numbers kept as written (`_Number`); None if `value` is not
    strict JSON: UTF-8 without a byte order mark, and no object with a key twice (readers
    disagree on which value of a doubled key counts)."""
    try:
        return load_json(value.decode("utf-8"), parse_float=_Number, parse_int=_Number,
                         parse_constant=_Number, object_pairs_hook=_object)
    except ValueError:
        return None


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out = dict(pairs)
    if len(out) != len(pairs):
        raise ValueError("an object with a key twice")
    return out


def _unconsolidated(key: str, value: Value) -> Value | None:
    """`value` without the consolidated metadata a combination makes stale: None for a v2
    `.zmetadata` document, and a v3 `zarr.json` without its `consolidated_metadata`."""
    name = key.rpartition("/")[2]
    if name == ".zmetadata":
        return None
    if name != "zarr.json":
        return value
    if not isinstance(value, bytes):
        raise ValueError(f"{key!r} is held as a reference, so its consolidated metadata, which the "  # noqa: TRY004
                         "result would make stale, cannot be dropped; hold Zarr documents as bytes")
    doc = _load(value)
    if doc is None and _listed(value):
        raise ValueError(f"{key!r} is not strict JSON (malformed, with a byte order mark or a key "
                         "twice, or nested too deep), so its consolidated metadata cannot be dropped")
    if not isinstance(doc, dict) or doc.get("consolidated_metadata") is None:
        return value
    del doc["consolidated_metadata"]
    return _dump(doc).encode()


def _listed(value: bytes) -> bool:
    """Whether a reader as lenient as zarr-python's (any of UTF-8, 16 or 32, a byte order
    mark, keys twice, escaped names) would find a `consolidated_metadata` listing in `value`."""
    copies: list[bool] = []

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        # every copy of a doubled key counts, as readers disagree on which one wins; an
        # object's members are built before it, so the last call is the top level's
        copies[:] = [v is not None for k, v in items if k == "consolidated_metadata"]
        return dict(items)

    try:
        doc = load_json(value, object_pairs_hook=pairs)
    except ValueError:
        return b"consolidated_metadata" in value  # can't tell: take a mention as one
    return isinstance(doc, dict) and any(copies)


def _dump(doc: object) -> str:
    """`doc`, from `_load`, as compact JSON: its keys in their order, its numbers as written,
    its strings escaped to ASCII (so a lone surrogate is still valid JSON). A stack walks it."""
    parts: list[str] = []
    work: list[object] = [doc]
    while work:
        x = work.pop()
        if isinstance(x, _Token):
            parts.append(x)
        elif isinstance(x, dict):
            items: list[object] = [_Token("{")]
            for n, (k, v) in enumerate(x.items()):
                items += [_Token("," if n else ""), _Token(json.dumps(k) + ":"), v]
            work.extend(reversed([*items, _Token("}")]))
        elif isinstance(x, list):
            items = [_Token("[")]
            for n, v in enumerate(x):
                items += [_Token("," if n else ""), v]
            work.extend(reversed([*items, _Token("]")]))
        elif isinstance(x, _Number):
            parts.append(str(x))
        else:
            parts.append(json.dumps(x))
    return "".join(parts)


def _form(key: str, value: Value, identity: Callable[[int], object], *, documents: bool = True) -> object:
    """What `union` compares of a store's `value` at `key`: with `documents`, a Zarr document
    (`zarr.json`, `.zarray`, `.zgroup`, `.zattrs`) held as bytes as its JSON value, a
    `zarr.json` without `consolidated_metadata`; else what the value reads (`_reads`)."""
    reads = _reads(value, identity)
    name = key.rpartition("/")[2]
    if documents and name in _DOCUMENTS and all(kind == "data" for kind, *_ in reads):
        doc = _load(b"".join(cast("bytes", what) for _, what, *_ in reads))
        if doc is not None:
            if isinstance(doc, dict) and name == "zarr.json":
                doc.pop("consolidated_metadata", None)
            return ("json", _canonical(doc))
    return ("reads", reads)


def _reads(value: Value, identity: Callable[[int], object]) -> tuple[tuple[str, object, int, int, int | None], ...]:
    """What `value` reads, as segments whatever its form: the bytes it holds or names (literal
    data, a data source, a key source to bytes) as `("data", bytes)`, adjacent ones joined;
    a range of another source as `("range", identity, offset, length)`, adjacent ranges of
    one source joined. So `b"abc"`, `Range(data=b"abc")` and a one-part `Concat` agree."""
    parts = value.parts if isinstance(value, Concat) else (value,)
    # ("data", bytes, 0, 0, None), or ("range", source, offset, length, crc32c): a range the
    # reader would refuse (past the end of the bytes it names, or failing its checksum)
    # stays a range, so it never equals bytes that read
    out: list[tuple[str, object, int, int, int | None]] = []
    for r in parts:
        if isinstance(r, bytes):
            seg: tuple[str, object, int, int, int | None] = ("data", r, 0, 0, None)
        elif r.data is not None:
            seg = ("data", r.data, 0, 0, None)
        elif not r.length:
            continue  # a zero-length range is never resolved, nor its checksum checked
        else:
            source = identity(r.source)
            held = (source.data if isinstance(source, Source) else
                    source[1] if isinstance(source, tuple) and source[0] == "bytes" else None)
            part = held[r.offset:r.offset + r.length] if isinstance(held, bytes) else None
            if part is not None and len(part) == r.length and (r.crc32c is None or crc32c(part) == r.crc32c):
                seg = ("data", part, 0, 0, None)
            else:
                seg = ("range", source, r.offset, r.length, r.crc32c)
        kind, what, offset, length, crc = seg
        if kind == "data" and not what:
            continue  # reads nothing
        last = out[-1] if out else None
        if last is not None and last[0] == kind == "data":
            cast("list[bytes]", last[1]).append(cast("bytes", what))  # joined once, at the end
        elif (last is not None and last[0] == kind == "range" and last[1] == what and last[4] is None
              and crc is None and last[2] + last[3] == offset):
            out[-1] = ("range", what, last[2], last[3] + length, None)
        else:
            out.append(("data", [what], 0, 0, None) if kind == "data" else seg)
    return tuple(("data", b"".join(cast("list[bytes]", what)), 0, 0, None) if kind == "data"
                 else (kind, what, offset, length, crc) for kind, what, offset, length, crc in out)


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()[:16]


def _hidden_base(key: str, value: bytes) -> str:
    """The name a hidden key had before a combination named it by its value's digest."""
    base, sep, digest = key.rpartition("~")
    return base if sep and digest == _digest(value) and base not in (SOURCES_KEY, INDEX_KEY) else key


def _hidden_value(store: ReferenceStore, i: int, k: str) -> bytes:
    """The bytes of hidden key `k` of `store` (the `i`th), which a key source names."""
    value = store.mapping.get(k)
    if value is None:
        raise ValueError(f"store {i} has a reference to its hidden key {k!r}, which it does not hold")
    if not isinstance(value, bytes):
        raise ValueError(  # noqa: TRY004 (a store the operation cannot combine, not a wrong argument)
            f"a key source names the hidden key {k!r}, whose value is a reference; "
            "a combination carries only bytes hidden values")
    return value


def _hidden_names(named: list[dict[str, bytes]]) -> tuple[list[dict[str, str]], dict[str, bytes]]:
    """The names, in a combination, of the hidden keys its carried values read: for each
    store, {hidden key: name}, and the values by name. A name all the stores that carry it
    give one value stays; otherwise each value is named `key~` and the start of its SHA-256
    digest, so no name depends on the order of the stores.

    Raises ValueError when two different values come to one name (a hidden key named like
    the digest name of another value)."""
    distinct: dict[str, set[bytes]] = {}
    for found in named:
        for k, value in found.items():
            distinct.setdefault(_hidden_base(k, value), set()).add(value)
    names: list[dict[str, str]] = []
    values: dict[str, bytes] = {}
    for found in named:
        out = {}
        for k, value in found.items():
            base = _hidden_base(k, value)
            name = base if len(distinct[base]) == 1 else f"{base}~{_digest(value)}"
            if values.setdefault(name, value) != value:
                raise ValueError(f"two different hidden values come to the name {name!r}")
            out[k] = name
        names.append(out)
    return names, values


# ---------------------------------------------------------------- the result's options


def _registry_key(resolver: Resolver) -> tuple[object, ...]:
    """What tells two resolvers' registered stores apart: the same objects, or none."""
    reg = resolver._registry  # noqa: SLF001 (refkv's own Resolver)
    return (None if reg is None else id(reg),
            tuple((s, n, p, id(st)) for s, n, p, st in resolver._prefixes))  # noqa: SLF001


def _reach(stores: Sequence[ReferenceStore], options: Options) -> None:
    """Fill in `options`' policy, registry, base URL, page size and request cap for a
    combination of `stores`, so that no store's sources become readable that its own
    options refuse. Only the stores with URL sources read anything outside themselves, so
    only they decide the policy, registry and base URL; the first store with keys or
    sources gives the page size and request cap.

    Raises ValueError when the stores' policies, or registries, differ and `options` sets none."""
    present = [s for s in stores if s.mapping or s.sources] or list(stores)
    if not present:
        return
    options.setdefault("page_size", present[0].page_size)
    options.setdefault("max_concurrency", present[0].max_concurrency)
    stores = [s for s in present if any(src.url is not None for src in s.sources)] or present[:1]
    first = stores[0]
    if "policy" not in options:
        if any(s.policy != first.policy for s in stores):
            raise ValueError("the stores have different reader policies; pass policy= to say "
                             "which the result has")
        options["policy"] = first.policy
    if "registry" not in options:
        if any(_registry_key(s.resolver) != _registry_key(first.resolver) for s in stores):
            raise ValueError("the stores have different registries; pass registry= to say "
                             "which the result has")
        options["registry"] = first.resolver
    options.setdefault("url", first.url)


def _build(mapping: dict[str, Value], sources: list[Source], *, read_at_open: Iterable[str] = (),
           compressed: Iterable[str] = (), bounds: Iterable[Bound] | None, template: ReferenceStore | None,
           options: Options) -> ReferenceStore:
    """A store of `mapping`, its sources cut to those used and the most used first."""
    counts = Counter(s for v in mapping.values() for s in _used(v))
    order = sorted(counts, key=lambda s: (-counts[s], s))
    table = {old: new for new, old in enumerate(order)}
    full = [table.get(i, 0) for i in range(len(sources))]
    mapping = {k: _renumber(v, full) for k, v in mapping.items()}
    kwargs: dict[str, object] = {}
    if template is not None:
        kwargs.update(registry=template.resolver, policy=template.policy,
                      max_concurrency=template.max_concurrency, page_size=template.page_size)
    kwargs.update(options)
    kwargs.setdefault("info", {})
    return ReferenceStore(mapping, [sources[i] for i in order],
                          read_at_open=[k for k in read_at_open if k in mapping],
                          compressed=[k for k in compressed if isinstance(mapping.get(k), bytes)],
                          bounds=bounds, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------- overlay and union


def _combine(stores: Sequence[ReferenceStore], agree: bool, options: Options) -> ReferenceStore:
    for s in stores:
        if not isinstance(s, ReferenceStore):
            raise TypeError(f"the operands are ReferenceStores, not {type(s).__name__}")
    _undefault(options)
    _reach(stores, options)
    _check_boxes(stores)
    cover = _Cover(enumerate(s.bounds for s in stores))
    absolute = [_absolute(s) for s in stores]

    # Each store's keys inside its own bounds, as (key, held, kept): `kept` the value the
    # result would keep (None for a `.zmetadata` it drops), and whether this store answers.
    answers: list[list[tuple[str, Value, Value | None]]] = []
    carried: list[set[str]] = []
    covering: dict[str, set[int]] = {}  # for `agree`: each key's stores, found once
    for i, store in enumerate(stores):
        own = _Cover([(0, store.bounds)]) if store.bounds_stated else None  # derived bounds hold every key
        mine: list[tuple[str, Value, Value | None]] = []
        wins: set[str] = set()
        for key, held in store.mapping.items():
            if key.startswith(RESERVED_PREFIX) or (own is not None and not own.contains(key)):
                continue
            if agree:
                which = covering.get(key)
                if which is None:
                    which = covering[key] = cover.which(key)
                owner = max(which)
            else:
                owner = cover.owner(key)
            # only the value the result keeps is rewritten (and may be refused); another
            # store's is compared as it is, `_form` reading a document without its listing
            if owner == i:
                kept = _unconsolidated(key, held)
            else:
                kept = None if key.rpartition("/")[2] == ".zmetadata" else held
            mine.append((key, held, kept))
            if kept is not None and owner == i:
                wins.add(key)
        answers.append(mine)
        carried.append(wins)

    # The hidden keys the carried values read, named
    named: list[dict[str, bytes]] = []
    for i, store in enumerate(stores):
        found: dict[str, bytes] = {}
        for key, held, _ in answers[i]:
            if key in carried[i]:
                for s in _used(held):
                    k = store.sources[s].key
                    if k is not None and k.startswith(RESERVED_PREFIX) and k not in found:
                        found[k] = _hidden_value(store, i, k)
        named.append(found)
    hidden_names, hidden = _hidden_names(named)

    def identity(i: int, *, follow: bool = True) -> Callable[[int], object]:
        """Each source of store `i` as what it reads: a URL source as itself (absolute), a
        key source as the value of that key in store `i` (followed one step)."""
        def source(s: int) -> object:
            src = absolute[i][s]
            if src.key is None:
                return src
            held = stores[i].mapping.get(src.key)
            if held is None or not follow:
                return ("missing" if held is None else "key", src.key)
            if isinstance(held, bytes):
                return ("bytes", held)
            return ("reference", _form(src.key, held, identity(i, follow=False), documents=False))
        return source

    if agree:
        # a key some store reads through a key source must be held as the same bytes by every
        # store that holds it: a key source reads only bytes, so which store wins must not
        # change what that source reads
        targets = {k for i, s in enumerate(stores) for _, held, _ in answers[i] for src in _used(held)
                   if (k := s.sources[src].key) is not None and not k.startswith(RESERVED_PREFIX)}
        seen: dict[str, tuple[object, int, set[int]]] = {}
        for i, mine in enumerate(answers):
            for key, held, kept in mine:
                if kept is None:
                    continue  # a key the result drops is not compared
                if key in targets:  # what a key source reads: the bytes held, before any rewrite
                    form: object = ("target", held) if isinstance(held, bytes) else ("target-reference", i)
                else:
                    form = _form(key, kept, identity(i))
                first = seen.setdefault(key, (form, i, set()))
                if first[0] != form:
                    raise ValueError(f"{key!r}: stores {first[1]} and {i} hold different values")
                first[2].add(i)
        for key, (_, _, holders) in seen.items():
            missing = covering[key] - holders
            if missing:
                raise ValueError(f"{key!r}: store {min(holders)} holds it, but store {min(missing)}, "
                                 "whose bounds contain it too, does not")

    sources: list[Source] = []
    index: dict[Source, int] = {}
    mapping: dict[str, Value] = {}
    origin: dict[str, int] = {}
    rewritten: set[str] = set()
    read_at_open: list[str] = []
    compressed: list[str] = []
    internal: list[tuple[int, str]] = []
    for i, store in enumerate(stores):
        names = hidden_names[i]
        table = []
        for original in absolute[i]:
            src = dataclasses.replace(original, key=names[original.key]) if original.key in names else original
            if src not in index:
                index[src] = len(sources)
                sources.append(src)
            table.append(index[src])
        for key, held, kept in answers[i]:
            if key not in carried[i] or kept is None:
                continue
            mapping[key] = _renumber(kept, table)
            origin[key] = i
            if kept is not held:
                rewritten.add(key)
            for s in _used(held):
                k = store.sources[s].key
                if k is not None and not k.startswith(RESERVED_PREFIX):
                    internal.append((i, k))
        read_at_open += [k for k in store.read_at_open if origin.get(k) == i]
        read_at_open += [names[k] for k in store.read_at_open if k in names]
        compressed += [k for k in store.compressed if origin.get(k) == i]
        compressed += [names[k] for k in store.compressed if k in names]
    for i, target in internal:
        j = origin.get(target)
        if target in rewritten:
            raise ValueError(f"store {i} has a reference to its own key {target!r}, whose value the "
                             "result rewrites (dropping its consolidated metadata)")
        # a key source reads its key's bytes (a key holding a reference is refused), so the
        # result must hold the same bytes there, not a reference that reads them
        own_value = stores[i].mapping.get(target)
        kept_value = None if j is None else stores[j].mapping[target]
        if j != i and not (isinstance(own_value, bytes) and isinstance(kept_value, bytes) and own_value == kept_value):
            raise ValueError(f"store {i} has a reference to its own key {target!r}, which the result "
                             "takes from another store or leaves out")
    mapping.update(hidden)
    bounds = [b for s in stores for b in s.bounds]
    return _build(mapping, sources, read_at_open=read_at_open, compressed=compressed,
                  bounds=bounds, template=None, options=options)


def _undefault(options: Options) -> None:
    """Drop a `url=None` or `policy=None` from `options`: None asks for the default, which an
    operator decides from its stores, never the looser default of a new `ReferenceStore`."""
    for name in ("url", "policy"):
        if name in options and options[name] is None:  # type: ignore[literal-required]
            del options[name]  # type: ignore[misc]


def _layout(rank: int, encoding: str, sep: str) -> tuple[int, str, str]:
    """What tells chunk keys apart: rank, encoding, and the separator, except for a key with
    no separator in it: one of no axes (`c`, `0`), or a v2 key of one axis."""
    return (rank, encoding, "" if rank == 0 or (encoding == "v2" and rank == 1) else sep)


def _check_boxes(stores: Sequence[ReferenceStore]) -> None:
    """Raise ValueError for a `ChunkBox` of an array some store describes, whose rank or
    chunk key encoding is not the array's: it would take in none of the array's chunks."""
    layouts: dict[str, set[tuple[int, str, str]]] = {}
    for s in stores:
        for path, kind in nodes(s.mapping).items():
            if kind == "array":
                try:
                    shape, _, encoding, sep = _array(s, path)
                except ValueError:
                    continue
                layouts.setdefault(path, set()).add((len(shape), encoding, sep))
    known = {path: {_layout(*lay) for lay in lays} for path, lays in layouts.items()}
    for s in stores:
        for b in s.bounds:
            if isinstance(b, ChunkBox) and b.array in known and (
                    _layout(len(b.start), b.encoding, b.separator) not in known[b.array]):
                rank, encoding, sep = min(layouts[b.array])
                raise ValueError(f"the bound {b!r} takes in no chunk of array {b.array!r}, which has "
                                 f"{rank} axes and chunk key encoding {encoding!r} with separator {sep!r}")


def overlay(*stores: ReferenceStore, **options: Unpack[Options]) -> ReferenceStore:
    """One store answering each key from the last of `stores` whose bounds contain it.

    The result holds the key's value from that store, or nothing if that store holds none
    (inside its bounds, a missing key is absent: a chunk's fill value). Its bounds are
    every store's. A store's keys outside its own bounds are not carried. Combine many
    stores in one call: each call copies its stores, so a fold of pairwise calls takes
    time quadratic in their number.

    Keyword arguments set the result's options (`ReferenceStore`). By default its policy
    and registry are those of the stores with URL sources (which must agree), its base
    URL one that lets none of them read what its own refuses (the reader policy tells
    only local `file:` bases from remote ones), its `info` empty, and its request cap and
    page size the first store's that holds anything. An explicit `url=` is the caller's
    choice, and a local one lets every store read local files.

    Raises ValueError when a value carried into the result reads through a key source (an
    internal reference) whose key the result takes, with another value, from another
    store, leaves out or rewrites, or names a hidden key its own store does not hold; when
    the stores' policies or registries differ and none is given; and when no base URL
    serves them all.
    """
    return _combine(stores, False, options)  # type: ignore[arg-type]


def union(*stores: ReferenceStore, **options: Unpack[Options]) -> ReferenceStore:
    """`overlay`, where any two `stores` whose bounds share a key must answer it the same.

    Values are compared by what they read, whatever their form: bytes, literal ranges,
    data sources and key sources to bytes by those bytes, and ranges of URL sources by
    the absolute URL, pins, offset and length, adjacent ranges joined. Zarr
    documents (`zarr.json`, `.zarray`, `.zgroup`, `.zattrs`) are compared as canonical
    JSON, a `zarr.json` without the consolidated metadata the result drops; the result
    keeps the last store's bytes. Keys the result drops (`.zmetadata`) are not compared.

    Raises ValueError naming the key when two stores hold different values for it, or
    when one holds it and another whose bounds contain it does not, and as `overlay`
    does. On stores with disjoint bounds, the order of `stores` changes only the order of
    the result's keys and sources, and the options taken from the first store (its page
    size and request cap, and its base URL where the stores' differ).
    """
    return _combine(stores, True, options)  # type: ignore[arg-type]


# ---------------------------------------------------------------- one store


def map_keys(store: ReferenceStore, f: Callable[[str], str | None], *,
             bounds: Iterable[Bound] | None = None, **options: Unpack[Options]) -> ReferenceStore:
    """`store` with each key renamed to `f(key)`, or dropped where `f` returns None.

    Key sources (internal references) are renamed with their keys, and hidden keys (under
    the reserved prefix) are kept as they are. Consolidated metadata is dropped, as the
    renamed keys may no longer match it. The result's bounds are `bounds`; else, for a
    store whose bounds are derived, derived again from its keys; else the store's stated
    `Key` bounds renamed with `f`. A combination's result states its operands' bounds:
    rename it with `bounds=`, or after `.with_bounds(None)` to derive them again.

    Raises ValueError when two keys are renamed to one, when a key is renamed into, or out
    of, the reserved prefix, when a value uses a key source whose key is dropped or
    rewritten, when the store states a `KeyRange`, `Prefix` or `ChunkBox` bound and
    `bounds` is not given, since a function on keys cannot move one, and when a key outside
    the store's stated bounds is renamed onto one inside them.
    """
    if bounds is None and store.bounds_stated:
        renamed: dict[str, str] = {}  # each distinct Key bound's new name, by it
        for b in store.bounds:
            if not isinstance(b, Key):
                raise ValueError(  # noqa: TRY004 (a bound the operation cannot move, not a wrong argument)
                    f"map_keys cannot move the bound {b!r}; pass bounds= for the result")
            if b.key not in renamed and (new := f(b.key)) is not None:
                renamed[b.key] = new
        bounded = set(renamed.values())
        if len(bounded) != len(renamed):  # a repeated bound is one bound, two distinct ones are not
            raise ValueError("two of the store's Key bounds are renamed to one key")
        moved: list[Bound] = [Key(new) for new in renamed.values()]
        inside = {b.key for b in store.bounds if isinstance(b, Key)}
        for key in store.mapping:
            if key not in inside and not key.startswith(RESERVED_PREFIX) and f(key) in bounded:
                raise ValueError(f"{key!r}, outside the store's bounds, is renamed to {f(key)!r}, inside them")
        bounds = moved
    return _map_keys(store, f, bounds, {}, [], options, unconsolidate=True)  # type: ignore[arg-type]


def _map_keys(store: ReferenceStore, f: Callable[[str], str | None], bounds: Iterable[Bound] | None,
              extra: dict[str, Value], read_at_open: list[str], options: Options, *,
              unconsolidate: bool) -> ReferenceStore:
    """`map_keys`, then the bytes values of `extra` added, read at open with `read_at_open`;
    consolidated metadata dropped with `unconsolidate`, since renamed keys make it stale.
    Relative source URLs are made absolute when `options` gives the result another base."""
    mapping: dict[str, Value] = {}
    names: dict[str, str] = {}
    rewritten: set[str] = set()
    for key, held in store.mapping.items():
        new = key if key.startswith(RESERVED_PREFIX) else f(key)
        if new is None:
            continue
        if not isinstance(new, str) or not new:
            raise ValueError(f"{key!r} is renamed to {new!r}; keys are non-empty strings")
        if new.startswith(RESERVED_PREFIX) != key.startswith(RESERVED_PREFIX):
            raise ValueError(f"{key!r} is renamed to {new!r}, across the reserved prefix "
                             f"{RESERVED_PREFIX!r}")
        if new in names:
            raise ValueError(f"{names[new]!r} and {key!r} are both renamed to {new!r}")
        names[new] = key
        value = _unconsolidated(new, held) if unconsolidate else held
        if value is not None:
            mapping[new] = value
            if value is not held:
                rewritten.add(key)
    for k, v in extra.items():
        if k in names:
            rewritten.add(names[k])
        mapping[k] = v
    renamed = {old: new for new, old in names.items() if new in mapping}
    used = {s for v in mapping.values() for s in _used(v)}
    _undefault(options)
    base = options.get("url", store.url)
    originals = store.sources if base == store.url else _absolute(store)
    sources = []
    for i, s in enumerate(originals):
        if s.key is not None and i in used:
            if s.key not in renamed:
                raise ValueError(f"a key source names {s.key!r}, which the result does not hold")
            if s.key in rewritten:
                raise ValueError(f"a key source names {s.key!r}, whose value the result rewrites")
        sources.append(dataclasses.replace(s, key=renamed[s.key]) if s.key in renamed else s)
    options["url"] = base
    return _build(mapping, sources,
                  read_at_open=[*(renamed[k] for k in store.read_at_open if k in renamed), *read_at_open],
                  compressed=[renamed[k] for k in store.compressed if k in renamed],
                  bounds=bounds, template=store, options=options)


def _mount_bound(b: Bound, path: str) -> Bound:
    if isinstance(b, Key):
        return Key(f"{path}/{b.key}")
    if isinstance(b, Prefix):
        return Prefix(f"{path}/{b.path}" if b.path else path)
    if isinstance(b, KeyRange):
        return KeyRange(f"{path}/{b.start}", f"{path}/{b.stop}" if b.stop is not None else f"{path}0")
    return dataclasses.replace(b, array=f"{path}/{b.array}" if b.array else path)


def mount(store: ReferenceStore, path: str, **options: Unpack[Options]) -> ReferenceStore:
    """`store` under `path/`: key `k` becomes `path/k`, and its bounds move with it.

    No group is added above it; `normalize` adds the missing ones, once the stores are
    combined. Raises ValueError for an empty path, or one with a leading, trailing or
    doubled `/`.
    """
    if not isinstance(path, str) or not path or path.startswith("/") or path.endswith("/") or "//" in path:
        raise ValueError(f"mount path {path!r}: a non-empty node path, with no leading, trailing or doubled '/'")
    bounds = [_mount_bound(b, path) for b in store.bounds] if store.bounds_stated else None  # derived stay derived
    return _map_keys(store, lambda k: f"{path}/{k}", bounds, {}, [],
                     options, unconsolidate=False)  # type: ignore[arg-type]


def _array(store: ReferenceStore, array: str) -> tuple[list[int], list[int], str, str]:
    """(shape, chunk shape, encoding, separator) of the array at `array` in `store`."""
    base = f"{array}/" if array else ""
    v3, v2 = store.mapping.get(base + "zarr.json"), store.mapping.get(base + ".zarray")
    if v3 is not None and node_type(v3) is None and v2 is not None:
        v3 = None  # a zarr.json that marks no node, beside a v2 array: the v2 array decides
    doc = v3 if v3 is not None else v2
    if not isinstance(doc, bytes):
        raise ValueError(  # noqa: TRY004 (a missing array, not a wrong argument type)
            f"{array!r} is not an array of the store: no zarr.json or .zarray document")
    try:
        meta = load_json(doc)
        if v3 is not None:
            if meta.get("node_type") != "array":
                raise ValueError(f"{array!r} is a {meta.get('node_type')}, not an array")
            grid = meta["chunk_grid"]
            if grid.get("name") != "regular":
                raise ValueError(f"{array!r} has a {grid.get('name')!r} chunk grid; shift_chunks needs a regular one")
            encoding = meta["chunk_key_encoding"]
            name = encoding["name"]
            sep = (encoding.get("configuration") or {}).get("separator") or ("/" if name == "default" else ".")
            shape, chunks = list(meta["shape"]), list(grid["configuration"]["chunk_shape"])
        else:
            name, sep = "v2", meta.get("dimension_separator") or "."
            shape, chunks = list(meta["shape"]), list(meta["chunks"])
        if name not in ("default", "v2") or sep not in ("/", ".") or len(shape) != len(chunks):
            raise ValueError(f"{array!r}: chunk key encoding {name!r} with separator {sep!r}, or a "
                             "chunk shape of another rank than the shape")
        return [operator.index(n) for n in shape], [operator.index(c) for c in chunks], name, sep
    except (KeyError, TypeError, AttributeError, json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValueError(f"{array!r}: malformed array metadata ({type(e).__name__}: {e})") from None


def _shift_bounds(store: ReferenceStore, array: str, rank: int, encoding: str, sep: str, new_axes: int,
                  offset: tuple[int, ...], grid: tuple[int, ...]) -> list[Bound]:
    """The bounds of `shift_chunks`' result: the store's bounds on the array's chunks, moved."""
    whole = ChunkBox(array, offset, tuple(o + g for o, g in zip(offset, grid, strict=True)),
                     encoding, sep)  # type: ignore[arg-type]
    base = f"{array}/" if array else ""
    # the keys that may be chunk keys of the array: `c` and what follows it, or digits (v2)
    chunks = (KeyRange(base + "c", base + "c" + chr(ord(sep) + 1)) if encoding == "default"
              else KeyRange(base + "0", base + ":"))
    out: list[Bound] = []
    for b in store.bounds:
        if isinstance(b, Prefix) and (not b.path or array == b.path or array.startswith(b.path + "/")):
            out.append(whole)
        elif isinstance(b, ChunkBox) and b.array == array and (
                _layout(len(b.start), b.encoding, b.separator) == _layout(rank, encoding, sep)):
            start = tuple(o + x for o, x in zip(offset, (0,) * new_axes + b.start, strict=True))
            stop = tuple(o + x for o, x in zip(offset, (1,) * new_axes + b.stop, strict=True))
            out.append(ChunkBox(array, start, stop, encoding, sep))  # type: ignore[arg-type]
        elif isinstance(b, Key):
            c = parse_chunk_key(b.key, array, rank, encoding, sep)
            if c is not None:
                moved = tuple(o + x for o, x in zip(offset, (0,) * new_axes + c, strict=True))
                out.append(Key(chunk_key(array, moved, encoding, sep)))
        elif isinstance(b, (KeyRange, Prefix)):
            r = b.as_range() if isinstance(b, Prefix) else b
            if (r.stop is None or chunks.start < r.stop) and (chunks.stop is None or r.start < chunks.stop):
                raise ValueError(f"shift_chunks cannot move the bound {b!r}, which may take in some of "
                                 f"{array!r}'s chunks")
        elif isinstance(b, ChunkBox) and b.array == array:
            raise ValueError(f"shift_chunks cannot move the bound {b!r}, of another chunk key encoding or rank")
    return out


def shift_chunks(store: ReferenceStore, array: str, offset: Sequence[SupportsIndex], *, new_axes: int = 0,
                 **options: Unpack[Options]) -> ReferenceStore:
    """The chunks of the array at `array` alone, moved: `new_axes` new leading axes of one
    chunk each, then `offset` added to every coordinate.

    The result holds those chunks under the same array path, and no metadata: a header
    store gives that. Only the chunks inside the store's bounds move, and its bounds on
    them move too: a bound over the whole array (such as the derived one) becomes the
    `ChunkBox` the moved chunk grid fills, a `ChunkBox` of the array moves with its
    chunks, and a `Key` bound of a chunk moves with it. For the planes of a stack,
    `shift_chunks(plane, "0", (i, 0, 0), new_axes=1)` puts plane `i`'s chunk `(y, x)` at
    `(i, y, x)`. Hidden keys are kept, and key sources renamed with the chunks they name.

    Raises ValueError when `array` is not an array of `store` with a regular chunk grid
    and well-formed metadata, when `offset` has another length than the moved array's
    axes or a negative entry, when a chunk reads through a key source whose key is not a
    moved chunk, or when the store states a bound on part of the array that does not move
    with it (a `KeyRange`, or a `Prefix` inside the array).
    """
    shape, chunks, encoding, sep = _array(store, array)
    rank = len(shape)
    if isinstance(new_axes, bool) or any(isinstance(o, bool) for o in offset):
        raise ValueError(f"offset {offset!r} and new_axes {new_axes!r} are integers, not bools")
    try:
        new_axes = operator.index(new_axes)
        moves = tuple(operator.index(o) for o in offset)
    except TypeError:
        raise ValueError(f"offset {offset!r} and new_axes {new_axes!r} are integers, not bools") from None
    if new_axes < 0:
        raise ValueError(f"new_axes is a count of axes, not {new_axes!r}")
    if len(moves) != rank + new_axes:
        raise ValueError(f"offset {moves} has {len(moves)} entries; the moved array has {rank + new_axes} axes")
    if any(o < 0 for o in moves):
        raise ValueError(f"offset {moves}: entries are integers of 0 or more")
    grid = (1,) * new_axes + tuple(-(-n // c) if c else 0 for n, c in zip(shape, chunks, strict=True))
    bounds = _shift_bounds(store, array, rank, encoding, sep, new_axes, moves, grid)
    own = _Cover([(0, store.bounds)])

    def move(key: str) -> str | None:
        c = parse_chunk_key(key, array, rank, encoding, sep)
        if c is None or not own.contains(key):
            return None
        return chunk_key(array, tuple(o + x for o, x in zip(moves, (0,) * new_axes + c, strict=True)),
                         encoding, sep)

    return _map_keys(store, move, bounds, {}, [], options, unconsolidate=False)  # type: ignore[arg-type]


def normalize(store: ReferenceStore, **options: Unpack[Options]) -> ReferenceStore:
    """`store` as a valid Zarr hierarchy: keys no valid node owns dropped, and a group added
    at each missing ancestor of a node.

    An array owns every key below it: its chunks are kept, and a node document below it is
    dropped. A group owns only its metadata: another key whose nearest node above it is a
    group is dropped, as a chunk an `overlay` left under a group that replaced its array.
    Ownership is decided in the hierarchy with the added groups (a root group among them),
    so a second `normalize` changes nothing, and every key that is neither a node's
    metadata nor below an array, such as a stray file at the root, is dropped. Where v2 and v3
    documents mark one path, the v3 document is kept, and a v2 group's documents beside a
    v3 group too, so that v2 nodes below it stay reachable. Each missing ancestor of a v3 node
    gets a v3 group, and of a v2 node a v2 group, where it has no group of that version,
    replacing a document there that marks no node: every node is reachable from the root
    through groups of its own version. A store with no Zarr nodes is returned with the same keys. The result's bounds
    are the store's, plus the added groups' keys when they were stated; derived otherwise.
    It reads the store as one whole hierarchy: normalize a combination, not a patch or
    other bounded part of one.

    Raises ValueError when a value reads through a key source whose key is dropped or
    replaced, and when a `zarr.json` is held as a reference.
    """
    for key, value in store.mapping.items():
        if key.rpartition("/")[2] == "zarr.json" and not isinstance(value, bytes):
            raise ValueError(f"{key!r} is held as a reference, so normalize cannot tell what node, "
                             "if any, it marks; hold Zarr documents as bytes")
    found = nodes(store.mapping)
    bounds_kept = store.bounds if store.bounds_stated else None
    if not found:
        return _map_keys(store, lambda k: k, bounds_kept, {}, [], options,  # type: ignore[arg-type]
                         unconsolidate=False)
    arrays = {p for p, kind in found.items() if kind == "array"}
    v3 = {p for p in found if node_type(store.mapping.get(f"{p}/zarr.json" if p else "zarr.json")) is not None}

    def array_above(path: str) -> str | None:
        """The outermost array strictly above `path`, which owns it."""
        return next((a for a in ancestors_from_root(path) if a in arrays), None)

    # the paths with a v2 node document that normalize keeps: a v2 array's where no v3
    # document marks the path, a v2 group's where none or a v3 group does
    v2 = {path for path, _, name in (k.rpartition("/") for k in store.mapping) if path in found and (
        (name == ".zarray" and path not in v3) or (name == ".zgroup" and (path not in v3 or found[path] == "group")))}
    # the groups to add: at each ancestor of a node with no array above it that has no group
    # of the node's own Zarr version, so that the node is reachable from the root in it
    added: dict[str, bytes] = {}
    for path in sorted(p for p in found if array_above(p) is None):
        version, marker, group = (v3, "zarr.json", GROUP_V3) if path in v3 else (v2, ".zgroup", GROUP_V2)
        for a in ancestors(path):
            if a not in version:
                added.setdefault((f"{a}/" if a else "") + marker, group)
    # ownership is decided in the hierarchy as it will be, added groups included, so that
    # a second normalize finds nothing more to do
    new_groups = {k.rpartition("/")[0] for k in added} - found.keys()
    every = {**found, **dict.fromkeys(new_groups, "group")}
    v3 |= {k.rpartition("/")[0] for k in added if k.rpartition("/")[2] == "zarr.json"}

    keep: set[str] = set()
    for key in store.mapping:
        if key.startswith(RESERVED_PREFIX):
            keep.add(key)
            continue
        path, _, name = key.rpartition("/")
        if path in every and name in ("zarr.json", ".zarray", ".zgroup", ".zattrs"):
            if path in new_groups and name != ".zattrs":
                continue  # a document there that marks no node: the added group replaces it
            if name == "zarr.json" and path not in v3:
                continue  # a zarr.json beside a v2 node that marks no node itself
            # a node's metadata, with no array above it; where v2 and v3 documents mark one
            # path, a v2 document is kept only beside a v3 group, as a v2 group's
            v2_beside = name != "zarr.json" and path in v3
            if array_above(path) is None and (not v2_beside or (every[path] == "group" and name != ".zarray")):
                keep.add(key)
            continue
        if array_above(key) is not None:
            keep.add(key)  # an array's chunk, or anything else below an array
    bounds = [*bounds_kept, *(Key(k) for k in added)] if bounds_kept is not None else None
    return _map_keys(store, lambda k: k if k in keep else None, bounds, dict(added),  # type: ignore[arg-type]
                     list(added), options, unconsolidate=True)
