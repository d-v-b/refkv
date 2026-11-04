"""The NIfTI convention's source metadata (spec/zarrify/nifti.md), for
spec/generate_schemas.py."""

SCHEMA = {"anyOf": [{
    "type": "object",
    "description": "The root's: the version, the byte order, the header, and what the layout derives from it "
                   "(spec/zarrify/nifti.md §5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#5-source-metadata>)",
    "properties": {
        "nifti_version": {"enum": [1, 2]},
        "byte_order": {"enum": ["little", "big"]},
        "header": {
            "type": "object",
            "description": "Every header field, by its NIfTI-1 or NIfTI-2 name, in the standard's order",
            "additionalProperties": {"anyOf": [
                {"$ref": "#/$defs/headerValue"},
                {"type": "object",
                 "properties": {"bits": {"type": "string", "pattern": "^([0-9a-f]{8}|[0-9a-f]{16})$"}},
                 "required": ["bits"], "additionalProperties": False,
                 "description": "A float's IEEE 754 bits, most significant first: a negative zero, or a NaN "
                                "other than the canonical one"},
                {"type": "array", "items": {"anyOf": [
                    {"type": "number"}, {"type": "string"},
                    {"type": "object",
                     "properties": {"bits": {"type": "string", "pattern": "^([0-9a-f]{8}|[0-9a-f]{16})$"}},
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
        "data_file": {
            "type": "string",
            "description": "A header-and-image pair's image file, its key "
                           "(spec/zarrify/nifti.md §7.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#75-header-and-image-pairs>)",
        },
        "gzip": {
            "type": "object",
            "description": "A file gzipped whole: where the voxels start in its stream, and what the stream's start "
                           "holds before them "
                           "(spec/zarrify/nifti.md §7.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#74-gzipped-files>)",
            "properties": {
                "skip": {"type": "integer", "minimum": 0},
                "extender": {"type": "array", "items": {"type": "integer"}, "minItems": 4, "maxItems": 4},
                "extensions": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"esize": {"type": "integer"}, "ecode": {"type": "integer"},
                                   "data": {"$ref": "#/$defs/text"}},
                    "required": ["esize", "ecode"],
                    "additionalProperties": False,
                }},
            },
            "required": ["skip", "extender", "extensions"],
            "additionalProperties": False,
        },
    },
    "required": ["nifti_version", "byte_order", "header"],
    "additionalProperties": False,
},
    {"$ref": "#/$defs/mirror"},
    {"$ref": "#/$defs/mirrorView"},
]}
