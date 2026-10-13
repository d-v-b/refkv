"""Virtualizing image files and N5 / Zarr v2 stores in vzip archives (spec/virtualize.md).

TIFF (NDPI included), ND2, CZI, DICOM and NIfTI files, and Sentinel-2 SAFE products
(spec/virtualize/safe.md: a `.SAFE.zip` file, or a `.SAFE` directory, a store input), go
through the IR (`vzip.ir`): the Rust core's sans-IO parser of the format, driven by its
read planner (a file) or by the store's reads (`vzip.ir.store`), projected to the
convention's hierarchy and mirrored under `vzip_source` by the Rust core, whose TIFF
parser finds NDPI itself (spec/virtualize/ndpi.md §4
<https://github.com/d-v-b/vzip/blob/main/spec/virtualize/ndpi.md#4-ndpi-profile>).
Every other profile is a subpackage: ims (file inputs), n5, zarr2, ome_zarr
(store inputs, spec/virtualize/n5.md, spec/virtualize/zarr2.md and
spec/virtualize/ome-zarr.md); common holds what they share, and store the store
machinery (§1.4 <https://github.com/d-v-b/vzip/blob/main/spec/virtualize.md#14-store-inputs>–§1.6
<https://github.com/d-v-b/vzip/blob/main/spec/virtualize.md#16-json-documents>).

    python -m vzip.virtualize <url or path> <out.vzip> [--url <source url>] [--checksums]
        [--allow-private-hosts] [--connections N] [--byte-cost SECONDS_PER_MB]

Exits with status 3 if the input is rejected, and with status 1 if the reader policy
refuses the input, or a source the archive would name (such as a `--url` on a private
host without `--allow-private-hosts`): the archive is checked against the policy the
input is read under, and is not written.

In Python, `virtualize(location)` (or `virtualize_store`, for a store) returns the
result as a `vzip.ReferenceStore`: readable in memory, and written as a vzip archive
by `store.save(path)`; `store.info` holds the format and the profile's summary.

A file or a store at an http(s) URL is read under the reader policy (spec/archive.md
§6.7 <https://github.com/d-v-b/vzip/blob/main/spec/archive.md#67-reader-policy>): a host that is, or
resolves to, a loopback, private, link-local or special
address is refused unless the policy has `allow_private_hosts`
(`--allow-private-hosts`), and a request that would go through a proxy unless
`allow_unchecked_proxy`. Every request for the input is made under it: a file's,
and a store's listing and object reads, each redirect target checked before it is
requested.

Every url source pins its size, and the input file's source its ETag when
every response for it gave the same strong one (spec/virtualize.md §1.2
<https://github.com/d-v-b/vzip/blob/main/spec/virtualize.md#12-input>, §1.4
<https://github.com/d-v-b/vzip/blob/main/spec/virtualize.md#14-store-inputs>).
`--checksums` also records the CRC-32C of every range of a url source
(spec/archive.md §4.2 <https://github.com/d-v-b/vzip/blob/main/spec/archive.md#42-range>), which
reads all the bytes the archive references: off by default.

The archive's source is the input URL (or, with --url, the given one, which is
how a local file is described by the URL it will be served from). A URL ending
in `/`, or a local directory, is a store input.
"""

from __future__ import annotations

from vzip.policy import Policy, crc32c
from vzip.refstore import ReferenceStore
from vzip.virtualize.common import Output, Rejected, file_reader, http_reader
from vzip.virtualize.ims import virtualize_ims
from vzip.virtualize.n5 import virtualize_n5
from vzip.virtualize.ome_zarr import declares_04, virtualize_ome_zarr
from vzip.virtualize.store import WORKERS, StoreOutput, choose_profile, object_url, open_store
from vzip.virtualize.zarr2 import virtualize_zarr2
from vzip.ir.store import StoreIrOutput

# The HDF5 signature, which starts Imaris IMS files (§1.2
# <https://github.com/d-v-b/vzip/blob/main/spec/virtualize.md#12-input>).
HDF5 = b"\x89HDF\r\n\x1a\n"
# The first bytes of a TIFF or BigTIFF file, in either byte order (§1.2
# <https://github.com/d-v-b/vzip/blob/main/spec/virtualize.md#12-input>).
TIFF_MAGIC = (b"II*\0", b"MM\0*", b"II+\0", b"MM\0+")
ND2_MAGIC = bytes.fromhex("DACEBE0A")
CZI_MAGIC = b"ZISRAWFILE" + bytes(6)
# A ZIP local file header: a SAFE product's zip form (spec/virtualize/safe.md §12.1
# <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/safe.md#121-choosing-the-profile>).
ZIP_MAGIC = b"PK\x03\x04"
NOT_SUPPORTED = "not a TIFF, NDPI, ND2, DICOM, NIfTI, IMS, CZI or SAFE zip file"

