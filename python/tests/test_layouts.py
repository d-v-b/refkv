"""Layouts plugins write (spec/layout.md; refkv.zarrify.layouts): docs/examples/ome_zarr_06.py's
OME-Zarr 0.6 layout over TIFF-family, MRC and HDF5 inputs, `copy`, and each way a plugin layout
is refused or fails."""

import dataclasses
import json
import runpy
from pathlib import Path
from typing import ClassVar

import h5py
import jsonschema
import numpy as np
import pytest
import zarr

import refkv
import refkv.zarr
from refkv import Range
from refkv.zarrify import Image, layouts, zarrify
from refkv.zarrify.__main__ import main
from refkv.zarrify.common import PROFILES
from refkv.zarrify.layouts import (
    Base,
    Layout,
    LayoutFailed,
    LayoutRejected,
    LayoutUsageError,
    copy,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures"
LAYOUT_SCHEMA = json.loads((ROOT / "spec" / "layout" / "schema.json").read_text())
Ome06 = runpy.run_path(str(ROOT / "docs" / "examples" / "ome_zarr_06.py"))["Ome06"]
NAME = "acme-ome.ome06"
MOVIE = {"name": "movie", "array": "stimulus/templates/movie/data", "dims": ["t", "x", "y"], "order": ["t", "y", "x"],
         "axes": [{"name": "t", "type": "time"}, {"name": "y", "type": "space"}, {"name": "x", "type": "space"}]}


class Dist:
    def __init__(self, name):
        self.name = name


class Entry:
    """An entry point of the group refkv.layouts, as importlib.metadata gives one."""

    def __init__(self, name, plugin, dist=None):
        self.name, self._plugin, self.dist = name, plugin, Dist(dist or name.split(".")[0])

    def load(self):
        if isinstance(self._plugin, Exception):
            raise self._plugin
        return self._plugin


def install(monkeypatch, *entries):
    """Makes `entries` the installed plugins."""
    monkeypatch.setattr(layouts, "_entry_points", lambda: entries)


class WithMrc(Ome06):
    """The example, reading MRC too, whose output also has a dataset layout."""

    name = "acme-ome.mrc"
    formats: ClassVar[dict[str, set[int]]] = {**Ome06.formats, "mrc": {0}}


class Writes(Ome06):
    """A plugin whose write returns what the test sets."""

    name = "acme-ome.writes"

    def __init__(self, entries=None, error=None):
        self.entries, self.error = entries, error

    def write(self, base, _options):
        if self.error is not None:
            raise self.error
        assert self.entries is not None
        return self.entries(base)


def document(store, key):
    """A JSON document of `store`, parsed."""
    value = store.mapping[key]
    assert isinstance(value, bytes)
    return json.loads(value)


def read(store, path):
    return zarr.open_array(refkv.zarr.store(store), path=path, mode="r")[...]


def channel_last(tmp_path):
    """An HDF5 file holding a chunked, compressed (y, x, c) image."""
    path = tmp_path / "rgb.h5"
    with h5py.File(path, "w") as f:
        f.create_dataset("image", data=np.arange(6 * 5 * 3, dtype="uint16").reshape(6, 5, 3), chunks=(4, 3, 3),
                         compression="gzip", shuffle=True)
    return path


RGB = {"name": "rgb", "array": "image", "dims": ["y", "x", "c"], "order": ["c", "y", "x"], "scale": [1, 0.5, 0.5],
       "axes": [{"name": "c", "type": "channel"}, {"name": "y", "type": "space", "unit": "micrometer"},
                {"name": "x", "type": "space", "unit": "micrometer"}]}


@pytest.mark.parametrize(("source", "plugin", "options", "extra"), [
    ("tiff/float32_uncompressed_be.ome.tif", Ome06, None, {}),  # tiled, big-endian
    ("tiff/strips_uint8.tif", Ome06, None, {"checksums": True}),
    ("tiff/bilevel_lzw_strips_short.tif", Ome06, None, {}),  # sharded strips
    ("tiff/strips_rgb_contig_deflate_short.tif", Ome06, None, {}),  # rectilinear, starts with a transpose
    ("ndpi/ndpi_jpegxr_cy5.ndpi", Ome06, None, {}),
    ("qptiff/qptiff_fl_lzw_predictor.qptiff", Ome06, None, {}),
    ("ims/ims_latest_extensible.ims", Ome06, None, {}),
    ("mrc/mrc_mode_int4_image.mrc", WithMrc, None, {}),  # a dataset layout beside it
    ("mrc/mrc_mode_int4_image.mrc", WithMrc, None, {"layouts": [Image()]}),
    ("hdf5/nwb_ophys.nwb", Ome06, {"images": [MOVIE]}, {}),
    ("rgb.h5", Ome06, {"images": [RGB]}, {"path": "rgb"}),
])
def test_a_plugin_layout_presents_the_base_s_arrays(monkeypatch, tmp_path, source, plugin, options, extra):
    """A plugin's layout sits under refkv_layouts/<path>, declared and named on the root; it
    reads back equal to the base's arrays (transposed as the options say); the base's entries
    are those of the same call without the plugin; and the archive saves and reopens."""
    install(monkeypatch, Entry(plugin.name, plugin()))
    path = channel_last(tmp_path) if source == "rgb.h5" else FIXTURES / source
    node = extra.pop("path", None)
    store = zarrify(str(path), plugins=[Layout(plugin.name, options, node)], **extra)
    base = zarrify(str(path), **extra)
    where = f"refkv_layouts/{node or plugin.path}"

    mine = {k: v for k, v in store.mapping.items() if not k.startswith("refkv_layouts/") and k != "zarr.json"}
    assert mine == {k: v for k, v in base.mapping.items() if k != "zarr.json"}
    root = document(base, "zarr.json")["attributes"]["refkv_zarrified"]
    whole, expected = document(store, "zarr.json"), document(base, "zarr.json")
    prop = whole["attributes"]["refkv_zarrified"]
    named = prop.pop("layouts")
    assert named == {**expected["attributes"]["refkv_zarrified"].pop("layouts", {}), plugin.name: where}
    assert list(named) == sorted(named)
    assert whole == expected  # the root is the base's but for its layouts
    keys = list(document(store, "zarr.json")["attributes"]["refkv_zarrified"])
    assert keys[keys.index("source") + 1] == "layouts"
    group = document(store, "refkv_layouts/zarr.json")["attributes"]
    assert group == {"zarr_conventions": [layouts.CMO], "refkv_layout": {"version": 0}}

    attrs = document(store, f"{where}/zarr.json")["attributes"]
    assert [c["uuid"] for c in attrs["zarr_conventions"]] == [layouts.UUID, Ome06.convention.uuid]
    assert attrs["acme_ome_" + plugin.name.split(".")[1]] == {"version": 0}
    assert attrs["refkv_layout"] == {
        "version": 0, "layout": {"name": plugin.name, "uuid": Ome06.convention.uuid, "version": 0},
        "source": root["source"], "base": {"uuid": PROFILES[root["profile"]][0], "version": 0},
        "options": {"images": []} if options is None else plugin().options(options)}
    jsonschema.validate(document(store, "refkv_layouts/zarr.json"), LAYOUT_SCHEMA)
    jsonschema.validate(document(store, f"{where}/zarr.json"), LAYOUT_SCHEMA)
    profile_schema = json.loads((ROOT / "spec" / "zarrify" / root["profile"] / "schema.json").read_text())
    jsonschema.validate(document(store, "zarr.json"), profile_schema)
    image_node = where if options is None else f"{where}/{options['images'][0]['name']}"
    ome = document(store, f"{image_node}/zarr.json")["attributes"]["ome"]
    assert ome["version"] == "0.6"

    if options is None:
        for level in ome["multiscales"][0]["datasets"]:
            assert np.array_equal(read(store, f"{where}/{level['path']}"), read(base, level["path"]))
    else:
        image = options["images"][0]
        order = [image["dims"].index(d) for d in image["order"]]
        expected = np.transpose(read(base, image["array"]), order)
        assert np.array_equal(read(store, f"{where}/{image['name']}/0"), expected)
    saved = tmp_path / "out.vzip"
    store.save_vzip(saved)
    reopened = refkv.open(str(saved))
    assert reopened.get(f"{where}/zarr.json") == store.mapping[f"{where}/zarr.json"]


@pytest.mark.parametrize(("source", "array", "order"), [
    ("tiff/bilevel_lzw_strips_short.tif", "0", [1, 0]),  # sharded
    ("tiff/strips_rgb_contig_deflate_short.tif", "0", [1, 2, 0]),  # rectilinear, a transpose first
    ("tiff/strips_rgb_contig_deflate_short.tif", "0", [0, 1, 2]),
    ("hdf5/nwb_ophys.nwb", "stimulus/templates/movie/data", [2, 0, 1]),
    ("hdf5/hdf5_filters.h5", None, None),  # every array of rank 2 or more, reversed
])
def test_copy_transposes_an_array_to_the_same_values(source, array, order):
    """`copy` with `order` decodes to the base's values transposed by `order`, as numpy.transpose
    takes it, for sharded, rectilinear and transposed arrays alike; without it, to the same
    values, with no attributes."""
    store = zarrify(str(FIXTURES / source))
    base = Base(store, None)
    arrays = [(array, order)] if array is not None else [
        (k[:-len("/zarr.json")], list(range(len(d["shape"])))[::-1])
        for k in base.keys()  # noqa: SIM118 (Base.keys, not a dict's)
        if k.endswith("/zarr.json") and not k.startswith("refkv_source/")
        and (d := base.document(k))["node_type"] == "array" and len(d["shape"]) >= 2]
    assert arrays
    for path, perm in arrays:
        copied = refkv.ReferenceStore(copy(base, path, order=perm), store._sources)
        assert np.array_equal(read(copied, ""), np.transpose(read(store, path), perm)), path
        assert document(copied, "zarr.json")["attributes"] == {}


def reserved_file(tmp_path, kind):
    """A file of `kind` whose root holds an object named refkv_layouts, beside one named x."""
    if kind == "h5":
        path = tmp_path / "reserved.h5"
        with h5py.File(path, "w") as f:
            f.create_group("refkv_layouts").create_dataset("x", data=[1, 2])
            f.create_dataset("x", data=[3, 4])
    elif kind == "npz":
        path = tmp_path / "reserved.npz"
        np.savez(path, refkv_layouts=np.arange(2), x=np.arange(3))
    else:
        path = tmp_path / "reserved.safetensors"
        header = json.dumps({"refkv_layouts/w": {"dtype": "F32", "shape": [2], "data_offsets": [0, 8]},
                             "x": {"dtype": "F32", "shape": [2], "data_offsets": [8, 16]}}).encode()
        path.write_bytes(len(header).to_bytes(8, "little") + header + np.arange(4, dtype="<f4").tobytes())
    return path


@pytest.mark.parametrize("kind", ["h5", "npz", "safetensors"])
def test_a_source_name_refkv_layouts_is_reserved(tmp_path, kind):
    """A root object named refkv_layouts, in an HDF5, .npz or safetensors file, is not presented,
    with the reason reserved, as one named refkv_source is not; its sibling x is."""
    store = zarrify(str(reserved_file(tmp_path, kind)))
    assert not any(k.startswith("refkv_layouts") for k in store.mapping)
    assert "x/zarr.json" in store.mapping
    assert "reserved" in json.dumps([document(store, k) for k in store.mapping if k.endswith("zarr.json")])


@pytest.mark.parametrize(("encoding", "shape", "keys", "order"), [
    ({"name": "default", "configuration": {"separator": "/"}}, [2, 3], ["c/0/0", "c/1/0"], [1, 0]),
    ({"name": "default", "configuration": {"separator": "."}}, [2, 3], ["c.0.0", "c.1.0"], [1, 0]),
    ({"name": "v2", "configuration": {"separator": "."}}, [2, 3], ["0.0", "1.0"], [1, 0]),
    ({"name": "v2", "configuration": {"separator": "/"}}, [2, 3], ["0/0", "1/0"], [1, 0]),
    ({"name": "default", "configuration": {"separator": "/"}}, [], ["c"], []),  # a scalar
])
def test_copy_follows_the_array_s_chunk_key_encoding(encoding, shape, keys, order):
    """`copy` permutes a chunk key's indexes in the array's own encoding and separator, and copies
    a scalar, whose only permutation is [], as it is."""
    chunk = [1, *shape[1:]] if shape else []
    values = np.arange(int(np.prod(shape)), dtype="<u2").reshape(shape)
    array = {"zarr_format": 3, "node_type": "array", "shape": shape, "data_type": "uint16",
             "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": chunk}},
             "chunk_key_encoding": encoding, "fill_value": 0,
             "codecs": [{"name": "bytes", "configuration": {"endian": "little"}}], "attributes": {}}
    root = {"zarr_format": 3, "node_type": "group",
            "attributes": {"refkv_zarrified": {"profile": "tiff", "version": 0, "source": {"url": "x.tif"}}}}
    rows = [values[i].tobytes() for i in range(shape[0])] if shape else [values.tobytes()]
    store = refkv.ReferenceStore({"zarr.json": json.dumps(root).encode(), "a/zarr.json": json.dumps(array).encode(),
                                  **{f"a/{k}": row for k, row in zip(keys, rows, strict=True)}})
    copied = refkv.ReferenceStore(copy(Base(store, None), "a", order=order))
    assert np.array_equal(read(copied, ""), np.transpose(values, order))


