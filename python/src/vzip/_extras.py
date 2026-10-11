"""The optional parts of vzip, and the error that names the extra each one needs.

The base install reads and writes vzip archives, holds a `ReferenceStore`, and
virtualizes image files and stores, without Zarr. The extras add:

- `zarr`: zarr-python, for `vzip.zarr`, which opens a vzip store as a
  read-only zarr store (for zarr and xarray), and registers the codecs that
  archives of virtualized image files and stores use (`vzip.zarr.codecs`);
- `images`: the JPEG, JPEG 2000 and JPEG XR decoders of those codecs
  (imagecodecs), with the `zarr` extra;
- `virtualizarr`: VirtualiZarr, for `ReferenceStore.from_virtualizarr`,
  `vzip.convert` and `vzip.shards`, with the `zarr` extra, since they build
  Zarr metadata. vzip does not import xarray: it calls the methods of the
  datasets VirtualiZarr builds.
"""

from __future__ import annotations


def missing(extra: str, feature: str, error: ImportError) -> ImportError:
    """The ImportError for `feature` when an optional package of `extra` is missing."""
    name = error.name or str(error)
    e = ImportError(f"{feature} needs {name!r}, which vzip's {extra!r} extra installs: "
                    f"pip install 'vzip[{extra}] @ git+https://github.com/d-v-b/vzip', or "
                    f"from a clone, pip install '.[{extra}]'", name=error.name)
    e.__cause__ = error
    return e
