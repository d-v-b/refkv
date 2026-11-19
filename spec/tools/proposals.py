"""Checks that every proposal (design/proposals/<name>.md) says what of it has landed.

    uv run python spec/tools/proposals.py check

A proposal has, after its title and before its first section (`## `), a paragraph whose first
line starts `Status (YYYY-MM-DD): `, the date being the day the status was last made true:

    Status (2026-11-16): step 1 has landed (d-v-b/refkv#283). Steps 2 to 4 have not.

A proposal is a plan, and stays in the repository after its work lands, so without the line a
reader cannot tell what of it the code does. The check cannot tell a stale status from a fresh
one: a pull request that lands a step of a proposal updates the proposal's status line and its
date (docs/adding-a-format.md).
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROPOSALS = ROOT / "design" / "proposals"

FORM = "Status (YYYY-MM-DD): "
STATUS = re.compile(r"Status \((\d{4}-\d{2}-\d{2})\): \S")


def introduction(text: str) -> list[str]:
    """The lines of the document before its first section's heading."""
    lines = text.split("\n")
    end = next((i for i, line in enumerate(lines) if line.startswith("## ")), len(lines))
    return lines[:end]


def problem_of(name: str, text: str) -> str | None:
    """Why the proposal `name`.md has no well-formed status line, or None."""
    lines = [line for line in introduction(text) if line.startswith("Status")]
    if not lines:
        return f"{name}.md: no line that starts `{FORM}` before the first section"
    m = next((m for line in lines if (m := STATUS.match(line))), None)
    if m is None:
        return f"{name}.md: its status line does not start `{FORM}`: {lines[0]!r}"
    try:
        dt.date.fromisoformat(m.group(1))
    except ValueError:
        return f"{name}.md: its status line's date, {m.group(1)}, is not a date"
    return None


def check() -> list[str]:
    paths = sorted(PROPOSALS.glob("*.md"))
    if not paths:
        return [f"{PROPOSALS.relative_to(ROOT).as_posix()}/: no proposals"]
    found = (problem_of(p.stem, p.read_text(encoding="utf-8")) for p in paths)
    return [f for f in found if f is not None]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("command", choices=["check"])
    p.parse_args()
    found = check()
    for line in found:
        print(line)
    if found:
        print(f"proposals: {len(found)} problem(s); spec/tools/proposals.py gives the form", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
