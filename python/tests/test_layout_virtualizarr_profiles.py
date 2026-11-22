"""The layout `refkv.virtualizarr` finds its profiles by discovery: each a module of
`refkv.zarrify.layouts.virtualizarr` named for the profile, whose `write` presents its inputs."""

import importlib
import pkgutil
from pathlib import Path

import pytest

from refkv.build import read
from refkv.zarrify.layouts import virtualizarr
from refkv.zarrify.layouts.virtualizarr import WRITERS, VirtualiZarr

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def test_profiles() -> None:
    """The layout reads version 0 of each profile a module is named for, and gives each its
    module's `write`; a module whose name starts with `_` is no profile."""
    found = {info.name: importlib.import_module(f"{virtualizarr.__name__}.{info.name}")
             for info in pkgutil.iter_modules(virtualizarr.__path__) if not info.name.startswith("_")}
    assert found
    assert dict(VirtualiZarr.formats) == {name: frozenset({0}) for name in found}
    assert dict(WRITERS) == {name: m.write for name, m in found.items()}


def test_unpresented_profile() -> None:
    """An input of a profile no module is named for is a ValueError."""
    npy = next(p for p in sorted((FIXTURES / "npy").iterdir()) if p.suffix == ".npy")
    with read(str(npy)) as parsed, pytest.raises(ValueError, match=r"refkv\.virtualizarr presents no npy input"):
        VirtualiZarr().write([parsed], {"nonfinite": "token"})
