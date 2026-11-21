"""The description of every accepted MP4 and QuickTime fixture (spec/zarrify/mp4.md §8.5
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp4.md#85-the-description>), compared with
ffmpeg (through PyAV), the reference reader: the brands; each track's ID, sample entry type,
timescale and sample count; its samples, at their offsets and sizes, in decode order, and which are
sync samples; its presentation times, up to the shift an edit list gives them; its configuration
box's payload as ffmpeg's decoder takes it; a video track's size, chroma and bit depth (its pixel
format); and an audio track's sample rate and channels. ffmpeg exposes neither VP9's `vpcC` box nor
the boxes' own fields, so those are checked against the bytes of the IR's elements. A second test
asserts which cases the fixtures reached, and a third that the description holds none of the
output's decisions."""

import base64
import json
from collections import Counter
from functools import cache
from pathlib import Path
from typing import Any

import av
import jsonschema
import pytest
from ir_helpers import FIXTURES, ROOT

from refkv.build import read

FILES = sorted(p for p in (FIXTURES / "mp4").iterdir() if p.is_file() and "reject" not in p.name)
SCHEMA = json.loads((ROOT / "spec" / "zarrify" / "mp4" / "description.json").read_text())
# ffmpeg's pixel formats by (chroma_format_idc, bit depth)
PIXEL_FORMATS = {"gray": (0, 8), "gray10le": (0, 10), "gray12le": (0, 12), "yuv420p": (1, 8), "yuv420p10le": (1, 10),
                 "yuv420p12le": (1, 12), "yuv422p": (2, 8), "yuv422p10le": (2, 10), "yuv422p12le": (2, 12),
                 "yuv444p": (3, 8), "yuv444p10le": (3, 10), "yuv444p12le": (3, 12)}
PCM = {"sowt", "twos", "raw ", "NONE", "in24", "in32", "fl32", "fl64", "ipcm", "fpcm", "lpcm"}
# members a description never has: the output's decisions (spec/zarrify/mp4.md §8.5
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp4.md#85-the-description>)
DECISIONS = {"reason", "format", "geometry", "path", "chunk_frames", "data_type", "codecs", "setup", "spf", "frames"}


@cache
def described(path: Path) -> tuple[dict[str, Any], list[list[tuple[int, int]]]]:
    """(the description, each track's samples' (offset, length) in the IR)."""
    with read(path) as parsed:
        d: Any = parsed.description
        ir = parsed.ir
        samples = []
        for t in d["tracks"]:
            first = t["elements"]["first"]
            samples.append([] if first is None else [ir.extent(i) for i in range(first, first + t["samples"])])
    return d, samples


def demuxed(path: Path, index: int) -> list[av.Packet]:
    with av.open(str(path)) as container:
        return [p for p in container.demux(container.streams[index]) if p.size]


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_the_description_is_what_ffmpeg_reads(path):
    d, samples = described(path)
    jsonschema.validate(d, SCHEMA)
    with av.open(str(path)) as container:
        meta = container.metadata
        if d["brand"] is not None:
            assert d["brand"]["major"] == meta["major_brand"]
            assert str(d["brand"]["minor"]) == meta["minor_version"]
            assert "".join(d["brand"]["compatible"]) == meta.get("compatible_brands", "")
        assert len(container.streams) == len(d["tracks"])
        for t, s, ours in zip(d["tracks"], container.streams, samples, strict=True):
            check_track(path, t, s, ours)


