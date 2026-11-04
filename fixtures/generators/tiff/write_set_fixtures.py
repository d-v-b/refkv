"""Writes TIFFs to fixtures/tiff/ whose image lies across files (spec/zarrify/tiff.md §7.4
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#74-several-files>), and Micro-Manager
stacks (spec/zarrify/tiff.md §7.5
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#75-micro-manager-stacks>): tifffile
writes neither, so they are written here, byte by byte. `edge_reject_*` inputs must be rejected.

Usage: uv run python fixtures/generators/tiff/write_set_fixtures.py
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import numpy as np

OUT = Path(__file__).parents[2] / "tiff"
WIDTH, HEIGHT = 16, 8
INDEX_MAP, DISPLAY, COMMENTS, SUMMARY, INDEX = 54773648, 483765892, 99384722, 2355492, 3453623


def plane(k: int) -> np.ndarray:
    """Plane `k` of an image, a pattern no other plane has."""
    y, x = np.mgrid[0:HEIGHT, 0:WIDTH]
    return (x * 3 + y * 5 + k * 257).astype("<u2")


def write(name: str, planes: list[np.ndarray], description: str | None = None, summary: dict | None = None,
          index: list[tuple[int, int, int, int]] | None = None) -> None:
    """A little-endian classic TIFF of one strip a plane: with `summary` and `index` (channel, slice,
    frame, position, by plane), a Micro-Manager file's header; `description` is IFD 0's."""
    summary_bytes = json.dumps(summary).encode() if summary is not None else b""
    buf = bytearray(b"II*\0\0\0\0\0")
    if summary is not None:
        buf += bytes(32) + summary_bytes
    text = description.encode() + b"\0" if description is not None else None
    described = len(buf)
    if text is not None:
        buf += text + bytes(len(text) % 2)
    ifds = []
    for p in planes:
        data = p.tobytes()
        at = len(buf)
        buf += data
        ifds.append((at, len(data), len(buf)))
        buf += bytes(2 + 12 * (11 if text is not None and len(ifds) == 1 else 10) + 4)
    for k, (at, size, ifd) in enumerate(ifds):
        tags = [(256, 3, 1, WIDTH), (257, 3, 1, HEIGHT), (258, 3, 1, 16), (259, 3, 1, 1), (262, 3, 1, 1)]
        if text is not None and k == 0:
            tags.append((270, 2, len(text), described))
        tags += [(273, 4, 1, at), (277, 3, 1, 1), (278, 3, 1, HEIGHT), (279, 4, 1, size), (339, 3, 1, 1)]
        entries = b"".join(struct.pack("<HHII", *t) for t in tags)
        following = ifds[k + 1][2] if k + 1 < len(ifds) else 0
        buf[ifd:ifd + 2 + len(entries) + 4] = struct.pack("<H", len(tags)) + entries + struct.pack("<I", following)
    struct.pack_into("<I", buf, 4, ifds[0][2])
    if summary is not None:
        map_at = len(buf)
        buf += struct.pack("<II", INDEX, len(ifds))
        for (channel, slice_, frame, position), (_, _, ifd) in zip(index or [], ifds, strict=True):
            buf += struct.pack("<5I", channel, slice_, frame, position, ifd)
        struct.pack_into("<8I", buf, 8, INDEX_MAP, map_at, DISPLAY, 0, COMMENTS, 0, SUMMARY, len(summary_bytes))
    (OUT / name).write_bytes(buf)


def ome(size_z: int, tiff_data: str, uuid: str | None = None) -> str:
    """An OME-XML document of one image of `size_z` planes."""
    root = f' UUID="{uuid}"' if uuid else ""
    return (f'<?xml version="1.0"?><OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06"{root}>'
            f'<Image ID="Image:0" Name="set"><Pixels ID="Pixels:0" DimensionOrder="XYZCT" Type="uint16"'
            f' SizeX="{WIDTH}" SizeY="{HEIGHT}" SizeZ="{size_z}" SizeC="1" SizeT="1"'
            f' PhysicalSizeX="0.5" PhysicalSizeY="0.5" PhysicalSizeZ="2">'
            f'<Channel ID="Channel:0:0" Name="GFP"/>{tiff_data}</Pixels></Image></OME>')


def data(first_z: int, count: int, file: str, uuid: str) -> str:
    """A `TiffData` of `count` planes from `first_z`, in `file`, whose first IFD holds the first."""
    return (f'<TiffData FirstZ="{first_z}" IFD="0" PlaneCount="{count}">'
            f'<UUID FileName="{file}">urn:uuid:{uuid}</UUID></TiffData>')


def ome_sets() -> None:
    a, b = "00000000-0000-0000-0000-00000000000a", "00000000-0000-0000-0000-00000000000b"
    # every file holds the document, whose UUID says which file it is
    links = data(0, 2, "ome_set_a.ome.tif", a) + data(2, 3, "ome_set_b.ome.tif", b)
    write("ome_set_a.ome.tif", [plane(0), plane(1)], ome(5, links, f"urn:uuid:{a}"))
    write("ome_set_b.ome.tif", [plane(2), plane(3), plane(4)], ome(5, links, f"urn:uuid:{b}"))
    # a sibling with no OME-XML has nothing to say about itself
    links = data(0, 1, "ome_set_c.ome.tif", a) + data(1, 1, "ome_set_plain.tif", b)
    write("ome_set_c.ome.tif", [plane(0)], ome(2, links, f"urn:uuid:{a}"))
    write("ome_set_plain.tif", [plane(1)])
    # a sibling whose own UUID is not the one the document records
    links = data(0, 1, "edge_reject_ome_set_identity.ome.tif", a) + data(1, 1, "ome_set_stranger.ome.tif", b)
    write("edge_reject_ome_set_identity.ome.tif", [plane(0)], ome(2, links, f"urn:uuid:{a}"))
    write("ome_set_stranger.ome.tif", [plane(1)], ome(1, "<TiffData/>", "urn:uuid:ffffffff-0000-0000-0000-000000000000"))
    # a sibling of another size
    links = data(0, 1, "edge_reject_ome_set_size.ome.tif", a) + data(1, 1, "ome_set_narrow.tif", b)
    write("edge_reject_ome_set_size.ome.tif", [plane(0)], ome(2, links, f"urn:uuid:{a}"))
    global WIDTH
    width, WIDTH = WIDTH, 8
    write("ome_set_narrow.tif", [plane(1)])
    WIDTH = width
    # a TiffData that names a file a parent directory holds
    write("edge_reject_ome_set_outside.ome.tif", [plane(0)], ome(2, data(0, 1, "edge_reject_ome_set_outside.ome.tif", a)
                                                                 + data(1, 1, "../secret.tif", b), f"urn:uuid:{a}"))


def micromanager() -> None:
    uuid = "390dd25c-dc45-45a8-b421-44772f3de4f4"
    summary = {"Channels": 2, "Slices": 2, "Frames": 2, "UUID": uuid, "ChNames": ["BF", "GFP"], "PixelSize_um": 0.5,
               "z-step_um": 2.0, "MicroManagerVersion": "1.4.22"}
    index = [(c, z, t, 0) for t in range(2) for z in range(2) for c in range(2)]
    planes = [plane(k) for k in range(8)]
    # all of it in one file
    write("mm_single_MMStack_Pos0.ome.tif", planes, summary=summary, index=index)
    # split, as a 4 GB limit splits it
    write("mm_split_MMStack_Pos0.ome.tif", planes[:5], summary=summary, index=index[:5])
    write("mm_split_MMStack_Pos0_1.ome.tif", planes[5:], summary=summary, index=index[5:])
    # an acquisition stopped early: fewer planes than the summary says, and no second file
    write("mm_stopped_MMStack_Pos0.ome.tif", planes[:3], summary=summary, index=index[:3])
    # a later file named as the input
    write("edge_reject_mm_part_MMStack_Pos0_1.ome.tif", planes[5:], summary=summary, index=index[5:])
    # a second file of another stack
    write("edge_reject_mm_foreign_MMStack_Pos0.ome.tif", planes[:5], summary=summary, index=index[:5])
    write("edge_reject_mm_foreign_MMStack_Pos0_1.ome.tif", planes[5:], summary={**summary, "UUID": "another"}, index=index[5:])
    # several positions in one file
    write("edge_reject_mm_positions_MMStack.ome.tif", planes[:2], summary={**summary, "Frames": 1, "Slices": 1, "Channels": 2},
          index=[(0, 0, 0, 0), (0, 0, 0, 1)])


if __name__ == "__main__":
    ome_sets()
    micromanager()
