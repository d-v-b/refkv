"""The profile registry (spec/virtualize/profiles/<name>.json) and what derives from it (profiles.py)."""

import json
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


def test_the_documents_cmos_are_the_registrys():
    """Every copy of a CMO of refkv's conventions in the documents is the registry's, and each
    format's document shows its own in its declaration."""
    assert profiles.cmo_errors(profiles.load(), profiles.documents()) == []


def tiff() -> tuple[dict, dict[str, str]]:
    """The TIFF entry, and a set of documents of which its own is the only one."""
    e = next(e for e in profiles.load() if e["name"] == "tiff")
    return e, {"spec/virtualize/tiff.md": (ROOT / "spec" / "virtualize" / "tiff.md").read_text(encoding="utf-8")}


def test_a_drifted_copy_is_an_error():
    e, texts = tiff()
    drifted = json.dumps({**profiles.cmo(e), "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/virtualize/tif.md"})
    assert profiles.cmo_errors([e], texts | {"docs/x.md": f"\n\n{drifted}"}) == [
        f"docs/x.md:3: not the CMO of tiff, which is {json.dumps(profiles.cmo(e))}"]


def test_a_copy_that_is_not_json_is_an_error():
    e, texts = tiff()
    broken = json.dumps(profiles.cmo(e)).replace('", "name"', '" "name"')
    assert profiles.cmo_errors([e], texts | {"docs/x.md": broken}) == ["docs/x.md:1: a CMO of refkv's that is not JSON"]


def test_a_copy_of_no_entry_is_an_error():
    e, texts = tiff()
    unknown = json.dumps({**profiles.cmo(e), "uuid": "00000000-0000-4000-8000-000000000000"})
    assert profiles.cmo_errors([e], texts | {"docs/x.md": unknown}) == [
        "docs/x.md:1: a CMO of refkv's whose UUID is no registry entry's"]


def test_a_declaration_without_its_cmo_is_an_error():
    e, texts = tiff()
    moved = texts["spec/virtualize/tiff.md"].replace("## 1. Declaration\n", "## 1. Declaration\n\n## 2. Moved\n", 1)
    assert profiles.cmo_errors([e], {"spec/virtualize/tiff.md": moved}) == [
        "spec/virtualize/tiff.md: its declaration (section 1) does not show its CMO"]
