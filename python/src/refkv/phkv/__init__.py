"""The perfect-hash binding (spec/bindings/phkv.md
<https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md>): a reference store kept in
one file with a perfect hash of its keys, so that a lookup reads a few ranges of it, however many
keys it holds. A phkv store.

- `open(url)`: the phkv store at `url` (a local path or an `http(s)` URL), as a read-only store,
  a `PhkvStore` (`refkv.phkv.store`). `refkv.open(url, binding="phkv")` comes here.
- `PhkvWriter`: writes a standalone phkv store to a binary file (`refkv.phkv.writer`), as
  `ReferenceStore.save_phkv` does.

The container is read and written by the Rust core, which `refkv._core` binds. The store and the
writer are imported on first use, as `refkv.vzip`'s are.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Unpack

if TYPE_CHECKING:
    from refkv.phkv.store import PhkvOptions, PhkvStore
    from refkv.phkv.writer import PhkvWriter


def open(url: str, **kwargs: Unpack[PhkvOptions]) -> PhkvStore:  # noqa: A001 (phkv.open, the public name)
    """Open the phkv store at `url` (a local path or an `http(s)` URL) as a read-only store:
    `refkv.phkv.open("data.phkv").get("zarr.json")`.

    Keyword arguments go to `PhkvStore`: `policy`, `registry` (the stores to read sources with,
    such as an `obspec_utils` `ObjectStoreRegistry` or a mapping `{"s3://bucket": S3Store(...)}`),
    `stats`, and `max_concurrency` (8 by default; None for no cap)."""
    from refkv.phkv.store import PhkvStore  # noqa: PLC0415 (import refkv imports no phkv code)

    return PhkvStore(str(url), **kwargs)


def __getattr__(name: str) -> type[PhkvStore | PhkvWriter]:
    if name == "PhkvStore":
        from refkv.phkv.store import PhkvStore  # noqa: PLC0415 (import refkv imports no phkv code)

        return PhkvStore
    if name == "PhkvWriter":
        from refkv.phkv.writer import PhkvWriter  # noqa: PLC0415 (import refkv imports no phkv code)

        return PhkvWriter
    raise AttributeError(f"module 'refkv.phkv' has no attribute {name!r}")


__all__ = ["PhkvStore", "PhkvWriter", "open"]
