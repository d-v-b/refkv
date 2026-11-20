"""The stored arrays of the H5J fixtures (spec/stored-array.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>), compared with the
readers of what an H5J file holds: h5py for its datasets, and ffmpeg's demuxer and HEVC decoder,
through PyAV, for each channel's MP4 file.

- Each dataset of bytes is described as the HDF5 parser describes it: an array of one dimension of
  8-bit elements, the dataset's length, whose one row's bytes are h5py's, a channel's whole MP4 file
  among them.
- Each channel the profile zarrifies also has its MP4 file's track described as the MP4 parser
  describes one, its elements renumbered into the H5J file's IR: one array, the luma plane of its
  4:0:0 pictures, in an `hevc` chain whose `setup` is the configuration ffmpeg gives its decoder; a
  row a sample, in decode order, at the sample's place in the H5J file (the dataset's address plus
  the sample's offset in the MP4 file), flagged a sync sample as ffmpeg's demuxer flags it; and the
  rows' bytes, decoded in order, are ffmpeg's pictures of the demuxer's packets.
- A channel the profile records has no track description: its MP4 file, if it is one, was not
  joined to the IR."""

import io
from pathlib import Path

import av
import h5py
import numpy as np
import pytest
from av.video.codeccontext import VideoCodecContext
from described import Described, described
from ir_helpers import FIXTURES

from refkv._narrow import at, sequence
from refkv.ir.stored import Source, Value

FILES = sorted(p for p in (FIXTURES / "h5j").iterdir() if "reject" not in p.name)


def pictures(setup: bytes, units: list[bytes]) -> list[np.ndarray]:
    """The luma planes ffmpeg's HEVC decoder, set up with `setup`, makes of `units` given in order,
    in the order it outputs them."""
    context = av.CodecContext.create("hevc", "r")
    assert isinstance(context, VideoCodecContext)
    context.extradata = setup
    frames = []
    for j, unit in enumerate(units):
        packet = av.Packet(unit)
        packet.pts = j
        frames += context.decode(packet)
    frames += context.decode(None)
    return [np.frombuffer(f.planes[0], np.uint8).copy() for f in frames]


def byte_arrays(d: Described) -> dict[bytes, int]:
    """The descriptions of one dimension of 8-bit elements with one row, by that row's bytes."""
    out = {}
    for k, a in enumerate(d.stored.arrays):
        rows = d.rows.get(k, [])
        if a.item_bits == 8 and a.shape is not None and len(a.shape) == 1 and len(rows) == 1:
            out[d.unit(rows[0])] = k
    return out


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_h5j_channels_are_described_as_h5py_and_ffmpeg_read_them(path: Path):
    """Every dataset of bytes, and every zarrified channel's track, is described as h5py and
    ffmpeg read them."""
    d = described(path)
    assert not d.stored.undescribed
    by_bytes = byte_arrays(d)
    names = [[c.name for chain in a.chains for c in d.stored.chains[chain]] for a in d.stored.arrays]
    hevc = [k for k, n in enumerate(names) if n == ["hevc"]]
    presented = {str(at(c, "name")) for c in sequence(d.out.summary["channels"])}
    assert presented
    tracks = []
    with h5py.File(path, "r") as f:
        for name, ds in f["Channels"].items():
            if ds.dtype != np.uint8 or ds.chunks is not None:
                continue
            data = ds[()].tobytes()
            # the dataset, as the HDF5 parser describes it
            k = by_bytes.get(data)
            assert k is not None, f"{name}: no description of the dataset's bytes"
            assert d.stored.arrays[k].shape == (len(data),)
            if name not in presented:
                continue
            # its MP4 file's track: the luma plane of its pictures, a row a sample
            offset = ds.id.get_offset()
            c = av.open(io.BytesIO(data), mode="r")
            stream = c.streams.video[0]
            packets = [p for p in c.demux(stream) if p.size]
            setup = bytes(stream.codec_context.extradata or b"")
            first = [(None, offset + packets[0].pos, packets[0].size)]
            mine = [k for k in hevc if d.parts(d.rows[k][0]) == first]
            assert len(mine) == 1, f"{name}: {len(mine)} track descriptions"
            k = mine[0]
            tracks.append(k)
            a = d.stored.arrays[k]
            rows = d.rows[k]
            assert a.index == 0
            assert a.shape == (len(packets), stream.codec_context.height, stream.codec_context.width)
            assert a.item_bits == (16 if (stream.codec_context.pix_fmt or "").startswith("gray1") else 8)
            (codec,) = d.stored.chains[a.chains[0]]
            assert isinstance(codec.parameters["setup"], Source)
            assert d.place(codec.parameters["setup"].place) == setup
            assert codec.parameters["plane"] == Value(0)
            assert [d.parts(r) for r in rows] == [[(None, offset + p.pos, p.size)] for p in packets]
            assert [r["p.hevc.sync"] for r in rows] == [int(p.is_keyframe) for p in packets]
            assert [r["coords"] for r in rows] == [[j, 0, 0] for j in range(len(packets))]
            whole = pictures(setup, [d.unit(r) for r in rows])
            reference = pictures(setup, [bytes(p) for p in packets])
            assert len(whole) == len(packets)
            assert all(np.array_equal(x, y) for x, y in zip(whole, reference, strict=True))
    # no track is described but a zarrified channel's
    assert sorted(tracks) == sorted(hevc)
