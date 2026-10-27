"""The ND2 convention's source metadata (spec/zarrify/nd2.md), for
spec/generate_schemas.py."""

SCHEMA = {"anyOf": [
    {
        "type": "object",
        "description": "The root's: the signature and the decoded chunks it keeps (spec/zarrify/nd2.md §5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nd2.md#5-source-metadata>)",
        "properties": {
            "signature": {"$ref": "#/$defs/text", "description": "The signature chunk's data up to its first NUL"},
            "chunks": {
                "type": "object",
                "description": "Each decoded chunk (lite variant or XML variant) the root keeps, by name, as a "
                               "JSON tree: an object, or [name, value] pairs when a name repeats, when a name is "
                               "an array index (0, 1, ... without leading zeros), or when the object would read "
                               'as a tag ({"utf16": B}, {"int": digits}, {"float": '
                               '"NaN"|"Infinity"|"-Infinity"|"-0"}); at most 65536 bytes of JSON in all',
                "additionalProperties": {"type": ["object", "array"]},
            },
        },
        "required": ["signature", "chunks"],
        "additionalProperties": False,
    },
    {"$ref": "#/$defs/mirror"},
    {"$ref": "#/$defs/mirrorView"},
]}
