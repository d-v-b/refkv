"""The MP4 convention's source metadata (spec/zarrify/mp4.md), for
spec/generate_schemas.py."""

_MP4_EDITS = {"type": "array", "description": "The track's edit list (its elst box's entries), recorded and not applied",
              "items": {"type": "object", "properties": {"duration": {"type": "integer", "minimum": 0},
                                                         "media_time": {"type": "integer"}, "rate": {"type": "number"}},
                        "required": ["duration", "media_time", "rate"], "additionalProperties": False}}
_MP4_TRACK_COMMON = {
    "id": {"type": "integer", "minimum": 0, "description": "The track's ID (its tkhd box's)"},
    "handler": {"type": "string", "description": "The handler type of its mdia box's hdlr box: vide or soun"},
    "codec": {"type": "string", "description": "Its sample entry's type: avc1, mp4a or .mp3"},
    "timescale": {"type": "integer", "minimum": 1, "description": "The media timescale: time units a second"},
    "duration": {"type": "integer", "minimum": 0, "description": "The media duration, in the timescale"},
    "edits": _MP4_EDITS,
    "frames": {"type": "integer", "minimum": 1, "description": "The track's samples: coded pictures, or coded audio frames"},
    "chunk_frames": {"type": "integer", "minimum": 1, "description": "The frames of a chunk of the track's arrays"},
}
SCHEMA = {"anyOf": [
    {
        "type": "object",
        "description": "The root's: the file's brands, the movie's timescale and duration, and every track, with where it is or why it is not zarrified (spec/zarrify/mp4.md §5.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp4.md#51-the-root>)",
        "properties": {
            "brand": {"type": "object", "properties": {"major": {"type": "string"}, "minor": {"type": "integer"},
                                                       "compatible": {"type": "array", "items": {"type": "string"}}},
                      "required": ["major", "minor", "compatible"], "additionalProperties": False},
            "timescale": {"type": "integer", "minimum": 0},
            "duration": {"type": "integer", "minimum": 0},
            "tracks": {"type": "array", "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "handler": {"type": "string"}, "codec": {"type": "string"},
                               "path": {"type": ["string", "null"], "description": "The track's group, when it is zarrified"},
                               "reason": {"type": ["string", "null"], "description": "Why it is not, when it is not"}},
                "required": ["id", "handler", "codec", "path", "reason"], "additionalProperties": False}},
        },
        "required": ["timescale", "duration", "tracks"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "description": "A video track's group (spec/zarrify/mp4.md §5.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp4.md#52-the-tracks>)",
        "properties": {
            **_MP4_TRACK_COMMON,
            "format": {"const": "h264"},
            "width": {"type": "integer", "minimum": 1}, "height": {"type": "integer", "minimum": 1},
            "profile": {"type": "integer", "description": "The SPS's profile_idc"},
            "level": {"type": "integer", "description": "The SPS's level_idc"},
            "sync_samples": {"type": "integer", "minimum": 1, "description": "The keyframes"},
        },
        "required": [*_MP4_TRACK_COMMON, "format", "width", "height", "profile", "level", "sync_samples"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "description": "An audio track's group (spec/zarrify/mp4.md §5.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp4.md#52-the-tracks>)",
        "properties": {
            **_MP4_TRACK_COMMON,
            "format": {"enum": ["aac", "mp3"]},
            "sample_rate": {"type": "integer", "minimum": 1},
            "channels": {"type": "integer", "minimum": 1},
            "samples_per_frame": {"enum": [576, 1024, 1152]},
            "samples": {"type": "integer", "minimum": 1, "description": "frames * samples_per_frame"},
        },
        "required": [*_MP4_TRACK_COMMON, "format", "sample_rate", "channels", "samples_per_frame", "samples"],
        "additionalProperties": False,
    },
    {"$ref": "#/$defs/mirror"},
    {"$ref": "#/$defs/mirrorView"},
]}
