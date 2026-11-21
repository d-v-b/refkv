"""The cross-format blocks of the documents, rendered when the documentation site is built
(docs-site/, `just docs`), and the links of the Markdown the site renders.

    uv run python spec/tools/blocks.py check    # fail on a block no renderer has
    uv run python spec/tools/blocks.py show <block> [page]   # print a block as the site renders it

A document marks a block where the site renders one, by a pair of marker comments:

    <!-- profiles:<block> -->
    A sentence for the reader on GitHub, which the site replaces.
    <!-- /profiles -->

What the site writes in its place is made, at build time, from the profile registry
(spec/zarrify/profiles/<name>.json) and each format's own files: its document
(spec/zarrify/<name>.md), its stored arrays (spec/zarrify/<name>/stored-arrays.md) and its
user's README (docs/zarrify/<name>/README.md). No list of formats is committed, so adding or
changing a format edits only that format's files. `check` (and spec/tools/test_blocks.py) renders
every block of every document, so that a block the site would fail on fails `just sections` too,
and fails on a renderer whose block no document marks.

Each renderer takes `link`, which gives the relative link from the page being rendered to a path
of the repository (with an anchor or not): a block renders the same wherever it is placed.
"""

from __future__ import annotations

import posixpath
import re
import sys
from collections.abc import Callable
from pathlib import Path

import profiles

ROOT = Path(__file__).resolve().parents[2]
ZARRIFY = ROOT / "spec" / "zarrify"
# a pair of marker comments and what is between them
BLOCK = re.compile(r"(<!-- profiles:([\w-]+) -->\n)(.*?)(\n[ \t]*<!-- /profiles -->)", re.DOTALL)
# a format document's section titled "The description" (python/tests/test_description_documented.py)
DESCRIPTION = re.compile(r"^#{2,4} ([\d.]+ The description)$", re.MULTILINE)

Link = Callable[[str], str]


def linker(page: str) -> Link:
    """The links from the document at `page` (a path of the repository) to paths of the repository."""
    here = posixpath.dirname(page)

    def link(target: str) -> str:
        path, hash_, anchor = target.partition("#")
        if path == page and anchor:
            return f"#{anchor}"
        return posixpath.relpath(path, here or ".") + hash_ + anchor

    return link


def document(e: dict) -> str:
    """The path of an entry's document."""
    return f"spec/zarrify/{e['name']}.md"


def heading_link(e: dict, anchor: str, link: Link) -> str:
    """A link to the heading of the profile's document with this anchor, its text `<p>.md §n`."""
    doc = document(e)
    for heading in re.findall(r"^#+ (.+)$", (ROOT / doc).read_text(encoding="utf-8"), re.MULTILINE):
        if profiles.slug(heading) == anchor:
            return f"[{e['name']}.md §{heading.split()[0].rstrip('.')}]({link(f'{doc}#{anchor}')})"
    raise ValueError(f"{e['name']}: spec/zarrify/{e['name']}.md has no heading with the anchor {anchor!r}")


def profile_link(e: dict, link: Link) -> str:
    """A link to the profile's heading, the first of its document's Part 2."""
    n, heading = profiles.profile_heading(e)
    return heading_link(e, f"{n}-{profiles.slug(heading)}", link)


def spec_table(entries: list[dict], link: Link) -> str:
    """Each profile, its input and what it reads."""
    rows = ["| profile | input | inputs |", "|---|---|---|"]
    for e in profiles.profiles(entries):
        part2 = link(document(e) + "#part-2-the-profile")
        rows.append(f"| [{e['title']}]({part2}) | {profiles.input_kind(e)} | {e['inputs']} |")
    return "\n".join(rows)


def spec_list(entries: list[dict], link: Link) -> str:
    """Each profile's heading, in prose."""
    items = []
    for e in profiles.profiles(entries):
        n, heading = profiles.profile_heading(e)
        target = link(f"spec/zarrify/{e['name']}.md#{n}-{profiles.slug(heading)}")
        items.append(f"- the {heading}: [spec/zarrify/{e['name']}.md §{n}]({target})")
    return ";\n".join(items) + "."


def sniff_table(entries: list[dict], link: Link) -> str:
    """The file profiles with a signature, and its precedence."""
    rows = ["| profile | `sniff` |", "|---|---|"]
    rows += [f"| {e['title']} ({profile_link(e, link)}) | {e['sniff']} |" for e in entries if e["sniff"] is not None]
    return "\n".join(rows)


def beside_table(entries: list[dict], link: Link) -> str:
    """The profiles that read a file with the files beside it."""
    rows = ["| profile | read as a store | the named file | specified in |", "|---|---|---|---|"]
    forms = {"always": "always", "on_demand": "on demand"}
    rows += [f"| {e['title']} | {forms[run['form']]} | {run['named']} | {heading_link(e, run['section'], link)} |"
             for e in entries if (run := e["store_run"]) is not None and run["form"] != "listed"]
    return "\n".join(rows)


def conventions_table(entries: list[dict], link: Link) -> str:
    """Each convention, a profile's or a dataset layout's, and its UUID."""
    rows = ["| format | convention | UUID |", "|---|---|---|"]
    layouts = link("spec/conventions.md#9-dataset-layouts")
    for e in entries:
        source = e["source"] + (f" ([§9]({layouts}))" if e["kind"] == "layout" else "")
        rows.append(f"| {source} | [{e['name']}]({link(document(e))}) | `{e['uuid']}` |")
    return "\n".join(rows)


