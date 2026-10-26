"""Writes MP4 files to fixtures/mp4/ that exercise the edges of the MP4 profile
(spec/zarrify/mp4.md §8.1
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp4.md#81-boxes>) byte by byte, with
no media and no ffmpeg: an `ftyp` box with a 64-bit size whose payload is too short for its
major brand and minor version. `edge_reject_*` files must be rejected.

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


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for old in OUT.glob("edge_*.mp4"):
        old.unlink()
    # a 20-byte ftyp box, its 16-byte header leaving 4 bytes of the 8 its fields need, then an
    # empty moov box
    (OUT / "edge_reject_mp4_ftyp_short.mp4").write_bytes(box(b"ftyp", b"isom", large=True) + box(b"moov"))
    for p in sorted(OUT.glob("edge_*.mp4")):
        print(p.name, p.stat().st_size)


if __name__ == "__main__":
    main()
