"""The profile registry, spec/virtualize/profiles.json, and what derives from it.

    uv run python spec/tools/profiles.py          # write the derived files
    uv run python spec/tools/profiles.py check    # fail if any derived file is stale
    uv run python spec/tools/profiles.py sample   # one accepted fixture of each file profile

The registry is the one place a profile's identity is written: its name, its number as a
section of spec/virtualize.md, its convention's title, UUID and version, its kind (a file
input, a store input, both, or a dataset layout, which is a convention but no profile), the
suffixes of its fixture files, the revision its frozen reference twin records (null: none),
and the phrases the tables describe it by. Everything else is derived from it here:

- `python/src/refkv/virtualize/profiles.py` and `js/src/virtualize/profiles.ts`, the hosts'
  copies (a package cannot read the spec), and `rust/refkv-ir/profiles.json`, the Rust
  core's (`include_str!`: the crate cannot include a file outside itself in its package);
- the profile tables of spec/virtualize.md, spec/conventions.md and docs/virtualize/README.md,
  and the formats sentence of demo/index.html, each between a pair of marker comments
  (`<!-- profiles:<block> -->` and `<!-- /profiles -->`).

Adding a format is one entry here, and its own files. `spec/tools/test_profiles.py` checks
that the derived files are current (`just sections` runs it).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REGISTRY_FILE = ROOT / "spec" / "virtualize" / "profiles.json"
KINDS = ("file", "store", "both", "layout")
FIELDS = ("name", "section", "title", "uuid", "version", "kind", "suffixes", "reference", "source", "inputs", "for")
NOTE = "generated from spec/virtualize/profiles.json by spec/tools/profiles.py; edit the registry, not this"


def load() -> list[dict]:
    """The registry's entries, in its order, validated."""
    return validate(json.loads(REGISTRY_FILE.read_text(encoding="utf-8")))


def validate(entries: list[dict]) -> list[dict]:
    for e in entries:
        if tuple(e) != FIELDS:
            raise ValueError(f"{e.get('name')}: the members must be {FIELDS}, in that order")
        if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", e["name"]):
            raise ValueError(f"{e['name']!r}: not a profile name")
        if e["kind"] not in KINDS:
            raise ValueError(f"{e['name']}: kind {e['kind']!r} is not one of {KINDS}")
        if (e["section"] is None) != (e["kind"] == "layout"):
            raise ValueError(f"{e['name']}: a profile has a section number, a layout none")
        if bool(e["suffixes"]) != (e["kind"] in ("file", "both")):
            raise ValueError(f"{e['name']}: a file input has suffixes, a store or layout none")
        if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", e["uuid"]):
            raise ValueError(f"{e['name']}: {e['uuid']!r} is not a UUID")
    for key in ("name", "uuid", "section"):
        values = [e[key] for e in entries if e[key] is not None]
        if len(set(values)) != len(values):
            raise ValueError(f"two entries share a {key}")
    return entries


def profiles(entries: list[dict]) -> list[dict]:
    """The profiles (the entries with a section number), by number."""
    return sorted((e for e in entries if e["section"] is not None), key=lambda e: e["section"])


def python_module(entries: list[dict]) -> str:
    lines = [f'"""The profile registry: {NOTE}."""', "", "REGISTRY: tuple[dict, ...] = ("]
    lines += [f"    {e!r}," for e in entries]
    lines += [")", ""]
    return "\n".join(lines)


def ts_module(entries: list[dict]) -> str:
    names = " | ".join(json.dumps(e["name"]) for e in entries)
    lines = [
        f"// The profile registry: {NOTE}.",
        "",
        f"export type Profile = {names};",
        "",
        "export interface ProfileEntry {",
        "  name: Profile;",
        "  section: number | null;",
        "  title: string;",
        "  uuid: string;",
        "  version: number;",
        '  kind: "file" | "store" | "both" | "layout";',
        "  suffixes: string[];",
        "  reference: number | null;",
        "  source: string;",
        "  inputs: string;",
        "  for: string;",
        "}",
        "",
        "export const REGISTRY: ProfileEntry[] = [",
    ]
    lines += [f"  {json.dumps(e, ensure_ascii=False)}," for e in entries]
    lines += ["];", ""]
    return "\n".join(lines)


def input_kind(e: dict) -> str:
    return {"file": "file", "store": "store", "both": "file or store"}[e["kind"]]


def spec_table(entries: list[dict]) -> str:
    rows = ["| § | profile | input | inputs |", "|---|---|---|---|"]
    for e in profiles(entries):
        rows.append(f"| {e['section']} | [{e['title']}](virtualize/{e['name']}.md#part-2-the-profile) | {input_kind(e)} | {e['inputs']} |")
    return "\n".join(rows)


