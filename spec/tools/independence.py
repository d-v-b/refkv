"""Reports the files of other formats a branch changes, and the shared files it adds a format's name to.

    uv run python spec/tools/independence.py [--base origin/main] [--head HEAD] [--strict] [--github]

A format's PRs edit only that format's files (d-v-b/refkv#378): a file that lists every format, in
prose or in code, is edited by every format's PRs and conflicts across them. This reports the files
of a branch's diff since its merge base with `--base` that break this, so that independence does not
erode one convenient edit at a time. It keeps no list of the formats: they are the registry's
profiles (spec/zarrify/profiles/<name>.json), and a format's own paths are found by its name.

**A path belongs to a format** when the first of its components that names a profile names that
format. A component names a profile when its stem (the text before its first `.`), split at `_` and
`-`, holds the profile's name as a run of whole words; the longest name wins. So
`rust/refkv-ir/src/nifti_pair.rs`, `spec/zarrify/changes/mp3/<date>-<slug>.md`,
`python/tests/ir/test_czi_description.py`, `fixtures/dicom_series/...` and
`rust/refkv-ir/src/ome_zarr.rs` belong to nifti, mp3, czi, dicom and ome-zarr, and
`fixtures/grib/expected/grib_bitmap.0.npy` to grib. The output manifests,
`conformance/outputs/manifests/<fixture directory>.jsonl`, record each input's profile: a row a
branch changes is the branch's when its profile or its fixture directory's format is one of the
branch's formats, so that a change to NPY that alters the NPY files among the GRIB fixtures is NPY's,
and a GRIB fixture's expected `.npy` file, which records the NPY profile, is GRIB's too.

**A branch's formats** are the profiles its commits' conventional-commit scopes name
(`fix(npy): ...`, `refactor(nifti,nrrd): ...`). A commit whose type has no scope, or a scope that
is no profile (`refactor(ir): ...`, `docs: ...`), changes what several formats share, so a branch
with one, or with no commit that names a profile, has as formats every format whose paths it
changes (a manifest's rows count by their fixture directories, unless their profile is one of
those formats already). They decide only which formats' files are the branch's own: what a shared
file gains is read the same whatever they are.

**Reported:** a file of a format that is not one of the branch's formats; and a file of no format,
not in `SHARED` below, when a line the branch adds to it names a profile, whatever the branch's
formats. A line names one when it holds the profile's name, its name in capitals or its title
(`fits`, `FITS`; `npy`, `NPY`, `NumPy`; `safetensors`; `Neuroglancer precomputed`), as whole words,
case and all, the longest spelling at each word: words split at anything but letters, digits, `_`
and `-`, so `safe_l1c`, `tiff_set` and `thread-safe` name no profile (and `safetensors` names its
own, not `safe`), and `MRC dataset` names mrc-dataset, not mrc. A title that holds `Zarr`
(`OME-Zarr`, `Zarr v3`) is no spelling, since it names refkv's output as often as the profile; the
names `ome-zarr` and `zarr3` still are. A name of lowercase letters alone may be an English word or
an identifier (`none fits`, `far from safe`, `const safe = ...`, `safe.test(c)`), so it names a
profile only where code names a module or a path: in quotes or backticks on both sides, after `.`,
`/` or `::`, before `/` or `::`, or in an import or a module declaration (`"fits"`, `` `tiff` ``,
`zarrify/fits.md`, `refkv.grib`, `crate::nifti`, `mod vsi;`, `use crate::{fits, grib};`,
`from refkv import common, fits`). A name with a digit or a `-` (`hdf5`, `nd2`, `zarr2`,
`ome-zarr`) is no word, and counts anywhere. What this misses: such a name as a bare key or field
(`nifti: [...]`, which looks like the parameter `safe: impl Fn`), alone on a line of a wrapped list
(`    fits,`), or before a `.` alone (`grib.md` in a relative link, which looks like
`fits.then_some`). What it still counts: an identifier quoted or joined that only shares a
profile's name (the function `refkv.build.fits`, `"fits"` in its `__all__`). These are the limits
of reading words alone; the PR's reviewer judges the rest.
One more format added to a shared list, in prose or in code, is a line naming it; a change to
shared machinery that names no format is not reported, whichever formats the branch also changes,
and neither is a change that only removes lines, such as a fixed format's entry leaving a table of
strict xfails: taking a format off a list is the cure, not the problem.

**Warns, unless `--strict`.** Shared machinery lands with its first user (#378), so a format's PR
can rightly change a file every format uses, and a branch that changes several formats' code at
once is sometimes the right change. What is reported is for the author and the reviewer to judge,
not a gate: preflight and CI print it (with `--github`, as annotations on the PR's diff), and exit
0, and the PR names each reported file with why. `--strict` exits 1 when anything is reported, for
a session that means to change one format's files alone.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import profiles

ROOT = Path(__file__).resolve().parents[2]
MANIFESTS = "conformance/outputs/manifests/"
CONVENTIONAL = re.compile(r"^[a-z]+(?:\(([^)]*)\))?!?: ")
CENTRAL = ("a file of no format, not shared on purpose, to which the branch adds a line naming {formats}: {line!r}; "
           "if it lists formats, split it into a file per format or state a rule that names none "
           "(docs/adding-a-format.md, section 8)")

# The files of no format that a format's PR may edit, each with why it is shared on purpose: a
# change here is a change to machinery every format uses, or to a file derived from the registry,
# not one more format added to a list. Keep it short; a file that lists the formats belongs in no
# allowlist, but split into per-format files (#415).
SHARED: dict[str, str] = {
    "rust/refkv-ir/src/project/mod.rs": "the projections' shared helpers",
    "rust/refkv-ir/src/run.rs": "the run traits every parser implements",
    "rust/refkv-ir/src/array.rs": "the IR's array model",
    "rust/refkv-ir/src/ir.rs": "the IR's element model",
    "python/src/refkv/zarrify/profile_classes.py": "written whole from the registry by `just profiles` (#378 item 10)",
}


@dataclass(frozen=True)
class Change:
    """One file of a branch's diff: its path; for a file of no format, the lines the branch adds to it;
    and, for an output manifest, the fixture inputs of the rows it changes, as (input, profile or None
    for a refusal)."""

    path: str
    added: tuple[str, ...] = ()
    rows: tuple[tuple[str, str | None], ...] = ()


def words(stem: str) -> list[str]:
    return re.split(r"[-_]", stem)


def named(component: str, names: list[str]) -> str | None:
    """The profile a path component names, if any: the longest name its stem holds as whole words."""
    ws = words(component.split(".", maxsplit=1)[0])
    held = [n for n in names if any(ws[i:i + len(words(n))] == words(n) for i in range(len(ws)))]
    return max(held, key=lambda n: len(words(n)), default=None)


def owner(path: str, names: list[str]) -> str | None:
    """The format a path belongs to: the one its first component naming a profile names."""
    for component in PurePosixPath(path).parts:
        if found := named(component, names):
            return found
    return None


Spellings = dict[tuple[str, ...], tuple[str, bool]]


def spellings(entries: list[dict]) -> Spellings:
    """Each run of words that names a profile of the registry `entries` (its name, its name in capitals,
    its title unless that holds `Zarr`), with the profile and whether it counts only where code names a
    module or a path: the name, when it is lowercase letters alone."""
    out: Spellings = {}
    for e in entries:
        name = e["name"]
        out[(name.upper(),)] = (name, False)
        if "Zarr" not in e["title"]:
            out[tuple(e["title"].split())] = (name, False)
        out.setdefault((name,), (name, name.isalpha() and name.islower()))
    return out


QUOTES = "\"'`"
# A line that is an import or a module declaration, every word after whose keyword names a module:
# Rust's `mod x;` and `use a::{x, y};`, Python's `import x, y` and `from a import x, y`.
STATEMENT = re.compile(r"^\s*(?:(?:pub(?:\([^)]*\))?\s+)?mod\s+\w+\s*;|(?:pub(?:\([^)]*\))?\s+)?use\s+[\w:]*::"
                       r"|import\s+[\w.]+(?:\s*,\s*[\w.]+)*\s*$|from\s+[\w.]+\s+import\s)")


def code_like(line: str, start: int, end: int) -> bool:
    """Whether the word at `line[start:end]` stands where code names a module or a path: in quotes or
    backticks on both sides, after `.`, `/` or `::`, before `/` or `::`, or in an import or a module
    declaration (`STATEMENT`)."""
    before, after = line[start - 1:start] if start else "", line[end:end + 1]
    return ((before != "" and before in QUOTES and after == before) or before in ("/", ".") or after == "/"
            or line[max(start - 2, 0):start] == "::" or line[end:end + 2] == "::"
            or bool(STATEMENT.match(line)))


def mentions(line: str, spelled: Spellings) -> set[str]:
    """The profiles a line names, by `spellings`, as whole words: at each word the longest spelling that
    starts there, one that counts only where code names a module or a path only there (`code_like`)."""
    words = [(m.group().strip("-"), m.start() + len(m.group()) - len(m.group().lstrip("-")))
             for m in re.finditer(r"[\w-]+", line)]
    found, i = set(), 0
    while i < len(words):
        hits = [s for s in spelled if tuple(w for w, _ in words[i:i + len(s)]) == s]
        if not hits:
            i += 1
            continue
        spelling = max(hits, key=len)
        name, strict = spelled[spelling]
        start = words[i][1]
        if not strict or code_like(line, start, start + len(spelling[0])):
            found.add(name)
        i += len(spelling)
    return found


def directory(change: Change, row: tuple[str, str | None], names: list[str]) -> str | None:
    """The format of a manifest row's fixture directory."""
    return owner(f"fixtures/{row[0]}", names) or owner(change.path, names)


