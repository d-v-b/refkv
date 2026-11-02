"""The store readers (spec/zarrify.md §1.4
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>, §1.5
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>): `Store`, its
prefetch of documents, a store listed over http(s) (`HttpStore`) or from a local directory
(`DirStore`), `open_store`, and the store profile a store's root keys select
(`choose_profile`).
"""

from __future__ import annotations

import functools
import http.client
import os
import stat
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from email.message import Message
from pathlib import Path
from typing import Literal, Protocol, override

from refkv._narrow import JSON
from refkv.policy import Policy
from refkv.reader import _send
from refkv.zarrify.common import UA, Rejected, _retry, _ThreadConnections, follow
from refkv.zarrify.store.documents import MAX_DOCUMENT, parse_json
from refkv.zarrify.store.listing import parse_listing
from refkv.zarrify.store.urls import check_store_url, listing_endpoint, object_url, query_encode


class ListingFailed(OSError):  # noqa: N818 (public name)
    """The listing could not be read (a network error or a 408/429/5xx status): a failure, §1.4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>."""


class StoreLimit(OSError):  # noqa: N818 (public name)
    """An implementation's resource limit (§1.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>, Appendix A
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#appendix-a-conformance>): a failure, not a
    rejection."""


def _ignored(rel: str) -> bool:
    return rel == "" or rel.endswith("/") or any(s in ("", ".", "..") for s in rel.split("/"))


def _folder_marker(rel: str, size: int) -> bool:
    """An ignored object whose key is not recorded (§1.4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>): an empty one
    whose relative key is empty or ends in /."""
    return size == 0 and (rel == "" or rel.endswith("/"))


WORKERS = 16
PREFETCH_BYTES = 64 << 20

type Read = tuple[Literal[True], bytes] | tuple[Literal[False], Exception]
"""The result of reading an object: (True, its bytes), or (False, the exception the read raised)."""


class _Prefetch:
    """Reads the objects of `plan` (keys, in the order a profile will read them)
    ahead of the profile with up to `workers` threads, holding at most `budget`
    bytes read or being read and not yet taken (but always one object, so that
    a document of MAX_DOCUMENT bytes is read). Each result, the bytes or the
    exception the read raised, is handed over once, by `take`, when the
    profile reads that key; so a failed read fails the profile exactly where
    the sequential read would, and only if the profile reads that key. Each
    thread calls `release`, if given, as it ends (to close the connections it
    kept alive)."""

    def __init__(self, read: Callable[[str], bytes], plan: list[tuple[str, int]], workers: int, budget: int,
                 wanted: Callable[[str], bool | None] | None, release: Callable[[], None] | None = None) -> None:
        self._read, self._budget, self._wanted, self._release = read, budget, wanted, release
        self._queue = [k for k, _ in reversed(plan)]  # popped from the end: the next key in plan order
        self._size = dict(plan)
        self._state: dict[str, str] = {k: "queued" for k, _ in plan}  # queued, running, done
        self._result: dict[str, Read] = {}
        self._held = 0
        self._closed = False
        self._cv = threading.Condition()
        for _ in range(min(workers, len(plan))):
            threading.Thread(target=self._work, daemon=True).start()

    def _next(self) -> str | None:
        with self._cv:
            while True:
                if self._closed or not self._queue:
                    return None
                key = self._queue[-1]
                if self._state.get(key) != "queued":  # taken by the profile before it was started
                    self._queue.pop()
                    continue
                wanted = True if self._wanted is None else self._wanted(key)
                if wanted is None:  # not known yet: asked again after the profile's next read
                    self._cv.wait()
                    continue
                if not wanted:
                    self._queue.pop()
                    del self._state[key]
                    continue
                size = self._size[key]
                if self._held == 0 or self._held + size <= self._budget:
                    self._queue.pop()
                    self._state[key] = "running"
                    self._held += size
                    return key
                self._cv.wait()

    def _work(self) -> None:
        try:
            while (key := self._next()) is not None:
                r: Read
                try:
                    r = (True, self._read(key))
                except Exception as e:  # noqa: BLE001 - handed over as is by take()
                    r = (False, e)
                with self._cv:
                    self._state[key] = "done"
                    self._result[key] = r
                    self._cv.notify_all()
        finally:
            if self._release is not None:
                self._release()

    def take(self, key: str) -> Read | None:
        """The result of reading `key`, waiting for it; None if `key` was not
        planned, was taken already, or was not started (the caller reads it)."""
        with self._cv:
            self._cv.notify_all()  # the profile has read on: `wanted` may now know more
            state = self._state.pop(key, None)
            if state is None or state == "queued":
                return None
            while key not in self._result:
                self._cv.wait()
            self._held -= self._size[key]
            self._cv.notify_all()
            return self._result.pop(key)

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    def drain(self) -> None:
        """Starts no more reads, and waits for those running to end: their results stay to be
        taken, and a key not started is the caller's to read."""
        with self._cv:
            self._closed = True
            self._cv.notify_all()
            while any(v == "running" for v in self._state.values()):
                self._cv.wait()


