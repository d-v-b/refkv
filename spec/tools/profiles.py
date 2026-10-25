"""The profile registry, spec/zarrify/profiles/<name>.json (one file per profile), and what
derives from it.

    uv run python spec/tools/profiles.py          # write the derived files
    uv run python spec/tools/profiles.py check    # fail if any derived file is stale, or a CMO copy
    uv run python spec/tools/profiles.py sample   # one accepted fixture of each file profile

The registry is the one place a profile's identity is written, one file per profile so that
two profiles added at once never touch the same file: its name (the file's), its convention's
title, UUID and version, its kind (a file input, a store input, both, or a dataset layout,
which is a convention but no profile), the precedence of its signature among the file
profiles' (`sniff`, lower first, ties by name; null for none), for a file profile with none the
profile whose reading chooses it (`chosen_by`: the TIFF profile's chooses NDPI and QPTIFF), the
suffixes of its fixture files, the revision its frozen reference twin records (null: none), and
the phrases the indexes describe it by. Everything else is derived from it:

- at build or load time, by each host, from the files themselves: the Rust core's
  `build.rs` (from its copies, `rust/refkv-ir/profiles/<name>.json`, which this writes: the
  crate cannot include a file outside itself in its package), which the Python host reads
  through `refkv._core.registry`; the browser code's `#profiles` (js/src/zarrify/); and the
  demo page's formats sentence, examples and credits (js/build.mjs, from each profile's
  demo/examples/<name>.html);
- here, the index pages of the documents, where the profiles' own documents come together:
  the profile tables of spec/zarrify.md, spec/conventions.md and docs/zarrify/README.md,
  each between a pair of marker comments (`<!-- profiles:<block> -->` and `<!-- /profiles -->`),
  one row per profile, by name, so that two profiles added at once conflict only when their
  names sort next to each other;
- and the Python package's source and store classes, one per profile
  (python/src/refkv/zarrify/profile_classes.py, written whole, which refkv.zarrify re-exports):
  code, so that a type checker reads them.

Adding a format is its registry file and its own files; `just profiles` then regenerates the
indexes and the classes. `spec/tools/test_profiles.py` checks that the derived files are current (`just
sections` runs it).

The documents also write out each convention's metadata object (CMO, spec/conventions.md §1
<https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#1-conventions>), which is
derived from the registry but copied by hand, to be read in place. `check` checks the copies:
every CMO of refkv's conventions in spec/ (but its history, spec/history/) and docs/ MUST be
the one the registry gives (`cmo`), and each format's document MUST show its own in its
declaration, its section 1. `cmo` is what refkv writes, `refkv.zarrify.common.convention`,
as python/tests/test_zarrify_conventions.py checks (this tool does not import refkv, whose
Rust core it would have to build).
"""

from __future__ import annotations

import contextlib
import json
import re
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REGISTRY_DIR = ROOT / "spec" / "zarrify" / "profiles"
COPY_DIR = ROOT / "rust" / "refkv-ir" / "profiles"
KINDS = ("file", "store", "both", "layout")
CONVENTION_KEY = "refkv_zarrified"
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# A flat JSON object with a member "uuid", as a CMO is written (its members are strings).
CMO_TEXT = re.compile(r'\{[^{}]*"uuid"\s*:[^{}]*\}')
FIELDS = ("name", "title", "uuid", "version", "kind", "sniff", "chosen_by", "suffixes", "reference", "source", "inputs",
          "for")


def load() -> list[dict]:
    """The registry's entries, by name, validated."""
    entries = []
    for path in sorted(REGISTRY_DIR.glob("*.json")):
        e = json.loads(path.read_text(encoding="utf-8"))
        if e.get("name") != path.stem:
            raise ValueError(f"{path.name}: the file is not named for its profile {e.get('name')!r}")
        entries.append(e)
    return validate(sorted(entries, key=lambda e: e["name"]))


