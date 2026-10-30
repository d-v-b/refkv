"""The NumPy convention's source metadata (spec/zarrify/npy.md), for spec/generate_schemas.py."""

_NPY = "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md"
_REASONS = ["not_npy", "name", "reserved", "method", "header", "datatype", "size"]

# a header's descr: a type string, or a list of fields, each [name, descr] or [name, descr, shape]
_DESCR = {
    "anyOf": [
        {"type": "string", "description": "A type string, as the header writes it"},
        {
            "type": "array",
            "description": "A structured data type's fields, in order",
            "items": {
                "type": "array",
                "minItems": 2,
                "maxItems": 3,
                "prefixItems": [
                    {"type": "string"},
                    {"$ref": "#/$defs/npyDescr"},
                    {"type": "array", "items": {"type": "integer", "minimum": 0}},
                ],
                "items": False,
            },
        },
    ]
}

HEADER = {
    "type": "object",
    "description": f"An NPY preamble and header ({_NPY}#type-npyheader)",
    "properties": {
        "version": {
            "type": "array",
            "items": {"enum": [1, 2, 3, 0]},
            "minItems": 2,
            "maxItems": 2,
            "description": "[major, minor]",
        },
        "descr": {"$ref": "#/$defs/npyDescr"},
        "fortran_order": {"type": "boolean"},
        "shape": {"type": "array", "items": {"type": "integer", "minimum": 0}, "maxItems": 64},
        "offset": {"type": "integer", "minimum": 10, "description": "The data's offset in the file"},
    },
    "required": ["version", "descr", "fortran_order", "shape", "offset"],
    "additionalProperties": False,
}

MEMBER = {
    "type": "object",
    "description": f"A member of a .npz archive ({_NPY}#type-npymember)",
    "properties": {
        "name": {"$ref": "#/$defs/text"},
        "method": {"type": "integer", "minimum": 0, "description": "Its compression method"},
        "size": {"type": "integer", "minimum": 0},
        "compressed_size": {"type": "integer", "minimum": 0},
        "path": {"type": "string", "minLength": 1, "description": "Its array's path, when presented"},
        "reason": {"enum": _REASONS, "description": "Why it is not presented"},
        "header": {"$ref": "#/$defs/npyHeader"},
    },
    "required": ["name", "method", "size", "compressed_size"],
    "oneOf": [{"required": ["path"]}, {"required": ["reason"]}],
    "additionalProperties": False,
}

SCHEMA = {
    "anyOf": [
        {
            "type": "object",
            "description": f"The root's: a .npy file's array, or a .npz archive's members ({_NPY}#51-the-root)",
            "properties": {
                "kind": {"enum": ["npy", "npz"]},
                "array": {"$ref": "#/$defs/npyHeader"},
                "members": {"type": "array", "items": {"$ref": "#/$defs/npyMember"}},
            },
            "required": ["kind"],
            "oneOf": [{"required": ["array"]}, {"required": ["members"]}],
            "additionalProperties": False,
        },
        {"$ref": "#/$defs/mirror"},
        {"$ref": "#/$defs/mirrorView"},
    ]
}

DEFS = {"npyDescr": _DESCR, "npyHeader": HEADER, "npyMember": MEMBER}
