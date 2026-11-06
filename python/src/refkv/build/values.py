"""The values an output built with `refkv.build` holds (the design of d-v-b/refkv#278, "References
and values"): parts that name what they read, so that numbering the source table is left to `seal`;
`Value`, a sequence of parts with the check a routine built it under; `Array`; and `Written`, an
output's entries with their placement."""

from __future__ import annotations

import json
import weakref
from collections.abc import Iterator, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Self

from refkv import Range
from refkv.zarrify.layouts import MAX_PAYLOAD, chunk_key

if TYPE_CHECKING:
    from refkv.build.parsed import Parsed

__all__ = ["Array", "Binding", "Check", "Copy", "Data", "Literal", "Part", "Ref", "Shared", "Value", "Written",
           "Zeros", "concat", "cut", "fits", "payload_size", "value"]

# the most sources an output's table holds: spec/refkv.md §6.7
# <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>'s default `max_sources`,
# which a source index is measured against before `seal` numbers the table
MAX_SOURCES = 1 << 22
CRC_FIELD = 6  # the most bytes a range's crc32c field takes: a tag, and a varint of 32 bits


@dataclass(frozen=True)
class Ref:
    """Bytes `offset` to `offset + length` of url source `source` of an input (`parsed.sources`)."""

    parsed: Parsed = field(compare=False, repr=False)
    source: int
    offset: int
    length: int

    # an input by identity: two reads of one file are two inputs
    def __eq__(self, other: object) -> bool:
        return (isinstance(other, Ref) and other.parsed is self.parsed
                and (other.source, other.offset, other.length) == (self.source, self.offset, self.length))

    def __hash__(self) -> int:
        return hash((id(self.parsed), self.source, self.offset, self.length))


@dataclass(frozen=True)
class Shared:
    """A byte string a convention names as shared (a JPEG tile's tables), which `seal` makes a data
    source, read whole: one per distinct string."""

    data: bytes


@dataclass(frozen=True)
class Zeros:
    """`length` zero bytes, of the zero data source."""

    length: int


@dataclass(frozen=True)
class Data:
    """Bytes `offset` to `offset + length` of a data source of the caller's own, `data` (all of it
    when `length` is None): `cut(Data(blob), a, b)` is bytes `a` to `b` of that one source."""

    data: bytes
    offset: int = 0
    length: int | None = None

    @property
    def size(self) -> int:
        return len(self.data) - self.offset if self.length is None else self.length


@dataclass(frozen=True)
class Literal:
    """Bytes in the reference itself, counted in its payload."""

    data: bytes


@dataclass(frozen=True)
class Copy:
    """The bytes of a `Ref` that are to be copied into the output (an edge chunk too large to pad,
    spec/conventions.md §7 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays>):
    a caller fills it with `parsed.bytes`; `seal` refuses one left unfilled."""

    ref: Ref


type Part = Ref | Shared | Zeros | Data | Literal | Copy


@dataclass(frozen=True)
class Binding:
    """The array a checked chunk was built in, as stored, and the chunk's key in it: what `seal`
    reads back from the document the chunk is written under. JSON members are kept as their
    canonical text, so that a binding compares by value."""

    shape: tuple[int, ...]
    data_type: str
    chunk_grid: str
    codecs: str
    fill_value: str
    key: tuple[int, ...]

    @classmethod
    def of(cls, doc: Mapping[str, Any], key: Sequence[int]) -> Binding:
        """The binding of chunk `key` of the Zarr v3 array document `doc`."""
        return cls(tuple(doc["shape"]), _canonical(doc["data_type"]), _canonical(doc["chunk_grid"]),
                   _canonical(doc["codecs"]), _canonical(doc.get("fill_value")), tuple(key))


@dataclass(frozen=True)
class Check:
    """The rule of a convention a chunk was built under, which `seal` holds it to, where its binding
    holds, in place of the read rule a caller's values meet. Only refkv's routines make checks
    (`project` today, its rule `"builtin"`, a value of a built-in output), each for the parts of
    one value: `seal` honors a check one of them issued, on the very parts it was issued for, and
    no other, so a caller cannot make one, copy one or move one to other parts to escape the read
    rule."""

    rule: str
    binding: Binding
    parts: tuple[Part, ...] = field(default=(), compare=False, repr=False)

    def issued_for(self, parts: tuple[Part, ...]) -> bool:
        """Whether a routine of refkv issued this very check, for `parts`."""
        return _ISSUED.get(id(self)) is self and self.parts == parts


