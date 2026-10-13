"""The read path every reference store shares: a mapping whose values are bytes or
references.

A reference store maps each key to bytes or to a reference (spec/refkv.md, Part 1
<https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#part-1-reference-values>).
`ReferenceReader` resolves references, whatever holds the mapping: the in-memory
`refkv.ReferenceStore`, or `refkv.vzip.VZipStore`, which reads a vzip archive (the
ZIP binding, Part 2
<https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#part-2-the-zip-binding>).
Every store has the operations of spec/refkv.md §6.2
<https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#62-operations>, as
methods that return at once (`get`, `classify`, `raw`, `list`) and as coroutines
(`get_async`, ...). Nothing here depends on Zarr, or on ZIP: `refkv.zarr` wraps a
store as a read-only zarr store.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import functools
import http.client
import math
import os
import re
import socket
import threading
from collections.abc import AsyncIterator, Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from email.message import Message
from typing import Literal
from urllib.parse import unquote, urljoin, urlparse, urlsplit

from obstore.store import HTTPStore, LocalStore

from refkv._sync import run
from refkv.errors import PayloadError, RequestError, ResolutionError
from refkv.pb import Reference, Source, parts
from refkv.policy import Policy, crc32c, public_s3_url
from refkv.uri import file_path, file_uri
from refkv.uri import resolve as resolve_reference


@dataclass
class Stats:
    """A count of the requests a store made, and the bytes they returned.

    Pass one to a store and read it after a computation, to see what the
    computation fetched:

        stats = refkv.Stats()
        ds = xr.open_zarr(refkv.zarr.open("oisst.vzip", stats=stats))
        ds.sst.isel(time=0).load()
        print(stats.external_requests, stats.external_bytes)

    - `archive_requests`, `archive_bytes`: reads of the archive itself (its
      tail, central directory, pages and bytes entries). Zero for a
      `ReferenceStore`, which holds its mapping in memory.
    - `external_requests`, `external_bytes`: reads of the sources that
      references point to, such as the original netCDF files. Nearby ranges of
      one source are merged into one request, so this counts requests, not
      references.
    - `log`: every request, as `(url, start, length)`.

    One `Stats` may be shared by several stores; `reset()` sets it back to
    zero."""

    archive_requests: int = 0
    archive_bytes: int = 0
    external_requests: int = 0
    external_bytes: int = 0
    log: list[tuple[str, int, int]] = field(default_factory=list)

    def record(self, url: str, start: int, n: int, *, external: bool) -> None:
        if external:
            self.external_requests += 1
            self.external_bytes += n
        else:
            self.archive_requests += 1
            self.archive_bytes += n
        self.log.append((url, start, n))

    def reset(self) -> None:
        self.__init__()


class Resolver:
    """The object stores a reference store reads with: the application's, registered
    under URL prefixes, and the few refkv makes itself.

    Every reference store takes `registry=`: an
    `obspec_utils.registry.ObjectStoreRegistry` (the one VirtualiZarr's
    `open_virtual_dataset` takes), a plain mapping of URL prefixes to stores,
    such as `{"s3://my-bucket": S3Store("my-bucket", ...)}`, or a `Resolver`.
    A store registered for a prefix serves every URL under it: an `s3://`
    bucket, or an `https://` host whose requests need credentials or headers.
    A prefix matches whole path segments, and the longest one wins.

    Registering a store is the application allowing it: a source URL under a
    registered prefix passes the reader policy's scheme rule (spec §6.7
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy> rule 1), so
    `Policy(schemes={"s3"})` is not needed to read it. The rest of the policy
    still applies to it:

    - rule 2: a `Policy(prefixes=...)` list replaces rule 1, registration
      included, so a URL outside the list is refused;
    - rule 1's `file` rule: an archive opened from a URL still may not read
      `file:` sources, whatever is registered, unless the policy has the
      unsafe `allow_files_from_remote_archives`;
    - rule 3 (private hosts): refkv checks an `http(s)` source URL's host as
      written, so an IP literal or `localhost` of a non-public class is
      refused. It cannot check the addresses a registered store connects to,
      or the redirects it follows: the store makes its own connections. For a
      source that no store is registered for, refkv's own HTTP client checks
      every address it connects to.

    Without a registered store, refkv reads `http(s)` sources with its own HTTP
    client, the archive itself (local, or over `http(s)`) with obstore, and
    local `file:` sources from the file system. It makes no store for any
    other scheme: obstore's `from_url` would look for credentials (environment
    variables, configuration files, the cloud metadata service at
    169.254.169.254), out of the reader policy's reach. Reading such a URL
    without a registered store is a `ResolutionError`.

    For a public S3 bucket:

        from obstore.store import S3Store

        registry = {"s3://my-bucket": S3Store("my-bucket", region="us-east-1",
                                              skip_signature=True)}
        store = refkv.open("data.vzip", registry=registry)
    """

    _OWN = ("file", "http", "https")  # the schemes refkv makes stores for itself

    def __init__(self, registry=None) -> None:
        self._registry = None  # an ObjectStoreRegistry-like object, with .resolve(url)
        self._prefixes: list[tuple[str, str, tuple[str, ...], object]] = []
        self._made: dict[str, object] = {}  # the stores refkv made, by scheme://netloc
        if isinstance(registry, Resolver):
            self._registry, self._prefixes = registry._registry, list(registry._prefixes)
        elif isinstance(registry, Mapping):
            for prefix, store in registry.items():
                p = urlsplit(str(prefix))
                if not p.scheme:
                    raise ValueError(f"a registry prefix is a URL with a scheme, such as "
                                     f"'s3://my-bucket', not {prefix!r}")
                self._prefixes.append((p.scheme.lower(), p.netloc, _segments(p.path), store))
        elif registry is not None:
            if not callable(getattr(registry, "resolve", None)):
                raise TypeError(f"registry is an obspec_utils ObjectStoreRegistry, a mapping of "
                                f"URL prefixes to stores, or a refkv Resolver, not "
                                f"{type(registry).__name__}")
            self._registry = registry

    def registered(self, url: str) -> tuple[object, str] | None:
        """The store the application registered for `url`, and the object's path
        in it; None if there is none."""
        if self._registry is not None:
            try:
                store, path = self._registry.resolve(url)
            except ValueError:
                return None
            return store, unquote(path)
        p = urlsplit(url)
        segs = _segments(p.path)
        best = None
        for scheme, netloc, prefix, store in self._prefixes:
            if (scheme == p.scheme.lower() and netloc == p.netloc
                    and segs[:len(prefix)] == prefix
                    and (best is None or len(prefix) > len(best[0]))):
                best = (prefix, store)
        if best is None:
            return None
        store = best[1]
        # as obspec_utils does: the path is relative to the store's own root
        root = getattr(store, "prefix", None) or (
            urlsplit(store.url).path if isinstance(getattr(store, "url", None), str) else "")
        path = unquote(p.path).lstrip("/")
        root = unquote(str(root)).strip("/")
        if root and (path == root or path.startswith(root + "/")):
            path = path[len(root):].lstrip("/")
        return store, path

    def resolve(self, url: str) -> tuple[object, str]:
        """The store that reads `url`, and the object's path in it."""
        found = self.registered(url)
        if found is not None:
            return found
        p = urlparse(url)
        scheme = p.scheme.lower()
        if scheme == "file":
            return self._made.setdefault("file://", LocalStore()), file_path(url)
        if scheme not in self._OWN:
            root = f"{scheme}://{p.netloc}"
            raise ResolutionError(
                f"{url}: no store is registered for {root}. refkv does not make one itself, "
                f"since it would look for credentials outside the reader policy "
                f"(spec §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>). "
                f"Pass registry={{{root!r}: store}}, such as an obstore S3Store, or an "
                f"obspec_utils ObjectStoreRegistry"
                + ("; or, for a public bucket, write its HTTPS URLs "
                   "(rewrite_urls=refkv.public_s3_url)." if public_s3_url(url) else "."))
        root = f"{scheme}://{p.netloc}"
        if root not in self._made:
            self._made[root] = HTTPStore.from_url(
                root, client_options={"allow_http": scheme == "http"})
        # object_store paths are decoded; it percent-encodes them itself when building URLs
        return self._made[root], unquote(p.path).lstrip("/")


