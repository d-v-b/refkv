"""The HDF4 profile (spec/zarrify/hdf4.md
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md>) against pyhdf, the HDF4 library's
binding: every dataset of every accepted fixture (fixtures/hdf4) is presented with the values,
shape, dimension names and attributes the library reads, or listed with the reason the fixture
plants (hdf4_oracle.py). The fixtures' hierarchy, storage and source metadata are then checked
against what the generator wrote. The rejected fixtures are test_rejects.py's.

The kerchunk and VirtualiZarr HDF4 readers' bugs (the plan design/plans/hdf4.md lists them among
its findings that shaped the design, item 10) that step 1 covers each have a fixture here: floats
and integers of both byte orders (`little_*`), plain datasets referenced in place (`plain`), deflate
named only where the data is deflated (`deflated`, `chunked_deflate`, and `chunked` without it),
datasets in no Vgroup (`alone`),
data in linked blocks referenced, compressed or not (`records`, the chunk tables), dimension names of
a rank-3 dataset (`cube`), and nested Vgroups (`outer/inner`)."""

from pathlib import Path

import numpy as np
import pytest
import zarr

import refkv.zarr
from refkv._narrow import at, ndarray, sequence
from refkv.zarrify import zarrify

# pyhdf, where it has a wheel (pyproject.toml); the oracle imports it
pytest.importorskip("pyhdf")
from hdf4_oracle import UNPRESENTED, nodes, own, problems, v3
from ir_helpers import FIXTURES

HDF4 = FIXTURES / "hdf4"
ACCEPTED = sorted(p for p in HDF4.glob("*.hdf") if "reject" not in p.name)


def opened(path: Path) -> tuple[zarr.Group, dict]:
    store = zarrify(str(path))
    return zarr.open_group(refkv.zarr.store(store), mode="r"), store.info


@pytest.mark.parametrize("path", ACCEPTED, ids=lambda p: p.name)
def test_datasets_are_what_pyhdf_reads(path):
    root, info = opened(path)
    assert info["format"] == "hdf4"
    assert problems(path, root, UNPRESENTED.get(path.name, {})) == []


