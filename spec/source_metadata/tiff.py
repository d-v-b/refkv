"""The TIFF convention's source metadata (spec/virtualize/tiff.md), for
spec/generate_schemas.py."""

SCHEMA = {"anyOf": [
    {
        "type": "object",
        "description": "The root's: the file's byte order and variant",
        "properties": {"byte_order": {"enum": ["little", "big"]}, "bigtiff": {"type": "boolean"}},
        "required": ["byte_order", "bigtiff"],
        "additionalProperties": False,
    },
    {"$ref": "#/$defs/mirror"},
    {"$ref": "#/$defs/mirrorView"},
]}