def _segments(path: str) -> tuple[str, ...]:
    """The non-empty segments of a URL path, decoded."""
    return tuple(unquote(s) for s in path.split("/") if s)


def _resolver(registry) -> Resolver:
    return registry if isinstance(registry, Resolver) else Resolver(registry)


@dataclass(frozen=True)
class RangeRequest:
    """Bytes `[min(start, n), min(end, n))` of a value of `n` bytes: spec §6.2
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#62-operations>'s
    *range(start, end)*. `start > end` is a request error, whether or not the key
    is present."""

    start: int
    end: int


@dataclass(frozen=True)
class OffsetRequest:
    """Bytes `[min(start, n), n)` of a value of `n` bytes: spec §6.2
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#62-operations>'s
    *offset(start)*."""

    start: int


@dataclass(frozen=True)
class SuffixRequest:
    """The last `count` bytes of a value, or all of it if it is shorter: spec §6.2
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#62-operations>'s
    *suffix(count)*."""

    count: int


Request = RangeRequest | OffsetRequest | SuffixRequest
"""The part of a value that `get` reads; None reads the whole value."""


Kind = Literal["bytes", "reference", "missing"]
"""What `classify` says a key holds (spec §6.2
<https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#62-operations>)."""


def _abs_range(size: int, request: Request | None) -> tuple[int, int]:
    """The window [start, end) that `request` selects of a value of `size` bytes."""
    if request is None:
        return 0, size
    if not isinstance(request, (RangeRequest, OffsetRequest, SuffixRequest)):
        raise TypeError(f"a request is a RangeRequest, an OffsetRequest, a SuffixRequest or "
                        f"None, not {request!r}")
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in vars(request).values()):
        raise RequestError(f"a request holds non-negative integers, not {request!r}")
    if isinstance(request, RangeRequest):
        if request.start > request.end:
            raise RequestError(f"byte range start {request.start} > end {request.end}")
        return min(request.start, size), min(request.end, size)
    if isinstance(request, OffsetRequest):
        return min(request.start, size), size
    return max(size - request.count, 0), size


