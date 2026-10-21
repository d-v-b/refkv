"""Section references (§n) in refkv: links to the headings they mean, and a check.

    uv run python spec/tools/sections.py check [paths...]
    uv run python spec/tools/sections.py migrate [--write] [--report refs.tsv]

A reference to a section of a document of this repository is a link to that
section's heading, never a bare "§n":

- in Markdown, a relative link to the heading's anchor, its text keeping the
  "§n": `[§6.7](#67-reader-policy)`, `[spec/refkv.md §6.7](refkv.md#67-reader-policy)`;
- in code (comments and docstrings, and the descriptions in the JSON schemas and
  source models), the "§n" followed by the heading's URL in angle brackets:
  `spec/refkv.md §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>`.

`check` fails on a bare reference outside the records of past states (EXCLUDED)
and the user-facing strings listed in bare_refs_allowed.txt, and on a link
(relative, or a full URL of this repository) to a document or heading that does
not exist. A reference to another standard ("RFC 9110 §5.6.7", "PS3.5 §6.2.2")
is not one to this repository, and stays as it is.

`migrate` was the one-time conversion: it resolves each bare reference to the
heading it means, by the words before it ("spec/refkv.md", "conventions", "the
profile", or an old name such as "SPEC.md"), else by where it is (a format's code
cites that format's document), and rewrites it as a link. section_overrides.tsv
settles what that cannot. The report lists every reference and its heading.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import io
import json
import re
import subprocess
import sys
import tokenize
from dataclasses import dataclass, field
from fnmatch import fnmatch
from functools import cache
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
URL = "https://github.com/d-v-b/refkv/blob/main/"
HERE = Path(__file__).resolve().parent

# Records of past states: their references cite the documents as they were then, so
# a bare one is allowed. (Where the snapshot of that revision is in spec/history/,
# migrate linked to it.)
EXCLUDED = (
    "spec/history/**",
    "spec/tools/**",  # this tool, whose examples and tests are bare references
    "python/tests/reference/**",
    "design/conformance/**",
    "conformance/impls/**",
    "conformance/virtualize/reference/**",
    "design/IR_NOTES.md",
    "design/plans/**",
)
# Records that migrate left as they are: frozen snapshots, the frozen reference
# twins and their tests (revision 20, of which there is no snapshot), and dated notes.
LEFT_ALONE = (
    "spec/history/**",
    "spec/tools/**",  # this tool, whose examples and tests are bare references
    "python/tests/reference/**",
    "conformance/virtualize/reference/**",
    "design/IR_NOTES.md",
    "design/plans/**",
)
# Written by spec/generate_schemas.py.
GENERATED = ("spec/virtualize/*/schema.json",)

# The profile registry (spec/virtualize/profiles.json): the format documents, each a profile
# numbered as a section of spec/virtualize.md (Part 2 of the format's document), and the dataset
# layouts' conventions (spec/conventions.md §9): a document each, no profile number.
REGISTRY: list[dict] = json.loads((ROOT / "spec" / "virtualize" / "profiles.json").read_text(encoding="utf-8"))
FORMATS = tuple(e["name"] for e in REGISTRY if e["section"] is not None)
PROFILE_NUMBERS = {e["name"]: e["section"] for e in REGISTRY if e["section"] is not None}
LAYOUTS = tuple(e["name"] for e in REGISTRY if e["section"] is None)
DOCS = FORMATS + LAYOUTS

# Old names of the documents, from before the repository was reorganized.
OLD_NAMES = {
    "SPEC.md": "spec",
    "spec/archive.md": "spec",
    "VIRTUALIZE.md": "spec/virtualize.md",
    "conventions/README.md": "spec/conventions.md",
    "TECHNIQUES.md": "spec/TECHNIQUES.md",
    "ARCHITECTURE.md": "design/ARCHITECTURE.md",
}

# ---------------------------------------------------------------- headings


def slug(text: str) -> str:
    """The anchor GitHub gives a heading, before repeats are numbered: the text as it
    renders, lowercase, without punctuation but hyphens and underscores, each space a
    hyphen."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


@dataclass(frozen=True)
class Heading:
    doc: str
    level: int
    title: str
    anchor: str
    number: str | None  # "8.7", "8", or None
    part: int | None  # in a document of parts ("# Part 2. ..."), the part
    line: int


FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
ATX = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
NUMBER = re.compile(r"^(\d+(?:\.\d+)*)\.?(?=\s)")
PART = re.compile(r"^Part (\d+)\b")


def parse_headings(doc: str, text: str) -> list[Heading]:
    """The ATX headings of a Markdown document, outside fenced code, with GitHub's
    anchors (a repeated one gets -1, -2, ...)."""
    out: list[Heading] = []
    seen: set[str] = set()
    fence: str | None = None
    part: int | None = None
    for i, line in enumerate(text.splitlines(), start=1):
        m = FENCE.match(line)
        if m:
            if fence is None:
                fence = m.group(1)
            elif m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not line.strip()[len(m.group(1)) :]:
                fence = None
            continue
        if fence is not None:
            continue
        m = ATX.match(line)
        if not m:
            continue
        level, title = len(m.group(1)), m.group(2)
        base = anchor = slug(title)
        n = 1
        while anchor in seen:
            anchor = f"{base}-{n}"
            n += 1
        seen.add(anchor)
        p = PART.match(title)
        if level == 1 and p:
            part = int(p.group(1))
        num = NUMBER.match(title)
        out.append(Heading(doc, level, title, anchor, num.group(1) if num else None, part, i))
    return out


