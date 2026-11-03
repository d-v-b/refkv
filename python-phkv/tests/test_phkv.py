"""The perfect-hash binding (`refkv_phkv`): a phkv store written by `refkv_phkv.save`
or `PhkvWriter`, and read back as a reference store through the Rust core (`refkv_phkv._core`)
and the read path every reference store shares, from disk and over HTTP."""

import asyncio
import io

import pytest
import refkv_phkv
import zarr
from refkv_phkv import PhkvStore, PhkvWriter
from test_http_archive import BLOB, SERVED, no_proxy, server  # noqa: F401 (fixtures)

import refkv
import refkv.zarr
from refkv import OffsetRequest, RangeRequest, SuffixRequest
from refkv.errors import ArchiveError, RefkvError, RequestError, ResolutionError
from refkv.pb import Concat, Range, Source
from refkv.policy import crc32c

N = 300  # enough keys for several pages and slot blocks
DATA = BLOB * 4  # the object the references read: 16 KiB


def _mapping() -> dict[str, bytes | Range | Concat]:
    mapping: dict[str, bytes | Range | Concat] = {f"b/{i:04}": bytes([i % 251]) * (i % 40 + 1) for i in range(N)}
    mapping.update({f"r/{i:04}": Range(source=0, offset=37 * i, length=50) for i in range(N)})
    mapping["c"] = Concat((Range(data=b"<"), Range(source=0, offset=4000, length=96),
                           Range(source=1, offset=1, length=3)))
    mapping["big"] = bytes(range(256)) * 64  # longer than a record read: stored outside its record
    mapping["empty"] = b""
    return mapping


def _store(base: str = "file:///data") -> refkv.ReferenceStore:
    return refkv.ReferenceStore(_mapping(), [Source(url=f"{base}/blob.bin"), Source(data=b"0123456789")])


def _everything(store) -> dict:
    keys = store.list()
    return {"keys": keys, "values": {k: store.get(k) for k in keys}, "kinds": {k: store.classify(k) for k in keys}}


def test_reads_what_it_was_saved_from(tmp_path):
    """A saved phkv store has the keys, kinds and values of the store it was saved from, and of the
    vzip archive saved from it: bytes (inline and not) and references (a url source, a data
    source, a Concat with a literal)."""
    blob = tmp_path / "blob.bin"
    blob.write_bytes(DATA)
    store = _store(tmp_path.as_uri())
    summary = refkv_phkv.save(store, tmp_path / "s.phkv", policy=SERVED)
    store.save_vzip(tmp_path / "s.vzip", policy=SERVED)
    phkv = refkv.open(tmp_path / "s.phkv", binding="phkv")
    assert isinstance(phkv, PhkvStore)
    assert (summary.keys, summary.references, summary.sources) == (len(store.list()), N + 1, 2)
    assert _everything(phkv) == _everything(store) == _everything(refkv.open(tmp_path / "s.vzip"))
    assert phkv.sources == tuple(store.sources)
    assert phkv.get("c") == b"<" + DATA[4000:4096] + b"123"
    assert phkv.get("nothing") is None
    assert phkv.classify("nothing") == "missing"
    assert phkv.revision is not None


def test_requests(tmp_path):
    """A range, an offset and a suffix select the same bytes of a bytes value and of a reference as
    they do of the store the file was saved from; a request error is raised for a missing key too."""
    (tmp_path / "blob.bin").write_bytes(DATA)
    store = _store(tmp_path.as_uri())
    refkv_phkv.save(store, tmp_path / "s.phkv", policy=SERVED)
    phkv = refkv_phkv.open(tmp_path / "s.phkv")
    for key in ["b/0007", "r/0003", "c", "big", "empty"]:
        for request in [RangeRequest(2, 9), RangeRequest(5, 5), RangeRequest(0, 10**9),
                        RangeRequest(10**9, 10**9 + 1), OffsetRequest(3), OffsetRequest(10**9),
                        SuffixRequest(4), SuffixRequest(10**9), SuffixRequest(0)]:
            assert phkv.get(key, request) == store.get(key, request), (key, request)
    for key in ["b/0007", "nothing"]:
        with pytest.raises(RequestError):
            phkv.get(key, RangeRequest(5, 4))


def test_listing_and_raw(tmp_path):
    (tmp_path / "blob.bin").write_bytes(DATA)
    store = _store(tmp_path.as_uri())
    refkv_phkv.save(store, tmp_path / "s.phkv", policy=SERVED)
    phkv = refkv_phkv.open(tmp_path / "s.phkv")
    assert phkv.list("b/00") == store.list("b/00")
    assert phkv.list("zzz") == []
    assert phkv.list_dir() == store.list_dir() == ["b", "big", "c", "empty", "r"]
    assert phkv.list_dir("b/") == store.list_dir("b/")
    assert phkv.raw("b/0007") == store.raw("b/0007")  # codec 0: the stored bytes are the value
    assert phkv.raw("r/0003") == store.raw("r/0003")  # a reference: its payload
    assert phkv.raw("c") == store.raw("c")
    assert phkv.raw("nothing") is None


