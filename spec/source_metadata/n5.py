"""The N5 convention's source metadata (spec/zarrify/n5.md), for
spec/generate_schemas.py."""

SCHEMA = {"anyOf": [
    {
        "type": "object",
        "description": "A node's (spec/zarrify/n5.md §5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/n5.md#5-source-metadata>)",
        "properties": {
            "attributes": {"type": "object", "minProperties": 1,
                           "description": "The node's attributes.json without the layout members"},
            "metadata": {
                "type": "object", "minProperties": 1,
                "description": "The layout members the Zarr metadata does not reproduce",
                "properties": {"n5": {"description": "The member n5 (the container's version)"},
                               "compression": {"type": "object", "minProperties": 1,
                                               "description": "The compression's members the codec does not carry"},
                               "compressionType": {"description": "compressionType, when compression is present"}},
                "additionalProperties": False,
            },
        },
        "minProperties": 1,
        "additionalProperties": False,
    },
    # refkv_source's: a store IR's description, and the recorded ignored keys (§5.1
    # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/n5.md#51-the-ir-mirror>)
    {"allOf": [{"$ref": "#/$defs/storeMirror"},
               {"properties": {"ir": {"required": ["objects"]}, "empty_dirs": False}}]},
    {"$ref": "#/$defs/mirrorView"},
]}
