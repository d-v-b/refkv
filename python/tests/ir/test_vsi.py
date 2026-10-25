"""The VSI IR: the Rust parser and its schema, the image projection (spec/zarrify/vsi.md)
against the synthetic slides' known pixels (fixtures/vsi/expected, the arrays the generator
encoded into the tile files) and, when a JVM and Bio-Formats are at hand, against Bio-Formats
(bioio-bioformats), the independent reader; the mirror's rebuild of every file; the inputs the
profile rejects; and the host's side: the tile files probed beside the `.vsi` file, from a local
directory and over HTTP, where a missing file is a 404. VSI has no frozen Python reference.

Bio-Formats decodes JPEG 2000 tiles with its own decoder, whose rounding differs from OpenJPEG's
(imagecodecs') by one gray level on about 1.5% of the pixels of the public fluorescence sample,
so JPEG 2000 arrays are compared within 1; raw and JPEG arrays must be equal."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import numpy as np
import pytest
import zarr
from ir_helpers import FIXTURES, ROOT

import refkv.zarr
from refkv import _core
from refkv._narrow import JSON, at, integer, mapping, ndarray, sequence, string
from refkv.ir.store import StoreIrOutput
from refkv.policy import Policy
from refkv.uri import path_reference
from refkv.zarrify import Rejected
from refkv.zarrify import _zarrify as zarrify
from refkv.zarrify.store import object_url
from refkv.zarrify.vsi import VsiFiles, split_input

sys.path.insert(0, str(ROOT / "conformance" / "zarrify"))
from proxy import Proxy

VSI = FIXTURES / "vsi"
SLIDES = sorted(p for p in VSI.glob("*.vsi") if not p.name.startswith("vsi_reject"))
REJECTED = sorted(p for p in VSI.glob("vsi_reject_*.vsi"))
URL = "https://data.test/slides/{}"


def expected_array(name: str, stack: int) -> np.ndarray | None:
    """The generator's level-0 array of a stack, in TCZYX(S) order."""
    p = VSI / "expected" / name / f"{stack}.npy"
    return np.load(p) if p.exists() else None


def as_tczyx(arr: np.ndarray, axes: JSON, samples: JSON) -> np.ndarray:
    """Our array (its axes a subset of t, c, z, y, x, c being the samples when the file has
    several) in TCZYX(S) order, the samples last; `axes` and `samples` as the summary has them."""
    names = [string(a) for a in sequence(axes)]
    if integer(samples) > 1:
        arr = np.moveaxis(arr, names.index("c"), -1)
        names.remove("c")
    for ax in ("t", "c", "z"):
        if ax not in names:
            arr = np.expand_dims(arr, {"t": 0, "c": 1, "z": 2}[ax])
    return arr


def zarrified(path: Path, url: str | None = None) -> tuple[StoreIrOutput, zarr.Group]:
    fmt, out = zarrify(str(path), url=url)
    assert fmt == "vsi"
    assert isinstance(out, StoreIrOutput)
    return out, zarr.open_group(refkv.zarr.store(out.to_store()), mode="r", zarr_format=3)


@pytest.mark.parametrize("path", SLIDES, ids=lambda p: p.name)
def test_slides_match_their_known_pixels_and_rebuild(path, tmp_path):
    """Every stack's level 0 is the array the generator encoded (missing tiles the background),
    read back through zarr-python; the root is a bioformats2raw layout whose series are the
    stacks with pixels then the macro image; every file of the input is rebuilt from the mirror
    alone (the files are described in place: from their own bytes, nothing held)."""
    out, root = zarrified(path)
    assert root.attrs["ome"] == {"version": "0.5", "bioformats2raw.layout": 3}
    images = [mapping(im) for im in sequence(out.summary["images"])]
    assert at(root["OME"].attrs, "ome", "series") == [str(k) for k in range(len(images))]
    assert images[-1]["name"] == "macro image"
    checked = 0
    for k, im in enumerate(images):
        g = root.get_group(str(k))
        ms = mapping(at(g.attrs, "ome", "multiscales", 0))
        assert ms["name"] == im["name"]
        assert len(sequence(ms["datasets"])) == len(sequence(im["levels"]))
        if im["kind"] != "stack" or im.get("id") is None:
            continue
        want = expected_array(path.stem, integer(im["id"]))
        if want is None:
            continue
        got = as_tczyx(ndarray(g.get_array("0")[...]), im["axes"], im["samples"])
        assert got.shape == want.shape, im["name"]
        assert got.dtype == want.dtype, im["name"]
        assert np.array_equal(got, want), im["name"]
        checked += 1
    assert checked == len([im for im in images if im["source"] == "tiles"])
    # the rebuild: every object from its own bytes, through the mirror's table
    rebuilt = _core.rebuild_store(out.entries.get)
    files = {k: path.parent / k for k, _, _ in rebuilt}
    assert set(files) == set(out.sizes)
    assert path.name in files
    for key, size, held in rebuilt:
        assert held is None, key  # described in place: nothing held
        assert size == files[key].stat().st_size, key
    out.write(str(tmp_path / "a.vzip"))


