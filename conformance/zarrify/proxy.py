"""A caching HTTP server for the zarrification harness (HARNESS.md).

Serves, over plain HTTP:

* `/f/<name>`: the file `<name>` of a local directory (the synthetic fixtures);
* `/u/<id>/<name>`: the remote http(s) object whose URL is the base64url
  decoding of `<id>` (`<name>` is only there to keep the file's name visible);
* `/u/<id>/<key>`, when that URL ends in `/` (a remote store): the store's
  object `<key>`, at that URL followed by `<key>`.

Both answer `HEAD` (with `Content-Length`) and `GET` with a single
`Range: bytes=a-b` (206 with `Content-Range`); a `GET` without `Range` is
refused (403), since no zarrifier needs one. Remote bytes are fetched in
64 KiB blocks, once, and cached on disk, so that every implementation reads
the same bytes and upstream servers see each block only once.

Stores (spec/zarrify.md §1.4
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>, §1.5
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>) are listed with S3
ListObjectsV2, path style, in the buckets `f` and `u`: `GET /f/?list-type=2&prefix=n5/x/` lists
the fixture directory `n5/x/`, and `GET /u/?list-type=2&prefix=<id>/<rest>`
lists the remote store that `<id>` names (its own listing, fetched once and
cached), under `<rest>`, with keys rewritten to start with `<id>/`. Pages
hold `page_size` keys (default 100, so that the small fixtures exercise
pagination); the continuation token is opaque.

The proxy is a Hugging Face Hub endpoint too (spec/zarrify.md §1.5
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>), at `hub/` (the
`HF_ENDPOINT` compare.py gives the zarrifiers it runs): the model repository `f/fixtures` is the
fixture directory, at the commit `FIXTURES_COMMIT` (its revision `main`, or the commit itself),
so that `hub/f/fixtures/tree/main/n5/x/` is the fixture store `n5/x/` listed the Hub's way; any
other repository is the Hub's (`HUB`), its revision and tree fetched once and cached, its files
read through the block cache. Tree pages hold `hub_page_size` entries (default 16, so that the
fixture stores exercise pagination), directories among them,
and link the next page with a `Link` header whose URL is relative (the Hub's are absolute: both
resolve). A fixture file's `resolve` URL redirects to its `/f/` URL, as the Hub's redirect to
their storage.

An upstream request that fails, after `Upstream._open`'s retries, is answered as the
upstream server answered it when it says the object is not there (404 or 410: a zarrifier's
probe for a file beside the input expects that), and otherwise with 502; either way it is
recorded (`Failures`) under the `<id>` it was made for, so that compare.py can tell an input
its server did not serve from a zarrifier that crashed.

Usage: python conformance/zarrify/proxy.py <fixture dir> <cache dir> [port]
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import override
from xml.sax.saxutils import escape

BLOCK = 1 << 16
UA = "refkv-zarrify-harness"
ROOT = Path(__file__).resolve().parents[2]
HUB = "https://huggingface.co"
FIXTURES_REPO = ("models", "f", "fixtures")
FIXTURES_COMMIT = hashlib.sha1(b"refkv fixtures").hexdigest()  # noqa: S324 (a name, not a digest of anything)


def _store_api():
    """refkv.zarrify.store, imported from the source tree (the proxy runs without the package)."""
    sys.path.insert(0, str(ROOT / "python" / "src"))
    from refkv.zarrify import store  # noqa: PLC0415 (the source tree, on the path above)

    return store


def encode_id(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def decode_id(i: str) -> str:
    return base64.urlsafe_b64decode(i + "=" * (-len(i) % 4)).decode()


def upstream_failure(e: BaseException) -> tuple[int, str] | None:
    """(the status the proxy answers with, the kind of failure) for the error an upstream
    request raised: 404 and "missing" for an object the server says is not there (404, 410),
    502 and "outage" for any other HTTP status or a network failure (a timeout, a reset
    connection, a short block); None for an error that is not the upstream server's."""
    if isinstance(e, urllib.error.HTTPError):
        return (404, "missing") if e.code in (404, 410) else (502, "outage")
    if isinstance(e, OSError):  # URLError, ConnectionError, TimeoutError, Upstream.block's short block
        return 502, "outage"
    return None


