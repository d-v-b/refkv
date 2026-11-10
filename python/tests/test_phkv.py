"""The perfect-hash binding (`refkv.phkv`): a phkv store written by `ReferenceStore.save_phkv`
or `PhkvWriter`, and read back as a reference store through the Rust core (`refkv._core`)
and the read path every reference store shares, from disk and over HTTP."""

import asyncio
import gc
import io
import subprocess
import sys
import warnings
import weakref

import pytest
import zarr
from test_http_archive import BLOB, SERVED, no_proxy, server  # noqa: F401 (fixtures)

import refkv
import refkv.phkv
import refkv.zarr
from refkv import OffsetRequest, RangeRequest, SuffixRequest
from refkv.errors import ArchiveError, BodyError, RefkvError, RequestError, ResolutionError
from refkv.pb import Concat, Range, Source
from refkv.phkv import PhkvStore, PhkvWriter
from refkv.policy import Policy, crc32c
from refkv.reader import Found

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


def _store(base: str = "file:///data", policy: refkv.Policy | None = None) -> refkv.ReferenceStore:
    return refkv.ReferenceStore(_mapping(), [Source(url=f"{base}/blob.bin"), Source(data=b"0123456789")], policy=policy)


def _files(directory) -> refkv.Policy:
    """The policy that lets a store read the files of `directory`."""
    return refkv.Policy(prefixes=(directory.as_uri() + "/",))


def _everything(store) -> dict:
    keys = store.list()
    return {"keys": keys, "values": {k: store.get(k) for k in keys}, "kinds": {k: store.classify(k) for k in keys}}


def test_reads_what_it_was_saved_from(tmp_path):
    """A saved phkv store has the keys, kinds and values of the store it was saved from, and of the
    vzip archive saved from it: bytes (inline and not) and references (a url source, a data
    source, a Concat with a literal)."""
    blob = tmp_path / "blob.bin"
    blob.write_bytes(DATA)
    store = _store(tmp_path.as_uri(), _files(tmp_path))
    summary = store.save_phkv(tmp_path / "s.phkv", policy=SERVED)
    store.save_vzip(tmp_path / "s.vzip", policy=SERVED)
    phkv = refkv.open(tmp_path / "s.phkv", binding="phkv", policy=_files(tmp_path))
    assert isinstance(phkv, PhkvStore)
    assert (summary.keys, summary.references, summary.sources) == (len(store.list()), N + 1, 2)
    vzip = refkv.open(tmp_path / "s.vzip", policy=_files(tmp_path))
    assert _everything(phkv) == _everything(store) == _everything(vzip)
    assert phkv.sources == tuple(store.sources)
    assert phkv.get("c") == b"<" + DATA[4000:4096] + b"123"
    assert phkv.get("nothing") is None
    assert phkv.classify("nothing") == "missing"
    assert phkv.revision is not None


def test_requests(tmp_path):
    """A range, an offset and a suffix, of any size, select the same bytes of a bytes value and of a
    reference as they do of the store the file was saved from; a request error is raised for a
    missing key too."""
    (tmp_path / "blob.bin").write_bytes(DATA)
    store = _store(tmp_path.as_uri(), _files(tmp_path))
    store.save_phkv(tmp_path / "s.phkv", policy=SERVED)
    phkv = refkv.phkv.open(tmp_path / "s.phkv", policy=_files(tmp_path))
    for key in ["b/0007", "r/0003", "c", "big", "empty"]:
        for request in [RangeRequest(2, 9), RangeRequest(5, 5), RangeRequest(0, 10**9),
                        RangeRequest(10**9, 10**9 + 1), OffsetRequest(3), OffsetRequest(10**9),
                        SuffixRequest(4), SuffixRequest(10**9), SuffixRequest(0),
                        RangeRequest(0, 2**70), RangeRequest(2**70, 2**71), OffsetRequest(2**70),
                        SuffixRequest(2**70)]:
            assert phkv.get(key, request) == store.get(key, request), (key, request)
    for key in ["b/0007", "nothing"]:
        with pytest.raises(RequestError):
            phkv.get(key, RangeRequest(5, 4))


