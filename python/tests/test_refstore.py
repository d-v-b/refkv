"""The in-memory store (vzip.ReferenceStore), its persistence through the ZIP binding
(`save`), the read path it shares with a saved archive (`vzip.open`), and both read
through zarr and xarray (`vzip.zarr`)."""

import dataclasses
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest
import xarray as xr
import zarr
from zarr.core.buffer import default_buffer_prototype
from zarr.core.sync import sync

import vzip
import vzip.zarr
from vzip.convert import write_vzip
from vzip.errors import ResolutionError
from vzip.pb import Concat, Range, Source

from conftest import virtualize

PROTO = default_buffer_prototype()


def _values(store) -> dict:
    """Every listed key of `store` (a vzip store) and its value."""
    return {k: store.get(k) for k in store.list()}


@pytest.fixture
def no_proxy(monkeypatch):
    for name in ["http_proxy", "https_proxy", "all_proxy", "no_proxy"]:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)


# ------------------------------------------------------------ reads before and after save


def test_store_reads_what_its_saved_archive_reads(netcdf_files, tmp_path):
    """A store built from virtual datasets reads the same keys, values and dataset in
    memory as after it is saved and reopened, for every way of saving it; the summary
    describes the archive written."""
    paths, expected = netcdf_files
    vdss = [virtualize(p) for p in paths]
    combined = xr.concat(vdss, dim="time", coords="minimal", compat="override")
    cases = [(vdss[0], xr.open_dataset(paths[0], engine="h5netcdf"), 1), (combined, expected, 3)]
    for i, (vds, exp, n_sources) in enumerate(cases):
        store = vzip.ReferenceStore.from_virtualizarr(vds)
        in_memory = _values(store)
        xr.testing.assert_identical(xr.open_zarr(vzip.zarr.store(store)).load(), exp.load())
        assert store.stats.archive_requests == 0 and store.stats.external_requests > 0
        for page_size in [None, 256]:
            for mirror_refs in [True, False]:
                for relative_to in [None, f"file://{tmp_path}/"]:
                    out = tmp_path / f"{i}-{page_size}-{mirror_refs}-{relative_to is None}.vzip"
                    summary = store.save(out, page_size=page_size, mirror_refs=mirror_refs,
                                         relative_to=relative_to)
                    refs = sum(not isinstance(v, bytes) for v in store.mapping.values())
                    assert summary == vzip.Summary(str(out), len(in_memory), refs, n_sources,
                                                   out.stat().st_size)
                    reopened = vzip.open(out)
                    assert _values(reopened) == in_memory
                    if relative_to is not None:
                        assert all(not s.url.startswith("file:") for s in reopened.sources)
                    xr.testing.assert_identical(xr.open_zarr(vzip.zarr.store(reopened)).load(),
                                                exp.load())


def test_write_vzip_is_the_store_in_one_step(netcdf_files, tmp_path, capsys):
    paths, expected = netcdf_files
    vds = xr.concat([virtualize(p) for p in paths], dim="time", coords="minimal",
                    compat="override")
    summary = write_vzip(vds, tmp_path / "a.vzip", page_size=256)
    assert capsys.readouterr().out == ""
    other = vzip.ReferenceStore.from_virtualizarr(vds).save(tmp_path / "b.vzip", page_size=256)
    assert summary == dataclasses.replace(other, path=str(tmp_path / "a.vzip"))
    assert (tmp_path / "a.vzip").read_bytes() == (tmp_path / "b.vzip").read_bytes()
    stats = vzip.Stats()
    xr.testing.assert_identical(
        xr.open_zarr(vzip.zarr.open(tmp_path / "a.vzip", stats=stats)).load(), expected.load())
    assert stats.archive_requests > 0 and stats.external_requests > 0


def test_from_virtualizarr_rewrites_urls(netcdf_files, tmp_path):
    """`rewrite_urls` as a prefix mapping (the longest prefix wins) or a function; the
    rewritten store reads the files at their new URLs."""
    paths, expected = netcdf_files
    vds = xr.concat([virtualize(p) for p in paths], dim="time", coords="minimal",
                    compat="override")
    moved = tmp_path / "moved"
    moved.mkdir()
    for p in paths:
        (moved / p.name).write_bytes(p.read_bytes())
    old, new = f"file://{tmp_path}/", f"file://{moved}/"
    cases = [
        {old: new},
        {"file:///": "file:///nowhere/", old: new},
        lambda u: u.replace(old, new) if u.startswith(old) else None,
    ]
    for rewrite in cases:
        store = vzip.ReferenceStore.from_virtualizarr(vds, rewrite_urls=rewrite)
        assert sorted(s.url for s in store.sources) == sorted(f"{new}{p.name}" for p in paths)
        xr.testing.assert_identical(xr.open_zarr(vzip.zarr.store(store)).load(), expected.load())


