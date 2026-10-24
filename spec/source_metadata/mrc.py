"""The MRC convention's source metadata (spec/virtualize/mrc.md), for
spec/generate_schemas.py."""

SCHEMA = {"anyOf": [{
    "type": "object",
    "description": "The root's: the byte order, the kind of file, the data type, the header, and what the layout derives from it (spec/virtualize/mrc.md §5 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mrc.md#5-source-metadata>)",
    "properties": {
        "byte_order": {"enum": ["little", "big"]},
        "kind": {"enum": ["image", "image_stack", "volume", "volume_stack"]},
        "data_type": {"enum": ["int8", "uint8", "int16", "float32", "complex64", "uint16", "float16"]},
        "header": {
            "type": "object",
            "description": "Every header field, by its MRC2014 name, in the standard's order; the extra space as base64, the labels as an array of ten texts",
            "additionalProperties": {"anyOf": [
                {"$ref": "#/$defs/headerValue"},
                {"type": "object", "properties": {"bits": {"type": "string", "pattern": "^[0-9a-f]{8}$"}},
                 "required": ["bits"], "additionalProperties": False,
                 "description": "A float's IEEE 754 bits, most significant first: a negative zero, or a NaN "
                                "other than the canonical one"},
                {"type": "array", "items": {"anyOf": [{"type": "number"}, {"$ref": "#/$defs/text"},
                                                      {"type": "object", "properties": {"bits": {"type": "string", "pattern": "^[0-9a-f]{8}$"}},
                                                       "required": ["bits"], "additionalProperties": False,
                                                       "description": "A float's IEEE 754 bits, most significant first: a negative zero, or a NaN "
                                                                      "other than the canonical one"}]}},
            ]},
        },
        "axis_map": {
            "type": "object",
            "description": "Whether mapc, mapr and maps named the axes",
            "properties": {"applied": {"type": "boolean"}},
            "required": ["applied"],
            "additionalProperties": False,
        },
        "voxel_size": {
            "type": "object",
            "description": "The voxel size in angstroms, when the cell and grid give one along every axis",
            "properties": {"x": {"type": "number"}, "y": {"type": "number"}, "z": {"type": "number"}},
            "required": ["x", "y", "z"],
            "additionalProperties": False,
        },
    },
    "required": ["byte_order", "kind", "data_type", "header", "axis_map"],
    "additionalProperties": False,
},
    {"$ref": "#/$defs/mirror"},
    {"$ref": "#/$defs/mirrorView"},
]}
