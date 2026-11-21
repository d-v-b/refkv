"""The description of every accepted FLAC fixture (spec/zarrify/flac.md §8.4
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/flac.md#84-the-description>), compared with
ffmpeg (through PyAV) and libsndfile (through soundfile), the reference readers: STREAMINFO's sample
rate, channels, bits and total samples, and its bytes as ffmpeg's decoder takes them; each frame
ffmpeg demuxes, at its offset; the Vorbis comment's fields; the first CUESHEET's tracks, which ffmpeg
reads as chapters; and each picture ffmpeg attaches, its description and data. ffmpeg exposes
neither the metadata blocks' headers nor the SEEKTABLE and APPLICATION blocks, so those are read
here from the bytes RFC 9639 lays out. A second test asserts which cases the fixtures reached, and a
third that the description holds none of the output's decisions."""

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

FILES = sorted(p for p in (FIXTURES / "flac").glob("*.flac") if "reject" not in p.name)
SCHEMA = json.loads((ROOT / "spec" / "zarrify" / "flac" / "description.json").read_text())
# RFC 9639 §8.1: the metadata block types by number
BLOCKS = ["STREAMINFO", "PADDING", "APPLICATION", "SEEKTABLE", "VORBIS_COMMENT", "CUESHEET", "PICTURE"]
# libsndfile's subtypes by bits a sample (it reads 8, 16 and 24 only)
SUBTYPES = {8: "PCM_S8", 16: "PCM_16", 24: "PCM_24"}
# members a description never has: the output's decisions (spec/zarrify/flac.md §8.4
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/flac.md#84-the-description>)
DECISIONS = {"shard_frames", "shards", "data_type", "codecs", "setup", "shape", "duration", "dimension_names"}


@cache
def described(path: Path) -> tuple[dict[str, Any], list[tuple[int, int]], list[bytes | None]]:
    """(the description, each frame's (offset, length) in the IR, each picture's data)."""
    data = path.read_bytes()
    with read(path) as parsed:
        d: Any = parsed.description
        ir = parsed.ir
        first = d["elements"]["frames"]
        frames = [ir.extent(i) for i in range(first, first + d["frames"])]
        pictures = []
        for e in d["elements"]["pictures"]:
            o, n = ir.extent(e) if e is not None else (0, 0)
            pictures.append(data[o:o + n] if e is not None else None)
    return d, frames, pictures


