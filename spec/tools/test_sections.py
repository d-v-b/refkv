"""Tests of spec/tools/sections.py: the anchors, the resolution of references, the
rewrite, and the check (one test of what it accepts, one per problem it reports)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import sections  # noqa: E402

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

BINDING = """# A binding

# Part 2. The ZIP binding

## 4. Shared

## 9. What the binding defines

### 9.1 Structure
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
        "spec/bindings/vzip.md": BINDING,
        "spec/history/archive/SPEC.r1.md": "# S\n\n## 1. Introduction\n\n### 1.1 Old\n",
        "spec/virtualize.md": "# V\n\n## 1. Introduction\n\n### 1.2 Input\n",
        "spec/virtualize/tiff.md": FORMAT,
        "spec/conventions.md": "# C\n\n## 3. Arrays\n",
    }
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text, encoding="utf-8")
    monkeypatch.setattr(sections, "ROOT", tmp_path)
    monkeypatch.setattr(sections, "OVERRIDES_FILE", tmp_path / "none.tsv")
    for f in (sections.headings, sections.anchors, sections.repo_md_files, sections.overrides):
        f.cache_clear()
    monkeypatch.setattr(sections, "repo_md_files", lambda: frozenset(files))
    yield tmp_path
    for f in (sections.headings, sections.anchors, sections.overrides):
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


def test_headings(repo):
    hs = sections.headings("spec/refkv.md")
    assert [(h.number, h.anchor) for h in hs] == [
        (None, "a-spec"),
        ("1", "1-introduction"),
        ("1.1", "11-output-and-equivalence"),
        ("2", "2-the-layout"),
        ("2", "2-the-layout-1"),  # a repeat; the fenced one is no heading
        ("4", "4-shared"),
    ]
    parts = [(h.number, h.part) for h in sections.headings("spec/virtualize/tiff.md") if h.number]
    assert parts == [("3", 1), ("4", 1), ("3", 2), ("3.1", 2)]


@pytest.mark.parametrize(
    ("path", "text", "status", "target"),
    [
        # a qualifier names the document
        ("python/src/refkv/x.py", "# spec/refkv.md §1.1\n", "ok", "spec/refkv.md#11-output-and-equivalence"),
        ("python/src/refkv/x.py", "# SPEC.md §1.1\n", "ok", "spec/refkv.md#11-output-and-equivalence"),
        ("python/src/refkv/x.py", "# (spec §1.1)\n", "ok", "spec/refkv.md#11-output-and-equivalence"),
        # the specification is two documents, numbered as one
        ("python/src/refkv/x.py", "# SPEC.md §9.1\n", "ok", "spec/bindings/vzip.md#91-structure"),
        ("python/src/refkv/vzip/x.py", "# §9.1 here\n", "ok", "spec/bindings/vzip.md#91-structure"),
        ("python/src/refkv/x.py", "# (spec §4)\n", "ambiguous", ""),
        # a record of a past round cites the snapshot of its revision
        ("design/conformance/archive/r1/x.md", "SPEC.md §1.1\n", "ok", "spec/history/archive/SPEC.r1.md#11-old"),
        ("js/src/x.ts", "// conventions §3\n", "ok", "spec/conventions.md#3-arrays"),
        # spec/virtualize.md's §3-§13 are the profiles
        ("python/src/refkv/virtualize/x.py", "# spec/virtualize.md §3.1\n", "ok", "spec/virtualize/tiff.md#31-reading"),
        # a format's document by its path, or by its name, is its convention
        ("rust/refkv-ir/src/xml.rs", "// spec/virtualize/tiff.md §3\n", "ok", "spec/virtualize/tiff.md#3-ome-xml"),
        ("spec/virtualize.md", "decimal (TIFF §3)\n", "ok", "spec/virtualize/tiff.md#3-ome-xml"),
        ("python/src/refkv/virtualize/tiff/x.py", "# the profile §3\n", "ok", "spec/virtualize/tiff.md#3-tiff-profile"),
        # a bare reference takes the file's document; a format's code, its format's
        ("python/src/refkv/reader.py", "# §2 here\n", "ambiguous", ""),
        ("python/src/refkv/virtualize/tiff/x.py", "# §4\n", "ok", "spec/virtualize/tiff.md#4-the-image"),
        ("python/src/refkv/virtualize/tiff/x.py", "# §3\n", "ambiguous", ""),
        ("python/src/refkv/virtualize/tiff/x.py", "# §1.2\n", "review", "spec/virtualize.md#12-input"),
        # a chain keeps the qualifier
        ("js/src/x.ts", "// spec/virtualize.md §1.2, §3.1\n", "ok", "spec/virtualize/tiff.md#31-reading"),
        # other standards, links, and what cannot be settled
        ("python/src/refkv/x.py", "# RFC 3986 §5.2.2\n", "external", ""),
        ("python/src/refkv/x.py", "# RFC 9110 §5.6.7 and §1.1\n", "external", ""),
        ("python/src/refkv/x.py", f"# §1.1 <{URL}spec/refkv.md#11-output-and-equivalence>\n", "linked", ""),
        ("spec/refkv.md", "[§1.1](#11-output-and-equivalence)\n", "linked", ""),
        ("python/src/refkv/x.py", "# spec/refkv.md §7\n", "unresolved", ""),
        ("python/src/refkv/x.py", "# nothing.md §1\n", "unresolved", ""),
        ("somewhere/else.py", "# §1\n", "unresolved", ""),
    ],
)
def test_resolve(repo, path, text, status, target):
    ref = sections.analyze(path, text)[-1]
    got = f"{ref.target.doc}#{ref.target.anchor}" if ref.target else ""
    assert (ref.status, got) == (status, target)


