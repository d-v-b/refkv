"""The arrays a source stores, at the byte level (spec/stored-array.md
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md>), as the IR describes them: the
descriptions, which `refkv._core.Ir.stored_arrays` gives as one JSON document (the chain table and
the list), read here into frozen dataclasses; and the chunk map, which `refkv._core.Ir.chunk_map`
gives as an object of the Arrow PyCapsule interface, read by pyarrow, polars or nanoarrow without
a copy (refkv itself depends on none of them)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from refkv._narrow import JSON, integer, mapping, sequence, string


@dataclass(frozen=True)
class ElementPlace:
    """Bytes of an IR element (`member`: for an element in a run's subtree, the run's member it is
    in), or `range`, an (offset, length) within the bytes its recipe gives."""

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
    """A codec of the IR's list, by name, with its parameters."""

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
    shape: tuple[int, ...]
    axes: tuple[int, ...] | None
    item_bits: int | None
    pad_from: int | None
    grid: Grid | None
    chains: tuple[int, ...]
    fill: Fill
    unit_parameters: tuple[UnitParameter, ...]


@dataclass(frozen=True)
class Stored:
    """An IR's stored arrays: the chain table, each chain a tuple of codecs, and the descriptions."""

    chains: tuple[tuple[Codec, ...], ...]
    arrays: tuple[StoredArray, ...]


def _get(m: Mapping[str, JSON], key: str, where: str) -> JSON:
    """Member `key` of the object `m` found at `where`; KeyError naming it when `m` has none."""
    if key not in m:
        raise KeyError(f"{where}.{key}: missing" if where else f"{key}: missing")
    return m[key]


def _optional_int(v: JSON, where: str) -> int | None:
    return None if v is None else integer(v, where)


def _ints(v: JSON, where: str) -> tuple[int, ...]:
    return tuple(integer(x, f"{where}[{k}]") for k, x in enumerate(sequence(v, where)))


def _pair(v: JSON, where: str) -> tuple[int, int]:
    p = _ints(v, where)
    if len(p) != 2:
        raise ValueError(f"{where}: expected two integers, got {len(p)}")
    return p[0], p[1]


def _place(v: JSON, where: str) -> Place:
    m = mapping(v, where)
    if "element" in m:
        r = _get(m, "range", where)
        return ElementPlace(integer(_get(m, "element", where), f"{where}.element"),
                            _optional_int(_get(m, "member", where), f"{where}.member"),
                            None if r is None else _pair(r, f"{where}.range"))
    if "object" in m:
        o = _get(m, "object", where)
        return ExtentPlace(None if o is None else string(o, f"{where}.object"),
                           integer(_get(m, "offset", where), f"{where}.offset"),
                           integer(_get(m, "length", where), f"{where}.length"))
    raise ValueError(f"{where}: a place names neither an element nor an object")


def _param(v: JSON, where: str) -> Param:
    m = mapping(v, where)
    if "value" in m:
        return Value(_get(m, "value", where))
    if "source" in m:
        return Source(_place(_get(m, "source", where), f"{where}.source"))
    raise ValueError(f"{where}: a parameter is neither a value nor a source")


def _codec(v: JSON, where: str) -> Codec:
    m = mapping(v, where)
    params = mapping(_get(m, "parameters", where), f"{where}.parameters")
    return Codec(string(_get(m, "name", where), f"{where}.name"),
                 {k: _param(p, f"{where}.parameters.{k}") for k, p in params.items()})


def _axis(v: JSON, where: str) -> Each | Listed:
    m = mapping(v, where)
    if "each" in m:
        return Each(integer(_get(m, "each", where), f"{where}.each"))
    if "list" in m:
        runs = sequence(_get(m, "list", where), f"{where}.list")
        return Listed(tuple(integer(r, f"{where}.list[{k}]") if isinstance(r, int) else _pair(r, f"{where}.list[{k}]")
                            for k, r in enumerate(runs)))
    raise ValueError(f"{where}: an axis is neither each nor list")


def _grid(v: JSON, where: str) -> Grid | None:
    if v is None:
        return None
    m = mapping(v, where)
    edge = string(_get(m, "edge", where), f"{where}.edge")
    if edge not in ("full", "short"):
        raise ValueError(f"{where}.edge: {edge!r} is neither 'full' nor 'short'")
    axes = sequence(_get(m, "axes", where), f"{where}.axes")
    units = tuple(_axis(a, f"{where}.axes[{k}]") for k, a in enumerate(axes))
    return Grid(units, "full" if edge == "full" else "short")


def _fill(v: JSON, where: str) -> Fill:
    if v == "undefined":
        return "undefined"
    if v == "format_default":
        return "format_default"
    if isinstance(v, str):
        raise ValueError(f"{where}: {v!r} is not a fill")  # noqa: TRY004 (a string, of no form a fill has)
    return Given(_param(_get(mapping(v, where), "given", where), f"{where}.given"))


def _unit_parameter(v: JSON, where: str) -> UnitParameter:
    m = mapping(v, where)
    return UnitParameter(integer(_get(m, "chain", where), f"{where}.chain"),
                         integer(_get(m, "codec", where), f"{where}.codec"),
                         string(_get(m, "name", where), f"{where}.name"))


def _array(v: JSON, where: str) -> StoredArray:
    m = mapping(v, where)
    path = _get(m, "path", where)
    axes = _get(m, "axes", where)
    ups = sequence(_get(m, "unit_parameters", where), f"{where}.unit_parameters")
    return StoredArray(
        element=integer(_get(m, "element", where), f"{where}.element"),
        path=None if path is None else string(path, f"{where}.path"),
        index=_optional_int(_get(m, "index", where), f"{where}.index"),
        declarations=tuple(_place(p, f"{where}.declarations[{k}]")
                           for k, p in enumerate(sequence(_get(m, "declarations", where), f"{where}.declarations"))),
        shape=_ints(_get(m, "shape", where), f"{where}.shape"),
        axes=None if axes is None else _ints(axes, f"{where}.axes"),
        item_bits=_optional_int(_get(m, "item_bits", where), f"{where}.item_bits"),
        pad_from=_optional_int(_get(m, "pad_from", where), f"{where}.pad_from"),
        grid=_grid(_get(m, "grid", where), f"{where}.grid"),
        chains=_ints(_get(m, "chains", where), f"{where}.chains"),
        fill=_fill(_get(m, "fill", where), f"{where}.fill"),
        unit_parameters=tuple(_unit_parameter(u, f"{where}.unit_parameters[{k}]") for k, u in enumerate(ups)),
    )


def read(document: str) -> Stored:
    """The stored arrays of the JSON document `refkv._core.Ir.stored_arrays` gives; TypeError or
    KeyError for a value of another type or a missing key, ValueError for one of no form the
    document has."""
    doc = mapping(json.loads(document), "stored arrays")
    chains = sequence(_get(doc, "chains", ""), "chains")
    arrays = sequence(_get(doc, "arrays", ""), "arrays")
    return Stored(
        tuple(tuple(_codec(c, f"chains[{k}][{j}]") for j, c in enumerate(sequence(chain, f"chains[{k}]")))
              for k, chain in enumerate(chains)),
        tuple(_array(a, f"arrays[{k}]") for k, a in enumerate(arrays)),
    )
