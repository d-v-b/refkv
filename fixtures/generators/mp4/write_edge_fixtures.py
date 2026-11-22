"""Writes MP4 files to fixtures/mp4/ that exercise the edges of the MP4 profile
(spec/zarrify/mp4.md §8.1
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp4.md#81-boxes>, §8.2
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp4.md#82-tracks>) byte by byte, with
no ffmpeg: an `ftyp` box with a 64-bit size whose payload is too short for its major brand and
minor version; from the fixture `mp4_h264_two_tracks.mp4`, two tracks of one track ID; and, from
fragmented fixtures, tracks whose stts box counts samples their tables do not lay out, so that
no sample has a time. `edge_reject_*` files must be rejected.

Usage: uv run python fixtures/generators/mp4/write_edge_fixtures.py
"""

from __future__ import annotations

import struct
from pathlib import Path

OUT = Path(__file__).parents[2] / "mp4"


def box(typ: bytes, payload: bytes = b"", large: bool = False) -> bytes:
    """A box: its header (with a 64-bit size when `large`), then its payload."""
    if large:
        return struct.pack(">I4sQ", 1, typ, 16 + len(payload)) + payload
    return struct.pack(">I4s", 8 + len(payload), typ) + payload


def track_ids_alike(mp4: bytes) -> bytes:
    """`mp4` with its second tkhd box's track_id set to its first's."""
    ats = []
    i = mp4.find(b"tkhd")
    while i >= 0:
        # the payload: version (1 byte), flags (3), creation and modification times (4 or 8
        # bytes each, by the version), then track_id (4)
        ats.append(i + 4 + 4 + (16 if mp4[i + 4] == 1 else 8))
        i = mp4.find(b"tkhd", i + 4)
    assert len(ats) == 2, ats
    out = bytearray(mp4)
    out[ats[1]:ats[1] + 4] = mp4[ats[0]:ats[0] + 4]
    return bytes(out)


def boxes_in(mp4: bytes, parent: bytes) -> list[tuple[int, bytes, int]]:
    """Each child box of the one `parent` box in `mp4`: its offset, type and size."""
    p = mp4.find(parent) - 4
    end = p + struct.unpack(">I", mp4[p:p + 4])[0]
    p += 8
    out = []
    while p < end:
        size, typ = struct.unpack(">I4s", mp4[p:p + 8])
        out.append((p, typ, size))
        p += size
    return out


def without_stsz(mp4: bytes) -> bytes:
    """`mp4`, of one track, with its stsz box renamed `free`: its tables lay out no sample, while
    its stts box still counts the samples they held, so the samples of its fragments have no
    time. Nothing moves."""
    assert mp4.count(b"stsz") == 1
    i = mp4.find(b"stsz")
    return mp4[:i] + b"free" + mp4[i + 4:]


def stts_of_three(mp4: bytes) -> bytes:
    """`mp4`, of one track whose tables hold no sample (all in fragments), with its stts box
    given one entry of 3 samples and its stsz box dropped, a free box taking up the bytes freed,
    so that its fragments' samples have no time. Nothing moves: stts, stsc and stsz are the
    stbl box's second to fourth boxes, and become stts, stsc and free."""
    kids = boxes_in(mp4, b"stbl")
    (t, tt, ts), (c, ct, cs), (z, zt, zs) = kids[1:4]
    assert (tt, ct, zt) == (b"stts", b"stsc", b"stsz"), kids
    assert ts + zs >= 32, kids
    stts = box(b"stts", struct.pack(">4xIII", 1, 3, 1))
    return mp4[:t] + stts + mp4[c:c + cs] + box(b"free", bytes(ts + zs - len(stts) - 8)) + mp4[z + zs:]


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for old in OUT.glob("edge_*.mp4"):
        old.unlink()
    # a 20-byte ftyp box, its 16-byte header leaving 4 bytes of the 8 its fields need, then an
    # empty moov box
    (OUT / "edge_reject_mp4_ftyp_short.mp4").write_bytes(box(b"ftyp", b"isom", large=True) + box(b"moov"))
    # the second of two zarrifiable tracks has the first's track ID: it is not zarrified
    (OUT / "edge_mp4_duplicate_track_id.mp4").write_bytes(
        track_ids_alike((OUT / "mp4_h264_two_tracks.mp4").read_bytes()))
    # samples of a Motion JPEG track and an H.264 track that have no time: rejected
    (OUT / "edge_reject_mp4_mjpeg_untimed.mp4").write_bytes(stts_of_three((OUT / "mp4_frag_mjpeg.mp4").read_bytes()))
    (OUT / "edge_reject_mp4_h264_untimed.mp4").write_bytes(without_stsz((OUT / "mp4_frag_hybrid.mp4").read_bytes()))
    for p in sorted(OUT.glob("edge_*.mp4")):
        print(p.name, p.stat().st_size)


if __name__ == "__main__":
    main()
