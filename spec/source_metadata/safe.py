"""The Sentinel-2 SAFE convention's source metadata (spec/zarrify/safe.md), for
spec/generate_schemas.py."""

_MEASURE = {"anyOf": [{"type": ["number", "string"]}, {
    "type": "object", "properties": {"value": {"type": ["number", "string"]}, "unit": {"type": "string"}},
    "required": ["value", "unit"], "additionalProperties": False}]}

_VALUE = {"type": ["number", "string"]}

_RECORDS = {"type": "array", "items": {"type": "object", "additionalProperties": _VALUE}, "minItems": 1}

SCHEMA = {"oneOf": [
    {
        "type": "object",
        "description": "The root's: the product's identity, from the product and tile metadata (§6.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#63-the-source-metadata-node>)",
        "properties": {
            **{k: {"type": "string"} for k in ("PRODUCT_URI", "PROCESSING_LEVEL", "PRODUCT_TYPE", "PROCESSING_BASELINE",
                                               "TILE_ID", "SENSING_TIME", "HORIZONTAL_CS_NAME", "HORIZONTAL_CS_CODE")},
            "Special_Values": _RECORDS,
            "U": _VALUE,
        },
        "required": ["HORIZONTAL_CS_CODE"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "description": "A band array's: a derived convenience copied from the product metadata (§6.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#62-band-arrays>), and the band "
                       "file's SIZ segment",
        "properties": {
            "IMAGE_FILE": {"type": "string"},
            "bandId": {"type": "integer", "minimum": 0},
            "physicalBand": {"type": "string"},
            "RESOLUTION": _VALUE,
            "Wavelength": {"type": "object", "properties": {k: _MEASURE for k in ("MIN", "MAX", "CENTRAL")},
                           "additionalProperties": False, "minProperties": 1},
            **{k: _VALUE for k in ("PHYSICAL_GAINS", "RADIO_ADD_OFFSET", "BOA_ADD_OFFSET")},
            **{k: _MEASURE for k in ("SOLAR_IRRADIANCE", "QUANTIFICATION_VALUE", "BOA_QUANTIFICATION_VALUE",
                                     "AOT_QUANTIFICATION_VALUE", "WVP_QUANTIFICATION_VALUE")},
            "Scene_Classification_List": _RECORDS,
            "siz": {"type": "string", "contentEncoding": "base64",
                    "description": "The band file's SIZ marker segment, which its chunks replace"},
        },
        "required": ["IMAGE_FILE", "siz"],
        "additionalProperties": False,
    },
    # refkv_source's: the IR mirror's description, the empty directories and the keys of the
    # ignored objects (§6.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#63-the-source-metadata-node>)
    {"$ref": "#/$defs/storeMirror"},
    # a view document, without the members the branches above require (or `refkv_source`'s of
    # revision 25), which name no SAFE IR element (`siz` and `xml` do: they are not excluded), so
    # that a document of those branches with a wrong member is invalid (one without any of them,
    # such as band metadata without IMAGE_FILE, reads as a view document: member names alone
    # cannot tell those apart)
    {"allOf": [{"$ref": "#/$defs/mirrorView"}, {"not": {"anyOf": [
        {"required": [m]} for m in ("ir", "IMAGE_FILE", "HORIZONTAL_CS_CODE", "empty_dirs", "ignored",
                                     "xml_arrays", "empty")]}}]},
]}

# The GeoZarr members a SAFE node may hold besides the convention's (spec/zarrify/safe.md §5
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#5-georeferencing-geozarr>); their
# own schemas (zarr-conventions proj, spatial and multiscales v0.1) validate them.
ATTRIBUTES = {
    "proj:code": {"type": "string"},
    "proj:wkt2": {"type": "string"},
    **{f"spatial:{k}": {"type": "array"} for k in ("dimensions", "transform", "shape", "bbox")},
    "spatial:registration": {"type": "string"},
    "multiscales": {"type": "object"},
}

# The root's source URL.
URL = {"description": "The URL of the source: a .SAFE directory's store URL, or a zip file's"}
