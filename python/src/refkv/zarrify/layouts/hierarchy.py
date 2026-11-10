"""Building and checking a plugin layout's Zarr documents (spec/layout.md
<https://github.com/d-v-b/refkv/blob/main/spec/layout.md>), through zarr-metadata's models of Zarr
v2 and v3 metadata: `copy` an array of the base, `array` and `group` documents written by hand,
and `check`, which a plugin's tests call on its output. It needs the `layouts` extra
(`pip install refkv[layouts]`).

    from refkv.zarrify.layouts import base
    from refkv.zarrify.layouts.hierarchy import check, copy, group

    b = base("image.ome.tif")
    out = {"zarr.json": group(), **copy(b, "0", to="0")}
    assert check(b, out) == []
"""

from __future__ import annotations

import json
import math
import operator
import posixpath
from collections.abc import Mapping, Sequence
from typing import Any

try:
    from zarr_metadata.model import (
        ZarrV2ArrayMetadata,
        ZarrV2ConsolidatedMetadata,
        ZarrV2GroupMetadata,
        ZarrV3ArrayMetadata,
        ZarrV3GroupMetadata,
    )
except ImportError as e:  # the extra is not installed
    raise ImportError("refkv.zarrify.layouts.hierarchy needs zarr-metadata: pip install refkv[layouts]") from e

from refkv.pb import Concat, Range
from refkv.zarrify.layouts import (
    DOCUMENTS,
    MIRROR,
    REFKV_MEMBERS,
    REFKV_UUIDS,
    Base,
    EntryValue,
    Problem,
    Written,
    _str_keys,
    _without_checksum,
    chunk_coords,
    chunk_key,
    described,
    dump,
    prepare,
    store_of,
)

# chunk_coords and chunk_key are refkv.zarrify.layouts', here too for a plugin that builds arrays
__all__ = ["Problem", "array", "check", "chunk_coords", "chunk_key", "copy", "group", "node_name"]

Value = EntryValue


# ---- copying an array of the base (spec/layout.md)

def _inverse(order: Sequence[int]) -> list[int]:
    out = [0] * len(order)
    for i, o in enumerate(order):
        out[o] = i
    return out


def _with_transpose(codecs: list[dict[str, Any]], inverse: list[int]) -> list[dict[str, Any]]:
    """`codecs` with a transpose of `inverse` first, composed with a transpose they start with:
    transposing by `inverse` and then by the base's order `t` is one transpose by
    `[inverse[t[i]] for each i]`; an identity transpose is left out."""
    if codecs and codecs[0].get("name") == "transpose":
        t = codecs[0]["configuration"]["order"]
        order, rest = [inverse[i] for i in t], codecs[1:]
    else:
        order, rest = inverse, codecs
    if order == list(range(len(order))):
        return list(rest)
    return [{"name": "transpose", "configuration": {"order": order}}, *rest]


