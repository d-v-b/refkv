"""The ENVI convention's source metadata (spec/zarrify/envi.md), for spec/generate_schemas.py."""

SCHEMA = {"anyOf": [{
    "type": "object",
    "description": "The root's: the header's fields and comments, and what the layout derives from them "
                   "(spec/zarrify/envi.md §5.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#51-the-root>)",
    "properties": {
        "fields": {
            "type": "object",
            "description": "Every field of the header, by its name in lower case, its value as written (a braced "
                           "value with its braces and line ends)",
            "additionalProperties": {"$ref": "#/$defs/text"},
            "required": ["samples", "lines", "bands", "interleave", "data type"],
        },
        "comments": {"type": "array", "items": {"$ref": "#/$defs/text"},
                     "description": "The comments, in order, each without its leading ; and the spaces around it"},
        "data_type": {"enum": ["uint8", "int16", "uint16", "int32", "uint32", "int64", "uint64", "float32",
                               "float64", "complex64", "complex128"]},
        "interleave": {"enum": ["bsq", "bil", "bip"]},
        "byte_order": {"enum": ["little", "big"]},
        "compression": {"enum": ["none", "gzip"]},
        "data_file": {"type": "string", "description": "The data file's key, beside the header file"},
    },
    "required": ["fields", "comments", "data_type", "interleave", "compression", "data_file"],
    "additionalProperties": False,
},
    {"$ref": "#/$defs/mirror"},
    {"$ref": "#/$defs/mirrorView"},
]}