# the checks refkv's routines issued, by identity: a copy (dataclasses.replace) is not among them
_ISSUED: weakref.WeakValueDictionary[int, Check] = weakref.WeakValueDictionary()


def _issue(rule: str, binding: Binding, parts: tuple[Part, ...]) -> Check:
    """A check made by a routine of refkv for `parts`, which `seal` honors on them."""
    check = Check(rule, binding, parts)
    _ISSUED[id(check)] = check
    return check


@dataclass(frozen=True)
class Value:
    """A reference value: its parts, in order, and the check it was built under, or None."""

    parts: tuple[Part, ...]
    check: Check | None = None

    def __post_init__(self) -> None:
        if not self.parts:
            raise ValueError("a value has at least one part")
        for p in self.parts:
            if not isinstance(p, (Ref, Shared, Zeros, Data, Literal, Copy)):
                raise TypeError(f"a value's parts are Ref, Shared, Zeros, Data, Literal or Copy, not "
                                f"{type(p).__name__}")

    @property
    def size(self) -> int:
        """The bytes the value reads."""
        return sum(_size(p) for p in self.parts)


def value(v: Value | Part | Sequence[Part]) -> Value:
    """`v` as a `Value`: a value as it is, a part or a sequence of parts unchecked."""
    if isinstance(v, Value):
        return v
    if isinstance(v, (Ref, Shared, Zeros, Data, Literal, Copy)):
        return Value((v,))
    if isinstance(v, (str, bytes, bytearray, memoryview)) or not isinstance(v, Sequence):
        raise TypeError(f"a value is a Value, a part or a sequence of parts, not {type(v).__name__}")
    return Value(tuple(v))


def _size(p: Part) -> int:
    if isinstance(p, (Ref,)):
        return p.length
    if isinstance(p, Copy):
        return p.ref.length
    if isinstance(p, Zeros):
        return p.length
    if isinstance(p, Data):
        return p.size
    return len(p.data)


def _cut_part(p: Part, a: int, b: int) -> Part:
    """Bytes `a` to `b` of what part `p` reads, `0 <= a < b <= its size`."""
    if isinstance(p, Ref):
        return Ref(p.parsed, p.source, p.offset + a, b - a)
    if isinstance(p, Zeros):
        return Zeros(b - a)
    if isinstance(p, Data):
        return Data(p.data, p.offset + a, b - a)
    if isinstance(p, Copy):
        return Copy(_cut_part(p.ref, a, b))  # type: ignore[arg-type]
    if isinstance(p, Shared):  # a part of a shared string is a range of its data source
        return Data(p.data, a, b - a)
    return Literal(p.data[a:b])


def cut(v: Value | Part | Sequence[Part], start: int, stop: int) -> Value:
    """The value that reads bytes `start` to `stop` of what `v` reads, unchecked. ValueError for
    bounds outside it."""
    val = value(v)
    if not (type(start) is int and type(stop) is int and 0 <= start < stop <= val.size):
        raise ValueError(f"bytes {start} to {stop} of a value of {val.size} bytes: not within it")
    out: list[Part] = []
    at = 0
    for p in val.parts:
        n = _size(p)
        lo, hi = max(start, at), min(stop, at + n)
        if lo < hi:
            out.append(p if (lo, hi) == (at, at + n) and not isinstance(p, Shared) else _cut_part(p, lo - at, hi - at))
        at += n
    return Value(tuple(out))


def _continues(p: Part, q: Part) -> Part | None:
    """`p` and `q` as one part, when `q` reads on where `p` stops, or None."""
    if isinstance(p, Ref) and isinstance(q, Ref) and p.parsed is q.parsed and p.source == q.source \
            and p.offset + p.length == q.offset:
        return Ref(p.parsed, p.source, p.offset, p.length + q.length)
    if isinstance(p, Data) and isinstance(q, Data) and p.data is q.data and p.offset + p.size == q.offset:
        return Data(p.data, p.offset, p.size + q.size)
    if isinstance(p, Zeros) and isinstance(q, Zeros):
        return Zeros(p.length + q.length)
    return None


