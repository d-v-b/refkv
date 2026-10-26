"""The FLAC convention's source metadata (spec/zarrify/flac.md), for
spec/generate_schemas.py."""

from source_metadata.mp3 import ID3V1, ID3V2

_COUNT = {"type": "integer", "minimum": 0}
# A 64-bit integer of the source, as spec/conventions.md §6
# <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#6-source-metadata-as-json> writes it: a
# number up to 2^53 - 1 in magnitude, else the string of its decimal digits.
_U64 = {"anyOf": [_COUNT, {"type": "string", "pattern": "^[1-9][0-9]{15,19}$"}]}

SCHEMA = {"anyOf": [
    {
        "type": "object",
        "description": "The root's: the stream's parameters, STREAMINFO's fields, and the decoded metadata blocks and "
                       "tags (spec/zarrify/flac.md §5.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/flac.md#51-the-root>)",
        "properties": {
            "sample_rate": {"type": "integer", "minimum": 0},
            "channels": {"type": "integer", "minimum": 1, "maximum": 8},
            "bits_per_sample": {"type": "integer", "minimum": 4, "maximum": 32},
            "block_size": {"type": "integer", "minimum": 1, "maximum": 65535,
                           "description": "The first frame's block size: every frame's but the last's"},
            "frames": {"type": "integer", "minimum": 1},
            "samples": {"type": "integer", "minimum": 1, "description": "The samples per channel the frames hold"},
            "duration": {"type": ["number", "null"],
                         "description": "samples / sample_rate, in seconds; null for a sample rate of 0"},
            "streaminfo": {
                "type": "object",
                "description": "The STREAMINFO block's fields",
                "properties": {
                    "min_block_size": _COUNT, "max_block_size": _COUNT, "min_frame_size": _COUNT,
                    "max_frame_size": _COUNT, "sample_rate": _COUNT,
                    "channels": {"type": "integer", "minimum": 1, "maximum": 8},
                    "bits_per_sample": {"type": "integer", "minimum": 4, "maximum": 32},
                    "total_samples": _COUNT,
                    "md5": {"type": "string", "pattern": "^[0-9a-f]{32}$",
                            "description": "The MD5 of the samples, as 32 lowercase hexadecimal digits"},
                },
                "required": ["min_block_size", "max_block_size", "min_frame_size", "max_frame_size", "sample_rate",
                             "channels", "bits_per_sample", "total_samples", "md5"],
                "additionalProperties": False,
            },
            "blocks": {
                "type": "array", "minItems": 1,
                "description": "The metadata blocks, in file order: each one's type and body length",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"anyOf": [
                            {"enum": ["STREAMINFO", "PADDING", "APPLICATION", "SEEKTABLE", "VORBIS_COMMENT",
                                      "CUESHEET", "PICTURE"]},
                            {"type": "integer", "minimum": 7, "maximum": 126}]},
                        "size": _COUNT,
                    },
                    "required": ["type", "size"],
                    "additionalProperties": False,
                },
            },
            "seek_table": {
                "type": "object",
                "description": "The SEEKTABLE block's seek points and placeholders (the points are in the mirror)",
                "properties": {"points": _COUNT, "placeholders": _COUNT},
                "required": ["points", "placeholders"],
                "additionalProperties": False,
            },
            "vorbis_comment": {
                "type": "object",
                "description": "The VORBIS_COMMENT block: its vendor string and each field, whole, in order",
                "properties": {"vendor": {"$ref": "#/$defs/text"},
                               "comments": {"type": "array", "items": {"$ref": "#/$defs/text"}}},
                "required": ["vendor", "comments"],
                "additionalProperties": False,
            },
            "cuesheets": {
                "type": "array", "minItems": 1,
                "description": "The CUESHEET blocks' fields, in file order",
                "items": {
                    "type": "object",
                    "description": "The CUESHEET block's fields",
                    "properties": {
                        "catalog": {"$ref": "#/$defs/text"},
                        "lead_in": _U64,
                        "cd": {"type": "boolean"},
                        "tracks": {"type": "array", "items": {
                            "type": "object",
                            "properties": {
                                "offset": _U64, "number": _COUNT, "isrc": {"$ref": "#/$defs/text"},
                                "audio": {"type": "boolean"}, "pre_emphasis": {"type": "boolean"},
                                "indexes": {"type": "array", "items": {
                                    "type": "object",
                                    "properties": {"offset": _U64, "number": _COUNT},
                                    "required": ["offset", "number"],
                                    "additionalProperties": False,
                                }},
                            },
                            "required": ["offset", "number", "isrc", "audio", "pre_emphasis", "indexes"],
                            "additionalProperties": False,
                        }},
                    },
                    "required": ["catalog", "lead_in", "cd", "tracks"],
                    "additionalProperties": False,
                },
            },
            "pictures": {
                "type": "array", "minItems": 1,
                "description": "The PICTURE blocks' fields, in file order; the i-th one's data is the array "
                               "pictures/<i>, absent for a size of 0",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": _COUNT, "media_type": {"$ref": "#/$defs/text"},
                        "description": {"$ref": "#/$defs/text"}, "width": _COUNT, "height": _COUNT, "depth": _COUNT,
                        "colors": _COUNT, "size": _COUNT,
                    },
                    "required": ["type", "media_type", "description", "width", "height", "depth", "colors", "size"],
                    "additionalProperties": False,
                },
            },
            "applications": {
                "type": "array", "minItems": 1,
                "description": "The APPLICATION blocks: each one's ID and the length of its data (in the mirror)",
                "items": {
                    "type": "object",
                    "properties": {"id": {"$ref": "#/$defs/text"}, "size": _COUNT},
                    "required": ["id", "size"],
                    "additionalProperties": False,
                },
            },
            "id3v2": ID3V2,
            "id3v1": ID3V1,
        },
        "required": ["sample_rate", "channels", "bits_per_sample", "block_size", "frames", "samples", "duration",
                     "streaminfo", "blocks"],
        "additionalProperties": False,
    },
    {"$ref": "#/$defs/mirror"},
    {"$ref": "#/$defs/mirrorView"},
]}
