"""The MP4 IR: the Rust parser and its schema, the projection (spec/virtualize/mp4.md) and the
`refkv.stream` codec against ffmpeg's decoding of every synthetic file, the mirror's rebuild, the
tracks the profile does not virtualize, and the inputs it rejects. MP4 has no frozen Python
reference: ffmpeg's demuxer and decoders (through PyAV), which read the file's tables
themselves, are the reader it is compared with."""

import json

import av
import numpy as np
import pytest

from ir_helpers import FIXTURES, core_setting
from ir_helpers import virtualized as shared_virtualized
from refkv import _core
from refkv.zarr.codecs.stream import stream_units

FILES = sorted(p for p in (FIXTURES / "mp4").iterdir() if p.suffix in (".mp4", ".mov"))
ACCEPTED = [p for p in FILES if "reject" not in p.name]
# AAC with noise substitution: a chunk decodes its noise from a generator of its own
# (spec/virtualize/mp4.md §4.2
# <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp4.md#42-audio-tracks>)
NOISE = {"mp4_aac_pns.mp4"}


def virtualized(path, tmp_path, chunk_frames=None):
    """(the output, the hierarchy's root group), with at most `chunk_frames` frames a chunk (None: the
    profile's 256); the archive rebuilds the file (ir_helpers.virtualized)."""
    with core_setting(_core._set_chunk_frames, chunk_frames):
        out, root, ir = shared_virtualized(path, tmp_path, "mp4")
    return out, root

def decoded(path, index):
    """ffmpeg's demuxer's packets of stream `index`, decoded in one pass by a decoder that is
    given them alone (no edit list, no priming trimmed): (kind, frames, packets' presentation
    times)."""
    c = av.open(str(path))
    s = c.streams[index]
    ctx = av.CodecContext.create(s.codec_context.name, "r")
    ctx.extradata = s.codec_context.extradata
    packets = [p for p in c.demux(s) if p.size]
    frames = []
    for k, p in enumerate(packets):
        q = av.Packet(bytes(p))
        q.pts = k
        frames += ctx.decode(q)
    frames += ctx.decode(None)
    return s.type, frames, sorted(p.pts for p in packets)


def plane(f, k):
    p = f.planes[k]
    return np.frombuffer(p, np.uint8).reshape(p.height, p.line_size)[:, :p.width]


def samples(frames):
    return np.concatenate([f.to_ndarray().T if f.format.is_planar else f.to_ndarray().reshape(f.samples, -1)
                           for f in frames]).astype(np.float32)


@pytest.mark.parametrize("chunk_frames", [None, 7])
@pytest.mark.parametrize("path", ACCEPTED, ids=lambda p: p.name)
def test_tracks_are_ffmpegs_decoding(path, chunk_frames, tmp_path):
    """Every virtualized track is ffmpeg's decoding of its samples, value for value: a video
    track's Y, U and V planes and its presentation times, an audio track's samples (spec/virtualize/mp4.md
    §4 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp4.md#4-the-layout>), with chunks of
    the profile's size and of 7 frames (so that every track has several chunks, each with its
    lead-in); the file is rebuilt from the mirror."""
    out, root = virtualized(path, tmp_path, chunk_frames)
    meta = root.attrs["refkv_virtualized"]["mp4"]
    assert set(root.attrs) == {"zarr_conventions", "refkv_virtualized"}
    paths = [t["path"] for t in meta["tracks"] if t["path"]]
    assert paths == [t["path"] for t in out.summary["tracks"]] and paths
    for track in meta["tracks"]:
        if track["path"] is None:
            continue
        g = root[track["path"]]
        tm = g.attrs["refkv_virtualized"]["mp4"]
        assert tm["id"] == track["id"] and tm["format"] in ("h264", "aac", "mp3")
        kind, frames, pts = decoded(path, track["id"] - 1)
        if kind == "video":
            assert tm["frames"] == len(frames) and tm["width"] == frames[0].width
            for k, p in enumerate("yuv"):
                want = np.stack([plane(f, k) for f in frames])
                got = g[p][...]
                assert got.dtype == np.uint8 and got.shape == want.shape, p
                assert np.array_equal(got, want), p
            # the media timeline's times: ffmpeg's demuxer applies the edit list, which the
            # layout records and does not apply
            shift = next((e["media_time"] for e in tm["edits"] if e["media_time"] >= 0), 0)
            assert g["time"][...].tolist() == [t + shift for t in pts]
            assert g["y"].metadata.dimension_names == ("time", "y", "x")
        else:
            want = samples(frames)
            got = g["samples"][...]
            assert got.dtype == np.float32 and got.shape == want.shape
            assert tm["samples"] == len(got) == tm["frames"] * tm["samples_per_frame"]
            if path.name in NOISE and chunk_frames is not None:
                # the noise bands differ; the signal's level does not
                assert abs(np.sqrt(np.mean(got ** 2)) - np.sqrt(np.mean(want ** 2))) < 0.1 * np.sqrt(np.mean(want ** 2))
            else:
                assert np.array_equal(got, want)
    assert out.data == {}  # no data sources


