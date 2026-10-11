"""vzip: ZIP plus a reference value type.

A vzip store maps keys to values that are either bytes or references to byte ranges
of other objects (spec/archive.md, Part 1
<https://github.com/d-v-b/vzip/blob/main/spec/archive.md#part-1-reference-values>). Any
backend that can tell a bytes value from a reference value can hold one; this package
reads and writes the ZIP binding, a vzip archive (Part 2
<https://github.com/d-v-b/vzip/blob/main/spec/archive.md#part-2-the-zip-binding>).

- `ReferenceStore`: a store in memory. `ReferenceStore.from_virtualizarr(vds)` builds
  one from a VirtualiZarr virtual dataset, and `store.save(path)` writes it as a
  vzip archive.
- `open(url)`: a saved archive, as a store (`VZipStore`) that reads the same values.
- Both have the operations of spec §6.2
  <https://github.com/d-v-b/vzip/blob/main/spec/archive.md#62-operations>: `get(key)`,
  or `get(key, RangeRequest(start, end))` (also `OffsetRequest`, `SuffixRequest`),
  `classify`, `raw`, `list`, each also a coroutine (`get_async`, ...).
- `Policy`, `Stats`: what a store may read, and what it fetched. Every store also
  takes `registry=`, the application's object stores by URL prefix (an
  `obspec_utils` `ObjectStoreRegistry`, or a mapping), and `max_concurrency`
  (8 requests in flight by default).

None of this needs Zarr. `vzip.zarr`, with the `zarr` extra (`pip install
'vzip[zarr]'`), opens a store for zarr and xarray:
`xr.open_zarr(vzip.zarr.open("data.vzip"))`, or `vzip.zarr.store(store)` for a
`ReferenceStore`.

docs/virtual-datasets.md
<https://github.com/d-v-b/vzip/blob/main/docs/virtual-datasets.md> walks through the
virtual-dataset workflow; `help(vzip.refstore)` has its details.
"""

from vzip.archive import VZipWriter
from vzip.pb import Concat, Range, Source
from vzip.policy import Policy, public_s3_url
from vzip.refstore import ReferenceStore, Summary
from vzip.store import OffsetRequest, RangeRequest, Stats, SuffixRequest, VZipStore


def open(url: str, **kwargs) -> VZipStore:  # noqa: A001 - like gzip.open, zarr.open
    """Open the vzip archive at `url` (a local path or an `http(s)` URL) as a
    read-only store: `vzip.open("data.vzip").get("zarr.json")`. For zarr and
    xarray, `vzip.zarr.open(url)` takes the same arguments and wraps the store.

    Keyword arguments go to `VZipStore`: `policy`, `registry` (the stores to
    read sources with, such as an `obspec_utils` `ObjectStoreRegistry` or a
    mapping `{"s3://bucket": S3Store(...)}`), `stats`, `max_concurrency` (8 by
    default; None for no cap), and `resolve`."""
    return VZipStore(str(url), **kwargs)


def __getattr__(name: str):
    # `import vzip` then `vzip.zarr.open(...)`: the adapter is imported on first use,
    # so that `import vzip` imports no zarr
    if name == "zarr":
        import importlib

        return importlib.import_module("vzip.zarr")
    raise AttributeError(f"module 'vzip' has no attribute {name!r}")


__all__ = [
    "Concat",
    "OffsetRequest",
    "Policy",
    "Range",
    "RangeRequest",
    "ReferenceStore",
    "Source",
    "Stats",
    "SuffixRequest",
    "Summary",
    "VZipStore",
    "VZipWriter",
    "open",
    "public_s3_url",
]