@cache
def headings(doc: str) -> tuple[Heading, ...]:
    path = ROOT / doc
    if not path.is_file():
        return ()
    return tuple(parse_headings(doc, path.read_text(encoding="utf-8")))


@cache
def anchors(doc: str) -> frozenset[str]:
    """A document's anchors: its headings', and its HTML anchors' ids."""
    path = ROOT / doc
    found = {h.anchor for h in headings(doc)}
    if path.is_file():
        found |= set(re.findall(r"""<a\s+(?:id|name)=["']([^"']+)["']""", path.read_text(encoding="utf-8")))
    return frozenset(found)


def format_of_doc(doc: str) -> str | None:
    m = re.fullmatch(r"spec/virtualize/([\w-]+)\.md", doc)
    return m.group(1) if m and m.group(1) in DOCS else None


def numbered(doc: str, number: str, part: int | None = None) -> list[Heading]:
    """The headings of `doc` numbered `number` (in `part`, if given)."""
    if doc == "spec/virtualize.md" and number[0].isdigit():
        top = int(number.split(".")[0])
        for f, n in PROFILE_NUMBERS.items():
            if top == n:
                return numbered(f"spec/virtualize/{f}.md", number, 2)
    return [h for h in headings(doc) if h.number == number and (part is None or h.part == part)]


# ---------------------------------------------------------------- where references are


@dataclass(frozen=True)
class Region:
    start: int
    end: int
    kind: str  # "md" (prose), "comment", "docstring" or "string"


def md_regions(text: str) -> list[Region]:
    """The prose of a Markdown document: not fenced code, inline code or HTML comments."""
    out: list[Region] = []
    pos = start = 0
    fence: str | None = None
    for line in text.splitlines(keepends=True):
        m = FENCE.match(line)
        if m:
            if fence is None:
                out.append(Region(start, pos, "md"))
                fence = m.group(1)
            elif m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                fence = None
                start = pos + len(line)
        pos += len(line)
    if fence is None:
        out.append(Region(start, len(text), "md"))
    holes = [(m.start(), m.end()) for m in re.finditer(r"(`+)(?:(?!\1).)+?\1|<!--.*?-->", text, re.S)]
    cut: list[Region] = []
    for r in out:
        s = r.start
        for a, b in holes:
            if b <= s or a >= r.end:
                continue
            if a > s:
                cut.append(Region(s, a, "md"))
            s = max(s, b)
        if s < r.end:
            cut.append(Region(s, r.end, "md"))
    return cut


def py_regions(text: str) -> list[Region]:
    """The comments and strings of Python source. A string that is a statement of its
    own is a docstring."""
    offsets = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))

    def at(rc: tuple[int, int]) -> int:
        return offsets[rc[0] - 1] + rc[1]

    toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    fstart = getattr(tokenize, "FSTRING_START", -1)
    fend = getattr(tokenize, "FSTRING_END", -1)
    skip = {tokenize.NL, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT}
    out: list[Region] = []
    depth = 0  # inside an f-string
    for i, t in enumerate(toks):
        if t.type == tokenize.COMMENT:
            out.append(Region(at(t.start), at(t.end), "comment"))
            continue
        if t.type == fend:
            depth -= 1
            continue
        if depth or t.type not in (tokenize.STRING, fstart):
            if t.type == fstart:
                depth += 1
            continue
        j = i
        if t.type == fstart:
            depth += 1
            nest = 0
            while True:
                if toks[j].type == fstart:
                    nest += 1
                elif toks[j].type == fend:
                    nest -= 1
                    if nest == 0:
                        break
                j += 1
        prev = next((u for u in reversed(toks[:i]) if u.type not in skip), None)
        k = j + 1
        while k < len(toks) and (toks[k].type in skip or toks[k].type == tokenize.STRING):
            k += 1
        nxt = toks[k] if k < len(toks) else None
        alone = (prev is None or prev.type in (tokenize.NEWLINE, tokenize.ENCODING)) and (
            nxt is None or nxt.type in (tokenize.NEWLINE, tokenize.ENDMARKER)
        )
        out.append(Region(at(t.start), at(toks[j].end), "docstring" if alone else "string"))
    return out