def test_copied_references_are_the_base_s_and_options_are_recorded_as_planned(monkeypatch):
    """A layout's reference is written as the base holds it, checksums included, whatever the
    plugin's value carried, and its recorded options are the canonical ones, whatever write did
    to its copy."""
    def tampered(base, options):
        options["path"] = "/home/me"
        entries = {f"0/{k}": v for k, v in copy(base, "0").items()}
        return {"zarr.json": group(), **{k: dataclasses.replace(v, crc32c=12345) if isinstance(v, Range) else v
                                        for k, v in entries.items()}}
    install(monkeypatch, Entry(Writes.name, Writes(error=None, entries=None)))
    monkeypatch.setattr(Writes, "write", lambda _self, base, options: tampered(base, options))
    store = zarrify(str(FIXTURES / "tiff/strips_uint8.tif"), checksums=True,
                    plugins=[Layout(Writes.name, path="writes")])
    copied = {k: v for k, v in store.mapping.items() if k.startswith("refkv_layouts/") and isinstance(v, Range)}
    assert copied
    for k, v in copied.items():
        assert v == store.mapping[k.removeprefix("refkv_layouts/writes/")]
        assert v.crc32c is not None
        assert v.crc32c != 12345
    assert document(store, "refkv_layouts/writes/zarr.json")["attributes"]["refkv_layout"]["options"] == {"images": []}
    assert np.array_equal(read(store, "refkv_layouts/writes/0"), read(store, "0"))


