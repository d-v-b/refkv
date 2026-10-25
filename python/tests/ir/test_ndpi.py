"""NDPI on the IR (spec/zarrify/ndpi.md): the Rust TIFF parser's NDPI variant and the
NDPI projection against the frozen profile, the mirror, the header's data sources, and
the hostile probes (gen_probes.py)."""

import json
import struct

import imagecodecs
import numpy as np
import pytest
import zarr
from ir_helpers import FIXTURES, REVISED, as_the_reference, assert_rebuilds, same_hierarchy
from refkv_reference import virtualize  # the frozen reference (ir_helpers)

from refkv.ir import zarrify as via_ir
from refkv.zarr import ZarrStore
from refkv.zarrify import Rejected
from refkv.zarrify import zarrify as shipped

NDPIS = sorted((FIXTURES / "ndpi").iterdir())


@pytest.mark.parametrize("path", [p for p in NDPIS if p.name not in REVISED], ids=lambda p: p.name)
def test_projects_as_today_and_rebuilds(path, tmp_path):
    as_the_reference(path, tmp_path, "ndpi")


def test_omero_is_rgb():
    """§4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#4-the-image>: the
    JPEG strips decode to RGB, so `omero` has R, G and B, each with uint8's range as its window."""
    _, out, _ = via_ir(str(FIXTURES / "ndpi" / "ndpi_levels.ndpi"), "u", mirror=False)
    ome = json.loads(out.bytes_entries["zarr.json"])["attributes"]["ome"]
    window = {"min": 0, "max": 255, "start": 0, "end": 255}
    assert ome["omero"] == {"channels": [{"label": label, "color": c, "active": True, "window": window}
                                         for label, c in (("R", "FF0000"), ("G", "00FF00"), ("B", "0000FF"))]}


def test_header_pieces_are_data_sources():
    """§7 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#7-ndpi-profile>: the
    header before and after SOF0 are data sources, one per distinct byte string in order of
    first use, shared by every chunk and level; the strip's own SOF0, which each chunk
    replaces, is in the mirror (the strip is an element, rebuilt byte for byte)."""
    path = FIXTURES / "ndpi" / "ndpi_levels.ndpi"
    _, out, _ = via_ir(str(path), "https://data.test/x.ndpi")
    data = list(out.data)
    assert [out.data[d] for d in data] == list(range(1, len(data) + 1))
    file = path.read_bytes()
    used = set()
    for key, ranges in out.refs.items():
        if key.startswith("refkv_source/") or len(ranges) == 1:  # the mirror, or a whole strip
            continue
        before, sof, after = ranges[:3]
        head, tail = data[before[0] - 1], data[after[0] - 1]
        assert before[1:] == (0, len(head)), key
        assert after[1:] == (0, len(tail)), key
        assert isinstance(sof, bytes), key
        p = file.rfind(head + b"\xff\xc0" + sof[2:4], 0, ranges[3][0])  # this level's strip
        assert head.startswith(b"\xff\xd8"), key
        assert p >= 0, key
        assert file[p + len(head) + len(sof):].startswith(tail), key
        used |= {before[0], after[0]}
    assert used == set(range(1, len(data) + 1))


def test_every_ifd_and_high_word_is_an_element():
    """§5.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#51-elements>: the
    12-byte header, each IFD's entries and their high words, the pointer tags' IFDs read in
    NDPI's layout, a duplicate tag, and the strips."""
    _, _, ir = via_ir(str(FIXTURES / "ndpi" / "ndpi_pointers.ndpi"), "u")
    paths = {ir.path(i): i for i in range(len(ir))}
    assert ir.extent(paths["header"]) == (0, 12)
    assert ir.type(paths["header/first_ifd"]) == "<u8"
    for p in ("ifds/1/exif", "ifds/1/ifd_65449", "ifds/1/tags/65420~1", "ifds/1/tags/65500/high", "ifds/1/strips/0",
              "ifds/0/next_ifd"):
        assert p in paths, p
    assert ir.type(paths["ifds/0/next_ifd"]) == "<u8"
    assert ir.type(paths["ifds/1/tags/65500/high"]) == "<u4"


