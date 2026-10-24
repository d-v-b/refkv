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
from refkv.zarr.codecs import stream
from refkv.zarr.codecs.stream import Shared, StreamCodec, stream_units

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


def chunk_units(out, key, data):
    """The frames of the chunk `key` and their positions (conventions §10
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#10-coded-streams>)."""
    lit, *ranges = out.refs[key]
    return stream_units(lit + b"".join(data[o:o + n] for o, n in ranges))


def test_video_chunks_start_at_keyframes(tmp_path):
    """Video chunks are runs of whole groups of pictures, a group longer than a chunk split
    (spec/virtualize/mp4.md §4.1
    <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp4.md#41-video-tracks>):
    mp4_h264_long.mp4 (400 pictures of 32 x 32 in groups of 50) has chunks of 250 and 150
    pictures, a regular grid, each starting at a keyframe with no lead-in.
    mp4_h264_irregular_groups.mp4 (60 pictures, groups starting at 0, 7, 9, 23 and 40) in
    chunks of at most 7 has a rectilinear grid; a chunk that starts at a keyframe has no
    lead-in, and one inside a group decodes from its keyframe (§8.3
    <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp4.md#83-chunks>)."""
    path = FIXTURES / "mp4" / "mp4_h264_long.mp4"
    out, root = virtualized(path, tmp_path)
    assert root["tracks/1/y"].chunks == (250, 32, 32)
    units, positions = chunk_units(out, "tracks/1/y/c/1/0/0", path.read_bytes())
    assert sorted(positions) == list(range(150))
    assert out.refs["tracks/1/u/c/1/0/0"] == out.refs["tracks/1/y/c/1/0/0"] == out.refs["tracks/1/v/c/1/0/0"]
    codecs = json.loads(out.bytes_entries["tracks/1/u/zarr.json"])["codecs"]
    assert codecs[0]["name"] == "refkv.stream" and codecs[0]["configuration"]["plane"] == "u"
    assert codecs[0]["configuration"]["format"] == "h264" and codecs[0]["configuration"]["setup"]

    path = FIXTURES / "mp4" / "mp4_h264_irregular_groups.mp4"
    out, root = virtualized(path, tmp_path, chunk_frames=7)
    grid = json.loads(out.bytes_entries["tracks/1/y/zarr.json"])["chunk_grid"]
    assert grid == {"name": "rectilinear", "configuration": {"kind": "inline",
                                                             "chunk_shapes": [[7, 2, [7, 4], 3, [7, 2], 6], 48, 64]}}
    assert json.loads(out.bytes_entries["tracks/1/u/zarr.json"])["chunk_grid"]["configuration"]["chunk_shapes"][1:] == [24, 32]
    data = path.read_bytes()
    starts = [0, 7, 9, 16, 23, 30, 37, 40, 47, 54]
    for q, (a, b) in enumerate(zip(starts, starts[1:] + [60])):
        units, positions = chunk_units(out, f"tracks/1/y/c/{q}/0/0", data)
        assert sorted(p for p in positions if p >= 0) == list(range(b - a)), q
        if a in (0, 7, 9, 23, 40):
            # the keyframe first (a P-frame its last B-frames need is decoded and dropped after them)
            assert positions[0] == 0, q
        else:
            # every picture back to the keyframe decoded and dropped: 9 to 15 for 16, 23 to 29 for 30
            assert positions[0] == -1 and sum(p < 0 for p in positions) >= a - max(k for k in (0, 7, 9, 23, 40) if k <= a), q
    assert out.summary["tracks"][0]["chunks"] == 10


def test_a_video_chunk_s_planes_decode_once(tmp_path, monkeypatch):
    """Reading a chunk's Y, U and V decodes it once: the codec keeps the planes not asked for
    (refkv.zarr.codecs.stream); and the lead-in's pictures are dropped as the decoder gives
    them, never converted."""
    calls = []
    decode = stream.decode_chunk
    monkeypatch.setattr(stream, "decode_chunk", lambda *a: calls.append(1) or decode(*a))
    monkeypatch.setattr(stream, "SHARED", Shared(1 << 26))
    path = FIXTURES / "mp4" / "mp4_h264_irregular_groups.mp4"
    out, root = virtualized(path, tmp_path, chunk_frames=7)
    kind, frames, _ = decoded(path, 0)
    for k, p in enumerate("yuv"):
        assert np.array_equal(root["tracks/1"][p][16:23], np.stack([plane(f, k) for f in frames[16:23]])), p
    assert len(calls) == 1
    # the chunk of pictures 16 to 22 leads in from the keyframe at 9
    units, positions = chunk_units(out, "tracks/1/y/c/3/0/0", path.read_bytes())
    codec = StreamCodec(format="h264", setup=root["tracks/1/y"].metadata.codecs[0].setup, plane="y")
    given = list(stream._h264_frames(codec, units, positions))
    assert sorted(k for k, v in given if not v) == [k for k, p in enumerate(positions) if p < 0] != []


def test_the_codec_keeps_each_plane_once_within_its_limit():
    """The planes a decoding gave and no one asked for are kept until asked for, once each,
    the oldest dropped past the limit, and a chunk's planes larger than the limit not kept."""
    shared = Shared(300)
    runs = []

    def decode(n):
        def run():
            runs.append(n)
            return {p: np.zeros(n, np.uint8) for p in "yuv"}
        return run

    assert shared.take("a", "y", decode(100)).nbytes == 100
    assert set(shared.kept["a"]) == {"u", "v"}
    shared.take("a", "u", decode(100))
    shared.take("a", "v", decode(100))
    assert runs == [100] and "a" not in shared.kept
    shared.take("a", "v", decode(100))  # given once: decoded again
    assert runs == [100, 100]
    shared.take("b", "y", decode(100))  # past 300 bytes: a's planes dropped
    assert list(shared.kept) == ["b"]
    shared.take("c", "y", decode(200))  # 400 bytes: not kept
    assert list(shared.kept) == ["b"]


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