def row_owners(change: Change, row: tuple[str, str | None], names: list[str]) -> set[str]:
    """The formats a manifest row is of: its profile's and its fixture directory's."""
    return {f for f in (row[1], directory(change, row, names)) if f}


def branch_formats(changes: list[Change], scopes: set[str] | None, names: list[str]) -> set[str]:
    """The branch's formats: its scopes', else those whose paths it changes, a manifest's rows counting
    by their fixture directories unless their profile is one of the others."""
    if scopes:
        return scopes
    paths = {f for c in changes if not c.rows if (f := owner(c.path, names))}
    rows = {directory(c, r, names) for c in changes for r in c.rows if not row_owners(c, r, names) & paths}
    return paths | {f for f in rows if f}


def declared(subjects: list[str], names: list[str]) -> set[str] | None:
    """The profiles the conventional-commit scopes of `subjects` name, or None when one of them is
    of shared machinery (a type with no scope, or a scope that names no profile) or none names one."""
    out: set[str] = set()
    for subject in subjects:
        if not (m := CONVENTIONAL.match(subject)):
            continue  # a merge commit, or a subject in no conventional form, says nothing
        scope = {s.strip() for s in (m.group(1) or "").split(",")} - {""}
        if not scope or not scope <= set(names):
            return None
        out |= scope
    return out or None


