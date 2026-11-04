"""Zarr v2 stores through the Rust IR (rust/refkv-ir/src/zarr2.rs; spec/zarrify/zarr2.md) on
the synthetic stores: the hierarchy outside `refkv_source` as the convention gives it, and the
IR's mirror under `refkv_source`, which rebuilds the listing and every document read.

Equivalence with the frozen reference (python/tests/reference/test_zarrify_zarr2.py) and the
browser zarrifier is checked by conformance/zarrify/compare.py, and pixel correctness against
zarr-python's Zarr v2 reader by js/test/zarr2/verify.py.
"""

import json
import sys
import zipfile
from pathlib import Path

import numpy as np
import pytest
import zarr
from zarr.storage import LocalStore

import refkv.zarr
from refkv import _core
from refkv._narrow import at, mapping, ndarray
from refkv.ir.store import StoreIrOutput
from refkv.policy import Policy
from refkv.zarrify import _zarrify as zarrify  # the profile output, not the store
from refkv.zarrify.common import declare
from refkv.zarrify.store import object_url

sys.path.insert(0, str(Path(__file__).parents[3] / "conformance" / "zarrify"))
from proxy import Proxy

FIXTURES = Path(__file__).parents[3] / "fixtures" / "zarr2"
URL = "https://data.test/zarr2/{}/"

LE = {"name": "bytes", "configuration": {"endian": "little"}}
BE = {"name": "bytes", "configuration": {"endian": "big"}}
ONE = {"name": "bytes"}
T2 = {"name": "transpose", "configuration": {"order": [1, 0]}}
T3 = {"name": "transpose", "configuration": {"order": [2, 1, 0]}}
ZLIB = {"name": "numcodecs.zlib", "configuration": {"level": 1}}


def blosc(cname, clevel, shuffle, typesize, blocksize=0):
    return {"name": "blosc", "configuration": {"cname": cname, "clevel": clevel, "shuffle": shuffle,
                                               "typesize": typesize, "blocksize": blocksize}}


def named(name, **configuration):
    return {"name": name, "configuration": configuration}


FSO = named("numcodecs.fixedscaleoffset", scale=4, offset=1, dtype="<f8", astype="<i2")
QUANTIZE = named("numcodecs.quantize", digits=2, dtype="<f8", astype="<f8")
VLEN = {"name": "vlen-utf8"}
LZMA = named("numcodecs.lzma", format=1, check=-1, preset=None, filters=None)
FIELDS = named("struct", fields=[{"name": "a", "data_type": "uint16"}, {"name": "b", "data_type": "float32"}])
UTF32 = lambda n: named("fixed_length_utf32", length_bytes=n)  # noqa: E731
DATETIME = lambda u, k: named("numpy.datetime64", unit=u, scale_factor=k)  # noqa: E731

