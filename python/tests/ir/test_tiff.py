"""The TIFF IR (round 3): the Rust parser and its schema, the image projection against
today's profile, the compact mirror, and the amplification probes."""

import json

import numpy as np
import pytest
import tifffile
from ir_helpers import FIXTURES, REVISED, as_the_reference, omero_and_expected_windows, same_hierarchy, zarrified
from refkv_reference import virtualize  # the frozen reference (ir_helpers)

from refkv._narrow import at, integer, mapping, ndarray, sequence, string
from refkv.ir import zarrify as via_ir
from refkv.zarrify import Rejected

TIFFS = sorted((FIXTURES / "tiff").iterdir())


@pytest.mark.parametrize("path", [p for p in TIFFS if p.name not in REVISED], ids=lambda p: p.name)
def test_projects_as_today_and_rebuilds(path, tmp_path):
    as_the_reference(path, tmp_path, "tiff")


def channels(*spec):
    return [{"label": label, "color": c, "active": True} for label, c in spec]


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
    """§4.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#44-arrays>: the
    samples, R, G, B when they decode to RGB; else the OME-XML Channels' names and colors
    (test_omero_windows_sample_the_coarsest_level checks the windows)."""
    ome = root_ome(FIXTURES / "tiff" / name)
    assert list(ome) == ["version", "multiscales", "omero"]
    assert [{k: v for k, v in c.items() if k != "window"} for c in ome["omero"]["channels"]] == omero