def test_an_unknown_plugin_is_a_usage_error(monkeypatch):
    install(monkeypatch)
    with pytest.raises(LayoutUsageError, match="no layout plugin"):
        zarrify(str(FIXTURES / "tiff/strips_uint8.tif"), plugins=[Layout(NAME)])


def test_a_name_whose_key_refkv_writes_is_a_usage_error(monkeypatch):
    install(monkeypatch, Entry("zarr.conventions", Ome06(), dist="zarr"))
    with pytest.raises(LayoutUsageError, match="a member refkv writes"):
        zarrify(str(FIXTURES / "tiff/strips_uint8.tif"), plugins=[Layout("zarr.conventions")])


def test_two_layouts_of_one_convention_are_a_usage_error(monkeypatch):
    install(monkeypatch, Entry(NAME, Ome06()), Entry(WithMrc.name, WithMrc()))
    with pytest.raises(LayoutUsageError, match="one UUID"):
        zarrify(str(FIXTURES / "tiff/strips_uint8.tif"), plugins=[Layout(NAME), Layout(WithMrc.name, path="mrc")])


def test_a_name_not_of_its_distribution_is_a_usage_error(monkeypatch):
    install(monkeypatch, Entry(NAME, Ome06(), dist="other"))
    with pytest.raises(LayoutUsageError, match="starts with its distribution"):
        zarrify(str(FIXTURES / "tiff/strips_uint8.tif"), plugins=[Layout(NAME)])


