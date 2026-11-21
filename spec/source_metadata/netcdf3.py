"""The netCDF3 convention's source metadata (spec/zarrify/netcdf3.md), for spec/generate_schemas.py.

A node of the netCDF3 convention holds the source's own attributes beside the convention's
(spec/zarrify/netcdf3.md §5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#5-attributes>),
which every schema accepts."""

_NETCDF3 = "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md"
_TYPE_NAMES = ["NC_BYTE", "NC_CHAR", "NC_SHORT", "NC_INT", "NC_FLOAT", "NC_DOUBLE", "NC_UBYTE", "NC_USHORT",
               "NC_UINT", "NC_INT64", "NC_UINT64"]
_COUNT = {"type": "integer", "minimum": 0}
_TYPE = {"type": "object",
         "properties": {"type": {"enum": _TYPE_NAMES}, "count": _COUNT},
         "required": ["type", "count"], "additionalProperties": False,
         "description": f"An attribute's type and value count ({_NETCDF3}#61-the-root)"}
_TYPES = {"type": "object", "additionalProperties": {"$ref": "#/$defs/netcdf3AttributeType"},
          "description": "Each attribute's type, by name"}
_NOT_ATTRIBUTES = {"type": "object", "additionalProperties": {"enum": ["reserved", "fill_value"]},
                   "description": "The attributes not presented, by name, each with the reason"}

SCHEMA = {"anyOf": [
    {
        "type": "object",
        "description": f"The root's ({_NETCDF3}#61-the-root)",
        "properties": {
            "format": {"enum": ["classic", "64-bit offset", "64-bit data"]},
            "version": {"enum": [1, 2, 5]},
            "numrecs": {"anyOf": [_COUNT, {"const": "streaming"}],
                        "description": "The header's record count, or `streaming` for the all-ones value"},
            "records": _COUNT,
            "recsize": _COUNT,
            "header": _COUNT,
            "dimensions": {"type": "array", "items": {
                "type": "object",
                "properties": {"name": {"$ref": "#/$defs/text"}, "length": _COUNT, "unlimited": {"const": True}},
                "required": ["name", "length"], "additionalProperties": False}},
            "attributes": _TYPES,
            "unpresented_attributes": _NOT_ATTRIBUTES,
            "unpresented": {"type": "array",
                            "description": "The variables not presented, in the header's order "
                                           f"({_NETCDF3}#2-the-hierarchy)",
                            "items": {"type": "object",
                                      "properties": {"name": {"$ref": "#/$defs/text"}, "index": _COUNT,
                                                     "reason": {"enum": ["name", "reserved", "duplicate"]}},
                                      "required": ["name", "index", "reason"], "additionalProperties": False}},
        },
        "required": ["format", "version", "numrecs", "records", "recsize", "header", "dimensions"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "description": f"A variable's ({_NETCDF3}#62-variables)",
        "properties": {
            "index": _COUNT,
            "type": {"enum": _TYPE_NAMES},
            "record": {"type": "boolean"},
            "begin": _COUNT,
            "vsize": _COUNT,
            "attributes": _TYPES,
            "unpresented_attributes": _NOT_ATTRIBUTES,
        },
        "required": ["index", "type", "record", "begin", "vsize"],
        "additionalProperties": False,
    },
    {"$ref": "#/$defs/mirror"},
    {"$ref": "#/$defs/mirrorView"},
]}

DEFS = {"netcdf3AttributeType": _TYPE}
