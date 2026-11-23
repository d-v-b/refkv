"""The pieces of refkv's own convention that every one of its outputs writes the same way, each
the binding of the function the built-in projections call (the design of d-v-b/refkv#278, "The
routines"): `grid`, the cut of contiguous values into chunks; `filled`, a chunk the cut says to
copy, as its bytes; `gzip_member`, a deflate stream framed for the `gzip` codec; `mirror`, the IR
mirror's entries; and `declaration`, a node's attributes with the convention declared. A
projection written with them and the values of `refkv.build` writes what refkv's own writes,
which the rebuild tests show (`python/tests/test_rebuild_npy.py`)."""

from __future__ import annotations

import json
import operator
from typing import TYPE_CHECKING, Any

from refkv import _core
from refkv.errors import Rejected

from .values import Copy, Data, Literal, Part, Ref, Shared, Value, Written, Zeros, concat, value

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .parsed import Parsed

__all__ = ["declaration", "filled", "grid", "gzip_member", "mirror"]

U64 = (1 << 64) - 1


def grid(span: Ref, shape: Sequence[int], item_size: int, *,
         limit: int) -> tuple[list[int], dict[tuple[int, ...], Value]]:
    """A C-order array of `shape`, its elements of `item_size` bytes each, contiguous in `span`, cut
    into chunks of at most `limit` bytes as spec/conventions.md §7
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays> cuts
    contiguous values: the chunk shape it chooses (whole trailing slabs), and the chunks by their
    coordinates.

    A chunk is its range of `span`. An edge chunk, shorter than the chunk shape, is its range and
    then `Zeros`, the padding; where that reference would pass the payload limit (the input's
    `Parsed.chunking.copy_threshold`, as refkv's own output cuts it), its range is a `Copy`, which
    the caller writes as bytes (`filled`). An array with an axis of length 0 has no chunks.

    ValueError when `span` is not as long as the array, or an element is larger than `limit`."""
    if not isinstance(span, Ref):
        raise TypeError(f"a span is a Ref, the range that holds the array, not {type(span).__name__}")
    dims = [operator.index(n) for n in shape]
    item, most = operator.index(item_size), operator.index(limit)
    if any(not 0 <= n <= U64 for n in (*dims, item, most)) or item == 0 or most == 0:
        raise ValueError("a shape's lengths are 0 to 2^64 - 1, and an element and a chunk hold at least a byte")
    count = item
    for n in dims:
        count *= n
    if count != span.length:
        raise ValueError(f"an array of shape {dims} and {item}-byte elements is {count} bytes, and the span "
                         f"{span.length}")
    if item > most:
        raise ValueError(f"an element of {item} bytes is larger than a chunk's {most}")
    cut = _core.grid_chunks(span.offset, dims, item, most, span.parsed.chunking.copy_threshold)
    if cut is None:
        raise ValueError(f"an array of shape {dims} and {item}-byte elements cannot be cut into chunks")
    chunk_shape, rows = cut
    chunks: dict[tuple[int, ...], Value] = {}
    for coords, offset, length, pad, copied in rows:
        ref = Ref(span.parsed, span.source, offset, length)
        parts: list[Part] = [Copy(ref) if copied else ref]
        if pad:
            parts.append(Zeros(pad))
        chunks[tuple(coords)] = Value(tuple(parts))
    return list(chunk_shape), chunks


def filled(v: Value | Part | Sequence[Part] | bytes) -> bytes | Value:
    """A value with a `Copy` as the bytes it stands for, which an output holds as a bytes entry:
    each `Copy` read through its input (`Parsed.bytes`, so against its `max_bytes`), and the
    literals, zeros and data beside it written out. A value with no `Copy`, and bytes (an `Array`'s
    chunk may be either), are returned as they are.

    ValueError for a `Ref` beside a `Copy`: bytes of a source are copied only where a `Copy` says
    so."""
    if isinstance(v, bytes):
        return v
    val = value(v)
    if not any(isinstance(p, Copy) for p in val.parts):
        return val
    out = bytearray()
    for p in val.parts:
        if isinstance(p, Copy):
            out += p.ref.parsed.bytes((p.ref.source, p.ref.offset, p.ref.length))
        elif isinstance(p, Zeros):
            out += bytes(p.length)
        elif isinstance(p, (Literal, Shared)):
            out += p.data
        elif isinstance(p, Data):
            out += p.data[p.offset:p.offset + p.size]
        else:  # a Ref: a value of the wrong parts, not a part of the wrong type
            raise ValueError("a Ref beside a Copy: the bytes of a source are copied only where a Copy says so")  # noqa: TRY004
    return bytes(out)