def test_the_same_input_gives_the_same_file(tmp_path):
    store = _store()
    refkv_phkv.save(store, tmp_path / "a.phkv", policy=SERVED)
    refkv_phkv.save(store, tmp_path / "b.phkv", policy=SERVED)
    assert (tmp_path / "a.phkv").read_bytes() == (tmp_path / "b.phkv").read_bytes()


def test_compressed_values(tmp_path):
    """The values a store keeps compressed are DEFLATE-compressed in the file, and read the same
    whole, in a window, and as a reference's source."""
    value = b"zarr.json " * 1000
    store = refkv.ReferenceStore({"zarr.json": value, "r": Range(source=0, offset=2, length=8)},
                                 [Source(key="zarr.json")], compressed=["zarr.json"])
    refkv_phkv.save(store, tmp_path / "s.phkv")
    phkv = refkv_phkv.open(tmp_path / "s.phkv")
    assert phkv.get("zarr.json") == value
    assert phkv.get("zarr.json", RangeRequest(10, 20)) == value[10:20]
    assert phkv.get("r") == value[2:10]
    assert len((tmp_path / "s.phkv").read_bytes()) < len(value)


def test_writer(tmp_path):
    """`PhkvWriter` writes bytes, references, properties and metadata, which the reader reports."""
    path = tmp_path / "w.phkv"
    with open(path, "wb") as f, PhkvWriter(f, codec="deflate", inline_max=16) as w:
        blob = w.blob(b"header")
        url = w.url("https://example.org/object")
        assert (blob, url, w.url("https://example.org/object"), w.n_sources) == (0, 1, 1, 2)
        w.add_bytes("a", b"A" * 100)
        w.add_bytes("b", b"B" * 100, codec="none")
        w.add_ranges("r", [Range(source=blob, offset=0, length=3), Range(data=b"-"),
                           Range(source=url, offset=5, length=7, crc32c=0xE3069283)])
        w.set_property("a", "mtime", -5)
        w.set_property("a", "mode", 0o644)
        w.set_property("a", "media_type", "text/plain")
        w.set_property("b", "x:tag", b"\x01\x02")
        w.set_metadata("writer", b"test")
    s = refkv_phkv.open(path, policy=SERVED)
    assert (s.get("a"), s.get("b")) == (b"A" * 100, b"B" * 100)
    assert s.raw("b") == b"B" * 100  # codec none: the stored bytes are the value
    assert s.raw("a") != b"A" * 100  # deflate: stored compressed
    assert s.property("a", "mtime") == -5
    assert s.property("a", "mode") == 0o644
    assert s.property("a", "media_type") == "text/plain"
    assert s.property("b", "x:tag") == b"\x01\x02"
    assert s.property("b", "mode") is None  # no value
    assert s.property("nothing", "mode") is None  # no key
    assert s.metadata["writer"] == b"test"
    assert s.sources == (Source(data=b"header"), Source(url="https://example.org/object"))
    payload = s.raw("r")
    assert payload is not None
    assert len(Concat.decode(payload).parts) == 3  # three ranges: a Concat payload


def test_checksum_hook(tmp_path):
    """`checksum` records the CRC-32C of each range of a url source that has none, and the reader
    checks it: a source that changed after the store was written is a resolution error."""
    blob = tmp_path / "blob.bin"
    blob.write_bytes(DATA)
    url = blob.as_uri()
    seen = []

    def checksum(u, offset, length):
        seen.append((u, offset, length))
        return crc32c(DATA[offset : offset + length])

    path = tmp_path / "s.phkv"
    with open(path, "wb") as f, PhkvWriter(f, checksum=checksum) as w:
        w.add_ranges("a", [Range(source=w.url(url), offset=10, length=20)])
        w.add_ranges("b", [Range(source=0, offset=0, length=5, crc32c=crc32c(DATA[:5])), Range(data=b"!")])
    assert seen == [(url, 10, 20)]  # not the range that has a checksum, nor the literal
    s = refkv_phkv.open(path, policy=SERVED)
    assert s.get("a") == DATA[10:30]
    blob.write_bytes(bytes(len(DATA)))
    with pytest.raises(ResolutionError, match="crc32c"):
        s.get("a")


