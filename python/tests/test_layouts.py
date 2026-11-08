"""Layouts plugins write (spec/layout.md; refkv.zarrify.layouts): the output a plugin returns,
written as it is; the base and its IR; `cut`, `concat` and `payload_size`; `copy`, `array`, `group`
and `check`; the coding routines; and each way a layout request is refused or fails. The shipped
examples have tests of their own (test_example_*.py)."""

import ast
import json
from pathlib import Path
from typing import Any, override

import h5py
import numpy as np
import pytest
import zarr
import zarr.storage

import refkv
import refkv.zarr
from refkv import Concat, Range
from refkv.zarrify import Image, Rejected, layouts, zarrify
from refkv.zarrify.__main__ import main
from refkv.zarrify.coding import NoCodec, hdf5_attributes, tiff_byte_order, tiff_codecs, tiff_data_type, tiff_tags
from refkv.zarrify.layouts import (
    Base,
    Layout,
    LayoutFailed,
    LayoutRejected,
    LayoutUsageError,
    Written,
    base,
    concat,
    cut,
    dump,
    encode_entries,
    fill_value,
    payload_size,
)
from refkv.zarrify.layouts.hierarchy import array, check, chunk_coords, chunk_key, copy, group, node_name

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures"
TIFF = str(FIXTURES / "tiff" / "jpeg_aperio_rgb.tif")
NWB = str(FIXTURES / "hdf5" / "nwb_ophys.nwb")
NETCDF = str(FIXTURES / "hdf5" / "hdf5_netcdf4.h5")


class Plugin:
    """A plugin whose write returns `make(base)`."""

    def __init__(self, make, name="acme.test", formats="*"):
        self.name, self.formats, self.make = name, formats, make

    def options(self, given):
        return dict(given)

    def write(self, b, options):  # noqa: ARG002 (the plugin protocol's signature)
        return self.make(b)


class Dist:
    def __init__(self, name):
        self.name = name


class Entry:
    """An entry point of the group refkv.layouts, as importlib.metadata gives one."""

    def __init__(self, name, plugin, dist=None):
        self.name, self._plugin, self.dist = name, plugin, Dist(dist or name.split(".")[0])

    def load(self):
        if isinstance(self._plugin, BaseException):
            raise self._plugin
        return self._plugin


def install(monkeypatch, *entries):
    """Makes `entries` the installed plugins."""
    monkeypatch.setattr(layouts, "_entry_points", lambda: entries)


def read(store, path=""):
    """The values of the array at `path` of a store."""
    return zarr.open_array(refkv.zarr.store(store), path=path, mode="r")[...]


def images(b):
    """The base's image arrays, copied to the same paths, under a root group of the plugin's."""
    out: dict[str, Any] = {"zarr.json": group({"by": "acme"})}
    for k in b.keys():  # noqa: SIM118 (Base.keys, not a dict's)
        if k.endswith("/zarr.json") and not k.startswith("refkv_source/") and b.document(k)["node_type"] == "array":
            path = k[:-len("/zarr.json")]
            out |= {f"{path}/{kk}": v for kk, v in copy(b, path).items()}
    return out


def one(value):
    """A TIFF tag's one value, as `tiff_tags` gives it: a list of one."""
    assert isinstance(value, list)
    return int(value[0])


def doc_of(value):
    """A document entry, parsed."""
    assert isinstance(value, bytes)
    return json.loads(value)


def part(item):
    """An `elements` item that names an element: (element, member, start, stop)."""
    assert isinstance(item, tuple)
    return item


