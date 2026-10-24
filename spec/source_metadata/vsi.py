"""The VSI convention's source metadata (spec/virtualize/vsi.md), for
spec/generate_schemas.py."""

# A VSI slide (spec/virtualize/vsi.md §6 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#6-source-metadata>):
# the root's file, stacks and metadata tree; each image's stack properties and its tile file's
# headers or its IFD; the macro image's IFD; and the mirror's.
_VSI_PAIR = {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}

_VSI_NULLABLE_PAIR = {"anyOf": [_VSI_PAIR, {"type": "null"}]}

_VSI_FIELD = {
    "type": "object",
    "description": "A field of the metadata tree (spec/virtualize/vsi.md §6.1 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#61-the-root>)",
    "properties": {
        "tag": {"type": "integer", "minimum": 0},
        "tag2": {"type": "integer", "minimum": 0},
        "type": {"type": "integer", "minimum": 0},
        "name": {"type": "string"},
        "inline": {"const": True},
        "value": {},
        "fields": {"type": "array", "items": {"$ref": "#/$defs/vsiField"}},
        "ifd": {"type": "object", "properties": {"index": {"type": "integer", "minimum": 0}, "bytes": {"type": "integer", "minimum": 0}},
                "required": ["bytes"], "additionalProperties": False},
    },
    "required": ["tag", "type"],
    "additionalProperties": False,
}

_VSI_IFD = {
    "type": "object",
    "description": "An IFD image's IFD (spec/virtualize/vsi.md §4.3 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#43-the-macro-image>)",
    "properties": {k: {"type": "integer", "minimum": 0} for k in ("index", "width", "height", "samples", "bits", "compression", "photometric")},
    "required": ["index", "width", "height", "samples", "bits", "compression", "photometric"],
    "additionalProperties": False,
}

SCHEMA = {"anyOf": [
    {
        "type": "object",
        "description": "The root's: the .vsi file, the stacks and the metadata tree (spec/virtualize/vsi.md §6.1 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#61-the-root>)",
        "properties": {
            "file": {"type": "object",
                     "properties": {"key": {"type": "string"}, "size": {"type": "integer", "minimum": 32}, "ifds": {"type": "integer", "minimum": 0}},
                     "required": ["key", "size", "ifds"], "additionalProperties": False},
            "stacks": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "minimum": 0},
                    "name": {"type": "string"},
                    "type": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                    "external": {"type": "boolean"},
                    "width": {"anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}]},
                    "height": {"anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}]},
                    "tile_file": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["id", "name", "type", "external", "width", "height", "tile_file"],
                "additionalProperties": False,
            }},
            "tree": {"type": "object", "properties": {"fields": {"type": "array", "items": {"$ref": "#/$defs/vsiField"}}},
                     "required": ["fields"], "additionalProperties": False},
        },
        "required": ["file", "stacks", "tree"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "description": "A stack's image's: its properties from the tree, then its tile file's headers and levels, or its IFD (spec/virtualize/vsi.md §6.1 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#61-the-root>)",
        "properties": {
            "id": {"type": "integer", "minimum": 0},
            "type": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
            "type_name": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "width": {"anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}]},
            "height": {"anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}]},
            "dimension_sizes": {"type": "array", "items": {"type": "integer"}},
            "dimensions": {"type": "array", "items": {
                "type": "object", "properties": {"coordinate": {"type": "integer", "minimum": 2}, "meaning": {"type": "string"}},
                "required": ["coordinate", "meaning"], "additionalProperties": False}},
            "scale": _VSI_NULLABLE_PAIR,
            "unit": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "origin": _VSI_NULLABLE_PAIR,
            "z_step": {"anyOf": [{"type": "number"}, {"type": "null"}]},
            "z_unit": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "display": _VSI_NULLABLE_PAIR,
            "channels": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "name": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "channel_name": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "color": {"anyOf": [{"type": "string", "pattern": "^[0-9A-F]{6}$"}, {"type": "null"}]},
                    "display": _VSI_NULLABLE_PAIR,
                    "wavelength": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                },
                "required": ["name", "channel_name", "color", "display", "wavelength"],
                "additionalProperties": False,
            }},
            "creation_time": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
            "magnification": {"anyOf": [{"type": "number"}, {"type": "null"}]},
            "default_ifd": {"anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}]},
            "coarse_ifd": {"anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}]},
            "name": {"type": "string"},
            "tile_file": {"type": "string"},
            "ets": {
                "type": "object",
                "description": "The tile file's SIS and ETS headers (spec/virtualize/vsi.md §3 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#3-the-tile-files>)",
                "properties": {
                    "key": {"type": "string"},
                    "size": {"type": "integer", "minimum": 64},
                    "version": {"type": "integer"},
                    "coordinates": {"type": "integer", "minimum": 2, "maximum": 16},
                    "tiles": {"type": "integer", "minimum": 0},
                    "ets_version": {"type": "integer"},
                    "pixel_type": {"type": "integer", "minimum": 1, "maximum": 10},
                    "data_type": {"enum": ["int8", "uint8", "int16", "uint16", "int32", "uint32", "int64", "uint64", "float32", "float64"]},
                    "samples": {"enum": [1, 3, 4]},
                    "colorspace": {"type": "integer"},
                    "compression": {"enum": [0, 2, 3]},
                    "codec": {"enum": ["raw", "jpeg", "jpeg2000"]},
                    "quality": {"type": "integer"},
                    "tile_width": {"type": "integer", "minimum": 1, "maximum": 65535},
                    "tile_height": {"type": "integer", "minimum": 1, "maximum": 65535},
                    "tile_depth": {"const": 1},
                    "background": {"type": "string", "contentEncoding": "base64"},
                    "component_order": {"type": "integer"},
                    "use_pyramid": {"type": "boolean"},
                    "dimensions": {"type": "integer", "minimum": 2, "maximum": 10},
                    "dimension_sizes": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 10},
                },
                "required": ["key", "size", "version", "coordinates", "tiles", "ets_version", "pixel_type", "data_type", "samples",
                             "colorspace", "compression", "codec", "quality", "tile_width", "tile_height", "tile_depth", "background",
                             "component_order", "use_pyramid", "dimensions", "dimension_sizes"],
                "additionalProperties": False,
            },
            "levels": {"type": "array", "items": _VSI_PAIR, "minItems": 1},
            "tiles_outside": {"type": "integer", "minimum": 0},
            "ifd": _VSI_IFD,
        },
        "required": ["id", "type", "type_name", "width", "height", "dimension_sizes", "dimensions", "scale", "unit", "origin",
                     "z_step", "z_unit", "display", "channels", "creation_time", "magnification", "default_ifd", "coarse_ifd", "name"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "description": "The macro image's: its name and IFD (spec/virtualize/vsi.md §4.3 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#43-the-macro-image>)",
        "properties": {"name": {"const": "macro image"}, "ifd": _VSI_IFD},
        "required": ["name", "ifd"],
        "additionalProperties": False,
    },
    {"$ref": "#/$defs/mirror"},
    {"$ref": "#/$defs/mirrorView"},
]}

DEFS = {"vsiField": _VSI_FIELD}