def test_the_rgb_jpeg_slide_layout():
    """The brightfield-like fixture (spec/zarrify/vsi.md §4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#4-the-images>): levels sized as
    Bio-Formats sizes them, chunks the tiles, a missing tile absent (the fill value its
    background), the focus map from an IFD, the macro from its JPEG strip with a shared prefix,
    scales in micrometers and a translation from the origin."""
    out, root = zarrified(VSI / "vsi_rgb_jpeg.vsi", URL.format("vsi_rgb_jpeg.vsi"))
    assert at(root.attrs, "refkv_zarrified", "source") == {"url": URL.format("vsi_rgb_jpeg.vsi")}
    names = [at(root[str(k)].attrs, "ome", "multiscales", 0, "name") for k in range(4)]
    assert names == ["Overview", "20x_BF_01", "20x FocusMap", "macro image"]
    a = root.get_array("1/0")
    assert (a.shape, a.chunks, a.dtype, a.fill_value) == ((3, 200, 300), (3, 64, 64), np.uint8, 238)
    assert [c["name"] for c in json.loads(out.entries["1/0/zarr.json"])["codecs"]] == ["transpose", "imagecodecs_jpeg"]
    assert [root.get_array(f"1/{lv}").shape for lv in range(3)] == [(3, 200, 300), (3, 100, 150), (3, 50, 75)]
    assert "1/0/c/0/0/0" in out.refs
    assert "1/0/c/0/1/2" not in out.refs
    assert np.all(a[:, 64:128, 128:192] == 238)  # the missing tile
    assert root.get_array("2/0").shape == (5, 6)
    assert root.get_array("2/0").dtype == np.uint16
    macro = out.refs["3/0/c/0/0/0"]
    assert len(macro) == 3
    assert macro[0][:2] == ("object", "vsi_rgb_jpeg.vsi")
    assert macro[1][0] == 1
    assert len(out.data) == 1
    ms = at(root["1"].attrs, "ome", "multiscales", 0)
    assert at(ms, "axes", 1) == {"name": "y", "type": "space", "unit": "micrometer"}
    assert at(ms, "datasets", 1, "coordinateTransformations", 0, "scale") == [1, 0.27 * 2, 0.27 * 2]
    assert at(ms, "datasets", 0, "coordinateTransformations", 1, "translation") == [0, 4600.0, 37000.0]
    channels = [mapping(c) for c in sequence(at(root["1"].attrs, "ome", "omero", "channels"))]
    assert [c["label"] for c in channels] == ["R", "G", "B"]
    assert channels[0]["window"] == {"min": 0, "max": 255, "start": 0, "end": 255}
    own = mapping(at(root["1"].attrs, "refkv_zarrified", "vsi"))
    assert own["tile_file"] == "_vsi_rgb_jpeg_/stack10002/frame_t.ets"
    assert own["type_name"] == "Default image"
    # the source table: one url source per file some reference uses, sizes pinned
    urls, _data, _ = out.table()
    assert urls == [object_url(URL.format(""), k) for k in ("_vsi_rgb_jpeg_/stack10000/frame_t.ets",
                                                            "_vsi_rgb_jpeg_/stack10002/frame_t.ets",
                                                            "vsi_rgb_jpeg.vsi")]
    assert out.sizes["vsi_rgb_jpeg.vsi"] == (VSI / "vsi_rgb_jpeg.vsi").stat().st_size
    assert out.summary["tileFiles"] == 2
    assert out.summary["tiles"] == 37 - 2
    assert out.summary["chunks"] == 37


