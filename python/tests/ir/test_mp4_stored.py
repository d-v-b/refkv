"""The stored arrays of the MP4 and QuickTime fixtures (spec/stored-array.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>), compared with
ffmpeg's demuxer and decoders (through PyAV), the reference readers: every video or audio track
ffmpeg lists has its descriptions, and of a video track

- each sample ffmpeg's demuxer gives is a row, in decode order, at the sample's place, with the
  demuxer's keyframe flag as its `sync`;
- the codec's `setup` is the configuration ffmpeg gives its decoder;
- a decoder given the rows' bytes, in order, makes one picture a row, whose planes have the
  descriptions' shapes and sample size and are the planes ffmpeg decodes of the demuxer's packets;
- a decoder started at a row whose `sync` is 1, with no state, makes the same pictures of that row
  and of those after it up to the next such row, but for its leading pictures, which are presented
  before it and may need the group before.

A Motion JPEG track's pictures are compared by their places and by the shape imagecodecs decodes
each to. A second test asserts which cases the fixtures reached."""

import struct
from collections import Counter
from functools import cache
from pathlib import Path

import av
import imagecodecs
import numpy as np
import pytest
from av.stream import Stream
from av.video.codeccontext import VideoCodecContext
from described import Described, described
from ir_helpers import FIXTURES

from refkv.ir.stored import Each, ElementPlace, Source, StoredArray, Value

FILES = sorted(p for p in (FIXTURES / "mp4").iterdir() if p.suffix in (".mp4", ".mov") and "reject" not in p.name)
# ffmpeg's name for the decoder of each codec the IR names
DECODER = {"h264": "h264", "hevc": "hevc", "av1": "libdav1d", "vp9": "vp9"}
# the sync units after the first a track's decoding is started again at, at most
RESTARTS = 4


