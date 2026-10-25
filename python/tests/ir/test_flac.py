"""The FLAC IR: the Rust parser and its schema, the projection (spec/zarrify/flac.md) and its
virtual shards (spec/zarrify.md §2.1), the `refkv.stream` codec's FLAC decoder, and the mirror's
rebuild, on every synthetic file. The samples are checked against two readings independent of
refkv's frame search: STREAMINFO's MD5 of the stream's samples, and libsndfile's decoding of the
whole file (through soundfile; it reads 8, 16 and 24 bits, not 12, 20 or 32)."""

import base64
import hashlib
import json

import numpy as np
import pytest
import soundfile
import zarr
from ir_helpers import FIXTURES, core_setting
from ir_helpers import zarrified as shared_zarrified

import refkv
import refkv.zarr
from refkv import _core
from refkv._narrow import at, integer, mapping, ndarray, sequence, string
from refkv.pb import Concat, Range
from refkv.policy import crc32c
from refkv.vzip.archive import parse_central_directory, parse_tail
from refkv.zarr.codecs.stream import STREAM_FORMATS, StreamCodec, stream_units
from refkv.zarrify import _zarrify

FLACS = sorted((FIXTURES / "flac").glob("*.flac"))
ACCEPTED = [p for p in FLACS if "reject" not in p.name]
SOUNDFILE_BITS = (8, 16, 24)


def zarrified(path, tmp_path, shard_frames=None):
    """(the output, the hierarchy's root group, the IR), at most `shard_frames` frames a shard
    (None: the profile's 512); the archive rebuilds the file (ir_helpers.zarrified)."""
    with core_setting(_core._set_shard_frames, shard_frames):
        return shared_zarrified(path, tmp_path, "flac")


def md5(samples: np.ndarray, bits: int) -> str:
    """STREAMINFO's MD5 of `samples` (RFC 9639 §8.2
    <https://www.rfc-editor.org/rfc/rfc9639#section-8.2>): interleaved, each little-endian in
    whole bytes, sign-extended."""
    width = (bits + 7) // 8
    raw = samples.astype({1: "<i1", 2: "<i2", 3: "<i4", 4: "<i4"}[width]).tobytes()
    if width == 3:
        raw = b"".join(raw[k:k + 3] for k in range(0, len(raw), 4))
    return hashlib.md5(raw, usedforsecurity=False).hexdigest()


# (summary members, root metadata members) each file must have
CASES = {
    "flac_mono_16": ({"sampleRate": 16000, "channels": 1, "bitsPerSample": 16, "blockSize": 1024, "frames": 8,
                      "samples": 8000, "tags": []}, {"blocks": [{"type": "STREAMINFO", "size": 34}]}),
    "flac_stereo_16_seektable": ({"sampleRate": 44100, "channels": 2, "blockSize": 2048},
                                 {"seek_table": {"points": 2, "placeholders": 2},
                                  "vorbis_comment": {"vendor": "refkv fixtures",
                                                     "comments": ["TITLE=A tone", "ARTIST=refkv", "DATE=2026",
                                                                  "COMMENT=Zarrified, not copied"]}}),
    "flac_stereo_24": ({"bitsPerSample": 24, "channels": 2, "blockSize": 1152}, {}),
    "flac_mono_24": ({"bitsPerSample": 24, "sampleRate": 96000, "frames": 2}, {}),
    "flac_blocks": ({"pictures": 2},
                    {"applications": [{"id": "riff", "size": 14}],
                     "pictures": [{"type": 3, "media_type": "image/png", "description": "Front cover", "width": 4,
                                   "height": 2, "depth": 24, "colors": 0, "size": 88},
                                  {"type": 4, "media_type": "-->", "description": "", "width": 0, "height": 0,
                                   "depth": 0, "colors": 0, "size": 28}]}),
    "flac_id3v2": ({"tags": ["id3v2"]}, {"id3v2": [{"version": [4, 0], "flags": 0, "size": 55, "footer": False}]}),
    "flac_id3v1": ({"tags": ["id3v1"]}, {"id3v1": {"title": "A tone", "artist": "refkv", "album": "Fixtures",
                                                   "year": "2026", "comment": "FLAC", "track": 3, "genre": 12}}),
    "flac_many_frames": ({"frames": 1050, "blockSize": 16}, {}),
    "flac_single_frame": ({"frames": 1, "samples": 220, "blockSize": 220}, {}),
    "flac_bitwise_8": ({"bitsPerSample": 8}, {}),
    "flac_bitwise_12_side": ({"bitsPerSample": 12, "channels": 2}, {}),
    "flac_bitwise_20_mid": ({"bitsPerSample": 20}, {}),
    "flac_bitwise_32": ({"bitsPerSample": 32}, {}),
    "flac_bitwise_uncommon": ({"sampleRate": 11025, "blockSize": 100}, {}),
    "flac_bitwise_streaminfo_codes": ({"sampleRate": 22050, "bitsPerSample": 16}, {}),
}


