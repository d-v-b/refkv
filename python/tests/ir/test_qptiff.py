"""QPTIFF on the IR (spec/zarrify/qptiff.md): the Rust TIFF parser's QPTIFF variant and the
QPTIFF projection on the synthetic fixtures: every accepted file is a valid IR that rebuilds its
source, its levels read through zarr equal what tifffile decodes, its layout (channels, levels in
tiles and strips, omero, scale and position) is the convention's, and each input the profile
rejects is rejected for the convention's reason. QPTIFF has no frozen reference twin: tifffile is
the independent reader."""

import json
import logging
import re
from typing import cast

import imagecodecs
import numpy as np
import pytest
import tifffile
import zarr
from ir_helpers import FIXTURES, assert_rebuilds, omero_and_expected_windows

from refkv._narrow import at, mapping, ndarray, sequence
from refkv.ir import zarrify as via_ir
from refkv.zarr import ZarrStore
from refkv.zarrify import Rejected, zarrify

logging.getLogger("tifffile").setLevel(logging.ERROR)  # its warnings on the rejected fixtures

QPTIFFS = sorted((FIXTURES / "qptiff").glob("qptiff_*.qptiff"))
REJECTED = {
    "channel_count": "a QPTIFF with 3 channels has 2 ReducedResolution images, not a multiple of 3",
    "late_full": "a QPTIFF FullResolution image follows the baseline images",
    "level_grows": "QPTIFF level 1 is not smaller than level 0",
    "level_mismatch": "the images of QPTIFF level 1 differ in size, tiling or format",
    "no_description": "IFD 0 of a QPTIFF has no PerkinElmer-QPI image description",
    "not_baseline": "IFD 0 of a QPTIFF is not a FullResolution image",
    "predictor_float": "unsupported predictor 3",
    "rgb_channels": "QPTIFF channels have 3 samples per pixel",
    "strip_count": "IFD at 8 has 3 tiles or strips, expected 5",
}


def docs(out) -> dict:
    return {k: json.loads(v) for k, v in out.bytes_entries.items()
            if k.endswith("zarr.json") and not k.startswith("refkv_source/")}


def tifffile_levels(path) -> list[np.ndarray]:
    """Each level's pixels as tifffile decodes the pages, grouped by the ImageType of their
    descriptions (the convention §3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#3-images-and-channels>), as
    `[c, y, x]` arrays: the channels stacked, or an RGB image's samples first. A level in
    strips whose last strip is short and whose compression is neither none nor LZW is not a
    level (§4.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#42-tiles-and-strips>)."""
    with tifffile.TiffFile(path) as tf:
        pages = [p for p in tf.pages if isinstance(p, tifffile.TiffPage)]
        assert len(pages) == len(tf.pages)  # pages, not frames (tifffile's default)
        kinds = [(re.search(r"<ImageType>([^<]*)</ImageType>", p.description or "") or [None, None])[1]
                 for p in pages]
        full = [i for i, k in enumerate(kinds) if k == "FullResolution"]
        reduced = [i for i, k in enumerate(kinds) if k == "ReducedResolution"]
        n = len(full)
        out = []
        for ks in [full] + [reduced[i:i + n] for i in range(0, len(reduced), n)]:
            page = pages[ks[0]]
            if not page.is_tiled and page.compression not in (1, 5):
                rows = min(page.tags[278].value if 278 in page.tags else page.imagelength, page.imagelength)
                if page.imagelength > rows and page.imagelength % rows:
                    continue
            a = np.stack([tf.pages[k].asarray() for k in ks])
            # an RGB image's samples first; one gray channel has no channel axis
            out.append(np.moveaxis(a[0], -1, 0) if a.ndim == 4 else a[0] if n == 1 else a)
        return out


