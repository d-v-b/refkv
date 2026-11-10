"""The fixtures whose layout or verdict their profile changed after the freeze (revised/README.md)."""

from pathlib import Path

DIRECTORY = Path(__file__).parent / "revised"


def names_of(path: Path) -> list[str]:
    """The fixture names of one format's list: its lines, less the comments."""
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line and not line.startswith("#")]


REVISED = {name for path in DIRECTORY.glob("*.txt") for name in names_of(path)}
