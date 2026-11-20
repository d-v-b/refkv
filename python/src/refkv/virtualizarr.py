"""Reference stores as VirtualiZarr manifest stores, and a VirtualiZarr parser that zarrifies.

- `manifest_store(store, registry)`: the `virtualizarr.manifests.ManifestStore` that holds the
  hierarchy of a reference store (a `refkv.ReferenceStore`, or any store refkv opens, such as a
  `refkv.vzip.VZipStore`), its chunks read later through `registry`.
- `RefkvParser`: a VirtualiZarr parser, `RefkvParser()(url, registry)`, that zarrifies the file
  or store at `url` and converts the result, so that
  `virtualizarr.open_virtual_dataset(url, registry=registry, parser=RefkvParser())` opens any
  input refkv can zarrify.

A manifest entry is one byte range of one object, so the conversion maps each chunk as follows:

- a reference to one range of a url source becomes a manifest entry;
- a bytes value, and a reference whose ranges are all literal bytes, `data` sources or `key`
  sources, becomes an inlined entry;
- a reference made from several ranges, at least one of a url source (an HDF5 `vlen-utf8`
  chunk, a JPEG tile with its shared tables), is read now and inlined, which copies its bytes.

The ManifestStore reads its manifests' URLs through the registry without refkv's reader policy,
so each url source is checked against the store's policy here, and the pins and checksums a
store can record for its sources are not kept, as a manifest has nowhere to keep them.

This module lives in refkv, not VirtualiZarr, so that refkv's tests catch a change to refkv
that would break it. It needs the `virtualizarr` extra (`pip install 'refkv[virtualizarr]'`).
docs/virtual-datasets.md <https://github.com/d-v-b/refkv/blob/main/docs/virtual-datasets.md>
shows it in use.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import unquote

from refkv import _sync
from refkv._extras import missing
from refkv.pb import parts
from refkv.uri import file_path
from refkv.uri import resolve as resolve_uri
from refkv.zarrify import zarrify
from refkv.zarrify.common import SOURCE_NODE

try:
    import zarr
    import zarr.api.asynchronous
    from obspec_utils.registry import ObjectStoreRegistry
    from virtualizarr.manifests import ChunkManifest, ManifestArray, ManifestGroup, ManifestStore
    from virtualizarr.manifests.manifest import ChunkEntry
    from virtualizarr.parsers.zarr.common import metadata_as_v3
    from zarr.core.metadata import ArrayV2Metadata, ArrayV3Metadata
except ImportError as e:
    raise missing("virtualizarr", "VirtualiZarr manifest stores (refkv.virtualizarr)", e) from e

import refkv.zarr

if TYPE_CHECKING:
    from refkv.policy import Policy
    from refkv.reader import ReferenceReader
    from refkv.zarrify.layouts import Layout

__all__ = ["DEFAULT_LAYOUT", "RefkvParser", "manifest_store"]

DEFAULT_LAYOUT: Layout | None = None
"""The layout `RefkvParser` zarrifies with when given none: None, refkv's default layout."""

_METADATA_KEYS = frozenset({"zarr.json", ".zarray", ".zattrs", ".zgroup", ".zmetadata"})
"""The names of a node's metadata documents, in Zarr v3 and v2: what an array's keys hold
besides its chunks."""


