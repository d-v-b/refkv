"""Section references (§n) in refkv: links to the headings they mean, and a check.

    uv run python spec/tools/sections.py check [paths...]

A reference to a section of a document of this repository is a link to that
section's heading, never a bare "§n":

- in Markdown, a relative link to the heading's anchor, its text keeping the
  "§n": `[§6.7](#67-reader-policy)`, `[spec/refkv.md §6.7](refkv.md#67-reader-policy)`;
- in code (comments and docstrings, and the descriptions in the JSON schemas and
  source models), the "§n" followed by the heading's URL in angle brackets:
  `spec/refkv.md §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>`.

`check` fails on a bare reference outside the records of past states (EXCLUDED),
and on a link (relative, or a full URL of this repository) to a document or
heading that does not exist. A reference to another standard ("RFC 9110 §5.6.7",
"PS3.5 §6.2.2") is not one to this repository, and stays as it is.
"""

from __future__ import annotations

import argparse
import bisect
import io
import re
import subprocess
import sys
import tokenize
from dataclasses import dataclass
from fnmatch import fnmatch
from functools import cache
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
URL = "https://github.com/d-v-b/refkv/blob/main/"

# Records of past states: their references cite the documents as they were then, so
# a bare one is allowed.
EXCLUDED = (
    "spec/history/**",
    "spec/tools/**",  # this tool, whose examples and tests are bare references
    "python/tests/reference/**",
    "design/conformance/**",
    "conformance/impls/**",
    "conformance/zarrify/reference/**",
    "design/IR_NOTES.md",
    "design/plans/**",
    "CHANGELOG.md",  # release-please writes it from the merged PRs' titles
)

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
    holes = [(m.start(), m.end()) for m in re.finditer(r"(`+)(?:(?!\1).)+?\1|<!--.*?-->", text, re.DOTALL)]
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
            assert m is not None  # c is a word character
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


CODE_SPAN = re.compile(r"(`+)(?!`).+?(?<!`)\1(?!`)", re.DOTALL)


def blank_code_spans(text: str) -> str:
    """`text` with each inline code span's characters (its backticks included) replaced by
    spaces, so that a bracket in code (the interval `[0, size)`) is not read as a link's."""
    return CODE_SPAN.sub(lambda m: " " * len(m.group(0)), text)


def is_linked_md(text: str, start: int, end: int) -> bool:
    """Whether text[start:end] is in a Markdown link's text (brackets in code spans aside)."""
    text = blank_code_spans(text)
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


# ---------------------------------------------------------------- paths


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


def _normalize(parts: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for p in parts:
        if p == "..":
            if out:
                out.pop()
        elif p != ".":
            out.append(p)
    return out


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
            problems += [f"{path}:{ref.line}: bare reference {text[ref.start : ref.end]!r}"
                         for ref in bare_refs(path, text)]
        if not matches(path, "spec/history/**"):
            for line, link, why in broken_links(path, text):
                problems.append(f"{path}:{line}: broken link {link} ({why})")
    return problems


def check(args: argparse.Namespace) -> int:
    paths = args.paths
    if not paths:
        # every file git tracks, and every one it would (new, not ignored): a file written and not
        # yet added is checked before the commit that adds it, not by CI after it
        listed = ["git", "ls-files", "--cached", "--others", "--exclude-standard"]
        out = subprocess.run(listed, cwd=ROOT, capture_output=True, text=True, check=True)
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
    ap = argparse.ArgumentParser(description="Section references: a check.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="fail on bare references and on links to missing headings")
    c.add_argument("paths", nargs="*", help="the files (default: every text file git tracks or would)")
    return check(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