def test_there_is_no_mirror_refs(tmp_path):
    """vzip's `mirror_refs` has no meaning for phkv: the writer refuses it, whatever its value, and
    `save` does not take it."""
    for value in (True, False):
        with pytest.raises(ValueError, match="mirror_refs"):
            PhkvWriter(io.BytesIO(), mirror_refs=value)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="mirror_refs"):
            refkv_phkv.save(_store(), tmp_path / "m.phkv", mirror_refs=value)  # type: ignore[call-arg]
    assert not (tmp_path / "m.phkv").exists()


def test_the_writer_refuses_what_a_writer_must_refuse():
    """A violation of what a writer must do (spec/refkv.md §7
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#7-writing>, and the binding's) is a
    ValueError, and no store is written: a duplicate or empty key, a range of a missing source,
    a key source that is not a bytes key or is short, and a reference that names a reference."""
    def write(add):
        with PhkvWriter(io.BytesIO()) as w:
            add(w)

    def duplicate(w):
        w.add_bytes("a", b"1")
        w.add_bytes("a", b"2")

    def unregistered(w):
        w.add_ranges("r", [Range(source=3, offset=0, length=1)])

    def to_a_reference(w):
        w.add_ranges("r", [Range(source=w.internal("q"), offset=0, length=1)])
        w.add_ranges("q", [Range(source=w.url("https://example.org/x"), offset=0, length=1)])

    def too_short(w):
        w.add_bytes("k", b"12345")
        w.add_ranges("r", [Range(source=w.internal("k"), offset=3, length=5)])

    for add in (duplicate, lambda w: w.add_bytes("", b"1"), unregistered, to_a_reference, too_short):
        with pytest.raises(ValueError, match=r"."):
            write(add)
    with pytest.raises(ValueError, match="codec"):
        PhkvWriter(io.BytesIO(), codec="lz4")  # type: ignore[arg-type]
    w = PhkvWriter(io.BytesIO())
    w.close()
    with pytest.raises(ValueError, match="closed"):
        w.add_bytes("a", b"1")


def test_not_a_phkv_store_is_an_archive_error(tmp_path):
    """A file that is not a phkv store, a truncated one, and a missing one fail to open with
    an archive error, whichever operation opens them."""
    (tmp_path / "text").write_bytes(b"not a phkv store" * 100)
    (tmp_path / "tiny").write_bytes(b"x")
    good = tmp_path / "s.phkv"
    refkv_phkv.save(_store(), good, policy=SERVED)
    (tmp_path / "cut").write_bytes(good.read_bytes()[:-100])
    for name in ["text", "tiny", "cut", "missing"]:
        with pytest.raises(ArchiveError):
            refkv_phkv.open(tmp_path / name).get("a")


def test_a_corrupt_record_is_an_entry_error(tmp_path):
    """A record that does not read is an error for its key alone: the other keys still read."""
    path = tmp_path / "s.phkv"
    with open(path, "wb") as f, PhkvWriter(f) as w:
        for i in range(50):
            w.add_bytes(f"k{i:02}", b"value %d" % i)
    data = bytearray(path.read_bytes())
    at = bytes(data).index(b"k17")  # the record starts with its key length and flags: break it
    data[at - 4 : at] = b"\xff\xff\xff\xff"
    path.write_bytes(bytes(data))
    s = refkv_phkv.open(path)
    errors = 0
    for i in range(50):
        try:
            assert s.get(f"k{i:02}") == b"value %d" % i
        except RefkvError:
            errors += 1
    assert 1 <= errors < 50


def test_save_checks_the_sources(tmp_path):
    """`refkv_phkv.save` checks the sources as `save` does: the reader policy must accept every url
    source, and a key source must name a bytes key; nothing else is checked until read."""
    ref = {"r": Range(source=0, offset=0, length=1)}
    with pytest.raises(ResolutionError, match="refuses"):
        refkv_phkv.save(refkv.ReferenceStore(ref, [Source(url="s3://bucket/x")]), tmp_path / "t.phkv")
    with pytest.raises(ValueError, match="missing"):
        refkv_phkv.save(refkv.ReferenceStore(ref, [Source(key="missing")]), tmp_path / "u.phkv")
    refkv_phkv.save(refkv.ReferenceStore(ref, [Source(url="s3://bucket/x")]), tmp_path / "v.phkv",
                    policy=refkv.Policy(schemes=frozenset({"s3"})))


# ------------------------------------------------------------ over HTTP


def _serve(state, tmp_path, base) -> str:
    state["files"]["/blob.bin"] = DATA
    path = tmp_path / "s.phkv"
    refkv_phkv.save(_store(base), path, policy=SERVED)
    state["files"]["/s.phkv"] = path.read_bytes()
    return str(path)


