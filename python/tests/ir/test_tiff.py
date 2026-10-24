"""The TIFF IR (round 3): the Rust parser and its schema, the image projection against
today's profile, the compact mirror, and the amplification probes."""

import json

import numpy as np
import pytest
import tifffile

from ir_helpers import FIXTURES, as_the_reference, omero_and_expected_windows, same_hierarchy
from refkv.ir import virtualize as via_ir
from refkv.virtualize import Rejected
from refkv_reference import virtualize  # the frozen reference (ir_helpers)

TIFFS = sorted((FIXTURES / "tiff").iterdir())


@pytest.mark.parametrize("path", TIFFS, ids=lambda p: p.name)
def test_projects_as_today_and_rebuilds(path, tmp_path):
    as_the_reference(path, tmp_path, "tiff")


def channels(*spec):
    return [{"label": l, "color": c, "active": True} for l, c in spec]


RGB = channels(("R", "FF0000"), ("G", "00FF00"), ("B", "0000FF"))


def root_ome(path) -> dict:
    _, out, _ = via_ir(str(path), "u", mirror=False)
    return json.loads(out.bytes_entries["zarr.json"])["attributes"]["ome"]


@pytest.mark.parametrize(("name", "omero"), [
    ("jpeg_aperio_rgb.tif", RGB),  # an Aperio SVS's JPEG tiles, RGB
    ("jpeg_ycbcr.tif", RGB),  # JPEG decodes YCbCr to RGB
    ("rgb_contig_zstd.ome.tif", RGB),
    ("edge_ycbcr_unsubsampled.tif", channels(("S0", "FFFFFF"), ("S1", "FFFFFF"), ("S2", "FFFFFF"))),
    ("jpeg_gray.tif", channels(("C0", "FFFFFF"))),
    ("tczyx_uint16_deflate.ome.tif", channels(("C0", "FFFFFF"), ("C1", "FFFFFF"))),
    ("svs_like_int16.tif", channels(("C0", "FFFFFF"))),
    ("float32_uncompressed_be.ome.tif", channels(("C0", "FFFFFF"))),  # its window sampled
    ("edge_ome_channels.tif", channels(("DAPI", "0000FF"), ("C1", "00FF00"), ("<GFP>", "FF0000"), ("C3", "FFFFFF"),
                                       ("C4", "FFFFFF"))),
])
def test_omero(name, omero):
    """§4.4 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/tiff.md#44-arrays>: the
    samples, R, G, B when they decode to RGB; else the OME-XML Channels' names and colors
    (test_omero_windows_sample_the_coarsest_level checks the windows)."""
    ome = root_ome(FIXTURES / "tiff" / name)
    assert list(ome) == ["version", "multiscales", "omero"]
    assert [{k: v for k, v in c.items() if k != "window"} for c in ome["omero"]["channels"]] == omero


@pytest.mark.parametrize("path", TIFFS, ids=lambda p: p.name)
def test_omero_windows_sample_the_coarsest_level(path):
    """§4.4 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/tiff.md#44-arrays>: each
    channel's window is the one numpy computes from the coarsest level read through zarr-python
    (ir_helpers.omero_and_expected_windows), and an image with a channel of a float type that
    has none has no omero."""
    try:
        omero, want = omero_and_expected_windows(path, "tiff")
    except Rejected:
        return
    if None in want:
        assert omero is None
    else:
        assert [c["window"] for c in omero["channels"]] == want


def tiled(path, data, **kw):
    """`data` written by tifffile at `path`, in 16 × 16 tiles, without metadata."""
    tifffile.imwrite(path, data, tile=(16, 16), metadata=None, **kw)
    return path


def ramp(dtype, shape=(32, 32)):
    return (np.arange(np.prod(shape)) % 200 + 1).astype(dtype).reshape(shape)


def tile_at(path, k: int) -> tuple[int, int, int, int]:
    """Tile `k` of page 0: its offset and byte count, and where its TileByteCounts value is
    and how long."""
    with tifffile.TiffFile(path) as tf:
        page = tf.pages[0]
        counts = page.tags["TileByteCounts"]
        size = counts.valuebytecount // counts.count
        return page.dataoffsets[k], page.databytecounts[k], counts.valueoffset + k * size, size


TYPE_RANGE = {"uint16": {"min": 0, "max": 65535, "start": 0, "end": 65535}}


