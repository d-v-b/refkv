"""The HDF5 convention's source metadata (spec/zarrify/hdf5.md), for spec/generate_schemas.py.

A node of the HDF5 convention holds the source's own attributes beside the convention's
(spec/zarrify/hdf5.md §2.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#21-declaration-and-attributes>):
`OPEN_ATTRIBUTES` lets the schema accept them, and `ATTRIBUTES` describes the members hdmf-zarr's
convention reserves, which refkv writes."""

_HDF5 = "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md"
_REASONS = ["name", "reserved", "named_datatype", "object", "datatype", "filter", "filter_mask", "edge_chunks",
            "layout", "external_storage", "null_dataspace", "fill_value", "size", "shared", "budget"]
_DATATYPE = {"type": "object", "properties": {"class": {"type": "string"}, "size": {"type": "integer", "minimum": 0}},
             "required": ["class", "size"],
             "description": f"An HDF5 datatype, described ({_HDF5}#7-source-metadata)"}
_TYPES = {"type": "object", "additionalProperties": {"$ref": "#/$defs/hdf5Datatype"},
          "description": "Each attribute's HDF5 datatype, by name"}
_NOT = {"type": "object", "additionalProperties": {"enum": _REASONS},
        "description": "What the hierarchy does not present, by name, each with the reason"}

SCHEMA = {"anyOf": [
    {
        "type": "object",
        "description": f"A group's ({_HDF5}#71-groups)",
        "properties": {
            "address": {"type": "integer", "minimum": 0, "description": "Its object header's address"},
            "superblock": {"enum": [0, 1, 2, 3], "description": "The root's: the superblock's version"},
            "attributes": _TYPES,
            "unpresented_attributes": _NOT,
            "unpresented": _NOT,
            "external_links": {"type": "array", "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "type": {"type": "integer"},
                               "file": {"anyOf": [{"$ref": "#/$defs/text"}, {"type": "null"}]},
                               "path": {"anyOf": [{"$ref": "#/$defs/text"}, {"type": "null"}]}},
                "required": ["name", "type", "file", "path"], "additionalProperties": False}},
        },
        "required": ["address"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "description": f"A dataset's ({_HDF5}#72-datasets)",
        "properties": {
            "address": {"type": "integer", "minimum": 0},
            "datatype": {"$ref": "#/$defs/hdf5Datatype"},
            "maxshape": {"type": "array", "items": {"type": ["integer", "null"], "minimum": 0},
                         "description": "The dataspace's maximum sizes, null for unlimited"},
            "layout": {"enum": ["compact", "contiguous", "chunked"]},
            "chunks": {"type": "array", "items": {"type": "integer", "minimum": 1}},
            "index": {"enum": ["btree1", "single", "implicit", "fixed_array", "extensible_array", "btree2", "none"]},
            "filters": {"type": "array", "items": {"type": "integer", "minimum": 0}},
            "fill_value": {"type": "string", "description": "The fill value message's bytes, in base64"},
            "attributes": _TYPES,
            "unpresented_attributes": _NOT,
        },
        "required": ["address", "datatype", "layout"],
        "additionalProperties": False,
    },
    {"$ref": "#/$defs/mirror"},
    {"$ref": "#/$defs/mirrorView"},
]}

DEFS = {"hdf5Datatype": _DATATYPE}

_REF = {"type": "object", "properties": {"_REFERENCE": {
    "type": "object", "properties": {"source": {"const": "."}, "path": {"type": "string"}},
    "required": ["source", "path"], "additionalProperties": False}}, "required": ["_REFERENCE"]}

ATTRIBUTES = {
    "_LINKS": {"type": "array", "description": "hdmf-zarr's links: a group's soft links and second hard links",
               "items": {"type": "object",
                         "properties": {"name": {"type": "string"}, "source": {"const": "."},
                                        "path": {"type": "string"}},
                         "required": ["name", "source", "path"], "additionalProperties": False}},
    "_DTYPE": {"const": "object_reference", "description": "hdmf-zarr's mark of an array of object references"},
    "_REFERENCE_FIELDS": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                          "description": "hdmf-zarr's list of a struct's reference members"},
    ".specloc": {"anyOf": [_REF, {"type": "string"}], "description": "An NWB file's cached specifications"},
}

# the source's attributes, any names (but the reserved ones, which ATTRIBUTES gives)
OPEN_ATTRIBUTES = True