def report(changes: list[Change], scopes: set[str] | None, names: list[str],
           spelled: Spellings) -> tuple[set[str], list[tuple[str, str]]]:
    """The branch's formats, and each reported file with why (`spelled`: `spellings` of the registry)."""
    formats = branch_formats(changes, scopes, names)
    found = []
    for c in changes:
        if c.rows:  # an output manifest: the rows of no format of the branch's
            of = set().union(*(row_owners(c, r, names) for r in c.rows if not row_owners(c, r, names) & formats))
        else:
            of = {f} if (f := owner(c.path, names)) else set()
        if of - formats:
            why = (f"a file of {', '.join(sorted(of - formats))}, which is not one of this branch's formats "
                   f"({', '.join(sorted(formats))})")
            found.append((c.path, why))
        elif not c.rows and not of and c.path not in SHARED:
            named_in = [(line, m) for line in c.added if (m := mentions(line, spelled))]
            if named_in:
                line, m = named_in[0]
                text = line.strip() if len(line.strip()) <= 80 else line.strip()[:77] + "..."
                found.append((c.path, CENTRAL.format(formats=", ".join(sorted(m)), line=text)))
    return formats, found


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def changes(base: str, head: str, names: list[str]) -> list[Change]:
    """The files changed between `base` and `head` (moves as a removal and an addition); a binary
    file adds no lines."""
    out = []
    for path in git("diff", "--name-only", "--no-renames", "-z", base, head).split("\0"):
        if not path:
            continue
        if path.startswith(MANIFESTS) and path.endswith(".jsonl"):
            out.append(Change(path, rows=manifest_rows(git("diff", "-U0", "--no-renames", base, head, "--", path))))
        elif owner(path, names) is None:
            out.append(Change(path, added=added_lines(git("diff", "-U0", "--no-renames", base, head, "--", path))))
        else:
            out.append(Change(path))
    return out


def added_lines(diff: str) -> tuple[str, ...]:
    """The lines a file's diff adds (after its header, which ends at its first hunk)."""
    lines = diff.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("@@")), len(lines))
    return tuple(line[1:] for line in lines[start:] if line.startswith("+"))


def manifest_rows(diff: str) -> tuple[tuple[str, str | None], ...]:
    """The (input, profile) of each row an output manifest's diff adds or removes."""
    rows = set()
    for line in diff.splitlines():
        if line[:1] in "+-" and not line.startswith(("+++", "---")):
            try:
                row = json.loads(line[1:])
            except ValueError:
                continue
            rows.add((row.get("input", ""), row.get("plain", {}).get("profile")))
    return tuple(sorted(rows, key=lambda r: (r[0], r[1] or "")))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--base", default="origin/main", help="compare from the merge base of this and --head")
    p.add_argument("--head", default="HEAD")
    p.add_argument("--strict", action="store_true", help="exit 1 when anything is reported")
    p.add_argument("--github", action="store_true", help="also print each finding as a GitHub annotation")
    a = p.parse_args(argv)
    entries = profiles.load()
    names = [e["name"] for e in entries]
    since = git("merge-base", a.base, a.head).strip()
    subjects = git("log", "--no-merges", "--format=%s", f"{since}..{a.head}").splitlines()
    scopes = declared(subjects, names)
    formats, found = report(changes(since, a.head, names), scopes, names, spellings(entries))
    how = "named by its commits' scopes" if scopes else "whose files it changes, as it changes shared machinery"
    print(f"independence: the branch's formats ({how}): {', '.join(sorted(formats)) or 'none'}")
    for path, why in found:
        print(f"  {path}: {why}")
        if a.github:
            print(f"::warning file={path},title=format independence::{why}")
    if found:
        print(f"independence: {len(found)} file(s) of another format, or shared and gaining a format's name; "
              "name each in the PR with why, or move what it says into the format's own files")
        if a.strict:
            sys.exit(1)


if __name__ == "__main__":
    main()
