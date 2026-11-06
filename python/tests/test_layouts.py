"""Layouts plugins write (spec/layout.md; refkv.zarrify.layouts): docs/examples/ome_zarr_06.py's
OME-Zarr 0.6 layout over TIFF-family, MRC and HDF5 inputs, `copy`, and each way a plugin layout
is refused or fails."""

import asyncio
import dataclasses
import json
import runpy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, ClassVar

import h5py
import jsonschema
import numpy as np
import pytest
import zarr
import zarr.storage

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
    install(monkeypatch, Entry(plugin.name, plugin))  # the class, as a real entry point loads it
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
    prop = document(store, "zarr.json")["attributes"]["refkv_zarrified"]
    if kind == "h5":
        assert prop["hdf5"]["unpresented"]["refkv_layouts"] == "reserved"
    else:
        records = prop["npy"]["members"] if kind == "npz" else prop["safetensors"]["tensors"]
        assert {r["name"]: r.get("reason") for r in records}[
            "refkv_layouts.npy" if kind == "npz" else "refkv_layouts/w"] == "reserved"


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


def sharded():
    """A sharded array of 2x2 inner chunks per shard, as zarr-python writes one, in a base."""
    values = np.arange(4 * 6, dtype="<u2").reshape(4, 6)
    written = zarr.storage.MemoryStore()
    a = zarr.create_array(written, shape=(4, 6), shards=(4, 6), chunks=(2, 3), dtype="uint16", fill_value=0,
                          compressors=None)
    a[...] = values
    entries = {k: v.to_bytes() for k, v in written._store_dict.items() if not k.endswith("zarr.json")}
    doc = json.loads(written._store_dict["zarr.json"].to_bytes())
    root = {"zarr_format": 3, "node_type": "group",
            "attributes": {"refkv_zarrified": {"profile": "tiff", "version": 0, "source": {"url": "x.tif"}}}}
    store = refkv.ReferenceStore({"zarr.json": json.dumps(root).encode(), "a/zarr.json": json.dumps(doc).encode(),
                                  **{f"a/{k}": v for k, v in entries.items()}})
    return store, values


@pytest.mark.parametrize("order", [[1, 0], [0, 1]])
def test_copy_transposes_a_shard_s_inner_chunks_and_index(order):
    """`copy` transposes a sharded array of several inner chunks per shard (whose index a missing
    index transpose would read wrong) to the base's values, transposed."""
    store, values = sharded()
    copied = refkv.ReferenceStore(copy(Base(store, None), "a", order=order))
    assert np.array_equal(read(copied, ""), np.transpose(values, order))


def test_copied_references_are_the_base_s_and_options_are_recorded_as_planned(monkeypatch):
    """A layout's reference is written as the base holds it, checksums included, whatever the
    plugin's value carried, and its recorded options are the canonical ones, whatever write did
    to its copy."""
    def tampered(base, options):
        options["path"] = "/home/me"
        entries = {f"0/{k}": v for k, v in copy(base, "0").items()}
        assert all(v.crc32c is None for v in entries.values() if isinstance(v, Range))  # a plugin sees no checksums
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


def test_formats_of_no_profile_of_this_refkv_are_a_usage_error(monkeypatch):
    class Typo(Ome06):
        formats: ClassVar[dict[str, set[int]]] = {"tif": {0}}
    install(monkeypatch, Entry(NAME, Typo))
    with pytest.raises(LayoutUsageError, match="names no file profile"):
        zarrify(str(FIXTURES / "tiff/strips_uint8.tif"), plugins=[Layout(NAME)])


def test_plugins_that_is_not_a_list_is_a_usage_error(monkeypatch):
    install(monkeypatch, Entry(NAME, Ome06))
    with pytest.raises(LayoutUsageError, match="a list of Layout"):
        zarrify(str(FIXTURES / "tiff/strips_uint8.tif"), plugins=Layout(NAME))  # type: ignore[arg-type] (the mistake under test)


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
    with pytest.raises(LayoutFailed, match="one of the base's references"):
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


def test_a_plugin_that_exits_fails(monkeypatch):
    install(monkeypatch, Entry(Writes.name, Writes(error=SystemExit(3))))
    with pytest.raises(LayoutFailed, match="SystemExit"):
        zarrify(str(FIXTURES / "tiff/strips_uint8.tif"), plugins=[Layout(Writes.name)])


def test_a_provenance_cmo_is_a_failure(monkeypatch):
    cmo = {"uuid": layouts.PROVENANCE_UUID.upper(), "name": "other", "schema_url": "x", "spec_url": "y",
           "description": "z"}
    doc = {"zarr_format": 3, "node_type": "group", "attributes": {"zarr_conventions": [cmo]}}
    install(monkeypatch, Entry(Writes.name, Writes(lambda _base: {"zarr.json": json.dumps(doc).encode()})))
    with pytest.raises(LayoutFailed, match="declares a refkv convention"):
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


TIF = str(FIXTURES / "tiff/strips_uint8.tif")


def doc_bytes(attributes=None, **members):
    """A group's document, or with `node_type` and the members given, another one."""
    return json.dumps({"zarr_format": 3, "node_type": "group", "attributes": attributes or {}, **members}).encode()


def fails(monkeypatch, entries, match):
    """Runs a plugin whose write returns `entries(base)`, and expects LayoutFailed matching `match`."""
    install(monkeypatch, Entry(Writes.name, Writes(entries)))
    with pytest.raises(LayoutFailed, match=match):
        zarrify(TIF, plugins=[Layout(Writes.name)])


