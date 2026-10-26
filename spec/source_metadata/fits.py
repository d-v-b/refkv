"""The FITS convention's source metadata (spec/zarrify/fits.md), for
spec/generate_schemas.py."""

from source_metadata import BIG_INTEGER

# The FITS convention's cards (spec/zarrify/fits.md §5.3
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#53-cards>): [keyword, value,
# comment] triples in header order, duplicates kept; null past the 65536-byte budget of a node's document.
FITS_CARDS = {
    "description": "The header's cards, [keyword, value, comment] each, in order: a string, a logical, an integer "
                   "(exact; as a string of digits past 2^53 - 1), a real (its binary64 value), a complex as [re, im], "
                   'null for no value, or {"text": t} for a value the standard does not define; a commentary '
                   "card's text is its comment. Null when the cards would take more than 65536 bytes of JSON",
    "anyOf": [
        {"type": "null"},
        {"type": "array", "items": {
            "type": "array", "minItems": 3, "maxItems": 3,
            "prefixItems": [
                {"type": "string"},
                {"anyOf": [{"type": "null"}, {"type": "boolean"}, {"type": "number"}, {"type": "string"},
                           {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
                           {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"],
                            "additionalProperties": False}]},
                {"type": ["string", "null"]},
            ],
        }},
    ],
}

FITS_NUMBER = {"anyOf": [{"type": ["number", "null"]}, BIG_INTEGER]}

FITS_STRING = {"type": ["string", "null"]}

SCHEMA = {"anyOf": [
    {
        "type": "object",
        "description": "The root's: the table of HDUs and the primary header's cards (spec/zarrify/fits.md §5.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#51-the-root>)",
        "properties": {
            "hdus": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "minimum": 0},
                    "type": {"enum": ["primary", "image", "table", "bintable"]},
                    "kind": {"enum": ["empty", "image", "table"]},
                    "name": FITS_STRING, "version": {"type": ["integer", "null"]},
                    "bitpix": {"enum": [8, 16, 32, 64, -32, -64]},
                    "naxis": {"type": "array", "items": {"type": "integer", "minimum": 0}},
                    "pcount": {"type": "integer", "minimum": 0}, "gcount": {"type": "integer", "minimum": 1},
                    "offset": {"type": "integer", "minimum": 0}, "header_bytes": {"type": "integer", "minimum": 2880},
                    "data_offset": {"type": "integer", "minimum": 2880},
                    "data_bytes": {"type": "integer", "minimum": 0},
                    "compression": {"anyOf": [{"type": "null"}, {
                        "type": "object",
                        "description": "A tile-compressed image's compression (spec/zarrify/fits.md §3.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#33-tile-compressed-images>)",
                        "properties": {
                            "type": {"enum": ["RICE_1", "GZIP_1", "GZIP_2", "NOCOMPRESS"]},
                            "parameters": {"type": "object",
                                           "properties": {"BLOCKSIZE": {"enum": [16, 32]},
                                                          "BYTEPIX": {"enum": [1, 2, 4]}},
                                           "required": ["BLOCKSIZE", "BYTEPIX"], "additionalProperties": False},
                            "zbitpix": {"enum": [8, 16, 32, 64, -32, -64]},
                            "tile": {"type": "array", "items": {"type": "integer", "minimum": 1},
                                     "description": "The tile shape, in the array's order"},
                            "quantize": {"enum": ["NO_DITHER", "SUBTRACTIVE_DITHER_1", "SUBTRACTIVE_DITHER_2"]},
                            "dither_seed": {"type": ["integer", "null"], "minimum": 1, "maximum": 10000},
                            "zblank": {"type": ["integer", "null"]},
                            "tiles": {"type": "integer", "minimum": 0},
                        },
                        "required": ["type", "parameters", "zbitpix", "tile", "quantize", "dither_seed", "zblank",
                                     "tiles"],
                        "additionalProperties": False,
                    }]},
                },
                "required": ["index", "type", "kind", "name", "version", "bitpix", "naxis", "pcount", "gcount",
                             "offset", "header_bytes", "data_offset", "data_bytes", "compression"],
                "additionalProperties": False,
            }},
            "cards": FITS_CARDS,
        },
        "required": ["hdus", "cards"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "description": "An HDU node's: its index and cards, an image's scaling cards, a table's columns",
        "properties": {
            "index": {"type": "integer", "minimum": 0},
            "cards": FITS_CARDS,
            "bscale": FITS_NUMBER, "bzero": FITS_NUMBER, "blank": FITS_NUMBER, "bunit": FITS_STRING,
            "columns": {"type": "array", "items": {
                "type": "object",
                "properties": {"name": FITS_STRING, "form": {"type": "string"}, "unit": FITS_STRING, "dim": FITS_STRING,
                               "scale": FITS_NUMBER, "zero": FITS_NUMBER, "null": {"type": ["string", "number", "null"]}, "disp": FITS_STRING,
                               "bcol": FITS_NUMBER},
                "required": ["name", "form", "unit", "dim", "scale", "zero", "null", "disp", "bcol"],
                "additionalProperties": False,
            }},
        },
        "required": ["index", "cards"],
        "additionalProperties": False,
    },
    {"$ref": "#/$defs/mirror"},
    {"$ref": "#/$defs/mirrorView"},
]}