def c_like_regions(text: str, rust: bool) -> list[Region]:
    """The comments and string literals of TypeScript or Rust source."""
    out: list[Region] = []
    i, n = 0, len(text)
    stack: list[str] = []  # TypeScript: open template literals ("`") and braces ("{")
    prev = ""  # the last significant token
    while i < n:
        c = text[i]
        if stack and stack[-1] == "`":
            s = i
            while i < n and text[i] != "`" and not text.startswith("${", i):
                i += 2 if text[i] == "\\" else 1
            out.append(Region(s, i, "string"))
            if i < n and text[i] == "`":
                stack.pop()
                i += 1
                prev = "`"
            elif i < n:
                stack.append("{")
                i += 2
                prev = "{"
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(Region(i, j, "comment"))
            i = j
            continue
        if text.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if rust and text.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif text.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            out.append(Region(i, j, "comment"))
            i = j
            continue
        if rust:
            m = re.compile(r'b?r(#*)"').match(text, i)
            if m and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")):
                close = '"' + m.group(1)
                j = text.find(close, m.end())
                j = n if j < 0 else j + len(close)
                out.append(Region(i, j, "string"))
                i, prev = j, '"'
                continue
            if c == "'":
                m = re.compile(r"'(?:\\(?:u\{[0-9a-fA-F]+\}|x[0-9a-fA-F]{2}|.)|[^\\'\n])'").match(text, i)
                i, prev = (m.end(), "'") if m else (i + 1, "'")  # a char, or a lifetime
                continue
        if c == "`" and not rust:
            stack.append("`")
            i += 1
            continue
        if c == '"' or (c == "'" and not rust):
            j = i + 1
            while j < n and text[j] != c and (rust or text[j] != "\n"):
                j += 2 if text[j] == "\\" else 1
            j = min(n, j + 1)
            out.append(Region(i, j, "string"))
            i, prev = j, c
            continue
        if not rust and c == "/" and (prev == "" or prev in "(,=:[!&|?{};+-*%<>~^" or prev in ("return", "typeof")):
            m = re.compile(r"/(?:\\.|\[(?:\\.|[^\]\\\n])*\]|[^/\\\n\[])+/[a-z]*").match(text, i)
            if m:
                i, prev = m.end(), "/"
                continue
        if not rust and stack:
            if c == "{":
                stack.append("{")
            elif c == "}":
                stack.pop()
        if c.isalnum() or c == "_":
            m = re.compile(r"\w+").match(text, i)
            prev = m.group(0)
            i = m.end()
            continue
        if not c.isspace():
            prev = c
        i += 1
    return out


def hash_regions(text: str) -> list[Region]:
    """`#` comments (YAML, shell, justfile, the corpus lists)."""
    out = []
    pos = 0
    for line in text.splitlines(keepends=True):
        j = line.find("#")
        if j >= 0:
            out.append(Region(pos + j, pos + len(line.rstrip("\n")), "comment"))
        pos += len(line)
    return out


def json_regions(text: str) -> list[Region]:
    return [Region(m.start(), m.end(), "string") for m in re.finditer(r'"(?:\\.|[^"\\])*"', text)]


def regions(path: str, text: str) -> list[Region]:
    suffix = PurePosixPath(path).suffix
    if suffix == ".md":
        return md_regions(text)
    if suffix == ".py":
        return py_regions(text)
    if suffix in (".ts", ".mjs", ".js"):
        return c_like_regions(text, rust=False)
    if suffix == ".rs":
        return c_like_regions(text, rust=True)
    if suffix == ".json":
        return json_regions(text)
    return hash_regions(text)


# ---------------------------------------------------------------- references

TOKEN = re.compile(r"§\s?(?P<id>\d+(?:\.\d+)*|[A-Z](?:\.\d+)+)(?!\w)")
# Other standards, cited by section.
EXTERNAL = re.compile(
    r"(?:RFC\s?\d+|PS\s?3\.\d+|TIFF\s6\.0|ISO(?:/IEC)?\s[\d\-:]+|ITU-T\s\S+|ECMA-\d+|ECMA-262|IEEE\s[\d.]+"
    r"|ECMAScript|APPNOTE(?:\.TXT)?|JPEG\sXR|the RFC|OME-NGFF\s\d+\.\d+)(?:'s)?,?\s*$"
)
CHAIN = re.compile(r"^\s*(?:,|;|–|-|—|,?\s*(?:and|or|to))?\s*$")
# A comment's leader on a continued line, dropped when reading what precedes a reference.
LEADER = re.compile(r"\n[ \t]*(?:#+|//[/!]?|\*(?!\*)|--)?[ \t]*")


@dataclass
class Ref:
    path: str
    start: int  # of the "§n"
    end: int
    number: str
    kind: str
    line: int
    qualifier: str | None = None  # the words naming the document, if any
    qstart: int | None = None  # their offset, when they are plain text on this line
    doc: str | None = None  # the document the qualifier names
    part: int | None = None
    target: Heading | None = None
    status: str = ""  # linked, external, ok, review, ambiguous, unresolved, kept
    note: str = ""
    text: str | None = None  # the "§n" to write, if not the one there
    candidates: list[Heading] = field(default_factory=list)


def is_linked_md(text: str, start: int, end: int) -> bool:
    """Whether text[start:end] is in a Markdown link's text."""
    open_ = text.rfind("[", 0, start)
    if open_ < 0 or text.rfind("]", open_, start) >= 0:
        return False
    close = text.find("]", end)
    return close >= 0 and text.startswith("(", close + 1) and "\n\n" not in text[open_:close]


URL_AFTER = re.compile(r"(?:[ \t]|\n[ \t]*(?:#+|//[/!]?|\*|--)?)*<" + re.escape(URL))


def is_linked_code(text: str, end: int) -> bool:
    """Whether a "§n" in code is followed by its URL."""
    return bool(URL_AFTER.match(text, end))


def find_refs(path: str, text: str) -> list[Ref]:
    """Every "§n" in the prose, comments and strings of a file."""
    out: list[Ref] = []
    line_starts = [0] + [m.end() for m in re.finditer(r"\n", text)]
    for r in regions(path, text):
        for m in TOKEN.finditer(text, r.start, r.end):
            line = bisect.bisect_right(line_starts, m.start())
            out.append(Ref(path, m.start(), m.end(), m.group("id"), r.kind, line))
    return out


def preceding(text: str, start: int, back: int = 120) -> str:
    """The text before a reference, without the comment leaders of continued lines."""
    return LEADER.sub(" ", text[max(0, start - back) : start])


# ---------------------------------------------------------------- resolution


