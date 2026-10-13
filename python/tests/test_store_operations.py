"""The operations of every reference store, without zarr (spec §6.2
<https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#62-operations>): `get` with and
without a request, `classify`, `list`, `list_dir`, `raw` and the source table, on a
`ReferenceStore` and on the archives it saves, which read the same."""

import asyncio
import itertools

import pytest

import refkv
from refkv import OffsetRequest, RangeRequest, SuffixRequest
from refkv._sync import run
from refkv.errors import RequestError, ResolutionError
from refkv.pb import Concat, Range, Source

SRC = bytes(range(64))
HIDDEN = b"xyz"


def _mapping():
    return {
        "a": b"plain bytes",
        "b": Range(source=0, offset=8, length=4),
        "c": Concat((Range(data=b"<"), Range(source=0, offset=60, length=4),
                     Range(source=1, offset=1, length=2))),
        "d/e": b"{}",
        "d/f/g": Range(source=0, offset=0, length=2),
        "__vz__/shared": HIDDEN,
    }


def _stores(tmp_path):
    """The store in memory, and the archives it saves, with and without a page index."""
    (tmp_path / "src.bin").write_bytes(SRC)
    sources = [Source(url=f"file://{tmp_path}/src.bin"), Source(key="__vz__/shared")]
    store = refkv.ReferenceStore(_mapping(), sources)
    out = {"memory": store}
    for page_size in [None, 64]:
        path = tmp_path / f"{page_size}.vzip"
        store.save(path, page_size=page_size)
        out[f"archive, page_size={page_size}"] = refkv.open(path)
    return out


VALUES = {
    "a": b"plain bytes",
    "b": SRC[8:12],
    "c": b"<" + SRC[60:64] + HIDDEN[1:3],
    "d/e": b"{}",
    "d/f/g": SRC[0:2],
}
REQUESTS = {
    None: slice(None),
    RangeRequest(1, 3): slice(1, 3),
    RangeRequest(2, 100): slice(2, 100),
    RangeRequest(3, 3): slice(3, 3),
    OffsetRequest(2): slice(2, None),
    OffsetRequest(100): slice(100, None),
    SuffixRequest(2): slice(-2, None),
    SuffixRequest(100): slice(-100, None),
    SuffixRequest(0): slice(0, 0),
}


def test_operations(tmp_path):
    """Every operation, on each store, gives the values of the mapping, the same in
    memory and from the archive, and the same from the coroutines."""
    want_raw = {
        "a": b"plain bytes",
        "b": Range(source=0, offset=8, length=4).encode(),
        "c": _mapping()["c"].encode(),
        "d/e": b"{}",
        "__vz__/shared": HIDDEN,
        "nope": None,
    }
    for name, store in _stores(tmp_path).items():
        for (key, value), (request, cut) in itertools.product(VALUES.items(), REQUESTS.items()):
            assert store.get(key, request) == value[cut], (name, key, request)
            assert run(store.get_async(key, request)) == value[cut], (name, key, request)
        for key, request in itertools.product(["nope", "__vz__/shared", "d", "d/"], REQUESTS):
            assert store.get(key, request) is None, (name, key, request)
        assert {k: store.classify(k) for k in [*VALUES, "nope", "__vz__/shared", "d"]} == {
            "a": "bytes", "b": "reference", "c": "reference", "d/e": "bytes",
            "d/f/g": "reference", "nope": "missing", "__vz__/shared": "missing", "d": "missing",
        }, name
        assert {p: store.list(p) for p in ["", "d", "d/", "d/f/", "c", "x", "__vz__"]} == {
            "": ["a", "b", "c", "d/e", "d/f/g"], "d": ["d/e", "d/f/g"], "d/": ["d/e", "d/f/g"],
            "d/f/": ["d/f/g"], "c": ["c"], "x": [], "__vz__": [],
        }, name
        assert {p: store.list_dir(p) for p in ["", "d", "d/", "d/f", "a", "x"]} == {
            "": ["a", "b", "c", "d"], "d": ["e", "f"], "d/": ["e", "f"], "d/f": ["g"],
            "a": [], "x": [],
        }, name
        assert {k: store.raw(k) for k in want_raw} == want_raw, name
        assert store.sources == (Source(url=f"file://{tmp_path}/src.bin"),
                                 Source(key="__vz__/shared")), name

        async def listed():
            return ([k async for k in store.list_async("d")],
                    [k async for k in store.list_dir_async("d")],
                    await store.classify_async("b"), await store.raw_async("a"))
        assert run(listed()) == (["d/e", "d/f/g"], ["e", "f"], "reference", b"plain bytes"), name
        # an archive also holds its format entries, which the raw view shows
        assert (store.raw("__vz__/sources") is None) == (name == "memory"), name


def test_get_refuses_start_after_end(tmp_path):
    for name, store in _stores(tmp_path).items():
        for key in ["a", "b", "nope"]:
            with pytest.raises(RequestError, match="start 3 > end 2"):
                store.get(key, RangeRequest(3, 2))


def test_get_refuses_a_negative_request(tmp_path):
    for name, store in _stores(tmp_path).items():
        for request in [RangeRequest(-1, 2), OffsetRequest(-1), SuffixRequest(-1)]:
            with pytest.raises(RequestError, match="non-negative integers"):
                store.get("a", request)


def test_get_refuses_a_request_of_another_type(tmp_path):
    for name, store in _stores(tmp_path).items():
        with pytest.raises(TypeError, match="a request is a RangeRequest"):
            store.get("a", (0, 2))


def test_get_of_a_refused_source_raises(tmp_path):
    """A source the reader policy refuses (a private host, spec §6.7
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy> rule 3) fails
    the get of a reference to it, in memory and from the archive, without reading it;
    classify, which reads no source, still says it is a reference."""
    store = refkv.ReferenceStore({"r": Range(source=0, offset=0, length=4)},
                                [Source(url="http://127.0.0.1:9/x.bin")])
    store.save(tmp_path / "r.vzip", policy=refkv.Policy(allow_private_hosts=True))
    for s in [store, refkv.open(tmp_path / "r.vzip")]:
        with pytest.raises(ResolutionError, match="127.0.0.1"):
            s.get("r")
        assert s.classify("r") == "reference"
        assert s.stats.external_requests == 0


def test_a_method_that_returns_at_once_refuses_refkvs_own_loop(tmp_path):
    store = _stores(tmp_path)["memory"]

    async def nested():
        return store.get("a")

    with pytest.raises(RuntimeError, match="await its _async form"):
        run(nested())
    assert asyncio.run(store.get_async("a")) == b"plain bytes"  # another loop is fine
