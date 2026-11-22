"""Reading an input for an output built with `refkv.build`: `read`, and the `Parsed` it returns, with
the accessors that turn the IR's elements into values (the design of d-v-b/refkv#278, "Reading" and
"References and values"). A `Parsed` is what a plugin layout is given of each input.

`read` runs `zarrify`'s own parse, so that sniffing, siblings, pins, the reader policy and the
object stores are exactly `zarrify`'s, and stops there: it keeps the IR and the sources it
opened, and makes no output. The built-in output is `project`'s, made when it is asked for, with
the reads that output alone needs (the ranges a projection copies or samples)."""

from __future__ import annotations

import asyncio
import bisect
import concurrent.futures
import dataclasses
import json
import os
import threading
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self, overload

import refkv.ir  # its Ir, read at call time: refkv.ir imports refkv.zarrify, which imports this module (a cycle)
import refkv.zarrify as _zarrify  # its parse, read at call time: refkv.zarrify imports this module (a cycle)
from refkv import Range, ReferenceStore, Source, _core, _sync
from refkv.errors import RefkvError
from refkv.ir.kind import Kind
from refkv.ir.store import StoreIrOutput, StoreParse
from refkv.policy import Policy
from refkv.reader import Registry, Resolver
from refkv.uri import path_reference
from refkv.zarrify.common import PROFILES, Output, reading_own_files
from refkv.zarrify.layouts import MERGE_GAP
from refkv.zarrify.store.urls import object_url

from .values import Part, Ref, Shared, Value

if TYPE_CHECKING:
    from refkv.ir import Ir
    from refkv.zarrify import _Parse
    from refkv.zarrify.store.beside import Scope

__all__ = ["NoIrError", "Parsed", "read", "read_async"]

STRUCT, VALUE, DATA, DERIVED, ALIAS, GAP, OBJECT = (Kind.STRUCT, Kind.VALUE, Kind.DATA, Kind.DERIVED, Kind.ALIAS,
                                                    Kind.GAP, Kind.OBJECT)
MAX_BYTES = 64 << 20  # the default bound on what `Parsed.bytes` reads from sources


class NoIrError(ValueError):
    """An input whose run keeps no IR, so that `read` has nothing to give: a store of DICOM
    files, which is joined from its files' outputs
    (spec/zarrify/dicom.md §9 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#9-the-store-form>)."""