@pytest.mark.parametrize("path", QPTIFFS, ids=lambda p: p.name)
def test_accepted_fixtures_are_valid_irs_that_rebuild(path, tmp_path):
    data = path.read_bytes()
    fmt, out, ir = via_ir(str(path), f"https://data.test/{path.name}")
    assert fmt == "qptiff"
    ir.check()
    root = docs(out)["zarr.json"]["attributes"]["refkv_zarrified"]
    assert root["profile"] == "qptiff"
    assert root["qptiff"] == {"byte_order": "little", "bigtiff": "bigtiff" in path.name}
    assert_rebuilds(out, data, tmp_path)


@pytest.mark.parametrize("path", QPTIFFS, ids=lambda p: p.name)
def test_levels_read_as_tifffile_decodes_them(path):
    """Every level, read through zarr-python (the LZW codec, JPEG strips, the rectilinear grid
    of a short last strip), equals the channels' pages as tifffile decodes them."""
    zarr.config.set({"array.rectilinear_chunks": True})
    store = zarrify(str(path))
    assert store.info["format"] == "qptiff"
    g = zarr.open_group(ZarrStore(store), mode="r", zarr_format=3)
    want = tifffile_levels(path)
    assert [at(d, "path") for d in sequence(at(g.attrs, "ome", "multiscales", 0, "datasets"))] == [
        str(i) for i in range(len(want))]
    for i, level in enumerate(want):
        got = ndarray(g.get_array(str(i))[...])
        assert got.dtype == level.dtype, (path.name, i)
        assert got.shape == level.shape, (path.name, i)
        assert np.array_equal(got, level), (path.name, i)


def test_layout_of_the_fluorescence_scan():
    """The convention §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#3-images-and-channels>
    and §4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#4-the-image> on
    qptiff_fl_uint8_lzw.qptiff: three LZW channels with names (an entity decoded, a ScanProfile's
    Name not taken) and colors in omero, the pixel size from the resolution tags, the position
    from XPosition and YPosition, two tiled levels and one in strips of 16 rows whose last strip
    is 8 rows, padded to the chunk with a literal tail spliced into its LZW stream; the
    thumbnail, overview and label are not levels."""
    _, out, _ = via_ir(str(FIXTURES / "qptiff" / "qptiff_fl_uint8_lzw.qptiff"), "u")
    d = docs(out)
    ome = d["zarr.json"]["attributes"]["ome"]
    ms = ome["multiscales"][0]
    assert ms["name"] == "Fixture-Slide"
    assert [a["name"] for a in ms["axes"]] == ["c", "y", "x"]
    assert [a.get("unit") for a in ms["axes"]] == [None, "micrometer", "micrometer"]
    scales = [ds["coordinateTransformations"][0]["scale"] for ds in ms["datasets"]]
    assert scales[0] == [1, 10000 / 20080, 10000 / 20080]
    assert scales[1][1] == 2 * scales[0][1]
    assert all(ds["coordinateTransformations"][1] == {"type": "translation", "translation": [0, 484400.0, 88600.0]}
               for ds in ms["datasets"])
    assert [{k: v for k, v in c.items() if k != "window"} for c in ome["omero"]["channels"]] == [
        {"label": "DAPI", "color": "0000FF", "active": True},
        {"label": "Cy3 & TRITC", "color": "FF3C9D", "active": True},
        {"label": "Opal 520", "color": "00FF00", "active": True},
    ]  # their windows sampled (test_omero_windows_sample_the_coarsest_level)
    assert d["0/zarr.json"]["shape"] == [3, 96, 128]
    assert d["0/zarr.json"]["chunk_grid"] == {"name": "regular", "configuration": {"chunk_shape": [1, 32, 32]}}
    assert d["0/zarr.json"]["codecs"] == [{"name": "bytes"}, {"name": "refkv.tiff_lzw"}]
    assert d["2/zarr.json"]["shape"] == [3, 24, 32]
    assert d["2/zarr.json"]["chunk_grid"] == {"name": "regular", "configuration": {"chunk_shape": [1, 16, 32]}}
    chunks = sorted(k for k in out.refs if not k.startswith("refkv_source/"))
    assert len(chunks) == 3 * (12 + 4 + 2)
    assert chunks[-1] == "2/c/2/1/0"
    assert len(out.refs["0/c/1/2/3"]) == 1
    assert len(out.refs["0/c/1/2/3"][0]) == 2  # one range of source 0
    first, second = out.refs["2/c/0/0/0"], out.refs["2/c/0/1/0"]
    assert len(first) == 1
    assert len(second) == 2
    assert isinstance(second[1], bytes)  # the strip's prefix, then the tail
    assert second[0][0] == first[0][0] + first[0][1]  # the last strip follows the first
    assert len(out.data) == 0  # no JPEG, no zeros: no data sources


