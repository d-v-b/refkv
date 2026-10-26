"""The OME-Zarr convention's source metadata (spec/zarrify/ome-zarr.md), for
spec/generate_schemas.py."""

SCHEMA = {"anyOf": [
    {
        "type": "object",
        "description": "A node's (spec/zarrify/ome-zarr.md §8 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ome-zarr.md#8-source-metadata>)",
        "properties": {
            "attributes": {"type": "object", "minProperties": 1,
                           "description": "The node's .zattrs, without the OME members the ome object gives back"},
            "metadata": {"type": "object", "minProperties": 1,
                         "description": "The members of .zarray or .zgroup the Zarr metadata does not reproduce"},
            "unversioned": {"type": "array", "minItems": 1, "uniqueItems": True,
                            "items": {"enum": ["multiscales", "omero", "image-label", "plate", "well"]},
                            "description": "The OME members that had no version, which ome gives back as they are"},
        },
        "minProperties": 1,
        "additionalProperties": False,
    },
    # refkv_source's: the Zarr v2 store's IR, as zarr2's (spec/zarrify/ome-zarr.md §8.2
    # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ome-zarr.md#82-the-store-ir>)
    {"allOf": [{"$ref": "#/$defs/storeMirror"},
               {"properties": {"ir": {"required": ["objects"]}, "empty_dirs": False}}]},
    {"$ref": "#/$defs/mirrorView"},
]}
