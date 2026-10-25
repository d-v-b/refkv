"""An input file no IR format recognizes is rejected by its first bytes (spec/zarrify.md
§1.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>), from the
request that opens the source: no second read of a remote one."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import override

import pytest

from refkv.policy import Policy
from refkv.zarrify import NOT_SUPPORTED, Rejected, zarrify

BODY = b"not an image file " * 100


@pytest.fixture
def server():
    """A local range server serving BODY at `/f.bin`, recording each request's Range header."""
    ranges: list[str | None] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        @override
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            ranges.append(self.headers.get("Range"))
            a, b = (int(x) for x in self.headers["Range"][6:].split("-"))
            b = min(b, len(BODY) - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {a}-{b}/{len(BODY)}")
            self.send_header("Content-Length", str(b + 1 - a))
            self.end_headers()
            self.wfile.write(BODY[a:b + 1])

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/f.bin", ranges
    srv.shutdown()
    srv.server_close()


def test_rejects_an_unrecognized_file(server, tmp_path, monkeypatch):
    """A local file and an http(s) one are rejected with the same message; the http
    one after its opening request alone."""
    for name in ["http_proxy", "https_proxy", "all_proxy", "no_proxy"]:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    path = tmp_path / "f.bin"
    path.write_bytes(BODY)
    url, ranges = server
    for location, policy in [(str(path), None), (url, Policy(allow_private_hosts=True))]:
        with pytest.raises(Rejected) as e:
            zarrify(location, policy=policy)
        assert str(e.value) == NOT_SUPPORTED
    assert len(ranges) == 1
    assert ranges[0].startswith("bytes=0-")
