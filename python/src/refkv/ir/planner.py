"""The host's side of the read planner (design/ARCHITECTURE.md §3.5
<https://github.com/d-v-b/refkv/blob/main/design/ARCHITECTURE.md#35-resources-and-io>): the I/O.

The policy (what to request for a parser's batch: coalescing by cost, the
amplification cap, multi-range packing, read-ahead, the budget) is the Rust
core's (`rust/refkv-ir/src/plan.rs`), behind `refkv._core.Run`. This module only
performs the requests a run asks for and reports each one's bytes and time:

- `FileTransport`: a local file, mapped; its requests are served in the calling
  thread.
- `HttpTransport`: an http(s) object read through the archive reader's HTTP
  layer (`refkv.reader`), under the reader policy of spec/refkv.md §6.7
  <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>: the host is
  resolved here, every address is checked against rule 3 (loopback, private,
  link-local and special addresses are refused unless the policy has
  `allow_private_hosts`), a request that would go through a proxy is refused
  unless `allow_unchecked_proxy`, and redirects are followed here (at most 5),
  each target checked before it is requested. At most `connections` requests run
  at a time (8 by default). Requests are retried on 429, 5xx
  and connection errors with exponential backoff and jitter, waiting at least
  what `Retry-After` says. Several ranges go in one request
  (`multipart/byteranges`) when the run asks; a server that answers otherwise
  is closed before its body is read, and the run plans single ranges. It
  records every response's ETag, for the source's `etag` pin.
- `drive_async(run, transport)`: the loop, at most `concurrency` requests at a time, as a
  coroutine; `drive(run, transport)` waits for it.
"""

from __future__ import annotations

import asyncio
import email.utils
import http.client
import mmap
import os
import random
import re
import socket
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from typing import Protocol, cast
from urllib.parse import urljoin, urlparse

from refkv import _core, _sync
from refkv.errors import ResolutionError
from refkv.policy import Policy
from refkv.reader import _POOL, Check, _check_http_url, _connect, _proxy
from refkv.reader import _send as reader_send
from refkv.zarrify.common import UA, EtagLog, Rejected

RETRY = (429, 500, 502, 503, 504)
REDIRECTS = (301, 302, 303, 307, 308)
OPEN = 1 << 16  # the bytes the request that opens a remote source reads


class FileTransport:
    """A local file: ranges are slices of its mapping."""

    remote = False

    def __init__(self, path: str) -> None:
        with open(path, "rb") as fh:
            self.size = os.fstat(fh.fileno()).st_size
            self.data = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) if self.size else b""
        self.head = (0, b"", 0.0)
        self.etag: Callable[[], str | None] = lambda: None

    def get(self, start: int, end: int) -> bytes:
        return bytes(self.data[start:end])

    def fetch(self, spans: list[tuple[int, int]]) -> tuple[list[bytes], float]:
        return [self.get(a, b) for a, b in spans], 0.0