class Store:
    """A listed store: `objects` maps each relative key to its size (§1.4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>), `ignored`
    holds the relative keys of the ignored objects that are recorded, and `folders`
    those of the empty objects whose keys end in `/` (directories, to the SAFE profile)."""

    url: str
    objects: dict[str, int]
    ignored: list[str]
    folders: list[str]
    listed: int = 0
    requests: int = 0
    workers: int = 1
    prefetch_bytes: int = PREFETCH_BYTES
    _prefetch: _Prefetch | None = None
    _kept: dict[str, Read] | None = None

    def read(self, key: str) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError

    def read_range(self, key: str, offset: int, length: int) -> bytes:  # pragma: no cover - interface
        """The bytes [offset, offset + length) of the object `key` (spec/zarrify/safe.md §8.2
        <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#82-objects-and-reading>)."""
        raise NotImplementedError

    def probe(self, key: str) -> int | None:
        """The size of the object `key` when the store has it, else None: the answer to a run's
        probe (`refkv.ir.store.drive_store`). A listed store has the objects it listed."""
        return self.objects.get(key)

    def prefetch(self, keys: Iterable[str], wanted: Callable[[str], bool | None] | None = None) -> None:
        """Starts reading the documents `keys`, in that order (the order the
        profile reads them), concurrently if `workers` > 1; once per store.
        `wanted(key)`, asked before a read starts, is False for a key the
        profile will not read after all, and None while that is not known yet
        (asked again after each document the profile reads). Empty documents and documents §1.6
        <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#16-json-documents> rejects for
        their size are not read."""
        if self.workers <= 1 or self._prefetch is not None:
            return
        plan = [(k, self.objects[k]) for k in dict.fromkeys(keys) if 0 < self.objects[k] <= MAX_DOCUMENT]
        self._prefetch = _Prefetch(self.read, plan, self.workers, self.prefetch_bytes, wanted, self._release)

    def _release(self) -> None:
        """Closes the connections the calling thread keeps alive, if any."""

    def close(self) -> None:
        """Stops reading ahead, and closes the calling thread's kept-alive connections
        (each reading-ahead thread closes its own as it ends). The store can still be
        read afterwards."""
        if self._prefetch is not None:
            self._prefetch.close()
        self._release()

    def document(self, key: str, *, keep: bool = False) -> JSON:
        """The JSON document at `key` (§1.6
        <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#16-json-documents>). With
        `keep`, its bytes are kept for the next read of `key` (each document is read once)."""
        size = self.objects[key]
        if size > MAX_DOCUMENT:
            raise Rejected(f"{key}: JSON document of {size} bytes exceeds {MAX_DOCUMENT}")
        if not size:
            return parse_json(b"")
        if self._kept is None:
            self._kept = {}
        r = self._kept.get(key)
        if r is None and self._prefetch is not None:
            r = self._prefetch.take(key)
        if r is None:
            try:
                r = (True, self.read(key))
            except Exception as e:  # noqa: BLE001 (kept, and raised below)
                r = (False, e)
        if keep:
            self._kept[key] = r
        else:
            self._kept.pop(key, None)
        if r[0] is False:
            raise r[1]
        return parse_json(r[1])

    def _add(self, listed: list[tuple[str, int]], prefix: str, seen: set[str]) -> None:
        for key, size in listed:
            if key in seen:
                raise Rejected(f"key {key!r} is listed twice")
            seen.add(key)
            rel = key[len(prefix):]
            if not _ignored(rel):
                self.objects[rel] = size
            elif not _folder_marker(rel, size):
                self.ignored.append(rel)
            elif rel and not _ignored(rel[:-1]):
                self.folders.append(rel)
        self.listed = len(seen)