def variant(**attributes) -> Any:
    """The example plugin with some attributes changed."""
    return type("Variant", (Ome06,), attributes)


def refused(monkeypatch, plugin, match, plugins=None):
    """Asks for `plugin`'s layout (or `plugins`), and expects LayoutUsageError matching `match`."""
    install(monkeypatch, Entry(plugin.name, plugin))
    with pytest.raises(LayoutUsageError, match=match):
        zarrify(TIF, plugins=plugins if plugins is not None else [Layout(plugin.name)])


def convention(**changed):
    return dataclasses.replace(Ome06.convention, **changed)


def test_a_plugin_sees_one_base_whatever_else_the_call_asks_for(monkeypatch):
    """Base hides the dataset layout, the root's layouts and the ranges' checksums: a plugin sees
    the same keys, root and values with or without the dataset layout and checksums; and the
    output keeps the CMOs and key members the plugin wrote, reads its documents at open, and
    declares the convention planned, whatever the plugin did to its own."""
    seen = []

    class Records(WithMrc):
        def write(self, base, options):
            seen.append((base.keys(), base.document("zarr.json"), dict(base.array("0")[1])))
            self.convention = convention(uuid="11111111-1111-4111-8111-111111111111")
            out = super().write(base, options)
            extra = {"uuid": "22222222-2222-4222-8222-222222222222", "name": "other", "schema_url": "s",
                     "spec_url": "p", "description": "d"}
            node = json.loads(out["zarr.json"])
            node["attributes"] |= {"zarr_conventions": [extra], "acme_ome_mrc": {"note": 1}}
            return {**out, "zarr.json": json.dumps(node).encode()}

    install(monkeypatch, Entry(Records.name, Records))
    mrc = str(FIXTURES / "mrc/mrc_mode_int4_image.mrc")
    store = zarrify(mrc, plugins=[Layout(Records.name)])
    zarrify(mrc, plugins=[Layout(Records.name)], layouts=[Image()], checksums=True)
    assert seen[0] == seen[1]
    assert not any(k.startswith("dataset/") for k in seen[0][0])
    assert "layouts" not in seen[0][1]["attributes"]["refkv_zarrified"]
    attrs = document(store, "refkv_layouts/ome06/zarr.json")["attributes"]
    assert [c["uuid"] for c in attrs["zarr_conventions"]] == [layouts.UUID, Ome06.convention.uuid,
                                                              "22222222-2222-4222-8222-222222222222"]
    assert attrs["acme_ome_mrc"] == {"version": 0, "note": 1}
    assert {"refkv_layouts/zarr.json", "refkv_layouts/ome06/zarr.json"} <= set(store.read_at_open)


def transposed(t, shape=(2, 3, 4)):
    """A base of one array, stored transposed by `t` (its first codec a transpose), and its values."""
    values = np.arange(int(np.prod(shape)), dtype="<u2").reshape(shape)
    stored = np.transpose(values, t)
    array = {"zarr_format": 3, "node_type": "array", "shape": list(shape), "data_type": "uint16",
             "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": list(shape)}},
             "chunk_key_encoding": {"name": "default"}, "fill_value": 0, "dimension_names": ["a", "b", "c"],
             "codecs": [{"name": "transpose", "configuration": {"order": list(t)}},
                        {"name": "bytes", "configuration": {"endian": "little"}}], "attributes": {}}
    root = {"zarr_format": 3, "node_type": "group",
            "attributes": {"refkv_zarrified": {"profile": "tiff", "version": 0, "source": {"url": "x.tif"}}}}
    store = refkv.ReferenceStore({"zarr.json": json.dumps(root).encode(), "a/zarr.json": json.dumps(array).encode(),
                                  "a/c/0/0/0": np.ascontiguousarray(stored).tobytes()})
    return store, values


@pytest.mark.parametrize(("t", "order"), [([1, 2, 0], [2, 0, 1]), ([1, 2, 0], [1, 0, 2]), ([2, 0, 1], [0, 2, 1]),
                                          ([1, 0, 2], [1, 0, 2])])
def test_copy_composes_its_transpose_with_the_base_s(t, order):
    """A copy of an array that starts with a transpose decodes to the base's values transposed, for
    orders that do not commute with it, and drops the transpose when the two cancel; its
    dimension names are permuted with it."""
    store, values = transposed(t)
    entries = copy(Base(store, None), "a", order=order)
    copied = refkv.ReferenceStore(entries)
    assert np.array_equal(read(copied, ""), np.transpose(values, order))
    doc = document(refkv.ReferenceStore(entries), "zarr.json")
    assert doc["dimension_names"] == [["a", "b", "c"][o] for o in order]
    composed = [_inverse_of(order)[i] for i in t]
    assert doc["codecs"][0]["name"] == ("bytes" if composed == [0, 1, 2] else "transpose")


def _inverse_of(order):
    out = [0] * len(order)
    for i, o in enumerate(order):
        out[o] = i
    return out


def test_copy_refuses_an_order_that_is_no_permutation():
    with pytest.raises(ValueError, match="not a permutation"):
        copy(Base(transposed([0, 1, 2])[0], None), "a", order=[0, 0, 1])


def test_copy_refuses_an_order_of_names():
    with pytest.raises(ValueError, match="axis indexes"):
        copy(Base(transposed([0, 1, 2])[0], None), "a", order=["a", "b", "c"])  # type: ignore[arg-type]