def plane(frame: av.VideoFrame, k: int) -> np.ndarray:
    """Plane `k` of a decoded picture, its samples of 1 byte, or of 2 in the machine's order."""
    p = frame.planes[k]
    size = 2 if any(c.bits > 8 for c in frame.format.components) else 1
    rows = np.frombuffer(p, dtype=f"u{size}").reshape(p.height, p.line_size // size)
    return rows[:, :p.width]


def pictures(decoder: str, setup: bytes, units: list[bytes]) -> dict[int, av.VideoFrame]:
    """The pictures a decoder with no state makes of `units`, given in order, by each one's
    position among them."""
    context = av.CodecContext.create(decoder, "r")
    assert isinstance(context, VideoCodecContext)
    context.extradata = setup
    frames = []
    for j, unit in enumerate(units):
        packet = av.Packet(unit)
        packet.pts = j
        frames += context.decode(packet)
    frames += context.decode(None)
    assert len({f.pts for f in frames}) == len(frames), "a picture a unit"
    return {int(f.pts): f for f in frames}


def units_of(a: StoredArray) -> tuple[int, ...] | None:
    """The lengths of the array's units, where every axis is cut evenly."""
    if a.grid is None or not all(isinstance(x, Each) for x in a.grid.axes):
        return None
    return tuple(x.length for x in a.grid.axes if isinstance(x, Each))


def check_samples(d: Described, k: int, packets: list[av.Packet]) -> list[str]:
    """An array's rows against the demuxer's packets: a row a sample, in decode order, whole, at
    the sample's place."""
    a = d.stored.arrays[k]
    rows = d.rows.get(k, [])
    if a.shape is None or a.shape[0] != len(packets) or len(rows) != len(packets):
        return [f"shape {a.shape} and {len(rows)} rows, ffmpeg {len(packets)} samples"]
    if units_of(a) != (1, *a.shape[1:]) or a.grid is None or a.grid.edge != "short":
        return [f"grid {a.grid}, not a sample a unit"]
    said = []
    for j, (row, packet) in enumerate(zip(rows, packets, strict=True)):
        if row["coords"] != [j, *[0] * (len(a.shape) - 1)] or row["range"] is not None:
            said.append(f"row {j} at {row['coords']} of a range {row['range']}")
        elif d.parts(row) != [(None, packet.pos, packet.size)]:
            said.append(f"sample {j} at {d.parts(row)}, ffmpeg {packet.pos} of {packet.size}")
    return said


def check_video(d: Described, ks: list[int], stream: av.VideoStream, packets: list[av.Packet],
                seen: Counter) -> list[str]:
    """A video track's three planes against ffmpeg's demuxer and decoder."""
    arrays = [d.stored.arrays[k] for k in ks]
    said = [s for k in ks for s in check_samples(d, k, packets)]
    if said or len(ks) != 3:
        return said or [f"{len(ks)} arrays, not a picture's three planes"]
    chains = [d.stored.chains[a.chains[0]] for a in arrays]
    codec = chains[0][0]
    want = [(codec.name, {"setup", "plane"}, p) for p in range(3)]
    got = []
    for c in (c for chain in chains for c in chain):
        p = c.parameters.get("plane")
        got.append((c.name, set(c.parameters), p.value if isinstance(p, Value) else None))
    setup = codec.parameters.get("setup")
    if got != want or codec.name not in DECODER or not isinstance(setup, Source):
        return [f"chains {got}"]
    seen[f"tracks of {codec.name}"] += 1
    # the codec's configuration is the one ffmpeg gives its decoder, where it gives one
    theirs = bytes(stream.codec_context.extradata or b"")
    mine = d.place(setup.place)
    if theirs and mine != theirs:
        said.append("the setup is not the configuration ffmpeg gives its decoder")
    if any(setup.place not in a.declarations or not all(isinstance(p, ElementPlace) for p in a.declarations)
           for a in arrays):
        said.append("the declarations lack the codec's configuration")
    # each row says whether a decoder may start at it, as the demuxer's keyframe flag does
    column = f"p.{codec.name}.sync"
    sync = [r[column] for r in d.rows[ks[0]]]
    if any(a.unit_parameters != arrays[0].unit_parameters or [r[column] for r in d.rows[k]] != sync
           for k, a in zip(ks, arrays, strict=True)):
        said.append("the planes' units do not share their sync flags")
    names = [(u.chain, u.codec, u.name) for u in arrays[0].unit_parameters]
    if names != [(0, 0, "sync")] or sync != [int(p.is_keyframe) for p in packets]:
        said.append(f"sync {sync} of {names}, ffmpeg's keyframes {[int(p.is_keyframe) for p in packets]}")
        return said
    seen["pictures"] += len(packets)
    seen["sync units"] += sum(sync)
    # a decoder given the rows' bytes in order, against ffmpeg's of the demuxer's packets
    decoder = DECODER[codec.name]
    units = [d.unit(r) for r in d.rows[ks[0]]]
    whole = pictures(decoder, mine, units)
    reference = pictures(decoder, theirs, [bytes(p) for p in packets])
    if sorted(whole) != list(range(len(units))):
        said.append(f"{len(whole)} pictures of {len(units)} units")
        return said
    for p, a in enumerate(arrays):
        assert a.shape is not None
        assert a.item_bits is not None
        got_plane = plane(whole[0], p)
        if got_plane.shape != a.shape[1:] or got_plane.dtype.itemsize * 8 != a.item_bits:
            said.append(f"plane {p} of shape {a.shape[1:]} and {a.item_bits} bits decodes to {got_plane.shape} "
                        f"{got_plane.dtype}")
        if any(plane(whole[j], p).tobytes() != plane(reference[j], p).tobytes() for j in whole):
            said.append(f"plane {p} decodes to other values than ffmpeg's")
    luma, chroma = arrays[0].shape, arrays[1].shape
    assert luma is not None
    assert chroma is not None
    seen[f"tracks of chroma planes 1/{luma[2] // chroma[2]} by 1/{luma[1] // chroma[1]}"] += 1
    seen["tracks of 16-bit samples"] += arrays[0].item_bits == 16
    seen["pictures decoded"] += len(whole)
    # a decoder started at a sync unit: the group's pictures but its leading ones, which are
    # presented before the sync unit and may need the group before
    starts = [j for j, s in enumerate(sync) if s]
    times = [p.pts for p in packets if p.pts is not None]
    assert len(times) == len(packets), "ffmpeg gives every sample a presentation time"
    for at, start in enumerate(starts[1:1 + RESTARTS], 1):
        end = starts[at + 1] if at + 1 < len(starts) else len(units)
        group = pictures(decoder, mine, units[start:end])
        seen["groups decoded from their sync unit"] += 1
        for j in range(start, end):
            if times[j] < times[start]:
                seen["leading pictures"] += 1
                continue
            made = group.get(j - start)
            if made is None or any(plane(made, p).tobytes() != plane(whole[j], p).tobytes() for p in range(3)):
                said.append(f"picture {j}, decoded from the sync unit {start}, is not the stream's")
            seen["pictures decoded from a sync unit"] += 1
    return said


def check_jpeg(d: Described, k: int, packets: list[av.Packet], seen: Counter) -> list[str]:
    """A Motion JPEG track's pictures against the demuxer's packets and imagecodecs' decoding."""
    a = d.stored.arrays[k]
    said = check_samples(d, k, packets)
    if said:
        return said
    assert a.shape is not None
    if a.item_bits != 8 or a.unit_parameters or any(c.parameters for c in d.stored.chains[a.chains[0]]):
        said.append(f"samples of {a.item_bits} bits, unit parameters or codec parameters")
    seen["tracks of jpeg"] += 1
    seen["tracks of jpeg of one component"] += len(a.shape) == 3
    for j, row in enumerate(d.rows[k]):
        made = imagecodecs.jpeg_decode(d.unit(row))
        if made.shape != a.shape[1:] or made.dtype != np.uint8:
            said.append(f"picture {j} decodes to {made.shape} {made.dtype}, not {a.shape[1:]} of 8 bits")
        seen["jpeg pictures decoded"] += 1
    return said


def check_track(d: Described, ks: list[int], stream: Stream, packets: list[av.Packet],
                seen: Counter) -> list[str]:
    """One track's descriptions against ffmpeg's stream."""
    arrays = [d.stored.arrays[k] for k in ks]
    said = []
    if [a.index for a in arrays] != list(range(len(arrays))):
        said.append(f"indexes {[a.index for a in arrays]}")
    if any(a.fill != "undefined" or a.axes is not None or a.pad_from is not None for a in arrays):
        said.append("a fill value, axes or a padding")
    names = [[c.name for chain in a.chains for c in d.stored.chains[chain]] for a in arrays]
    if names == [[]]:
        # a track the IR does not model: what declares it, and no more
        (a,) = arrays
        seen[f"tracks of no grid, {stream.type}"] += 1
        if (a.shape, a.item_bits, a.grid, a.unit_parameters) != (None, None, None, ()) or d.rows.get(ks[0]):
            said.append("a shape, a sample size, a grid or rows for a track the IR does not model")
        return said
    if not isinstance(stream, av.VideoStream):
        return [*said, f"chains {names} of a track that is no video"]
    # the sample entry's fields, which declare the pictures' size
    fields = d.place(arrays[0].declarations[0])
    luma = arrays[0].shape
    if luma is None or struct.unpack_from(">HH", fields, 24) != (luma[2], luma[1]):
        said.append(f"shape {luma}, not the sample entry's width and height")
    if names == [["jpeg"]]:
        return said + check_jpeg(d, ks[0], packets, seen)
    return said + check_video(d, ks, stream, packets, seen)


@cache
def compared(path: Path) -> tuple[tuple[str, ...], Counter]:
    """What differs between the descriptions of the file at `path` and ffmpeg, and what the
    comparison saw."""
    d = described(path)
    said: list[str] = []
    seen: Counter = Counter()
    if d.stored.undescribed:
        said.append("descriptions dropped for room")
    # the trak boxes, in order, as ffmpeg numbers its streams
    moov = d.ir.at("moov")
    assert moov is not None
    traks = [c for c in d.ir.children(moov) if d.ir.name(c).split("~")[0] == "trak"]
    by_track: dict[int, list[int]] = {}
    for k, a in enumerate(d.stored.arrays):
        by_track.setdefault(traks.index(a.element), []).append(k)
    with av.open(str(path)) as container:
        streams = list(container.streams)
        if len(streams) != len(traks):
            return (f"{len(traks)} trak boxes, ffmpeg {len(streams)} streams",), seen
        for t, stream in enumerate(streams):
            if stream.type not in ("video", "audio"):
                if t in by_track:
                    said.append(f"track {t}: descriptions of a {stream.type} track")
                continue
            if t not in by_track:
                said.append(f"track {t}: no description of a {stream.type} track")
                continue
            seen["tracks"] += 1
            container.seek(0)
            packets = [p for p in container.demux(stream) if p.size]
            said += [f"track {t}: {s}" for s in check_track(d, by_track[t], stream, packets, seen)]
    return tuple(said), seen


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_mp4_tracks_are_described_as_ffmpeg_reads_them(path):
    """Every video and audio track of the file is described, a video track's samples as ffmpeg's
    demuxer gives them, decoding to the pictures ffmpeg's decoder makes."""
    said, seen = compared(path)
    assert said == ()
    assert seen["tracks"] >= 1


def test_the_fixtures_hold_every_case_the_comparison_reads():
    """The fixtures reach each case of the comparison, so that none of it passes for want of a
    file. The video tracks of no grid are Motion JPEG the IR does not model: streams with no
    markers (Motion JPEG B), with no frame header in the first sample, of 12 bits, and of a
    picture's fields."""
    seen: Counter = Counter()
    for path in FILES:
        seen += compared(path)[1]
    assert {k: v for k, v in seen.items() if v} == {
        "tracks": 118,
        "tracks of h264": 35,
        "tracks of hevc": 14,
        "tracks of av1": 2,
        "tracks of vp9": 5,
        "tracks of jpeg": 8,
        "tracks of jpeg of one component": 1,
        "tracks of no grid, audio": 50,
        "tracks of no grid, video": 4,
        "tracks of chroma planes 1/2 by 1/2": 48,
        "tracks of chroma planes 1/2 by 1/1": 6,
        "tracks of chroma planes 1/1 by 1/1": 2,
        "tracks of 16-bit samples": 9,
        "pictures": 2265,
        "sync units": 211,
        "pictures decoded": 2265,
        "groups decoded from their sync unit": 138,
        "pictures decoded from a sync unit": 1362,
        "leading pictures": 64,
        "jpeg pictures decoded": 80,
    }
