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
  chunk, a JPEG tile with its shared tables), is read now and inlined, which copies its bytes,
  as is a range of an object whose name holds a `#` or `?`, which a manifest path cannot name.

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
from refkv.build import NoIrError, apply, project, read, seal
from refkv.errors import ResolutionError
from refkv.pb import parts
from refkv.uri import resolve as resolve_uri
from refkv.zarrify import Rejected, zarrify
from refkv.zarrify.common import SOURCE_NODE
from refkv.zarrify.layouts import Layout, LayoutRejected, plan

try:
    import zarr
    import zarr.api.asynchronous
    from obspec_utils.registry import ObjectStoreRegistry
    from virtualizarr.manifests import ChunkManifest, ManifestArray, ManifestGroup, ManifestStore
    from virtualizarr.manifests.manifest import ChunkEntry
    from zarr.core.metadata import ArrayV2Metadata, ArrayV3Metadata
except ImportError as e:
    raise missing("virtualizarr", "VirtualiZarr manifest stores (refkv.virtualizarr)", e) from e

import refkv.zarr

if TYPE_CHECKING:
    from zarr.abc.numcodec import Numcodec

    from refkv.policy import Policy
    from refkv.reader import ReferenceReader

__all__ = ["DEFAULT_LAYOUT", "RefkvParser", "manifest_store"]

DEFAULT_LAYOUT: Layout | None = Layout("refkv.virtualizarr")
"""The layout `RefkvParser` zarrifies with when given none: `refkv.virtualizarr`
(`refkv.zarrify.layouts.virtualizarr`), which presents a file as VirtualiZarr's own parsers
present it."""

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
      given, None for refkv's default layout. An input of a format the layout does not read, or
      one the layout refuses (a FITS image with `BSCALE = 0`), is written as refkv's own output
      instead, so that the parser takes any input refkv can zarrify. Either way the input is
      read once: both are projections of the one parse.

    VirtualiZarr names an ENVI raster by its data file, refkv by its `.hdr` header: an input no
    profile accepts is zarrified by the header beside it, found as `ENVIParser` finds it (the
    input's name with `.hdr` added, then with its suffix replaced by `.hdr`), where there is one.

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
        try:
            store = self._zarrify(url, registry)
        except LayoutRejected:
            raise
        except Rejected:
            header = _envi_header(url, registry)
            if header is None:
                raise
            # the data file is the one at url: the profile probes others beside the header, and
            # the scope keeps it from opening them
            store = self._zarrify(header, registry, scope=url)
        # the mirror is left out of the traversal, not converted and then dropped
        mirror = frozenset() if self.group else frozenset({SOURCE_NODE})
        return _sync.run(_manifest_store(store, registry, self.group, set(self.skip_variables or ()), mirror))

    def _zarrify(self, url: str, registry: ObjectStoreRegistry[Any], scope: str | None = None) -> ReferenceReader:
        """`url` zarrified: read once, then written by the parser's layout where it reads the
        input's profile and takes the input, and as refkv's own output of the same reading where
        it does not. `scope` is `zarrify`'s."""
        if self.layout is None:
            return zarrify(url, policy=self.policy, registry=registry, scope=scope)
        planned = plan(self.layout)
        try:
            parsed = read(url, policy=self.policy, registry=registry, scope=scope)
        except NoIrError:  # a store of DICOM files, which takes no layout: refkv's own output
            return zarrify(url, policy=self.policy, registry=registry, scope=scope)
        with parsed:
            info = {"format": parsed.profile, "elements": len(parsed.ir)}
            if planned.reads(parsed.profile, parsed.version):
                try:
                    return apply(planned, [parsed], info=info)
                except LayoutRejected:  # an input the layout reads the profile of, and refuses
                    pass
            return seal(project(parsed).written, [parsed], info=info)


def _envi_header(url: str, registry: ObjectStoreRegistry[Any]) -> str | None:
    """The URL of the ENVI header beside the data file at `url`, as `ENVIParser` finds it: `url`
    with `.hdr` added, else with its suffix replaced by `.hdr`; None where neither can be read
    (a probe that fails for any reason, such as S3's 403 for a missing key without list rights,
    is no header, as for `ENVIParser`) or the registry holds no store for `url`."""
    head, _, name = url.rpartition("/")
    stem = name.rpartition(".")[0]
    candidates = [f"{url}.hdr"] + ([f"{head}/{stem}.hdr"] if stem else [])
    for candidate in candidates:
        try:
            store, path = registry.resolve(candidate)
        except ValueError:
            return None
        try:
            store.head(path)
        except Exception:  # noqa: BLE001, S112 (any failed probe is no header; the caller's Rejected stands)
            continue
        return candidate
    return None


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
    return _sync.run(_manifest_store(store, registry, group, set(skip_variables or ()), frozenset()))


async def _manifest_store(
    store: ReferenceReader,
    registry: ObjectStoreRegistry[Any],
    group: str | None,
    skip: set[str],
    skip_groups: frozenset[str],
) -> ManifestStore:
    """`manifest_store`, leaving out the groups `skip_groups` of the root."""
    urls: dict[int, str | None] = {}
    root = await _manifest_group(refkv.zarr.store(store), store, registry, group, skip, urls, skip_groups)
    return ManifestStore(group=root, registry=registry)


async def _manifest_group(
    zarr_store: refkv.zarr.ZarrStore,
    store: ReferenceReader,
    registry: ObjectStoreRegistry[Any],
    path: str | None,
    skip: set[str],
    urls: dict[int, str | None],
    skip_groups: frozenset[str] = frozenset(),
) -> ManifestGroup:
    """The ManifestGroup of the group at `path`, its subgroups but `skip_groups` included."""
    node = await zarr.api.asynchronous.open_group(store=zarr_store, path=path or "", mode="r")
    names = [name async for name in node.array_keys() if name not in skip]
    nodes = await asyncio.gather(*(node.getitem(name) for name in names))
    arrays = await asyncio.gather(
        *(_manifest_array(cast("zarr.AsyncArray[Any]", a), store, registry, urls) for a in nodes))
    children = [name async for name in node.group_keys() if name not in skip_groups]
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
    urls: dict[int, str | None],
) -> ManifestArray:
    """The ManifestArray of `array`: its metadata as Zarr v3, and an entry for each chunk the
    store holds. A chunk it does not hold is missing from the manifest too, so a reader gives
    the fill value for it."""
    metadata = _metadata_v3(array.metadata)
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


