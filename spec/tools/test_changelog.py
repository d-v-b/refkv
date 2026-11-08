"""The changelogs of the format documents (changelog.py)."""

import subprocess

import changelog
import pytest

HEAD = "# X\n\nIntro.\n\nConvention version: 0 · UUID: `u` ·\nSchema: [schema.json](x/schema.json)\n\n"
BODY = "This document has two parts.\n\n## 1. One\n\nText.\n\n<!-- profiles:t -->\nold\n<!-- /profiles -->\n"
POINTER = "Changes: [changes/x/](changes/x/), one file per change."
ADDED = "Added 2026-10-30."


def doc(*paragraphs: str, body: str = BODY) -> str:
    return HEAD + "".join(f"{p}\n\n" for p in paragraphs) + body


def test_changelogs_that_are_well_formed():
    files = {"2026-10-31-a-change.md": "Changed 2026-10-31: a change\nover two lines ([§1](#1-one)).\n",
             "2026-10-31-another.md": "Changed 2026-10-31, with no change to any output: another.\n",
             "2026-10-30-a-third.md":
                 "Changed 2026-10-30, with no change to the output of a file accepted before: x.\n"}
    history = doc(POINTER, f"{changelog.HISTORY} [the log](h.md).")
    for text, changes in ((doc(POINTER, ADDED), files), (doc(POINTER, ADDED), {}),
                          (history, {"2026-10-22-x.md": "Changed 2026-10-22: x.\n"})):
        assert changelog.problems_of("x", text, changes) == []
    assert changelog.check(None) == []


def test_a_document_with_no_version_line():
    assert changelog.problems_of("x", "# no version line\n", {}) == ["x.md: no version line"]


@pytest.mark.parametrize(
    ("file", "text", "problem"),
    [
        ("2026-10-31.md", "Changed 2026-10-31: x.", "not named"),
        ("2026-10-31-x.md", "Changed on 2026-10-31: x.", "not one paragraph"),
        ("2026-10-31-x.md", "Changed 2026-10-31: x.\n\nMore.", "not one paragraph"),
        ("2026-10-31-x.md", "Changed 2026-10-30: x.", "named for 2026-10-31"),
        ("2026-10-31-x.md", "Changed 2026-10-31: x", "does not end in a period"),
        ("2026-10-29-x.md", "Changed 2026-10-29: x.", "dated before the document was added"),
    ],
)
def test_a_malformed_change_file(file, text, problem):
    (found,) = changelog.problems_of("x", doc(POINTER, ADDED), {file: text})
    assert problem in found


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        (doc(POINTER, "Changed 2026-10-31: x.", ADDED), "a change is in the document"),
        (doc(ADDED), "no `Changes: "),
        (doc(POINTER), "does not end in `Added"),
    ],
)
def test_a_malformed_document(text, problem):
    (found,) = changelog.problems_of("x", text, {})
    assert problem in found


def test_a_change_with_no_file_added(tmp_path, monkeypatch):
    """A change outside the changelog, since the base, with no change file the base lacks."""
    docs = tmp_path / "spec" / "zarrify"
    (docs / "changes" / "x").mkdir(parents=True)
    path = docs / "x.md"
    run = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)  # noqa: E731
    run("init", "-q")
    path.write_text(doc(POINTER, ADDED), encoding="utf-8")
    (docs / "changes" / "x" / "2026-10-31-x.md").write_text("Changed 2026-10-31: x.\n", encoding="utf-8")
    run("add", ".")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x")
    monkeypatch.setattr(changelog, "ROOT", tmp_path)
    monkeypatch.setattr(changelog, "DOCS", docs)
    path.write_text(doc(POINTER, ADDED, body=BODY.replace("old", "new")), encoding="utf-8")
    assert changelog.unrecorded("HEAD", path, path.read_text(encoding="utf-8")) is None
    path.write_text(doc(POINTER, ADDED, body=BODY + "More text.\n"), encoding="utf-8")
    assert changelog.unrecorded("HEAD", path, path.read_text(encoding="utf-8")) == (
        "x.md: its text changed since HEAD, and no file was added to changes/x/")
    (docs / "changes" / "x" / "2026-11-01-y.md").write_text("Changed 2026-11-01: y.\n", encoding="utf-8")
    assert changelog.unrecorded("HEAD", path, path.read_text(encoding="utf-8")) is None


def test_a_hub_document_with_no_link(tmp_path, monkeypatch):
    (tmp_path / "spec" / "zarrify" / "changes").mkdir(parents=True)
    (tmp_path / "spec" / "conventions.md").write_text("# C\n\nNo link.\n", encoding="utf-8")
    monkeypatch.setattr(changelog, "ROOT", tmp_path)
    monkeypatch.setattr(changelog, "DOCS", tmp_path / "spec" / "zarrify")
    assert changelog.hub_problems("conventions", "spec/conventions.md", None) == [
        "spec/conventions.md: no link to zarrify/changes/conventions/"]


def test_a_change_directory_of_no_document(tmp_path, monkeypatch):
    docs = tmp_path / "spec" / "zarrify"
    (docs / "changes" / "gone").mkdir(parents=True)
    (docs / "changes" / "zarrify").mkdir()
    (docs / "changes" / "conventions").mkdir()
    (tmp_path / "spec" / "zarrify.md").write_text("Profiles version: 0\n\nx](zarrify/changes/zarrify/)\n",
                                                  encoding="utf-8")
    (tmp_path / "spec" / "conventions.md").write_text("x](zarrify/changes/conventions/)\n", encoding="utf-8")
    monkeypatch.setattr(changelog, "ROOT", tmp_path)
    monkeypatch.setattr(changelog, "DOCS", docs)
    assert changelog.check(None) == ["changes/gone/: no document gone"]
