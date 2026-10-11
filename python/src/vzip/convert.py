"""VirtualiZarr virtual datasets as vzip stores.

`vzip.ReferenceStore.from_virtualizarr(vds)` is the entry point; this module does
the conversion, and keeps the older one-step function `write_vzip`.

vzip does not import xarray: it calls the methods of the dataset it is given.

The conversion literally builds the two key spaces separately and overlays
them: zarr metadata and any loadable (non-virtual) variables go into an
in-memory zarr store (the bytes key space); every manifest entry becomes a
reference (the reference key space).
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import TYPE_CHECKING

from vzip._extras import missing

if TYPE_CHECKING:
    import xarray as xr

try:
    import zarr
    from virtualizarr.manifests import ManifestArray
    from virtualizarr.writers.icechunk import extract_codecs, update_attributes
except ImportError as e:
    raise missing("virtualizarr", "Converting VirtualiZarr datasets (vzip.convert)", e) from e
from zarr.storage import MemoryStore  # noqa: E402

from vzip.refstore import Summary


def consolidate(store) -> None:
    """Write Zarr consolidated metadata into the root zarr.json, so readers
    that know it find every array there instead of listing the archive's
    every key to discover them. It is a zarr-python convention, not part of
    the Zarr v3 specification; readers that don't know it list as before."""
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Consolidated metadata is currently not part")
        zarr.consolidate_metadata(store, zarr_format=3)


def _write_loadable(vds: xr.Dataset, loadable: dict, store) -> None:
    """Write the loadable (non-virtual) variables of `vds` into the zarr `store`
    with xarray's CF encoding, through the class of `vds` itself, so that vzip
    never imports xarray.

    The variables go into a new dataset of their own, without the coordinate
    roles and attributes of `vds`: `split_virtual_dataset` writes those for
    every variable afterwards."""
    type(vds)(loadable).to_zarr(store, zarr_format=3, consolidated=False, mode="a")


def split_virtual_dataset(
    vds: xr.Dataset,
) -> tuple[dict[str, bytes], dict[str, tuple[str, int, int]]]:
    """Split a virtual dataset into (bytes key space, reference key space).

    References are (url, offset, length): virtualizarr only has single ranges.
    """
    mem = MemoryStore()
    virtual = {k: v for k, v in vds.variables.items() if isinstance(v.data, ManifestArray)}
    loadable = {k: v for k, v in vds.variables.items() if k not in virtual}
    if loadable:
        _write_loadable(vds, loadable, mem)
    group = zarr.open_group(mem, mode="a", zarr_format=3)

    refs: dict[str, tuple[str, int, int]] = {}
    inlined: dict[str, bytes] = {}
    for name, var in virtual.items():
        ma: ManifestArray = var.data
        md = ma.metadata
        filters, serializer, compressors = extract_codecs(md.inner_codecs)
        arr = group.require_array(
            name=name,
            shape=md.shape,
            chunks=md.chunks,
            shards=md.shards,
            dtype=md.data_type.to_native_dtype(),
            filters=filters,
            compressors=compressors,
            serializer=serializer,
            dimension_names=var.dims,
            fill_value=md.fill_value,
        )
        update_attributes(arr, var.attrs, encoding=var.encoding)
        for idx, entry in ma.manifest.iter_refs():
            key = f"{name}/{arr.metadata.encode_chunk_key(idx)}"
            refs[key] = (entry["path"], entry["offset"], entry["length"])
        for idx, data in ma.manifest._inlined.items():
            inlined[f"{name}/{arr.metadata.encode_chunk_key(idx)}"] = bytes(data)
    update_attributes(group, vds.attrs, coords=vds.coords)
    consolidate(mem)

    concrete = {k: v.to_bytes() for k, v in mem._store_dict.items()}
    concrete.update(inlined)
    return concrete, refs


def read_at_open(key: str, vds: xr.Dataset) -> bool:
    """Whether xarray reads `key` when it opens the dataset: the metadata
    documents, and the chunks of indexed coordinates such as `time`.

    Writers add these with `late=True`, so that they sit in the part of the
    archive a reader fetches when it opens it (spec §14.2
    <https://github.com/d-v-b/vzip/blob/main/spec/archive.md#142-recommended-layout-informative>) and
    cost no request. """
    return key.endswith("zarr.json") or key.split("/", 1)[0] in vds.xindexes


def write_vzip(
    vds: xr.Dataset,
    path: str | Path,
    *,
    mirror_refs: bool = True,
    relative_to: str | None = None,
    page_size: int | None = None,
) -> Summary:
    """Write a VirtualiZarr virtual dataset to a vzip archive at `path`, and return a
    `vzip.Summary` of it.

    The same as `vzip.ReferenceStore.from_virtualizarr(vds).save(path, ...)`, which
    is the preferred spelling: the store reads before it is saved, and takes
    `rewrite_urls` and the reader options.

    - `vds`: the virtual dataset, for example from `virtualizarr.open_virtual_dataset`
      or several of them concatenated with `xr.concat`.
    - `path`: the local file to write.
    - `mirror_refs`: also write each reference's payload as its entry's body, for
      ZIP tools that ignore extra fields (default True).
    - `relative_to`: a URL prefix; source URLs under it are written relative to
      the archive.
    - `page_size`: write a page index with pages of about this many bytes (for
      example 65536), so that readers load only the part of the central directory
      they need; None writes none. `ReferenceStore.save` says when it helps.

    Raises ValueError if the default reader policy would refuse a source, such as
    an `s3://` URL; `ReferenceStore.save` says how to fix it."""
    from vzip.refstore import ReferenceStore

    return ReferenceStore.from_virtualizarr(vds).save(
        path, mirror_refs=mirror_refs, relative_to=relative_to, page_size=page_size)


__all__ = ["consolidate", "read_at_open", "split_virtual_dataset", "write_vzip"]
