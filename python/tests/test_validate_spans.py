"""conformance/archive/validate.py's check of the bytes before the central directory
(spec §10.1 <https://github.com/d-v-b/vzip/blob/main/spec/archive.md#101-structure>
rules 1 and 2): entries may be padded apart, but padding holds no local header."""

import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "conformance" / "archive"))

from validate import check_spans  # noqa: E402

PAD = b"\0" * 4096


def test_layouts_the_spec_allows():
    # (file bytes before the central directory, entry spans): packed entries, padding
    # between entries, padding before the central directory, and no entries at all
    for buf, spans in [
        (b"a" * 10 + b"b" * 5, [(0, 10), (10, 15)]),
        (b"a" * 10 + PAD + b"b" * 5, [(0, 10), (4106, 4111)]),
        (b"a" * 10 + PAD, [(0, 10)]),
        (b"", []),
    ]:
        assert check_spans(buf, spans, len(buf)) == [], spans


def test_bytes_before_the_first_entry():
    assert "before the first entry" in check_spans(PAD + b"a", [(4096, 4097)], 4097)[0]


def test_entries_that_overlap():
    assert "belong to two entries" in check_spans(b"a" * 10, [(0, 8), (5, 10)], 10)[0]


def test_an_entry_that_overlaps_the_central_directory():
    assert "belong to two entries" in check_spans(b"a" * 10, [(0, 10)], 8)[0]


def test_a_local_header_signature_in_padding():
    buf = b"a" * 10 + b"\0\0PK\x03\x04\0\0" + b"b" * 5
    problems = check_spans(buf, [(0, 10), (18, 23)], len(buf))
    assert problems == ["bytes [10, 18) belong to no entry but hold a local file header "
                        "signature at 12"]
