"""The zarrifiers' pins and range checksums (spec/zarrify.md §1.2
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>, §1.4
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>;
spec/refkv.md §4.2 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#42-range>, §5.1
<https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#51-pins>)."""

import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, override

import pytest

from refkv._narrow import string
from refkv.pb import Concat, Range, Source, decode_table
from refkv.policy import Policy, crc32c
from refkv.vzip.archive import Entry, parse_central_directory, parse_tail
from refkv.vzip.store import VZipStore
from refkv.zarrify import zarrify

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
TIFF = FIXTURES / "tiff" / "tczyx_uint16_deflate.ome.tif"
JPEG = FIXTURES / "tiff" / "jpeg_ycbcr.tif"  # has data sources (shared JPEG tables)
STORE = FIXTURES / "zarr2" / "zarr2_dtypes"
N5_STORE = FIXTURES / "n5" / "n5_objects"
# the test server is on 127.0.0.1 (spec/refkv.md §6.7
# <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy> rule 3)
LOOPBACK = Policy(allow_private_hosts=True)


@pytest.fixture
def server():
    """Serves files of the repository's fixtures with HEAD and range GETs, and
    the ETag `state["etag"](path, n)` for the n-th response (None: no ETag)."""
    state = {"etag": lambda _path, _n: '"fixed"', "count": 0}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        @override
        def log_message(self, format, *args):
            pass

        def _send(self, head: bool):
            body = (FIXTURES / self.path.lstrip("/")).read_bytes()
            state["count"] += 1
            etag = state["etag"](self.path, state["count"])
            rng = self.headers.get("Range")
            if rng and "," in rng:  # several ranges: the whole object, as a server without multipart answers
                rng = None
            content_range = None
            if rng:
                a, b = (int(x) for x in rng[6:].split("-"))
                data, status = body[a:b + 1], 206
                content_range = f"bytes {a}-{a + len(data) - 1}/{len(body)}"
            else:
                data, status = body, 200
            self.send_response(status)
            if content_range:
                self.send_header("Content-Range", content_range)
            self.send_header("Content-Length", str(len(data)))
            if etag:
                self.send_header("ETag", etag)
            self.end_headers()
            if not head:
                self.wfile.write(data)

        def do_GET(self):
            self._send(False)

        def do_HEAD(self):
            self._send(True)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", state
    srv.shutdown()


def _table(path) -> tuple[list[Source], dict[str, list[Range]]]:
    """The source table and the references of an archive."""
    with zipfile.ZipFile(path) as z:
        sources, _ = decode_table(z.read("__vz__/sources"))
    with open(path, "rb") as f:
        data = f.read()
    d = parse_tail(data, len(data))
    cd = data[d.cd_offset:d.cd_offset + d.cd_size]
    entries: dict[str, Entry] = parse_central_directory(cd, trust_offsets=True)
    refs: dict[str, list[Range]] = {}
    for k, e in entries.items():
        if e.is_ref:
            ref = e.ref
            assert ref is not None, k
            refs[k] = list(ref.parts if isinstance(ref, Concat) else [ref])
    return sources, refs


def test_pins_and_checksums(tmp_path, server):
    base, _state = server
    tiff_size, jpeg_size = TIFF.stat().st_size, JPEG.stat().st_size
    cases: list[tuple[str, str | None, bool, dict[str, Any]]] = [
        # (location, url, checksums, expected pins of source 0)
        (str(TIFF), "https://example.test/a.tif", False, {"size": tiff_size}),
        (str(TIFF), "https://example.test/a.tif", True, {"size": tiff_size}),
        (f"{base}/tiff/{TIFF.name}", None, False, {"size": tiff_size, "etag": '"fixed"'}),
        (f"{base}/tiff/{JPEG.name}", None, True, {"size": jpeg_size, "etag": '"fixed"'}),
        # the output names another URL than the one read: no ETag
        (f"{base}/tiff/{TIFF.name}", "https://example.test/a.tif", False, {"size": tiff_size}),
    ]
    for location, url, checksums, pins in cases:
        out = tmp_path / "out.vzip"
        zarrify(location, url, checksums=checksums, policy=LOOPBACK).save(out, policy=LOOPBACK)
        sources, refs = _table(out)
        assert sources[0] == Source(url=url or location, **pins), (location, url)
        body = (TIFF if TIFF.name in location else JPEG).read_bytes()
        for k, ranges in refs.items():
            for r in ranges:
                if r.data is None and r.source == 0:
                    want = crc32c(body[r.offset:r.offset + r.length]) if checksums else None
                    assert r.crc32c == want, (k, r)
        if url is None:  # read back, checking the pins and checksums
            s = VZipStore(str(out), policy=Policy(allow_private_hosts=True))  # a 127.0.0.1 server
            for k in refs:
                assert s.get(k) is not None

    # a store input: every source pins its object's size (N5 through the IR's store run)
    for store in (STORE, N5_STORE):
        for checksums in (False, True):
            zarrify(str(store), "https://example.test/s/", checksums=checksums).save(tmp_path / "s.vzip")
            sources, refs = _table(tmp_path / "s.vzip")
            sizes = {f"https://example.test/s/{p.relative_to(store).as_posix()}": p.stat().st_size
                     for p in store.rglob("*") if p.is_file()}
            assert sources
            assert all(s.size == sizes[string(s.url)] and s.etag is None for s in sources)
            for k, (r,) in refs.items():
                obj = store / string(sources[r.source].url).removeprefix("https://example.test/s/")
                assert r.crc32c == (crc32c(obj.read_bytes()) if checksums else None), k


def test_no_etag_pin_when_the_responses_disagree(tmp_path, server):
    base, state = server
    state["etag"] = lambda _path, n: '"fixed"' if n % 2 else None  # some responses have none
    zarrify(f"{base}/tiff/{TIFF.name}", policy=LOOPBACK).save(tmp_path / "out.vzip", policy=LOOPBACK)
    assert _table(tmp_path / "out.vzip")[0][0].etag is None


def test_fails_when_the_etag_changes_while_reading(server):
    base, state = server
    state["etag"] = lambda _path, n: f'"v{n}"'
    with pytest.raises(OSError, match="changed while it was read"):
        zarrify(f"{base}/tiff/{TIFF.name}", policy=LOOPBACK)
