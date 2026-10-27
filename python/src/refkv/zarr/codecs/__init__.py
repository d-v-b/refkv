"""Zarr v3 codecs needed to read zarrified files and stores, which neither zarr-python nor
imagecodecs-zarr (the `images` extra) provides: one module per codec, named for it, each of
which registers its codec with zarr when it is imported (a format that brings a codec brings
its module). Importing this package imports them all; a module whose name starts with `_`
holds what codecs share.

zarr also finds each one by name, without this package being imported first, through the
`zarr.codecs` entry points of python/pyproject.toml, which name every module here: a codec
added here is added there too (tests/test_codec_entry_points.py checks they agree). Loading
one imports `refkv.zarr`, whose import turns on zarr's `array.rectilinear_chunks`.
"""

from __future__ import annotations

import importlib
import pkgutil

for _module in pkgutil.iter_modules(__path__):
    if not _module.name.startswith("_"):
        importlib.import_module(f"{__name__}.{_module.name}")
