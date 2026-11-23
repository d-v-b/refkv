"""Shared parts of the zarrifiers (spec/zarrify.md §1
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#1-introduction>, §2
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#2-the-zarr-layout>)."""

from __future__ import annotations

import http.client
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from email.message import Message
from pathlib import Path
from typing import TYPE_CHECKING

from refkv._files import replacing
from refkv._narrow import integer, string
from refkv.errors import ResolutionError
from refkv.pb import Concat, Range, Source
from refkv.policy import Policy
from refkv.reader import Registry, _check_http_url, new_connection
from refkv.refstore import ReferenceStore, bulk
from refkv.uri import file_path, file_uri, is_uri_reference
from refkv.uri import resolve as resolve_reference
from refkv.vzip.archive import VZipWriter
from refkv.zarrify.profiles import REGISTRY

if TYPE_CHECKING:
    from refkv import _core


class Rejected(Exception):  # noqa: N818 (public name)
    """The input is not accepted by the profile (spec/zarrify.md §1.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>). `format`: the Rust
    core's store format that refused it once a store run was started, or the profile that run
    decided (`ome-zarr` once the 0.4 test passed) for a rejection of the output's checks; else
    None."""

    def __init__(self, *args: object, format: str | None = None) -> None:  # noqa: A002 (the public keyword)
        super().__init__(*args)
        self.format = format


# A range of an output: (offset, length) of source 0, (source, offset, length)
# of any source, or literal bytes.
Part = tuple[int, int] | tuple[int, int, int] | bytes

@dataclass
class Output:
    """A zarrifier's output: the url source 0, the data sources after it,
    and the entries (§1.1
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#11-output-and-equivalence>, §1.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>)."""

    url: str
    bytes_entries: dict[str, bytes] = field(default_factory=dict)
    refs: dict[str, list[Part]] = field(default_factory=dict)
    summary: dict[str, object] = field(default_factory=dict)
    # The data sources 1, 2, ... in order of first use (§1.2
    # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>), and their indexes.
    data: dict[bytes, int] = field(default_factory=dict)
    # The documents under these prefixes are read only when asked for, like chunks:
    # those of the source metadata node, and of nodes a profile has one of per tile.
    lazy: tuple[str, ...] = ("refkv_source/",)
    # Source 0's pins (§1.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>),
    # and the CRC-32C of a url source's bytes, when the
    # ranges are to carry checksums (spec/refkv.md §4.2
    # <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#42-range>); set by
    # refkv.zarrify._zarrify.
    size: int | None = None
    etag: str | None = None
    checksum: Callable[[str, int, int], int] | None = None
    # The IR the output was projected from, which a plugin's layout is given (spec/layout.md
    # <https://github.com/d-v-b/refkv/blob/main/spec/layout.md>); set by refkv.ir.
    ir: _core.Ir | None = None

    def json(self, key: str, value: object) -> None:
        # UTF-8, not \u escapes: the documents are the size the budgets count, as JSON.stringify writes them.
        self.bytes_entries[key] = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()

    def shared(self, value: bytes) -> tuple[int, int, int]:
        """A range of all of `value`, as a data source (§1.2
        <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>): the source is
        added to the table the first time `value` is used."""
        value = bytes(value)
        i = self.data.setdefault(value, len(self.data) + 1)
        return (i, 0, len(value))

    def write(self, out_path: str) -> None:
        with replacing(out_path) as fh:
            w = VZipWriter(fh, page_size=1 << 16, checksum=self.checksum)
            src = w.source(Source(url=self.url, size=self.size, etag=self.etag))
            for value, i in self.data.items():
                if w.blob(value) != i:
                    raise AssertionError("data sources out of order")

            def rng(r: Part) -> Range:
                if isinstance(r, bytes):
                    return Range(data=r)
                if len(r) == 3:
                    return Range(source=r[0], offset=r[1], length=r[2])
                return Range(source=src, offset=r[0], length=r[1])

            for key in sorted(self.refs):
                w.add_ranges(key, [rng(r) for r in self.refs[key]])
            for key in sorted(self.bytes_entries):
                # The hierarchy's documents are read when the archive opens; those of
                # the source metadata node (and the lazy prefixes), like chunks, only when asked for.
                late = key.endswith("zarr.json") and not key.startswith(self.lazy)
                w.add_bytes(key, self.bytes_entries[key], compress=not late, late=late)
            w.close()

    def to_store(self, *, policy: Policy | None = None, registry: Registry | None = None,
                 info: dict[str, object] | None = None) -> ReferenceStore:
        """The output as a `refkv.ReferenceStore`, which `save_vzip` writes as the archive
        `write` writes, byte for byte (`reference_store`)."""
        sources = [Source(url=self.url, size=self.size, etag=self.etag)]
        for value, i in sorted(self.data.items(), key=lambda item: item[1]):
            if i != len(sources):
                raise AssertionError("data sources out of order")
            sources.append(Source(data=value))

        def rng(r: Part) -> Range:
            if isinstance(r, bytes):
                return Range(data=r)
            if len(r) == 3:
                return Range(source=r[0], offset=r[1], length=r[2])
            return Range(source=0, offset=r[0], length=r[1])

        refs = [(key, [rng(r) for r in self.refs[key]]) for key in sorted(self.refs)]
        entries = [(key, self.bytes_entries[key],
                    key.endswith("zarr.json") and not key.startswith(self.lazy))
                   for key in sorted(self.bytes_entries)]
        return reference_store(refs, entries, sources, checksum=self.checksum, policy=policy, registry=registry,
                               info=info)