def copy(base: Base, path: str, order: Sequence[int] | None = None, attributes: Mapping[str, object] | None = None,
         dimension_names: Sequence[str | None] | None = None, *, to: str | None = None) -> dict[str, Value]:
    """The entries of the array at `path` of `base`, keys relative to the array, its `zarr.json`
    among them: its document with `attributes` as given (`{}` by default) and `dimension_names`
    as given, in the copy's axis order (else the base's, permuted with the axes), and its chunks'
    values as they are. With `order`, a permutation of its axes as `numpy.transpose` takes one
    (the copy's axis `i` is the base's axis `order[i]`), the array transposed: its shape, chunk
    grid, dimension names and chunk keys permuted, and a `transpose` codec of the inverse order
    first (for a sharded array, first in the shard's inner and index codecs), so that it decodes
    to the base's values, transposed. With `to`, the keys are below that path, from the output's
    root. ValueError for arguments it cannot take."""
    doc, chunks = base.array(path)
    n = len(doc["shape"])
    if isinstance(dimension_names, str):
        raise ValueError("dimension_names is a list of names, each a string or None, not a string")  # noqa: TRY004 (as copy's other refusals)
    given = None if dimension_names is None else list(dimension_names)  # each argument read once
    attrs = dict(attributes or {})
    if not _str_keys(attrs):
        raise ValueError("attributes names its members, and those of the objects in it, by strings")
    if given is not None and not all(g is None or isinstance(g, str) for g in given):
        raise ValueError("dimension_names is a list of names, each a string or None")
    if given is not None and len(given) != n:
        raise ValueError(f"{len(given)} dimension names for an array of {n} dimensions")
    names = doc.get("dimension_names") if given is None else given
    if order is not None:
        listed = list(order)
        try:
            indexes = [operator.index(o) for o in listed]
        except TypeError:
            indexes = None
        if indexes is None or any(isinstance(o, bool) for o in listed):
            raise ValueError(f"order {listed} holds the base's axis indexes, integers (as numpy.transpose takes)")
        if sorted(indexes) != list(range(n)):
            raise ValueError(f"order {indexes} is not a permutation of the array's {n} axes")
        order = indexes
    if order is not None and order != list(range(n)):
        inverse = _inverse(order)
        doc["shape"] = [doc["shape"][o] for o in order]
        grid = doc["chunk_grid"]["configuration"]
        for member in ("chunk_shape", "chunk_shapes"):
            if member in grid:
                grid[member] = [grid[member][o] for o in order]
        codecs = list(doc["codecs"])
        if codecs and codecs[0].get("name") == "sharding_indexed":
            config = codecs[0]["configuration"]
            config["chunk_shape"] = [config["chunk_shape"][o] for o in order]
            config["codecs"] = _with_transpose(list(config["codecs"]), inverse)
            config["index_codecs"] = _with_transpose(list(config["index_codecs"]), [*inverse, n])
        else:
            doc["codecs"] = _with_transpose(codecs, inverse)
        if names is not None and given is None:
            names = [names[o] for o in order]
        chunks = {chunk_key([chunk_coords(k, doc)[o] for o in order], doc): v for k, v in chunks.items()}
    doc["attributes"] = attrs
    if names is not None:
        doc["dimension_names"] = list(names)
    else:
        doc.pop("dimension_names", None)
    return _under(to, {"zarr.json": _valid_array(doc), **dict(sorted(chunks.items()))})


def _under(to: str | None, entries: dict[str, Value]) -> dict[str, Value]:
    """`entries`, keys below the path `to` (keys relative to it), or as they are without one."""
    if to is None:
        return entries
    if not isinstance(to, str) or (to and any(s in ("", ".", "..") for s in to.split("/"))):
        raise ValueError(f"to {to!r} is a path from the output's root: segments, none empty, `.` or `..`")
    return {f"{to}/{k}" if to else k: v for k, v in entries.items()}


def _valid_array(doc: Mapping[str, Any]) -> bytes:
    """An array document as bytes, once zarr-metadata's model holds it valid (ValueError, its
    error chained, otherwise)."""
    try:
        ZarrV3ArrayMetadata.from_json(doc)
    except ValueError as e:
        raise ValueError(f"not a valid Zarr v3 array document: {e}") from e
    return dump(doc)


# ---- documents written by hand

def _zero(data_type: object) -> object:
    """A data type's zero, as a Zarr v3 fill value; ValueError for one whose zero this does not
    know (`array` is then given `fill_value`)."""
    if isinstance(data_type, dict):
        name = data_type.get("name")
        if name in ("numpy.datetime64", "numpy.timedelta64"):
            return 0
        if name == "fixed_length_utf32":
            return ""
        raise ValueError(f"the data type {name!r} has no zero this knows: give array() its fill_value")
    if data_type == "bool":
        return False
    if isinstance(data_type, str) and data_type.startswith("complex"):
        return [0.0, 0.0]
    if isinstance(data_type, str) and data_type.startswith(("float", "bfloat")):
        return 0.0
    return 0