def validate(entries: list[dict]) -> list[dict]:
    for e in entries:
        if tuple(e) != FIELDS:
            raise ValueError(f"{e.get('name')}: the members must be {FIELDS}, in that order")
        if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", e["name"]):
            raise ValueError(f"{e['name']!r}: not a profile name")
        if e["kind"] not in KINDS:
            raise ValueError(f"{e['name']}: kind {e['kind']!r} is not one of {KINDS}")
        if e["sniff"] is not None and (type(e["sniff"]) is not int or e["kind"] not in ("file", "both")):
            raise ValueError(f"{e['name']}: only a file profile has a signature, whose precedence is an integer")
        if (e["chosen_by"] is None) != (e["sniff"] is not None or e["kind"] != "file"):
            raise ValueError(f"{e['name']}: a file profile with no signature names the profile that chooses it, "
                             "and no other profile does")
        if bool(e["suffixes"]) != (e["kind"] in ("file", "both")):
            raise ValueError(f"{e['name']}: a file input has suffixes, a store or layout none")
        if not UUID.fullmatch(e["uuid"]):
            raise ValueError(f"{e['name']}: {e['uuid']!r} is not a UUID")
    signed = {e["name"] for e in entries if e["sniff"] is not None}
    for e in entries:
        if e["chosen_by"] is not None and e["chosen_by"] not in signed:
            raise ValueError(f"{e['name']}: chosen by {e['chosen_by']!r}, which is not a profile with a signature")
    for key in ("name", "uuid"):
        values = [e[key] for e in entries]
        if len(set(values)) != len(values):
            raise ValueError(f"two entries share a {key}")
    return entries


def profiles(entries: list[dict]) -> list[dict]:
    """The profiles (the entries that are not dataset layouts), by name."""
    return [e for e in entries if e["kind"] != "layout"]


def input_kind(e: dict) -> str:
    return {"file": "file", "store": "store", "both": "file or store"}[e["kind"]]


def spec_table(entries: list[dict]) -> str:
    rows = ["| profile | input | inputs |", "|---|---|---|"]
    rows += [f"| [{e['title']}](zarrify/{e['name']}.md#part-2-the-profile) | {input_kind(e)} | {e['inputs']} |"
             for e in profiles(entries)]
    return "\n".join(rows)


def profile_heading(e: dict) -> tuple[str, str]:
    """The number and text of the profile's heading in its document (`## <n>. <T> profile`, the
    first heading of its Part 2)."""
    text = (ROOT / "spec" / "zarrify" / f"{e['name']}.md").read_text(encoding="utf-8")
    part2 = text.find("\n# Part 2")
    m = re.search(r"^## (\d+)\. (.* profile)$", text[part2:], re.MULTILINE) if part2 >= 0 else None
    if m is None:
        raise ValueError(f"{e['name']}: spec/zarrify/{e['name']}.md has no Part 2 heading `## <n>. <T> profile`")
    return m.group(1), m.group(2)


def profile_link(e: dict, base: str) -> str:
    """A link to the profile's heading, from a document at `base` (the path to spec/zarrify/)."""
    n, heading = profile_heading(e)
    return f"[{e['name']}.md §{n}]({base}{e['name']}.md#{n}-{slug(heading)})"


def spec_list(entries: list[dict]) -> str:
    items = []
    for e in profiles(entries):
        n, heading = profile_heading(e)
        items.append(f"- the {heading}: [spec/zarrify/{e['name']}.md §{n}](zarrify/{e['name']}.md#{n}-{slug(heading)})")
    return ";\n".join(items) + "."


def sniff_table(entries: list[dict]) -> str:
    rows = ["| profile | `sniff` |", "|---|---|"]
    rows += [f"| {e['title']} ({profile_link(e, 'zarrify/')}) | {e['sniff']} |"
             for e in entries if e["sniff"] is not None]
    return "\n".join(rows)


def slug(text: str) -> str:
    return re.sub(r"[^\w\- ]", "", text.lower()).replace(" ", "-")


def conventions_table(entries: list[dict]) -> str:
    rows = ["| format | convention | UUID |", "|---|---|---|"]
    for e in entries:
        source = e["source"] + (" ([§9](#9-dataset-layouts))" if e["kind"] == "layout" else "")
        rows.append(f"| {source} | [{e['name']}](zarrify/{e['name']}.md) | `{e['uuid']}` |")
    return "\n".join(rows)


def docs_table(entries: list[dict]) -> str:
    rows = ["| format | for | README | profile |", "|---|---|---|---|"]
    for e in profiles(entries):
        f = e["name"]
        readme = (f"[{f}]({f}/README.md)" if (ROOT / "docs" / "zarrify" / f / "README.md").is_file()
                  else f"[convention](../../spec/zarrify/{f}.md)")
        rows.append(f"| {e['title']} | {e['for']} | {readme} | {profile_link(e, '../../spec/zarrify/')} |")
    return "\n".join(rows)


def class_name(name: str, suffix: str) -> str:
    """A profile's class: its name's parts, capitalized, and `suffix` (`ome-zarr`, "Store": `OmeZarrStore`)."""
    return "".join(part[:1].upper() + part[1:] for part in name.split("-")) + suffix


