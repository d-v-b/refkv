"""The description of every accepted MP3 fixture (spec/zarrify/mp3.md §8.4
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp3.md#84-the-description>), compared with
ffmpeg (through PyAV) and libsndfile (through soundfile), the reference readers: the stream's sample
rate and channels; each audio frame ffmpeg demuxes, its offset, and its bitrate and main data as its
header gives them; the LAME tag's encoder, and its delay and padding as libsndfile trims them; and
the ID3v1 tag's fields as ffmpeg reads them. ffmpeg exposes neither the ID3v2 tags' headers nor the
APE tag's footer, so those are read here from the bytes the ID3v2 and APEv2 specifications lay out.
A second test asserts which cases the fixtures reached, and a third that the description holds none
of the output's decisions."""

import json
import struct
from collections import Counter
from functools import cache
from pathlib import Path
from typing import Any

import av
import jsonschema
import pytest
import soundfile
from ir_helpers import FIXTURES, ROOT

from refkv.build import read

FILES = sorted(p for p in (FIXTURES / "mp3").glob("*.mp3") if "reject" not in p.name)
SCHEMA = json.loads((ROOT / "spec" / "zarrify" / "mp3" / "description.json").read_text())
# ISO/IEC 11172-3 and 13818-3: Layer III bitrates in kbps by index, MPEG-1 then MPEG-2 and 2.5
BITRATES = {3: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
            2: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160]}
VERSIONS = {0: "MPEG-2.5", 2: "MPEG-2", 3: "MPEG-1"}
# members a description never has: the output's decisions (spec/zarrify/mp3.md §8.4
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp3.md#84-the-description>)
DECISIONS = {"chunk_frames", "chunks", "lead", "data_type", "codecs", "shape", "samples", "duration", "bitrate_mode",
             "bitrate_kbps", "dimension_names"}


@cache
def described(path: Path) -> tuple[dict[str, Any], list[tuple[int, int]]]:
    """(the description, each audio frame's (offset, length) in the IR)."""
    with read(path) as parsed:
        d: Any = parsed.description
        ir = parsed.ir
        first = d["elements"]["frames"]
        frames = [ir.extent(i) for i in range(first, first + d["frames"])]
    return d, frames


def header(b: bytes) -> tuple[int, bool, int]:
    """(bitrate in kbps, CRC, main data in bytes) of the Layer III frame whose bytes are `b`, from its
    four-byte header: its length less the header, the CRC and the side information."""
    version, crc, index, mode = (b[1] >> 3) & 3, b[1] & 1 == 0, b[2] >> 4, b[3] >> 6
    mono = mode == 3
    side = (17 if mono else 32) if version == 3 else (9 if mono else 17)
    return BITRATES[2 if version != 3 else 3][index], crc, len(b) - 4 - 2 * crc - side


def id3v2_headers(data: bytes) -> list[dict[str, Any]]:
    """The ID3v2 tags' headers at the start of `data` (ID3v2.4, "ID3v2 header"): `ID3`, the version's two bytes,
    the flags, and a syncsafe size, which a footer (flag 0x10) follows by 10 bytes."""
    out, p = [], 0
    while data[p:p + 3] == b"ID3":
        size = 0
        for c in data[p + 6:p + 10]:
            size = size << 7 | c
        footer = bool(data[p + 5] & 0x10)
        out.append({"version": [data[p + 3], data[p + 4]], "flags": data[p + 5], "size": size, "footer": footer})
        p += 10 + size + 10 * footer
    return out


