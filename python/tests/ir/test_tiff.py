"""The TIFF IR (round 3): the Rust parser and its schema, the image projection against
today's profile, the compact mirror, and the amplification probes."""

import json
import struct
import zlib
from typing import Any

import imagecodecs
import numpy as np
import pytest
import tifffile
import zarr
from ir_helpers import FIXTURES, REVISED, as_the_reference, omero_and_expected_windows, same_hierarchy, zarrified
from PIL import Image
from refkv_reference import virtualize  # the frozen reference (ir_helpers)

import refkv
import refkv.zarr
from refkv._narrow import at, integer, mapping, ndarray, sequence, string
from refkv.ir import zarrify as via_ir
from refkv.policy import crc32c
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
    "strips_uint16_short.tif": ([([70, 50], [70, 50])], ["bytes"], [None]),  # its strips joined
    "strips_uint16_lzw_short.tif": ([([70, 50], [16, 50])], ["bytes", "refkv.tiff_lzw"], ["lzw"]),
    "strips_rgb_contig_short.tif": ([([3, 70, 50], [3, 70, 50])], ["transpose", "bytes"], [None]),
    "strips_rgb_planar_lzw.ome.tif": ([([3, 70, 50], [1, 16, 50]), ([3, 35, 25], [1, 16, 25])],
                                      ["bytes", "refkv.tiff_lzw"], ["lzw", "lzw"]),
    "strips_tcyx_uint16_lzw.ome.tif": ([([2, 3, 70, 50], [1, 1, 16, 50]), ([2, 3, 35, 25], [1, 1, 16, 25])],
                                       ["bytes", "refkv.tiff_lzw"], ["lzw", "lzw"]),
    "strips_uint16_pyramid_short.ome.tif": ([([70, 50], [70, 50]), ([35, 25], [35, 25])], ["bytes"],
                                            [None, None]),
}


def inner_of(meta):
    """(the chunk shape, the codecs) of a level's document: the inner chunks' when it is sharded, and a
    `rectilinear` grid's chunk lengths for its shape."""
    if meta["codecs"][0]["name"] == "sharding_indexed":
        config = meta["codecs"][0]["configuration"]
        return config["chunk_shape"], config["codecs"]
    grid = meta["chunk_grid"]["configuration"]
    return grid.get("chunk_shape", grid.get("chunk_shapes")), meta["codecs"]


