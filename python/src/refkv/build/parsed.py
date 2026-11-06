"""Reading an input for an output built with `refkv.build`: `read`, and the `Parsed` it returns, with
the IR's accessors that `refkv.zarrify.layouts.Base` had (design/proposals/profile-plugins.md,
"Reading" and "References and values").

In this first step `read` runs the whole `zarrify` path, so that sniffing, siblings, pins, the
reader policy and the object stores are exactly `zarrify`'s, and keeps the IR and the sources it
opened; the built-in output it also made is `project`'s. A parse that stops before the projection
comes with the plan."""

from __future__ import annotations

import bisect
import json
import os
from pathlib import Path
from typing import Any, Self, overload

from refkv import Range, ReferenceStore, Source, _sync
from refkv.ir import Ir
from refkv.ir.kind import Kind
from refkv.ir.store import StoreIrOutput
from refkv.policy import Policy
from refkv.reader import Registry, Resolver
from refkv.uri import path_reference
from refkv.zarrify import _zarrify_async
from refkv.zarrify.common import Output
from refkv.zarrify.store.urls import object_url

from .values import Part, Ref, Shared, Value

__all__ = ["Parsed", "read", "read_async"]

STRUCT, VALUE, DATA, DERIVED, ALIAS, GAP, OBJECT = (Kind.STRUCT, Kind.VALUE, Kind.DATA, Kind.DERIVED, Kind.ALIAS,
                                                    Kind.GAP, Kind.OBJECT)
MAX_BYTES = 64 << 20  # the default bound on what `Parsed.bytes` reads from sources