@pytest.mark.parametrize("path", TIFFS, ids=lambda p: p.name)
def test_omero_windows_sample_the_coarsest_level(path):
    """§4.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#44-arrays>: each
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
        assert [at(c, "window") for c in sequence(at(omero, "channels"))] == want


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
        page = tf.pages.first
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
    assert len(planes) == 100
    assert all(ir.kind(i) == 4 for i in planes)
    tiles = integer(ir.child(integer(ir.child(0, "ifds/0")), "tiles"))
    ks, starts, _lengths, _ = ir.members(tiles)
    assert len(ks) == 100
    assert set(starts) == {starts[0]}
    assert len(out.refs) == 100 * 100


def test_ifds_that_share_a_tile_table_name_it_by_alias(probes):
    _, _out, ir = via_ir(str(probes / "tiff_shared_1000_1024.tif"), "u", mirror=False)
    tiles = [integer(ir.child(integer(ir.child(0, f"ifds/{k}")), "tiles")) for k in range(1000)]
    assert ir.kind(tiles[0]) == 0
    assert all(ir.kind(t) == 4 and ir.target(t) == tiles[0] for t in tiles[1:])
    assert len(ir) < 50000  # about 40 per IFD (entries and values), none per tile: not 1000 x 1024


AXIS = {"T": "t", "C": "c", "S": "c", "Z": "z", "Y": "y", "X": "x"}
STRIPS = {
    # name: each level's shape and chunk shape, the codecs, and the padding of each level's
    # short last strip: None (no short strip), "zeros" (a zero data source) or "lzw" (a tail)
    "lzw_uint8.tif": ([([64, 64], [32, 32])], ["bytes", "refkv.tiff_lzw"], [None]),
    "strips_uint8.tif": ([([64, 64], [64, 64])], ["bytes"], [None]),  # one strip, the whole image
    "strips_uint16_short.tif": ([([70, 50], [16, 50])], ["bytes"], ["zeros"]),
    "strips_uint16_lzw_short.tif": ([([70, 50], [16, 50])], ["bytes", "refkv.tiff_lzw"], ["lzw"]),
    "strips_rgb_contig_short.tif": ([([3, 70, 50], [3, 16, 50])], ["transpose", "bytes"], ["zeros"]),
    "strips_rgb_planar_lzw.ome.tif": ([([3, 70, 50], [1, 16, 50]), ([3, 35, 25], [1, 16, 25])],
                                      ["bytes", "refkv.tiff_lzw"], ["lzw", "lzw"]),
    "strips_tcyx_uint16_lzw.ome.tif": ([([2, 3, 70, 50], [1, 1, 16, 50]), ([2, 3, 35, 25], [1, 1, 16, 25])],
                                       ["bytes", "refkv.tiff_lzw"], ["lzw", "lzw"]),
    # two levels whose last strips lack 1000 and 650 bytes: prefixes of one zero data source
    "strips_uint16_pyramid_short.ome.tif": ([([70, 50], [16, 50]), ([35, 25], [16, 25])], ["bytes"],
                                            ["zeros", "zeros"]),
}


def test_strips_and_lzw_are_the_images_pixels(tmp_path):
    """§4.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#43-data-types-and-codecs>,
    §4.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>: LZW tiles and
    strips are `refkv.tiff_lzw` chunks; a level in strips has chunks of RowsPerStrip rows by the
    image's width, one strip each, and a short last strip is padded to the chunk with a prefix of
    the output's one zero data source (uncompressed) or a literal tail of its LZW stream;
    interleaved or planar samples, the planes of an OME-TIFF and a SubIFD level alike. Each level
    reads as tifffile reads it, and the mirror rebuilds the file."""
    for name, (levels, codecs, padding) in STRIPS.items():
        zero_sources = set()
        path = FIXTURES / "tiff" / name
        out, g, _ = zarrified(path, tmp_path, "tiff")
        ms = mapping(at(g.attrs, "ome", "multiscales", 0))
        names = [string(at(a, "name")) for a in sequence(ms["axes"])]
        with tifffile.TiffFile(path) as tf:
            series = tf.series[0]
            want = [(level.asarray(), series.axes) for level in series.levels]
        assert len(want) == len(levels) == len(sequence(ms["datasets"])), name
        for li, ((shape, chunk), pad, (data, axes)) in enumerate(zip(levels, padding, want, strict=True)):
            meta = json.loads(out.bytes_entries[f"{li}/zarr.json"])
            assert meta["shape"] == shape, (name, li)
            assert meta["chunk_grid"]["configuration"]["chunk_shape"] == chunk, (name, li)
            assert [c["name"] for c in meta["codecs"]] == codecs, name
            letters = [AXIS[a] for a in axes]
            want_level = np.transpose(data, [letters.index(a) for a in names])
            assert np.array_equal(ndarray(g.get_array(str(li))[...]), want_level), (name, li)
            # the last strip's references: the strip, then its padding
            down = -(-shape[-2] // chunk[-2])
            last = [k for k in out.refs if k.startswith(f"{li}/c/") and int(k.split("/")[-2]) == down - 1]
            planes, across = int(np.prod(shape[:-2])) // int(np.prod(chunk[:-2])), -(-shape[-1] // chunk[-1])
            assert len(last) == planes * across, (name, li)  # the last row's chunks of every plane
            for key in last:
                parts = out.refs[key]
                assert len(parts) == (1 if pad is None else 2), key
                if pad == "zeros":  # (source, 0, length) of the zero data source
                    assert isinstance(parts[1], tuple), key
                    assert len(parts[1]) == 3, key
                    assert parts[1][1] == 0, key
                    zero_sources.add(parts[1][0])
                elif pad == "lzw":  # the literal tail
                    assert isinstance(parts[1], bytes), key
        assert len(zero_sources) == ("zeros" in padding), name


def strips_tiff(path, data, **kw):
    """`data` written by tifffile at `path`, in strips of 16 rows, without metadata."""
    tifffile.imwrite(path, data, rowsperstrip=16, metadata=None, photometric="minisblack", **kw)
    return path


def patch_tag(path, tag: str, *, value: int | None = None, code: int | None = None, page: int = 0):
    """Page `page`'s `tag`: its in-line value set to `value`, or its tag number to `code`."""
    with tifffile.TiffFile(path) as tf:
        p = tf.pages[page]
        assert isinstance(p, tifffile.TiffPage)
        t = p.tags[tag]
        at_value, at_entry, size = t.valueoffset, t.offset, t.valuebytecount // t.count
    b = bytearray(path.read_bytes())
    if value is not None:
        b[at_value:at_value + size] = value.to_bytes(size, "little")
    if code is not None:
        b[at_entry:at_entry + 2] = code.to_bytes(2, "little")
    path.write_bytes(b)
    return path


