"""What the profiles of the layout `refkv.virtualizarr` share: the conversions of values that
VirtualiZarr's parsers of several formats make alike."""

import base64
import math
import struct
from collections.abc import Mapping
from typing import Any

from refkv.build import Value
from refkv.zarrify.layouts import chunk_coords, chunk_key


def _nonfinite(value: object, token: bool) -> object:
    """`value`, each NaN or infinity in it as zarr-python writes it (a float, which `dump` writes as
    a bare token), or as the text `"NaN"`, `"Infinity"`, `"-Infinity"`."""
    if isinstance(value, float) and not math.isfinite(value):
        if token:
            return value
        return "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
    if isinstance(value, list):
        return [_nonfinite(v, token) for v in value]
    if isinstance(value, dict):
        return {k: _nonfinite(v, token) for k, v in value.items()}
    return value


def _fill_value_coded(value: object, kind: str | None) -> object:
    """A netCDF `_FillValue` as xarray's `FillValueCoder` encodes it for a dataset of datatype
    class `kind`: a float as the Base64 of its binary64, an integer as an integer."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    if kind == "float":
        return base64.standard_b64encode(struct.pack("<d", float(value))).decode()
    if kind in ("integer", "enum", "bitfield"):
        return int(value)
    return value


def _without_byte_axis(doc: dict[str, Any], chunks: Mapping[str, bytes | Value], name: str, length: int,
                       fill: object) -> tuple[dict[str, Any], dict[str, bytes | Value]]:
    """An array refkv's own output holds as uint8 with a trailing axis of its items' `length` bytes
    as one of the data type `name` (`null_terminated_bytes` or `raw_bytes`) of that length without
    the axis, of fill value `fill`, its chunks at the keys the shorter grid gives them."""
    doc = {**doc, "shape": doc["shape"][:-1],
           "chunk_grid": {"name": "regular", "configuration": {
               "chunk_shape": doc["chunk_grid"]["configuration"]["chunk_shape"][:-1]}},
           "data_type": {"name": name, "configuration": {"length_bytes": length}}, "fill_value": fill}
    if doc.get("dimension_names") is not None:
        doc["dimension_names"] = doc["dimension_names"][:-1]
    before = {**doc, "shape": [*doc["shape"], length]}
    return doc, {chunk_key(chunk_coords(k, before)[:-1], doc): v for k, v in chunks.items()}


def _node_name(name: str) -> bool:
    """Whether a Zarr node may be named `name`."""
    return bool(name) and "/" not in name and name not in (".", "..") and not name.startswith("__")


def _float32(value: float) -> float:
    """`value` rounded to the nearest binary32, as numpy casts it."""
    try:
        return float(struct.unpack("<f", struct.pack("<f", value))[0])
    except OverflowError:  # beyond binary32's range: an infinity, as numpy casts it
        return math.copysign(math.inf, value)


def _int_range(data_type: str) -> tuple[int, int]:
    """The least and greatest values of the integer data type `data_type` (`int16`, `uint8`, ...)."""
    bits = int(data_type.lstrip("uint"))
    return (0, 2**bits - 1) if data_type.startswith("u") else (-(2 ** (bits - 1)), 2 ** (bits - 1) - 1)
