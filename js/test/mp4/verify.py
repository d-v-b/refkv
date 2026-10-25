"""Checks the browser MP4 zarrifier's output against ffmpeg's decoding of each file.

Each fixtures/mp4/*.mp4 and *.mov is served over local HTTP and zarrified by
js/conformance/zarrify.ts (the browser code, run under Node). Every zarrified track is read
through the reference reader, zarr-python and the `refkv.stream` codec, and must equal ffmpeg's
decoding of the track (through PyAV, whose demuxer reads the file's tables itself), value for
value: a video track's Y, U and V planes and its presentation times on the media timeline, an
audio track's samples (spec/zarrify/mp4.md §4
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp4.md#4-the-layout>; AAC with noise
substitution only in its level, since a chunk decodes its noise from a generator of its own).
The file must be rebuilt byte for byte from the archive's IR mirror (refkv_source, §5.3
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp4.md#53-elements>). `mp4_reject_*`
files must be rejected.

Usage: uv run python js/test/mp4/verify.py [<dir of .mp4 and .mov files>]
"""

from __future__ import annotations

import sys
from pathlib import Path

import av
import numpy as np
import zarr

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "conformance" / "vzip"))

import refkv.zarr  # noqa: E402  (a vzip archive as a zarr store)

sys.path.insert(0, str(HERE.parent))
from ir_mirror import mirror_problems  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # js/test, for verify_common
from verify_common import LOOPBACK_SOURCES, run, serve  # noqa: E402
NOISE = {"mp4_aac_pns.mp4"}


def decoded(path: Path, index: int):
    """ffmpeg's demuxer's packets of stream `index`, decoded in one pass by a decoder given them
    alone: (kind, frames, the packets' presentation times, sorted)."""
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


def problems_of(path: Path, out: Path) -> list[str]:
    root = zarr.open_group(refkv.zarr.open(str(out), policy=LOOPBACK_SOURCES), mode="r", zarr_format=3)
    problems = []
    for track in root.attrs["refkv_zarrified"]["mp4"]["tracks"]:
        if track["path"] is None:
            continue
        g = root[track["path"]]
        tm = g.attrs["refkv_zarrified"]["mp4"]
        kind, frames, pts = decoded(path, track["id"] - 1)
        if kind == "video":
            for k, name in enumerate("yuv"):
                want = np.stack([np.frombuffer(f.planes[k], np.uint8).reshape(f.planes[k].height, f.planes[k].line_size)
                                 [:, :f.planes[k].width] for f in frames])
                got = g[name][...]
                if got.shape != want.shape or not np.array_equal(got, want):
                    problems.append(f"track {track['id']}'s {name} plane differs from ffmpeg's ({got.shape} vs {want.shape})")
            shift = next((e["media_time"] for e in tm["edits"] if e["media_time"] >= 0), 0)
            if g["time"][...].tolist() != [t + shift for t in pts]:
                problems.append(f"track {track['id']}'s times differ from ffmpeg's")
        else:
            want = np.concatenate([f.to_ndarray().T if f.format.is_planar else f.to_ndarray().reshape(f.samples, -1)
                                   for f in frames]).astype(np.float32)
            got = g["samples"][...]
            if got.shape != want.shape:
                problems.append(f"track {track['id']}'s samples are {got.shape}, ffmpeg's {want.shape}")
            elif path.name in NOISE:
                level = lambda a: float(np.sqrt(np.mean(a.astype(np.float64) ** 2)))  # noqa: E731
                if abs(level(got) - level(want)) > 0.1 * level(want):
                    problems.append(f"track {track['id']}'s level differs from ffmpeg's")
            elif not np.array_equal(got, want):
                problems.append(f"track {track['id']}'s samples differ from ffmpeg's")
    return problems + mirror_problems(out, path)


def main(fixtures: Path) -> int:
    paths = sorted(p for p in fixtures.iterdir() if p.suffix in (".mp4", ".mov"))
    return run(paths, lambda path, out, _: problems_of(path, out), url=serve(paths), width=36)


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "fixtures" / "mp4"))