def test_public_s3_url():
    assert {u: vzip.public_s3_url(u) for u in [
        "s3://noaa-pds/data/v2.1/a.nc", "S3://bucket-1/k%20ey", "s3://bucket", "s3://my.bucket/k",
        "https://bucket.s3.amazonaws.com/k", "gs://bucket/k",
    ]} == {
        "s3://noaa-pds/data/v2.1/a.nc": "https://noaa-pds.s3.amazonaws.com/data/v2.1/a.nc",
        "S3://bucket-1/k%20ey": "https://bucket-1.s3.amazonaws.com/k%20ey",
        "s3://bucket": "https://bucket.s3.amazonaws.com/",
        "s3://my.bucket/k": None,
        "https://bucket.s3.amazonaws.com/k": None,
        "gs://bucket/k": None,
    }


def test_store_from_a_mapping(tmp_path):
    """The explicit constructor: bytes, a Range, a Concat with a literal part and a key
    source naming a hidden entry, read in memory and from the saved archive alike."""
    src = tmp_path / "src.bin"
    src.write_bytes(bytes(range(64)))
    mapping = {
        "a": b"plain",
        "b": Range(source=0, offset=8, length=4),
        "c": Concat((Range(data=b"<"), Range(source=0, offset=60, length=4),
                     Range(source=1, offset=1, length=2))),
        "__vz__/shared": b"xyz",
    }
    store = vzip.ReferenceStore(mapping, [Source(url=f"file://{src}"), Source(key="__vz__/shared")])
    want = {"a": b"plain", "b": bytes([8, 9, 10, 11]), "c": b"<" + bytes([60, 61, 62, 63]) + b"yz"}
    assert _values(store) == want
    store.save(tmp_path / "m.vzip")
    assert _values(vzip.open(tmp_path / "m.vzip")) == want


def test_save_follows_the_store_layout(tmp_path):
    """`save` writes the values in the mapping's order, `read_at_open` last, deflates
    those of `compressed`, and writes the store's page index unless given another;
    `info` is the builder's, kept out of the archive."""
    import zipfile

    mapping = {"z": b"z" * 64, "r": Range(source=0, offset=0, length=4), "a/zarr.json": b"{}", "m": b"m" * 64}
    store = vzip.ReferenceStore(mapping, [Source(url="https://data.test/x")], read_at_open=["a/zarr.json"],
                                compressed=["m"], page_size=1 << 16, info={"format": "x", "n": [1]})
    assert store.info == {"format": "x", "n": [1]} and store.compressed == {"m"} and store.page_size == 1 << 16
    store.info["n"].append(2)  # a copy
    assert store.info == {"format": "x", "n": [1]}
    for page_size, index in ((..., True), (None, False)):
        path = tmp_path / f"{index}.vzip"
        store.save(path, page_size=page_size)
        entries = sorted(zipfile.ZipFile(path).infolist(), key=lambda e: e.header_offset)
        names = [e.filename for e in entries if not e.filename.startswith("__vz__/")]
        assert names == ["z", "r", "m", "a/zarr.json"]
        assert [e.filename for e in entries if e.compress_type == zipfile.ZIP_DEFLATED
                and not e.filename.startswith("__vz__/")] == ["m"]
        assert any(e.filename == "__vz__/index" for e in entries) == index
        saved = vzip.open(path)
        assert {k: saved.get(k) for k in ("z", "a/zarr.json", "m")} == {k: mapping[k] for k in ("z", "a/zarr.json", "m")}


# ------------------------------------------------------------ the reader policy


def _s3_store(**kwargs):
    """A one-array store whose chunk is a reference into a public S3 bucket."""
    mem = zarr.storage.MemoryStore()
    zarr.create_array(mem, name="x", shape=(4,), chunks=(4,), dtype="u1", dimension_names=["i"],
                      compressors=None)
    mapping = {k: v.to_bytes() for k, v in mem._store_dict.items()}
    mapping["x/c/0"] = Range(source=0, offset=0, length=4)
    return vzip.ReferenceStore(mapping, [Source(url="s3://some-bucket/obj.nc")], **kwargs)