def test_the_multidimensional_slide():
    """T, Z and C from the tile coordinates and the tree's dimension blocks; the channel names
    (the dye, not the optical path), colors from the LUTs and windows from the display limits;
    the z step in micrometers; the 12-bit background as the fill value; `frame_t_0.ets`."""
    out, root = zarrified(VSI / "vsi_raw_tzc.vsi")
    a = root.get_array("0/0")
    assert (a.shape, a.dtype, a.fill_value) == ((2, 2, 3, 30, 40), np.uint16, 4095)
    assert json.loads(out.entries["0/0/zarr.json"])["dimension_names"] == ["t", "c", "z", "y", "x"]
    ms = mapping(at(root["0"].attrs, "ome", "multiscales", 0))
    assert [mapping(x)["name"] for x in sequence(ms["axes"])] == ["t", "c", "z", "y", "x"]
    assert at(ms, "datasets", 0, "coordinateTransformations", 0, "scale") == [1, 1, 0.2, 0.065, 0.065]
    channels = [mapping(c) for c in sequence(at(root["0"].attrs, "ome", "omero", "channels"))]
    assert [(c["label"], c["color"]) for c in channels] == [("Cy5", "FF0000"), ("GFP", "00FF00")]
    assert channels[0]["window"] == {"min": 0, "max": 65535, "start": 88, "end": 169}
    own = mapping(at(root["0"].attrs, "refkv_zarrified", "vsi"))
    assert own["tile_file"] == "_vsi_raw_tzc_/stack1/frame_t_0.ets"
    assert [mapping(c)["channel_name"] for c in sequence(own["channels"])] == ["488/640_50um_1x"] * 2
    assert own["z_unit_name"] == "micrometer" if "z_unit_name" in own else own["z_unit"] == "10^-6m^1"


def test_the_root_metadata():
    """The root's source metadata: the file, the stacks, and the tree as JSON (spec/zarrify/vsi.md
    §6.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#61-the-root>): tags named
    as Bio-Formats names them, texts decoded from UTF-16, a blob over 1024 bytes as its length."""
    _out, root = zarrified(VSI / "vsi_metadata_edges.vsi")
    own = mapping(at(root.attrs, "refkv_zarrified", "vsi"))
    assert set(own) == {"file", "stacks", "tree"}
    assert at(own, "file", "key") == "vsi_metadata_edges.vsi"
    assert at(own, "stacks", 0, "name") == "µm édge"
    top = sequence(at(own, "tree", "fields"))
    doc = mapping(top[0])
    assert doc["tag"] == 2
    assert doc["type"] == 1
    assert at(doc, "fields", 0) == {"tag": 11, "type": 8192, "name": "Document Name", "value": "vsi_metadata_edges"}
    coll = mapping(top[1])
    assert (coll["tag"], coll["tag2"], coll["name"]) == (2000, 0, "Collection volume")
    stack = next(f for f in map(mapping, sequence(coll["fields"])) if f["tag"] == 2001)
    props = next(f for f in map(mapping, sequence(stack["fields"])) if f["tag"] == 2005)
    by_tag = {f["tag"]: f for f in map(mapping, sequence(props["fields"]))}
    assert by_tag[2024]["value"] == {"bytes": 1280}
    assert by_tag[175307]["value"] == "Défaut"
    assert by_tag[2020] == {"tag": 2020, "type": 13, "name": "Calibration units", "value": "10^-3m^1"}
    assert by_tag[2074] == {"tag": 2074, "type": 5, "name": "Image Type", "inline": True, "value": 4096}
    ifd = next(f for f in map(mapping, sequence(coll["fields"])) if f["tag"] == 2016)
    assert at(ifd, "ifd", "index") == 0
    assert integer(at(ifd, "ifd", "bytes")) > 0
    ms = at(root["0"].attrs, "ome", "multiscales", 0)
    assert at(ms, "axes", 0, "unit") == "millimeter"
    assert len(sequence(at(ms, "datasets", 0, "coordinateTransformations"))) == 1
    ome = mapping(at(root["0"].attrs, "ome"))
    assert "omero" not in ome or at(ome, "omero", "channels", 0, "window", "start") == 0
    assert at(root["0"].attrs, "refkv_zarrified", "vsi", "type_name") is None