# store: {array path: (data type, codecs, separator, fill value)}, summary members
CASES = {
    "zarr2_types": ({
        "crs": ("uint8", [ONE], ".", 0),
        "gage_id": ("uint8", [ONE, blosc("lz4", 5, "shuffle", 1)], ".", 0),
        "bytes_f_nested": ("uint8", [{"name": "transpose", "configuration": {"order": [1, 0, 2]}}, ONE], "/", 0),
        "bytes_fill": ("uint8", [ONE], ".", 32),
        "bytes_empty_fill": ("uint8", [ONE], ".", 0),
        "void": ("uint8", [ONE], ".", 0),
        "unicode_le": (UTF32(12), [LE], ".", "n/a"),
        "unicode_be": (UTF32(8), [BE], ".", ""),
        "complex64": ("complex64", [LE], ".", [1.5, "0x80000000"]),
        "complex128_be": ("complex128", [BE], ".", ["NaN", "Infinity"]),
        "complex_number_fill": ("complex128", [LE], ".", [0, 0]),
        "complex_null_fill": ("complex64", [LE], ".", [0, 0]),
        "datetime_nat": (DATETIME("ns", 1), [LE], ".", -9223372036854775808),
        "timedelta_be": (named("numpy.timedelta64", unit="s", scale_factor=10), [BE], ".", 7),
        "datetime_word": (DATETIME("D", 1), [LE], ".", "NaT"),
        "struct": (FIELDS, [LE], ".", {"a": 0, "b": 0}),
        "struct_nested": (named("struct", fields=[
            {"name": "p", "data_type": named("struct", fields=[{"name": "x", "data_type": "int16"}])},
            {"name": "q", "data_type": "uint8"}]), [BE], ".", {"p": {"x": 0}, "q": 0}),
    }, {"arrays": 17, "chunks": 13}),
    "zarr2_codecs": ({
        "lz4": ("uint16", [LE, named("numcodecs.lz4", acceleration=1)], ".", 0),
        "lz4_default": ("uint16", [LE, named("numcodecs.lz4", acceleration=1)], ".", 0),
        "lz4_extra": ("uint16", [LE, named("numcodecs.lz4", acceleration=1)], ".", 0),
        "bz2": ("uint16", [LE, named("numcodecs.bz2", level=1)], ".", 0),
        "bz2_default": ("uint16", [LE, named("numcodecs.bz2", level=1)], ".", 0),
        "lzma": ("uint16", [LE, LZMA], ".", 0),
        "lzma_default": ("uint16", [LE, LZMA], ".", 0),
        "fixedscaleoffset": ("float64", [FSO, LE], ".", 0),
        "fixedscaleoffset_f_zlib": ("float64", [T2, FSO, LE, ZLIB], ".", 0),
        "fixedscaleoffset_u1_blosc": ("float64", [
            named("numcodecs.fixedscaleoffset", scale=1, offset=0, dtype="<f8", astype="|u1"), ONE,
            blosc("lz4", 5, "bitshuffle", 1)], ".", 0),
        "fixedscaleoffset_be": ("float32", [
            named("numcodecs.fixedscaleoffset", scale=10, offset=0, dtype=">f4", astype=">i4"), BE], ".", 0),
        "quantize": ("float64", [QUANTIZE, LE, named("zstd", level=1, checksum=False)], ".", 0),
        "quantize_then_fixedscaleoffset": ("float64", [
            QUANTIZE, named("numcodecs.fixedscaleoffset", scale=8, offset=0, dtype="<f8", astype="<i4"), LE], ".", 0),
        "strings_anndata": ("string", [VLEN, blosc("lz4", 5, "shuffle", 1)], ".", ""),
        "strings_null_fill": ("string", [VLEN], ".", ""),
        "strings_f_fill": ("string", [T2, VLEN, ZLIB], ".", "none"),
    }, {"arrays": 16, "chunks": 16}),
    "zarr2_compressors": ({
        "raw_c": ("int16", [LE], ".", 0),
        "zlib_f": ("uint16", [T3, LE, ZLIB], ".", 0),
        "gzip_nested": ("float32", [LE, {"name": "gzip", "configuration": {"level": 5}}], "/", 0),
        "zstd_f_nested": ("float64", [T3, LE, {"name": "zstd", "configuration": {"level": 1, "checksum": False}}],
                          "/", 0),
        "blosc_shuffle": ("int32", [LE, blosc("lz4", 5, "shuffle", 4)], ".", 0),
        "blosc_bitshuffle": ("uint64", [LE, blosc("zstd", 3, "bitshuffle", 8)], ".", 0),
        "blosc_noshuffle": ("int64", [LE, blosc("zlib", 1, "noshuffle", 8, 256)], ".", 0),
        "blosc_autoshuffle_u1": ("uint8", [ONE, blosc("blosclz", 9, "bitshuffle", 1)], ".", 0),
        "blosc_autoshuffle_i2": ("int16", [LE, blosc("lz4hc", 2, "shuffle", 2)], ".", 0),
        "f_order_1d": ("uint32", [LE, ZLIB], ".", 0),
    }, {"arrays": 10, "groups": 1, "chunks": 12}),
    "zarr2_dtypes": ({
        "na_b1": ("bool", [ONE], ".", False), "na_i1": ("int8", [ONE], ".", 0), "be_i2": ("int16", [BE], ".", 0),
        "le_u2": ("uint16", [LE], ".", 0), "be_u4": ("uint32", [BE], ".", 0), "be_i8": ("int64", [BE], ".", 0),
        "le_f2": ("float16", [LE], ".", 0), "be_f2": ("float16", [BE], ".", 0), "be_f8": ("float64", [BE], ".", 0),
    }, {"arrays": 14}),
    "zarr2_fill_values": ({
        "nan": ("float32", [LE], ".", "NaN"), "inf": ("float64", [LE, ZLIB], ".", "Infinity"),
        "neg_inf": ("float32", [BE], ".", "-Infinity"), "null_int": ("int16", [LE], ".", 0),
        "null_bool": ("bool", [ONE], ".", False), "true_bool": ("bool", [ONE], ".", True),
        "neg_i1": ("int8", [ONE], ".", -5), "float_fill": ("float64", [LE], ".", 0.5),
        "float_as_int_fill": ("uint16", [LE], ".", 7), "f2_max": ("float16", [LE], ".", 65504),
    }, {"emptyChunks": 1, "chunks": 10}),
    "zarr2_scalar_root": ({"": ("float64", [LE, ZLIB], ".", 0)}, {"arrays": 1, "groups": 0, "chunks": 1}),
    "zarr2_hierarchy": ({
        "a/b/zero_d": ("int32", [LE], ".", 0), "a/c": ("uint16", [LE], "/", 0), "g/h": ("float32", [LE], ".", 0),
        "sp ace/é/x y": ("uint8", [ONE], ".", 0), "empty_shape": ("int16", [LE], ".", 0),
    }, {"arrays": 5, "groups": 6, "chunks": 5}),
    "zarr2_ome_attrs": ({"0/0": ("uint16", [LE, blosc("lz4", 5, "shuffle", 2)], "/", 0)}, {"groups": 8}),
    # The compressors' levels, which the codecs carry, and -0.0 fill values as hex fills.
    "zarr2_source_metadata": ({
        "null_fill": ("float32", [LE], ".", 0),
        "neg_zero_f2": ("float16", [BE], ".", "0x8000"),
        "neg_zero_f4": ("float32", [LE], ".", "0x80000000"),
        "neg_zero_f8": ("float64", [LE], ".", "0x8000000000000000"),
        "big_fill_f8": ("float64", [LE], ".", 18446744073709551616.0),
        "zlib_9": ("uint16", [LE, {"name": "numcodecs.zlib", "configuration": {"level": 9}}], ".", 0),
        "zlib_no_level": ("uint16", [LE, ZLIB], ".", 0),
        "gzip_default": ("uint16", [LE, {"name": "gzip", "configuration": {"level": 6}}], ".", 0),
        "zstd_checksum": ("uint16", [LE, {"name": "zstd", "configuration": {"level": -3, "checksum": True}}], ".", 0),
        "extra_members": ("uint16", [LE, blosc("lz4", 5, "shuffle", 2)], ".", 0),
    }, {"arrays": 10, "otherObjects": 0}),
    "zarr2_objects": ({"a": ("uint16", [LE], ".", 0)}, {"chunks": 3, "otherObjects": 6}),
}