def concat(*values: Value | Part | Sequence[Part], merge: bool = False) -> Value:
    """The one value of the parts of `values`, in order, unchecked; with `merge`, parts that continue
    one another (ranges of one source, of one data blob, or zeros) are joined."""
    if not values:
        raise ValueError("concat of no values")
    parts: list[Part] = []
    for v in values:
        for p in value(v).parts:
            joined = _continues(parts[-1], p) if merge and parts else None
            if joined is None:
                parts.append(p)
            else:
                parts[-1] = joined
    return Value(tuple(parts))


def _range_at_largest(p: Part) -> Range:
    """`p` as a Range whose source index is the largest a table holds, for measuring a payload
    before `seal` numbers the sources."""
    largest = MAX_SOURCES - 1
    if isinstance(p, Literal):
        return Range(data=p.data)
    if isinstance(p, Ref):
        return Range(largest, p.offset, p.length)
    if isinstance(p, Copy):
        return Range(data=bytes(p.ref.length))
    if isinstance(p, Data):
        return Range(largest, p.offset, p.size)
    return Range(largest, 0, _size(p))


def payload_size(v: Value | Part | Sequence[Part], *, checksums: bool | None = None) -> int:
    """The most a value's payload may be once sealed (spec/zarrify.md §1.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>): every source index at its
    largest, and every url range's crc32c at its largest when `checksums`, which is by default
    whether the value's inputs were read with them."""
    val = value(v)
    if checksums is None:
        checksums = any(isinstance(p, Ref) and p.parsed.checksums for p in val.parts)
    sizes = [len(_range_at_largest(p).encode()) + (CRC_FIELD if checksums and isinstance(p, Ref) else 0)
             for p in val.parts]
    if len(sizes) == 1:
        return sizes[0]
    return sum(1 + len(_varint(n)) + n for n in sizes)  # a Concat: each range as `0x0A varint(len(r)) r`


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def fits(v: Value | Part | Sequence[Part], *, checksums: bool | None = None) -> str | None:
    """Why `seal` might not write `v`, measured before the sources are numbered, or None: its
    payload with every source index at its largest, with checksums as `payload_size` counts them.
    It may refuse a value `seal` would write, never
    the other way; the reads a value makes are checked by `seal`, which merges them as readers do."""
    n = payload_size(v, checksums=checksums)
    if n > MAX_PAYLOAD:
        return f"a payload of up to {n} bytes, more than {MAX_PAYLOAD}"
    return None


