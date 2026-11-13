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
  one from a VirtualiZarr virtual dataset, and `store.save_vzip(path)` writes it as a
  vzip archive.
- `open(url)`: a saved store, as a store that reads the same values: with the ZIP
  binding (the default), a `refkv.vzip.VZipStore`, and with the perfect-hash binding
  (`binding="phkv"`, `store.save_phkv(path)` writes one), a `refkv.phkv.PhkvStore`.
- `vzip_to_phkv(src, dst)` and `phkv_to_vzip(src, dst)` convert a saved store from one binding
  to the other.
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

import dataclasses
import importlib
import os
from importlib.metadata import PackageNotFoundError, version
from types import EllipsisType, ModuleType
from typing import TYPE_CHECKING, Any, Literal, TypedDict, Unpack, overload
from urllib.parse import urlsplit

from refkv import phkv, vzip
from refkv.pb import Concat, Range, Source
from refkv.policy import Policy, public_s3_url
from refkv.reader import OffsetRequest, RangeRequest, Stats, SuffixRequest
from refkv.refstore import ReferenceStore, Summary
from refkv.uri import file_uri
from refkv.uri import resolve as resolve_uri

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


def _binding_of(url: str) -> str:
    """The binding a name suggests: `"phkv"` for a path that ends in `.phkv` (a query string or
    fragment aside), `"vzip"` otherwise."""
    return "phkv" if urlsplit(url).path.lower().endswith(".phkv") else "vzip"


@overload
def open(url: str, *, binding: Literal["vzip"], **kwargs: Unpack[VZipOptions]) -> VZipStore: ...  # noqa: A001 (refkv.open, the public name)
@overload
def open(url: str, *, binding: Literal["phkv"], **kwargs: Unpack[PhkvOptions]) -> PhkvStore: ...  # noqa: A001
@overload
def open(url: str, *, binding: str | None = None, **kwargs: Unpack[VZipOptions]) -> VZipStore | PhkvStore: ...  # noqa: A001
def open(url: str, *, binding: str | None = None, **kwargs: Any) -> VZipStore | PhkvStore:  # noqa: A001 (refkv.open, the public name)
    """Open the store saved at `url` (a local path or an `http(s)` URL, presigned or with
    a query string) as a read-only store, with the binding `binding`. Without one, a name that
    ends in `.phkv` is opened with `"phkv"` and any other with `"vzip"`; nothing is read to decide.
    The ZIP binding, `"vzip"`, opens a vzip archive as a `refkv.vzip.VZipStore`:
    `refkv.open("data.vzip").get("zarr.json")`. The perfect-hash binding, `"phkv"`, opens a phkv
    store as a `refkv.phkv.PhkvStore` (it has no `resolve` option). For zarr and
    xarray, `refkv.zarr.open(url)` takes the same arguments and wraps the store.

    Other keyword arguments go to the binding's store: `policy`, `registry` (the stores
    to read sources with, such as an `obspec_utils` `ObjectStoreRegistry` or a mapping
    `{"s3://bucket": S3Store(...)}`), `stats`, `max_concurrency` (8 by default; None for
    no cap), and `resolve`. A binding not in `BINDINGS` is a ValueError."""
    if binding is None:
        binding = _binding_of(str(url))
    if binding == "vzip":
        return vzip.open(str(url), **kwargs)
    if binding == "phkv":
        return phkv.open(str(url), **kwargs)
    raise ValueError(f"no binding {binding!r}: refkv reads stores with {', '.join(map(repr, BINDINGS))}")


def _convert(
    src: str | os.PathLike[str],
    dst: str | os.PathLike[str],
    binding: str,
    registry: Registry | None,
    policy: Policy | None,
    relative_to: str | None,
) -> ReferenceStore:
    """The store `src` of `binding`, read under `policy` and `registry`, as a `ReferenceStore`. Unless
    `relative_to` is given, each source URL that was relative in `src` and resolves to the same
    place from `dst` is relative again."""
    options: dict[str, Any] = {"policy": policy or Policy()}
    if registry is not None:
        options["registry"] = registry
    store = (vzip.open if binding == "vzip" else phkv.open)(os.fspath(src), **options)
    copy = ReferenceStore.from_store(store)
    if relative_to is None:
        base = file_uri(os.fspath(dst))
        sources = [dataclasses.replace(c, url=o.url)
                   if o.url is not None and c.url is not None and resolve_uri(base, o.url) == c.url else c
                   for o, c in zip(store.sources, copy.sources, strict=True)]
        copy = ReferenceStore(copy.mapping, sources, url=copy.url)
    return copy


def vzip_to_phkv(
    src: str | os.PathLike[str],
    dst: str | os.PathLike[str],
    *,
    inline_max: int = 4096,
    page_size: int = 4096,
    relative_to: str | None = None,
    policy: Policy | None = None,
    registry: Registry | None = None,
) -> Summary:
    """Convert the vzip archive `src` (a local path or an `http(s)` URL) to a phkv store at the
    local path `dst`, and return the `Summary` of the store written. The keys, bytes values and
    references are the same, and no reference is resolved: the sources are not read. The options
    are `ReferenceStore.save_phkv`'s; what the copy leaves behind is listed in
    `ReferenceStore.from_store`, which reads the whole directory of the archive into memory.

    A source URL that is relative in `src` stays relative if it resolves to the same place from
    `dst`, else it is written absolute; `relative_to` instead takes that prefix off every URL
    under it. `policy` and `registry` are the reader's, as for `open`, and `policy` also checks
    the sources, as for `save_phkv`. `dst` is written only once it is complete, and replaces a
    file there."""
    store = _convert(src, dst, "vzip", registry, policy, relative_to)
    return store.save_phkv(dst, inline_max=inline_max, page_size=page_size, relative_to=relative_to,
                           policy=policy)


def phkv_to_vzip(
    src: str | os.PathLike[str],
    dst: str | os.PathLike[str],
    *,
    page_size: int | EllipsisType | None = ...,
    mirror_refs: bool = True,
    relative_to: str | None = None,
    policy: Policy | None = None,
    registry: Registry | None = None,
) -> Summary:
    """Convert the phkv store `src` (a local path or an `http(s)` URL) to a vzip archive at the
    local path `dst`, and return the `Summary` of the archive written. The keys, bytes values and
    references are the same, and no reference is resolved: the sources are not read. The options
    are `ReferenceStore.save_vzip`'s, whose `page_size` default writes no page index: a store of
    many keys may want `page_size=65536`. What the copy leaves behind is listed in
    `ReferenceStore.from_store`, which reads the whole store into memory. Relative source URLs,
    `policy`, `registry` and `dst` are as for `vzip_to_phkv`.

    A vzip archive reserves the names under `__vz__/`, which a phkv store does not, so a key
    there is converted only if a `key` source names it, and then as a hidden key of the archive.
    Any other is a ValueError naming it, and nothing is written. A phkv store's properties
    (`mtime`, `mode`, `media_type`) and metadata have no place in an archive and are dropped."""
    store = _convert(src, dst, "phkv", registry, policy, relative_to)
    named = {s.key for s in store.sources if s.key is not None}
    stray = next((k for k in store.mapping if k.startswith("__vz__/") and k not in named), None)
    if stray is not None:
        raise ValueError(f"cannot convert {stray!r} to vzip: an archive reserves the names under '__vz__/' "
                         "(only a key that a source names is kept, as a hidden key)")
    return store.save_vzip(dst, page_size=page_size, mirror_refs=mirror_refs, relative_to=relative_to,
                           policy=policy)


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
    "phkv_to_vzip",
    "public_s3_url",
    "vzip_to_phkv",
]