def check_track(path: Path, t: dict[str, Any], s: Any, ours: list[tuple[int, int]]) -> None:
    """Track `t` of the description against ffmpeg's stream `s`, its samples `ours` in the IR."""
    at = f"{path.name}: track {t['index']}"
    e = t["entry"]
    assert t["id"] == s.id, at
    assert e["type"] == s.codec_tag, at
    assert s.time_base is not None, at
    assert t["timescale"] == s.time_base.denominator, at
    assert t["bytes"] == sum(n for _, n in ours), at
    assert t["sizes"] == list(dict.fromkeys(n for _, n in ours)), at
    packets = demuxed(path, s.index)
    theirs = [(p.pos, p.size) for p in packets]
    cc = s.codec_context
    if e["type"] in PCM:
        # ffmpeg cuts PCM into packets of its own, from the samples' bytes, of whole frames;
        # it lays out a QuickTime sample size that is not whole frames otherwise (the profile
        # zarrifies no such track)
        frame = cc.channels * cc.format.bytes
        fixed = t["stsz_sample_size"]
        if all(n % frame == 0 for n in t["sizes"]) and (fixed in (None, 1) or fixed % frame == 0):
            assert sum(n for _, n in theirs) == t["bytes"], at
    else:
        # ffmpeg may discard the last audio packets past the edit list's end
        assert theirs == ours[:len(theirs)], at
        assert len(ours) - len(theirs) <= 1, at
        assert t["samples"] == len(ours), at
    config = e["video_config"] or e["esds"] or {}
    payload = config.get("data", config.get("config"))
    if payload is not None and e["type"] != "vp09":
        assert base64.b64decode(payload) == bytes(cc.extradata or b""), at
    if t["handler"] == "vide":
        assert (e["width"], e["height"]) == (cc.width, cc.height), at
        sync = {k for k, p in enumerate(packets) if p.is_keyframe}
        assert set(t["sync"]) == sync, at
        pts = [p.pts or 0 for p in packets]
        # ffmpeg shifts every presentation time by the edit list's media time
        assert [p - t["pts"][0] for p in t["pts"]] == [p - pts[0] for p in pts], at
        fields = (e["video_config"] or {}).get("fields")
        if fields is not None and cc.pix_fmt in PIXEL_FORMATS:
            assert (fields["chroma"], fields["luma_bits"]) == PIXEL_FORMATS[cc.pix_fmt], at
    aac = (e["esds"] or {}).get("aac")
    # AAC-LC, and HE-AAC of SBR at twice its rate: ffmpeg reads others' rates otherwise
    lc = aac is not None and aac["object_type"] == 2 and aac["sbr_sample_rate"] in (None, 2 * aac["sample_rate"])
    if t["handler"] == "soun" and aac is not None and lc:
        assert cc.sample_rate == (aac["sbr_sample_rate"] or aac["sample_rate"]), at
        assert cc.channels == (2 if aac["parametric_stereo"] else aac["channels"] or e["channels"]), at
    elif t["handler"] == "soun" and e["type"] in PCM:
        assert (cc.sample_rate, cc.channels) == (e["sample_rate"] or t["timescale"], e["channels"]), at


def test_the_fixtures_reach_every_case():
    seen: Counter[str] = Counter()
    for path in FILES:
        d, _ = described(path)
        for t in d["tracks"]:
            e = t["entry"] or {}
            seen[e.get("type", "none")] += 1
            seen["edits"] += bool(t["edits"])
            seen["other_description"] += t["other_description"]
            seen["jpeg"] += e.get("jpeg") is not None
            seen["sbr"] += bool(((e.get("esds") or {}).get("aac") or {}).get("sbr_sample_rate"))
            seen["ps"] += bool(((e.get("esds") or {}).get("aac") or {}).get("parametric_stereo"))
            seen["leading"] += t["pts"] is not None and t["pts"] != sorted(t["pts"])
    for case in ("avc1", "hvc1", "hev1", "av01", "vp09", "mp4a", "jpeg", "mjpa", "dmb1", "mp4v", "sowt", "twos", "in32",
                 "fl32", "fl64", "ipcm", "fpcm", "lpcm", "raw ", "edits", "sbr", "ps", "leading"):
        assert seen[case], case


def test_the_description_holds_no_decision():
    d, _ = described(FIXTURES / "mp4" / "mp4_h264_aac.mp4")
    assert not DECISIONS & set(d)
    for t in d["tracks"]:
        assert not DECISIONS & set(t)
        assert set(t["elements"]) == {"trak", "first"}
