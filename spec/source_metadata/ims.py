"""The IMS convention's source metadata (spec/virtualize/ims.md), for
spec/generate_schemas.py."""

SCHEMA = {"anyOf": [
    {"$ref": "#/$defs/mirror"},
    {"$ref": "#/$defs/mirrorView"},
]}
