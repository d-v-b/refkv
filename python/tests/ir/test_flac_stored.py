"""The stored array of the FLAC fixtures (spec/stored-array.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>), compared with
the stream's own checksum and with libsndfile (through soundfile), the reference reader. Of each
file

- the array is keyed by the STREAMINFO block, which is its one declaration and its codec's `setup`;
- its shape is the samples and the channels libsndfile reads, and STREAMINFO's where it records
  its total; where the total is unknown (0), which libsndfile refuses, the samples ffmpeg's
  demuxer and decoder give;
- each frame is a row, in order, at its element, and is a unit of the block size, the last one
  shorter;
- each row's bytes, decoded alone by ffmpeg's FLAC decoder (through PyAV) given the `setup`'s
  bytes, are that frame's samples, each in whole bytes, little-endian and sign-extended, as
  `item_bits` says;
- the rows' decoded bytes, joined, have the MD5 STREAMINFO records (RFC 9639 §8.2
  <https://www.rfc-editor.org/rfc/rfc9639#section-8.2>), where it records one, and are the
  samples libsndfile reads, for the bit depths it reads (8, 16 and 24).

A second test asserts which cases the fixtures reached."""

import hashlib
from collections import Counter
from functools import cache
from pathlib import Path

import av
import numpy as np
import pytest
import soundfile
from described import Described, described
from ir_helpers import FIXTURES

from refkv.ir.stored import Each, ElementPlace, Source

FILES = sorted(p for p in (FIXTURES / "flac").glob("*.flac") if "reject" not in p.name)
SOUNDFILE_BITS = (8, 16, 24)


def whole_bytes(samples: np.ndarray, width: int) -> bytes:
    """Integer samples, interleaved, each `width` bytes, little-endian, sign-extended."""
    raw = samples.astype("<i8").tobytes()
    return b"".join(raw[k:k + width] for k in range(0, len(raw), 8))


def decoded(setup: bytes, unit: bytes, bits: int) -> np.ndarray:
    """One frame decoded alone by ffmpeg's FLAC decoder, given STREAMINFO's body: [samples,
    channels], each sample as the stream codes it (ffmpeg gives it in the high bits of 16 or 32)."""
    context = av.CodecContext.create("flac", "r")
    context.extradata = setup
    frames = context.decode(av.Packet(unit)) + context.decode(None)
    assert len(frames) == 1, "a frame a unit"
    a = frames[0].to_ndarray()
    a = a.T if frames[0].format.is_planar else a.reshape(frames[0].samples, -1)
    return np.right_shift(a.astype(np.int64), 8 * a.dtype.itemsize - bits)


@cache
def check(path: Path) -> tuple[list[str], Counter]:
    """What differs between a file's description and the references, and the cases it met."""
    d: Described = described(path)
    seen: Counter = Counter()
    if d.stored.undescribed or len(d.stored.arrays) != 1:
        return [f"{len(d.stored.arrays)} descriptions"], seen
    (a,) = d.stored.arrays
    said = []
    rows = d.rows.get(0, [])

    # the key, the declaration and the setup: the STREAMINFO block
    if a.path != "metadata/0/streaminfo" or a.index is not None:
        said.append(f"keyed by {a.path}")
    if a.declarations != (ElementPlace(a.element, None, None),):
        said.append(f"declarations {a.declarations}")
    (chain,) = [d.stored.chains[c] for c in a.chains]
    setup = chain[0].parameters.get("setup") if [c.name for c in chain] == ["flac"] else None
    if not isinstance(setup, Source) or setup.place != ElementPlace(a.element, None, None):
        return [*said, f"the chain {chain}"], seen
    info = d.place(setup.place)
    bits = ((info[12] & 1) << 4 | info[13] >> 4) + 1
    channels = (info[12] >> 1 & 7) + 1
    total = int.from_bytes(info[13:18]) & (1 << 36) - 1
    width = (bits + 7) // 8
    seen[f"{bits}-bit samples"] += 1
    known = total > 0
    if not known:
        seen["files of an unknown total"] += 1
        with av.open(str(path)) as container:
            total = sum(f.samples for f in container.decode(audio=0))

    # the shape and the grid: the samples and channels, a frame a unit
    if a.shape != (total, channels) or a.item_bits != 8 * width:
        said.append(f"shape {a.shape}, item_bits {a.item_bits}, not {(total, channels)} of {8 * width}")
    if a.grid is None or a.grid.edge != "short" or len(a.grid.axes) != 2:
        return [*said, f"the grid {a.grid}"], seen
    along, across = a.grid.axes
    if not isinstance(along, Each) or across != Each(channels):
        return [*said, f"the grid {a.grid}"], seen

    # each row a frame, in order, decoded alone
    at = d.ir.at("frames")
    assert at is not None
    frames = d.ir.children(at)
    if [r["element"] for r in rows] != frames or [r["coords"] for r in rows] != [[j, 0] for j in range(len(frames))]:
        said.append("the rows are not the frames in order")
    joined = []
    for j, r in enumerate(rows):
        samples = decoded(info, d.unit(r), bits)
        want = min(along.length, total - j * along.length)
        if samples.shape != (want, channels):
            said.append(f"frame {j} decodes to {samples.shape}, not {(want, channels)}")
        joined.append(samples)
    samples = np.concatenate(joined) if joined else np.zeros((0, channels), np.int64)
    seen["frames"] += len(rows)

    # STREAMINFO's MD5 of the decoded bytes, and libsndfile's samples
    md5 = info[18:34]
    if any(md5):
        seen["files with an MD5"] += 1
        if hashlib.md5(whole_bytes(samples, width), usedforsecurity=False).digest() != md5:
            said.append("the decoded rows do not have STREAMINFO's MD5")
    if bits in SOUNDFILE_BITS and known:
        seen["files libsndfile reads"] += 1
        read, _ = soundfile.read(path, dtype="int32", always_2d=True)
        if not np.array_equal(np.right_shift(read.astype(np.int64), 32 - bits), samples):
            said.append("the decoded rows are not libsndfile's samples")
    return said, seen


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_the_description_is_the_streams_samples(path):
    said, _ = check(path)
    assert not said, "\n".join(said)


def test_the_fixtures_reach_every_case():
    seen: Counter = Counter()
    for path in FILES:
        seen += check(path)[1]
    for bits in (8, 12, 16, 20, 24, 32):
        assert seen[f"{bits}-bit samples"], bits
    assert seen["files with an MD5"]
    assert seen["files libsndfile reads"]
    assert seen["files of an unknown total"]
    assert seen["frames"] > 1000