def test_each_storage_is_presented_as_the_convention_says():
    root, info = opened(HDF4 / "hdf4_layouts.hdf")
    arrays = {p: a for p, a in nodes(root) if isinstance(a, zarr.Array)}
    zlib = {"name": "numcodecs.zlib", "configuration": {"level": 1}}

    def codecs(p):
        return [c.to_dict() for c in v3(arrays[p]).codecs]

    layouts = {p: own(a)["layout"] for p, a in arrays.items()}
    assert layouts["plain"] == "contiguous"
    assert layouts["deflated"] == "compressed"
    assert own(arrays["deflated"])["compression"] == "deflate"
    assert layouts["chunked"] == layouts["chunked_deflate"] == "chunked"
    assert layouts["records"] == "linked"
    assert all(layouts[p] == "none" for p in arrays if p.startswith("empty"))
    # deflate where the data is deflated, and nowhere else
    assert zlib in codecs("deflated")
    assert zlib in codecs("chunked_deflate")
    assert zlib not in codecs("chunked")
    assert zlib not in codecs("plain")
    # the chunks: the header's chunk shape, and no chunk where the table lists none
    for p in ("chunked", "chunked_deflate"):
        a = arrays[p]
        assert a.chunks == (2, 4)
        assert own(a)["chunks"] == [2, 4]
    # 5 chunks a chunked dataset of numbers, [1, 1] never written, and 2 of text
    assert info["chunks"] == 12
    # byte order by the number type's class
    assert own(arrays["little_int16"])["number_type"]["class"] == 4
    assert {"name": "bytes", "configuration": {"endian": "little"}} in codecs("little_int16")
    assert {"name": "bytes", "configuration": {"endian": "big"}} in codecs("plain")
    # records past the SDD's size: the unlimited dimension's length is the record count
    records = own(arrays["records"])
    assert records["unlimited"] is True
    assert arrays["records"].shape == (7, 3)
    assert records["sdd"] == [2, 3]
    assert arrays["empty_records"].shape == (0, 2)
    # netCDF's fills, and a _FillValue, as the arrays' fill values
    fills = {p: arrays[p].metadata.fill_value for p in arrays if p.startswith("empty")}
    assert {p: v for p, v in fills.items() if not p.endswith("float32") and not p.endswith("float64")} == {
        "empty_int8": -127, "empty_uint8": 129, "empty_int16": -32767, "empty_uint16": 32769,
        "empty_int32": -2147483647, "empty_uint32": 2147483649, "empty_uchar8": 0, "empty_filled": 7,
        "empty_records": np.float32(15 * 2.0**119)}
    assert fills["empty_float32"] == fills["empty_float64"] == 15 * 2.0**119
    # the scale of `lat`, a coordinate variable, and the dimension names of the cube
    assert v3(arrays["cube"]).dimension_names == ("level", "lat", "lon")
    assert v3(arrays["lat"]).dimension_names == ("lat",)
    assert np.array_equal(arrays["lat"][...], [-10.0, 0.0, 10.0])
    # text: strings of the last axis's length, trailing spaces kept; a scalar of rank 1; chunks whole
    # along the strings, or the dataset is not presented
    assert arrays["text1"].shape == ()
    assert ndarray(arrays["text1"][...]).tolist() == b"hello"
    assert ndarray(arrays["text2"][...]).tolist() == [b"ab", b"cd  ", b"efgh"]
    assert arrays["text3"].shape == (2, 2)
    assert own(arrays["text3"])["text_axis"].startswith("fakeDim")
    assert v3(arrays["text2"]).data_type.to_json(zarr_format=3) == {"name": "null_terminated_bytes",
                                                                    "configuration": {"length_bytes": 4}}
    assert arrays["text_chunked"].chunks == (2,)
    assert "text_cut" not in arrays
    assert ndarray(arrays["text_empty"][...]).tolist() == [b"", b""]
    # a float dataset's _FillValue of text is its text
    assert arrays["text_fill"].attrs["_FillValue"] == "-9999"
    # attributes: text to its first NUL, trailing spaces kept; numbers one and several
    assert root.attrs["title_nul"] == "padded"
    assert root.attrs["title_spaces"] == "padded   "
    assert root.attrs["title_exact"] == "exact"
    assert root.attrs["versions"] == [1, 2, 3]
    assert root.attrs["ratio"] == 0.5
    assert root.attrs["StructMetadata.0"] == "GROUP=SwathStructure\nEND_GROUP=SwathStructure\nEND\n"
    assert arrays["plain"].attrs["missing"] == "NaN"
    assert arrays["plain"].attrs["valid_range"] == [-100, 100]
    assert own(root)["attributes"]["title_nul"] == {"type": "char8", "count": 9}


def test_groups_follow_the_writers_vgroups():
    root, info = opened(HDF4 / "hdf4_groups.hdf")
    kinds = {p: type(n).__name__ for p, n in nodes(root)}
    # a Vgroup not presented holds nothing below it: what it holds is the root's, its datasets after
    # the walk and its Vgroups as the walk's last top Vgroups
    assert kinds == {
        "": "Group", "alone": "Array", "twin": "Array", "orphan": "Array", "dup_member": "Array", "text": "Array",
        "lone": "Array", "hidden_table": "Array", "outer/table": "Array",
        "outer": "Group", "outer/inner": "Group", "outer/inner/inner_data": "Array", "outer/shared": "Array",
        "second": "Group", "loop_a": "Group", "loop_a/loop_b": "Group", "under": "Group", "under/under_data": "Array",
        # of the datasets named `pair`, the second is presented in the first group the walk reaches
        # it in where the name is free
        "pairs": "Group", "pairs/pair": "Array", "pair_again": "Group", "pair_again/pair": "Array",
        "pairs/pair_child": "Group", "pairs/pair_child/pair": "Array",
    }
    groups = dict(nodes(root))
    # a Vgroup and a dataset reached again are links of the group that holds them, in member order
    assert own(groups["second"])["links"] == [
        {"name": "inner", "tag": 1965, "ref": own(groups["outer/inner"])["ref"], "path": "outer/inner"},
        {"name": "shared", "tag": 720, "ref": own(groups["outer/shared"])["ndg"], "path": "outer/shared"},
        {"name": "table", "tag": 1962, "ref": own(groups["outer/table"])["ref"], "path": "outer/table"},
    ]
    # Vgroups that hold each other: the one of the least ref at the root, the other below it
    assert own(groups["loop_a/loop_b"])["links"][0]["path"] == "loop_a"
    # a Vgroup's attributes: its own (version 4) and an Attr0.0 member, as HDF-EOS writes them
    assert {k: groups["outer"].attrs[k] for k in ("note", "_FV_inner_data")} == {"note": "grid", "_FV_inner_data": -1}
    assert own(groups["outer"])["class"] == "Grid"
    # what is not presented, by group: names that are no node names or that a member holds already
    reasons = {p: sorted((u.get("name"), u["reason"]) for u in own(n).get("unpresented", [])) for p, n in nodes(root)}
    assert reasons[""] == [("bad/name", "name"), ("outer", "duplicate"), ("refkv_source", "reserved"),
                           ("twin", "duplicate")]
    assert reasons["second"] == [("__hidden", "name"), ("a/b", "name")]
    # a table whose name is no node name, which two Vgroups hold: recorded once, where first reached
    assert reasons["outer"] == [("x/y", "name")]
    # recorded once in each group that holds a `pair` already, however often the walk reaches it there
    assert reasons["pairs"] == [("pair", "duplicate")]
    assert reasons["pairs/pair_child"] == [("pair", "duplicate")]
    assert info["unpresented"] == {"duplicate": 4, "name": 4, "reserved": 1}


