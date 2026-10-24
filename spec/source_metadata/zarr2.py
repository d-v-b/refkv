"""The Zarr v2 convention's source metadata (spec/virtualize/zarr2.md), for
spec/generate_schemas.py."""

SCHEMA = {"anyOf": [
    {
        "type": "object",
        "description": "A node's (spec/virtualize/zarr2.md §4 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/zarr2.md#4-source-metadata>)",
        "properties": {
            "attributes": {"type": "object", "minProperties": 1, "description": "The node's .zattrs, as copied"},
            "metadata": {"type": "object", "minProperties": 1,
                         "description": "The members of .zarray or .zgroup the Zarr metadata does not reproduce"},
        },
        "minProperties": 1,
        "additionalProperties": False,
    },
    # refkv_source's: a store IR's description, and the recorded ignored keys (§5
    # <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/zarr2.md#5-other-objects>)
    {"allOf": [{"$ref": "#/$defs/storeMirror"},
               {"properties": {"ir": {"required": ["objects"]}, "empty_dirs": False}}]},
    {"$ref": "#/$defs/mirrorView"},
]}