class Parsed:
    """An input as `read` read it: its profile, its IR, its sources (each url source with its size
    as the run found it), and the accessors that turn IR elements into values. It keeps what it
    needs to read its sources until it is closed (a context manager)."""

    def __init__(self, fmt: str, out: Output | StoreIrOutput, location: str, *, policy: Policy | None,
                 registry: Resolver, checksums: bool, max_bytes: int, registry_given: Registry | None = None) -> None:
        if out.ir is None:
            raise ValueError(f"{location}: its run kept no IR")
        self._fmt = fmt
        self._out = out
        self._location = location
        self.policy = policy
        self.registry = registry
        self.registry_given = registry_given  # what the caller gave read, which seal compares across inputs
        self.checksums = checksums
        self.max_bytes = max_bytes
        self._read = 0  # bytes `bytes` has read from sources
        self._ir = Ir(out.ir)
        entries = out.entries if isinstance(out, StoreIrOutput) else out.bytes_entries
        prop = json.loads(entries["zarr.json"])["attributes"]["refkv_zarrified"]
        self.profile: str = prop["profile"]
        self.version: int = prop["version"]
        if isinstance(out, StoreIrOutput):  # every object the run listed, by key
            self._objects = sorted(out.sizes, key=lambda k: k.encode())
            self.sources: tuple[Source, ...] = tuple(Source(url=object_url(out.url, k), size=out.sizes[k])
                                                     for k in self._objects)
        else:
            self._objects = []
            self.sources = (Source(url=out.url, size=out.size, etag=out.etag),)
        self._index: dict[int, tuple[list[int], list[tuple[int, int, int, int | None]]]] | None = None
        self._sources_read: tuple[Source, ...] | None = None
        self.closed = False

    # ---- the input

    @property
    def ir(self) -> Ir:
        return self._ir

    @property
    def description(self) -> None:
        """The format's description: None until it is specified (design/proposals/profile-plugins.md,
        "The description")."""
        return None

    @property
    def plain_file(self) -> bool:
        """Whether the input is one file read alone: not a store, and with one source."""
        return not self._objects and len(self.sources) == 1

    def close(self) -> None:
        """Releases what reads the sources; a closed `Parsed`'s values can still be sealed without
        checksums, since its sources' sizes and pins are known."""
        self._sources_read = None
        self.closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ---- elements

    def _element(self, i: object) -> int:
        if type(i) is not int or not 0 <= i < len(self._ir):
            raise KeyError(f"no element {i!r} in the IR")
        return i

    def _target(self, i: int) -> int:
        """`i`, or what it is an alias of."""
        seen = set()
        while self._ir.kind(i) == ALIAS and i not in seen:
            seen.add(i)
            t = self._ir.target(i)
            if t < 0:
                break
            i = t
        return i

    def members(self, i: int) -> list[tuple[int, int, int]]:
        """Element `i`'s data members (an alias's are its target's), each `(number, start, length)`."""
        numbers, starts, lengths, _ = self._ir.members(self._target(self._element(i)))
        return list(zip(numbers, starts, lengths, strict=True))

    def _member(self, i: int, member: object) -> tuple[int, int, int]:
        _, starts, lengths, forms = self._ir.members(self._target(i))
        if type(member) is not int or not 0 <= member < len(starts):
            raise KeyError(f"element {i} ({self._ir.path(i)!r}) has no data member {member!r}")
        return starts[member], lengths[member], forms[member]

    def form(self, i: int, member: int | None = None) -> dict[str, Any] | None:
        """The form of element `i` (or of its data member `member`), parsed, or None (spec/conventions.md
        §8.5 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#85-forms-and-recipes>)."""
        i = self._element(i)
        text = self._ir.form(i) if member is None else self._ir.forms()[self._member(i, member)[2]]
        return None if text is None else json.loads(text)

    def _object(self, i: int) -> int | None:
        if not self._ir.object_bases():
            return None
        at = i
        while at >= 0:
            if self._ir.kind(at) == OBJECT:
                return at
            at = self._ir.parent(at)
        return None

    def source_of(self, i: int) -> int:
        """The index in `sources` of the source that holds element `i`'s bytes: 0 for a file IR, its
        object's for a store IR (spec/conventions.md §8.9
        <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#89-stores>)."""
        o = self._object(self._target(self._element(i)))
        if o is None:
            if self._ir.object_bases():
                raise ValueError(f"{self._ir.path(i)!r} is in no object of the store")
            return 0
        key = self._ir.path(o)
        if key not in self._objects:
            raise ValueError(f"the object {key!r} is not among the sources the run listed")
        return self._objects.index(key)

    def _source_space(self, i: int) -> None:
        if self._ir.kind(i) == DERIVED or self._ir.space(i) != 0:
            raise ValueError(f"{self._ir.path(i)!r} is in a derived space (the inflated bytes of another element) "
                             "or derived: no range of a source reads it")

    def reference(self, i: int, member: int | None = None) -> Value:
        """The value that reads element `i`'s bytes (or its data member `member`'s) as the profile
        reads them: a `Ref` of the source that holds them, or the parts of its form's recipe, a
        shared string as `Shared`. An alias is read by its own extent, which lies within its
        target's. ValueError for an element that holds no bytes of a source, or a chunk whose form
        skips a filter."""
        i = self._element(i)
        path = self._ir.path(i)
        if member is not None:
            self._source_space(self._target(i))
            start, length, form_id = self._member(self._target(i), member)
            text: str | None = self._ir.forms()[form_id]
        elif self._ir.kind(i) == ALIAS:
            start, length = self._ir.extent(i)
            t = self._target(i)
            ts, tn = self._ir.extent(t)
            if t == i or not (ts <= start and start + length <= ts + tn):
                raise ValueError(f"{path!r} is an alias whose extent is not within its target's")
            text = self._ir.form(i) or self._ir.form(t)
        else:
            self._source_space(i)
            if self._ir.kind(i) not in (DATA, VALUE):
                raise ValueError(f"{path!r} is not data or a value: it holds no bytes of its own")
            start, length = self._ir.extent(i)
            text = self._ir.form(i)
        form = None if text is None else json.loads(text)
        codec = form.get("codec") if isinstance(form, dict) else None
        if isinstance(codec, dict) and codec.get("mask"):
            raise ValueError(f"{path!r}: its form skips filters (mask {codec['mask']}), so it decodes otherwise "
                             "than its siblings")
        source = self.source_of(i)
        recipe = form.get("recipe") if isinstance(form, dict) else None
        if not recipe:
            return Value((Ref(self, source, start, length),))
        shared = self._ir.shared()
        parts: list[Part] = []
        # the parts of spec/conventions.md §8.5 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#85-forms-and-recipes>
        for step in recipe:
            if step[0] == "src":
                o, n = int(step[1]), step[2]
                parts.append(Ref(self, source, start + o, length - o if n is None else int(n)))
            elif step[0] == "rows":
                o, stride, n, c = (int(x) for x in step[1:5])
                parts += [Ref(self, source, start + o + k * stride, n) for k in range(c)]
            elif step[0] == "shared":
                parts.append(Shared(bytes(shared[int(step[1])])))
            else:
                raise ValueError(f"{path!r}: its recipe has a part {step[0]!r} this refkv does not read")
        return Value(tuple(parts))

    def span(self, i: int, member: int | None = None) -> Ref:
        """The bytes element `i` (or its data member `member`) takes in its source, no recipe applied.
        ValueError for an element that holds no bytes of a source."""
        i = self._element(i)
        if member is not None:
            t = self._target(i)
            self._source_space(t)
            start, length, _ = self._member(t, member)
            return Ref(self, self.source_of(i), start, length)
        self._source_space(i)
        if self._ir.kind(i) not in (DATA, VALUE, ALIAS, GAP):
            raise ValueError(f"{self._ir.path(i)!r} is not data or a value: it holds no bytes of its own")
        start, length = self._ir.extent(i)
        return Ref(self, self.source_of(i), start, length)

    def gaps(self) -> list[Ref]:
        """The IR's gaps, bytes of a source no element claims, as ranges of their sources."""
        kinds = self._ir.columns()["kind"]
        return [self.span(i) for i in range(len(self._ir)) if kinds[i] == GAP and self._ir.space(i) == 0]

    def _space_index(self, source: int) -> tuple[list[int], list[tuple[int, int, int, int | None]]]:
        if self._index is None:
            ir = self._ir
            by: dict[int, list[tuple[int, int, int, int | None]]] = {}
            covered: set[tuple[int, int, int]] = set()
            kinds = ir.columns()["kind"]

            def source_or_none(i: int) -> int | None:
                try:
                    return self.source_of(i)
                except ValueError:
                    return None
            for i in range(len(ir)):
                if kinds[i] == DATA and ir.space(i) == 0 and (s := source_or_none(i)) is not None:
                    a, n = ir.extent(i)
                    by.setdefault(s, []).append((a, a + n, i, None))
                    covered.add((s, a, n))
            for i in range(len(ir)):
                if kinds[i] == STRUCT and ir.space(i) == 0 and (s := source_or_none(i)) is not None:
                    _, starts, lengths, _forms = ir.members(i)
                    for k, (a, n) in enumerate(zip(starts, lengths, strict=True)):
                        if (s, a, n) not in covered:
                            by.setdefault(s, []).append((a, a + n, i, k))
            self._index = {}
            for s, spans in by.items():
                spans.sort(key=lambda t: (t[0], t[1]))
                self._index[s] = ([t[0] for t in spans], spans)
        return self._index.get(source, ([], []))

    def element_at(self, source: int, offset: int) -> tuple[int, int | None] | None:
        """(element, member) holding byte `offset` of source `source`: the innermost data element
        (member None), else a struct's data member no element covers; None for a byte no data
        holds, and KeyError for a source `sources` does not have."""
        if type(source) is not int or not 0 <= source < len(self.sources):
            raise KeyError(f"no source {source!r} among the input's")
        starts, spans = self._space_index(source)
        k = bisect.bisect_right(starts, offset) - 1
        while k >= 0:
            s, e, i, member = spans[k]
            if s <= offset < e:
                return i, member
            if e <= offset and (k == 0 or spans[k - 1][1] <= offset):
                break
            k -= 1
        return None

    # ---- bytes

    def _readable(self) -> tuple[Source, ...]:
        """The sources as they are read: a store's objects as listed, and a file as `read` was
        given it, which may differ from the URL its output names."""
        if self.closed:
            raise ValueError("the input is closed")
        if self._sources_read is None:
            if self._objects:
                self._sources_read = self.sources
            else:
                local = path_reference(str(Path(self._location).absolute()))
                where = self._location if "://" in self._location else local
                self._sources_read = (Source(url=where, size=self.sources[0].size, etag=self.sources[0].etag),)
        return self._sources_read

    def read_ranges(self, ranges: list[tuple[int, int, int]]) -> list[bytes]:
        """The bytes of each range `(source, offset, length)` of the input's sources, read together
        under their pins and the reader policy, without counting against `max_bytes` (as `seal`
        reads them for checksums)."""
        for source, offset, length in ranges:
            if not (type(source) is int and 0 <= source < len(self.sources)) or offset < 0 or length < 0:
                raise KeyError(f"no range {(source, offset, length)!r} of the input's sources")
            size = self.sources[source].size
            if size is not None and offset + length > size:
                raise ValueError(f"bytes {offset} to {offset + length} of source {source}, past its {size}")
        mapping = {str(k): Range(s, o, n) for k, (s, o, n) in enumerate(ranges) if n}
        store = ReferenceStore(mapping, self._readable(), policy=self.policy, registry=self.registry)
        return [store.get(str(k)) or b"" if n else b"" for k, (_, _, n) in enumerate(ranges)]

    @overload
    def bytes(self, item: int | tuple[int, int, int], /) -> bytes: ...
    @overload
    def bytes(self, item: int | tuple[int, int, int], other: int | tuple[int, int, int], /,
              *more: int | tuple[int, int, int]) -> list[bytes]: ...
    def bytes(self, *items: int | tuple[int, int, int]) -> bytes | list[bytes]:
        """The bytes of each item, an element (by index) or a range `(source, offset, length)` (a
        gap's, say): an element's own extent, from the IR where the parser kept them and otherwise
        read through the input's sources. Several items are read together. What is read from the
        sources counts against `max_bytes`; ValueError past it, and for an element with no bytes
        of a source (a node, a derived element, one in a derived space)."""
        if not items:
            raise TypeError("bytes() of nothing")
        out: list[bytes | None] = []
        wanted: list[tuple[int, int, int]] = []
        for item in items:
            if isinstance(item, tuple):
                wanted.append(item)
                out.append(None)
                continue
            i = self._element(item)
            raw = self._ir.raw(i)
            if raw is not None and self._ir.kind(i) != ALIAS:
                out.append(bytes(raw))
                continue
            ref = self.span(i)
            wanted.append((ref.source, ref.offset, ref.length))
            out.append(None)
        total = sum(n for _, _, n in wanted)
        if self._read + total > self.max_bytes:
            raise ValueError(f"reading {total} more bytes would pass the input's max_bytes of {self.max_bytes}")
        got = iter(self.read_ranges(wanted))
        self._read += total
        done = [b if b is not None else next(got) for b in out]
        return done[0] if len(items) == 1 else done


