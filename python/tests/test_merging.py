"""Reads of different values merged into one request (spec §6.3
<https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#63-resolving-a-reference>), and what they
must leave as it is: each value's result, pins, checksums and the reader policy (spec §6.7
<https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>)."""

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import override

import pytest

from refkv._sync import run
from refkv.errors import RequestError, ResolutionError
from refkv.pb import Concat, Range, Source
from refkv.policy import Policy, crc32c
from refkv.reader import RangeRequest, _merged, _Waiting
from refkv.refstore import ReferenceStore

BLOB = bytes(range(256)) * 4096  # 1 MiB
GAP = 1 << 16
SERVED = Policy(allow_private_hosts=True)


@pytest.fixture
def server():
    """Serves `state["body"]` with range requests, cutting a range at the end of the body, and
    records each request's range."""
    state = {"body": BLOB, "etag": '"v1"', "ranges": [], "gate": None}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        @override
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            a, b = (int(x) for x in self.headers["Range"][6:].split("-"))
            state["ranges"].append((a, b + 1))
            if state["gate"] is not None:
                state["gate"].wait(10)
            body = state["body"]
            if a >= len(body):
                self.send_response(416)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            data = body[a:b + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {a}-{a + len(data) - 1}/{len(body)}")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("ETag", state["etag"])
            self.end_headers()
            self.wfile.write(data)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/blob.bin", state
    srv.shutdown()


def _store(url, refs, *, pins=None, policy=SERVED, **kw):
    """A store whose keys are `refs`' keys: each a range (offset, length) of the source at `url`,
    or a list of `Range`s of it."""
    mapping = {k: Concat(parts=r) if isinstance(r, list) else Range(source=0, offset=r[0], length=r[1])
               for k, r in refs.items()}
    return ReferenceStore(mapping, sources=[Source(url=url, **(pins or {}))], url="https://store.test/",
                          policy=policy, **kw)


async def _gather(store, *keys):
    return await asyncio.gather(*(store.get_async(k) for k in keys), return_exceptions=True)


def _spans(reads, *, gap=GAP, amplification=4, longest=1 << 20):
    waiting = [_Waiting(a, b, None) for a, b in reads]
    runs = _merged(waiting, gap=gap, amplification=amplification, longest=longest)
    return [[(r.start, r.end) for r in run] for run in runs]


@pytest.mark.parametrize(("reads", "runs"), [
    ([], []),
    ([(0, 10)], [[(0, 10)]]),
    # adjacent, in any order
    ([(20, 30), (0, 10), (10, 20)], [[(0, 10), (10, 20), (20, 30)]]),
    # a gap of exactly the limit merges, and one byte more does not
    ([(0, 1), (1 + GAP, 2 + GAP)], [[(0, 1), (1 + GAP, 2 + GAP)]]),
    ([(0, 1), (2 + GAP, 3 + GAP)], [[(0, 1)], [(2 + GAP, 3 + GAP)]]),
    # overlapping and repeated reads share the run, and count once towards what it asks for
    ([(0, 10), (0, 10), (5, 20)], [[(0, 10), (0, 10), (5, 20)]]),
    # a read inside the last one: the run keeps the end it has
    ([(0, 100), (10, 20), (150, 160)], [[(0, 100), (10, 20), (150, 160)]]),
    # the amplification cap: small reads far apart stop at 4 times what they ask for, plus a gap
    ([(0, 4), (GAP, GAP + 4), (2 * GAP, 2 * GAP + 4)], [[(0, 4), (GAP, GAP + 4)], [(2 * GAP, 2 * GAP + 4)]]),
    # the cap on a run's length, and a read longer than it, which is not cut
    ([(0, 600_000), (600_000, 1_200_000)], [[(0, 600_000)], [(600_000, 1_200_000)]]),
    ([(0, 2 << 20), (2 << 20, (2 << 20) + 1)], [[(0, 2 << 20)], [(2 << 20, (2 << 20) + 1)]]),
])
def test_merged_runs(reads, runs):
    assert _spans(reads, longest=1 << 20) == runs


def test_values_read_at_once_share_requests(server):
    """What the merging does for every kind of read: each value gets its own bytes, in fewer
    requests, whatever the reference looks like and however the keys are read."""
    url, state = server
    good = crc32c(BLOB[300:400])
    refs = {
        "a": (0, 100),
        "b": (100, 100),  # adjacent
        "same": (0, 100),  # the same bytes as `a`
        "gap": (GAP, 100),  # across a gap
        "crc": [Range(source=0, offset=300, length=100, crc32c=good)],  # read whole
        "concat": [Range(source=0, offset=500, length=10), Range(data=b"!"), Range(source=0, offset=510, length=10)],
        "far": (900_000, 10),  # too far from all of them
    }
    wanted = {
        "a": BLOB[:100], "b": BLOB[100:200], "same": BLOB[:100], "gap": BLOB[GAP:GAP + 100],
        "crc": BLOB[300:400], "concat": BLOB[500:510] + b"!" + BLOB[510:520], "far": BLOB[900_000:900_010],
    }
    for pins in (None, {"size": len(BLOB), "etag": '"v1"'}):
        state["ranges"].clear()
        store = _store(url, refs, pins=pins)
        got = run(_gather(store, *refs))
        assert dict(zip(refs, got, strict=True)) == wanted
        assert sorted(state["ranges"]) == [(0, GAP + 100), (900_000, 900_010)]
        assert store.stats.external_requests == 2
    # a window of a checksummed range, read at the same time as the others: the whole range is read
    state["ranges"].clear()
    store = _store(url, {"crc": refs["crc"], "b": (100, 100)})

    async def windows():
        return await asyncio.gather(store.get_async("crc", RangeRequest(10, 20)), store.get_async("b"))

    assert run(windows()) == [BLOB[310:320], BLOB[100:200]]
    assert state["ranges"] == [(100, 400)]


def test_reads_one_at_a_time_are_not_merged(server):
    url, state = server
    store = _store(url, {"a": (0, 10), "b": (10, 10)})
    assert store.get("a") == BLOB[:10]
    assert store.get("b") == BLOB[10:20]
    assert state["ranges"] == [(0, 10), (10, 20)]


def test_no_window_merges_only_within_a_value(server):
    url, state = server
    store = _store(url, {"a": (0, 10), "b": (10, 10)})
    store.MERGE_WINDOW = None
    assert run(_gather(store, "a", "b")) == [BLOB[:10], BLOB[10:20]]
    assert sorted(state["ranges"]) == [(0, 10), (10, 20)]


def test_reads_wait_for_a_free_connection(server):
    """With every connection busy, reads wait, and merge with those that come meanwhile, even
    those whose windows have long passed."""
    url, state = server
    state["gate"] = threading.Event()
    store = _store(url, {"first": (500_000, 10), "a": (0, 10), "b": (10, 10)}, max_concurrency=1)

    async def read():
        first = asyncio.ensure_future(store.get_async("first"))
        await asyncio.sleep(0.05)  # `first` is in flight
        a = asyncio.ensure_future(store.get_async("a"))
        await asyncio.sleep(0.05)  # `a`'s window has passed
        b = asyncio.ensure_future(store.get_async("b"))
        await asyncio.sleep(0)
        state["gate"].set()
        return await asyncio.gather(first, a, b)

    assert run(read()) == [BLOB[500_000:500_010], BLOB[:10], BLOB[10:20]]
    assert state["ranges"] == [(500_000, 500_010), (0, 20)]


def test_reads_past_the_end_fail_only_their_own_value(server):
    """A run that ends past the end of the source fails; the values it could serve don't."""
    url, state = server
    state["body"] = BLOB[:1000]
    store = _store(url, {"good": (900, 50), "short": (950, 100)})
    good, short = run(_gather(store, "good", "short"))
    assert good == BLOB[900:950]
    assert isinstance(short, ResolutionError)
    assert state["ranges"][0] == (900, 1050)  # first together, then each on its own
    assert sorted(state["ranges"][1:]) == [(900, 950), (950, 1050)]


def test_pin_that_fails_fails_every_value(server):
    url, state = server
    state["etag"] = '"v2"'
    store = _store(url, {"a": (0, 10), "b": (10, 10)}, pins={"etag": '"v1"'})
    got = run(_gather(store, "a", "b"))
    assert all(isinstance(e, ResolutionError) and "pins" in str(e) for e in got)


def test_size_pin_that_fails_fails_every_value(server):
    url, _state = server
    store = _store(url, {"a": (0, 10), "b": (10, 10)}, pins={"size": 7})
    got = run(_gather(store, "a", "b"))
    assert all(isinstance(e, ResolutionError) and "pins" in str(e) for e in got)


def test_checksum_that_fails_fails_only_its_value(server):
    url, state = server
    store = _store(url, {"good": (0, 10), "bad": [Range(source=0, offset=10, length=10, crc32c=0)]})
    good, bad = run(_gather(store, "good", "bad"))
    assert good == BLOB[:10]
    assert isinstance(bad, ResolutionError)
    assert "crc32c" in str(bad)
    assert state["ranges"] == [(0, 20)]


def test_too_many_reads_for_one_value_is_refused_before_any_read(server):
    """Rule 5 counts one value's reads after its own coalescing, whatever other values would
    merge with them."""
    url, state = server
    many = [Range(source=0, offset=i * 200_000, length=1) for i in range(3)]
    store = _store(url, {"many": many, "a": (0, 1)}, policy=Policy(max_reads=2, allow_private_hosts=True))
    with pytest.raises(RequestError, match="3 reads"):
        store.get("many")
    assert state["ranges"] == []


def test_policy_refusal_fails_every_value_without_a_request(server):
    url, state = server
    store = _store(url, {"a": (0, 10), "b": (10, 10)}, policy=Policy())  # 127.0.0.1 is refused
    got = run(_gather(store, "a", "b"))
    assert all(isinstance(e, ResolutionError) for e in got)
    assert state["ranges"] == []


def test_value_given_up_leaves_the_others(server):
    url, state = server
    store = _store(url, {"a": (0, 10), "b": (10, 10), "gone": (600_000, 10)})

    async def read():
        gone = asyncio.ensure_future(store.get_async("gone"))
        a = asyncio.ensure_future(store.get_async("a"))
        b = asyncio.ensure_future(store.get_async("b"))
        await asyncio.sleep(0)
        gone.cancel()
        a.cancel()
        return await b

    assert run(read()) == BLOB[10:20]
    assert state["ranges"] == [(10, 20)]  # no request for a read nobody waits for


def test_a_local_file_is_read_at_once(tmp_path):
    (tmp_path / "blob.bin").write_bytes(BLOB)
    store = ReferenceStore({"a": Range(source=0, offset=0, length=10), "b": Range(source=0, offset=10, length=10)},
                           sources=[Source(url="blob.bin")], url=str(tmp_path) + "/")
    assert run(_gather(store, "a", "b")) == [BLOB[:10], BLOB[10:20]]
    assert store.stats.external_requests == 2
    assert store._merging is None  # nothing waited
