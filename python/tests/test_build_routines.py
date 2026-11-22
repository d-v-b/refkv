"""`refkv.build.routines`: each format's routines in a module of their own, which `refkv.build`
exports by the names the modules export, found by discovery rather than listed."""

import importlib
import pkgutil
from pathlib import Path

import pytest

import refkv.build
from refkv.build import routines
from refkv.build.routines import _discover


def test_exports() -> None:
    """`refkv.build` and `refkv.build.routines` export each name of each routine module's
    `__all__`, as that module's object, and the shared modules export nothing."""
    modules = [info.name for info in pkgutil.iter_modules(routines.__path__)]
    exported: dict[str, object] = {}
    for name in modules:
        module = importlib.import_module(f"refkv.build.routines.{name}")
        if name.startswith("_"):
            assert not getattr(module, "__all__", [])
            continue
        assert module.__all__
        exported.update({n: getattr(module, n) for n in module.__all__})
    assert sorted(exported) == routines.__all__
    assert set(exported) <= set(refkv.build.__all__)
    assert set(exported) <= set(dir(refkv.build))
    for name, value in exported.items():
        assert getattr(routines, name) is value
        assert getattr(refkv.build, name) is value


def test_unknown_name() -> None:
    """`refkv.build` has no name that neither it nor a routine module exports."""
    with pytest.raises(AttributeError, match="no_such_routine"):
        _ = refkv.build.no_such_routine


def test_twice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two routine modules that export one name are an ImportError, not one silently hiding the
    other."""
    package = tmp_path / "twice_routines"
    package.mkdir()
    (package / "__init__.py").write_text("")
    for name in ("a", "b"):
        (package / f"{name}.py").write_text('__all__ = ["same"]\nsame = 1\n')
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(ImportError, match=r"export same twice, the second time from twice_routines\.b"):
        _discover("twice_routines", [str(package)])