def test_a_video_chunk_holds_its_lead_in(tmp_path):
    """mp4_h264_long.mp4 (400 pictures of 32 x 32 in groups of 50, three B-frames) has two
    chunks of 256 pictures; the second's reference starts at the sync sample before picture
    256 in decode order, and its header places the lead-in's pictures at -1 and the chunk's
    256 to 399 at 0 to 143 (conventions §10
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#10-coded-streams>)."""
    out, root = virtualized(FIXTURES / "mp4" / "mp4_h264_long.mp4", tmp_path)
    assert root["tracks/1/y"].chunks == (256, 32, 32)
    lit, *ranges = out.refs["tracks/1/y/c/1/0/0"]
    data = (FIXTURES / "mp4" / "mp4_h264_long.mp4").read_bytes()
    units, positions = stream_units(lit + b"".join(data[o:o + n] for o, n in ranges))
    assert sorted(p for p in positions if p >= 0) == list(range(144))
    assert positions[0] == -1 and len(units) > 144
    assert out.refs["tracks/1/u/c/1/0/0"] == out.refs["tracks/1/y/c/1/0/0"] == out.refs["tracks/1/v/c/1/0/0"]
    codecs = json.loads(out.bytes_entries["tracks/1/u/zarr.json"])["codecs"]
    assert codecs[0]["name"] == "refkv.stream" and codecs[0]["configuration"]["plane"] == "u"
    assert codecs[0]["configuration"]["format"] == "h264" and codecs[0]["configuration"]["setup"]


# (file, the tracks it virtualizes, each other track's ID and the start of its reason)
TRACKS = [
    ("mp4_hevc_aac.mp4", ["tracks/2"], {1: "hev1 video is not supported"}),
    ("mp4_h264_10bit_aac.mp4", ["tracks/2"], {1: "H.264 with chroma format 1 and bit depth 10 is not supported"}),
    ("mp4_h264_aac.mp4", ["tracks/1", "tracks/2"], {}),
    ("mov_h264_aac.mov", ["tracks/1", "tracks/2"], {}),
    ("mp4_h264_two_tracks.mp4", ["tracks/1", "tracks/2"], {}),
]


def test_records_the_tracks_it_does_not_virtualize(tmp_path):
    for name, projected, reasons in TRACKS:
        out, root = virtualized(FIXTURES / "mp4" / name, tmp_path)
        tracks = root.attrs["refkv_virtualized"]["mp4"]["tracks"]
        assert [t["path"] for t in tracks if t["path"]] == projected, name
        assert {t["id"]: t["reason"][:len(reasons[t["id"]])] for t in tracks if t["reason"]} == reasons, name
        assert {s["id"] for s in out.summary["skipped"]} == set(reasons), name


def test_reads_the_movie_and_track_metadata(tmp_path):
    """The brands, the timescales, the edit list (recorded, not applied) and a VFR track's
    presentation times (spec/virtualize/mp4.md §5
    <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp4.md#5-source-metadata>)."""
    out, root = virtualized(FIXTURES / "mp4" / "mp4_h264_bframes.mp4", tmp_path)
    meta = root.attrs["refkv_virtualized"]["mp4"]
    assert meta["brand"]["major"] == "isom" and "mp41" in meta["brand"]["compatible"]
    track = root["tracks/1"].attrs["refkv_virtualized"]["mp4"]
    assert track["edits"] and track["edits"][0]["media_time"] > 0  # B-frames: the composition delay
    assert track["sync_samples"] == 3 and track["chunk_frames"] == 256
    _, root = virtualized(FIXTURES / "mp4" / "mp4_h264_vfr.mp4", tmp_path)
    times = root["tracks/1/time"][...]
    assert root["tracks/1"].attrs["refkv_virtualized"]["mp4"]["timescale"] == 1000
    assert len(set(np.diff(times).tolist())) > 1  # the steps differ