PAGE_SIZE = 1 << 16  # the page index of a zarrified archive (spec/bindings/vzip.md §12
# <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#12-page-index-optional>)


def reference_store(refs: Iterable[tuple[str, list[Range]]], entries: Iterable[tuple[str, bytes, bool]],
                    sources: list[Source], *, checksum: Callable[[str, int, int], int] | None = None,
                    policy: Policy | None = None, registry: Registry | None = None,
                    info: dict[str, object] | None = None) -> ReferenceStore:
    """A profile output as a `refkv.ReferenceStore`, in the layout every output's
    `write` gives its archive: the references (key, ranges) first, then the bytes
    entries (key, bytes, late), in the order given; a late entry is read when the
    archive opens (`read_at_open`), and every other one is compressed. With
    `checksum(url, offset, length)`, each range of a url source carries the CRC-32C
    of its bytes (spec/refkv.md §4.2
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#42-range>). The store
    reads its sources under `policy`, with the stores of `registry`."""
    def checked(r: Range) -> Range:
        if checksum is None or r.data is not None or r.crc32c is not None:
            return r
        url = sources[r.source].url
        return r if url is None else Range(r.source, r.offset, r.length, None, checksum(url, r.offset, r.length))

    mapping: dict[str, bytes | Range | Concat] = {}
    late: list[str] = []
    compressed: list[str] = []
    with bulk():
        for key, ranges in refs:
            if key in mapping:
                raise ValueError(f"duplicate key {key!r}")
            done = [checked(r) for r in ranges]
            mapping[key] = done[0] if len(done) == 1 else Concat(tuple(done))
        for key, data, is_late in entries:
            if key in mapping:
                raise ValueError(f"duplicate key {key!r}")
            mapping[key] = data
            (late if is_late else compressed).append(key)
        return ReferenceStore(mapping, sources, read_at_open=late, compressed=compressed, page_size=PAGE_SIZE,
                              policy=reading_own_files(policy, sources), registry=registry, info=info)