def gzip_member(stream: Value | Part | Sequence[Part], *, crc32: int, size: int) -> Value:
    """A raw deflate stream (a ZIP member's, method 8) as a gzip member, which the `gzip` codec
    reads: the header the built-in projections write, the stream, and the trailer, `crc32` the
    CRC-32 of what the stream inflates to and `size` its length (written modulo 2^32, as gzip
    holds it). The stream's own bytes are referenced, not copied."""
    crc, length = operator.index(crc32), operator.index(size)
    if not 0 <= crc < 1 << 32 or not 0 <= length <= U64:
        raise ValueError("a CRC-32 is 0 to 2^32 - 1, and a size 0 to 2^64 - 1")
    header, trailer = _core.gzip_frame(crc, length)
    return concat(Literal(header), stream, Literal(trailer))


def mirror(parsed: Parsed) -> Written:
    """The IR mirror of an input (spec/conventions.md §8
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#8-the-ir-mirror>): the entries
    refkv's own output holds under `refkv_source`, their keys from the output's root, with their
    placement (the mirror's documents are not read when the archive is opened): the IR's table,
    its view, and what the input's convention records beside them, the keys and directories a
    store's or an archive's listing ignored. All of them are bytes entries. A store's objects,
    which its own output also holds there (`refkv_source/objects`), are its projection's to
    write, not the mirror's.

    It reads nothing, so it may be made of a closed input, and while the input's built-in output
    is being made. Raises `refkv.zarrify.Rejected` for an IR whose mirror would pass its budget,
    as `zarrify` does."""
    try:
        _, entries, refs, _, lazy, _ = parsed._mirrored.entries()  # noqa: SLF001 (the parse's IR and description)
    except _core.Rejected as e:
        raise Rejected(str(e)) from None
    if refs:  # the mirror copies what it holds: a reference would be a change to the core's mirror
        raise AssertionError(f"the IR mirror holds a reference, at {refs[0][0]!r}")
    out = Written()
    prefixes = tuple(lazy)
    for key, document in entries:
        out[key] = bytes(document)
        if key.rpartition("/")[2] == "zarr.json" and key.startswith(prefixes):
            out.at_open[key] = False
    return out


def declaration(parsed: Parsed, path: str = "", *, own: object = None,
                attributes: Mapping[str, Any] | None = None, url: str | None = None) -> dict[str, Any]:
    """The attributes of the node at `path` of an output that follows refkv's convention for
    `parsed`'s profile (spec/conventions.md §2
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#2-attributes>): `attributes`,
    and the declaration, `zarr_conventions` and `refkv_zarrified`. The root's (`path` `""`) names
    the profile, its version and the source, by the URL the output names the input by (`url`,
    `parsed.url` when None: an HDF5 store's root names its named file, `Hdf5Hierarchy.url`); `own` is
    the node's source metadata, as the profile's document specifies it (`parsed.description` holds
    it where the profile has one). A node that is not the root and has no source metadata declares
    nothing: its attributes are `attributes`.

    A declaration is a claim that the node's subtree follows the convention, which `seal` does not
    check; `refkv.zarrify.layouts.hierarchy.check` does."""
    if not isinstance(path, str):
        raise TypeError(f"a path is a str, not {type(path).__name__}")
    return dict(json.loads(_core.coding("declare", json.dumps({
        "profile": parsed.profile, "url": None if path.strip("/") else (parsed.url if url is None else url), "own": own,
        "attributes": dict(attributes or {})}))))