def test_layout_of_an_rgb_scan_and_of_component_data():
    """An RGB brightfield scan is one channel of 3 interleaved samples (transpose, chunk c of 3,
    omero per sample with a shared JPEG prefix), and its JPEG level in strips whose last strip
    is short is not a level; a short last strip of an uncompressed level is padded with a zero
    data source; inForm's float32 component data in LZW strips has no pyramid."""
    _, out, _ = via_ir(str(FIXTURES / "qptiff" / "qptiff_bf_rgb_jpeg.qptiff"), "u")
    d = docs(out)
    assert d["0/zarr.json"]["codecs"] == [{"name": "transpose", "configuration": {"order": [1, 2, 0]}},
                                          {"name": "imagecodecs_jpeg"}]
    assert d["0/zarr.json"]["chunk_grid"]["configuration"]["chunk_shape"] == [3, 32, 32]
    labels = [c["label"] for c in d["zarr.json"]["attributes"]["ome"]["omero"]["channels"]]
    assert labels == ["BrightField R", "BrightField G", "BrightField B"]
    assert len(out.data) == 1
    assert out.data
    assert len(out.refs["0/c/0/0/0"]) == 3  # SOI, the Adobe marker, the rest
    assert "2/zarr.json" not in d
    assert not any(k.startswith("2/") for k in out.refs)  # 24 rows in JPEG strips of 16
    assert out.summary["levels"] == [[3, 96, 128], [3, 48, 64]]

    _, out, _ = via_ir(str(FIXTURES / "qptiff" / "qptiff_bf_rgb_uncompressed.qptiff"), "u")
    d = docs(out)
    assert d["1/zarr.json"]["chunk_grid"] == {"name": "regular", "configuration": {"chunk_shape": [3, 24, 32]}}
    assert sorted(k for k in out.refs if k.startswith("1/")) == ["1/c/0/0/0", "1/c/0/1/0"]
    whole, last = out.refs["1/c/0/0/0"], out.refs["1/c/0/1/0"]
    assert len(whole) == 1
    assert len(last) == 2
    assert last[0][1] == 8 * 32 * 3
    assert last[1] == (1, 0, 16 * 32 * 3)
    assert list(out.data) == [bytes(16 * 32 * 3)]  # the zeros, one data source

    _, out, _ = via_ir(str(FIXTURES / "qptiff" / "qptiff_component_float32_strips.qptiff"), "u")
    d = docs(out)
    assert d["0/zarr.json"]["data_type"] == "float32"
    assert "1/zarr.json" not in d
    assert d["0/zarr.json"]["chunk_grid"] == {"name": "regular", "configuration": {"chunk_shape": [1, 16, 48]}}
    assert len(out.refs["0/c/1/2/0"]) == 2
    assert isinstance(out.refs["0/c/1/2/0"][1], bytes)
    assert d["zarr.json"]["attributes"]["ome"]["multiscales"][0]["name"] == "HnE_3"
    channels = d["zarr.json"]["attributes"]["ome"]["omero"]["channels"]
    assert [{k: v for k, v in c.items() if k != "window"} for c in channels] == [
        {"label": "Eosin", "color": "FF3C9D", "active": True},
        {"label": "Hematoxylin", "color": "32459D", "active": True}]


@pytest.mark.parametrize("path", QPTIFFS, ids=lambda p: p.name)
def test_omero_windows_sample_the_coarsest_level(path):
    """§4.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#43-arrays>: each
    channel's window is the one numpy computes from the coarsest level read through zarr-python
    (ir_helpers.omero_and_expected_windows), and a float channel without one has none."""
    omero, want = omero_and_expected_windows(path, "qptiff")
    assert [mapping(c).get("window") for c in sequence(at(omero, "channels"))] == want


