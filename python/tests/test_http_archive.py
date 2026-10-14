"""An archive at an http(s) URL, read with refkv's own HTTP client
(`refkv.reader.HttpObjects`), which reads its sources too: the same connections,
redirect rules and address checks (spec §5.2
<https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#52-reading-over-http>, and §6.7
<https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy> rule 3, which refkv
applies to the archive's own host too)."""

import socket
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import refkv
import refkv.reader
from refkv._sync import run
from refkv.errors import ArchiveError, ResolutionError
from refkv.pb import Concat, Range, Source
from refkv.policy import Policy
from refkv.reader import HttpObjects, Resolver

BLOB = bytes(range(256)) * 16  # 4 KiB
# The test server is on 127.0.0.1, which rule 3 refuses by default.
SERVED = Policy(allow_private_hosts=True)


@dataclass(frozen=True)
class ServerOnly(Policy):
    """The default policy, but for the test server's address: rule 3 still refuses
    every other non-public address."""

    def check_address(self, url: str, address: str) -> None:
        if address != "127.0.0.1":
            super().check_address(url, address)


@pytest.fixture
def no_proxy(monkeypatch):
    for name in ["http_proxy", "https_proxy", "all_proxy", "no_proxy"]:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)


@pytest.fixture
def names(monkeypatch):
    """Host names the reader resolves to the addresses given (`names[host] = ip`)."""
    table: dict[str, str] = {}
    real = socket.getaddrinfo

    def fake(host, port, *args, **kwargs):
        if host in table:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (table[host], port))]
        return real(host, port, *args, **kwargs)

    monkeypatch.setattr(refkv.reader, "_getaddrinfo", fake)
    return table


@pytest.fixture
def server(no_proxy):
    """Serves `state["files"]` (path -> bytes) with range requests (`a-b` and `-n`) and
    `state["etag"]`. A first path segment changes that: `/norange/...` ignores Range
    (200, the whole object), `/status/<code>/...` answers that status, and `/moved/...`
    redirects to `state["location"]`. Counts requests, and records their paths."""
    state = {"files": {}, "etag": '"v1"', "location": "/", "paths": []}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, status, body=b"", headers=()):
            self.send_response(status)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            state["paths"].append(self.path)
            path, ranged = self.path, True
            etag = [("ETag", state["etag"])] if state["etag"] else []
            if path.startswith("/moved/"):
                return self._send(302, headers=[("Location", state["location"])])
            if path.startswith("/status/"):
                return self._send(int(path.split("/")[2]), b"no")
            if path.startswith("/norange/"):
                path, ranged = path[len("/norange"):], False
            data = state["files"].get(path.split("?", 1)[0])
            if data is None:
                return self._send(404, b"not found")
            if not ranged:
                return self._send(200, data, etag)
            spec = self.headers["Range"].removeprefix("bytes=")
            a, b = spec.split("-")
            if a == "":
                a, b = max(len(data) - int(b), 0), len(data) - 1
            else:
                a, b = int(a), min(int(b), len(data) - 1)
            if a >= len(data):
                return self._send(416, headers=[("Content-Range", f"bytes */{len(data)}")])
            self._send(206, data[a:b + 1],
                       [("Content-Range", f"bytes {a}-{b}/{len(data)}"), *etag])

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", state
    srv.shutdown()
    srv.server_close()


def _serve_archive(base, state, tmp_path, *, page_size=None, sources=None) -> str:
    """An archive of bytes values and references to /blob.bin on the server, written to
    tmp_path/a.vzip and served at /a.vzip: its local path."""
    state["files"]["/blob.bin"] = BLOB
    sources = sources or [Source(url=f"{base}/blob.bin")]
    mapping = {f"b/{i:03}": bytes([i]) * (i + 1) for i in range(40)}
    mapping.update({f"r/{i:03}": Range(source=0, offset=37 * i, length=50) for i in range(40)})
    mapping["c"] = Concat((Range(data=b"<"), Range(source=0, offset=4000, length=96)))
    path = tmp_path / "a.vzip"
    refkv.ReferenceStore(mapping, sources).save(path, page_size=page_size, policy=SERVED)
    state["files"]["/a.vzip"] = path.read_bytes()
    return str(path)