def test_reads_only_the_values_it_uses():
    """§7 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#7-ndpi-profile>: the
    values of an IFD that is not a level are not checked, so the macro image's McuStarts
    (LONG8, above 2^53 - 1) does not reject."""
    _, out, _ = via_ir(str(FIXTURES / "ndpi" / "ndpi_unused_values.ndpi"), "u")
    assert out.summary["levels"] == [[3, 48, 64]]


def test_a_frame_not_the_level_size_gets_the_level_size(tmp_path):
    """§7 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#7-ndpi-profile>
    (d-v-b/refkv#100): of edge_ndpi_frames.ndpi's strips without McuStarts, level 0's frame is its
    size, and its chunk the strip; level 1's is one row short and level 2's two columns wider,
    in the same MCU rows and columns, so their chunks are the strip with the level's height and
    width in its SOF0, and read through zarr as that stream decodes, at the level's size. The
    last IFD's frame is short by a whole MCU row: it is not a level. The mirror still rebuilds
    the file."""
    path = FIXTURES / "ndpi" / "edge_ndpi_frames.ndpi"
    data = path.read_bytes()
    _, out, _ = via_ir(str(path), "u")
    assert out.summary["levels"] == [[3, 48, 64], [3, 23, 40], [3, 12, 18]]
    g = zarr.open_group(ZarrStore(shipped(str(path))), mode="r", zarr_format=3)
    for level, (h, w) in enumerate([(48, 64), (23, 40), (12, 18)]):
        parts = out.refs[f"{level}/c/0/0/0"]
        if level == 0:
            (offset, length), = parts
            strip = data[offset:offset + length]
            want = imagecodecs.jpeg_decode(strip)
        else:
            (offset, at), size, (rest, length) = parts
            assert size == struct.pack(">HH", h, w)
            assert rest == offset + at + 4
            strip = data[offset:rest + length]
            assert strip[at - 5:at - 3] == b"\xff\xc0"  # the strip's own SOF0
            want = imagecodecs.jpeg_decode(strip[:at] + size + strip[at + 4:])
        assert want.shape == (h, w, 3)
        got = g[str(level)][...]
        assert np.array_equal(got, np.moveaxis(want, -1, 0))
    assert_rebuilds(out, data, tmp_path)


def test_a_level_0_frame_with_other_mcu_rows_is_rejected():
    """§7 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#7-ndpi-profile>: a
    level 0 whose frame needs other MCU rows than its size is rejected."""
    with pytest.raises(Rejected, match="NDPI level 0's JPEG frame of 64 × 16 pixels has other MCU rows"):
        via_ir(str(FIXTURES / "ndpi" / "edge_reject_ndpi_frame.ndpi"), "u")


def test_a_tiff_is_not_ndpi():
    fmt, _, _ = via_ir(str(FIXTURES / "tiff" / "jpeg_gray.tif"), "u")
    assert fmt == "tiff"


ACCEPTED = {"ndpi_high_bytes.ndpi", "ndpi_high_pointer.ndpi", "ndpi_intervals_256x256.ndpi",
            "ndpi_loop_pointers.ndpi", "ndpi_many_ifds.ndpi", "ndpi_overlap.ndpi"}  # at the tests' sizes


def test_probes_decide_as_today(probes):
    """NDPI's hostile probes (gen_probes.py): loops, huge counts and offsets are rejected where
    today rejects them; pointer loops and overlapping extents are aliases; many intervals and
    McuStartsHighBytes are projected as today; every IR the parser returns is checked."""
    names = sorted(p.name for p in probes.glob("ndpi_*"))
    assert set(names) >= ACCEPTED
    assert len(names) == 13
    for name in names:
        path, url = probes / name, f"https://data.test/{name}"
        if name not in ACCEPTED:
            with pytest.raises(Rejected):
                virtualize(str(path), url)
            with pytest.raises(Rejected):
                via_ir(str(path), url, mirror=False)
            continue
        _, today = virtualize(str(path), url)
        fmt, out, ir = via_ir(str(path), url, mirror=False)
        assert fmt == "ndpi", path.name
        ir.check()
        same_hierarchy(today, out)