def key(path: str) -> str:
    return f"{path}/zarr.json" if path else "zarr.json"


def listing(out, tmp_path: Path) -> dict[str, tuple[int, bytes | None]]:
    """The store's listing the archive's mirror rebuilds: key -> (size, the bytes of a held object)."""
    archive = tmp_path / "listing.vzip"
    out.write(str(archive))
    with zipfile.ZipFile(archive) as z:
        names = set(z.namelist())
        return {k: (n, b) for k, n, b in _core.rebuild_store(lambda k: z.read(k) if k in names else None)}


def test_zarrifies_the_synthetic_stores(tmp_path):
    for name, (arrays, summary) in CASES.items():
        fmt, out = zarrify(str(FIXTURES / name), url=URL.format(name))
        assert isinstance(out, StoreIrOutput)
        assert fmt == "zarr2", name
        assert {**out.summary, **summary} == out.summary, name
        for path, (dtype, codecs, sep, fill) in arrays.items():
            doc = mapping(out.docs[key(path)], key(path))
            assert (doc["data_type"], doc["codecs"], doc["chunk_key_encoding"], doc["fill_value"]) == \
                (dtype, codecs, {"name": "v2", "configuration": {"separator": sep}}, fill), (name, path)
        keys = [k for k, _ in out.chunks]
        assert keys == sorted(keys), name
        assert all(n > 0 for _, n in out.chunks), name
        assert out.sources == [object_url(URL.format(name), out.origins.get(k, k)) for k in keys], name
    # Chunk keys follow each array's separator and grid (two chunks, with a partial edge chunk,
    # along the first axis in C order and along the second in F order).
    _, out = zarrify(str(FIXTURES / "zarr2_compressors"), url=URL.format("c"))
    assert isinstance(out, StoreIrOutput)
    assert [k for k, _ in out.chunks if k.startswith(("raw_c/", "zstd_f_nested/"))] == [
        "raw_c/0.0", "raw_c/1.0", "zstd_f_nested/0/0/0", "zstd_f_nested/0/1/0"]
    # A missing chunk and an empty one have no entry.
    _, out = zarrify(str(FIXTURES / "zarr2_fill_values"), url=URL.format("f"))
    assert isinstance(out, StoreIrOutput)
    assert [k for k, _ in out.chunks if k.startswith(("nan/", "inf/"))] == ["inf/1.0", "nan/1.0"]
    # The empty one is an object of the mirror's listing, of size 0 (§4.1
    # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#41-elements>).
    assert listing(out, tmp_path)["nan/0.0"] == (0, None)
    # Stray objects and nodes inside arrays are kept whole under refkv_source/objects/
    # (spec/zarrify/zarr2.md §5
    # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#5-other-objects>);
    # .zmetadata is not.
    _, out = zarrify(str(FIXTURES / "zarr2_hierarchy"), url=URL.format("h"))
    assert isinstance(out, StoreIrOutput)
    assert [k for k, _ in out.chunks] == [
        "a/b/zero_d/0", "a/c/0/0", "a/c/1/0", "g/h/0", "refkv_source/objects/.zmetadata",
        "refkv_source/objects/a/c/0.0", "refkv_source/objects/a/c/0/9", "refkv_source/objects/a/c/00/1",
        "refkv_source/objects/a/c/inside/.zgroup~", "refkv_source/objects/g/h/0.0",
        "refkv_source/objects/notes/README.md", "refkv_source/objects/orphan/.zattrs", "sp ace/é/x y/0.0"]
    assert out.sources[-2] == "https://data.test/zarr2/h/orphan/.zattrs"
    assert out.sources[-1] == "https://data.test/zarr2/h/sp%20ace/%C3%A9/x%20y/0.0"
    assert out.docs["a/zarr.json"] == {"zarr_format": 3, "node_type": "group", "attributes": {}}
    assert at(out.docs, "a/c/zarr.json", "attributes") == declare({}, "zarr2", None,
                                                              {"attributes": {"_ARRAY_DIMENSIONS": ["y", "x"]}})
    assert not {"a/c/inside/zarr.json", "orphan/zarr.json", "ghost/zarr.json", "notes/zarr.json"} & set(out.docs)
    # Attributes are copied unchanged under the convention (spec/zarrify.md conventions §2
    # <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#2-attributes>), OME-NGFF 0.4
    # ones included: the root does not declare OME-NGFF 0.4 (§1.4
    # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>), so the store is
    # not read by the OME-Zarr profile.
    _, out = zarrify(str(FIXTURES / "zarr2_ome_attrs"), url=URL.format("o"))
    assert isinstance(out, StoreIrOutput)
    for path in ("", "0", "0/labels", "0/labels/cells", "v03", "has_ome", "mixed", "dup_axes"):
        zattrs = json.loads((FIXTURES / "zarr2_ome_attrs" / path / ".zattrs").read_text())
        expected = declare({}, "zarr2", URL.format("o") if path == "" else None, {"attributes": zattrs})
        assert at(out.docs, key(path), "attributes") == expected, path
    assert not any("dimension_names" in mapping(d, k) for k, d in out.docs.items() if not k.startswith("refkv_source/"))
    # Other conventions, a refkv declaration and a malformed zarr_conventions are all copied as they are.
    _, out = zarrify(str(FIXTURES / "zarr2_conventions"), url=URL.format("c"))
    assert isinstance(out, StoreIrOutput)
    for path in ("", "x", "v", "bad"):
        zattrs = json.loads((FIXTURES / "zarr2_conventions" / path / ".zattrs").read_text())
        assert at(out.docs, key(path), "attributes") == declare({}, "zarr2", URL.format("c") if path == "" else None,
                                                            {"attributes": zattrs}), path
    # Source metadata (spec/zarrify/zarr2.md §4
    # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#4-source-metadata>): the
    # attributes, with integers beyond 2^53
    # kept exact, and the members of .zarray and .zgroup the Zarr v3 metadata does not reproduce.
    url = URL.format("zarr2_source_metadata")
    _, out = zarrify(str(FIXTURES / "zarr2_source_metadata"), url=url)
    assert isinstance(out, StoreIrOutput)
    assert at(out.docs, "zarr.json", "attributes") == declare({}, "zarr2", url, {
        "attributes": {"id": 18446744073709551615, "neg": -9007199254740993, "safe": 9007199254740991},
        "metadata": {"creator": {"name": "a writer"}}})
    assert at(out.docs, "null_fill/zarr.json", "attributes") == declare({}, "zarr2", None,
                                                                    {"metadata": {"fill_value": None}})
    assert at(out.docs, "extra_members/zarr.json", "attributes") == declare({}, "zarr2", None, {
        "attributes": {"_ARRAY_DIMENSIONS": ["x"]}, "metadata": {"custom": {"x": 1}, "compressor": {"nthreads": 2}}})
    for path in ("neg_zero_f4", "zlib_9", "zstd_checksum"):
        assert at(out.docs, key(path), "attributes") == {}, path
    # A float fill written as an integer beyond 2^53 - 1 is kept as written (§4
    # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#4-source-metadata>).
    assert at(out.docs, key("big_fill_f8"), "attributes") == declare(
        {}, "zarr2", None, {"metadata": {"fill_value": 18446744073709551616}})
    # The archive holds the integers' every digit, and the hex fill.
    archive = tmp_path / "s.vzip"
    out.write(str(archive))
    with zipfile.ZipFile(archive) as z:
        assert b'"id":18446744073709551615,"neg":-9007199254740993' in z.read("zarr.json")
        assert b'"fill_value":"0x80000000"' in z.read("neg_zero_f4/zarr.json")
    # Every other object is referenced whole, from its own URL, under refkv_source/objects/ (a
    # node document's name escaped with ~); the node documents and the chunks are not; empty
    # objects are the mirror's, of size 0, and refkv_source's source metadata is the mirror.
    url = URL.format("zarr2_objects")
    _, out = zarrify(str(FIXTURES / "zarr2_objects"), url=url)
    assert isinstance(out, StoreIrOutput)
    others = [".zmetadata", "OME/METADATA.ome.xml", "README.md", "a/inner/.zgroup~", "a/notes.txt", "sub/.zattrs"]
    assert out.chunks == sorted([("a/0", 4), ("a/1", 4), ("sub/b/0", 2)] + [
        ("refkv_source/objects/" + k, (FIXTURES / "zarr2_objects" / k.rstrip("~")).stat().st_size) for k in others])
    assert out.sources == [url + out.origins.get(k, k) for k, _ in out.chunks]
    assert set(mapping(at(out.docs, "refkv_source/zarr.json", "attributes", "refkv_zarrified", "zarr2"))) == {"ir"}
    assert listing(out, tmp_path)["empty.txt"] == (0, None)
    assert "zarr2" not in mapping(at(out.docs, "zarr.json", "attributes", "refkv_zarrified"))
    assert "sub/zarr.json" in out.docs
    assert at(out.docs, "sub/zarr.json", "attributes") == {}
    # Escaping is one to one: one ~ more on each last segment that is a node document's name and ~s.
    _, out = zarrify(str(FIXTURES / "zarr2_node_names"), url=URL.format("n"))
    assert isinstance(out, StoreIrOutput)
    assert {k: out.origins[k] for k, _ in out.chunks if k.startswith("refkv_source/")} == {
        "refkv_source/objects/zarr.json~": "zarr.json", "refkv_source/objects/zarr.json~~": "zarr.json~",
        "refkv_source/objects/g/zarr.json~": "g/zarr.json", "refkv_source/objects/a/zarr.json~": "a/zarr.json",
        "refkv_source/objects/a/inner/.zgroup~": "a/inner/.zgroup", "refkv_source/objects/x/.zarray~~~": "x/.zarray~~",
        "refkv_source/objects/x/zarr.jsonx": "x/zarr.jsonx"}
    sizes = listing(out, tmp_path)
    assert (sizes["e"], sizes["g/e2"]) == ((0, None), (0, None))
    # A float fill written as an integer beyond 2^53 - 1 keeps its digits in M; F is its binary64 value.
    _, out = zarrify(str(FIXTURES / "zarr2_fill_float_int"), url=URL.format("i"))
    assert isinstance(out, StoreIrOutput)
    m = {p: mapping(mapping(at(out.docs, key(p), "attributes")).get("refkv_zarrified", {})).get("zarr2")
         for p in ("h", "k", "s")}
    assert m == {"h": {"metadata": {"fill_value": 9007199254740993}},
                 "k": {"metadata": {"fill_value": -18446744073709551616}}, "s": None}
    assert [at(out.docs, key(p), "fill_value") for p in ("h", "k", "s")] == [2.0**53, -(2.0**64), 2**53 - 1]
    archive = tmp_path / "i.vzip"
    out.write(str(archive))
    with zipfile.ZipFile(archive) as z:
        assert b'"fill_value":9007199254740993' in z.read("h/zarr.json")


