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
a carried value reads one through a key source, renamed if another store's differs.
Consolidated metadata (a `zarr.json`'s `consolidated_metadata`, a v2 `.zmetadata`) lists
the nodes of one store, so `overlay`, `union`, `map_keys` and `normalize` drop it.

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
import heapq
import json
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
    ancestors,
    chunk_key,
    nodes,
    parse_chunk_key,
)
from refkv.pb import Concat, Range, Reference, Source
from refkv.refstore import ReferenceStore
from refkv.uri import resolve as resolve_reference
from refkv.vzip.names import RESERVED_PREFIX

if TYPE_CHECKING:
    from refkv.policy import Policy
    from refkv.reader import Registry, Stats

__all__ = [
    "ChunkBox", "Key", "KeyRange", "Prefix",
    "map_keys", "mount", "normalize", "overlay", "shift_chunks", "union",
]

Value = bytes | Reference

# Boxes at most this many chunks deep on their first axis are indexed by that axis'
# coordinate; deeper ones are checked against every key of their array.
_BUCKET_DEPTH = 64


class Options(TypedDict, total=False):
    """The options of the store an operator returns (see `ReferenceStore`)."""

    url: str
    page_size: int | None
    info: dict[str, object]
    registry: Registry | None
    stats: Stats | None
    policy: Policy | None
    max_concurrency: int | None


class _Cover:
    """Which of several numbered bound sets contain a key: `which(key)`, as a set."""

    def __init__(self, sets: Iterable[tuple[int, Iterable[Bound]]]) -> None:
        self.keys: dict[str, set[int]] = {}
        ranges: list[tuple[str, str | None, int]] = []
        self.boxes: dict[tuple[str, int, str, str], tuple[dict[int, list[tuple[ChunkBox, int]]],
                                                         list[tuple[ChunkBox, int]]]] = {}
        for i, bounds in sets:
            for b in bounds:
                if isinstance(b, Key):
                    self.keys.setdefault(b.key, set()).add(i)
                elif isinstance(b, (KeyRange, Prefix)):
                    r = b.as_range() if isinstance(b, Prefix) else b
                    if r.stop is None or r.start < r.stop:
                        ranges.append((r.start, r.stop, i))
                else:
                    group = (b.array, len(b.start), b.encoding, b.separator)
                    buckets, wide = self.boxes.setdefault(group, ({}, []))
                    if any(a == z for a, z in zip(b.start, b.stop, strict=True)):
                        continue  # an empty box
                    if b.start and b.stop[0] - b.start[0] <= _BUCKET_DEPTH:
                        for c0 in range(b.start[0], b.stop[0]):
                            buckets.setdefault(c0, []).append((b, i))
                    else:
                        wide.append((b, i))
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
        for (array, rank, encoding, sep), (buckets, wide) in self.boxes.items():
            c = parse_chunk_key(key, array, rank, encoding, sep)
            if c is None:
                continue
            for b, i in (buckets.get(c[0], []) if c else []) + wide:
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


def _hidden_names(store: ReferenceStore, taken: dict[str, Value], tag: int) -> dict[str, str]:
    """{hidden key: its name in a combination} for each hidden key of `store` that a key source
    names, given the hidden values `taken` so far (which it extends): the name an equal value
    already has, else the key itself if free, else a new one (`key~tag`, ...).

    Raises ValueError for such a hidden key whose value is a reference, not bytes."""
    out: dict[str, str] = {}
    for s in store.sources:
        k = s.key
        if k is None or not k.startswith(RESERVED_PREFIX) or k in out or k not in store.mapping:
            continue
        value = store.mapping[k]
        if not isinstance(value, bytes):
            raise ValueError(  # noqa: TRY004 (a store the operation cannot combine, not a wrong argument)
                f"a key source names the hidden key {k!r}, whose value is a reference; "
                "a combination carries only bytes hidden values")
        same = [n for n, v in taken.items() if v == value and (n == k or n.startswith(k + "~"))]
        if same:
            out[k] = same[0]
            continue
        name, n = k, 0
        while name in taken:
            n += 1
            name = f"{k}~{tag}" if n == 1 else f"{k}~{tag}.{n}"
        taken[name] = value
        out[k] = name
    return out