def reading_own_files(policy: Policy | None, sources: Iterable[Source]) -> Policy:
    """`policy` (default `Policy()`), which also lets the store a zarrifier just built read the
    local files it names: the input its caller named and the files that input led it to, each
    below the input's directory (spec/zarrify.md §1.9
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#19-sources-beside-the-input>).
    It allows the directory that holds them all, as a prefix, so that a file replaced by a link
    after the store was built is not read through it (spec/refkv.md §6.7
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy> rule 2); a store
    that also names other sources allows each of them by its URL as well, since a prefix list
    replaces rule 1, which allowed them. A saved store does not keep this: whoever opens it
    chooses (rule 1)."""
    policy = policy or Policy()
    if policy.prefixes is not None:
        return policy
    base = file_uri(Path.cwd()) + "/"
    urls = [resolve_reference(base, s.url) for s in sources if s.url is not None]
    files = [u for u in urls if u.lower().startswith("file:")]
    if not files:
        return policy
    paths = [file_path(u, allow_host=policy.allow_file_hosts) for u in files]
    directory = file_uri(os.path.commonpath([str(Path(p).parent) for p in paths]))
    if not directory.endswith("/"):  # a root's URL ends with one already: file:///
        directory += "/"
    others = dict.fromkeys(u for u in urls if not u.lower().startswith("file:"))
    return replace(policy, prefixes=(directory, *others))


MAX_PAYLOAD = 65519
SOURCE_NODE = "refkv_source"  # the source metadata node (spec/conventions.md §2 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#2-attributes>)


# The zarrification conventions (conventions §2
# <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#2-attributes>): one per profile, each
# with its fixed UUID, its current version and its name in the description, from the profile
# registry (spec/zarrify/profiles/, as `refkv.zarrify.profiles` reads it from the core).
CONVENTION_KEY = "refkv_zarrified"
PROFILES = {string(e["name"]): (string(e["uuid"]), integer(e["version"]), string(e["title"])) for e in REGISTRY}
UUIDS = frozenset(uuid for uuid, _, _ in PROFILES.values())


def convention(profile: str) -> dict[str, object]:
    """The Convention Metadata Object of a profile's convention (conventions §2
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#2-attributes>)."""
    uuid, version, title = PROFILES[profile]
    # Version 0 has no tag: its URLs name the development branch (conventions §1
    # <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#1-conventions>).
    ref, blob = ("heads/main", "main") if version == 0 else (f"tags/zarrify-{profile}-v{version}",) * 2
    return {
        "uuid": uuid,
        "schema_url": f"https://raw.githubusercontent.com/d-v-b/refkv/refs/{ref}/spec/zarrify/{profile}/schema.json",
        "spec_url": f"https://github.com/d-v-b/refkv/blob/{blob}/spec/zarrify/{profile}.md",
        "name": CONVENTION_KEY,
        "description": f"The Zarr layout of a {title} source zarrified by refkv, and the source's metadata",
    }


def _retry[T](f: Callable[[], T]) -> T:
    """Calls f, retrying transient network errors with backoff."""
    for attempt in range(5):
        try:
            return f()
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == 4:
                raise
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            if attempt == 4:
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


REDIRECTS = (301, 302, 303, 307, 308)


def follow[M: Message | None](base: str, url: str, send: Callable[[str], tuple[int, M, bytes]],
                              policy: Policy) -> tuple[str, int, M, bytes]:
    """(url, status, headers, body) of the request `send(url)` makes, following at
    most 5 redirects: the policy checks `url`, and each redirect's target before it
    is requested, for the input at `base`. `send` does not follow redirects."""
    for hop in range(6):
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
        if hop == 5:
            raise ResolutionError(f"{base}: more than 5 redirects")
        url = new
    raise AssertionError("unreachable")


class EtagLog:
    """The ETag of every response for one object, for its `etag` pin (spec/zarrify.md §1.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>)."""

    def __init__(self) -> None:
        self.seen: set[str | None] = set()
        self._lock = threading.Lock()

    def saw(self, etag: str | None) -> None:
        with self._lock:
            self.seen.add(etag)
            strong = {e for e in self.seen if e is not None and not e.startswith("W/")}
        if len(strong) > 1:  # the object changed while it was read: a failure, not a rejection
            raise OSError(f"the object changed while it was read (ETags {sorted(strong)})")

    def pin(self) -> str | None:
        """The object's strong ETag, if every response gave the same one, else None."""
        if len(self.seen) != 1:
            return None
        (etag,) = self.seen
        return etag if etag is not None and STRONG_ETAG.match(etag) else None


STRONG_ETAG = re.compile(r'^"[\x21\x23-\x7e]*"$')


# Some servers (Mendeley Data) refuse urllib's default User-Agent; the store reads send this one.
UA = "refkv-zarrify"
