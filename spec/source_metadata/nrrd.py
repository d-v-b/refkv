"""The NRRD convention's source metadata (spec/zarrify/nrrd.md), for spec/generate_schemas.py."""

SCHEMA = {"anyOf": [{
    "type": "object",
    "description": "The root's: the header's magic, fields, key/value pairs and comments, and what the layout "
                   "derives from them (spec/zarrify/nrrd.md §5.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#51-the-root>)",
    "properties": {
        "magic": {"enum": ["NRRD0001", "NRRD0002", "NRRD0003", "NRRD0004", "NRRD0005"]},
        "fields": {
            "type": "object",
            "description": "Every field of the header, by the name the NRRD format gives it (an alternate "
                           "identifier, such as `datafile`, named as its field), its descriptor as text",
            "propertyNames": {"enum": [
                "dimension", "type", "block size", "encoding", "endian", "content", "min", "max", "old min",
                "old max", "data file", "line skip", "byte skip", "number", "sample units", "sizes", "spacings",
                "thicknesses", "axis mins", "axis maxs", "centers", "labels", "units", "kinds", "space",
                "space dimension", "space units", "space origin", "space directions", "measurement frame"]},
            "additionalProperties": {"$ref": "#/$defs/text"},
            "required": ["dimension", "type", "encoding", "sizes"],
        },
        "key_values": {
            "type": "object",
            "description": "The key/value pairs, unescaped: each key's last value",
            "additionalProperties": {"$ref": "#/$defs/text"},
        },
        "comments": {"type": "array", "items": {"$ref": "#/$defs/text"},
                     "description": "The comments, in order, each without its leading # and spaces"},
        "data_type": {"enum": ["int8", "uint8", "int16", "uint16", "int32", "uint32", "int64", "uint64",
                               "float32", "float64"]},
        "encoding": {"enum": ["raw", "gzip"]},
        "byte_order": {"enum": ["little", "big"]},
        "axes": {"type": "array", "items": {"enum": ["t", "c", "z", "y", "x"]}, "minItems": 2, "maxItems": 5,
                 "description": "Each header axis's image axis, in the header's order (the fastest first)"},
        "data_file": {"type": "string", "description": "A detached header's data file: its key, below the "
                                                       "header's directory"},
        "orientation": {
            "type": "object",
            "description": "Present when the header has space directions: whether they place the image",
            "properties": {"applied": {"type": "boolean"}},
            "required": ["applied"],
            "additionalProperties": False,
        },
    },
    "required": ["magic", "fields", "key_values", "comments", "data_type", "encoding", "axes"],
    "additionalProperties": False,
},
    {"$ref": "#/$defs/mirror"},
    {"$ref": "#/$defs/mirrorView"},
]}
