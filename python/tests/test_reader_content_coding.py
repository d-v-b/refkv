"""A range of an object stored with a content coding (spec/refkv.md §5.2
<https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#52-reading-over-http>): one test of the
response the reader accepts, and one per response it still refuses."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import override

import pytest

from refkv.errors import ResolutionError
from refkv.reader import Source, http_range


class CodedHandler(BaseHTTPRequestHandler):
    """Serves one object, stored coded, as an object store does: a range of the stored bytes,
    with the Content-Encoding it was uploaded with. The path says what else to do."""

    BODY = b"coded bytes"

    def do_GET(self):
        if self.path == "/compressed":  # a proxy's compression: no range, another length
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"zz")
            return
        total = 999 if self.path == "/other-total" else len(self.BODY)
        self.send_response(206)
        self.send_header("Content-Encoding", "br")
        self.send_header("Content-Range", f"bytes 0-{len(self.BODY) - 1}/{total}")
        self.send_header("Content-Length", str(len(self.BODY)))
        self.end_headers()
        self.wfile.write(self.BODY)

    @override
    def log_message(self, format, *args):
        pass


@pytest.fixture
def coded_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), CodedHandler)
    Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_a_range_of_a_coded_object_reads_when_its_total_is_the_size_pin(coded_server):
    url = f"{coded_server}/x"
    body, size = http_range(url, 0, len(CodedHandler.BODY), Source(url=url, size=len(CodedHandler.BODY)))
    assert (body, size) == (CodedHandler.BODY, len(CodedHandler.BODY))


def test_a_coded_response_without_a_size_pin_is_refused(coded_server):
    url = f"{coded_server}/x"
    with pytest.raises(ResolutionError, match="response has Content-Encoding 'br'"):
        http_range(url, 0, len(CodedHandler.BODY), Source(url=url))


def test_a_coded_response_of_another_total_is_refused(coded_server):
    url = f"{coded_server}/other-total"
    with pytest.raises(ResolutionError, match="response has Content-Encoding 'br'"):
        http_range(url, 0, len(CodedHandler.BODY), Source(url=url, size=len(CodedHandler.BODY)))


def test_a_compressing_proxys_response_is_refused(coded_server):
    url = f"{coded_server}/compressed"
    with pytest.raises(ResolutionError, match="response has Content-Encoding 'gzip'"):
        http_range(url, 0, 2, Source(url=url, size=2))
