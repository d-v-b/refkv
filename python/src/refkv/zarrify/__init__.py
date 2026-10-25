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
        [--allow-private-hosts] [--connections N] [--byte-cost SECONDS_PER_MB]

Exits with status 3 if the input is rejected, and with status 1 if the reader policy
refuses the input, or a source the archive would name (such as a `--url` on a private
host without `--allow-private-hosts`): the archive is checked against the policy the
input is read under, and is not written.

In Python, `zarrify(location)` (or `zarrify_store`, for a store) returns the
result as a `refkv.ReferenceStore`: readable in memory, and written as a vzip archive
by `store.save(path)`; `store.info` holds the format and the profile's summary.

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

The archive's source is the input URL (or, with --url, the given one, which is
how a local file is described by the URL it will be served from). A URL ending
in `/`, or a local directory, is a store input.
"""

from __future__ import annotations

from refkv.policy import Policy, crc32c
from refkv.refstore import ReferenceStore
from refkv.zarrify.common import Output, Rejected
from refkv.zarrify.store import WORKERS, choose_profile, object_url, open_store
from refkv.ir.store import StoreIrOutput

NOT_SUPPORTED = "not a file of any supported format"

__all__ = ["Rejected", "zarrify", "zarrify_store"]


def _zarrify_store(location: str, url: str | None = None, *, max_objects: int | None = None,
                     checksums: bool = False, workers: int = WORKERS,
                     policy: Policy | None = None) -> tuple[str, StoreIrOutput]:
    """(format, output) for the store at `location` (§1.4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>), its documents
    read by up to `workers` requests at a time; with `checksums`, the output's ranges
    carry the CRC-32C of their bytes (spec/refkv.md §4.2
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#42-range>). A store at an http(s) URL
    is listed and read under the reader policy `policy` (default: `Policy()`)."""
    store = open_store(location, url, max_objects=max_objects, workers=workers, policy=policy)
    try:
        fmt = choose_profile(store)
        from refkv.ir.store import run_store

        if fmt == "n5":
            # the core's N5 store format
            out = run_store(store, "n5")[0]
        else:
            # the core's store formats: a SAFE directory, a Zarr v2 store, which the core decides
            # is OME-Zarr 0.4's or not (its summary's `format`)
            out = run_store(store, "safe" if fmt == "safe" else "zarr2")[0]
            if fmt != "safe":
                fmt = out.summary["format"]
    finally:
        store.close()
    if checksums:
        keys = {object_url(store.url, k): k for k in store.objects}
        out.checksum = lambda u, offset, length: crc32c(store.read_range(keys[u], offset, length))
    return fmt, out


def _zarrify_vsi(location: str, url: str | None, transport, *, checksums: bool = False,
                    policy: Policy | None = None, connections: int | None = None) -> tuple[str, StoreIrOutput]:
    """(format, output) for the VSI input at `location`, a `.vsi` file (opened as `transport`)
    whose tile files lie beside it (spec/zarrify/vsi.md §9.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#92-the-tile-files>): a store
    run of the core's `vsi` format over the files, probed as the run asks for them
    (`refkv.zarrify.vsi.VsiFiles`). `url`, when given, is the URL of the `.vsi` file's
    directory (ending in `/`), the store URL the output's object URLs are made from."""
    from refkv.ir.store import run_store
    from refkv.zarrify.vsi import VsiFiles

    store = VsiFiles(location, url, policy=policy, workers=connections, transport=transport)
    try:
        out = run_store(store, "vsi")[0]
    finally:
        store.close()
    if checksums:
        keys = {object_url(store.url, k): k for k in store.objects}
        out.checksum = lambda u, offset, length: crc32c(store.read_range(keys[u], offset, length))
    return "vsi", out


def is_store(location: str) -> bool:
    if location.startswith(("http://", "https://")):
        return location.split("?", 1)[0].split("#", 1)[0].endswith("/")
    import os

    return os.path.isdir(location)


def _zarrify(location: str, url: str | None = None, *, checksums: bool = False,
               policy: Policy | None = None, connections: int | None = None,
               byte_cost: float | None = None, workers: int = WORKERS) -> tuple[str, Output | StoreIrOutput]:
    """(format, output) for the file at `location`, by its first bytes, or for
    the store at `location` (a URL ending in `/`, or a directory).

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
    A store's documents are read by up to `workers` requests at a time."""
    if is_store(location):
        return _zarrify_store(location, url, checksums=checksums, workers=workers, policy=policy)
    from refkv.ir import sniff
    from refkv.ir.planner import open_transport

    transport = open_transport(location, policy)
    size = transport.size
    held = transport.head[1]

    def first(offset: int, length: int) -> bytes:
        """The source's bytes, from those the transport opened it with when it holds them."""
        if offset < 0 or offset + length > size:
            raise Rejected(f"read of [{offset}, {offset + length}) outside the {size}-byte file")
        if offset + length <= len(held):
            return held[offset:offset + length]
        return transport.get(offset, offset + length)

    ir_format = sniff(first(0, min(552, size)))
    if ir_format is None:
        raise Rejected(NOT_SUPPORTED)
    if ir_format == "vsi":
        return _zarrify_vsi(location, url, transport, checksums=checksums, policy=policy, connections=connections)
    from refkv.ir import zarrify as via_ir

    fmt, out, _ = via_ir(location, url, transport=transport, connections=connections, byte_cost=byte_cost)
    if checksums:
        out.checksum = lambda u, offset, length: crc32c(transport.get(offset, offset + length))
    return fmt, out


def zarrify(location: str, url: str | None = None, *, checksums: bool = False,
               policy: Policy | None = None, connections: int | None = None,
               byte_cost: float | None = None, workers: int = WORKERS) -> ReferenceStore:
    """The file at `location` (a local path or an http(s) URL), by its first bytes, or
    the store at `location` (a URL ending in `/`, or a directory), zarrified
    (spec/zarrify.md <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md>), as a
    `refkv.ReferenceStore`: the Zarr hierarchy, each chunk a reference to the
    bytes of the source that hold it.

        store = zarrify("https://example.org/image.ome.tiff")
        store.info["format"]      # "tiff"
        store.save("image.vzip")  # the vzip archive

    The store reads in memory (`store.get`, `refkv.zarr.store(store)`), its sources
    under `policy`, and `store.save(path)` writes it as a vzip archive. `store.info`
    is `{"format": ..., **summary}`: the profile that zarrified the input (such as
    "tiff", "nifti" or "zarr2") and its summary of what it found, as `python -m
    refkv.zarrify` prints it. An input no profile accepts raises `Rejected`.

    - `url`: the URL the archive names the source by (default: `location`); a
      local directory needs one, the store URL (ending in `/`) it is served from.
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
    - `workers`: how many of a store's documents are read at a time."""
    fmt, out = _zarrify(location, url, checksums=checksums, policy=policy, connections=connections,
                           byte_cost=byte_cost, workers=workers)
    return out.to_store(policy=policy, info={"format": fmt, **out.summary})


def zarrify_store(location: str, url: str | None = None, *, max_objects: int | None = None,
                     checksums: bool = False, workers: int = WORKERS,
                     policy: Policy | None = None) -> ReferenceStore:
    """The store at `location` (§1.4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>), zarrified: what
    `zarrify` does for a store, with the same `url` (needed for a local directory),
    `checksums`, `policy` and `workers`; `max_objects` caps how many objects the
    store may list."""
    fmt, out = _zarrify_store(location, url, max_objects=max_objects, checksums=checksums,
                                 workers=workers, policy=policy)
    return out.to_store(policy=policy, info={"format": fmt, **out.summary})