def test_a_plugin_that_names_itself_otherwise_is_a_usage_error(monkeypatch):
    install(monkeypatch, Entry("acme-ome.other", Ome06()))
    with pytest.raises(LayoutUsageError, match="names itself"):
        zarrify(str(FIXTURES / "tiff/strips_uint8.tif"), plugins=[Layout("acme-ome.other")])


def test_a_plugin_that_fails_to_load_is_a_usage_error(monkeypatch):
    install(monkeypatch, Entry(NAME, ImportError("no module acme_ome")))
    with pytest.raises(LayoutUsageError, match="failed to load"):
        zarrify(str(FIXTURES / "tiff/strips_uint8.tif"), plugins=[Layout(NAME)])


def test_options_the_plugin_refuses_are_a_usage_error(monkeypatch):
    install(monkeypatch, Entry(NAME, Ome06()))
    with pytest.raises(LayoutUsageError, match="options are refused"):
        zarrify(str(FIXTURES / "tiff/strips_uint8.tif"), plugins=[Layout(NAME, {"images": [{"name": "x"}]})])


def test_two_layouts_at_one_path_are_a_usage_error(monkeypatch):
    install(monkeypatch, Entry(NAME, Ome06()), Entry(WithMrc.name, WithMrc()))
    with pytest.raises(LayoutUsageError, match="one path"):
        zarrify(str(FIXTURES / "tiff/strips_uint8.tif"), plugins=[Layout(NAME), Layout(WithMrc.name, path="ome06")])


