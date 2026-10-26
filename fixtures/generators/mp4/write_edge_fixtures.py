"""Writes MP4 files to fixtures/mp4/ that exercise the edges of the MP4 profile
(spec/zarrify/mp4.md §8.1
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp4.md#81-boxes>, §8.2
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp4.md#82-tracks>) byte by byte, with
no ffmpeg: an `ftyp` box with a 64-bit size whose payload is too short for its major brand and
minor version, and, from the fixture `mp4_h264_two_tracks.mp4`, two tracks of one track ID.
`edge_reject_*` files must be rejected.

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
    for p in sorted(OUT.glob("edge_*.mp4")):
        print(p.name, p.stat().st_size)


if __name__ == "__main__":
    main()
