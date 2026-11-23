"""The strict JSON reader of a store's metadata documents (spec/zarrify.md §1.6
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#16-json-documents>), its limits, and
the readings of its numbers.
"""

from __future__ import annotations

import json
import re
from typing import NoReturn

from refkv._narrow import JSON
from refkv.zarrify.common import Rejected

MAX_SAFE = 2**53 - 1
MAX_DOCUMENT = 1 << 24
MAX_DEPTH = 256


def _bad_constant(name: str) -> NoReturn:
    raise Rejected(f"JSON literal {name} is not allowed")


def _float(s: str) -> float:
    v = float(s)
    if v in (float("inf"), float("-inf")):
        raise Rejected(f"JSON number {s[:40]} is not finite in binary64")
    return v


def _int(s: str) -> int:
    """An integer literal, kept exact (spec/zarrify/n5.md, zarr2, ome-zarr: copied values keep
    every digit), and rejected when its binary64 value is infinite (§1.6
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#16-json-documents>)."""
    try:
        v = int(s)
    except ValueError:  # more digits than Python converts: far beyond binary64
        raise Rejected("JSON number is not finite in binary64") from None
    if not -MAX_SAFE <= v <= MAX_SAFE:
        try:
            float(v)
        except OverflowError:
            raise Rejected("JSON number is not finite in binary64") from None
    return v


_TOKENS = re.compile(r'"(?:[^"\\]|\\.)*"|[\[\]{}]', re.DOTALL)

def parse_json(data: bytes) -> JSON:
    """A metadata document by §1.6
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#16-json-documents>, or Rejected."""
    if len(data) > MAX_DOCUMENT:
        raise Rejected(f"JSON document of {len(data)} bytes exceeds {MAX_DOCUMENT}")
    if data.startswith(b"\xef\xbb\xbf"):
        raise Rejected("JSON document starts with a byte order mark")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise Rejected("JSON document is not UTF-8") from None
    depth = deepest = 0
    for m in _TOKENS.finditer(text):
        t = m.group()
        if t in "[{":
            depth += 1
            deepest = max(deepest, depth)
        elif t in "]}":
            depth -= 1
    if deepest > MAX_DEPTH:
        raise Rejected(f"JSON nests more than {MAX_DEPTH} deep")
    try:
        return json.loads(text, parse_constant=_bad_constant, parse_float=_float, parse_int=_int)
    except json.JSONDecodeError as e:
        raise Rejected(f"invalid JSON: {e}") from None


def as_int(v: JSON, lo: int = -MAX_SAFE, hi: int = MAX_SAFE) -> int | None:
    """`v` as an integer of §1.6
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#16-json-documents> within [lo, hi],
    or None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, float):
        if not v.is_integer():
            return None
        v = int(v)
    if not isinstance(v, int) or not -MAX_SAFE <= v <= MAX_SAFE:
        return None
    return v if lo <= v <= hi else None


def is_number(v: JSON) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def num(v: JSON) -> JSON:
    """A number as the layout reads it (§1.6
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#16-json-documents>): its binary64
    value. Integers beyond
    2^53 − 1 are parsed exactly, so that copied values keep their digits; this is
    the value the layout's checks and arithmetic use."""
    return float(v) if isinstance(v, int) and not isinstance(v, bool) and not -MAX_SAFE <= v <= MAX_SAFE else v
