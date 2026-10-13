"""What refkv imports.

- refkv does not import xarray: it calls the methods of the datasets it is given,
  and xarray is a dependency only of the documentation's examples and the tests.
- Only the zarr adapter (`refkv.zarr`) and the VirtualiZarr conversion
  (`refkv.convert`, `refkv.shards`) import zarr, which the base install does not
  have: the other modules neither import it nor import the adapter."""

import ast
import shutil
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "refkv"

ZARR_MODULES = {"zarr/__init__.py", "zarr/codecs.py", "convert.py", "shards.py"}
"""The modules that may import zarr, relative to the package."""


def imports_of(module: str, source: str) -> list[int]:
    """The line numbers of the imports of `module` (or a submodule) in `source`, at
    module level or in a function, other than those under `if TYPE_CHECKING:`,
    which never run."""
    tree = ast.parse(source)
    skipped = {
        id(node)
        for block in ast.walk(tree)
        if isinstance(block, ast.If) and ast.unparse(block.test) in ("TYPE_CHECKING", "typing.TYPE_CHECKING")
        for stmt in block.body
        for node in ast.walk(stmt)
    }
    lines = []
    for node in ast.walk(tree):
        if id(node) in skipped:
            continue
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names = [node.module or ""]
            if node.module == module.rpartition(".")[0]:  # from refkv import zarr
                names += [f"{node.module}.{a.name}" for a in node.names]
        else:
            continue
        if any(n == module or n.startswith(module + ".") for n in names):
            lines.append(node.lineno)
    return lines


def importers(src: Path, module: str, allowed: set[str] = frozenset()) -> dict[str, list[int]]:
    """The modules of the package at `src`, other than `allowed`, that import
    `module`, with the lines that do."""
    return {
        rel: lines
        for path in sorted(src.rglob("*.py"))
        if (rel := path.relative_to(src).as_posix()) not in allowed
        and (lines := imports_of(module, path.read_text()))
    }


def zarr_importers(src: Path) -> dict[str, list[int]]:
    """The modules outside `ZARR_MODULES` that import zarr or the adapter."""
    found = importers(src, "zarr", ZARR_MODULES)
    for rel, lines in importers(src, "refkv.zarr", ZARR_MODULES).items():
        found[rel] = sorted(set(found.get(rel, [])) | set(lines))
    return found


def test_imports_are_found():
    cases = {
        "import xarray as xr\n": [1],
        "import os, xarray\n": [1],
        "from xarray import Dataset\n": [1],
        "from xarray.core import dataset\n": [1],
        "import xarray.testing\n": [1],
        "def f():\n    import xarray as xr\n": [2],
        "try:\n    import xarray\nexcept ImportError:\n    pass\n": [2],
        "if TYPE_CHECKING:\n    import xarray as xr\nelse:\n    import xarray\n": [4],
        "if typing.TYPE_CHECKING:\n    from xarray import Dataset\n": [],
        "import xarray_extras\nfrom . import xarray\nx = 'import xarray'\n": [],
    }
    assert {src: imports_of("xarray", src) for src in cases} == cases
    adapter = {
        "import refkv.zarr\n": [1],
        "from refkv.zarr import store\n": [1],
        "from refkv import zarr\n": [1],
        "from refkv import store, zarr\n": [1],
        "from refkv import store\nimport refkv.zarrish\n": [],
    }
    assert {src: imports_of("refkv.zarr", src) for src in adapter} == adapter


def test_refkv_does_not_import_xarray():
    found = importers(SRC, "xarray")
    assert found == {}, f"modules of refkv import xarray (file: lines): {found}"


def test_only_the_adapter_imports_zarr():
    found = zarr_importers(SRC)
    assert found == {}, (f"modules of refkv outside {sorted(ZARR_MODULES)} import zarr or "
                         f"refkv.zarr (file: lines): {found}")


def test_the_zarr_guard_finds_a_planted_import(tmp_path):
    """The guard, on a copy of the package with imports of zarr planted in a core
    module (in a function), in a new module, and of the adapter in the CLI."""
    pkg = tmp_path / "refkv"
    shutil.copytree(SRC, pkg, ignore=shutil.ignore_patterns("__pycache__"))
    assert zarr_importers(pkg) == {}
    store = pkg / "store.py"
    n = store.read_text().count("\n")
    store.write_text(store.read_text() + "\n\ndef planted():\n    from zarr.abc.store import Store\n")
    (pkg / "virtualize" / "planted.py").write_text("import zarr.storage\n")
    cli = pkg / "cli.py"
    cli.write_text("import refkv.zarr\n" + cli.read_text())
    assert zarr_importers(pkg) == {"cli.py": [1], "store.py": [n + 4], "virtualize/planted.py": [1]}


def test_importing_the_core_imports_no_zarr():
    """Every module but the zarr ones, imported in a fresh interpreter, leaves zarr
    unimported."""
    names = sorted(
        ".".join(("refkv", *path.relative_to(SRC).with_suffix("").parts)).removesuffix(".__init__")
        for path in SRC.rglob("*.py")
        if path.relative_to(SRC).as_posix() not in ZARR_MODULES and path.name != "__main__.py")
    code = f"""
import importlib, sys
for name in {names!r}:
    importlib.import_module(name)
loaded = sorted(n for n in sys.modules if n == "zarr" or n.startswith(("zarr.", "refkv.zarr")))
print(len([n for n in sys.modules if n.startswith("refkv.")]), loaded)
"""
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    count, loaded = out.stdout.split(" ", 1)
    assert int(count) > 15 and loaded.strip() == "[]", out.stdout
