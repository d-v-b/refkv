"""The NDPI convention's source metadata (spec/virtualize/ndpi.md), for
spec/generate_schemas.py."""

SCHEMA = {"anyOf": [{"$ref": "#/$defs/mirror"}, {"$ref": "#/$defs/mirrorView"}]}
