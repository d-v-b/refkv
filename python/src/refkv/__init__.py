"""refkv: a reference value type for key-value stores.

A reference store maps keys to values that are either bytes or references to byte ranges
of other objects (spec/refkv.md, Part 1
<https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#part-1-reference-values>). Any
backend that can tell a bytes value from a reference value can hold one; a binding says
how. The ZIP binding keeps a store in one ZIP file, a vzip archive (Part 2
<https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#part-2-the-zip-binding>), which
`refkv.vzip` reads and writes; the perfect-hash binding keeps it in one file with a perfect hash
of its keys (spec/bindings/phkv.md
<https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md>), which `refkv.phkv` reads and
writes.

- `ReferenceStore`: a store in memory. `ReferenceStore.from_virtualizarr(vds)` builds
  one from a VirtualiZarr virtual dataset, and `store.save(path)` writes it as a
  vzip archive.
- `open(url)`: a saved store, as a store that reads the same values: with the ZIP
  binding (the default), a `refkv.vzip.VZipStore`, and with the perfect-hash binding
  (`binding="phkv"`, `store.save_phkv(path)` writes one), a `refkv.phkv.PhkvStore`.
- Both have the operations of spec/refkv.md §6.2
  <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#62-operations>: `get(key)`,
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

import importlib
from importlib.metadata import PackageNotFoundError, version
from types import ModuleType
from typing import TYPE_CHECKING, Any, Literal, TypedDict, Unpack, overload

from refkv import phkv, vzip
from refkv.pb import Concat, Range, Source
from refkv.policy import Policy, public_s3_url
from refkv.reader import OffsetRequest, RangeRequest, Stats, SuffixRequest
from refkv.refstore import ReferenceStore, Summary

try:
    __version__ = version("refkv")
    """The installed distribution's version (git's, at build: python/tools/version.py)."""
except PackageNotFoundError:  # a source tree on sys.path, not installed
    __version__ = "0.0.0+unknown"

if TYPE_CHECKING:
    from refkv.phkv.store import PhkvOptions, PhkvStore
    from refkv.reader import Registry
    from refkv.vzip.store import VZipOptions, VZipStore

    # closed (PEP 728), which typing.TypedDict takes only from Python 3.15
    class OpenOptions(TypedDict, total=False, closed=True):
        """`open`'s keyword arguments, which `refkv.zarr.open` passes on."""

        binding: str
        resolve: bool
        registry: Registry | None
        stats: Stats | None
        policy: Policy | None
        max_concurrency: int | None

BINDINGS = ("vzip", "phkv")
"""The bindings `open` can read a store with: the ZIP binding, `"vzip"`, and the perfect-hash
binding, `"phkv"`."""


@overload
def open(url: str, *, binding: Literal["vzip"] = "vzip", **kwargs: Unpack[VZipOptions]) -> VZipStore: ...  # noqa: A001 (refkv.open, the public name)
@overload
def open(url: str, *, binding: Literal["phkv"], **kwargs: Unpack[PhkvOptions]) -> PhkvStore: ...  # noqa: A001
@overload
def open(url: str, *, binding: str, **kwargs: Unpack[VZipOptions]) -> VZipStore | PhkvStore: ...  # noqa: A001
def open(url: str, *, binding: str = "vzip", **kwargs: Any) -> VZipStore | PhkvStore:  # noqa: A001 (refkv.open, the public name)
    """Open the store saved at `url` (a local path or an `http(s)` URL, presigned or with
    a query string, of any name) as a read-only store, with the binding `binding`. The
    ZIP binding, `"vzip"` (the default), opens a vzip archive as a `refkv.vzip.VZipStore`:
    `refkv.open("data.vzip").get("zarr.json")`. The perfect-hash binding, `"phkv"`, opens a phkv
    store as a `refkv.phkv.PhkvStore` (it has no `resolve` option). For zarr and
    xarray, `refkv.zarr.open(url)` takes the same arguments and wraps the store.

    Other keyword arguments go to the binding's store: `policy`, `registry` (the stores
    to read sources with, such as an `obspec_utils` `ObjectStoreRegistry` or a mapping
    `{"s3://bucket": S3Store(...)}`), `stats`, `max_concurrency` (8 by default; None for
    no cap), and `resolve`. A binding not in `BINDINGS` is a ValueError."""
    if binding == "vzip":
        return vzip.open(str(url), **kwargs)
    if binding == "phkv":
        return phkv.open(str(url), **kwargs)
    raise ValueError(f"no binding {binding!r}: refkv reads stores with {', '.join(map(repr, BINDINGS))}")


def __getattr__(name: str) -> ModuleType:
    # `import refkv` then `refkv.zarr.open(...)`: the adapter is imported on first use,
    # so that `import refkv` imports no zarr
    if name == "zarr":
        return importlib.import_module("refkv.zarr")
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