def test_a_plugin_layout_on_a_store_input_is_a_usage_error(monkeypatch):
    install(monkeypatch, Entry(NAME, Ome06()))
    with pytest.raises(LayoutUsageError, match="store input"):
        zarrify(str(FIXTURES / "zarr2" / "zarr2_hierarchy") + "/", plugins=[Layout(NAME)])


def test_a_base_the_plugin_does_not_read_is_rejected(monkeypatch):
    install(monkeypatch, Entry(NAME, Ome06()))
    with pytest.raises(LayoutRejected, match="does not read the mrc profile"):
        zarrify(str(FIXTURES / "mrc/mrc_mode_int4_image.mrc"), plugins=[Layout(NAME)])


def test_a_plugin_s_rejection_rejects_the_layout_request(monkeypatch):
    install(monkeypatch, Entry(NAME, Ome06()))
    with pytest.raises(LayoutRejected, match="named by the layout's options"):
        zarrify(str(FIXTURES / "hdf5/nwb_ophys.nwb"), plugins=[Layout(NAME)])


def test_a_plugin_s_exception_is_a_failure(monkeypatch):
    install(monkeypatch, Entry(Writes.name, Writes(error=KeyError("ome"))))
    with pytest.raises(LayoutFailed, match="KeyError"):
        zarrify(str(FIXTURES / "tiff/strips_uint8.tif"), plugins=[Layout(Writes.name)])


def group():
    return json.dumps({"zarr_format": 3, "node_type": "group", "attributes": {}}).encode()


def array_entries(base):
    return {f"0/{k}": v for k, v in copy(base, "0").items()}


def test_a_reference_the_base_does_not_hold_is_a_failure(monkeypatch):
    def made(base):
        return {"zarr.json": group(), **array_entries(base), "0/c/0/0": Range(source=0, offset=0, length=8)}
    install(monkeypatch, Entry(Writes.name, Writes(made)))
    with pytest.raises(LayoutFailed, match="not one of the base's"):
        zarrify(str(FIXTURES / "tiff/strips_uint8.tif"), plugins=[Layout(Writes.name)])


def test_a_refkv_declaration_in_a_layout_is_a_failure(monkeypatch):
    def declared(_base):
        doc = {"zarr_format": 3, "node_type": "group", "attributes": {"refkv_zarrified": {"tiff": {}}}}
        return {"zarr.json": json.dumps(doc).encode()}
    install(monkeypatch, Entry(Writes.name, Writes(declared)))
    with pytest.raises(LayoutFailed, match="refkv_zarrified"):
        zarrify(str(FIXTURES / "tiff/strips_uint8.tif"), plugins=[Layout(Writes.name)])


def test_a_key_with_a_dotdot_segment_is_a_failure(monkeypatch):
    install(monkeypatch, Entry(Writes.name, Writes(lambda _base: {"zarr.json": group(), "../x": b"1"})))
    with pytest.raises(LayoutFailed, match="not a key of a layout"):
        zarrify(str(FIXTURES / "tiff/strips_uint8.tif"), plugins=[Layout(Writes.name)])


def test_an_entry_outside_any_array_is_a_failure(monkeypatch):
    install(monkeypatch, Entry(Writes.name, Writes(lambda _base: {"zarr.json": group(), "stray": b"1"})))
    with pytest.raises(LayoutFailed, match="not below an array"):
        zarrify(str(FIXTURES / "tiff/strips_uint8.tif"), plugins=[Layout(Writes.name)])


def test_a_node_without_a_parent_group_is_a_failure(monkeypatch):
    install(monkeypatch, Entry(Writes.name, Writes(lambda base: {"zarr.json": group(), **{
        f"a/{k}": v for k, v in array_entries(base).items()}})))
    with pytest.raises(LayoutFailed, match="is not a group of the layout"):
        zarrify(str(FIXTURES / "tiff/strips_uint8.tif"), plugins=[Layout(Writes.name)])


def test_a_plugin_that_changes_its_base_fails(monkeypatch):
    def forge(base):
        base.uuid = "forged"
        return {"zarr.json": group()}
    install(monkeypatch, Entry(Writes.name, Writes(forge)))
    with pytest.raises(LayoutFailed, match="AttributeError"):
        zarrify(str(FIXTURES / "tiff/strips_uint8.tif"), plugins=[Layout(Writes.name)])


