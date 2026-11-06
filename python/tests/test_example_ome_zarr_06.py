"""The example layout `refkv.example.ome-zarr-06` (refkv.zarrify.layouts.examples.ome_zarr_06):
an OME-Zarr 0.6 image of TIFF, NDPI, QPTIFF and IMS files and of HDF5 datasets, checked with
`check`, read back with zarr against the base and h5py, validated by ome-zarr-models' 0.6 models,
and written alike under another PYTHONHASHSEED; and each way its options or an input are refused."""

import functools
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import h5py
import numpy as np
import pytest
import zarr

import refkv.zarr
from refkv.zarrify import Rejected, zarrify
from refkv.zarrify.layouts import MIRROR, Base, Layout, LayoutRejected, base, encode_entries, without_checksums
from refkv.zarrify.layouts.examples.ome_zarr_06 import OmeZarr06
from refkv.zarrify.layouts.hierarchy import check

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
ALLOWED: frozenset[str] = frozenset()  # the check codes the module's docstring names: none

Json = dict[str, Any]
T: Json = {"name": "t", "type": "time", "unit": "second"}
C: Json = {"name": "c", "type": "channel"}
Z, Y, X = ({"name": n, "type": "space", "unit": "micrometer"} for n in "zyx")
MOVIE: Json = {"name": "movie", "array": "stimulus/templates/movie/data", "dims": ["t", "x", "y"],
         "order": ["t", "y", "x"], "axes": [T, Y, X], "scale": [0.033, 0.5, 0.5]}  # NWB's (frame, x, y)
PLAIN: Json = {"name": "plain", "array": "/plain", "dims": ["y", "x"], "axes": [Y, X]}  # a leading / is dropped
SHUFFLE: Json = {"name": "shuffled", "array": "shuffle", "dims": ["x", "y"], "order": ["y", "x"], "axes": [Y, X],
           "scale": [2, 3]}  # shuffled and compressed
D3: Json = {"name": "refkv_source", "array": "d3", "dims": ["y", "x", "c"], "order": ["c", "y", "x"],
      "axes": [C, Y, X]}  # the mirror's name, free when the mirror is not kept

CASES: list[tuple[str, Json, bool]] = [
    ("tiff/strips_uint16_pyramid_short.ome.tif", {}, False),  # two levels, no translation
    ("tiff/strips_uint8.tif", {}, True),  # with checksums
    ("tiff/strips_rgb_contig_deflate_short.tif", {"source": False}, False),  # rectilinear, a transpose first
    ("tiff/bilevel_lzw_strips_short.tif", {}, False),  # sharded
    ("tiff/edge_ome_plane_position.tif", {}, False),  # z, y, x, translated; TIFF's z is not moved
    ("tiff/tczyx_uint16_deflate.ome.tif", {}, False),
    ("ndpi/ndpi_focal_planes.ndpi", {}, False),  # NDPI's z is a plane's own position: not moved
    ("ndpi/ndpi_jpegxr_cy5.ndpi", {"source": False}, False),
    ("qptiff/qptiff_fl_uint8_lzw.qptiff", {}, False),  # three levels, translated
    ("qptiff/qptiff_bf_rgb_jpeg.qptiff", {"source": False}, False),
    ("ims/ims_latest_reconstruction.ims", {}, False),  # IMS's z is placed by its corner: moved
    ("ims/ims_latest_timelapse.ims", {"source": False}, False),
    ("ims/ims_shuffle_deflate.ims", {}, False),
    ("hdf5/nwb_ophys.nwb", {"images": [MOVIE]}, False),
    ("hdf5/hdf5_filters.h5", {"images": [PLAIN, SHUFFLE]}, True),
    ("hdf5/hdf5_numbers.h5", {"images": [D3], "source": False}, False),
]

