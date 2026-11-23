"""refkv's documentation site: Sphinx, reading Markdown with MyST (`just docs`, which runs
`sphinx-build -c docs-site . docs-site/_build`).

The site renders the repository's Markdown where it is, at the same paths: README.md, docs/ and
spec/ (`include_patterns`), so that every relative link between them, and every link to a
section's heading (spec/tools/sections.py), works on the site as it does on GitHub: MyST gives
each heading the anchor GitHub gives it. As Sphinx reads each document (`read`, on its `source-read` event), it:

- renders the document's cross-format blocks (`<!-- profiles:<block> -->`) from the profile
  registry (spec/zarrify/profiles/<name>.json) and each format's own files (spec/tools/blocks.py),
  so that no list of formats is committed;
- points a link to a file the site does not render (code, a schema, a directory with no README)
  at that file on GitHub, and leaves a link to a path that does not exist for MyST to report
  (but in a record of a past state, and a format's changelog pointer before its first change);
- adds the navigation (`toctree`): the README lists the guides (docs/*.md), the specification
  (spec/*.md and its bindings) and each entry of the registry's document; a format's document
  lists its user's README and its stored arrays.
"""

from __future__ import annotations

import posixpath
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from sphinx.application import Sphinx
from sphinx.errors import ExtensionError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "spec" / "tools"))

import blocks  # noqa: E402 (spec/tools, put on the path above)
import profiles  # noqa: E402
import sections  # noqa: E402

REPO = "https://github.com/d-v-b/refkv"
# an HTML anchor, as the documents mark a definition in a paragraph (spec/tools/sections.py reads them)
ANCHOR = re.compile(r"""<a\s+(?:id|name)=["']([^"']+)["']>\s*</a>""")

project = "refkv"
author = "the refkv authors"
copyright = "the refkv authors"  # noqa: A001 (Sphinx's name)
# githubpages: the .nojekyll file, so that GitHub Pages, serving the gh-pages branch, serves _static/
extensions = ["myst_parser", "sphinx.ext.githubpages"]
source_suffix = {".md": "markdown"}
root_doc = "README"
# the Markdown the site renders: the README, and every document of docs/ and spec/
include_patterns = ["README.md", "docs/**.md", "spec/**.md"]
exclude_patterns = ["docs-site/_build"]
# anchors on headings of every level, as GitHub's, for `file.md#anchor` links
myst_heading_anchors = 6
# `[]{#name}`: the target an HTML anchor of the documents becomes (`page`)
myst_enable_extensions = ["strikethrough", "attrs_inline"]
# the records of past states and each format's changelog are linked to, not in the navigation;
# a link to a heading that does not exist stays a warning, which `just docs` (-W) fails on
# a JSON example that elides with "..." is not JSON, and stays unhighlighted
suppress_warnings = ["toc.not_included", "misc.highlighting_failure"]

html_theme = "furo"
html_title = "refkv"
html_baseurl = "https://d-v-b.github.io/refkv/"
# index.html, which sends the site's root to its root document, README.html
html_extra_path = ["extra"]
html_theme_options = {
    # the pages are the repository's own files at their own paths: GitHub's editor is at the same path
    "source_repository": f"{REPO}/",
    "source_branch": "main",
    "source_directory": "",
}


def github(path: str, anchor: str) -> str:
    """The URL of a path of the repository on GitHub's main branch."""
    if path == ".":
        return REPO
    kind = "tree" if (ROOT / path).is_dir() else "blob"
    return f"{REPO}/{kind}/main/{path}{anchor}"


def pending_changes(path: str) -> bool:
    """Whether `path` is the changes directory of a format's document that records no change yet,
    which its changelog pointer names all the same (spec/tools/changelog.py)."""
    head, name = posixpath.split(path)
    return head == "spec/zarrify/changes" and (ROOT / "spec" / "zarrify" / f"{name}.md").is_file()