def test_listing_and_raw(tmp_path):
    (tmp_path / "blob.bin").write_bytes(DATA)
    store = _store(tmp_path.as_uri())
    store.save_phkv(tmp_path / "s.phkv", policy=SERVED)
    phkv = refkv.phkv.open(tmp_path / "s.phkv")
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
    store.save_phkv(tmp_path / "a.phkv", policy=SERVED)
    store.save_phkv(tmp_path / "b.phkv", policy=SERVED)
    assert (tmp_path / "a.phkv").read_bytes() == (tmp_path / "b.phkv").read_bytes()


def test_compressed_values(tmp_path):
    """The values a store keeps compressed are DEFLATE-compressed in the file, and read the same
    whole, in a window, and as a reference's source."""
    value = b"zarr.json " * 1000
    store = refkv.ReferenceStore({"zarr.json": value, "r": Range(source=0, offset=2, length=8)},
                                 [Source(key="zarr.json")], compressed=["zarr.json"])
    store.save_phkv(tmp_path / "s.phkv")
    phkv = refkv.phkv.open(tmp_path / "s.phkv")
    assert phkv.get("zarr.json") == value
    assert phkv.get("zarr.json", RangeRequest(10, 20)) == value[10:20]
    assert phkv.get("r") == value[2:10]
    assert len((tmp_path / "s.phkv").read_bytes()) < len(value)


def test_counts_a_compressed_key_sources_value_whole_once(tmp_path):
    """A range of a compressed key source counts the value whole, once a get, as the reader decodes
    it whole: two ranges of 8 bytes read under a budget of the value's size, and not under one less."""
    value = b"zarr.json " * 1000
    two = Concat((Range(source=0, offset=2, length=8), Range(source=0, offset=20, length=8)))
    store = refkv.ReferenceStore({"zarr.json": value, "r": two}, [Source(key="zarr.json")],
                                 compressed=["zarr.json"])
    store.save_phkv(tmp_path / "s.phkv")
    assert refkv.phkv.open(tmp_path / "s.phkv", policy=Policy(max_get_bytes=len(value))).get("r") == \
        value[2:10] + value[20:28]
    with pytest.raises(RequestError, match=f"more than {len(value) - 1} bytes"):
        refkv.phkv.open(tmp_path / "s.phkv", policy=Policy(max_get_bytes=len(value) - 1)).get("r")


def test_looks_up_a_key_source_once_a_get(tmp_path, monkeypatch):
    """200 ranges of one stored key source look its value up once, to count it for rule 5."""
    value = bytes(range(250)) * 40
    ranges = Concat(tuple(Range(source=0, offset=k, length=1) for k in range(200)))
    store = refkv.ReferenceStore({"k": value, "r": ranges}, [Source(key="k")])
    store.save_phkv(tmp_path / "s.phkv")
    phkv = refkv.phkv.open(tmp_path / "s.phkv")
    looked_up = []
    held_whole = phkv._key_found

    async def counted(key):
        looked_up.append(key)
        return await held_whole(key)

    monkeypatch.setattr(phkv, "_key_found", counted)
    assert phkv.get("r") == value[:200]
    assert looked_up == ["k"]


def test_counts_an_inline_key_value_held_whole(tmp_path):
    """An inline value, which the reader reads whole with its record, is fetched whole once a get:
    64 one-byte ranges of a 1 MiB inline value are refused under a bound of 1 MiB less one."""
    path = tmp_path / "w.phkv"
    with open(path, "wb") as f, PhkvWriter(f, inline_max=2 << 20) as w:
        w.add_bytes("k", bytes(1 << 20), codec="none")
        k = w.internal("k")
        w.add_ranges("r", [Range(source=k, offset=i, length=1) for i in range(64)])
    assert refkv.phkv.open(path, policy=Policy(max_get_bytes=1 << 20)).get("r") == bytes(64)
    with pytest.raises(RequestError):
        refkv.phkv.open(path, policy=Policy(max_get_bytes=(1 << 20) - 1)).get("r")


def test_held_values_past_the_bound_stop_the_lookups(tmp_path, monkeypatch):
    """A get whose key values held whole pass its bound is refused as soon as they do: the lookups
    not yet done are cancelled, so far fewer than its 200 keys are looked up."""
    path = tmp_path / "w.phkv"
    with open(path, "wb") as f, PhkvWriter(f, inline_max=1 << 20) as w:
        ranges = []
        for i in range(200):
            w.add_bytes(f"k/{i:03}", bytes(10_000), codec="none")
            ranges.append(Range(source=w.internal(f"k/{i:03}"), offset=0, length=1))
        w.add_ranges("r", ranges)
    phkv = refkv.phkv.open(path, policy=Policy(max_get_bytes=25_000), max_concurrency=4)
    looked_up = []
    held_whole = phkv._key_found

    async def counted(key):
        held = await held_whole(key)
        looked_up.append(key)
        return held

    monkeypatch.setattr(phkv, "_key_found", counted)
    with pytest.raises(RequestError, match="more than 25000 bytes"):
        phkv.get("r")
    assert 3 <= len(looked_up) < 20