def test_copy_refuses_an_unknown_chunk_key_encoding():
    store, _ = transposed([0, 1, 2])
    doc = document(store, "a/zarr.json") | {"chunk_key_encoding": {"name": "other"}}
    mapping = {**store.mapping, "a/zarr.json": json.dumps(doc).encode()}
    with pytest.raises(ValueError, match="not one copy can permute"):
        copy(Base(refkv.ReferenceStore(mapping), None), "a", order=[1, 0, 2])


def test_copy_refuses_a_key_that_is_no_chunk_key():
    store, _ = transposed([0, 1, 2])
    mapping = {k: v for k, v in store.mapping.items() if k != "a/c/0/0/0"} | {"a/x/0/0/0": store.mapping["a/c/0/0/0"]}
    with pytest.raises(ValueError, match="not a chunk key"):
        copy(Base(refkv.ReferenceStore(mapping), None), "a", order=[1, 0, 2])


def test_a_plugin_s_unread_version_is_rejected_by_name(monkeypatch):
    install(monkeypatch, Entry(NAME, variant(formats={"tiff": {1}})))
    with pytest.raises(LayoutRejected, match=r"at version 0 \(it reads tiff v1\)"):
        zarrify(TIF, plugins=[Layout(NAME)])


def test_a_bad_name_is_a_usage_error(monkeypatch):
    refused(monkeypatch, Ome06, "not a layout's name", [Layout("NoDot")])


def test_a_refkv_name_is_a_usage_error(monkeypatch):
    refused(monkeypatch, Ome06, "starts with refkv", [Layout("refkv-x.y")])


def test_two_entry_points_of_one_name_are_a_usage_error(monkeypatch):
    install(monkeypatch, Entry(NAME, Ome06), Entry(NAME, Ome06))
    with pytest.raises(LayoutUsageError, match="2 entry points"):
        zarrify(TIF, plugins=[Layout(NAME)])


def test_a_bad_plugin_path_is_a_usage_error(monkeypatch):
    refused(monkeypatch, variant(path="a/b"), "its path")


def test_a_convention_that_is_no_conventioninfo_is_a_usage_error(monkeypatch):
    refused(monkeypatch, variant(convention={"uuid": "x"}), "not a ConventionInfo")


def test_a_convention_uuid_that_is_no_uuid_is_a_usage_error(monkeypatch):
    refused(monkeypatch, variant(convention=convention(uuid="nope")), "is not a UUID")


def test_a_convention_uuid_in_upper_case_is_a_usage_error(monkeypatch):
    refused(monkeypatch, variant(convention=convention(uuid=Ome06.convention.uuid.upper())), "in lower case")


def test_a_refkv_convention_uuid_is_a_usage_error(monkeypatch):
    refused(monkeypatch, variant(convention=convention(uuid=layouts.UUID)), "a refkv convention's")


def test_a_convention_version_that_is_no_natural_number_is_a_usage_error(monkeypatch):
    refused(monkeypatch, variant(convention=convention(version=True)), "not a natural number")


def test_an_empty_description_is_a_usage_error(monkeypatch):
    refused(monkeypatch, variant(convention=convention(description="")), "description is not a string")


def test_a_convention_with_a_lone_surrogate_is_a_usage_error(monkeypatch):
    refused(monkeypatch, variant(convention=convention(description="\ud800")), "not valid Unicode")


def test_a_negative_version_in_formats_is_a_usage_error(monkeypatch):
    refused(monkeypatch, variant(formats={"tiff": {-1}}), "formats is not a mapping")


def test_a_plugin_without_write_is_a_usage_error(monkeypatch):
    refused(monkeypatch, variant(write=None), "has options\\(\\) and write\\(\\)")


def test_a_value_that_is_no_layout_is_a_usage_error(monkeypatch):
    refused(monkeypatch, Ome06, "holds Layout values", [NAME])


def test_options_that_are_no_mapping_are_a_usage_error(monkeypatch):
    refused(monkeypatch, Ome06, "a mapping", [Layout(NAME, '{"images": []}')])  # type: ignore[arg-type]


def test_a_path_that_is_no_string_is_a_usage_error(monkeypatch):
    refused(monkeypatch, Ome06, "its path is a string", [Layout(NAME, path=5)])  # type: ignore[arg-type]


def test_an_invalid_layout_path_is_a_usage_error(monkeypatch):
    refused(monkeypatch, Ome06, "one key segment", [Layout(NAME, path="__x")])


def test_options_that_return_no_mapping_are_a_usage_error(monkeypatch):
    refused(monkeypatch, variant(options=lambda _self, _given: [1]), "not a mapping")


def test_canonical_options_that_are_no_json_are_a_usage_error(monkeypatch):
    refused(monkeypatch, variant(options=lambda _self, _given: {"a": (1, 2)}), "not JSON as they are")


def test_options_nested_too_deep_are_a_usage_error(monkeypatch):
    deep: dict = {}
    for _ in range(100000):
        deep = {"a": deep}
    refused(monkeypatch, Ome06, "RecursionError", [Layout(NAME, deep)])


def test_two_layouts_of_one_key_are_a_usage_error(monkeypatch):
    one, two = variant(name="acme-ome.a-b"), variant(name="acme-ome.a.b", path="other",
                                                      convention=convention(uuid="33333333-3333-4333-8333-333333333333"))
    install(monkeypatch, Entry(one.name, one), Entry(two.name, two))
    with pytest.raises(LayoutUsageError, match="one key"):
        zarrify(TIF, plugins=[Layout(one.name), Layout(two.name)])


