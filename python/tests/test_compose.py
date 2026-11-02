"""Bounds (refkv.bounds) and the operators that combine reference stores by them
(refkv.compose): overlay, union, map_keys, mount, shift_chunks and normalize."""

import json
import random

import numpy as np
import pytest
import tifffile
import zarr

import refkv.zarr
from refkv import ReferenceStore
from refkv.bounds import ChunkBox, Key, KeyRange, Prefix, chunk_key, derive, parse_chunk_key
from refkv.compose import map_keys, mount, normalize, overlay, shift_chunks, union
from refkv.pb import Concat, Range, Source
from refkv.zarrify import zarrify

GROUP = b'{"zarr_format":3,"node_type":"group","attributes":{}}'


def array(shape, chunks, separator="/", encoding="default"):
    """A v3 array document of uint8 with a regular grid."""
    return json.dumps({
        "zarr_format": 3, "node_type": "array", "shape": list(shape), "data_type": "uint8",
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": list(chunks)}},
        "chunk_key_encoding": {"name": encoding, "configuration": {"separator": separator}},
        "fill_value": 0, "codecs": [{"name": "bytes"}],
    }).encode()


def values(store):
    return dict(store.mapping)


def document(store, key):
    """The JSON document at `key` of `store`."""
    value = store.mapping[key]
    assert isinstance(value, bytes)
    return json.loads(value)


def test_bounds_contain_their_keys():
    cases = [
        (Key("a/b"), "a/b", True), (Key("a/b"), "a/bc", False),
        (KeyRange("b", "d"), "b", True), (KeyRange("b", "d"), "cz", True), (KeyRange("b", "d"), "d", False),
        (KeyRange("b"), "zzz", True), (KeyRange("b"), "a", False), (KeyRange("é"), "z", False),
        (Prefix("0/c/1"), "0/c/1/0", True), (Prefix("0/c/1"), "0/c/10/0", False),
        (Prefix("0/c/1"), "0/c/1", False), (Prefix(""), "anything", True),
        (ChunkBox("0", (1, 0), (3, 2)), "0/c/2/1", True), (ChunkBox("0", (1, 0), (3, 2)), "0/c/3/1", False),
        (ChunkBox("0", (1, 0), (3, 2)), "0/c/01/1", False), (ChunkBox("0", (1, 0), (3, 2)), "0/c/2", False),
        (ChunkBox("0", (1, 0), (3, 2)), "0/zarr.json", False),
        (ChunkBox("", (0,), (5,)), "c/4", True), (ChunkBox("a", (0,), (5,), separator="."), "a/c.4", True),
        (ChunkBox("a", (0, 0), (5, 5), "v2", "."), "a/4.0", True),
        (ChunkBox("a", (0, 0), (5, 5), "v2", "/"), "a/4/0", True),
        (ChunkBox("a", (), ()), "a/c", True), (ChunkBox("a", (), (), "v2", "."), "a/0", True),
        (ChunkBox("a", (0,), (0,)), "a/c/0", False),
    ]
    for bound, key, expected in cases:
        assert bound.contains(key) is expected, (bound, key)
    for b, k, _ in cases:
        if isinstance(b, Prefix):
            assert b.as_range().contains(k) is b.contains(k), (b, k)
    for coords in [(), (0,), (3, 12, 0)]:
        for encoding, sep in [("default", "/"), ("default", "."), ("v2", "."), ("v2", "/")]:
            key = chunk_key("x/y", coords, encoding, sep)
            assert parse_chunk_key(key, "x/y", len(coords), encoding, sep) == coords


def test_derived_bounds_follow_the_zarr_nodes():
    mapping = {"zarr.json": GROUP, "a/zarr.json": array([4], [2]), "a/c/0": b"x", "g/zarr.json": GROUP,
               "g/stray": b"s", "v2/.zgroup": b'{"zarr_format":2}', "v2/.zattrs": b"{}",
               "v2/arr/.zarray": b"{}", "v2/arr/0": b"y", "loose": b"z", "__vz__/hidden": b"h"}
    assert set(derive(mapping)) == {Key("zarr.json"), Prefix("a"), Key("g/zarr.json"), Key("g/stray"),
                                    Key("v2/.zgroup"), Key("v2/.zattrs"), Prefix("v2/arr"), Key("loose")}
    assert derive({"zarr.json": array([4], [2]), "c/0": b"x"}) == (Prefix(""),)
    store = ReferenceStore(mapping)
    assert set(store.bounds) == set(derive(mapping))
    assert not store.bounds_stated
    stated = store.with_bounds([Key("loose")])
    assert stated.bounds == (Key("loose"),)
    assert stated.bounds_stated
    assert values(stated) == values(store)


