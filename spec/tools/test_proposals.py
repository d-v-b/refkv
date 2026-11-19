"""The status lines of the proposals (proposals.py)."""

import proposals

STATUS = "Status (2026-11-16): step 1 has landed (d-v-b/refkv#1).\nSteps 2 and 3 have not.\n"


def doc(*paragraphs: str) -> str:
    return "# Proposal: x\n\n" + "".join(f"{p}\n" for p in paragraphs) + "\n## The change\n\nText.\n"


def test_proposals_with_a_status_line(tmp_path, monkeypatch):
    for text in (doc(STATUS), doc("An introduction\nof two lines.\n", STATUS, "More of it.\n"),
                 doc("Status (2024-02-29): landed.\n"), "# Proposal: with no sections\n\n" + STATUS,
                 doc("Status codes are what this note proposes.\n", STATUS)):
        assert proposals.problem_of("x", text) is None
    for name in ("a", "b"):
        (tmp_path / f"{name}.md").write_text(doc(STATUS), encoding="utf-8")
    (tmp_path / "notes.txt").write_text("not a proposal\n", encoding="utf-8")
    monkeypatch.setattr(proposals, "PROPOSALS", tmp_path)
    assert proposals.check() == []


def test_a_proposal_with_no_status_line():
    after = doc("An introduction.\n") + "\n" + STATUS
    for text in (doc("An introduction.\n"), after, doc("Its status (2026-11-16): landed.\n")):
        assert proposals.problem_of("x", text) == (
            "x.md: no line that starts `Status (YYYY-MM-DD): ` before the first section")


def test_a_status_with_no_date():
    for line in ("Status: proposed.", "Status (November 2026): landed.", "Status (2026-11-16) landed.",
                 "Status (2026-11-16):", "Status(2026-11-16): landed."):
        assert proposals.problem_of("x", doc(line + "\n")) == (
            f"x.md: its status line does not start `Status (YYYY-MM-DD): `: {line!r}")


def test_a_status_whose_date_is_not_one():
    for date in ("2026-13-01", "2026-02-30", "2025-02-29", "0000-01-01"):
        assert proposals.problem_of("x", doc(f"Status ({date}): landed.\n")) == (
            f"x.md: its status line's date, {date}, is not a date")


def test_a_directory_with_a_malformed_proposal(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text(doc(STATUS), encoding="utf-8")
    (tmp_path / "b.md").write_text(doc("Status: proposed.\n"), encoding="utf-8")
    (tmp_path / "c.md").write_text(doc("An introduction.\n"), encoding="utf-8")
    monkeypatch.setattr(proposals, "PROPOSALS", tmp_path)
    assert proposals.check() == [
        "b.md: its status line does not start `Status (YYYY-MM-DD): `: 'Status: proposed.'",
        "c.md: no line that starts `Status (YYYY-MM-DD): ` before the first section"]


def test_a_directory_with_no_proposals(tmp_path, monkeypatch):
    monkeypatch.setattr(proposals, "ROOT", tmp_path)
    monkeypatch.setattr(proposals, "PROPOSALS", tmp_path / "design" / "proposals")
    assert proposals.check() == ["design/proposals/: no proposals"]
