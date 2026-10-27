"""Narrowing of decoded values (refkv._narrow's shape, standard library only): each helper gives
its value as the type it names, or raises TypeError naming where the value was found."""

from collections.abc import Mapping, Sequence

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