def test_every_accepted_fixture_has_a_case():
    assert {p.stem for p in ACCEPTED} == set(CASES)


@pytest.mark.parametrize("shard_frames", [None, 3])
def test_zarrifies_the_synthetic_files(tmp_path, shard_frames):
    """Each file, in shards of the profile's 512 frames and of 3: the root's metadata and the
    summary; the array, sharded, one frame an inner chunk; its samples, whose MD5 is
    STREAMINFO's and which libsndfile's decoding gives; each shard's reference, a stream header
    and a frame's range per frame, then its index; the pictures' arrays, their bytes."""
    for name, (summary, members) in CASES.items():
        path = FIXTURES / "flac" / f"{name}.flac"
        out, root, _ = zarrified(path, tmp_path, shard_frames)
        assert {**out.summary, **summary} == out.summary, name
        meta = mapping(at(root.attrs, "refkv_zarrified", "flac"))
        for k, v in members.items():
            assert meta.get(k) == v, (name, k)
        keys = ["sample_rate", "channels", "bits_per_sample", "block_size", "frames", "samples", "duration",
                "streaminfo", "blocks"]
        keys += [k for k in ("seek_table", "vorbis_comment", "cuesheet", "pictures", "applications", "id3v2", "id3v1")
                 if k in meta]
        assert list(meta) == keys, name
        frames, block, channels = integer(meta["frames"]), integer(meta["block_size"]), integer(meta["channels"])
        bits = integer(meta["bits_per_sample"])
        per = min(shard_frames or 512, frames)
        assert (out.summary["shardFrames"], out.summary["shards"]) == (per, -(-frames // per)), name
        arr = root.get_array("samples")
        got = ndarray(arr[...])
        assert got.shape == (meta["samples"], channels), name
        assert got.dtype == {8: np.int8, 16: np.int16}.get(-(-bits // 8) * 8, np.int32), name
        assert arr.chunks == (block, channels), name
        assert arr.shards == (per * block, channels), name
        assert md5(got, bits) == at(meta, "streaminfo", "md5"), name
        if bits in SOUNDFILE_BITS:
            want, _ = soundfile.read(path, dtype="int32", always_2d=True)
            assert np.array_equal(got, np.right_shift(want, 32 - bits)), name
        data = path.read_bytes()
        for q in range(-(-frames // per)):
            parts = out.refs[f"samples/c/{q}/0"]
            index = parts[-1]
            assert isinstance(index, bytes)
            assert len(index) == 16 * per + 4
            for k in range(per):
                o, n = np.frombuffer(index[16 * k:16 * k + 16], "<u8").tolist()
                if q * per + k >= frames:
                    assert (o, n) == (2**64 - 1, 2**64 - 1)
                    continue
                head, frame = parts[2 * k], parts[2 * k + 1]
                assert isinstance(head, bytes)
                assert isinstance(frame, tuple)
                fo, fn = frame[-2:]
                units, positions = stream_units(head + data[fo:fo + fn])
                assert positions == [0]
                assert len(units[0]) == fn
                assert n == len(head) + fn
        for i, picture in enumerate(sequence(meta.get("pictures", []))):
            size = integer(mapping(picture)["size"])
            pic = ndarray(root.get_array(f"pictures/{i}")[...])
            assert pic.dtype == np.uint8
            assert pic.tobytes() in data
            assert len(pic) == size
        doc = json.loads(out.bytes_entries["samples/zarr.json"])
        assert doc["dimension_names"] == ["time", "channel"]
        sharding = doc["codecs"][0]["configuration"]
        index_codecs = [{"name": "bytes", "configuration": {"endian": "little"}}, {"name": "crc32c"}]
        assert sharding["index_codecs"] == index_codecs
        setup = base64.b64decode(sharding["codecs"][0]["configuration"]["setup"])
        assert setup[:8] == b"fLaC\x80\x00\x00\x22"
        assert len(setup) == 42
        assert set(root.attrs) == {"zarr_conventions", "refkv_zarrified"}


def test_a_read_of_a_few_samples_reads_their_frames(tmp_path):
    """Reading 7 samples of frame 500 of flac_many_frames.flac (frames of 16 samples) reads the
    archive's reference and that frame's bytes, one request; 20 frames, one request of their
    bytes: the shard index tells zarr's sharding codec where they are."""
    path = (FIXTURES / "flac" / "flac_many_frames.flac").resolve()
    out, _, ir = zarrified(path, tmp_path)
    out.write(str(tmp_path / "a.vzip"))
    # frames' (offset, length), from the IR's data elements
    cols = ir.columns()
    kind = np.frombuffer(cols["kind"], np.uint8)
    start, length = np.frombuffer(cols["start"], "<u8"), np.frombuffer(cols["len"], "<u8")
    frames = sorted(zip(start[kind == 2].tolist(), length[kind == 2].tolist(), strict=True))
    stats = refkv.Stats()
    root = zarr.open_group(refkv.zarr.open(str(tmp_path / "a.vzip"), stats=stats), mode="r", zarr_format=3)
    arr = root.get_array("samples")
    stats.reset()
    ndarray(arr[16 * 500 + 3:16 * 500 + 10])
    assert (stats.external_requests, stats.external_bytes) == (1, frames[500][1])
    stats.reset()
    ndarray(arr[16 * 600:16 * 620])
    assert stats.external_requests == 1
    assert stats.external_bytes == sum(n for _, n in frames[600:620])


def references(path) -> dict[str, list[Range]]:
    """The references of an archive, each its ranges."""
    data = path.read_bytes()
    d = parse_tail(data, len(data))
    entries = parse_central_directory(data[d.cd_offset:d.cd_offset + d.cd_size], trust_offsets=True)
    refs = {}
    for k, e in entries.items():
        if e.is_ref and e.ref is not None:
            refs[k] = list(e.ref.parts if isinstance(e.ref, Concat) else [e.ref])
    return refs


def test_checksums_keep_each_frame_its_own_range(tmp_path):
    """With checksums, the shards' ranges are those of the output without (a frame's range is
    never joined to the next frame's: a stream header lies between them, spec/zarrify.md §2.1
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#21-virtual-shards>), and each range
    of source 0 carries the CRC-32C of its bytes."""
    path = FIXTURES / "flac" / "flac_blocks.flac"
    _, plain = _zarrify(str(path), "https://data.test/a.flac")
    _, checked = _zarrify(str(path), "https://data.test/a.flac", checksums=True)
    assert plain.refs == checked.refs
    checked.write(str(tmp_path / "a.vzip"))
    data = path.read_bytes()
    refs = references(tmp_path / "a.vzip")
    for k in plain.refs:
        for r in refs[k]:
            if r.data is None:
                assert r.crc32c == crc32c(data[r.offset:r.offset + r.length]), k


def test_the_flac_decoder_rejects_a_setup_that_is_not_streaminfo():
    codec = StreamCodec(format="flac", setup=base64.b64encode(b"fLaC" + bytes(30)).decode())
    with pytest.raises(ValueError, match="a FLAC setup is fLaC and a STREAMINFO block"):
        list(STREAM_FORMATS["flac"](codec, [b""], [0]))


def test_the_flac_decoder_rejects_a_setup_without_its_marker():
    codec = StreamCodec(format="flac", setup=base64.b64encode(b"fLaX" + bytes(38)).decode())
    with pytest.raises(ValueError, match="a FLAC setup is fLaC and a STREAMINFO block"):
        list(STREAM_FORMATS["flac"](codec, [b""], [0]))


def test_the_root_names_the_profile(tmp_path):
    _, root, _ = zarrified(FIXTURES / "flac" / "flac_mono_16.flac", tmp_path)
    assert string(at(root.attrs, "refkv_zarrified", "profile")) == "flac"