def docs_table(entries: list[dict], link: Link) -> str:
    """Each profile, who it is for, its user's README and its specification."""
    rows = ["| format | for | README | profile |", "|---|---|---|---|"]
    for e in profiles.profiles(entries):
        f = e["name"]
        readme = (f"[{f}]({link(f'docs/zarrify/{f}/README.md')})"
                  if (ROOT / "docs" / "zarrify" / f / "README.md").is_file()
                  else f"[convention]({link(f'spec/zarrify/{f}.md')})")
        rows.append(f"| {e['title']} | {e['for']} | {readme} | {profile_link(e, link)} |")
    return "\n".join(rows)


def stored_arrays(entries: list[dict], link: Link) -> str:
    """Each profile's stored arrays (spec/zarrify/<p>/stored-arrays.md), whole, its headings two
    levels down, below a section of level 2, and its links made from here."""
    parts = []
    for e in profiles.profiles(entries):
        path = f"spec/zarrify/{e['name']}/stored-arrays.md"
        text = (ROOT / path).read_text(encoding="utf-8")
        text = map_links(text, lambda t, path=path: rebase(t, path, link))
        parts.append(re.sub(r"^(#+) ", r"\1## ", text.strip(), flags=re.MULTILINE))
    return "\n\n".join(parts)


def descriptions(entries: list[dict], link: Link) -> str:
    """The profiles whose document has a section titled "The description", and the JSON Schema
    of the description (spec/zarrify/<p>/description.json) where there is one."""
    items = []
    for e in profiles.profiles(entries):
        doc = f"spec/zarrify/{e['name']}.md"
        m = DESCRIPTION.search((ROOT / doc).read_text(encoding="utf-8"))
        if m is None:
            continue
        n = m.group(1).split()[0].rstrip(".")
        item = f"- {e['title']}: [{e['name']}.md §{n}]({link(f'{doc}#{profiles.slug(m.group(1))}')})"
        schema = f"spec/zarrify/{e['name']}/description.json"
        if (ROOT / schema).is_file():
            item += f", and its JSON Schema, [{e['name']}/description.json]({link(schema)})"
        items.append(item)
    if not items:
        return "No profile returns a description yet."
    return ";\n".join(items) + "."


RENDERERS: dict[str, Callable[[list[dict], Link], str]] = {
    "spec-table": spec_table,
    "spec-list": spec_list,
    "sniff-table": sniff_table,
    "beside-table": beside_table,
    "conventions-table": conventions_table,
    "docs-table": docs_table,
    "stored-arrays": stored_arrays,
    "descriptions": descriptions,
}


def render(text: str, page: str, entries: list[dict]) -> str:
    """`text`, the document at `page`, with each block's content replaced by its renderer's."""
    link = linker(page)

    def one(m: re.Match[str]) -> str:
        make = RENDERERS.get(m.group(2))
        if make is None:
            raise ValueError(f"{page}: no renderer of the block {m.group(2)!r} (spec/tools/blocks.py)")
        return m.group(1) + make(entries, link) + m.group(4)

    return BLOCK.sub(one, text)


# Markdown's links, outside code: a fenced code block, an inline code span, an inline link or
# image (its target, then an optional title), or a link reference definition
LINKS = re.compile(
    r"(?P<fence>^[ \t]*(?P<f>`{3,}|~{3,})[^\n]*\n.*?^[ \t]*(?P=f)[ \t]*$)"
    r"|(?P<code>(?P<ticks>`+).+?(?<!`)(?P=ticks)(?!`))"
    r"|(?P<inline>\]\()(?P<target><[^>\n]*>|[^)\s]+)"
    r"|(?P<ref>^[ \t]{0,3}\[[^\]\n]+\]:[ \t]*)(?P<reftarget>\S+)",
    re.MULTILINE | re.DOTALL,
)


def map_links(text: str, fn: Callable[[str], str]) -> str:
    """`text` with each link's target `t` outside code replaced by `fn(t)`."""

    def one(m: re.Match[str]) -> str:
        if m.group("inline"):
            return m.group("inline") + fn(m.group("target"))
        if m.group("ref"):
            return m.group("ref") + fn(m.group("reftarget"))
        return m.group(0)

    return LINKS.sub(one, text)


def is_relative(target: str) -> bool:
    """Whether a link's target is a path of this repository's, not a URL or an anchor alone."""
    return not (target.startswith(("#", "/", "<")) or re.match(r"[a-zA-Z][a-zA-Z0-9+.-]*:", target))


def resolve(target: str, page: str) -> str:
    """The path of the repository a relative link from the document at `page` names, with its anchor."""
    path, hash_, anchor = target.partition("#")
    return posixpath.normpath(posixpath.join(posixpath.dirname(page), path)) + hash_ + anchor


def rebase(target: str, source: str, link: Link) -> str:
    """A relative link of the document at `source`, made from the page that `link` links from."""
    return link(resolve(target, source)) if is_relative(target) else target


def check(texts: dict[str, str] | None = None) -> list[str]:
    """Each document of `texts` (each document's path and Markdown; by default, every document
    profiles.documents() names) whose blocks do not render, and why, and each renderer whose block
    no document marks, as a marker that lost its form would leave it."""
    entries = profiles.load()
    out = []
    used: set[str] = set()
    for path, text in (profiles.documents() if texts is None else texts).items():
        used |= {m.group(2) for m in BLOCK.finditer(text)}
        try:
            render(text, path, entries)
        except (ValueError, OSError) as e:
            out.append(f"{path}: {e}")
    out += [f"no document marks the block {name!r}" for name in RENDERERS if name not in used]
    return out


def main(argv: list[str]) -> int:
    if argv[:1] == ["check"]:
        errors = check()
        for error in errors:
            print(error, file=sys.stderr)
        return 1 if errors else 0
    if argv[:1] == ["show"] and len(argv) in (2, 3):
        page = argv[2] if len(argv) == 3 else "spec/zarrify.md"
        print(RENDERERS[argv[1]](profiles.load(), linker(page)))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
