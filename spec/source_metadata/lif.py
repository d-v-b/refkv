"""The LIF convention's source metadata (spec/zarrify/lif.md), for
spec/generate_schemas.py."""

_ATTRIBUTES = {"type": "object", "additionalProperties": {"type": "string"},
               "description": "An XML element's attributes, as text, references decoded, in document order"}

SCHEMA = {"anyOf": [
    {
        "type": "object",
        "description": "The root's: the file's version (spec/zarrify/lif.md §5.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#51-the-root>)",
        "properties": {"version": {"enum": [1, 2]}},
        "required": ["version"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "description": "A series image's: its image element, memory, dimensions and channels, and its field "
                       "(spec/zarrify/lif.md §5.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#52-the-images>)",
        "properties": {
            "image": {"type": "integer", "minimum": 0},
            "path": {"type": "array", "items": {"type": ["string", "null"]}, "minItems": 1},
            "attributes": _ATTRIBUTES,
            "memory": _ATTRIBUTES,
            "dimensions": {"type": "array", "items": _ATTRIBUTES, "minItems": 1, "maxItems": 16},
            "channels": {"type": "array", "items": _ATTRIBUTES, "minItems": 1, "maxItems": 1024},
            "field": {"type": "object", "propertyNames": {"pattern": "^[0-9]+$"},
                      "additionalProperties": {"type": "integer", "minimum": 0}, "minProperties": 1,
                      "description": "The field's index along each field dimension, by its DimID"},
            "tile_scan": _ATTRIBUTES,
            "tile": _ATTRIBUTES,
        },
        "required": ["image", "path", "attributes", "memory", "dimensions", "channels"],
        "additionalProperties": False,
    },
    {"$ref": "#/$defs/mirror"},
    {"$ref": "#/$defs/mirrorView"},
]}
