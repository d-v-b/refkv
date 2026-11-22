"""How the layout `refkv.virtualizarr` presents an HDF5 or netCDF-4 input, as VirtualiZarr 2.8.0's
`HDFParser` does (`virtualizarr/parsers/hdf/hdf.py`).

**HDF5 and netCDF-4** (`HDFParser`): every group and dataset the HDF5 convention presents at its
path (`refkv.build.hdf5_hierarchy`, made from the parser's description: no output of refkv's),
with its `dimension_names` (its dimension scales, else the names netCDF-4 gives an axis without
one), and `[]` for a scalar. Attributes are the source's, as `coding.hdf5_attributes` gives them,
without the
bookkeeping `HDFParser` hides (`CLASS`, `NAME`, `DIMENSION_LIST`, `REFERENCE_LIST`,
`_Netcdf4Dimid`, `_Netcdf4Coordinates`, `_nc3_strict`, `_NCProperties`) nor any whose value is
`"DIMENSION_SCALE"`; an array of one element is that element, an array of fixed-length strings a
list of strings, a null attribute `""`, and a numeric dataset's `_FillValue` is encoded as xarray's
`FillValueCoder` encodes it (a float as the Base64 of its binary64). A fixed-length string
dataset, which refkv's own output holds as uint8 with a trailing axis of its bytes, is a
`null_terminated_bytes` array without that axis, its chunks at the keys the shorter grid gives
them, as `HDFParser` presents it through numpy's `S<n>`; `vlen-utf8` gets its empty
configuration.

Where `HDFParser`'s presentation is a defect rather than a contract, the layout keeps refkv's:

- the names netCDF-4 gives an axis without a dimension scale (a dimension scale of its group of
  the same size, else `phony_dim_<n>`, numbered across the file by size), where `HDFParser` names
  every such axis `phony_dim_<axis>`, which gives one name two sizes in a group that xarray then
  refuses to open;
- the chunks of variable-length strings, which `HDFParser` reads at parse time and inlines;
- refkv's chunk grid, where refkv splits a large contiguous dataset or HDU into chunks within its
  payload limit or `HDFParser` trims an oversized chunk, and the `numcodecs.zlib` level (refkv
  writes 1; a decoder ignores it, and the IR does not hold the filter's client data);
- a one-element array of fixed-length strings on a dataset with dimension names is its string,
  where `HDFParser` keeps a list: refkv's own output writes such an attribute as its element
  (hdf5.md §6 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#6-attributes>), and its
  source metadata does not say which it was;
- the datasets refkv does not present (spec/zarrify/hdf5.md §7.4
  <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#74-what-is-not-presented>), such
  as a fixed-length string dataset whose fill value is not one byte repeated;
- an empty fixed-length string attribute is `""`, not `HDFParser`'s `" "`.

Given refkv's own output as a base, `check` would report `copy` at a fixed-length string dataset,
whose chunks are those of refkv's own output at the keys of a grid without its trailing axis of
bytes."""

import base64
from collections.abc import Mapping
from typing import Any

from refkv.build import Parsed, Written, filled
from refkv.build.routines.hdf5 import Hdf5Hierarchy, hdf5_hierarchy
from refkv.zarrify.coding import hdf5_attributes
from refkv.zarrify.layouts import dump

from ._common import _fill_value_coded, _nonfinite, _without_byte_axis

# the attributes HDFParser hides (virtualizarr/parsers/hdf/hdf.py, _extract_attrs)
HIDDEN = frozenset(["REFERENCE_LIST", "CLASS", "DIMENSION_LIST", "NAME", "_Netcdf4Dimid", "_Netcdf4Coordinates",
                    "_nc3_strict", "_NCProperties"])
VLEN_UTF8: dict[str, object] = {"name": "vlen-utf8", "configuration": {}}  # as zarr-python writes the codec
NUMERIC = frozenset(["integer", "float", "enum", "bitfield"])  # HDF5 datatype classes FillValueCoder encodes


def _one(value: object, kind: object) -> object:
    """An array of one element in all as that element, as HDFParser takes `v.flatten()[0]`, but
    an array of fixed-length strings (datatype class `kind` `string`), which HDFParser keeps a
    list."""
    if kind == "string":
        return value
    inner = value
    while isinstance(inner, list) and len(inner) == 1:
        inner = inner[0]
    return value if isinstance(inner, list) else inner