class Parsed:
    """An input as `read` read it: its profile, its IR, its sources (each url source with its size
    as the run found it), and the accessors that turn IR elements into values.

    It holds the input open (a local file is mapped) until it is closed, so that the built-in
    output can be made of it (`refkv.build.project`). Use it as a context manager, `with
    read(...) as p:` or `async with await read_async(...) as p:`, or call `close()`: one that is
    collected unclosed releases the input then, with a `ResourceWarning`."""

    def __init__(self, parse: _Parse, location: str, *, policy: Policy | None,
                 registry: Resolver, checksums: bool, max_bytes: int, registry_given: Registry | None = None,
                 read_base: str | None = None) -> None:
        self._fmt = parse.fmt
        # the parse keeps what the built-in output reads through (a file's transport, a local
        # file's mapping among them) until this is closed
        self._parse: _Parse | None = parse
        # the built-in output, once asked for: made by the first to ask, which the others wait for
        self._made: concurrent.futures.Future[Output | StoreIrOutput] | None = None
        self._making = threading.Lock()
        # the last error of a read of the input refkv made itself (a source's bytes, the built-in
        # output's reads), kept so that `refkv.build.apply` can tell it from a plugin's own
        self._own_error: BaseException | None = None
        run = parse.parse
        self._location = location
        #: the URL an output names the input by: the one `read` was given, else where it read
        self.url: str = run.url
        self.policy = policy
        self.registry = registry
        self.registry_given = registry_given
        # a store's objects are read under this URL, where `read` opened them (the URL the output names
        # them by, `out.url`, may be another, given as `url`)
        # where the sources are read, made absolute now: a relative path is the working directory's
        # when `read` ran, not when the bytes are read
        cwd = path_reference(str(Path.cwd())) + "/"
        base = read_base if read_base is not None else run.url
        self._read_base = base if "://" in base or base.startswith("/") or _drive(base) else cwd + base
        self._file_place = location if "://" in location else path_reference(str(Path(location).absolute()))
        self.checksums = checksums
        self.max_bytes = max_bytes
        self._read = 0  # bytes `bytes` has read from sources
        self._ir = refkv.ir.Ir(run.ir)
        self._mirrored = run.mirrored  # what makes the IR mirror, kept past `close`
        self._fits: _core.Fits | None = getattr(run, "fits", None)  # a FITS file's IR and description
        self._hdf5: _core.Hdf5 | None = getattr(run, "hdf5", None)  # an HDF5 input's IR and description
        self._image: _core.OmeImage | None = getattr(run, "image", None)  # a TIFF's (or IMS file's) IR and description
        self._netcdf3: _core.Netcdf3 | None = getattr(run, "netcdf3", None)  # a netCDF3 file's IR and description
        self._envi: _core.Envi | None = getattr(run, "envi", None)  # an ENVI raster's description
        self._zarr: _core.Zarr | None = getattr(run, "zarr", None)  # a Zarr store's IR and description
        self._description: dict[str, Any] | None = run.description
        # the profile the run decided, and the version of its convention, as an output's root
        # declares them
        self.profile: str = parse.fmt
        self.version: int = PROFILES[parse.fmt][1]
        if isinstance(run, StoreParse):  # every object the run listed, by key
            self._objects = sorted(run.sizes, key=lambda k: k.encode())
            self.sources: tuple[Source, ...] = tuple(Source(url=object_url(run.url, k), size=run.sizes[k])
                                                     for k in self._objects)
        else:
            self._objects = []
            self.sources = (Source(url=run.url, size=run.size, etag=run.etag),)
        self._index: dict[int, tuple[list[int], list[tuple[int, int, int, int | None]]]] | None = None
        self._sources_read: tuple[Source, ...] | None = None
        self.closed = False

    # ---- the input

    @property
    def ir(self) -> Ir:
        return self._ir

    @property
    def description(self) -> dict[str, Any] | None:
        """The profile's description of the input: what its parser found of the source, as the
        profile's document specifies it (the design of d-v-b/refkv#278, "The description"), a
        copy the caller may change. None for a profile that has none yet, and for a store of a
        format whose parser returns none yet (HDF5 files beside each other).

        Each profile that returns one specifies it in its own document, in the section of Part 2
        titled "The description" (`spec/zarrify/<profile>.md`, with its JSON Schema beside it
        where it has one, `spec/zarrify/<profile>/description.json`);
        python/tests/test_description_documented.py checks that every such profile has that
        section."""
        return None if self._description is None else json.loads(json.dumps(self._description))

    @property
    def plain_file(self) -> bool:
        """Whether the input is one file read alone: not a store, and with one source."""
        return not self._objects and len(self.sources) == 1

    def close(self) -> None:
        """Releases what reads the sources; a closed `Parsed`'s values can still be sealed without
        checksums, since its sources' sizes and pins are known. Its built-in output (`project`) is
        there if it was asked for before, and is not made after."""
        self._sources_read = None
        parse, self._parse = self._parse, None
        if parse is not None:
            parse.close()
        self.closed = True

    async def _output_async(self) -> Output | StoreIrOutput:
        """The built-in output of the input, made once (`refkv.build.project`): an output only
        built from, so not held to the payload limit with checksums, which `seal` holds what is
        written to.

        It is made by a task of its own on refkv's loop, which the first to ask starts and every
        caller waits for, from any thread or loop: so one output is made, one caller at a time
        uses the run, and a caller that is cancelled stops waiting without stopping the work or
        the others. One that failed is made again when next asked for, and those waiting for it
        get its error."""
        return await asyncio.wrap_future(self._output())

    def _output(self) -> concurrent.futures.Future[Output | StoreIrOutput]:
        """The future of the built-in output, its making started if it was not."""
        with self._making:
            if self._made is not None:
                return self._made
            if self._parse is None:
                raise ValueError(f"{self._location}: closed before its built-in output was asked for")
            # a future of its own, running: a waiter's cancellation, which asyncio passes on to
            # the future it waits for, neither cancels it nor reaches the task
            made: concurrent.futures.Future[Output | StoreIrOutput] = concurrent.futures.Future()
            made.set_running_or_notify_cancel()
            self._made = made
            task = _sync.submit(self._make(self._parse))

        def settle(done: concurrent.futures.Future[Output | StoreIrOutput]) -> None:
            error = concurrent.futures.CancelledError() if done.cancelled() else done.exception()
            if error is None:
                made.set_result(done.result())
                return
            with self._making:
                self._made = None
            made.set_exception(error)

        task.add_done_callback(settle)
        return made

    async def _make(self, parse: _Parse) -> Output | StoreIrOutput:
        out = await parse.output_async(written=False)
        # the run's checksum function holds what read the input: `Parsed` reads its sources
        # itself, and `seal` checksums them
        out.checksum = None
        if isinstance(out, Output):  # the pin, after the output's own reads
            self.sources = (dataclasses.replace(self.sources[0], etag=out.etag),)
            self._sources_read = None
        return out

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        self.close()

    def __del__(self, _warn: Callable[..., None] = warnings.warn) -> None:
        # `closed` is unset where __init__ raised before it held anything
        if not getattr(self, "closed", True):
            _warn(f"unclosed refkv.build.Parsed of {self._location!r}", ResourceWarning, stacklevel=2, source=self)
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

    def _form_text(self, form_id: int) -> str | None:
        """A form by its id, or None for the id of no form (the core's u32::MAX)."""
        forms = self._ir.forms()
        return forms[form_id] if 0 <= form_id < len(forms) else None

    def form(self, i: int, member: int | None = None) -> dict[str, Any] | None:
        """The form of element `i` (or of its data member `member`), parsed, or None (spec/conventions.md
        §8.5 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#85-forms-and-recipes>)."""
        i = self._element(i)
        text = self._ir.form(i) if member is None else self._form_text(self._member(i, member)[2])
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
        <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#89-stores>). ValueError for an
        element of a store IR in no object (its root, say), or in an object the run did not list."""
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
            text: str | None = self._form_text(form_id)
        elif self._ir.kind(i) == ALIAS:
            start, length = self._alias_extent(i, (DATA, VALUE))
            t = self._target(i)
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
        if self._ir.kind(i) == ALIAS:
            start, length = self._alias_extent(i, (DATA, VALUE, GAP))
            return Ref(self, self.source_of(i), start, length)
        self._source_space(i)
        if self._ir.kind(i) not in (DATA, VALUE, GAP):
            raise ValueError(f"{self._ir.path(i)!r} is not data or a value: it holds no bytes of its own")
        start, length = self._ir.extent(i)
        return Ref(self, self.source_of(i), start, length)

    def encoded(self, i: int) -> Ref:
        """The bytes of its source that a derived element `i` is derived from: a deflated archive
        member's stream, a compressed record. They are not the bytes the elements in its space
        are ranges of (`ir.space(j)` is `i` for such an element `j`): those exist only once these
        are decoded, by the transform the element's form names. ValueError for an element that is
        not derived from bytes of a source."""
        i = self._element(i)
        if self._ir.kind(i) != DERIVED or self._ir.space(i) != 0:
            raise ValueError(f"{self._ir.path(i)!r} is not an element derived from bytes of a source")
        start, length = self._ir.extent(i)
        return Ref(self, self.source_of(i), start, length)

    def _alias_extent(self, i: int, kinds: tuple[Kind, ...]) -> tuple[int, int]:
        """An alias's own extent, which lies within its target's, a target of one of `kinds` in a
        source's space: ValueError otherwise (an alias of a struct, which holds no bytes of its own)."""
        path = self._ir.path(i)
        t = self._target(i)
        if t == i or self._ir.kind(t) not in kinds:
            raise ValueError(f"{path!r} is an alias of {self._ir.path(t)!r}, which holds no bytes of its own")
        self._source_space(t)
        start, length = self._ir.extent(i)
        ts, tn = self._ir.extent(t)
        if not (ts <= start and start + length <= ts + tn):
            raise ValueError(f"{path!r} is an alias whose extent is not within its target's")
        return start, length

    def gaps(self) -> list[Ref]:
        """The IR's gaps, bytes of a source no element claims, as ranges of their sources."""
        kinds = self._ir.columns()["kind"]
        return [self.span(i) for i in range(len(self._ir)) if kinds[i] == GAP and self._ir.space(i) == 0]

    def _space_index(self, source: int) -> tuple[list[int], list[tuple[int, int, int, int | None]]]:
        if self._index is None:
            self._reach: dict[int, list[int]] = {}
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
                spans.sort(key=lambda t: (t[0], -t[1]))  # the inner of two spans of one start last
                self._index[s] = ([t[0] for t in spans], spans)
                ends: list[int] = []
                top = -1
                for t in spans:  # the furthest end of any span at or before each one, for element_at
                    top = max(top, t[1])
                    ends.append(top)
                self._reach[s] = ends
        return self._index.get(source, ([], []))

    def element_at(self, source: int, offset: int) -> tuple[int, int | None] | None:
        """(element, member) holding byte `offset` of source `source`: the innermost data element
        (member None), else a struct's data member no element covers; None for a byte no data
        holds, and KeyError for a source `sources` does not have."""
        if type(source) is not int or not 0 <= source < len(self.sources):
            raise KeyError(f"no source {source!r} among the input's")
        starts, spans = self._space_index(source)
        reach = self._reach.get(source, [])
        k = bisect.bisect_right(starts, offset) - 1
        while k >= 0 and reach[k] > offset:  # some span at or before k may still hold the byte
            s, e, i, member = spans[k]
            if s <= offset < e:
                return i, member
            k -= 1
        return None

    # ---- bytes

    def _read_place(self, source: int) -> str:
        """Where source `source` is read from: a store's object at its URL, a file as `read` was
        given it, which may differ from the URL its output names."""
        if self._objects:
            return object_url(self._read_base, self._objects[source])
        return self._file_place

    def _readable(self) -> tuple[Source, ...]:
        """The sources as they are read (`_read_place`), with their pins."""
        if self.closed:
            raise ValueError("the input is closed")
        if self._sources_read is None:
            if self._objects:
                self._sources_read = tuple(Source(url=self._read_place(k), size=s.size, etag=s.etag)
                                           for k, s in enumerate(self.sources))
            else:
                self._sources_read = (Source(url=self._read_place(0), size=self.sources[0].size,
                                             etag=self.sources[0].etag),)
        return self._sources_read

    def _read_ranges(self, ranges: list[tuple[int, int, int]]) -> list[bytes]:
        """The bytes of each range `(source, offset, length)` of the input's sources, read together
        under their pins and the reader policy, without counting against `max_bytes` (as `seal`
        reads them for checksums)."""
        for source, offset, length in ranges:
            if not (type(source) is int and 0 <= source < len(self.sources)) or offset < 0 or length < 0:
                raise KeyError(f"no range {(source, offset, length)!r} of the input's sources")
            size = self.sources[source].size
            if size is not None and offset + length > size:
                raise ValueError(f"bytes {offset} to {offset + length} of source {source}, past its {size}")
        runs = _runs(ranges, (self.policy or Policy()).max_coalesce_gap)  # as the reader merges them: each read once
        mapping = {str(k): Range(src, a, b - a) for k, (src, a, b) in enumerate(runs)}
        # the input's own bytes, read one run a get: a reader's limits on a `get` (rule 5) are not
        # build's, and `bytes` bounds what it reads by `max_bytes`; `seal`'s checksums read the
        # ranges its values name, however many
        rules = reading_own_files(self.policy, self._readable())  # its own files, as zarrify's store reads them
        longest = max((b - a for _, a, b in runs), default=0)
        policy = dataclasses.replace(rules, max_get_bytes=max(rules.max_get_bytes, longest),
                                     max_reads=max(rules.max_reads, 1))
        store = ReferenceStore(mapping, self._readable(), policy=policy, registry=self.registry)

        async def each() -> list[bytes | None]:  # together
            return list(await asyncio.gather(*(store.get_async(k) for k in mapping)))
        try:
            got = [b or b"" for b in _sync.run(each())]
        except (OSError, RefkvError) as e:  # the read failed, or the policy refused it
            self._own_error = e
            raise
        except RuntimeError as e:  # called on refkv's own event loop, which this blocking read would stall
            raise RuntimeError("Parsed.bytes and seal's checksums block: call them from a thread, not from "
                               "refkv's event loop") from e
        out = []
        for src, offset, length in ranges:
            if not length:
                out.append(b"")
                continue
            k = next(k for k, (s2, a, b) in enumerate(runs) if s2 == src and a <= offset and offset + length <= b)
            start = offset - runs[k][1]
            out.append(got[k][start:start + length])
        return out

    @overload
    def bytes(self, item: int | tuple[int, int, int], /) -> bytes: ...
    @overload
    def bytes(self, item: int | tuple[int, int, int], other: int | tuple[int, int, int], /,
              *more: int | tuple[int, int, int]) -> list[bytes]: ...
    def bytes(self, *items: int | tuple[int, int, int]) -> bytes | list[bytes]:
        """The bytes of each item, an element (by index) or a range `(source, offset, length)` (a
        gap's, say): an element's own extent, from the IR where the parser kept them and otherwise
        read through the input's sources. An element in a derived space gives its bytes there (the
        inflated stream's) where the parser kept them, and a ValueError where it kept none or kept
        a decoded value of another length (`ir.value` gives that), since no range of a source holds
        them. Several items are read together. What is read from
        the sources counts against `max_bytes`; ValueError past it, and for an element with no
        bytes of its own (a node, a derived element); TypeError for a range that is not three ints,
        and KeyError for one of no source of the input, or of a negative offset or length."""
        if not items:
            raise TypeError("bytes() of nothing")
        out: list[bytes | None] = []
        wanted: list[tuple[int, int, int]] = []
        for item in items:
            if isinstance(item, tuple):
                if len(item) != 3 or not all(type(x) is int for x in item):
                    raise TypeError(f"a range is (source, offset, length), each an int, not {item!r}")
                wanted.append(item)
                out.append(None)
                continue
            i = self._element(item)
            raw = self._ir.raw(i)
            if raw is not None and self._ir.kind(i) != ALIAS and len(raw) == self._ir.extent(i)[1]:
                out.append(bytes(raw))  # the element's own bytes, as the parser kept them
                continue  # (bytes of another length are a decoded value, ir.value's, not the element's)
            ref = self.span(i)
            wanted.append((ref.source, ref.offset, ref.length))
            out.append(None)
        total = _fetched(wanted, (self.policy or Policy()).max_coalesce_gap)
        if self._read + total > self.max_bytes:
            raise ValueError(f"reading {total} more bytes (ranges as the reader merges them) would pass the input's "
                             f"max_bytes of {self.max_bytes}")
        got = iter(self._read_ranges(wanted))
        self._read += total
        done = [b if b is not None else next(got) for b in out]
        return done[0] if len(items) == 1 else done


