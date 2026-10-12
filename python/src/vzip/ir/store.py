"""The host's side of a store run (rust/vzip-ir/src/store.rs; conventions §8.9
<https://github.com/d-v-b/vzip/blob/main/spec/conventions.md#89-stores>): the I/O of a store
input that the Rust core parses, projects and mirrors.

The core is sans-IO. The host lists the store and reads its objects through the store readers
of `vzip.virtualize.store` (`HttpStore`: S3 `ListObjectsV2` and range requests, every request
under the reader policy of spec/archive.md §6.7
<https://github.com/d-v-b/vzip/blob/main/spec/archive.md#67-reader-policy>; `DirStore`: a local
directory), and drives the run:

- `listing(store)`: the store's objects as (key, size), keys in byte order: what
  `vzip_ir.StoreRun` builds the IR's directories and objects from.
- `drive_store(run, store)`: the loop. Each `poll()` is a batch: one request per object (its
  spans, a whole object being one span), made at most `store.workers` at a time (the
  concurrency of #37's prefetch of documents), then `complete()`.
- `virtualize_store(location, url, fmt)`: the store listed under the policy, run, and the
  output (`StoreIrOutput`), whose references to objects become one url source per object
  referenced, its size pinned, in order of the entries' keys (spec/virtualize.md §1.4
  <https://github.com/d-v-b/vzip/blob/main/spec/virtualize.md#14-store-inputs>).

No production profile uses it yet: `zarr2-proto` (rust/vzip-ir/src/proto_zarr2.rs) is the
only store format of the core, a prototype.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from vzip.virtualize.common import Rejected
from vzip.virtualize.store import Store, object_url, open_store


def listing(store: Store) -> list[tuple[str, int]]:
    """The store's objects as (key, size), keys in the order of their UTF-8 bytes."""
    return sorted(store.objects.items(), key=lambda kv: kv[0].encode("utf-8", "surrogatepass"))


def _fetch(store: Store, key: str, spans: list[tuple[int, int]]) -> list[bytes]:
    size = store.objects[key]
    if len(spans) == 1 and spans[0] == (0, size):
        return [store.read(key)]
    return [store.read_range(key, a, b - a) for a, b in spans]


def drive_store(run, store: Store, workers: int | None = None) -> None:
    """Performs the object reads a store run asks for until it is done, at most `workers`
    (default `store.workers`) at a time; each reading thread closes its kept-alive
    connections at the end."""
    import vzip_ir

    workers = max(1, workers or store.workers)
    pool = ThreadPoolExecutor(workers) if workers > 1 else None
    try:
        while True:
            try:
                reqs = run.poll()
            except vzip_ir.Rejected as e:
                raise Rejected(str(e)) from None
            if reqs is None:
                return
            if pool is None or len(reqs) == 1:
                results = [_fetch(store, key, spans) for _, key, spans in reqs]
            else:
                results = list(pool.map(lambda q: _fetch(store, q[1], q[2]), reqs))
            for k, parts in enumerate(results):
                try:
                    run.complete(k, parts)
                except vzip_ir.Rejected as e:
                    raise Rejected(str(e)) from None
    finally:
        if pool is not None:
            # one release per thread: each task waits until every thread holds one
            barrier = threading.Barrier(workers)

            def release() -> None:
                try:
                    barrier.wait(timeout=10)
                except threading.BrokenBarrierError:
                    pass
                store._release()

            for f in [pool.submit(release) for _ in range(workers)]:
                f.result()
            pool.shutdown()
        store._release()


@dataclass
class StoreIrOutput:
    """The output of a store run (`vzip_ir.StoreRun.output`): bytes entries, references to
    whole objects (`refs`: entry key -> (object key, size)), the lazy prefixes and the summary."""

    url: str
    entries: dict[str, bytes] = field(default_factory=dict)
    refs: dict[str, tuple[str, int]] = field(default_factory=dict)
    lazy: tuple[str, ...] = ("vzip_source/",)
    summary: dict = field(default_factory=dict)

    @property
    def sources(self) -> list[str]:
        """The source table: one url source per reference, in order of the entries' keys."""
        return [object_url(self.url, self.refs[k][0]) for k in sorted(self.refs, key=lambda k: k.encode())]

    def write(self, out_path: str) -> None:
        from vzip.archive import VZipWriter

        with open(out_path, "wb") as fh:
            w = VZipWriter(fh, page_size=1 << 16)
            keys = sorted(self.refs, key=lambda k: k.encode())
            w.add_url_refs(((k, object_url(self.url, self.refs[k][0]), self.refs[k][1]) for k in keys), pin_size=True)
            for key in sorted(self.entries):
                late = key.endswith("zarr.json") and not key.startswith(self.lazy)
                w.add_bytes(key, self.entries[key], compress=not late, late=late)
            w.close()


def from_rust(t) -> StoreIrOutput:
    """(url, entries, refs, data sources, lazy prefixes, summary JSON) of a store run as a
    StoreIrOutput: every reference is one whole object (a store output has no data sources)."""
    url, entries, refs, data, lazy, summary = t
    if data:
        raise AssertionError("a store output with data sources")
    out = StoreIrOutput(url, dict(entries), lazy=tuple(lazy), summary=json.loads(summary))
    for k, parts in refs:
        if len(parts) != 1 or parts[0][0] != "object" or parts[0][2] != 0:
            raise AssertionError(f"{k}: a reference other than one whole object")
        _, key, _, n = parts[0]
        out.refs[k] = (key, n)
    return out


def virtualize_store(location: str, url: str | None = None, fmt: str = "zarr2-proto", mirror: bool = True,
                     policy=None, workers: int | None = None, opener=None):
    """(output, IR, run counts) of the store at `location` (an http(s) store URL, listed and
    read under `policy`, or a local directory served at `url`), parsed by the core's store
    format `fmt`."""
    import vzip_ir

    kw = {} if workers is None else {"workers": workers}
    store = open_store(location, url, policy=policy, opener=opener, **kw)
    try:
        run = vzip_ir.StoreRun(fmt, listing(store), max(1, store.workers))
    except vzip_ir.Rejected as e:
        raise Rejected(str(e)) from None
    drive_store(run, store)
    stats = json.loads(run.stats())
    try:
        t, ir = run.output(store.url, mirror)
    except vzip_ir.Rejected as e:
        raise Rejected(str(e)) from None
    return from_rust(t), ir, stats