def test_resolve_by_part(repo):
    """In a format's document, a bare reference means the part it is in first."""
    got = []
    for text in (FORMAT + "\nas §3 says\n", FORMAT.replace("## 4. The image", "## 4. The image\n\nas §3 says")):
        (repo / "spec/virtualize/tiff.md").write_text(text)
        sections.headings.cache_clear()
        got.append(sections.analyze("spec/virtualize/tiff.md", text)[0].target.anchor)
    assert got == ["3-tiff-profile", "3-ome-xml"]


def test_override(repo, monkeypatch):
    row = ["python/src/refkv/virtualize/tiff/x.py", "1", "§3", "0", "spec/virtualize/tiff.md#3-ome-xml", ""]
    (repo / "o.tsv").write_text("# a comment\n" + "\t".join(row) + "\n")
    monkeypatch.setattr(sections, "OVERRIDES_FILE", repo / "o.tsv")
    sections.overrides.cache_clear()
    ref = sections.analyze("python/src/refkv/virtualize/tiff/x.py", "# §3\n")[0]
    assert (ref.status, ref.target.anchor) == ("ok", "3-ome-xml")


def test_rewrite(repo):
    cases = [
        (
            "spec/refkv.md",
            "See §1.1 and spec/virtualize.md §1.2.\n",
            "See [§1.1](#11-output-and-equivalence) and [spec/virtualize.md §1.2](virtualize.md#12-input).\n",
        ),
        (
            "spec/virtualize/tiff.md",
            FORMAT + "\nconventions §3\n",
            FORMAT + "\n[conventions §3](../conventions.md#3-arrays)\n",
        ),
        (
            "python/src/refkv/x.py",
            "x = 1  # spec §1.1\n",
            f"x = 1  # spec §1.1 <{URL}spec/refkv.md#11-output-and-equivalence>\n",
        ),
        (
            "python/src/refkv/x.py",
            'raise ValueError("bad (spec §1)")\n',
            f'raise ValueError("bad (spec §1 <{URL}spec/refkv.md#1-introduction>)")\n',
        ),
        # a comment line made too long is wrapped, the URL whole
        (
            "js/src/x.ts",
            "  // " + "word " * 14 + "(spec/refkv.md §1.1) more\n",
            "  // " + "word " * 14 + "(spec/refkv.md §1.1\n"
            f"  // <{URL}spec/refkv.md#11-output-and-equivalence>) more\n",
        ),
        (
            "python/src/refkv/x.py",
            'def f():\n    """' + "word " * 14 + '(spec §1.1)."""\n',
            'def f():\n    """' + "word " * 14 + "(spec §1.1\n"
            f'    <{URL}spec/refkv.md#11-output-and-equivalence>)."""\n',
        ),
        # and its last piece joins the next line of the same text, when they fit
        (
            "python/src/refkv/x.py",
            'def f():\n    """' + "word " * 14 + '(spec §1.1) a\n    b c."""\n',
            'def f():\n    """' + "word " * 14 + "(spec §1.1\n"
            f'    <{URL}spec/refkv.md#11-output-and-equivalence>) a b c."""\n',
        ),
        # a one-line JSDoc comment made too long becomes a block
        (
            "js/src/x.ts",
            "  /** " + "word " * 14 + "(spec §1.1). */\n",
            "  /**\n   * " + "word " * 14 + "(spec §1.1\n"
            f"   * <{URL}spec/refkv.md#11-output-and-equivalence>).\n   */\n",
        ),
        # but not when it closes the docstring
        (
            "python/src/refkv/x.py",
            'def f():\n    """' + "word " * 14 + '(spec §1.1)."""\n    return 1\n',
            'def f():\n    """' + "word " * 14 + "(spec §1.1\n"
            f'    <{URL}spec/refkv.md#11-output-and-equivalence>)."""\n    return 1\n',
        ),
    ]
    for path, text, want in cases:
        refs = sections.analyze(path, text)
        assert sections.rewrite(text, refs) == want, path


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
    assert "".join(found).count("§") == 2 and "§1" in found[0] and "§5" in found[-1]


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