def test_a_trailing_axis_adds_to_the_shape_and_the_chunk_keys():
    """`|S<n>` and `|V<n>` (spec/zarrify/zarr2.md §3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#3-arrays>, §3.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#32-chunks>): each chunk's entry
    has one more index than its object's key, but for a scalar's."""
    _, out = zarrify(str(FIXTURES / "zarr2_types"), url=URL.format("t"))
    assert isinstance(out, StoreIrOutput)
    for path, shape, chunk_shape, entry, origin in (
            ("crs", [1], [1], "crs/0", "crs/0"),
            ("gage_id", [3, 15], [3, 15], "gage_id/0.0", "gage_id/0"),
            ("bytes_f_nested", [2, 3, 2], [2, 3, 2], "bytes_f_nested/0/0/0", "bytes_f_nested/0/0"),
            ("bytes_fill", [2, 2], [1, 2], "bytes_fill/1.0", "bytes_fill/1")):
        doc = mapping(out.docs[key(path)], path)
        assert (doc["shape"], at(doc, "chunk_grid", "configuration", "chunk_shape")) == (shape, chunk_shape), path
        assert entry in {k for k, _ in out.chunks}, path
        assert out.origins.get(entry, entry) == origin, path
    # M keeps what the Zarr metadata does not give back (§4
    # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#4-source-metadata>).
    metadata = lambda p: at(out.docs, key(p), "attributes", "refkv_zarrified", "zarr2", "metadata")  # noqa: E731
    assert metadata("void") == {"dtype": "|V2", "fill_value": None}
    assert metadata("bytes_fill") == {"dtype": "|S2", "fill_value": "ICA="}
    assert metadata("complex_number_fill") == {"fill_value": 0}
    assert metadata("struct") == {"fill_value": "AAAAAAAA"}


