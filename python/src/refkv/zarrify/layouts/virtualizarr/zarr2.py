"""How the layout `refkv.virtualizarr` presents a Zarr v2 store, as VirtualiZarr 2.8.0's `ZarrParser`
does (`virtualizarr/parsers/zarr/`).

**Zarr v2** (`ZarrParser`): the nodes refkv's own output presents, each with its `.zattrs` as
attributes, and an array's xarray `_ARRAY_DIMENSIONS` its `dimension_names` rather than an
attribute (where it names each axis with a string or null; another stays an attribute, on which
`ZarrParser` fails). An array of `|S<n>` or `|V<n>`, which refkv's own output holds as uint8 with
a trailing axis of its bytes, is a `null_terminated_bytes` or `raw_bytes` array without that
axis, blosc's `typesize` its length, as `ZarrParser` keeps the source's data type. A root array
is its own chunks.

Where `ZarrParser`'s presentation is a defect rather than a contract, the layout keeps refkv's:

- a Zarr v2 directory's own chunk key encoding, where the `ManifestStore` of `ZarrParser` presents
  the default encoding with a `.` separator;
- the Zarr v2 arrays refkv does not present (spec/zarrify/zarr2.md §3.4
  <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#34-arrays-that-are-not-presented>),
  such as one of the `delta` filter, and no `transpose` codec on a 1-D Fortran-order array, where
  `ZarrParser` writes one that keeps the order.

Given refkv's own output as a base, `check` would report `copy` at an array of bytes, whose chunks
are those of refkv's own output at the keys of a grid without its trailing axis of bytes."""

import base64
import re
from collections.abc import Mapping
from typing import Any

from refkv.build import Parsed, Written
from refkv.build.routines.zarr import ZarrHierarchy, zarr_hierarchy
from refkv.zarrify.layouts import dump

from ._common import _nonfinite, _without_byte_axis

ZARR2_BYTES = re.compile(r"\|(?P<kind>[SV])(?P<length>[1-9][0-9]*)")  # a Zarr v2 dtype of bytes
ZARR2_BYTES_TYPES = {"S": "null_terminated_bytes", "V": "raw_bytes"}


def _zarr2_bytes_fill(fill: object, kind: str, length: int) -> str:
    """The Zarr v3 fill value of a `|S<n>` (`kind` S) or `|V<n>` array of Zarr v2 fill value `fill`
    (the Base64 of its bytes, or null for zero bytes): the Base64 of its `length` bytes, trailing
    NULs dropped for `null_terminated_bytes`, as zarr-python writes them."""
    try:
        raw = base64.standard_b64decode(fill) if isinstance(fill, str) else b""
    except ValueError:
        raw = b""
    raw = raw[:length].ljust(length, b"\0")
    return base64.standard_b64encode(raw.rstrip(b"\0") if kind == "S" else raw).decode()


def _zarr2(pr: ZarrHierarchy, token: bool) -> Written:
    """Each node the Zarr v2 convention presents with its `.zattrs` as attributes (refkv's source
    metadata, zarr2.md §4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#4-source-metadata>),
    an array's `_ARRAY_DIMENSIONS` its `dimension_names`, as `ZarrParser` promotes it."""
    out = Written()
    for path in pr.nodes():
        prefix = f"{path}/" if path else ""
        metadata = pr.source_metadata(path) or {}
        given = metadata.get("attributes") if isinstance(metadata, dict) else None
        attributes: dict[str, Any] = dict(given) if isinstance(given, dict) else {}
        if pr.node_type(path) == "group":
            out[f"{prefix}zarr.json"] = dump({"zarr_format": 3, "node_type": "group",
                                              "attributes": _nonfinite(attributes, token)}, allow_nan=token)
            continue
        doc, chunks = pr.array_entries(path)
        found = metadata.get("metadata") if isinstance(metadata, dict) else None
        source: Mapping[str, Any] = found if isinstance(found, dict) else {}
        dtype = ZARR2_BYTES.fullmatch(str(source.get("dtype")))
        shape, grid = doc["shape"], doc["chunk_grid"]["configuration"]["chunk_shape"]
        length = int(dtype["length"]) if dtype else 0
        if dtype and doc["data_type"] == "uint8" and shape and shape[-1] == length and grid[-1] == length:
            # zarr2.md §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#3-arrays> holds
            # |S<n> and |V<n> as uint8 with a trailing axis; ZarrParser keeps the source's data type
            doc, chunks = _without_byte_axis(doc, chunks, ZARR2_BYTES_TYPES[dtype["kind"]], length,
                                             _zarr2_bytes_fill(source.get("fill_value"), dtype["kind"], length))
            doc["codecs"] = [{**c, "configuration": {**c["configuration"], "typesize": length}}
                             if c.get("name") == "blosc" and "configuration" in c else c for c in doc["codecs"]]
            # an array in Fortran order starts with a transpose, which took the byte axis, last, with it
            first = doc["codecs"][0] if doc["codecs"] else {}
            order = first.get("configuration", {}).get("order") if first.get("name") == "transpose" else None
            if isinstance(order, list) and order and order[-1] == len(doc["shape"]):
                doc["codecs"] = [{**first, "configuration": {**first["configuration"], "order": order[:-1]}},
                                 *doc["codecs"][1:]]
        names = attributes.get("_ARRAY_DIMENSIONS")
        # names a Zarr v3 array takes (ZarrParser fails on others, which stay an attribute)
        if isinstance(names, list) and len(names) == len(doc["shape"]) and \
                all(n is None or isinstance(n, str) for n in names):
            del attributes["_ARRAY_DIMENSIONS"]
            doc["dimension_names"] = names
        doc["attributes"] = _nonfinite(attributes, token)
        out[f"{prefix}zarr.json"] = dump(doc, allow_nan=token)
        out.update({f"{prefix}{k}": v for k, v in chunks.items()})
    return out


def write(parsed: Parsed, token: bool) -> Written:
    """A Zarr v2 store as `ZarrParser` presents it; `token` whether a NaN or infinite attribute
    is a bare JSON token (the option `nonfinite`)."""
    return _zarr2(zarr_hierarchy(parsed), token)

