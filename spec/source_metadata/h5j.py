"""The H5J convention's source metadata (spec/zarrify/h5j.md), for
spec/generate_schemas.py."""

# 64-bit integers, exact whatever their magnitude (spec/conventions.md §6
# <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#6-source-metadata-as-json>)
_U64 = {"type": "integer", "minimum": 0}
_I64 = {"type": "integer"}

# an HDF5 object's attributes, as the HDF5 convention writes them (spec/zarrify/hdf5.md §6
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#6-attributes>)
_ATTRIBUTES = {"type": "object", "description": "The HDF5 object's attributes, as the HDF5 convention writes them"}

_EDITS = {"type": "array",
          "description": "The track's edit list (its elst box's entries), recorded and not applied",
          "items": {"type": "object", "properties": {"duration": _U64, "media_time": _I64, "rate": {"type": "number"}},
                    "required": ["duration", "media_time", "rate"], "additionalProperties": False}}

SCHEMA = {"anyOf": [
    {
        "type": "object",
        "description": "The root's: the root group's and /Channels's attributes, and every channel, with where it is "
                       "or why it is not zarrified (spec/zarrify/h5j.md §5.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/h5j.md#51-the-root>)",
        "properties": {
            "attributes": _ATTRIBUTES,
            "channels_attributes": _ATTRIBUTES,
            "channels": {"type": "array", "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "path": {"type": ["string", "null"]},
                               "reason": {"type": ["string", "null"]}},
                "required": ["name", "path", "reason"], "additionalProperties": False}},
        },
        "required": ["attributes", "channels_attributes", "channels"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "description": "A channel's image group: its dataset, its attributes and its MP4 file's track "
                       "(spec/zarrify/h5j.md §5.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/h5j.md#52-a-channel>)",
        "properties": {
            "dataset": {"type": "string", "description": "The channel's dataset, Channels/Channel_<n>"},
            "offset": {**_U64, "description": "Where the dataset's bytes, the MP4 file, start in the H5J file"},
            "length": {**_U64, "description": "The dataset's bytes"},
            "attributes": _ATTRIBUTES,
            "brand": {"type": ["object", "null"], "properties": {
                "major": {"type": "string"}, "minor": {"type": "integer"},
                "compatible": {"type": "array", "items": {"type": "string"}}}},
            "movie_timescale": {"type": "integer", "minimum": 0},
            "movie_duration": _U64,
            "track": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "minimum": 0}, "codec": {"type": "string"},
                    "format": {"enum": ["h264", "hevc", "av1", "vp9"]},
                    "timescale": {"type": "integer", "minimum": 1}, "duration": _U64, "edits": _EDITS,
                    "bit_depth": {"enum": [8, 10, 12]}, "profile": {"type": "integer"}, "level": {"type": "integer"},
                    "width": {"type": "integer", "minimum": 1,
                              "description": "The coded pictures' width, padding included"},
                    "height": {"type": "integer", "minimum": 1,
                               "description": "The coded pictures' height, padding included"},
                    "chroma": {"const": "4:0:0"},
                    "frames": {"type": "integer", "minimum": 1, "description": "The pictures: the slices"},
                    "sync_samples": {"type": "integer", "minimum": 1},
                },
                "required": ["id", "codec", "format", "timescale", "duration", "edits", "bit_depth", "profile", "level",
                             "width", "height", "chroma", "frames", "sync_samples"],
                "additionalProperties": False,
            },
            "chunk_frames": {"type": "integer", "minimum": 1, "description": "The slices of a chunk"},
        },
        "required": ["dataset", "offset", "length", "attributes", "brand", "movie_timescale", "movie_duration",
                     "track", "chunk_frames"],
        "additionalProperties": False,
    },
    {"$ref": "#/$defs/mirror"},
    {"$ref": "#/$defs/mirrorView"},
]}