@pytest.mark.parametrize("name", ["zarr2_types", "zarr2_codecs"])
def test_the_arrays_read_as_zarr_python_reads_the_zarr_v2_store(name, tmp_path):
    """Each array, through the core's Zarr v3 hierarchy and zarr-python's codecs, against
    zarr-python's own Zarr v2 reading of the store."""
    base = Proxy(FIXTURES.parent, tmp_path).base
    policy = Policy(allow_private_hosts=True)
    _, out = zarrify(f"{base}f/zarr2/{name}/", policy=policy)
    assert isinstance(out, StoreIrOutput)
    root = zarr.open_group(refkv.zarr.store(out.to_store(policy=policy)), mode="r", zarr_format=3)
    copy = tmp_path / name
    (copy).mkdir()
    for f in (FIXTURES / name).rglob("*"):
        if f.is_file():
            to = copy / f.relative_to(FIXTURES / name)
            to.parent.mkdir(parents=True, exist_ok=True)
            to.write_bytes(f.read_bytes())
            if to.name == ".zarray" and to.parent.name == "lz4_extra":
                # numcodecs takes no member it does not know, and it does not change how chunks decode
                z = json.loads(to.read_text())
                del z["compressor"]["writer"]
                to.write_text(json.dumps(z))
    for path, array in root.arrays():
        # zarr-python 3 refuses a complex fill of one number, which zarr-python 2 reads; no chunks here
        if path in ("refkv_source", "complex_number_fill"):
            continue
        got = ndarray(array[...])
        if path == "struct_nested":  # zarr-python's Zarr v2 reader does not take a nested structure
            want = np.frombuffer((copy / path / "0").read_bytes(), dtype=[("p", [("x", ">i2")]), ("q", "u1")])
        else:
            want = ndarray(zarr.open_array(LocalStore(copy, read_only=True), path=path, mode="r", zarr_format=2)[...])
        if want.dtype.kind in "SV" and not want.dtype.names:  # its bytes, as a last axis
            want = np.frombuffer(want.tobytes(), "u1").reshape(got.shape)
        if got.dtype.kind in "TO":  # zarr-python's own string types
            got, want = got.astype(object), want.astype(object)
        assert got.shape == want.shape, path
        assert got.dtype.names == want.dtype.names, path
        assert np.array_equal(got, want, equal_nan=got.dtype.kind in "fcmM"), path


