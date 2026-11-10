"""The MP4 profile's video arrays against the stored arrays' descriptions (spec/stored-array.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): what the profile
writes of a video track's storage, read off the track's descriptions by the profile's rules
(spec/zarrify/mp4.md §4.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp4.md#41-video-tracks>
and §8.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp4.md#83-chunks>), is what the
profile wrote.

A description holds a track's samples in decode order, each with whether a decoder may start at it.
The profile presents the pictures in presentation order, in chunks cut at the sync samples, each a
stream of the samples a decoder needs for its pictures. So the test takes one thing from outside
the descriptions, each sample's presentation time, which ffmpeg's demuxer gives, and derives the
rest: the planes' documents, the chunks' lengths, and each chunk's bytes, its header of the
samples' sizes and positions and the ranges of the samples from the sync sample it decodes from.
A Motion JPEG track's chunks are its samples, a picture each, in presentation order.

The profile is run with few pictures a chunk, so that a track is several chunks and some start
after a sync sample's leading pictures, and with its own 256."""

import base64
import json
from collections import Counter
from functools import cache
from pathlib import Path

import av
import numpy as np
import pytest
from described import Described, described
from ir_helpers import FIXTURES, core_setting

from refkv import _core
from refkv._narrow import JSON, at, integer, mapping, sequence, string
from refkv.ir.stored import Source
from refkv.zarrify import _zarrify
from refkv.zarrify.common import Output

FILES = sorted(p for p in (FIXTURES / "mp4").iterdir() if p.suffix in (".mp4", ".mov") and "reject" not in p.name)
VIDEO = ("h264", "hevc", "av1", "vp9")
# the most pictures a chunk holds, and the most bytes of its luma plane
# (spec/zarrify/mp4.md §4.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp4.md#41-video-tracks>)
MOST, LUMA = 256, 2**25

type Part = tuple[int, int] | bytes
"""a part of a reference: (offset, length) of the file, or a literal"""


def varint(n: int) -> bytes:
    """An unsigned integer as protobuf encodes it."""
    out = bytearray()
    while n >= 0x80:
        out.append(n & 0x7F | 0x80)
        n >>= 7
    return bytes([*out, n])


def stream_chunk(sizes: list[int], positions: list[int]) -> bytes:
    """The header of a `refkv.stream` chunk (spec/conventions.md §10
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#10-coded-streams>): the frames'
    count, then each one's size and its position less the one before's, the first's less -1."""
    out, before = varint(len(sizes)), -1
    for size, position in zip(sizes, positions, strict=True):
        step = position - before
        out += varint(size) + varint(2 * step if step >= 0 else -2 * step - 1)
        before = position
    return out


def joined(ranges: list[tuple[int, int]]) -> list[Part]:
    """`ranges` with each that starts where the one before it ends made one with it."""
    out: list[tuple[int, int]] = []
    for offset, length in ranges:
        if out and sum(out[-1]) == offset:
            out[-1] = (out[-1][0], out[-1][1] + length)
        else:
            out.append((offset, length))
    return list(out)


def lengths(grid: JSON, n: int) -> list[int]:
    """The chunks' lengths along an array's first axis, of `n` elements."""
    if at(grid, "name") == "regular":
        length = integer(at(grid, "configuration", "chunk_shape", 0))
        return [min(length, n - a) for a in range(0, n, length)]
    out: list[int] = []
    for x in sequence(at(grid, "configuration", "chunk_shapes", 0)):
        out += [integer(x)] if isinstance(x, int) else [integer(at(x, 0))] * integer(at(x, 1))
    return out


def cut(order: list[int], sync: list[int], most: int) -> list[int]:
    """The lengths of a video track's chunks: at most `most` pictures, cut at the greatest
    presentation index of a sync sample within that, the pictures presented in `order`."""
    starts = sorted(p for p, j in enumerate(order) if sync[j])
    n, a, out = len(order), 0, []
    while a < n:
        within = [p for p in starts if a < p <= a + most]
        b = n if n - a <= most else within[-1] if within else a + most
        out.append(b - a)
        a = b
    return out