def classes(entries: list[dict]) -> list[tuple[str, str, dict]]:
    """(class, base, entry) of each profile's source and store classes, by class name: a file
    profile's `<Name>Source`, a store profile's `<Name>Store`, and both for a profile of both."""
    bases = {"file": ["Source"], "store": ["Store"], "both": ["Source", "Store"]}
    return sorted(((class_name(e["name"], b), b, e) for e in entries for b in bases.get(e["kind"], [])),
                  key=lambda c: c[0])


def classes_module(entries: list[dict]) -> str:
    """python/src/refkv/zarrify/profile_classes.py: the classes, as code a type checker reads."""
    out = ['"""The source and store classes, one per profile of the registry (spec/zarrify.md §1.7',
           "<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#17-named-inputs-and-layouts>):",
           "written by spec/tools/profiles.py from spec/zarrify/profiles/ (`just profiles`; `just sections`",
           'checks that it is current). Edit the registry, not this file."""',
           "",
           "from __future__ import annotations",
           "",
           "from dataclasses import dataclass",
           "from typing import ClassVar",
           "",
           "from refkv.zarrify.sources import Source, Store"]
    for cls, base, e in classes(entries):
        what = "a file" if base == "Source" else "a store"
        untested = "its first bytes" if base == "Source" else "its root's keys"
        doc = textwrap.wrap(f'"""{e["source"]}: {what} the `{e["name"]}` profile reads, {untested} untested '
                            f'(`{base}` lists its read options)."""', 96, initial_indent="    ",
                            subsequent_indent="    ")
        out += ["", "", "@dataclass(frozen=True)", f"class {cls}({base}):", *doc, "",
                f'    profile: ClassVar[str] = "{e["name"]}"']
    names = [cls for cls, _, _ in classes(entries)]
    out += ["", "", "# each class, by its name", "CLASSES: dict[str, type[Source | Store]] = {"]
    out += [f'    "{n}": {n},' for n in names] + ["}", "", "__all__ = ["] + [f'    "{n}",' for n in names] + ["]", ""]
    return "\n".join(out)


BLOCKS = {
    "spec/zarrify.md": {"spec-table": spec_table, "sniff-table": sniff_table, "spec-list": spec_list},
    "spec/conventions.md": {"conventions-table": conventions_table},
    "docs/zarrify/README.md": {"docs-table": docs_table},
}
# the files written whole
FILES = {"python/src/refkv/zarrify/profile_classes.py": classes_module}


def with_blocks(text: str, blocks: dict, entries: list[dict]) -> str:
    """`text` with each marked block's content replaced by its generator's."""
    for name, make in blocks.items():
        pattern = re.compile(rf"(<!-- profiles:{re.escape(name)} -->\n).*?(\n[ \t]*<!-- /profiles -->)", re.DOTALL)
        if not pattern.search(text):
            raise ValueError(f"no block {name!r}")
        text = pattern.sub(lambda m, make=make: m.group(1) + make(entries) + m.group(2), text, count=1)
    return text


def copies() -> dict[Path, str]:
    """The Rust core's copies of the registry's files: each file's path and content."""
    return {COPY_DIR / p.name: p.read_text(encoding="utf-8") for p in sorted(REGISTRY_DIR.glob("*.json"))}


def derived(entries: list[dict]) -> dict[str, str]:
    """Each derived file's path and content: the index pages, and the files written whole."""
    return {path: with_blocks((ROOT / path).read_text(encoding="utf-8"), blocks, entries)
            for path, blocks in BLOCKS.items()} | {path: make(entries) for path, make in FILES.items()}


def stale() -> list[str]:
    """The derived files whose content is not the registry's, and the copies of no profile."""
    out = [path for path, text in derived(load()).items()
           if not (ROOT / path).is_file() or (ROOT / path).read_text(encoding="utf-8") != text]
    want = copies()
    out += [p.relative_to(ROOT).as_posix() for p, text in want.items()
            if not p.is_file() or p.read_text(encoding="utf-8") != text]
    out += [p.relative_to(ROOT).as_posix() for p in sorted(COPY_DIR.glob("*.json")) if p not in want]
    return out


def cmo(e: dict) -> dict:
    """The CMO of an entry's convention, as refkv writes it (spec/conventions.md §1
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#1-conventions>): at version 0,
    which has no tag, its URLs name the development branch."""
    p, n = e["name"], e["version"]
    ref, blob = ("heads/main", "main") if n == 0 else (f"tags/zarrify-{p}-v{n}",) * 2
    return {
        "uuid": e["uuid"],
        "schema_url": f"https://raw.githubusercontent.com/d-v-b/refkv/refs/{ref}/spec/zarrify/{p}/schema.json",
        "spec_url": f"https://github.com/d-v-b/refkv/blob/{blob}/spec/zarrify/{p}.md",
        "name": CONVENTION_KEY,
        "description": f"The Zarr layout of a {e['title']} source zarrified by refkv, and the source's metadata",
    }


