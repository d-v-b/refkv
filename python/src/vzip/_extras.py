"""The optional parts of vzip, and the error that names the extra each one needs.

The base install reads and writes vzip archives and holds a `ReferenceStore`.
The extras add:

- `virtualizarr`: `ReferenceStore.from_virtualizarr` and `vzip.convert`, and
  what the netCDF workflow of docs/virtual-datasets.md reads files with;
- `images`: the JPEG, JPEG 2000 and JPEG XR codecs that archives of
  virtualized image files use (`vzip.codecs`).
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
