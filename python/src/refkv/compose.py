"""Operators that combine reference stores into new ones, by their bounds.

Each store answers for the keys of its bounds (`refkv.bounds`, `ReferenceStore.bounds`):
inside them a key it does not hold is absent, outside them it has nothing to say. The
operators build new stores and change none:

- `overlay(*stores)`: each key from the last store whose bounds contain it, a value or
  absence, as a later layer wins in TensorStore's `stack` and `kvstack`. Associative and
  idempotent, with the empty store as identity; not commutative.
- `union(*stores)`: `overlay`, where stores whose bounds share a key must answer it the
  same (equal values, or both absent). On disjoint bounds it is also commutative: the
  operator for a stack or a mosaic.
- `map_keys(store, f)`: each key renamed to `f(key)`, or dropped where it gives None.
- `mount(store, path)`: the store under `path/`, its bounds moved with it.
- `shift_chunks(store, array, offset)`: the chunks of one array alone, moved by `offset`
  (after `new_axes` new leading axes), bounded by the box they fill.
- `normalize(store)`: keys no valid Zarr node owns dropped, missing ancestor groups added.

A combination's sources are each store's, relative URLs resolved against that store's
base URL, equal ones merged, and the unused dropped; references are renumbered to match,
the most used source first. A store's keys outside its own bounds are never carried into
a combination. Its hidden keys (under the binding's reserved prefix) are carried only when
a carried value reads one through a key source, renamed by its content if another
store's differs.
Consolidated metadata (a `zarr.json`'s `consolidated_metadata`, a v2 `.zmetadata`) lists
the nodes of one store, so `overlay`, `union`, `map_keys` and `normalize` drop it. Every
result's `info` is empty unless `info=` is given.

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
import itertools
import json
import math
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from typing import TYPE_CHECKING, TypedDict, Unpack

from refkv.bounds import (
    MARKERS,
    Bound,
    ChunkBox,
    Key,
    KeyRange,
    Prefix,
    _node_type,
    ancestors,
    chunk_key,
    nodes,
    parse_chunk_key,
)
from refkv.pb import Concat, Range, Reference, Source
from refkv.policy import Policy, _scheme
from refkv.refstore import ReferenceStore
from refkv.uri import resolve as resolve_reference
from refkv.vzip.names import RESERVED_PREFIX

if TYPE_CHECKING:
    from refkv.reader import Registry, Resolver, Stats

__all__ = [
    "ChunkBox", "Key", "KeyRange", "Prefix",
    "map_keys", "mount", "normalize", "overlay", "shift_chunks", "union",
]

Value = bytes | Reference

# Boxes of at most this many chunks are indexed chunk by chunk. A larger one is indexed
# by the coordinate of its shallowest axis when that is at most _BOX_DEPTH chunks deep (a
# stack's plane is one chunk deep); else it is checked against every chunk key of its array.
_BOX_CELLS = 4096
_BOX_DEPTH = 64


class Options(TypedDict, total=False):
    """The options of the store an operator returns (see `ReferenceStore`)."""

    url: str
    page_size: int | None
    info: dict[str, object]
    registry: Registry | None
    stats: Stats | None
    policy: Policy | None
    max_concurrency: int | None


@dataclasses.dataclass
class _Boxes:
    """The chunk boxes of one array and encoding, indexed (see `_BOX_CELLS`)."""

    cells: dict[tuple[int, ...], set[int]] = dataclasses.field(default_factory=dict)
    slabs: dict[tuple[int, int], list[tuple[ChunkBox, int]]] = dataclasses.field(default_factory=dict)
    wide: list[tuple[ChunkBox, int]] = dataclasses.field(default_factory=list)


class _Cover:
    """Which of several numbered bound sets contain a key: `which(key)`, as a set."""

    def __init__(self, sets: Iterable[tuple[int, Iterable[Bound]]]) -> None:
        self.keys: dict[str, set[int]] = {}
        ranges: list[tuple[str, str | None, int]] = []
        # by array path, then by (rank, encoding, separator): the small boxes' chunks; the
        # large boxes by (axis, coordinate) on their shallowest axis; and the rest
        self.boxes: dict[str, dict[tuple[int, str, str], _Boxes]] = {}
        for i, bounds in sets:
            for b in bounds:
                if isinstance(b, Key):
                    self.keys.setdefault(b.key, set()).add(i)
                elif isinstance(b, (KeyRange, Prefix)):
                    r = b.as_range() if isinstance(b, Prefix) else b
                    if r.stop is None or r.start < r.stop:
                        ranges.append((r.start, r.stop, i))
                else:
                    groups = self.boxes.setdefault(b.array, {})
                    group = groups.setdefault((len(b.start), b.encoding, b.separator), _Boxes())
                    extents = [z - a for a, z in zip(b.start, b.stop, strict=True)]
                    if math.prod(extents) <= _BOX_CELLS:
                        for c in itertools.product(*(range(a, z) for a, z in zip(b.start, b.stop, strict=True))):
                            group.cells.setdefault(c, set()).add(i)
                    elif min(extents) <= _BOX_DEPTH:
                        axis = extents.index(min(extents))
                        for x in range(b.start[axis], b.stop[axis]):
                            group.slabs.setdefault((axis, x), []).append((b, i))
                    else:
                        group.wide.append((b, i))
        self._sweep(ranges)

    def _sweep(self, ranges: list[tuple[str, str | None, int]]) -> None:
        """Split the key line at every range's ends; record, for each piece, the sets
        whose ranges cover it."""
        points = sorted({s for s, _, _ in ranges} | {e for _, e, _ in ranges if e is not None})
        starts: dict[str, list[tuple[str | None, int]]] = {}
        for s, e, i in ranges:
            starts.setdefault(s, []).append((e, i))
        self.points = points
        self.pieces: list[frozenset[int]] = []
        active: Counter[int] = Counter()
        ends: list[tuple[str, int]] = []  # (end, set), a heap
        for p in points:
            while ends and ends[0][0] <= p:
                _, i = heapq.heappop(ends)
                active[i] -= 1
                if not active[i]:
                    del active[i]
            for e, i in starts.get(p, ()):
                active[i] += 1
                if e is not None:
                    heapq.heappush(ends, (e, i))
            self.pieces.append(frozenset(active))

    def which(self, key: str) -> set[int]:
        out = set(self.keys.get(key, ()))
        k = bisect.bisect_right(self.points, key) - 1
        if k >= 0:
            out |= self.pieces[k]
        for array in ancestors(key):
            for (rank, encoding, sep), group in self.boxes.get(array, {}).items():
                c = parse_chunk_key(key, array, rank, encoding, sep)
                if c is None:
                    continue
                out |= group.cells.get(c, set())
                candidates = [group.wide, *(group.slabs.get((axis, x), ()) for axis, x in enumerate(c))]
                for b, i in itertools.chain.from_iterable(candidates):
                    if i not in out and all(a <= x < z for x, a, z in zip(c, b.start, b.stop, strict=True)):
                        out.add(i)
        return out

    def contains(self, key: str) -> bool:
        return bool(self.which(key))


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


def _unconsolidated(key: str, value: Value) -> Value | None:
    """`value` without the consolidated metadata a combination makes stale: None for a v2
    `.zmetadata` document, and a v3 `zarr.json` without its `consolidated_metadata`."""
    name = key.rpartition("/")[2]
    if name == ".zmetadata":
        return None
    if name != "zarr.json" or not isinstance(value, bytes):
        return value
    try:
        doc = json.loads(value)
    except ValueError:
        return value
    if not isinstance(doc, dict) or doc.get("consolidated_metadata") is None:
        return value
    del doc["consolidated_metadata"]
    return json.dumps(doc, separators=(",", ":")).encode()


def _agreement_form(key: str, value: Value) -> object:
    """What `union` compares of `value`: a `zarr.json` document as parsed JSON without its
    `consolidated_metadata`, so that formatting and listings don't count; else the value."""
    if key.rpartition("/")[2] != "zarr.json" or not isinstance(value, bytes):
        return value
    try:
        doc = json.loads(value)
    except ValueError:
        return value
    if isinstance(doc, dict):
        doc.pop("consolidated_metadata", None)
    # as canonical text, not as Python values, which would equate true, 1 and 1.0
    return ("json", json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def _hidden_names(stores: Sequence[ReferenceStore]) -> tuple[list[dict[str, str]], dict[str, bytes]]:
    """The names, in a combination of `stores`, of the hidden keys their key sources name:
    for each store, {hidden key: name}, and the values by name. A key every store that
    has it holds the same value under keeps its name; otherwise each distinct value is
    named `key~` and the start of its SHA-256 digest, so no name depends on the order of
    `stores`.

    Raises ValueError for such a hidden key whose value is a reference, not bytes."""
    found: list[dict[str, bytes]] = []
    distinct: dict[str, set[bytes]] = {}  # by base name: a name a combination gave stays one
    for store in stores:
        named: dict[str, bytes] = {}
        for s in store.sources:
            k = s.key
            if k is None or not k.startswith(RESERVED_PREFIX) or k in named or k not in store.mapping:
                continue
            value = store.mapping[k]
            if not isinstance(value, bytes):
                raise ValueError(  # noqa: TRY004 (a store the operation cannot combine, not a wrong argument)
                    f"a key source names the hidden key {k!r}, whose value is a reference; "
                    "a combination carries only bytes hidden values")
            named[k] = value
            distinct.setdefault(_hidden_base(k, value), set()).add(value)
        found.append(named)
    names: list[dict[str, str]] = []
    values: dict[str, bytes] = {}
    for named in found:
        out = {}
        for k, value in named.items():
            base = _hidden_base(k, value)
            name = base if len(distinct[base]) == 1 else f"{base}~{_digest(value)}"
            out[k] = name
            values[name] = value
        names.append(out)
    return names, values


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()[:16]


def _hidden_base(key: str, value: bytes) -> str:
    """The name a hidden key had before a combination named it by its value's digest."""
    base, sep, digest = key.rpartition("~")
    return base if sep and digest == _digest(value) else key


def _registry_key(resolver: Resolver) -> tuple[object, ...]:
    """What tells two resolvers' registered stores apart: the same objects, or none."""
    reg = resolver._registry  # noqa: SLF001 (refkv's own Resolver)
    return (None if reg is None else id(reg),
            tuple((s, n, p, id(st)) for s, n, p, st in resolver._prefixes))  # noqa: SLF001


def _has_files(store: ReferenceStore) -> bool:
    return any(s.url is not None and _scheme(s.url) == "file" for s in _absolute(store))


def _reach(stores: Sequence[ReferenceStore], options: Options) -> None:
    """Fill in `options`' policy, registry and base URL for a combination of `stores`, so
    that no store's sources become readable that its own options refuse.

    Raises ValueError when the stores' policies, or registries, differ and `options` sets
    none, and when no base URL serves them all: a remote store's `file:` sources, which a
    remote base refuses, beside a local store's, which need a local one."""
    if not stores:
        return
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
    if "url" in options:
        return
    local = [s for s in stores if _scheme(s.url) == "file"]
    remote = [s for s in stores if _scheme(s.url) != "file"]
    policy = options["policy"] or Policy()
    if not local or not remote:
        options["url"] = first.url
    elif policy.allow_files_from_remote_archives or not any(_has_files(s) for s in remote):
        options["url"] = local[0].url
    elif not any(_has_files(s) for s in local):
        options["url"] = remote[0].url
    else:
        raise ValueError("a remote store has file: sources, which its base URL keeps it from "
                         "reading, and a local store has file: sources too, so no base URL "
                         "serves both; pass url= to choose (a local one lets every store read "
                         "local files)")


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


def _combine(stores: Sequence[ReferenceStore], agree: bool, options: Options) -> ReferenceStore:
    for s in stores:
        if not isinstance(s, ReferenceStore):
            raise TypeError(f"the operands are ReferenceStores, not {type(s).__name__}")
    _reach(stores, options)
    cover = _Cover(enumerate(s.bounds for s in stores))
    sources: list[Source] = []
    index: dict[Source, int] = {}
    mapping: dict[str, Value] = {}
    origin: dict[str, int] = {}
    answers: dict[str, tuple[object, set[int], int]] = {}  # for `agree`: compared form, holders, first holder
    read_at_open: list[str] = []
    compressed: list[str] = []
    internal: list[tuple[int, str]] = []  # (store, key) of each key source a carried value uses
    hidden_names, hidden = _hidden_names(stores)
    for i, store in enumerate(stores):
        table = []
        names = hidden_names[i]
        absolute = [dataclasses.replace(s, key=names[s.key]) if s.key in names else s for s in _absolute(store)]
        for src in absolute:
            if src not in index:
                index[src] = len(sources)
                sources.append(src)
            table.append(index[src])
        own = _Cover([(0, store.bounds)])
        for key, held in store.mapping.items():
            if key.startswith(RESERVED_PREFIX) or not own.contains(key):
                continue
            value = _unconsolidated(key, _renumber(held, table))
            if agree and value is not None:
                seen = answers.get(key)
                form = _agreement_form(key, value)
                if seen is None:
                    answers[key] = (form, {i}, i)
                elif seen[0] != form:
                    raise ValueError(f"{key!r}: stores {seen[2]} and {i} hold different values")
                else:
                    seen[1].add(i)
            if max(cover.which(key)) != i or value is None:
                continue
            mapping[key] = value
            origin[key] = i
            for s in _used(held):
                k = store.sources[s].key
                if k is None:
                    continue
                if not k.startswith(RESERVED_PREFIX):
                    internal.append((i, k))
                elif k not in names:
                    raise ValueError(f"store {i} has a reference to its hidden key {k!r}, which it "
                                     "does not hold")
        read_at_open += [k for k in store.read_at_open if origin.get(k) == i]
        compressed += [k for k in store.compressed if origin.get(k) == i]
    if agree:
        for key, (_, holders, _) in answers.items():
            missing = cover.which(key) - holders
            if missing:
                raise ValueError(f"{key!r}: store {min(holders)} holds it, but store {min(missing)}, "
                                 "whose bounds contain it too, does not")
    for i, target in internal:
        if origin.get(target) != i:
            raise ValueError(f"store {i} has a reference to its own key {target!r}, which the result "
                             "takes from another store or leaves out")
    used = {sources[s].key for v in mapping.values() for s in _used(v)}
    mapping.update((k, v) for k, v in hidden.items() if k in used)
    bounds = [b for s in stores for b in s.bounds]
    return _build(mapping, sources, read_at_open=read_at_open, compressed=compressed,
                  bounds=bounds, template=stores[0] if stores else None, options=options)


def overlay(*stores: ReferenceStore, **options: Unpack[Options]) -> ReferenceStore:
    """One store answering each key from the last of `stores` whose bounds contain it.

    Combine many stores in one call: each call copies its stores, so a fold of pairwise
    calls takes time quadratic in their number.

    The result holds the key's value from that store, or nothing if that store holds none
    (inside its bounds, a missing key is absent: a chunk's fill value). Its bounds are
    every store's. A store's keys outside its own bounds are not carried.

    Keyword arguments set the result's options (`ReferenceStore`). By default its policy
    and registry are the stores' (which must agree), its base URL one that lets no store
    read what its own refuses (the reader policy tells only local `file:` bases from
    remote ones), its `info` empty, and its request cap and page size the first store's.

    Raises ValueError when a value carried into the result is a reference through a key
    source (an internal reference) whose key the result takes from another store, or
    leaves out, or names a hidden key its own store does not hold; when the stores'
    policies or registries differ and none is given; and when no base URL serves them
    all (see `url=`).
    """
    return _combine(stores, False, dict(options))  # type: ignore[arg-type]


def union(*stores: ReferenceStore, **options: Unpack[Options]) -> ReferenceStore:
    """`overlay`, where any two `stores` whose bounds share a key must answer it the same.

    `zarr.json` documents are compared as JSON, without their consolidated metadata, which
    the result drops; the result keeps the last store's bytes.

    Raises ValueError naming the key when two stores hold different values for it, or
    when one holds it and another whose bounds contain it does not. On stores with
    disjoint bounds, the order of `stores` changes only the order of the result's keys
    and sources.
    """
    return _combine(stores, True, dict(options))  # type: ignore[arg-type]


def map_keys(store: ReferenceStore, f: Callable[[str], str | None], *,
             bounds: Iterable[Bound] | None = None, **options: Unpack[Options]) -> ReferenceStore:
    """`store` with each key renamed to `f(key)`, or dropped where `f` returns None.

    Key sources (internal references) are renamed with their keys. The result's bounds
    are `bounds`, or derived from its keys: a function on keys says nothing about the
    ranges and boxes of stated bounds. Consolidated metadata is dropped, as the renamed
    keys may no longer match it.

    Raises ValueError when two keys are renamed to one, when a key is renamed into, or out
    of, the binding's reserved prefix, or when a value uses a key source whose key is
    dropped. Hidden keys (under the reserved prefix) are kept as they are.
    """
    return _map_keys(store, f, bounds, {}, [], dict(options), unconsolidate=True)  # type: ignore[arg-type]


def _map_keys(store: ReferenceStore, f: Callable[[str], str | None], bounds: Iterable[Bound] | None,
              extra: dict[str, Value], read_at_open: list[str], options: Options, *,
              unconsolidate: bool) -> ReferenceStore:
    """`map_keys`, then the bytes values of `extra` added, read at open with `read_at_open`;
    consolidated metadata dropped with `unconsolidate`, since renamed keys make it stale."""
    mapping: dict[str, Value] = {}
    names: dict[str, str] = {}
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
    mapping.update(extra)
    renamed = {old: new for new, old in names.items() if new in mapping}
    used = {s for v in mapping.values() for s in _used(v)}
    sources = []
    for i, s in enumerate(store.sources):
        if s.key is not None and s.key not in renamed and i in used:
            raise ValueError(f"a key source names {s.key!r}, which the result does not hold")
        sources.append(dataclasses.replace(s, key=renamed[s.key]) if s.key in renamed else s)
    options.setdefault("url", store.url)
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
    return _map_keys(store, lambda k: f"{path}/{k}", [_mount_bound(b, path) for b in store.bounds], {}, [],
                     dict(options), unconsolidate=False)  # type: ignore[arg-type]


def _array(store: ReferenceStore, array: str) -> tuple[list[int], list[int], str, str]:
    """(shape, chunk shape, encoding, separator) of the array at `array` in `store`."""
    base = f"{array}/" if array else ""
    v3, v2 = store.mapping.get(base + "zarr.json"), store.mapping.get(base + ".zarray")
    doc = v3 if v3 is not None else v2
    if not isinstance(doc, bytes):
        raise ValueError(  # noqa: TRY004 (a missing array, not a wrong argument type)
            f"{array!r} is not an array of the store: no zarr.json or .zarray document")
    meta = json.loads(doc)
    if v3 is not None:
        if meta.get("node_type") != "array":
            raise ValueError(f"{array!r} is a {meta.get('node_type')}, not an array")
        grid = meta["chunk_grid"]
        if grid.get("name") != "regular":
            raise ValueError(f"{array!r} has a {grid.get('name')!r} chunk grid; shift_chunks needs a regular one")
        encoding = meta["chunk_key_encoding"]
        name = encoding["name"]
        sep = encoding.get("configuration", {}).get("separator", "/" if name == "default" else ".")
        return list(meta["shape"]), list(grid["configuration"]["chunk_shape"]), name, sep
    return list(meta["shape"]), list(meta["chunks"]), "v2", meta.get("dimension_separator") or "."


def shift_chunks(store: ReferenceStore, array: str, offset: Sequence[int], *, new_axes: int = 0,
                 **options: Unpack[Options]) -> ReferenceStore:
    """The chunks of the array at `array` alone, moved: `new_axes` new leading axes of one
    chunk each, then `offset` added to every coordinate.

    The result holds those chunks under the same array path, and no metadata: a header
    store gives that. Its bounds are the `ChunkBox` the moved array's chunk grid fills,
    so it answers for exactly those chunks, holding them or not. For the planes of a
    stack, `shift_chunks(plane, "0", (i, 0, 0), new_axes=1)` puts plane `i`'s chunk
    `(y, x)` at `(i, y, x)`. Hidden keys are kept, and key sources renamed with the
    chunks they name.

    Raises ValueError when `array` is not an array of `store` with a regular chunk grid,
    when `offset` has another length than the moved array's axes or a negative entry, or
    when a chunk reads through a key source whose key is not a chunk of the array.
    """
    shape, chunks, encoding, sep = _array(store, array)
    rank = len(shape)
    if isinstance(new_axes, bool) or not isinstance(new_axes, int) or new_axes < 0:
        raise ValueError(f"new_axes is a count of axes, not {new_axes!r}")
    offset = tuple(offset)
    if len(offset) != rank + new_axes:
        raise ValueError(f"offset {offset} has {len(offset)} entries; the moved array has {rank + new_axes} axes")
    if any(isinstance(o, bool) or not isinstance(o, int) or o < 0 for o in offset):
        raise ValueError(f"offset {offset}: entries are integers of 0 or more")
    grid = (1,) * new_axes + tuple(-(-n // c) if c else 0 for n, c in zip(shape, chunks, strict=True))
    def move(key: str) -> str | None:
        c = parse_chunk_key(key, array, rank, encoding, sep)
        if c is None:
            return None
        return chunk_key(array, tuple(o + x for o, x in zip(offset, (0,) * new_axes + c, strict=True)),
                         encoding, sep)

    box = ChunkBox(array, offset, tuple(o + g for o, g in zip(offset, grid, strict=True)),
                   encoding, sep)  # type: ignore[arg-type]
    return _map_keys(store, move, [box], {}, [], dict(options), unconsolidate=False)  # type: ignore[arg-type]


def normalize(store: ReferenceStore, **options: Unpack[Options]) -> ReferenceStore:
    """`store` as a valid Zarr hierarchy: keys no valid node owns dropped, and a group added
    at each missing ancestor of a node.

    A node's metadata is kept unless an array is above it. Any other key is kept when
    the nearest node above it is a kept array (its chunks), or when no node is above it
    at all (it is not part of the hierarchy); under a group, it is dropped, as a chunk an
    `overlay` left under a group that replaced its array. A store with no Zarr nodes is
    returned with the same keys. Where v2 and v3 documents mark one path, the v3 document
    is kept. A missing group is v3 when the store has a v3 node, and replaces a document
    there that marks no node. The result's bounds are the store's, plus the added groups'
    keys when they were stated; derived otherwise.
    """
    found = nodes(store.mapping)
    if not found:
        return map_keys(store, lambda k: k, bounds=store.bounds if store.bounds_stated else None, **options)
    arrays = {p for p, kind in found.items() if kind == "array"}
    v3 = {k.rpartition("/")[0] for k, v in store.mapping.items()
          if k.rpartition("/")[2] == "zarr.json" and _node_type(v) is not None}

    def valid(path: str) -> bool:
        return not any(a in arrays for a in ancestors(path)) if path else True

    keep: dict[str, Value] = {}
    for key, value in store.mapping.items():
        if key.startswith(RESERVED_PREFIX):
            keep[key] = value
            continue
        path, _, name = key.rpartition("/")
        if (name in MARKERS or name == ".zattrs") and path in found:
            if valid(path) and not (name != "zarr.json" and path in v3):  # v3 decides a path both mark
                keep[key] = value
            continue
        owner = next((p for p in ancestors(key) if p in found), None)
        if owner is None or (found[owner] == "array" and valid(owner)):
            keep[key] = value
    added: list[str] = []
    # a missing group is v3 if the store has any v3 node, whatever the order of its keys
    marker, group = (("zarr.json", b'{"zarr_format":3,"node_type":"group","attributes":{}}') if v3
                     else (".zgroup", b'{"zarr_format":2}'))
    for path in sorted(p for p in found if valid(p)):
        for a in ancestors(path):
            key = (f"{a}/" if a else "") + marker
            if a in found or key in added:
                continue
            keep[key] = group  # replacing a document there that marks no node
            added.append(key)
    bounds = [*store.bounds, *(Key(k) for k in added)] if store.bounds_stated else None
    return _map_keys(store, lambda k: k if k in keep else None, bounds, {k: keep[k] for k in added},
                     added, dict(options), unconsolidate=True)  # type: ignore[arg-type]