async def _collect(keys: AsyncIterator[str]) -> list[str]:
    return [k async for k in keys]


class _Pool:
    """Kept-alive HTTP connections, per origin, shared by the reader threads.

    A new TCP connection costs far more than a small range read, so reusing
    connections is most of what makes reading many chunks fast."""

    def __init__(self, max_idle: int = 32) -> None:
        self._idle: dict[tuple[str, str, int | None], list] = {}
        self._lock = threading.Lock()
        self._max_idle = max_idle

    def get(self, scheme: str, host: str, port: int | None):
        """A connection, and whether it was used before."""
        with self._lock:
            idle = self._idle.get((scheme, host, port))
            if idle:
                return idle.pop(), True
        return new_connection(scheme, host, port), False

    def put(self, scheme: str, host: str, port: int | None, conn) -> None:
        with self._lock:
            idle = self._idle.setdefault((scheme, host, port), [])
            if len(idle) < self._max_idle:
                idle.append(conn)
                return
        conn.close()


_POOL = _Pool()


def new_connection(scheme: str, host: str, port: int | None, timeout: float = 60):
    """An unopened connection to `host`, which only `_connect` (checking the address) opens."""
    cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    conn = cls(host, port, timeout=timeout)
    conn._create_connection = _refuse_unchecked  # connect() must go through _connect
    return conn


# Resolves host names (replaceable in tests).
_getaddrinfo = socket.getaddrinfo


Check = Callable[[str], None]
"""Raises ResolutionError for an IP address the reader's policy refuses (spec §6.7
<https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy> rule 3)."""


def _refuse_unchecked(*args, **kwargs):
    raise AssertionError("a pooled connection was opened without an address check")


def _connect(conn: http.client.HTTPConnection, check: Check | None) -> None:
    """Open `conn` to an address of its host that `check` allows (spec §6.7
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy> rule 3).

    The host is resolved here, each address is checked, and the socket is
    connected to the checked address itself, so a name that resolves
    differently a moment later (DNS rebinding) cannot reach a refused
    address. The request keeps the host name: `Host`, and for https the TLS
    server name and certificate check, come from `conn.host`."""

    def create(address, timeout, source_address=None):
        host, port = address
        refused, error = None, None
        for family, type_, proto, _, sockaddr in _getaddrinfo(host, port, 0, socket.SOCK_STREAM):
            if check is not None:
                try:
                    check(sockaddr[0])
                except ResolutionError as e:
                    refused = refused or e
                    continue
            sock = socket.socket(family, type_, proto)
            try:
                sock.settimeout(timeout)
                if source_address:
                    sock.bind(source_address)
                sock.connect(sockaddr)
                return sock
            except OSError as e:
                sock.close()
                error = e
        if error is not None:
            raise error
        if refused is not None:
            raise refused
        raise OSError(f"{host}: no address")

    conn._create_connection = create
    try:
        conn.connect()
    finally:
        conn._create_connection = _refuse_unchecked


def _proxy(url: str) -> str | None:
    """The proxy a request for `url` goes through, if any: the scheme's proxy from the
    environment (`http_proxy`, `https_proxy`, in either case) or, where urllib reads
    them, the system's settings, unless `no_proxy` (or the system) exempts the host.
    `all_proxy` is not used."""
    import urllib.request

    pu = urlparse(url)
    proxy = urllib.request.getproxies().get(pu.scheme)
    if not proxy or urllib.request.proxy_bypass(pu.hostname):
        return None
    return proxy


