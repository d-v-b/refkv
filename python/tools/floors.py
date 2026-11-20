"""The floors of refkv's requirements (python/pyproject.toml's dependencies and extras), for CI's
pytest-min-versions job, which installs each at its floor and runs the tests.

Usage, from anywhere:

    floors.py names     print the name of each requirement, one per line: the job passes each to
                        `uv pip install --upgrade-package`, so that the lowest-direct resolution
                        replaces what `uv sync` installed (without it, uv keeps an installed
                        version that satisfies the requirement)
    floors.py check     check that the running environment has each requirement at its floor,
                        and fail naming each that is not
"""

import importlib.metadata
import sys
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def requirements() -> list[Requirement]:
    """Each of refkv's requirements on another distribution: its own extras, such as refkv[zarr]
    in another extra, are left out."""
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    specs = [*project["dependencies"], *(s for extra in project["optional-dependencies"].values() for s in extra)]
    return [r for r in map(Requirement, specs) if r.name != project["name"]]


def floor(requirement: Requirement) -> Version:
    """The version of a requirement's `>=` specifier; each of refkv's requirements has one."""
    floors = [Version(s.version) for s in requirement.specifier if s.operator == ">="]
    if len(floors) != 1:
        raise ValueError(f"{requirement} has no single >= floor")
    return floors[0]


def check() -> list[str]:
    """A line for each requirement installed at other than its floor (or not at all)."""
    problems = []
    for requirement in requirements():
        try:
            installed = Version(importlib.metadata.version(requirement.name))
        except importlib.metadata.PackageNotFoundError:
            problems.append(f"{requirement.name}: not installed, floor {floor(requirement)}")
            continue
        if installed != floor(requirement):
            problems.append(f"{requirement.name}: {installed} installed, floor {floor(requirement)}")
    return problems


def main(argv: list[str]) -> int:
    match argv:
        case ["names"]:
            print("\n".join(sorted({r.name for r in requirements()})))
            return 0
        case ["check"]:
            problems = check()
            if problems:
                print("\n".join(problems), file=sys.stderr)
                return 1
            print("every requirement is at its floor")
            return 0
        case _:
            print(__doc__, file=sys.stderr)
            return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
