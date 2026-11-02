"""Zarrifying image files and stores, without copying them, in vzip archives (spec/zarrify.md).

Every format (the profiles of spec/zarrify/profiles/; docs/zarrify/README.md lists
them) goes through the IR (`refkv.ir`): the Rust core's sans-IO parser of the format, driven by
its read planner (a file) or by the store's reads (`refkv.ir.store`; a Sentinel-2 SAFE product
is either, a `.SAFE.zip` file or a `.SAFE` directory), projected to the convention's hierarchy
and mirrored under `refkv_source` by the Rust core. A run may decide the profile itself: the
core's TIFF parser finds NDPI (spec/zarrify/ndpi.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#7-ndpi-profile>), and its
Zarr v2 parser decides between Zarr v2 (spec/zarrify/zarr2.md) and OME-Zarr 0.4
(spec/zarrify/ome-zarr.md).
common holds what the profiles share, and store the store
machinery (§1.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>–§1.6
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#16-json-documents>).

    python -m refkv.zarrify <url or path> <out.vzip> [--url <source url>] [--checksums]
        [--allow-private-hosts] [--connections N] [--byte-cost SECONDS_PER_MB] [--workers N]

Exits with status 3 if the input is rejected, with status 2 on a usage error (such as a
local directory without `--url`), and with status 1 if the reader policy
refuses the input, or a source the archive would name (such as a `--url` on a private
host without `--allow-private-hosts`): the archive is checked against the policy the
input is read under, and is not written.

In Python, `zarrify(location)` (or `zarrify_store`, for a store; `zarrify_async` and
`zarrify_store_async` from async code) returns the result as a `refkv.ReferenceStore`:
readable in memory, and written as a vzip archive by `store.save(path)`; `store.info` holds
the format and the profile's summary.

A file or a store at an http(s) URL is read under the reader policy (spec/refkv.md
§6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>): a host that is, or
resolves to, a loopback, private, link-local or special
address is refused unless the policy has `allow_private_hosts`
(`--allow-private-hosts`), and a request that would go through a proxy unless
`allow_unchecked_proxy`. Every request for the input is made under it: a file's,
and a store's listing and object reads, each redirect target checked before it is
requested.

Every url source pins its size, and the input file's source its ETag when
every response for it gave the same strong one (spec/zarrify.md §1.2
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>, §1.4
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>).
`--checksums` also records the CRC-32C of every range of a url source
(spec/refkv.md §4.2 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#42-range>), which
reads all the bytes the archive references: off by default.

The archive's source is the input URL, or a local file's path as a
percent-encoded URI reference (or, with --url, the given one, which is how a
local file is described by the URL it will be served from). A URL ending
in `/`, or a local directory, is a store input.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from pathlib import Path

# the package, not its names: refkv.ir imports refkv.zarrify.common, so it is still loading when
# refkv.ir is imported first
from refkv import _sync, ir
from refkv._narrow import string
from refkv.ir.planner import Transport, open_transport
from refkv.ir.store import StoreIrOutput, run_store_async
from refkv.policy import Policy, crc32c
from refkv.refstore import ReferenceStore
from refkv.zarrify import profile_classes
from refkv.zarrify.common import Output, Rejected
from refkv.zarrify.profile_classes import *  # noqa: F403 (one class per profile: profile_classes.__all__)
from refkv.zarrify.sources import Dataset, Image, Source, Store, layout_names
from refkv.zarrify.store import (
    WORKERS,
    ListingFailed,
    StoreLimit,
    choose_profile,
    hub_endpoint,
    hub_page,
    object_url,
    open_store,
)
from refkv.zarrify.store import Store as Listed
from refkv.zarrify.store.beside import VsiFiles

NOT_SUPPORTED = "not a file of any supported format"

__all__ = ["Dataset", "Image", "ListingFailed", "Rejected", "Source", "Store", "StoreLimit", "zarrify",
           "zarrify_async", "zarrify_store", "zarrify_store_async"]
__all__ += profile_classes.__all__  # the source and store classes, one per profile


def _no_store_layouts(layouts: Iterable[Image | Dataset] | None, fmt: str) -> None:
    """Checks `layouts` for the store profile `fmt`. No store profile has a dataset layout
    (`DATASET_LAYOUTS`), and a store projection takes none, so one the registry gave would be
    refused here, not dropped."""
    if layout_names(layouts, fmt):
        raise NotImplementedError(f"{fmt}: a store profile writes no dataset layout yet")


async def _zarrify_store_async(location: str, url: str | None = None, *, max_objects: int | None = None,
                               checksums: bool = False, workers: int = WORKERS, policy: Policy | None = None,
                               fmt: str | None = None,
                               layouts: Iterable[Image | Dataset] | None = None) -> tuple[str, StoreIrOutput]:
    """(format, output) for the store at `location` (§1.4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>), its documents
    read by up to `workers` requests at a time; with `checksums`, the output's ranges
    carry the CRC-32C of their bytes (spec/refkv.md §4.2
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#42-range>). A store at an http(s) URL
    is listed and read under the reader policy `policy` (default: `Policy()`). The store
    profile is `fmt`, when given, else the one its root keys select (§1.7
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#17-named-inputs-and-layouts>);
    `layouts` the layouts it writes (`refkv.zarrify.sources.layout_names`)."""
    if fmt is not None:
        _no_store_layouts(layouts, fmt)  # before the store is listed
    named = fmt

    def listed() -> tuple[Listed, str]:
        """The store listed, and its profile; on a thread, whose connections are closed after."""
        store = open_store(location, url, max_objects=max_objects, workers=workers, policy=policy)
        try:
            return store, named or choose_profile(store)
        except BaseException:
            store.close()
            raise
        finally:
            store._release()  # noqa: SLF001 (this thread's connections)

    store, fmt = await asyncio.to_thread(listed)
    try:
        if named is None:
            _no_store_layouts(layouts, fmt)  # a named profile's were checked before the store was listed
        # the core's store formats: N5, a SAFE directory, and Zarr v2, which the core decides is
        # OME-Zarr 0.4's or not (its summary's `format`)
        out = (await run_store_async(store, {"ome-zarr": "zarr2"}.get(fmt, fmt)))[0]
        if fmt in ("zarr2", "ome-zarr"):
            found = string(out.summary["format"], "summary.format")
            if named == "ome-zarr" and found != "ome-zarr":
                raise Rejected("not an OME-Zarr 0.4 store: the Zarr v2 store format found no OME-NGFF 0.4 "
                               "image, plate or bioformats2raw layout at its root")
            fmt = found
    finally:
        store.close()
    if checksums:
        keys = {object_url(store.url, k): k for k in store.objects}
        out.checksum = lambda u, offset, length: crc32c(store.read_range(keys[u], offset, length))
    return fmt, out


def _zarrify_store(location: str, url: str | None = None, *, max_objects: int | None = None,
                   checksums: bool = False, workers: int = WORKERS, policy: Policy | None = None,
                   fmt: str | None = None,
                   layouts: Iterable[Image | Dataset] | None = None) -> tuple[str, StoreIrOutput]:
    """`_zarrify_store_async`, the caller's thread waiting for it (`refkv._sync.run`)."""
    return _sync.run(_zarrify_store_async(location, url, max_objects=max_objects, checksums=checksums,
                                          workers=workers, policy=policy, fmt=fmt, layouts=layouts))


# The file profiles whose input is read with the files beside it, as a store run that probes for
# them: (the format's title, the named file) as a rejection names them.
BESIDE = {"vsi": ("VSI", ".vsi file"), "nrrd": ("NRRD", "header file"), "grib": ("GRIB", "GRIB file")}


async def _zarrify_beside_async(fmt: str, location: str, url: str | None, transport: Transport, *,
                                checksums: bool = False, policy: Policy | None = None,
                                connections: int | None = None) -> tuple[str, StoreIrOutput]:
    """(format, output) for the input at `location` of a profile of `BESIDE`, a file (opened as
    `transport`) read with the files beside it: a VSI slide's `.vsi` file, whose tile files lie
    beside it (spec/zarrify/vsi.md §9.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#92-the-tile-files>), or a NRRD
    header file, whose data file a detached header names (spec/zarrify/nrrd.md §8.3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#83-the-data-file>): a store
    run of the core's `fmt` format over the files, probed as the run asks for them
    (`refkv.zarrify.store.beside.VsiFiles`). `url`, when given, is the URL of the named file's
    directory (ending in `/`), the store URL the output's object URLs are made from."""
    title, noun = BESIDE[fmt]
    store = VsiFiles(location, url, policy=policy, workers=connections, transport=transport, title=title, noun=noun)
    try:
        out = (await run_store_async(store, fmt))[0]
    finally:
        store.close()
    if checksums:
        keys = {object_url(store.url, k): k for k in store.objects}
        out.checksum = lambda u, offset, length: crc32c(store.read_range(keys[u], offset, length))
    return fmt, out


def is_store(location: str) -> bool:
    """Whether `location` is a store input (§1.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>): an http(s) URL whose path
    ends in `/`, or a local directory."""
    if location.startswith(("http://", "https://")):
        return location.split("?", 1)[0].split("#", 1)[0].endswith("/")
    return Path(location).is_dir()


async def _zarrify_async(location: str, url: str | None = None, *, checksums: bool = False,
                         policy: Policy | None = None, connections: int | None = None,
                         byte_cost: float | None = None, workers: int = WORKERS, fmt: str | None = None,
                         layouts: Iterable[Image | Dataset] | None = None) -> tuple[str, Output | StoreIrOutput]:
    """(format, output) for the file at `location`, by its first bytes, or for
    the store at `location` (a URL ending in `/`, or a directory); for the file at `location`
    by the file profile `fmt`, when given, its first bytes untested (§1.7
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#17-named-inputs-and-layouts>).

    Source 0 pins the file's size, and its ETag when every response gave the
    same strong one and the output names the URL that was read (§1.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>). With
    `checksums`, the output's ranges of source 0 carry the CRC-32C of their
    bytes (spec/refkv.md §4.2 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#42-range>),
    which reads them. `policy` is the reader policy a file
    or store at an http(s) URL is read under (default: `Policy()`, private hosts
    refused).
    For a file input, `connections` is how many requests run at a time to a
    remote source (default 8), and `byte_cost` the seconds of wall time the read
    planner charges a byte fetched beyond those asked (default 0.1 s per MB).
    A store's documents are read by up to `workers` requests at a time. `layouts`: the
    layouts the output holds (`refkv.zarrify.sources.layout_names`)."""
    if fmt is None and is_store(location):
        return await _zarrify_store_async(location, url, checksums=checksums, workers=workers, policy=policy,
                                          layouts=layouts)
    # a Hub tree's web page (§1.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>)
    if location.startswith(("http://", "https://")) and hub_page(location, hub_endpoint()):
        raise Rejected(f"{location} is a Hub repository's page, not a file: end it in / to zarrify the repository")
    # the names, before the file is read when the profile is named (once: `layouts` may be an iterator)
    names = layout_names(layouts, fmt) if fmt is not None else None
    transport = await asyncio.to_thread(open_transport, location, policy)
    if fmt is None:
        want = min(552, transport.size)
        held = transport.head[1]
        head = held[:want] if want <= len(held) else await asyncio.to_thread(transport.get, 0, want)
        fmt = ir.sniff(head)
        if fmt is None:
            raise Rejected(NOT_SUPPORTED)
        names = layout_names(layouts, fmt)
    if fmt in BESIDE:
        return await _zarrify_beside_async(fmt, location, url, transport, checksums=checksums, policy=policy,
                                           connections=connections)
    found, out, _ = await ir.zarrify_async(location, url, transport=transport, connections=connections,
                                           byte_cost=byte_cost, fmt=fmt, layouts=names, checksums=checksums)
    if checksums:
        out.checksum = lambda _u, offset, length: crc32c(transport.get(offset, offset + length))
    return found, out


def _zarrify(location: str, url: str | None = None, *, checksums: bool = False,
             policy: Policy | None = None, connections: int | None = None,
             byte_cost: float | None = None, workers: int = WORKERS, fmt: str | None = None,
             layouts: Iterable[Image | Dataset] | None = None) -> tuple[str, Output | StoreIrOutput]:
    """`_zarrify_async`, the caller's thread waiting for it (`refkv._sync.run`)."""
    return _sync.run(_zarrify_async(location, url, checksums=checksums, policy=policy, connections=connections,
                                    byte_cost=byte_cost, workers=workers, fmt=fmt, layouts=layouts))


async def zarrify_async(location: str | Source | Store, url: str | None = None, *, checksums: bool | None = None,
                        policy: Policy | None = None, connections: int | None = None,
                        byte_cost: float | None = None, workers: int | None = None,
                        layouts: Iterable[Image | Dataset] | None = None) -> ReferenceStore:
    """The file at `location` (a local path or an http(s) URL), by its first bytes, or
    the store at `location` (a URL ending in `/`, or a directory), zarrified
    (spec/zarrify.md <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md>), as a
    `refkv.ReferenceStore`: the Zarr hierarchy, each chunk a reference to the
    bytes of the source that hold it; `zarrify` is the same, blocking.

        store = await zarrify_async("https://example.org/image.ome.tiff")
        store.info["format"]      # "tiff"
        store.save("image.vzip")  # the vzip archive

    The store reads in memory (`store.get`, `refkv.zarr.store(store)`), its sources
    under `policy`, and `store.save(path)` writes it as a vzip archive. `store.info`
    is `{"format": ..., **summary}`: the profile that zarrified the input (such as
    "tiff", "nifti" or "zarr2") and its summary of what it found, as `python -m
    refkv.zarrify` prints it. While the
    input's reads are in flight the event loop is free: the reads block, each on a thread of
    the run's own, and the calls into the Rust core run on those threads too.

    - `url`: the URL the archive names the source by (default: `location`, a local
      path percent-encoded as a URI reference); a local directory needs one, the
      store URL (ending in `/`) it is served from.
    - `checksums`: each range of a url source carries the CRC-32C of its bytes
      (spec/refkv.md §4.2 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#42-range>),
      which reads every byte the store references.
    - `policy`: the reader policy (spec/refkv.md §6.7
      <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>) the input is
      read under, and the store's sources after it (default `Policy()`: private
      hosts refused). `save` checks the sources against the policy the archive's
      readers will use, `Policy()` unless given: pass it this one when the
      sources are on a private host.
    - `connections`, `byte_cost`: for a file the Rust core reads (`refkv.ir`), the
      requests at a time to a remote source (default 8), and the seconds of wall
      time the read planner charges a byte fetched beyond those asked (default
      0.1 s per MB).
    - `workers`: how many of a store's documents are read at a time (default 16), for a
      store at an http(s) URL; a local directory is read one document at a time.
    - `layouts`: the layouts the hierarchy holds, `[Image()]` or `[Image(), Dataset()]`
      (`refkv.zarrify.sources`); by default, the image and every dataset layout the format
      has (conventions §9 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#9-dataset-layouts>).

    `location` may instead be a source or store class's instance, such as
    `TiffSource(url)` or `Zarr2Store(url)` (`refkv.zarrify.sources`), which names the profile,
    so that the input is not tested for one, and holds the read options: `url`,
    `checksums`, `policy`, `connections`, `byte_cost` and `workers` are then its own, and
    passing one beside it with any value but None raises `TypeError`.

    Errors:

    - `Rejected` (`refkv.zarrify.Rejected`): no profile accepts the input, or the profile
      that reads it finds what its specification rejects (spec/zarrify.md §1.2
      <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>).
    - `refkv.errors.ResolutionError`: the reader policy refuses the input, or a request for it
      (a host, an address it resolves to, a redirect target).
    - `FileNotFoundError`: a local path that does not exist.
    - `OSError`: the input could not be read, such as an HTTP status no retry mends, a
      response of the wrong length, or a local file that changed while it was read.
      `ListingFailed` and `StoreLimit` (`refkv.zarrify`) are the two of a store: its listing
      could not be read (a network error, or a 408, 429 or 5xx status), or it lists more than
      `max_objects` objects.
    - `ValueError`: a local directory without `url`, or an argument out of range.
    - `TypeError`: a read option passed beside a source or store instance.
    - `NotImplementedError`: a layout the profile cannot write."""
    if isinstance(location, (Source, Store)):
        given = {"url": url, "checksums": checksums, "policy": policy, "connections": connections,
                 "byte_cost": byte_cost, "workers": workers}
        if any(v is not None for v in given.values()):
            options = ", ".join(k for k, v in given.items() if v is not None)
            raise TypeError(f"{options}: a source's read options are its own, {type(location).__name__}(location, ...)")
        source = location
        if isinstance(source, Store):
            fmt, out = await _zarrify_store_async(source.store_url, source.url, max_objects=source.max_objects,
                                                  checksums=source.checksums, workers=source.workers,
                                                  policy=source.policy, fmt=source.profile, layouts=layouts)
        else:
            fmt, out = await _zarrify_async(source.location, source.url, checksums=source.checksums,
                                            policy=source.policy, connections=source.connections,
                                            byte_cost=source.byte_cost, fmt=source.profile, layouts=layouts)
        return await asyncio.to_thread(out.to_store, policy=source.policy, info={"format": fmt, **out.summary})
    fmt, out = await _zarrify_async(location, url, checksums=bool(checksums), policy=policy,
                                    connections=connections, byte_cost=byte_cost,
                                    workers=WORKERS if workers is None else workers, layouts=layouts)
    return await asyncio.to_thread(out.to_store, policy=policy, info={"format": fmt, **out.summary})


def zarrify(location: str | Source | Store, url: str | None = None, *, checksums: bool | None = None,
            policy: Policy | None = None, connections: int | None = None,
            byte_cost: float | None = None, workers: int | None = None,
            layouts: Iterable[Image | Dataset] | None = None) -> ReferenceStore:
    """`zarrify_async`, the caller's thread waiting for it: the same arguments, result and errors.
    Called from a coroutine on refkv's own event loop, it raises RuntimeError (await
    `zarrify_async` there)."""
    return _sync.run(zarrify_async(location, url, checksums=checksums, policy=policy, connections=connections,
                                   byte_cost=byte_cost, workers=workers, layouts=layouts))


async def zarrify_store_async(location: str, url: str | None = None, *, max_objects: int | None = None,
                              checksums: bool = False, workers: int = WORKERS, policy: Policy | None = None,
                              layouts: Iterable[Image | Dataset] | None = None) -> ReferenceStore:
    """The store at `location` (§1.4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>), zarrified: what
    `zarrify_async` does for a store, with the same `url` (needed for a local directory),
    `checksums`, `policy`, `workers` and `layouts`, and the same errors; `max_objects` caps how
    many objects the store may list (beyond it, `StoreLimit`). `workers`, `max_objects` and
    `policy` apply to a store at an http(s) URL: a local directory is read one document at a
    time, without them."""
    fmt, out = await _zarrify_store_async(location, url, max_objects=max_objects, checksums=checksums,
                                          workers=workers, policy=policy, layouts=layouts)
    return await asyncio.to_thread(out.to_store, policy=policy, info={"format": fmt, **out.summary})


def zarrify_store(location: str, url: str | None = None, *, max_objects: int | None = None,
                  checksums: bool = False, workers: int = WORKERS, policy: Policy | None = None,
                  layouts: Iterable[Image | Dataset] | None = None) -> ReferenceStore:
    """`zarrify_store_async`, the caller's thread waiting for it, as `zarrify` waits for
    `zarrify_async`."""
    return _sync.run(zarrify_store_async(location, url, max_objects=max_objects, checksums=checksums,
                                         workers=workers, policy=policy, layouts=layouts))
