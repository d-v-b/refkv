"""The TIFF convention's source metadata (spec/zarrify/tiff.md), for
spec/generate_schemas.py."""

SCHEMA = {"anyOf": [
    {
        "type": "object",
        "description": "The root's: the file's byte order and variant",
        "properties": {"byte_order": {"enum": ["little", "big"]}, "bigtiff": {"type": "boolean"},
                       "files": {"type": "array", "items": {"type": "string"}}, "micromanager": {"type": "object"}},
        "required": ["byte_order", "bigtiff"],
        "additionalProperties": False,
    },
    {"$ref": "#/$defs/mirror"},
    {"$ref": "#/$defs/mirrorView"},
]}