def array(shape: Sequence[int], data_type: str | Mapping[str, Any], chunk_shape: Sequence[Any],
          codecs: Sequence[Mapping[str, Any]], chunks: Mapping[Any, Value],
          dimension_names: Sequence[str | None] | None = None, attributes: Mapping[str, object] | None = None,
          fill_value: object = None, *, to: str | None = None) -> dict[str, Value]:
    """A Zarr v3 array written by hand: its `zarr.json`, with a `regular` chunk grid of
    `chunk_shape`, or a `rectilinear` one when it gives an axis's lengths as a list (each length
    an int, or a `[length, count]` run, as refkv's own rectilinear grids give them), the
    `default` chunk key encoding with its separator `/`, and the fill value 0 of the data type
    unless given (as Zarr v3 writes it in JSON); then `chunks`, by chunk coordinates (tuples of
    `int`), at their keys, below the path `to` when given. ValueError for a document zarr-metadata's model
    refuses."""
    if not _str_keys(dict(attributes or {})):
        raise ValueError("attributes names its members, and those of the objects in it, by strings")
    rectilinear = any(not isinstance(c, int) for c in chunk_shape)
    grid: dict[str, Any] = (
        {"name": "rectilinear", "configuration": {"kind": "inline", "chunk_shapes": [
            c if isinstance(c, int) else [list(x) if isinstance(x, (list, tuple)) else x for x in c]
            for c in chunk_shape]}}
        if rectilinear else {"name": "regular", "configuration": {"chunk_shape": [int(c) for c in chunk_shape]}})
    doc: dict[str, Any] = {
        "zarr_format": 3, "node_type": "array", "shape": [int(d) for d in shape],
        "data_type": data_type if isinstance(data_type, str) else dict(data_type), "chunk_grid": grid,
        "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
        "codecs": [dict(c) for c in codecs],
        "fill_value": _zero(data_type) if fill_value is None else fill_value,
        "attributes": dict(attributes or {}),
    }
    if dimension_names is not None:
        doc["dimension_names"] = list(dimension_names)
    out: dict[str, Value] = {"zarr.json": _valid_array(doc)}
    for coords in sorted(chunks):
        out[chunk_key(coords, doc)] = chunks[coords]
    return _under(to, out)


def node_name(name: str) -> None:
    """Checks a Zarr v3 node's name, one key segment: not empty, no `/`, not `.` or `..` alone,
    not starting with `__` (which Zarr v3 reserves), not `zarr.json`; ValueError otherwise. A
    plugin checks the names its options give with it, before any base exists."""
    if not isinstance(name, str):
        raise ValueError(f"a node's name is a string, not {type(name).__name__}")  # noqa: TRY004 (a ValueError, as options() raises)
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{name!r} is not a Zarr v3 node's name: it is not valid Unicode") from None
    if not name or "/" in name or "\0" in name or set(name) == {"."} or name.startswith("__") \
            or name == "zarr.json":
        raise ValueError(f"{name!r} is not a Zarr v3 node's name (one segment, not empty, no /, not only dots, "
                         "not starting with __, not zarr.json)")


def group(attributes: Mapping[str, object] | None = None) -> bytes:
    """A Zarr v3 group's `zarr.json`."""
    doc = {"zarr_format": 3, "node_type": "group", "attributes": dict(attributes or {})}
    if not _str_keys(doc["attributes"]):
        raise ValueError("attributes names its members, and those of the objects in it, by strings")
    ZarrV3GroupMetadata.from_json(doc)
    return dump(doc)


# ---- checking a plugin's output (spec/layout.md)

def _no_constant(name: str) -> object:
    raise ValueError(f"{name} is not JSON")


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError(f"an object holds a member twice: {min(k for k in keys if keys.count(k) > 1)!r}")
    return dict(pairs)


def _finite(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"{text} is beyond binary64")
    return value


def _strict(value: bytes) -> object:
    """A document, parsed as strict JSON in UTF-8: no byte order mark, NaN or infinity, member
    twice, or lone surrogate."""
    text = value.decode("utf-8")
    if text.startswith("﻿"):
        raise ValueError("a byte order mark")
    doc = json.loads(text, parse_constant=_no_constant, parse_float=_finite, object_pairs_hook=_no_duplicates)
    stack = [doc]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            item.encode("utf-8")
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return doc


