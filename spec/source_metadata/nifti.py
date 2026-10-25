"""The NIfTI convention's source metadata (spec/zarrify/nifti.md), for
spec/generate_schemas.py."""

SCHEMA = {"anyOf": [{
    "type": "object",
    "description": "The root's: the version, the byte order, the header, and what the layout derives from it (spec/zarrify/nifti.md §5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#5-source-metadata>)",
    "properties": {
        "nifti_version": {"enum": [1, 2]},
        "byte_order": {"enum": ["little", "big"]},
        "header": {
            "type": "object",
            "description": "Every header field, by its NIfTI-1 or NIfTI-2 name, in the standard's order",
            "additionalProperties": {"anyOf": [
                {"$ref": "#/$defs/headerValue"},
                {"type": "object", "properties": {"bits": {"type": "string", "pattern": "^([0-9a-f]{8}|[0-9a-f]{16})$"}},
                "required": ["bits"], "additionalProperties": False,
                "description": "A float's IEEE 754 bits, most significant first: a negative zero, or a NaN "
                               "other than the canonical one"},
                {"type": "array", "items": {"anyOf": [{"type": "number"}, {"type": "string"},
                                                      {"type": "object", "properties": {"bits": {"type": "string", "pattern": "^([0-9a-f]{8}|[0-9a-f]{16})$"}},
                                                      "required": ["bits"], "additionalProperties": False,
                                                      "description": "A float's IEEE 754 bits, most significant first: a negative zero, or a NaN "
                                                                     "other than the canonical one"}]}},
            ]},
        },
        "affine": {
            "type": "object",
            "properties": {"form": {"enum": ["sform", "qform"]}, "applied": {"type": "boolean"}},
            "required": ["form", "applied"],
            "additionalProperties": False,
        },
        "scaling": {
            "type": "object",
            "properties": {"slope": {"type": "number"}, "inter": {"type": "number"}},
            "required": ["slope", "inter"],
            "additionalProperties": False,
        },
    },
    "required": ["nifti_version", "byte_order", "header"],
    "additionalProperties": False,
},
    {"$ref": "#/$defs/mirror"},
    {"$ref": "#/$defs/mirrorView"},
]}