def test_reading_a_refused_scheme_says_how_to_fix_it():
    store = _s3_store()
    with pytest.raises(ResolutionError, match=r"'s3' is not allowed .*vzip\.public_s3_url.*"
                                              r"registry=\{'s3://some-bucket': S3Store"):
        xr.open_zarr(vzip.zarr.store(store), consolidated=False).x.values


def test_save_refuses_sources_the_reader_policy_refuses(tmp_path):
    with pytest.raises(ValueError, match=r"not saving .*default reader policy refuses 1 of its 1 "
                                         r"sources.*vzip\.public_s3_url"):
        _s3_store().save(tmp_path / "x.vzip")
    assert not (tmp_path / "x.vzip").exists()


def test_save_counts_each_refused_source_of_a_shared_origin(tmp_path):
    """The policy decides once per origin without a prefix list, but counts every source;
    a prefix list decides each source by its whole URL."""
    urls = ["https://data.test/a", "http://127.0.0.1/b", "https://data.test/c", "http://127.0.0.1/d",
            "http://127.0.0.1/e"]
    store = vzip.ReferenceStore({f"k{i}": Range(source=i, offset=0, length=1) for i in range(5)},
                                [Source(url=u) for u in urls])
    with pytest.raises(ValueError, match=r"refuses 3 of its 5 sources.*127\.0\.0\.1/b"):
        store.save(tmp_path / "x.vzip")
    with pytest.raises(ValueError, match=r"refuses 4 of its 5 sources.*127\.0\.0\.1/b"):
        store.save(tmp_path / "x.vzip", policy=vzip.Policy(prefixes=("https://data.test/a",)))
    # a policy with its own check decides each source

    class ByPath(vzip.Policy):
        def check(self, base, url, **kwargs):
            if url.endswith("/c"):
                raise ResolutionError(f"{url}: refused by path")
            super().check(base, url, **kwargs)

    with pytest.raises(ValueError, match=r"refuses 1 of its 5 sources.*data\.test/c: refused by path"):
        store.save(tmp_path / "x.vzip", policy=ByPath(allow_private_hosts=True))
    # an invalid URL is refused on its own, after or before its origin's other sources
    for urls in (["https://data.test/a", "https://data.test/a b", "https://data.test/c"],
                 ["https://data.test/a b", "https://data.test/a", "https://data.test/c"]):
        store = vzip.ReferenceStore({f"k{i}": Range(source=i, offset=0, length=1) for i in range(3)},
                                    [Source(url=u) for u in urls])
        with pytest.raises(ValueError, match=r"refuses 1 of its 3 sources.*not a valid URI reference"):
            store.save(tmp_path / "x.vzip")
    assert not (tmp_path / "x.vzip").exists()


def test_save_checks_against_the_policy_given(tmp_path):
    store = _s3_store()
    store.save(tmp_path / "ok.vzip", policy=vzip.Policy(schemes={"s3"}))
    with pytest.raises(ValueError, match="reader policy given refuses"):
        store.save(tmp_path / "x.vzip", policy=vzip.Policy(prefixes=("https://data.example/",)))


# ------------------------------------------------------------ the application's stores


