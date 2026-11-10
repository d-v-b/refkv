"""Checks the changelogs of the format documents (spec/zarrify/<name>.md), and of spec/zarrify.md
and spec/conventions.md, in the form spec/zarrify.md's Profiles section gives
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#profiles>: one file per change,
spec/zarrify/changes/<name>/<date>-<slug>.md, a paragraph `Changed <date>[, with no change to any
output]: ...` (or another qualifier after the date) with the date of its name; and in the document,
after its version line, the pointer to that directory, then `Added <date>.` or, for a document
older than its changelog, the pointer to the history.

    uv run python spec/tools/changelog.py check [--base REF]

With `--base`, a format's document whose text outside its changelog (and outside the blocks
generated from the registry) differs from REF's must have a change file that REF does not: a
change to a profile's normative text records itself. One file per change, so that two PRs that
change one document add two files rather than edit one paragraph.
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
# The documents that are not a format's, with their version lines: their changes are recorded in
# the same form, but not checked against a base.
HUBS = {"zarrify": ("spec/zarrify.md", "Profiles version: "), "conventions": ("spec/conventions.md", None)}

CHANGED = re.compile(r"Changed (\d{4}-\d{2}-\d{2})(?:, [^:]+)?: \S")
ADDED = re.compile(r"Added (\d{4}-\d{2}-\d{2})\.")
HISTORY = "Changes before 2026-10-22 are recorded in"
POINTER = "Changes: "
FILE = re.compile(r"(\d{4}-\d{2}-\d{2})-[a-z0-9]+(?:-[a-z0-9]+)*\.md")
GENERATED = re.compile(r"<!-- profiles:[\w-]+ -->.*?<!-- /profiles -->", re.DOTALL)


@dataclass
class Changelog:
    entries: list[str]  # each paragraph's text, its lines joined by spaces
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


def changelog(text: str, version: str = "Convention version: ") -> Changelog | None:
    """The document's changelog: the paragraphs after its version line's that are a change, the
    pointer, `Added` or the history's pointer."""
    lines = text.split("\n")
    first = next((i for i, line in enumerate(lines) if line.startswith(version)), None)
    if first is None:
        return None
    paras = paragraphs(lines, first)
    entries, start, end = [], paras[0][1], paras[0][1]
    for a, b in paras[1:]:
        p = " ".join(line.strip() for line in lines[a:b])
        if not p.startswith(("Changed ", "Added ", HISTORY, POINTER)):
            break
        entries.append(p)
        end = b
    return Changelog(entries, start, end)


def pointer(name: str) -> str:
    """The pointer a format's document has after its version line."""
    return f"{POINTER}[changes/{name}/](changes/{name}/), one file per change."


def changes(name: str) -> dict[str, str]:
    """A document's change files: each one's name and text."""
    return {p.name: p.read_text(encoding="utf-8") for p in sorted((DOCS / "changes" / name).glob("*.md"))}


def change_problems(name: str, files: dict[str, str]) -> list[str]:
    out = []
    for file, text in files.items():
        where = f"changes/{name}/{file}"
        m = FILE.fullmatch(file)
        if m is None:
            out.append(f"{where}: not named `<date>-<slug>.md`")
            continue
        body = text.strip()
        c = CHANGED.match(" ".join(body.split()))
        if "\n\n" in body or c is None:
            out.append(f"{where}: not one paragraph `Changed <date>[, with no change to any output]: ...`")
        elif c.group(1) != m.group(1):
            out.append(f"{where}: dated {c.group(1)}, and named for {m.group(1)}")
        elif not body.endswith("."):
            out.append(f"{where}: does not end in a period")
    return out


def problems_of(name: str, text: str, files: dict[str, str], version: str = "Convention version: ") -> list[str]:
    """The problems of a format's document `name`.md and its change files."""
    log = changelog(text, version)
    if log is None:
        return [f"{name}.md: no version line"]
    out = change_problems(name, files)
    if any(e.startswith("Changed ") for e in log.entries):
        out.append(f"{name}.md: a change is in the document: move it to spec/zarrify/changes/{name}/<date>-<slug>.md")
    if pointer(name) not in log.entries:
        out.append(f"{name}.md: no `{pointer(name)}` after the version line")
    last = log.entries[-1] if log.entries else ""
    m = ADDED.fullmatch(last)
    if m is None and not last.startswith(HISTORY):
        out.append(f"{name}.md: the changelog does not end in `Added <date>.` or the history's pointer")
    elif m is not None and any(f[:10] < m.group(1) for f in files if FILE.fullmatch(f)):
        out.append(f"{name}.md: a change is dated before the document was added")
    return out


def hub_problems(name: str, path: str, version: str | None) -> list[str]:
    """The problems of spec/zarrify.md's or spec/conventions.md's changelog."""
    text = (ROOT / path).read_text(encoding="utf-8")
    out = change_problems(name, changes(name))
    if f"](zarrify/changes/{name}/)" not in text:
        out.append(f"{path}: no link to zarrify/changes/{name}/")
    log = changelog(text, version) if version is not None else None
    if log is not None and any(e.startswith("Changed ") for e in log.entries):
        out.append(f"{path}: a change is in the document: move it to spec/zarrify/changes/{name}/<date>-<slug>.md")
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


def changes_at(ref: str, name: str) -> set[str]:
    """The names of a document's change files at `ref`."""
    rel = (DOCS / "changes" / name).relative_to(ROOT).as_posix()
    r = subprocess.run(["git", "ls-tree", "--name-only", f"{ref}:{rel}"], cwd=ROOT, capture_output=True, text=True,
                       check=False)
    return set(r.stdout.split()) if r.returncode == 0 else set()


def unrecorded(ref: str, path: Path, text: str) -> str | None:
    """Why the document's change since `ref` is not recorded, or None."""
    old = at(ref, path)
    if old is None or outside(old) == outside(text):
        return None
    if set(changes(path.stem)) <= changes_at(ref, path.stem):
        return f"{path.name}: its text changed since {ref}, and no file was added to changes/{path.stem}/"
    return None


def check(base: str | None) -> list[str]:
    out = []
    for path in sorted(DOCS.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        out += problems_of(path.stem, text, changes(path.stem))
        if base is not None and (why := unrecorded(base, path, text)):
            out.append(why)
    for name, (path, version) in HUBS.items():
        out += hub_problems(name, path, version)
    documents = {p.stem for p in DOCS.glob("*.md")} | set(HUBS)
    out += [f"changes/{d.name}/: no document {d.name}" for d in sorted((DOCS / "changes").iterdir())
            if d.is_dir() and d.name not in documents]
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
