"""The MP3 IR: the Rust parser and its schema, the projection (spec/virtualize/mp3.md) and the
`mp3` codec against two decoders on every synthetic file, the mirror's rebuild, the inputs the
profile rejects, and the lead-in of each chunk. MP3 has no frozen Python reference: miniaudio
(the codec's own decoder, run over the whole stream) and ffmpeg (an independent decoder, run by
the fixture generator and stored) are the readers it is compared with."""

import io
import json

import miniaudio
import numpy as np
import pytest
import zarr

import refkv.zarr
from ir_helpers import FIXTURES
from refkv import _core
from refkv.ir import virtualize as via_ir
from refkv.ir.cmirror import rebuild_from_archive
from refkv.virtualize import Rejected

MP3S = sorted((FIXTURES / "mp3").glob("*.mp3"))
ACCEPTED = [p for p in MP3S if not p.name.startswith("mp3_reject")]
EXPECTED = FIXTURES / "mp3" / "expected"
# ffmpeg's decoding and miniaudio's differ by rounding (about half the samples of a joint
# stereo stream by one unit): the most a sample may differ, and the most the mean may
FFMPEG_MAX, FFMPEG_MEAN = 8, 1.0


def virtualized(path, tmp_path, chunk_frames=None):
    """(the output, the hierarchy's root group, read in memory from the file, the IR), with
    `chunk_frames` frames a chunk (None: the profile's 256); the archive rebuilds the file."""
    data = path.read_bytes()
    _core._set_chunk_frames(chunk_frames)
    try:
        fmt, out, ir = via_ir(str(path))
    finally:
        _core._set_chunk_frames(None)
    assert fmt == "mp3"
    ir.check()
    out.write(str(tmp_path / "a.vzip"))
    buf = io.BytesIO()
    rebuild_from_archive(str(tmp_path / "a.vzip"), lambda o, n: data[o:o + n], buf.write)
    assert buf.getvalue() == data
    root = zarr.open_group(refkv.zarr.store(out.to_store()), mode="r", zarr_format=3)
    return out, root, ir


def chunk_parts(out, q):
    """(keep, [(offset, length), ...]) of chunk q's reference: its literal and its ranges of
    source 0 (spec/virtualize/mp3.md §8.2
    <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp3.md#82-chunks>)."""
    lit, *ranges = out.refs[f"samples/c/{q}/0"]
    assert isinstance(lit, bytes) and len(lit) == 4
    assert all(len(r) == 2 for r in ranges)
    return int.from_bytes(lit, "little"), ranges


def frames_of(ir):
    """(offset, length) of each audio frame, in file order: the IR's data elements
    (spec/virtualize/mp3.md §5.3
    <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp3.md#53-elements>)."""
    cols = ir.columns()
    kind = np.frombuffer(cols["kind"], np.uint8)
    start, length = np.frombuffer(cols["start"], "<u8"), np.frombuffer(cols["len"], "<u8")
    data = np.flatnonzero(kind == 2)
    return sorted(zip(start[data].tolist(), length[data].tolist()))


def audio_frames(path, ir):
    """The audio frames' bytes, one after the other (the bytes between them left out)."""
    data = path.read_bytes()
    return b"".join(data[o:o + n] for o, n in frames_of(ir))


def sequential(audio: bytes, channels: int) -> np.ndarray:
    """miniaudio's decoding of the audio frames in one pass, int16 [samples, channels]."""
    info = miniaudio.mp3_get_info(audio)
    assert info.nchannels == channels
    d = miniaudio.decode(audio, miniaudio.SampleFormat.SIGNED16, nchannels=channels, sample_rate=info.sample_rate)
    return np.frombuffer(d.samples, dtype=np.int16).reshape(-1, channels)


