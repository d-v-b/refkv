"""Checks the browser FLAC zarrifier's output against independent readings of the file.

Each fixtures/flac/*.flac is served over local HTTP and zarrified by
js/conformance/zarrify.ts (the browser code, run under Node). The array, sharded, is read
through the reference reader, zarr-python's sharding codec and the `refkv.stream` codec, and
must be the stream's samples (spec/zarrify/flac.md §4.3
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/flac.md#43-the-samples>): STREAMINFO's
MD5 of them, and, for 8, 16 and 24 bits, libsndfile's decoding of the whole file (through
soundfile), sample for sample; ffmpeg's demuxer and decoder (through PyAV), reading the file
itself, must give them too. The root's metadata must agree with the array, and the file must be
rebuilt byte for byte from the archive's IR mirror (refkv_source, §5.2
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/flac.md#52-the-ir-mirror>). Rejected
fixtures (`edge_reject_flac_*`) must be rejected.

Usage: uv run python js/test/flac/verify.py [<dir of .flac files>]
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import av
import numpy as np
import soundfile
import zarr

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "conformance" / "vzip"))

import refkv.zarr  # noqa: E402  (a vzip archive as a zarr store)
from refkv._narrow import at, integer, mapping, ndarray, string  # noqa: E402

sys.path.insert(0, str(HERE.parent))
from ir_mirror import mirror_problems  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # js/test, for verify_common
from verify_common import LOOPBACK_SOURCES, run, serve  # noqa: E402


def md5(samples: np.ndarray, bits: int) -> str:
    """STREAMINFO's MD5 of `samples` (RFC 9639 §8.2
    <https://www.rfc-editor.org/rfc/rfc9639#section-8.2>): interleaved, each little-endian in
    whole bytes, sign-extended."""
    width = (bits + 7) // 8
    raw = samples.astype({1: "<i1", 2: "<i2", 3: "<i4", 4: "<i4"}[width]).tobytes()
    if width == 3:
        raw = b"".join(raw[k:k + 3] for k in range(0, len(raw), 4))
    return hashlib.md5(raw, usedforsecurity=False).hexdigest()


def ffmpeg(path: Path, bits: int) -> np.ndarray:
    """ffmpeg's decoding of the file (its own demuxer and frame search), shifted back from the
    high bits of 16 or 32: [samples, channels]."""
    with av.open(str(path)) as c:
        frames = [f.to_ndarray() for f in c.decode(audio=0)]
        planar = c.streams.audio[0].format.is_planar
        channels = c.streams.audio[0].channels
    a = np.concatenate([f.T if planar else f.reshape(-1, channels) for f in frames])
    return np.right_shift(a, 8 * a.dtype.itemsize - bits)


def problems_of(path: Path, out: Path) -> list[str]:
    root = zarr.open_group(refkv.zarr.open(str(out), policy=LOOPBACK_SOURCES), mode="r", zarr_format=3)
    meta = mapping(at(root.attrs, "refkv_zarrified", "flac"))
    bits = integer(meta["bits_per_sample"])
    got = ndarray(root.get_array("samples")[...])
    problems = []
    if got.shape != (meta["samples"], meta["channels"]):
        return [f"shape {got.shape} is not {meta['samples']} x {meta['channels']}"]
    if md5(got, bits) != string(at(meta, "streaminfo", "md5")):
        problems.append("the samples' MD5 is not STREAMINFO's")
    if bits in (8, 16, 24):
        want, _ = soundfile.read(path, dtype="int32", always_2d=True)
        if not np.array_equal(got, np.right_shift(want, 32 - bits)):
            problems.append("samples differ from libsndfile's")
    want = ffmpeg(path, bits)
    if want.shape != got.shape or not np.array_equal(got, want):
        problems.append(f"samples differ from ffmpeg's ({got.shape} vs {want.shape})")
    return problems + mirror_problems(out, path)


def main(fixtures: Path) -> int:
    paths = sorted(fixtures.glob("*.flac"))
    return run(paths, lambda path, out, _: problems_of(path, out), url=serve(paths), width=44)


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "fixtures" / "flac"))