def tiles_from_ir(b):
    """The TIFF base's level 0 rebuilt from its IR: its tiles' references, its tags' codecs."""
    ifd = b.ir.index("ifds/0")
    tags = tiff_tags(b, ifd)
    tiles = b.ir.index("ifds/0/tiles")
    form = b.form(tiles, 0)
    assert form is not None
    shape = form["geometry"]["shape"]
    width, length, spp = one(tags[256]), one(tags[257]), one(tags[277])
    across = -(-width // shape[1])
    codecs = [{"name": "transpose", "configuration": {"order": [1, 2, 0]}},
              *tiff_codecs(tags, little_endian=True, x_axis=1, row_length=shape[1] * spp)]
    numbers = b.ir.members(tiles)[0]
    chunks = {(0, j // across, j % across): b.reference(tiles, k) for k, j in enumerate(numbers)}
    return {f"img/{k}": v for k, v in array([spp, length, width], tiff_data_type(tags), [spp, *shape], codecs,
                                             chunks).items()} | {"zarr.json": group()}


@pytest.mark.parametrize(("source", "make", "checksums"), [
    (TIFF, images, False),
    (TIFF, lambda b: b.entries(), False),  # refkv's whole output, as it is
    (TIFF, tiles_from_ir, False),
    (TIFF, tiles_from_ir, True),  # the plugin's own ranges get checksums
    (NWB, lambda b: {"zarr.json": group(), **{f"m/{k}": v for k, v in copy(
        b, "stimulus/templates/movie/data", order=[0, 2, 1], attributes=b.attributes(
            "stimulus/templates/movie/data")).items()}}, False),
    (TIFF, lambda b: Written(images(b), read_at_open=["zarr.json"], stored=["0/zarr.json"]), False),
    (TIFF, lambda b: images(b) | {"z": concat(b.zeros(70000), b.zeros(10))}, False),  # a zero source refkv adds
])
def test_a_plugin_writes_the_whole_output(source, make, checksums):
    """The output is exactly what the plugin returns: its keys, its documents, the base's
    references (checksums and all) and its own from the IR, which read the base's values; its
    entries read at open are those it names, or its documents; `info` names the layout."""
    written = make(base(source, checksums=checksums))
    entries = written.entries if isinstance(written, Written) else written
    store = zarrify(source, checksums=checksums, layout=Layout(Plugin(make)))
    assert set(store.mapping) == set(entries)
    assert store.info["layouts"] == ["acme.test"]
    plain = zarrify(source)
    for k, v in store.mapping.items():
        if isinstance(v, bytes):
            assert v == entries[k]
        else:
            for r in (v.parts if isinstance(v, Concat) else (v,)):
                url = r.data is None and store._sources[r.source].url is not None
                assert (r.crc32c is not None) == (checksums and url)
    if make is tiles_from_ir:
        assert np.array_equal(read(store, "img"), read(plain, "0"))
    if isinstance(written, Written):
        assert store.read_at_open == frozenset(["zarr.json"])
        assert "0/zarr.json" not in store.compressed
    else:
        documents = {k for k in entries if k.endswith("zarr.json") and not k.startswith("refkv_source/")}
        assert documents <= store.read_at_open
    if "z" in entries:  # the zero data source the plugin asked for, after the base's sources
        assert store._sources[:-1] == plain._sources
        assert store._sources[-1].data == bytes(70000)
        assert store.get("z") == bytes(70010)
    else:
        assert store._sources == plain._sources


@pytest.mark.parametrize(("source", "array_path", "order"), [
    ("tiff/bilevel_lzw_strips_short.tif", "0", [1, 0]),  # sharded
    ("tiff/strips_rgb_contig_deflate_short.tif", "0", [1, 2, 0]),  # rectilinear, a transpose first
    ("tiff/strips_rgb_contig_deflate_short.tif", "0", [0, 1, 2]),
    ("hdf5/nwb_ophys.nwb", "stimulus/templates/movie/data", [2, 0, 1]),
])
def test_copy_transposes_an_array_to_the_same_values(source, array_path, order):
    """`copy` with `order` decodes to the base's values transposed by `order`, as numpy.transpose
    takes it, for sharded, rectilinear and transposed arrays alike, and `check` finds a copy one."""
    b = base(str(FIXTURES / source))
    store = zarrify(str(FIXTURES / source))
    entries = copy(b, array_path, order=order)
    copied = refkv.ReferenceStore(entries, store._sources)
    assert np.array_equal(read(copied), np.transpose(read(store, array_path), order))
    assert doc_of(entries["zarr.json"])["attributes"] == {}
    out = {"zarr.json": group(), **copy(b, array_path, order=order, to="x")}
    assert out.keys() == {"zarr.json", *(f"x/{k}" for k in entries)}
    assert check(b, out) == []


def synthetic(doc, chunks):
    """A base of one array `a` of document `doc` and `chunks` (key -> bytes), built by hand."""
    root = {"zarr_format": 3, "node_type": "group",
            "attributes": {"refkv_zarrified": {"profile": "tiff", "version": 0, "source": {"url": "x.tif"}}}}
    return Base(refkv.ReferenceStore({"zarr.json": json.dumps(root).encode(), "a/zarr.json": json.dumps(doc).encode(),
                                      **{f"a/{k}": v for k, v in chunks.items()}}), None)


def plain_array(shape, chunk, encoding=None, codecs=None, names=None):
    doc = {"zarr_format": 3, "node_type": "array", "shape": shape, "data_type": "uint16",
           "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": chunk}},
           "chunk_key_encoding": encoding or {"name": "default"}, "fill_value": 0,
           "codecs": codecs or [{"name": "bytes", "configuration": {"endian": "little"}}], "attributes": {}}
    if names:
        doc["dimension_names"] = names
    return doc


@pytest.mark.parametrize(("encoding", "shape", "keys", "order"), [
    ({"name": "default", "configuration": {"separator": "/"}}, [2, 3], ["c/0/0", "c/1/0"], [1, 0]),
    ({"name": "default", "configuration": {"separator": "."}}, [2, 3], ["c.0.0", "c.1.0"], [1, 0]),
    ({"name": "v2", "configuration": {"separator": "."}}, [2, 3], ["0.0", "1.0"], [1, 0]),
    ({"name": "v2", "configuration": {"separator": "/"}}, [2, 3], ["0/0", "1/0"], [1, 0]),
])
def test_copy_follows_the_array_s_chunk_key_encoding(encoding, shape, keys, order):
    """`copy` permutes a chunk key's indexes in the array's own encoding and separator."""
    values = np.arange(int(np.prod(shape)), dtype="<u2").reshape(shape)
    rows = [values[i].tobytes() for i in range(shape[0])]
    b = synthetic(plain_array(shape, [1, *shape[1:]], encoding), dict(zip(keys, rows, strict=True)))
    assert np.array_equal(read(refkv.ReferenceStore(copy(b, "a", order=order))), np.transpose(values, order))


@pytest.mark.parametrize(("t", "order"), [([1, 2, 0], [2, 0, 1]), ([1, 2, 0], [1, 0, 2]), ([2, 0, 1], [0, 2, 1]),
                                          ([1, 0, 2], [1, 0, 2])])
def test_copy_composes_its_transpose_with_the_base_s(t, order):
    """A copy of an array that starts with a transpose decodes to the base's values transposed, and
    drops the transpose when the two cancel; its dimension names are permuted with it."""
    values = np.arange(24, dtype="<u2").reshape(2, 3, 4)
    codecs = [{"name": "transpose", "configuration": {"order": t}},
              {"name": "bytes", "configuration": {"endian": "little"}}]
    b = synthetic(plain_array([2, 3, 4], [2, 3, 4], codecs=codecs, names=["a", "b", "c"]),
                  {"c/0/0/0": np.ascontiguousarray(np.transpose(values, t)).tobytes()})
    entries = copy(b, "a", order=order)
    assert np.array_equal(read(refkv.ReferenceStore(entries)), np.transpose(values, order))
    doc = doc_of(entries["zarr.json"])
    assert doc["dimension_names"] == [["a", "b", "c"][o] for o in order]
    inverse = [order.index(i) for i in range(3)]
    assert doc["codecs"][0]["name"] == ("bytes" if [inverse[i] for i in t] == [0, 1, 2] else "transpose")


def test_copy_transposes_a_shard_s_inner_chunks_and_index():
    """`copy` transposes a sharded array of several inner chunks per shard to the base's values."""
    values = np.arange(24, dtype="<u2").reshape(4, 6)
    written = zarr.storage.MemoryStore()
    a = zarr.create_array(written, shape=(4, 6), shards=(4, 6), chunks=(2, 3), dtype="uint16", fill_value=0,
                          compressors=None)
    a[...] = values
    doc = json.loads(written._store_dict["zarr.json"].to_bytes())
    b = synthetic(doc, {k: v.to_bytes() for k, v in written._store_dict.items() if k != "zarr.json"})
    assert np.array_equal(read(refkv.ReferenceStore(copy(b, "a", order=[1, 0]))), values.T)


def test_the_base_bridges_its_output_and_its_ir():
    """`reference` reads an element (a JPEG tile, with its tables) as the base's chunk does;
    `elements` maps each part of a chunk to the bytes of an element it reads; `element_at` finds
    an element from a byte; `attributes` drops refkv's declarations; `source_metadata` gives the
    node's own; `form` the element's geometry."""
    b = base(TIFF)
    tiles = b.ir.index("ifds/0/tiles")
    _, chunks = b.array("0")
    first = chunks["c/0/0/0"]
    assert isinstance(first, Concat)
    assert b.reference(tiles, 0) == first
    assert b.reference(b.ir.index("ifds/0/tiles/0")) == first
    parts = b.elements("0")["c/0/0/0"]
    assert parts[1] is None  # the JPEG tables, a data source
    assert part(parts[0])[0] == part(parts[2])[0] == (b.element_at(0, first.parts[0].offset) or (None,))[0]
    assert (b.form(tiles, 0) or {})["geometry"]["shape"] == [32, 32]
    assert "refkv_zarrified" not in b.attributes("")
    assert b.source_metadata("") is not None
    h = base(NETCDF)
    (item,) = h.elements("sst")["c/0/0"]
    i, member, start, stop = part(item)
    assert h.reference(i, member) == cut(h.reference(i, member), start, stop)
    assert h.zero_source is None
    assert h.checksums is False
    assert b.members(tiles)[0] == (0, first.parts[0].offset, b.ir.extent(b.ir.index("ifds/0/tiles/0"))[1])
    assert (b.datasets, b.mirror, b.node_type("0")) == ({}, "refkv_source", "array")
    assert "0" in b.nodes()
    assert "refkv_source" not in b.nodes()
    assert "refkv_source" in b.nodes(mirror=True)
    assert not any(k.startswith("refkv_source/") for k in b.entries(mirror=False))
    assert b.zeros(10) == base(TIFF).zeros(10) == Range(len(b.sources), 0, 10)  # whatever Base made it
    assert "refkv_zarrified" not in doc_of(b.entries("zarr.json", declarations=False)["zarr.json"])["attributes"]
    assert "0/zarr.json" in b
    assert "nope" not in b
    s = base(str(FIXTURES / "tiff" / "ome_set_a.ome.tif"))  # a set read as a store: each file its own source
    strips = s.ir.index("ome_set_b.ome.tif/ifds/0/strips")
    assert s.source_of(strips) == 1
    assert s.reference(strips, 0) in s.array("0")[1].values()
    x = base(str(FIXTURES / "hdf5" / "hdf5_external.h5"))  # an external link's target, in a file of its own
    (item,) = x.elements("linked_dataset")["c/0/0"]
    j, m, a, z = part(item)
    assert cut(x.reference(j, m), a, z) == x.array("linked_dataset")[1]["c/0/0"]


@pytest.mark.parametrize(("value", "start", "stop", "want"), [
    (Range(0, 10, 5), 1, 3, Range(0, 11, 2)),
    (Concat((Range(0, 10, 5), Range(data=b"abc"), Range(1, 0, 4))), 4, 9, Concat((Range(0, 14, 1), Range(data=b"abc"),
                                                                                   Range(1, 0, 1)))),
    (Concat((Range(0, 10, 5), Range(1, 0, 4))), 5, 9, Range(1, 0, 4)),
])
def test_cut_concat_and_payload_size(value, start, stop, want):
    """`cut` keeps the bytes asked for, parts of a data source as ranges of it; `concat` joins
    values flat; `payload_size` is the encoded size, with room for each source range's checksum."""
    got = cut(value, start, stop)
    assert got == want
    assert concat(cut(value, 0, start), got, cut(value, stop, value.size)).size == value.size
    assert payload_size(Range(0, 1, 2)) == len(Range(0, 1, 2).encode())
    assert payload_size(got, checksums=True) == payload_size(got) + 6 * sum(r.data is None for r in (
        got.parts if isinstance(got, Concat) else (got,)))


@pytest.mark.parametrize("source", [TIFF, NWB, NETCDF, str(FIXTURES / "hdf5" / "hdf5_filters.h5"),
                                    str(FIXTURES / "tiff" / "strips_rgb_contig_deflate_short.tif")])
def test_check_finds_nothing_in_the_base_s_own_output(source):
    assert check(base(source), base(source).entries()) == []


def bad(b):
    """Outputs of the base `b`, each with one problem, by the code check gives it."""
    entries = images(b)
    chunk = next(k for k in entries if k.startswith("0/c/"))
    doc = json.loads(entries["0/zarr.json"])
    return {
        "key": {**entries, "__vz__/x": b"1"},
        "reference": {**entries, "x": Range(0, 0, 10 ** 9)},
        "open": Written(entries, read_at_open=[chunk]),
        "document": {**entries, "y/zarr.json": entries[chunk]},
        "orphan": {**entries, "z/c/0": b"1"},
        "json": {**entries, "j/zarr.json": b'{"a": NaN}'},
        "zarr": {**entries, "q/zarr.json": dump({"zarr_format": 3, "node_type": "group", "attributes": {}, "x": 1})},
        "parent": {**entries, "p/q/zarr.json": group()},
        "copy": {**entries, "0/zarr.json": dump(doc | {"shape": [d + 1 for d in doc["shape"]]})},
        "refkv": {**entries, "zarr.json": dump({"zarr_format": 3, "node_type": "group", "attributes": {
            "refkv_zarrified": {"profile": "tiff"}}})},
        "chunk": {**entries, "0/c/9/9/9": entries[chunk]},
        "consolidated": {**entries, "v/.zgroup": dump({"zarr_format": 2}),
                         "v/.zmetadata": dump({"zarr_consolidated_format": 1, "metadata": {}})},
    }


@pytest.mark.parametrize("code", ["key", "reference", "open", "document", "orphan", "json", "zarr", "parent", "copy",
                                  "refkv", "chunk", "consolidated"])
def test_check_reports_each_problem_by_its_code(code):
    b = base(TIFF)
    assert code in {p.code for p in check(b, bad(b)[code])}


def test_check_reports_a_chunk_of_the_wrong_size():
    b = base(NETCDF)
    (item,) = b.elements("counts")["c/0"]
    i, member, _, _ = part(item)
    doc = doc_of(copy(b, "counts")["zarr.json"])
    whole = b.reference(i, member)
    out = {"zarr.json": group(), "s/zarr.json": dump(doc), "s/c/0": cut(whole, 0, whole.size - 1)}
    assert "size" in {p.code for p in check(b, out)}


def test_the_coding_routines_give_the_base_s_codecs():
    b = base(TIFF)
    tags = tiff_tags(b, b.ir.index("ifds/0"))
    doc = b.document("0/zarr.json")
    assert tiff_data_type(tags) == doc["data_type"]
    assert tiff_codecs(tags, little_endian=tiff_byte_order(b, b.ir.index("ifds/0")), x_axis=1,
                       row_length=96) == doc["codecs"][1:]
    assert tiff_codecs({259: [65001], 258: [1]}, little_endian=True, x_axis=1, row_length=8, chunk_shape=[4, 8]) == [
        {"name": "imagecodecs_eer", "configuration": {"shape": [4, 8], "skipbits": 7, "horzbits": 2, "vertbits": 2}}]
    # a netCDF _FillValue's Base64 binary64, undone (spec/zarrify/hdf5.md §6
    # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#6-attributes>)
    assert hdf5_attributes(base(NETCDF), "sst")["_FillValue"] == -1.5


def test_fits_says_why_refkv_would_not_write_a_value():
    b = base(TIFF)
    assert b.fits(b.array("0")[1]["c/0/0/0"]) is None
    assert b.fits(Range(0, 0, 4)) is None
    assert "past" in (b.fits(Range(0, 0, 10 ** 9)) or "")
    assert "payload" in (b.fits(Concat(tuple(Range(data=b"ab") for _ in range(20000)))) or "")


def test_chunk_keys_node_names_and_encoded_entries():
    """`chunk_key` and `chunk_coords` are inverse in an array's encoding; `node_name` takes a
    Zarr v3 node's name; `encode_entries` is one encoding of entries, checksums aside."""
    for encoding, coords, key in [({"name": "default"}, [1, 2], "c/1/2"),
                                  ({"name": "v2", "configuration": {"separator": "."}}, [1, 2], "1.2"),
                                  ({"name": "default", "configuration": {"separator": "."}}, [], "c")]:
        doc = {"chunk_key_encoding": encoding}
        assert chunk_key(coords, doc) == key
        assert chunk_coords(key, doc) == coords
    node_name("image-0")
    one = {"a": b"1", "b": Range(0, 0, 4)}
    assert encode_entries(one) == encode_entries({"b": Range(0, 0, 4, crc32c=5), "a": b"1"})
    assert encode_entries(one) != encode_entries({"a": b"1", "b": Concat((Range(0, 0, 4),))})


@pytest.mark.parametrize("name", ["", "a/b", ".", "..", "__x", "zarr.json", 5])
def test_a_name_that_is_no_node_s_is_refused(name):
    with pytest.raises(ValueError, match="node's name"):
        node_name(name)  # type: ignore[arg-type]


def test_a_coding_routine_refuses_what_the_profile_rejects():
    with pytest.raises(NoCodec, match="unsupported compression 6"):
        tiff_codecs({259: [6], 258: [8]}, little_endian=True, x_axis=1, row_length=8)


def test_examples_import_only_refkv_s_public_api():
    """Each shipped example imports only the standard library, refkv's public modules and their
    public names, so that it can be copied out of refkv and changed."""
    public = {"refkv", "refkv.ir", "refkv.zarrify", "refkv.zarrify.layouts", "refkv.zarrify.layouts.hierarchy",
              "refkv.zarrify.coding"}
    for path in (ROOT / "python" / "src" / "refkv" / "zarrify" / "layouts" / "examples").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "refkv":
                assert node.module in public, f"{path.name}: {node.module}"
                assert not any(a.name.startswith("_") for a in node.names), f"{path.name}: {node.module}"
            elif isinstance(node, ast.Import):
                for a in node.names:
                    assert a.name.split(".")[0] != "refkv" or a.name in public, f"{path.name}: {a.name}"


# ---- the errors: one test each

def test_an_unknown_plugin_is_a_usage_error(monkeypatch):
    install(monkeypatch)
    with pytest.raises(LayoutUsageError, match="no layout plugin is named"):
        zarrify(TIFF, layout=Layout("acme.none"))


def test_a_name_not_of_its_distribution_is_a_usage_error(monkeypatch):
    install(monkeypatch, Entry("acme.x", Plugin(images, name="acme.x"), dist="other"))
    with pytest.raises(LayoutUsageError, match="starts with its distribution"):
        zarrify(TIFF, layout=Layout("acme.x"))


def test_a_plugin_that_names_itself_otherwise_is_a_usage_error(monkeypatch):
    install(monkeypatch, Entry("acme.x", Plugin(images, name="acme.y")))
    with pytest.raises(LayoutUsageError, match="set its name"):
        zarrify(TIFF, layout=Layout("acme.x"))


def test_a_plugin_that_fails_to_load_is_a_usage_error(monkeypatch):
    install(monkeypatch, Entry("acme.x", ImportError("no zarr_metadata: pip install refkv[layouts]")))
    with pytest.raises(LayoutUsageError, match="refkv\\[layouts\\]"):
        zarrify(TIFF, layout=Layout("acme.x"))


def test_two_entry_points_of_one_name_are_a_usage_error(monkeypatch):
    install(monkeypatch, Entry("acme.x", Plugin(images, name="acme.x")), Entry("acme.x", Plugin(images, name="acme.x")))
    with pytest.raises(LayoutUsageError, match="2 entry points"):
        zarrify(TIFF, layout=Layout("acme.x"))


@pytest.mark.parametrize("formats", [["tiff"], {"tiff": ["0"]}, {"nope": {0}}, "tiff"])
def test_formats_that_are_not_formats_are_a_usage_error(formats):
    with pytest.raises(LayoutUsageError, match="formats"):
        zarrify(TIFF, layout=Layout(Plugin(images, formats=formats)))


def test_a_bad_name_is_a_usage_error():
    with pytest.raises(LayoutUsageError, match=r"not <distribution>\.<layout>"):
        zarrify(TIFF, layout=Layout(Plugin(images, name="Acme")))


def test_options_the_plugin_refuses_are_a_usage_error():
    class Refusing(Plugin):
        @override
        def options(self, given):
            raise ValueError("no")
    with pytest.raises(LayoutUsageError, match="refused"):
        zarrify(TIFF, layout=Layout(Refusing(images)))


def test_options_that_are_not_json_are_a_usage_error():
    with pytest.raises(LayoutUsageError, match="not a JSON object"):
        zarrify(TIFF, layout=Layout(Plugin(images), {"x": float("nan")}))


def test_canonical_options_that_are_not_json_are_a_usage_error():
    class Tuples(Plugin):
        @override
        def options(self, given):
            return {"t": (1, 2)}
    with pytest.raises(LayoutUsageError, match="not JSON as they are"):
        zarrify(TIFF, layout=Layout(Tuples(images)))


def test_options_that_raise_otherwise_are_a_failure():
    class Broken(Plugin):
        @override
        def options(self, given):
            raise KeyError("bug")
    with pytest.raises(LayoutFailed, match="options\\(\\) raised"):
        zarrify(TIFF, layout=Layout(Broken(images)))


def test_a_layout_beside_built_in_layouts_is_a_usage_error():
    with pytest.raises(LayoutUsageError, match="not both"):
        zarrify(TIFF, layouts=[Image()], layout=Layout(Plugin(images)))


def test_a_layout_that_is_not_a_layout_is_a_usage_error():
    with pytest.raises(LayoutUsageError, match="one Layout"):
        zarrify(TIFF, layout=[Layout(Plugin(images))])  # type: ignore[arg-type]


def test_a_layout_on_a_store_input_is_a_usage_error(tmp_path):
    with pytest.raises(LayoutUsageError, match="store input"):
        zarrify(str(tmp_path), "https://example.org/x/", layout=Layout(Plugin(images)))


def test_a_base_the_plugin_does_not_read_is_rejected():
    with pytest.raises(LayoutRejected, match="does not read the tiff"):
        zarrify(TIFF, layout=Layout(Plugin(images, formats={"hdf5": {0}})))


def test_a_plugin_s_rejection_rejects_the_layout_request():
    def rejects(_b):
        raise Rejected("not this one")
    with pytest.raises(LayoutRejected, match="not this one"):
        zarrify(TIFF, layout=Layout(Plugin(rejects)))


@pytest.mark.parametrize("error", [KeyError("bug"), SystemExit(1)])
def test_a_plugin_s_exception_is_a_failure(error):
    def raises(_b):
        raise error
    with pytest.raises(LayoutFailed, match=type(error).__name__):
        zarrify(TIFF, layout=Layout(Plugin(raises)))


def test_a_write_that_returns_no_mapping_is_a_failure():
    with pytest.raises(LayoutFailed, match="not a mapping"):
        zarrify(TIFF, layout=Layout(Plugin(lambda _b: [("zarr.json", group())])))


# the ids are short: pytest puts a test's id in an environment variable, which Windows bounds
@pytest.mark.parametrize("key", ["", "__vz__/x", "x" * 65536, "\ud800", 5],
                         ids=["empty", "reserved", "long", "surrogate", "int"])
def test_a_key_the_archive_cannot_hold_is_a_failure(key):
    with pytest.raises(LayoutFailed, match="key"):
        zarrify(TIFF, layout=Layout(Plugin(lambda b: {**images(b), key: b"1"})))


@pytest.mark.parametrize("value", [
    Range(0, 0, 10 ** 9),  # past the file
    Range(7, 0, 1),  # no such source
    Range(0, -1, 2),
    Range(True, 0, 1),
    Range(data=b"x", offset=1),
    Concat([Range(0, 0, 1)]),  # type: ignore[arg-type] (parts not a tuple)
    "text",
])
def test_a_reference_refkv_does_not_write_is_a_failure(value):
    with pytest.raises(LayoutFailed, match="x:"):
        zarrify(TIFF, layout=Layout(Plugin(lambda b: {**images(b), "x": value})))


def test_a_payload_past_the_limit_is_a_failure():
    big = Concat(tuple(Range(data=b"ab") for _ in range(20000)))
    with pytest.raises(LayoutFailed, match="payload"):
        zarrify(TIFF, layout=Layout(Plugin(lambda b: {**images(b), "x": big})))


def test_read_at_open_naming_no_bytes_entry_is_a_failure():
    with pytest.raises(LayoutFailed, match="not a bytes entry"):
        zarrify(TIFF, layout=Layout(Plugin(lambda b: Written(images(b), read_at_open=["nope"]))))


def test_more_read_at_open_than_a_reader_reads_is_a_failure(monkeypatch):
    monkeypatch.setattr(layouts, "MAX_OPEN", 10)
    with pytest.raises(LayoutFailed, match="read at open"):
        zarrify(TIFF, layout=Layout(Plugin(images)))


def test_the_cli_writes_a_plugin_s_output(monkeypatch, tmp_path, capsys):
    install(monkeypatch, Entry("acme.test", Plugin(images)))
    assert main([TIFF, str(tmp_path / "o.vzip"), "--layout", "acme.test", "--layout-options", "{}"]) == 0
    assert json.loads(capsys.readouterr().out)["layouts"] == ["acme.test"]
    assert refkv.open(str(tmp_path / "o.vzip")).get("0/zarr.json") is not None


@pytest.mark.parametrize(("args", "status"), [
    (["--layout", "acme.none"], 2),
    (["--layout-options", "{}"], 2),
    (["--layout", "acme.test", "--layout", "acme.test"], 2),
    (["--layout", "acme.test", "--layout-options", '{"a": 1, "a": 2}'], 2),
    (["--layout", "acme.test", "--layout-options", "[]"], 2),
])
def test_the_cli_exits_2_for_a_layout_asked_for_wrongly(monkeypatch, tmp_path, args, status):
    install(monkeypatch, Entry("acme.test", Plugin(images)))
    assert main([TIFF, str(tmp_path / "o.vzip"), *args]) == status


def test_the_cli_exits_3_for_a_rejected_layout(monkeypatch, tmp_path):
    install(monkeypatch, Entry("acme.test", Plugin(images, formats={"hdf5": {0}})))
    assert main([TIFF, str(tmp_path / "o.vzip"), "--layout", "acme.test"]) == 3


def test_the_cli_exits_1_for_a_failed_layout(monkeypatch, tmp_path):
    install(monkeypatch, Entry("acme.test", Plugin(lambda _b: {"x": "text"})))
    assert main([TIFF, str(tmp_path / "o.vzip"), "--layout", "acme.test"]) == 1


def _lazy_failure():
    yield "zarr.json"
    raise ValueError("lazy bug")


def _lazy_rejection():
    yield "zarr.json"
    raise Rejected("not this one")


@pytest.mark.parametrize(("make", "error"), [
    (lambda b: Written(b.entries(), read_at_open=_lazy_failure()), LayoutFailed),
    (lambda b: Written(b.entries(), stored=_lazy_rejection()), LayoutRejected),
])
def test_plugin_code_refkv_runs_after_write_is_the_plugin_s(make, error):
    """A generator or mapping `write` returns runs the plugin's code when refkv reads it: what it
    raises is the plugin's failure, or its rejection, as from `write` itself."""
    with pytest.raises(error):
        zarrify(TIFF, layout=Layout(Plugin(make)))


@pytest.mark.parametrize("extra", [
    {"g/zarr.json": dump({"zarr_format": 3, "node_type": "group", "consolidated_metadata": {"metadata": "ab"}})},
    {"g/zarr.json": group(), "g/a/zarr.json": dump({"node_type": "array", "chunk_key_encoding": "x"}), "g/a/c/0": b"1"},
    {"a/zarr.json": b"[" * 100000 + b"]" * 100000},
])
def test_check_reports_malformed_documents_rather_than_raising(extra):
    b = base(TIFF)
    assert {p.code for p in check(b, b.entries(mirror=False) | extra)} & {"zarr", "json", "chunk"}


def test_a_chunk_is_its_elements_bytes():
    """`elements` names every element a part runs across (strips the profile joined into one
    range), and the references of those bytes, joined, are the base's chunk; `reference` reads a
    recipe's `rows` step (an ND2 frame's padded rows) as spec/conventions.md §8.5
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#85-forms-and-recipes> says."""
    b = base(str(FIXTURES / "tiff" / "bilevel_strips_short.tif"))
    value = b.array("0")[1]["c/0/0"]
    assert isinstance(value, Range)
    pieces = [part(item) for item in b.elements("0")["c/0/0"]]
    assert len(pieces) > 1
    assert concat(*(cut(b.span(i, m), a, z) for i, m, a, z in pieces), merge=True) == value
    j = base(TIFF)  # a JPEG tile's span is its own bytes, where its reference splices in the tables
    jpeg = j.array("0")[1]["c/0/0/0"]
    assert isinstance(jpeg, Concat)
    rebuilt: list[Range | Concat] = []
    for item, lit in zip(j.elements("0")["c/0/0/0"], jpeg.parts, strict=True):
        if item is None:  # the tables, a data source
            rebuilt.append(lit)
        else:
            i, m, a, z = part(item)
            rebuilt.append(cut(j.span(i, m), a, z))
    assert concat(*rebuilt) == jpeg
    n = base(str(FIXTURES / "nd2" / "nd2_padded_rgb.nd2"))
    frame, _, _, _ = part(n.elements("0/0")["c/0/0/0/0"][0])
    assert n.reference(frame) == n.array("0/0")[1]["c/0/0/0/0"]


@pytest.mark.parametrize(("key", "value"), [("c/01/0", None), ("c/1/0", b"12")])
def test_check_reports_a_chunk_a_reader_does_not_read(key, value):
    """A key the encoding does not give (`c/01/0`, read by no reader) is `chunk`; a bytes chunk of
    a fixed-size array short of its bytes is `size`."""
    b = base(TIFF)
    rows = {(0, 0): bytes(8), (1, 0): bytes(8)}
    a = array([2, 4], "uint16", [1, 4], [{"name": "bytes", "configuration": {"endian": "little"}}], rows, to="a")
    out = {"zarr.json": group(), "a/zarr.json": a["a/zarr.json"], "a/c/0/0": a["a/c/0/0"],
           f"a/{key}": value if value is not None else a["a/c/1/0"]}
    codes = {p.code for p in check(b, out)}
    assert codes == ({"chunk"} if value is None else {"size"})


def test_fill_value_reads_bfloat16_bits_as_a_float32_s_high_half():
    assert fill_value({"data_type": "bfloat16", "fill_value": "0x3f80"}) == 1.0
    assert fill_value({"data_type": "float32", "fill_value": "0x3f800000"}) == 1.0


def test_check_finds_a_copy_by_its_transposes():
    """A copy of an array of axes of one length (all 2s) is found by its transposes, not by
    trying permutations, and a datetime array's chunks are sized by its element's 8 bytes."""
    n = base(str(FIXTURES / "nifti" / "nifti_n2_le_complex128_5d.nii"))
    assert check(n, {"zarr.json": group(), **copy(n, "0", order=[4, 1, 3, 0, 2], to="x")}) == []
    dt = {"name": "numpy.datetime64", "configuration": {"unit": "ns", "scale_factor": 1}}
    out = {"zarr.json": group(), **array([4], dt, [4], [{"name": "bytes", "configuration": {"endian": "little"}}],
                                         {(0,): bytes(16)}, to="a")}
    assert {p.code for p in check(base(TIFF), out)} == {"size"}


def test_values_refkv_encodes_decode_to_python_values():
    assert hdf5_attributes(base(str(FIXTURES / "hdf5" / "hdf5_attributes.h5")), "attrs")["latin1"] == "café"
    assert fill_value({"data_type": "float8_e8m0fnu", "fill_value": "NaN"}) != fill_value(
        {"data_type": "float8_e8m0fnu", "fill_value": "NaN"})  # a NaN: unequal to itself


def test_a_member_in_a_derived_space_has_no_reference():
    """A struct's members inside an inflated stream (a FITS file in gzip) are no bytes of the
    file: `reference` and `span` refuse them, as they refuse the struct."""
    b = base(str(FIXTURES / "fits" / "fits_gzip_mef.fits.gz"))
    hdu = b.ir.index("stream/hdus/1")
    for read in (b.reference, b.span):
        with pytest.raises(ValueError, match="derived space"):
            read(hdu, 0)


def test_check_reports_a_copy_that_leaves_a_chunk_out_and_a_dot_node():
    b = base(str(FIXTURES / "tiff" / "bilevel_tiled.tif"))
    full = copy(b, "0", to="img")
    assert {p.code for p in check(b, {"zarr.json": group(), **{k: v for k, v in full.items()
                                                               if k != "img/c/1/1"}})} == {"copy"}
    assert "zarr" in {p.code for p in check(b, {"zarr.json": group(), "a/../zarr.json": group()})}


@pytest.mark.parametrize(("member", "error"), [(-1, KeyError), (999, KeyError), (None, ValueError)])
def test_span_refuses_what_has_no_bytes_of_its_own(member, error):
    b = base(str(FIXTURES / "tiff" / "bilevel_tiled.tif"))
    with pytest.raises(error):
        b.span(b.ir.index("ifds/0/tiles"), member)  # a struct, whose members have bytes


def test_a_text_fill_value_stays_text(tmp_path):
    """Only a numeric `_FillValue` of a float dataset is the Base64 of a binary64; a string one
    is the text it is."""
    path = tmp_path / "fill.h5"
    with h5py.File(path, "w") as f:
        f.create_dataset("x", data=np.arange(4.0)).attrs["_FillValue"] = "AAAAAAAA8D8="
    assert hdf5_attributes(base(path), "x")["_FillValue"] == "AAAAAAAA8D8="


def test_check_reports_a_node_of_the_other_zarr_version():
    v2 = {"zarr_format": 2, "shape": [4], "chunks": [4], "dtype": "|u1", "compressor": None, "fill_value": 0,
          "order": "C", "filters": None}
    out = {"zarr.json": group(), "a/.zarray": dump(v2), "a/0": bytes(4)}
    assert "parent" in {p.code for p in check(base(TIFF), out)}


def test_array_needs_the_fill_value_of_a_type_without_a_known_zero():
    with pytest.raises(ValueError, match="fill_value"):
        array([4], {"name": "raw_bytes", "configuration": {"length_bytes": 2}}, [4], [{"name": "bytes"}], {})


def test_a_name_that_is_no_plain_string_is_a_usage_error():
    class Text(str):
        __slots__ = ()
    with pytest.raises(LayoutUsageError, match="not <distribution>"):
        zarrify(TIFF, layout=Layout(Plugin(images, name=Text("acme.x"))))


def test_an_integer_fill_value_past_2_53_is_exact(tmp_path):
    """hdf5.md §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#3-datasets>: an integer fill value is a
    JSON integer whatever its magnitude, so a copy of the dataset is valid Zarr v3 and its fill
    value is the number."""
    path = tmp_path / "big.h5"
    with h5py.File(path, "w") as f:
        f.create_dataset("count", shape=(4,), dtype="u8", chunks=(2,), fillvalue=np.uint64(2**63 + 5))[:2] = [1, 2]
    b = base(path)
    assert fill_value(b.document("count/zarr.json")) == 2**63 + 5
    assert check(b, b.entries()) == []
    assert check(b, {"zarr.json": group(), **copy(b, "count", to="c")}) == []