def _everything(store) -> dict:
    keys = store.list()
    return {"keys": keys, "values": {k: store.get(k) for k in keys},
            "raw": {k: store.raw(k) for k in keys}, "sources": store.sources}


# ------------------------------------------------------------ reads


@pytest.mark.parametrize("page_size", [None, 256], ids=["unpaged", "paged"])
@pytest.mark.parametrize("route", ["/a.vzip", "/a.vzip?sig=x", "/norange/a.vzip", "/moved/a.vzip"])
def test_reads_an_archive_over_http(server, tmp_path, page_size, route):
    """An archive read over HTTP has the keys, values, raw values and sources of the
    same archive read from disk: unpaged and paged, with a query (which reaches the
    server), from a server that ignores Range (200, the whole object), and through a
    redirect. The reads are the reader's own requests, counted in its Stats."""
    base, state = server
    local = refkv.open(_serve_archive(base, state, tmp_path, page_size=page_size), policy=SERVED)
    state["location"] = "/a.vzip"
    stats = refkv.Stats()
    remote = refkv.open(base + route, policy=SERVED, stats=stats)
    assert _everything(remote) == _everything(local)
    assert stats.archive_requests >= 1
    assert all(p.split("?")[0].endswith(("a.vzip", "blob.bin")) for p in state["paths"])
    if "?" in route:
        assert route in state["paths"]


def test_http_objects_reads_ranges_and_suffixes(server):
    """`HttpObjects` reads as `LocalFiles` does: a range cut at the end of the object,
    and a suffix with the object's size, from a server that serves ranges and one
    that ignores them."""
    base, state = server
    state["files"]["/f.bin"] = bytes(range(10))
    objects = HttpObjects(base, SERVED)
    ranges = {(0, 10): bytes(range(10)), (2, 5): bytes([2, 3, 4]), (8, 20): bytes([8, 9]),
              (4, 4): b""}
    for path in ["/f.bin", "/norange/f.bin"]:
        for (start, end), want in ranges.items():
            assert run(objects.get_range_async(path, start=start, end=end)) == want, (path, start)
        for n, want in {3: bytes([7, 8, 9]), 10: bytes(range(10)), 64: bytes(range(10))}.items():
            got = run(objects.get_async(path, options={"range": {"suffix": n}}))
            assert (run(got.buffer_async()), got.meta["size"]) == (want, 10), (path, n)
    store, path = Resolver().resolve(f"{base}/d/f%20g.bin?x=1#frag", policy=SERVED)
    assert (type(store), store.origin, path) == (HttpObjects, base, "/d/f%20g.bin?x=1")


# ------------------------------------------------------------ errors


def test_refuses_an_archive_on_a_private_host(server, tmp_path, names):
    """Rule 3 applies to the archive's own host: a loopback literal, and a name that
    resolves to a loopback address, are refused by default, before any request;
    allow_private_hosts allows them."""
    base, state = server
    local = refkv.open(_serve_archive(base, state, tmp_path), policy=SERVED)
    port = base.rsplit(":", 1)[1]
    names["archive.test"] = "127.0.0.1"
    for url in [f"{base}/a.vzip", f"http://localhost:{port}/a.vzip",
                f"http://archive.test:{port}/a.vzip"]:
        with pytest.raises(ArchiveError, match="is a loopback address.*allow_private_hosts=True"):
            refkv.open(url).list()
        assert state["paths"] == []
        assert _everything(refkv.open(url, policy=SERVED)) == _everything(local)
        state["paths"].clear()


