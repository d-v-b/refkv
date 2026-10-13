"""The ZIP binding (Part 2 of the specification
<https://github.com/d-v-b/refkv/blob/main/spec/archive.md#part-2-the-zip-binding>): a
reference store kept in one ZIP file, a vzip archive.

- `open(url)`: the vzip archive at `url` (a local path or an `http(s)` URL), as a
  read-only store, a `VZipStore` (`refkv.vzip.store`). `refkv.open(url)` comes here
  with its default binding.
- `VZipWriter`: writes a vzip archive to a binary file (`refkv.vzip.archive`), as
  `ReferenceStore.save` does.
- `refkv.vzip.names`: the names the binding reserves, under `__vz__/`.

`python -m refkv.vzip.cli` is the archive conformance harness's command line
(conformance/vzip/HARNESS.md).

The store and the writer are imported on first use: `import refkv`, whose
`ReferenceStore` keeps the reserved names out of its mapping, imports only
`refkv.vzip.names`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from refkv.vzip.archive import VZipWriter
    from refkv.vzip.store import VZipStore


def open(url: str, **kwargs) -> VZipStore:  # noqa: A001 - like gzip.open, zarr.open
    """Open the vzip archive at `url` (a local path or an `http(s)` URL) as a read-only
    store: `refkv.vzip.open("data.vzip").get("zarr.json")`.

    Keyword arguments go to `VZipStore`: `policy`, `registry` (the stores to read
    sources with, such as an `obspec_utils` `ObjectStoreRegistry` or a mapping
    `{"s3://bucket": S3Store(...)}`), `stats`, `max_concurrency` (8 by default; None for
    no cap), and `resolve`."""
    from refkv.vzip.store import VZipStore

    return VZipStore(str(url), **kwargs)


def __getattr__(name: str):
    if name == "VZipStore":
        from refkv.vzip.store import VZipStore

        return VZipStore
    if name == "VZipWriter":
        from refkv.vzip.archive import VZipWriter

        return VZipWriter
    raise AttributeError(f"module 'refkv.vzip' has no attribute {name!r}")


__all__ = ["VZipStore", "VZipWriter", "open"]
