"""Tests of spec/tools/sections.py: the anchors, where references are found, and the check
(one test of what it accepts, one per problem it reports)."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import sections

URL = sections.URL

SPEC = """# A spec

## 1. Introduction

### 1.1 Output and equivalence

## 2. The layout

```
## 9. Not a heading
```

## 2. The layout

## 4. Shared
"""

FORMAT = """# TIFF

# Part 1. The convention

## 3. OME-XML

## 4. The image

# Part 2. The profile

## 3. TIFF profile

### 3.1 Reading
"""


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repository of a few documents, as sections.ROOT."""
    files = {
        "spec/refkv.md": SPEC,
        "spec/zarrify/tiff.md": FORMAT,
    }
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text, encoding="utf-8")
    monkeypatch.setattr(sections, "ROOT", tmp_path)
    for f in (sections.headings, sections.anchors):
        f.cache_clear()
    yield tmp_path
    for f in (sections.headings, sections.anchors):
        f.cache_clear()


def test_slug():
    cases = {
        "8.7 Reader policy": "87-reader-policy",
        "3–13. Profiles": "313-profiles",
        "Part 2. The profile": "part-2-the-profile",
        "Appendix A: Complete schema": "appendix-a-complete-schema",
        "1.4 Store inputs (`list`)": "14-store-inputs-list",
        "See [the spec](x.md) now": "see-the-spec-now",
        "snake_case and-hyphen": "snake_case-and-hyphen",
    }
    assert {t: sections.slug(t) for t in cases} == cases


@pytest.mark.usefixtures("repo")
def test_headings():
    hs = sections.headings("spec/refkv.md")
    assert [(h.number, h.anchor) for h in hs] == [
        (None, "a-spec"),
        ("1", "1-introduction"),
        ("1.1", "11-output-and-equivalence"),
        ("2", "2-the-layout"),
        ("2", "2-the-layout-1"),  # a repeat; the fenced one is no heading
        ("4", "4-shared"),
    ]
    parts = [(h.number, h.part) for h in sections.headings("spec/zarrify/tiff.md") if h.number]
    assert parts == [("3", 1), ("4", 1), ("3", 2), ("3.1", 2)]


def test_regions():
    py = 'x = "§1"  # §2\ndef f():\n    """§3"""\n'
    assert [(r.kind, py[r.start : r.end]) for r in sections.py_regions(py)] == [
        ("string", '"§1"'),
        ("comment", "# §2"),
        ("docstring", '"""§3"""'),
    ]
    ts = 'const a = `x ${b + "§1"} §2`; // §3\nconst r = /"/; /* §4 */\n'
    kinds = [
        (r.kind, ts[r.start : r.end]) for r in sections.c_like_regions(ts, rust=False) if "§" in ts[r.start : r.end]
    ]
    assert kinds == [("string", '"§1"'), ("string", " §2"), ("comment", "// §3"), ("comment", "/* §4 */")]
    rs = "fn f<'a>(c: char) { let s = \"§1\"; let c = '\"'; } /// §2\n"
    assert [rs[r.start : r.end] for r in sections.c_like_regions(rs, rust=True)] == ['"§1"', "/// §2\n"[:-1]]
    md = "§1 `§2`\n```\n§3\n```\n<!-- §4 -->§5\n"
    found = [md[r.start : r.end] for r in sections.md_regions(md)]
    assert "".join(found).count("§") == 2
    assert "§1" in found[0]
    assert "§5" in found[-1]


# ---------------------------------------------------------------- the check


def write(repo, files: dict[str, str]) -> list[str]:
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    sections.anchors.cache_clear()
    sections.headings.cache_clear()
    return sections.check_files(sorted(files))


def test_check_accepts(repo):
    """Links to existing headings, other standards' sections, placeholders, and bare
    references in the records of past states."""
    problems = write(
        repo,
        {
            "spec/new.md": "[§1.1](refkv.md#11-output-and-equivalence), [§2](#x), RFC 9110 §5.6.7, "
            "cited as §n.\n\n## x\n",
            "python/src/refkv/y.py": f"# spec §1.1 <{URL}spec/refkv.md#11-output-and-equivalence>\n"
            f"# a §2\n# <{URL}spec/refkv.md#2-the-layout-1>\n",
            "js/src/y.ts": f"const s = `(spec §2 <{URL}spec/refkv.md#2-the-layout>)`;\n",
            "design/conformance/archive/r1/x.md": "bare §3.1 is a record\n",
            "spec/history/archive/SPEC.r1.md": "[§9](#nowhere)\n",
        },
    )
    assert problems == []


def test_check_bare_reference(repo):
    assert write(repo, {"python/src/refkv/y.py": "# see §1.1\n"}) == ["python/src/refkv/y.py:1: bare reference '§1.1'"]


def test_check_bare_reference_after_code_bracket(repo):
    """A bracket in a code span (the interval `[0, size)`) opens no link."""
    assert write(repo, {"spec/new.md": "the bytes `[0, size)` (see §1.2](x.md)\n"}) == [
        "spec/new.md:1: bare reference '§1.2'"
    ]


def test_check_missing_heading(repo):
    assert write(repo, {"spec/new.md": "[§9](refkv.md#9-nothing)\n"}) == [
        "spec/new.md:1: broken link refkv.md#9-nothing (no heading #9-nothing in spec/refkv.md)"
    ]


def test_check_missing_document(repo):
    assert write(repo, {"spec/new.md": "[§1](gone.md#1-introduction)\n"}) == [
        "spec/new.md:1: broken link gone.md#1-introduction (no file spec/gone.md)"
    ]


def test_check_missing_heading_by_url(repo):
    assert write(repo, {"js/src/y.ts": f"// §9 <{URL}spec/refkv.md#9-nothing>\n"}) == [
        f"js/src/y.ts:1: broken link {URL}spec/refkv.md#9-nothing (no heading #9-nothing in spec/refkv.md)"
    ]


def test_check_missing_document_by_url(repo):
    assert write(repo, {"js/src/y.ts": f"// §9 <{URL}spec/gone.md#9-nothing>\n"}) == [
        f"js/src/y.ts:1: broken link {URL}spec/gone.md#9-nothing (no file spec/gone.md)"
    ]


def test_check_reads_a_file_that_is_not_added_yet(repo):
    """With no paths given, the check reads what git tracks and what it would: a new file with a
    bare reference fails it before the commit that adds the file, and an ignored one does not."""
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    no_paths = argparse.Namespace(paths=[])
    assert sections.check(no_paths) == 0
    (repo / "ignored.py").write_text("# a bare reference: conventions §3\n", encoding="utf-8")
    assert sections.check(no_paths) == 0
    (repo / "new.py").write_text("# a bare reference: conventions §3\n", encoding="utf-8")
    assert sections.check(no_paths) == 1
