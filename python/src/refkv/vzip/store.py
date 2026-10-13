"""`VZipStore`: a vzip archive, the store kept in one ZIP file (Part 2
<https://github.com/d-v-b/refkv/blob/main/spec/archive.md#part-2-the-zip-binding>),
read as a reference store:

* bytes keys      -> the body of the zip entry
* reference keys  -> the concatenation of byte ranges of other objects

`VZipStore(url, resolve=False)` behaves like a plain zip reader: reference
entries return their serialized `Range`/`Concat` payload. `resolve=True` (the default)
follows references with additional range requests.
"""

from __future__ import annotations

import asyncio
import bisect
from collections.abc import AsyncIterator, Iterable

from refkv.errors import (
    ArchiveError,
    BodyError,
    EntryError,
    PayloadError,
    ResolutionError,
    RefkvError,
)
from refkv.pb import decode_cd_index
from refkv.policy import Policy
from refkv.reader import Stats, Request, Kind, _abs_range, DEFAULT_CONCURRENCY, ReferenceReader
from refkv.vzip.archive import (
    LFH_SIZE,
    RESERVED_PREFIX,
    TAIL_GUESS,
    INDEX_KEY,
    SOURCES_KEY,
    Entry,
    parse_central_directory,
    parse_local_header,
    InflateLimitError,
    inflate_clean,
    parse_tail,
    read_table,
)


def _check_index(pages, pinned, cd_size: int, file_size: int) -> None:
    """Spec §12.2 <https://github.com/d-v-b/refkv/blob/main/spec/archive.md#122-lookup>: a malformed
    page index is an archive error."""
    pos, prev = 0, None
    for p in pages:
        if not p.first_key:
            raise ArchiveError("a page has an empty first_key")
        if p.length == 0 or p.offset != pos or p.offset + p.length > cd_size:
            raise ArchiveError("pages are not contiguous, empty, or outside the directory")
        if prev is not None and p.first_key.encode() <= prev.encode():
            raise ArchiveError("page first_key values do not strictly increase")
        pos, prev = p.offset + p.length, p.first_key
    seen = set()
    for e in pinned:
        if not e.key:
            raise ArchiveError("a pinned key is empty")
        if e.key in seen or e.key in (SOURCES_KEY, INDEX_KEY):
            raise ArchiveError(f"pinned key {e.key!r} is duplicated or a format entry")
        if e.method not in (0, 8):
            raise ArchiveError(f"pinned {e.key!r} has method {e.method}")
        if e.method != 0 and (e.size >= 0xFFFFFFFF or e.csize >= 0xFFFFFFFF):
            raise ArchiveError(f"pinned {e.key!r} is large and not STORED (spec §12.2 <https://github.com/d-v-b/refkv/blob/main/spec/archive.md#122-lookup>)")
        if e.data_offset + e.csize > file_size:
            raise ArchiveError(f"pinned {e.key!r} lies outside the file")
        seen.add(e.key)