def stack_parts(n=3):
    """A header and `n` planes, each plane a 4 x 4 array of 2 x 2 chunks."""
    planes = [ReferenceStore({"zarr.json": GROUP, "0/zarr.json": array([4, 4], [2, 2]),
                              **{f"0/c/{y}/{x}": bytes([i * 10 + y * 2 + x]) * 4 for y in range(2) for x in range(2)}})
              for i in range(n)]
    header = ReferenceStore({"zarr.json": GROUP, "0/zarr.json": array([n, 4, 4], [1, 2, 2])},
                            bounds=[Key("zarr.json"), Key("0/zarr.json")])
    return header, [shift_chunks(p, "0", (i, 0, 0), new_axes=1) for i, p in enumerate(planes)]


def test_overlay_and_union_answer_each_key_from_the_store_that_bounds_it():
    header, planes = stack_parts()
    stack = union(header, *planes)
    assert values(stack) == values(union(*reversed(planes), header))
    data = np.asarray(zarr.open_group(refkv.zarr.store(stack), mode="r").get_array("0")[...])
    assert data.shape == (3, 4, 4)
    assert data[2, 3, 1] == 2 * 10 + 1 * 2 + 0
    patch = ReferenceStore({"0/c/1/0/0": b"\xff" * 4}, bounds=[ChunkBox("0", (1, 0, 0), (2, 1, 1))])
    patched = overlay(stack, patch)
    assert patched.mapping["0/c/1/0/0"] == b"\xff" * 4
    assert len(patched.mapping) == len(stack.mapping)
    gone = ReferenceStore({}, bounds=[ChunkBox("0", (2, 0, 0), (3, 2, 2))])
    cleared = np.asarray(zarr.open_group(refkv.zarr.store(overlay(stack, gone)), mode="r").get_array("0")[...])
    assert not cleared[2].any()
    assert cleared[1].any()
    # a key outside a store's own bounds is not carried
    assert "zarr.json" not in overlay(ReferenceStore({"zarr.json": GROUP}, bounds=[Key("x")])).mapping
    # the later store wins a key both bound
    a, b = ReferenceStore({"k": b"a"}), ReferenceStore({"k": b"b"})
    assert overlay(a, b).mapping["k"] == b"b"
    assert overlay(b, a).mapping["k"] == b"a"
    # union accepts stores that answer a shared key the same
    assert union(a, ReferenceStore({"k": b"a", "j": b"j"})).mapping == {"k": b"a", "j": b"j"}
    # node shadowing's counterexample: the same keys however the overlay nests
    arr = array([1], [1])
    A = ReferenceStore({"zarr.json": GROUP, "p/zarr.json": GROUP, "p/q/zarr.json": arr, "p/q/c/0": b"1"})
    B = ReferenceStore({"zarr.json": GROUP, "p/zarr.json": arr, "p/c/0": b"2"})
    C = ReferenceStore({"zarr.json": GROUP, "p/zarr.json": GROUP, "p/r/zarr.json": arr, "p/r/c/0": b"3"})
    nested = [overlay(C, overlay(B, A)), overlay(overlay(C, B), A), overlay(C, B, A)]
    assert all(values(n) == values(nested[0]) for n in nested)
    assert set(normalize(nested[0]).mapping) == {"zarr.json", "p/zarr.json", "p/q/zarr.json", "p/q/c/0"}
    # identity and idempotence
    empty = ReferenceStore({})
    for s in (A, stack):
        assert values(overlay(s, s)) == values(overlay(s)) == values(overlay(empty, s)) == values(overlay(s, empty))