class Response(Protocol):
    """What an opener's response gives: urlopen's, or a test's stand-in."""

    status: int

    def read(self) -> bytes:
        """The body."""
        ...


type Opener = Callable[[urllib.request.Request], AbstractContextManager[Response]]
"""A urlopen, in place of the network (a test's)."""


class HttpStore(Store):
    """A store listed by S3 ListObjectsV2 (§1.5
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>), every request
    under the reader
    policy (spec/refkv.md §6.7
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>): the listing's, each
    object read's (the prefetch
    threads' too), and each redirect target's, which is checked before it is
    requested. Unless the policy has `allow_private_hosts`, the address each
    request is sent to is checked, on a new connection and on a kept-alive one,
    and a request that would go through a proxy is refused unless the policy has
    `allow_unchecked_proxy`."""

    def __init__(self, url: str, *, max_objects: int | None = None,
                 opener: Opener | None = None, workers: int = WORKERS, policy: Policy | None = None) -> None:
        self.workers = workers  # concurrent document reads (prefetch)
        self.policy = policy or Policy()
        # Where the store is listed and read; `url`, the store's name in the
        # output, may be changed afterwards (--url).
        self.location = url
        self.url = url
        self.endpoint, self.prefix = listing_endpoint(url)
        self.objects = {}
        self.ignored = []
        self.folders = []
        # A test's urlopen, in place of the network: the policy then checks URLs as written.
        self._opener = opener
        self._conns = _ThreadConnections()
        seen: set[str] = set()
        tokens: set[str] = set()
        token = None
        while True:
            q = f"?list-type=2&prefix={query_encode(self.prefix)}"
            if token is not None:
                q += f"&continuation-token={query_encode(token)}"
            body = self._get_listing(self.endpoint + q)
            listed, token = parse_listing(body, self.prefix)
            self._add(listed, self.prefix, seen)
            if max_objects is not None and self.listed > max_objects:
                raise StoreLimit(f"the store lists more than {max_objects} objects")
            if token is None:
                break
            if token in tokens:  # the listing would repeat itself (§1.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>)
                raise Rejected(f"the listing gives the continuation token {token[:80]!r} again")
            tokens.add(token)

    def _send(self, url: str, headers: dict[str, str]) -> tuple[int, Message | None, bytes]:
        """One GET of `url`, not following redirects: (status, headers, body). A
        network error is a URLError, as urllib's; a refusal a ResolutionError."""
        if self._opener is not None:  # it follows redirects, and raises HTTPError, as urlopen does
            with self._opener(urllib.request.Request(url, headers=headers)) as r:  # noqa: S310 (an http(s) URL)
                return r.status, getattr(r, "headers", None), r.read()
        check = None
        if not self.policy.allow_private_hosts:
            check = functools.partial(self.policy.check_address, url)
        try:
            return _send(url, headers, check, self.policy.allow_unchecked_proxy, self._conns)
        except (OSError, http.client.HTTPException) as e:
            if isinstance(e, urllib.error.URLError):
                raise
            raise urllib.error.URLError(e) from None

    def _get(self, url: str, headers: dict[str, str]) -> tuple[int, bytes]:
        """(status, body) of a GET of `url`, following at most 5 redirects; the policy
        checks `url`, and each redirect's target before it is requested."""
        headers = {"User-Agent": UA, **headers}
        _, status, _, body = follow(self.location, url, lambda u: self._send(u, headers), self.policy)
        return status, body

    def _get_listing(self, url: str) -> bytes:
        self.requests += 1

        def get() -> bytes:
            status, body = self._get(url, {})
            if status != 200:
                # urllib takes no headers, whatever typeshed says
                raise urllib.error.HTTPError(url, status, f"HTTP {status}", None, None)  # pyrefly: ignore[bad-argument-type]
            return body

        try:
            return _retry(get)
        except urllib.error.HTTPError as e:
            if e.code in (408, 429) or e.code >= 500:
                raise ListingFailed(f"{url}: HTTP {e.code}") from None
            raise Rejected(f"the store has no listing: {url} answered HTTP {e.code}") from None
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            raise ListingFailed(f"{url}: {e}") from None

    def _get_range(self, url: str, start: int, end: int) -> bytes:
        """The bytes [start, end) of the object at `url`, over the calling thread's
        kept-alive connection to its host."""

        def get() -> bytes:
            status, body = self._get(url, {"Range": f"bytes={start}-{end - 1}"})
            if status not in (200, 206):
                # urllib takes no headers, whatever typeshed says
                raise urllib.error.HTTPError(url, status, f"HTTP {status}", None, None)  # pyrefly: ignore[bad-argument-type]
            return body

        return _retry(get)

    @override
    def _release(self) -> None:
        self._conns.release()

    @override
    def read(self, key: str) -> bytes:
        size = self.objects[key]
        if size == 0:
            return b""
        url = object_url(self.location, key)
        data = self._get_range(url, 0, size)
        if len(data) != size:
            raise OSError(f"{url}: read {len(data)} bytes, the listing says {size}")
        return data

    @override
    def read_range(self, key: str, offset: int, length: int) -> bytes:
        if length == 0:
            return b""
        url = object_url(self.location, key)
        data = self._get_range(url, offset, offset + length)
        if len(data) != length:
            raise OSError(f"{url}: read {len(data)} bytes at {offset}, not {length}")
        return data