# what the determinism check runs in a second process: each case's entries, encoded
SCRIPT = """
import json, sys
from refkv.zarrify.layouts import base, encode_entries
from refkv.zarrify.layouts.examples.ome_zarr_06 import OmeZarr06

plugin = OmeZarr06()
print(json.dumps([encode_entries(plugin.write(base(path, checksums=checksums), plugin.options(options))).hex()
                  for path, options, checksums in json.loads(sys.argv[1])]))
"""


@functools.cache
def base_of(source: str, checksums: bool = False) -> Base:
    return base(str(FIXTURES / source), checksums=checksums)


@functools.cache
def base_store(source: str) -> refkv.zarr.ZarrStore:
    """The built-in output, as zarr reads it."""
    return refkv.zarr.store(zarrify(str(FIXTURES / source)))


def read(store: refkv.zarr.ZarrStore, path: str) -> np.ndarray:
    """The array at `path` of `store`, read with zarr."""
    return np.asarray(zarr.open_array(store, path=path, mode="r")[...])


def ome(store: refkv.zarr.ZarrStore, path: str = "") -> Any:
    """The `ome` attributes of the group at `path` of `store`."""
    return zarr.open_group(store, path=path, mode="r").attrs["ome"]


@pytest.fixture(scope="module")
def elsewhere() -> list[str]:
    """Each case's entries as another process writes them, under another PYTHONHASHSEED."""
    seed = "1" if os.environ.get("PYTHONHASHSEED") != "1" else "2"
    cases = [[str(FIXTURES / s), o, c] for s, o, c in CASES]
    done = subprocess.run([sys.executable, "-c", SCRIPT, json.dumps(cases)], capture_output=True, text=True,
                          env={**os.environ, "PYTHONHASHSEED": seed}, check=True, timeout=600)
    return json.loads(done.stdout)


def validate(store: refkv.zarr.ZarrStore, path: str = "") -> None:
    """The image group validated by ome-zarr-models' OME-Zarr 0.6 models, when it is installed:
    its `ome` attributes, and the group with its arrays unless one has a `rectilinear` grid,
    which pydantic-zarr's array model does not take."""
    v06 = pytest.importorskip("ome_zarr_models.v06")
    attrs = ome(store, path)
    v06.image.ImageAttrs.model_validate(attrs)
    paths = [f"{path}/{d['path']}".lstrip("/") for ms in attrs["multiscales"] for d in ms["datasets"]]
    grids: list[Any] = [zarr.open_array(store, path=p, mode="r").metadata.to_dict()["chunk_grid"] for p in paths]
    if all(g["name"] == "regular" for g in grids):
        v06.Image.from_zarr(zarr.open_group(store, path=path, mode="r"))