def test_combining_merges_source_tables():
    a = ReferenceStore({"x": Range(source=1, offset=0, length=2), "y": Range(source=1, offset=2, length=2)},
                       [Source(url="unused.bin"), Source(url="d.bin", size=9)], url="file:///data/")
    b = ReferenceStore({"z": Concat((Range(source=0, offset=4, length=1), Range(data=b"!"))),
                        "w": Range(source=1, offset=0, length=1)},
                       [Source(url="file:///data/d.bin", size=9), Source(url="https://h.test/e.bin")],
                       url="file:///other/")
    out = overlay(a, b)
    assert list(out.sources) == [Source(url="file:///data/d.bin", size=9), Source(url="https://h.test/e.bin")]
    assert out.mapping["x"] == Range(source=0, offset=0, length=2)
    assert out.mapping["z"] == Concat((Range(source=0, offset=4, length=1), Range(data=b"!")))
    assert out.mapping["w"] == Range(source=1, offset=0, length=1)
    assert out.url == "file:///data/"
    assert overlay(a, b, url="https://h.test/").url == "https://h.test/"
    # read_at_open and compressed follow the keys they name
    c = ReferenceStore({"m/zarr.json": GROUP, "n": b"n" * 100}, compressed=["n"])
    d = ReferenceStore({"m/zarr.json": b'{"zarr_format":3,"node_type":"group","attributes":{"x":1}}'})
    assert overlay(c, d).read_at_open == {"m/zarr.json"}
    assert overlay(c, d).compressed == {"n"}
    # key sources (internal references) are carried when their key comes from their own store
    e = ReferenceStore({"hdr": b"HEAD", "v": Range(source=0, offset=0, length=2)}, [Source(key="hdr")])
    assert overlay(e, ReferenceStore({"other": b"o"})).mapping["v"] == Range(source=0, offset=0, length=2)


def test_map_keys_mount_shift_and_normalize():
    s = ReferenceStore({"zarr.json": GROUP, "0/zarr.json": array([4], [2]), "0/c/0": b"a", "0/c/1": b"b",
                        "hdr": b"H", "v": Range(source=0, length=1)}, [Source(key="hdr")])
    renamed = map_keys(s, lambda k: None if k == "0/c/1" else "x" + k)
    assert set(renamed.mapping) == {"xzarr.json", "x0/zarr.json", "x0/c/0", "xhdr", "xv"}
    assert list(renamed.sources) == [Source(key="xhdr")]
    assert not renamed.bounds_stated
    m = mount(s.with_bounds([Key("zarr.json"), Prefix("0"), KeyRange("h", "w"), KeyRange("v"),
                             ChunkBox("0", (0,), (2,)), Prefix("")]), "a/b")
    assert set(m.mapping) == {"a/b/" + k for k in s.mapping}
    assert list(m.sources) == [Source(key="a/b/hdr")]
    assert m.bounds == (Key("a/b/zarr.json"), Prefix("a/b/0"), KeyRange("a/b/h", "a/b/w"),
                        KeyRange("a/b/v", "a/b0"), ChunkBox("a/b/0", (0,), (2,)), Prefix("a/b"))
    for encoding, sep, v2 in [("default", "/", False), ("default", ".", False), ("v2", ".", True), ("v2", "/", True)]:
        doc = (json.dumps({"zarr_format": 2, "shape": [5, 3], "chunks": [2, 3], "dimension_separator": sep}).encode()
               if v2 else array([5, 3], [2, 3], sep, encoding))
        chunks = {chunk_key("im", (y, 0), encoding, sep): bytes([y]) for y in range(3)}
        src = ReferenceStore({("im/.zarray" if v2 else "im/zarr.json"): doc, **chunks})
        moved = shift_chunks(src, "im", (4, 1, 0), new_axes=1)
        assert values(moved) == {chunk_key("im", (4, 1 + y, 0), encoding, sep): bytes([y]) for y in range(3)}
        assert moved.bounds == (ChunkBox("im", (4, 1, 0), (5, 4, 1), encoding, sep),)  # pyrefly: ignore[bad-argument-type]
    n = normalize(ReferenceStore({"a/b/zarr.json": GROUP, "a/b/c/zarr.json": array([1], [1]), "a/b/c/c/0": b"0",
                                  "a/b/c/x/zarr.json": GROUP, "a/b/stray": b"s", "loose": b"l",
                                  "v2/.zarray": b"{}", "v2/0": b"0"}))
    assert set(n.mapping) == {"zarr.json", "a/zarr.json", "a/b/zarr.json", "a/b/c/zarr.json", "a/b/c/c/0",
                              "loose", "v2/.zarray", "v2/0"}
    assert {"zarr.json", "a/zarr.json"} <= n.read_at_open
    flat = ReferenceStore({"k": b"v"}, bounds=[Key("k")])
    assert values(normalize(flat)) == {"k": b"v"}
    assert normalize(flat).bounds == (Key("k"),)