class HttpTransport:
    """An http(s) object, read under the reader policy (spec/refkv.md §6.7
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>)."""

    remote = True

    def __init__(self, url: str, policy: Policy | None = None, attempts: int = 6, base_delay: float = 0.5) -> None:
        self.url, self.attempts, self.base_delay = url, attempts, base_delay
        self.policy = policy or Policy()
        self.retries = 0
        self.etags = EtagLog()
        _check_http_url(url)
        self.policy.check(url, url)
        # the request that opens the source: its size, its first bytes, and a time for the cost model
        t = time.perf_counter()
        status, msg, body = self._retry(lambda: self._get({"Range": f"bytes=0-{OPEN - 1}"}))
        dt = time.perf_counter() - t
        crange = msg.get("Content-Range") or ""
        m = re.fullmatch(r"(?i:bytes) (\d+)-(\d+)/(\d+)", crange.strip())
        if status == 206 and m and int(m[1]) == 0 and len(body) == int(m[2]) + 1:
            self.size = int(m[3])
        elif status == 416:  # an empty object
            self.size, body = 0, b""
        else:  # a 200 (whose body is not read): the server does not serve ranges; or an error
            raise _FatalError(f"{self.url}: HTTP {status} for a range of its first bytes")
        self.head = (0, body[:OPEN], dt)
        self.etag = self.etags.pin

    # ---- one request

    def _check(self, url: str) -> Check | None:
        if self.policy.allow_private_hosts:
            return None
        return lambda ip: self.policy.check_address(url, ip)

    def _get(self, headers: dict[str, str], multipart: bool = False) -> tuple[int, Message, bytes]:
        """One GET of the object (redirects followed and checked): (status, headers, body).
        With `multipart`, a body that is not multipart/byteranges (a 200 is the whole
        object) is not read: the body is b"" and the connection closed."""
        url = self.url
        headers = {**headers, "User-Agent": UA, "Accept-Encoding": "identity"}
        for hop in range(6):
            status, msg, body = _send(url, headers, self._check(url), self.policy.allow_unchecked_proxy, multipart)
            if status not in REDIRECTS:
                break
            locations = msg.get_all("Location") or []
            if len(locations) != 1:
                raise ResolutionError(f"{url}: HTTP {status} with {len(locations)} Location fields")
            new = urljoin(url, locations[0]).split("#", 1)[0]
            if urlparse(new).scheme.lower() not in ("http", "https"):
                raise ResolutionError(f"redirect to a non-http URL: {new}")
            _check_http_url(new)
            self.policy.check(self.url, new)
            if hop == 5:
                raise ResolutionError(f"{self.url}: more than 5 redirects")
            url = new
        self.url = url  # later requests go where the redirects led
        if status in RETRY:
            raise _RetryError(status, msg.get("Retry-After"))
        if status in (200, 206):
            self.etags.saw(msg.get("ETag"))
        return status, msg, body

    def _wait(self, attempt: int, retry_after: str | None) -> None:
        delay = self.base_delay * (2 ** attempt) * (0.5 + random.random())  # noqa: S311 (backoff jitter)
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                when = email.utils.parsedate_to_datetime(retry_after)
                delay = max(delay, when.timestamp() - time.time()) if when else delay
        self.retries += 1
        time.sleep(min(delay, 120))

    def _retry[T](self, f: Callable[[], T]) -> T:
        for attempt in range(self.attempts):
            try:
                return f()
            except (ResolutionError, _FatalError):
                raise
            except _RetryError as e:
                if attempt == self.attempts - 1:
                    raise OSError(f"{self.url}: HTTP {e.status} after {self.attempts} attempts") from None
                self._wait(attempt, e.retry_after)
            except (OSError, http.client.HTTPException):
                if attempt == self.attempts - 1:
                    raise
                self._wait(attempt, None)
        raise AssertionError("unreachable")

    def get(self, start: int, end: int) -> bytes:
        def once() -> bytes:
            status, _msg, body = self._get({"Range": f"bytes={start}-{end - 1}"})
            if status != 206 or len(body) != end - start:
                raise _FatalError(f"{self.url}: HTTP {status}, {len(body)} bytes for [{start}, {end})")
            return body

        return self._retry(once)

    def get_many(self, spans: list[tuple[int, int]]) -> list[bytes] | None:
        """Several ranges in one request (multipart/byteranges), or None when the server
        does not answer with them, or fails on the request."""
        header = "bytes=" + ",".join(f"{a}-{b - 1}" for a, b in spans)

        def once() -> dict[tuple[int, int], bytes] | None:
            status, msg, body = self._get({"Range": header}, multipart=True)
            ctype = msg.get("Content-Type") or ""
            if status != 206 or "multipart/byteranges" not in ctype:
                return None
            return _parts(body, ctype)

        # tried once: a server that does not answer it with multipart/byteranges, or fails
        # on it, gets single ranges instead (which retry)
        try:
            got = once()
        except ResolutionError:
            raise
        except (OSError, http.client.HTTPException, _RetryError):
            return None
        if got is None:
            return None
        out = []
        for a, b in spans:
            data = got.get((a, b))
            if data is None:
                raise OSError(f"{self.url}: the multipart answer lacks [{a}, {b})")
            out.append(data)
        return out

    def fetch(self, spans: list[tuple[int, int]]) -> tuple[list[bytes] | None, float]:
        t = time.perf_counter()
        got = [self.get(*spans[0])] if len(spans) == 1 else self.get_many(spans)
        return got, time.perf_counter() - t