def test_two_layouts_of_one_name_are_a_usage_error(monkeypatch):
    refused(monkeypatch, Ome06, "one name", [Layout(NAME), Layout(NAME, path="other")])


def test_a_write_that_returns_no_mapping_fails(monkeypatch):
    fails(monkeypatch, lambda _base: None, "not a mapping of keys")


def test_a_mid_key_zarr_json_segment_is_a_failure(monkeypatch):
    fails(monkeypatch, lambda _base: {"zarr.json": doc_bytes(), "a/zarr.json/zarr.json": doc_bytes()},
          "not a key of a layout")


def test_a_key_longer_than_an_archive_holds_is_a_failure(monkeypatch):
    fails(monkeypatch, lambda base: {"zarr.json": doc_bytes(), **array_entries(base), "0/" + "a" * 70000: b"x"},
          "more than an archive's")


def test_a_document_that_is_no_bytes_is_a_failure(monkeypatch):
    fails(monkeypatch, lambda _base: {"zarr.json": {"zarr_format": 3}}, "a document is bytes")


def test_a_document_with_a_byte_order_mark_is_a_failure(monkeypatch):
    fails(monkeypatch, lambda _base: {"zarr.json": "﻿".encode() + doc_bytes()}, "byte order mark")


def test_a_document_with_a_member_twice_is_a_failure(monkeypatch):
    twice = b'{"zarr_format":3,"node_type":"group","attributes":{},"attributes":{}}'
    fails(monkeypatch, lambda _base: {"zarr.json": twice}, "a member twice")


def test_a_document_with_a_lone_surrogate_is_a_failure(monkeypatch):
    lone = b'{"zarr_format":3,"node_type":"group","attributes":{"a":"\\ud800"}}'
    fails(monkeypatch, lambda _base: {"zarr.json": lone}, "not valid Unicode")


def test_a_document_with_an_infinite_number_is_a_failure(monkeypatch):
    big = b'{"zarr_format":3,"node_type":"group","attributes":{"a":1e400}}'
    fails(monkeypatch, lambda _base: {"zarr.json": big}, "beyond binary64")


def test_a_document_whose_zarr_format_is_no_integer_is_a_failure(monkeypatch):
    fails(monkeypatch, lambda _base: {"zarr.json": b'{"zarr_format":3.0,"node_type":"group"}'}, "Zarr v3 group")


def test_a_document_of_another_node_type_is_a_failure(monkeypatch):
    fails(monkeypatch, lambda _base: {"zarr.json": b'{"zarr_format":3,"node_type":"other"}'}, "Zarr v3 group")


def test_an_array_document_without_its_members_is_a_failure(monkeypatch):
    bare = b'{"zarr_format":3,"node_type":"array"}'
    fails(monkeypatch, lambda _base: {"zarr.json": doc_bytes(), "a/zarr.json": bare}, "an array's document holds")


def test_a_layout_node_that_is_an_array_is_a_failure(monkeypatch):
    fails(monkeypatch, lambda base: {k.removeprefix("0/"): v for k, v in array_entries(base).items()}, "not a group")


def test_a_node_inside_an_array_is_a_failure(monkeypatch):
    fails(monkeypatch, lambda base: {"zarr.json": doc_bytes(), **array_entries(base), "0/x/zarr.json": doc_bytes()},
          "is not a group of the layout")


def test_attributes_that_are_no_object_are_a_failure(monkeypatch):
    fails(monkeypatch, lambda _base: {"zarr.json": b'{"zarr_format":3,"node_type":"group","attributes":[]}'},
          "attributes is not an object")


def test_a_refkv_layout_member_is_a_failure(monkeypatch):
    fails(monkeypatch, lambda _base: {"zarr.json": doc_bytes({"refkv_layout": {}})}, "holds refkv_layout")


def test_a_refkv_provenance_member_is_a_failure(monkeypatch):
    fails(monkeypatch, lambda _base: {"zarr.json": doc_bytes({"refkv_provenance": {}})}, "holds refkv_provenance")


def test_a_plugin_key_that_is_no_object_is_a_failure(monkeypatch):
    fails(monkeypatch, lambda _base: {"zarr.json": doc_bytes({"acme_ome_writes": 1})}, "does not hold an object")


def test_a_plugin_key_holding_version_is_a_failure(monkeypatch):
    fails(monkeypatch, lambda _base: {"zarr.json": doc_bytes({"acme_ome_writes": {"version": 1}})},
          "must not hold version")


def test_zarr_conventions_that_are_no_list_are_a_failure(monkeypatch):
    fails(monkeypatch, lambda _base: {"zarr.json": doc_bytes({"zarr_conventions": {"uuid": "x"}})},
          "not a list of objects")


def test_a_cmo_name_that_is_no_string_is_a_failure(monkeypatch):
    fails(monkeypatch, lambda _base: {"zarr.json": doc_bytes({"zarr_conventions": [{"uuid": "x", "name": ["n"]}]})},
          "name is not a string")


def test_a_cmo_named_as_the_plugin_s_key_is_a_failure(monkeypatch):
    cmo = {"uuid": "44444444-4444-4444-8444-444444444444", "name": "acme_ome_writes"}
    fails(monkeypatch, lambda _base: {"zarr.json": doc_bytes({"zarr_conventions": [cmo]})},
          "otherwise than its ConventionInfo")


