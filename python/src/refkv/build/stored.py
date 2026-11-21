"""`stored_array`: an array the IR describes (spec/stored-array.md
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md>), as a Zarr array of the units the
source stores, their bytes referenced where they lie.

A stored array is the IR's account of one array of the source at the byte level: its shape, how
the source cuts it into units, the codecs that decode a unit, and a map of the units it holds. It
says nothing of an output. This routine is the plainest projection of one: the same shape and the
same units, each a chunk. What the description leaves to whoever reads the source's declarations
(the data type, its byte order, the fill value) the caller gives. refkv's own profiles decide more
on top (other chunks, shards, a presentation of strings), and so may any other layout."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from refkv.ir.stored import Each, StoredArray

from .values import Array, Ref, Value, cut

if TYPE_CHECKING:
    from refkv.ir import Unit

    from .parsed import Parsed

__all__ = ["stored_array", "zarr_codecs"]


def zarr_codecs(chain: Sequence[Any]) -> list[dict[str, Any]]:
    """The Zarr v3 bytes-to-bytes codecs that undo the IR's codec chain `chain`
    (spec/stored-array.md §3 <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#3-codecs>),
    in Zarr's order: the codecs after `bytes` of an array whose units are encoded with it. A chain
    is in decoding order, a unit's bytes first, so Zarr lists its codecs reversed. ValueError for
    a codec this does not translate yet: only the empty chain (units stored as they are) is, until
    the formats whose units are encoded come to read their descriptions."""
    if chain:
        names = ", ".join(str(c.name) for c in chain)
        raise ValueError(f"no Zarr codecs yet for the IR's chain of {names}")
    return []


def _lengths(axis: object, length: int, short: bool) -> int | list[Any]:
    """A storage axis's chunk lengths, as `Array.chunk_shape` takes them: an int for units of one
    length (a regular grid's), or the lengths as a list (a rectilinear one's)."""
    if isinstance(axis, Each):
        n = axis.length
        # under `short`, a last unit the axis's end cuts is stored cut: its own length
        if short and length % n and length > n:
            return [[n, length // n] if length // n > 1 else n, length % n]
        return min(n, length) if short and length else n
    runs = [list(r) if isinstance(r, tuple) else r for r in axis.runs]  # type: ignore[attr-defined]
    return runs or 1  # an axis of no units: any chunk length does


def _unit(parsed: Parsed, unit: Unit) -> Value:
    """The bytes of a unit as a value (spec/stored-array.md §4.1
    <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#41-the-arrow-schema>): what its
    element's recipe gives; with a range, that range of the element's own bytes, taken before any
    recipe; or its ranges of the sources. ValueError for a unit in a member of a run, whose
    element's extent is the run's first member's: this does not place the others yet."""
    if unit.ranges is not None:
        objects = parsed._objects  # noqa: SLF001 (the input's objects, by key)
        return Value(tuple(Ref(parsed, 0 if key is None else objects.index(key), offset, length)
                           for key, offset, length in unit.ranges))
    if unit.element is None:
        raise ValueError(f"the unit at {unit.coords} names neither an element nor ranges")
    if unit.member is not None:
        raise ValueError(f"the unit at {unit.coords} lies in member {unit.member} of a run of elements, "
                         "which stored_array does not place yet")
    space = parsed.ir.space(unit.element)
    if space != 0:
        raise ValueError(f"the unit at {unit.coords} lies in what {parsed.ir.path(space)!r} decodes to, which no "
                         "range of a source holds: reference that element's own bytes (Parsed.encoded) under "
                         "the codec that undoes its transform")
    if unit.range is None:
        return parsed.reference(unit.element)
    return cut(Value((parsed.span(unit.element),)), unit.range[0], unit.range[0] + unit.range[1])


def stored_array(parsed: Parsed, array: int, data_type: str | Mapping[str, Any], *, endian: str | None = None,
                 inner_shape: Sequence[int] = (), fill_value: object = None,
                 dimension_names: Sequence[str | None] | None = None,
                 attributes: Mapping[str, Any] | None = None) -> Array:
    """The stored array at position `array` of `parsed.ir.stored_arrays().arrays`, as a Zarr v3
    `Array` of the units the source stores:

    - its shape is the source's, in the source's own order of the axes; where the storage order
      differs (`axes`: a Fortran-order array), a `transpose` codec comes first, so that a chunk
      decodes from the bytes as stored;
    - its chunk grid is the description's grid: regular where every unit of an axis has one
      length, rectilinear otherwise (a last unit stored cut, under the `short` edge);
    - its chunks are the chunk map's units, at their coordinates, each a value of the bytes that
      hold it;
    - its codecs are `bytes`, of the byte order `endian` (`"little"`, `"big"`, or None for a type
      that has none), then the Zarr codecs of its chain (`zarr_codecs`).

    The caller gives what the description leaves to the source's declarations, which a profile's
    routine reads (`refkv.zarrify.coding.npy_array_type` for a NumPy header): `data_type`, the
    Zarr v3 data type of an element; `endian`; `fill_value` (the data type's zero when None); and
    `inner_shape`, trailing axes that the data type's presentation adds to each element (a
    `uint8` presentation of fixed-length bytes adds their length). `dimension_names`, in the
    array's order, and `attributes` are the caller's too.

    ValueError for an array whose storage the IR does not model (no grid, or no extent: a
    description a profile may still present, from the declaring structure), whose
    elements are not whole bytes, whose units are encoded with more than one chain or skip codecs
    of it, whose chain has no Zarr codecs yet, whose units lie in the members of a run of
    elements (an ND2 file's frames), which this does not place yet, or whose units lie in a
    derived space (a deflated `.npz` member's data, which only its stream, decoded, holds)."""
    stored = parsed.ir.stored_arrays()
    if not 0 <= array < len(stored.arrays):
        raise ValueError(f"no stored array {array}: the IR describes {len(stored.arrays)}")
    a: StoredArray = stored.arrays[array]
    where = a.path or f"element {a.element}"
    if a.grid is None or a.shape is None:
        raise ValueError(f"{where}: the IR does not model this array's storage (it has no grid)")
    if a.item_bits is None or a.item_bits % 8:
        raise ValueError(f"{where}: its elements are {a.item_bits} bits, not whole bytes")
    if len(a.chains) != 1:
        raise ValueError(f"{where}: its units are encoded with {len(a.chains)} chains, not one")
    n = len(a.shape)
    # storage axis i is the source's axis axes[i]
    axes = list(a.axes) if a.axes is not None else list(range(n))
    inner = [int(x) for x in inner_shape]
    short = a.grid.edge == "short"
    lengths = [_lengths(axis, a.shape[i], short) for i, axis in enumerate(a.grid.axes)]
    shape: list[int] = [0] * n
    chunk_shape: list[Any] = [1] * n
    for i, at in enumerate(axes):
        shape[at], chunk_shape[at] = a.shape[i], lengths[i]
    codecs: list[dict[str, Any]] = []
    if a.axes is not None:
        codecs.append({"name": "transpose", "configuration": {"order": [*axes, *range(n, n + len(inner))]}})
    codecs.append({"name": "bytes", "configuration": {"endian": endian}} if endian else {"name": "bytes"})
    codecs += zarr_codecs(stored.chains[a.chains[0]])
    chunks: dict[tuple[int, ...], Value] = {}
    for unit in parsed.ir.units(array):
        if unit.chain is not None or unit.skips:
            raise ValueError(f"{where}: the unit at {unit.coords} is encoded apart from its array's chain")
        coords = [0] * n
        for i, at in enumerate(axes):
            coords[at] = unit.coords[i]
        chunks[*coords, *([0] * len(inner))] = _unit(parsed, unit)
    return Array([*shape, *inner], data_type if isinstance(data_type, str) else dict(data_type),
                 [*chunk_shape, *inner], codecs, dict(chunks), fill_value,
                 None if dimension_names is None else list(dimension_names),
                 None if attributes is None else dict(attributes))