class VZipStore(ReferenceReader):
    """A vzip archive, opened as a read-only store.

    `refkv.open(url)` returns one; so does `VZipStore(url)`. `url` is a local
    path or an `http(s)` URL (or any URL a store in the `registry` serves). The
    first operation reads the archive's tail, and with it the metadata documents
    a reader reads first; a reference's value is then read from the source it
    names, such as the netCDF file it came from:

        import refkv

        store = refkv.open("oisst.vzip")
        store.get("zarr.json")  # bytes, from the archive
        store.get("sst/c/0/0/0/0")  # a reference: bytes of a netCDF file

    `refkv.zarr.open(url)` opens it as a zarr store, for zarr and xarray:

        ds = xr.open_zarr(refkv.zarr.open("oisst.vzip"))
        ds.sst.isel(time=0).mean().compute()  # reads one chunk of a netCDF file

    The store reads like the `refkv.ReferenceStore` it was saved from: same
    values, same reader policy, same options.

    - `resolve`: True (the default) follows references; False gives the raw
      view (spec §13.2
      <https://github.com/d-v-b/refkv/blob/main/spec/archive.md#132-operations>), in which a
      reference key's value is its serialized `Range` or `Concat`, as a ZIP
      tool sees it.
    - `policy`: which sources may be resolved, and the reader's limits (spec
      §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/archive.md#67-reader-policy>). The
      default, `Policy()`, lets every archive read `http` and `https` sources on
      public hosts, not through a proxy, and a local archive also read `file`
      sources; it refuses every other scheme, and loopback, private and special
      addresses.
    - `registry`, `stats`, `max_concurrency`: as for every reference store
      (`ReferenceReader`): the application's object stores, such as the
      `ObjectStoreRegistry` given to VirtualiZarr; a count of the requests;
      and the most requests in flight, to the sources and to the archive (8
      by default, None for no cap).
    """

    def __init__(
        self,
        url: str,
        *,
        resolve: bool = True,
        registry=None,
        stats: Stats | None = None,
        policy: Policy | None = None,
        max_concurrency: int | None = DEFAULT_CONCURRENCY,
    ) -> None:
        super().__init__(url, registry=registry, stats=stats, policy=policy,
                         max_concurrency=max_concurrency)
        self.resolve = resolve
        self._entries: dict[str, Entry] = {}
        self._keys: list[str] = []
        self.revision: int | None = None  # the spec revision the writer followed (spec §1.3 <https://github.com/d-v-b/refkv/blob/main/spec/archive.md#13-versioning>)
        self.is_vzip = False
        self._buf, self._buf_start = b"", 0  # bytes fetched while opening
        self._key_cache: dict[str, bytes] = {}  # values of hidden entries used by key ranges
        self._cd_offset = 0
        self._pages: list = []  # CdIndex pages, when the archive has one
        self._page_keys: list[str] = []
        self._loaded: set[int] = set()
        self._fetching: dict[int, asyncio.Future] = {}  # page -> the task reading it
        self._bad_pages: dict[int, str] = {}
        self._file_size = 0

    def __eq__(self, other: object) -> bool:
        return isinstance(other, VZipStore) and other.url == self.url

    def __repr__(self) -> str:
        return f"VZipStore({self.url!r}, resolve={self.resolve})"

    # ------------------------------------------------------------ raw IO

    async def _open(self) -> None:
        if self._is_open:
            return
        try:
            await self._open_archive()
        except ArchiveError:
            raise
        except Exception as e:  # noqa: BLE001 - spec §6.1 <https://github.com/d-v-b/refkv/blob/main/spec/archive.md#61-opening>: every open failure
            raise ArchiveError(f"{type(e).__name__}: {e}") from None
        self._refresh_keys()
        await super()._open()

    async def _open_archive(self) -> None:
        store, path = self.resolver.resolve(self.url)
        async with self._limit():
            res = await store.get_async(path, options={"range": {"suffix": TAIL_GUESS}})
            tail = bytes(await res.buffer_async())
        size = res.meta["size"]
        tail_start = size - len(tail)
        self.stats.record(self.url, tail_start, len(tail), external=False)
        d = parse_tail(tail, size)
        self.is_vzip = d.is_vzip
        self._cd_offset = d.cd_offset
        self._file_size = size
        if d.cd_offset + d.cd_size > size:
            raise ArchiveError("central directory lies outside the file")
        for off, n in [(d.sources_offset, d.sources_size), (d.index_offset or 0, d.index_size)]:
            if off + n > size:
                raise ArchiveError("a format entry's body lies outside the file")
        if d.index_offset is not None:
            # Paged: fetch URL table + late entries + CdIndex (contiguous, just before
            # the CD), but not the CD itself; pages are loaded on demand.
            end = d.index_offset + d.index_size
            if d.sources_offset < tail_start:
                head = await self._read(self.url, d.sources_offset, min(end, tail_start), external=False)
                if end >= tail_start:
                    head = head[: tail_start - d.sources_offset] + tail
                self._buf, self._buf_start = head, d.sources_offset
            else:
                self._buf, self._buf_start = tail, tail_start
            index_body = self._slice(d.index_offset, d.index_size, 8)
            sources_body = self._slice(d.sources_offset, d.sources_size, 8)
            pages, pinned = decode_cd_index(index_body)
            # the trailer records are in no page; the comment locates both entries
            for k, off, csize, body in [
                (SOURCES_KEY, d.sources_offset, d.sources_size, sources_body),
                (INDEX_KEY, d.index_offset, d.index_size, index_body),
            ]:
                self._entries[k] = Entry(k, -1, len(body), csize, 8, None, off)
            _check_index(pages, pinned, d.cd_size, size)
            self._pages = pages
            self._page_keys = [p.first_key for p in pages]
            for e in pinned:
                self._entries[e.key] = Entry(
                    e.key, -1, e.size, e.csize, e.method, None, e.data_offset
                )
            self._sources, self.revision = read_table(self._slice(d.sources_offset, d.sources_size, 8))
        else:
            # Unpaged: the vzip comment locates the URL table, so one more read (at
            # most) gets both it and the whole central directory.
            want = d.cd_offset if d.sources_offset is None else min(d.cd_offset, d.sources_offset)
            if want < tail_start:
                head = await self._read(self.url, want, tail_start, external=False)
                tail, tail_start = head + tail, want
            self._buf, self._buf_start = tail, tail_start
            cd = self._slice(d.cd_offset, d.cd_size, 0)
            self._entries = parse_central_directory(cd, trust_offsets=True)
            if INDEX_KEY in self._entries:
                raise ValueError("__vz__/index entry in an archive without a page index")
            if d.sources_offset is not None:
                self._sources, self.revision = read_table(
                    self._slice(d.sources_offset, d.sources_size, 8))
            elif SOURCES_KEY in self._entries:
                e = self._entries[SOURCES_KEY]
                self._sources, self.revision = read_table(await self._entry_bytes(e, 0, e.size))
        if len(self._sources) > self.policy.max_sources:  # spec §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/archive.md#67-reader-policy> rule 4
            raise ArchiveError(f"the source table has {len(self._sources)} sources; this reader "
                               f"allows {self.policy.max_sources}")
        if any(src.url == "" or src.key == "" for src in self._sources):
            raise ValueError("empty url or key in the source table")

    def _slice(self, offset: int, n: int, method: int) -> bytes:
        """A format entry's body, fetched while opening (inflated within spec §6.7
        <https://github.com/d-v-b/refkv/blob/main/spec/archive.md#67-reader-policy> rule 6)."""
        lo = offset - self._buf_start
        raw = self._buf[lo : lo + n]
        if method != 8:
            return raw
        try:
            return inflate_clean(raw, self.policy.max_format_entry)
        except InflateLimitError as e:
            raise ArchiveError(f"a format entry {e}; this reader allows "
                               f"{self.policy.max_format_entry} (spec §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/archive.md#67-reader-policy>)") from None

    def _refresh_keys(self) -> None:
        self._keys = sorted(
            k for k in self._entries if self.resolve is False or not k.startswith(RESERVED_PREFIX)
        )

    async def _load_pages(self, idxs: Iterable[int]) -> None:
        """Load pages `idxs`; a page another caller is already fetching is read once."""
        todo = [i for i in dict.fromkeys(idxs) if i not in self._loaded]
        new = [i for i in todo if i not in self._fetching]
        if new:
            task = asyncio.ensure_future(self._fetch_pages(new))
            self._fetching.update(dict.fromkeys(new, task))
        # shield: one caller being cancelled must not cancel a fetch others await
        await asyncio.gather(*(asyncio.shield(t) for t in {self._fetching[i] for i in todo}))

    async def _fetch_pages(self, idxs: list[int]) -> None:
        try:
            datas = await asyncio.gather(*(
                self._read(self.url, self._cd_offset + self._pages[i].offset,
                           self._cd_offset + self._pages[i].offset + self._pages[i].length,
                           external=False)
                for i in idxs
            ))
            for i, data in zip(idxs, datas):
                try:
                    parsed = parse_central_directory(data, trust_offsets=True)
                except ValueError as e:
                    self._bad_pages[i] = str(e)
                    parsed = {}
                for k, e in parsed.items():
                    if bisect.bisect_right(self._page_keys, k) - 1 == i:  # spec §12.2 <https://github.com/d-v-b/refkv/blob/main/spec/archive.md#122-lookup>
                        self._entries.setdefault(k, e)
                self._loaded.add(i)
            self._refresh_keys()
        finally:
            for i in idxs:
                self._fetching.pop(i, None)

    async def _lookup(self, key: str) -> None:
        """Make sure `key`'s central directory record is loaded, if it exists."""
        if key in self._entries or not self._pages:
            return
        i = bisect.bisect_right(self._page_keys, key) - 1
        if i >= 0:
            await self._load_pages([i])
            if i in self._bad_pages:
                raise EntryError(f"page {i}, which holds {key!r}, cannot be parsed: "
                                 f"{self._bad_pages[i]}")

    async def _load_prefix(self, prefix: str) -> None:
        if not self._pages:
            return
        # spec §13.2 <https://github.com/d-v-b/refkv/blob/main/spec/archive.md#132-operations>: page
        # [lo, hi) is read iff (no hi or prefix < hi) and
        # (lo <= prefix or lo starts with prefix), comparing UTF-8 bytes
        pb = prefix.encode()
        keys = [k.encode() for k in self._page_keys]
        idxs = [i for i, lo in enumerate(keys)
                if (i + 1 == len(keys) or pb < keys[i + 1]) and (lo <= pb or lo.startswith(pb))]
        await self._load_pages(idxs)
        bad = [i for i in idxs if i in self._bad_pages]
        if bad:
            raise EntryError(f"pages {bad} cannot be parsed")

    async def _entry_bytes(self, e: Entry, start: int, end: int) -> bytes:
        """Bytes [start, end) of an entry's *body* (the naive view)."""
        if (
            e.data_offset is not None
            and self._buf_start <= e.data_offset
            and e.data_offset + e.csize <= self._buf_start + len(self._buf)
        ):
            # already fetched by _open (e.g. metadata written with late=True)
            lo = e.data_offset - self._buf_start
            raw = self._buf[lo : lo + e.csize]
            return (self._inflate(e, raw) if e.method == 8 else raw)[start:end]
        if e.method == 8:  # deflated: inflate the whole entry
            if e.data_offset is None:
                await self._locate(e)
            raw = await self._read(self.url, e.data_offset, e.data_offset + e.csize, external=False)
            return self._inflate(e, raw)[start:end]
        if e.method != 0:
            raise NotImplementedError(f"zip compression method {e.method}")
        if e.data_offset is None:
            await self._locate(e)
        return await self._read(self.url, e.data_offset + start, e.data_offset + end, external=False)

    def _inflate(self, e: Entry, raw: bytes) -> bytes:
        """An entry's DEFLATE body, inflated no further than its record's size allows
        (spec §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/archive.md#67-reader-policy> rule
        6); a format entry by the policy's bound."""
        limit = self.policy.max_format_entry if e.name in (SOURCES_KEY, INDEX_KEY) else e.size
        return inflate_clean(raw, limit)

    async def _locate(self, e: Entry) -> None:
        hdr = await self._read(
            self.url, e.header_offset, e.header_offset + LFH_SIZE + 1024, external=False,
            exact=False,
        )
        e.data_offset = e.header_offset + parse_local_header(hdr)

    async def _body(self, e: Entry, start: int, end: int) -> bytes:
        """A bytes entry's (inflated) body slice; failures are body errors (spec §6.4
        <https://github.com/d-v-b/refkv/blob/main/spec/archive.md#64-errors>)."""
        try:
            if e.method == 0 and e.csize != e.size:
                raise ValueError(f"STORED entry with sizes {e.csize} != {e.size}")
            if e.method == 8:
                data = await self._entry_bytes(e, 0, 1 << 64)  # whole inflated body
                if len(data) != e.size:
                    raise ValueError(f"inflates to {len(data)} bytes, record says {e.size}")
                return data[start:end]
            return await self._entry_bytes(e, start, end)
        except RefkvError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise BodyError(f"{e.name!r}: {type(exc).__name__}: {exc}") from None

    async def _key_bytes(self, key: str, start: int, end: int) -> bytes:
        """Bytes [start, end) of another entry's value (a `key` source, spec §5
        <https://github.com/d-v-b/refkv/blob/main/spec/archive.md#5-source-table>).

        The target must be a bytes entry. Hidden entries (shard indexes etc.) are
        cached whole; other STORED entries are range-read directly.
        """
        if key not in self._key_cache:
            await self._lookup(key)
            e = self._entries.get(key)
            if e is None:
                raise ResolutionError(f"key source {key!r} is missing")
            if key in (SOURCES_KEY, INDEX_KEY):
                raise ResolutionError(f"key source {key!r} names a format entry")
            if e.error:
                raise ResolutionError(f"key source {key!r} has an entry error: {e.error}")
            if e.is_ref:
                raise ResolutionError(f"key source {key!r} is a reference entry")
            if end > e.size:
                raise ResolutionError(f"key source {key!r} is shorter than {end} bytes")
            if not key.startswith(RESERVED_PREFIX) and e.method == 0:
                return await self._body(e, start, end)
            self._key_cache[key] = await self._body(e, 0, e.size)
        data = self._key_cache[key]
        if end > len(data):
            raise ResolutionError(f"key source {key!r} is shorter than {end} bytes")
        return data[start:end]

    # ------------------------------------------------------------ the operations

    def _classify(self, key: str) -> Kind:
        """The overlay's decision function, on the records loaded (spec §11.1
        <https://github.com/d-v-b/refkv/blob/main/spec/archive.md#111-classification>).

        Raises EntryError for an entry error (spec §6.4
        <https://github.com/d-v-b/refkv/blob/main/spec/archive.md#64-errors>). """
        e = self._entries.get(key)
        if e is None or (self.resolve and key.startswith(RESERVED_PREFIX)):
            return "missing"
        if e.error:
            raise EntryError(f"{key!r}: {e.error}")
        return "reference" if (self.resolve and e.is_ref) else "bytes"

    async def classify_async(self, key: str) -> Kind:
        """`classify`, with the lookup, in the order of spec §6.4
        <https://github.com/d-v-b/refkv/blob/main/spec/archive.md#64-errors>."""
        await self._ensure_open()
        if self.resolve and key.startswith(RESERVED_PREFIX):
            return "missing"
        await self._lookup(key)
        return self._classify(key)

    async def get_async(self, key: str, request: Request | None = None) -> bytes | None:
        await self._ensure_open()
        _abs_range(0, request)  # 1. request errors, even for missing keys
        kind = await self.classify_async(key)  # 2-3. hidden, lookup, entry error
        if kind == "missing":
            return None
        e = self._entries[key]
        if kind == "reference":
            try:
                ref = e.ref
            except ValueError as exc:
                raise PayloadError(f"{key!r}: {exc}") from None
            start, end = _abs_range(ref.size, request)
            return await self._ref_bytes(ref, start, end)
        start, end = _abs_range(e.size, request)
        return await self._body(e, start, end)

    async def raw_async(self, key: str) -> bytes | None:
        """The inflated body of `key`'s entry, whatever it is: a reference
        entry's body (its payload, or empty if the writer did not mirror it), a
        hidden entry's, a format entry's (spec §13.2
        <https://github.com/d-v-b/refkv/blob/main/spec/archive.md#132-operations>)."""
        await self._ensure_open()
        await self._lookup(key)
        e = self._entries.get(key)
        if e is None:
            return None
        if e.error:
            raise EntryError(f"{key!r}: {e.error}")
        return await self._body(e, 0, e.size)

    async def list_async(self, prefix: str = "") -> AsyncIterator[str]:
        await self._ensure_open()
        await self._load_prefix(prefix)
        i = bisect.bisect_left(self._keys, prefix)
        while i < len(self._keys) and self._keys[i].startswith(prefix):
            yield self._keys[i]
            i += 1

    async def list_dir_async(self, prefix: str = "") -> AsyncIterator[str]:
        """The distinct next path segments of the keys under `prefix`.

        Keys are sorted (spec §12.1
        <https://github.com/d-v-b/refkv/blob/main/spec/archive.md#121-layout>), so after finding child
        `c` with keys below it, the listing seeks straight past `prefix + c + "/"`: in an archive
        with a page index it reads the pages where each child starts, not
        every page under `prefix` as `list` must (spec §13.2
        <https://github.com/d-v-b/refkv/blob/main/spec/archive.md#132-operations>). """
        await self._ensure_open()
        prefix = prefix.rstrip("/")
        prefix = prefix + "/" if prefix else ""
        seen: set[str] = set()
        cursor = prefix
        while (k := await self._next_key(cursor)) is not None and k.startswith(prefix):
            child, sep, _ = k[len(prefix) :].partition("/")
            if child not in seen:
                seen.add(child)
                yield child
            # "0" follows "/": the least string above every key under child/
            cursor = prefix + child + "0" if sep else k + "\0"

    async def _next_key(self, cursor: str) -> str | None:
        """The least listed key that is at least `cursor`, loading only the pages
        needed to find it."""
        if not self._pages:
            i = bisect.bisect_left(self._keys, cursor)
            return self._keys[i] if i < len(self._keys) else None
        while True:
            if self.resolve and cursor.startswith(RESERVED_PREFIX):
                cursor = RESERVED_PREFIX[:-1] + "0"  # hidden keys are never listed
            p = max(bisect.bisect_right(self._page_keys, cursor) - 1, 0)
            await self._load_pages([p])
            if p in self._bad_pages:
                raise EntryError(f"page {p} cannot be parsed: {self._bad_pages[p]}")
            hi = self._page_keys[p + 1] if p + 1 < len(self._pages) else None
            i = bisect.bisect_left(self._keys, cursor)
            # pinned keys of later pages are known already; this page's own end is `hi`
            if i < len(self._keys) and (hi is None or self._keys[i] < hi):
                return self._keys[i]
            if hi is None:
                return None
            cursor = hi

    # ------------------------------------------------------------ helpers

    def entry(self, key: str) -> Entry | None:
        return self._entries.get(key)
