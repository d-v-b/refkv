"""The GRIB convention's source metadata (spec/zarrify/grib.md), for spec/generate_schemas.py."""

_GRIB = "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md"
_REASONS = ["grid_source", "reduced", "grid", "grid_size", "earth", "product", "statistic", "time", "packing",
            "bitmap", "values"]

UNPRESENTED = {
    "type": "object",
    "description": f"A field the profile does not present ({_GRIB}#type-gribunpresented)",
    "properties": {
        "message": {"type": "integer", "minimum": 0, "description": "Its message, from 0 in the file's order"},
        "field": {"type": "integer", "minimum": 0, "description": "Its field in the message, from 0"},
        "reason": {"enum": _REASONS},
    },
    "required": ["message", "field", "reason"],
    "additionalProperties": False,
}

SCHEMA = {
    "anyOf": [
        {
            "type": "object",
            "description": f"The root's: the file's messages and fields, and those not presented "
                           f"({_GRIB}#type-gribroot)",
            "properties": {
                "messages": {"type": "integer", "minimum": 1},
                "fields": {"type": "integer", "minimum": 1},
                "index": {"type": ["string", "null"],
                          "description": "The index file the run read and the file agreed with"},
                "unpresented": {"type": "array", "items": {"$ref": "#/$defs/gribUnpresented"}},
            },
            "required": ["messages", "fields", "index", "unpresented"],
            "additionalProperties": False,
        },
        {"$ref": "#/$defs/mirror"},
        {"$ref": "#/$defs/mirrorView"},
    ]
}

DEFS = {"gribUnpresented": UNPRESENTED}