def documents() -> dict[str, str]:
    """The documents that may copy a CMO, by path from the repository's root: the Markdown of
    spec/, but the records of past states (spec/history/), and of docs/."""
    out = {}
    for p in sorted(q for d in ("spec", "docs") for q in (ROOT / d).rglob("*.md")):
        path = p.relative_to(ROOT).as_posix()
        if not path.startswith("spec/history/"):
            out[path] = p.read_text(encoding="utf-8")
    return out


def declaration(text: str) -> str:
    """A format's document's declaration: its section 1, up to its section 2."""
    m = re.search(r"^## 1\. .*?(?=^## 2\. )", text, re.MULTILINE | re.DOTALL)
    return m.group(0) if m else ""


def cmo_errors(entries: list[dict], texts: dict[str, str]) -> list[str]:
    """In `texts` (each document's path and Markdown): each copy of a CMO of refkv's conventions
    that is not the registry's, and each format's document whose declaration does not show its
    own. A copy of refkv's is a flat object with a member "uuid" that is an entry's UUID, or that
    names the key `refkv_zarrified` and has a UUID (not a placeholder, as conventions §1's)."""
    by_uuid = {e["uuid"]: e for e in entries}
    out = []
    for path, text in texts.items():
        for m in CMO_TEXT.finditer(text):
            where = f"{path}:{text.count(chr(10), 0, m.start()) + 1}"
            try:
                value = json.loads(m.group(0))
            except json.JSONDecodeError:
                value = None
            uuid = value.get("uuid") if isinstance(value, dict) else None
            if uuid in by_uuid:
                want = cmo(by_uuid[uuid])
                if value != want:
                    out.append(f"{where}: not the CMO of {by_uuid[uuid]['name']}, which is {json.dumps(want)}")
            elif value is None and any(u in m.group(0) for u in by_uuid):
                out.append(f"{where}: a CMO of refkv's that is not JSON")
            elif isinstance(value, dict) and value.get("name") == CONVENTION_KEY and UUID.fullmatch(str(uuid)):
                out.append(f"{where}: a CMO of refkv's whose UUID is no registry entry's")
    for e in entries:
        path = f"spec/zarrify/{e['name']}.md"
        shown = []
        for m in CMO_TEXT.finditer(declaration(texts.get(path, ""))):
            with contextlib.suppress(json.JSONDecodeError):
                shown.append(json.loads(m.group(0)))
        if cmo(e) not in shown:
            out.append(f"{path}: its declaration (section 1) does not show its CMO")
    return out


def sample(entries: list[dict]) -> list[str]:
    """One fixture of each profile that reads a file, as a path from the repository's root: the
    first, by name, of its suffixes under fixtures/<p> that is not named as rejected
    (`unsupported_*`, `*_reject_*`; conformance/zarrify/HARNESS.md)."""
    out = []
    for e in entries:
        files = sorted(p for s in e["suffixes"] for p in (ROOT / "fixtures" / e["name"]).glob(f"*{s}")
                       if "reject" not in p.name and not p.name.startswith("unsupported"))
        if files:
            out.append(files[0].relative_to(ROOT).as_posix())
    return out


def main(argv: list[str]) -> int:
    if argv[:1] == ["sample"]:
        print(" ".join(sample(load())))
        return 0
    if argv[:1] == ["check"]:
        paths = stale()
        for p in paths:
            print(f"{p}: stale (run `uv run python spec/tools/profiles.py`)", file=sys.stderr)
        errors = cmo_errors(load(), documents())
        for error in errors:
            print(error, file=sys.stderr)
        return 1 if paths or errors else 0
    entries = load()
    want = {ROOT / path: text for path, text in derived(entries).items()} | copies()
    COPY_DIR.mkdir(exist_ok=True)
    for p in COPY_DIR.glob("*.json"):
        if p not in want:
            p.unlink()
    for p, text in want.items():
        # unchanged files keep their times, so that nothing rebuilds for them
        if not p.is_file() or p.read_text(encoding="utf-8") != text:
            p.write_text(text, encoding="utf-8")
    print(f"wrote {len(BLOCKS) + len(FILES)} derived files and {len(copies())} copies from {len(entries)} entries")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