def test_strips_and_lzw_are_the_images_pixels(tmp_path):
    """§4.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#43-data-types-and-codecs>,
    §4.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>: LZW tiles and
    strips are `refkv.tiff_lzw` chunks; a level in compressed strips has inner chunks of
    RowsPerStrip rows by the image's width, one strip each, and a short last strip is padded to
    the inner chunk with a literal tail of its LZW stream, while uncompressed strips join
    (test_uncompressed_strips_join_into_larger_chunks); interleaved or planar samples, the planes
    of an OME-TIFF and a SubIFD level alike. Each level reads as tifffile reads it, and the mirror
    rebuilds the file."""
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
            inner, inner_codecs = inner_of(meta)
            assert inner == chunk, (name, li)
            assert [c["name"] for c in inner_codecs] == codecs, name
            letters = [AXIS[a] for a in axes]
            want_level = np.transpose(data, [letters.index(a) for a in names])
            assert np.array_equal(ndarray(g.get_array(str(li))[...]), want_level), (name, li)
            # the last row's references: its shards' or the strip's, then its padding
            grid = meta["chunk_grid"]["configuration"]["chunk_shape"]
            down = -(-shape[-2] // grid[-2])
            last = [k for k in out.refs if k.startswith(f"{li}/c/") and int(k.split("/")[-2]) == down - 1]
            planes, across = int(np.prod(shape[:-2])) // int(np.prod(chunk[:-2])), -(-shape[-1] // chunk[-1])
            assert len(last) == planes * across, (name, li)  # the last row's chunks of every plane
            for key in last:
                parts = out.refs[key]
                if meta["codecs"][0]["name"] == "sharding_indexed":  # the strips, a tail if any, the index
                    assert isinstance(parts[-1], bytes), key
                    assert isinstance(parts[-2], bytes) == (pad == "lzw"), key
                    continue
                assert len(parts) == (1 if pad is None else 2), key
                if pad == "zeros":  # (source, 0, length) of the zero data source
                    assert isinstance(parts[1], tuple), key
                    assert len(parts[1]) == 3, key
                    assert parts[1][1] == 0, key
                    zero_sources.add(parts[1][0])
        assert len(zero_sources) == ("zeros" in padding), name


# (data, tifffile's arguments, the level's chunk shape, and the ranges of each chunk down the y axis)
ONE_ROW: dict[str, Any] = {"rowsperstrip": 1, "photometric": "minisblack"}
RGB_ONE_ROW: dict[str, Any] = {"rowsperstrip": 1, "photometric": "rgb"}
JOINED: dict[str, tuple[np.ndarray, dict[str, Any], list[int], list[int]]] = {
    "a plane within the limits is one chunk": (ramp("uint8", (40, 16)), ONE_ROW, [40, 16], [1]),
    "several rows a strip": (ramp("uint16", (70, 50)), {**ONE_ROW, "rowsperstrip": 16}, [70, 50], [1]),
    "big-endian": (ramp("uint16", (40, 16)), {**ONE_ROW, "byteorder": ">"}, [40, 16], [1]),
    "float": (ramp("float32", (40, 16)), ONE_ROW, [40, 16], [1]),
    "interleaved samples": (ramp("uint8", (40, 16, 3)), {**RGB_ONE_ROW, "planarconfig": "contig"}, [3, 40, 16], [1]),
    "planar samples, a chunk each": (ramp("uint8", (3, 40, 16)), {**RGB_ONE_ROW, "planarconfig": "separate"},
                                     [1, 40, 16],
                                     [1]),
    # 2^20 bytes hold 1048 rows, so 2000 rows are 2 even chunks of 1000
    "above the byte limit, even chunks": (ramp("uint8", (2000, 1000)), ONE_ROW, [1000, 1000], [1, 1]),
    # 2^20 bytes would hold 65536 rows, 2^10 strips only 1024
    "at the strip limit": (ramp("uint8", (4096, 16)), ONE_ROW, [1024, 16], [1, 1, 1, 1]),
    # the last chunk lacks a row, then 3 rows with its short last strip of 1 row: zero bytes after it
    "a short last chunk is padded": (ramp("uint8", (2001, 1000)), ONE_ROW, [1001, 1000], [1, 2]),
    "a short last strip in a padded chunk": (ramp("uint8", (2005, 1000)), {**ONE_ROW, "rowsperstrip": 2}, [1004, 1000],
                                             [1, 2]),
}
STAY: dict[str, tuple[np.ndarray, dict[str, Any], list[int], list[int]]] = {
    # 600000 bytes a strip: two do not fit in 2^20, and the short last strip is padded as before
    "two strips do not fit": (ramp("uint8", (1500, 1000)), {**ONE_ROW, "rowsperstrip": 600}, [600, 1000], [1, 1, 2]),
}


@pytest.mark.parametrize("case", [*JOINED, *STAY])
def test_uncompressed_strips_join_into_larger_chunks(case, tmp_path):
    """§4.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>: a level's
    uncompressed strips join into chunks of at most 2^20 bytes and 2^10 strips, as even as whole
    strips allow, one range when they meet; a level whose two strips do not fit keeps a chunk per
    strip (compressed strips: test_compressed_strips_are_the_inner_chunks_of_shards). Each level
    reads as tifffile reads it."""
    data, kw, chunk, ranges = {**JOINED, **STAY}[case]
    path = tmp_path / "s.tif"
    tifffile.imwrite(path, data, metadata=None, **kw)
    out, g, _ = zarrified(path, tmp_path, "tiff")
    meta = json.loads(out.bytes_entries["0/zarr.json"])
    assert meta["chunk_grid"]["configuration"]["chunk_shape"] == chunk
    with tifffile.TiffFile(path) as tf:
        want = tf.series[0].asarray()
    got = ndarray(g.get_array("0")[...])
    assert got.shape == want.shape or got.shape == np.moveaxis(want, -1, 0).shape
    assert np.array_equal(got, want if got.shape == want.shape else np.moveaxis(want, -1, 0))
    first_sample = "0/c/0/" if len(chunk) == 3 else "0/c/"
    assert [len(out.refs[f"{first_sample}{row}/0"]) for row in range(len(ranges))] == ranges
    if case in JOINED:
        assert np.prod(chunk) * want.dtype.itemsize <= 2**20


def test_strips_join_with_a_range_per_run_of_the_file(tmp_path):
    """§7.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#73-chunks>: strips that
    do not meet in the file are a chunk of several ranges, in the strips' order, those that meet
    merged."""
    path = FIXTURES / "tiff" / "edge_strips_apart.tif"
    out, g, _ = zarrified(path, tmp_path, "tiff")
    with tifffile.TiffFile(path) as tf:
        page = tf.pages[0]
        assert isinstance(page, tifffile.TiffPage)
        offsets = page.dataoffsets
        want = page.asarray()
    assert json.loads(out.bytes_entries["0/zarr.json"])["chunk_grid"]["configuration"]["chunk_shape"] == [8, 8]
    assert out.refs["0/c/0/0"] == [(offsets[0], 16), (offsets[1], 32), (offsets[3], 16)]
    assert np.array_equal(ndarray(g.get_array("0")[...]), want)


def test_a_level_with_an_absent_strip_stays_one_strip_a_chunk(tmp_path):
    """§4.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>: a level with
    a strip that does not hold exactly its rows stays one strip a chunk, and its absent strip is
    the fill value."""
    out, g, _ = zarrified(FIXTURES / "tiff" / "edge_strips_absent.tif", tmp_path, "tiff")
    assert json.loads(out.bytes_entries["0/zarr.json"])["chunk_grid"]["configuration"]["chunk_shape"] == [2, 8]
    assert "0/c/1/0" not in out.refs
    assert ndarray(g.get_array("0")[2:4]).max() == 0


def test_joined_levels_share_one_zero_data_source(tmp_path):
    """§4.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>: the padding of
    each level's short last chunk is a prefix of the output's one zero data source."""
    path = tmp_path / "p.tif"
    with tifffile.TiffWriter(path, ome=True) as tif:
        kw = {"rowsperstrip": 1, "photometric": "minisblack"}
        tif.write(ramp("uint8", (4005, 1000)), subifds=1, metadata={"axes": "YX"}, **kw)
        tif.write(ramp("uint8", (2001, 999)), subfiletype=1, **kw)
    out, g, _ = zarrified(path, tmp_path, "tiff")
    pads = [out.refs[key][-1] for key in ("0/c/3/0", "1/c/1/0")]
    assert all(len(p) == 3 for p in pads)
    assert [p[-1] for p in pads] == [3000, 999]
    assert pads[0][0] == pads[1][0] != 0
    assert np.array_equal(ndarray(g.get_array("1")[...]), ramp("uint8", (2001, 999)))


# (data, tifffile's arguments, the inner chunk shape, the shard shape, and the shards of a plane or
# sample plane; the inner codecs are test_predictor_2_is_summed_back_by_imagecodecs_delta's and
# test_strips_and_lzw_are_the_images_pixels's concern)
ONE_ROW_ZLIB: dict[str, Any] = {**ONE_ROW, "compression": "zlib"}
SHARDED: dict[str, tuple[np.ndarray, dict[str, Any], list[int], list[int], int]] = {
    "LZW": (ramp("uint8", (40, 16)), {**ONE_ROW, "compression": "lzw"}, [1, 16], [40, 16], 1),
    "Deflate": (ramp("uint8", (40, 16)), ONE_ROW_ZLIB, [1, 16], [40, 16], 1),
    "zstd": (ramp("uint8", (40, 16)), {**ONE_ROW, "compression": "zstd"}, [1, 16], [40, 16], 1),
    "several rows a strip": (ramp("uint16", (64, 50)), {**ONE_ROW_ZLIB, "rowsperstrip": 16}, [16, 50], [64, 50], 1),
    "big-endian": (ramp("uint16", (40, 16)), {**ONE_ROW_ZLIB, "byteorder": ">"}, [1, 16], [40, 16], 1),
    "float": (ramp("float32", (40, 16)), ONE_ROW_ZLIB, [1, 16], [40, 16], 1),
    "Predictor 2": (ramp("uint16", (40, 16)), {**ONE_ROW_ZLIB, "predictor": True}, [1, 16], [40, 16], 1),
    "LZW with Predictor 2": (ramp("uint16", (40, 16)), {**ONE_ROW, "compression": "lzw", "predictor": True}, [1, 16],
                             [40, 16], 1),
    "interleaved samples": (ramp("uint8", (40, 16, 3)),
                            {**RGB_ONE_ROW, "compression": "zlib", "planarconfig": "contig"},
                            [3, 1, 16], [3, 40, 16], 1),
    "planar samples, shards each": (ramp("uint8", (3, 40, 16)),
                                    {**RGB_ONE_ROW, "compression": "zlib", "planarconfig": "separate"},
                                    [1, 1, 16], [1, 40, 16], 1),
    # 2^10 strips a shard, and 3000 of them as 3 even shards
    "at the strip limit": (ramp("uint8", (4096, 16)), ONE_ROW_ZLIB, [1, 16], [1024, 16], 4),
    "above the strip limit, even shards": (ramp("uint8", (3000, 16)), ONE_ROW_ZLIB, [1, 16], [1000, 16], 3),
    "two strips": (ramp("uint8", (2, 16)), ONE_ROW_ZLIB, [1, 16], [2, 16], 1),
    # a shard's shape may pass the array's: 70 rows in 5 strips of 16 rows, the last short and padded
    "a short last LZW strip": (ramp("uint16", (70, 50)), {**ONE_ROW, "compression": "lzw", "rowsperstrip": 16},
                               [16, 50], [80, 50], 1),
}


@pytest.mark.parametrize("case", SHARDED)
def test_compressed_strips_are_the_inner_chunks_of_shards(case, tmp_path):
    """§4.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>,
    spec/zarrify.md §2.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#21-virtual-shards>: a
    level's LZW, Deflate and zstd strips are the inner chunks of shards, at most 2^10 strips and as
    even as whole strips allow, each shard one range where its strips meet and a literal index of
    their lengths. Each level reads as tifffile reads it, and the mirror rebuilds the file."""
    data, kw, inner, shard, shards = SHARDED[case]
    path = tmp_path / "s.tif"
    tifffile.imwrite(path, data, metadata=None, **kw)
    out, g, _ = zarrified(path, tmp_path, "tiff")
    meta = json.loads(out.bytes_entries["0/zarr.json"])
    assert meta["chunk_grid"]["configuration"]["chunk_shape"] == shard
    assert [c["name"] for c in meta["codecs"]] == ["sharding_indexed"]
    config = meta["codecs"][0]["configuration"]
    assert config["chunk_shape"] == inner
    assert config["index_location"] == "end"
    assert config["index_codecs"] == [{"name": "bytes", "configuration": {"endian": "little"}}, {"name": "crc32c"}]
    with tifffile.TiffFile(path) as tf:
        page = tf.pages[0]
        assert isinstance(page, tifffile.TiffPage)
        want = tf.series[0].asarray()
        offsets, counts = page.dataoffsets, page.databytecounts
    got = ndarray(g.get_array("0")[...])
    assert np.array_equal(got, want if got.shape == want.shape else np.moveaxis(want, -1, 0))
    planes = 3 if case.startswith("planar") else 1
    assert len([k for k in out.refs if k.startswith("0/c/")]) == shards * planes
    # the first shard: its strips one range, then their lengths in the index
    per = len(offsets) // planes // shards + (len(offsets) // planes % shards > 0)
    first = out.refs["0/c/0/0/0" if len(shard) == 3 else "0/c/0/0"]
    index = first[-1]
    assert isinstance(index, bytes)
    entries = np.frombuffer(index[:-4], dtype="<u8").reshape(-1, 2)
    assert len(entries) == shard[-2] // inner[-2]
    held = entries[:per]
    # (a padded last strip holds its tail, and not all its bytes)
    whole = per - 1 if case == "a short last LZW strip" else per
    assert held[:whole, 1].tolist() == [int(n) for n in counts[:whole]]
    assert first[0][0] == int(offsets[0])
    assert first[0][1] >= sum(int(n) for n in counts[:whole])
    assert held[:, 0].tolist() == np.cumsum([0, *held[:-1, 1]]).tolist()
    assert (entries[per:] == 2**64 - 1).all()
    assert int.from_bytes(index[-4:], "little") == crc32c(index[:-4])


def test_one_compressed_strip_is_one_chunk(tmp_path):
    """§4.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>: a level of one
    strip has nothing to gather, and is not sharded."""
    path = tmp_path / "s.tif"
    kw: dict[str, Any] = {**ONE_ROW_ZLIB, "rowsperstrip": 40}
    tifffile.imwrite(path, ramp("uint8", (40, 16)), metadata=None, **kw)
    out, g, _ = zarrified(path, tmp_path, "tiff")
    assert [c["name"] for c in json.loads(out.bytes_entries["0/zarr.json"])["codecs"]] == ["bytes", "numcodecs.zlib"]
    assert np.array_equal(ndarray(g.get_array("0")[...]), ramp("uint8", (40, 16)))


def compressed_strips(path, rows: list[bytes], order: list[int], counts: list[int | None] | None = None):
    """A little-endian uint8 TIFF of Deflate strips of 1 row, written in the file in the order `order`,
    with a gap of 3 bytes between them; each strip's count `counts[k]` in place of its stream's length
    when given (0: absent)."""
    streams = [zlib.compress(r) for r in rows]
    n = len(rows)
    tags = 9
    table = 8 + 2 + tags * 12 + 4
    data = table + 8 * n
    offsets, blob = [0] * n, bytearray()
    for k in order:
        offsets[k] = data + len(blob)
        blob += streams[k] + b"gap"
    lengths = [len(s) if counts is None or counts[k] is None else counts[k] for k, s in enumerate(streams)]
    entries = [(256, 4, 1, len(rows[0])), (257, 4, 1, n), (258, 3, 1, 8), (259, 3, 1, 8), (262, 3, 1, 1),
               (273, 4, n, table), (277, 3, 1, 1), (278, 4, 1, 1), (279, 4, n, table + 4 * n)]
    head = b"II*\0" + struct.pack("<I", 8) + struct.pack("<H", tags)
    head += b"".join(struct.pack("<HHII", *e) for e in entries) + struct.pack("<I", 0)
    path.write_bytes(head + struct.pack(f"<{n}I", *offsets) + struct.pack(f"<{n}I", *lengths) + bytes(blob))
    return offsets


def shard_index(parts) -> np.ndarray:
    """The (offset, length) entries of the literal index that ends a shard's reference `parts`."""
    index = parts[-1]
    assert isinstance(index, bytes)
    return np.frombuffer(index[:-4], dtype="<u8").reshape(-1, 2)


def test_a_shard_has_a_range_per_run_of_the_file(tmp_path):
    """spec/zarrify.md §2.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#21-virtual-shards>:
    strips that do not meet in the file, or are not in the file's order, are a range each, in the
    strips' order, and the index gives each its place in the shard, not in the file."""
    data = ramp("uint8", (4, 16))
    rows = [r.tobytes() for r in data]
    path = tmp_path / "s.tif"
    offsets = compressed_strips(path, rows, [3, 0, 2, 1])
    out, g, _ = zarrified(path, tmp_path, "tiff")
    lengths = [len(zlib.compress(r)) for r in rows]
    parts = out.refs["0/c/0/0"]
    assert parts[:-1] == [(o, n) for o, n in zip(offsets, lengths, strict=True)]
    places = np.cumsum([0, *lengths[:-1]])
    assert shard_index(parts).tolist() == [[int(a), n] for a, n in zip(places, lengths, strict=True)]
    assert np.array_equal(ndarray(g.get_array("0")[...]), data)


def test_an_absent_strip_is_an_empty_entry_of_the_shard(tmp_path):
    """§4.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#44-arrays>,
    spec/zarrify.md §2.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#21-virtual-shards>: a
    strip of no bytes is an inner chunk the shard does not hold, and reads as the fill value; a
    shard that holds no strip has no entry."""
    data = ramp("uint8", (4, 16))
    path = tmp_path / "s.tif"
    compressed_strips(path, [r.tobytes() for r in data], [0, 1, 2, 3], [None, 0, None, None])
    out, g, _ = zarrified(path, tmp_path, "tiff")
    assert (shard_index(out.refs["0/c/0/0"])[1] == 2**64 - 1).all()
    want = data.copy()
    want[1] = 0
    assert np.array_equal(ndarray(g.get_array("0")[...]), want)

    path = tmp_path / "none.tif"
    compressed_strips(path, [r.tobytes() for r in data], [0, 1, 2, 3], [0, 0, 0, 0])
    out, g, _ = zarrified(path, tmp_path, "tiff")
    assert not [k for k in out.refs if k.startswith("0/c/")]
    assert np.array_equal(ndarray(g.get_array("0")[...]), np.zeros((4, 16), "uint8"))


@pytest.mark.parametrize(("region", "requests", "strips"), [
    ((Ellipsis,), 1, range(40)),  # the whole shard: one value, one range
    ((7,), 1, range(7, 8)),  # one row: its strip alone
    ((slice(8, 12),), 1, range(8, 12)),  # the strips of a crop meet: one read
])
def test_a_read_through_a_shard_reads_its_strips_alone(region, requests, strips, tmp_path):
    """§4.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>: a reader of a
    few rows reads the shard's literal index, which takes no request, and those strips' bytes."""
    data = ramp("uint8", (40, 16))
    path = tmp_path / "s.tif"
    tifffile.imwrite(path, data, metadata=None, **ONE_ROW_ZLIB)
    with tifffile.TiffFile(path) as tf:
        page = tf.pages[0]
        assert isinstance(page, tifffile.TiffPage)
        counts = page.databytecounts
    archive = tmp_path / "s.vzip"
    _, out, _ = via_ir(str(path), None)
    out.to_store().save_vzip(archive)
    stats = refkv.Stats()
    policy = refkv.Policy(allow_file_hosts=True)
    a = zarr.open_array(refkv.zarr.open(archive, stats=stats, policy=policy), path="0", mode="r")
    assert np.array_equal(ndarray(a[region]), data[region])
    assert stats.external_requests == requests
    assert stats.external_bytes == sum(int(counts[k]) for k in strips)


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


RECTILINEAR = {
    # name: each level's chunk lengths along `y`, or the integer of a regular grid's chunk shape
    "strips_uint16_deflate_short.tif": [[[16, 4], 6]],
    "bilevel_packbits_strips_short.tif": [[[16, 2], 8]],
    "bilevel_g4_strips_short.tif": [[[16, 2], 8]],
    "strips_uint16_zstd_short.tif": [[[16, 4], 6]],
    "strips_uint16_deflate_be_predictor_short.tif": [[[16, 4], 6]],
    "strips_rgb_contig_jpeg_short.tif": [[[16, 4], 6]],
    "strips_rgb_contig_deflate_short.tif": [[16, 8]],  # two strips, no run
    "strips_rgb_planar_jpeg2000_short.tif": [[[16, 4], 6]],
    "strips_tcyx_uint16_deflate_short.ome.tif": [[[16, 4], 6], [[16, 2], 3]],
    "strips_uint8_deflate_short_level.ome.tif": [[32, 16], 24],  # a level of one strip stays regular
}


def test_a_short_last_strip_that_cannot_be_padded_has_a_rectilinear_grid(tmp_path):
    """§4.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>: a level whose
    last strip is short and not uncompressed or LZW has the `rectilinear` grid, a run of full strips
    and the short one, a chunk a strip, unsharded. Every level reads as tifffile reads it (the
    windows: test_omero_windows_sample_the_coarsest_level)."""
    for name, levels in RECTILINEAR.items():
        path = FIXTURES / "tiff" / name
        out, g, _ = zarrified(path, tmp_path, "tiff")
        ms = mapping(at(g.attrs, "ome", "multiscales", 0))
        names = [string(at(a, "name")) for a in sequence(ms["axes"])]
        with tifffile.TiffFile(path) as tf:
            series = tf.series[0]
            want = [(level.asarray(), series.axes) for level in series.levels]
        assert len(want) == len(levels), name
        for li, ((data, axes), y) in enumerate(zip(want, levels, strict=True)):
            meta = json.loads(out.bytes_entries[f"{li}/zarr.json"])
            assert all(string(at(c, "name")) != "sharding_indexed" for c in sequence(meta["codecs"])), (name, li)
            grid = mapping(meta["chunk_grid"])
            if isinstance(y, int):
                assert grid["name"] == "regular", (name, li)
                assert at(grid, "configuration", "chunk_shape", names.index("y")) == y, (name, li)
            else:
                assert grid["name"] == "rectilinear", (name, li)
                assert at(grid, "configuration", "chunk_shapes", names.index("y")) == y, (name, li)
            letters = [AXIS[a] for a in axes]
            want_level = np.transpose(data, [letters.index(a) for a in names])
            got = ndarray(g.get_array(str(li))[...])
            # JPEG and JPEG 2000 are as tifffile decodes the same streams
            assert np.array_equal(got, want_level), (name, li)


def test_the_short_last_strips_of_common_writers_read_back(tmp_path):
    """§4.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>: what tifffile's
    and Pillow's defaults write with zlib and zstd, which leave a short last strip at these heights."""
    data = ramp("uint8", (1000, 1000))
    for name, compression in (("zlib", "zlib"), ("zstd", "zstd")):
        path = tmp_path / f"{name}.tif"
        tifffile.imwrite(path, data, compression=compression, metadata=None, photometric="minisblack")
        _, g, _ = zarrified(path, tmp_path, "tiff")
        assert np.array_equal(ndarray(g.get_array("0")[...]), data), name
    path = tmp_path / "pillow.tif"
    Image.fromarray(data).save(path, compression="tiff_adobe_deflate")
    with tifffile.TiffFile(path) as tf:
        page = tf.pages.first
        assert page.rowsperstrip < 1000
        assert 1000 % page.rowsperstrip != 0
    _, g, _ = zarrified(path, tmp_path, "tiff")
    assert np.array_equal(ndarray(g.get_array("0")[...]), data)


def test_a_read_of_part_of_a_rectilinear_level_decodes_its_short_chunk(tmp_path):
    """§4.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>: the short chunk
    decodes to its own rows, alone and across its neighbor."""
    path = FIXTURES / "tiff" / "strips_uint16_deflate_short.tif"
    _, g, _ = zarrified(path, tmp_path, "tiff")
    a = g.get_array("0")
    with tifffile.TiffFile(path) as tf:
        want = tf.asarray()
    assert np.array_equal(ndarray(a[64:70, 3:9]), want[64:70, 3:9])  # the short chunk alone
    assert np.array_equal(ndarray(a[60:66, :]), want[60:66, :])  # across the last two


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
            assert inner_of(json.loads(out.bytes_entries[f"{li}/zarr.json"]))[1] == codecs, (name, li)
            letters = [AXIS[a] for a in axes]
            want_level = np.transpose(data, [letters.index(a) for a in names])
            assert np.array_equal(ndarray(g.get_array(str(li))[...]), want_level), (name, li)


def test_predictor_3_is_rejected():
    """§4.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#43-data-types-and-codecs>:
    no codec undoes the floating-point predictor."""
    with pytest.raises(Rejected, match="unsupported predictor 3"):
        via_ir(str(FIXTURES / "tiff" / "unsupported_predictor_float.tif"), "u")


def packints(bits, runlen):
    return {"name": "imagecodecs_packints", "configuration": {"bitspersample": bits, "runlen": runlen}}


PACKBITS, G4 = {"name": "imagecodecs_packbits"}, {"name": "imagecodecs_ccittfax4"}
PACKED = {
    # name: the data type and the codecs of level 0's chunks. tifffile's bilevel files have no tag 258.
    "bilevel.tif": ("uint8", [packints(1, 45)]),
    "bilevel_tiled.tif": ("uint8", [packints(1, 16)]),  # a tile's row, padded to a whole byte
    "bilevel_deflate.tif": ("uint8", [packints(1, 45), ZLIB]),
    "bilevel_packbits.tif": ("uint8", [packints(1, 45), PACKBITS]),
    "bilevel_strips_short.tif": ("uint8", [packints(1, 45)]),  # joined
    "bilevel_lzw_strips_short.tif": ("uint8", [packints(1, 45), LZW]),  # sharded, a tail on its last strip
    "bilevel_g4.tif": ("uint8", [G4]),
    "bilevel_g4_strips_no_bits.tif": ("uint8", [G4]),
    "bilevel_packbits_strips_short.tif": ("uint8", [packints(1, 45), PACKBITS]),
    "bilevel_g4_strips_short.tif": ("uint8", [G4]),
    "uint12_strips.tif": ("uint16", [packints(12, 51)]),
    "uint12_strips_short.tif": ("uint16", [packints(12, 51)]),
    "uint12_be.tif": ("uint16", [packints(12, 51)]),  # packed bits have one order, whatever the file's
    "uint12_rgb_contig.tif": ("uint16", [INTERLEAVED, packints(12, 153)]),  # a row of 3 samples a pixel
    "uint12_deflate_strips.tif": ("uint16", [packints(12, 51), ZLIB]),
    "uint12_lzw.tif": ("uint16", [packints(12, 51), LZW]),
    "packbits_uint8.tif": ("uint8", [U8, PACKBITS]),
    "packbits_uint16_be.tif": ("uint16", [BE, PACKBITS]),
    "packbits_rgb_contig.tif": ("uint8", [INTERLEAVED, U8, PACKBITS]),
}


def test_packed_samples_packbits_and_ccitt_read_as_tifffile_reads_them(tmp_path):
    """§4.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#43-data-types-and-codecs>: 1-
    and 12-bit samples are `imagecodecs_packints` in the place of `bytes`, of `uint8` and `uint16`,
    `runlen` the samples of a row; a file without tag 258 is 1-bit, as TIFF 6.0 says; PackBits and CCITT
    Group 4 are their imagecodecs codecs. Each reads as tifffile reads it, 1 bit as 0 and 1."""
    for name, (data_type, codecs) in PACKED.items():
        path = FIXTURES / "tiff" / name
        out, g, _ = zarrified(path, tmp_path, "tiff")
        meta = json.loads(out.bytes_entries["0/zarr.json"])
        assert meta["data_type"] == data_type, name
        assert inner_of(meta)[1] == codecs, name
        ms = mapping(at(g.attrs, "ome", "multiscales", 0))
        names = [string(at(a, "name")) for a in sequence(ms["axes"])]
        with tifffile.TiffFile(path) as tf:
            series = tf.series[0]
            data, axes = series.asarray(), series.axes
        letters = [AXIS[a] for a in axes]
        want = np.transpose(data, [letters.index(a) for a in names]).astype(data_type)
        assert np.array_equal(ndarray(g.get_array("0")[...]), want), name


def test_the_window_of_packed_samples_is_their_own_range():
    """§4.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#44-arrays>: a `uint8` of 1 bit
    or a `uint16` of 12 would show dark in its type's range."""
    for name, top in (("bilevel.tif", 1), ("uint12_strips.tif", 4095)):
        channel = sequence(at(root_ome(FIXTURES / "tiff" / name), "omero", "channels"))[0]
        assert at(channel, "window") == {"min": 0, "max": top, "start": 0, "end": top}, name


@pytest.mark.parametrize(("name", "bits"), [("eer_v0_frame.tif", (8, 2, 2)), ("eer_v1_frames.tif", (7, 2, 2)),
                                            ("eer_v2_frames.tif", (8, 3, 1))])
def test_eer_frames_are_the_t_axis(name, bits, tmp_path):
    """§4.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#41-planes>, §4.3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#43-data-types-and-codecs>: an EER file's
    IFDs are frames along `t`, `imagecodecs_eer` the codec, its bits the compression's or, of 65002, the
    tags'. tifffile reads EER only from an acquisition's metadata, so each frame is checked against
    imagecodecs' decoding of that strip."""
    path = FIXTURES / "tiff" / name
    out, g, _ = zarrified(path, tmp_path, "tiff")
    with tifffile.TiffFile(path) as tf:
        pages = [p for p in tf.pages if isinstance(p, tifffile.TiffPage)]
        want = []
        for p in pages:
            tf.filehandle.seek(p.dataoffsets[0])
            stream = tf.filehandle.read(p.databytecounts[0])
            want.append(imagecodecs.eer_decode(stream, (32, 48), *bits).astype("uint8"))
    meta = json.loads(out.bytes_entries["0/zarr.json"])
    skip, horz, vert = bits
    assert sequence(meta["codecs"]) == [{"name": "imagecodecs_eer", "configuration": {
        "shape": [32, 48], "skipbits": skip, "horzbits": horz, "vertbits": vert}}]
    assert meta["data_type"] == "uint8"
    got = ndarray(g.get_array("0")[...])
    assert np.array_equal(got, np.stack(want) if len(want) > 1 else want[0])


@pytest.mark.parametrize(("name", "message"), [
    ("edge_reject_bits_10.tif", "unsupported sample type: 10-bit, SampleFormat 1"),
    ("edge_reject_bits_12_int.tif", "unsupported sample type: 12-bit, SampleFormat 2"),
    ("edge_reject_bits_1_jpeg2000.tif", "unsupported compression 34712 of 1-bit samples"),
    ("edge_reject_bits_12_predictor.tif", "predictor 2 with 12-bit samples"),
    ("edge_reject_ccitt_uint8.tif", "unsupported CCITT: 8-bit, 1 samples"),
    ("edge_reject_ccitt_fill_order.tif", "the image at 16 has FillOrder 2"),
    ("edge_reject_ccitt_group3.tif", "unsupported compression 3"),
    ("edge_reject_eer_uint8.tif", "unsupported EER: 8-bit, 1 samples"),
    ("edge_reject_eer_short.tif", "the image at 20 is in EER strips whose last is short"),
    ("edge_reject_eer_bits_differ.tif", "the image at 174 has other EER bits than IFD 0"),
    ("edge_reject_bilevel_pages.tif", "is another page of IFD 0's size and format"),
])
def test_what_packed_samples_and_their_codecs_do_not_hold_is_rejected(name, message):
    """§4.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#43-data-types-and-codecs>; the
    stack of bilevel pages, without tag 258, §4.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#42-pyramid-levels>."""
    with pytest.raises(Rejected, match=message):
        via_ir(str(FIXTURES / "tiff" / name), "u")


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
