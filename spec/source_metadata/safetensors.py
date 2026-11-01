"""The safetensors convention's source metadata (spec/zarrify/safetensors.md), for
spec/generate_schemas.py."""

_ST = "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md"
_SAFE = {"type": "integer", "minimum": 0, "maximum": 9007199254740991}
_DTYPES = ["BOOL", "U8", "I8", "U16", "I16", "U32", "I32", "U64", "I64", "F16", "F32", "F64", "C64", "BF16",
           "F8_E5M2", "F8_E4M3", "F8_E4M3FNUZ", "F8_E5M2FNUZ", "F8_E8M0", "F4", "F6_E2M3", "F6_E3M2"]

TENSOR = {
    "type": "object",
    "description": f"A tensor of the header ({_ST}#type-safetensorstensor)",
    "properties": {
        "name": {"type": "string", "description": "Its name"},
        "dtype": {"enum": _DTYPES, "description": "Its entry's dtype"},
        "shape": {"type": "array", "items": _SAFE},
        "data_offsets": {"type": "array", "items": _SAFE, "minItems": 2, "maxItems": 2},
        "extra": {"type": "object", "description": "Its entry's other members, as written"},
        "path": {"type": "string", "minLength": 1, "description": "Its array's path, when presented"},
        "reason": {"enum": ["name", "reserved", "path"], "description": "Why it is not presented"},
    },
    "required": ["name", "dtype", "shape", "data_offsets"],
    "oneOf": [{"required": ["path"]}, {"required": ["reason"]}],
    "additionalProperties": False,
}

SCHEMA = {
    "anyOf": [
        {
            "type": "object",
            "description": f"The root's: the header's length, metadata and tensors ({_ST}#51-the-root)",
            "properties": {
                "header_length": {"type": "integer", "minimum": 0, "maximum": 100000000},
                "metadata": {"type": "object", "additionalProperties": {"type": "string"},
                             "description": "The header's __metadata__, in its order"},
                "tensors": {"type": "array", "items": {"$ref": "#/$defs/safetensorsTensor"}},
            },
            "required": ["header_length", "tensors"],
            "additionalProperties": False,
        },
        {"$ref": "#/$defs/mirror"},
        {"$ref": "#/$defs/mirrorView"},
    ]
}

DEFS = {"safetensorsTensor": TENSOR}