def test_a_stack_of_zarrified_tiffs_reads_as_one_array(tmp_path):
    for z in range(3):
        tifffile.imwrite(tmp_path / f"z{z}.tif", np.full((40, 30), z + 1, np.uint16), tile=(16, 16), compression="zlib")
    stores = [zarrify(str(tmp_path / f"z{z}.tif")) for z in range(3)]
    doc = stores[0].mapping["0/zarr.json"]
    assert isinstance(doc, bytes)
    meta = json.loads(doc)
    meta.update(shape=[3, *meta["shape"]], dimension_names=["z", *meta["dimension_names"]])
    meta["chunk_grid"]["configuration"]["chunk_shape"] = [1, *meta["chunk_grid"]["configuration"]["chunk_shape"]]
    header = ReferenceStore({"zarr.json": GROUP, "0/zarr.json": json.dumps(meta).encode()},
                            bounds=[Key("zarr.json"), Key("0/zarr.json")])
    stack = union(header, *(shift_chunks(s, "0", (i, 0, 0), new_axes=1) for i, s in enumerate(stores)))
    assert len(stack.sources) == 3
    stack.save(tmp_path / "stack.vzip", relative_to=f"{tmp_path.as_uri()}/")
    data = np.asarray(zarr.open_group(refkv.zarr.open(str(tmp_path / "stack.vzip")), mode="r").get_array("0")[...])
    assert data.shape == (3, 40, 30)
    assert [int(data[z].min()) for z in range(3)] == [1, 2, 3]


def test_overlay_is_associative_and_union_commutative_on_random_stores():
    rng = random.Random(7)
    keys = ["a", "a/b", "a/c", "b", "b/0", "c"]

    def store():
        held = {k: bytes([rng.randrange(3)]) for k in rng.sample(keys, rng.randrange(len(keys) + 1))}
        bounds = rng.choice([None, [Prefix(rng.choice(["", "a", "b"]))],
                             [KeyRange(rng.choice(keys), rng.choice([None, "c"]))],
                             [Key(k) for k in rng.sample(keys, 2)]])
        return ReferenceStore(held, bounds=bounds)

    for _ in range(300):
        a, b, c = store(), store(), store()
        assert values(overlay(overlay(a, b), c)) == values(overlay(a, overlay(b, c))) == values(overlay(a, b, c))
        assert values(overlay(a, a)) == values(overlay(a))
        try:
            ab = union(a, b)
        except ValueError:
            with pytest.raises(ValueError, match="store"):
                union(b, a)
            continue
        assert values(ab) == values(union(b, a))


def test_union_refuses_two_values_for_one_key():
    with pytest.raises(ValueError, match="'k': stores 0 and 1 hold different values"):
        union(ReferenceStore({"k": b"a"}), ReferenceStore({"k": b"b"}))


def test_union_refuses_a_key_one_store_holds_and_another_bounds_as_absent():
    with pytest.raises(ValueError, match="'k': store 0 holds it, but store 1, whose bounds contain it too, does not"):
        union(ReferenceStore({"k": b"a"}), ReferenceStore({}, bounds=[Prefix("")]))


def test_combining_refuses_an_internal_reference_to_a_key_another_store_answers():
    e = ReferenceStore({"hdr": b"HEAD", "v": Range(source=0, length=2)}, [Source(key="hdr")])
    with pytest.raises(ValueError, match="store 0 has a reference to its own key 'hdr'"):
        overlay(e, ReferenceStore({"hdr": b"other"}))


def test_combining_refuses_local_and_remote_bases_without_a_url():
    with pytest.raises(ValueError, match="local and remote base URLs; pass url="):
        overlay(ReferenceStore({"a": b"a"}), ReferenceStore({"b": b"b"}, url="https://h.test/"))