def ape_footer(data: bytes) -> dict[str, int] | None:
    """The APEv2 tag's footer before the file's ID3v1 tag, or at its end: `APETAGEX`, the version,
    the size (the footer's 32 bytes included, a header of 32 more when flag bit 31 is set) and the
    item count."""
    end = len(data) - 128 if data[-128:-125] == b"TAG" else len(data)
    f = data[end - 32:end]
    if f[:8] != b"APETAGEX":
        return None
    version, size, items, flags = struct.unpack("<4I", f[8:24])
    return {"version": version, "size": size + 32 * bool(flags & 0x8000_0000), "items": items}


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_the_description_is_what_ffmpeg_and_libsndfile_read(path):
    d, frames = described(path)
    jsonschema.validate(d, SCHEMA)
    data = path.read_bytes()
    assert len(d["kbps"]) == len(d["main_data"]) == d["frames"] == len(frames)
    # each frame as its header gives it
    heads = [header(data[o:o + n]) for o, n in frames]
    assert d["kbps"] == [h[0] for h in heads]
    assert d["main_data"] == [h[2] for h in heads]
    assert d["crc_frames"] == sum(h[1] for h in heads)
    assert d["version"] == VERSIONS[(data[frames[0][0] + 1] >> 3) & 3]
    assert d["samples_per_frame"] == (1152 if d["version"] == "MPEG-1" else 576)
    assert d["id3v2"] == id3v2_headers(data)
    assert d["ape"] == ape_footer(data)
    try:
        container = av.open(str(path))
    except av.error.InvalidDataError:  # one frame: too little for ffmpeg's probe
        assert d["frames"] == 1
        return
    with container:
        stream = container.streams.audio[0]
        assert (d["sample_rate"], d["channels"]) == (stream.sample_rate, stream.channels)
        packets = [(p.pos or 0, p.size) for p in container.demux(stream) if p.size]
        if d["stray_bytes"] == 0:
            # ffmpeg resyncs on stray bytes otherwise, where its packets differ from the frames; it
            # passes on the bytes of an APE tag before an ID3v1 tag as a packet past the last frame
            end = frames[-1][0] + frames[-1][1]
            assert [p for p in packets if p[0] < end] == frames
        lame = d["info"] is not None and d["info"]["encoder"] is not None
        if lame and d["info"]["tag"] in ("Xing", "Info") and "encoder" in stream.metadata:
            assert stream.metadata["encoder"].rstrip() == d["info"]["encoder"]
        if d["id3v1"] is not None and not d["id3v2"]:
            v1, theirs = d["id3v1"], container.metadata
            for ours, key in (("title", "title"), ("artist", "artist"), ("album", "album"), ("year", "date"),
                              ("comment", "comment")):
                assert v1[ours] == theirs.get(key, ""), ours
            assert (str(v1["track"]) if v1["track"] is not None else None) == theirs.get("track")
    info = soundfile.info(str(path))
    assert (info.samplerate, info.channels) == (d["sample_rate"], d["channels"])
    if lame and d["info"]["delay"] is not None:
        # libsndfile (mpg123) trims the LAME tag's delay and padding
        trimmed = d["frames"] * d["samples_per_frame"] - d["info"]["delay"] - d["info"]["padding"]
        assert info.frames == trimmed


def test_the_fixtures_reach_every_case():
    seen: Counter[str] = Counter()
    for path in FILES:
        d, _ = described(path)
        seen[d["version"]] += 1
        seen["variable"] += len(set(d["kbps"])) > 1
        seen["crc"] += d["crc_frames"] > 0
        seen["stray"] += d["stray_bytes"] > 0
        for k in ("id3v1", "ape"):
            seen[k] += d[k] is not None
        seen["id3v2"] += len(d["id3v2"])
        seen["footer"] += any(t["footer"] for t in d["id3v2"])
        if d["info"] is not None:
            seen[d["info"]["tag"]] += 1
            seen["lame"] += d["info"]["encoder"] is not None
    for case in ("MPEG-1", "MPEG-2", "MPEG-2.5", "variable", "crc", "stray", "id3v1", "ape", "id3v2", "footer",
                 "Xing", "Info", "lame"):
        assert seen[case], case


def test_the_description_holds_no_decision():
    d, _ = described(FIXTURES / "mp3" / "mp3_tags_ape_id3v1.mp3")
    assert not DECISIONS & set(d)
    assert set(d["elements"]) == {"frames", "info", "id3v1"}