async def read_async(location: str | os.PathLike[str], url: str | None = None, *, policy: Policy | None = None,
                     registry: Registry | None = None, checksums: bool = False,
                     max_bytes: int = MAX_BYTES) -> Parsed:
    """The input at `location` read as `zarrify` reads it (the same sniffing, siblings, pins, reader
    policy and object stores), as a `Parsed`. Raises `Rejected` as `zarrify` does, and ValueError
    for an input whose run keeps no IR (a store of DICOM files)."""
    location = os.fspath(location)
    resolver = Resolver(registry)
    fmt, out = await _zarrify_async(location, url, checksums=checksums, policy=policy, registry=resolver)
    if not isinstance(out, (Output, StoreIrOutput)) or out.ir is None:
        raise ValueError(f"{location}: its run keeps no IR, so refkv.build cannot read it")
    return Parsed(fmt, out, location, policy=policy, registry=resolver, checksums=checksums, max_bytes=max_bytes,
                  registry_given=registry)


def read(location: str | os.PathLike[str], url: str | None = None, *, policy: Policy | None = None,
         registry: Registry | None = None, checksums: bool = False, max_bytes: int = MAX_BYTES) -> Parsed:
    """`read_async`, the caller's thread waiting for it."""
    return _sync.run(read_async(location, url, policy=policy, registry=registry, checksums=checksums,
                                max_bytes=max_bytes))