def test_windows_of_rgb_samples_are_the_type_range():
    """The uncompressed RGB scan's samples, which the core decodes, keep the type's range: a
    window per sample would tint them."""
    _, out, _ = via_ir(str(FIXTURES / "qptiff" / "qptiff_bf_rgb_uncompressed.qptiff"), "u", mirror=False)
    full = {"min": 0, "max": 255, "start": 0, "end": 255}
    assert [c["window"] for c in docs(out)["zarr.json"]["attributes"]["ome"]["omero"]["channels"]] == [full] * 3


def broken(tmp_path, name: str, key: str):
    """A copy of the fixture `name` whose chunk `key` (one range of the source, not a padded
    last strip) is overwritten with 0xFF bytes, which no LZW stream starts with: the IR
    output of the copy."""
    path = FIXTURES / "qptiff" / name
    _, out, _ = via_ir(str(path), "u", mirror=False)
    part, = out.refs[key]
    assert isinstance(part, tuple)
    assert len(part) == 2
    offset, length = part
    b = bytearray(path.read_bytes())
    b[offset:offset + length] = b"\xff" * length
    (tmp_path / name).write_bytes(b)
    _, out, _ = via_ir(str(tmp_path / name), "u", mirror=False)
    return [c.get("window") for c in docs(out)["zarr.json"]["attributes"]["ome"]["omero"]["channels"]]


def test_a_chunk_that_does_not_decode_gives_the_type_range(tmp_path):
    """Channel 1's sampled strip of the coarsest level (level 2, 24 rows in strips of 16: strip 0)
    not decoding gives that channel uint8's range; the others are sampled."""
    windows = broken(tmp_path, "qptiff_fl_uint8_lzw.qptiff", "2/c/1/0/0")
    assert windows[1] == {"min": 0, "max": 255, "start": 0, "end": 255}
    assert windows[0]["start"] > 0
    assert windows[2]["start"] > 0


def test_a_float_chunk_that_does_not_decode_gives_no_window(tmp_path):
    """Channel 0's sampled strip of the float32 component data (40 rows in strips of 16: strip 1)
    not decoding leaves that channel without a window, which a float type has no range for."""
    windows = broken(tmp_path, "qptiff_component_float32_strips.qptiff", "0/c/0/1/0")
    assert windows[0] is None
    assert windows[1] is not None


def test_unknown_and_undescribed_images_are_ignored_and_96_dpi_is_no_pixel_size():
    """qptiff_fl_uint16_lzw_bigtiff.qptiff: an IFD of ImageType `Macro` and one without a
    description are kept in the mirror only; 96 dots per inch gives no unit and no translation."""
    _, out, ir = via_ir(str(FIXTURES / "qptiff" / "qptiff_fl_uint16_lzw_bigtiff.qptiff"), "u")
    d = docs(out)
    ms = d["zarr.json"]["attributes"]["ome"]["multiscales"][0]
    assert "name" not in ms
    assert [a.get("unit") for a in ms["axes"]] == [None, None, None]
    assert ms["datasets"][1]["coordinateTransformations"] == [{"type": "scale", "scale": [1, 2.0, 2.0]}]
    assert out.summary["levels"] == [[2, 64, 80], [2, 32, 40]]
    paths = {ir.path(i) for i in range(len(ir))}
    assert "ifds/5/strips" in paths
    assert "ifds/6/strips" in paths  # the ignored images' strips