def test_registry_reads_registered_sources(netcdf_files, tmp_path):
    """`registry=` takes an obspec_utils ObjectStoreRegistry, a plain mapping of URL
    prefixes to stores, or a Resolver. A source under a registered prefix, of any
    scheme, is read with its store and passes the reader policy without
    `Policy(schemes=...)`: for a store built from a virtual dataset, a store built
    from a mapping (an unpinned and a size-pinned source, a bucket and a path
    prefix of it), and the archive each saves, opened with the same registry."""
    from obspec_utils.registry import ObjectStoreRegistry
    from obstore.store import MemoryStore

    from vzip.store import Resolver

    paths, expected = netcdf_files
    vds = xr.concat([virtualize(p) for p in paths], dim="time", coords="minimal",
                    compat="override")
    bucket = MemoryStore()
    for p in paths:
        bucket.put(f"data/{p.name}", p.read_bytes())
    blob = bytes(range(64))
    bucket.put("raw/blob.bin", blob)
    mapping = {"a": Range(source=0, offset=8, length=4),
               "b": Concat((Range(source=1, offset=0, length=2), Range(data=b"!")))}
    sources = [Source(url="s3://bucket/raw/blob.bin"),
               Source(url="s3://bucket/raw/blob.bin", size=len(blob))]
    s3 = vzip.Policy(schemes={"s3"})  # for save: what the archive's readers allow
    for prefix in ["s3://bucket", "s3://bucket/", "s3://bucket/data"]:
        forms = {"mapping": {prefix: bucket, "s3://other": MemoryStore()},
                 "ObjectStoreRegistry": ObjectStoreRegistry({prefix: bucket}),
                 "Resolver": Resolver({prefix: bucket})}
        for form, registry in forms.items():
            store = vzip.ReferenceStore.from_virtualizarr(
                vds, rewrite_urls={f"file://{tmp_path}/": "s3://bucket/data/"},
                registry=registry)
            xr.testing.assert_identical(xr.open_zarr(vzip.zarr.store(store)).load(),
                                        expected.load())
            store.save(tmp_path / "v.vzip", policy=s3)
            reopened = vzip.zarr.open(tmp_path / "v.vzip", registry=registry)
            xr.testing.assert_identical(xr.open_zarr(reopened).load(), expected.load())
            if prefix == "s3://bucket/data":
                continue  # the raw/ sources are outside it
            store = vzip.ReferenceStore(mapping, sources, registry=registry)
            want = {"a": blob[8:12], "b": blob[:2] + b"!"}
            assert _values(store) == want, form
            store.save(tmp_path / "m.vzip", policy=s3)
            assert _values(vzip.open(tmp_path / "m.vzip", registry=registry)) == want, form


def test_unregistered_s3_source_is_refused_without_credential_lookup(monkeypatch):
    """A scheme the policy allows, but no store registered for its URL: vzip makes no
    store itself (obstore's from_url would query credential sources, the cloud
    metadata address among them), and says to register one."""
    import obstore.store

    def no_lookup(*a, **k):
        raise AssertionError("vzip built a store from a URL")

    for name in ["from_url", "S3Store", "GCSStore", "AzureStore"]:
        monkeypatch.setattr(obstore.store, name, no_lookup)
    store = _s3_store(policy=vzip.Policy(schemes={"s3"}))
    with pytest.raises(ResolutionError, match=r"no store is registered for s3://some-bucket\. "
                                              r".*registry=\{'s3://some-bucket': store\}"):
        xr.open_zarr(vzip.zarr.store(store), consolidated=False).x.values


def test_registry_does_not_lift_a_prefix_list():
    """Rule 2 replaces rule 1: a registered URL outside the policy's prefixes is refused."""
    from obstore.store import MemoryStore

    store = _s3_store(registry={"s3://some-bucket": MemoryStore()},
                      policy=vzip.Policy(prefixes=("https://data.example/",)))
    with pytest.raises(ResolutionError, match="not under an allowed URL prefix"):
        xr.open_zarr(vzip.zarr.store(store), consolidated=False).x.values


def test_registry_does_not_let_a_remote_store_read_files(tmp_path):
    """Registering a store for file: URLs does not let a remote store read local files."""
    from obstore.store import LocalStore

    (tmp_path / "obj.bin").write_bytes(b"1234")
    mem = zarr.storage.MemoryStore()
    zarr.create_array(mem, name="x", shape=(4,), chunks=(4,), dtype="u1", compressors=None)
    mapping = {k: v.to_bytes() for k, v in mem._store_dict.items()}
    mapping["x/c/0"] = Range(source=0, offset=0, length=4)
    store = vzip.ReferenceStore(mapping, [Source(url=f"file://{tmp_path}/obj.bin")],
                                url="https://data.example/", registry={"file://": LocalStore()})
    with pytest.raises(ResolutionError, match="may not read local files"):
        zarr.open_array(vzip.zarr.store(store), path="x", mode="r")[:]


def test_registry_of_another_type_is_refused():
    with pytest.raises(TypeError, match="ObjectStoreRegistry, a mapping"):
        vzip.ReferenceStore({}, registry=["s3://bucket"])


def test_registry_prefix_without_a_scheme_is_refused():
    with pytest.raises(ValueError, match="a registry prefix is a URL with a scheme"):
        vzip.ReferenceStore({}, registry={"bucket": object()})


# ------------------------------------------------------------ the request cap


