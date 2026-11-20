"""Checks that the stored-array spec accounts for every profile's arrays.

    uv run python spec/tools/stored_formats.py check

spec/stored-array.md §7 has a paragraph for each format whose parser describes its arrays, which
opens with a link to the format's convention (`**CZI** ([spec/zarrify/czi.md](zarrify/czi.md))`),
and a list of the formats whose parsers describe none yet, each a link to its convention. Every
profile of the registry (spec/zarrify/profiles), but a layout, which presents another profile's
arrays, is in exactly one of the two. So a profile cannot land with no description and no line
that says so, and a description cannot land while the list still names its format.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import profiles

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "spec" / "stored-array.md"

HEADING = "## 7. The formats"
PARAGRAPH = re.compile(r"^\*\*[^*]+\*\* \(\[spec/zarrify/([a-z0-9-]+)\.md\]\(zarrify/\1\.md\)\)")
LIST = re.compile(r"^The parsers of (.+?)\s+describe\s+no\s+array\s+yet", re.DOTALL)
LINK = re.compile(r"\[[^\]]+\]\(zarrify/([a-z0-9-]+)\.md\)")


def section(text: str) -> str:
    """§7 of the spec: from its heading to the next of its level, or the end."""
    start = text.index(HEADING)
    end = text.find("\n## ", start + len(HEADING))
    return text[start:] if end < 0 else text[start:end]


def accounted(text: str) -> tuple[list[str], list[str]]:
    """The formats §7 has paragraphs for, and those its list names, by convention name, in order."""
    s = section(text)
    paragraphs = [m.group(1) for line in s.split("\n") if (m := PARAGRAPH.match(line))]
    m = LIST.search(s.split("\n\n", 2)[1]) if s.count("\n\n") >= 2 else None
    listed = LINK.findall(m.group(1)) if m else []
    return paragraphs, listed


def problems(names: list[str], text: str) -> list[str]:
    """Why §7 of `text` does not account for the profiles `names` once each."""
    paragraphs, listed = accounted(text)
    out = []
    for name in names:
        n = paragraphs.count(name) + listed.count(name)
        if n == 0:
            out.append(f"{name}: no paragraph in §7, and not in its list of formats that describe no array")
        elif paragraphs.count(name) and listed.count(name):
            out.append(f"{name}: a paragraph in §7, and in its list of formats that describe no array")
        elif n > 1:
            out.append(f"{name}: named {n} times in §7")
    strays = [name for name in dict.fromkeys(paragraphs + listed) if name not in names]
    out += [f"{name}: in §7, but no profile of the registry" for name in strays]
    return out


def check() -> list[str]:
    names = [e["name"] for e in profiles.load() if e["kind"] != "layout"]
    return problems(names, SPEC.read_text(encoding="utf-8"))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("command", choices=["check"])
    p.parse_args()
    found = check()
    for line in found:
        print(line)
    if found:
        print(f"stored_formats: {len(found)} problem(s); stored_formats.py says what §7 holds", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