def documents(pattern: str) -> list[str]:
    """The documents matching `pattern`, as docnames, a directory's README first."""
    paths = [p.relative_to(ROOT).with_suffix("").as_posix() for p in ROOT.glob(pattern)]
    return sorted(paths, key=lambda p: (posixpath.basename(p) != "README", p))


def toctree(caption: str | None, docnames: list[str]) -> str:
    """A hidden toctree of these documents (absolute docnames)."""
    options = ":hidden:\n" + (f":caption: {caption}\n" if caption else "")
    entries = "".join(f"/{d}\n" for d in docnames)
    return f"\n\n```{{toctree}}\n{options}\n{entries}```\n"


def navigation(docname: str, entries: list[dict[str, Any]]) -> str:
    """The toctrees of a document: the README's sections, and a format document's own files."""
    if docname == root_doc:
        formats = [f"spec/zarrify/{e['name']}" for e in entries]
        return (toctree("Guides", documents("docs/*.md"))
                + toctree("Specification", documents("spec/*.md") + documents("spec/bindings/*.md"))
                + toctree("Formats", formats))
    for e in entries:
        if docname == f"spec/zarrify/{e['name']}":
            own = [f"docs/zarrify/{e['name']}/README", f"spec/zarrify/{e['name']}/stored-arrays"]
            return toctree(None, [d for d in own if (ROOT / f"{d}.md").is_file()])
    return ""


def page(text: str, here: str, docname: str, sources: set[str]) -> str:
    """The document at `here` as the site renders it: its blocks rendered, its links to files the site
    does not render made GitHub's, and its navigation added."""
    entries = profiles.load()
    link = blocks.linker(here)

    def fix(target: str) -> str:
        if not blocks.is_relative(target):
            return target
        path, hash_, anchor = blocks.resolve(target, here).partition("#")
        if path in sources or path.startswith(".."):
            return target
        # a path that does not exist stays as it is, which MyST reports, but in a record of a past
        # state, whose links cite the documents as they were and which sections.py does not check,
        # and a format's changelog pointer before its first change (spec/tools/changelog.py)
        if not (ROOT / path).exists() and not sections.excluded(here) and not pending_changes(path):
            return target
        if f"{path}/README.md" in sources:
            return link(f"{path}/README.md{hash_}{anchor}")
        return github(path, hash_ + anchor)

    text = ANCHOR.sub(r"[]{#\1}", blocks.render(text, here, entries))
    return blocks.map_links(text, fix) + navigation(docname, entries)


def read(app: Sphinx, docname: str, source: list[str]) -> None:
    sources = {posixpath.normpath(app.env.doc2path(d, base=False).as_posix()) for d in app.env.found_docs}
    here = app.env.doc2path(docname, base=False).as_posix()
    source[0] = page(source[0], here, docname, sources)


def publish_schemas(app: Sphinx, exception: Exception | None) -> None:
    """Serve every URL a version-0 CMO names on the site (spec/conventions.md §1
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#1-conventions>), or fail the
    build: its `schema_url`, to which this copies the convention's JSON Schema from the repository
    (spec/zarrify/<p>/schema.json, a path to update when the schemas move), and its `spec_url`, the
    format's page, which a move of its document must keep, by a redirect page or otherwise. The
    paths come from `profiles.cmo`, the CMO refkv writes, at version 0 whatever a format's current
    version: the archives written at version 0 name them for good."""
    if exception is not None or app.builder.format != "html":
        return
    missing = []
    for e in profiles.load():
        cmo = profiles.cmo({**e, "version": 0})
        schema, page = (str(cmo[k]).removeprefix(profiles.SITE + "/") for k in ("schema_url", "spec_url"))
        target = Path(app.outdir) / schema
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "spec" / "zarrify" / e["name"] / "schema.json", target)
        if not (Path(app.outdir) / page).is_file():
            missing.append(str(cmo["spec_url"]))
    if missing:
        raise ExtensionError(f"the site does not serve these CMO URLs: {', '.join(missing)}")


def setup(app: Sphinx) -> dict[str, bool]:
    app.connect("source-read", read)
    app.connect("build-finished", publish_schemas)
    return {"parallel_read_safe": True}
