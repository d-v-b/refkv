"""Helpers shared by the tests of the frozen reference implementation (a plain module: the test
modules here import it, after putting conformance/zarrify/reference on sys.path)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from refkv_reference.legacy import Output


def ranges_of(out: Output, key: str) -> list[tuple[int, int]]:
    """The (offset, length) ranges of source 0 that the entry `key` references."""
    ranges: list[tuple[int, int]] = []
    for part in out.refs[key]:
        assert isinstance(part, tuple), (key, part)
        assert len(part) == 2, (key, part)
        ranges.append(part)
    return ranges
