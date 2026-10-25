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

from refkv._narrow import ndarray
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
            span, = parts
            assert isinstance(span, tuple)
            assert len(span) == 2
            offset, length = span
            strip = data[offset:offset + length]
            want = imagecodecs.jpeg_decode(strip)
        else:
            head, size, tail = parts
            assert isinstance(head, tuple)
            assert len(head) == 2
            assert isinstance(tail, tuple)
            assert len(tail) == 2
            (offset, at), (rest, length) = head, tail
            assert size == struct.pack(">HH", h, w)
            assert isinstance(size, bytes)
            assert rest == offset + at + 4
            strip = data[offset:rest + length]
            assert strip[at - 5:at - 3] == b"\xff\xc0"  # the strip's own SOF0
            want = imagecodecs.jpeg_decode(strip[:at] + size + strip[at + 4:])
        assert want.shape == (h, w, 3)
        got = ndarray(g.get_array(str(level))[...])
        assert np.array_equal(got, np.moveaxis(want, -1, 0))
    assert_rebuilds(out, data, tmp_path)


def test_a_level_0_frame_with_other_mcu_rows_is_rejected():
    """§7 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#7-ndpi-profile>: a
    level 0 whose frame needs other MCU rows than its size is rejected."""
    with pytest.raises(Rejected, match="NDPI level 0's JPEG frame of 64 × 16 pixels has other MCU rows"):
        via_ir(str(FIXTURES / "ndpi" / "edge_reject_ndpi_frame.ndpi"), "u")


JPEGXR_SLIDES = {
    # name: (data type, samples, levels' shapes and chunk shapes, label, root's source metadata,
    # the chunks of tiles of byte count 0)
    "ndpi_jpegxr_gray16.ndpi": ("uint16", 1, [([1, 100, 160], [1, 64, 64]), ([1, 50, 80], [1, 32, 32]),
                                              ([1, 25, 40], [1, 25, 40])],
                                "TxRed", {"Fluorescence": "TxRed", "Wavelength": 650.0, "ExposureTime": 1024000},
                                {"1/c/0/1/1"}),
    "ndpi_jpegxr_rgb.ndpi": ("uint8", 3, [([3, 60, 90], [3, 32, 48]), ([3, 30, 45], [3, 30, 45])], None, None,
                             set()),
    # 24bppBGR, decoding in R, G, B order too; level 0 a single tile, in line, whose high tables
    # hold its entries' high words
    "ndpi_jpegxr_bgr.ndpi": ("uint8", 3, [([3, 32, 48], [3, 32, 48]), ([3, 8, 12], [3, 8, 12])], None, None, set()),
}


def test_jpegxr_levels_are_their_streams_pixels(tmp_path):
    """§3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#3-levels>, §4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#4-the-image> (d-v-b/refkv#123):
    a JPEG XR level, tiled or in one strip, has the data type and samples of its streams' pixel
    format, not the 8-bit YCbCr its tags say; each tile (with its high bytes) or the strip is a
    chunk as it is, one of byte count 0 none; `omero` is R, G, B or the one channel labeled
    with the Fluorescence, the windows the type's range; the root's source metadata is level 0's
    acquisition tags. Read through zarr, each chunk is its stream as imagecodecs decodes it, an
    RGB slide's samples in R, G, B order (the fixtures' red is the brightest, blue the darkest),
    and the mirror rebuilds the file."""
    for name, (data_type, samples, levels, label, own, absent) in JPEGXR_SLIDES.items():
        path = FIXTURES / "ndpi" / name
        data = path.read_bytes()
        _, out, _ = via_ir(str(path), "u")
        root = json.loads(out.bytes_entries["zarr.json"])["attributes"]
        assert root["refkv_zarrified"].get("ndpi") == own, name
        top = 255 if data_type == "uint8" else 65535
        window = {"min": 0, "max": top, "start": 0, "end": top}
        names = [("R", "FF0000"), ("G", "00FF00"), ("B", "0000FF")] if samples == 3 else [(label or "C0", "FFFFFF")]
        assert root["ome"]["omero"]["channels"] == [{"label": n, "color": c, "active": True, "window": window}
                                                    for n, c in names], name
        g = zarr.open_group(ZarrStore(shipped(str(path))), mode="r", zarr_format=3)
        for li, (shape, chunk) in enumerate(levels):
            meta = json.loads(out.bytes_entries[f"{li}/zarr.json"])
            assert (meta["shape"], meta["data_type"], meta["chunk_grid"]["configuration"]["chunk_shape"]) == (
                shape, data_type, chunk), (name, li)
            codecs = [c["name"] for c in meta["codecs"]]
            assert codecs == (["transpose", "imagecodecs_jpegxr"] if samples == 3 else ["imagecodecs_jpegxr"]), name
            got = ndarray(g.get_array(str(li))[...])
            if samples == 3:
                means = got.reshape(3, -1).mean(axis=1)
                assert means[0] > means[1] > means[2], (name, li, means)
            down, across = -(-shape[1] // chunk[1]), -(-shape[2] // chunk[2])
            for row in range(down):
                for col in range(across):
                    key = f"{li}/c/0/{row}/{col}"
                    region = got[:, row * chunk[1]:(row + 1) * chunk[1], col * chunk[2]:(col + 1) * chunk[2]]
                    assert (key not in out.refs) == (key in absent), key
                    if key in absent:  # a tile of byte count 0: the fill value
                        assert not region.any(), key
                        continue
                    span, = out.refs[key]
                    assert isinstance(span, tuple)
                    assert len(span) == 2
                    offset, length = span
                    want = imagecodecs.jpegxr_decode(data[offset:offset + length])
                    want = np.moveaxis(want if want.ndim == 3 else want[..., None], -1, 0)
                    assert np.array_equal(region, want[:, :region.shape[1], :region.shape[2]]), key
        assert_rebuilds(out, data, tmp_path, f"{path.stem}.vzip")


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