def _send(url: str, headers: dict[str, str], check: Check | None = None,
          unchecked_proxy: bool = False, pool=None, method: str = "GET") -> tuple[int, Message, bytes]:
    """One request of `url` (a GET, or `method`), without following redirects:
    (status, headers, body).

    `check` is applied to the address the request is sent to: when a new
    connection is opened, and again to a reused one's peer. A request that
    would go through a proxy cannot be checked, so with `check` it is
    refused unless `unchecked_proxy` (spec §6.7
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy> rule 3). `pool` keeps
    the connections alive (default: the reader's, shared by its threads); it
    has `_Pool`'s `get` and `put`."""
    pool = _POOL if pool is None else pool
    pu = urlparse(url)
    proxy = _proxy(url)
    if proxy is not None:
        if check is not None:
            if not unchecked_proxy:
                raise ResolutionError(
                    f"{url}: the request would go through the proxy {proxy}, and the reader "
                    f"cannot check the address the proxy connects to (spec §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>); set "
                    f"allow_unchecked_proxy=True to send it anyway, or exempt the host with "
                    f"no_proxy")
            # Check what the name resolves to here: this catches literal addresses and
            # honest names, not DNS rebinding.
            port = pu.port or (443 if pu.scheme == "https" else 80)
            for *_, sockaddr in _getaddrinfo(pu.hostname, port, 0, socket.SOCK_STREAM):
                check(sockaddr[0])
        return _send_urllib(url, headers, method)
    # the request target exactly as written in the URL: path and query, without the fragment
    rest = url.split("://", 1)[1]
    target = rest[re.match(r"[^/?#]*", rest).end():].split("#", 1)[0]
    target = target if target.startswith("/") else "/" + target
    for attempt in (0, 1):
        conn, reused = pool.get(pu.scheme, pu.hostname, pu.port)
        try:
            if conn.sock is None:
                _connect(conn, check)
            if check is not None:  # also a reused connection, opened for another archive
                check(conn.sock.getpeername()[0])
            conn.request(method, target, headers=headers)
            r = conn.getresponse()
            body = r.read()
        except ResolutionError:
            conn.close()
            raise
        except Exception:
            conn.close()
            if reused and attempt == 0:  # the server closed an idle connection: retry once
                continue
            raise
        if r.will_close:
            conn.close()
        else:
            pool.put(pu.scheme, pu.hostname, pu.port, conn)
        return r.status, r.msg, body
    raise AssertionError("unreachable")


def _send_urllib(url: str, headers: dict[str, str], method: str = "GET") -> tuple[int, Message, bytes]:
    """`_send` through urllib, which honors the proxy settings in the environment."""
    import urllib.error
    import urllib.request

    class NoRedirects(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args):
            return None  # http_range follows redirects itself

    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.build_opener(NoRedirects).open(req, timeout=60) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


_REDIRECTS = (301, 302, 303, 307, 308)


def http_range(url: str, start: int, end: int, src: Source,
               allow: Callable[[str], None] | None = None,
               address: Callable[[str, str], None] | None = None,
               unchecked_proxy: bool = False) -> tuple[bytes, int | None]:
    """Bytes [start, end) of an http(s) object, and its size if known (spec §5.2
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#52-reading-over-http>).

    `allow(url)` raises for a redirect target the reader's policy refuses
    (spec §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>); it is
    called before the request to that target is sent.
    `address(url, ip)` raises for an IP address the policy refuses (rule 3);
    it is called on the address each request is sent to, before sending it.
    With `address`, a request that would go through a proxy is refused unless
    `unchecked_proxy`."""
    import email.utils

    from refkv.uri import is_uri_reference

    _check_http_url(url)
    headers = {"Range": f"bytes={start}-{end - 1}", "Accept-Encoding": "identity"}
    if src.etag is not None:
        headers["If-Match"] = src.etag
    if src.modified_not_after is not None:
        t = src.modified_not_after
        if not -62135596800 <= t <= 253402300799:  # years 1..9999
            raise ResolutionError("modified_not_after cannot be written as an HTTP-date")
        when = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc) + datetime.timedelta(
            seconds=t)
        headers["If-Unmodified-Since"] = email.utils.format_datetime(when, usegmt=True)
    first = url
    for redirects in range(6):  # spec §5.2 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#52-reading-over-http>: at most 5 redirects
        check = None if address is None else (lambda ip, u=url: address(u, ip))
        try:
            status, msg, body = _send(url, headers, check, unchecked_proxy)
        except ResolutionError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ResolutionError(f"{url}: {type(e).__name__}: {e}") from None
        if status not in _REDIRECTS:
            break
        locations = msg.get_all("Location") or []
        if not locations:
            raise ResolutionError(f"{url}: HTTP {status} without a Location")
        if len(locations) > 1:
            raise ResolutionError("redirect with more than one Location field")
        if not is_uri_reference(locations[0]):
            raise ResolutionError(f"redirect with an invalid Location: {locations[0]!r}")
        newurl = urljoin(url, locations[0]).split("#", 1)[0]
        if urlparse(newurl).scheme.lower() not in ("http", "https"):
            raise ResolutionError(f"redirect to a non-http URL: {newurl}")
        _check_http_url(newurl)
        if allow is not None:
            allow(newurl)
        if redirects == 5:
            raise ResolutionError(f"{first}: more than 5 redirects")
        url = newurl
    if status in (412, 416) or not 200 <= status < 300:
        what = {412: "a pin failed (412)", 416: "the object is shorter than the range (416)"}
        raise ResolutionError(f"{url}: {what.get(status, f'HTTP {status}')}")
    for name in ("Content-Range", "ETag", "Last-Modified"):
        if len(msg.get_all(name) or []) > 1:
            raise ResolutionError(f"{url}: more than one {name} field")
    encs = msg.get_all("Content-Encoding") or []
    enc = ", ".join(e.strip() for e in encs).lower() if encs else "identity"
    crange = msg.get("Content-Range")
    etag, last_modified = msg.get("ETag"), msg.get("Last-Modified")
    # spec §5.2 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#52-reading-over-http>:
    # servers may ignore conditional headers, so check the response itself
    if src.etag is not None and etag != src.etag:
        raise ResolutionError(f"{url}: the ETag is {etag!r}, but the archive pins {src.etag!r}: "
                              f"the source changed since the archive was written, or its server "
                              f"does not send the ETag")
    if src.modified_not_after is not None:
        lm = imf_fixdate(last_modified)
        if lm is None:
            raise ResolutionError(f"{url}: Last-Modified {last_modified!r} is not an IMF-fixdate")
        if lm > src.modified_not_after:
            raise ResolutionError(f"{url}: Last-Modified {last_modified} is after the pin")
    if enc != "identity":
        raise ResolutionError(f"{url}: response has Content-Encoding {enc!r}")
    if status == 200:  # the server ignored Range
        if len(body) < end:
            raise ResolutionError(f"{url} is shorter than {end} bytes")
        return body[start:end], len(body)
    if status != 206 or not crange:
        raise ResolutionError(f"{url}: unexpected response {status} {crange!r}")
    m = re.fullmatch(r"(?i:bytes) (\d+)-(\d+)/(\d+|\*)", crange.strip())
    if not m:
        raise ResolutionError(f"{url}: invalid Content-Range {crange!r}")
    a, z, total = int(m[1]), int(m[2]), m[3]
    if a != start or z != end - 1 or len(body) != z - a + 1 or (total != "*" and z >= int(total)):
        raise ResolutionError(f"{url}: server returned {crange!r} ({len(body)} bytes) for "
                              f"[{start}, {end})")
    return body, None if total == "*" else int(total)


