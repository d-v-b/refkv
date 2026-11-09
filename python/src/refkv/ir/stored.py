"""The arrays a source stores, at the byte level (spec/stored-array.md
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md>), as the IR describes them: the
descriptions, which `refkv._core.Ir.stored_arrays` gives as one JSON document (the chain table, the
list and the `undescribed` mark), read here into frozen dataclasses; and the chunk map, which `refkv._core.Ir.chunk_map`
gives as an object of the Arrow PyCapsule interface, read by pyarrow, polars or nanoarrow without
a copy (refkv itself depends on none of them)."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from refkv._narrow import JSON, boolean, integer, mapping, sequence, string


@dataclass(frozen=True)
class ElementPlace:
    """Bytes of an IR element (`member`: for an element in a run's subtree, the run's member it is
    in), or `range`, an (offset, length) within the element's extent (its bytes in its own space)."""

    element: int
    member: int | None
    range: tuple[int, int] | None


@dataclass(frozen=True)
class ExtentPlace:
    """A range of an object by its key (None: the parsed source), from the object's start."""

    object: str | None
    offset: int
    length: int


type Place = ElementPlace | ExtentPlace


@dataclass(frozen=True)
class Value:
    """A parameter given as a value."""

    value: JSON


@dataclass(frozen=True)
class Source:
    """A parameter the codec makes from bytes of the source."""

    place: Place


type Param = Value | Source


@dataclass(frozen=True)
class Codec:
    """A codec of the IR's list, by name, with its parameters. Its parameters are a dict, to read
    and not to change: like a parameter's JSON value, they leave the codec unhashable, but it pickles
    and copies as plain data."""

    name: str
    parameters: Mapping[str, Param]


@dataclass(frozen=True)
class Each:
    """An axis's units, all `length` long, the last cut by the grid's edge."""

    length: int


@dataclass(frozen=True)
class Listed:
    """An axis's units, each one's length in order: a length, or (length, count) for a run."""

    runs: tuple[int | tuple[int, int], ...]


@dataclass(frozen=True)
class Grid:
    """How the source cuts an array into units."""

    axes: tuple[Each | Listed, ...]
    edge: Literal["full", "short"]


@dataclass(frozen=True)
class Given:
    """A fill value the source gives: one decoded element's bytes."""

    param: Param


type Fill = Literal["undefined", "format_default"] | Given


@dataclass(frozen=True)
class UnitParameter:
    """Parameter `name` of codec `codec` of the array's chain `chain`, a chunk map column."""

    chain: int
    codec: int
    name: str


@dataclass(frozen=True)
class StoredArray:
    """An array as the source stores it (spec/stored-array.md §2
    <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#2-the-description>)."""

    element: int
    path: str | None
    index: int | None
    declarations: tuple[Place, ...]
    shape: tuple[int, ...] | None
    """None where the source declares no extent (an HDF5 null dataspace), unlike a scalar's ()"""
    axes: tuple[int, ...] | None
    item_bits: int | None
    pad_from: int | None
    grid: Grid | None
    chains: tuple[int, ...]
    fill: Fill
    unit_parameters: tuple[UnitParameter, ...]


@dataclass(frozen=True)
class Stored:
    """An IR's stored arrays: the chain table, each chain a tuple of codecs, the descriptions, and
    the `undescribed` mark (spec/stored-array.md §6
    <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#6-in-the-ir>)."""

    chains: tuple[tuple[Codec, ...], ...]
    arrays: tuple[StoredArray, ...]
    undescribed: bool | tuple[str, ...]
    """a file IR's: whether its source kept no descriptions at all at the descriptions' limit; a
    store IR's: the keys of the objects whose arrays kept none"""


