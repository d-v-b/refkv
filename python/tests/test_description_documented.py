"""Every profile whose parser returns a description specifies it in its own document, in a section
of Part 2 titled "The description" (`spec/zarrify/<profile>.md`), and every JSON Schema of a
description, `spec/zarrify/<profile>/description.json`, has that section beside it. No list of the
profiles that return one is kept anywhere else: this test reads the registry and the fixtures."""

import json
import re
from functools import cache
from pathlib import Path

import pytest

from refkv import _core
from refkv.build import NoIrError, read
from refkv.zarrify.common import Rejected

ROOT = Path(__file__).parents[2]
SPEC = ROOT / "spec" / "zarrify"
FIXTURES = ROOT / "fixtures"
HEADING = re.compile(r"^#{2,4} [\d.]+ The description$", re.MULTILINE)
PROFILES = sorted(e["name"] for e in json.loads(_core.registry()))


def documented(profile: str) -> bool:
    """Whether the profile's document has a section titled "The description"."""
    doc = SPEC / f"{profile}.md"
    return doc.is_file() and HEADING.search(doc.read_text()) is not None


@cache
def described() -> set[str]:
    """The profiles that return a description of some fixture they read, by the profile that read
    it: a fixture directory holds files of other profiles too, such as an ND2 file's NumPy
    sidecars."""
    out: set[str] = set()
    for path in sorted(FIXTURES.glob("*/*")):
        if "reject" in path.name or path.name.startswith(".") or path.parent.name == "generators":
            continue
        try:
            with read(str(path)) as p:
                if p.description is not None:
                    out.add(p.profile)
        except (Rejected, NoIrError):  # a fixture its profile refuses, or an input with no IR
            continue
    return out



def test_some_profile_returns_a_description():
    """So that the test below cannot skip every profile unnoticed."""
    assert described()


@pytest.mark.parametrize("profile", PROFILES)
def test_description_is_documented(profile):
    if profile not in described():
        pytest.skip(f"{profile}'s parser returns no description of its fixtures")
    assert documented(profile), (
        f"{profile}'s parser returns a description, but spec/zarrify/{profile}.md has no section "
        'titled "The description"')


@pytest.mark.parametrize("schema", sorted(SPEC.glob("*/description.json")), ids=lambda p: p.parent.name)
def test_schema_has_section(schema):
    assert documented(schema.parent.name), (
        f'{schema.relative_to(ROOT)} has no section titled "The description" in '
        f"spec/zarrify/{schema.parent.name}.md")
