"""Checks that every profile specifies the stored arrays its parser describes.

    uv run python spec/tools/stored_formats.py check

Each profile of the registry (spec/zarrify/profiles), but a layout, which presents another
profile's arrays, has a file of its own, `spec/zarrify/<profile>/stored-arrays.md`
(spec/stored-array.md §7), which says what its parser describes as stored arrays, or that it
describes none yet. So a profile cannot land without saying, and adding or changing one format's
stored arrays edits no file another format shares.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import profiles

ROOT = Path(__file__).resolve().parents[2]
ZARRIFY = ROOT / "spec" / "zarrify"
NAME = "stored-arrays.md"


def problems(names: list[str], specified: list[str]) -> list[str]:
    """Why the profiles `names` and the profiles with a stored-arrays file, `specified`, differ."""
    out = [f"{name}: no spec/zarrify/{name}/{NAME}" for name in names if name not in specified]
    out += [f"{name}: spec/zarrify/{name}/{NAME}, but no profile of the registry but a layout"
            for name in specified if name not in names]
    return out


def specified(root: Path = ZARRIFY) -> list[str]:
    """The profiles that have a stored-arrays file, by name."""
    return sorted(p.parent.name for p in root.glob(f"*/{NAME}"))


def check() -> list[str]:
    names = [e["name"] for e in profiles.load() if e["kind"] != "layout"]
    return problems(names, specified())


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("command", choices=["check"])
    p.parse_args()
    found = check()
    for line in found:
        print(line)
    if found:
        print(f"stored_formats: {len(found)} problem(s); see spec/stored-array.md §7", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