@pytest.mark.parametrize(("source", "given", "checksums"), CASES)
def test_the_output_is_an_ome_zarr_06_image_of_the_base_s_arrays(source: str, given: Json, checksums: bool,
                                                                  elsewhere: list[str]) -> None:
    """The output has no `check` problems; zarrify writes exactly it; each array reads back equal
    to the base's (and, for HDF5, to h5py's), transposed into the image's order; the metadata is
    0.6, each level's translation moved to its pixel's center on the axes placed by a corner; the
    mirror is kept unless `source` is false; and another process writes the same entries."""
    b = base_of(source, checksums)
    plugin = OmeZarr06()
    options = plugin.options(given)
    entries = plugin.write(b, options)
    assert [p for p in check(b, entries) if p.code not in ALLOWED] == []
    assert encode_entries(entries).hex() == elsewhere[CASES.index((source, given, checksums))]

    store = zarrify(str(FIXTURES / source), layout=Layout(plugin, given), checksums=checksums)
    assert {k: v if isinstance(v, bytes) else without_checksums(v) for k, v in store.mapping.items()} == entries
    mirror = b.entries(f"{MIRROR}/")
    kept = {k: v for k, v in mirror.items() if entries.get(k) == v}
    assert kept == (mirror if options["source"] else {})
    out = refkv.zarr.store(store)

    if b.profile == "hdf5":
        assert zarr.open_group(out, mode="r").attrs.asdict() == {}
        with h5py.File(FIXTURES / source) as f:
            for image in options["images"]:
                order = [image["dims"].index(d) for d in image["order"]]
                got = read(out, f"{image['name']}/0")
                dataset: Any = f[image["array"]]
                assert np.array_equal(got, np.transpose(dataset[...], order))
                assert np.array_equal(got, np.transpose(read(base_store(source), image["array"]), order))
                copied = zarr.open_array(out, path=f"{image['name']}/0", mode="r")
                assert copied.metadata.to_dict()["dimension_names"] == tuple(image["order"])
                assert copied.attrs.asdict() == b.attributes(image["array"])
                ms = ome(out, image["name"])["multiscales"]
                assert ms[0]["coordinateSystems"][0]["axes"] == image["axes"]
                assert ms[0]["datasets"][0]["coordinateTransformations"][0]["scale"] == image["scale"]
                validate(out, image["name"])
        return

    before = b.document("zarr.json")["attributes"]["ome"]
    after = ome(out)
    assert after["version"] == "0.6"
    assert after.get("omero") == before.get("omero")
    cornered = {"y", "x", *({"z"} if b.profile == "ims" else ())}
    for old, new in zip(before["multiscales"], after["multiscales"], strict=True):
        assert new.get("name") == old.get("name")
        assert new["coordinateSystems"] == [{"name": "physical", "axes": old["axes"]}]
        for level, dataset in zip(old["datasets"], new["datasets"], strict=True):
            p = level["path"]
            steps = {t["type"]: t for t in level["coordinateTransformations"]}
            scale = steps["scale"]["scale"]
            corner = steps.get("translation", {"translation": [0] * len(scale)})["translation"]
            (sequence,) = dataset["coordinateTransformations"]
            center = [c + s / 2 if a["name"] in cornered else c
                      for c, s, a in zip(corner, scale, old["axes"], strict=True)]
            assert sequence["transformations"] == [{"type": "scale", "scale": scale},
                                                   {"type": "translation", "translation": center}]
            assert np.array_equal(read(out, p), read(base_store(source), p))
    if options["source"]:  # the mirror's arrays read as the base's do
        for k, v in mirror.items():
            if k.endswith("/zarr.json") and isinstance(v, bytes) and json.loads(v)["node_type"] == "array":
                p = k[: -len("/zarr.json")]
                assert np.array_equal(read(out, p), read(base_store(source), p))
    validate(out)


# ---- options() refuses (a LayoutUsageError from zarrify)

def test_an_option_it_does_not_take_is_refused():
    with pytest.raises(ValueError, match="the options are source and images"):
        OmeZarr06().options({"path": "ome"})


def test_a_source_that_is_not_a_boolean_is_refused():
    with pytest.raises(ValueError, match="source is true or false"):
        OmeZarr06().options({"source": "no"})


@pytest.mark.parametrize("images", [{}, [], "movie"])
def test_images_that_are_not_a_list_of_images_are_refused(images):
    with pytest.raises(ValueError, match="images is a list of at least one image"):
        OmeZarr06().options({"images": images})


@pytest.mark.parametrize("image", [{"name": "movie"}, {**MOVIE, "units": "s"}, ["movie"]])
def test_an_image_without_its_members_is_refused(image):
    with pytest.raises(ValueError, match="an image holds name, array, dims and axes"):
        OmeZarr06().options({"images": [image]})


@pytest.mark.parametrize("name", ["", "a/b", "/movie", "__movie", "..", "zarr.json", 3])
def test_an_image_name_that_is_not_one_key_segment_is_refused(name):
    with pytest.raises(ValueError, match="node's name"):
        OmeZarr06().options({"images": [{**MOVIE, "name": name}]})


def test_an_image_named_as_the_kept_mirror_is_refused():
    with pytest.raises(ValueError, match="'refkv_source' is not an image's name while the mirror is kept"):
        OmeZarr06().options({"images": [{**MOVIE, "name": "refkv_source"}]})