@pytest.fixture
def counting_server():
    """An HTTP server of `files` (path -> bytes) that answers range requests slowly,
    and records the most requests it had in flight at once."""
    state = {"files": {}, "now": 0, "max": 0}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            with lock:
                state["now"] += 1
                state["max"] = max(state["max"], state["now"])
            try:
                time.sleep(0.03)
                data = state["files"][self.path]
                spec = self.headers["Range"].removeprefix("bytes=")
                a, b = spec.split("-")
                if a == "":
                    a, b = max(len(data) - int(b), 0), len(data) - 1
                else:
                    a, b = int(a), min(int(b) if b else len(data) - 1, len(data) - 1)
                body = data[a:b + 1]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {a}-{b}/{len(data)}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            finally:
                with lock:
                    state["now"] -= 1

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", state
    srv.shutdown()
    srv.server_close()


def test_max_concurrency_caps_requests_in_flight(counting_server, tmp_path, no_proxy):
    """With zarr reading 32 chunks at once, each chunk a reference to its own range of
    a served file, a store keeps at most `max_concurrency` requests in flight: the
    in-memory store, and the saved archive read over HTTP, its own reads (tail,
    pages) included: 8 by default. With None, more are in flight."""
    base, state = counting_server
    n = 32
    state["files"]["/src.bin"] = bytes(range(n)) * 1024
    mem = zarr.storage.MemoryStore()
    zarr.create_array(mem, name="x", shape=(n,), chunks=(1,), dtype="u1", compressors=None)
    mapping = {k: v.to_bytes() for k, v in mem._store_dict.items()}
    mapping.update({f"x/c/{i}": Range(source=0, offset=1024 * i + i, length=1) for i in range(n)})
    sources = [Source(url=f"{base}/src.bin")]
    policy = vzip.Policy(allow_private_hosts=True)
    vzip.ReferenceStore(mapping, sources).save(tmp_path / "a.vzip", page_size=64, policy=policy)
    state["files"]["/a.vzip"] = (tmp_path / "a.vzip").read_bytes()
    stores = {
        "memory": lambda **cap: vzip.ReferenceStore(mapping, sources, policy=policy, **cap),
        "archive": lambda **cap: vzip.open(f"{base}/a.vzip", policy=policy, **cap),
    }
    caps = [({"max_concurrency": None}, None), ({"max_concurrency": 1}, 1),
            ({"max_concurrency": 3}, 3), ({}, 8)]
    with zarr.config.set({"async.concurrency": 64}):
        for kind, make in stores.items():
            for kwargs, cap in caps:
                state["max"] = 0
                store = make(**kwargs)
                assert store.max_concurrency == cap
                values = zarr.open_array(vzip.zarr.store(store), path="x", mode="r")[:]
                assert np.array_equal(values, np.arange(n)), kind
                assert store.stats.external_requests == n
                if cap is None:
                    assert state["max"] > 8, kind
                else:
                    assert 1 <= state["max"] <= cap, (kind, cap, state["max"])


@pytest.mark.parametrize("cap", [0, -1, 1.5, True, "8"])
def test_max_concurrency_must_be_a_positive_integer(cap, tmp_path):
    with pytest.raises(ValueError, match="max_concurrency"):
        vzip.ReferenceStore({}, max_concurrency=cap)
    with pytest.raises(ValueError, match="max_concurrency"):
        vzip.open(tmp_path / "a.vzip", max_concurrency=cap)


# ------------------------------------------------------------ constructor errors


def test_constructor_rejects_a_value_of_another_type():
    with pytest.raises(TypeError, match="bytes, a Range or a Concat"):
        vzip.ReferenceStore({"k": "text"})


def test_constructor_rejects_a_range_outside_the_source_table():
    with pytest.raises(ValueError, match="uses source 1; the table has 1"):
        vzip.ReferenceStore({"k": Range(source=1, offset=0, length=1)}, [Source(url="https://a/b")])


def test_constructor_rejects_a_format_entry_key():
    with pytest.raises(ValueError, match="format entry"):
        vzip.ReferenceStore({"__vz__/sources": b""})


def test_constructor_rejects_an_empty_key():
    with pytest.raises(ValueError, match="non-empty"):
        vzip.ReferenceStore({"": b""})


def test_constructor_rejects_compressed_naming_no_bytes_value():
    with pytest.raises(ValueError, match="compressed names 'r', which is not a key of a bytes value"):
        vzip.ReferenceStore({"r": Range(source=0, offset=0, length=1)}, [Source(url="https://a/b")],
                            compressed=["r"])