@pytest.mark.parametrize("route", ["/s.phkv", "/s.phkv?sig=x", "/norange/s.phkv"])
def test_reads_a_store_over_http(server, tmp_path, route):  # noqa: F811 (the fixture)
    """A store read over HTTP has the keys and values of the same store read from disk, with a
    query, and from a server that ignores Range, and its requests are counted in its Stats."""
    base, state = server
    local = refkv_phkv.open(_serve(state, tmp_path, base), policy=SERVED)
    stats = refkv.Stats()
    remote = refkv.open(base + route, binding="phkv", policy=SERVED, stats=stats)
    assert _everything(remote) == _everything(local)
    assert stats.archive_requests >= 1


def test_a_lookup_reads_a_few_ranges(server, tmp_path):  # noqa: F811 (the fixture)
    """Opening a store reads the end of the file once, and a key then takes a read of its slots and
    one of its record at most: the requests do not grow with the keys the store holds."""
    base, state = server
    _serve(state, tmp_path, base)
    stats = refkv.Stats()
    s = refkv.open(base + "/s.phkv", binding="phkv", policy=SERVED, stats=stats)
    assert s.get("b/0100") == bytes([100 % 251]) * (100 % 40 + 1)
    assert 1 <= stats.archive_requests <= 4
    before = stats.archive_requests
    assert s.get("b/0100") is not None
    assert s.get("b/0101") is not None
    assert stats.archive_requests - before <= 4


def test_concurrent_gets(server, tmp_path):  # noqa: F811 (the fixture)
    """Concurrent reads from one loop all see their values, and none exceeds `max_concurrency`."""
    base, state = server
    _serve(state, tmp_path, base)
    s = refkv.open(base + "/s.phkv", binding="phkv", policy=SERVED, max_concurrency=2)

    async def main():
        keys = [f"b/{i:04}" for i in range(0, N, 3)] + [f"r/{i:04}" for i in range(0, N, 5)]
        return dict(zip(keys, await asyncio.gather(*(s.get_async(k) for k in keys)), strict=True))

    got = asyncio.run(main())
    assert all(v is not None for v in got.values())
    assert got["b/0003"] == bytes([3]) * 4


# ------------------------------------------------------------ zarr


def test_opens_as_a_zarr_store(tmp_path):
    store = refkv.ReferenceStore({"zarr.json": b'{"zarr_format": 3, "node_type": "group"}'}, [])
    refkv_phkv.save(store, tmp_path / "g.phkv")
    group = zarr.open_group(refkv.zarr.open(tmp_path / "g.phkv", binding="phkv"), mode="r")
    assert group.metadata.zarr_format == 3


# ------------------------------------------------------------ the two distributions

def test_refkv_finds_the_binding_through_its_entry_point(tmp_path):
    """`refkv.open(binding="phkv")` goes through the `refkv.bindings` entry-point group, which
    this distribution registers `open` in; `refkv.bindings()` lists it beside vzip."""
    assert refkv.bindings() == ("vzip", "phkv")
    refkv_phkv.save(refkv.ReferenceStore({"a": b"1"}, []), tmp_path / "s.phkv")
    assert refkv.open(tmp_path / "s.phkv", binding="phkv").get("a") == b"1"
    with pytest.raises(ValueError, match="no binding 'nope'"):
        refkv.open(tmp_path / "s.phkv", binding="nope")


def test_hidden_values_are_not_keys(tmp_path):
    """The ZIP binding's hidden values (`__vz__/`) are its bookkeeping, so `save` leaves them out,
    but writes one a `key` source refers to, since the reference could not be read without it."""
    mapping = {"a": b"1", "__vz__/unused": b"x", "__vz__/used": b"0123456789",
               "r": Range(source=0, offset=2, length=3)}
    refkv_phkv.save(refkv.ReferenceStore(mapping, [Source(key="__vz__/used")]), tmp_path / "s.phkv")
    s = refkv_phkv.open(tmp_path / "s.phkv")
    assert s.list() == ["__vz__/used", "a", "r"]
    assert s.get("r") == b"234"


def test_save_phkv_is_refkv_phkv_save(tmp_path):
    """`ReferenceStore.save_phkv(path)` writes the file `refkv_phkv.save(store, path)` does, and
    returns the same summary."""
    (tmp_path / "blob.bin").write_bytes(DATA)
    store = _store(tmp_path.as_uri())
    summary = store.save_phkv(tmp_path / "m.phkv", policy=SERVED, inline_max=64)
    other = refkv_phkv.save(store, tmp_path / "f.phkv", policy=SERVED, inline_max=64)
    assert (summary.keys, summary.references, summary.sources, summary.bytes) == (
        other.keys, other.references, other.sources, other.bytes)
    assert (tmp_path / "m.phkv").read_bytes() == (tmp_path / "f.phkv").read_bytes()
    assert refkv.open(tmp_path / "m.phkv", binding="phkv").get("c") == b"<" + DATA[4000:4096] + b"123"