def test_a_stream_past_its_tile_decodes_to_the_tile():
    """§4.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#41-data-types-and-codecs>:
    the first tile of qptiff_fl_lzw_narrow_eoi.qptiff ends with its end code one bit too narrow,
    as some Vectra scanners write (code 514 to a reader), so a whole-stream decode gives 1026
    bytes for a 1024-byte tile; the codec decodes into the chunk's size and gives the tile."""
    path = FIXTURES / "qptiff" / "qptiff_fl_lzw_narrow_eoi.qptiff"
    _, out, _ = via_ir(str(path), "u")
    part, = out.refs["0/c/0/0"]
    assert isinstance(part, tuple)
    assert len(part) == 2
    offset, length = part
    stream = path.read_bytes()[offset:offset + length]
    assert len(imagecodecs.lzw_decode(stream)) == 1026
    # cast: imagecodecs' stub omits the numpy array `out` the codec takes
    assert len(imagecodecs.lzw_decode(stream, out=cast("bytearray", np.empty(1024, np.uint8)))) == 1024
    # test_levels_read_as_tifffile_decodes_them reads it through zarr and compares with tifffile


@pytest.mark.parametrize("name", sorted(REJECTED))
def test_rejects(name):
    path = FIXTURES / "qptiff" / f"edge_reject_qptiff_{name}.qptiff"
    with pytest.raises(Rejected) as e:
        via_ir(str(path), "u")
    assert str(e.value).startswith(REJECTED[name])


def test_a_tiff_and_an_ndpi_are_not_qptiff():
    for file in ("tiff/jpeg_gray.tif", "ndpi/ndpi_levels.ndpi"):
        fmt, _, _ = via_ir(str(FIXTURES / file), "u")
        assert fmt != "qptiff"


def test_padding_of_more_than_2_22_bytes_is_rejected(tmp_path):
    """§4.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#42-tiles-and-strips>:
    the TIFF convention's limit on the padding of a short last strip applies to QPTIFF too."""
    path = tmp_path / "big.qptiff"
    desc = "<PerkinElmer-QPI-ImageDescription><ImageType>FullResolution</ImageType></PerkinElmer-QPI-ImageDescription>"
    tifffile.imwrite(path, np.zeros((1101, 4096), np.uint8), rowsperstrip=1100, compression="lzw", metadata=None,
                     software="PerkinElmer-QPI", description=desc)
    with pytest.raises(Rejected, match="level 0 pads its last strip with 4501504 zero bytes, more than 4194304"):
        via_ir(str(path), "u")


def test_a_level_that_cannot_be_padded_is_left_out_whatever_it_lacks(tmp_path):
    """§4.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#42-tiles-and-strips>:
    a reduced level in Deflate strips whose last strip is short is left out, however many bytes
    it lacks of its chunk (the limit on the padding is on strips that are padded)."""
    path = tmp_path / "dropped.qptiff"

    def desc(kind):
        return f"<PerkinElmer-QPI-ImageDescription><ImageType>{kind}</ImageType></PerkinElmer-QPI-ImageDescription>"

    with tifffile.TiffWriter(path, bigtiff=True) as tif:
        tif.write(np.zeros((2304, 8192), np.uint8), tile=(512, 512), compression="zlib", metadata=None,
                  software="PerkinElmer-QPI", description=desc("FullResolution"))
        tif.write(np.zeros((1101, 4096), np.uint8), rowsperstrip=1100, compression="zlib", metadata=None,
                  software="PerkinElmer-QPI", description=desc("ReducedResolution"))
    _, out, _ = via_ir(str(path), "u")
    assert json.loads(out.bytes_entries["zarr.json"])["attributes"]["ome"]["multiscales"][0]["datasets"] == [
        {"path": "0", "coordinateTransformations": [{"type": "scale", "scale": [1.0, 1.0]}]}]


def test_an_lzw_tail_that_would_read_more_than_2_24_bytes_is_rejected(tmp_path):
    """§4.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#42-tiles-and-strips>:
    the TIFF convention's limit on the bytes of a short last LZW strip read for its tail applies to
    QPTIFF too."""
    path = tmp_path / "long.qptiff"
    desc = "<PerkinElmer-QPI-ImageDescription><ImageType>FullResolution</ImageType></PerkinElmer-QPI-ImageDescription>"
    tifffile.imwrite(path, np.zeros((1900, 8192), np.uint8), rowsperstrip=1200, compression="lzw", metadata=None,
                     software="PerkinElmer-QPI", description=desc)
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