@pytest.mark.parametrize("page_size", [0, -1, True])
def test_a_page_size_that_is_not_positive_is_rejected(tmp_path, page_size):
    with pytest.raises(ValueError, match=f"page_size is a positive number of bytes or None, not {page_size}"):
        vzip.ReferenceStore({}, page_size=page_size)
    with pytest.raises(ValueError, match=f"page_size is a positive number of bytes or None, not {page_size}"):
        vzip.ReferenceStore({}).save(tmp_path / "x.vzip", page_size=page_size)
    assert not (tmp_path / "x.vzip").exists()


def test_constructor_rejects_a_source_of_another_type():
    with pytest.raises(TypeError, match="Source"):
        vzip.ReferenceStore({}, ["https://a/b"])


# ------------------------------------------------------------ no mutations


@pytest.fixture(params=["memory", "archive"])
def store(request, tmp_path):
    """A store of each kind, holding one small array, read through zarr."""
    mem = zarr.storage.MemoryStore()
    zarr.create_array(mem, name="x", shape=(2,), chunks=(2,), dtype="u1")
    s = vzip.ReferenceStore({k: v.to_bytes() for k, v in mem._store_dict.items()})
    if request.param == "memory":
        return vzip.zarr.store(s)
    s.save(tmp_path / "a.vzip")
    return vzip.zarr.open(tmp_path / "a.vzip")


READ_ONLY = "is read-only"
BUF = PROTO.buffer.from_bytes(b"x")


def test_set_raises(store):
    with pytest.raises(ValueError, match=READ_ONLY):
        sync(store.set("k", BUF))


def test_set_if_not_exists_raises(store):
    with pytest.raises(ValueError, match=READ_ONLY):
        sync(store.set_if_not_exists("k", BUF))


def test_set_partial_values_raises(store):
    with pytest.raises(ValueError, match=READ_ONLY):
        sync(store.set_partial_values([("k", 0, b"x")]))


def test_delete_raises(store):
    with pytest.raises(ValueError, match=READ_ONLY):
        sync(store.delete("x/zarr.json"))


def test_delete_dir_raises(store):
    with pytest.raises(ValueError, match=READ_ONLY):
        sync(store.delete_dir("x"))


def test_clear_raises(store):
    with pytest.raises(ValueError, match=READ_ONLY):
        sync(store.clear())


def test_with_read_only_false_raises(store):
    assert store.with_read_only(True) is store
    with pytest.raises(ValueError, match=READ_ONLY):
        store.with_read_only(False)


def test_to_zarr_into_the_store_raises(store):
    with pytest.raises(ValueError):
        xr.Dataset({"y": ("i", np.arange(3))}).to_zarr(store, zarr_format=3)
    assert "y/zarr.json" not in _values(store.reader)


def test_zarr_open_for_writing_raises(store):
    with pytest.raises(ValueError):
        zarr.open_group(store, mode="w")
    assert "x/zarr.json" in _values(store.reader)


def test_sources_cannot_be_changed(store):
    with pytest.raises(AttributeError):
        store.reader.sources.append(Source(url="https://a/b"))
    with pytest.raises(AttributeError):
        store.reader.sources = ()


def test_mapping_cannot_be_changed():
    s = vzip.ReferenceStore({"k": b"v"})
    with pytest.raises(TypeError):
        s.mapping["k"] = b"w"
    with pytest.raises(TypeError):
        del s.mapping["k"]
    with pytest.raises(AttributeError):
        s.mapping = {}
    assert _values(s) == {"k": b"v"}


# ------------------------------------------------------------ the public API


def test_public_api():
    assert set(vzip.__all__) == {
        "Concat", "OffsetRequest", "Policy", "Range", "RangeRequest", "ReferenceStore",
        "Source", "Stats", "SuffixRequest", "Summary", "VZipStore", "VZipWriter", "open",
        "public_s3_url"}
    assert all(hasattr(vzip, name) for name in vzip.__all__)
    assert isinstance(vzip.open("x.vzip"), vzip.VZipStore)
    assert set(vzip.zarr.__all__) == {"ZarrStore", "open", "store"}
    assert isinstance(vzip.zarr.open("x.vzip"), vzip.zarr.ZarrStore)
    assert vzip.zarr.open("x.vzip").reader == vzip.open("x.vzip")
    store = vzip.ReferenceStore({})
    assert vzip.zarr.store(store).reader is store
    from vzip.convert import write_vzip  # the older name still imports
    assert callable(write_vzip)
