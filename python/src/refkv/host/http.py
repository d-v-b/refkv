"""The HTTP requests of the store readers (`refkv.host.stores`): retries of transient errors,
kept-alive connections per thread, and redirects followed under the reader policy."""

from __future__ import annotations

import http.client
import threading
import time
import urllib.error
import urllib.parse
from collections.abc import Callable
from email.message import Message

from refkv._rules import DEFAULTS
from refkv.errors import ResolutionError
from refkv.policy import Policy
from refkv.reader import _check_http_url, new_connection
from refkv.uri import is_uri_reference

__all__ = ["REDIRECTS", "follow"]


def _retry[T](f: Callable[[], T]) -> T:
    """Calls f, retrying transient network errors with backoff."""
    attempts: int = DEFAULTS["attempts"]
    for attempt in range(attempts):
        try:
            return f()
        except urllib.error.HTTPError as e:
            if e.code not in DEFAULTS["retry"] or attempt == attempts - 1:
                raise
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            if attempt == attempts - 1:
                raise
        time.sleep(2**attempt)
    raise AssertionError("unreachable")


class _ThreadConnections:
    """Kept-alive connections, one per thread and origin, with the interface of
    `refkv.reader._Pool`: each thread that reads closes its own (`release`) as it
    ends. A new TLS connection per range costs a round trip or more, which
    dominates many small reads, such as a SAFE product's band files'."""

    def __init__(self) -> None:
        self._local = threading.local()

    def _conns(self) -> dict[tuple[str, str, int | None], http.client.HTTPConnection]:
        conns: dict[tuple[str, str, int | None], http.client.HTTPConnection] | None
        conns = getattr(self._local, "conns", None)
        if conns is None:
            conns = {}
            self._local.conns = conns
        return conns

    def get(self, scheme: str, host: str, port: int | None) -> tuple[http.client.HTTPConnection, bool]:
        conn = self._conns().pop((scheme, host, port), None)
        if conn is not None:
            return conn, True
        return new_connection(scheme, host, port, timeout=120), False

    def put(self, scheme: str, host: str, port: int | None, conn: http.client.HTTPConnection) -> None:
        old = self._conns().pop((scheme, host, port), None)
        if old is not None:
            old.close()
        self._conns()[(scheme, host, port)] = conn

    def release(self) -> None:
        conns = self._conns()
        while conns:
            conns.popitem()[1].close()


REDIRECTS: tuple[int, ...] = tuple(DEFAULTS["redirects"])


def follow[M: Message | None](base: str, url: str, send: Callable[[str], tuple[int, M, bytes]],
                              policy: Policy) -> tuple[str, int, M, bytes]:
    """(url, status, headers, body) of the request `send(url)` makes, following at
    most 5 redirects: the policy checks `url`, and each redirect's target before it
    is requested, for the input at `base`. `send` does not follow redirects."""
    most: int = DEFAULTS["max_redirects"]
    for hop in range(most + 1):
        policy.check(url)
        status, msg, body = send(url)
        if status not in REDIRECTS:
            return url, status, msg, body
        headers: Message | None = msg
        locations = (headers.get_all("Location") if headers is not None else None) or []
        if len(locations) != 1:
            raise ResolutionError(f"{url}: HTTP {status} with {len(locations)} Location fields")
        if not is_uri_reference(locations[0]):
            raise ResolutionError(f"redirect with an invalid Location: {locations[0]!r}")
        new = urllib.parse.urljoin(url, locations[0]).split("#", 1)[0]
        if urllib.parse.urlsplit(new).scheme.lower() not in ("http", "https"):
            raise ResolutionError(f"redirect to a non-http URL: {new}")
        _check_http_url(new)
        if hop == most:
            raise ResolutionError(f"{base}: more than {most} redirects")
        url = new
    raise AssertionError("unreachable")
