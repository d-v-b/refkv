"""The NDPI convention's source metadata (spec/zarrify/ndpi.md), for
spec/generate_schemas.py."""

# A float whose bits a JSON number or "NaN" does not keep: its IEEE 754 bits, most significant first
_BITS = {"type": "object", "properties": {"bits": {"type": "string", "pattern": "^([0-9a-f]{8}|[0-9a-f]{16})$"}},
         "required": ["bits"], "additionalProperties": False}

# An image left out of the levels, and why
_LEFT_OUT = {
    "type": "object",
    "properties": {
        "ifd": {"type": "integer", "minimum": 0, "description": "Its main-chain index"},
        "magnification": {"anyOf": [{"type": "number"}, {"enum": ["Infinity"]}]},
        "width": {"type": "integer", "minimum": 0},
        "height": {"type": "integer", "minimum": 0},
        "reason": {"enum": ["frame", "size", "chunks", "no_offset", "duplicate", "offsets"]},
        "offset": {"type": "integer", "description": "Tag 65424, in nanometers"},
    },
    "required": ["ifd", "magnification", "width", "height", "reason"],
    "additionalProperties": False,
}

SCHEMA = {"anyOf": [
    {
        "type": "object",
        "description": "The root's: the first image's acquisition tags and the focal planes "
                       "(spec/zarrify/ndpi.md §5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#5-source-metadata>)",
        "properties": {
            "Fluorescence": {"$ref": "#/$defs/text", "description": "Tag 65434, the fluorescence filter set's name"},
            "Wavelength": {"anyOf": [{"type": "number"}, {"enum": ["NaN", "Infinity", "-Infinity"]}, _BITS],
                           "description": "Tag 65451, the emission wavelength, in nanometers"},
            "ExposureTime": {"type": "integer", "minimum": 0,
                             "description": "Tag 65454, the exposure time, in microseconds"},
            "FocalOffsets": {"type": "array", "items": {"type": "integer"}, "minItems": 1,
                             "description": "Tag 65424 of the planes of each level kept, ascending, in "
                                            "nanometers: one for each index along z"},
            "LeftOut": {"type": "array", "items": _LEFT_OUT, "minItems": 1,
                        "description": "The images of positive Magnification in no level, in chain order "
                                       "(spec/zarrify/ndpi.md §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#3-levels>)"},
        },
        "minProperties": 1,
        "additionalProperties": False,
    },
    {"$ref": "#/$defs/mirror"},
    {"$ref": "#/$defs/mirrorView"},
]}
