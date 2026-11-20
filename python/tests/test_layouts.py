"""Layouts plugins write (spec/layout.md; refkv.zarrify.layouts, refkv.build): the output a plugin
returns of the input as refkv parsed it, sealed as it is; the input's IR, and refkv's own output of
it where a plugin asks for it; `copy`, `array`, `group` and `check`; the coding routines; and each
way a layout request is refused or fails. The shipped examples have tests of their own
(test_example_*.py), and `refkv.build`'s tools theirs (test_build.py)."""

import ast
import contextlib
import importlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import override

import h5py
import numpy as np
import pytest
import zarr
import zarr.codecs
import zarr.storage

import refkv
import refkv.zarr
from refkv import Concat, Range
from refkv.build import (
    Literal,
    Parsed,
    Part,
    Ref,
    SealError,
    Shared,
    Value,
    Written,
    Zeros,
    concat,
    cut,
    fits,
    payload_size,
    project,
    read,
    seal,
)
from refkv.zarrify import Image, Rejected, layouts, zarrify
from refkv.zarrify.__main__ import main
from refkv.zarrify.coding import NoCodec, hdf5_attributes, tiff_byte_order, tiff_codecs, tiff_data_type, tiff_tags
from refkv.zarrify.layouts import (
    Layout,
    LayoutFailed,
    LayoutRejected,
    LayoutUsageError,
    dump,
    encode_entries,
    fill_value,
)
from refkv.zarrify.layouts.hierarchy import array, check, chunk_coords, chunk_key, copy, group, node_name

# the module, which the package's `seal` function shadows as an attribute
seal_module = importlib.import_module("refkv.build.seal")
ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures"
TIFF = str(FIXTURES / "tiff" / "jpeg_aperio_rgb.tif")
NWB = str(FIXTURES / "hdf5" / "nwb_ophys.nwb")
NETCDF = str(FIXTURES / "hdf5" / "hdf5_netcdf4.h5")
MOVIE = "stimulus/templates/movie/data"  # an array of NWB's


class Plugin:
    """A plugin whose write returns `make(inputs)`."""

    def __init__(self, make, name="acme.test", formats="*"):
        self.name, self.formats, self.make = name, formats, make

    def options(self, given):
        return dict(given)

    def write(self, inputs, options):  # noqa: ARG002 (the plugin protocol's signature)
        return self.make(inputs)


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


def no_output_of_refkv_s(monkeypatch):
    """Makes the making of refkv's own output of any parsed input fail."""
    async def refuse(_parsed):
        raise AssertionError("refkv's own output was built")
    monkeypatch.setattr(Parsed, "_output_async", refuse)


@contextlib.contextmanager
def opened(source, **how):
    """The input at `source` as a plugin is given it, and refkv's own output of it."""
    with read(source, **how) as p:
        yield p, project(p)


def decoded(store, path=""):
    """The values of the array at `path` of a store."""
    return zarr.open_array(refkv.zarr.store(store), path=path, mode="r")[...]


def zarr_store(tmp_path, values, **how):
    """A Zarr v3 store of one array `a` of `values`, uncompressed, which zarr-python wrote with
    `how`: an input whose documents and chunks refkv's own output keeps as they are."""
    root = zarr.open_group(zarr.storage.LocalStore(tmp_path / "s"), mode="w")
    root.create_array("a", shape=values.shape, dtype=values.dtype, fill_value=0, compressors=None, **how)[...] = values
    return tmp_path / "s"


def arrays_of(pr):
    """The image arrays of refkv's own output `pr`, copied to the same paths, under a root group
    of the plugin's."""
    out = {"zarr.json": group({"by": "acme"})}
    for k in pr.keys():  # noqa: SIM118 (Projected.keys, not a dict's)
        if k.endswith("/zarr.json") and not k.startswith("refkv_source/") and pr.document(k)["node_type"] == "array":
            path = k[:-len("/zarr.json")]
            out |= copy(pr, path, to=path)
    return out


def images(inputs):
    """What a plugin writes that asks for refkv's own output of its input, and copies its image
    arrays (`arrays_of`)."""
    [parsed] = inputs
    return arrays_of(project(parsed))


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