def _members(m: Mapping[str, JSON], want: frozenset[str], where: str) -> Mapping[str, JSON]:
    """The object `m` found at `where`, whose members must be exactly `want`; ValueError naming the
    missing and the unknown ones."""
    if set(m) != want:
        missing, unknown = sorted(want - set(m)), sorted(set(m) - want)
        parts = [f"missing {', '.join(missing)}"] if missing else []
        parts += [f"unknown {', '.join(unknown)}"] if unknown else []
        raise ValueError(f"{where or 'stored arrays'}: {'; '.join(parts)}")
    return m


def _count(v: JSON, where: str, least: int = 0) -> int:
    """`v` as an integer of at least `least` (0: a count, 1: a length)."""
    i = integer(v, where)
    if i < least:
        raise ValueError(f"{where}: {i} is below {least}")
    return i


def _optional(v: JSON, where: str, least: int = 0) -> int | None:
    return None if v is None else _count(v, where, least)


def _counts(v: JSON, where: str) -> tuple[int, ...]:
    return tuple(_count(x, f"{where}[{k}]") for k, x in enumerate(sequence(v, where)))


def _pair(v: JSON, where: str, least: tuple[int, int] = (0, 0)) -> tuple[int, int]:
    s = sequence(v, where)
    if len(s) != 2:
        raise ValueError(f"{where}: expected two integers, got {len(s)}")
    return _count(s[0], f"{where}[0]", least[0]), _count(s[1], f"{where}[1]", least[1])


# a base64 string with its padding (RFC 4648 section 4), as spec/stored-array.json's pattern
_BASE64 = re.compile(r"(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?")
_ELEMENT = frozenset({"element", "member", "range"})
_EXTENT = frozenset({"object", "offset", "length"})


def _place(v: JSON, where: str) -> Place:
    m = mapping(v, where)
    if "element" in m:
        _members(m, _ELEMENT, where)
        r = m["range"]
        return ElementPlace(_count(m["element"], f"{where}.element"), _optional(m["member"], f"{where}.member"),
                            None if r is None else _pair(r, f"{where}.range"))
    if "object" in m:
        _members(m, _EXTENT, where)
        o = m["object"]
        return ExtentPlace(None if o is None else string(o, f"{where}.object"), _count(m["offset"], f"{where}.offset"),
                           _count(m["length"], f"{where}.length"))
    raise ValueError(f"{where}: a place names neither an element nor an object")


def _param(v: JSON, where: str) -> Param:
    m = mapping(v, where)
    if set(m) == {"value"}:
        return Value(m["value"])
    if set(m) == {"source"}:
        return Source(_place(m["source"], f"{where}.source"))
    raise ValueError(f"{where}: a parameter is exactly one of value and source, not {sorted(m)}")


def _codec(v: JSON, where: str) -> Codec:
    m = _members(mapping(v, where), frozenset({"name", "parameters"}), where)
    params = mapping(m["parameters"], f"{where}.parameters")
    return Codec(string(m["name"], f"{where}.name"),
                 {k: _param(p, f"{where}.parameters.{k}") for k, p in params.items()})


def _run(v: JSON, where: str) -> int | tuple[int, int]:
    return _count(v, where, 1) if isinstance(v, int) else _pair(v, where, (1, 2))


def _axis(v: JSON, where: str) -> Each | Listed:
    m = mapping(v, where)
    if set(m) == {"each"}:
        return Each(_count(m["each"], f"{where}.each", 1))
    if set(m) == {"list"}:
        runs = sequence(m["list"], f"{where}.list")
        return Listed(tuple(_run(r, f"{where}.list[{k}]") for k, r in enumerate(runs)))
    raise ValueError(f"{where}: an axis is exactly one of each and list, not {sorted(m)}")


def _grid(v: JSON, where: str) -> Grid | None:
    if v is None:
        return None
    m = _members(mapping(v, where), frozenset({"axes", "edge"}), where)
    edge = string(m["edge"], f"{where}.edge")
    if edge not in ("full", "short"):
        raise ValueError(f"{where}.edge: {edge!r} is neither 'full' nor 'short'")
    units = tuple(_axis(a, f"{where}.axes[{k}]") for k, a in enumerate(sequence(m["axes"], f"{where}.axes")))
    return Grid(units, "full" if edge == "full" else "short")