def _metadata_v3(metadata: ArrayV2Metadata | ArrayV3Metadata) -> ArrayV3Metadata:
    """`metadata` as Zarr v3, its chunk key encoding `default` with `.` between coordinates,
    the keys a ManifestStore gives its chunks."""
    document = metadata.to_dict() if isinstance(metadata, ArrayV3Metadata) else _v2_as_v3(metadata)
    document["chunk_key_encoding"] = {"name": "default", "configuration": {"separator": "."}}
    return ArrayV3Metadata.from_dict(document)


def _v2_as_v3(metadata: ArrayV2Metadata) -> dict[str, Any]:
    """The Zarr v3 document of the Zarr v2 array `metadata`, as zarr-python's `migrate_v2_to_v3`
    writes it, and as VirtualiZarr's ZarrParser presents it: a missing fill value is the data
    type's default, and the `_ARRAY_DIMENSIONS` attribute is the array's dimension names."""
    attributes = dict(metadata.attributes)
    names = attributes.pop("_ARRAY_DIMENSIONS", None)
    codecs: list[dict[str, Any]] = []
    if metadata.order == "F" and len(metadata.shape) > 1:
        codecs.append({"name": "transpose", "configuration": {"order": list(range(len(metadata.shape)))[::-1]}})
    codecs.extend(_numcodecs(f) for f in metadata.filters or ())
    endian = getattr(metadata.dtype, "endianness", None)
    codecs.append({"name": "bytes", "configuration": {"endian": endian}} if endian else {"name": "bytes"})
    dtype = metadata.dtype
    if metadata.compressor is not None:
        codecs.append(_compressor(metadata.compressor, dtype.to_native_dtype().itemsize))
    fill = dtype.default_scalar() if metadata.fill_value is None else metadata.fill_value
    return {
        "zarr_format": 3,
        "node_type": "array",
        "shape": list(metadata.shape),
        "data_type": dtype.to_json(zarr_format=3),
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": list(metadata.chunks)}},
        "chunk_key_encoding": {"name": "default"},
        "fill_value": dtype.to_json_scalar(fill, zarr_format=3),
        "codecs": codecs,
        "attributes": attributes,
        "dimension_names": names,
    }


