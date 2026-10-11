"""vzip does not import xarray: it calls the methods of the datasets it is given,
and xarray is a dependency only of the documentation's examples and the tests."""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "vzip"


def xarray_imports(source: str) -> list[int]:
    """The line numbers of the imports of xarray (or a submodule) in `source`, at
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
        else:
            continue
        if any(n == "xarray" or n.startswith("xarray.") for n in names):
            lines.append(node.lineno)
    return lines


def test_xarray_imports_are_found():
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
    assert {src: xarray_imports(src) for src in cases} == cases


def test_vzip_does_not_import_xarray():
    found = {
        str(path.relative_to(SRC)): lines
        for path in sorted(SRC.rglob("*.py"))
        if (lines := xarray_imports(path.read_text()))
    }
    assert found == {}, f"modules of vzip import xarray (file: lines): {found}"