class RefkvParser:
    """A VirtualiZarr parser (`__call__(url, registry) -> ManifestStore`) that zarrifies `url`
    with refkv, reading it through `registry`, and converts the result with `manifest_store`.

    - `group`: the group of the zarrified hierarchy to use as the ManifestStore's root, its
      path with `/` between names; None for the root.
    - `skip_variables`: the names of arrays to leave out, at every level of the hierarchy.
    - `policy`: the `refkv.Policy` refkv reads `url` under, and that the chunks' sources must
      satisfy; None for the policy `refkv.zarrify.zarrify` gives the store it builds.
    - `layout`: the `refkv.zarrify.layouts.Layout` to zarrify with; `DEFAULT_LAYOUT` when not
      given.

    refkv's mirror of the file's own structure (the `refkv_source` group, spec/conventions.md
    §2 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#2-attributes>) describes
    the file, not its data, and is left out.
    """

    def __init__(
        self,
        group: str | None = None,
        skip_variables: Iterable[str] | None = None,
        policy: Policy | None = None,
        layout: Layout | None = DEFAULT_LAYOUT,
    ) -> None:
        self.group = group
        self.skip_variables = skip_variables
        self.policy = policy
        self.layout = layout

    def __call__(self, url: str, registry: ObjectStoreRegistry[Any]) -> ManifestStore:
        """The ManifestStore of the file or store at `url`, read through `registry`."""
        # VirtualiZarr names a local input by its file: URL, which zarrify does not take yet; it
        # reads the same file by its path
        location = file_path(url) if url.startswith("file:") else url
        store = zarrify(location, policy=self.policy, registry=registry, layout=self.layout)
        result = manifest_store(store, registry, group=self.group, skip_variables=self.skip_variables)
        root = result._group  # noqa: SLF001 (VirtualiZarr has no public accessor)
        if SOURCE_NODE not in root.groups:
            return result
        return ManifestStore(
            group=ManifestGroup(
                arrays=root.arrays,
                groups={k: v for k, v in root.groups.items() if k != SOURCE_NODE},
                attributes=root.metadata.attributes,
            ),
            registry=registry,
        )


def manifest_store(
    store: ReferenceReader,
    registry: ObjectStoreRegistry[Any],
    *,
    group: str | None = None,
    skip_variables: Iterable[str] | None = None,
) -> ManifestStore:
    """The `virtualizarr.manifests.ManifestStore` holding the hierarchy of the reference store
    `store`, whose chunks it reads through `registry` (the module's docstring says how each
    chunk maps to a manifest entry).

    - `store`: a `refkv.ReferenceStore`, such as `refkv.zarrify.zarrify`'s result, or a store
      `refkv.open` opened.
    - `registry`: an `obspec_utils` `ObjectStoreRegistry` with a store for each url source.
    - `group`: the group of the store to use as the ManifestStore's root, its path with `/`
      between names; None for the root.
    - `skip_variables`: the names of arrays to leave out, at every level of the hierarchy.

    Raises `refkv.errors.ResolutionError` for a url source the store's policy refuses, and
    NotImplementedError for an array whose chunk grid is not regular.
    """
    return _sync.run(_manifest_store(store, registry, group, set(skip_variables or ())))


async def _manifest_store(
    store: ReferenceReader,
    registry: ObjectStoreRegistry[Any],
    group: str | None,
    skip: set[str],
) -> ManifestStore:
    urls: dict[int, str] = {}
    root = await _manifest_group(refkv.zarr.store(store), store, registry, group, skip, urls)
    return ManifestStore(group=root, registry=registry)


async def _manifest_group(
    zarr_store: refkv.zarr.ZarrStore,
    store: ReferenceReader,
    registry: ObjectStoreRegistry[Any],
    path: str | None,
    skip: set[str],
    urls: dict[int, str],
) -> ManifestGroup:
    """The ManifestGroup of the group at `path`, its subgroups included."""
    node = await zarr.api.asynchronous.open_group(store=zarr_store, path=path or "", mode="r")
    names = [name async for name in node.array_keys() if name not in skip]
    nodes = await asyncio.gather(*(node.getitem(name) for name in names))
    arrays = await asyncio.gather(
        *(_manifest_array(cast("zarr.AsyncArray[Any]", a), store, registry, urls) for a in nodes))
    children = [name async for name in node.group_keys()]
    groups = await asyncio.gather(
        *(_manifest_group(zarr_store, store, registry, f"{path}/{name}" if path else name, skip, urls)
          for name in children))
    return ManifestGroup(
        arrays=dict(zip(names, arrays, strict=True)),
        groups=dict(zip(children, groups, strict=True)),
        attributes=dict(node.attrs),
    )