def _dir(key: str) -> str:
    return posixpath.dirname(key)


def _refkv_declared(attrs: object) -> bool:
    """Whether attributes hold a member or CMO of refkv's."""
    if not isinstance(attrs, dict):
        return False
    if any(m in attrs for m in REFKV_MEMBERS):
        return True
    cmos = attrs.get("zarr_conventions")
    return isinstance(cmos, list) and any(isinstance(c, dict) and (
        str(c.get("uuid", "")).lower() in REFKV_UUIDS or c.get("name") in REFKV_MEMBERS) for c in cmos)


def _item_size(data_type: object) -> int | None:
    """The bytes of an element of a Zarr v3 data type: a numeric type's, `bool`'s, a datetime's or
    timedelta's 8, a fixed-length UTF-32 string's, or a `struct`'s fields' summed; None for one
    whose size this does not know."""
    if data_type == "bool":
        return 1
    if isinstance(data_type, dict) and data_type.get("name") in ("numpy.datetime64", "numpy.timedelta64"):
        return 8
    if isinstance(data_type, dict) and data_type.get("name") in ("fixed_length_utf32", "raw_bytes",
                                                                  "null_terminated_bytes"):
        length = (data_type.get("configuration") or {}).get("length_bytes")
        return length if isinstance(length, int) else None
    if isinstance(data_type, dict) and data_type.get("name") == "struct":
        fields = (data_type.get("configuration") or {}).get("fields")
        sizes = [_item_size(f.get("data_type")) for f in fields] if isinstance(fields, list) and all(
            isinstance(f, dict) for f in fields) else [None]
        return None if not sizes or None in sizes else sum(sizes)  # type: ignore[arg-type]
    if not isinstance(data_type, str):
        return None
    digits = data_type.lstrip("abcdefghijklmnopqrstuvwxyz")
    return int(digits) // 8 if digits.isdigit() and int(digits) % 8 == 0 else None


def _lengths(axis: object) -> list[int] | None:
    """A rectilinear grid's chunk lengths along one axis, its `[length, count]` runs expanded;
    None for an axis of one regular length."""
    if isinstance(axis, int):
        return None
    out: list[int] = []
    for x in axis if isinstance(axis, list) else []:
        if isinstance(x, int):
            out.append(x)
        elif isinstance(x, list) and len(x) == 2 and all(isinstance(y, int) for y in x):
            out += [x[0]] * x[1]
    return out


