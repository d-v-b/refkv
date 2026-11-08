"""The frozen reference's integers as the shipped code writes them, for comparing the two
(HARNESS.md). Since the freeze, every integer is an exact JSON integer at any magnitude
(spec/conventions.md §6
<https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#6-source-metadata-as-json>), where the
reference writes one past 2^53 - 1 as the string of its digits, and ND2's as the tag
`{"int": digits}`; ND2 no longer has that tag, so a record named `int` alone is an object, where
the reference writes it as pairs (spec/zarrify/nd2.md §5.1
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nd2.md#51-decoded-chunks>). And an integer
read from text outside the range its format declares (a DICOM `IS` value's, an ND2 XML variant's
runtype's) is that text, where the reference writes the integer in one of its forms."""

from __future__ import annotations

import re

MAX_SAFE = 2**53 - 1
_DECIMAL = re.compile(r"[+-]?[0-9]+")


def _big(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and abs(v) > MAX_SAFE


def _integer_of(ref: object) -> int | None:
    """The integer the reference's `ref` writes, as a number, the string of its digits or ND2's tag."""
    if isinstance(ref, int) and not isinstance(ref, bool):
        return ref
    if isinstance(ref, dict) and list(ref) == ["int"]:
        ref = ref["int"]
    if isinstance(ref, str) and _DECIMAL.fullmatch(ref):
        return int(ref)
    return None


def exact_integers(ref: object, ours: object) -> object:
    """The reference's JSON value `ref`, each of its forms of an integer read as `ours` writes it
    where `ours` has that integer: the string of its digits and the tag `{"int": digits}` past
    2^53 - 1 as the exact integer; any of its forms of an integer that `ours` writes as decimal text
    (outside its format's range) as that text; and the pairs `[["int", v]]` of a record named
    `int` alone as the object `ours` has. Any other value is unchanged, and compares as it did."""
    if _big(ours):
        if isinstance(ref, str) and ref == str(ours):
            return ours
        if isinstance(ref, dict) and list(ref) == ["int"] and ref["int"] == str(ours):
            return ours
        return ref
    if isinstance(ours, str) and _DECIMAL.fullmatch(ours) and ref != ours and _integer_of(ref) == int(ours):
        return ours
    if (isinstance(ours, dict) and list(ours) == ["int"] and isinstance(ref, list) and len(ref) == 1
            and isinstance(ref[0], list) and len(ref[0]) == 2 and ref[0][0] == "int"):
        return {"int": exact_integers(ref[0][1], ours["int"])}
    if isinstance(ref, dict) and isinstance(ours, dict):
        return {k: exact_integers(v, ours[k]) if k in ours else v for k, v in ref.items()}
    if isinstance(ref, list) and isinstance(ours, list) and len(ref) == len(ours):
        return [exact_integers(x, y) for x, y in zip(ref, ours, strict=True)]
    return ref
