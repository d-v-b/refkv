"""Writes the HDF4 inputs the profile rejects (spec/zarrify/hdf4.md §8.3
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#83-checks>) to fixtures/hdf4/:
each `hdf4_reject_*` is the committed `hdf4_small.hdf` (write_fixtures.py) with a few bytes
patched, so that it breaks one check, the first its message names. The patches need no HDF4
library, so this runs everywhere and gives the same bytes. conformance/zarrify/rejects/hdf4.json
lists each with its message.

Usage: uv run python fixtures/generators/hdf4/write_rejects.py
"""

from __future__ import annotations

import struct
from pathlib import Path

OUT = Path(__file__).parents[2] / "hdf4"

LINKED, NT, VS, SD, SPECIAL = 20, 106, 1963, 702, 0x4000


def descriptors(b: bytes):
    """Each descriptor's offset in the file and its (tag, ref, offset, length)."""
    out, at = [], 4
    while True:
        ndd, nxt = struct.unpack_from(">hI", b, at)
        for k in range(ndd):
            o = at + 6 + 12 * k
            out.append((o, struct.unpack_from(">HHII", b, o)))
        if nxt == 0:
            return out
        at = nxt


def element(b: bytes, tag: int, ref: int) -> tuple[int, int]:
    return next((o, n) for _, (t, r, o, n) in descriptors(b) if (t, r) == (tag, ref))


def rejects(source: Path) -> dict[str, bytes]:
    b = source.read_bytes()
    dds = descriptors(b)

    def patched(at: int, fmt: str, *v, then: tuple = ()) -> bytes:
        out = bytearray(b)
        struct.pack_into(fmt, out, at, *v)
        if then:
            struct.pack_into(then[1], out, then[0], *then[2:])
        return bytes(out)

    # a linked element (a chunk table's records) and its first link table: a code (2 bytes), the
    # length, block length and blocks a table (4 each), then the table's ref
    linked = next((t, r) for _, (t, r, _, _) in dds if t == SPECIAL | VS)
    link_off, _ = element(b, *linked)
    first_table = struct.unpack_from(">H", b, link_off + 14)[0]
    table_off, _ = element(b, LINKED, first_table)
    # the first chunked dataset's header: a code, its length, a version, four lengths, then the
    # chunk table's tag and ref; the table's second block holds its second row, whose first
    # coordinate is patched
    sd_tag, sd_ref = next((t, r) for _, (t, r, _, _) in dds if t == SPECIAL | SD)
    chunked_off, _ = element(b, sd_tag, sd_ref)
    chunk_table_ref = struct.unpack_from(">H", b, chunked_off + 2 + 4 + 1 + 16 + 2)[0]
    rows = struct.unpack_from(">H", b, element(b, SPECIAL | VS, chunk_table_ref)[0] + 14)[0]
    blocks = struct.unpack_from(">HH", b, element(b, LINKED, rows)[0])
    row_off = element(b, LINKED, blocks[1])[0]
    nt_desc = next(o for o, (t, _, _, _) in dds if t == NT)
    other_nt = next(r for o, (t, r, _, _) in dds if t == NT and o != nt_desc)
    return {
        "hdf4_reject_magic.hdf": patched(0, ">B", 0x0F),
        "hdf4_reject_block_past_end.hdf": patched(4, ">h", 30000),
        "hdf4_reject_block_count.hdf": patched(4, ">h", -2),
        "hdf4_reject_block_loop.hdf": patched(6, ">I", 4),
        "hdf4_reject_element_past_end.hdf": patched(nt_desc + 4, ">I", len(b)),
        "hdf4_reject_described_twice.hdf": patched(nt_desc + 2, ">H", other_nt),
        "hdf4_reject_special_code.hdf": patched(chunked_off, ">H", 9),
        # one block a table, so that the element's two blocks need a second table, which is the first
        "hdf4_reject_link_loop.hdf": patched(link_off + 10, ">I", 1, then=(table_off, ">H", first_table)),
        "hdf4_reject_chunk_outside.hdf": patched(row_off, ">I", 9),
    }


def main() -> None:
    for p in OUT.glob("hdf4_reject_*.hdf"):
        p.unlink()
    for name, data in rejects(OUT / "hdf4_small.hdf").items():
        (OUT / name).write_bytes(data)


if __name__ == "__main__":
    main()