def test_two_images_of_one_name_are_refused():
    with pytest.raises(ValueError, match="two images are named 'movie'"):
        OmeZarr06().options({"images": [MOVIE, {**MOVIE, "array": "other"}]})


@pytest.mark.parametrize("change", [
    {"dims": ["t", "x", "x"]},  # a dimension twice
    {"order": ["t", "y", "z"]},  # not an order of dims
    {"axes": [T, X, Y]},  # not named as order
    {"scale": [1, 1]},  # a scale short
])
def test_an_image_whose_dims_order_axes_and_scale_disagree_is_refused(change):
    with pytest.raises(ValueError, match="give each of dims"):
        OmeZarr06().options({"images": [{**MOVIE, **change}]})


@pytest.mark.parametrize("axes", [
    [{"name": "t", "type": "frame"}, Y, X],  # a type 0.6 allows, which the example does not
    [{"name": "t", "type": "time", "unit": 1}, Y, X],
    [{"name": "t", "type": "time", "color": "red"}, Y, X],
])
def test_an_axis_it_does_not_take_is_refused(axes):
    with pytest.raises(ValueError, match="an axis"):
        OmeZarr06().options({"images": [{**MOVIE, "axes": axes}]})


@pytest.mark.parametrize(("order", "axes"), [
    (["y", "t", "x"], [Y, T, X]),  # space before time
    (["t", "y"], [T, Y]),  # one space axis
])
def test_axes_out_of_ome_zarr_s_order_are_refused(order, axes):
    image = {**MOVIE, "dims": order, "order": order, "axes": axes, "scale": [1] * len(order)}
    with pytest.raises(ValueError, match="a time, then a channel, then 2 or 3 of space"):
        OmeZarr06().options({"images": [image]})


@pytest.mark.parametrize("scale", [[1, 0, 1], [1, -1, 1], [1, True, 1], [1, "2", 1]])
def test_a_scale_that_is_not_positive_is_refused(scale):
    with pytest.raises(ValueError, match="a scale is a finite, positive number"):
        OmeZarr06().options({"images": [{**MOVIE, "scale": scale}]})


# ---- write() rejects (a LayoutRejected from zarrify)

def test_an_hdf5_file_without_images_is_rejected():
    with pytest.raises(LayoutRejected, match="option images"):
        zarrify(str(FIXTURES / "hdf5/nwb_ophys.nwb"), layout=Layout(OmeZarr06()))


def test_images_of_a_tiff_are_rejected():
    plugin = OmeZarr06()
    with pytest.raises(Rejected, match="images name an HDF5 file's datasets"):
        plugin.write(base_of("tiff/strips_uint8.tif"), plugin.options({"images": [MOVIE]}))


def test_an_array_the_base_does_not_have_is_rejected():
    plugin = OmeZarr06()
    with pytest.raises(Rejected, match="is not an array of the base"):
        plugin.write(base_of("hdf5/nwb_ophys.nwb"), plugin.options({"images": [{**MOVIE, "array": "stimulus"}]}))


def test_an_array_of_another_rank_is_rejected():
    plugin = OmeZarr06()
    image = {**MOVIE, "dims": ["y", "x"], "order": ["y", "x"], "axes": [Y, X], "scale": [1, 1]}
    with pytest.raises(Rejected, match="has 3 dimensions, not 2"):
        plugin.write(base_of("hdf5/nwb_ophys.nwb"), plugin.options({"images": [image]}))


def test_an_array_of_strings_is_rejected():
    plugin = OmeZarr06()
    image = {"name": "text", "array": "grid", "dims": ["y", "x"], "axes": [Y, X]}
    with pytest.raises(Rejected, match="not numbers"):
        plugin.write(base_of("hdf5/hdf5_strings.h5"), plugin.options({"images": [image]}))


def test_a_format_it_does_not_read_is_rejected():
    with pytest.raises(LayoutRejected, match="does not read the mrc profile"):
        zarrify(str(FIXTURES / "mrc/mrc_mode_int4_image.mrc"), layout=Layout(OmeZarr06()))