def test_the_cli_exits_2_for_options_without_a_name(monkeypatch, tmp_path):
    install(monkeypatch, Entry(NAME, Ome06))
    assert main([TIF, str(tmp_path / "o.vzip"), "--layout", NAME, "--layout-options", "{}"]) == 2


PREFIX = "refkv_layouts/ome06/"


def test_a_layout_holds_what_its_checks_allow(monkeypatch):
    """At the edges the checks allow: a key exactly as long as an archive holds (its prefix
    included), a bytearray document (written as bytes), a CMO whose URL only resembles refkv's, a
    plugin that writes its own CMO on its node (written once), a numpy order, and a distribution
    whose name normalizes to the layout's first segment."""
    longest = "0/" + "a" * (65535 - len(PREFIX.encode()) - 2)  # 65535 bytes with its prefix
    near = {"uuid": "55555555-5555-4555-8555-555555555555", "schema_url": "https://github.com/d-v-b/refkvx/s"}

    class Edges(Writes):
        def write(self, base, _options):
            mine = layouts._cmo(self.name, self.convention)
            node = doc_bytes({"zarr_conventions": [near, mine]})
            order: Any = np.array([1, 0])  # a numpy order, as numpy.transpose takes
            return {"zarr.json": bytearray(node), **{f"0/{k}": v for k, v in copy(base, "0", order=order).items()},
                    longest: b"x"}

    install(monkeypatch, Entry(Writes.name, Edges(), dist="Acme_Ome"))
    store = zarrify(TIF, plugins=[Layout(Writes.name)])
    assert store.mapping[PREFIX + longest] == b"x"
    node = store.mapping[PREFIX + "zarr.json"]
    assert type(node) is bytes
    uuids = [c["uuid"] for c in json.loads(node)["attributes"]["zarr_conventions"]]
    assert uuids == [layouts.UUID, Ome06.convention.uuid, near["uuid"]]
    assert np.array_equal(read(store, PREFIX + "0"), read(store, "0").T)


def test_the_base_s_view_hides_checksums_in_concatenations_and_refuses_unknown_paths():
    """Base strips every checksum, inside a Concat too, and names what is missing."""
    store = zarrify(str(FIXTURES / "tiff/strips_uint16_lzw_short.tif"), checksums=True)
    base = Base(store, None)
    values = base.array("0")[1].values()
    concats = [v for v in values if isinstance(v, refkv.Concat)]
    assert concats
    assert all(r.crc32c is None for v in concats for r in v.parts)
    with pytest.raises(KeyError, match="no document '0'"):
        base.document("0")
    with pytest.raises(KeyError, match="no array 'missing'"):
        base.array("missing")


def test_a_group_is_no_array_of_the_base():
    base = Base(zarrify(str(FIXTURES / "hdf5/nwb_ophys.nwb")), None)
    with pytest.raises(KeyError, match="a group of the base"):
        base.array("stimulus")


def test_a_base_that_holds_refkv_layouts_is_rejected():
    store = zarrify(TIF)
    held = refkv.ReferenceStore({**store.mapping, "refkv_layouts/x": b"1"}, store._sources)
    with pytest.raises(LayoutRejected, match="holds refkv_layouts"):
        layouts.join(held, None, [])


def test_a_plugin_that_exits_while_it_loads_is_a_usage_error(monkeypatch):
    class Exits(Ome06):
        def __init__(self):
            raise SystemExit(2)
    refused(monkeypatch, Exits, "failed to load: SystemExit")


def test_an_entry_point_that_names_a_function_is_a_usage_error(monkeypatch):
    def make():
        return Ome06()
    install(monkeypatch, Entry(NAME, make))
    with pytest.raises(LayoutUsageError, match="name the plugin's class"):
        zarrify(TIF, plugins=[Layout(NAME)])


def test_a_plugin_attribute_that_raises_is_a_usage_error(monkeypatch):
    class Raises(Ome06):
        @property
        def formats(self):
            raise RuntimeError("no")
    refused(monkeypatch, Raises, "cannot be read: RuntimeError")


def test_a_negative_convention_version_is_a_usage_error(monkeypatch):
    refused(monkeypatch, variant(convention=convention(version=-1)), "not a natural number")


def test_a_boolean_format_version_is_a_usage_error(monkeypatch):
    refused(monkeypatch, variant(formats={"tiff": {True}}), "formats is not a mapping")


def test_a_format_name_that_is_no_string_is_a_usage_error(monkeypatch):
    refused(monkeypatch, variant(formats={1: {0}}), "formats is not a mapping")


def test_options_with_a_lone_surrogate_are_a_usage_error(monkeypatch):
    refused(monkeypatch, variant(options=lambda _self, _given: {"a": "\ud800"}), "not JSON as they are")


def test_options_that_cannot_be_written_are_a_usage_error(monkeypatch):
    refused(monkeypatch, variant(options=lambda _self, _given: {"a": {1}}), "not a JSON object")


def test_options_that_exit_are_a_usage_error(monkeypatch):
    def exits(_self, _given):
        raise SystemExit(1)
    refused(monkeypatch, variant(options=exits), "options are refused")


def test_a_dot_segment_is_a_failure(monkeypatch):
    fails(monkeypatch, lambda _base: {"zarr.json": doc_bytes(), "./zarr.json": doc_bytes()}, "not a key of a layout")