__all__ = ["Rejected", "virtualize", "virtualize_store"]


def is_dicom(head: bytes) -> bool:
    """The DICOM row of spec/virtualize.md §1.2
    <https://github.com/d-v-b/vzip/blob/main/spec/virtualize.md#12-input>."""
    return head[128:132] == b"DICM"


def _virtualize_store(location: str, url: str | None = None, *, max_objects: int | None = None,
                     checksums: bool = False, workers: int = WORKERS,
                     policy: Policy | None = None) -> tuple[str, StoreOutput | StoreIrOutput]:
    """(format, output) for the store at `location` (§1.4
    <https://github.com/d-v-b/vzip/blob/main/spec/virtualize.md#14-store-inputs>), its documents
    read by up to `workers` requests at a time; with `checksums`, the output's ranges
    carry the CRC-32C of their bytes (spec/archive.md §4.2
    <https://github.com/d-v-b/vzip/blob/main/spec/archive.md#42-range>). A store at an http(s) URL
    is listed and read under the reader policy `policy` (default: `Policy()`)."""
    store = open_store(location, url, max_objects=max_objects, workers=workers, policy=policy)
    try:
        fmt = choose_profile(store)
        if fmt == "safe":
            from vzip.ir.store import virtualize_listed

            fmt, out = fmt, virtualize_listed(store, "safe")[0]
        elif fmt == "n5":
            fmt, out = fmt, virtualize_n5(store)
        elif declares_04(store):
            fmt, out = "ome-zarr", virtualize_ome_zarr(store)
        else:
            fmt, out = fmt, virtualize_zarr2(store)
    finally:
        store.close()
    if checksums:
        keys = {object_url(store.url, k): k for k in store.objects}
        out.checksum = lambda u, offset, length: crc32c(store.read_range(keys[u], offset, length))
    return fmt, out


def is_store(location: str) -> bool:
    if location.startswith(("http://", "https://")):
        return location.split("?", 1)[0].split("#", 1)[0].endswith("/")
    import os

    return os.path.isdir(location)


def _virtualize(location: str, url: str | None = None, *, checksums: bool = False,
               policy: Policy | None = None, connections: int | None = None,
               byte_cost: float | None = None, workers: int = WORKERS) -> tuple[str, Output | StoreOutput | StoreIrOutput]:
    """(format, output) for the file at `location`, by its first bytes, or for
    the store at `location` (a URL ending in `/`, or a directory).

    Source 0 pins the file's size, and its ETag when every response gave the
    same strong one and the output names the URL that was read (§1.2
    <https://github.com/d-v-b/vzip/blob/main/spec/virtualize.md#12-input>). With
    `checksums`, the output's ranges of source 0 carry the CRC-32C of their
    bytes (spec/archive.md §4.2 <https://github.com/d-v-b/vzip/blob/main/spec/archive.md#42-range>),
    which reads them. `policy` is the reader policy a file
    or store at an http(s) URL is read under (default: `Policy()`, private hosts
    refused).
    For TIFF (NDPI included), ND2, CZI, DICOM, NIfTI and SAFE zip files, `connections` is how many requests run at a time to a
    remote source (default 8), and `byte_cost` the seconds of wall time the read
    planner charges a byte fetched beyond those asked (default 0.1 s per MB).
    A store's documents are read by up to `workers` requests at a time."""
    if is_store(location):
        return _virtualize_store(location, url, checksums=checksums, workers=workers, policy=policy)
    from vzip.ir import nifti_version
    from vzip.ir.planner import open_transport

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

    head = first(0, min(552, size))
    ir_format = "dicom" if is_dicom(head) else None
    if ir_format is None:
        if head[:4] in TIFF_MAGIC:
            ir_format = "tiff"  # or NDPI, which the core finds
        elif head[:4] == ND2_MAGIC:
            ir_format = "nd2"
        elif head[:16] == CZI_MAGIC:
            ir_format = "czi"
        elif nifti_version(head) is not None:
            ir_format = "nifti"
        elif head[:4] == ZIP_MAGIC:
            ir_format = "safe"
    if ir_format is not None:
        from vzip.ir import virtualize as via_ir

        fmt, out, _ = via_ir(location, url, transport=transport, connections=connections, byte_cost=byte_cost)
        if checksums:
            out.checksum = lambda u, offset, length: crc32c(transport.get(offset, offset + length))
        return fmt, out
    if location.startswith(("http://", "https://")):
        read, size = http_reader(location, policy=policy)
    else:
        read, size = file_reader(location)
    url = url or location
    fmt, out = _virtualize_file(url, read, size)
    out.size = size
    out.etag = read.etag() if url == location and hasattr(read, "etag") else None
    if checksums:
        exact = getattr(read, "uncached", read)
        out.checksum = lambda u, offset, length: crc32c(exact(offset, length))
    return fmt, out


