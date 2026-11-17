"""The HDF4 convention's source metadata (spec/zarrify/hdf4.md), for spec/generate_schemas.py.

A node of the HDF4 convention holds the source's own attributes beside the convention's
(spec/zarrify/hdf4.md §7 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#7-attributes>),
which every schema accepts."""

_HDF4 = "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md"
_REASONS = ["name", "reserved", "duplicate", "depth", "number_type", "dimensions", "size", "compression", "chunks",
            "length", "external", "special", "dfsd", "version", "datatype", "fields", "byte_order", "record_size",
            "object"]
_ATTRIBUTE_REASONS = ["reserved", "budget", "datatype", "attribute"]
_REF = {"type": "integer", "minimum": 0, "maximum": 65535}
_TYPE = {"type": "object",
         "properties": {"type": {"type": "string"}, "count": {"type": "integer", "minimum": 0},
                        "endian": {"const": "little"}},
         "required": ["type", "count"], "additionalProperties": False,
         "description": f"An attribute's field type and value count ({_HDF4}#81-groups)"}
_TYPES = {"type": "object", "additionalProperties": {"$ref": "#/$defs/hdf4AttributeType"},
          "description": "Each attribute's type, by name"}
_NOT_ATTRIBUTES = {"type": "object", "additionalProperties": {"enum": _ATTRIBUTE_REASONS},
                   "description": "The attributes not presented, by name, each with the reason"}
_UNPRESENTED = {"type": "array",
                "description": f"The members not presented, in the walk's order ({_HDF4}#84-what-is-not-presented)",
                "items": {"type": "object",
                          "properties": {"name": {"$ref": "#/$defs/text"}, "tag": _REF, "ref": _REF,
                                         "reason": {"enum": _REASONS}},
                          "required": ["tag", "ref", "reason"], "additionalProperties": False}}
_LINKS = {"type": "array", "description": "The members the walk reached again, each at its canonical path",
          "items": {"type": "object",
                    "properties": {"name": {"$ref": "#/$defs/text"}, "tag": _REF, "ref": _REF,
                                   "path": {"type": "string"}},
                    "required": ["name", "tag", "ref", "path"], "additionalProperties": False}}

SCHEMA = {"anyOf": [
    {
        "type": "object",
        "description": f"A group's: the root's or a Vgroup's ({_HDF4}#81-groups)",
        "properties": {
            "ref": _REF,
            "class": {"$ref": "#/$defs/text"},
            "version": {"type": "object",
                        "properties": {"major": {"type": "integer"}, "minor": {"type": "integer"},
                                       "release": {"type": "integer"}, "text": {"$ref": "#/$defs/text"}},
                        "required": ["major", "minor", "release", "text"], "additionalProperties": False,
                        "description": "The root's: the version of the library that wrote the file"},
            "cdf": {"anyOf": [_REF, {"type": "null"}], "description": "The root's: the CDF0.0 Vgroup's ref"},
            "attributes": _TYPES,
            "unpresented_attributes": _NOT_ATTRIBUTES,
            "links": _LINKS,
            "unpresented": _UNPRESENTED,
        },
        "additionalProperties": False,
    },
    {
        "type": "object",
        "description": f"A dataset's ({_HDF4}#82-datasets)",
        "properties": {
            "ref": _REF,
            "ndg": _REF,
            "number_type": {"type": "object",
                            "properties": {"type": {"type": "string"}, "width": {"type": "integer"},
                                           "class": {"type": "integer"}},
                            "required": ["type", "width", "class"], "additionalProperties": False},
            "layout": {"enum": ["none", "contiguous", "linked", "compressed", "chunked"]},
            "compression": {"type": "string",
                            "description": "The method's name: none, rle, nbit, skipping_huffman, deflate, szip, jpeg, "
                                           "imcomp, or `code <n>` for another"},
            "chunks": {"type": "array", "items": {"type": "integer", "minimum": 1}},
            "chunk_fill_value": {"type": "string", "description": "The chunked header's fill value, in base64"},
            "unlimited": {"const": True},
            "sdd": {"type": "array", "items": {"type": "integer", "minimum": 0},
                    "description": "The SDD's sizes, of a dataset with an unlimited dimension"},
            "coordinate": {"type": "boolean"},
            "text_axis": {"$ref": "#/$defs/text", "description": "A dataset of text's last axis's name"},
            "attributes": _TYPES,
            "unpresented_attributes": _NOT_ATTRIBUTES,
        },
        "required": ["ref", "number_type", "layout"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "description": f"A table's, on its array or its group ({_HDF4}#82-datasets)",
        "properties": {
            "ref": _REF,
            "class": {"$ref": "#/$defs/text"},
            "interlace": {"type": "integer", "minimum": 0},
            "records": {"type": "integer", "minimum": 0},
            "fields": {"type": "array", "items": {
                "type": "object",
                "properties": {"name": {"$ref": "#/$defs/text"}, "type": {"type": "string"},
                               "order": {"type": "integer", "minimum": 0}, "offset": {"type": "integer", "minimum": 0},
                               "size": {"type": "integer", "minimum": 0}, "endian": {"const": "little"}},
                "required": ["name", "type", "order", "offset", "size"], "additionalProperties": False}},
            "field_attributes": {"type": "object", "additionalProperties": {"type": "object"},
                                 "description": "Each field's attributes as JSON, by the field's name"},
            "attributes": _TYPES,
            "unpresented_attributes": _NOT_ATTRIBUTES,
        },
        "required": ["ref", "class", "interlace", "records", "fields"],
        "additionalProperties": False,
    },
    {"$ref": "#/$defs/mirror"},
    {"$ref": "#/$defs/mirrorView"},
]}

DEFS = {"hdf4AttributeType": _TYPE}
