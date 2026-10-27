"""`PhkvStore`: a phkv store, refkv's perfect-hash binding (spec/bindings/phkv.md
<https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md>), read as a reference store:

* bytes keys      -> the value of the record (decoded, within the window asked for)
* reference keys  -> the concatenation of byte ranges of other objects

The container (its perfect hash, slots, records and pages) is read by the Rust core,
`refkv._core.PhkvStore`, which reads the store through the two callbacks this class gives it. A
lookup takes a few ranged reads: the end of the file when the store opens (the superblock, the
perfect hash and the page index), then a block of slots and a record. The Rust code is blocking,
so each call into it runs on a thread, and its reads run on the event loop, under the store's
`max_concurrency`, `Stats` and registry. A reference is resolved here, by the code every
reference store shares (`ReferenceReader`).
"""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import TYPE_CHECKING, ClassVar, TypedDict, cast, override

from refkv import _core
from refkv._sync import run
from refkv.errors import (
    ArchiveError,
    BodyError,
    EntryError,
    PayloadError,
    RefkvError,
    RequestError,
    ResolutionError,
)
from refkv.pb import Concat, Range, Reference, Source
from refkv.policy import Policy
from refkv.reader import (
    DEFAULT_CONCURRENCY,
    Kind,
    OffsetRequest,
    RangeRequest,
    ReferenceReader,
    Registry,
    Request,
    Stats,
    SuffixRequest,
    _abs_range,
)

MAX_PAYLOAD = 1 << 24
"""The longest reference payload a phkv store may hold (spec/bindings/phkv.md §19.3
<https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#193-reference-records>)."""

_ERRORS: dict[str, type[RefkvError]] = {
    "archive": ArchiveError,
    "entry": EntryError,
    "body": BodyError,
    "payload": PayloadError,
    "resolution": ResolutionError,
    "request": RequestError,
}

# The loop whose coroutines the Rust core's reads run on: the one that awaited the call. The
# value is copied into the thread the call runs on (asyncio.to_thread copies the context).
_loop: contextvars.ContextVar[asyncio.AbstractEventLoop] = contextvars.ContextVar("refkv_phkv_loop")

if TYPE_CHECKING:
    # closed (PEP 728), which typing.TypedDict takes only from Python 3.15
    class PhkvOptions(TypedDict, total=False, closed=True):
        """`PhkvStore`'s keyword arguments, which `refkv.phkv.open` and `refkv.open` pass on."""

        registry: Registry | None
        stats: Stats | None
        policy: Policy | None
        max_concurrency: int | None


def _error(e: _core.PhkvReadError) -> RefkvError:
    cls, message = e.args
    return _ERRORS.get(cls, RefkvError)(message)


def _mode(request: Request | None) -> tuple[int, int, int]:
    """`request` as `_core.PhkvStore.fetch` takes it: (mode, a, b), checked by `_abs_range`."""
    match request:
        case RangeRequest(start, end):
            return 1, start, end
        case OffsetRequest(start):
            return 2, start, 0
        case SuffixRequest(count):
            return 3, count, 0
        case _:
            return 0, 0, 0


def _reference(kind: int, payload: bytes) -> Reference:
    if len(payload) > MAX_PAYLOAD:
        raise PayloadError(f"a payload of {len(payload)} bytes, longer than 2^24")
    try:
        return Range.decode(payload) if kind == 1 else Concat.decode(payload)
    except ValueError as e:
        raise PayloadError(str(e)) from None


