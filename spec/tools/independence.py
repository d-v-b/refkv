"""Reports the files a branch changes outside its formats' own paths and the files shared on purpose.

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
those formats already).

**Reported:** a file of a format that is not one of the branch's formats, and a file of no format
that is not in `SHARED` below, which the branch adds lines to while it has formats at all. So a
branch of no format (shared machinery, tooling, docs) is never reported, nor is a change that only
removes lines from a file of no format, such as a fixed format's entry leaving a table of strict
xfails: taking a format off a list is the cure, not the problem.

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
CENTRAL = ("a file of no format, and not shared on purpose: if it lists formats, split it into a file per format "
           "or state a rule that names none (docs/adding-a-format.md, section 8)")

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
    """One file of a branch's diff: its path, whether the branch adds lines to it, and, for an output
    manifest, the fixture inputs of the rows it changes, as (input, profile or None for a refusal)."""

    path: str
    adds: bool
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


def report(changes: list[Change], scopes: set[str] | None, names: list[str]) -> tuple[set[str], list[tuple[str, str]]]:
    """The branch's formats, and each reported file with why."""
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
        elif not c.rows and not of and formats and c.adds and c.path not in SHARED:
            found.append((c.path, CENTRAL))
    return formats, found


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def changes(base: str, head: str) -> list[Change]:
    """The files changed between `base` and `head` (moves as a removal and an addition)."""
    out = []
    deleted = set(git("diff", "--name-only", "--no-renames", "--diff-filter=D", "-z", base, head).split("\0"))
    for field in git("diff", "--numstat", "--no-renames", "-z", base, head).split("\0"):
        if not field:
            continue
        added, _, path = field.split("\t", 2)  # a binary file's counts are "-"
        rows: tuple[tuple[str, str | None], ...] = ()
        if path.startswith(MANIFESTS) and path.endswith(".jsonl"):
            rows = manifest_rows(git("diff", "-U0", "--no-renames", base, head, "--", path))
        out.append(Change(path, added != "0" and path not in deleted, rows))
    return out


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
    names = [e["name"] for e in profiles.load()]
    since = git("merge-base", a.base, a.head).strip()
    subjects = git("log", "--no-merges", "--format=%s", f"{since}..{a.head}").splitlines()
    scopes = declared(subjects, names)
    formats, found = report(changes(since, a.head), scopes, names)
    how = "named by its commits' scopes" if scopes else "whose files it changes, as it changes shared machinery"
    print(f"independence: the branch's formats ({how}): {', '.join(sorted(formats)) or 'none'}")
    for path, why in found:
        print(f"  {path}: {why}")
        if a.github:
            print(f"::warning file={path},title=format independence::{why}")
    if found:
        print(f"independence: {len(found)} file(s) outside the branch's formats and the files shared on purpose; "
              "name each in the PR with why, or move what it says into the format's own files")
        if a.strict:
            sys.exit(1)


if __name__ == "__main__":
    main()
