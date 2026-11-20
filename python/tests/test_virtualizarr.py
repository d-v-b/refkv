"""refkv.virtualizarr: reference stores as VirtualiZarr manifest stores, and RefkvParser.

conformance/virtualizarr compares RefkvParser with VirtualiZarr's own parsers."""

import json

import h5py
import numpy as np
import pytest
import xarray as xr
import zarr
from obspec_utils.registry import ObjectStoreRegistry
from obstore.store import LocalStore
from virtualizarr import open_virtual_dataset
from virtualizarr.parsers import HDFParser

import refkv
from refkv import Concat, Policy, Range, ReferenceStore, Source
from refkv.errors import ResolutionError
from refkv.virtualizarr import RefkvParser, manifest_store
from refkv.zarrify.layouts import Layout

GROUP = b'{"zarr_format": 3, "node_type": "group", "attributes": {"title": "kinds"}}'


def array_document(shape, chunks, separator="/", encoding="default"):
    """A zarr.json for an uncompressed little-endian int32 array, its fill value -1."""
    return json.dumps({
        "zarr_format": 3,
        "node_type": "array",
        "shape": shape,
        "data_type": "int32",
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": chunks}},
        "chunk_key_encoding": {"name": encoding, "configuration": {"separator": separator}},
        "fill_value": -1,
        "codecs": [{"name": "bytes", "configuration": {"endian": "little"}}],
        "attributes": {"units": "m"},
    }).encode()


def int32s(*values):
    return np.array(values, dtype="<i4").tobytes()


def read(store, path):
    """The values of the array at `path` in the ManifestStore `store`."""
    return np.asarray(zarr.open_array(store, path=path, mode="r", zarr_format=3)[...])


@pytest.fixture
def registry():
    return ObjectStoreRegistry({"file://": LocalStore()})


@pytest.fixture
def source(tmp_path):
    """A file holding the int32s 0..7, with a space in its name."""
    path = tmp_path / "the source.bin"
    path.write_bytes(int32s(*range(8)))
    return path


def kinds_store(tmp_path, source):
    """A store with each kind of value a chunk can hold, and each chunk key encoding."""
    return ReferenceStore(
        {
            "zarr.json": GROUP,
            "kinds/zarr.json": array_document([14], [2]),
            # a range of a url source, and the same in a Concat of one
            "kinds/c/0": Range(source=0, offset=0, length=8),
            "kinds/c/1": Concat((Range(source=0, offset=8, length=8),)),
            # bytes, a literal range, and a range of a data source and of a key source
            "kinds/c/2": int32s(10, 11),
            "kinds/c/3": Range(data=int32s(12, 13)),
            "kinds/c/4": Range(source=1, offset=0, length=8),
            "kinds/c/5": Range(source=3, offset=0, length=8),
            # several ranges, one of a url source: read now and inlined
            "kinds/c/6": Concat((Range(data=int32s(20)), Range(source=0, offset=4, length=4))),
            # kinds/c/7 is missing
            "dots/zarr.json": array_document([2, 2], [1, 1], separator="."),
            "dots/c.0.0": int32s(1),
            "dots/c.1.1": int32s(2),
            "v2_keys/zarr.json": array_document([2], [1], separator=".", encoding="v2"),
            "v2_keys/1": int32s(3),
            "scalar/zarr.json": array_document([], []),
            "scalar/c": int32s(4),
            "nested/zarr.json": GROUP,
            "nested/inner/zarr.json": array_document([2], [2]),
            # a source relative to the store's URL
            "nested/inner/c/0": Range(source=2, offset=24, length=8),
            "skipped/zarr.json": array_document([2], [2]),
            "skipped/c/0": int32s(16, 17),
        },
        [
            Source(url=source.as_uri()),
            Source(data=int32s(14, 15)),
            Source(url="the%20source.bin"),
            Source(key="skipped/c/0"),
        ],
        url=f"{tmp_path.as_uri()}/",
        policy=Policy(schemes=frozenset({"file"})),
    )


