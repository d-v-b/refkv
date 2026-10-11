"""vzip: ZIP plus a reference value type.

A vzip store maps keys to values that are either bytes or references to byte ranges
of other objects (spec/archive.md, Part 1
<https://github.com/d-v-b/vzip/blob/main/spec/archive.md#part-1-reference-values>). Any
backend that can tell a bytes value from a reference value can hold one; this package
reads and writes the ZIP binding, a vzip archive (Part 2
<https://github.com/d-v-b/vzip/blob/main/spec/archive.md#part-2-the-zip-binding>).

- `ReferenceStore`: a store in memory. `ReferenceStore.from_virtualizarr(vds)` builds
  one from a VirtualiZarr virtual dataset; `xr.open_zarr(store)` reads it, and
  `store.save(path)` writes it as a vzip archive.
- `open(url)`: a saved archive, as a store (`VZipStore`) that reads the same values.
- `Policy`, `Resolver`, `Stats`: what a store may read, how it reaches sources other
  than http(s), and what it fetched.

See `help(vzip.refstore)` for the virtual-dataset workflow.
"""

from vzip.archive import VZipWriter
from vzip.pb import Concat, Range, Source
from vzip.policy import Policy, public_s3_url
from vzip.refstore import ReferenceStore, Summary
from vzip.store import Resolver, Stats, VZipStore


def open(url: str, **kwargs) -> VZipStore:  # noqa: A001 - like gzip.open, zarr.open
    """Open the vzip archive at `url` (a local path or an `http(s)` URL) as a
    read-only zarr store: `xr.open_zarr(vzip.open("data.vzip"))`.

    Keyword arguments go to `VZipStore`: `policy`, `resolver`, `stats`,
    `max_concurrency`, and `resolve`."""
    return VZipStore(str(url), **kwargs)


__all__ = [
    "Concat",
    "Policy",
    "Range",
    "ReferenceStore",
    "Resolver",
    "Source",
    "Stats",
    "Summary",
    "VZipStore",
    "VZipWriter",
    "open",
    "public_s3_url",
]