@pytest.mark.parametrize(("compressed", "loads"),
                         [(["k"], [(0, 10_000)]), ([], [(k * 100, k * 100 + 3) for k in range(50)])])
def test_reads_a_key_source_from_its_lookup(tmp_path, monkeypatch, compressed, loads):
    """50 ranges of one key source are read from the record its one lookup read: a compressed value
    whole once, its ranges cut from it, and a value stored apart by range; never by another lookup."""
    value = bytes(range(250)) * 40
    ranges = Concat(tuple(Range(source=0, offset=k * 100, length=3) for k in range(50)))
    store = refkv.ReferenceStore({"k": value, "r": ranges}, [Source(key="k")], compressed=compressed)
    store.save_phkv(tmp_path / "s.phkv", inline_max=0)
    phkv = refkv.phkv.open(tmp_path / "s.phkv")
    looked_up, loaded, ranged = [], [], []
    key_found, key_bytes = phkv._key_found, phkv._key_bytes

    async def counted_found(key):
        found = await key_found(key)
        assert found is not None
        looked_up.append(key)

        async def load(start, end):
            loaded.append((start, end))
            return await found.load(start, end)

        return Found(found.size, found.held, load)

    async def counted_bytes(key, start, end):
        ranged.append((key, start, end))
        return await key_bytes(key, start, end)

    monkeypatch.setattr(phkv, "_key_found", counted_found)
    monkeypatch.setattr(phkv, "_key_bytes", counted_bytes)
    assert phkv.get("r") == b"".join(value[k * 100 : k * 100 + 3] for k in range(50))
    assert (looked_up, sorted(loaded), ranged) == (["k"], loads, [])


