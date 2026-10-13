"""The host's side of a store run (rust/refkv-ir/src/store.rs; conventions §8.9
<https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#89-stores>): the I/O of a store
input that the Rust core parses, projects and mirrors.

The core is sans-IO. The host lists the store and reads its objects through the store readers
of `refkv.virtualize.store` (`HttpStore`: S3 `ListObjectsV2` and range requests, every request
under the reader policy of spec/archive.md §6.7
<https://github.com/d-v-b/refkv/blob/main/spec/archive.md#67-reader-policy>; `DirStore`: a local
directory), and drives the run:

- `listing(store)`: the store's objects as (key, size), keys in byte order: what
  `refkv._ir.StoreRun` builds the IR's directories and objects from.
- `drive_store(run, store)`: the loop. Each `poll()` is a batch: one request per object (its
  spans, a whole object being one span), made at most `store.workers` at a time (the
  concurrency of #37's prefetch of documents), then `complete()`.
- `run_store(store, fmt)`: a listed store run (its ignored keys given to the core), and its
  output; `virtualize_store(location, url, fmt)`: the store listed under the policy, then
  run. The output (`StoreIrOutput`) references objects through one url source per object
  some reference uses, its size pinned, in order of first use, and its data sources follow
  (spec/virtualize.md §1.4
  <https://github.com/d-v-b/refkv/blob/main/spec/virtualize.md#14-store-inputs>).

The core's store formats: `zarr2` (rust/refkv-ir/src/zarr2.rs, spec/virtualize/zarr2.md), which
`refkv.virtualize` sends Zarr v2 stores to (deciding which are OME-Zarr 0.4's,
spec/virtualize/ome-zarr.md), and `safe` (rust/refkv-ir/src/safe/,
spec/virtualize/safe.md), a SAFE product's directory form.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from refkv.virtualize.common import Rejected
from refkv.virtualize.store import Store, object_url, open_store


def listing(store: Store) -> list[tuple[str, int]]:
    """The store's objects as (key, size), keys in the order of their UTF-8 bytes."""
    return sorted(store.objects.items(), key=lambda kv: kv[0].encode("utf-8", "surrogatepass"))


def _fetch(store: Store, key: str, spans: list[tuple[int, int]]) -> list[bytes]:
    size = store.objects[key]
    if len(spans) == 1 and spans[0] == (0, size):
        return [_whole(store, key)]
    return [store.read_range(key, a, b - a) for a, b in spans]


def _whole(store: Store, key: str) -> bytes:
    """The object `key`, from what the store read already when it has it (a document kept, or
    read ahead by `Store.prefetch`, as choosing the profile does), else read now: so no
    document is read twice."""
    r = store._kept.pop(key, None) if store._kept is not None else None
    if r is None and store._prefetch is not None:
        r = store._prefetch.take(key)
    if r is None:
        return store.read(key)
    ok, value = r
    if not ok:
        raise value
    return value