def test_combining_refuses_an_operand_that_is_not_a_reference_store():
    with pytest.raises(TypeError, match="the operands are ReferenceStores, not dict"):
        overlay(ReferenceStore({}), {"k": b"v"})  # pyrefly: ignore[bad-argument-type]


def test_map_keys_refuses_two_keys_renamed_to_one():
    with pytest.raises(ValueError, match="'a' and 'b' are both renamed to 'x'"):
        map_keys(ReferenceStore({"a": b"1", "b": b"2"}), lambda _: "x")


def test_map_keys_refuses_a_rename_into_the_reserved_prefix():
    with pytest.raises(ValueError, match="across the reserved prefix"):
        map_keys(ReferenceStore({"a": b"1"}), lambda _: "__vz__/a")


def test_map_keys_refuses_dropping_the_key_of_a_used_key_source():
    s = ReferenceStore({"hdr": b"H", "v": Range(source=0, length=1)}, [Source(key="hdr")])
    with pytest.raises(ValueError, match="a key source names 'hdr', which is dropped"):
        map_keys(s, lambda k: None if k == "hdr" else k)


def test_mount_refuses_a_path_with_slashes_at_its_ends():
    with pytest.raises(ValueError, match="mount path 'a/'"):
        mount(ReferenceStore({"k": b"v"}), "a/")


def test_shift_chunks_refuses_a_path_that_is_not_an_array():
    with pytest.raises(ValueError, match="'g' is a group, not an array"):
        shift_chunks(ReferenceStore({"g/zarr.json": GROUP}), "g", ())


def test_shift_chunks_refuses_an_offset_of_the_wrong_length():
    with pytest.raises(ValueError, match=r"offset \(1,\) has 1 entries; the moved array has 2 axes"):
        shift_chunks(ReferenceStore({"zarr.json": array([4], [2])}), "", (1,), new_axes=1)


def test_shift_chunks_refuses_a_negative_offset():
    with pytest.raises(ValueError, match="entries are integers of 0 or more"):
        shift_chunks(ReferenceStore({"zarr.json": array([4], [2])}), "", (-1,))


def test_bounds_refuse_malformed_arguments():
    with pytest.raises(ValueError, match="stop is before start"):
        KeyRange("b", "a")
    with pytest.raises(ValueError, match="no leading, trailing or doubled"):
        Prefix("a/")
    with pytest.raises(ValueError, match="different lengths"):
        ChunkBox("a", (0,), (1, 1))
    with pytest.raises(ValueError, match="0 <= start <= stop"):
        ChunkBox("a", (2,), (1,))
    with pytest.raises(TypeError, match="not a single Key"):
        ReferenceStore({}, bounds=Key("a"))  # pyrefly: ignore[bad-argument-type]
    with pytest.raises(TypeError, match="a bound is a Key, KeyRange, Prefix or ChunkBox, not str"):
        ReferenceStore({}, bounds=["a"])  # pyrefly: ignore[bad-argument-type]