def test_raw_pixel_types():
    out, root = zarrified(VSI / "vsi_raw_types.vsi")
    images = [mapping(im) for im in sequence(out.summary["images"])]
    types = [im["dataType"] for im in images if im["kind"] == "stack"]
    assert types == ["int8", "int16", "int32", "uint32", "int64", "uint64", "float32", "float64"]
    for k, im in enumerate(images[:-1]):
        assert root.get_array(f"{k}/0").dtype == np.dtype(string(im["dataType"])), im["name"]
        want = expected_array("vsi_raw_types", integer(im["id"]))
        assert want is not None, im["name"]
        assert np.array_equal(ndarray(root.get_array(f"{k}/0")[...]), want[0, 0, 0]), im["name"]


def test_the_second_tile_file_name_is_read_when_the_first_is_absent_and_the_first_wins():
    """`frame_t.ets`, else `frame_t_0.ets` (spec/zarrify/vsi.md §9.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#92-the-tile-files>): the
    Zenodo-like fixture has the second alone; the metadata fixture has both, and the second
    (which is not an ETS file) is never read."""
    out, _ = zarrified(VSI / "vsi_raw_tzc.vsi")
    assert set(out.sizes) == {"vsi_raw_tzc.vsi", "_vsi_raw_tzc_/stack1/frame_t_0.ets"}
    out, _ = zarrified(VSI / "vsi_metadata_edges.vsi")
    assert set(out.sizes) == {"vsi_metadata_edges.vsi", "_vsi_metadata_edges_/stack10002/frame_t.ets"}


@pytest.mark.parametrize("path", REJECTED, ids=lambda p: p.name)
def test_rejects(path):
    """Every `vsi_reject_*` fixture is rejected (rust/refkv-ir/tests/vsi.rs checks each message)."""
    with pytest.raises(Rejected):
        zarrify(str(path), url=URL.format(path.name))


def test_a_url_that_does_not_name_the_file_is_rejected():
    with pytest.raises(Rejected, match=re.escape("does not name the .vsi file")):
        zarrify(str(VSI / "vsi_raw_tzc.vsi"), url=URL.format("other.vsi"))
    with pytest.raises(Rejected, match="has a query or a fragment"):
        zarrify(str(VSI / "vsi_raw_tzc.vsi"), url="https://data.test/vsi_raw_tzc.vsi?x")


def test_split_input():
    assert split_input("https://h/a/b%20c.vsi") == ("https://h/a/", "b c.vsi")
    assert split_input("dir/x.vsi") == ("dir", "x.vsi")
    assert split_input("x.vsi") == ("", "x.vsi")
    with pytest.raises(Rejected):
        split_input("https://h/a/x.vsi?q")


def test_a_local_input_without_a_url_names_its_files_by_path(tmp_path):
    """The store URL of a local `.vsi` is its directory's path reference, so the archive's
    sources are the files' paths, which the reader opens."""
    out, _root = zarrified(VSI / "vsi_raw_bgr.vsi")
    urls = out.table()[0]
    d = path_reference(str(VSI))  # the path itself on POSIX, a file: URI on Windows
    d = d if d.endswith("/") else d + "/"
    assert urls == [object_url(d, "_vsi_raw_bgr_/stack10000/frame_t.ets"), object_url(d, "vsi_raw_bgr.vsi")]
    out.write(str(tmp_path / "a.vzip"))
    g = zarr.open_group(refkv.zarr.open(str(tmp_path / "a.vzip")), mode="r", zarr_format=3)
    want = expected_array("vsi_raw_bgr", 10000)
    assert want is not None
    assert np.array_equal(np.moveaxis(ndarray(g.get_array("0/0")[...]), 0, -1), want[0, 0, 0])
    # a BGR file's samples are B, G, R
    channels = sequence(at(g["0"].attrs, "ome", "omero", "channels"))
    assert [mapping(c)["label"] for c in channels] == ["B", "G", "R"]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """The fixtures served over HTTP by the harness proxy (ranges, and a 404 for a file that is
    not there, as a plain web server gives them): the directory's URL, ending in `/`."""
    return Proxy(VSI, tmp_path_factory.mktemp("cache")).base + "f/"


