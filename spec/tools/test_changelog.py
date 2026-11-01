"""The format documents' changelogs (changelog.py)."""

import subprocess

import changelog
import pytest

HEAD = "# X\n\nIntro.\n\nConvention version: 0 · UUID: `u` ·\nSchema: [schema.json](x/schema.json)\n\n"
BODY = "This document has two parts.\n\n## 1. One\n\nText.\n\n<!-- profiles:t -->\nold\n<!-- /profiles -->\n"


def doc(*entries: str, body: str = BODY) -> str:
    return HEAD + "".join(f"{e}\n\n" for e in entries) + body


def test_changelogs_that_are_well_formed():
    for text in (
        doc("Added 2026-10-30."),
        doc("Changed 2026-10-31: a change\nover two lines ([§1](#1-one)).", "Changed 2026-10-31, with no change to any"
            " output: another.", "Changed 2026-10-30, with no change to the output of a file accepted before: a third.",
            "Added 2026-10-30."),
        doc("Changed 2026-10-22: a change.", f"{changelog.HISTORY} [the log](h.md)."),
    ):
        assert changelog.problems_of("x.md", text) == []
    assert changelog.problems_of("README.md", "# no version line\n") == ["README.md: no version line"]
    assert changelog.check(None) == []


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        (doc(), "no changelog after the version line"),
        (doc("Changed on 2026-10-31: x.", "Added 2026-10-30."), "an entry is not"),
        (doc("Changed 2026-10-31: x", "Added 2026-10-30."), "does not end in a period"),
        (doc("Changed 2026-10-30: x.", "Changed 2026-10-31: y.", "Added 2026-10-30."), "not newest first"),
        (doc("Changed 2026-10-31: x."), "the last entry is neither"),
        (doc("Changed 2026-10-29: x.", "Added 2026-10-30."), "dated before the document was added"),
    ],
)
def test_a_malformed_changelog(text, problem):
    (found,) = changelog.problems_of("x.md", text)
    assert problem in found


def test_a_change_its_changelog_does_not_record(tmp_path, monkeypatch):
    """A change outside the changelog, since the base, with the same changelog."""
    docs = tmp_path / "spec" / "zarrify"
    docs.mkdir(parents=True)
    path = docs / "x.md"
    run = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)  # noqa: E731
    run("init", "-q")
    path.write_text(doc("Added 2026-10-30."), encoding="utf-8")
    run("add", ".")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x")
    monkeypatch.setattr(changelog, "ROOT", tmp_path)
    monkeypatch.setattr(changelog, "DOCS", docs)
    path.write_text(doc("Added 2026-10-30.", body=BODY.replace("old", "new")),
                    encoding="utf-8")
    assert changelog.check("HEAD") == []
    path.write_text(doc("Added 2026-10-30.", body=BODY + "More text.\n"), encoding="utf-8")
    assert changelog.check("HEAD") == ["x.md: its text changed since HEAD, and its changelog did not"]
    path.write_text(doc("Changed 2026-10-31: more text.", "Added 2026-10-30.", body=BODY + "More text.\n"),
                    encoding="utf-8")
    assert changelog.check("HEAD") == []