def _virtualize_file(url: str, read, size: int) -> tuple[str, Output]:
    """The profiles that are not the IR's, by the file's first bytes."""
    head = read(0, min(552, size))
    if head[:8] == HDF5:
        return "ims", virtualize_ims(url, read, size)
    raise Rejected(NOT_SUPPORTED)


def virtualize(location: str, url: str | None = None, *, checksums: bool = False,
               policy: Policy | None = None, connections: int | None = None,
               byte_cost: float | None = None, workers: int = WORKERS) -> ReferenceStore:
    """The file at `location` (a local path or an http(s) URL), by its first bytes, or
    the store at `location` (a URL ending in `/`, or a directory), virtualized
    (spec/virtualize.md <https://github.com/d-v-b/vzip/blob/main/spec/virtualize.md>), as a
    `vzip.ReferenceStore`: the Zarr hierarchy, each chunk a reference to the
    bytes of the source that hold it.

        store = virtualize("https://example.org/image.ome.tiff")
        store.info["format"]      # "tiff"
        store.save("image.vzip")  # the vzip archive

    The store reads in memory (`store.get`, `vzip.zarr.store(store)`), its sources
    under `policy`, and `store.save(path)` writes it as a vzip archive. `store.info`
    is `{"format": ..., **summary}`: the profile that virtualized the input (such as
    "tiff", "nifti" or "zarr2") and its summary of what it found, as `python -m
    vzip.virtualize` prints it. An input no profile accepts raises `Rejected`.

    - `url`: the URL the archive names the source by (default: `location`); a
      local directory needs one, the store URL (ending in `/`) it is served from.
    - `checksums`: each range of a url source carries the CRC-32C of its bytes
      (spec/archive.md §4.2 <https://github.com/d-v-b/vzip/blob/main/spec/archive.md#42-range>),
      which reads every byte the store references.
    - `policy`: the reader policy (spec/archive.md §6.7
      <https://github.com/d-v-b/vzip/blob/main/spec/archive.md#67-reader-policy>) the input is
      read under, and the store's sources after it (default `Policy()`: private
      hosts refused). `save` checks the sources against the policy the archive's
      readers will use, `Policy()` unless given: pass it this one when the
      sources are on a private host.
    - `connections`, `byte_cost`: for a file the Rust core reads (`vzip.ir`), the
      requests at a time to a remote source (default 8), and the seconds of wall
      time the read planner charges a byte fetched beyond those asked (default
      0.1 s per MB).
    - `workers`: how many of a store's documents are read at a time."""
    fmt, out = _virtualize(location, url, checksums=checksums, policy=policy, connections=connections,
                           byte_cost=byte_cost, workers=workers)
    return out.to_store(policy=policy, info={"format": fmt, **out.summary})


def virtualize_store(location: str, url: str | None = None, *, max_objects: int | None = None,
                     checksums: bool = False, workers: int = WORKERS,
                     policy: Policy | None = None) -> ReferenceStore:
    """The store at `location` (§1.4
    <https://github.com/d-v-b/vzip/blob/main/spec/virtualize.md#14-store-inputs>), virtualized: what
    `virtualize` does for a store, with the same `url` (needed for a local directory),
    `checksums`, `policy` and `workers`; `max_objects` caps how many objects the
    store may list."""
    fmt, out = _virtualize_store(location, url, max_objects=max_objects, checksums=checksums,
                                 workers=workers, policy=policy)
    return out.to_store(policy=policy, info={"format": fmt, **out.summary})