def _send(url: str, headers: dict[str, str], check: Check | None, unchecked_proxy: bool,
          multipart: bool) -> tuple[int, Message, bytes]:
    """`refkv.reader._send` (one GET, not following redirects, its connection's address
    checked by `check`), except that a response other than a 206 (or, with
    `multipart`, other than multipart/byteranges) is closed before its body is
    read: a 200 is the whole object."""
    if _proxy(url) is not None:
        return reader_send(url, headers, check, unchecked_proxy)
    pu = urlparse(url)
    rest = url.split("://", 1)[1]
    origin = cast("re.Match[str]", re.match(r"[^/?#]*", rest))  # which every string matches
    target = rest[origin.end():].split("#", 1)[0]
    target = target if target.startswith("/") else "/" + target
    host = cast("str", pu.hostname)  # a checked http(s) URL has one (_check_http_url)
    for attempt in (0, 1):
        conn, reused = _POOL.get(pu.scheme, host, pu.port)
        try:
            if conn.sock is None:
                _connect(conn, check)
            if check is not None:
                check(cast("socket.socket", conn.sock).getpeername()[0])  # connected
            conn.request("GET", target, headers=headers)
            r = conn.getresponse()
            if r.status != 206 or (multipart and "multipart/byteranges" not in (r.getheader("Content-Type") or "")):
                conn.close()  # never read a body that is not the ranges (a 200 is the whole object)
                return r.status, r.msg, b""
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
            _POOL.put(pu.scheme, host, pu.port, conn)
        return r.status, r.msg, body
    raise AssertionError("unreachable")


def _parts(body: bytes, ctype: str) -> dict[tuple[int, int], bytes]:
    """The parts of a multipart/byteranges body, by (start, end)."""
    boundary = ctype.split("boundary=", 1)[1].strip().strip('"').encode()
    out: dict[tuple[int, int], bytes] = {}
    pos, sep = 0, b"--" + boundary
    while True:
        k = body.find(sep, pos)
        if k < 0 or body[k + len(sep):k + len(sep) + 2] == b"--":
            return out
        head_end = body.find(b"\r\n\r\n", k)
        if head_end < 0:
            return out
        start = end = None
        for line in body[k + len(sep):head_end].split(b"\r\n"):
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-range":
                rng = value.strip().split()[1].split(b"/")[0]
                a, b = rng.split(b"-")
                start, end = int(a), int(b) + 1
        if start is None or end is None:  # set together
            return out
        out[(start, end)] = body[head_end + 4:head_end + 4 + end - start]
        pos = head_end + 4 + end - start


class _FatalError(OSError):
    """A response no retry mends (a status other than 206 and those of RETRY)."""


class _RetryError(Exception):
    def __init__(self, status: int, retry_after: str | None) -> None:
        self.status, self.retry_after = status, retry_after


class Transport(Protocol):
    """What the planner reads a source through: `FileTransport`, `HttpTransport`, or a
    test's stand-in."""

    remote: bool
    size: int
    head: tuple[int, bytes, float]  # the bytes that opened the source: (offset, bytes, seconds)
    etag: Callable[[], str | None]

    def get(self, start: int, end: int) -> bytes:
        """Bytes [start, end) of the source."""
        ...

    def fetch(self, spans: list[tuple[int, int]]) -> tuple[list[bytes] | None, float]:
        """The bytes of `spans`, and the seconds the read took."""
        ...


