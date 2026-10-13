"""The frozen reference oracle of the TIFF, ND2, CZI, DICOM, NIfTI, NDPI and SAFE profiles: the
Python implementations `vzip.virtualize` shipped before it moved these formats to the
Rust IR (rust/vzip-ir), kept unchanged (but for their import paths, and the revision
SAFE records, 25) so that `compare.py` can compare the IR path against an independent
implementation.
Nothing shipped imports this package. Every other format, and what the profiles
share (`vzip.virtualize.common`), is the shipped code.

`virtualize(location, url, checksums=...)` is `vzip.virtualize._virtualize` (the
(format, output) under the public `virtualize`, which returns a ReferenceStore) as it
was: the same dispatch by first bytes, the same readers, with TIFF, ND2, CZI, DICOM, NIfTI, NDPI
and SAFE going to the frozen modules. A file or store at an http(s) URL is read by the
shipped readers under `policy` (default: `Policy()`, private hosts refused)."""

from __future__ import annotations

from vzip.policy import Policy, crc32c
from vzip.virtualize import is_store
from vzip.virtualize import _virtualize_store as shipped_store  # the profile output, not the store
from vzip.virtualize.common import Output, Rejected, file_reader, http_reader
from vzip_reference.dicom import is_dicom, virtualize_dicom
from vzip.virtualize.ims import virtualize_ims
from vzip_reference import nifti
from vzip.virtualize.store import open_store, choose_profile, object_url
from vzip_reference import ndpi
from vzip_reference.safe import SafeOutput, is_zip, virtualize_safe_store, virtualize_safe_zip
from vzip_reference.czi import is_czi, virtualize_czi
from vzip_reference.nd2 import is_nd2, virtualize_nd2
from vzip_reference.tiff import virtualize_tiff

HDF5 = b"\x89HDF\r\n\x1a\n"
TIFF_MAGIC = (b"II*\0", b"MM\0*", b"II+\0", b"MM\0+")
NOT_SUPPORTED = "not a TIFF, NDPI, ND2, DICOM, NIfTI, IMS, CZI or SAFE zip file"


def virtualize_store(location: str, url: str | None = None, *, checksums: bool = False, policy: Policy | None = None):
    """`vzip.virtualize.virtualize_store` as it was: a SAFE directory goes to the frozen
    profile, any other store to the shipped code."""
    store = open_store(location, url, policy=policy)
    try:
        if choose_profile(store) != "safe":
            store.close()
            return shipped_store(location, url, checksums=checksums, policy=policy)
        out = virtualize_safe_store(store)
    finally:
        store.close()
    if checksums:
        keys = {object_url(store.url, k): k for k in store.objects}
        out.checksum = lambda u, offset, length: crc32c(store.read_range(keys[u], offset, length))
    return "safe", out


def virtualize(location: str, url: str | None = None, *, checksums: bool = False,
               policy: Policy | None = None):
    if is_store(location):
        return virtualize_store(location, url, checksums=checksums, policy=policy)
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


def _virtualize_file(url: str, read, size: int) -> tuple[str, Output | SafeOutput]:
    head = read(0, min(552, size))
    if is_dicom(head):
        return "dicom", virtualize_dicom(url, read, size)
    if head[:4] in TIFF_MAGIC:
        first = ndpi.detect(read, size)
        if first is not None:
            return "ndpi", ndpi.virtualize_ndpi(url, read, size, first)
        return "tiff", virtualize_tiff(url, read, size)
    if is_nd2(head):
        return "nd2", virtualize_nd2(url, read, size)
    if head[:8] == HDF5:
        return "ims", virtualize_ims(url, read, size)
    if nifti.detect(head) is not None:
        return "nifti", nifti.virtualize_nifti(url, read, size)
    if is_czi(head):
        return "czi", virtualize_czi(url, read, size)
    if is_zip(head):
        return "safe", virtualize_safe_zip(url, read, size)
    raise Rejected(NOT_SUPPORTED)
