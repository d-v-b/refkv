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
from refkv.reader import RangeRequest, _coalesced, _merged, _Waiting
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
    mapping = {k: Concat(parts=tuple(r)) if isinstance(r, list) else Range(source=0, offset=r[0], length=r[1])
               for k, r in refs.items()}
    return ReferenceStore(mapping, sources=[Source(url=url, **(pins or {}))], url="https://store.test/",
                          policy=policy, **kw)


async def _gather(store, *keys):
    return await asyncio.gather(*(store.get_async(k) for k in keys), return_exceptions=True)


def _spans(reads, *, gap=GAP, amplification=4, longest=1 << 20):
    waiting = [_Waiting(a, b, None) for a, b in reads]  # pyrefly: ignore[bad-argument-type] (planning uses no future)
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


@pytest.mark.parametrize(("reads", "gap", "budget", "runs"), [
    # reads that overlap or touch are one run, whatever the budget
    ([(0, 0, 10), (0, 10, 20), (0, 5, 8)], GAP, 0, [[0, 0, 20]]),
    # a gap within the limit is closed while the runs fetch at most the budget
    ([(0, 0, 1), (0, 100, 101)], GAP, 1 << 20, [[0, 0, 101]]),
    ([(0, 0, 1), (0, 100, 101)], GAP, 100, [[0, 0, 1], [0, 100, 101]]),
    # the narrowest gaps first: of 10 and 5 bytes, a budget of 8 closes only the 5
    ([(0, 0, 1), (0, 11, 12), (0, 17, 18)], GAP, 8, [[0, 0, 1], [0, 11, 18]]),
    # a gap past the limit is never closed, nor one between two sources
    ([(0, 0, 1), (0, 2 + GAP, 3 + GAP)], GAP, 1 << 30, [[0, 0, 1], [0, 2 + GAP, 3 + GAP]]),
    ([(0, 0, 1), (1, 1, 2)], GAP, 1 << 30, [[0, 0, 1], [1, 1, 2]]),
    # a limit of 0 combines only reads that touch
    ([(0, 0, 1), (0, 1, 2), (0, 3, 4)], 0, 1 << 30, [[0, 0, 2], [0, 3, 4]]),
])
def test_coalesced(reads, gap, budget, runs):
    assert _coalesced(reads, gap=gap, budget=budget) == runs


def test_a_value_coalesces_within_the_bytes_it_may_take(server):
    """One value's ranges are combined, closest first, only while what it fetches stays within the
    policy's `max_get_bytes`, and never across more than `max_coalesce_gap`; the shared reads keep
    its runs apart."""
    url, state = server
    refs = {"r": [Range(source=0, offset=k << 16, length=1) for k in range(4)]}
    for policy, requests in ((SERVED, 1), (Policy(allow_private_hosts=True, max_get_bytes=1 << 17), 3),
                             (Policy(allow_private_hosts=True, max_coalesce_gap=0), 4)):
        state["ranges"].clear()
        store = _store(url, refs, policy=policy)
        assert store.get("r") == bytes(BLOB[k << 16] for k in range(4))
        assert len(state["ranges"]) == requests, policy


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


def test_the_policy_sets_how_much_shared_reads_may_amplify(server):
    """Reads of different values combine into one request while it fetches at most the policy's
    `max_read_amplification` times the bytes they ask for, plus a gap: small reads a gap apart are
    two requests under the default, one under a larger cap."""
    url, state = server
    refs = {"a": (0, 4), "b": (GAP, 4), "c": (2 * GAP, 4)}
    for amplification, ranges in ((4, 2), (10_000, 1)):
        state["ranges"].clear()
        store = _store(url, refs, policy=Policy(allow_private_hosts=True, max_read_amplification=amplification))
        assert run(_gather(store, *refs)) == [BLOB[0:4], BLOB[GAP:GAP + 4], BLOB[2 * GAP:2 * GAP + 4]]
        assert len(state["ranges"]) == ranges


def test_values_read_at_once_share_one_request_across_their_own_gaps(server):
    """Two values whose pieces interleave and touch, each one's pieces too far apart for its own
    coalescing, are read together in one request, as a full-width read of a tiled image's chunks
    is: the shared read fills each value's gaps with the other's pieces."""
    url, state = server
    x = [Range(source=0, offset=k * 4000, length=2000) for k in range(3)]
    y = [Range(source=0, offset=k * 4000 + 2000, length=2000) for k in range(3)]
    store = _store(url, {"x": x, "y": y}, policy=Policy(allow_private_hosts=True, max_coalesce_gap=1000))
    assert run(_gather(store, "x", "y")) == [b"".join(BLOB[r.offset:r.offset + 2000] for r in v) for v in (x, y)]
    assert state["ranges"] == [(0, 12000)]


def test_tiles_of_padded_rows_read_at_once_share_requests(server):
    """Three tiles of a strip image whose rows are padded, read at once, share requests across the
    rows: each tile's own pieces are more than the coalescing gap apart, so no tile's coalescing
    could combine them, and the other tiles' pieces bridge them."""
    url, state = server
    stride, width = 25_000, 7_000  # 4,000 bytes of padding after each row of three tiles
    tiles = {f"t{t}": [Range(source=0, offset=row * stride + t * width, length=width) for row in range(8)]
             for t in range(3)}
    store = _store(url, tiles, policy=Policy(allow_private_hosts=True, max_coalesce_gap=16_000))
    want = [b"".join(BLOB[r.offset:r.offset + width] for r in v) for v in tiles.values()]
    assert run(_gather(store, *tiles)) == want
    assert len(state["ranges"]) < 8


def test_a_shared_request_fetches_no_more_than_a_get_may_take(server):
    """Reads of different values combine only into requests of at most the policy's
    `max_get_bytes`: two 4 KiB reads 64 KiB apart are two requests under a budget of 4 KiB."""
    url, state = server
    refs = {"a": (0, 4096), "b": (GAP, 4096)}
    store = _store(url, refs, policy=Policy(allow_private_hosts=True, max_get_bytes=4096))
    assert run(_gather(store, *refs)) == [BLOB[:4096], BLOB[GAP:GAP + 4096]]
    assert sorted(state["ranges"]) == [(0, 4096), (GAP, GAP + 4096)]


def test_a_short_data_source_is_read_only_once_the_value_passes_rule_5(server):
    """The byte limit refuses a value before any of its reads, a data source's included: its
    first range runs past a short data source, its second past the budget."""
    url, state = server
    mapping = {"r": Concat((Range(source=0, offset=0, length=10), Range(source=1, offset=0, length=1 << 20)))}
    store = ReferenceStore(mapping, sources=[Source(data=b"abcd"), Source(url=url)], url="https://store.test/",
                           policy=Policy(allow_private_hosts=True, max_get_bytes=1 << 10))
    with pytest.raises(RequestError, match="more than 1024 bytes"):
        store.get("r")
    assert state["ranges"] == []


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
                           sources=[Source(url="blob.bin")], url=str(tmp_path) + "/",
                           policy=Policy(prefixes=(tmp_path.as_uri() + "/",)))
    assert run(_gather(store, "a", "b")) == [BLOB[:10], BLOB[10:20]]
    assert store.stats.external_requests == 2
    assert store._merging is None  # nothing waited