def test_over_http_the_tile_files_are_probed_and_a_404_is_absent(server, tmp_path):
    """The host opens each candidate file at the input's directory; a 404 is a file that does
    not exist (`frame_t.ets` for the Zenodo-like fixture), so the second name is read."""
    policy = Policy(allow_private_hosts=True)
    fmt, out = zarrify(server + "vsi_raw_tzc.vsi", policy=policy)
    assert fmt == "vsi"
    assert isinstance(out, StoreIrOutput)
    assert set(out.sizes) == {"vsi_raw_tzc.vsi", "_vsi_raw_tzc_/stack1/frame_t_0.ets"}
    assert out.url == server
    assert out.summary["listingRequests"] == 1  # one file opened besides the .vsi
    out.write(str(tmp_path / "a.vzip"))
    g = zarr.open_group(refkv.zarr.open(str(tmp_path / "a.vzip"), policy=policy), mode="r", zarr_format=3)
    assert at(g.attrs, "refkv_zarrified", "source") == {"url": server + "vsi_raw_tzc.vsi"}
    want = expected_array("vsi_raw_tzc", 1)
    assert want is not None
    assert np.array_equal(ndarray(g.get_array("0/0")[...]), want)
    # a missing tile file over HTTP is a rejection, as it is locally
    with pytest.raises(Rejected, match="has no tile file"):
        zarrify(server + "vsi_reject_missing_tile_file.vsi", policy=policy)


def test_the_files_store(server):
    s = VsiFiles(server + "vsi_raw_bgr.vsi", policy=Policy(allow_private_hosts=True))
    assert s.probe("nope.ets") is None
    ets = "_vsi_raw_bgr_/stack10000/frame_t.ets"
    assert s.probe(ets) == (VSI / ets).stat().st_size
    assert s.read_range("vsi_raw_bgr.vsi", 0, 4) == b"II*\0"
    assert s.etag("vsi_raw_bgr.vsi") is None or isinstance(s.etag("vsi_raw_bgr.vsi"), str)


# ---------------------------------------------------------------- Bio-Formats

def bioformats():
    """bioio-bioformats' reader, when it can start its JVM (it installs one; it needs Maven to
    fetch Bio-Formats), else None."""
    if os.environ.get("REFKV_NO_BIOFORMATS"):
        return None
    try:
        from bioio_bioformats import Reader  # noqa: PLC0415 (optional, and it starts a JVM)

        Reader(str(VSI / "vsi_no_stacks.vsi"))
    except Exception:  # noqa: BLE001 - no JVM, no Maven, no network: not a failure of the profile
        return None
    return Reader


@pytest.mark.parametrize("name", ["vsi_rgb_jpeg", "vsi_raw_tzc", "vsi_jpeg2000_c", "vsi_raw_bgr",
                                  "vsi_raw_gray_pyramid"])