def _size_mismatch(url: str, pin: int, size: int | None) -> str:
    if size is None:
        return f"{url}: the archive pins the size {pin}, which the response does not give"
    return (f"{url}: the source is {size} bytes, but the archive pins {pin}: the source "
            f"changed since the archive was written")


def _check_http_url(url: str) -> None:
    """spec §5.2 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#52-reading-over-http>: no
    userinfo, a non-empty host, a port of at most 65535."""
    pu = urlparse(url)
    if "@" in pu.netloc or not pu.hostname:
        raise ResolutionError(f"{url}: userinfo or empty host in an http(s) URL")
    port = pu.netloc.rpartition(":")[2] if ":" in pu.netloc.rsplit("]", 1)[-1] else ""
    if port and (not port.isdigit() or int(port) > 65535):
        raise ResolutionError(f"{url}: invalid port {port!r}")


_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def imf_fixdate(v: str | None) -> int | None:
    """Seconds since the epoch for an IMF-fixdate (RFC 9110 §5.6.7), else None."""
    m = re.fullmatch(r"(\w{3}), (\d{2}) (\w{3}) (\d{4}) (\d{2}):(\d{2}):(\d{2}) GMT", v or "")
    if not m or m[1] not in _DAYS or m[3] not in _MONTHS:
        return None
    leap = int(m[7]) == 60  # a leap second counts as the following second (§5.2 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#52-reading-over-http>)
    try:
        t = datetime.datetime(int(m[4]), _MONTHS.index(m[3]) + 1, int(m[2]), int(m[5]),
                              int(m[6]), 59 if leap else int(m[7]), tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
    if _DAYS[t.weekday()] != m[1]:
        return None
    if leap:
        t += datetime.timedelta(seconds=1)
    return int((t - datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)).total_seconds())


DEFAULT_CONCURRENCY = 8
"""The default `max_concurrency` of every reference store."""


UNCAPPED_THREADS = 64
"""The threads a store with `max_concurrency=None` reads HTTP sources with."""


