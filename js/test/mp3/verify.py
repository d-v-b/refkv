"""Checks the browser MP3 zarrifier's output against two decodings of its frames.

Each fixtures/mp3/*.mp3 is served over local HTTP and zarrified by
js/conformance/zarrify.ts (the browser code, run under Node). The array is read
through the reference reader, zarr-python and the `refkv.stream` codec, and must equal
ffmpeg's float decoder (through PyAV, the codec's own) decoding the audio frames in one
pass, sample for sample (spec/zarrify/mp3.md §4.1
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp3.md#41-the-array>), and ffmpeg's
(fixtures/mp3/expected, written by the generator) within a few units where the generator
stored it. The root's metadata must agree with the array, and the file must be rebuilt byte
for byte from the archive's IR mirror (refkv_source, §5.2
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp3.md#52-the-ir-mirror>).
`mp3_reject_*` files must be rejected.

Usage: uv run python js/test/mp3/verify.py [<dir of .mp3 files>]
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
from refkv._narrow import at, mapping, ndarray  # noqa: E402
from refkv.ir.cmirror import Archive, load  # noqa: E402

sys.path.insert(0, str(HERE.parent))
from ir_mirror import mirror_problems  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # js/test, for verify_common
from verify_common import LOOPBACK_SOURCES, run, serve  # noqa: E402

FFMPEG_MAX, FFMPEG_MEAN = 1, 0.6


def audio_frames(path: Path, out: Path) -> list[bytes]:
    """The audio frames' bytes, in order, from the archive's mirror: its data
    elements (spec/zarrify/mp3.md §5.3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp3.md#53-elements>)."""
    data = path.read_bytes()
    ir = load(Archive(str(out), lambda o, n: data[o:o + n]))
    cols = ir.columns()
    kind = np.frombuffer(cols["kind"], np.uint8)
    start, length = np.frombuffer(cols["start"], "<u8"), np.frombuffer(cols["len"], "<u8")
    data_elements = np.flatnonzero(kind == 2)
    frames = sorted(zip(start[data_elements].tolist(), length[data_elements].tolist(), strict=True))
    return [data[o:o + n] for o, n in frames]


def problems_of(path: Path, out: Path) -> list[str]:
    root = zarr.open_group(refkv.zarr.open(str(out), policy=LOOPBACK_SOURCES), mode="r", zarr_format=3)
    meta = mapping(at(root.attrs, "refkv_zarrified", "mp3"))
    got = ndarray(root.get_array("samples")[...])
    problems = []
    if got.dtype != np.float32 or got.shape != (meta["samples"], meta["channels"]):
        problems.append(f"shape {got.shape} {got.dtype} is not {meta['samples']} x {meta['channels']} float32")
        return problems
    ctx = av.CodecContext.create("mp3float", "r")
    assert isinstance(ctx, av.AudioCodecContext)
    decoded = [d for p in audio_frames(path, out) for d in ctx.decode(av.Packet(p))] + ctx.decode(None)
    if {f.sample_rate for f in decoded} != {meta["sample_rate"]}:
        problems.append(f"sample rate {meta['sample_rate']} is not the decoder's "
                        f"{sorted({f.sample_rate for f in decoded})}")
    want = np.concatenate([f.to_ndarray().T if f.format.is_planar else f.to_ndarray().reshape(f.samples, -1)
                           for f in decoded]).astype(np.float32)
    if want.shape != got.shape or not np.array_equal(got, want):
        problems.append(f"samples differ from the decoder's ({got.shape} vs {want.shape})")
    expected = path.parent / "expected" / f"{path.stem}.npy"
    if expected.exists():
        w = np.load(expected).astype(np.int32)
        scaled = np.clip(np.rint(got * 32768.0), -32768, 32767).astype(np.int32)
        diff = np.abs(scaled - w) if w.shape == got.shape else None
        if diff is None or diff.max() > FFMPEG_MAX or diff.mean() > FFMPEG_MEAN:
            problems.append(f"samples differ from ffmpeg's ({'shape' if diff is None else (diff.max(), diff.mean())})")
    return problems + mirror_problems(out, path)


def main(fixtures: Path) -> int:
    paths = sorted(fixtures.glob("*.mp3"))
    return run(paths, lambda path, out, _: problems_of(path, out), url=serve(paths), width=40)


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "fixtures" / "mp3"))