def metadata_blocks(data: bytes) -> list[tuple[int, bytes]]:
    """Each metadata block's type and body (RFC 9639 §8.1), after the ID3v2 tags at the start and
    `fLaC`."""
    p = 0
    while data[p:p + 3] == b"ID3":
        size = 0
        for c in data[p + 6:p + 10]:
            size = size << 7 | c
        p += 10 + size + 10 * bool(data[p + 5] & 0x10)
    assert data[p:p + 4] == b"fLaC"
    p += 4
    out = []
    while True:
        kind, n = data[p] & 0x7F, int.from_bytes(data[p + 1:p + 4], "big")
        out.append((kind, data[p + 4:p + 4 + n]))
        p += 4 + n
        if data[p - 4 - n] & 0x80:
            return out


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_the_description_is_what_ffmpeg_and_libsndfile_read(path):
    d, frames, pictures = described(path)
    jsonschema.validate(d, SCHEMA)
    data = path.read_bytes()
    si = d["streaminfo"]
    blocks = metadata_blocks(data)
    assert d["blocks"] == [{"type": BLOCKS[k] if k < len(BLOCKS) else k, "size": len(b)} for k, b in blocks]
    seek = [b for k, b in blocks if k == 3]
    if seek:
        points = [struct.unpack(">Q", seek[0][i:i + 8])[0] for i in range(0, len(seek[0]) // 18 * 18, 18)]
        placeholders = points.count(2**64 - 1)
        assert d["seek_table"] == {"points": len(points) - placeholders, "placeholders": placeholders}
    else:
        assert d["seek_table"] is None
    assert d["applications"] == [{"id": b[:4].decode("latin1"), "size": len(b) - 4} for k, b in blocks if k == 2]
    assert len(d["pictures"]) == len(pictures) == sum(k == 6 for k, _ in blocks)
    assert len(d["cuesheets"]) == sum(k == 5 for k, _ in blocks)
    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        assert (si["sample_rate"], si["channels"]) == (stream.sample_rate, stream.channels)
        streaminfo = bytes(stream.codec_context.extradata or b"")
        assert streaminfo == bytes(next(b for k, b in blocks if k == 0))
        assert si["md5"] == streaminfo[18:34].hex()
        assert (si["min_block_size"], si["max_block_size"]) == struct.unpack(">HH", streaminfo[:4])
        demuxed = [(p.pos or 0, p.size, sum(f.samples for f in p.decode())) for p in container.demux(stream) if p.size]
        if d["id3v1"] is not None:
            # ffmpeg passes on an ID3v1 tag's 128 bytes with the last frame
            o, n, k = demuxed[-1]
            demuxed[-1] = (o, n - 128, k)
        assert [(o, n) for o, n, _ in demuxed] == frames
        # each frame's samples, as ffmpeg decodes them
        assert d["block_size"] == demuxed[0][2]
        assert d["samples"] == sum(k for _, _, k in demuxed)
        if d["vorbis_comment"] is not None:
            fields = [c.partition("=") for c in d["vorbis_comment"]["comments"]]
            assert {k.upper(): v for k, _, v in fields} == {k.upper(): v for k, v in container.metadata.items()}
        if d["cuesheets"]:
            # ffmpeg reads the first CUESHEET's tracks but the lead-out as chapters, titled by ISRC
            tracks = [t for t in d["cuesheets"][0]["tracks"] if t["number"] not in (170, 255)]
            assert [(c["start"], c["metadata"].get("title", "")) for c in container.chapters()] == [
                (t["offset"], t["isrc"]) for t in tracks]
    with av.open(str(path)) as container:
        attached = [s for s in container.streams if s.type == "video"]
        theirs = [(s.metadata.get("title", ""), bytes(p)) for s in attached for p in container.demux(s) if p.size]
        ours = [(p["description"], b) for p, b in zip(d["pictures"], pictures, strict=True)
                if b is not None and p["media_type"] != "-->"]
        assert theirs == ours
    if si["bits_per_sample"] in SUBTYPES:
        info = soundfile.info(str(path))
        assert (info.samplerate, info.channels, info.subtype) == (si["sample_rate"], si["channels"],
                                                                 SUBTYPES[si["bits_per_sample"]])
        if si["total_samples"]:
            assert info.frames == si["total_samples"] == d["samples"]


def test_the_fixtures_reach_every_case():
    seen: Counter[str] = Counter()
    for path in FILES:
        d, _, pictures = described(path)
        seen[str(d["streaminfo"]["bits_per_sample"])] += 1
        seen["unknown_total"] += d["streaminfo"]["total_samples"] == 0
        for k in ("seek_table", "vorbis_comment", "id3v1"):
            seen[k] += d[k] is not None
        for k in ("cuesheets", "pictures", "applications", "id3v2"):
            seen[k] += len(d[k]) > 0
        seen["empty_picture"] += None in pictures
    for case in ("8", "16", "24", "32", "unknown_total", "seek_table", "vorbis_comment", "id3v1", "cuesheets",
                 "pictures", "applications", "id3v2", "empty_picture"):
        assert seen[case], case


def test_the_description_holds_no_decision():
    d, _, _ = described(FIXTURES / "flac" / "flac_blocks.flac")
    assert not DECISIONS & set(d)
    assert set(d["elements"]) == {"frames", "streaminfo", "id3v1", "pictures"}
    assert not any("element" in p for p in d["pictures"])