def test_a_short_last_strip_of_deflate_is_rejected():
    """§4.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>: a level
    in Deflate strips whose last strip is short cannot be padded, and rejects the input."""
    with pytest.raises(Rejected, match="level 0 is in strips of compression 8 whose last strip is short"):
        via_ir(str(FIXTURES / "tiff" / "edge_reject_strips_deflate_short.tif"), "u")


def delta(axis):
    return {"name": "imagecodecs_delta", "configuration": {"axis": axis, "dist": 1}}


LZW, ZLIB, ZSTD = {"name": "refkv.tiff_lzw"}, {"name": "numcodecs.zlib", "configuration": {"level": 1}}, \
    {"name": "zstd", "configuration": {"level": 0, "checksum": False}}
LE, BE, U8 = {"name": "bytes", "configuration": {"endian": "little"}}, \
    {"name": "bytes", "configuration": {"endian": "big"}}, {"name": "bytes"}
INTERLEAVED = {"name": "transpose", "configuration": {"order": [1, 2, 0]}}
PREDICTOR = {
    # name: the codecs: imagecodecs_delta along `x` in stored order, before `bytes`
    "predictor_deflate_uint8.tif": [delta(1), U8, ZLIB],
    "predictor_lzw_uint16.tif": [delta(1), LE, LZW],
    "predictor_deflate_uint16_be.tif": [delta(1), BE, ZLIB],
    "predictor_zstd_int16.tif": [delta(1), LE, ZSTD],
    "predictor_rgb_contig_lzw.tif": [INTERLEAVED, delta(1), U8, LZW],  # x before the samples
    "predictor_rgb_contig_uint16_deflate.tif": [INTERLEAVED, delta(1), LE, ZLIB],  # in strips
    "predictor_strips_uint16_lzw_short.tif": [delta(1), LE, LZW],  # its short last strip padded
    "predictor_tcyx_uint16_lzw.ome.tif": [delta(3), LE, LZW],  # and a SubIFD level
}


def test_predictor_2_is_summed_back_by_imagecodecs_delta(tmp_path):
    """§4.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#43-data-types-and-codecs>:
    Predictor 2 of an integer type, with LZW, Deflate or zstd, puts `imagecodecs_delta` along the
    stored axis `x` before `bytes`; uint8, uint16 and int16, either byte order, interleaved samples,
    tiles and strips (a short last one padded) alike. Every level reads as tifffile reads it
    (the windows: test_omero_windows_sample_the_coarsest_level)."""
    for name, codecs in PREDICTOR.items():
        path = FIXTURES / "tiff" / name
        out, g, _ = zarrified(path, tmp_path, "tiff")
        ms = mapping(at(g.attrs, "ome", "multiscales", 0))
        names = [string(at(a, "name")) for a in sequence(ms["axes"])]
        with tifffile.TiffFile(path) as tf:
            series = tf.series[0]
            want = [(level.asarray(), series.axes) for level in series.levels]
        assert len(want) == len(sequence(ms["datasets"])), name
        for li, (data, axes) in enumerate(want):
            assert json.loads(out.bytes_entries[f"{li}/zarr.json"])["codecs"] == codecs, (name, li)
            letters = [AXIS[a] for a in axes]
            want_level = np.transpose(data, [letters.index(a) for a in names])
            assert np.array_equal(ndarray(g.get_array(str(li))[...]), want_level), (name, li)


def test_predictor_3_is_rejected():
    """§4.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#43-data-types-and-codecs>:
    no codec undoes the floating-point predictor."""
    with pytest.raises(Rejected, match="unsupported predictor 3"):
        via_ir(str(FIXTURES / "tiff" / "unsupported_predictor_float.tif"), "u")


