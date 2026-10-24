"""The profile registry (spec/virtualize/profiles/<name>.json) and what derives from it (profiles.py)."""

import re
from pathlib import Path

import pytest

import profiles

ROOT = Path(__file__).resolve().parents[2]


def test_the_derived_files_are_current():
    assert profiles.stale() == []


def test_every_profile_has_its_documents():
    """Each entry has its convention document, whose header states the registry's version and
    UUID, with its schema and (a profile's) its Part 2, whose profile heading is numbered after
    Part 1; a profile has its rules schema and fixtures."""
    for e in profiles.load():
        doc = ROOT / "spec" / "virtualize" / f"{e['name']}.md"
        text = doc.read_text(encoding="utf-8")
        assert re.search(rf"^Convention version: {e['version']} .*UUID: `{e['uuid']}`", text, re.M), doc
        assert (ROOT / "spec" / "virtualize" / e["name"] / "schema.json").is_file(), doc
        if e["kind"] == "layout":
            assert "# Part 2" not in text, doc
            continue
        part1 = text[: text.index("\n# Part 2")]
        last = max(int(n) for n in re.findall(r"^## (\d+)\. ", part1, re.M))
        assert profiles.profile_heading(e)[0] == str(last + 1), doc
        assert (ROOT / "rust" / "refkv-ir" / "schema" / f"{e['name']}.json").is_file(), doc
        assert (ROOT / "fixtures" / e["name"]).is_dir(), doc


def test_the_registry_is_validated():
    for bad in ({"kind": "image"}, {"sniff": "10"}, {"kind": "store", "suffixes": []}, {"suffixes": []}, {"uuid": "x"},
                {"name": "Tiff"}):
        entries = profiles.load()
        entries[0].update(bad)
        with pytest.raises(ValueError):
            profiles.validate(entries)