class ReferenceReader:
    """The read path that every reference store shares: a read-only mapping whose
    values are bytes or references (spec/refkv.md, Part 1
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#part-1-reference-values>), resolved
    under a reader policy (spec §6.7
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>).

    Subclasses say where the mapping and the source table come from:
    `refkv.ReferenceStore` holds them in memory, and `VZipStore` reads them from a
    vzip archive. Both resolve a reference here, with the same pins, checksums,
    request merging, `Stats` and request cap, so a store reads the same values
    before and after it is saved.

    The operations are those of spec §6.2
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#62-operations>, each
    as a method that returns at once and as a coroutine:

    - `get(key, request=None)`: the value, or the part of it a `RangeRequest`,
      `OffsetRequest` or `SuffixRequest` selects; None if the key is missing.
    - `classify(key)`: `"bytes"`, `"reference"` or `"missing"`.
    - `raw(key)`: what the backend holds under the key, references not
      followed, hidden keys included; None if it holds nothing.
    - `list(prefix="")`: the keys that start with `prefix`, sorted.
    - `list_dir(prefix="")`: the distinct next path segments of the keys
      under `prefix` ("a/b" lists "c" for the key "a/b/c/d").
    - `sources`: the source table.

    A failure raises one of the error classes of spec §6.4
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#64-errors> (`refkv.errors`).
    The methods that return at once run the coroutines on a thread of refkv's own,
    so they work in a notebook too; from a coroutine, await the `_async` forms.
    `refkv.zarr.store(s)` wraps a store as a read-only zarr store.

    Keyword arguments:

    - `url`: the store's base URI, against which relative source URLs resolve
      (spec §5 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#5-source-table>); its
      scheme decides whether the store is local or remote for the policy.
    - `registry`: the application's object stores, by URL prefix: an
      `obspec_utils.registry.ObjectStoreRegistry`, a mapping such as
      `{"s3://my-bucket": S3Store(...)}`, or a `Resolver`. Sources under a
      registered prefix are read with its store, and pass the policy's scheme
      rule; `Resolver` says what is and isn't checked for them.
    - `stats`: counts the requests (`Stats`).
    - `policy`: what may be resolved (`Policy`; the default is `Policy()`).
    - `max_concurrency`: the most requests this store has in flight at once,
      to its sources and to the archive itself: 8 by default, as many
      connections as the virtualizer opens to one host, and polite to the
      hosts of public data. None for no cap. Zarr's `async.concurrency` setting
      (default 10) caps something else: the chunks one zarr operation reads at
      once, per operation. One chunk can need several requests (a reference
      with ranges of several sources), and a dask computation runs many zarr
      operations at once, so only this option bounds the connections a
      computation opens. It counts the requests of this store object, per event
      loop; several stores each have their own cap. HTTP sources are read on
      the store's own threads, as many as the cap (64 with no cap), so the cap
      is the same on every machine: it doesn't depend on the CPU count, as
      asyncio's shared default thread pool does.
    """

    def __init__(
        self,
        url: str,
        *,
        registry=None,
        stats: Stats | None = None,
        policy: Policy | None = None,
        max_concurrency: int | None = DEFAULT_CONCURRENCY,
    ) -> None:
        if max_concurrency is not None and (
                isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int)
                or max_concurrency < 1):
            raise ValueError(f"max_concurrency must be a positive integer or None, "
                             f"not {max_concurrency!r}")
        self.policy = policy or Policy()
        # spec §5 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#5-source-table>:
        # absolute, lexically normalised, symlinks not resolved
        self.url = url if urlparse(url).scheme else file_uri(url)
        self.resolver = _resolver(registry)
        self.stats = stats or Stats()
        self.max_concurrency = max_concurrency
        self._is_open = False
        self._sources: list[Source] = []
        self._semaphore: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None
        self._threads: ThreadPoolExecutor | None = None
        self._threads_lock = threading.Lock()

    # ------------------------------------------------------------ the operations

    async def get_async(self, key: str, request: Request | None = None) -> bytes | None:
        """`get`, as a coroutine."""
        raise NotImplementedError

    async def classify_async(self, key: str) -> Kind:
        """`classify`, as a coroutine."""
        raise NotImplementedError

    async def raw_async(self, key: str) -> bytes | None:
        """`raw`, as a coroutine."""
        raise NotImplementedError

    def list_async(self, prefix: str = "") -> AsyncIterator[str]:
        """`list`, as an asynchronous iterator."""
        raise NotImplementedError

    def list_dir_async(self, prefix: str = "") -> AsyncIterator[str]:
        """`list_dir`, as an asynchronous iterator."""
        raise NotImplementedError

    def get(self, key: str, request: Request | None = None) -> bytes | None:
        """The value of `key`, or the part of it `request` selects (a
        `RangeRequest`, `OffsetRequest` or `SuffixRequest`); None if the key is
        missing. A reference is resolved: its ranges are read from their sources."""
        return run(self.get_async(key, request))

    def classify(self, key: str) -> Kind:
        """What `key` holds: `"bytes"`, `"reference"` or `"missing"`, from how it
        is marked alone, without reading a reference's sources."""
        return run(self.classify_async(key))

    def raw(self, key: str) -> bytes | None:
        """What the backend holds under `key`, without following references: a
        reference's serialized `Range` or `Concat` payload, and the hidden keys
        too (spec §6.2 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#62-operations>).
        None if it holds nothing."""
        return run(self.raw_async(key))

    def list(self, prefix: str = "") -> list[str]:
        """Every key that starts with `prefix`, sorted (hidden keys are not listed)."""
        return run(_collect(self.list_async(prefix)))

    def list_dir(self, prefix: str = "") -> list[str]:
        """The distinct next path segments of the keys under `prefix`, a
        "directory" whose trailing "/" may be left out."""
        return run(_collect(self.list_dir_async(prefix)))

    @property
    def sources(self) -> tuple[Source, ...]:
        """The source table, which the ranges of references index (a copy)."""
        return tuple(self._sources)

    # ------------------------------------------------------------ resolution

    async def _open(self) -> None:
        """Read what the store needs before its first operation."""
        self._is_open = True

    async def _ensure_open(self) -> None:
        if not self._is_open:
            await self._open()

    def _executor(self) -> ThreadPoolExecutor:
        """The store's own threads for blocking HTTP reads, as many as its cap."""
        with self._threads_lock:
            if self._threads is None:
                self._threads = ThreadPoolExecutor(
                    max_workers=self.max_concurrency or UNCAPPED_THREADS,
                    thread_name_prefix="refkv-read")
            return self._threads

    def _limit(self) -> contextlib.AbstractAsyncContextManager:
        """Held around each request: the `max_concurrency` cap. A semaphore belongs
        to one event loop, so a store used from another loop gets a new one."""
        if self.max_concurrency is None:
            return contextlib.nullcontext()
        loop = asyncio.get_running_loop()
        if self._semaphore is None or self._semaphore[0] is not loop:
            self._semaphore = (loop, asyncio.Semaphore(self.max_concurrency))
        return self._semaphore[1]

    async def _key_bytes(self, key: str, start: int, end: int) -> bytes:
        """Bytes [start, end) of the bytes value at `key` (a `key` source, spec §5
        <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#5-source-table>)."""
        raise NotImplementedError

    async def _read(
        self, url: str, start: int, end: int, *, external: bool, exact: bool = True
    ) -> bytes:
        """Bytes [start, end) of `url`; with exact=False a short read is allowed."""
        if end <= start:
            return b""
        store, path = self.resolver.resolve(url)
        try:
            async with self._limit():
                data = bytes(await store.get_range_async(path, start=start, end=end))
        except Exception as e:  # e.g. the range starts past the end of the object
            raise ValueError(f"cannot read [{start}, {end}) of {url}: {e}") from None
        self.stats.record(url, start, len(data), external=external)
        if exact and len(data) != end - start:
            raise ValueError(f"{url} is shorter than {end} bytes")
        return data

    async def _url_bytes(self, src: Source, start: int, end: int) -> bytes:
        """Bytes [start, end) of a url source, checking its pins (spec §5.1
        <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#51-pins>)."""
        url = resolve_reference(self.url, src.url)
        registered = self.resolver.registered(url) is not None
        allow = lambda u: self.policy.check(self.url, u, registered=registered)  # noqa: E731
        # spec §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy> rule
        # 3: check every address, unless the application allows them all
        address = None if self.policy.allow_private_hosts else self.policy.check_address
        allow(url)  # spec §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>
        if urlparse(url).scheme.lower() in ("http", "https") and not registered:  # spec §5.2 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#52-reading-over-http>, pinned or not
            async with self._limit():
                data, size = await asyncio.get_running_loop().run_in_executor(
                    self._executor(), functools.partial(
                        http_range, url, start, end, src, allow, address,
                        self.policy.allow_unchecked_proxy))
            self.stats.record(url, start, len(data), external=True)
            if src.size is not None and size != src.size:
                raise ResolutionError(_size_mismatch(url, src.size, size))
            return data
        if not src.pinned:
            return await self._read(url, start, end, external=True)
        if urlparse(url).scheme.lower() == "file":
            path = file_path(url)
            st = os.stat(path)
            if src.etag is not None:
                raise ResolutionError("an etag pin cannot be checked for a file: URL")
            if src.size is not None and st.st_size != src.size:
                raise ResolutionError(_size_mismatch(url, src.size, st.st_size))
            if (src.modified_not_after is not None
                    and math.floor(st.st_mtime) > src.modified_not_after):
                raise ResolutionError("modified_not_after pin failed: the file changed")
            return await self._read(url, start, end, external=True)
        store, path = self.resolver.resolve(url)
        options: dict = {"range": (start, end)}
        if src.etag is not None:
            options["if_match"] = src.etag
        if src.modified_not_after is not None:
            options["if_unmodified_since"] = datetime.datetime.fromtimestamp(
                src.modified_not_after, datetime.timezone.utc)
        try:
            async with self._limit():
                res = await store.get_async(path, options=options)
                data = bytes(await res.buffer_async())
        except Exception as exc:  # noqa: BLE001 - 412 Precondition Failed and friends
            raise ResolutionError(f"pin check or read failed for {url}: {exc}") from None
        self.stats.record(url, start, len(data), external=True)
        if src.size is not None and res.meta["size"] != src.size:
            raise ResolutionError(_size_mismatch(url, src.size, res.meta["size"]))
        if len(data) != end - start:
            raise ResolutionError(f"{url} is shorter than {end} bytes")
        return data

    # Reads of the same url source closer than this are combined into one
    # request (spec §5.2
    # <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#52-reading-over-http> allows it): a
    # Concat of many short ranges, such as
    # one per image row, would otherwise cost one request per range.
    MERGE_GAP = 1 << 16

    async def _ref_bytes(self, ref: Reference, start: int, end: int) -> bytes:
        """Bytes [start, end) of the value described by `ref` (spec §6.3
        <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#63-resolving-a-reference>)."""
        for r in parts(ref):  # payload errors: every range, even ones outside [start, end)
            if r.data is None and r.source >= len(self._sources):
                raise PayloadError(f"range uses source {r.source}; the table has "
                                   f"{len(self._sources)}")
        # Each piece is the bytes of a range overlapping the window: literal bytes, a
        # coroutine (a key source), or a (source, a, b) read of a url source. A range
        # with a crc32c is read whole and checked (spec §6.3
        # <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#63-resolving-a-reference>);
        # `cuts` says which part of each piece the window takes, and which checksum it must have.
        pieces: list = []
        cuts: list[tuple[int, int, int | None]] = []
        pos = 0
        for r in parts(ref):
            lo, hi = max(start, pos), min(end, pos + r.size)
            if lo < hi:
                if r.data is not None:
                    pieces.append(r.data[lo - pos : hi - pos])
                    cuts.append((0, hi - lo, None))
                    pos += r.size
                    continue
                if r.crc32c is None:
                    a, b = r.offset + lo - pos, r.offset + hi - pos
                    cuts.append((0, b - a, None))
                else:
                    a, b = r.offset, r.offset + r.length
                    cuts.append((lo - pos, hi - pos, r.crc32c))
                src = self._sources[r.source]
                if src.data is not None:
                    if b > len(src.data):
                        raise ResolutionError(f"data source {r.source} is shorter than {b} bytes")
                    pieces.append(src.data[a:b])
                elif src.key is not None:
                    pieces.append(self._key_bytes(src.key, a, b))
                else:
                    pieces.append((r.source, a, b))
            pos += r.size
        # Combine nearby reads of each url source, then slice them apart.
        reads = sorted({p for p in pieces if isinstance(p, tuple)}, key=lambda t: (t[0], t[1]))
        runs: list[list] = []  # [source, a, b]
        for source, a, b in reads:
            if runs and runs[-1][0] == source and a - runs[-1][2] <= self.MERGE_GAP:
                runs[-1][2] = max(runs[-1][2], b)
            else:
                runs.append([source, a, b])
        coros = [p for p in pieces if not isinstance(p, (bytes, tuple))]
        if len(runs) > self.policy.max_reads:  # spec §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy> rule 5, before any read
            for c in coros:
                c.close()
            raise RequestError(f"the value needs {len(runs)} reads; this reader allows "
                               f"{self.policy.max_reads} (spec §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>)")
        coros += [self._url_bytes(self._sources[s], a, b) for s, a, b in runs]
        try:
            done = await asyncio.gather(*coros)
        except ResolutionError:
            raise
        except Exception as exc:  # noqa: BLE001 - spec §6.4 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#64-errors>: failures resolving a range
            raise ResolutionError(f"{type(exc).__name__}: {exc}") from None
        other = iter(done[: len(done) - len(runs)])
        fetched = list(zip(runs, done[len(done) - len(runs):]))

        def from_runs(source: int, a: int, b: int) -> bytes:
            for (s, ra, rb), data in fetched:
                if s == source and ra <= a and b <= rb:
                    return data[a - ra : b - ra]
            raise AssertionError("read not covered by a run")

        out = []
        for p, (lo, hi, crc) in zip(pieces, cuts):
            if isinstance(p, bytes):
                data = p
            elif isinstance(p, tuple):
                data = from_runs(*p)
            else:
                data = next(other)
            if crc is not None and crc32c(data) != crc:  # spec §4.2 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#42-range>: the source changed
                raise ResolutionError(f"the bytes read do not match the range's crc32c "
                                      f"{crc:#010x} (got {crc32c(data):#010x}): the source "
                                      f"changed since the archive was written")
            out.append(data[lo:hi])
        return b"".join(out)