def profile_heading(e: dict) -> str:
    """The text of the profile's heading in its document (`## <n>. <T> profile`)."""
    text = (ROOT / "spec" / "virtualize" / f"{e['name']}.md").read_text(encoding="utf-8")
    part2 = text.find("\n# Part 2")
    m = re.search(rf"^## {e['section']}\. (.*)$", text[part2:], re.M) if part2 >= 0 else None
    if m is None:
        raise ValueError(f"{e['name']}: spec/virtualize/{e['name']}.md has no Part 2 heading numbered {e['section']}")
    return m.group(1)


def spec_list(entries: list[dict]) -> str:
    items = []
    for e in profiles(entries):
        n, f, heading = e["section"], e["name"], profile_heading(e)
        items.append(f"- [§{n}](virtualize/{f}.md#{n}-{slug(heading)}), the {heading}: [spec/virtualize/{f}.md](virtualize/{f}.md#part-2-the-profile)")
    return ";\n".join(items) + "."


def slug(text: str) -> str:
    return re.sub(r"[^\w\- ]", "", text.lower()).replace(" ", "-")


def conventions_table(entries: list[dict]) -> str:
    rows = ["| format | convention | UUID |", "|---|---|---|"]
    for e in entries:
        source = e["source"] + (" ([§9](#9-dataset-layouts))" if e["kind"] == "layout" else "")
        rows.append(f"| {source} | [{e['name']}](virtualize/{e['name']}.md) | `{e['uuid']}` |")
    return "\n".join(rows)


def docs_table(entries: list[dict]) -> str:
    rows = ["| format | for | README | profile |", "|---|---|---|---|"]
    for e in profiles(entries):
        f = e["name"]
        readme = f"[{f}]({f}/README.md)" if (ROOT / "docs" / "virtualize" / f / "README.md").is_file() else f"[convention](../../spec/virtualize/{f}.md)"
        rows.append(f"| {e['title']} | {e['for']} | {readme} | [§{e['section']}](../../spec/virtualize/{f}.md#part-2-the-profile) |")
    return "\n".join(rows)


def prose_list(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def demo_lede(entries: list[dict]) -> str:
    files = prose_list([e["source"] for e in profiles(entries) if e["kind"] == "file"])
    stores = prose_list([e["source"] for e in profiles(entries) if e["kind"] == "store"])
    both = [e["source"] for e in profiles(entries) if e["kind"] == "both"]
    text = f"{files} files, {stores} stores"
    if both:
        text += f", and {prose_list(both)} products"
    return f"    {text} are supported;"


BLOCKS = {
    "spec/virtualize.md": {"spec-table": spec_table, "spec-list": spec_list},
    "spec/conventions.md": {"conventions-table": conventions_table},
    "docs/virtualize/README.md": {"docs-table": docs_table},
    "demo/index.html": {"demo-lede": demo_lede},
}
COPIES = {
    "python/src/refkv/virtualize/profiles.py": python_module,
    "js/src/virtualize/profiles.ts": ts_module,
    "rust/refkv-ir/profiles.json": lambda entries: REGISTRY_FILE.read_text(encoding="utf-8"),
}


def with_blocks(text: str, blocks: dict, entries: list[dict]) -> str:
    """`text` with each marked block's content replaced by its generator's."""
    for name, make in blocks.items():
        pattern = re.compile(rf"(<!-- profiles:{re.escape(name)} -->\n).*?(\n[ \t]*<!-- /profiles -->)", re.S)
        if not pattern.search(text):
            raise ValueError(f"no block {name!r}")
        text = pattern.sub(lambda m: m.group(1) + make(entries) + m.group(2), text, count=1)
    return text


def derived(entries: list[dict]) -> dict[str, str]:
    """Each derived file's path and content."""
    out = {path: make(entries) for path, make in COPIES.items()}
    for path, blocks in BLOCKS.items():
        out[path] = with_blocks((ROOT / path).read_text(encoding="utf-8"), blocks, entries)
    return out


def stale() -> list[str]:
    """The derived files whose content is not the registry's."""
    return [path for path, text in derived(load()).items()
            if not (ROOT / path).is_file() or (ROOT / path).read_text(encoding="utf-8") != text]


def sample(entries: list[dict]) -> list[str]:
    """One fixture of each profile that reads a file, as a path from the repository's root: the
    first, by name, of its suffixes under fixtures/<p> that is not named as rejected
    (`unsupported_*`, `*_reject_*`; conformance/virtualize/HARNESS.md)."""
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
        return 1 if paths else 0
    for path, text in derived(load()).items():
        (ROOT / path).write_text(text, encoding="utf-8")
    print(f"wrote {len(COPIES) + len(BLOCKS)} files from {len(load())} entries")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