def _base_url(stores: Sequence[ReferenceStore], url: str | None) -> str | None:
    """The result's base URL: `url`, or the stores' one, or the first store's when all are
    local or all remote (the reader policy tells only those apart). Mixed: an error."""
    if url is not None or not stores:
        return url
    local = {s.url.startswith("file:") for s in stores}
    if len(local) > 1:
        raise ValueError("the stores have local and remote base URLs; pass url= to say "
                         "which the result has (it decides whether file: sources may be read)")
    return stores[0].url


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
    url = _base_url(stores, options.pop("url", None))
    cover = _Cover(enumerate(s.bounds for s in stores))
    sources: list[Source] = []
    index: dict[Source, int] = {}
    mapping: dict[str, Value] = {}
    origin: dict[str, int] = {}
    answers: dict[str, tuple[Value, set[int], int]] = {}  # for `agree`: value, holders, first holder
    read_at_open: list[str] = []
    compressed: list[str] = []
    internal: list[tuple[int, str]] = []  # (store, key) of each key source a carried value uses
    hidden: dict[str, Value] = {}  # the hidden keys key sources name, by their names in the result
    for i, store in enumerate(stores):
        table = []
        names = _hidden_names(store, hidden, i)
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
            value = _renumber(held, table)
            if agree:
                seen = answers.get(key)
                if seen is None:
                    answers[key] = (value, {i}, i)
                elif seen[0] != value:
                    raise ValueError(f"{key!r}: stores {seen[2]} and {i} hold different values")
                else:
                    seen[1].add(i)
            if max(cover.which(key)) != i:
                continue
            value = _unconsolidated(key, value)
            if value is None:
                continue
            mapping[key] = value
            origin[key] = i
            internal += [(i, k) for s in _used(held)
                         if (k := absolute[s].key) is not None and not k.startswith(RESERVED_PREFIX)]
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
    if url is not None:
        options["url"] = url
    return _build(mapping, sources, read_at_open=read_at_open, compressed=compressed,
                  bounds=bounds, template=stores[0] if stores else None, options=options)


def overlay(*stores: ReferenceStore, **options: Unpack[Options]) -> ReferenceStore:
    """One store answering each key from the last of `stores` whose bounds contain it.

    The result holds the key's value from that store, or nothing if that store holds none
    (inside its bounds, a missing key is absent: a chunk's fill value). Its bounds are
    every store's. A store's keys outside its own bounds are not carried.

    Keyword arguments set the result's options (`ReferenceStore`): by default its base
    URL is the stores' (pass `url=` when some are local and some remote), its `info` is
    empty, and its registry, policy, request cap and page size are the first store's.

    Raises ValueError when a value carried into the result is a reference through a key
    source (an internal reference) whose key the result takes from another store, or
    leaves out.
    """
    return _combine(stores, False, dict(options))  # type: ignore[arg-type]


def union(*stores: ReferenceStore, **options: Unpack[Options]) -> ReferenceStore:
    """`overlay`, where any two `stores` whose bounds share a key must answer it the same.

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
            raise ValueError(f"a key source names {s.key!r}, which is dropped")
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
    return list(meta["shape"]), list(meta["chunks"]), "v2", meta.get("dimension_separator", ".")


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
    returned with the same keys. The result's bounds are the store's, plus the added
    groups' keys when they were stated; derived otherwise.
    """
    found = nodes(store.mapping)
    if not found:
        return map_keys(store, lambda k: k, bounds=store.bounds if store.bounds_stated else None, **options)
    arrays = {p for p, kind in found.items() if kind == "array"}

    def valid(path: str) -> bool:
        return not any(a in arrays for a in ancestors(path)) if path else True

    keep: dict[str, Value] = {}
    for key, value in store.mapping.items():
        if key.startswith(RESERVED_PREFIX):
            keep[key] = value
            continue
        path, _, name = key.rpartition("/")
        if (name in MARKERS or name == ".zattrs") and path in found:
            if valid(path):
                keep[key] = value
            continue
        owner = next((p for p in ancestors(key) if p in found), None)
        if owner is None or (found[owner] == "array" and valid(owner)):
            keep[key] = value
    added: list[str] = []
    kept = {p for p in found if valid(p)}
    for path in sorted(kept):
        version = 3 if (f"{path}/" if path else "") + "zarr.json" in store.mapping else 2
        for a in ancestors(path):
            base = f"{a}/" if a else ""
            if a in found or base + "zarr.json" in keep or base + ".zgroup" in keep:
                continue
            key = base + ("zarr.json" if version == 3 else ".zgroup")
            keep[key] = (b'{"zarr_format":3,"node_type":"group","attributes":{}}' if version == 3
                         else b'{"zarr_format":2}')
            added.append(key)
    bounds = [*store.bounds, *(Key(k) for k in added)] if store.bounds_stated else None
    return _map_keys(store, lambda k: k if k in keep else None, bounds, {k: keep[k] for k in added},
                     added, dict(options), unconsolidate=True)  # type: ignore[arg-type]