def check_video(d: Described, ks: list[int], out: Output, path: str, times: list[int], frames: int,
                seen: Counter) -> list[str]:
    """A video track's three planes, as the profile wrote them at `path`, against its descriptions."""
    arrays = [d.stored.arrays[k] for k in ks]
    rows = d.rows[ks[0]]
    codec = d.stored.chains[arrays[0].chains[0]][0]
    setup = codec.parameters["setup"]
    assert isinstance(setup, Source)
    sync = [r[f"p.{codec.name}.sync"] for r in rows]
    n = len(rows)
    # the samples by presentation, and each one's presentation index
    order = sorted(range(n), key=lambda j: times[j])
    index = {j: p for p, j in enumerate(order)}
    luma = arrays[0].shape
    assert luma is not None
    assert arrays[0].item_bits is not None
    most = min(frames, max(1, LUMA // (luma[1] * luma[2] * (arrays[0].item_bits // 8))))
    want_lengths = cut(order, sync, most)
    starts = [j for j, s in enumerate(sync) if s]
    said = []
    first: list | None = None
    for plane, a in zip("yuv", arrays, strict=True):
        assert a.shape is not None
        assert a.item_bits is not None
        doc = mapping(json.loads(out.bytes_entries[f"{path}/{plane}/zarr.json"]), plane)
        configuration = {"format": codec.name, "setup": base64.b64encode(d.place(setup.place)).decode(), "plane": plane}
        whole = [integer(x) for x in sequence(doc["shape"])]
        if whole != list(a.shape) or np.dtype(string(doc["data_type"])).itemsize * 8 != a.item_bits:
            said.append(f"{plane}: shape {a.shape} of {a.item_bits} bits; the profile's {whole} {doc['data_type']}")
        if doc["codecs"] != [{"name": "refkv.stream", "configuration": configuration}]:
            said.append(f"{plane}: the profile's codecs are not the stream of {codec.name}, its setup and its plane")
        if lengths(doc["chunk_grid"], n) != want_lengths:
            said.append(f"{plane}: chunks of {lengths(doc['chunk_grid'], n)}, not {want_lengths}")
            continue
        a_q = 0
        for q, length in enumerate(want_lengths):
            # the chunk's samples, and the sync sample it decodes from: the last at or before the
            # first of them in decode order, or the one before while one of them is its leading picture
            held = [order[p] for p in range(a_q, a_q + length)]
            s = max(i for i, j in enumerate(starts) if j <= min(held))
            leading = s > 0 and any(j > starts[s] and times[j] < times[starts[s]] for j in held)
            decoded = range(starts[s - 1] if leading else starts[s], max(held) + 1)
            places = [d.parts(rows[j])[0][1:] for j in decoded]
            positions = [index[j] - a_q if a_q <= index[j] < a_q + length else -1 for j in decoded]
            want: list[Part] = [stream_chunk([n for _, n in places], positions), *joined(places)]
            got = out.refs[f"{path}/{plane}/c/{q}/0/0"]
            if list(got) != want:
                said.append(f"{plane} chunk {q} is {list(got)[:3]}, not {want[:3]}")
            if plane == "y":
                seen["chunks"] += 1
                seen["chunks decoded from the sync sample before"] += leading
                seen["chunks that decode samples they do not hold"] += -1 in positions
            a_q += length
        # the three planes of a chunk are the same stream
        chunks = [out.refs[f"{path}/{plane}/c/{q}/0/0"] for q in range(len(want_lengths))]
        first = chunks if first is None else first
        if chunks != first:
            said.append(f"{plane}'s chunks are not y's")
    seen["video tracks"] += 1
    seen["video tracks of several chunks"] += len(want_lengths) > 1
    return said


def check_jpeg(d: Described, k: int, out: Output, path: str, times: list[int], seen: Counter) -> list[str]:
    """A Motion JPEG track's pictures, as the profile wrote them at `path`, against its description."""
    a = d.stored.arrays[k]
    rows = d.rows[k]
    assert a.shape is not None
    doc = mapping(json.loads(out.bytes_entries[f"{path}/pictures/zarr.json"]), path)
    said = []
    if [integer(x) for x in sequence(doc["shape"])] != list(a.shape) or doc["data_type"] != "uint8":
        said.append(f"shape {a.shape}; the profile's {doc['shape']} {doc['data_type']}")
    if at(doc["chunk_grid"], "configuration", "chunk_shape") != [1, *a.shape[1:]] or doc["codecs"] != [
            {"name": "imagecodecs_jpeg"}]:
        said.append(f"the profile's chunks {doc['chunk_grid']} and codecs {doc['codecs']}, not a picture's")
    # a picture a chunk, in presentation order
    for q, j in enumerate(sorted(range(len(rows)), key=lambda j: times[j])):
        got = out.refs[f"{path}/pictures/c/{q}/{'/'.join('0' * (len(a.shape) - 1))}"]
        if list(got) != [d.parts(rows[j])[0][1:]]:
            said.append(f"picture {q} is {list(got)}, not sample {j}")
        seen["jpeg chunks"] += 1
    seen["jpeg tracks"] += 1
    return said


@cache
def compared(path: Path, frames: int) -> tuple[tuple[str, ...], Counter]:
    """What differs between the arrays the profile wrote of the file at `path`, of at most `frames`
    pictures a chunk, and the file's descriptions, and what the comparison saw."""
    d = described(path)
    with core_setting(_core._set_chunk_frames, None if frames == MOST else frames):
        _, out = _zarrify(str(path), scope=str(path.parent))
    assert isinstance(out, Output), "an MP4 is one file"
    root = mapping(json.loads(out.bytes_entries["zarr.json"]), "root")
    tracks = sequence(at(root, "attributes", "refkv_zarrified", "mp4", "tracks"))
    moov = d.ir.at("moov")
    assert moov is not None
    traks = [c for c in d.ir.children(moov) if d.ir.name(c).split("~")[0] == "trak"]
    by_track: dict[int, list[int]] = {}
    for k, a in enumerate(d.stored.arrays):
        by_track.setdefault(traks.index(a.element), []).append(k)
    said: list[str] = []
    seen: Counter = Counter()
    with av.open(str(path)) as container:
        for t, ks in sorted(by_track.items()):
            a = d.stored.arrays[ks[0]]
            names = [c.name for chain in a.chains for c in d.stored.chains[chain]]
            where = at(tracks[t], "path")
            if a.grid is None or names == ["source"]:
                # an audio track, or a video track the IR does not model: its samples' places are
                # described, and what they decode to is not
                seen["tracks of no codec the list names"] += 1
                continue
            if where is None:
                seen["described tracks the profile writes no array of"] += 1
                continue
            container.seek(0)
            times = [p.pts for p in container.demux(container.streams[t]) if p.size and p.pts is not None]
            if len(times) != len(d.rows[ks[0]]):
                said.append(f"track {t}: {len(d.rows[ks[0]])} rows, ffmpeg {len(times)} samples with a time")
            elif names[0] in VIDEO:
                said += [f"track {t}: {s}" for s in check_video(d, ks, out, string(where), times, frames, seen)]
            else:
                said += [f"track {t}: {s}" for s in check_jpeg(d, ks[0], out, string(where), times, seen)]
    return tuple(said), seen


@pytest.mark.parametrize("frames", [4, MOST])
@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_the_mp4_profile_writes_what_the_descriptions_give(path, frames):
    """Every video array the profile writes of the file is what its track's descriptions give."""
    said, _ = compared(path, frames)
    assert said == ()


def test_the_fixtures_hold_every_case_the_profile_is_compared_in():
    """The fixtures reach each case of the comparison, so that none of it passes for want of a
    file."""
    seen: Counter = Counter()
    for path in FILES:
        for frames in (4, MOST):
            seen += Counter({f"{k}, {frames} a chunk": v for k, v in compared(path, frames)[1].items()})
    assert {k: v for k, v in seen.items() if v} == {
        "chunks decoded from the sync sample before, 4 a chunk": 11,
        "chunks that decode samples they do not hold, 256 a chunk": 2,
        "chunks that decode samples they do not hold, 4 a chunk": 528,
        "chunks, 256 a chunk": 54,
        "chunks, 4 a chunk": 613,
        "described tracks the profile writes no array of, 256 a chunk": 4,
        "described tracks the profile writes no array of, 4 a chunk": 4,
        "jpeg chunks, 256 a chunk": 80,
        "jpeg chunks, 4 a chunk": 80,
        "jpeg tracks, 256 a chunk": 8,
        "jpeg tracks, 4 a chunk": 8,
        "tracks of no codec the list names, 256 a chunk": 54,
        "tracks of no codec the list names, 4 a chunk": 54,
        "video tracks of several chunks, 256 a chunk": 2,
        "video tracks of several chunks, 4 a chunk": 52,
        "video tracks, 256 a chunk": 52,
        "video tracks, 4 a chunk": 52,
    }