def test_combining_drops_consolidated_metadata_and_carries_hidden_headers():
    consolidated = json.dumps({"zarr_format": 3, "node_type": "group", "attributes": {},
                               "consolidated_metadata": {"kind": "inline", "metadata": {}}}).encode()
    a = ReferenceStore({"zarr.json": consolidated, "x/zarr.json": array([2], [2]), "x/c/0": b"aa"})
    b = ReferenceStore({"zarr.json": consolidated, "y/zarr.json": array([2], [2]), "y/c/0": b"bb", ".zmetadata": b"{}"})
    out = overlay(a, b)
    assert document(out, "zarr.json") == {"zarr_format": 3, "node_type": "group", "attributes": {}}
    assert ".zmetadata" not in out.mapping
    # union compares what it keeps: root groups that differ only in their listings and formatting agree,
    # and a .zmetadata it drops is not compared
    plain = json.dumps({"zarr_format": 3, "node_type": "group", "attributes": {"unit": "µm"},
                        "consolidated_metadata": None}, indent=2, ensure_ascii=False).encode()
    listed = json.dumps({"zarr_format": 3, "node_type": "group", "attributes": {"unit": "µm"},
                         "consolidated_metadata": {"kind": "inline", "metadata": {"x": {}}}}).encode()
    c = ReferenceStore({"zarr.json": listed, ".zmetadata": b"{}", "x/zarr.json": array([2], [2])},
                       bounds=[Key("zarr.json"), Key(".zmetadata"), Prefix("x")])
    d = ReferenceStore({"zarr.json": plain, "y/zarr.json": array([2], [2])},
                       bounds=[Key("zarr.json"), Key(".zmetadata"), Prefix("y")])
    assert document(union(c, d), "zarr.json")["attributes"] == {"unit": "µm"}
    with pytest.raises(ValueError, match=r"'zarr\.json': stores 0 and 1 hold different values"):
        union(c, ReferenceStore({"zarr.json": GROUP}))
    assert sorted(zarr.open_group(refkv.zarr.store(out), mode="r").array_keys()) == ["x", "y"]
    assert "consolidated_metadata" in document(mount(a, "m"), "m/zarr.json")
    # hidden keys that key sources name are carried, renamed when two stores' differ
    h1 = ReferenceStore({"__vz__/hdr": b"ONE", "p": Range(source=0, length=3)}, [Source(key="__vz__/hdr")])
    h2 = ReferenceStore({"__vz__/hdr": b"TWO", "q": Range(source=0, length=3)}, [Source(key="__vz__/hdr")])
    both = overlay(h1, h2)
    assert both.get("p") == b"ONE"
    assert both.get("q") == b"TWO"
    assert values(overlay(h1, h1)) == values(overlay(h1))
    h3 = ReferenceStore({"__vz__/hdr": b"TWO", "q": Range(source=0, length=3)}, [Source(key="__vz__/hdr")],
                        bounds=[Key("q")])
    shared = union(h1.with_bounds([Key("p")]), h2.with_bounds([Key("q")]), h3)
    forward, backward = union(h1, h2), union(h2, h1)
    assert set(forward.mapping) == set(backward.mapping)
    assert all(forward.get(k) == backward.get(k) for k in ("p", "q"))
    assert len(shared.sources) == 2
    assert shared.get("q") == b"TWO"
    assert overlay(h1).get("p") == b"ONE"
    # shift_chunks renames a key source with the chunk it names, and keeps hidden headers
    s = ReferenceStore({"zarr.json": array([4], [2]), "c/0": b"ab", "c/1": Range(source=0, length=1),
                        "__vz__/h": b"H", "c/2": Range(source=1, length=1)},
                       [Source(key="c/0"), Source(key="__vz__/h")])
    moved = shift_chunks(s, "", (3,))
    assert set(moved.mapping) == {"c/3", "c/4", "c/5", "__vz__/h"}
    assert moved.get("c/4") == b"a"
    assert moved.get("c/5") == b"H"


def test_shift_chunks_refuses_a_chunk_that_reads_through_a_dropped_key():
    s = ReferenceStore({"zarr.json": array([2], [2]), "hdr": b"H", "c/0": Range(source=0, length=1)},
                       [Source(key="hdr")])
    with pytest.raises(ValueError, match="a key source names 'hdr', which is dropped"):
        shift_chunks(s, "", (0,))


def test_combining_refuses_a_hidden_key_source_whose_value_is_a_reference():
    s = ReferenceStore({"__vz__/h": Range(source=1, length=1), "v": Range(source=0, length=1)},
                       [Source(key="__vz__/h"), Source(url="d.bin")])
    with pytest.raises(ValueError, match="names the hidden key '__vz__/h', whose value is a reference"):
        overlay(s)


def test_map_keys_refuses_a_rename_to_something_other_than_a_string():
    with pytest.raises(ValueError, match="'a' is renamed to 1; keys are non-empty strings"):
        map_keys(ReferenceStore({"a": b"1"}), lambda _: 1)  # pyrefly: ignore[bad-argument-type]


def test_map_keys_refuses_a_key_source_naming_dropped_consolidated_metadata():
    s = ReferenceStore({".zmetadata": b"{}", "v": Range(source=0, length=1)}, [Source(key=".zmetadata")])
    with pytest.raises(ValueError, match=r"a key source names '\.zmetadata', which is dropped"):
        map_keys(s, lambda k: k)