class PhkvStore(ReferenceReader):
    """A phkv store, opened as a read-only store.

    `refkv.phkv.open(url)` returns one, and so does `refkv.open(url, binding="phkv")`.
    `url` is a local path or an `http(s)` URL (or any URL a store in the `registry` serves). The
    first operation reads the end of the file; a lookup then reads a block of slots and a
    record, over a network a few requests for the first key and about one for each key after:

        import refkv

        store = refkv.phkv.open("oisst.phkv")
        store.get("zarr.json")  # bytes, from the file
        store.get("sst/c/0/0/0/0")  # a reference: bytes of a netCDF file

    The store reads like the `refkv.ReferenceStore` it was saved from (`save_phkv`): same
    values, same reader policy, same options. A phkv store reserves no names, so the hidden
    entries a vzip archive keeps under `__vz__/` are ordinary keys here.

    - `policy`: which sources may be resolved, and the reader's limits (spec §6.7
      <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>), as for
      `VZipStore`.
    - `registry`, `stats`, `max_concurrency`: as for every reference store
      (`ReferenceReader`).
    - `revision`: the spec revision the writer followed, if it said (spec §1.3
      <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#13-versioning>).
    - `metadata`: the entries of the store's METADATA section, by name.
    - `property(key, name)`: a key's property (spec §20
      <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#20-properties>), such as
      `"mtime"`, `"mode"` or `"media_type"`; None if the key has none.
    """

    def __init__(
        self,
        url: str,
        *,
        registry: Registry | None = None,
        stats: Stats | None = None,
        policy: Policy | None = None,
        max_concurrency: int | None = DEFAULT_CONCURRENCY,
    ) -> None:
        super().__init__(url, registry=registry, stats=stats, policy=policy, max_concurrency=max_concurrency)
        self._core: _core.PhkvStore | None = None
        self.revision: int | None = None
        self.metadata: dict[str, bytes] = {}
        self._size = 0
        self._tail, self._tail_start = b"", 0  # the end of the file, read once

    def __eq__(self, other: object) -> bool:
        return isinstance(other, PhkvStore) and other.url == self.url

    __hash__: ClassVar[None] = None  # unhashable, as a class that defines __eq__ alone is

    def __repr__(self) -> str:
        return f"PhkvStore({self.url!r})"

    # ------------------------------------------------------------ the Rust core's reads

    def _wait[T](self, coro: Coroutine[object, object, T]) -> T:
        """The result of a coroutine run on the loop that awaited the call, from the thread the
        call runs on."""
        return asyncio.run_coroutine_threadsafe(coro, _loop.get()).result()

    async def _suffix(self) -> None:
        """Read the end of the file, with its size: the one request every open begins with."""
        store, path = self.resolver.resolve(self.url, allow_file_host=True, policy=self.policy,
                                            executor=self._executor())
        async with self._limit():
            res = await store.get_async(path, options={"range": {"suffix": _core.PhkvStore.tail()}})
            tail = bytes(await res.buffer_async())
        self._size = res.meta["size"]
        self._tail, self._tail_start = tail, self._size - len(tail)
        self.stats.record(self.url, self._tail_start, len(tail), external=False)

    def _size_of(self) -> int:
        self._wait(self._suffix())
        return self._size

    def _read_at(self, offset: int, length: int) -> bytes:
        if length == 0:
            return b""
        if offset >= self._tail_start:
            data = self._tail[offset - self._tail_start : offset - self._tail_start + length]
        else:
            data = self._wait(self._read(self.url, offset, offset + length, external=False))
        if len(data) != length:
            raise ValueError(f"{self.url} is shorter than {offset + length} bytes")
        return data

    async def _blocking[T](self, f: Callable[..., T], *args: object) -> T:
        """`f(*args)`, which the Rust core runs, on a thread; its reads run here. A failure of the
        core is an error of spec §6.4 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#64-errors>."""
        token = _loop.set(asyncio.get_running_loop())
        try:
            return await asyncio.to_thread(f, *args)
        except _core.PhkvReadError as e:
            raise _error(e) from None
        finally:
            _loop.reset(token)

    # ------------------------------------------------------------ opening

    @override
    async def _open(self) -> None:
        if self._is_open:
            return
        try:
            await self._blocking(self._open_core)
        except ArchiveError:
            raise
        except Exception as e:  # noqa: BLE001 - spec §6.1 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#61-opening>: every open failure
            raise ArchiveError(f"{type(e).__name__}: {e}") from None
        await super()._open()

    def _open_core(self) -> None:
        core = _core.PhkvStore(self._size_of, self._read_at, self.policy.max_sources, self.policy.max_format_entry)
        self._sources = [Source.decode(s) for s in core.sources()]
        self.revision = core.revision
        self.metadata = dict(core.metadata())
        self._core = core

    @property
    def _store(self) -> _core.PhkvStore:
        return cast("_core.PhkvStore", self._core)  # set by _open, which every operation awaits first

    # ------------------------------------------------------------ the operations

    @override
    async def _key_bytes(self, key: str, start: int, end: int) -> bytes:
        return await self._blocking(self._store.key_bytes, key, start, end)

    @override
    async def classify_async(self, key: str) -> Kind:
        await self._ensure_open()
        return await self._blocking(self._store.classify, key) or "missing"

    @override
    async def get_async(self, key: str, request: Request | None = None) -> bytes | None:
        await self._ensure_open()
        _abs_range(0, request)  # 1. request errors, even for missing keys
        found = await self._blocking(self._store.fetch, key, *_mode(request))
        if found is None:
            return None
        if found[0] == "bytes":
            return found[1]
        ref = _reference(found[1], found[2])
        start, end = _abs_range(ref.size, request)
        return await self._ref_bytes(ref, start, end)

    @override
    async def raw_async(self, key: str) -> bytes | None:
        """What the store holds under `key`: a bytes value's stored bytes, not decoded, and a
        reference's payload (spec/bindings/phkv.md §22.2
        <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#222-operations>)."""
        await self._ensure_open()
        return await self._blocking(self._store.raw, key)

    @override
    async def list_async(self, prefix: str = "") -> AsyncIterator[str]:
        await self._ensure_open()
        for key in await self._blocking(self._store.list, prefix):
            yield key

    @override
    async def list_dir_async(self, prefix: str = "") -> AsyncIterator[str]:
        prefix = prefix.rstrip("/")
        prefix = prefix + "/" if prefix else ""
        seen: set[str] = set()
        async for k in self.list_async(prefix):
            child = k[len(prefix) :].split("/", 1)[0]
            if child not in seen:
                seen.add(child)
                yield child

    async def property_async(self, key: str, name: str) -> int | str | bytes | None:
        """`property`, as a coroutine."""
        await self._ensure_open()
        return await self._blocking(self._store.property, key, name)

    def property(self, key: str, name: str) -> int | str | bytes | None:
        """The property `name` of `key` (spec/bindings/phkv.md §22.2
        <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#222-operations>), or
        None if the key is missing or has no value for it. A malformed PROPERTY section is an
        error."""
        return run(self.property_async(key, name))


__all__ = ["PhkvStore"]