def fmt_of_path(path: str) -> str | None:
    """The format whose code (or tests, fixtures, schema) a file is, if any."""
    p = path.replace("ome_zarr", "ome-zarr")
    for f in sorted(DOCS, key=len, reverse=True):
        if re.search(rf"(?:^|/){re.escape(f)}(?:/|\.rs$|\.json$)", p):
            return f
        if re.search(rf"(?:^|/)test_virtualize_{re.escape(f)}\.py$", p):
            return f
    return None


# The default document of a bare reference, by the file it is in: (glob, rule). A
# rule is "self" (the file's own headings), "fmt" (the document of the file's format,
# by fmt_of_path, then spec/virtualize.md), "history:<archive|virtualize>" (the
# snapshot of the round, by the file's rN directory), "spec" (the specification's two
# documents, spec/refkv.md and spec/bindings/vzip.md, numbered as one), or a document.
CONTEXTS: list[tuple[str, str]] = [
    ("spec/refkv.md", "spec"),
    ("spec/bindings/vzip.md", "spec"),
    ("spec/*.md", "self"),
    ("spec/bindings/*.md", "self"),
    ("spec/virtualize/*.md", "self"),
    ("design/ARCHITECTURE.md", "self"),
    ("design/conformance/archive/**", "history:archive"),
    ("design/conformance/virtualize/**", "history:virtualize"),
    ("conformance/impls/virtualize/**", "spec/history/virtualize/VIRTUALIZE.r4.md"),
    ("conformance/impls/**", "spec/history/archive/SPEC.r7.md"),
    ("conformance/vzip/**", "spec"),
    ("conformance/virtualize/**", "spec/virtualize.md"),
    ("design/comparison/**", "spec"),
    ("design/experiments/**", "spec"),
    ("design/*.md", "spec"),
    ("python/src/refkv/virtualize/*/**", "fmt"),
    ("python/src/refkv/virtualize/**", "spec/virtualize.md"),
    ("python/src/refkv/ir/**", "spec/conventions.md"),
    ("python/src/refkv/**", "spec"),
    ("js/src/virtualize/ir/**", "spec/conventions.md"),
    ("js/src/virtualize/*/**", "fmt"),
    ("js/src/virtualize/**", "spec/virtualize.md"),
    ("js/src/**", "spec"),
    ("demo/map/**", "spec/virtualize/safe.md"),
    ("js/conformance/**", "spec/virtualize.md"),
    ("js/test/*/**", "fmt"),
    ("js/test/ir_mirror.py", "spec/conventions.md"),
    ("js/test/store.test.ts", "spec/virtualize.md"),
    ("js/test/chunks.test.ts", "spec/virtualize.md"),
    ("js/test/**", "spec"),
    ("python/tests/ir/**", "spec/conventions.md"),
    ("python/tests/reference/**", "fmt"),
    ("python/tests/test_virtualize_*.py", "fmt"),
    ("python/tests/test_compare.py", "spec/virtualize.md"),
    ("python/tests/**", "spec"),
    ("fixtures/**", "fmt"),
    ("rust/refkv-ir/**", "fmt:spec/conventions.md"),
    ("spec/generate_schemas.py", "spec/virtualize.md"),
    ("docs/**", "spec/virtualize.md"),
    (".github/**", "spec/virtualize.md"),
    ("justfile", "spec/virtualize.md"),
    ("js/README.md", "spec"),
]


def matches(path: str, pattern: str) -> bool:
    """Glob matching where * stays within a directory and a trailing /** is any depth."""
    if pattern.endswith("/**"):
        head = pattern[:-3]
        parts = path.split("/")
        depth = head.count("/") + 1
        return len(parts) > depth and fnmatch("/".join(parts[:depth]), head)
    return fnmatch(path, pattern) and path.count("/") == pattern.count("/")


def excluded(path: str) -> bool:
    return any(matches(path, p) for p in EXCLUDED)


def context(path: str) -> str | None:
    return next((rule for pattern, rule in CONTEXTS if matches(path, pattern)), None)


def history_doc(path: str, which: str) -> str | None:
    m = re.search(r"/r(\d+)/", path)
    if not m:
        return None
    name = "SPEC" if which == "archive" else "VIRTUALIZE"
    doc = f"spec/history/{which}/{name}.r{m.group(1)}.md"
    return doc if (ROOT / doc).is_file() else None


def snapshots(path: str) -> dict[str, str] | None:
    """For a record of a past state, the snapshots in spec/history/ of the documents it
    cited (by their names now, and "HARNESS.md"), else None."""
    m = re.search(r"/r(\d+)/", path)
    if matches(path, "design/conformance/archive/**") and m:
        spec, harness = f"SPEC.r{m.group(1)}", f"HARNESS.r{m.group(1)}"
        which = "archive"
    elif matches(path, "design/conformance/virtualize/**") and m:
        spec, harness, which = f"VIRTUALIZE.r{m.group(1)}", None, "virtualize"
    elif matches(path, "conformance/impls/virtualize/**"):
        spec, harness, which = "VIRTUALIZE.r4", None, "virtualize"
    elif matches(path, "conformance/impls/**"):
        spec, harness, which = "SPEC.r7", "HARNESS.r7", "archive"
    else:
        return None
    out = {}
    if (ROOT / f"spec/history/{which}/{spec}.md").is_file():
        out[f"spec/{which}.md"] = f"spec/history/{which}/{spec}.md"
        if which == "archive":
            out["spec"] = out["spec/archive.md"]
    if harness and (ROOT / f"spec/history/{which}/{harness}.md").is_file():
        out["HARNESS.md"] = out[f"conformance/{which}/HARNESS.md"] = f"spec/history/{which}/{harness}.md"
    return out


