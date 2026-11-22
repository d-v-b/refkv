"""How the layout `refkv.virtualizarr` presents a netCDF3 file, as VirtualiZarr 2.8.0's `NetCDF3Parser`
does (`virtualizarr/parsers/netcdf3.py`).

**netCDF3** (`NetCDF3Parser`): the variables and dimension names the netCDF3 convention presents
(`refkv.build.netcdf3_hierarchy`, made from the parser's description: no output of refkv's), the
attributes it presents with a float attribute's values as numbers, and a variable's fill value
its `_FillValue` in its data type, else zarr-python's default, 0.

Where `NetCDF3Parser`'s presentation is a defect rather than a contract, the layout keeps refkv's:

- netCDF3 text that is not UTF-8 is refkv's `{"latin1": T}` (spec/conventions.md §6
  <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#6-source-metadata-as-json>), where
  `NetCDF3Parser` writes replacement characters, and fails on such a name;
- the netCDF3 variables refkv does not present (spec/zarrify/netcdf3.md §2
  <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#2-the-hierarchy>), and a
  variable's `_FillValue` it does not present (§5
  <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#5-attributes>), which
  `NetCDF3Parser` fails to encode or encodes as no reader decodes it, the variable's fill value
  then refkv's;
- a netCDF3 variable's integer `_FillValue` that its data type does not hold, on which
  `NetCDF3Parser` fails, leaves the variable refkv's fill value.

Given refkv's own output as a base, `check` would report `copy` at a variable whose fill value is
`NetCDF3Parser`'s rather than that of refkv's own output."""

import base64
import math
import struct
from collections.abc import Mapping
from typing import Any

from refkv.build import Parsed, Written, filled
from refkv.build.routines.netcdf3 import Netcdf3Hierarchy, netcdf3_hierarchy
from refkv.zarrify.coding import hdf5_value
from refkv.zarrify.layouts import dump

from ._common import _float32, _int_range, _nonfinite

NC_FLOAT_SIZES = {"NC_FLOAT": 4, "NC_DOUBLE": 8}


def _zarr_float(value: float) -> object:
    """A float as a Zarr v3 fill value: a number, or `"NaN"`, `"Infinity"`, `"-Infinity"`."""
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    return value


def _netcdf3_attributes(pr: Netcdf3Hierarchy, path: str, token: bool) -> dict[str, Any]:
    """The netCDF3 attributes of the node at `path` as `NetCDF3Parser` presents them: a float
    attribute's float values (netcdf3.md §5
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#5-attributes> writes them as
    hdf5.md's float values) as numbers, NaN and the infinities as `_nonfinite` writes them."""
    metadata = pr.source_metadata(path) or {}
    types = metadata.get("attributes", {}) if isinstance(metadata, dict) else {}
    out: dict[str, Any] = {}
    for name, given in pr.attributes(path).items():
        size = NC_FLOAT_SIZES.get(str((types.get(name) or {}).get("type")))
        floats = size is not None and not (name == "_FillValue" and path)  # a variable's: FillValueCoder's form
        out[name] = _nonfinite(hdf5_value(given, {"class": "float", "size": size}) if floats else given, token)
    return out


def _netcdf3_fill(doc: Mapping[str, Any], attributes: Mapping[str, Any], unpresented: Mapping[str, Any]) -> object:
    """The fill value `NetCDF3Parser` gives a variable: its `_FillValue`, in the variable's data
    type, else zarr-python's default (0); refkv's where netcdf3.md §5
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#5-attributes> does not
    present the `_FillValue` (one `NetCDF3Parser` fails to encode, or one on an `NC_CHAR` variable)
    or the variable's data type does not hold it."""
    data_type = doc.get("data_type")
    if not isinstance(data_type, str) or "_FillValue" in unpresented:
        return doc.get("fill_value")
    fill = attributes.get("_FillValue")
    if data_type.startswith("float"):
        if isinstance(fill, str):  # the Base64 of its binary64 (FillValueCoder's)
            value = float(struct.unpack("<d", base64.standard_b64decode(fill))[0])
            return _zarr_float(_float32(value) if data_type == "float32" else value)
        return 0.0
    if not isinstance(fill, int) or isinstance(fill, bool):
        return 0
    # an integer of another type the variable's does not hold, on which NetCDF3Parser fails: refkv's
    return fill if _int_range(data_type)[0] <= fill <= _int_range(data_type)[1] else doc.get("fill_value")


def _netcdf3(pr: Netcdf3Hierarchy, token: bool) -> Written:
    out = Written()
    for path in pr.nodes():
        prefix = f"{path}/" if path else ""
        attributes = _netcdf3_attributes(pr, path, token)
        if pr.node_type(path) == "group":
            out[f"{prefix}zarr.json"] = dump({"zarr_format": 3, "node_type": "group", "attributes": attributes},
                                             allow_nan=token)
            continue
        doc, chunks = pr.array_entries(path)
        metadata = pr.source_metadata(path) or {}
        unpresented = metadata.get("unpresented_attributes", {}) if isinstance(metadata, dict) else {}
        doc = {**doc, "attributes": attributes, "fill_value": _netcdf3_fill(doc, attributes, unpresented)}
        out[f"{prefix}zarr.json"] = dump(doc, allow_nan=token)
        out.update({f"{prefix}{k}": filled(v) for k, v in chunks.items()})
    return out


def write(parsed: Parsed, token: bool) -> Written:
    """A netCDF3 file as `NetCDF3Parser` presents it; `token` whether a NaN or infinite attribute
    is a bare JSON token (the option `nonfinite`)."""
    return _netcdf3(netcdf3_hierarchy(parsed), token)