def drive_store(run, store: Store, workers: int | None = None, fmt: str | None = None) -> None:
    """Performs the object reads a store run asks for until it is done, at most `workers`
    (default `store.workers`) at a time; each reading thread closes its kept-alive
    connections at the end. `fmt` is the run's store format, which a rejection names
    (`Rejected.format`); pass it whenever it is known."""
    from refkv import _ir

    workers = max(1, workers or store.workers)
    pool = ThreadPoolExecutor(workers) if workers > 1 else None
    try:
        while True:
            try:
                reqs = run.poll()
            except _ir.Rejected as e:
                raise Rejected(str(e), format=fmt) from None
            if reqs is None:
                return
            if pool is None or len(reqs) == 1:
                results = [_fetch(store, key, spans) for _, key, spans in reqs]
            else:
                results = list(pool.map(lambda q: _fetch(store, q[1], q[2]), reqs))
            for k, parts in enumerate(results):
                try:
                    run.complete(k, parts)
                except _ir.Rejected as e:
                    raise Rejected(str(e), format=fmt) from None
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
    """The output of a store run (`refkv._ir.StoreRun.output`): bytes entries, references
    (`refs`: entry key -> its parts, each ("object", key, offset, length) of a store's
    object, (source, offset, length) of data source `source` (from 1, the core's numbering),
    or literal bytes), the data sources, the lazy prefixes and the summary. `sizes` are the
    listed objects' sizes, which the url sources pin."""

    url: str
    entries: dict[str, bytes] = field(default_factory=dict)
    refs: dict[str, list] = field(default_factory=dict)
    data: list[bytes] = field(default_factory=list)
    lazy: tuple[str, ...] = ("vzip_source/",)
    summary: dict = field(default_factory=dict)
    sizes: dict[str, int] = field(default_factory=dict)
    # The CRC-32C of a url source's bytes, when the ranges are to carry checksums
    # (spec/archive.md §4.2 <https://github.com/d-v-b/refkv/blob/main/spec/archive.md#42-range>);
    # set by refkv.virtualize._virtualize_store.
    checksum: Callable[[str, int, int], int] | None = None

    @property
    def docs(self) -> dict[str, object]:
        """The JSON documents (the entries whose keys end in `.json`), parsed."""
        return {k: json.loads(v) for k, v in self.entries.items() if k.endswith(".json")}

    @property
    def chunks(self) -> list[tuple[str, int]]:
        """The references as (entry key, size), in order of the entries' keys."""
        return [(k, sum(_length(p) for p in self.refs[k])) for k in sorted(self.refs, key=lambda k: k.encode())]

    @property
    def origins(self) -> dict[str, str]:
        """The object key of each reference that is one whole object (a Zarr v2 store's
        references are) whose entry key is not its object's."""
        return {k: o for k, o in self._whole_objects().items() if k != o}

    def _whole_objects(self) -> dict[str, str]:
        """Each reference that is one whole listed object (entry key -> object key): one part,
        from the object's byte 0, of its listed size (`sizes`)."""
        whole = {}
        for k, parts in self.refs.items():
            p = parts[0] if len(parts) == 1 else None
            if isinstance(p, tuple) and p[0] == "object" and p[2] == 0 and self.sizes.get(p[1]) == p[3]:
                whole[k] = p[1]
        return whole

    def table(self) -> tuple[list[str], list[bytes], dict[str, list]]:
        """The source table and the references numbered by it: one url source per object
        some reference uses, in order of first use (the references in order of their keys,
        each one's ranges in order), then the data sources, also in order of first use
        (spec/virtualize.md §1.4
        <https://github.com/d-v-b/refkv/blob/main/spec/virtualize.md#14-store-inputs>; a Zarr v2
        or N5 store's objects are each one reference's, so this is one source per reference,
        in order of the entries' keys; spec/virtualize/safe.md §12.8
        <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/safe.md#128-the-source-table>).
        A reference's parts become (source, offset, length) or bytes."""
        objects, data, refs = self._numbered()
        return [object_url(self.url, k) for k in objects], data, refs

    def _numbered(self) -> tuple[list[str], list[bytes], dict[str, list]]:
        """`table`, with the url sources as the object keys they are the urls of."""
        keys = sorted(self.refs, key=lambda k: k.encode())
        objects: dict[str, int] = {}
        for k in keys:
            for p in self.refs[k]:
                if isinstance(p, tuple) and p[0] == "object":
                    objects.setdefault(p[1], len(objects))
        data: dict[int, int] = {}
        out = {}
        for k in keys:
            ranges = []
            for p in self.refs[k]:
                if isinstance(p, (bytes, bytearray)):
                    ranges.append(bytes(p))
                elif p[0] == "object":
                    ranges.append((objects[p[1]], p[2], p[3]))
                elif len(p) == 3:
                    i = data.setdefault(p[0], len(objects) + len(data))
                    ranges.append((i, p[1], p[2]))
                else:
                    raise AssertionError(f"{k}: a reference to source 0 in a store's output")
            out[k] = ranges
        return list(objects), [self.data[i - 1] for i in data], out

    @property
    def sources(self) -> list[str]:
        """The url sources (`table`)."""
        return self.table()[0]

    def write(self, out_path: str) -> None:
        from refkv.vzip.archive import VZipWriter
        from refkv.pb import Range, Source

        with open(out_path, "wb") as fh:
            w = VZipWriter(fh, page_size=1 << 16, checksum=self.checksum)
            whole = self._whole_objects()
            if not self.data and len(whole) == len(self.refs) and len(set(whole.values())) == len(whole):
                # every reference one whole object of its own (a Zarr v2 store's): the bulk path
                # (no interning, for millions of chunks) writes the same table, one source per
                # reference in order of the entries' keys, its size pinned
                keys = sorted(self.refs, key=lambda k: k.encode())
                w.add_url_refs(((k, object_url(self.url, whole[k]), self.refs[k][0][3]) for k in keys), pin_size=True)
            else:
                objects, data, refs = self._numbered()
                for i, k in enumerate(objects):
                    if w.source(Source(url=object_url(self.url, k), size=self.sizes.get(k))) != i:
                        raise AssertionError("url sources out of order")
                for i, value in enumerate(data):
                    if w.blob(value) != len(objects) + i:
                        raise AssertionError("data sources out of order")
                for key, ranges in refs.items():
                    w.add_ranges(key, [Range(data=r) if isinstance(r, bytes) else
                                       Range(source=r[0], offset=r[1], length=r[2]) for r in ranges])
            for key in sorted(self.entries):
                late = key.endswith("zarr.json") and not key.startswith(self.lazy)
                w.add_bytes(key, self.entries[key], compress=not late, late=late)
            w.close()

    def to_store(self, *, policy=None, info: dict | None = None):
        """The output as a `refkv.ReferenceStore`, which `save` writes as the archive
        `write` writes, byte for byte (`refkv.virtualize.common.reference_store`)."""
        from refkv.pb import Range, Source
        from refkv.virtualize.common import reference_store

        objects, data, table = self._numbered()
        sources = [Source(url=object_url(self.url, k), size=self.sizes.get(k)) for k in objects] + \
            [Source(data=v) for v in data]
        refs = [(key, [Range(data=r) if isinstance(r, bytes) else Range(source=r[0], offset=r[1], length=r[2])
                       for r in ranges]) for key, ranges in table.items()]
        entries = [(key, self.entries[key], key.endswith("zarr.json") and not key.startswith(self.lazy))
                   for key in sorted(self.entries)]
        return reference_store(refs, entries, sources, checksum=self.checksum, policy=policy, info=info)