def test_predictor_2_of_a_float_type_is_rejected():
    """§4.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#43-data-types-and-codecs>:
    a float's differences are taken on its bits, which imagecodecs_delta does not sum back."""
    with pytest.raises(Rejected, match="predictor 2 with SampleFormat 3"):
        via_ir(str(FIXTURES / "tiff" / "unsupported_predictor_2_float.tif"), "u")


@pytest.mark.parametrize(("name", "compression"), [("edge_reject_predictor_uncompressed.tif", 1),
                                                    ("edge_reject_predictor_jpeg.tif", 7)])
def test_predictor_2_without_lzw_deflate_or_zstd_is_rejected(name, compression):
    """§4.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#43-data-types-and-codecs>:
    readers disagree on whether uncompressed bytes hold differences, and a JPEG stream does not
    use them."""
    with pytest.raises(Rejected, match=f"predictor 2 with compression {compression}"):
        via_ir(str(FIXTURES / "tiff" / name), "u")


def test_rows_per_strip_0_is_rejected(tmp_path):
    path = patch_tag(strips_tiff(tmp_path / "r.tif", ramp("uint8", (40, 16))), "RowsPerStrip", value=0)
    with pytest.raises(Rejected, match="has RowsPerStrip 0"):
        via_ir(str(path), "u")


def test_strips_without_byte_counts_are_rejected(tmp_path):
    path = patch_tag(strips_tiff(tmp_path / "c.tif", ramp("uint8", (40, 16))), "StripByteCounts", code=65000)
    with pytest.raises(Rejected, match="has no tag 279"):
        via_ir(str(path), "u")


def test_an_image_with_neither_tiles_nor_strips_is_rejected(tmp_path):
    path = patch_tag(strips_tiff(tmp_path / "o.tif", ramp("uint8", (40, 16))), "StripOffsets", code=65000)
    with pytest.raises(Rejected, match="has neither tiles nor strips"):
        via_ir(str(path), "u")


