"""The lists of fixtures a profile changed after the freeze (conformance/zarrify/revised/README.md)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.append(str(ROOT / "conformance" / "zarrify"))
from revised_fixtures import DIRECTORY, names_of  # noqa: E402


def test_each_list_is_sorted_and_names_its_formats_fixtures():
    lists = sorted(DIRECTORY.glob("*.txt"))
    assert lists
    for path in lists:
        names = names_of(path)
        assert names == sorted(set(names)), path.name
        fixtures = {p.name for p in (ROOT / "fixtures" / path.stem).rglob("*")}
        assert not set(names) - fixtures, path.name