def test_matches_bioformats(name):
    """Every level of every stack Bio-Formats reads from the synthetic slides is our array
    (within 1 for JPEG 2000, whose decoders round differently), and so is the macro image;
    the scenes are our images in order (Bio-Formats skips the focus maps); the channel names
    and physical sizes agree."""
    Reader = bioformats()
    if Reader is None:
        pytest.skip("Bio-Formats is not available (bioio-bioformats needs a JVM and Maven)")
    out, root = zarrified(VSI / f"{name}.vsi")
    r = Reader(str(VSI / f"{name}.vsi"))
    images = [mapping(im) for im in sequence(out.summary["images"])]
    ours = [(k, im) for k, im in enumerate(images) if im["source"] == "tiles" or im["kind"] == "macro"]
    scenes = r.scenes
    assert scenes is not None
    assert len(scenes) == len(ours)
    for (k, im), scene in zip(ours, scenes, strict=True):
        r.set_scene(scene)
        r.set_resolution_level(0)  # the level stays set across scenes
        assert scene.lower() == string(im["name"]).lower()
        rgb = "S" in str(r.dims)
        order = "TCZYXS" if rgb else "TCZYX"
        tolerance = 1 if im.get("codec") == "jpeg2000" else 0
        bgr = at(root[str(k)].attrs, "ome", "omero", "channels", 0, "label") == "B"
        for level in r.resolution_levels:
            r.set_resolution_level(level)
            want = r.get_image_data(order)
            got = as_tczyx(ndarray(root.get_array(f"{k}/{level}")[...]), im["axes"], im["samples"])
            if bgr:
                got = got[..., ::-1]  # stored blue first; Bio-Formats reorders to RGB
            assert got.shape == want.shape, (scene, level)
            diff = np.abs(got.astype(np.int64) - want.astype(np.int64)).max()
            assert diff <= tolerance, (scene, level, diff)
        if im["kind"] == "stack" and im["samples"] == 1 and integer(at(im, "sizes", "c")) > 1:
            own = at(root[str(k)].attrs, "refkv_zarrified", "vsi", "channels")
            names = r.channel_names
            assert names is not None
            assert list(names) == [mapping(c)["channel_name"] for c in sequence(own)]
        if im["kind"] == "stack":
            ms = mapping(at(root[str(k)].attrs, "ome", "multiscales", 0))
            r.set_resolution_level(0)
            px = r.physical_pixel_sizes
            scale = sequence(at(ms, "datasets", 0, "coordinateTransformations", 0, "scale"))
            axes = [mapping(a)["name"] for a in sequence(ms["axes"])]
            if px.X is not None:
                assert scale[axes.index("x")] == pytest.approx(px.X)
                assert scale[axes.index("y")] == pytest.approx(px.Y)
            if px.Z is not None and "z" in axes:
                assert scale[axes.index("z")] == pytest.approx(px.Z)


@pytest.mark.skipif(not os.environ.get("REFKV_VSI_SAMPLES"),
                    reason="set REFKV_VSI_SAMPLES to a directory of public .vsi samples")
def test_public_samples_match_bioformats():
    """A public sample (its `.vsi` and sidecar directory under REFKV_VSI_SAMPLES, such as the
    OME CellSens samples) against Bio-Formats: every plane of every level, raw and JPEG exactly,
    JPEG 2000 within 1; at level 0 of a slide over 4 megapixels, a crop."""
    Reader = bioformats()
    if Reader is None:
        pytest.skip("Bio-Formats is not available")
    for path in sorted(Path(os.environ["REFKV_VSI_SAMPLES"]).rglob("*.vsi")):
        if not (path.parent / f"_{path.stem}_").is_dir():
            continue  # the .vsi file alone, without its tile files
        out, root = zarrified(path)
        r = Reader(str(path))
        ours = [(k, im) for k, im in enumerate(map(mapping, sequence(out.summary["images"])))
                if im["source"] == "tiles" or im["kind"] == "macro"]
        scenes = r.scenes
        assert scenes is not None, path.name
        assert len(scenes) == len(ours), path.name
        for (k, im), scene in zip(ours, scenes, strict=True):
            r.set_scene(scene)
            r.set_resolution_level(0)
            rgb = "S" in str(r.dims)
            order = "TCZYXS" if rgb else "TCZYX"
            tolerance = 1 if im.get("codec") == "jpeg2000" else 0
            bgr = at(root[str(k)].attrs, "ome", "omero", "channels", 0, "label") == "B"
            for level in r.resolution_levels:
                r.set_resolution_level(level)
                w, h = map(integer, sequence(at(im, "levels", level)))
                if w * h > 4_000_000:
                    ys, xs = slice(h // 2 - 256, h // 2 + 256), slice(w // 2 - 256, w // 2 + 256)
                    want = np.asarray(r.get_image_dask_data(order)[:, :, :, ys, xs])
                    a = root.get_array(f"{k}/{level}")
                    got = as_tczyx(ndarray(a[(slice(None),) * (a.ndim - 2) + (ys, xs)]), im["axes"], im["samples"])
                else:
                    want = r.get_image_data(order)
                    got = as_tczyx(ndarray(root.get_array(f"{k}/{level}")[...]), im["axes"], im["samples"])
                if bgr:
                    got = got[..., ::-1]
                assert got.shape == want.shape, (path.name, scene, level)
                diff = np.abs(got.astype(np.int64) - want.astype(np.int64)).max()
                assert diff <= tolerance, (path.name, scene, level, diff)