def test_an_array_that_cannot_be_presented_is_left_out_and_recorded(tmp_path):
    """Each failure leaves its array out, the rest of the hierarchy written, and every byte kept
    (spec/zarrify/zarr2.md §3.4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#34-arrays-that-are-not-presented>)."""
    name = "zarr2_left_out"
    _, out = zarrify(str(FIXTURES / name), url=URL.format(name))
    assert isinstance(out, StoreIrOutput)
    own = at(out.docs, "zarr.json", "attributes", "refkv_zarrified", "zarr2")
    reasons = {r["path"]: r["reason"] for r in mapping({"r": own}, "o")["r"]["not_presented"]}
    assert list(reasons) == sorted(reasons)
    assert {p: r.split(":")[0] for p, r in reasons.items()} == {
        "a/filtered": "filter delta is not supported",
        "bad_attrs": "bad_attrs/.zattrs is not a JSON object",
        "empty_doc": "empty_doc/.zarray",
        "fill": "fill_value AAE= is not a |S2",
        "not_json": "not_json/.zarray",
        "pickled": "dtype |O is not supported",
        "shapeless": "shapeless",
    }
    assert out.summary["notPresented"] == 7
    assert out.summary["arrays"] == 1
    for path in reasons:
        assert key(path) not in out.docs, path
    assert key("kept") in out.docs
    assert key("a") in out.docs  # its ancestor is still a group
    # the chunks are other objects, and the documents read are the mirror's, whatever they hold
    others = {k for k, _ in out.chunks if k.startswith("refkv_source/objects/")}
    assert others == {f"refkv_source/objects/{k}" for k in (
        "pickled/0", "pickled/inside/.zgroup~", "a/filtered/0", "shapeless/0", "not_json/0", "empty_doc/0", "bad_attrs/0.0")}
    sizes = listing(out, tmp_path)
    assert sizes["not_json/.zarray"] == (9, b"{not json")
    assert sizes["empty_doc/.zarray"] == (0, None)
    assert sizes["pickled/empty"] == (0, None)
    assert sizes["pickled/.zattrs"][1] == (FIXTURES / name / "pickled/.zattrs").read_bytes()


