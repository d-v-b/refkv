"""The precomputed convention's source metadata (spec/zarrify/precomputed.md), for
spec/generate_schemas.py."""

SCHEMA = {"anyOf": [
    {
        "type": "object",
        "description": "The root's (spec/zarrify/precomputed.md §7 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/precomputed.md#7-source-metadata>)",
        "properties": {
            "info": {"type": "object", "description": "The info document, every member"},
            "not_presented": {
                "type": "array", "minItems": 1,
                "description": "The scales that are not presented, in the order of scales",
                "items": {
                    "type": "object",
                    "properties": {"index": {"type": "integer", "minimum": 0,
                                             "description": "The scale's index in scales"},
                                   "reason": {"type": "string", "description": "Why it is not presented"}},
                    "required": ["index", "reason"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["info"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "description": "An array's: the scale it presents",
        "properties": {
            "index": {"type": "integer", "minimum": 0, "description": "The scale's index in scales"},
            "scale": {"type": "object", "description": "The scale, every member"},
        },
        "required": ["index", "scale"],
        "additionalProperties": False,
    },
    # refkv_source's: a store IR's description, and the recorded ignored keys (§7.1
    # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/precomputed.md#71-the-ir-mirror>)
    {"allOf": [{"$ref": "#/$defs/storeMirror"},
               {"properties": {"ir": {"required": ["objects"]}, "empty_dirs": False}}]},
    {"$ref": "#/$defs/mirrorView"},
]}