def test_refuses_a_redirect_of_the_archive_to_a_private_address(server, tmp_path, names):
    """A redirect of the archive to a refused address, written as an IP literal or a
    name that resolves to one, is refused before the request to it."""
    base, state = server
    _serve_archive(base, state, tmp_path)
    port = base.rsplit(":", 1)[1]
    names["archive.test"] = "127.0.0.1"
    names["metadata.test"] = "169.254.169.254"
    policy = ServerOnly()
    assert refkv.open(f"http://archive.test:{port}/a.vzip", policy=policy).list()
    for location, cls in [("http://169.254.169.254/a.vzip", "link-local"),
                          (f"http://metadata.test:{port}/a.vzip", "link-local"),
                          (f"http://127.0.0.2:{port}/a.vzip", "loopback")]:
        state["location"], state["paths"] = location, []
        with pytest.raises(ArchiveError, match=f"is a {cls} address"):
            refkv.open(f"http://archive.test:{port}/moved/a.vzip", policy=policy).list()
        assert state["paths"] == ["/moved/a.vzip"]  # the redirect only


def test_refuses_an_archive_through_a_proxy(server, tmp_path, names, monkeypatch):
    """Rule 3 cannot check where a proxy connects, so an archive whose request would
    go through one is refused, naming the opt-out; with it, the proxy (here the test
    server) serves the archive."""
    base, state = server
    local = refkv.open(_serve_archive(base, state, tmp_path), policy=SERVED)
    names["public.test"] = "93.184.215.14"
    monkeypatch.setenv("http_proxy", base)
    state["files"]["http://public.test/a.vzip"] = state["files"]["/a.vzip"]
    with pytest.raises(ArchiveError, match="through the proxy.*allow_unchecked_proxy=True"):
        refkv.open("http://public.test/a.vzip").list()
    assert state["paths"] == []
    store = refkv.open("http://public.test/a.vzip", policy=Policy(allow_unchecked_proxy=True))
    assert store.list() == local.list()
    assert state["paths"][0] == "http://public.test/a.vzip"  # an absolute target: the proxy


@pytest.mark.parametrize("pin", ["etag", "size"])
def test_detects_a_pin_mismatch_of_a_source_of_an_http_archive(server, tmp_path, pin):
    """A source of an archive read over HTTP is read with its pins checked: an ETag
    or a size that changed since the archive was written fails its read."""
    base, state = server
    src = (Source(url=f"{base}/blob.bin", etag='"v1"') if pin == "etag"
           else Source(url=f"{base}/blob.bin", size=len(BLOB)))
    _serve_archive(base, state, tmp_path, sources=[src])
    store = refkv.open(f"{base}/a.vzip", policy=SERVED)
    assert store.get("r/000") == BLOB[:50]
    if pin == "etag":
        state["etag"] = '"v2"'
    else:
        state["files"]["/blob.bin"] = BLOB + b"more"
    with pytest.raises(ResolutionError, match="the source changed"):
        store.get("r/001")


@pytest.mark.parametrize("status", [403, 404, 500])
def test_reports_an_http_error_status_of_the_archive(server, status):
    base, state = server
    with pytest.raises(ArchiveError, match=f"/status/{status}/a.vzip: HTTP {status}"):
        refkv.open(f"{base}/status/{status}/a.vzip", policy=SERVED).list()


def test_http_objects_refuses_a_range_past_the_end(server):
    base, state = server
    state["files"]["/f.bin"] = b"abc"
    with pytest.raises(ResolutionError, match=r"shorter than the range \(416\)"):
        run(HttpObjects(base, SERVED).get_range_async("/f.bin", start=4, end=6))


def test_http_objects_reads_only_a_suffix_with_get(server):
    base, _ = server
    with pytest.raises(NotImplementedError, match="reads a suffix"):
        run(HttpObjects(base, SERVED).get_async("/f.bin", options={"range": {"offset": 1}}))
