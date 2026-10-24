"""The CZI convention's source metadata (spec/virtualize/czi.md), for
spec/generate_schemas.py."""

_GUID = {"type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"}

_DIMENSIONS = {"type": "object", "description": "The Start of each of S, B, H, I, R, V the series key has",
               "properties": {k: {"type": "integer"} for k in "SBHIRV"}, "minProperties": 1,
               "additionalProperties": False}

_ATTACHMENT = {"oneOf": [
    {
        "type": "object",
        "description": "An A1 attachment entry, and the form of its data at attachments/<k> (spec/virtualize/czi.md §5.3 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/czi.md#53-elements>)",
        "properties": {
            "name": {"$ref": "#/$defs/text"},
            "content_file_type": {"$ref": "#/$defs/text"},
            "content_guid": _GUID,
            "form": {"enum": ["time_stamps", "focus_positions", "event_list", "bytes", "empty"]},
            "segment_entry": {"type": "string", "contentEncoding": "base64",
                              "description": "The attachment segment's copy of the entry, when it differs"},
        },
        "required": ["name", "content_file_type", "content_guid", "form"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "description": "An entry of another schema: its 128 bytes",
        "properties": {"entry": {"type": "string", "contentEncoding": "base64"}},
        "required": ["entry"],
        "additionalProperties": False,
    },
]}

SCHEMA = {"anyOf": [
    {
        "type": "object",
        "description": "The root's: the file header (spec/virtualize/czi.md §5.1 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/czi.md#51-the-root>)",
        "properties": {
            "version": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2},
            "primary_file_guid": _GUID,
            "file_guid": _GUID,
            "file_part": {"const": 0},
            "update_pending": {"type": "integer"},
        },
        "required": ["version", "primary_file_guid", "file_guid", "file_part", "update_pending"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "description": "An image's: the dimensions that select its series (spec/virtualize/czi.md §4.2 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/czi.md#42-images>)",
        "properties": {"dimensions": _DIMENSIONS},
        "required": ["dimensions"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "description": "A tile array's: its tile position, the planes at its index 0, and its copy (spec/virtualize/czi.md §4.4 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/czi.md#44-tiles>)",
        "properties": {
            "dimensions": _DIMENSIONS,
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "size": {"type": "array", "items": {"type": "integer", "minimum": 1}, "minItems": 2, "maxItems": 2},
            "stored_size": {"type": "array", "items": {"type": "integer", "minimum": 1}, "minItems": 2,
                            "maxItems": 2},
            "planes": {"type": "object", "properties": {k: {"type": "integer"} for k in "tcz"},
                       "required": ["t", "c", "z"], "additionalProperties": False},
            "copy": {"type": "integer", "minimum": 0},
        },
        "required": ["x", "y", "size", "stored_size", "planes", "copy"],
        "additionalProperties": False,
    },
    {"$ref": "#/$defs/mirror"},
    {"$ref": "#/$defs/mirrorView"},
]}
