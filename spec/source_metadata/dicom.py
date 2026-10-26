"""The DICOM convention's source metadata (spec/zarrify/dicom.md), for
spec/generate_schemas.py."""

# A dataset in the DICOM JSON Model (PS3.18 §F.2), as spec/zarrify/dicom.md §5
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#5-source-metadata> writes it.
DEFS = {
    "dataset": {
        "type": "object",
        "properties": {
            "duplicates": {
                "type": "array",
                "description": "The later elements with a tag already in the dataset, in file order",
                "items": {
                    "type": "object",
                    "patternProperties": {"^[0-9A-F]{8}$": {"$ref": "#/$defs/attribute"}},
                    "additionalProperties": False,
                    "minProperties": 1,
                    "maxProperties": 1,
                },
            },
        },
        "patternProperties": {"^[0-9A-F]{8}$": {"$ref": "#/$defs/attribute"}},
        "additionalProperties": False,
    },
    "attribute": {
        "type": "object",
        "properties": {
            "vr": {"type": "string", "pattern": "^[A-Z]{2}$"},
            "Value": {"type": "array"},
            "InlineBinary": {"type": "string", "contentEncoding": "base64"},
            "BulkDataURI": {"type": "string",
                            "description": "refkv_source/<path>: the IR's element at <path>, whose value it is"},
            "Structures": {"type": "array", "items": {"$ref": "#/$defs/dataset"},
                           "description": "A gathered sequence's distinct item structures (its items are the IR's "
                                          "elements <its path>/<index>)"},
            "Gathered": {"const": True,
                         "description": "A value in an item of a gathered sequence: the IR's element <the "
                                        "sequence's path>/<item index>/<path within the item>"},
            "LittleEndian": {"const": True,
                             "description": "An explicit UN value in explicit VR big endian, whose bytes are in "
                                            "little endian"},
        },
        "required": ["vr"],
        "maxProperties": 3,
        "additionalProperties": False,
    },
}

SCHEMA = {"anyOf": [{
    "type": "object",
    "description": "The root's (spec/zarrify/dicom.md §5.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#51-the-root>);"
                   " a path refkv_source/<p> names the IR's element at <p>",
    "properties": {
        "preamble": {"type": "string", "contentEncoding": "base64",
                     "description": "The 128-byte preamble, unless all zero"},
        "meta": {"$ref": "#/$defs/dataset",
                 "description": "The File Meta Information, without its large attributes (in the mirror only)"},
        "dataset": {"$ref": "#/$defs/dataset",
                    "description": "The dataset, without the per-frame groups and the large attributes (in the mirror "
                                   "only)"},
        "pixel_extra": {"type": "string",
                        "description": "refkv_source/pixel_extra: the IR's element holding the native pixel data after "
                                       "the frames"},
        "pixel_unreferenced": {"type": "string",
                               "description": "refkv_source/pixel_unreferenced: the IR's struct holding the pixel data "
                                              "the Extended Offset Table skips"},
        "pixel_unreferenced_headers": {"const": True,
                                       "description": "Whether pixel_unreferenced also holds the frames' item "
                                                      "headers"},
        "pixel_fragments": {"type": "string",
                            "description": "refkv_source/pixel_fragments, when a frame has more than one fragment "
                                           "(the fragments are the IR's dataset/7FE00010/fragments/<j>)"},
        "pixel_offset_table": {"const": True, "description": "Whether the Basic Offset Table is not empty"},
        "trailing": {"type": "string",
                     "description": "refkv_source/trailing: the IR's element holding the bytes after the last element"},
    },
    "required": ["meta", "dataset"],
    "additionalProperties": False,
},
    {"$ref": "#/$defs/mirror"},
    {"$ref": "#/$defs/mirrorView"},
]}
