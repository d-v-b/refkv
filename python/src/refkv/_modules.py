"""The modules of a package, found by discovery: how a package of one module per format (the
routines of `refkv.build.routines`, the writers of a layout) finds its formats without a list of
them."""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterable
from types import ModuleType

__all__ = ["submodules"]


def submodules(package: str, path: Iterable[str]) -> dict[str, ModuleType]:
    """The modules of `package` (its `__name__`, at its `__path__`), imported, by name in ascending
    order, but those whose name starts with `_`, which hold what the others share."""
    return {info.name: importlib.import_module(f"{package}.{info.name}")
            for info in sorted(pkgutil.iter_modules(path), key=lambda info: info.name)
            if not info.name.startswith("_")}