@cache
def repo_md_files() -> frozenset[str]:
    out = subprocess.run(["git", "ls-files", "*.md"], cwd=ROOT, capture_output=True, text=True, check=True)
    return frozenset(out.stdout.split())


def _normalize(parts: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for p in parts:
        if p == "..":
            if out:
                out.pop()
        elif p != ".":
            out.append(p)
    return out


def doc_for_name(name: str, path: str) -> str | None:
    """The document a name in the text means: a path from the file or the root, a
    document's old name, or a file name that only one document has."""
    if name in OLD_NAMES:
        return OLD_NAMES[name]
    m = re.fullmatch(r"(?:\.\./)*conventions/([\w-]+)/README\.md", name)
    if m:
        f = m.group(1).replace("_", "-")
        return f"spec/virtualize/{f}.md" if f in DOCS else None
    if re.fullmatch(r"(?:\.\./)*profiles/([\w-]+)\.md", name):
        return "spec/virtualize.md"
    here = PurePosixPath(path).parent
    for cand in (here / name, PurePosixPath(name)):
        norm = "/".join(_normalize(cand.parts))
        if norm in repo_md_files():
            return norm
    hits = sorted(p for p in repo_md_files() if p.endswith("/" + name) and not p.startswith("spec/history/"))
    spec_hits = [p for p in hits if p.startswith("spec/")]
    if len(spec_hits) == 1:
        return spec_hits[0]
    return hits[0] if len(hits) == 1 else None


# The words that name a format's document: its name and its title, lowercase (the registry's).
FORMAT_WORDS = {w: e["name"] for e in REGISTRY for w in (e["name"], e["title"].lower())}
# A format's words, the longest first (so that "mrc-dataset" is not read as "mrc").
_FMT_WORDS = "|".join(re.escape(w) for w in sorted(FORMAT_WORDS, key=len, reverse=True))
_FMT = r"(?:the\s+)?(?:(" + _FMT_WORDS + r")\s+)?"
# A format's title as written ("TIFF", "Zarr v2"), or its name in capitals where its title holds
# it ("SAFE" of "Sentinel-2 SAFE").
_FORMAT_NAMES = sorted({e["title"] for e in REGISTRY}
                       | {e["name"].upper() for e in REGISTRY if re.search(rf"\b{e['name'].upper()}\b", e["title"])},
                       key=len, reverse=True)
Q_PATH = re.compile(r"(?<![\w/.\-\[])(\[?((?:\.{1,2}/)*[\w.\-]+(?:/[\w.\-]+)*\.md)(?:\]\([^()\s]*\))?)(?:'s)?,?\s*$")
Q_OLD = re.compile(r"(?<![\w/])(SPEC|VIRTUALIZE)(?:'s)?\s*$")
Q_CONVENTIONS = re.compile(r"(?<![\w/\-])(conventions)(?:'s?)?\s*$", re.I)
Q_CONVENTION = re.compile(r"(?<![\w/\-])(" + _FMT + r"convention)(?:'s)?\s*$", re.I)
Q_PROFILE = re.compile(r"(?<![\w/\-])(" + _FMT + r"profile)(?:'s)?\s*$", re.I)
# A format's name alone ("TIFF §3"): its convention.
Q_FORMAT = re.compile(r"(?<![\w/\-])(" + "|".join(re.escape(w) for w in _FORMAT_NAMES) + r")\s*$")
Q_SPEC = re.compile(r"(?<![\w/\-])((?:the\s+)?(?:archive\s+)?spec)(?:'s)?\s*$")


def qualify(path: str, text: str, ref: Ref) -> tuple[str | None, str | None, int | None, int | None]:
    """(document, its words, their offset when plain text on the line, part) that the
    words just before a reference name: "external" for another standard, "unknown"
    for a name that is no document, None for none."""
    pre = preceding(text, ref.start)
    line_start = text.rfind("\n", 0, ref.start) + 1

    def offset(m: re.Match[str]) -> int | None:
        # the words' offset in the file, when they are plain text on the reference's line
        q = m.group(1)
        if "[" in q or "]" in q:
            return None
        k = text.rfind(q, line_start, ref.start)
        if k < 0 or text[k + len(q) : ref.start].strip():
            return None
        return k

    if EXTERNAL.search(pre):
        return "external", None, None, None
    m = Q_PATH.search(pre)
    if m:
        doc = doc_for_name(m.group(2), path)
        if doc is None:
            return "unknown", m.group(2), None, None
        return doc, m.group(2), offset(m), None
    m = Q_OLD.search(pre)
    if m:
        return ("spec" if m.group(1) == "SPEC" else "spec/virtualize.md"), m.group(1), offset(m), None
    m = Q_CONVENTIONS.search(pre)
    if m:
        return "spec/conventions.md", m.group(1), offset(m), None
    for q, part in ((Q_CONVENTION, 1), (Q_PROFILE, 2)):
        m = q.search(pre)
        if m:
            f = FORMAT_WORDS.get((m.group(2) or "").lower()) or format_of_doc(path) or fmt_of_path(path)
            return (f"spec/virtualize/{f}.md" if f else "unknown"), m.group(1), offset(m), part
    m = Q_FORMAT.search(pre)
    if m:
        return f"spec/virtualize/{FORMAT_WORDS[m.group(1).lower()]}.md", m.group(1), offset(m), 1
    m = Q_SPEC.search(pre)
    if m:
        return "spec", m.group(1), offset(m), None
    return None, None, None, None


def part_at(path: str, line: int) -> int | None:
    part = None
    for h in headings(path):
        if h.line > line:
            break
        part = h.part
    return part


# The specification: its two documents, whose section numbers are distinct.
SPEC: list[tuple[str, int | None]] = [("spec/refkv.md", None), ("spec/bindings/vzip.md", None)]


def tiers(path: str, ref: Ref) -> list[list[tuple[str, int | None]]]:
    """Where a bare reference may point, tier by tier: the first tier with a heading of
    its number settles it (two headings make it ambiguous)."""
    rule = context(path)
    if rule is None:
        return []
    if rule == "self":
        if format_of_doc(path):
            if part_at(path, ref.line) == 2:
                return [[(path, 2)], [("spec/virtualize.md", None)], [(path, 1)]]
            return [[(path, 1)], [(path, 2)], [("spec/virtualize.md", None)]]
        return [[(path, None)]]
    if rule == "spec":
        return [SPEC]
    if rule.startswith("history:"):
        doc = history_doc(path, rule.split(":", 1)[1])
        return [[(doc, None)]] if doc else []
    if rule.startswith("fmt"):
        # the format's document, else (a file of no format) the fallback, then spec/virtualize.md
        f = fmt_of_path(path)
        if f is None:
            fallback = rule.partition(":")[2]
            return ([[(fallback, None)]] if fallback else []) + [[("spec/virtualize.md", None)]]
        doc = f"spec/virtualize/{f}.md"
        return [[(doc, 1), (doc, 2)], [("spec/virtualize.md", None)]]
    return [[(rule, None)]]


# A format document's example: code does not cite it (its "§6" is the profile's).
EXAMPLE = re.compile(r"^\d+\.? Example$")


def resolve(path: str, text: str, ref: Ref, prev: Ref | None) -> None:
    """Settles a reference's target and status."""
    if is_linked_md(text, ref.start, ref.end) if ref.kind == "md" else is_linked_code(text, ref.end):
        ref.status = "linked"
        return
    if not ref.number[0].isdigit():
        ref.status = "external"
        return
    doc, words, offset, part = qualify(path, text, ref)
    chained = prev is not None and CHAIN.match(LEADER.sub(" ", text[prev.end : ref.start])) is not None
    if doc is None and chained:
        if prev.status == "external":
            doc = "external"
        elif prev.doc is not None:
            doc, part, ref.qualifier = prev.doc, prev.part, prev.qualifier
    if doc == "external":
        ref.status = "external"
        return
    then = snapshots(path)
    if then is not None and doc is not None and words is not None:
        # a record of a past state cites the documents as they were: their snapshots
        name = words if doc == "unknown" else doc
        if name not in then:
            ref.status, ref.note = "unresolved", f"no snapshot of {name!r} of that revision"
            return
        doc = then[name]
    if doc == "unknown":
        ref.status, ref.note = "unresolved", f"no document {words!r}"
        return
    if words is not None:
        ref.qualifier, ref.qstart = words, offset
    ref.doc, ref.part = doc, part
    if doc is not None:
        if doc == "spec":
            order = [SPEC]
        elif format_of_doc(doc) and part is None:
            # a format's document by its path: Part 1 (its profile is cited as spec/virtualize.md §n)
            order: list[list[tuple[str, int | None]]] = [[(doc, 1)], [(doc, 2)]]
        else:
            order = [[(doc, part)]]
    else:
        order = tiers(path, ref)
        if not order:
            ref.status, ref.note = "unresolved", "no default document for this file"
            return
    for i, tier in enumerate(order):
        cands = [
            h for d, p in tier for h in numbered(d, ref.number, p) if ref.kind == "md" or not EXAMPLE.match(h.title)
        ]
        if len(cands) == 1:
            ref.target = cands[0]
            ref.status = "ok" if i == 0 else "review"
            ref.note = "" if i == 0 else f"tier {i + 1}"
            return
        if len(cands) > 1:
            ref.status, ref.candidates = "ambiguous", cands
            ref.note = " | ".join(f"{h.doc}#{h.anchor}" for h in cands)
            return
    ref.status = "unresolved"
    ref.note = f"no heading {ref.number} in " + ", ".join(sorted({d for t in order for d, _ in t}))


# ---------------------------------------------------------------- overrides

OVERRIDES_FILE = HERE / "section_overrides.tsv"


@cache
def overrides() -> dict[tuple[str, int, str, int], tuple[str, str]]:
    """(path, line, "§n", which one of those on the line) -> (target, new text): the
    target "doc#anchor", or "keep" to leave the reference; the new text a "§n" to
    write instead (for a reference to a renumbered section), or ""."""
    out: dict[tuple[str, int, str, int], tuple[str, str]] = {}
    if not OVERRIDES_FILE.is_file():
        return out
    for row in csv.reader(OVERRIDES_FILE.read_text(encoding="utf-8").splitlines(), delimiter="\t"):
        if not row or row[0].startswith("#"):
            continue
        path, line, token, nth, target, new = (row + [""] * 6)[:6]
        out[(path, int(line), token, int(nth))] = (target, new)
    return out


def apply_override(ref: Ref, text: str, nth: int) -> None:
    key = (ref.path, ref.line, text[ref.start : ref.end], nth)
    o = overrides().get(key)
    if o is None:
        return
    target, new = o
    if target == "keep":
        ref.status, ref.target, ref.note = "kept", None, "override: keep"
        return
    doc, anchor = target.split("#", 1)
    h = next((h for h in headings(doc) if h.anchor == anchor), None)
    if h is None:
        raise SystemExit(f"override {key}: no heading #{anchor} in {doc}")
    ref.target, ref.status = h, "ok"
    ref.note = "override" + (f", written {new}" if new else "")
    ref.text = new or None


# ---------------------------------------------------------------- rewriting


def relpath(target: str, frm: str) -> str:
    a = PurePosixPath(target).parts
    b = PurePosixPath(frm).parent.parts
    k = 0
    while k < len(a) - 1 and k < len(b) and a[k] == b[k]:
        k += 1
    return "/".join([".."] * (len(b) - k) + list(a[k:]))


def link_for(ref: Ref) -> str:
    h = ref.target
    assert h is not None
    if ref.kind == "md":
        return "#" + h.anchor if h.doc == ref.path else relpath(h.doc, ref.path) + "#" + h.anchor
    return URL + h.doc + "#" + h.anchor


WIDTH = 100
JSDOC_LINE = re.compile(r"^([ \t]*)/\*\* (.*?) ?\*/$")
COMMENT_LINE = re.compile(r"^([ \t]*(?:#+|//[/!]?|\*)[ \t]?)(?=\S)")


def wrap(line: str, prefix: str, width: int) -> list[str]:
    """A line of comment text wrapped at spaces to `width`, each line after the first
    starting with `prefix`; a word (a URL) is never broken."""
    words = line[len(prefix) :].split(" ")
    out, cur = [], prefix
    for w in words:
        if cur.strip() and cur != prefix and len(cur) + 1 + len(w) > width:
            out.append(cur)
            cur = prefix + w
        else:
            cur = cur + w if cur == prefix else cur + " " + w
    out.append(cur)
    return out


def rewrap(text: str, changed: set[int], docstring_lines: set[int]) -> str:
    """The lines a rewrite lengthened past WIDTH, wrapped when they are comment or
    docstring text alone (a line that also holds code or a string is left as it is).
    The last piece joins the next line when that line continues the same text and
    the two fit."""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if i not in changed or len(line) <= WIDTH:
            out.append(line)
            i += 1
            continue
        jsdoc = JSDOC_LINE.match(line)
        if jsdoc:
            # a one-line JSDoc comment becomes a block of lines
            indent = jsdoc.group(1)
            out.append(indent + "/**")
            out.extend(wrap(indent + " * " + jsdoc.group(2), indent + " * ", WIDTH))
            out.append(indent + " */")
            i += 1
            continue
        m = COMMENT_LINE.match(line)
        if m and not line.lstrip().startswith("/**"):
            prefix = m.group(1)
            if prefix.strip() == "*" and not prefix.endswith(" "):
                prefix += " "
        elif i in docstring_lines:
            prefix = line[: len(line) - len(line.lstrip())]
        else:
            out.append(line)
            i += 1
            continue
        pieces = wrap(line, prefix, WIDTH)
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        if len(pieces) > 1 and nxt is not None and i + 1 not in changed and nxt.startswith(prefix):
            rest = nxt[len(prefix) :]
            tail = pieces[-1][len(prefix) :]
            if (
                rest
                and rest[0] not in " -*|>`#0123456789"
                and "*/" not in nxt
                and '"""' not in tail
                and "'''" not in tail
                and len(prefix) + len(tail) + 1 + len(rest) <= WIDTH
            ):
                lines[i + 1] = prefix + tail + " " + rest
                pieces.pop()
        out.extend(pieces)
        i += 1
    return "\n".join(out)


def rewrite(text: str, refs: list[Ref]) -> str:
    """The text with each settled reference made a link: in Markdown a relative link,
    elsewhere the URL after the reference (and a comment line it makes too long
    wrapped)."""
    edits: list[tuple[int, int, str]] = []
    for ref in refs:
        if ref.status not in ("ok", "review") or ref.target is None:
            continue
        token = ref.text or text[ref.start : ref.end]
        if ref.kind == "md":
            s = ref.qstart if ref.qstart is not None else ref.start
            edits.append((s, ref.end, f"[{text[s : ref.start]}{token}]({link_for(ref)})"))
        else:
            edits.append((ref.start, ref.end, f"{token} <{link_for(ref)}>"))
    changed: set[int] = set()
    docstring: set[int] = set()
    for ref in refs:
        if ref.kind != "md" and ref.status in ("ok", "review") and ref.target is not None:
            changed.add(ref.line - 1)
            if ref.kind == "docstring":
                docstring.add(ref.line - 1)
    for s, e, new in sorted(edits, reverse=True):
        text = text[:s] + new + text[e:]
    return rewrap(text, changed, docstring) if changed else text


def analyze(path: str, text: str) -> list[Ref]:
    """Every reference of a file, resolved."""
    refs = find_refs(path, text)
    prev = None
    seen: dict[tuple[int, str], int] = {}
    for ref in refs:
        resolve(path, text, ref, prev)
        tok = text[ref.start : ref.end]
        nth = seen.get((ref.line, tok), 0)
        seen[(ref.line, tok)] = nth + 1
        if ref.status not in ("linked", "external"):
            apply_override(ref, text, nth)
        prev = ref
    return refs


def files_with_refs() -> list[str]:
    out = subprocess.run(["git", "grep", "-lI", "§"], cwd=ROOT, capture_output=True, text=True)
    return sorted(out.stdout.split())


def migrate(args: argparse.Namespace) -> int:
    rows = []
    for path in args.paths or files_with_refs():
        if any(matches(path, p) for p in GENERATED + LEFT_ALONE):
            continue
        text = (ROOT / path).read_text(encoding="utf-8")
        refs = analyze(path, text)
        for ref in refs:
            ls = text.rfind("\n", 0, ref.start) + 1
            le = text.find("\n", ref.end)
            rows.append(
                [
                    path,
                    ref.line,
                    ref.kind,
                    text[ref.start : ref.end],
                    ref.qualifier or "",
                    ref.status,
                    f"{ref.target.doc}#{ref.target.anchor}" if ref.target else "",
                    ref.target.title if ref.target else "",
                    ref.note,
                    text[ls : le if le >= 0 else None].strip()[:400],
                ]
            )
        if args.write:
            new = rewrite(text, refs)
            if new != text:
                (ROOT / path).write_text(new, encoding="utf-8")
    if args.report:
        with open(args.report, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh, delimiter="\t")
            w.writerow(["path", "line", "kind", "token", "qualifier", "status", "target", "title", "note", "text"])
            w.writerows(rows)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r[5]] = counts.get(r[5], 0) + 1
    print(" ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 0


# ---------------------------------------------------------------- the check

MD_LINK = re.compile(r"\[(?:[^\[\]]|\[[^\]]*\])*\]\(([^()\s]+)(?:\s+\"[^\"]*\")?\)")
FULL_URL = re.compile(re.escape(URL) + r"([^\s#<>()\[\]\"'`]+)#([\w\-]+)")
TEXT_SUFFIXES = {".md", ".py", ".ts", ".mjs", ".js", ".rs", ".json", ".yml", ".yaml", ".txt", ".sh", ".toml"}


def bare_refs(path: str, text: str) -> list[Ref]:
    """A file's references that are not links (other standards' aside)."""
    out = []
    prev_external = None
    for ref in find_refs(path, text):
        linked = is_linked_md(text, ref.start, ref.end) if ref.kind == "md" else is_linked_code(text, ref.end)
        external = (
            not ref.number[0].isdigit()
            or EXTERNAL.search(preceding(text, ref.start)) is not None
            or (
                prev_external is not None
                and CHAIN.match(LEADER.sub(" ", text[prev_external.end : ref.start])) is not None
            )
        )
        prev_external = ref if external else None
        if not (linked or external):
            out.append(ref)
    return out


def broken_links(path: str, text: str) -> list[tuple[int, str, str]]:
    """(line, link, why) for each link to a document of this repository, or a heading
    of one, that does not exist."""
    out = []

    def line_of(i: int) -> int:
        return text.count("\n", 0, i) + 1

    if path.endswith(".md"):
        prose = md_regions(text)
        for m in MD_LINK.finditer(text):
            if not any(r.start <= m.start() < r.end for r in prose):
                continue
            target = m.group(1)
            if re.match(r"^[a-z][\w+.-]*:", target) or "#" not in target:
                continue
            file, anchor = target.split("#", 1)
            doc = path if not file else "/".join(_normalize((PurePosixPath(path).parent / file).parts))
            if not doc.endswith(".md"):
                continue
            if not (ROOT / doc).is_file():
                out.append((line_of(m.start()), target, f"no file {doc}"))
            elif anchor not in anchors(doc):
                out.append((line_of(m.start()), target, f"no heading #{anchor} in {doc}"))
    for m in FULL_URL.finditer(text):
        doc, anchor = m.group(1), m.group(2)
        if not (ROOT / doc).is_file():
            out.append((line_of(m.start()), m.group(0), f"no file {doc}"))
        elif doc.endswith(".md") and anchor not in anchors(doc):
            out.append((line_of(m.start()), m.group(0), f"no heading #{anchor} in {doc}"))
    return out


def check_files(paths: list[str]) -> list[str]:
    problems = []
    for path in paths:
        try:
            text = (ROOT / path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        if not excluded(path):
            for ref in bare_refs(path, text):
                problems.append(f"{path}:{ref.line}: bare reference {text[ref.start : ref.end]!r}")
        if not matches(path, "spec/history/**"):
            for line, link, why in broken_links(path, text):
                problems.append(f"{path}:{line}: broken link {link} ({why})")
    return problems


def check(args: argparse.Namespace) -> int:
    paths = args.paths
    if not paths:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True)
        paths = [
            p
            for p in out.stdout.splitlines()
            if PurePosixPath(p).suffix in TEXT_SUFFIXES or PurePosixPath(p).name == "justfile"
        ]
    problems = check_files(paths)
    for p in problems:
        print(p, file=sys.stderr)
    if problems:
        print(
            f"sections: {len(problems)} problem(s). Cite a section as a link to its heading "
            "(spec/tools/sections.py says how).",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Section references: a check, and the conversion to links.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="fail on bare references and on links to missing headings")
    c.add_argument("paths", nargs="*", help="the files (default: every tracked text file)")
    m = sub.add_parser("migrate", help="rewrite bare references as links")
    m.add_argument("paths", nargs="*", help="the files (default: every file with a §)")
    m.add_argument("--write", action="store_true", help="rewrite the files (else report only)")
    m.add_argument("--report", help="a TSV of every reference and its heading")
    args = ap.parse_args(argv)
    return check(args) if args.cmd == "check" else migrate(args)


if __name__ == "__main__":
    sys.exit(main())
