"""refkv: a reference value type for key-value stores.

A reference store maps keys to values that are either bytes or references to byte ranges
of other objects (spec/archive.md, Part 1
<https://github.com/d-v-b/refkv/blob/main/spec/archive.md#part-1-reference-values>). Any
backend that can tell a bytes value from a reference value can hold one; a binding says
how. The ZIP binding keeps a store in one ZIP file, a vzip archive (Part 2
<https://github.com/d-v-b/refkv/blob/main/spec/archive.md#part-2-the-zip-binding>), which
`refkv.vzip` reads and writes.

- `ReferenceStore`: a store in memory. `ReferenceStore.from_virtualizarr(vds)` builds
  one from a VirtualiZarr virtual dataset, and `store.save(path)` writes it as a
  vzip archive.
- `open(url)`: a saved store, as a store that reads the same values: with the ZIP
  binding (the default, and today the only one), a `refkv.vzip.VZipStore`.
- Both have the operations of spec/archive.md §6.2
  <https://github.com/d-v-b/refkv/blob/main/spec/archive.md#62-operations>: `get(key)`,
  or `get(key, RangeRequest(start, end))` (also `OffsetRequest`, `SuffixRequest`),
  `classify`, `raw`, `list`, each also a coroutine (`get_async`, ...).
- `Policy`, `Stats`: what a store may read, and what it fetched. Every store also
  takes `registry=`, the application's object stores by URL prefix (an
  `obspec_utils` `ObjectStoreRegistry`, or a mapping), and `max_concurrency`
  (8 requests in flight by default).

None of this needs Zarr, and `import refkv` imports none of the ZIP binding's code
but the names it reserves (`refkv.vzip.names`): its reader and writer are imported when
a store is opened or saved. `refkv.zarr`, with the `zarr` extra (`pip
install 'refkv[zarr]'`), opens a store for zarr and xarray:
`xr.open_zarr(refkv.zarr.open("data.vzip"))`, or `refkv.zarr.store(store)` for a
`ReferenceStore`.

docs/virtual-datasets.md
<https://github.com/d-v-b/refkv/blob/main/docs/virtual-datasets.md> walks through the
virtual-dataset workflow; `help(refkv.refstore)` has its details.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from refkv.pb import Concat, Range, Source
from refkv.policy import Policy, public_s3_url
from refkv.reader import OffsetRequest, RangeRequest, Stats, SuffixRequest
from refkv.refstore import ReferenceStore, Summary

if TYPE_CHECKING:
    from refkv.vzip.store import VZipStore

BINDINGS = ("vzip",)
"""The bindings `open` can read a store with: the ZIP binding, `"vzip"`."""


def open(url: str, *, binding: str = "vzip", **kwargs) -> VZipStore:  # noqa: A001 - like gzip.open
    """Open the store saved at `url` (a local path or an `http(s)` URL, presigned or with
    a query string, of any name) as a read-only store, with the binding `binding`. The
    ZIP binding, `"vzip"` (the default, and today the only one), opens a vzip archive as a
    `refkv.vzip.VZipStore`: `refkv.open("data.vzip").get("zarr.json")`. For zarr and
    xarray, `refkv.zarr.open(url)` takes the same arguments and wraps the store.

    Other keyword arguments go to the binding's store: `policy`, `registry` (the stores
    to read sources with, such as an `obspec_utils` `ObjectStoreRegistry` or a mapping
    `{"s3://bucket": S3Store(...)}`), `stats`, `max_concurrency` (8 by default; None for
    no cap), and `resolve`. A binding not in `BINDINGS` is a ValueError."""
    if binding == "vzip":
        from refkv import vzip

        return vzip.open(str(url), **kwargs)
    raise ValueError(f"no binding {binding!r}: refkv reads stores with {', '.join(map(repr, BINDINGS))}")


def __getattr__(name: str):
    # `import refkv` then `refkv.zarr.open(...)`: the adapter is imported on first use,
    # so that `import refkv` imports no zarr. `refkv.vzip` is bound already (refstore
    # imports refkv.vzip.names); this keeps it reachable should that change
    if name in ("zarr", "vzip"):
        import importlib

        return importlib.import_module(f"refkv.{name}")
    raise AttributeError(f"module 'refkv' has no attribute {name!r}")


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
    "open",
    "public_s3_url",
]