def _zero(data_type: object) -> object:
    """A data type's zero, as a Zarr v3 fill value; ValueError for one whose zero this does not know."""
    if isinstance(data_type, dict):
        name = data_type.get("name")
        if name in ("numpy.datetime64", "numpy.timedelta64"):
            return 0
        if name == "fixed_length_utf32":
            return ""
        raise ValueError(f"the data type {name!r} has no zero this knows: give the Array its fill_value")
    if not isinstance(data_type, str):
        raise TypeError(f"a data type is a name or an object, not {type(data_type).__name__}")
    if data_type == "bool":
        return False
    if data_type.startswith("complex"):
        return [0.0, 0.0]
    if data_type.startswith(("float", "bfloat")):
        return 0.0
    if data_type.startswith("r") and data_type[1:].isdigit():  # raw bits: one zero per byte
        return [0] * (int(data_type[1:]) // 8)
    return 0


def _canonical(x: object) -> str:
    return json.dumps(x, sort_keys=True, separators=(",", ":"))


type ArrayChunk = Value | Part | Sequence[Part] | bytes


@dataclass
class Array:
    """A Zarr v3 array: its shape, data type, chunk shape (an int per axis for a regular grid, or an
    axis's lengths for a rectilinear one), codecs, fill value and dimension names, and its chunks by
    coordinates, each a value or bytes; and its attributes and chunk key encoding (the `default` one,
    with `/`, when None). The fill value, when None, is the data type's zero (an error for a type
    whose zero this does not know). The chunks a routine returns are checked, and keep their checks
    in an `Array` a caller builds around them; a value a caller makes is not checked."""

    shape: list[int]
    data_type: str | dict[str, Any]
    chunk_shape: list[Any]  # an int per axis (a regular grid), or an axis's lengths as a list (rectilinear)
    codecs: list[dict[str, Any]]
    chunks: dict[tuple[int, ...], ArrayChunk]
    fill_value: Any = None
    dimension_names: list[str | None] | None = None
    attributes: dict[str, Any] | None = None
    chunk_key_encoding: dict[str, Any] | None = None

    def grid(self) -> dict[str, Any]:
        """The chunk grid: `regular`, or `rectilinear` when an axis's lengths are given as a list
        (each an int or a `[length, count]` run, as refkv's own rectilinear grids give them)."""
        if all(isinstance(c, int) for c in self.chunk_shape):
            return {"name": "regular", "configuration": {"chunk_shape": list(self.chunk_shape)}}
        return {"name": "rectilinear", "configuration": {"kind": "inline", "chunk_shapes": [
            c if isinstance(c, int) else [list(x) if isinstance(x, (list, tuple)) else x for x in c]
            for c in self.chunk_shape]}}

    def document(self, *, attributes: Mapping[str, Any] | None = None,
                 dimension_names: Sequence[str | None] | None = None) -> dict[str, Any]:
        """The array's `zarr.json`, as a dict; `attributes` and `dimension_names`, when given, replace
        the array's."""
        doc: dict[str, Any] = {
            "zarr_format": 3, "node_type": "array", "shape": list(self.shape), "data_type": self.data_type,
            "chunk_grid": self.grid(),
            "chunk_key_encoding": self.chunk_key_encoding or {"name": "default", "configuration": {"separator": "/"}},
            "fill_value": _zero(self.data_type) if self.fill_value is None else self.fill_value,
            "codecs": list(self.codecs),
            "attributes": dict(attributes if attributes is not None else (self.attributes or {})),
        }
        names = dimension_names if dimension_names is not None else self.dimension_names
        if names is not None:
            doc["dimension_names"] = list(names)
        return doc

    def entries(self, *, to: str = "", attributes: Mapping[str, Any] | None = None,
                dimension_names: Sequence[str | None] | None = None) -> Written:
        """The array's entries at path `to` (`""` for the root): its `zarr.json`, and its chunks at
        their keys in its chunk key encoding. `attributes` and `dimension_names`, when given,
        replace the array's."""
        prefix = f"{to.strip('/')}/" if to.strip("/") else ""
        doc = self.document(attributes=attributes, dimension_names=dimension_names)
        out = Written()
        out[f"{prefix}zarr.json"] = json.dumps(doc, separators=(",", ":")).encode()
        for coords, v in self.chunks.items():
            out[f"{prefix}{chunk_key(list(coords), doc)}"] = v if isinstance(v, bytes) else value(v)
        return out


class Written(MutableMapping[str, "bytes | Value"]):
    """An output's entries, keys from its root, each bytes or a value, with their placement:
    `at_open`, the bytes entries placed explicitly, each read at open by a vzip reader (True) or kept
    for later (False), every other bytes entry following refkv's default (a Zarr document is read at
    open); and `stored`, those kept uncompressed. `written | other` and `written |= other` (another
    `Written` or a plain mapping) keep every placement; checks travel with the values, so a plain
    dict loses only placements."""

    def __init__(self, entries: Mapping[str, bytes | Value | Part | Sequence[Part]] | None = None, *,
                 at_open: Mapping[str, bool] | None = None, stored: set[str] | None = None) -> None:
        self._entries: dict[str, bytes | Value] = {}
        self.at_open: dict[str, bool] = dict(at_open or {})
        self.stored: set[str] = set(stored or ())
        for k, v in (entries or {}).items():
            self[k] = v

    def __getitem__(self, key: str) -> bytes | Value:
        return self._entries[key]

    def __setitem__(self, key: str, v: bytes | bytearray | memoryview | Value | Part | Sequence[Part]) -> None:
        self._entries[key] = bytes(v) if isinstance(v, (bytes, bytearray, memoryview)) else value(v)

    def __delitem__(self, key: str) -> None:
        del self._entries[key]
        self.at_open.pop(key, None)
        self.stored.discard(key)

    def __iter__(self) -> Iterator[str]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:
        return f"Written({len(self)} entries)"

    def _merge(self, other: Mapping[str, Any]) -> None:
        for k, v in other.items():
            self[k] = v
        if isinstance(other, Written):
            self.at_open |= other.at_open
            self.stored |= other.stored

    def __ror__(self, other: Mapping[str, Any]) -> Written:
        out = Written(other)
        out._merge(self)
        return out

    def __or__(self, other: Mapping[str, Any]) -> Written:
        out = Written(self, at_open=self.at_open, stored=self.stored)
        out._merge(other)
        return out

    def __ior__(self, other: Mapping[str, Any]) -> Self:  # type: ignore[override,misc]
        self._merge(other)
        return self