class Failures:
    """The upstream requests that failed, by the `<id>` of the remote input they were made for
    (`/u/<id>/...`, and a listing's `prefix=<id>/...`)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.by_id: dict[str, list[tuple[str, str, str]]] = {}

    def add(self, sid: str, url: str, kind: str, reason: str) -> None:
        with self.lock:
            self.by_id.setdefault(sid, []).append((url, kind, reason))

    def unavailable(self, sid: str) -> str | None:
        """Why the remote input `sid` names could not be read, or None: an outage of any request
        made for it, or the input itself (a file, or a store's listing; a Hub repository's
        revision or tree, under the `sid` `hub:<kind>/<owner>/<repo>`) missing upstream. Another
        object missing (a file beside the input, a store's object) is a probe's answer, not this."""
        hub = sid.startswith("hub:")
        root = None if hub else decode_id(sid)
        with self.lock:
            failed = list(self.by_id.get(sid, ()))
        for url, kind, reason in failed:
            if kind == "outage" or url == root or (hub and "/api/" in url):
                return f"{url}: {reason}"
        return None


def write_whole(f: Path, data: bytes) -> None:
    """Write a cache file whole or not at all: the nightly saves the cache of a job killed
    mid-write too, and a truncated file would be read as complete by every later run."""
    tmp = f.with_name(f"{f.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_bytes(data)
    tmp.replace(f)


class Upstream:
    def __init__(self, cache: Path) -> None:
        self.cache = cache
        cache.mkdir(parents=True, exist_ok=True)
        # write_whole's, of a run killed mid-write: an hour old, so not another process's in flight
        # (the cache is shared)
        for tmp in cache.glob("*.tmp"):
            with contextlib.suppress(FileNotFoundError):  # swept by another process meanwhile
                if time.time() - tmp.stat().st_mtime > 3600:
                    tmp.unlink()
        self.lock = threading.Lock()
        self.sizes: dict[str, int] = {}

    def _open(self, req):
        for attempt in range(6):
            try:
                return urllib.request.urlopen(req, timeout=120)  # noqa: S310 (an http(s) corpus URL)
            except urllib.error.HTTPError as e:
                if e.code not in (429, 500, 502, 503, 504) or attempt == 5:
                    raise
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                if attempt == 5:
                    raise
            time.sleep(2**attempt)
        raise AssertionError("unreachable: the last attempt raises")

    def size(self, url: str) -> int:
        if url not in self.sizes:
            f = self.cache / (hashlib.sha256(url.encode()).hexdigest() + ".size")
            if f.exists():
                self.sizes[url] = int(f.read_text())
            else:
                req = urllib.request.Request(url, headers={"Range": "bytes=0-0", "User-Agent": UA})  # noqa: S310 (an http(s) corpus URL)
                with self._open(req) as r:
                    total = (r.headers.get("Content-Range") or "").rpartition("/")[2]
                    self.sizes[url] = int(total) if total.isdecimal() else int(r.headers["Content-Length"])
                write_whole(f, str(self.sizes[url]).encode())
        return self.sizes[url]

    def block(self, url: str, i: int) -> bytes:
        f = self.cache / f"{hashlib.sha256(url.encode()).hexdigest()}.{i}"
        start = i * BLOCK
        end = min(self.size(url), start + BLOCK)
        if f.exists() and len(cached := f.read_bytes()) == end - start:
            return cached
        req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end - 1}", "User-Agent": UA})  # noqa: S310 (an http(s) corpus URL)
        with self._open(req) as r:
            data = r.read()
        if len(data) != end - start:
            raise OSError(f"{url}: short block {i}")
        write_whole(f, data)
        return data

    def read(self, url: str, start: int, end: int) -> bytes:
        out = bytearray()
        pos = start
        while pos < end:
            b = self.block(url, pos // BLOCK)
            o = pos % BLOCK
            take = min(len(b) - o, end - pos)
            out += b[o : o + take]
            pos += take
        return bytes(out)


def local_listing(fixtures: Path, prefix: str) -> list[tuple[str, int]]:
    """The regular files under `fixtures` whose relative path starts with `prefix`."""
    base = prefix.rpartition("/")[0]
    top = fixtures / base if base else fixtures
    if not top.resolve().is_relative_to(fixtures.resolve()) or not top.is_dir():
        return []
    out = []
    for dirpath, dirnames, filenames in os.walk(top):
        dirnames[:] = [d for d in dirnames if not (Path(dirpath) / d).is_symlink()]
        for name in filenames:
            full = Path(dirpath) / name
            if full.is_symlink() or not full.is_file():
                continue
            key = full.relative_to(fixtures).as_posix()
            if key.startswith(prefix):
                out.append((key, full.stat().st_size))
    return sorted(out)


def listing_xml(prefix: str, keys: list[tuple[str, int]], token: str | None) -> bytes:
    parts = ['<?xml version="1.0" encoding="UTF-8"?>\n<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">',
             f"<Prefix>{escape(prefix)}</Prefix><KeyCount>{len(keys)}</KeyCount>",
             f"<IsTruncated>{'true' if token else 'false'}</IsTruncated>"]
    if token:
        parts.append(f"<NextContinuationToken>{escape(token)}</NextContinuationToken>")
    for k, n in keys:
        parts.append(f"<Contents><Key>{escape(k)}</Key><Size>{n}</Size><StorageClass>STANDARD</StorageClass></Contents>")
    parts.append("</ListBucketResult>")
    return "".join(parts).encode()


class RemoteListings:
    """Complete listings of remote stores, fetched once and cached on disk."""

    def __init__(self, upstream: Upstream) -> None:
        self.upstream = upstream

    def keys(self, store_url: str, rest: str) -> list[tuple[str, int]]:
        """(relative key, size) of the objects of the store at `store_url` whose
        relative key starts with `rest`."""
        store = _store_api()
        f = self.upstream.cache / (hashlib.sha256(f"list:{store_url}|{rest}".encode()).hexdigest() + ".json")
        if f.exists():
            return [tuple(x) for x in json.loads(f.read_text())]
        repo = store.hub_repo(store_url, HUB)
        if repo is not None:  # a Hub store: its files at the revision's commit, relative to its directory
            sha = store.parse_hub_revision(self.get(repo.revision_url()))
            prefix = repo.directory
            out = sorted((e["path"][len(prefix):], e["size"])
                         for e in self.tree(repo.api, sha, prefix[:-1]) if e["type"] == "file"
                         and e["path"].startswith(prefix + rest))
            write_whole(f, json.dumps(out).encode())
            return out
        endpoint, prefix = store.listing_endpoint(store_url)
        full = prefix + rest
        out, token = [], None
        while True:
            q = f"?list-type=2&prefix={store.query_encode(full)}"
            if token is not None:
                q += f"&continuation-token={store.query_encode(token)}"
            req = urllib.request.Request(endpoint + q, headers={"User-Agent": UA})  # noqa: S310 (an http(s) corpus URL)
            with self.upstream._open(req) as r:
                body = r.read()
            listed, token = store.parse_listing(body, full)
            out += [(k[len(prefix):], n) for k, n in listed]
            if token is None:
                break
        out.sort()
        write_whole(f, json.dumps(out).encode())
        return out

    def get(self, url: str) -> bytes:
        """The body of a GET of the Hub API URL `url`, fetched once and cached (a revision at a
        commit, or a tree page, does not change)."""
        f = self.upstream.cache / (hashlib.sha256(f"hub:{url}".encode()).hexdigest() + ".json")
        if f.exists():
            return f.read_bytes()
        with self.upstream._open(urllib.request.Request(url, headers={"User-Agent": UA})) as r:  # noqa: S310 (the Hub's API)
            body = r.read()
        write_whole(f, body)
        return body

    def tree(self, api: str, sha: str, directory: str) -> list[dict]:
        """Every entry of the Hub tree of the repository at `api` (its API URL), at the commit `sha`,
        under `directory` (no trailing /), every page followed and cached whole."""
        store = _store_api()
        url = f"{api}/tree/{sha}{'/' + store.object_url('', directory) if directory else ''}?recursive=true"
        f = self.upstream.cache / (hashlib.sha256(f"hubtree:{url}".encode()).hexdigest() + ".json")
        if f.exists():
            return json.loads(f.read_text())
        out: list[dict] = []
        link: str | None = url
        while link is not None:
            with self.upstream._open(urllib.request.Request(link, headers={"User-Agent": UA})) as r:  # noqa: S310 (the Hub's API)
                out += json.loads(r.read())
                link = store.next_link(r.headers.get_all("Link") or [], link, HUB)
        write_whole(f, json.dumps(out).encode())
        return out


def local_tree(fixtures: Path, directory: str) -> list[dict] | None:
    """The Hub tree entries of the fixture directory `directory` (relative, no trailing /, or
    empty), every file and directory under it, in path order; None if it is not a directory."""
    top = fixtures / directory if directory else fixtures
    if not top.resolve().is_relative_to(fixtures.resolve()) or not top.is_dir():
        return None
    out = []
    for dirpath, dirnames, filenames in os.walk(top):
        dirnames[:] = [d for d in dirnames if not (Path(dirpath) / d).is_symlink()]
        out.extend({"type": "directory", "size": 0, "path": (Path(dirpath) / d).relative_to(fixtures).as_posix()}
                   for d in dirnames)
        for name in filenames:
            full = Path(dirpath) / name
            if full.is_symlink() or not full.is_file():
                continue
            out.append({"type": "file", "size": full.stat().st_size, "path": full.relative_to(fixtures).as_posix()})
    return sorted(out, key=lambda e: e["path"])


def make_handler(fixtures: Path, upstream: Upstream, page_size: int = 100, failures: Failures | None = None,
                 hub_page_size: int = 16):
    remote = RemoteListings(upstream)
    failures = failures or Failures()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        @override
        def log_message(self, format, *args):
            pass

        def _remote(self) -> tuple[str, str] | None:
            """(`<id>`, the upstream URL) of a request for a remote object, or None."""
            parts = self.path.split("?")[0].split("/")
            if len(parts) < 3 or parts[1] != "u" or not parts[2]:
                return None
            sid = parts[2]
            url = decode_id(sid)
            key = "/".join(parts[3:])
            if url.endswith("/"):  # a remote store: the object under it
                if not key:
                    return None
                url += key
            elif key and key != url.split("?")[0].rsplit("/", 1)[-1]:
                # a file beside the remote file (a VSI slide's tile files, a detached NRRD
                # header's data file, probed by the zarrifiers at the file's directory)
                url = url.rsplit("/", 1)[0] + "/" + key
            return sid, url

        def _hub(self) -> bool:
            """Answers a request to the Hub endpoint at /hub/ (the module's docstring); False if
            this is not one."""
            path, _, query = self.path.partition("?")
            if not path.startswith("/hub/"):
                return False
            parts = [urllib.parse.unquote(x) for x in path[len("/hub/"):].split("/")]
            if parts[0] == "api":
                return self._hub_api(parts[1:], query)
            kind = "datasets" if parts[0] == "datasets" else "models"
            parts = parts[1:] if kind == "datasets" else parts
            if len(parts) < 5 or parts[2] != "resolve":
                return self._send(404, {}, b"not a Hub URL") or True
            owner, repo, _, sha, *rest = parts
            if (kind, owner, repo) == FIXTURES_REPO:
                if sha != FIXTURES_COMMIT:
                    return self._send(404, {}, b"no such revision") or True
                return self._send(302, {"Location": f"/f/{urllib.parse.quote('/'.join(rest))}"}) or True
            kind_path = "/datasets" if kind == "datasets" else ""
            url = f"{HUB}{kind_path}/{owner}/{repo}/resolve/{sha}/{urllib.parse.quote('/'.join(rest))}"
            self._range(f"hub:{kind}/{owner}/{repo}", url, upstream.size, upstream.read)
            return True

        def _hub_api(self, parts: list[str], query: str) -> bool:
            q = urllib.parse.parse_qs(query, keep_blank_values=True)
            if len(parts) < 5 or parts[0] not in ("models", "datasets") or parts[3] not in ("revision", "tree"):
                return self._send(404, {}, b"not a Hub API URL") or True
            kind, owner, repo, op, rev, *rest = parts
            sid = f"hub:{kind}/{owner}/{repo}"
            fixtures_repo = (kind, owner, repo) == FIXTURES_REPO
            api = f"{HUB}/api/{kind}/{owner}/{repo}"
            try:
                if op == "revision":
                    if fixtures_repo:
                        if rev not in ("main", FIXTURES_COMMIT):
                            return self._send(404, {}, b'{"error": "Invalid rev id"}') or True
                        body = json.dumps({"_id": "fixtures", "id": "f/fixtures", "sha": FIXTURES_COMMIT,
                                           "gated": False}).encode()
                    else:
                        body = remote.get(f"{api}/revision/{urllib.parse.quote(rev, safe='')}?{query}")
                    return self._send(200, {"Content-Type": "application/json"}, body) or True
                if q.get("recursive") != ["true"]:
                    return self._send(400, {}, b"recursive=true only") or True
                directory = "/".join(rest)
                if fixtures_repo:
                    entries = local_tree(fixtures, directory) if rev == FIXTURES_COMMIT else None
                    if entries is None:
                        return self._send(404, {}, b'{"error": "does not exist"}') or True
                else:
                    entries = remote.tree(api, rev, directory)
            except Exception as e:  # noqa: BLE001
                self._failed(e, sid, f"{api}/{op}/{rev}")
                return True
            start = int(q["cursor"][0]) if "cursor" in q else 0
            page = entries[start : start + hub_page_size]
            headers = {"Content-Type": "application/json"}
            if start + len(page) < len(entries):
                d = "/" + urllib.parse.quote(directory) if directory else ""
                after = f"/hub/api/{kind}/{owner}/{repo}/tree/{rev}{d}?recursive=true&cursor={start + len(page)}"
                headers["Link"] = f'<{after}>; rel="next"'
            self._send(200, headers, json.dumps(page).encode())
            return True

        def _target(self):
            parts = self.path.split("?")[0].split("/")
            if len(parts) >= 3 and parts[1] == "f":
                path = fixtures / urllib.request.url2pathname("/".join(parts[2:]))
                if not path.resolve().is_relative_to(fixtures.resolve()) or not path.is_file():
                    return None
                data = path.read_bytes()
                return len(data), lambda a, b: data[a:b]
            if (r := self._remote()) is not None:
                url = r[1]
                return upstream.size(url), lambda a, b: upstream.read(url, a, b)
            return None

        def _failed(self, e: Exception, sid: str | None, url: str) -> None:
            """Answers a request whose upstream request raised `e`, and records it under `sid`."""
            failure = upstream_failure(e)
            if failure is None:
                return self._send(500, {}, f"{type(e).__name__}: {e}".encode())
            if sid is not None:
                failures.add(sid, url, failure[1], f"{type(e).__name__}: {e}")
            return self._send(failure[0], {}, f"{type(e).__name__}: {e}".encode())

        def _fetch(self):
            """`_target()`, or None after answering the request when its upstream request failed."""
            try:
                return True, self._target()
            except Exception as e:  # noqa: BLE001 (answered: a client sees the status, not a dropped connection)
                r = self._remote()
                self._failed(e, *(r or (None, "")))
                return False, None

        def _send(self, status, headers, body=b""):
            self.send_response(status)
            headers = {"Content-Length": str(len(body)), **headers}
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _listing(self) -> bool:
            """Answers a ListObjectsV2 request (§1.5
            <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>); False
            if this is not one."""
            path, _, query = self.path.partition("?")
            if path not in ("/f/", "/u/") or not query:
                return False
            q = urllib.parse.parse_qs(query, keep_blank_values=True)
            if q.get("list-type") != ["2"]:
                self._send(400, {}, b"list-type=2 only")
                return True
            prefix = q.get("prefix", [""])[0]
            sid, _, rest = prefix.partition("/")
            store_url = ""
            try:
                if path == "/f/":
                    keys = local_listing(fixtures, prefix)
                else:
                    store_url = decode_id(sid)
                    keys = ([(f"{sid}/{k}", n) for k, n in remote.keys(store_url, rest)]
                            if store_url.endswith("/") else [])
            except Exception as e:  # noqa: BLE001
                # a store's listing failing is the store failing: recorded under the store's URL
                self._failed(e, sid if store_url else None, store_url)
                return True
            start = 0
            if "continuation-token" in q:
                start = int(base64.urlsafe_b64decode(q["continuation-token"][0]))
            page = keys[start : start + page_size]
            end = start + len(page)
            token = base64.urlsafe_b64encode(str(end).encode()).decode() if end < len(keys) else None
            self._send(200, {"Content-Type": "application/xml"}, listing_xml(prefix, page, token))
            return True

        def do_HEAD(self):
            answered, t = self._fetch()
            if not answered:
                return None
            if t is None:
                return self._send(404, {"Content-Length": "0"})
            self._send(200, {"Content-Length": str(t[0]), "Accept-Ranges": "bytes"})
            return None

        def _range(self, sid: str, url: str, size, read) -> None:
            """Answers a range request for the upstream object at `url`, recording a failure under `sid`."""
            try:
                n = size(url)
            except Exception as e:  # noqa: BLE001
                return self._failed(e, sid, url)
            return self._ranged(n, lambda a, b: read(url, a, b), sid, url)

        def do_GET(self):
            if self._listing() or self._hub():
                return None
            answered, t = self._fetch()
            if not answered:
                return None
            if t is None:
                return self._send(404, {}, b"not found")
            r = self._remote()
            return self._ranged(*t, *(r or (None, "")))

        def _ranged(self, size, read, sid, url):
            rng = self.headers.get("Range", "")
            if not rng.startswith("bytes=") or "," in rng:
                return self._send(403, {}, b"send a single Range")
            a, _, b = rng[6:].partition("-")
            if a == "":
                start, end = max(0, size - int(b)), size
            else:
                start, end = int(a), (size if b == "" else min(size, int(b) + 1))
            if start >= size or end <= start:
                return self._send(416, {"Content-Range": f"bytes */{size}"})
            try:
                body = read(start, end)
            except Exception as e:  # noqa: BLE001
                return self._failed(e, sid, url)
            self._send(206, {"Content-Range": f"bytes {start}-{end - 1}/{size}", "Accept-Ranges": "bytes"}, body)
            return None

    return Handler


class Proxy:
    """Runs the server in a thread; `base` is its URL, ending in "/"."""

    def __init__(self, fixtures: Path, cache: Path, port: int = 0, page_size: int = 100) -> None:
        self.failures = Failures()
        self.server = ThreadingHTTPServer(("127.0.0.1", port),
                                          make_handler(fixtures, Upstream(cache), page_size, self.failures))
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def unavailable(self, url: str) -> str | None:
        """Why the remote input this proxy serves at `url` (from `remote`, `remote_store` or
        `hub`) could not be read (`Failures.unavailable`), or None, as for a fixture."""
        if url.startswith(f"{self.base}hub/"):
            parts = url[len(self.base) + 4 :].split("/")
            kind, parts = ("datasets", parts[1:]) if parts[0] == "datasets" else ("models", parts)
            if (kind, *parts[:2]) == FIXTURES_REPO:
                return None
            return self.failures.unavailable(f"hub:{kind}/{parts[0]}/{parts[1]}")
        if not url.startswith(f"{self.base}u/"):
            return None
        return self.failures.unavailable(url[len(self.base) + 2 :].split("/", 1)[0])

    @property
    def hub_endpoint(self) -> str:
        """The proxy's Hub endpoint, for `HF_ENDPOINT`."""
        return f"{self.base}hub"

    def hub(self, url: str) -> str:
        """The proxy's URL for the Hub store, or file, at `url` (under https://huggingface.co/)."""
        if not url.startswith(f"{HUB}/"):
            raise ValueError(f"{url} is not on {HUB}")
        return f"{self.hub_endpoint}/{url[len(HUB) + 1:]}"

    def hub_fixture_store(self, name: str) -> str:
        """The Hub store URL of a fixture directory (`name` relative to the fixtures), as a
        directory of the repository f/fixtures."""
        return f"{self.hub_endpoint}/f/fixtures/tree/main/{urllib.parse.quote(name)}/"

    def local(self, name: str) -> str:
        return f"{self.base}f/{name}"

    def local_store(self, name: str) -> str:
        """The store URL of a fixture directory (`name` relative to the fixtures)."""
        return f"{self.base}f/{urllib.parse.quote(name)}/"

    def remote_store(self, url: str) -> str:
        """The proxy's store URL for the remote store at `url` (ending in /)."""
        return f"{self.base}u/{encode_id(url)}/"

    def remote(self, url: str) -> str:
        name = url.split("?", maxsplit=1)[0].rstrip("/").rsplit("/", 1)[-1] or "file"
        if name == "content":  # Zenodo: .../files/<name>/content
            name = url.rsplit("/files/", maxsplit=1)[-1].split("/", maxsplit=1)[0]
        return f"{self.base}u/{encode_id(url)}/{name}"


if __name__ == "__main__":
    p = Proxy(Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]) if len(sys.argv) > 3 else 8765)
    print(p.base, flush=True)
    threading.Event().wait()