_BLOSC_SHUFFLES = {0: "noshuffle", 1: "shuffle", 2: "bitshuffle"}
"""Zarr v3's names for numcodecs' Blosc shuffles."""


def _compressor(codec: Numcodec, item_size: int) -> dict[str, Any]:
    """The Zarr v3 codec of the Zarr v2 compressor `codec`: Zarr v3's own `gzip`, `zstd` or
    `blosc`, as zarr-python's `migrate_v2_to_v3` writes them, else its numcodecs codec."""
    config = codec.get_config()
    if codec.codec_id == "gzip":
        return {"name": "gzip", "configuration": {"level": config["level"]}}
    if codec.codec_id == "zstd":
        return {"name": "zstd", "configuration": {"level": config["level"], "checksum": config.get("checksum", False)}}
    if codec.codec_id == "blosc" and config["shuffle"] in _BLOSC_SHUFFLES:
        return {"name": "blosc", "configuration": {
            "typesize": item_size, "cname": config["cname"], "clevel": config["clevel"],
            "shuffle": _BLOSC_SHUFFLES[config["shuffle"]], "blocksize": config["blocksize"]}}
    return _numcodecs(codec)


def _numcodecs(codec: Numcodec) -> dict[str, Any]:
    """The Zarr v3 codec of the numcodecs codec `codec`, as zarr-python names it."""
    config = {k: v for k, v in codec.get_config().items() if k != "id"}
    return {"name": f"numcodecs.{codec.codec_id}", "configuration": config}


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
    urls: dict[int, str | None],
) -> ChunkEntry:
    """The manifest entry of the chunk at `key`."""
    value = await store.value_async(key)
    ranges = () if value is None or isinstance(value, bytes) else parts(value)
    sources = store.sources
    external = [r for r in ranges if r.data is None and sources[r.source].url is not None]
    if len(ranges) == 1 and external:
        r = external[0]
        path = _source_url(r.source, store, registry, urls)
        if path is not None:
            return ChunkEntry.with_validation(path=path, offset=r.offset, length=r.length)
    # bytes, a reference with nothing to read outside the store, one made from several ranges,
    # or one of an object a manifest path cannot name: the store reads it, under its own policy
    data = await store.get_async(key)
    if data is None:  # listed a moment ago; a store that changes under the conversion
        raise KeyError(key)
    return ChunkEntry.with_validation(path="", offset=0, length=len(data), inlined_data=data)


def _source_url(index: int, store: ReferenceReader, registry: ObjectStoreRegistry[Any],
                urls: dict[int, str | None]) -> str | None:
    """The absolute URL of the url source `index`, once the store's policy has allowed it, as
    the registry takes it; None for an object whose name has a `#` or `?`, which a manifest path
    cannot hold (the ManifestStore cuts its paths there), so that its chunks are inlined."""
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
            try:
                store.policy.check_file(url)
            # a file: URL that breaks the rules of spec §5
            # <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#5-source-table>, as the reader
            # reports it
            except ValueError as e:
                raise ResolutionError(f"{url}: {e}") from None
        # refkv's URLs are percent-encoded, and a registry takes an object's path as written
        path = unquote(url)
        urls[index] = None if "#" in path or "?" in path else path
    return urls[index]
