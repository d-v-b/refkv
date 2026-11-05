"""The table of codec names in spec/conventions.md §3: it names every codec the Rust core writes, and
refkv's `zarr.codecs` entry points are its codecs of the package refkv."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def codec_names() -> dict[str, str]:
    """Each codec of the table, and its Python package."""
    text = (ROOT / "spec" / "conventions.md").read_text(encoding="utf-8")
    table = text[text.index("| codec | whose name | its Python package | specified in |"):]
    return dict(re.findall(r"^\| `([^`]+)` \| [^|]+ \| ([^|]+?) \|", table.split("\n\n")[0], re.MULTILINE))


def test_the_table_names_every_codec_refkv_writes_and_provides():
    names = codec_names()
    assert list(names) == sorted(names)
    core = set()
    for path in (ROOT / "rust" / "refkv-ir" / "src").rglob("*.rs"):
        core |= set(re.findall(r'"(imagecodecs_\w+|numcodecs\.\w+|refkv\.\w+)"', path.read_text(encoding="utf-8")))
    assert core <= set(names), core - set(names)
    pyproject = tomllib.loads((ROOT / "python" / "pyproject.toml").read_text(encoding="utf-8"))
    assert set(pyproject["project"]["entry-points"]["zarr.codecs"]) == {n for n, p in names.items() if p == "refkv"}
