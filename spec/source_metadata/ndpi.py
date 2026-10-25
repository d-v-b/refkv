"""The NDPI convention's source metadata (spec/zarrify/ndpi.md), for
spec/generate_schemas.py."""

SCHEMA = {"anyOf": [{"$ref": "#/$defs/mirror"}, {"$ref": "#/$defs/mirrorView"}]}