def _fill(v: JSON, where: str) -> Fill:
    if v == "undefined":
        return "undefined"
    if v == "format_default":
        return "format_default"
    if isinstance(v, str):
        raise ValueError(f"{where}: {v!r} is not a fill")  # noqa: TRY004 (a string, of no form a fill has)
    m = _members(mapping(v, where), frozenset({"given"}), where)
    p = _param(m["given"], f"{where}.given")
    if isinstance(p, Value) and not _BASE64.fullmatch(string(p.value, f"{where}.given.value")):
        raise ValueError(f"{where}.given.value: not base64")
    return Given(p)


def _unit_parameter(v: JSON, where: str) -> UnitParameter:
    m = _members(mapping(v, where), frozenset({"chain", "codec", "name"}), where)
    return UnitParameter(_count(m["chain"], f"{where}.chain"), _count(m["codec"], f"{where}.codec"),
                         string(m["name"], f"{where}.name"))


_ARRAY = frozenset({"element", "path", "index", "declarations", "shape", "axes", "item_bits", "pad_from", "grid",
                    "chains", "fill", "unit_parameters"})


def _array(v: JSON, where: str) -> StoredArray:
    m = _members(mapping(v, where), _ARRAY, where)
    path, axes, shape = m["path"], m["axes"], m["shape"]
    declarations = sequence(m["declarations"], f"{where}.declarations")
    ups = sequence(m["unit_parameters"], f"{where}.unit_parameters")
    return StoredArray(
        element=_count(m["element"], f"{where}.element"),
        path=None if path is None else string(path, f"{where}.path"),
        index=_optional(m["index"], f"{where}.index"),
        declarations=tuple(_place(p, f"{where}.declarations[{k}]") for k, p in enumerate(declarations)),
        shape=None if shape is None else _counts(shape, f"{where}.shape"),
        axes=None if axes is None else _counts(axes, f"{where}.axes"),
        item_bits=_optional(m["item_bits"], f"{where}.item_bits", 1),
        pad_from=_optional(m["pad_from"], f"{where}.pad_from"),
        grid=_grid(m["grid"], f"{where}.grid"),
        chains=_counts(m["chains"], f"{where}.chains"),
        fill=_fill(m["fill"], f"{where}.fill"),
        unit_parameters=tuple(_unit_parameter(u, f"{where}.unit_parameters[{k}]") for k, u in enumerate(ups)),
    )


def read(document: str) -> Stored:
    """The stored arrays of the JSON document `refkv._core.Ir.stored_arrays` gives, held to
    spec/stored-array.json: TypeError for a value of another type, ValueError for an object whose
    members are not exactly its form's or a value the schema refuses (a negative integer, a length
    of 0, a run of fewer than two units, a fill value that is not base64, an undescribed key listed
    twice)."""
    doc = _members(mapping(json.loads(document), "stored arrays"), frozenset({"chains", "arrays", "undescribed"}), "")
    chains = sequence(doc["chains"], "chains")
    arrays = sequence(doc["arrays"], "arrays")
    mark = doc["undescribed"]
    undescribed: bool | tuple[str, ...]
    if isinstance(mark, list):
        undescribed = tuple(string(k, f"undescribed[{n}]") for n, k in enumerate(sequence(mark, "undescribed")))
        twice = sorted(k for k, n in Counter(undescribed).items() if n > 1)
        if twice:
            raise ValueError(f"undescribed: {', '.join(twice)} listed twice")
    else:
        undescribed = boolean(mark, "undescribed")
    return Stored(
        tuple(tuple(_codec(c, f"chains[{k}][{j}]") for j, c in enumerate(sequence(chain, f"chains[{k}]")))
              for k, chain in enumerate(chains)),
        tuple(_array(a, f"arrays[{k}]") for k, a in enumerate(arrays)),
        undescribed,
    )
