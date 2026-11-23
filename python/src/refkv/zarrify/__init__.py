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
        [--layout NAME [--layout-options JSON]]

`--layout` names a plugin, which writes the whole output (`refkv.zarrify.layouts`; spec/layout.md
<https://github.com/d-v-b/refkv/blob/main/spec/layout.md>), from the built-in output.

Exits with status 3 if the input is rejected (or a plugin layout refused for it), with status 2 on
a usage error (such as a local directory without `--url`, which the command needs, since an
archive's readers refuse `file:` sources by default; or a layout no plugin provides), and with
status 1 if a plugin layout failed, if the reader policy
refuses the input, or a source the archive would name (such as a `--url` on a private
host without `--allow-private-hosts`): the archive is checked against the policy the
input is read under, and is not written.

In Python, `zarrify(location)` (or `zarrify_store`, for a store; `zarrify_async` and
`zarrify_store_async` from async code) returns the result as a `refkv.ReferenceStore`:
readable in memory, and written as a vzip archive by `store.save_vzip(path)`; `store.info` holds
the format and the profile's summary.

A file or a store at an http(s) URL is read under the reader policy (spec/refkv.md
§6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>): a host that is, or
resolves to, a loopback, private, link-local or special
address is refused unless the policy has `allow_private_hosts`
(`--allow-private-hosts`), and a request that would go through a proxy unless
`allow_unchecked_proxy`. Every request for the input is made under it: a file's,
and a store's listing and object reads, each redirect target checked before it is
requested.

A URL under a prefix the application registered a store for (`registry=`, as `refkv.open` takes
it), such as an `s3://` bucket's, is read with that store: a file with its `head`, `get_range` and
`get_ranges`, a store listed with its `list`, and the result reads its sources with it too.

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
import contextlib
import dataclasses
import functools
import os
import threading
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path

from refkv import _sync, ir
from refkv._rules import rule as _host_rule
from refkv.errors import Failed, Rejected
from refkv.host import (
    WORKERS,
    ListingFailed,
    StoreLimit,
    choose_profile,
    hub_endpoint,
    hub_page,
    object_url,
    object_urls,
    open_store,
)
from refkv.host import Store as Listed
from refkv.host.beside import Scope, VsiFiles, scope_keys
from refkv.host.stores import DirStore
from refkv.ir.output import Output
from refkv.ir.planner import Transport, open_transport
from refkv.ir.store import StoreIrOutput, StoreParse, parse_store_async
from refkv.policy import Policy, crc32c
from refkv.profiles import BESIDE, CASE, ON_DEMAND
from refkv.reader import Registry, Resolver
from refkv.refstore import ReferenceStore
from refkv.uri import file_path
from refkv.zarrify import profile_classes
from refkv.zarrify.dicom_series import SeriesOutput, zarrify_series_async
from refkv.zarrify.layouts import (
    Layout,
    LayoutFailed,
    LayoutRejected,
    LayoutUsageError,
    Planned,
    described,
    plan,
)
from refkv.zarrify.profile_classes import *  # noqa: F403 (one class per profile: profile_classes.__all__)
from refkv.zarrify.sources import Dataset, Image, Source, Store, layout_names

# a plugin layout is given the input parsed (refkv.build.read's Parsed) and its output is sealed
# (refkv.build.seal): modules of refkv.build, which reads its inputs with this module's parse, so
# each side names the other's module and reads its names at call time (a cycle). They come after
# this package's own modules, which refkv.build imports names from.
# isort: split
import refkv.build.parsed as _build_parsed
import refkv.build.plugin as _build_plugin

NOT_SUPPORTED = "not a file of any supported format"

__all__ = ["Dataset", "Failed", "Image", "Layout", "ListingFailed", "Rejected", "Source", "Store", "StoreLimit",
           "zarrify", "zarrify_async", "zarrify_store", "zarrify_store_async"]
__all__ += profile_classes.__all__  # the source and store classes, one per profile


def _no_store_layouts(layouts: Iterable[Image | Dataset] | None, fmt: str) -> None:
    """Checks `layouts` for the store profile `fmt`. No store profile has a dataset layout
    (`DATASET_LAYOUTS`), and a store projection takes none, so one the registry gave would be
    refused here, not dropped."""
    if layout_names(layouts, fmt):
        raise NotImplementedError(f"{fmt}: a store profile writes no dataset layout yet")


@dataclasses.dataclass(frozen=True)
class _Parse:
    """An input parsed, with no output made of it: its profile (`fmt`), the core's parse of it
    (a file's or a store's, which holds its IR), and its built-in output when asked for
    (`output_async`), as often as asked. Reading the input and deciding its profile are done; what
    the built-in output alone reads is read when one is asked for."""

    fmt: str
    parse: ir.FileParse | StoreParse
    _output: Callable[[bool], Awaitable[Output | StoreIrOutput]]
    _close: Callable[[], None]

    async def output_async(self, written: bool = True) -> Output | StoreIrOutput:
        """The built-in output, its IR and (when the call asked for checksums) its checksum
        function set. `written`: the output is written as it is; false for one only built from,
        which is not held to the payload limit with checksums."""
        return await self._output(written)

    def close(self) -> None:
        """Releases what the input was read through (a local file's mapping, and those of the
        files beside it). An output made before stays; its checksum function, which reads
        through the same, does not work after, and no other output is made."""
        self._close()


async def _zarrify_store_async(location: str, url: str | None = None, *, max_objects: int | None = None,
                               checksums: bool = False, workers: int = WORKERS, policy: Policy | None = None,
                               fmt: str | None = None,
                               layouts: Iterable[Image | Dataset] | None = None,
                               registry: Resolver | None = None, written: bool = True,
                               ) -> tuple[str, StoreIrOutput | SeriesOutput]:
    """`_parse_store_async`, and the built-in output of what it parsed."""
    parsed = await _parse_store_async(location, url, max_objects=max_objects, checksums=checksums, workers=workers,
                                      policy=policy, fmt=fmt, layouts=layouts, registry=registry, written=written)
    if isinstance(parsed, SeriesOutput):
        return "dicom", parsed
    out = await parsed.output_async(written)
    if isinstance(out, Output):
        raise TypeError("internal: a store run gave a file's output")
    return parsed.fmt, out


async def _parse_store_async(location: str, url: str | None = None, *, max_objects: int | None = None,
                             checksums: bool = False, workers: int = WORKERS, policy: Policy | None = None,
                             fmt: str | None = None,
                             layouts: Iterable[Image | Dataset] | None = None,
                             registry: Resolver | None = None, written: bool = True,
                             ) -> _Parse | SeriesOutput:
    """The store at `location` parsed (§1.4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>), its documents
    read by up to `workers` requests at a time; with `checksums`, the output's ranges
    carry the CRC-32C of their bytes (spec/refkv.md §4.2
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#42-range>). A store at an http(s) URL
    is listed and read under the reader policy `policy` (default: `Policy()`), and one under a
    prefix of `registry` with the store registered for it. The store
    profile is `fmt`, when given, else the one its root keys select (§1.7
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#17-named-inputs-and-layouts>);
    `layouts` the layouts it writes (`refkv.zarrify.sources.layout_names`). A store of DICOM
    files, which is joined from its files' outputs and keeps no IR, is its output already
    (a `SeriesOutput`, made for `written`)."""
    location = local_path(location, policy)
    if fmt is not None:
        _no_store_layouts(layouts, fmt)  # before the store is listed
    named = fmt

    def listed() -> tuple[Listed, str]:
        """The store listed, and its profile; on a thread, whose connections are closed after."""
        store = open_store(location, url, max_objects=max_objects, workers=workers, policy=policy,
                           registry=registry)
        try:
            return store, named or choose_profile(store)
        except BaseException:
            store.close()
            raise
        finally:
            store._release()  # noqa: SLF001 (this thread's connections)

    store, fmt = await asyncio.to_thread(listed)
    if fmt == "dicom":
        try:
            return await _zarrify_dicom_store(store, location, url, checksums=checksums, policy=policy,
                                              workers=workers, registry=registry, written=written)
        finally:
            store.close()
    try:
        if named is None:
            _no_store_layouts(layouts, fmt)  # a named profile's were checked before the store was listed
        # the core's store formats: N5, a SAFE directory, and Zarr v2, which the core decides is
        # OME-Zarr 0.4's or not (the run's profile)
        parsed = await parse_store_async(store, {"ome-zarr": "zarr2"}.get(fmt, fmt))
        if fmt in ("zarr2", "ome-zarr"):
            if named == "ome-zarr" and parsed.profile != "ome-zarr":
                raise Rejected("not an OME-Zarr 0.4 store: the Zarr v2 store format found no OME-NGFF 0.4 "
                               "image, plate or bioformats2raw layout at its root")
            fmt = parsed.profile
    finally:
        store.close()
    return _Parse(fmt, parsed, _store_output(parsed, store, checksums), store.close)


def _store_output(parsed: StoreParse, store: Listed,
                  checksums: bool) -> Callable[[bool], Awaitable[StoreIrOutput]]:
    """A store parse's built-in output, as `_Parse` gives it: its IR set (for refkv.build, which
    reads a store's IR as a file's), and, with `checksums`, its checksum function, which reads
    `store`'s objects."""
    async def output(written: bool) -> StoreIrOutput:
        out = await asyncio.to_thread(parsed.output, True, checksums, written)
        out.ir = parsed.ir
        if checksums:
            keys = dict(zip(object_urls(store.url, list(store.objects)), store.objects, strict=True))
            out.checksum = lambda u, offset, length: crc32c(store.read_range(keys[u], offset, length))
        return out

    return output


async def _zarrify_dicom_store(store: Listed, location: str, url: str | None, *, checksums: bool,
                               policy: Policy | None, workers: int, registry: Resolver | None,
                               written: bool = True) -> SeriesOutput:
    """The DICOM store form (spec/zarrify/dicom.md §9
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#9-the-store-form>): each key
    ending in `.dcm` zarrified as a file, the others recorded."""
    keys = sorted(k for k in store.objects if k.endswith(".dcm"))
    others = sorted(k for k in store.objects if not k.endswith(".dcm"))

    async def read(key: str) -> ReferenceStore:
        # a local directory's files are read there, and named by the URL it is served from
        local = isinstance(store, DirStore)
        where = str(Path(location) / key) if local else object_url(location, key)
        named = object_url(store.url, key) if local or url is not None else None
        fmt, out = await _zarrify_async(where, named, checksums=checksums, policy=policy, fmt="dicom",
                                        registry=registry, written=written)
        return await asyncio.to_thread(out.to_store, policy=policy, registry=registry,
                                       info={"format": fmt, **out.summary})

    return await zarrify_series_async(keys, others, store.url, read, workers=workers)


def _zarrify_store(location: str, url: str | None = None, *, max_objects: int | None = None,
                   checksums: bool = False, workers: int = WORKERS, policy: Policy | None = None,
                   fmt: str | None = None,
                   layouts: Iterable[Image | Dataset] | None = None) -> tuple[str, StoreIrOutput]:
    """`_zarrify_store_async`, the caller's thread waiting for it (`refkv._sync.run`), for a
    store the core reads: a store of DICOM files is joined in `zarrify_async`'s path alone."""
    fmt, out = _sync.run(_zarrify_store_async(location, url, max_objects=max_objects, checksums=checksums,
                                              workers=workers, policy=policy, fmt=fmt, layouts=layouts))
    if isinstance(out, SeriesOutput):
        raise TypeError("a store of DICOM files has no IR output: zarrify it with zarrify_store")
    return fmt, out


async def _parse_beside_async(fmt: str, location: str, url: str | None, transport: Transport, *,
                              checksums: bool = False, policy: Policy | None = None,
                              connections: int | None = None,
                              scope: list[str] | None = None, registry: Resolver | None = None) -> _Parse:
    """The parse of the input at `location` of a profile of `BESIDE`, a file (opened as
    `transport`) read with the files beside it: a VSI slide's `.vsi` file, whose tile files lie
    beside it (spec/zarrify/vsi.md §9.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#92-the-tile-files>), or a NRRD
    header file, whose data file a detached header names (spec/zarrify/nrrd.md §8.3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#83-the-data-file>): a store
    run of the core's `fmt` format over the files, probed as the run asks for them
    (`refkv.host.beside.VsiFiles`). `url`, when given, is the URL of the named file's
    directory (ending in `/`), the store URL the output's object URLs are made from. `scope`, when
    given, the keys of the input's scope (§1.9
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#19-sources-beside-the-input>,
    **Scope**; `scope_keys`): no file outside it is opened."""
    title, noun = BESIDE[fmt]
    store = VsiFiles(location, url, policy=policy, workers=connections, transport=transport, title=title, noun=noun,
                     case=fmt in CASE, registry=registry)
    try:
        parsed = await parse_store_async(store, fmt, scope=scope)
    finally:
        store.close()
    return _Parse(fmt, parsed, _store_output(parsed, store, checksums), store.close_files)


def local_path(location: str, policy: Policy | None = None) -> str:
    """The local path a `file:` URL `location` names, which zarrify reads as it reads that path,
    whether or not a store is registered for `file:` URLs (§1.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>); any other `location` as
    it is. Raises `refkv.errors.ResolutionError` for a `file:` URL with a host (a UNC path) that
    `policy` (default `Policy()`) does not allow, and ValueError for one that names no local path."""
    if location[:5].lower() != "file:":  # `file:///x`, or RFC 8089's minimal form `file:/x`
        return location
    policy = policy or Policy()
    policy.check_file_host(location)
    try:
        return file_path(location, allow_host=policy.allow_file_hosts)
    except ValueError as e:
        raise ValueError(f"{location}: {e}") from None


def is_store(location: str) -> bool:
    """Whether `location` is a store input (§1.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>): a URL (not a `file:` one)
    whose path ends in `/`, or a local directory, by its path or its `file:` URL (the core's
    `refkv_ir::host::input`, under the default policy's `file:` rules)."""
    try:
        r = _host_rule("input", location=location, windows=os.name == "nt", allow_file_hosts=False)
    except ValueError:  # a `file:` URL that names no local path: read as a file, which fails there
        return False
    if r["kind"] != "local":
        return r["kind"] == "store"
    return Path(r["path"]).is_dir()


async def _zarrify_async(location: str, url: str | None = None, *, checksums: bool = False,
                         policy: Policy | None = None, connections: int | None = None,
                         byte_cost: float | None = None, workers: int = WORKERS, fmt: str | None = None,
                         layouts: Iterable[Image | Dataset] | None = None, scope: Scope | None = None,
                         registry: Resolver | None = None,
                         written: bool = True) -> tuple[str, Output | StoreIrOutput | SeriesOutput]:
    """`_parse_async`, and the built-in output of what it parsed: (format, output)."""
    parsed = await _parse_async(location, url, checksums=checksums, policy=policy, connections=connections,
                                byte_cost=byte_cost, workers=workers, fmt=fmt, layouts=layouts, scope=scope,
                                registry=registry, written=written)
    if isinstance(parsed, SeriesOutput):
        return "dicom", parsed
    return parsed.fmt, await parsed.output_async(written)


async def _parse_async(location: str, url: str | None = None, *, checksums: bool = False,
                       policy: Policy | None = None, connections: int | None = None,
                       byte_cost: float | None = None, workers: int = WORKERS, fmt: str | None = None,
                       layouts: Iterable[Image | Dataset] | None = None, scope: Scope | None = None,
                       registry: Resolver | None = None, written: bool = True) -> _Parse | SeriesOutput:
    """The parse (`_Parse`) of the file at `location`, by its first bytes, or of
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
    refused), and a URL under a prefix of `registry` is read with the store registered for it.
    For a file input, `connections` is how many requests run at a time to a
    remote source (default 8), and `byte_cost` the seconds of wall time the read
    planner charges a byte fetched beyond those asked (default 0.1 s per MB).
    A store's documents are read by up to `workers` requests at a time. `layouts`: the
    layouts the output holds (`refkv.zarrify.sources.layout_names`). `written`: the output is written
    as it is; false for one only built from (`refkv.build.project`), which is not
    held to the payload limit with checksums (`Failed`), as the output built from it is. `scope`:
    what a file input may read besides itself (`scope_keys`); a store input, whose objects are its
    listing's, takes none.

    The options that are an output's (`checksums`, `layouts`) are kept for the parse's
    `output_async`, which takes `written`; a store of DICOM files, which keeps no IR, is its
    output already (a `SeriesOutput`, made for `written`)."""
    if fmt is None and is_store(location):
        if scope is not None:
            raise ValueError("a store input reads the objects its listing gives: scope is for a file input")
        return await _parse_store_async(location, url, checksums=checksums, workers=workers, policy=policy,
                                        layouts=layouts, registry=registry, written=written)
    within = None if scope is None else scope_keys(location, scope)
    location = local_path(location, policy)  # a `file:` URL, read as its path (its scope keys are the same)
    # a Hub tree's web page (§1.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>)
    if location.startswith(("http://", "https://")) and hub_page(location, hub_endpoint()):
        raise Rejected(f"{location} is a Hub repository's page, not a file: end it in / to zarrify the repository")
    # the names, before the file is read when the profile is named (once: `layouts` may be an iterator)
    names = layout_names(layouts, fmt) if fmt is not None else None
    transport = await asyncio.to_thread(open_transport, location, policy, registry)
    if fmt is None:
        fmt = await ir.sniffed(transport)
        if fmt is None:
            raise Rejected(NOT_SUPPORTED)
        names = layout_names(layouts, fmt)
    if fmt in BESIDE and fmt not in ON_DEMAND:
        return await _parse_beside_async(fmt, location, url, transport, checksums=checksums, policy=policy,
                                         connections=connections, scope=within, registry=registry)
    try:
        parsed = await ir.parse_async(location, url, transport=transport, connections=connections,
                                      byte_cost=byte_cost, fmt=fmt)
    except ir.NamesSiblingsError as e:
        return await _parse_beside_async(e.args[0], location, url, transport, checksums=checksums, policy=policy,
                                         connections=connections, scope=within, registry=registry)

    async def output(written: bool) -> Output:
        out = await parsed.output_async(layouts=names, checksums=checksums, written=written)
        if checksums:
            out.checksum = lambda _u, offset, length: crc32c(transport.get(offset, offset + length))
        return out

    return _Parse(parsed.fmt, parsed, output, parsed.close)


def _zarrify(location: str, url: str | None = None, *, checksums: bool = False,
             policy: Policy | None = None, connections: int | None = None,
             byte_cost: float | None = None, workers: int = WORKERS, fmt: str | None = None,
             layouts: Iterable[Image | Dataset] | None = None,
             scope: Scope | None = None) -> tuple[str, Output | StoreIrOutput]:
    """`_zarrify_async`, the caller's thread waiting for it (`refkv._sync.run`), for an input the
    core reads: a store of DICOM files is joined in `zarrify_async`'s path alone."""
    fmt, out = _sync.run(_zarrify_async(location, url, checksums=checksums, policy=policy, connections=connections,
                                        byte_cost=byte_cost, workers=workers, fmt=fmt, layouts=layouts,
                                        scope=scope))
    if isinstance(out, SeriesOutput):
        raise TypeError("a store of DICOM files has no IR output: zarrify it with zarrify")
    return fmt, out


async def _built_in(parse: _Parse | SeriesOutput, policy: Policy | None, registry: Resolver) -> ReferenceStore:
    """The built-in output of a parsed input, as a `refkv.ReferenceStore`: its projection, then
    the store, on a thread of its own."""
    if isinstance(parse, SeriesOutput):
        fmt, out = "dicom", parse
    else:
        fmt, out = parse.fmt, await parse.output_async()

    def store() -> ReferenceStore:
        return out.to_store(policy=policy, registry=registry, info={"format": fmt, **out.summary})

    return await _on_own_thread(store)


async def _laid_out(planned: Planned, parse: _Parse | SeriesOutput, location: str, url: str | None, *,
                    policy: Policy | None, registry: Registry | None, resolver: Resolver, checksums: bool,
                    scope: Scope | None = None) -> ReferenceStore:
    """The output the plugin layout `planned` writes of a parsed input (spec/layout.md
    <https://github.com/d-v-b/refkv/blob/main/spec/layout.md>): the input as `refkv.build.read`
    gives it, the plugin's `write` on a thread of its own, and its output sealed
    (`refkv.build.plugin.apply`). No output of refkv's own is built, unless the plugin asks for
    it. The input is closed when the plugin's output is made."""
    if isinstance(parse, SeriesOutput):  # the DICOM store form's, a series of files' outputs with no IR
        raise LayoutRejected(f"{planned.name}: a store of DICOM files has no IR for a plugin layout: "
                             "name one of its files")
    # a `file:` URL, read as its path
    parsed = await _build_parsed.parsed_async(parse, local_path(location, policy), url, policy=policy,
                                              registry=registry, resolver=resolver, checksums=checksums, scope=scope)
    try:
        info = {"format": parsed.profile, "elements": len(parsed.ir)}
        return await _on_own_thread(functools.partial(_build_plugin.apply, planned, [parsed], info=info))
    finally:
        parsed.close()


async def _on_own_thread[T](f: Callable[..., T], *args: object) -> T:
    """`f(*args)`, run on a thread of its own: a plugin's code may call refkv's blocking API,
    zarrify itself among it, which needs the event loop's executor that `asyncio.to_thread` would
    have it hold (spec/layout.md <https://github.com/d-v-b/refkv/blob/main/spec/layout.md>)."""
    loop = asyncio.get_running_loop()
    done: asyncio.Future[T] = loop.create_future()

    def settle(result: T | None, error: BaseException | None) -> None:
        if done.cancelled():
            return
        if error is not None:
            done.set_exception(error)
        else:
            done.set_result(result)  # type: ignore[arg-type]

    def run() -> None:
        try:
            result = f(*args)
        except Exception as e:  # noqa: BLE001 (handed to the caller)
            outcome: tuple[T | None, BaseException | None] = (None, e)
        except BaseException as e:  # noqa: BLE001 (handed on as a failure: the caller's future must settle)
            outcome = (None, LayoutFailed(described(e)))
        else:
            outcome = (result, None)
        with contextlib.suppress(RuntimeError):  # the loop is closed: nobody waits for it
            loop.call_soon_threadsafe(settle, *outcome)

    threading.Thread(target=run, name="refkv-layouts", daemon=True).start()
    return await done


def _planned(layout: object, layouts: object) -> Planned | None:
    """The plugin layout asked for, loaded and checked before the input is opened."""
    if layout is None:
        return None
    if layouts is not None:
        raise LayoutUsageError("a call names a plugin layout (layout=) or the built-in layouts (layouts=), not both: "
                               "a plugin's output is the whole output")
    return plan(layout)


async def zarrify_async(location: str | Source | Store, url: str | None = None, *, checksums: bool | None = None,
                        policy: Policy | None = None, connections: int | None = None,
                        byte_cost: float | None = None, workers: int | None = None,
                        layouts: Iterable[Image | Dataset] | None = None,
                        scope: Scope | None = None,
                        registry: Registry | None = None, layout: Layout | None = None) -> ReferenceStore:
    """The file at `location` (a local path or `file:` URL, an http(s) URL, or a URL under a
    prefix of `registry`), by its first bytes, or
    the store at `location` (a URL ending in `/`, or a local directory by its path or `file:`
    URL), zarrified
    (spec/zarrify.md <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md>), as a
    `refkv.ReferenceStore`: the Zarr hierarchy, each chunk a reference to the
    bytes of the source that hold it; `zarrify` is the same, blocking.

        store = await zarrify_async("https://example.org/image.ome.tiff")
        store.info["format"]      # "tiff"
        store.save_vzip("image.vzip")  # the vzip archive

    The store reads in memory (`store.get`, `refkv.zarr.store(store)`), its sources
    under `policy`, and `store.save_vzip(path)` writes it as a vzip archive. `store.info`
    is `{"format": ..., **summary}`: the profile that zarrified the input (such as
    "tiff", "nifti" or "zarr2") and its summary of what it found, as `python -m
    refkv.zarrify` prints it. While the
    input's reads are in flight the event loop is free: the reads block, each on a thread of
    the run's own, and the calls into the Rust core run on those threads too.

    - `url`: the URL the archive names the source by (default: `location`, a local
      path percent-encoded as a URI reference). A local directory's is the store URL
      (ending in `/`) it is served from, or its own `file:` URL, the default (spec/zarrify.md
      §1.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>).

      A `file:` URL `location` is read as the local path it names, with or without a
      `file://` store in `registry`, and gives the store that path gives: the returned
      store's policy allows the input's own files, below its directory, and no other local
      file. Saving it (`save_vzip`) still checks its sources against the policy of the
      archive's readers.
    - `checksums`: each range of a url source carries the CRC-32C of its bytes
      (spec/refkv.md §4.2 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#42-range>),
      which reads every byte the store references.
    - `policy`: the reader policy (spec/refkv.md §6.7
      <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>) the input is
      read under, and the store's sources after it (default `Policy()`: private
      hosts refused). `save_vzip` checks the sources against the policy the archive's
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
    - `scope`: what a file input may read besides itself, when it names files beside it (spec/zarrify.md
      §1.9 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#19-sources-beside-the-input>),
      such as an HDF5 file's external storage or a detached NRRD header's data file: a path, or
      several, written as `location` is (local paths, or URLs), each the input's
      directory or a file or directory below it (default: the input's directory). A file the input
      needs outside it is not opened, and the input is rejected, naming it (`out of scope: the input
      names the file "<key>"`); `scope=location` reads the input alone. Give one for an untrusted
      file in a directory that holds files it must not reach, such as other users' uploads. A
      store input takes none: its objects are its listing's.
    - `registry`: the application's object stores by URL prefix, as `refkv.open` takes them
      (`refkv.reader.Resolver`): an obspec_utils `ObjectStoreRegistry` (VirtualiZarr's), or a
      mapping such as `{"s3://my-bucket": S3Store("my-bucket", ...)}`. A file or a store under a
      registered prefix is read with its store (`head`, `get_range` and `get_ranges`; a store
      listed with `list`), which makes its own connections with its own credentials; the
      returned store reads its sources with the registry too. Registering a prefix allows its
      URLs under the policy's rule 1, as for `refkv.open`.

            registry = {"s3://my-bucket": S3Store("my-bucket", region="us-east-1")}
            store = zarrify("s3://my-bucket/data/file.nc", registry=registry)
    - `layout`: a layout a plugin writes, `Layout("acme-ome.ome06")` or `Layout(plugin)`
      (`refkv.zarrify.layouts`; spec/layout.md
      <https://github.com/d-v-b/refkv/blob/main/spec/layout.md>), for a file or store input
      of a profile the plugin's `formats` name: the output is then the plugin's, written from
      the input as parsed (a `refkv.build.Parsed`) and sealed (`refkv.build.seal`); refkv's own
      output is built only where the plugin asks for it. `layouts` is None.

    `location` may instead be a source or store class's instance, such as
    `TiffSource(url)` or `Zarr2Store(url)` (`refkv.zarrify.sources`), which names the profile,
    so that the input is not tested for one, and holds the read options: `url`,
    `checksums`, `policy`, `connections`, `byte_cost`, `workers`, `scope` and `registry` are then its own, and
    passing one beside it with any value but None raises `TypeError`.

    Errors:

    - `Rejected` (`refkv.zarrify.Rejected`): no profile accepts the input, or the profile
      that reads it finds what its specification rejects (spec/zarrify.md §1.2
      <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>).
    - `refkv.errors.ResolutionError`: the reader policy refuses the input, or a request for it
      (a host, an address it resolves to, a redirect target, a `file:` URL's host), or the input
      is a URL of another scheme than http(s) or `file` that `registry` has no store for.
    - `FileNotFoundError`: a local path (or `file:` URL) that does not exist.
    - `OSError`: the input could not be read, such as an HTTP status no retry mends, a
      response of the wrong length, or a local file that changed while it was read.
      `ListingFailed` and `StoreLimit` (`refkv.zarrify`) are the two of a store: its listing
      could not be read (a network error, or a 408, 429 or 5xx status), or it lists more than
      `max_objects` objects. A registered store raises its own errors, as it raises them.
    - `Failed` (`refkv.zarrify.Failed`, a `ValueError`): the output would pass the payload limit
      only with its checksums, which is no rejection of the input (spec/zarrify.md §1.2
      <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>).
    - `ValueError`: a `file:` URL that names no local path (one with a query, say), or an
      argument out of range.
    - `TypeError`: a read option passed beside a source or store instance.
    - `NotImplementedError`: a layout the profile cannot write.
    - `refkv.zarrify.layouts.LayoutUsageError` (a `ValueError`), `LayoutRejected` (a
      `Rejected`) and `LayoutFailed`: a plugin layout asked for wrongly, refused for this
      input, or failed."""
    planned = await _on_own_thread(_planned, layout, layouts)
    if isinstance(location, (Source, Store)):
        given = {"url": url, "checksums": checksums, "policy": policy, "connections": connections,
                 "byte_cost": byte_cost, "workers": workers, "scope": scope, "registry": registry}
        if any(v is not None for v in given.values()):
            options = ", ".join(k for k, v in given.items() if v is not None)
            raise TypeError(f"{options}: a source's read options are its own, {type(location).__name__}(location, ...)")
        source = location
        resolver = Resolver(source.registry)
        if isinstance(source, Store):
            where, scoped, parsing = source.store_url, None, _parse_store_async(
                source.store_url, source.url, max_objects=source.max_objects, checksums=source.checksums,
                workers=source.workers, policy=source.policy, fmt=source.profile, layouts=layouts,
                registry=resolver, written=planned is None)
        else:
            where, scoped, parsing = source.location, source.scope, _parse_async(
                source.location, source.url, checksums=source.checksums, policy=source.policy,
                connections=source.connections, byte_cost=source.byte_cost, fmt=source.profile, layouts=layouts,
                scope=source.scope, registry=resolver, written=planned is None)
        if planned is not None:
            return await _laid_out(planned, await parsing, where, source.url, policy=source.policy,
                                   registry=source.registry, resolver=resolver, checksums=bool(source.checksums),
                                   scope=scoped)
        return await _built_in(await parsing, source.policy, resolver)
    resolver = Resolver(registry)
    parsing = _parse_async(location, url, checksums=bool(checksums), policy=policy, connections=connections,
                           byte_cost=byte_cost, workers=WORKERS if workers is None else workers, layouts=layouts,
                           scope=scope, registry=resolver, written=planned is None)
    if planned is not None:
        return await _laid_out(planned, await parsing, location, url, policy=policy, registry=registry,
                               resolver=resolver, checksums=bool(checksums), scope=scope)
    return await _built_in(await parsing, policy, resolver)


def zarrify(location: str | Source | Store, url: str | None = None, *, checksums: bool | None = None,
            policy: Policy | None = None, connections: int | None = None,
            byte_cost: float | None = None, workers: int | None = None,
            layouts: Iterable[Image | Dataset] | None = None, scope: Scope | None = None,
            registry: Registry | None = None, layout: Layout | None = None) -> ReferenceStore:
    """`zarrify_async`, the caller's thread waiting for it: the same arguments, result and errors.
    Called from a coroutine on refkv's own event loop, it raises RuntimeError (await
    `zarrify_async` there)."""
    return _sync.run(zarrify_async(location, url, checksums=checksums, policy=policy, connections=connections,
                                   byte_cost=byte_cost, workers=workers, layouts=layouts, scope=scope,
                                   registry=registry, layout=layout))


async def zarrify_store_async(location: str, url: str | None = None, *, max_objects: int | None = None,
                              checksums: bool = False, workers: int = WORKERS, policy: Policy | None = None,
                              layouts: Iterable[Image | Dataset] | None = None,
                              registry: Registry | None = None) -> ReferenceStore:
    """The store at `location` (§1.4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>), zarrified: what
    `zarrify_async` does for a store, with the same `url` (a local directory's own `file:` URL
    by default),
    `checksums`, `policy`, `workers`, `layouts` and `registry`, and the same errors; `max_objects` caps how
    many objects the store may list (beyond it, `StoreLimit`). `workers`, `max_objects` and
    `policy` apply to a store at an http(s) URL: a local directory is read one document at a
    time, without them."""
    resolver = Resolver(registry)
    fmt, out = await _zarrify_store_async(location, url, max_objects=max_objects, checksums=checksums,
                                          workers=workers, policy=policy, layouts=layouts, registry=resolver)
    return await asyncio.to_thread(out.to_store, policy=policy, registry=resolver,
                                   info={"format": fmt, **out.summary})


def zarrify_store(location: str, url: str | None = None, *, max_objects: int | None = None,
                  checksums: bool = False, workers: int = WORKERS, policy: Policy | None = None,
                  layouts: Iterable[Image | Dataset] | None = None, registry: Registry | None = None) -> ReferenceStore:
    """`zarrify_store_async`, the caller's thread waiting for it, as `zarrify` waits for
    `zarrify_async`."""
    return _sync.run(zarrify_store_async(location, url, max_objects=max_objects, checksums=checksums,
                                         workers=workers, policy=policy, layouts=layouts, registry=registry))