def test_an_empty_segment_is_a_failure(monkeypatch):
    fails(monkeypatch, lambda _base: {"zarr.json": doc_bytes(), "a//zarr.json": doc_bytes()}, "not a key of a layout")


def test_a_key_with_a_lone_surrogate_is_a_failure(monkeypatch):
    fails(monkeypatch, lambda _base: {"zarr.json": doc_bytes(), "\ud800/zarr.json": doc_bytes()},
          "not a key of a layout")


def test_a_key_one_byte_longer_than_an_archive_holds_is_a_failure(monkeypatch):
    key = "0/" + "a" * (65535 - len(PREFIX.encode()) - 1)  # 65536 bytes with its prefix
    fails(monkeypatch, lambda base: {"zarr.json": doc_bytes(), **array_entries(base), key: b"x"},
          "more than an archive's")


def test_an_array_document_without_its_codecs_is_a_failure(monkeypatch):
    def partial(base):
        doc = document(refkv.ReferenceStore({"zarr.json": copy(base, "0")["zarr.json"]}), "zarr.json")
        del doc["codecs"]
        return {"zarr.json": doc_bytes(), "0/zarr.json": json.dumps(doc).encode()}
    fails(monkeypatch, partial, "an array's document holds")


def test_a_member_name_with_a_lone_surrogate_is_a_failure(monkeypatch):
    lone = b'{"zarr_format":3,"node_type":"group","attributes":{"\\ud800":1}}'
    fails(monkeypatch, lambda _base: {"zarr.json": lone}, "not valid Unicode")


def test_a_document_nested_too_deep_is_a_failure(monkeypatch):
    deep = b'{"zarr_format":3,"node_type":"group","attributes":{"a":' + b"[" * 300 + b"]" * 300 + b"}}"
    fails(monkeypatch, lambda _base: {"zarr.json": deep}, "nested deeper than 256")


def test_zarr_conventions_holding_no_objects_are_a_failure(monkeypatch):
    fails(monkeypatch, lambda _base: {"zarr.json": doc_bytes({"zarr_conventions": ["x"]})}, "not a list of objects")


def test_a_refkv_uuid_in_another_form_is_a_failure(monkeypatch):
    cmo = {"uuid": "urn:uuid:" + layouts.UUID.upper()}
    fails(monkeypatch, lambda _base: {"zarr.json": doc_bytes({"zarr_conventions": [cmo]})}, "declares a refkv")


def test_a_range_subclass_is_a_failure(monkeypatch):
    class Mine(Range):
        pass

    def subclassed(base):
        entries = array_entries(base)
        k, v = next((k, v) for k, v in entries.items() if isinstance(v, Range))
        assert isinstance(v, Range)
        return {"zarr.json": doc_bytes(), **entries, k: Mine(v.source, v.offset, v.length, v.data, v.crc32c)}
    fails(monkeypatch, subclassed, "one of the base's references")


def test_a_missing_node_document_is_a_failure(monkeypatch):
    fails(monkeypatch, array_entries, "has no zarr.json")


def test_dimension_names_as_a_string_are_refused():
    with pytest.raises(ValueError, match="a list of names"):
        copy(Base(zarrify(TIF), None), "0", dimension_names="yx")


def nested(depth):
    """A group's document whose attribute `a` nests arrays to make the document `depth` deep."""
    return b'{"zarr_format":3,"node_type":"group","attributes":{"a":' + b"[" * (depth - 2) + b"]" * (depth - 2) + b"}}"


def test_plugins_that_call_zarrify_run_many_at_once(monkeypatch):
    """A plugin may call zarrify itself, from many calls at once: each plugin runs on a thread of
    its own, not on the event loop's executor, which nested calls would use up."""
    class Nests(Ome06):
        def write(self, base, options):
            zarrify(str(FIXTURES / "tiff/strips_uint16_short.tif"))
            return super().write(base, options)

    install(monkeypatch, Entry(NAME, Nests))
    with ThreadPoolExecutor(24) as pool:
        stores = list(pool.map(lambda _: zarrify(TIF, plugins=[Layout(NAME)]), range(24)))
    assert all(s.mapping == stores[0].mapping for s in stores)


def test_documents_at_the_bounds_are_written(monkeypatch):
    """A document nested exactly 256 deep, and an integer of 400 digits, kept exactly, are
    written; dimension names given with an order are the copy's own, not permuted again; a
    document read twice from the base is two dicts."""
    big = b'{"zarr_format":3,"node_type":"group","attributes":{"n":' + b"9" * 400 + b"}}"

    def edges(base):
        root = base.document("zarr.json")
        root["attributes"].clear()
        assert base.document("zarr.json")["attributes"]
        copied = copy(base, "0", order=[1, 0], dimension_names=["x", "y"])
        assert document(refkv.ReferenceStore({"zarr.json": copied["zarr.json"]}), "zarr.json")["dimension_names"] == [
            "x", "y"]
        return {"zarr.json": nested(256), "big/zarr.json": big}

    install(monkeypatch, Entry(Writes.name, Writes(edges)))
    store = zarrify(TIF, plugins=[Layout(Writes.name)])
    assert document(store, PREFIX + "big/zarr.json")["attributes"]["n"] == int("9" * 400)


def test_a_one_segment_name_is_a_usage_error(monkeypatch):
    refused(monkeypatch, Ome06, "not a layout's name", [Layout("nodot")])