@pytest.mark.parametrize("path", ACCEPTED, ids=lambda p: p.name)
def test_chunks_decode_as_the_stream_does(path, tmp_path):
    """With 4 frames a chunk (so that every file has several chunks, each with its lead-in),
    the array read through the mp3 codec is miniaudio's decoding of the audio frames in one
    pass, sample for sample (spec/virtualize/mp3.md §4.1
    <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp3.md#41-the-array>, §8.2
    <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp3.md#82-chunks>); the root's
    metadata and the summary agree with the array; the file is rebuilt from the mirror."""
    out, root, ir = virtualized(path, tmp_path, chunk_frames=4)
    meta = root.attrs["refkv_virtualized"]["mp3"]
    arr = root["samples"]
    got = arr[...]
    frames = frames_of(ir)
    assert len(frames) == meta["frames"]
    assert got.dtype == np.int16 and got.shape == (meta["samples"], meta["channels"])
    assert meta["samples"] == meta["frames"] * meta["samples_per_frame"]
    assert arr.chunks == (4 * meta["samples_per_frame"], meta["channels"])
    chunks = -(-meta["frames"] // 4)
    assert out.summary["chunks"] == chunks and out.summary["chunkFrames"] == 4
    assert set(root.attrs) == {"zarr_conventions", "refkv_virtualized"}
    want = sequential(audio_frames(path, ir), meta["channels"])
    assert want.shape == got.shape
    assert np.array_equal(got, want)
    # each chunk keeps its own frames' samples; its ranges are the frames from its lead-in's
    # first to its own last, runs of consecutive frames merged, and the first has no lead-in
    for q in range(chunks):
        a, b = 4 * q, min(4 * q + 4, len(frames))
        keep, ranges = chunk_parts(out, q)
        assert keep == (b - a) * meta["samples_per_frame"]
        l = next(j for j, (o, _) in enumerate(frames) if o == ranges[0][0])
        assert (l == 0) if q == 0 else (l < a)
        assert ranges[-1][0] + ranges[-1][1] == frames[b - 1][0] + frames[b - 1][1]
        assert sum(n for _, n in ranges) == sum(n for _, n in frames[l:b])
        assert all(ranges[k][0] > ranges[k - 1][0] + ranges[k - 1][1] for k in range(1, len(ranges)))
    assert out.data == {}  # no data sources
    doc = json.loads(out.bytes_entries["samples/zarr.json"])
    assert doc["codecs"] == [{"name": "mp3"}] and doc["dimension_names"] == ["time", "channel"]
    assert doc["fill_value"] == 0 and doc["data_type"] == "int16"


@pytest.mark.parametrize("path", [p for p in ACCEPTED if (EXPECTED / f"{p.stem}.npy").exists()], ids=lambda p: p.name)
@pytest.mark.parametrize("chunk_frames", [None, 4])
def test_matches_ffmpeg(path, chunk_frames, tmp_path):
    """The array is ffmpeg's decoding of the audio frames (fixtures/mp3/expected, written by
    the generator) within a few units per sample, whole (256 frames a chunk) and in chunks of
    4 frames with their lead-ins."""
    _, root, _ = virtualized(path, tmp_path, chunk_frames)
    got = root["samples"][...].astype(np.int32)
    want = np.load(EXPECTED / f"{path.stem}.npy").astype(np.int32)
    assert got.shape == want.shape
    diff = np.abs(got - want)
    assert diff.max() <= FFMPEG_MAX and diff.mean() <= FFMPEG_MEAN, (diff.max(), diff.mean())


def test_a_long_stream_has_chunks_of_256_frames(tmp_path):
    """mp3_mpeg25_long.mp3 (333 frames of 576 samples) has two chunks by default; the second's
    lead-in starts before frame 256, and both decode as the stream does."""
    path = FIXTURES / "mp3" / "mp3_mpeg25_long.mp3"
    out, root, ir = virtualized(path, tmp_path)
    meta = root.attrs["refkv_virtualized"]["mp3"]
    assert meta["frames"] > 256 and out.summary["chunks"] == 2
    arr = root["samples"]
    assert arr.chunks == (256 * 576, 1)
    keep1, ranges1 = chunk_parts(out, 1)
    assert keep1 == (meta["frames"] - 256) * 576
    # frames of 72 bytes hold 59 bytes of main data (72 - 4 - 9): the lead-in is frames 254
    # and 255, and before them the 5 frames (295 bytes) that hold the 255-byte reservoir
    frames = frames_of(ir)
    assert ranges1 == [(frames[249][0], frames[-1][0] + frames[-1][1] - frames[249][0])]
    got = arr[...]
    assert np.array_equal(got, sequential(audio_frames(path, ir), 1))


# (summary members, root metadata members) each file must have
CASES = {
    "mp3_mpeg1_stereo_cbr": ({"version": "MPEG-1", "sampleRate": 44100, "channels": 2, "bitrate": 128, "info": None, "tags": [],
                              "strayBytes": 0},
                             {"samples_per_frame": 1152, "channel_mode": "joint_stereo", "crc_frames": 0, "bitrate_mode": "constant"}),
    "mp3_mpeg1_mono_vbr": ({"version": "MPEG-1", "channels": 1, "bitrate": "variable", "info": "Xing"},
                           {"bitrate_mode": "variable", "bitrate_kbps": None}),
    "mp3_mpeg2_stereo_crc": ({"version": "MPEG-2", "sampleRate": 22050, "channels": 2}, {"samples_per_frame": 576}),
    "mp3_mpeg2_mono_nores": ({"version": "MPEG-2", "sampleRate": 16000, "bitrate": 32}, {}),
    "mp3_mpeg25_mono_8k": ({"version": "MPEG-2.5", "sampleRate": 8000, "channels": 1, "bitrate": 16}, {"samples_per_frame": 576}),
    "mp3_mpeg25_long": ({"version": "MPEG-2.5", "sampleRate": 8000, "bitrate": 8, "chunks": 2}, {}),
    "mp3_mpeg2_mono_16k": ({"frames": 22, "samples": 12672, "duration": 0.792, "tags": []}, {"stray_bytes": 0}),
    "mp3_tags_id3v2_id3v1": ({"tags": ["id3v2", "id3v1"]},
                             {"id3v1": {"title": "A title", "artist": "An artist", "album": "An album", "year": "2021",
                                        "comment": "Some comment", "track": 7, "genre": 0}}),
    "mp3_tags_id3v1_only": ({"tags": ["id3v1"]}, {}),
    "mp3_tags_ape": ({"tags": ["ape"]}, {"ape": {"version": 2000, "items": 2, "size": 32 + 32 + 2 * 8 + 6 + 9 + 7 + 10}}),
    "mp3_tags_ape_id3v1": ({"tags": ["id3v1", "ape"]}, {"ape": {"version": 2000, "items": 1, "size": 32 + 8 + 6 + 9}}),
    "mp3_info_lavc": ({"tags": ["id3v2"], "info": "Info"}, {}),
    "mp3_abr_xing": ({"info": "Xing", "bitrate": "variable"}, {}),
    "mp3_id3v2_footer": ({"tags": ["id3v2"]}, {"id3v2": [{"version": [4, 0], "flags": 16, "size": 40, "footer": True}]}),
    "mp3_id3v2_twice": ({}, {"id3v2": [{"version": [3, 0], "flags": 0, "size": 16, "footer": False},
                                       {"version": [4, 0], "flags": 0, "size": 24, "footer": False}]}),
    "mp3_id3v2_padding": ({"strayBytes": 64}, {}),
    "mp3_single_frame": ({"frames": 1, "chunks": 1, "samples": 576}, {}),
    "mp3_crc_mixed": ({}, {"crc_frames": 5}),
    "mp3_junk_between_frames": ({"strayBytes": 35 + 100}, {}),
    "mp3_junk_false_sync": ({"strayBytes": 26}, {}),
    "mp3_truncated_last_frame": ({"strayBytes": 72}, {}),
    "mp3_id3v1_after_truncated": ({"strayBytes": 72, "tags": ["id3v1"]}, {}),
}


def test_every_accepted_fixture_has_a_case():
    assert {p.stem for p in ACCEPTED} == set(CASES)


def test_virtualizes_the_synthetic_files(tmp_path):
    for name, (summary, members) in CASES.items():
        out, root, _ = virtualized(FIXTURES / "mp3" / f"{name}.mp3", tmp_path)
        assert {**out.summary, **summary} == out.summary, name
        meta = root.attrs["refkv_virtualized"]["mp3"]
        for k, v in members.items():
            assert meta.get(k) == v, (name, k)
        keys = ["version", "layer", "sample_rate", "channels", "channel_mode", "samples_per_frame", "frames", "samples",
                "duration", "bitrate_mode", "bitrate_kbps", "crc_frames"]
        keys += [k for k in ("info", "id3v2", "id3v1", "ape") if k in meta] + ["stray_bytes"]
        assert list(meta) == keys, name
        assert set(root.attrs) == {"zarr_conventions", "refkv_virtualized"}, name


def test_the_info_frame_is_read_and_not_audio(tmp_path):
    """A LAME Xing frame and an ffmpeg Info frame: their fields are the root's `info`, and
    the first audio frame is the frame after them (the first chunk's range starts there)."""
    out, root, _ = virtualized(FIXTURES / "mp3" / "mp3_abr_xing.mp3", tmp_path)
    info = root.attrs["refkv_virtualized"]["mp3"]["info"]
    assert info["tag"] == "Xing" and info["encoder"].startswith("LAME") and info["frames"] == out.summary["frames"]
    assert isinstance(info["delay"], int) and isinstance(info["padding"], int)
    _, ranges = chunk_parts(out, 0)
    assert ranges[0][0] > 0 and out.summary["strayBytes"] == 0
    out, root, _ = virtualized(FIXTURES / "mp3" / "mp3_info_lavc.mp3", tmp_path)
    info = root.attrs["refkv_virtualized"]["mp3"]["info"]
    assert info["tag"] == "Info" and info["encoder"].startswith("Lavc") and info["frames"] == out.summary["frames"]


@pytest.mark.parametrize("name,message", [
    ("mp3_reject_layer2", "MPEG-2 Layer II (the frame at 0) is not supported: only Layer III is"),
    ("mp3_reject_layer1", "MPEG-1 Layer I (the frame at 0) is not supported: only Layer III is"),
    ("mp3_reject_free_format", "free-format bitrate (the frame header at 0) is not supported"),
    ("mp3_reject_sample_rate_change", "changes the sample rate from 16000 to 22050"),
    ("mp3_reject_channels_change", "changes the channel count from 1 to 2"),
    ("mp3_reject_version_change", "changes the version from MPEG-2 to MPEG-1"),
    ("mp3_reject_layer_change", "changes the layer from III to II"),
    ("mp3_reject_no_frames", "no MPEG audio frames"),
    ("mp3_reject_only_id3v1", "not a file of any supported format"),
    ("mp3_reject_id3v2_outside", "the 100010-byte ID3v2 tag at 0 is outside the 60-byte file"),
    ("mp3_reject_id3v2_syncsafe", "the ID3v2 tag at 0 has a size byte that is not syncsafe (128)"),
    ("mp3_reject_short", "file too short for an MPEG audio frame header"),
])
def test_rejects(name, message):
    """Each rejected input, with its message (a parameter change names the frame's offset
    before the message checked here; a file no row of spec/virtualize.md §1.2
    <https://github.com/d-v-b/refkv/blob/main/spec/virtualize.md#12-input> matches is rejected by
    the host before the parser)."""
    path = FIXTURES / "mp3" / f"{name}.mp3"
    with pytest.raises(Rejected) as e:
        via_ir(str(path), f"https://data.test/{name}.mp3")
    assert message in str(e.value), str(e.value)
    if "changes the" in message:
        assert str(e.value).startswith("the frame at ")


def test_every_reject_fixture_has_a_test():
    assert {p.stem for p in MP3S if p.name.startswith("mp3_reject")} == {
        name for name, _ in test_rejects.pytestmark[0].args[1]}
