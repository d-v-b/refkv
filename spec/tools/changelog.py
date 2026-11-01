"""Checks the changelogs of the format documents (spec/zarrify/<name>.md), in the form
spec/zarrify.md's Profiles section gives
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#profiles>: the paragraphs after the
version line, one entry each, `Changed <date>[, with no change to any output]: ...` (or another
qualifier after the date), newest first,
then `Added <date>.` or, for a document older than its changelog, the pointer to the history.

    uv run python spec/tools/changelog.py check [--base REF]

With `--base`, a document whose text outside its changelog (and outside the blocks generated from
the registry) differs from REF's must have a changelog that differs too: a change to a
profile's normative text records itself.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "spec" / "zarrify"

CHANGED = re.compile(r"Changed (\d{4}-\d{2}-\d{2})(?:, [^:]+)?: \S")
ADDED = re.compile(r"Added (\d{4}-\d{2}-\d{2})\.")
HISTORY = "Changes before 2026-10-22 are recorded in"
GENERATED = re.compile(r"<!-- profiles:[\w-]+ -->.*?<!-- /profiles -->", re.S)


@dataclass
class Changelog:
    entries: list[str]  # each entry's text, its lines joined by spaces
    start: int  # the changelog's first and last line + 1, of the document's lines
    end: int


def paragraphs(lines: list[str], start: int) -> list[tuple[int, int]]:
    """The (first, last + 1) lines of each paragraph from line `start` on."""
    out, i = [], start
    while i < len(lines):
        if not lines[i].strip():
            i += 1
            continue
        j = i
        while j < len(lines) and lines[j].strip():
            j += 1
        out.append((i, j))
        i = j
    return out


def changelog(text: str) -> Changelog | None:
    """The document's changelog: the paragraphs after its version line's that are entries."""
    lines = text.split("\n")
    first = next((i for i, line in enumerate(lines) if line.startswith("Convention version: ")), None)
    if first is None:
        return None
    paras = paragraphs(lines, first)
    entries, start, end = [], paras[0][1], paras[0][1]
    for a, b in paras[1:]:
        p = " ".join(line.strip() for line in lines[a:b])
        if not (p.startswith(("Changed ", "Added ")) or p.startswith(HISTORY)):
            break
        entries.append(p)
        end = b
    return Changelog(entries, start, end)


def problems_of(name: str, text: str) -> list[str]:
    log = changelog(text)
    if log is None:
        return [f"{name}: no version line"]
    if not log.entries:
        return [f"{name}: no changelog after the version line"]
    out = []
    *changes, last = log.entries
    dates = []
    for e in changes:
        m = CHANGED.match(e)
        if m is None:
            out.append(f"{name}: an entry is not `Changed <date>[, with no change to any output]: ...`: {e[:60]!r}")
            continue
        if not e.endswith("."):
            out.append(f"{name}: the entry of {m.group(1)} does not end in a period")
        dates.append(m.group(1))
    if any(a < b for a, b in zip(dates, dates[1:], strict=False)):
        out.append(f"{name}: the entries are not newest first")
    m = ADDED.fullmatch(last)
    if m is None and not last.startswith(HISTORY):
        out.append(f"{name}: the last entry is neither `Added <date>.` nor the history's pointer")
    elif m is not None and dates and dates[-1] < m.group(1):
        out.append(f"{name}: a change is dated before the document was added")
    return out


def outside(text: str) -> str:
    """The document's text but its changelog and its generated blocks."""
    log = changelog(text)
    lines = text.split("\n")
    if log is not None:
        lines = lines[: log.start] + lines[log.end :]
    return GENERATED.sub("", "\n".join(lines))


def at(ref: str, path: Path) -> str | None:
    rel = path.relative_to(ROOT).as_posix()
    r = subprocess.run(["git", "show", f"{ref}:{rel}"], cwd=ROOT, capture_output=True, text=True, check=False)
    return r.stdout if r.returncode == 0 else None


def unrecorded(ref: str, path: Path, text: str) -> str | None:
    """Why the document's change since `ref` is not recorded, or None."""
    old = at(ref, path)
    if old is None or outside(old) == outside(text):
        return None
    before, now = changelog(old), changelog(text)
    if before is not None and now is not None and before.entries == now.entries:
        return f"{path.name}: its text changed since {ref}, and its changelog did not"
    return None


def check(base: str | None) -> list[str]:
    out = []
    for path in sorted(DOCS.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        out += problems_of(path.name, text)
        if base is not None and (why := unrecorded(base, path, text)):
            out.append(why)
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("command", choices=["check"])
    p.add_argument("--base", help="a git ref: each document whose text changed since it must record the change")
    args = p.parse_args()
    found = check(args.base)
    for line in found:
        print(line)
    if found:
        print(f"changelog: {len(found)} problem(s); spec/zarrify.md's Profiles section gives the form", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