def tiles_from_ir(inputs):
    """What a plugin writes of a TIFF input from its IR alone, asking for no output of refkv's:
    its level 0, of its tiles' references and its tags' codecs."""
    [p] = inputs
    ifd = p.ir.index("ifds/0")
    tags = tiff_tags(p, ifd)
    tiles = p.ir.index("ifds/0/tiles")
    form = p.form(tiles, 0)
    assert form is not None
    shape = form["geometry"]["shape"]
    width, length, spp = one(tags[256]), one(tags[257]), one(tags[277])
    across = -(-width // shape[1])
    codecs = [{"name": "transpose", "configuration": {"order": [1, 2, 0]}},
              *tiff_codecs(tags, little_endian=True, x_axis=1, row_length=shape[1] * spp)]
    numbers = p.ir.members(tiles)[0]
    chunks = {(0, j // across, j % across): p.reference(tiles, k) for k, j in enumerate(numbers)}
    return {"zarr.json": group(), **array([spp, length, width], tiff_data_type(tags), [spp, *shape], codecs, chunks,
                                          to="img")}


def whole(inputs):
    """refkv's whole output, as it is, its placements kept (a `Written`)."""
    [p] = inputs
    return project(p).entries()


def unplaced(inputs):
    """refkv's whole output as a plain mapping, which keeps no placement."""
    return dict(whole(inputs))


def movie(inputs):
    """An array of NWB's, transposed, with its attributes, under a root group of the plugin's."""
    [p] = inputs
    pr = project(p)
    return {"zarr.json": group(), **copy(pr, MOVIE, order=[0, 2, 1], attributes=pr.attributes(MOVIE), to="m")}


def placed(inputs):
    """The image arrays, the root alone read at open, and one document kept uncompressed."""
    entries = images(inputs)
    return Written(entries, at_open={k: k == "zarr.json" for k, v in entries.items() if isinstance(v, bytes)},
                   stored={"0/zarr.json"})


def zeroed(inputs):
    """The image arrays, and a value of zeros: a zero data source refkv adds."""
    return images(inputs) | {"z": concat(Zeros(70000), Zeros(10))}


@pytest.mark.parametrize(("source", "make", "checksums"), [
    (TIFF, images, False),
    (TIFF, whole, False),
    (TIFF, unplaced, False),
    (TIFF, tiles_from_ir, False),
    (TIFF, tiles_from_ir, True),  # the plugin's ranges get checksums
    (NWB, movie, False),
    (TIFF, placed, False),
    (TIFF, zeroed, False),
])
def test_a_plugin_writes_the_whole_output(source, make, checksums):
    """The output is exactly what the plugin returns: its keys, its documents, and its references,
    those of refkv's own output and its own from the IR (checksums and all), which read that
    output's values; its bytes entries are placed as its `Written` places them, a document
    otherwise read at open; its sources are those its values use, the input's file first; `info`
    names the profile, the IR's elements and the layout."""
    with read(source, checksums=checksums) as p:
        entries = dict(make([p]))
        info = {"format": p.profile, "elements": len(p.ir), "layouts": ["acme.test"]}
    store = zarrify(source, checksums=checksums, layout=Layout(Plugin(make)))
    assert set(store.mapping) == set(entries)
    assert store.info == info
    plain = zarrify(source)
    for k, v in store.mapping.items():
        if isinstance(v, bytes):
            assert v == entries[k]
        else:
            for r in (v.parts if isinstance(v, Concat) else (v,)):
                url = r.data is None and store.sources[r.source].url is not None
                assert (r.crc32c is not None) == (checksums and url)
    if make is tiles_from_ir:
        assert np.array_equal(decoded(store, "img"), decoded(plain, "0"))
    documents = {k for k in entries if k.endswith("zarr.json")}
    if make is placed:
        assert store.read_at_open == frozenset(["zarr.json"])
        assert "0/zarr.json" not in store.compressed
    elif make is whole:  # refkv's own placements, which keep the IR mirror's documents for later
        assert (sorted(store.read_at_open), sorted(store.compressed)) == (
            sorted(plain.read_at_open), sorted(plain.compressed))
        assert not documents <= store.read_at_open
    else:
        assert documents <= store.read_at_open
    if make is zeroed:  # the zero data source the plugin asked for, after the sources of refkv's own output
        assert store.sources[:-1] == plain.sources
        assert store.sources[-1].data == bytes(70000)
        assert store.get("z") == bytes(70010)
    else:
        assert store.sources == plain.sources


def test_a_plugin_that_asks_for_no_output_of_refkv_s_has_none_built(monkeypatch):
    """A plugin is given the input parsed, and nothing built of it: one that never calls `project`
    writes its entries, a document and a reference from the IR, where refkv's own output cannot
    be made; one that calls it fails there."""
    tile = zarrify(TIFF).get("0/c/0/0/0")
    no_output_of_refkv_s(monkeypatch)

    def write(inputs):
        [parsed] = inputs
        return {"zarr.json": group(), "tile": parsed.reference(parsed.ir.index("ifds/0/tiles"), 0)}

    store = zarrify(TIFF, layout=Layout(Plugin(write)))
    assert set(store.mapping) == {"zarr.json", "tile"}
    assert store.get("tile") == tile
    with pytest.raises(LayoutFailed, match="refkv's own output was built"):
        zarrify(TIFF, layout=Layout(Plugin(images)))


def test_a_bytes_entry_placed_for_later_is_not_read_at_open():
    """A `Written` that places a bytes entry with `at_open` False keeps it out of what a reader is
    given at open, where a document is read at open by default."""
    def write(_inputs):
        return Written({"zarr.json": group(), "a/zarr.json": group(), "b/zarr.json": group()},
                       at_open={"a/zarr.json": False})

    assert zarrify(TIFF, layout=Layout(Plugin(write))).read_at_open == frozenset(["zarr.json", "b/zarr.json"])


@pytest.mark.parametrize(("source", "array_path", "order"), [
    ("tiff/bilevel_lzw_strips_short.tif", "0", [1, 0]),  # sharded
    ("tiff/strips_rgb_contig_deflate_short.tif", "0", [1, 2, 0]),  # rectilinear, a transpose first
    ("tiff/strips_rgb_contig_deflate_short.tif", "0", [0, 1, 2]),
    ("hdf5/nwb_ophys.nwb", MOVIE, [2, 0, 1]),
])
def test_copy_transposes_an_array_to_the_same_values(source, array_path, order):
    """`copy` with `order` decodes to the values of refkv's own output transposed by `order`, as
    numpy.transpose takes it, for sharded, rectilinear and transposed arrays alike, and `check`
    finds a copy one."""
    store = zarrify(str(FIXTURES / source))
    with opened(str(FIXTURES / source)) as (p, pr):
        entries = copy(pr, array_path, order=order)
        copied = seal(entries, [p])
        out = {"zarr.json": group(), **copy(pr, array_path, order=order, to="x")}
        assert check(out, [p]) == []
    assert np.array_equal(decoded(copied), np.transpose(decoded(store, array_path), order))
    assert doc_of(entries["zarr.json"])["attributes"] == {}
    assert out.keys() == {"zarr.json", *(f"x/{k}" for k in entries)}


@pytest.mark.parametrize(("name", "separator", "keys"), [
    ("default", "/", ["c/0/0", "c/1/0"]),
    ("default", ".", ["c.0.0", "c.1.0"]),
    ("v2", ".", ["0.0", "1.0"]),
    ("v2", "/", ["0/0", "1/0"]),
])
def test_copy_follows_the_array_s_chunk_key_encoding(tmp_path, name, separator, keys):
    """`copy` permutes a chunk key's indexes in the array's own encoding and separator."""
    values = np.arange(6, dtype="<u2").reshape(2, 3)
    path = zarr_store(tmp_path, values, chunks=(1, 3), chunk_key_encoding={"name": name, "separator": separator})
    with opened(path) as (p, pr):
        assert sorted(pr.array_entries("a")[1]) == keys
        assert np.array_equal(decoded(seal(copy(pr, "a", order=[1, 0]), [p])), values.T)


@pytest.mark.parametrize(("t", "order"), [([1, 2, 0], [2, 0, 1]), ([1, 2, 0], [1, 0, 2]), ([2, 0, 1], [0, 2, 1]),
                                          ([1, 0, 2], [1, 0, 2])])
def test_copy_composes_its_transpose_with_the_array_s(tmp_path, t, order):
    """A copy of an array that starts with a transpose decodes to the array's values transposed,
    and drops the transpose when the two cancel; its dimension names are permuted with it."""
    values = np.arange(24, dtype="<u2").reshape(2, 3, 4)
    path = zarr_store(tmp_path, values, chunks=(2, 3, 4), filters=[zarr.codecs.TransposeCodec(order=t)],
                      dimension_names=["a", "b", "c"])
    with opened(path) as (p, pr):
        assert pr.document("a/zarr.json")["codecs"][0] == {"name": "transpose", "configuration": {"order": t}}
        entries = copy(pr, "a", order=order)
        assert np.array_equal(decoded(seal(entries, [p])), np.transpose(values, order))
    doc = doc_of(entries["zarr.json"])
    assert doc["dimension_names"] == [["a", "b", "c"][o] for o in order]
    inverse = [order.index(i) for i in range(3)]
    assert doc["codecs"][0]["name"] == ("bytes" if [inverse[i] for i in t] == [0, 1, 2] else "transpose")


def test_copy_transposes_a_shard_s_inner_chunks_and_index(tmp_path):
    """`copy` transposes a sharded array of several inner chunks per shard to the array's values."""
    values = np.arange(24, dtype="<u2").reshape(4, 6)
    path = zarr_store(tmp_path, values, shards=(4, 6), chunks=(2, 3))
    with opened(path) as (p, pr):
        assert pr.document("a/zarr.json")["codecs"][0]["name"] == "sharding_indexed"
        assert np.array_equal(decoded(seal(copy(pr, "a", order=[1, 0]), [p])), values.T)


def test_a_parsed_input_s_elements_read_as_the_chunks_of_refkv_s_own_output():
    """`reference` reads an element (a JPEG tile, with its tables) as the chunk of refkv's own
    output does; `Projected.elements` maps each part of a chunk to the bytes of an element it
    reads; `element_at` finds an element from a byte; `form` gives the element's geometry, and
    `members` a struct's data members; each file of a set read as a store is a source of its
    own, as is the target of an HDF5 external link."""
    with opened(TIFF) as (p, pr):
        tiles = p.ir.index("ifds/0/tiles")
        first = pr.array_entries("0")[1]["c/0/0/0"]
        assert isinstance(first, Value)
        head, tables, _ = first.parts
        assert isinstance(head, Ref)
        assert isinstance(tables, Shared)
        assert p.reference(tiles, 0) == first
        assert p.reference(p.ir.index("ifds/0/tiles/0")) == first
        parts = pr.elements("0")["c/0/0/0"]
        assert parts[1] is None  # the JPEG tables, a shared string
        assert part(parts[0])[0] == part(parts[2])[0] == (p.element_at(0, head.offset) or (None,))[0]
        assert (p.form(tiles, 0) or {})["geometry"]["shape"] == [32, 32]
        assert p.members(tiles)[0] == (0, head.offset, p.ir.extent(p.ir.index("ifds/0/tiles/0"))[1])
    with opened(NETCDF) as (h, hr):
        (item,) = hr.elements("sst")["c/0/0"]
        i, member, start, stop = part(item)
        assert h.reference(i, member) == cut(h.reference(i, member), start, stop)
    # a set read as a store: each file its own source
    with opened(str(FIXTURES / "tiff" / "ome_set_a.ome.tif")) as (s, sr):
        strips = s.ir.index("ome_set_b.ome.tif/ifds/0/strips")
        assert s.source_of(strips) == 1
        assert s.reference(strips, 0) in sr.array_entries("0")[1].values()
    # an external link's target, in a file of its own
    with opened(str(FIXTURES / "hdf5" / "hdf5_external.h5")) as (x, xr):
        (item,) = xr.elements("linked_dataset")["c/0/0"]
        j, m, a, z = part(item)
        assert cut(x.reference(j, m), a, z) == xr.array_entries("linked_dataset")[1]["c/0/0"]


def test_refkv_s_own_output_gives_its_hierarchy():
    """`Projected.attributes` drops refkv's declarations; `source_metadata` gives the node's own;
    `nodes` and `entries` leave the IR mirror out where asked; `entries` writes the documents
    without the declarations where asked; a key of the output is in it."""
    with opened(TIFF) as (p, pr):
        assert (pr.profile, pr.version, pr.checksums) == (p.profile, p.version, False)
        assert "refkv_zarrified" not in pr.attributes("")
        assert pr.source_metadata("") is not None
        assert (pr.datasets, pr.mirror, pr.node_type("0")) == ({}, "refkv_source", "array")
        assert "0" in pr.nodes()
        assert "refkv_source" not in pr.nodes()
        assert "refkv_source" in pr.nodes(mirror=True)
        assert not any(k.startswith("refkv_source/") for k in pr.entries(mirror=False))
        assert "refkv_zarrified" not in doc_of(pr.entries("zarr.json", declarations=False)["zarr.json"])["attributes"]
        assert "0/zarr.json" in pr
        assert "nope" not in pr


CUTS = [
    ([(10, 5)], 1, 3, [(11, 2)]),
    ([(10, 5), b"abc", (40, 4)], 4, 9, [(14, 1), b"abc", (40, 1)]),
    ([(10, 5), (40, 4)], 5, 9, [(40, 4)]),
]


def value_of(p, parts):
    """A value of `parts`, each a literal's bytes or (offset, length) of the input `p`'s file."""
    return Value(tuple(Literal(x) if isinstance(x, bytes) else Ref(p, 0, *x) for x in parts))


@pytest.mark.parametrize(("parts", "start", "stop", "want"), CUTS)
def test_cut_and_payload_size(parts, start, stop, want):
    """`cut` keeps the bytes asked for, across the parts they span, a part kept whole as it is;
    `payload_size` has room for each source range's checksum."""
    with read(TIFF) as p:
        got = cut(value_of(p, parts), start, stop)
        assert got == value_of(p, want)
        assert payload_size(got, checksums=True) == payload_size(got, checksums=False) + 6 * sum(
            isinstance(x, Ref) for x in got.parts)


@pytest.mark.parametrize(("parts", "start", "stop"), [
    CUTS[0][:3],
    CUTS[1][:3],
    CUTS[2][:3],
])
def test_the_cuts_of_a_value_join_to_it(parts, start, stop):
    """The bytes before a cut, the cut and the bytes after it, joined flat by `concat`, read what
    the value reads, a cut that ends at the value's end included."""
    with read(TIFF) as p:
        value = value_of(p, parts)
        joined = concat(cut(value, 0, start), cut(value, start, stop), cut(value, stop, value.size))
        assert joined.size == value.size


@pytest.mark.parametrize("source", [TIFF, NWB, NETCDF, str(FIXTURES / "hdf5" / "hdf5_filters.h5"),
                                    str(FIXTURES / "tiff" / "strips_rgb_contig_deflate_short.tif")])
def test_check_finds_nothing_in_refkv_s_own_output(source):
    with opened(source) as (p, pr):
        assert check(pr.entries(), [p]) == []


def bad(p, pr):
    """Outputs of the input `p`, of refkv's own output `pr` of it, each with one problem, by the
    code check gives it."""
    entries = arrays_of(pr)
    chunk = next(k for k in entries if k.startswith("0/c/"))
    doc = doc_of(entries["0/zarr.json"])
    misplaced = Written(entries)
    misplaced.at_open[chunk] = True  # placed after it was set: a placement of no bytes entry
    return {
        "key": {**entries, "__vz__/x": b"1"},
        "range": {**entries, "x": Ref(p, 0, 0, 10 ** 9)},
        "open": misplaced,
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


@pytest.mark.parametrize("code", ["key", "range", "open", "document", "orphan", "json", "zarr", "parent", "copy",
                                  "refkv", "chunk", "consolidated"])
def test_check_reports_each_problem_by_its_code(code):
    with opened(TIFF) as (p, pr):
        assert code in {x.code for x in check(bad(p, pr)[code], [p])}


def test_check_reports_a_chunk_of_the_wrong_size():
    with opened(NETCDF) as (p, pr):
        (item,) = pr.elements("counts")["c/0"]
        i, member, _, _ = part(item)
        doc = doc_of(copy(pr, "counts")["zarr.json"])
        whole_chunk = p.reference(i, member)
        out = {"zarr.json": group(), "s/zarr.json": dump(doc), "s/c/0": cut(whole_chunk, 0, whole_chunk.size - 1)}
        assert "size" in {x.code for x in check(out, [p])}


def test_the_coding_routines_give_the_codecs_of_refkv_s_own_output():
    with opened(TIFF) as (p, pr):
        tags = tiff_tags(p, p.ir.index("ifds/0"))
        doc = pr.document("0/zarr.json")
        assert tiff_data_type(tags) == doc["data_type"]
        assert tiff_codecs(tags, little_endian=tiff_byte_order(p, p.ir.index("ifds/0")), x_axis=1,
                           row_length=96) == doc["codecs"][1:]
    assert tiff_codecs({259: [65001], 258: [1]}, little_endian=True, x_axis=1, row_length=8, chunk_shape=[4, 8]) == [
        {"name": "imagecodecs_eer", "configuration": {"shape": [4, 8], "skipbits": 7, "horzbits": 2, "vertbits": 2}}]
    # a netCDF _FillValue's Base64 binary64, undone (spec/zarrify/hdf5.md §6
    # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#6-attributes>)
    with opened(NETCDF) as (_, pr):
        assert hdf5_attributes(pr, "sst")["_FillValue"] == -1.5


def test_fits_says_why_refkv_would_not_write_a_value():
    with opened(TIFF) as (p, pr):
        chunk = pr.array_entries("0")[1]["c/0/0/0"]
        assert isinstance(chunk, Value)
        assert fits(chunk) is None
        assert fits(Ref(p, 0, 0, 4)) is None
        assert "payload" in (fits(Value(tuple(Literal(b"ab") for _ in range(20000)))) or "")


def test_fits_says_a_range_lies_past_its_source():
    with read(TIFF) as p:
        assert "past" in (fits(Ref(p, 0, 0, 10 ** 9)) or "")


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
    public = {"refkv", "refkv.build", "refkv.ir", "refkv.zarrify", "refkv.zarrify.layouts",
              "refkv.zarrify.layouts.hierarchy", "refkv.zarrify.coding"}
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


def test_a_store_input_the_plugin_does_not_read_is_rejected():
    with pytest.raises(LayoutRejected, match="does not read the zarr2"):
        zarrify(str(FIXTURES / "zarr2" / "zarr2_hierarchy"), layout=Layout(Plugin(images, formats={"zarr3": {0}})))


@pytest.mark.parametrize(("location", "url"), [
    (str(FIXTURES / "dicom_series" / "multi_frame"), None),
    (str(FIXTURES / "dicom_series" / "multi_frame") + "/", "https://example.invalid/s/"),
], ids=["directory", "url"])
def test_a_store_of_dicom_files_takes_no_plugin_layout(location, url):
    """The DICOM store form joins its files' outputs, and keeps no IR for a plugin to write from
    (spec/layout.md §6 <https://github.com/d-v-b/refkv/blob/main/spec/layout.md#6-errors>)."""
    with pytest.raises(LayoutRejected, match="DICOM files has no IR for a plugin layout"):
        zarrify(location, url, layout=Layout(Plugin(images)))


def test_an_input_the_plugin_does_not_read_is_rejected_before_anything_is_built(monkeypatch):
    """A plugin whose `formats` do not name the input's profile is refused once the input is
    parsed: its `write` does not run, and refkv's own output is not made."""
    no_output_of_refkv_s(monkeypatch)
    ran = []

    def write(inputs):
        ran.append(True)
        return images(inputs)

    with pytest.raises(LayoutRejected, match="does not read the tiff"):
        zarrify(TIFF, layout=Layout(Plugin(write, formats={"hdf5": {0}})))
    assert ran == []


def test_an_input_refkv_s_own_projection_rejects_is_rejected_where_the_plugin_asks_for_it():
    """An N5 store with a key in the archive's reserved space parses, and refkv's own projection
    rejects it: a plugin that asks for that output is rejected with it, under the plugin's name,
    and one that writes entries of its own is not."""
    location, url = str(FIXTURES / "n5" / "n5_reject_reserved_key") + "/", "https://example.invalid/s/"
    with pytest.raises(Rejected, match="reserved __vz__/ space"):
        zarrify(location, url)
    with pytest.raises(LayoutRejected, match=r"^acme\.test: .*reserved __vz__/ space"):
        zarrify(location, url, layout=Layout(Plugin(images)))
    assert set(zarrify(location, url, layout=Layout(Plugin(lambda _inputs: {"zarr.json": group()}))).mapping) == {
        "zarr.json"}


def test_a_plugin_s_rejection_rejects_the_layout_request():
    def rejects(_inputs):
        raise Rejected("not this one")
    with pytest.raises(LayoutRejected, match=r"^acme\.test: not this one"):
        zarrify(TIFF, layout=Layout(Plugin(rejects)))


@pytest.mark.parametrize("error", [KeyError("bug"), SystemExit(1)])
def test_a_plugin_s_exception_is_a_failure(error):
    def raises(_inputs):
        raise error
    with pytest.raises(LayoutFailed, match=rf"^acme\.test: {type(error).__name__}"):
        zarrify(TIFF, layout=Layout(Plugin(raises)))


def refused(make, code, message, source=TIFF):
    """That what `make` writes of the input at `source` is refused: by `seal`, under `code`, and
    by `zarrify`, as the plugin's failure, the plugin's name and then seal's message, which
    `message` matches."""
    with read(source) as p, pytest.raises(SealError) as e:
        seal(make([p]), [p])
    assert e.value.code == code
    with pytest.raises(LayoutFailed, match=rf"^acme\.test: .*{message}"):
        zarrify(source, layout=Layout(Plugin(make)))


def test_a_write_that_returns_no_mapping_is_a_failure():
    refused(lambda _inputs: [("zarr.json", group())], "value", "a mapping of keys to entries, not list")


# the ids are short: pytest puts a test's id in an environment variable, which Windows bounds
@pytest.mark.parametrize("key", ["", "__vz__/x", "x" * 65536, "\ud800", 5],
                         ids=["empty", "reserved", "long", "surrogate", "int"])
def test_a_key_the_archive_cannot_hold_is_a_failure(key):
    refused(lambda inputs: {**images(inputs), key: b"1"}, "key", "key")


@pytest.mark.parametrize(("value", "code"), [
    (lambda p: Ref(p, 0, 0, 10 ** 9), "range"),  # past the file
    (lambda p: Ref(p, 7, 0, 1), "range"),  # no such source
    (lambda p: Ref(p, 0, -1, 2), "value"),
    (lambda p: Ref(p, True, 0, 1), "range"),
    (lambda _p: Literal("x"), "value"),  # type: ignore[arg-type] (a literal that is not bytes)
], ids=["past", "source", "negative", "bool", "literal"])
def test_a_reference_refkv_does_not_write_is_a_failure(value, code):
    refused(lambda inputs: {**images(inputs), "x": value(inputs[0])}, code, "x:")


@pytest.mark.parametrize("entry", ["text", Range(0, 0, 1), Concat((Range(0, 0, 1),))], ids=["text", "range", "concat"])
def test_an_entry_that_is_no_value_is_a_failure(entry):
    """An entry that is neither bytes nor a value of `refkv.build`'s, a `refkv.Range` or `Concat`
    of the store's own among them, which name a source by its index in a table `seal` numbers."""
    refused(lambda inputs: {**images(inputs), "x": entry}, "value", "an entry is bytes or a value")


def test_a_failure_names_the_entry_that_is_no_value():
    with pytest.raises(LayoutFailed, match="x:"):
        zarrify(TIFF, layout=Layout(Plugin(lambda inputs: {**images(inputs), "x": "text"})))


def test_a_payload_past_the_limit_is_a_failure():
    big = Value(tuple(Literal(b"ab") for _ in range(20000)))
    refused(lambda inputs: {**images(inputs), "x": big}, "payload", "x: a payload")


def test_a_placement_of_no_bytes_entry_is_a_failure():
    refused(lambda inputs: Written(images(inputs), at_open={"nope": True}), "open", "nope: .*not a bytes entry")


def test_a_placement_of_a_reference_is_a_failure():
    refused(lambda inputs: Written(tiles_from_ir(inputs), at_open={"img/c/0/0/0": True}), "open",
            "img/c/0/0/0: .*not a bytes entry")


def test_more_read_at_open_than_a_reader_reads_is_a_failure(monkeypatch):
    monkeypatch.setattr(seal_module, "MAX_OPEN", 10)
    refused(images, "open", "read at open")


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
    install(monkeypatch, Entry("acme.test", Plugin(lambda _inputs: {"x": "text"})))
    assert main([TIFF, str(tmp_path / "o.vzip"), "--layout", "acme.test"]) == 1


class Lazy(Mapping):
    """Entries that raise `error` when they are read, after the `write` that returned them."""

    def __init__(self, error):
        self.error = error

    def __getitem__(self, key):
        raise self.error

    def __iter__(self):
        raise self.error

    def __len__(self):
        return 1


@pytest.mark.parametrize(("raised", "error"), [
    (ValueError("lazy bug"), LayoutFailed),
    (Rejected("not this one"), LayoutRejected),
])
def test_plugin_code_refkv_runs_after_write_is_the_plugin_s(raised, error):
    """A mapping `write` returns runs the plugin's code when refkv reads it: what it raises is
    the plugin's failure, or its rejection, as from `write` itself."""
    with pytest.raises(error, match=rf"^acme\.test: .*{raised}"):
        zarrify(TIFF, layout=Layout(Plugin(lambda _inputs: Lazy(raised))))


@pytest.mark.parametrize("extra", [
    {"g/zarr.json": dump({"zarr_format": 3, "node_type": "group", "consolidated_metadata": {"metadata": "ab"}})},
    {"g/zarr.json": group(), "g/a/zarr.json": dump({"node_type": "array", "chunk_key_encoding": "x"}), "g/a/c/0": b"1"},
    {"a/zarr.json": b"[" * 100000 + b"]" * 100000},
])
def test_check_reports_malformed_documents_rather_than_raising(extra):
    with opened(TIFF) as (p, pr):
        assert {x.code for x in check(pr.entries(mirror=False) | extra, [p])} & {"zarr", "json", "chunk"}


def test_a_chunk_is_its_elements_bytes():
    """`elements` names every element a part runs across (strips the profile joined into one
    range), and the spans of those bytes, joined, are the chunk of refkv's own output;
    `reference` reads a recipe's `rows` step (an ND2 frame's padded rows) as spec/conventions.md §8.5
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#85-forms-and-recipes> says."""
    with opened(str(FIXTURES / "tiff" / "bilevel_strips_short.tif")) as (p, pr):
        value = pr.array_entries("0")[1]["c/0/0"]
        assert isinstance(value, Value)
        assert len(value.parts) == 1
        pieces = [part(item) for item in pr.elements("0")["c/0/0"]]
        assert len(pieces) > 1
        assert concat(*(cut(p.span(i, m), a, z) for i, m, a, z in pieces), merge=True) == value
    with opened(TIFF) as (j, jr):  # a JPEG tile's span is its own bytes, where its reference splices in the tables
        jpeg = jr.array_entries("0")[1]["c/0/0/0"]
        assert isinstance(jpeg, Value)
        rebuilt: list[Value | Part] = []
        for item, shared in zip(jr.elements("0")["c/0/0/0"], jpeg.parts, strict=True):
            if item is None:  # the tables, a shared string
                rebuilt.append(shared)
            else:
                i, m, a, z = part(item)
                rebuilt.append(cut(j.span(i, m), a, z))
        assert concat(*rebuilt) == jpeg
    with opened(str(FIXTURES / "nd2" / "nd2_padded_rgb.nd2")) as (n, nr):
        frame, _, _, _ = part(nr.elements("0/0")["c/0/0/0/0"][0])
        assert n.reference(frame) == nr.array_entries("0/0")[1]["c/0/0/0/0"]


@pytest.mark.parametrize(("key", "value"), [("c/01/0", None), ("c/1/0", b"12")])
def test_check_reports_a_chunk_a_reader_does_not_read(key, value):
    """A key the encoding does not give (`c/01/0`, read by no reader) is `chunk`; a bytes chunk of
    a fixed-size array short of its bytes is `size`."""
    rows = {(0, 0): bytes(8), (1, 0): bytes(8)}
    a = array([2, 4], "uint16", [1, 4], [{"name": "bytes", "configuration": {"endian": "little"}}], rows, to="a")
    out = {"zarr.json": group(), "a/zarr.json": a["a/zarr.json"], "a/c/0/0": a["a/c/0/0"],
           f"a/{key}": value if value is not None else a["a/c/1/0"]}
    with read(TIFF) as p:
        codes = {x.code for x in check(out, [p])}
    assert codes == ({"chunk"} if value is None else {"size"})


def test_fill_value_reads_bfloat16_bits_as_a_float32_s_high_half():
    assert fill_value({"data_type": "bfloat16", "fill_value": "0x3f80"}) == 1.0
    assert fill_value({"data_type": "float32", "fill_value": "0x3f800000"}) == 1.0


def test_check_finds_a_copy_by_its_transposes():
    """A copy of an array of axes of one length (all 2s) is found by its transposes, not by
    trying permutations, and a datetime array's chunks are sized by its element's 8 bytes."""
    with opened(str(FIXTURES / "nifti" / "nifti_n2_le_complex128_5d.nii")) as (n, nr):
        assert check({"zarr.json": group(), **copy(nr, "0", order=[4, 1, 3, 0, 2], to="x")}, [n]) == []
    dt = {"name": "numpy.datetime64", "configuration": {"unit": "ns", "scale_factor": 1}}
    out = {"zarr.json": group(), **array([4], dt, [4], [{"name": "bytes", "configuration": {"endian": "little"}}],
                                         {(0,): bytes(16)}, to="a")}
    with read(TIFF) as p:
        assert {x.code for x in check(out, [p])} == {"size"}


def test_values_refkv_encodes_decode_to_python_values():
    with opened(str(FIXTURES / "hdf5" / "hdf5_attributes.h5")) as (_, pr):
        assert hdf5_attributes(pr, "attrs")["latin1"] == "café"
    assert fill_value({"data_type": "float8_e8m0fnu", "fill_value": "NaN"}) != fill_value(
        {"data_type": "float8_e8m0fnu", "fill_value": "NaN"})  # a NaN: unequal to itself


def test_a_member_in_a_derived_space_has_no_reference():
    """A struct's members inside an inflated stream (a FITS file in gzip) are no bytes of the
    file: `reference` and `span` refuse them, as they refuse the struct."""
    with read(str(FIXTURES / "fits" / "fits_gzip_mef.fits.gz")) as p:
        hdu = p.ir.index("stream/hdus/1")
        for bridge in (p.reference, p.span):
            with pytest.raises(ValueError, match="derived space"):
                bridge(hdu, 0)


def test_check_reports_a_copy_that_leaves_a_chunk_out_and_a_dot_node():
    with opened(str(FIXTURES / "tiff" / "bilevel_tiled.tif")) as (p, pr):
        full = copy(pr, "0", to="img")
        assert {x.code for x in check({"zarr.json": group(), **{k: v for k, v in full.items()
                                                                if k != "img/c/1/1"}}, [p])} == {"copy"}
        assert "zarr" in {x.code for x in check({"zarr.json": group(), "a/../zarr.json": group()}, [p])}


@pytest.mark.parametrize("member", [-1, 999])
def test_span_refuses_a_member_the_element_does_not_have(member):
    with read(str(FIXTURES / "tiff" / "bilevel_tiled.tif")) as p, pytest.raises(KeyError):
        p.span(p.ir.index("ifds/0/tiles"), member)  # a struct, whose members have bytes


def test_a_text_fill_value_stays_text(tmp_path):
    """Only a numeric `_FillValue` of a float dataset is the Base64 of a binary64; a string one
    is the text it is."""
    path = tmp_path / "fill.h5"
    with h5py.File(path, "w") as f:
        f.create_dataset("x", data=np.arange(4.0)).attrs["_FillValue"] = "AAAAAAAA8D8="
    with opened(path) as (_, pr):
        assert hdf5_attributes(pr, "x")["_FillValue"] == "AAAAAAAA8D8="


def test_check_reports_a_node_of_the_other_zarr_version():
    v2 = {"zarr_format": 2, "shape": [4], "chunks": [4], "dtype": "|u1", "compressor": None, "fill_value": 0,
          "order": "C", "filters": None}
    out = {"zarr.json": group(), "a/.zarray": dump(v2), "a/0": bytes(4)}
    with read(TIFF) as p:
        assert "parent" in {x.code for x in check(out, [p])}


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
    with opened(path) as (p, pr):
        assert fill_value(pr.document("count/zarr.json")) == 2**63 + 5
        assert check(pr.entries(), [p]) == []
        assert check({"zarr.json": group(), **copy(pr, "count", to="c")}, [p]) == []
