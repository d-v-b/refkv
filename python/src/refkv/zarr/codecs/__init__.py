"""Zarr v3 codecs needed to read zarrified files and stores, which zarr-python does not
provide: one module per codec, named for it, each of which registers its codec with zarr when
it is imported (a format that brings a codec brings its module). Importing this package
imports them all; a module whose name starts with `_` holds what codecs share.
"""

from __future__ import annotations

import importlib
import pkgutil

for _module in pkgutil.iter_modules(__path__):
    if not _module.name.startswith("_"):
        importlib.import_module(f"{__name__}.{_module.name}")
