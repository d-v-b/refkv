"""The NDPI convention's source metadata (spec/zarrify/ndpi.md), for
spec/generate_schemas.py."""

SCHEMA = {"anyOf": [
    {
        "type": "object",
        "description": "The root's: level 0's acquisition tags "
                       "(spec/zarrify/ndpi.md §5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#5-source-metadata>)",
        "properties": {
            "Fluorescence": {"$ref": "#/$defs/text", "description": "Tag 65434, the fluorescence filter set's name"},
            "Wavelength": {"anyOf": [{"type": "number"}, {"enum": ["NaN", "Infinity", "-Infinity"]},
                                     {"type": "object", "properties": {"bits": {"type": "string",
                                                                                "pattern": "^([0-9a-f]{8}|[0-9a-f]{16})$"}},
                                      "required": ["bits"], "additionalProperties": False}],
                           "description": "Tag 65451, the emission wavelength, in nanometers"},
            "ExposureTime": {"anyOf": [{"type": "integer", "minimum": 0},
                                       {"type": "string", "pattern": "^[1-9][0-9]{15,19}$"}],
                             "description": "Tag 65454, the exposure time, in microseconds"},
        },
        "minProperties": 1,
        "additionalProperties": False,
    },
    {"$ref": "#/$defs/mirror"},
    {"$ref": "#/$defs/mirrorView"},
]}