def open_transport(location: str, policy: Policy | None = None) -> FileTransport | HttpTransport:
    """The transport of the source at `location`: an `HttpTransport` for an http(s) URL, read
    under the reader policy `policy` (default `Policy()`), which requests its first bytes now;
    otherwise a `FileTransport`, the local file at that path mapped into memory. Raises
    `refkv.errors.ResolutionError` when the policy refuses the URL, `FileNotFoundError` for a
    path that does not exist, and `OSError` when the first bytes cannot be read."""
    if location.startswith(("http://", "https://")):
        return HttpTransport(location, policy)
    return FileTransport(location)


CONNECTIONS = 8  # requests at a time to a remote host, by default (a browser has 6)


def concurrency(transport: Transport, connections: int | None = None) -> int:
    """Requests at a time: `connections` (default CONNECTIONS) for a remote source, 4
    for a local file; REFKV_CONCURRENCY overrides the default, for experiments."""
    if not transport.remote:
        return 4
    if connections:
        return connections
    env = os.environ.get("REFKV_CONCURRENCY")
    return int(env) if env else CONNECTIONS


def new_run(fmt: str, transport: Transport, conc: int | None = None, byte_cost: float | None = None) -> _core.Run:
    """A Rust run of format `fmt` over the transport, seeded with the bytes that opened
    it. `byte_cost`: seconds of wall time a byte fetched costs the planner (default:
    the core's, 0.1 s per MB); REFKV_BYTE_COST (seconds per MB) sets it for experiments."""
    amp = os.environ.get("REFKV_AMPLIFICATION")  # for experiments
    env_cost = os.environ.get("REFKV_BYTE_COST")
    if byte_cost is None and env_cost:
        byte_cost = float(env_cost) * 1e-6
    whole = os.environ.get("REFKV_WHOLE_BELOW")  # for experiments: the size read whole (bytes)
    run = _core.Run(fmt, transport.size, transport.remote, concurrency(transport, conc),
                      float(amp) if amp else None, byte_cost, int(whole) if whole else None)
    if os.environ.get("REFKV_MULTIRANGE") == "0":
        run.multirange = False
    o, head, dt = transport.head
    if head:
        run.seed(o, head)
        run.observe(len(head), dt)
    return run


async def drive_async(run: _core.Run, transport: Transport, conc: int | None = None) -> None:
    """Performs the requests a run asks for until it is done, at most `conc` at a time, without
    blocking the event loop: the transport's reads, which block, and the run's calls into the
    Rust core run on threads of the run's own (`conc` of them, so that the runs on one loop do
    not wait on each other's threads). A batch's requests are completed once all have
    answered, in order. `conc` is as `concurrency` reads it: a local file is always read 4 at a
    time, whatever `conc` is.

    Raises `refkv.zarrify.Rejected` when the run rejects the input, and whatever the
    transport's reads raise (`OSError`, `refkv.errors.ResolutionError`)."""
    conc = concurrency(transport, conc)
    loop = asyncio.get_running_loop()
    pool = ThreadPoolExecutor(conc, thread_name_prefix="refkv-read")

    def complete(results: list[tuple[list[bytes] | None, float]]) -> list[list[tuple[int, int]]] | None:
        """The batch's results given to the run, then the next batch."""
        for k, (parts, dt) in enumerate(results):
            run.complete(k, parts, dt)
        return run.poll()

    try:
        results: list[tuple[list[bytes] | None, float]] = []
        while True:
            try:
                reqs = await loop.run_in_executor(pool, complete, results)
            except _core.Rejected as e:
                raise Rejected(str(e)) from None
            if reqs is None:
                return
            results = list(await asyncio.gather(*(loop.run_in_executor(pool, transport.fetch, r) for r in reqs)))
    finally:
        # without waiting: a batch's other requests, after one failed, end on their threads
        pool.shutdown(wait=False, cancel_futures=True)


def drive(run: _core.Run, transport: Transport, conc: int | None = None) -> None:
    """`drive_async`, the caller's thread waiting for it (`refkv._sync.run`)."""
    _sync.run(drive_async(run, transport, conc))
