"""How the layout `refkv.virtualizarr` presents an ENVI raster, as VirtualiZarr 2.8.0's `ENVIParser`
does (`virtualizarr/parsers/envi.py`).

**ENVI** (`ENVIParser`): one array, named by the data file's name without its extension, in the
file's own axis order (the ENVI convention's transpose codec's order) with no transpose, its axes
`band`, `y` and `x` in that order, its chunks the convention's (`refkv.build.envi_image`, made
from the parser's description: no output of refkv's) at the keys of that order, and fill value 0.
Its attributes are the header fields `ENVIParser` carries through (`description`, `wavelength`,
`map info`, ...), each named with `_` for its spaces, a braced value a list of its
items as text, and `data ignore value` as its `_FillValue`, encoded as `FillValueCoder` encodes
it (a complex number as two Base64 binary64s). The root has no attributes.

Where `ENVIParser`'s presentation is a defect rather than a contract, the layout keeps refkv's:

- an ENVI gzip file's header offset is skipped in the inflated stream (refkv's `refkv.skip`
  codec), as GDAL skips it, where `ENVIParser` starts the data at that offset in the compressed
  bytes; the `gzip` level is refkv's 1, not `ENVIParser`'s 5;
- an ENVI `data ignore value` that numpy does not cast to the data type, on which `ENVIParser`
  fails, is the attribute `data_ignore_value`, its text;
- an ENVI header field that is not UTF-8, on which `ENVIParser` fails, is refkv's `{"latin1": T}`,
  braced or not."""

import base64
import struct
from collections.abc import Mapping
from typing import Any

from refkv.build import Parsed, Written
from refkv.build.routines.envi import EnviImage, envi_image
from refkv.zarrify.layouts import chunk_key, dump

from ._common import _float32, _int_range, _node_name

# the header fields ENVIParser carries through as attributes (virtualizarr/parsers/envi.py,
# PASSTHROUGH_ATTR_KEYS), each named with `_` for its spaces
ENVI_PASSTHROUGH = ("description", "sensor type", "acquisition time", "wavelength", "wavelength units", "fwhm", "bbl",
                    "band names", "class names", "class lookup", "classes", "security tag", "map info",
                    "projection info", "coordinate system string", "x start", "y start", "default bands",
                    "default stretch", "z plot titles", "z plot range", "sun azimuth", "sun elevation")
# refkv's axis names (envi.md §4.1
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#41-the-array>), as ENVIParser names them
ENVI_AXES = {"c": "band", "y": "y", "x": "x"}


def _envi_value(raw: object) -> object:
    """A header field's value as ENVIParser gives it: a braced value a list of its comma-separated
    items, stripped, empty ones left out; any other the text. Text that is not UTF-8, refkv's
    `{"latin1": T}`, on which ENVIParser fails, stays that."""
    if not isinstance(raw, str):
        return raw
    text = raw.strip()
    if not text.startswith("{"):
        return raw
    if text.endswith("}"):
        text = text[1:-1]
    items = (item.strip() for item in text.replace("\n", " ").split(","))
    return [item for item in items if item]


def _envi_fill(raw: object, data_type: str) -> object:
    """The `_FillValue` ENVIParser gives `data ignore value` `raw` on an array of `data_type`, as
    xarray's `FillValueCoder` encodes it (a float as the Base64 of its binary64, a complex number as
    two), or None for a value numpy does not cast to it (where ENVIParser fails)."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    try:
        if data_type.startswith("complex"):
            value = complex(text)
            parts = [value.real, value.imag]
            if data_type == "complex64":
                parts = [_float32(p) for p in parts]
            return [base64.standard_b64encode(struct.pack("<d", p)).decode() for p in parts]
        if data_type.startswith("float"):
            value = float(text)
            if data_type == "float32":
                value = _float32(value)
            return base64.standard_b64encode(struct.pack("<d", value)).decode()
        number = int(text)
    except ValueError:
        return None
    low, high = _int_range(data_type)
    return number if low <= number <= high else None


def _envi(image: EnviImage) -> Written:
    """An ENVI raster as ENVIParser presents it, from the ENVI convention's image (`envi_image`): no
    output of refkv's is made."""
    fields: Mapping[str, object] = image.source_metadata().get("fields", {})
    data_file = image.data_key
    stem = data_file.rsplit(".", 1)[0] if "." in data_file.lstrip(".") else data_file
    name = stem if stem and _node_name(stem) else "data"  # ENVIParser's Path(data).stem, else data
    doc = image.document()
    codecs = list(doc["codecs"])
    rank = len(doc["shape"])
    # the file's axis order, outermost first: refkv's transpose codec's order (envi.md §4.1
    # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#41-the-array>), which
    # ENVIParser presents as the array's own
    order = codecs.pop(0)["configuration"]["order"] if codecs[0]["name"] == "transpose" else list(range(rank))
    chunk_shape = doc["chunk_grid"]["configuration"]["chunk_shape"]
    attributes: dict[str, Any] = {key.replace(" ", "_"): _envi_value(fields[key])
                                  for key in ENVI_PASSTHROUGH if key in fields}
    if "data ignore value" in fields:
        fill = _envi_fill(fields["data ignore value"], doc["data_type"])
        if fill is None:  # ENVIParser fails; the text, under a name xarray does not decode
            attributes["data_ignore_value"] = fields["data ignore value"]
        else:
            attributes["_FillValue"] = fill
    data_type = doc["data_type"]
    default: object = 0.0 if data_type.startswith("float") else [0.0, 0.0] if data_type.startswith("complex") else 0
    presented = {
        **doc,
        "shape": [doc["shape"][i] for i in order],
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [chunk_shape[i] for i in order]}},
        "fill_value": default,
        "codecs": codecs,
        "dimension_names": [ENVI_AXES.get(doc["dimension_names"][i], doc["dimension_names"][i]) for i in order],
        "attributes": attributes,
    }
    out = Written({"zarr.json": dump({"zarr_format": 3, "node_type": "group", "attributes": {}}),
                   f"{name}/zarr.json": dump(presented)})
    for coords, value in image.chunks().items():
        out[f"{name}/{chunk_key([coords[i] for i in order], presented)}"] = value
    return out


def write(parsed: Parsed, token: bool) -> Written:  # noqa: ARG001 (every profile's writer takes it)
    """An ENVI raster as `ENVIParser` presents it; `token` whether a NaN or infinite attribute
    is a bare JSON token (the option `nonfinite`)."""
    return _envi(envi_image(parsed))

