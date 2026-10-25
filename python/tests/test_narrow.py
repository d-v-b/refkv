"""The narrowing helpers (refkv._narrow, and refkv.zarr._narrow for zarr's nodes): each gives its
value as the type it names, or raises TypeError naming where the value was found; `at` walks a path
into a JSON document."""

import numpy as np
import pytest
import zarr
import zarr.storage

from refkv._narrow import at, boolean, integer, mapping, ndarray, number, sequence, string
from refkv.zarr import _narrow as zarr_narrow


@pytest.mark.parametrize(
    ("narrow", "value"),
    [
        (mapping, {}),
        (mapping, {"a": 1}),
        (sequence, []),
        (sequence, [1, "a"]),
        (sequence, (1, 2)),
        (string, ""),
        (string, "a"),
        (integer, 0),
        (integer, -(2**70)),
        (number, 1),
        (number, 1.5),
        (boolean, True),
        (boolean, False),
    ],
)
def test_narrow(narrow, value):
    assert narrow(value) is value
    assert narrow(value, "where") is value


DOC = {"ome": {"series": [{"name": "a"}, {"name": "b"}, {"name": "c", "size": [3, 4]}]}, "n": None}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ((), DOC),
        (("n",), None),
        (("ome", "series", 2, "name"), "c"),
        (("ome", "series", -1, "size", 1), 4),
        (("ome", "series", 0), {"name": "a"}),
    ],
)
def test_at(path, expected):
    assert at(DOC, *path) == expected


def test_ndarray():
    a = np.arange(3)
    assert ndarray(a) is a
    assert ndarray([1, 2]).tolist() == [1, 2]


def test_wrong_kind():
    with pytest.raises(TypeError, match=r"^ome\.series: expected a sequence, got str$"):
        sequence("abc", "ome.series")
    with pytest.raises(TypeError, match=r"^value: expected a mapping, got list$"):
        mapping([])


def test_bool_is_not_an_integer():
    with pytest.raises(TypeError, match=r"^value: expected an integer, got bool$"):
        integer(True)


def test_bool_is_not_a_number():
    with pytest.raises(TypeError, match=r"^n: expected a number, got bool$"):
        number(False, "n")


def test_at_wrong_kind():
    with pytest.raises(TypeError, match=r"^ome\.series\[2\]: expected a sequence, got dict$"):
        at(DOC, "ome", "series", 2, 0)


def test_at_missing_key():
    with pytest.raises(KeyError, match=r"ome\.lineage: missing"):
        at(DOC, "ome", "lineage")


def test_at_index_out_of_range():
    with pytest.raises(IndexError, match=r"^ome\.series\[3\]: index out of range$"):
        at(DOC, "ome", "series", 3)


def test_zarr_nodes():
    root = zarr.group(store=zarr.storage.MemoryStore())
    root.create_array("a", shape=(2,), dtype="u1")
    root.create_group("g")
    assert zarr_narrow.array(root["a"]).shape == (2,)
    assert zarr_narrow.group(root["g"]).path == "g"


@pytest.mark.parametrize(("narrow", "path"), [(zarr_narrow.array, "g"), (zarr_narrow.group, "a")])
def test_zarr_node_of_the_other_kind(narrow, path):
    root = zarr.group(store=zarr.storage.MemoryStore())
    root.create_array("a", shape=(2,), dtype="u1")
    root.create_group("g")
    with pytest.raises(TypeError, match=f"{path}: expected an? "):
        narrow(root[path], path)
