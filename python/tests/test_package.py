"""The distribution around the package: its version, and the license files python/ ships."""

import re
from pathlib import Path

import refkv

ROOT = Path(__file__).resolve().parents[2]


def test_version():
    assert re.fullmatch(r"\d+\.\d+\.\d+(rc\d+)?(\.dev\d+)?|0\.0\.0\+unknown", refkv.__version__), refkv.__version__


def test_license_files_are_the_repositorys():
    for name in ("LICENSE-MIT", "LICENSE-APACHE"):
        assert (ROOT / "python" / name).read_bytes() == (ROOT / name).read_bytes(), f"python/{name} differs from {name}"