def _drive(ref: str) -> bool:
    """Whether a URI reference is a Windows path's (`C:/...`), which is absolute."""
    return len(ref) > 1 and ref[1] == ":" and ref[0].isalpha()


def _runs(ranges: list[tuple[int, int, int]], gap: int = MERGE_GAP) -> list[tuple[int, int, int]]:
    """`ranges` of each source at most `gap` apart (the input's reader policy's coalescing gap)
    merged into runs `(source, start, end)`, overlapping and repeated ranges once, the bytes between
    them included: what is read."""
    by: dict[int, list[tuple[int, int]]] = {}
    for source, offset, length in ranges:
        if length > 0:
            by.setdefault(source, []).append((offset, offset + length))
    out = []
    for source, spans in sorted(by.items()):
        spans.sort()
        start, end = spans[0]
        for a, b in spans[1:]:
            if a > end + gap:
                out.append((source, start, end))
                start, end = a, b
            else:
                end = max(end, b)
        out.append((source, start, end))
    return out


def _fetched(ranges: list[tuple[int, int, int]], gap: int = MERGE_GAP) -> int:
    """The bytes reading `ranges` together reads (`_runs`)."""
    return sum(b - a for _, a, b in _runs(ranges, gap))


async def read_async(location: str | os.PathLike[str], url: str | None = None, *, policy: Policy | None = None,
                     registry: Registry | None = None, checksums: bool = False,
                     max_bytes: int = MAX_BYTES, scope: Scope | None = None) -> Parsed:
    """The input at `location` read as `zarrify` reads it (the same sniffing, siblings, pins, reader
    policy and object stores), as a `Parsed`. `scope` is `zarrify`'s: what a file input may read
    besides itself. Raises `Rejected` as `zarrify` does, and `NoIrError` (a ValueError)
    for an input whose run keeps no IR (a store of DICOM files)."""
    location = os.fspath(location)
    resolver = Resolver(registry)
    # the built-in output, if asked for, is only built from: `seal` holds what is written to the limits
    parse = await _zarrify._parse_async(location, url, checksums=checksums, policy=policy,  # noqa: SLF001
                                        registry=resolver, written=False, scope=scope)
    if not isinstance(parse, _zarrify._Parse):  # noqa: SLF001
        raise NoIrError(f"{location}: its run keeps no IR, so refkv.build cannot read it")
    # a `file:` URL, read as its path
    return await parsed_async(parse, _zarrify.local_path(location, policy), url, policy=policy, registry=registry,
                              resolver=resolver, checksums=checksums, max_bytes=max_bytes, scope=scope)


