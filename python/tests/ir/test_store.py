"""The host of a store run (vzip.ir.store; conventions §8.9
<https://github.com/d-v-b/vzip/blob/main/spec/conventions.md#89-stores>): the listing and object
reads of the store readers, under the reader policy, driving the Rust core's Zarr v2 store format
(`zarr2`). One test of the runs over the Zarr v2 and OME-Zarr fixture stores (each node
document read once and concurrently, chunk objects never read, every document read rebuilt byte
for byte from the archive's mirror, the listing too; a store the format rejects reads documents
only), and one per failure."""

import io
import json
import threading
import time
import urllib.error
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

import vzip_ir
from vzip.ir.store import virtualize_store
from vzip.virtualize.common import Rejected
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
    try:
        out, ir, stats = virtualize_store(URL, opener=s3, workers=4)
    except Rejected:
        # rejected from the listing, or after reading documents only
        assert all(is_doc(k, objects[k]) and n == 1 for k, n in s3.reads.items()), s3.reads
        return
    # each document read once, in one batch, at most 4 at a time; no chunk object read
    docs = set(s3.reads)
    assert s3.reads == {k: 1 for k in docs} and all(is_doc(k, objects[k]) for k in docs)
    assert s3.most <= 4
    assert stats["batches"] == 1 and stats["objects"] == len(docs)
    # the referenced objects: one url source each, in order of the entries' keys
    assert sorted(set(out.sources)) == sorted(object_url(URL, k) for k, v in objects.items() if v and k not in docs)
    assert len(out.sources) == len(set(out.sources))
    # the archive's mirror alone rebuilds the listing and every document read
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


def test_the_ignored_keys_are_vzip_source_s_member_ignored():
    """The listed keys the store reader ignores and records (spec/virtualize.md §1.4
    <https://github.com/d-v-b/vzip/blob/main/spec/virtualize.md#14-store-inputs>) are the member
    `ignored` of vzip_source's source metadata, beside `ir`, in the order of their UTF-8 bytes."""
    root = FIXTURES / "zarr2" / "zarr2_hierarchy"
    s3 = FakeS3({**directory(root), "z//y": b"1", "./x": b"2", "é/../w": b"3"})
    out, _, _ = virtualize_store(URL, opener=s3, workers=2)
    own = json.loads(out.entries["vzip_source/zarr.json"])["attributes"]["vzip_virtualized"]["zarr2"]
    assert set(own) == {"ir", "ignored"}
    assert own["ignored"] == sorted(["z//y", "./x", "é/../w"], key=str.encode)