def _length(p) -> int:
    """The length of a reference's part: ("object", key, offset, length), (source, offset,
    length) or bytes."""
    return len(p) if isinstance(p, (bytes, bytearray)) else p[-1]


def from_rust(t, sizes: dict[str, int] | None = None) -> StoreIrOutput:
    """(url, entries, refs, data sources, lazy prefixes, summary JSON) of a store run as a
    StoreIrOutput; `sizes`, the listing's."""
    url, entries, refs, data, lazy, summary = t
    out = StoreIrOutput(url, dict(entries), lazy=tuple(lazy), summary=json.loads(summary), data=list(data),
                        sizes=dict(sizes or {}))
    for k, parts in refs:
        out.refs[k] = list(parts)
    return out


def run_store(store: Store, fmt: str = "zarr2", mirror: bool = True):
    """(output, IR, run counts) of the listed `store`, parsed by the core's store format `fmt`:
    the keys its listing ignores given to the run (the recorded ignored keys, which the
    projection records, and the empty `d/` keys, a SAFE directory's directories), its objects
    read at most `store.workers` at a time, and the summary's `listingRequests` the store's
    requests. With `mirror` false the output has no `vzip_source` mirror, and so no record of
    the ignored keys either. A rejection names `fmt` (`Rejected.format`), or, for one of the
    output's checks, the profile the run decided (`ome-zarr` once the 0.4 test passed)."""
    from refkv import _ir

    if store._prefetch is not None:
        # documents read ahead (choosing the profile does): the run takes those read, and reads
        # the rest itself, so that at most `store.workers` reads are in flight
        store._prefetch.drain()
    try:
        run = _ir.StoreRun(fmt, listing(store), max(1, store.workers))
        run.ignore(list(getattr(store, "ignored", [])), list(getattr(store, "folders", [])))
    except _ir.Rejected as e:
        raise Rejected(str(e), format=fmt) from None
    drive_store(run, store, fmt=fmt)
    stats = json.loads(run.stats())
    profile = run.profile()  # the output's checks are the decided profile's (OME-Zarr's, say)
    try:
        t, ir = run.output(store.url, mirror)
    except _ir.Rejected as e:
        raise Rejected(str(e), format=profile) from None
    out = from_rust(t, store.objects)
    out.summary["listingRequests"] = getattr(store, "requests", 0)
    return out, ir, stats


def virtualize_store(location: str, url: str | None = None, fmt: str = "zarr2", mirror: bool = True,
                     policy=None, workers: int | None = None, opener=None):
    """(output, IR, run counts) of the store at `location` (an http(s) store URL, listed and
    read under `policy`, or a local directory served at `url`), parsed by the core's store
    format `fmt`, as `run_store` (`mirror` false: no mirror, and no record of the ignored keys)."""
    kw = {} if workers is None else {"workers": workers}
    store = open_store(location, url, policy=policy, opener=opener, **kw)
    try:
        return run_store(store, fmt, mirror)
    finally:
        store.close()
