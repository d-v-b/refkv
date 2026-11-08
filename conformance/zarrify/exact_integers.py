"""The frozen reference's integers as the shipped code writes them, for comparing the two
(HARNESS.md). Since the freeze, every integer is an exact JSON integer at any magnitude
(spec/conventions.md §6
<https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#6-source-metadata-as-json>), where the
reference writes one past 2^53 - 1 as the string of its digits (without a `+` or leading zeros),
and ND2's as the tag `{"int": digits}`; ND2 no longer has that tag, so a record named `int` alone is
an object, where the reference writes it as pairs (spec/zarrify/nd2.md §5.1
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nd2.md#51-decoded-chunks>). And an integer
read from text outside the range its format declares, or its convention sets (a DICOM `IS`
value's, an ND2 XML variant's runtype's, a SAFE element value's), is that text, where the reference
writes it as an integer, its digit string or ND2's tag.

Each rule applies only where the reference's form shows a number, so that a text field the core
newly writes as an integer, or an integer it newly writes as text, still differs: a digit string
only in a member that holds numbers (`_numeric`), the text only in a value whose range the format
or convention declares (`_ranged`)."""

from __future__ import annotations

import re

MAX_SAFE = 2**53 - 1
_DECIMAL = re.compile(r"[+-]?[0-9]+")
# The reference's digit string of an integer past 2^53 - 1: no `+`, no leading zeros, 16 digits at least
_BIG_DIGITS = re.compile(r"-?[1-9][0-9]{15,}")
# DICOM's VRs of integers (spec/zarrify/dicom.md §5.2
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#52-the-datasets-as-json>)
_INTEGER_VRS = {"SL", "SS", "UL", "US", "SV", "UV", "IS"}


def _big(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and abs(v) > MAX_SAFE


def _decimal(text: str) -> tuple[bool, str]:
    """A decimal integer's text as (negative, digits without leading zeros), compared without
    Python's int, which refuses text of more than 4300 digits."""
    digits = text.lstrip("+-").lstrip("0") or "0"
    return text.startswith("-") and digits != "0", digits


def _big_digits(ref: object) -> bool:
    """Whether `ref` is the reference's digit string of an integer past 2^53 - 1."""
    if not isinstance(ref, str) or not _BIG_DIGITS.fullmatch(ref):
        return False
    digits = ref.lstrip("-")
    return len(digits) > 16 or int(digits) > MAX_SAFE


def _integer_form(ref: object) -> tuple[bool, str] | None:
    """The integer the reference's `ref` writes as an integer, as `_decimal` gives it: a JSON
    integer, ND2's tag `{"int": digits}`, or its digit string past 2^53 - 1; None for anything else,
    any other string among them."""
    if isinstance(ref, int) and not isinstance(ref, bool):
        return _decimal(str(ref))
    tag = ref.get("int") if isinstance(ref, dict) and list(ref) == ["int"] else None
    if isinstance(tag, str) and _DECIMAL.fullmatch(tag):
        return _decimal(tag)
    if _big_digits(ref):
        return _decimal(ref)  # type: ignore[arg-type]
    return None


def _ranged(key: str, ours: dict) -> bool:
    """Whether the member `key` of `ours` holds values whose format declares their integers' range:
    a DICOM `IS` attribute's `Value` (spec/zarrify/dicom.md §5.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#52-the-datasets-as-json>), and
    an ND2 XML variant's document, a chunk `CustomDataVar|...` (spec/zarrify/nd2.md §5.1
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nd2.md#51-decoded-chunks>). (A SAFE
    node's source metadata is too, given by `exact_integers`'s `safe`: `_safe_values`.)"""
    return (key == "Value" and ours.get("vr") == "IS") or key.startswith("CustomDataVar|")


def _numeric(parent: str, key: str, ours: dict) -> bool:
    """Whether the member `key` of `ours` (itself the member `parent`) holds numbers of the source,
    which the reference writes past 2^53 - 1 as their digit strings: a DICOM attribute's `Value` of
    an integer VR, an HDF5 attribute's `value` of an integer datatype (IMS), NIfTI's `header`, and
    NDPI's `ExposureTime`."""
    datatype = ours.get("datatype")
    integer_datatype = isinstance(datatype, dict) and datatype.get("class") in ("integer", "bitfield", "enum")
    return ((key == "Value" and ours.get("vr") in _INTEGER_VRS)
            or (key == "value" and integer_datatype)
            or (key == "header" and parent == "nifti")
            or key == "ExposureTime")


def _safe_values(parent: str, key: str) -> bool:
    """Whether the member `key` (of the member `parent`) is a SAFE node's source metadata, the
    element values of its XML documents (spec/zarrify/safe.md §6.1
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#61-values>): each numeric, and
    ranged by the convention; not the node's Zarr metadata or its GeoZarr attributes."""
    return parent == "refkv_zarrified" and key == "safe"


def exact_integers(ref: object, ours: object, *, safe: bool = False) -> object:
    """The reference's JSON value `ref`, each of its forms of an integer read as `ours` writes it
    where `ours` has that integer: in a member that holds numbers (`_numeric`; a SAFE node's source
    metadata, when `safe`: `_safe_values`), its digit string past 2^53 - 1 as the exact integer; the
    tag `{"int": digits}` as the exact integer; in a value whose format or convention declares a
    range (`_ranged`; a SAFE node's source metadata), its integer forms that `ours` writes as
    decimal text (outside that range) as that text; and the pairs `[["int", v]]` of a record named
    `int` alone as the object `ours` has. Any other value is unchanged, and compares as it did."""
    return _walk(ref, ours, "", numeric=False, ranged=False, safe=safe)


def _walk(ref: object, ours: object, parent: str, *, numeric: bool, ranged: bool, safe: bool = False) -> object:
    if _big(ours):
        if numeric and isinstance(ref, str) and ref == str(ours):
            return ours
        if isinstance(ref, dict) and list(ref) == ["int"] and ref["int"] == str(ours):
            return ours
        return ref
    if ranged and isinstance(ours, str) and _DECIMAL.fullmatch(ours) and _integer_form(ref) == _decimal(ours):
        return ours
    if (isinstance(ours, dict) and list(ours) == ["int"] and isinstance(ref, list) and len(ref) == 1
            and isinstance(ref[0], list) and len(ref[0]) == 2 and ref[0][0] == "int"):
        return {"int": _walk(ref[0][1], ours["int"], "int", numeric=numeric, ranged=ranged, safe=safe)}
    if isinstance(ref, dict) and isinstance(ours, dict):
        out = {}
        for k, v in ref.items():
            if k not in ours:
                out[k] = v
                continue
            values = safe and _safe_values(parent, k)
            out[k] = _walk(v, ours[k], k, numeric=numeric or values or _numeric(parent, k, ours),
                           ranged=ranged or values or _ranged(k, ours), safe=safe)
        return out
    if isinstance(ref, list) and isinstance(ours, list) and len(ref) == len(ours):
        return [_walk(x, y, parent, numeric=numeric, ranged=ranged, safe=safe) for x, y in zip(ref, ours, strict=True)]
    return ref