def test_a_document_with_nan_is_a_failure(monkeypatch):
    nan = b'{"zarr_format":3,"node_type":"group","attributes":{"x":NaN}}'
    install(monkeypatch, Entry(Writes.name, Writes(lambda _base: {"zarr.json": nan})))
    with pytest.raises(LayoutFailed, match="strict JSON"):
        zarrify(str(FIXTURES / "tiff/strips_uint8.tif"), plugins=[Layout(Writes.name)])


def test_consolidated_metadata_is_a_failure(monkeypatch):
    doc = {"zarr_format": 3, "node_type": "group", "attributes": {}, "consolidated_metadata": {}}
    install(monkeypatch, Entry(Writes.name, Writes(lambda _base: {"zarr.json": json.dumps(doc).encode()})))
    with pytest.raises(LayoutFailed, match="consolidated_metadata"):
        zarrify(str(FIXTURES / "tiff/strips_uint8.tif"), plugins=[Layout(Writes.name)])


def test_a_cmo_named_as_refkv_s_is_a_failure(monkeypatch):
    cmo = {"uuid": "00000000-0000-4000-8000-000000000000", "name": "refkv_layout", "schema_url": "x",
           "spec_url": "y", "description": "z"}
    doc = {"zarr_format": 3, "node_type": "group", "attributes": {"zarr_conventions": [cmo]}}
    install(monkeypatch, Entry(Writes.name, Writes(lambda _base: {"zarr.json": json.dumps(doc).encode()})))
    with pytest.raises(LayoutFailed, match="declares a refkv convention"):
        zarrify(str(FIXTURES / "tiff/strips_uint8.tif"), plugins=[Layout(Writes.name)])


def test_a_cmo_unlike_the_plugin_s_convention_is_a_failure(monkeypatch):
    cmo = {"uuid": Ome06.convention.uuid, "name": "other", "schema_url": "x", "spec_url": "y", "description": "z"}
    doc = {"zarr_format": 3, "node_type": "group", "attributes": {"zarr_conventions": [cmo]}}
    install(monkeypatch, Entry(Writes.name, Writes(lambda _base: {"zarr.json": json.dumps(doc).encode()})))
    with pytest.raises(LayoutFailed, match="otherwise than its ConventionInfo"):
        zarrify(str(FIXTURES / "tiff/strips_uint8.tif"), plugins=[Layout(Writes.name)])


def test_a_key_with_a_dunder_segment_is_a_failure(monkeypatch):
    install(monkeypatch, Entry(Writes.name, Writes(lambda _base: {"zarr.json": group(), "__x/zarr.json": group()})))
    with pytest.raises(LayoutFailed, match="not a key of a layout"):
        zarrify(str(FIXTURES / "tiff/strips_uint8.tif"), plugins=[Layout(Writes.name)])


def test_dimension_names_of_another_rank_are_refused():
    base = Base(zarrify(str(FIXTURES / "tiff/strips_uint8.tif")), None)
    with pytest.raises(ValueError, match="dimension names for an array of 2"):
        copy(base, "0", dimension_names=["y"])


def test_the_cli_writes_a_plugin_layout(monkeypatch, tmp_path, capsys):
    install(monkeypatch, Entry(NAME, Ome06()))
    out = tmp_path / "out.vzip"
    assert main([str(FIXTURES / "tiff/strips_uint8.tif"), str(out), "--layout", NAME]) == 0
    assert json.loads(capsys.readouterr().out)["layouts"] == [NAME]


def test_the_cli_exits_2_for_an_unknown_layout(monkeypatch, tmp_path):
    install(monkeypatch)
    assert main([str(FIXTURES / "tiff/strips_uint8.tif"), str(tmp_path / "o.vzip"), "--layout", NAME]) == 2


def test_the_cli_exits_3_for_a_rejected_layout(monkeypatch, tmp_path):
    install(monkeypatch, Entry(NAME, Ome06()))
    assert main([str(FIXTURES / "mrc/mrc_mode_int4_image.mrc"), str(tmp_path / "o.vzip"), "--layout", NAME]) == 3


def test_the_cli_exits_1_for_a_failed_layout(monkeypatch, tmp_path):
    install(monkeypatch, Entry(Writes.name, Writes(error=RuntimeError("bug"))))
    assert main([str(FIXTURES / "tiff/strips_uint8.tif"), str(tmp_path / "o.vzip"), "--layout", Writes.name]) == 1
