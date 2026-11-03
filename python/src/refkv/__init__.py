"""refkv: a reference value type for key-value stores.

A reference store maps keys to values that are either bytes or references to byte ranges
of other objects (spec/refkv.md, Part 1
<https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#part-1-reference-values>). Any
backend that can tell a bytes value from a reference value can hold one; a binding says
how. The ZIP binding keeps a store in one ZIP file, a vzip archive (Part 2
<https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#part-2-the-zip-binding>), which
`refkv.vzip` reads and writes.

- `ReferenceStore`: a store in memory. `ReferenceStore.from_virtualizarr(vds)` builds
  one from a VirtualiZarr virtual dataset, and `store.save_vzip(path)` writes it as a
  vzip archive.
- `open(url)`: a saved store, as a store that reads the same values: with the ZIP
  binding (the default), a `refkv.vzip.VZipStore`. Other bindings are distributions of their own,
  found through the entry-point group `refkv.bindings`: `refkv-phkv` (`import refkv_phkv`) reads
  and writes the perfect-hash binding, and `refkv.open(url, binding="phkv")` then opens it.
- Both have the operations of spec/refkv.md §6.2
  <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#62-operations>: `get(key)`,
  or `get(key, RangeRequest(start, end))` (also `OffsetRequest`, `SuffixRequest`),
  `classify`, `raw`, `list`, each also a coroutine (`get_async`, ...).
- `refkv.compose`: stores combined into new ones by their bounds (`refkv.bounds`), the
  keys each answers for: `overlay`, `union`, `mount`, `shift_chunks`.
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
from importlib.metadata import PackageNotFoundError, entry_points, version
from types import ModuleType
from typing import TYPE_CHECKING, Any, Literal, TypedDict, Unpack, overload

from refkv import vzip
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

BINDINGS = ("vzip",)
"""The bindings `refkv` itself reads stores with: the ZIP binding, `"vzip"`. Other distributions
add theirs through the entry-point group `refkv.bindings` (`bindings()` lists every one that is
installed)."""

BINDINGS_GROUP = "refkv.bindings"
"""The entry-point group in which a distribution registers a binding: the name is the binding's,
the value a callable `open(url, **kwargs)` that returns a read-only store, as `refkv.vzip.open`
does."""

_PACKAGES = {"phkv": "refkv-phkv"}
"""The distribution of each binding that is not refkv's own, for the error that names it."""


def bindings() -> tuple[str, ...]:
    """The bindings `open` can read a store with: `refkv`'s own, `"vzip"`, and those of the
    installed distributions that register one in the entry-point group `refkv.bindings` (such as
    `"phkv"`, from `refkv-phkv`)."""
    found = {ep.name for ep in entry_points(group=BINDINGS_GROUP)}
    return (*BINDINGS, *sorted(found - set(BINDINGS)))


@overload
def open(url: str, *, binding: Literal["vzip"] = "vzip", **kwargs: Unpack[VZipOptions]) -> VZipStore: ...  # noqa: A001 (refkv.open, the public name)
@overload
def open(url: str, *, binding: str, **kwargs: Any) -> Any: ...  # noqa: A001, ANN401 (a binding another distribution provides: its own store type)
def open(url: str, *, binding: str = "vzip", **kwargs: Any) -> Any:  # noqa: A001 (refkv.open, the public name)
    """Open the store saved at `url` (a local path or an `http(s)` URL, presigned or with
    a query string, of any name) as a read-only store, with the binding `binding`. The
    ZIP binding, `"vzip"` (the default), opens a vzip archive as a `refkv.vzip.VZipStore`:
    `refkv.open("data.vzip").get("zarr.json")`. A binding another distribution provides, such
    as `"phkv"` (`pip install refkv-phkv`), opens its own kind of store the same way:
    `refkv.open("data.phkv", binding="phkv")`. For zarr and xarray, `refkv.zarr.open(url)`
    takes the same arguments and wraps the store.

    Other keyword arguments go to the binding's store: `policy`, `registry` (the stores
    to read sources with, such as an `obspec_utils` `ObjectStoreRegistry` or a mapping
    `{"s3://bucket": S3Store(...)}`), `stats`, `max_concurrency` (8 by default; None for
    no cap), and, for the ZIP binding, `resolve`. A binding that is not in `bindings()` is a
    ValueError, which names the distribution to install if it is a known one."""
    if binding == "vzip":
        return vzip.open(str(url), **kwargs)
    for ep in entry_points(group=BINDINGS_GROUP, name=binding):
        return ep.load()(str(url), **kwargs)
    hint = f" (pip install {_PACKAGES[binding]})" if binding in _PACKAGES else ""
    raise ValueError(f"no binding {binding!r}{hint}: refkv reads stores with {', '.join(map(repr, bindings()))}")


def __getattr__(name: str) -> ModuleType:
    # `import refkv` then `refkv.zarr.open(...)`: the adapter is imported on first use,
    # so that `import refkv` imports no zarr
    if name == "zarr":
        return importlib.import_module("refkv.zarr")
    raise AttributeError(f"module 'refkv' has no attribute {name!r}")


__all__ = [
    "BINDINGS_GROUP",
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
    "bindings",
    "open",
    "public_s3_url",
]