def test_a_key_of_more_bytes_than_an_archive_holds_is_a_failure(monkeypatch):
    key = "0/" + "é" * ((65535 - len(PREFIX)) // 2)  # fewer characters than bytes allowed
    fails(monkeypatch, lambda base: {"zarr.json": doc_bytes(), **array_entries(base), key: b"x"},
          "more than an archive's")


def test_a_document_nested_one_deeper_than_256_is_a_failure(monkeypatch):
    fails(monkeypatch, lambda _base: {"zarr.json": nested(257)}, "nested deeper than 256")


def test_a_document_nested_past_python_s_recursion_is_a_failure(monkeypatch):
    fails(monkeypatch, lambda _base: {"zarr.json": nested(100000)}, "nested deeper than 256")


def test_an_integer_of_too_many_digits_is_a_failure(monkeypatch):
    many = b'{"zarr_format":3,"node_type":"group","attributes":{"n":' + b"9" * 5000 + b"}}"
    fails(monkeypatch, lambda _base: {"zarr.json": many}, "not strict JSON")


def test_a_zarr_v2_document_is_a_failure(monkeypatch):
    fails(monkeypatch, lambda _base: {"zarr.json": b'{"zarr_format":2,"node_type":"group"}'}, "Zarr v3 group")


def test_a_document_that_is_no_object_is_a_failure(monkeypatch):
    fails(monkeypatch, lambda _base: {"zarr.json": b"[1, 2]"}, "Zarr v3 group")


def test_an_array_document_without_its_fill_value_is_a_failure(monkeypatch):
    def partial(base):
        doc = document(refkv.ReferenceStore({"zarr.json": copy(base, "0")["zarr.json"]}), "zarr.json")
        del doc["fill_value"]
        return {"zarr.json": doc_bytes(), "0/zarr.json": json.dumps(doc).encode()}
    fails(monkeypatch, partial, "an array's document holds")


def test_an_array_document_whose_shape_is_no_list_is_a_failure(monkeypatch):
    def partial(base):
        doc = document(refkv.ReferenceStore({"zarr.json": copy(base, "0")["zarr.json"]}), "zarr.json")
        doc["shape"] = "64x64"
        return {"zarr.json": doc_bytes(), "0/zarr.json": json.dumps(doc).encode()}
    fails(monkeypatch, partial, "its shape a list")


def test_a_dataset_layout_s_cmo_is_a_failure(monkeypatch):
    cmo = {"uuid": PROFILES["mrc-dataset"][0]}
    fails(monkeypatch, lambda _base: {"zarr.json": doc_bytes({"zarr_conventions": [cmo]})}, "declares a refkv")


def test_a_cmo_name_that_is_null_is_a_failure(monkeypatch):
    cmo = {"uuid": "77777777-7777-4777-8777-777777777777", "name": None}
    fails(monkeypatch, lambda _base: {"zarr.json": doc_bytes({"zarr_conventions": [cmo]})}, "name is not a string")


def test_a_key_that_is_no_string_is_a_failure(monkeypatch):
    fails(monkeypatch, lambda _base: {"zarr.json": doc_bytes(), 5: b"x"}, "not a key of a layout")


def test_copy_refuses_a_boolean_order():
    with pytest.raises(ValueError, match="axis indexes"):
        copy(Base(zarrify(TIF), None), "0", order=[True, False])


def bounded(f, seconds=60):
    """`f()`'s result or its exception, failing the test if it has not returned in `seconds`: a
    plugin's exception that went unhandled would hang the call it was run for."""
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(f)
        try:
            return future.result(timeout=seconds)
        except TimeoutError:
            pytest.fail(f"no answer in {seconds} s: the call hangs")


@pytest.mark.parametrize("error", [KeyboardInterrupt(), asyncio.CancelledError(), GeneratorExit()])
def test_a_plugin_s_base_exception_in_write_fails(monkeypatch, error):
    install(monkeypatch, Entry(Writes.name, Writes(error=error)))
    with pytest.raises(LayoutFailed, match=type(error).__name__):
        bounded(lambda: zarrify(TIF, plugins=[Layout(Writes.name)]))


def test_a_plugin_s_base_exception_in_options_is_a_usage_error(monkeypatch):
    def cancels(_self, _given):
        raise asyncio.CancelledError
    install(monkeypatch, Entry(NAME, variant(options=cancels)))
    with pytest.raises(LayoutUsageError, match="CancelledError"):
        bounded(lambda: zarrify(TIF, plugins=[Layout(NAME)]))


def test_a_plugin_that_exits_while_its_attributes_are_read_is_a_usage_error(monkeypatch):
    class Exits(Ome06):
        @property
        def path(self):
            raise SystemExit(2)
    install(monkeypatch, Entry(NAME, Exits))
    with pytest.raises(LayoutUsageError, match="SystemExit"):
        bounded(lambda: zarrify(TIF, plugins=[Layout(NAME)]))


def test_options_that_nest_the_node_past_256_are_a_usage_error(monkeypatch):
    deep: dict = {}
    for _ in range(260):
        deep = {"a": deep}
    refused(monkeypatch, variant(options=lambda _self, _given: deep), "nest deeper than 253")


def test_given_options_with_nan_are_a_usage_error(monkeypatch):
    refused(monkeypatch, Ome06, "not a JSON object", [Layout(NAME, {"a": float("nan")})])


def test_canonical_options_with_infinity_are_a_usage_error(monkeypatch):
    refused(monkeypatch, variant(options=lambda _self, _given: {"a": float("inf")}), "not a JSON object")


def test_a_refkv_uuid_with_a_uuid_prefix_is_a_failure(monkeypatch):
    cmo = {"uuid": "uuid:" + layouts.UUID}
    fails(monkeypatch, lambda _base: {"zarr.json": doc_bytes({"zarr_conventions": [cmo]})}, "declares a refkv")


def test_a_refkv_uuid_in_braces_is_a_failure(monkeypatch):
    cmo = {"uuid": "{urn:uuid:" + layouts.UUID + "}"}
    fails(monkeypatch, lambda _base: {"zarr.json": doc_bytes({"zarr_conventions": [cmo]})}, "declares a refkv")


def test_zarr_conventions_that_are_an_object_are_a_failure(monkeypatch):
    fails(monkeypatch, lambda _base: {"zarr.json": doc_bytes({"zarr_conventions": {}})}, "not a list of objects")


def test_a_range_of_unhashable_data_is_a_failure(monkeypatch):
    fails(monkeypatch, lambda _base: {"zarr.json": doc_bytes(), "0/zarr.json": doc_bytes(),
                                      "0/c": Range(data=bytearray(b"x"))},  # type: ignore[arg-type] (the mistake)
          "one of the base's references")


def test_a_key_of_a_str_subclass_is_a_failure(monkeypatch):
    class Text(str):
        __slots__ = ()
    fails(monkeypatch, lambda _base: {Text("zarr.json"): doc_bytes()}, "keys are str, not Text")


def test_copy_reads_a_v2_key_s_default_separator():
    """A v2 chunk key encoding without a separator separates by `.`, and a copy of an array
    without dimension names has none."""
    store, _ = transposed([0, 1, 2])
    doc = document(store, "a/zarr.json") | {"chunk_key_encoding": {"name": "v2"}}
    doc.pop("dimension_names")
    mapping = {"zarr.json": store.mapping["zarr.json"], "a/zarr.json": json.dumps(doc).encode(),
               "a/0.0.0": store.mapping["a/c/0/0/0"]}
    entries = copy(Base(refkv.ReferenceStore(mapping), None), "a", order=[2, 1, 0])
    assert set(entries) == {"zarr.json", "0.0.0"}
    assert "dimension_names" not in document(refkv.ReferenceStore({"zarr.json": entries["zarr.json"]}), "zarr.json")



def test_options_253_deep_are_planned(monkeypatch):
    """Canonical options nest up to 253 deep, the node holding them then 256."""
    deep: dict = {}
    for _ in range(252):
        deep = {"a": deep}
    install(monkeypatch, Entry(NAME, variant(options=lambda _self, _given: deep)))
    assert layouts.plan([Layout(NAME)])[0].fresh_options() == deep


def test_options_254_deep_are_a_usage_error(monkeypatch):
    deep: dict = {}
    for _ in range(253):
        deep = {"a": deep}
    refused(monkeypatch, variant(options=lambda _self, _given: deep), "nest deeper than 253")


def test_an_array_document_without_its_chunk_key_encoding_is_a_failure(monkeypatch):
    def partial(base):
        doc = document(refkv.ReferenceStore({"zarr.json": copy(base, "0")["zarr.json"]}), "zarr.json")
        del doc["chunk_key_encoding"]
        return {"zarr.json": doc_bytes(), "0/zarr.json": json.dumps(doc).encode()}
    fails(monkeypatch, partial, "an array's document holds")


def test_an_entry_point_of_no_distribution_is_a_usage_error(monkeypatch):
    entry = Entry(NAME, Ome06)
    entry.dist = None  # type: ignore[assignment] (broken metadata)
    install(monkeypatch, entry)
    with pytest.raises(LayoutUsageError, match="provided by no distribution"):
        zarrify(TIF, plugins=[Layout(NAME)])


def test_an_entry_point_that_names_a_module_is_a_usage_error(monkeypatch):
    install(monkeypatch, Entry(NAME, json))
    with pytest.raises(LayoutUsageError, match="names the module json"):
        zarrify(TIF, plugins=[Layout(NAME)])


def test_options_with_a_member_not_named_by_a_string_are_a_usage_error(monkeypatch):
    refused(monkeypatch, Ome06, "named by strings", [Layout(NAME, {"a": {1: "b"}})])


def test_dimension_names_that_are_no_strings_are_refused():
    with pytest.raises(ValueError, match="each a string or None"):
        copy(Base(zarrify(TIF), None), "0", dimension_names=[0, 1])  # type: ignore[list-item]


def test_a_chunk_key_is_no_document_of_the_base():
    with pytest.raises(KeyError, match="no document '0/c/0/0'"):
        Base(zarrify(TIF), None).document("0/c/0/0")


def test_a_refkv_uuid_in_braces_twice_is_no_refkv_uuid():
    """A UUID is read with at most one prefix and one pair of braces, so a twice-braced refkv UUID
    is not refkv's (nor any UUID), and a non-ASCII one is none either."""
    assert layouts._uuid_form("{{" + layouts.UUID + "}}") is None
    assert layouts._uuid_form("uuid:uuid:" + layouts.UUID) is None
    assert layouts._uuid_form("{uuid:" + layouts.UUID.upper() + "}") == layouts.UUID
    assert layouts._uuid_form(layouts.UUID.replace("e", "\u212a")) is None