async def _manifest_array(
    array: zarr.AsyncArray[Any],
    store: ReferenceReader,
    registry: ObjectStoreRegistry[Any],
    urls: dict[int, str],
) -> ManifestArray:
    """The ManifestArray of `array`: its metadata as Zarr v3, and an entry for each chunk the
    store holds. A chunk it does not hold is missing from the manifest too, so a reader gives
    the fill value for it."""
    metadata = metadata_as_v3(array.metadata)
    chunk_grid = metadata.chunk_grid
    chunk_shape = getattr(chunk_grid, "chunk_shape", None)
    if chunk_shape is None:
        raise NotImplementedError(
            f"{array.path}: a manifest has a regular chunk grid, not {type(chunk_grid).__name__}")
    grid = tuple(-(-n // c) for n, c in zip(metadata.shape, chunk_shape, strict=True))
    prefix = f"{array.path}/" if array.path else ""
    keys = [k async for k in store.list_async(prefix) if k[len(prefix):] not in _METADATA_KEYS]
    entries = await asyncio.gather(*(_entry(key, store, registry, urls) for key in keys))
    manifest = {_manifest_key(key[len(prefix):], array.metadata): e
                for key, e in zip(keys, entries, strict=True)}
    return ManifestArray(metadata=metadata, chunkmanifest=ChunkManifest(manifest, shape=grid))


def _manifest_key(chunk_key: str, metadata: ArrayV2Metadata | ArrayV3Metadata) -> str:
    """The manifest's key for `chunk_key`, a chunk's key relative to its array: its grid
    coordinates joined by `.`."""
    if not metadata.shape:
        return "c"
    if isinstance(metadata, ArrayV2Metadata):
        return ".".join(chunk_key.split(metadata.dimension_separator))
    # the array's own decoding, as a v3 array may use the "v2" chunk key encoding
    return ".".join(str(i) for i in metadata.chunk_key_encoding.decode_chunk_key(chunk_key))


async def _entry(
    key: str,
    store: ReferenceReader,
    registry: ObjectStoreRegistry[Any],
    urls: dict[int, str],
) -> ChunkEntry:
    """The manifest entry of the chunk at `key`."""
    value = await store.value_async(key)
    ranges = () if value is None or isinstance(value, bytes) else parts(value)
    sources = store.sources
    external = [r for r in ranges if r.data is None and sources[r.source].url is not None]
    if len(ranges) == 1 and external:
        r = external[0]
        return ChunkEntry.with_validation(
            path=_source_url(r.source, store, registry, urls), offset=r.offset, length=r.length)
    # bytes, a reference with nothing to read outside the store, or one made from several
    # ranges, which a manifest entry cannot hold: the store reads it, under its own policy
    data = await store.get_async(key)
    if data is None:  # listed a moment ago; a store that changes under the conversion
        raise KeyError(key)
    return ChunkEntry.with_validation(path="", offset=0, length=len(data), inlined_data=data)


def _source_url(index: int, store: ReferenceReader, registry: ObjectStoreRegistry[Any], urls: dict[int, str]) -> str:
    """The absolute URL of the url source `index`, once the store's policy has allowed it, as
    the registry takes it."""
    if index not in urls:
        url = resolve_uri(store.url, cast("str", store.sources[index].url))
        try:
            registry.resolve(url)
            registered = True
        except ValueError:
            registered = False
        # the ManifestStore reads the URL through the registry, without the policy: this is the
        # last place to apply it
        store.policy.check(url, registered=registered)
        if url.startswith("file:"):
            store.policy.check_file(url)
        # refkv's URLs are percent-encoded, and a registry takes an object's path as written
        urls[index] = unquote(url)
    return urls[index]