class DirStore(Store):
    """A local directory, read as the store at `url` (§1.5
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>)."""

    def __init__(self, path: str, url: str) -> None:
        check_store_url(url)
        self.url = url
        self.root = Path(path)
        self.objects = {}
        self.ignored = []
        self.folders = []
        listed = []
        for dirpath, dirnames, filenames in os.walk(path):
            # symbolic links, and on Windows junctions, are not followed
            dirnames[:] = [d for d in dirnames if not ((p := Path(dirpath, d)).is_symlink() or p.is_junction())]
            rel_dir = os.path.relpath(dirpath, path)
            for name in filenames:
                st = Path(dirpath, name).lstat()
                if not stat.S_ISREG(st.st_mode):
                    continue
                rel = name if rel_dir == "." else f"{rel_dir}/{name}"
                rel = rel.replace(os.sep, "/")  # a key is `/`-separated on every system
                try:
                    rel.encode("utf-8")
                except UnicodeEncodeError:  # a name that is not UTF-8 (surrogate-escaped)
                    continue
                listed.append((rel, st.st_size))
        self._add(listed, "", set())

    @override
    def read(self, key: str) -> bytes:
        data = (self.root / key).read_bytes()
        if len(data) != self.objects[key]:
            raise OSError(f"{key}: the file changed while it was read")
        return data

    @override
    def read_range(self, key: str, offset: int, length: int) -> bytes:
        with open(self.root / key, "rb") as fh:
            fh.seek(offset)
            data = fh.read(length)
        if len(data) != length:
            raise OSError(f"{key}: the file changed while it was read")
        return data


def open_store(location: str, url: str | None = None, *, max_objects: int | None = None,
               opener: Opener | None = None, workers: int = WORKERS, policy: Policy | None = None) -> Store:
    """The store at an http(s) URL ending in `/` (listed and read there under the
    reader policy `policy`, default `Policy()`, and named `url` in the output if
    given, its documents read by up to `workers` requests at a time), or a local
    directory served at `url` (§1.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>), read sequentially."""
    if location.startswith(("http://", "https://")):
        store = HttpStore(location, max_objects=max_objects, opener=opener, workers=workers, policy=policy)
        if url is not None:
            check_store_url(url)
            store.url = url
        return store
    if url is None:
        raise ValueError("a local directory needs url, the URL it is served from")
    return DirStore(location, url)


def choose_profile(store: Store) -> str:
    """The store profile that the root keys select (§1.4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>)."""
    if ".zarray" in store.objects or ".zgroup" in store.objects:
        return "zarr2"
    if "attributes.json" in store.objects:
        return "n5"
    if "manifest.safe" in store.objects:
        return "safe"
    if any(k.endswith(".safetensors") for k in store.objects):
        return "safetensors"
    raise Rejected("not an N5, Zarr v2, SAFE or safetensors store: the root has no .zarray, .zgroup, "
                   "attributes.json or manifest.safe, and no key ends in .safetensors")
