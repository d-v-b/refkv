"""The QPTIFF convention's source metadata (spec/virtualize/qptiff.md), for
spec/generate_schemas.py."""

SCHEMA = {"anyOf": [
    {
        "type": "object",
        "description": "The root's: the file's byte order and variant (spec/virtualize/qptiff.md §5 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/qptiff.md#5-source-metadata>)",
        "properties": {"byte_order": {"enum": ["little", "big"]}, "bigtiff": {"type": "boolean"}},
        "required": ["byte_order", "bigtiff"],
        "additionalProperties": False,
    },
    {"$ref": "#/$defs/mirror"},
    {"$ref": "#/$defs/mirrorView"},
]}