def test_tables_are_arrays_of_their_records():
    root, info = opened(HDF4 / "hdf4_tables.hdf")
    nodes_ = dict(nodes(root))
    arrays = {p: a for p, a in nodes_.items() if isinstance(a, zarr.Array)}
    cal = arrays["Per File Data/Calibration"]
    # MO36's Calibration table: a struct of its fields, a float of order 6 a nested struct, text of
    # each padding as null_terminated_bytes, the trailing spaces kept
    fields = sequence(at(v3(cal).data_type.to_json(zarr_format=3), "configuration", "fields"))
    assert [at(f, "name") for f in fields] == ["band", "coeff", "name", "units", "code"]
    assert at(fields[1], "data_type", "name") == "struct"
    assert len(sequence(at(fields[1], "data_type", "configuration", "fields"))) == 6
    assert at(fields[2], "data_type") == {"name": "null_terminated_bytes", "configuration": {"length_bytes": 16}}
    rows = ndarray(cal[...])
    assert rows["name"].tolist() == [b"Chlorophyll", b"sst"]
    assert rows["units"].tolist() == [b"mg/m^3  ", b"deg_C   "]
    assert rows["code"].tolist() == [b"ABCD", b"EFGH"]
    assert v3(cal).dimension_names == ("record",)
    # its attributes: the Vdata's on the array, a field's in its source metadata
    assert cal.attrs["note"] == "calibration"
    assert own(cal)["field_attributes"] == {"band": {"units": "1"}}
    # a field's first attribute of a name claims it, presented or not
    assert "field_attributes" not in own(arrays["dup_attr"])
    # a table two Vgroups hold: presented once, linked from the other
    assert own(nodes_["Other"])["links"][0]["path"] == "Per File Data/Calibration"
    # records in linked blocks, fields not interlaced (a group of an array per field), no records,
    # little-endian fields, and fields of two byte orders, not presented
    assert own(nodes_["grown"])["records"] == 7
    assert arrays["grown"].shape == (7,)
    assert sorted(k for k in nodes_ if k.startswith("columns/")) == ["columns/a", "columns/b"]
    assert ndarray(arrays["columns/b"][...]).tolist() == [b"abc", b"de", b"f"]
    assert arrays["empty"].shape == (0,)
    little = [{"name": "bytes", "configuration": {"endian": "little"}}]
    assert [c.to_dict() for c in v3(arrays["little"]).codecs] == little
    # not interlaced, two byte orders: each field's array its own
    assert [c.to_dict() for c in v3(arrays["split/a"]).codecs] == little
    assert ndarray(arrays["split/b"][...]).tolist() == [2, 400000]
    assert info["unpresented"] == {"byte_order": 1, "fields": 2}