def test_windows_of_a_codec_the_core_does_not_decode_are_the_type_range():
    """JPEG tiles are not decoded: a gray JPEG image's window is uint8's range."""
    ome = root_ome(FIXTURES / "tiff" / "jpeg_gray.tif")
    assert ome["omero"]["channels"][0]["window"] == {"min": 0, "max": 255, "start": 0, "end": 255}


def test_windows_of_rgb_samples_are_the_type_range(tmp_path):
    """Uncompressed RGB samples, which the core decodes, keep the type's range: a window per
    sample would tint them."""
    path = tiled(tmp_path / "rgb.tif", np.stack([ramp("uint8")] * 3, axis=-1), photometric="rgb")
    full = {"min": 0, "max": 255, "start": 0, "end": 255}
    assert [c["window"] for c in root_ome(path)["omero"]["channels"]] == [full] * 3


@pytest.mark.parametrize(("dtype", "omero"), [("uint16", True), ("float32", False)])
def test_an_absent_chunk_gives_the_type_range_or_no_omero(tmp_path, dtype, omero):
    """The middle tile (tile 3 of 2 × 2) with a byte count of 0 is absent: an integer channel
    has the type's range, and an image of a float type no omero."""
    path = tiled(tmp_path / "a.tif", ramp(dtype))
    *_, at, size = tile_at(path, 3)
    b = bytearray(path.read_bytes())
    b[at:at + size] = bytes(size)
    path.write_bytes(b)
    ome = root_ome(path)
    assert ome.get("omero", {}).get("channels", [{}])[0].get("window") == (TYPE_RANGE[dtype] if omero else None)


def test_a_chunk_that_does_not_decode_gives_the_type_range(tmp_path):
    """A Deflate tile that is not a zlib stream does not decode: the type's range."""
    path = tiled(tmp_path / "d.tif", ramp("uint16"), compression="zlib")
    offset, length, *_ = tile_at(path, 3)
    b = bytearray(path.read_bytes())
    b[offset:offset + length] = b"\xff" * length
    path.write_bytes(b)
    assert root_ome(path)["omero"]["channels"][0]["window"] == TYPE_RANGE["uint16"]


@pytest.mark.parametrize("value", [0, 7])
@pytest.mark.parametrize("dtype", ["uint16", "float32"])
def test_a_chunk_without_contrast_gives_the_type_range_or_no_omero(tmp_path, dtype, value):
    """A middle tile of only the fill value 0, or of one value (its percentiles equal), gives no
    window: an integer channel has the type's range, and an image of a float type no omero."""
    data = ramp(dtype)
    data[16:, 16:] = value
    ome = root_ome(tiled(tmp_path / "c.tif", data))
    assert ome.get("omero", {}).get("channels", [{}])[0].get("window") == TYPE_RANGE.get(dtype)


def test_probes_decide_as_today(probes):
    """Loops, huge counts and too many IFDs are rejected where today rejects them;
    pointer loops are aliases; every IR the parser returns is checked."""
    for path in sorted(probes.glob("tiff_*")):
        url = f"https://data.test/{path.name}"
        try:
            _, today = virtualize(str(path), url)
        except Rejected:
            with pytest.raises(Rejected):
                via_ir(str(path), url, mirror=False)
            continue
        _, out, ir = via_ir(str(path), url, mirror=False)
        ir.check()
        same_hierarchy(today, out)


def kinds(ir, prefix):
    return [ir.kind(i) for i in range(len(ir)) if ir.path(i).startswith(prefix)]


def test_planes_and_tiles_that_share_bytes_are_aliases(probes):
    _, out, ir = via_ir(str(probes / "tiff_amp_100_100.tif"), "u", mirror=False)
    planes = [i for i in range(len(ir)) if ir.path(i).startswith("ome/planes")]
    assert len(planes) == 100 and all(ir.kind(i) == 4 for i in planes)
    tiles = ir.child(ir.child(0, "ifds/0"), "tiles")
    ks, starts, lengths, _ = ir.members(tiles)
    assert len(ks) == 100 and set(starts) == {starts[0]}
    assert len(out.refs) == 100 * 100


def test_ifds_that_share_a_tile_table_name_it_by_alias(probes):
    _, out, ir = via_ir(str(probes / "tiff_shared_1000_1024.tif"), "u", mirror=False)
    tiles = [ir.child(ir.child(0, f"ifds/{k}"), "tiles") for k in range(1000)]
    assert ir.kind(tiles[0]) == 0 and all(ir.kind(t) == 4 and ir.target(t) == tiles[0] for t in tiles[1:])
    assert len(ir) < 50000  # about 40 per IFD (entries and values), none per tile: not 1000 x 1024
