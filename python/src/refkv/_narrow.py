"""Narrowing of parsed JSON and of values read from zarr: each helper gives its value as the
type it names, or raises TypeError naming where the value was found."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np

type JSON = dict[str, JSON] | list[JSON] | str | int | float | bool | None
"""A parsed JSON value."""


def _wrong(v: object, where: str, kind: str) -> TypeError:
    return TypeError(f"{where or 'value'}: expected {kind}, got {type(v).__name__}")


def mapping(v: object, where: str = "") -> Mapping[str, JSON]:
    """`v` as a JSON object."""
    if not isinstance(v, Mapping):
        raise _wrong(v, where, "a mapping")
    return v


def sequence(v: object, where: str = "") -> Sequence[JSON]:
    """`v` as a JSON array (a sequence other than a string or bytes)."""
    if not isinstance(v, Sequence) or isinstance(v, (str, bytes, bytearray)):
        raise _wrong(v, where, "a sequence")
    return v


def string(v: object, where: str = "") -> str:
    """`v` as a string."""
    if not isinstance(v, str):
        raise _wrong(v, where, "a string")
    return v


def integer(v: object, where: str = "") -> int:
    """`v` as an integer (a bool is not one)."""
    if not isinstance(v, int) or isinstance(v, bool):
        raise _wrong(v, where, "an integer")
    return v


def number(v: object, where: str = "") -> float:
    """`v` as a number: an int or a float, not a bool."""
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        raise _wrong(v, where, "a number")
    return v


def boolean(v: object, where: str = "") -> bool:
    """`v` as a bool."""
    if not isinstance(v, bool):
        raise _wrong(v, where, "a boolean")
    return v


def at(doc: object, *path: str | int) -> JSON:
    """The value at `path` in `doc`: a str step indexes a mapping, an int step a sequence."""
    if not path:
        if doc is None or isinstance(doc, (str, int, float, dict, list)):
            return doc
        raise _wrong(doc, "", "a JSON value")
    v: object = doc
    out: JSON = None
    where = ""
    for step in path:
        if isinstance(step, str):
            m = mapping(v, where)
            where = f"{where}.{step}" if where else step
            if step not in m:
                raise KeyError(f"{where}: missing")
            out = m[step]
        else:
            s = sequence(v, where)
            where = f"{where}[{step}]"
            if not -len(s) <= step < len(s):
                raise IndexError(f"{where}: index out of range")
            out = s[step]
        v = out
    return out


def ndarray(v: object) -> np.ndarray:
    """`v` as a numpy array (`np.asarray`): a value read from a zarr array (`a[...]`)."""
    return np.asarray(v)