def _grid(doc: Mapping[str, Any]) -> list[list[int]] | None:
    """Each axis's chunk lengths, from the array's shape and its regular or rectilinear grid; None
    for a grid of another kind."""
    shape = doc.get("shape")
    given = doc.get("chunk_grid")
    grid: dict[str, Any] = given if isinstance(given, dict) else {}
    config: dict[str, Any] = grid.get("configuration") or {}
    if not isinstance(shape, list):
        return None
    if grid.get("name") == "regular" and isinstance(config.get("chunk_shape"), list):
        out = []
        for n, c in zip(shape, config["chunk_shape"], strict=False):
            if not isinstance(c, int) or c <= 0:
                return None
            out.append([c] * max(1, -(-n // c)))
        return out
    if grid.get("name") == "rectilinear" and isinstance(config.get("chunk_shapes"), list):
        out = []
        for n, axis in zip(shape, config["chunk_shapes"], strict=False):
            lengths = _lengths(axis)
            out.append(lengths if lengths is not None else [axis] * max(1, -(-n // axis)))
        return out
    return None


def _v2_grid(doc: Mapping[str, Any]) -> list[list[int]] | None:
    shape, chunks = doc.get("shape"), doc.get("chunks")
    if not (isinstance(shape, list) and isinstance(chunks, list) and len(shape) == len(chunks)
            and all(isinstance(c, int) and c > 0 for c in chunks)):
        return None
    return [[c] * max(1, -(-n // c)) for n, c in zip(shape, chunks, strict=True)]


def _fixed_size(doc: Mapping[str, Any]) -> bool:
    """Whether an array's codecs leave a chunk's size fixed: `transpose`, then `bytes`, and
    nothing after (Zarr v3), or no compressor and no filters (Zarr v2)."""
    if doc.get("zarr_format") == 2:
        return doc.get("compressor") is None and not doc.get("filters")
    names = [c.get("name") for c in doc.get("codecs", []) if isinstance(c, dict)]
    return names in (["bytes"], ["transpose", "bytes"])


def _v2_item_size(dtype: object) -> int | None:
    if isinstance(dtype, str) and len(dtype) >= 3 and dtype[0] in "<>|" and dtype[1] in "Mm" and dtype[2] == "8":
        return 8  # a datetime64 or timedelta64, `M8[ns]`
    if isinstance(dtype, str) and len(dtype) >= 3 and dtype[0] in "<>|" and dtype[2:].isdigit():
        return int(dtype[2:]) * (4 if dtype[1] == "U" else 1)
    if isinstance(dtype, list):
        sizes = [_v2_item_size(f[1]) if isinstance(f, list) and len(f) == 2 else None for f in dtype]
        return None if not sizes or None in sizes else sum(sizes)  # type: ignore[arg-type]
    return None


def _models_equal(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Whether two Zarr v3 array documents are one array, attributes and dimension names aside,
    as zarr-metadata's models compare them (a default chunk key separator, written or not, is the
    same)."""
    aside = ("attributes", "dimension_names")
    try:
        return ZarrV3ArrayMetadata.from_json({k: v for k, v in a.items() if k not in aside}) == \
            ZarrV3ArrayMetadata.from_json({k: v for k, v in b.items() if k not in aside})
    except ValueError:
        return False


def _copy_of(base: Base, doc: Mapping[str, Any], held: Mapping[str, Value], references: Mapping[str, Range | Concat],
             copies: dict[tuple[str, tuple[int, ...]], tuple[dict[str, Any], dict[str, Value]]]) -> bool:
    """Whether the array `doc`, holding the chunks `held` (by key relative to it; its references
    among them), is a `copy` of a base array: every chunk at the key `copy` gives it, and none
    left out, attributes and dimension names aside."""
    first = next(iter(references.values()))
    for path in base._arrays_holding(first):  # noqa: SLF001 (the package's own Base)
        base_doc = base.array(path)[0]
        order = _order_of(base_doc, doc)
        for candidate in sorted({tuple(range(len(base_doc["shape"]))), *([order] if order else [])}):
            if (path, candidate) not in copies:
                try:
                    entries = copy(base, path, order=list(candidate))
                except (ValueError, KeyError, TypeError):
                    continue
                document = json.loads(entries.pop("zarr.json"))  # type: ignore[arg-type]
                copies[path, candidate] = (document, entries)
            made, chunks = copies[path, candidate]
            plain = {k: v if isinstance(v, bytes) else _without_checksum(v) for k, v in held.items()}
            if _models_equal(made, doc) and plain == chunks:
                return True
    return False


def _leading_transpose(doc: Mapping[str, Any]) -> list[int] | None:
    """The order of the transpose an array's codecs (a sharded array's inner codecs) start with,
    or None."""
    codecs = doc.get("codecs")
    if isinstance(codecs, list) and codecs and isinstance(codecs[0], dict) \
            and codecs[0].get("name") == "sharding_indexed" and isinstance(codecs[0].get("configuration"), dict):
        codecs = codecs[0]["configuration"].get("codecs")
    if isinstance(codecs, list) and codecs and isinstance(codecs[0], dict) and codecs[0].get("name") == "transpose" \
            and isinstance(codecs[0].get("configuration"), dict):
        order = codecs[0]["configuration"].get("order")
        return order if isinstance(order, list) and all(type(i) is int for i in order) else None
    return None


def _order_of(base_doc: Mapping[str, Any], doc: Mapping[str, Any]) -> tuple[int, ...] | None:
    """The `order` `copy` would have been given to make `doc` of the base's array `base_doc`, read
    from their leading transposes (copy composes its inverse with the base's), or None."""
    n = len(base_doc.get("shape") or [])
    identity = list(range(n))
    t = _leading_transpose(base_doc) or identity
    r = _leading_transpose(doc)
    if sorted(t) != identity:
        return None
    if r is None:
        inverse = _inverse(t)  # copy left out an identity: its inverse undid the base's transpose
    elif sorted(r) == identity:
        inverse = [0] * n
        for i, ti in enumerate(t):
            inverse[ti] = r[i]
    else:
        return None
    return tuple(_inverse(inverse))


def _lenient(value: bytes) -> object:
    """A document parsed as Python's json does (NaN and infinities taken), for checking what a
    document says when it is not strict JSON."""
    return json.loads(value.decode("utf-8", errors="replace"))


def check(base: Base, written: Mapping[str, object] | Written) -> list[Problem]:
    """The problems of a plugin's output (spec/layout.md): `key`, `reference`, `open` and
    `archive`, what `zarrify` refuses; then `document`, `orphan`, `chunk`, `json`, `zarr`,
    `consolidated`, `parent`, `copy`, `size` and `refkv`, which a plugin's tests may allow where
    they expect them. `check(base, base.entries())` finds nothing. Raises TypeError for arguments
    that are not a `Base` and a mapping or `Written`."""
    if not isinstance(base, Base):
        raise TypeError(f"check takes a Base, not {type(base).__name__}")
    if not isinstance(written, (Mapping, Written)):
        raise TypeError(f"check takes the output's entries or a Written, not {type(written).__name__}")
    prepared, problems = prepare(base, written)
    if base.checksums:  # the most the checksums can take, which only reading the bytes tells
        for k in sorted(prepared.fresh):
            why = base.fits(prepared.values[k])
            if why:
                problems.append(Problem(k, "reference", why))
    if not problems:
        problems += store_of(base, prepared, policy=None, info={})[1]
    values = prepared.values
    base_values = base.entries()
    docs: dict[str, Any] = {}
    for k in sorted(values):
        v = values[k]
        last = k.rpartition("/")[2]
        if last not in DOCUMENTS:
            continue
        if not isinstance(v, bytes):
            problems.append(Problem(k, "document", f"a reference at {last}, which a reader reads as JSON"))
            continue
        try:
            docs[k] = _strict(v)
        except (ValueError, RecursionError) as e:  # a UnicodeError among them; arrays nested past the stack
            problems.append(Problem(k, "json", f"not strict JSON in UTF-8: {described(e)}"))
            try:  # what it says is still checked, as a reader that takes NaN reads it
                docs[k] = _lenient(v)
            except (ValueError, RecursionError):
                continue
    nodes: dict[str, str] = {}  # node path -> "group" or "array", Zarr v3 or v2
    versions: dict[str, int] = {}  # node path -> its Zarr version, by its documents
    for k, doc in docs.items():
        last, node = k.rpartition("/")[2], _dir(k)
        why = None
        try:
            if last == "zarr.json":
                kind = doc.get("node_type") if isinstance(doc, dict) else None
                # zarr-metadata's group model refuses a node_type that is not "group"
                model = ZarrV3ArrayMetadata.from_json(doc) if kind == "array" else ZarrV3GroupMetadata.from_json(doc)
                if model.extra_fields:
                    why = f"members Zarr v3 does not define: {', '.join(sorted(model.extra_fields))}"
                nodes[node] = str(kind)
                versions[node] = 3
            elif last == ".zarray":
                v2 = ZarrV2ArrayMetadata.from_json(doc)
                if getattr(v2, "extra_fields", None):
                    why = f"members Zarr v2 does not define: {', '.join(sorted(v2.extra_fields))}"
                chunks = doc.get("chunks") if isinstance(doc, dict) else None
                if isinstance(chunks, list) and not all(isinstance(c, int) and c > 0 for c in chunks):
                    why = f"its chunks {chunks} are not positive"
                nodes[node] = "array"
                versions[node] = 2
            elif last == ".zgroup":
                ZarrV2GroupMetadata.from_json(doc)
                nodes.setdefault(node, "group")
                versions.setdefault(node, 2)
            elif last == ".zattrs" and not isinstance(doc, dict):
                why = f".zattrs is an object, not {type(doc).__name__}"
            elif last == ".zmetadata":
                ZarrV2ConsolidatedMetadata.from_json(doc)
                problems += _consolidated(k, doc, docs)
        except ValueError as e:
            why = str(e)
        if why:
            problems.append(Problem(k, "zarr", why))
        if any(s.startswith("__") for s in node.split("/") if s):
            problems.append(Problem(k, "zarr", "a node's name starts with __, which Zarr v3 reserves"))
        if any(s in ("", ".", "..") for s in node.split("/")) and node:
            problems.append(Problem(k, "zarr", "a node's path has an empty, `.` or `..` segment, which Zarr refuses"))
    for node, _kind in sorted(nodes.items()):
        if not node:
            continue
        parent = _dir(node)
        holder = parent
        while holder and nodes.get(holder) != "array":
            holder = _dir(holder)
        if nodes.get(holder) == "array":
            problems.append(Problem(node, "parent", f"{node} is inside the array {holder or '(the root)'}"))
        elif nodes.get(parent) != "group":
            problems.append(Problem(node, "parent", f"{node}'s parent {parent or '(the root)'} has no group document"))
        elif versions.get(parent) != versions.get(node):  # a reader of one version does not see the other's
            problems.append(Problem(node, "parent", f"{node} is Zarr v{versions.get(node)}, its parent "
                                                    f"{parent or '(the root)'} Zarr v{versions.get(parent)}"))
    held: dict[str, dict[str, Value]] = {}
    for k in sorted(values):
        if k.rpartition("/")[2] in DOCUMENTS:
            continue
        holder = _dir(k)
        while holder not in nodes and holder:
            holder = _dir(holder)
        if nodes.get(holder) != "array":
            if k.startswith(f"{MIRROR}/") and base_values.get(k) == _plain(values[k]):
                # the mirror's objects, as the base holds them (spec/conventions.md §8.9
                # <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#89-stores>)
                continue
            problems.append(Problem(k, "orphan", "a chunk without its array's document above it"))
            continue
        held.setdefault(holder, {})[k[len(holder) + 1:] if holder else k] = values[k]
    copies: dict[tuple[str, tuple[int, ...]], tuple[dict[str, Any], dict[str, Value]]] = {}
    for holder, chunks in sorted(held.items()):
        v3 = docs.get(f"{holder}/zarr.json" if holder else "zarr.json")
        doc = v3 if isinstance(v3, dict) and v3.get("node_type") == "array" else \
            docs.get(f"{holder}/.zarray" if holder else ".zarray")
        if not isinstance(doc, dict):
            continue
        is_v3 = doc is v3
        grid = _grid(doc) if is_v3 else _v2_grid(doc)
        size = _item_size(doc.get("data_type")) if is_v3 else _v2_item_size(doc.get("dtype"))
        references: dict[str, Range | Concat] = {k: v for k, v in chunks.items() if not isinstance(v, bytes)}
        for k, v in sorted(chunks.items()):
            where = f"{holder}/{k}" if holder else k
            try:
                coords = chunk_coords(k, doc)
                canonical = chunk_key(coords, doc)
            except ValueError as e:
                problems.append(Problem(where, "chunk", f"not a chunk key of its array: {e}"))
                continue
            if canonical != k:  # a key the encoding does not give: a reader asks for the other one
                problems.append(Problem(where, "chunk", f"its array's encoding writes this chunk's key {canonical!r}"))
                continue
            if grid is not None and (len(coords) != len(grid) or any(c >= len(g) for c, g in zip(coords, grid,
                                                                                                strict=False))):
                problems.append(Problem(where, "chunk", f"chunk {coords} is outside the array's grid"))
                continue
            if grid is not None and size is not None and _fixed_size(doc):
                want = math.prod(g[c] for c, g in zip(coords, grid, strict=True)) * size
                got = len(v) if isinstance(v, bytes) else v.size
                if got != want:
                    problems.append(Problem(where, "size", f"it holds {got} bytes of a chunk of {want}"))
        if is_v3 and references and all(base.base_value(v) is not None for v in references.values()) \
                and not _copy_of(base, v3, chunks, references, copies):  # type: ignore[arg-type] (is_v3: v3 is a dict)
            problems.append(Problem(holder or "zarr.json", "copy", "its references are not a copy of a base array's, "
                                                                   "each at the key copy() gives it"))
    problems += _refkv_problems(base, values, docs)
    return problems


def _plain(v: Value) -> Value:
    return v if isinstance(v, bytes) else _without_checksum(v)


def _consolidated(key: str, doc: Mapping[str, Any], docs: Mapping[str, Any]) -> list[Problem]:
    """The Zarr v2 documents below `key`'s node that its consolidated metadata does not repeat as
    they are, and those it names that the output does not hold."""
    node = _dir(key)
    prefix = f"{node}/" if node else ""
    given = doc.get("metadata")
    metadata: dict[str, Any] = given if isinstance(given, dict) else {}
    out: list[Problem] = []
    held = {k[len(prefix):]: d for k, d in docs.items() if k.startswith(prefix)
            and k.rpartition("/")[2] in (".zarray", ".zgroup", ".zattrs")}
    for k in sorted(set(held) | set(metadata)):
        if k not in held:
            out.append(Problem(key, "consolidated", f"it names {k}, which the output does not hold"))
        elif k not in metadata:
            out.append(Problem(key, "consolidated", f"it leaves out {k}, which the output holds"))
        elif held[k] != metadata[k]:
            out.append(Problem(key, "consolidated", f"its {k} is not the output's"))
    return out


def _refkv_problems(base: Base, values: Mapping[str, Value], docs: Mapping[str, Any]) -> list[Problem]:
    """refkv's declarations a plugin's output holds other than the base's, or whose subtree is
    not the base's, entry for entry."""
    out: list[Problem] = []
    base_values = base.entries()
    for k, doc in sorted(docs.items()):
        last = k.rpartition("/")[2]
        found: list[tuple[str, object]] = []
        if last == "zarr.json" and isinstance(doc, dict):
            found.append((k, doc.get("attributes")))
        elif last == ".zattrs":
            found.append((k, doc))
        elif last == ".zmetadata" and isinstance(doc, dict) and isinstance(doc.get("metadata"), dict):
            found += [(f"{_dir(k)}/{m}".lstrip("/"), d) for m, d in doc["metadata"].items()]
        if isinstance(doc, dict) and isinstance(doc.get("consolidated_metadata"), dict):
            listed = doc["consolidated_metadata"].get("metadata")
            for m, d in (listed if isinstance(listed, dict) else {}).items():
                found.append((f"{m}/zarr.json", d.get("attributes") if isinstance(d, dict) else None))
        for at, attrs in found:
            if not _refkv_declared(attrs):
                continue
            if at != k or base_values.get(at) != values.get(at):  # a document is bytes: no checksums
                out.append(Problem(at, "refkv", "refkv's declarations, other than the base's at this key"))
                continue
            node = _dir(at)
            prefix = f"{node}/" if node else ""
            mine = {x: v if isinstance(v, bytes) else _without_checksum(v) for x, v in values.items()
                    if x.startswith(prefix)}
            theirs = {x: v for x, v in base_values.items() if x.startswith(prefix)}
            if mine != theirs:
                out.append(Problem(at, "refkv", "a node that keeps refkv's declarations, whose subtree is not the "
                                                "base's, entry for entry"))
    return out