def _presented(pr: Hdf5Hierarchy, path: str, kind: str | None) -> dict[str, Any]:
    """The attributes HDFParser presents for the node at `path`, `kind` its dataset's datatype
    class (None for a group), as Python values (NaN and the infinities as floats)."""
    metadata = pr.source_metadata(path) or {}
    described = metadata.get("attributes", {}) if isinstance(metadata, dict) else {}
    out: dict[str, Any] = {}
    for name, given in hdf5_attributes(pr, path).items():
        if name in HIDDEN:
            continue
        value = _one(given, (described.get(name) or {}).get("class"))
        if value is None and (described.get(name) or {}).get("class") is not None:
            value = ""  # a null dataspace: h5py's Empty, which HDFParser writes as ""
        if value == "DIMENSION_SCALE":
            continue
        out[name] = _fill_value_coded(value, kind) if name == "_FillValue" and kind in NUMERIC else value
    return out


def _string_length(pr: Hdf5Hierarchy, path: str, doc: Mapping[str, Any]) -> int | None:
    """The length of a fixed-length string dataset the HDF5 convention holds as uint8 with a
    trailing axis of its bytes, or None for another array."""
    metadata = pr.source_metadata(path)
    datatype = metadata.get("datatype") if isinstance(metadata, dict) else None
    if not isinstance(datatype, dict) or datatype.get("class") != "string" or doc.get("data_type") != "uint8":
        return None
    size, shape = datatype.get("size"), doc.get("shape") or []
    chunks = doc["chunk_grid"]["configuration"]["chunk_shape"]
    if type(size) is not int or not shape or shape[-1] != size or chunks[-1] != size:
        return None
    return size


def _bytes_fill(fill: object, length: int) -> str:
    """A uint8 fill value as a `null_terminated_bytes` one: the Base64 of `length` such bytes,
    trailing NULs dropped, as numpy holds `S<n>` (b"" for zero)."""
    byte = fill if isinstance(fill, int) and 0 <= fill < 256 else 0
    return base64.standard_b64encode(bytes([byte] * length).rstrip(b"\0")).decode()


def _hdf5(pr: Hdf5Hierarchy, token: bool) -> Written:
    """An HDF5 input as `HDFParser` presents it, from the HDF5 convention's hierarchy, an edge
    chunk the convention copies written as bytes."""
    out = Written()
    for path in pr.nodes():
        prefix = f"{path}/" if path else ""
        if pr.node_type(path) == "group":
            attrs = _nonfinite(_presented(pr, path, None), token)
            out[f"{prefix}zarr.json"] = dump({"zarr_format": 3, "node_type": "group", "attributes": attrs},
                                             allow_nan=token)
            continue
        doc, chunks = pr.array_entries(path)
        metadata = pr.source_metadata(path) or {}
        datatype = metadata.get("datatype") if isinstance(metadata, dict) else None
        kind = datatype.get("class") if isinstance(datatype, dict) else None
        doc = dict(doc)
        length = _string_length(pr, path, doc)
        if length is not None:
            doc, chunks = _without_byte_axis(doc, chunks, "null_terminated_bytes", length,
                                             _bytes_fill(doc.get("fill_value"), length))
            kind = "string"
        if doc.get("dimension_names") is None and doc["shape"] == []:
            doc["dimension_names"] = list[str]()
        doc["codecs"] = [VLEN_UTF8 if c == {"name": "vlen-utf8"} else c for c in doc["codecs"]]
        doc["attributes"] = _nonfinite(_presented(pr, path, kind), token)
        out[f"{prefix}zarr.json"] = dump(doc, allow_nan=token)
        out.update({f"{prefix}{k}": filled(v) for k, v in chunks.items()})
    return out


def write(parsed: Parsed, token: bool) -> Written:
    """An HDF5 or netCDF-4 input as `HDFParser` presents it; `token` whether a NaN or infinite attribute
    is a bare JSON token (the option `nonfinite`)."""
    return _hdf5(hdf5_hierarchy(parsed), token)