def test_a_root_array_that_cannot_be_presented_rejects_the_store():
    with pytest.raises(Exception, match="dtype .O is not supported"):
        zarrify(str(FIXTURES / "zarr2_reject_dtype_object"), url=URL.format("o"))


def test_root_array_keeps_other_objects_without_a_group(tmp_path):
    """An array has no children: under a root array, the other objects and the mirror are plain
    keys under refkv_source/ (spec/zarrify/zarr2.md §5
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#5-other-objects>)."""
    name = "zarr2/zarr2_root_array_objects"
    _, out = zarrify(str(FIXTURES.parent / name), url=f"https://data.test/{name}/")
    assert isinstance(out, StoreIrOutput)
    assert at(out.docs, "zarr.json", "node_type") == "array"
    assert "refkv_source/objects/README.md" in {k for k, _ in out.chunks}
    # Empty objects, an empty chunk object among them, are the mirror's objects of size 0.
    name = "zarr2/zarr2_root_array_empty"
    _, out = zarrify(str(FIXTURES.parent / name), url=f"https://data.test/{name}/")
    assert isinstance(out, StoreIrOutput)
    sizes = listing(out, tmp_path)
    assert (sizes["empty"], sizes["\u00e9mpty"]) == ((0, None), (0, None))
    assert "refkv_source/empty.json" not in out.docs
    name = "zarr2/zarr2_root_array_empty_chunk"
    _, out = zarrify(str(FIXTURES.parent / name), url=f"https://data.test/{name}/")
    assert isinstance(out, StoreIrOutput)
    assert [k for k, _ in out.chunks] == ["0"]
    assert listing(out, tmp_path)["1"] == (0, None)
