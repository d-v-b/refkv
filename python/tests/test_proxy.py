"""conformance/zarrify/proxy.py: an upstream request that fails is answered, not dropped, and
recorded under the input it was made for, so that compare.py can tell an input its server did
not serve from a zarrifier that crashed."""

import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import override

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "conformance" / "zarrify"))
import proxy  # noqa: E402

DATA = bytes(range(256)) * 4


class Origin(BaseHTTPRequestHandler):
    """An upstream server: /file.bin and /store/a are DATA, /down.bin answers 503, and
    anything else 404."""

    protocol_version = "HTTP/1.1"

    @override
    def log_message(self, format, *args):
        pass

    def _answer(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/down.bin":
            return self._answer(503)
        if self.path not in ("/file.bin", "/store/a"):
            return self._answer(404)
        a, _, b = self.headers["Range"][6:].partition("-")
        start, end = int(a), int(b) + 1
        return self._answer(206, DATA[start:end], {"Content-Range": f"bytes {start}-{end - 1}/{len(DATA)}"})


@pytest.fixture
def origin():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def status(url: str, method: str = "HEAD") -> int:
    req = urllib.request.Request(url, method=method, headers={"Range": "bytes=0-9"})  # noqa: S310 (as below)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 (the proxy, over http)
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_upstream_failure():
    def http(code: int) -> urllib.error.HTTPError:
        return urllib.error.HTTPError("https://x.test/f", code, "", None, None)  # type: ignore[arg-type]

    cases = [
        (http(404), (404, "missing")),
        (http(410), (404, "missing")),
        (http(403), (502, "outage")),
        (http(503), (502, "outage")),
        (urllib.error.URLError("timed out"), (502, "outage")),
        (TimeoutError(), (502, "outage")),
        (ConnectionResetError(), (502, "outage")),
        (OSError("short block 3"), (502, "outage")),
        (ValueError("a bug"), None),
    ]
    for e, want in cases:
        assert proxy.upstream_failure(e) == want, e


def test_failures_unavailable():
    file, store = "https://x.test/d/a.vsi", "https://x.test/s/"
    f, s = proxy.encode_id(file), proxy.encode_id(store)
    failures = proxy.Failures()
    assert failures.unavailable(f) is None  # nothing failed
    failures.add(f, "https://x.test/d/_a_/stack1/frame_t.ets", "missing", "HTTPError: 404")
    assert failures.unavailable(f) is None  # a probe's answer
    failures.add(s, "https://x.test/s/a/attributes.json", "missing", "HTTPError: 404")
    assert failures.unavailable(s) is None  # a store's object: also a probe's answer
    failures.add(f, file, "missing", "HTTPError: 404")
    assert failures.unavailable(f) == f"{file}: HTTPError: 404"  # the input itself
    failures.add(s, "https://x.test/s/a/0/0", "outage", "TimeoutError: timed out")
    assert failures.unavailable(s) == "https://x.test/s/a/0/0: TimeoutError: timed out"  # any outage
    other = proxy.Failures()
    other.add(s, store, "missing", "HTTPError: 404")
    assert other.unavailable(s) == f"{store}: HTTPError: 404"  # the store's listing


def test_proxy_answers_and_records_an_upstream_failure(origin, tmp_path, monkeypatch):
    monkeypatch.setattr(proxy.time, "sleep", lambda _s: None)  # Upstream._open's backoff
    p = proxy.Proxy(tmp_path, tmp_path / "cache")
    served, missing, down = (p.remote(f"{origin}/{n}") for n in ("file.bin", "gone.bin", "down.bin"))
    beside = served.rsplit("/", 1)[0] + "/beside.bin"
    assert [status(served), status(served, "GET")] == [200, 206]
    assert status(beside) == 404  # a probe's 404 is passed on, and is not the input's
    assert p.unavailable(served) is None
    assert [status(missing), status(missing, "GET")] == [404, 404]
    assert p.unavailable(missing) == f"{origin}/gone.bin: HTTPError: HTTP Error 404: Not Found"
    assert status(down) == 502
    assert p.unavailable(down) == f"{origin}/down.bin: HTTPError: HTTP Error 503: Service Unavailable"
    assert p.unavailable(p.local("tiff/a.tif")) is None  # a fixture has no upstream


def test_proxy_serves_fixture_stores_as_a_hub_repository(tmp_path):
    # the fixture repository f/fixtures (spec/zarrify.md §1.5
    # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>), its tree in
    # pages linked by relative next links, and its files redirected to /f/
    (tmp_path / "s" / "a").mkdir(parents=True)
    for i in range(20):
        (tmp_path / "s" / "a" / str(i)).write_bytes(DATA[:i])
    p = proxy.Proxy(tmp_path, tmp_path / "cache")
    sys.path.insert(0, str(ROOT / "python" / "src"))
    from refkv.policy import Policy  # noqa: PLC0415 (the package, after the path above)
    from refkv.zarrify.store import HubStore, open_store  # noqa: PLC0415 (as above)

    store = open_store(p.hub_fixture_store("s"), policy=Policy(allow_private_hosts=True), hub=p.hub_endpoint)
    assert isinstance(store, HubStore)
    assert store.commit == proxy.FIXTURES_COMMIT
    assert store.url == f"{p.hub_endpoint}/f/fixtures/resolve/{proxy.FIXTURES_COMMIT}/s/"
    assert store.objects == {f"a/{i}": i for i in range(20)}
    assert store.requests == 3  # the revision, and 21 entries in pages of 16
    assert store.read("a/7") == DATA[:7]
    assert p.unavailable(p.hub_fixture_store("s")) is None
    assert status(f"{p.hub_endpoint}/api/models/f/fixtures/revision/v9", "GET") == 404
    assert status(f"{p.hub_endpoint}/api/models/f/fixtures/tree/{proxy.FIXTURES_COMMIT}/t?recursive=true", "GET") == 404