async def parsed_async(parse: _Parse, location: str, url: str | None, *, policy: Policy | None,
                       registry: Registry | None, resolver: Resolver, checksums: bool,
                       max_bytes: int = MAX_BYTES, scope: Scope | None = None) -> Parsed:
    """The `Parsed` of a parse `refkv.zarrify` made of the input at `location` (named `url`),
    which it closes with it: for `read`, and for `zarrify` with a plugin layout, which parses
    with its own options. `scope` is the scope the input was parsed with."""
    run = parse.parse
    read_base = None
    try:
        if isinstance(run, StoreParse) and url is not None and _zarrify.is_store(location):
            # a store's objects lie under it, where `read` opened them; the output names them under `url`
            read_base = location if "://" in location else path_reference(os.path.abspath(location))  # noqa: PTH100, ASYNC240 (no I/O: the path made absolute)
            read_base = read_base if read_base.endswith("/") else read_base + "/"
        elif isinstance(run, StoreParse) and url is not None:
            # a file read as a store, its objects found beside it: read again as `read` opened them
            local = await _zarrify._parse_async(location, None, policy=policy, registry=resolver,  # noqa: SLF001
                                                fmt=parse.fmt, scope=scope)
            there = local.parse if isinstance(local, _zarrify._Parse) else None  # noqa: SLF001
            if isinstance(local, _zarrify._Parse):  # noqa: SLF001
                local.close()  # read for where its objects lie, and their sizes
            if not isinstance(there, StoreParse) or sorted(there.sizes.items()) != sorted(run.sizes.items()):
                raise ValueError(f"{location}: its objects, read at {location} and named under {url}, differ")  # noqa: TRY301
            read_base = there.url
        return Parsed(parse, location, policy=policy, registry=resolver, checksums=checksums, max_bytes=max_bytes,
                      registry_given=registry, read_base=read_base)
    except BaseException:
        parse.close()
        raise


def read(location: str | os.PathLike[str], url: str | None = None, *, policy: Policy | None = None,
         registry: Registry | None = None, checksums: bool = False, max_bytes: int = MAX_BYTES,
         scope: Scope | None = None) -> Parsed:
    """`read_async`, the caller's thread waiting for it."""
    return _sync.run(read_async(location, url, policy=policy, registry=registry, checksums=checksums,
                                max_bytes=max_bytes, scope=scope))