def test_an_lzw_last_strip_that_does_not_reach_its_size_is_rejected(tmp_path):
    """§4.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>: a short
    last LZW strip whose stream ends before the strip's size cannot be continued by a tail."""
    path = strips_tiff(tmp_path / "l.tif", ramp("uint8", (40, 16)), compression="lzw")
    with tifffile.TiffFile(path) as tf:
        counts = tf.pages.first.tags["StripByteCounts"]
        at_last = counts.valueoffset + (counts.count - 1) * (counts.valuebytecount // counts.count)
        size = counts.valuebytecount // counts.count
    b = bytearray(path.read_bytes())
    b[at_last:at_last + size] = (2).to_bytes(size, "little")
    path.write_bytes(b)
    with pytest.raises(Rejected, match=r"strip 2 of the IFD at \d+ does not decode to its size"):
        via_ir(str(path), "u")


def test_planes_in_strips_of_other_rows_are_rejected(tmp_path):
    """§4.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#42-pyramid-levels>:
    a level's planes have the same rows per strip."""
    path = tmp_path / "p.ome.tif"
    tifffile.imwrite(path, np.stack([ramp("uint8", (40, 16))] * 2), rowsperstrip=16, ome=True, metadata={"axes": "ZYX"})
    patch_tag(path, "RowsPerStrip", value=8, page=1)
    with pytest.raises(Rejected, match="planes of one pyramid level differ in size, tiling or format"):
        via_ir(str(path), "u")


def test_an_uncompressed_last_strip_written_whole_is_not_padded(tmp_path):
    """§4.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>: an
    uncompressed short last strip is padded with the zeros its byte count lacks of the chunk's
    size, so one written with all the chunk's rows has none, and the image reads as tifffile
    reads it."""
    data = ramp("uint8", (40, 16))
    path = strips_tiff(tmp_path / "w.tif", data)
    with tifffile.TiffFile(path) as tf:
        counts = tf.pages.first.tags["StripByteCounts"]
        size = counts.valuebytecount // counts.count
        at_last = counts.valueoffset + (counts.count - 1) * size
        last_offset = tf.pages.first.dataoffsets[-1]
    b = bytearray(path.read_bytes())
    assert last_offset + 8 * 16 == len(b)  # the last strip, of 8 rows, ends the file
    b += bytes([7]) * (8 * 16)  # its 16 rows, whole
    b[at_last:at_last + size] = (16 * 16).to_bytes(size, "little")
    path.write_bytes(b)
    out, g, _ = zarrified(path, tmp_path, "tiff")
    assert out.refs["0/c/2/0"] == [(last_offset, 16 * 16)]
    assert np.array_equal(ndarray(g.get_array("0")[...]), data)


def test_padding_of_more_than_2_22_bytes_is_rejected(tmp_path):
    """§4.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>: a short
    last strip that lacks more than 2^22 bytes of its chunk is not padded."""
    path = tmp_path / "big.tif"
    tifffile.imwrite(path, np.zeros((1101, 4096), np.uint8), rowsperstrip=1100, compression="lzw", metadata=None)
    with pytest.raises(Rejected, match="level 0 pads its last strip with 4501504 zero bytes, more than 4194304"):
        via_ir(str(path), "u")


def test_an_uncompressed_strip_that_lacks_more_than_2_22_bytes_is_rejected(tmp_path):
    """§4.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>: the limit
    is on the zeros a strip is padded with, `R × B − n`: a short last strip within it by its
    rows, but whose byte count is short of them, is not padded."""
    path = tmp_path / "shrunk.tif"
    tifffile.imwrite(path, np.zeros((1176, 4096), np.uint8), rowsperstrip=1100, metadata=None)  # last strip: 76 rows
    with tifffile.TiffFile(path) as tf:
        counts = tf.pages.first.tags["StripByteCounts"]
        size = counts.valuebytecount // counts.count
        at_last = counts.valueoffset + (counts.count - 1) * size
    b = bytearray(path.read_bytes())
    b[at_last:at_last + size] = (1).to_bytes(size, "little")
    path.write_bytes(b)
    with pytest.raises(Rejected, match="level 0 pads its last strip with 4505599 zero bytes, more than 4194304"):
        via_ir(str(path), "u")


def test_a_stack_of_pages_in_strips_without_ome_xml_is_rejected(tmp_path):
    """§4.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#42-pyramid-levels>: a
    plain TIFF of several pages of one size in strips (ImageJ's or tifffile's stack) has no
    mapping of its pages to planes, and is rejected rather than shown as its first page."""
    path = tmp_path / "stack.tif"
    tifffile.imwrite(path, np.stack([ramp("uint8", (40, 16))] * 3), rowsperstrip=16, metadata=None,
                     photometric="minisblack")
    with pytest.raises(Rejected, match="is another page of IFD 0's size and format"):
        via_ir(str(path), "u")


def test_later_pages_of_another_size_or_format_are_left_out(tmp_path):
    """§4.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#42-pyramid-levels>: an
    image in strips without OME-XML, followed by a smaller page (a thumbnail) and a page of its
    size in another format, is the one image of IFD 0: the later pages are not its planes."""
    data = ramp("uint8", (40, 16))
    path = tmp_path / "thumb.tif"
    with tifffile.TiffWriter(path) as tif:
        tif.write(data, rowsperstrip=16, metadata=None, photometric="minisblack")
        tif.write(data[::4, ::4], metadata=None, photometric="minisblack")
        tif.write(data.astype(np.uint16), rowsperstrip=16, metadata=None, photometric="minisblack")
    _, g, _ = zarrified(path, tmp_path, "tiff")
    assert len(sequence(at(g.attrs, "ome", "multiscales", 0, "datasets"))) == 1
    assert np.array_equal(ndarray(g.get_array("0")[...]), data)


def test_an_lzw_tail_that_would_read_more_than_2_24_bytes_is_rejected(tmp_path):
    """§4.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>: a short
    last LZW strip whose tail would read more than 2^24 of its bytes (its byte count, within
    3 × its 700 rows of 8192 bytes + 64) is not padded, though it lacks fewer zeros than the
    padding's limit; one of a few bytes is."""
    path = tmp_path / "long.tif"
    tifffile.imwrite(path, np.zeros((1900, 8192), np.uint8), rowsperstrip=1200, compression="lzw", metadata=None)
    via_ir(str(path), "u")  # the zeros' last strip is a few bytes: read whole
    with tifffile.TiffFile(path) as tf:
        counts = tf.pages.first.tags["StripByteCounts"]
        size = counts.valuebytecount // counts.count
        at_last = counts.valueoffset + (counts.count - 1) * size
        last_offset, last_count = tf.pages.first.dataoffsets[-1], tf.pages.first.databytecounts[-1]
    b = bytearray(path.read_bytes())
    assert last_offset + last_count == len(b)  # the last strip ends the file
    b += bytes(2**24)  # after its stream, so that its byte count can claim them
    b[at_last:at_last + size] = (last_count + 2**24).to_bytes(size, "little")
    path.write_bytes(b)
    with pytest.raises(Rejected, match=f"level 0's last LZW strip would be read up to {last_count + 2**24} bytes"):
        via_ir(str(path), "u")


def test_a_later_page_whose_values_do_not_load_is_not_a_page(tmp_path):
    """§4.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#42-pyramid-levels>: a
    later page whose ImageWidth is past 2^53 − 1 (a LONG8 in a BigTIFF) is not a page of IFD 0's
    size: the image in strips is accepted."""
    data = ramp("uint8", (40, 16))
    path = tmp_path / "bad.tif"
    with tifffile.TiffWriter(path, bigtiff=True) as tif:
        tif.write(data, rowsperstrip=16, metadata=None, photometric="minisblack")
        tif.write(data[::4, ::4], metadata=None, photometric="minisblack")
    with tifffile.TiffFile(path) as tf:
        page = tf.pages[1]
        assert isinstance(page, tifffile.TiffPage)
        entry = page.tags["ImageWidth"].offset
    b = bytearray(path.read_bytes())
    b[entry + 2:entry + 4] = (16).to_bytes(2, "little")  # LONG8
    b[entry + 12:entry + 20] = (2**60).to_bytes(8, "little")
    path.write_bytes(b)
    _, g, _ = zarrified(path, tmp_path, "tiff")
    assert np.array_equal(ndarray(g.get_array("0")[...]), data)


def test_lzw_tails_that_would_read_more_than_the_file_are_rejected(tmp_path):
    """§4.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>: the short
    last LZW strips of two planes, made to overlap over almost all of the file, would be read
    for their tails, together, past the file's size, which strips that do not overlap never are."""
    path = tmp_path / "overlap.ome.tif"
    tifffile.imwrite(path, np.zeros((2, 40, 4096), np.uint8), rowsperstrip=16, compression="lzw", ome=True,
                     metadata={"axes": "ZYX"})
    b = bytearray(path.read_bytes())
    for page, start in [(0, 8), (1, 9)]:  # each last strip from near the file's start to its end
        with tifffile.TiffFile(path) as tf:
            p = tf.pages[page]
            assert isinstance(p, tifffile.TiffPage)
            offsets, counts = p.tags["StripOffsets"], p.tags["StripByteCounts"]
            last = offsets.count - 1
            o_size, c_size = offsets.valuebytecount // offsets.count, counts.valuebytecount // counts.count
            at_offset, at_count = offsets.valueoffset + last * o_size, counts.valueoffset + last * c_size
        b[at_offset:at_offset + o_size] = start.to_bytes(o_size, "little")
        b[at_count:at_count + c_size] = (len(b) - start).to_bytes(c_size, "little")
    path.write_bytes(b)
    with pytest.raises(Rejected, match=f"would be read for their tails {2 * len(b) - 17} bytes, more than the file's"):
        via_ir(str(path), "u")
