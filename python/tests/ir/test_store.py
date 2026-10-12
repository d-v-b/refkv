"""The host of a store run (vzip.ir.store; conventions §8.9
<https://github.com/d-v-b/vzip/blob/main/spec/conventions.md#89-stores>): the listing and object
reads of the store readers, under the reader policy, driving the Rust core's prototype store
format (`zarr2-proto`). One test of the runs over the Zarr v2 and OME-Zarr fixture stores (each
document read once and concurrently, chunk objects never read, every document rebuilt byte for
byte from the archive's mirror, the listing too), and one per failure."""

import io
import threading
import time
import urllib.error
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

import vzip_ir
from vzip.ir.store import virtualize_store
from vzip.virtualize.store import object_url

FIXTURES = Path(__file__).parents[3] / "fixtures"
STORES = sorted(p for f in ("zarr2", "ome-zarr") for p in (FIXTURES / f).iterdir() if p.is_dir())
URL = "http://h/b/s/"
LISTING = "http://h/b/?list-type=2&prefix=s%2F"
DOCUMENTS = (".zarray", ".zgroup", ".zattrs")


class FakeS3:
    """An urlopen serving `objects` (key → bytes, or an HTTP status) at URL, counting each
    key's reads and the most reads in flight."""

    def __init__(self, objects: dict, short: frozenset = frozenset()):
        self.objects, self.short = objects, short
        self.by_url = {object_url(URL, k): k for k in objects}
        self.reads: dict[str, int] = {}
        self.inflight = self.most = 0
        self.lock = threading.Lock()

    def __call__(self, req, timeout=None):
        class R(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        if req.full_url == LISTING:
            contents = "".join(f"<Contents><Key>s/{escape(k)}</Key><Size>{len(v) if isinstance(v, bytes) else 1}</Size>"
                               "</Contents>" for k, v in sorted(self.objects.items()))
            return R(f"<ListBucketResult><IsTruncated>false</IsTruncated>{contents}</ListBucketResult>".encode())
        key = self.by_url[req.full_url]
        with self.lock:
            self.reads[key] = self.reads.get(key, 0) + 1
            self.inflight += 1
            self.most = max(self.most, self.inflight)
        try:
            time.sleep(0.002)
            v = self.objects[key]
            if isinstance(v, int):
                raise urllib.error.HTTPError(req.full_url, v, "refused", {}, None)
            return R(v[:-1] if key in self.short else v)
        finally:
            with self.lock:
                self.inflight -= 1


def directory(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def is_doc(key: str, data: bytes) -> bool:
    return key.rpartition("/")[2] in DOCUMENTS and len(data) > 0


@pytest.mark.parametrize("root", STORES, ids=lambda p: p.name)
def test_a_store_run_reads_each_document_once_and_its_mirror_rebuilds_them(root, tmp_path):
    objects = directory(root)
    s3 = FakeS3(objects)
    out, ir, stats = virtualize_store(URL, opener=s3, workers=4)
    docs = {k for k, v in objects.items() if is_doc(k, v)}
    # each document read once, in one batch, at most 4 at a time; no chunk object read
    assert s3.reads == {k: 1 for k in docs}
    assert s3.most <= 4
    assert stats["batches"] == (1 if docs else 0) and stats["objects"] == len(docs)
    # the referenced objects: one url source each, its key's, in order of the entries' keys
    referenced = sorted((k for k, v in objects.items() if v and k not in docs), key=str.encode)
    assert out.sources == [object_url(URL, k) for k in referenced]
    # the archive's mirror alone rebuilds the listing and every document
    path = tmp_path / "s.vzip"
    out.write(str(path))
    z = zipfile.ZipFile(path)

    def get(k):
        try:
            return z.read(k)
        except KeyError:
            return None

    rebuilt = vzip_ir.rebuild_store(get)
    assert sorted((k, n) for k, n, _ in rebuilt) == sorted((k, len(v)) for k, v in objects.items())
    assert {k: b for k, _, b in rebuilt if b is not None} == {k: objects[k] for k in docs}
    # a local directory gives the same output
    local, _, _ = virtualize_store(str(root), URL)
    assert (local.entries, local.refs) == (out.entries, out.refs)


def test_a_document_the_store_refuses_fails_the_run():
    s3 = FakeS3({".zgroup": 404, "a/0": b"x"})
    with pytest.raises(urllib.error.HTTPError):
        virtualize_store(URL, opener=s3, workers=2)


def test_a_document_shorter_than_listed_fails_the_run():
    s3 = FakeS3({".zgroup": b'{"zarr_format": 2}', "a/.zarray": b"{}"}, short=frozenset({".zgroup"}))
    with pytest.raises(OSError, match="the listing says"):
        virtualize_store(URL, opener=s3, workers=2)


def test_an_unknown_store_format_is_refused():
    with pytest.raises(ValueError, match="no store format"):
        vzip_ir.StoreRun("n5", [("attributes.json", 2)], 4)


def test_a_store_output_numbers_its_sources_by_first_use():
    """One url source per object a reference uses, in order of first use (references by
    key, ranges in order), each pinning its listed size; then the data sources, by first
    use too; literals stay."""
    from vzip.ir.store import StoreIrOutput

    out = StoreIrOutput(URL, refs={
        "b/c/0": [b"\x01", ("object", "x.jp2", 10, 4), (2, 0, 3)],
        "a/c/0": [("object", "y.jp2", 0, 8), (1, 0, 2), (2, 0, 3)],
        "z": [("object", "x.jp2", 0, 1)],
    }, data=[b"ab", b"cde"], sizes={"x.jp2": 20, "y.jp2": 9})
    urls, data, refs = out.table()
    assert urls == [object_url(URL, "y.jp2"), object_url(URL, "x.jp2")]
    assert data == [b"ab", b"cde"]
    assert refs == {"a/c/0": [(0, 0, 8), (2, 0, 2), (3, 0, 3)], "b/c/0": [b"\x01", (1, 10, 4), (3, 0, 3)],
                    "z": [(1, 0, 1)]}
