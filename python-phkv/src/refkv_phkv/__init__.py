"""refkv-phkv: the perfect-hash binding of refkv (spec/bindings/phkv.md
<https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md>), for Python. A phkv store keeps a
reference store in one file with a perfect hash of its keys, so that a lookup reads a few ranges
of it, however many keys it holds. It is a distribution of its own, beside `refkv` (which has
Part 1 and the ZIP binding, vzip), and it registers itself with `refkv` as the binding `"phkv"`.

- `open(url)`: the phkv store at `url` (a local path or an `http(s)` URL), as a read-only store,
  a `PhkvStore` (`refkv_phkv.store`). `refkv.open(url, binding="phkv")` comes here.
- `save(store, path)`: write a `refkv.ReferenceStore` as a phkv store.
- `PhkvWriter`: write a phkv store to a binary file, key by key (`refkv_phkv.writer`).

The container is read and written by the Rust crate `refkv-phkv`, which `refkv_phkv._core` binds.
The store and the writer are imported on first use.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Unpack

try:
    __version__ = version("refkv-phkv")
    """The installed distribution's version (git's, at build: python-phkv/tools/version.py)."""
except PackageNotFoundError:  # a source tree on sys.path, not installed
    __version__ = "0.0.0+unknown"

if TYPE_CHECKING:
    from refkv_phkv.store import PhkvOptions, PhkvStore
    from refkv_phkv.writer import PhkvWriter, save


def open(url: str, **kwargs: Unpack[PhkvOptions]) -> PhkvStore:  # noqa: A001 (refkv_phkv.open, the public name)
    """Open the phkv store at `url` (a local path or an `http(s)` URL) as a read-only store:
    `refkv_phkv.open("data.phkv").get("zarr.json")`.

    Keyword arguments go to `PhkvStore`: `policy`, `registry` (the stores to read sources with,
    such as an `obspec_utils` `ObjectStoreRegistry` or a mapping `{"s3://bucket": S3Store(...)}`),
    `stats`, and `max_concurrency` (8 by default; None for no cap)."""
    from refkv_phkv.store import PhkvStore  # noqa: PLC0415 (`import refkv_phkv` imports no reader)

    return PhkvStore(str(url), **kwargs)


def __getattr__(name: str) -> object:
    if name == "PhkvStore":
        from refkv_phkv.store import PhkvStore  # noqa: PLC0415 (`import refkv_phkv` imports no reader)

        return PhkvStore
    if name in ("PhkvWriter", "save"):
        from refkv_phkv import writer  # noqa: PLC0415 (`import refkv_phkv` imports no writer)

        return getattr(writer, name)
    raise AttributeError(f"module 'refkv_phkv' has no attribute {name!r}")


__all__ = ["PhkvStore", "PhkvWriter", "__version__", "open", "save"]