def test_cancelling_a_get_leaves_no_key_read_unawaited(tmp_path, monkeypatch):
    """A get cancelled while it counts its key sources, after counting one, has made no read of
    them, so none is left never awaited."""
    ranges = Concat((Range(source=0, offset=0, length=1), Range(source=1, offset=2, length=1)))
    store = refkv.ReferenceStore({"k": b"abcdef", "j": b"ghijkl", "r": ranges}, [Source(key="k"), Source(key="j")])
    store.save_phkv(tmp_path / "s.phkv")
    phkv = refkv.phkv.open(tmp_path / "s.phkv")

    async def stuck(key):
        if key == "j":
            await asyncio.Event().wait()

    monkeypatch.setattr(phkv, "_key_found", stuck)

    async def cancelled():
        task = asyncio.ensure_future(phkv.get_async("r"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        asyncio.run(cancelled())
        gc.collect()
    assert not [w for w in caught if "never awaited" in str(w.message)]


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
    s = refkv.phkv.open(path, policy=SERVED)
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
    s = refkv.phkv.open(path, policy=_files(tmp_path))
    assert s.get("a") == DATA[10:30]
    blob.write_bytes(bytes(len(DATA)))
    with pytest.raises(ResolutionError, match="crc32c"):
        s.get("a")


def test_there_is_no_mirror_refs(tmp_path):
    """vzip's `mirror_refs` has no meaning for phkv, and is refused, whatever its value."""
    for value in (True, False):
        with pytest.raises(ValueError, match="mirror_refs"):
            PhkvWriter(io.BytesIO(), mirror_refs=value)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="mirror_refs"):
            _store().save_phkv(tmp_path / "m.phkv", mirror_refs=value)  # type: ignore[arg-type]
    assert not (tmp_path / "m.phkv").exists()


def test_the_writer_refuses_what_a_writer_must_refuse():
    """A violation of what a writer must do (spec/refkv.md §7
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#7-writing>, and the binding's) is a
    ValueError, and no store is written: a duplicate or empty key, a range of a missing source,
    a key source that is not a bytes key or is short, a reference that names a reference, a literal
    range with a checksum, and a property its type cannot hold."""
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

    def negative_mode(w):
        w.add_bytes("k", b"1")
        w.set_property("k", "mode", -1)

    def too_large(w):
        w.add_bytes("k", b"1")
        w.set_property("k", "x:n", 2**64)

    for add in (duplicate, lambda w: w.add_bytes("", b"1"), unregistered, to_a_reference, too_short,
                lambda w: w.add_ranges("r", [Range(data=b"x", crc32c=5)]), negative_mode, too_large):
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
    _store().save_phkv(good, policy=SERVED)
    (tmp_path / "cut").write_bytes(good.read_bytes()[:-100])
    for name in ["text", "tiny", "cut", "missing"]:
        with pytest.raises(ArchiveError):
            refkv.phkv.open(tmp_path / name).get("a")


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
    s = refkv.phkv.open(path)
    errors = 0
    for i in range(50):
        try:
            assert s.get(f"k{i:02}") == b"value %d" % i
        except RefkvError:
            errors += 1
    assert 1 <= errors < 50


def test_revision_and_metadata_open_the_store(tmp_path):
    """`revision` and `metadata` report what the file holds, with no operation before them."""
    path = tmp_path / "s.phkv"
    with open(path, "wb") as f, PhkvWriter(f) as w:
        w.add_bytes("a", b"1")
        w.set_metadata("writer", b"test")
    assert refkv.phkv.open(path).metadata["writer"] == b"test"
    assert refkv.phkv.open(path).revision is not None


def test_a_store_nothing_holds_is_collected(tmp_path):
    """A store and its Rust core refer to each other, through the core's read callbacks: the garbage
    collector must see that, or every store opened stays in memory with its tail."""
    _store().save_phkv(tmp_path / "s.phkv", policy=SERVED)
    store = refkv.phkv.open(tmp_path / "s.phkv", policy=SERVED)
    assert store.get("b/0007") is not None
    ref = weakref.ref(store)
    del store
    gc.collect()
    assert ref() is None


def test_a_file_that_went_away_is_a_body_error(tmp_path):
    """A value that cannot be read after the store opened is the error the ZIP binding reports."""
    path = tmp_path / "s.phkv"
    refkv.ReferenceStore({f"k{i:04}": bytes(5000) for i in range(400)}, []).save_phkv(path)
    store = refkv.phkv.open(path)
    assert store.get("k0399") is not None  # opens the store, and reads the end of the file
    path.unlink()
    with pytest.raises(BodyError):
        store.get("k0000")


_LOCAL_READS = """
import asyncio, sys
from concurrent.futures import ThreadPoolExecutor
import refkv

async def main():
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(1))
    store = refkv.open(sys.argv[1], binding="phkv")
    values = await asyncio.gather(*(store.get_async(f"k{i:04}") for i in range(64)))
    assert values == [bytes([i]) * 5000 for i in range(64)]
    assert await store.get_async("c") == b"".join(bytes([i]) for i in range(64))

asyncio.run(main())
"""

_SHARED_WRITER = """
import io, threading
from refkv.phkv import PhkvWriter

w = PhkvWriter(io.BytesIO())

def values():
    for i in range(200):
        w.add_bytes(f"k{i}", b"v" * 100_000)

def names():
    for i in range(100_000):
        w.url(f"https://example.org/{i}")

threads = [threading.Thread(target=f) for f in (values, names)]
for t in threads:
    t.start()
for t in threads:
    t.join()
w.close()
"""


def _finishes(script: str, *args: object) -> None:
    """`script` runs to its end in a Python of its own, so a deadlock fails the test, not the run."""
    subprocess.run([sys.executable, "-c", script, *map(str, args)], check=True, timeout=60)


def test_reads_of_a_local_store_wait_for_nothing_they_hold(tmp_path):
    """A call into the Rust core waits for its reads, and a local read takes a thread of the loop's
    default executor. The calls must not fill that executor: with one thread in it, concurrent
    gets, and a get of many key sources, still finish."""
    mapping: dict[str, bytes | Concat] = {f"k{i:04}": bytes([i % 251]) * 5000 for i in range(400)}
    mapping["c"] = Concat(tuple(Range(source=i, offset=0, length=1) for i in range(64)))
    refkv.ReferenceStore(mapping, [Source(key=f"k{i:04}") for i in range(64)]).save_phkv(tmp_path / "s.phkv")
    _finishes(_LOCAL_READS, tmp_path / "s.phkv")


def test_threads_can_share_a_writer():
    """A writer's file object takes the interpreter, so a thread that holds the interpreter must not
    wait for the writer: two threads using one writer finish."""
    _finishes(_SHARED_WRITER)


def test_save_checks_the_sources(tmp_path):
    """`save_phkv` checks the sources as `save_vzip` does: the reader policy must accept every url
    source, and a key source must name a bytes key; nothing else is checked until read."""
    ref = {"r": Range(source=0, offset=0, length=1)}
    with pytest.raises(ResolutionError, match="refuses"):
        refkv.ReferenceStore(ref, [Source(url="s3://bucket/x")]).save_phkv(tmp_path / "t.phkv")
    with pytest.raises(ValueError, match="missing"):
        refkv.ReferenceStore(ref, [Source(key="missing")]).save_phkv(tmp_path / "u.phkv")
    refkv.ReferenceStore(ref, [Source(url="s3://bucket/x")]).save_phkv(
        tmp_path / "v.phkv", policy=refkv.Policy(schemes=frozenset({"s3"})))


# ------------------------------------------------------------ over HTTP


def _serve(state, tmp_path, base) -> str:
    state["files"]["/blob.bin"] = DATA
    path = tmp_path / "s.phkv"
    _store(base).save_phkv(path, policy=SERVED)
    state["files"]["/s.phkv"] = path.read_bytes()
    return str(path)


@pytest.mark.parametrize("route", ["/s.phkv", "/s.phkv?sig=x", "/norange/s.phkv"])
def test_reads_a_store_over_http(server, tmp_path, route):  # noqa: F811 (the fixture)
    """A store read over HTTP has the keys and values of the same store read from disk, with a
    query, and from a server that ignores Range, and its requests are counted in its Stats."""
    base, state = server
    local = refkv.phkv.open(_serve(state, tmp_path, base), policy=SERVED)
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
    store.save_phkv(tmp_path / "g.phkv")
    group = zarr.open_group(refkv.zarr.open(tmp_path / "g.phkv", binding="phkv"), mode="r")
    assert group.metadata.zarr_format == 3


def test_save_phkv_failing_keeps_the_previous_file(tmp_path):
    path = tmp_path / "a.phkv"
    path.write_bytes(b"previous")
    unsavable = refkv.ReferenceStore({"k": Range(source=0, length=5)}, [Source(key="missing")])
    with pytest.raises(ValueError, match="missing"):
        unsavable.save_phkv(path)
    assert path.read_bytes() == b"previous"
    assert [p.name for p in tmp_path.iterdir()] == ["a.phkv"]


def test_save_phkv_skips_hidden_values_unless_a_key_source_reads_one(tmp_path):
    """The `__vz__/` values are the ZIP binding's bookkeeping, so they are not keys of a phkv store,
    unless a `key` source refers to one."""
    mapping = {"a": b"x" * 8, "__vz__/h": b"hidden", "__vz__/s": b"0123456789", "r": Range(source=0, length=4)}
    store = refkv.ReferenceStore(mapping, [Source(key="__vz__/s")])
    store.save_phkv(tmp_path / "s.phkv")
    s = refkv.phkv.open(tmp_path / "s.phkv")
    assert asyncio.run(s.get_async("a")) == b"x" * 8
    assert asyncio.run(s.get_async("r")) == b"0123"
    assert asyncio.run(s.get_async("__vz__/s")) == b"0123456789"  # needed by "r", so kept
    assert asyncio.run(s.get_async("__vz__/h")) is None


def test_open_chooses_the_binding_from_the_name(tmp_path):
    store = refkv.ReferenceStore({"zarr.json": b"{}"}, [])
    store.save_phkv(tmp_path / "s.phkv")
    store.save_phkv(tmp_path / "S2.PHKV")
    store.save_vzip(tmp_path / "s.vzip")
    store.save_phkv(tmp_path / "odd.bin")
    assert isinstance(refkv.open(str(tmp_path / "s.phkv")), PhkvStore)
    assert isinstance(refkv.open(str(tmp_path / "S2.PHKV")), PhkvStore)
    assert type(refkv.open(str(tmp_path / "s.vzip"))).__name__ == "VZipStore"
    assert isinstance(refkv.open(str(tmp_path / "odd.bin"), binding="phkv"), PhkvStore)  # an explicit binding wins
    zs = refkv.zarr.open(str(tmp_path / "s.phkv"))  # the adapter follows the name too
    assert isinstance(zs, refkv.zarr.ZarrStore)