def test_manifest_store(tmp_path, source, registry):
    """Every kind of chunk, from a store in memory and the same store saved, with and without
    group= and skip_variables=, reads back the store's values through the ManifestStore."""
    in_memory = kinds_store(tmp_path, source)
    in_memory.save_vzip(tmp_path / "kinds.vzip")
    saved = refkv.open(str(tmp_path / "kinds.vzip"), policy=Policy(schemes=frozenset({"file"})))
    url = source.as_uri().replace("%20", " ")

    for store in (in_memory, saved):
        result = manifest_store(store, registry, skip_variables=["skipped"])
        assert dict(zarr.open_group(result, mode="r", zarr_format=3).attrs) == {"title": "kinds"}
        assert dict(zarr.open_array(result, path="kinds", mode="r", zarr_format=3).attrs) == {"units": "m"}
        np.testing.assert_array_equal(read(result, "kinds"), [0, 1, 2, 3, 10, 11, 12, 13, 14, 15, 16, 17, 20, 1])
        np.testing.assert_array_equal(read(result, "dots"), [[1, -1], [-1, 2]])
        np.testing.assert_array_equal(read(result, "v2_keys"), [-1, 3])
        assert read(result, "scalar") == 4
        np.testing.assert_array_equal(read(result, "nested/inner"), [6, 7])
        assert "skipped" not in result._group.arrays

        # single ranges of url sources stay references, by unquoted absolute URL; the rest is inlined
        refs = dict(result._group.arrays["kinds"].manifest.iter_refs())
        inlined = {"path": "__inlined__", "offset": 0, "length": 8}
        assert refs == {(0,): {"path": url, "offset": 0, "length": 8},
                        (1,): {"path": url, "offset": 8, "length": 8},
                        **dict.fromkeys([(2,), (3,), (4,), (5,), (6,)], inlined)}
        inner = dict(result._group.groups["nested"].arrays["inner"].manifest.iter_refs())
        assert inner == {(0,): {"path": url, "offset": 24, "length": 8}}

        nested = manifest_store(store, registry, group="nested")
        np.testing.assert_array_equal(read(nested, "inner"), [6, 7])

        everything = manifest_store(store, registry)
        np.testing.assert_array_equal(read(everything, "skipped"), [16, 17])


def test_from_virtualizarr_roundtrip(tmp_path, registry):
    """A virtual dataset through a ReferenceStore and back keeps its references and values."""
    path = tmp_path / "source.h5"
    expected = xr.Dataset({"a": (("x", "y"), np.arange(12.0).reshape(3, 4))}, coords={"x": [1, 2, 3]},
                          attrs={"title": "roundtrip"})
    expected.to_netcdf(path, engine="h5netcdf", encoding={"a": {"chunksizes": (1, 4)}})
    with open_virtual_dataset(path.as_uri(), registry=registry, parser=HDFParser(),
                              loadable_variables=["x"]) as vds:
        store = ReferenceStore.from_virtualizarr(vds, registry=registry, policy=Policy(schemes=frozenset({"file"})))
        original = vds["a"].data.manifest.dict()

    result = manifest_store(store, registry)
    assert result._group.arrays["a"].manifest.dict() == original
    actual = xr.open_zarr(result, zarr_format=3, consolidated=False).load()
    xr.testing.assert_identical(actual, expected)


def test_refkv_parser(tmp_path, registry):
    """RefkvParser zarrifies a file, by path or file: URL, to its arrays without refkv's mirror,
    with each of its options."""
    path = tmp_path / "a file.h5"
    data = np.arange(16.0).reshape(4, 4)
    with h5py.File(path, "w") as f:
        f.create_dataset("a", data=data, chunks=(2, 2), compression="gzip")
        f.create_dataset("g/b", data=data)

    for url in (str(path), path.as_uri()):
        result = RefkvParser()(url, registry)
        assert "refkv_source" not in result._group.groups
        np.testing.assert_array_equal(read(result, "a"), data)
        np.testing.assert_array_equal(read(result, "g/b"), data)
        assert result._group.arrays["a"].manifest.shape_chunk_grid == (2, 2)

    assert set(RefkvParser(skip_variables=["a"])(str(path), registry)._group.arrays) == set()
    assert set(RefkvParser(group="g")(str(path), registry)._group.arrays) == {"b"}
    bare = RefkvParser(layout=Layout("refkv.example.bare"))(str(path), registry)
    np.testing.assert_array_equal(read(bare, "a"), data)
    policy = Policy(prefixes=(f"{tmp_path.as_uri()}/",))
    with_policy = RefkvParser(policy=policy)(str(path), registry)
    np.testing.assert_array_equal(read(with_policy, "a"), data)

    with open_virtual_dataset(path.as_uri(), registry=registry, parser=RefkvParser()) as vds:
        assert set(vds.variables) == {"a"}


def test_scheme_outside_policy_raises(registry):
    store = ReferenceStore(
        {"zarr.json": GROUP, "a/zarr.json": array_document([1], [1]), "a/c/0": Range(length=4)},
        [Source(url="s3://bucket/source.bin")],
    )
    with pytest.raises(ResolutionError, match="the scheme 's3' is not allowed"):
        manifest_store(store, registry)


def test_local_source_not_allowed_raises(source, registry):
    """A registered file:// store does not let a store name local files its policy refuses."""
    store = ReferenceStore(
        {"zarr.json": GROUP, "a/zarr.json": array_document([1], [1]), "a/c/0": Range(length=4)},
        [Source(url=source.as_uri())],
    )
    with pytest.raises(ResolutionError, match="reads a local file only when it is allowed"):
        manifest_store(store, registry)


def test_inlined_local_source_not_allowed_raises(source, registry):
    """A chunk read to be inlined is read under the store's policy too."""
    store = ReferenceStore(
        {"zarr.json": GROUP, "a/zarr.json": array_document([2], [2]),
         "a/c/0": Concat((Range(data=int32s(0)), Range(length=4)))},
        [Source(url=source.as_uri())],
    )
    with pytest.raises(ResolutionError, match="reads a local file only when it is allowed"):
        manifest_store(store, registry)
