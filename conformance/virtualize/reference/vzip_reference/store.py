"""The store dispatch of `vzip.virtualize` as it was before SAFE directories and Zarr v2 and N5
stores moved onto the Rust IR (revision 26), with the frozen SAFE, Zarr v2 and N5 profiles
(`vzip_reference.safe`, `vzip_reference.zarr2`, `vzip_reference.n5`); the OME-Zarr profile is
the shipped one."""

from __future__ import annotations

from vzip.policy import Policy, crc32c
from vzip.virtualize.ome_zarr import declares_04, virtualize_ome_zarr
from vzip.virtualize.store import WORKERS, StoreOutput, choose_profile, object_url, open_store
from vzip_reference.n5 import virtualize_n5
from vzip_reference.safe import SafeOutput, virtualize_safe_store
from vzip_reference.zarr2 import virtualize_zarr2


def virtualize_store(location: str, url: str | None = None, *, max_objects: int | None = None,
                     checksums: bool = False, workers: int = WORKERS,
                     policy: Policy | None = None) -> tuple[str, StoreOutput | SafeOutput]:
    store = open_store(location, url, max_objects=max_objects, workers=workers, policy=policy)
    try:
        fmt = choose_profile(store)
        if fmt == "safe":
            fmt, out = fmt, virtualize_safe_store(store)
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
