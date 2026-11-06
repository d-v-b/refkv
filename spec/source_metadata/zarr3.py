"""The Zarr v3 convention's source metadata (spec/zarrify/zarr3.md), for
spec/generate_schemas.py."""

SCHEMA = {"anyOf": [
    {
        "type": "object",
        "description": "A node's (spec/zarrify/zarr3.md §4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#4-source-metadata>)",
        "properties": {
            "attributes": {"type": "object", "minProperties": 1,
                           "description": "The attributes moved out of the node's place: refkv_zarrified, and a "
                                          "zarr_conventions that is not a nonempty array of other CMOs"},
            "not_presented": {"type": "array", "minItems": 1, "items": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["path", "reason"],
                "additionalProperties": False,
            }, "description": "The root's: the nodes left out, and why"},
        },
        "minProperties": 1,
        "additionalProperties": False,
    },
    # refkv_source's: a store IR's or a zip file's IR's description, and the recorded ignored keys
    # (§5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#5-other-objects>)
    {"allOf": [{"$ref": "#/$defs/storeMirror"}, {"properties": {"empty_dirs": False}}]},
    {"$ref": "#/$defs/mirrorView"},
]}
