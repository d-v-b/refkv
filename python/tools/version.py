"""refkv's version, from git tags (RELEASING.md).

python/Cargo.toml's committed version is a placeholder, 0.0.0; maturin takes the package's
version from it (python/pyproject.toml's `dynamic = ["version"]`), and turns its semver
pre-release into PEP 440's (1.2.4-dev.3 is 1.2.4.dev3, 1.2.3-rc.1 is 1.2.3rc1). This script
derives the version from `git describe --tags --match 'v*'`:

- at a tag vX.Y.Z, or vX.Y.Z-rc.K: that version;
- N commits past vX.Y.Z: X.Y.(Z+1)-dev.N, before the next patch release;
- N commits past vX.Y.Z-rc.K: X.Y.Z-rc.(K+1).dev.N, before the next candidate;
- with no such tag: 0.0.0-dev.N, N the number of commits.

Usage, from anywhere (`just python::version` and the build recipes call it):

    version.py show           print the version
    version.py pep440 [V]     print V (default: the version), as PEP 440 spells it
    version.py write [TAG]    write the version into python/Cargo.toml and Cargo.lock: TAG's,
                              when given (a tag push, which may share its commit with
                              another tag, such as a release candidate's), else git's
    version.py run -- CMD...  write it, run CMD, and restore both files as they were

`write` changes two tracked files, for a checkout that is thrown away (CI's release
builds). Local builds use `run`, which leaves the tree as it found it."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
CARGO_TOML = HERE / "Cargo.toml"
CARGO_LOCK = HERE / "Cargo.lock"
CRATE = "refkv-python"

TAG = re.compile(r"v(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)(?:-rc\.(?P<rc>\d+))?")
"""A release tag: vX.Y.Z, or a release candidate vX.Y.Z-rc.K."""

# semver.org's grammar (https://semver.org/#is-there-a-suggested-regular-expression-regex-to-check-a-semver-string)
SEMVER = re.compile(
    r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?"
)


def version_for(tag: str | None, distance: int) -> str:
    """The version `distance` commits past `tag` (None: no release tag), as semver. Raises
    ValueError for a tag that is not vX.Y.Z or vX.Y.Z-rc.K, and for a result that is not
    semver (e.g. a tag with a leading zero, v1.02.3)."""
    if tag is None:
        version = f"0.0.0-dev.{distance}"
    else:
        m = TAG.fullmatch(tag)
        if m is None:
            raise ValueError(f"the tag {tag!r} is not vX.Y.Z or vX.Y.Z-rc.K")
        major, minor, patch, rc = m["major"], m["minor"], m["patch"], m["rc"]
        if distance == 0:
            version = tag[1:]
        elif rc is None:
            version = f"{major}.{minor}.{int(patch) + 1}-dev.{distance}"
        else:
            version = f"{major}.{minor}.{patch}-rc.{int(rc) + 1}.dev.{distance}"
    if SEMVER.fullmatch(version) is None:
        raise ValueError(f"the version {version!r} (from the tag {tag!r}) is not semver")
    return version


def pep440(version: str) -> str:
    """`version` (one `version_for` gives) as maturin writes it in the distributions'
    names: 1.2.4-dev.3 is 1.2.4.dev3, 1.2.3-rc.2.dev.1 is 1.2.3rc2.dev1."""
    m = re.fullmatch(r"(\d+\.\d+\.\d+)(?:-rc\.(\d+))?(?:[-.]dev\.(\d+))?", version)
    if m is None:
        raise ValueError(f"the version {version!r} is not X.Y.Z[-rc.K][-dev.N]")
    release, rc, dev = m.groups()
    return release + (f"rc{rc}" if rc else "") + (f".dev{dev}" if dev else "")


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True, check=False)


def describe() -> tuple[str | None, int]:
    """The nearest release tag and the number of commits past it (None and the number of
    commits when there is none)."""
    d = git("describe", "--tags", "--long", "--match", "v*")
    if d.returncode == 0:
        tag, distance, _ = d.stdout.strip().rsplit("-", 2)
        return tag, int(distance)
    count = git("rev-list", "--count", "HEAD")
    if count.returncode != 0:
        raise SystemExit(f"version.py: not a git checkout with commits: {count.stderr.strip()}")
    return None, int(count.stdout)


def current() -> str:
    return version_for(*describe())


def write(version: str) -> None:
    """Write `version` as the crate's in Cargo.toml and in its Cargo.lock entry."""
    toml = CARGO_TOML.read_text()
    toml, n = re.subn(r'(?m)^version = "[^"]*"', f'version = "{version}"', toml, count=1)
    assert n == 1, f"{CARGO_TOML}: no version line"
    lock = CARGO_LOCK.read_text()
    lock, n = re.subn(rf'(name = "{CRATE}"\nversion = )"[^"]*"', rf'\g<1>"{version}"', lock)
    assert n == 1, f"{CARGO_LOCK}: no entry for {CRATE}"
    CARGO_TOML.write_text(toml)
    CARGO_LOCK.write_text(lock)


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "show"
    if cmd == "show":
        print(current())
    elif cmd == "pep440":
        print(pep440(argv[1] if len(argv) > 1 else current()))
    elif cmd == "write":
        v = version_for(argv[1], 0) if len(argv) > 1 and argv[1] else current()
        write(v)
        print(f"version.py: refkv {v} ({pep440(v)}) written to {CARGO_TOML.name} and {CARGO_LOCK.name}",
              file=sys.stderr)
    elif cmd == "run":
        rest = argv[1:]
        if rest[:1] == ["--"]:
            rest = rest[1:]
        if not rest:
            raise SystemExit("usage: version.py run -- CMD...")
        saved = {p: p.read_bytes() for p in (CARGO_TOML, CARGO_LOCK)}
        try:
            v = current()
            write(v)
            print(f"version.py: building refkv {v} ({pep440(v)})", file=sys.stderr)
            return subprocess.run(rest, check=False).returncode
        finally:
            for p, b in saved.items():
                p.write_bytes(b)
    else:
        raise SystemExit(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
