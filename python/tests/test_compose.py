"""Bounds (refkv.bounds) and the operators that combine reference stores by them
(refkv.compose): overlay, union, map_keys, mount, shift_chunks and normalize."""

import hashlib
import itertools
import json
import random
import time

import numpy as np
import pytest
import tifffile
import zarr
from obstore.store import MemoryStore

import refkv.zarr
from refkv import ReferenceStore
from refkv.bounds import Bound, ChunkBox, Key, KeyRange, Prefix, chunk_key, derive, nodes, parse_chunk_key
from refkv.compose import _Cover, map_keys, mount, normalize, overlay, shift_chunks, union
from refkv.errors import ResolutionError
from refkv.pb import Concat, Range, Source
from refkv.policy import Policy
from refkv.zarrify import zarrify

GROUP = b'{"zarr_format":3,"node_type":"group","attributes":{}}'
GROUP_V2 = b'{"zarr_format":2}'


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
    both = {"g/.zarray": b"{}", "g/zarr.json": GROUP}
    assert nodes(both) == nodes(dict(reversed(both.items()))) == {"g": "group"}
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
    # large boxes, indexed by a shallow axis or not at all, bound the same chunks
    slab, wide = ChunkBox("0", (1, 0, 0), (2, 100, 100)), ChunkBox("0", (1, 0, 0), (100, 100, 100))
    for big, left in ((slab, (0, 2)), (wide, (0,))):
        cleared_big = overlay(stack, ReferenceStore({}, bounds=[big]))
        assert sorted(k for k in cleared_big.mapping if k.startswith("0/c/")) == [f"0/c/{z}/{y}/{x}" for z in left
                                                                                  for y in range(2) for x in range(2)]
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


def test_combining_keeps_each_store_from_reading_what_its_own_policy_refuses(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"AAAA")
    remote = ReferenceStore({"b": Range(source=0, length=4)}, [Source(url=(tmp_path / "a.bin").as_uri())],
                            url="https://h.test/archive/")
    for local_url in (f"{tmp_path.as_uri()}/", "FILE:///tmp/"):
        combined = overlay(ReferenceStore({"a": b"a"}, url=local_url), remote)
        assert combined.url == "https://h.test/archive/"
        with pytest.raises(ResolutionError, match="reads a local file only when it is allowed"):
            combined.get("b")
    # no base URL lets a store read a file, the first store's being a local one or not
    web = ReferenceStore({"b": Range(source=0, length=1)}, [Source(url="https://h.test/b.bin")], url="https://h.test/")
    disk = ReferenceStore({"a": Range(source=0, length=4)}, [Source(url="a.bin")], url=f"{tmp_path.as_uri()}/")
    assert overlay(web, disk).url == "https://h.test/"
    assert overlay(disk, web).url == f"{tmp_path.as_uri()}/"
    for combined in (overlay(web, disk), overlay(disk, remote), overlay(remote, disk)):
        with pytest.raises(ResolutionError, match="reads a local file only when it is allowed"):
            combined.get("a")
    # a store that reads nothing outside itself, such as a header, takes no part
    header = ReferenceStore({"h": b"h"}, policy=Policy(allow_private_hosts=True))
    assert overlay(header, web).url == "https://h.test/"
    assert overlay(header, web).policy == web.policy
    loose = Policy(allow_private_hosts=True)
    assert overlay(ReferenceStore({}), ReferenceStore({}, policy=loose), policy=loose).policy == loose


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
    nulls = json.dumps({"zarr_format": 2, "shape": [4], "chunks": [2], "dimension_separator": None}).encode()
    assert set(shift_chunks(ReferenceStore({"v/.zarray": nulls, "v/1": b"x"}), "v", (2,)).mapping) == {"v/3"}
    n = normalize(ReferenceStore({"a/b/zarr.json": GROUP, "a/b/c/zarr.json": array([1], [1]), "a/b/c/c/0": b"0",
                                  "a/b/c/x/zarr.json": GROUP, "a/b/stray": b"s", "loose": b"l",
                                  "v2/.zarray": b"{}", "v2/0": b"0"}))
    # each node's ancestors in its own version; "loose" lies under the root group added
    assert set(n.mapping) == {"zarr.json", "a/zarr.json", "a/b/zarr.json", "a/b/c/zarr.json", "a/b/c/c/0",
                              "v2/.zarray", "v2/0", ".zgroup"}
    assert {"zarr.json", "a/zarr.json"} <= n.read_at_open
    # v3 decides a path v2 and v3 both mark, whatever the key order; a broken ancestor document is replaced
    for order in (1, -1):
        mixed = dict(list({"p/.zarray": b"{}", "p/0": b"0", "p/zarr.json": GROUP, "p/q/zarr.json": array([1], [1]),
                           "a/zarr.json": b"not json", "a/b/zarr.json": GROUP}.items())[::order])
        assert set(normalize(ReferenceStore(mixed)).mapping) == {"zarr.json", "p/zarr.json", "p/q/zarr.json",
                                                                "a/zarr.json", "a/b/zarr.json"}
        assert normalize(ReferenceStore(mixed)).mapping["a/zarr.json"] == GROUP
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
    policy = Policy(prefixes=(f"{tmp_path.as_uri()}/",))
    zs = refkv.zarr.open(str(tmp_path / "stack.vzip"), policy=policy)
    data = np.asarray(zarr.open_group(zs, mode="r").get_array("0")[...])
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


def test_combining_refuses_stores_with_different_policies():
    src = [Source(url="https://h.test/x.bin")]
    with pytest.raises(ValueError, match="different reader policies; pass policy="):
        overlay(ReferenceStore({"a": Range(source=0, length=1)}, src),
                ReferenceStore({"b": Range(source=0, length=1)}, src, policy=Policy(allow_private_hosts=True)))


def test_combining_refuses_stores_with_different_registries():
    src = [Source(url="https://h.test/x.bin")]
    with pytest.raises(ValueError, match="different registries; pass registry="):
        overlay(ReferenceStore({"a": Range(source=0, length=1)}, src),
                ReferenceStore({"b": Range(source=0, length=1)}, src, registry={"s3://bucket": MemoryStore()}))


def test_combining_refuses_a_reference_to_a_hidden_key_its_store_lacks():
    a = ReferenceStore({"__vz__/h": b"SECRET", "va": Range(source=0, length=6)}, [Source(key="__vz__/h")])
    b = ReferenceStore({"vb": Range(source=0, length=6)}, [Source(key="__vz__/h")])
    with pytest.raises(ValueError, match="store 1 has a reference to its hidden key '__vz__/h', which it does not"):
        overlay(a, b)


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
    with pytest.raises(ValueError, match="a key source names 'hdr', which the result does not hold"):
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
    flag = json.dumps({"zarr_format": 3, "node_type": "group", "attributes": {"flag": True}}).encode()
    with pytest.raises(ValueError, match=r"'zarr\.json': stores 0 and 1 hold different values"):
        union(ReferenceStore({"zarr.json": flag}), ReferenceStore({"zarr.json": flag.replace(b"true", b"1")}))
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
    # a header a combination renamed keeps its name when combined again
    nested = union(overlay(h1, h2), h1)
    assert nested.get("p") == b"ONE"
    assert len(nested.sources) == 2
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
    with pytest.raises(ValueError, match="a key source names 'hdr', which the result does not hold"):
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
    with pytest.raises(ValueError, match=r"a key source names '\.zmetadata', which the result does not hold"):
        map_keys(s, lambda k: k)


def test_the_bounds_index_finds_the_bounds_that_contain_each_key():
    rng = random.Random(11)
    chunks = [f"0/c/{z}/{y}" for z in (0, 1, 70, 5000, 900000) for y in (0, 3)]
    keys = ["", "a", "a/b", "a/b/c", "a0", "b", "c", *chunks]
    for _ in range(400):
        sets = []
        for i in range(rng.randrange(1, 6)):
            bounds: list[Bound] = []
            for _ in range(rng.randrange(1, 4)):
                kind = rng.randrange(4)
                if kind == 0:
                    bounds.append(Key(rng.choice([k for k in keys if k])))
                elif kind == 1:
                    s = rng.choice(keys)
                    bounds.append(KeyRange(s, rng.choice([None, s + "z"])))
                elif kind == 2:
                    bounds.append(Prefix(rng.choice(["", "a", "a/b", "0", "0/c/1"])))
                else:  # boxes small, deep on one axis, and wide on every axis
                    lo = (rng.choice([0, 1, 70]), rng.choice([0, 3]))
                    depth = rng.choice([(1, 4), (6000, 1), (10**6, 10**6), (0, 2)])
                    bounds.append(ChunkBox("0", lo, (lo[0] + depth[0], lo[1] + depth[1])))
            sets.append((i, bounds))
        cover = _Cover(sets)
        for k in keys:
            assert cover.which(k) == {i for i, bs in sets if any(b.contains(k) for b in bs)}, (sets, k)


def test_operators_keep_bounds_hidden_flags_and_meaning_through_every_step(tmp_path):
    # normalize: an array owns everything below it, a stray node document included
    chunks = {f"a/c/{y}/{x}": b"x" * 4 for y in range(2) for x in range(2)}
    stray = overlay(ReferenceStore({"zarr.json": GROUP, "a/zarr.json": array([4, 4], [2, 2]), **chunks}),
                    ReferenceStore({"a/c/zarr.json": GROUP}, bounds=[Key("a/c/zarr.json")]))
    assert set(normalize(stray).mapping) == {"zarr.json", "a/zarr.json", *chunks}
    # stated bounds go through shift_chunks and map_keys, or they refuse
    patch = ReferenceStore({"0/zarr.json": array([4], [1]), **{f"0/c/{i}": b"P" for i in range(4)}},
                           bounds=[ChunkBox("0", (1,), (2,))])
    shifted = shift_chunks(patch, "0", (0,))
    assert set(shifted.mapping) == {"0/c/1"}
    assert shifted.bounds == (ChunkBox("0", (1,), (2,)),)
    assert shift_chunks(patch, "0", (2, 0), new_axes=1).bounds == (ChunkBox("0", (2, 1), (3, 2)),)
    keyed = ReferenceStore({"k": b"v", "j": b"w"}, bounds=[Key("k")])
    assert map_keys(keyed, lambda k: "x" + k).bounds == (Key("xk"),)
    # url= on one store keeps every source reading the same object
    (tmp_path / "d.bin").write_bytes(b"DATA")
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "d.bin").write_bytes(b"ELSE")
    rel = ReferenceStore({"v": Range(source=0, length=4)}, [Source(url="d.bin")], url=f"{tmp_path.as_uri()}/",
                         policy=Policy(prefixes=(f"{tmp_path.as_uri()}/",)))
    assert mount(rel, "m", url=f"{(tmp_path / 'other').as_uri()}/").get("m/v") == b"DATA"
    assert map_keys(rel, lambda k: k, url=f"{(tmp_path / 'other').as_uri()}/").get("v") == b"DATA"
    # a store with nothing in it takes no part in the result's options
    remote = ReferenceStore({"r": b"r"}, url="https://h.test/a/", page_size=4096)
    assert (overlay(ReferenceStore({}), remote).url, overlay(ReferenceStore({}), remote).page_size) == (
        "https://h.test/a/", 4096)
    # stores agreeing on a key their own references read through combine in either order
    s1 = ReferenceStore({"hdr": b"H", "v": Range(source=0, length=1)}, [Source(key="hdr")])
    s2 = ReferenceStore({"hdr": b"H"})
    assert union(s1, s2).get("v") == union(s2, s1).get("v") == b"H"
    # hidden keys keep their flags; a store without nodes keeps every key
    h = ReferenceStore({"__vz__/h": b"H" * 50, "v": Range(source=0, length=1)}, [Source(key="__vz__/h")],
                       compressed=["__vz__/h"])
    assert "__vz__/h" in overlay(h).compressed
    assert set(normalize(ReferenceStore({".zmetadata": b"{}", "k": b"v"})).mapping) == {".zmetadata", "k"}
    # malformed and deeply nested documents mark no node
    odd = ReferenceStore({"a//zarr.json": array([1], [1]), "/zarr.json": array([1], [1]),
                          "deep/zarr.json": b"[" * 100000, "other": b"o"})
    assert set(odd.bounds) == {Key("a//zarr.json"), Key("/zarr.json"), Key("deep/zarr.json"), Key("other")}
    assert overlay(odd).mapping.keys() == odd.mapping.keys()
    # numpy integers are integers
    assert shift_chunks(ReferenceStore({"zarr.json": array([4], [2]), "c/1": b"x"}), "", (np.int64(1),)).bounds == (
        ChunkBox("", (1,), (3,)),)
    assert ChunkBox("a", (np.int64(0),), (np.int32(2),)).stop == (2,)  # pyrefly: ignore[bad-argument-type]


def test_union_compares_a_deeply_nested_document_as_json():
    deep = b'{"zarr_format":3,"node_type":"group","attributes":{"x":' + b"[" * 500 + b"]" * 500 + b"}}"
    spaced = deep.replace(b'"attributes":', b'"attributes" : ').replace(b"{", b"{ ")
    both = union(ReferenceStore({"zarr.json": deep}), ReferenceStore({"zarr.json": spaced}))
    assert both.mapping["zarr.json"] == spaced
    with pytest.raises(ValueError, match="hold different values"):
        union(ReferenceStore({"zarr.json": deep}), ReferenceStore({"zarr.json": deep.replace(b"[]", b"[1]")}))
    # nested past DEEPEST, on any interpreter, documents are compared as bytes
    deeper = [d.replace(b"[" * 500, b"[" * 600).replace(b"]" * 500, b"]" * 600) for d in (deep, spaced)]
    with pytest.raises(ValueError, match="hold different values"):
        union(*(ReferenceStore({"zarr.json": d}) for d in deeper))


def test_union_refuses_a_number_against_an_object_that_spells_it():
    docs = [json.dumps({"zarr_format": 3, "node_type": "group", "attributes": {"x": x}}).encode()
            for x in (1.5, {"$number": "1.5"})]
    with pytest.raises(ValueError, match="hold different values"):
        union(ReferenceStore({"zarr.json": docs[0]}), ReferenceStore({"zarr.json": docs[1]}))


def test_combining_refuses_a_reference_into_a_document_another_store_writes_differently():
    pretty = json.dumps({"zarr_format": 3, "node_type": "group", "attributes": {}}, indent=2).encode()
    compact = json.dumps({"zarr_format": 3, "node_type": "group", "attributes": {}}).encode()
    i = ReferenceStore({"zarr.json": pretty, "v": Range(source=0, offset=10, length=5)}, [Source(key="zarr.json")])
    with pytest.raises(ValueError, match=r"store 0 has a reference to its own key 'zarr\.json'"):
        overlay(i, ReferenceStore({"zarr.json": compact}))


def test_union_refuses_numbers_that_differ_beyond_float_range():
    big = [json.dumps({"zarr_format": 3, "node_type": "group", "attributes": {}}).encode().replace(
        b'"attributes": {}', f'"attributes": {{"x": {n}}}'.encode()) for n in ("1e400", "2e400")]
    with pytest.raises(ValueError, match="hold different values"):
        union(ReferenceStore({"zarr.json": big[0]}), ReferenceStore({"zarr.json": big[1]}))


def test_map_keys_refuses_to_guess_how_a_stated_box_moves():
    with pytest.raises(ValueError, match="map_keys cannot move the bound"):
        map_keys(ReferenceStore({}, bounds=[ChunkBox("0", (2,), (3,))]), lambda k: k)


def test_shift_chunks_refuses_a_stated_key_range_over_the_array():
    s = ReferenceStore({"0/zarr.json": array([4], [1]), "0/c/1": b"x"}, bounds=[KeyRange("0/c/1", "0/c/2")])
    with pytest.raises(ValueError, match="shift_chunks cannot move the bound"):
        shift_chunks(s, "0", (0,))


def test_shift_chunks_refuses_malformed_array_metadata():
    for doc in ({".zarray": b'{"shape": [4]}'}, {".zarray": b"\x80not utf-8"}):
        with pytest.raises(ValueError, match="malformed array metadata"):
            shift_chunks(ReferenceStore(doc), "", (0,))


def test_combining_refuses_hidden_names_that_collide():
    digest = hashlib.sha256(b"B1").hexdigest()[:16]
    crafted = f"__vz__/h~{digest}"
    a = ReferenceStore({crafted: b"EVIL", "a": Range(source=0, length=4)}, [Source(key=crafted)])
    b = ReferenceStore({"__vz__/h": b"B1", "b": Range(source=0, length=2)}, [Source(key="__vz__/h")])
    c = ReferenceStore({"__vz__/h": b"C1", "c": Range(source=0, length=2)}, [Source(key="__vz__/h")])
    with pytest.raises(ValueError, match="two different hidden values come to the name"):
        union(b, c, a)


def test_combining_refuses_a_reference_into_a_document_it_rewrites():
    listed = json.dumps({"zarr_format": 3, "node_type": "group", "attributes": {},
                         "consolidated_metadata": {"kind": "inline", "metadata": {}}}).encode()
    s = ReferenceStore({"zarr.json": listed, "v": Range(source=0, length=5)}, [Source(key="zarr.json")])
    with pytest.raises(ValueError, match="whose value the result rewrites"):
        overlay(s)


def test_combinations_read_what_each_store_reads_at_any_scale():
    # union compares a store's own key sources by what they read
    a = ReferenceStore({"k": b"AAAA", "X": Range(source=0, length=4)}, [Source(key="k")], bounds=[Key("k"), Key("X")])
    b = ReferenceStore({"k": b"BBBB", "X": Range(source=0, length=4)}, [Source(key="k")], bounds=[Key("X")])
    for order in ((a, b), (b, a)):
        with pytest.raises(ValueError, match="'X': stores 0 and 1 hold different values"):
            union(*order)
    # a v2 box takes the v2 separator; one that matches no chunk of a described array is refused
    v2 = json.dumps({"zarr_format": 2, "shape": [2, 2], "chunks": [1, 1], "dtype": "|u1", "compressor": None,
                     "fill_value": 0, "order": "C", "filters": None}).encode()
    s = ReferenceStore({".zgroup": GROUP_V2, "a/.zarray": v2, **{f"a/{y}.{x}": bytes([y * 2 + x + 1])
                                                                 for y in range(2) for x in range(2)}})
    blank = ReferenceStore({}, bounds=[ChunkBox("a", (0, 0), (1, 2), encoding="v2")])
    assert sorted(k for k in overlay(s, blank).mapping if k[2:3].isdigit()) == ["a/1.0", "a/1.1"]
    # v2 documents compare as JSON
    assert union(ReferenceStore({".zgroup": GROUP_V2}), ReferenceStore({".zgroup": b'{\n  "zarr_format": 2\n}'}))
    # dropping consolidated metadata keeps every other number and string as written
    odd = (b'{"zarr_format":3,"node_type":"group","attributes":{"x":1E400,"y":0.30000000000000000000001,'
           b'"s":"\\ud800","u":"\\u00b5"},"consolidated_metadata":{"kind":"inline","metadata":{}}}')
    kept = overlay(ReferenceStore({"zarr.json": odd})).mapping["zarr.json"]
    assert kept == (b'{"zarr_format":3,"node_type":"group","attributes":{"x":1E400,"y":0.30000000000000000000001,'
                    b'"s":"\\ud800","u":"\\u00b5"}}')
    # a box to the end of a huge array, and a Prefix beside an array's chunks, are fine
    huge = ReferenceStore({"0/c/5/5": b"x"}, bounds=[ChunkBox("0", (0, 0), (10**30, 10**30))])
    assert overlay(huge).mapping["0/c/5/5"] == b"x"
    beside = ReferenceStore({"0/zarr.json": array([4], [1]), "0/c/1": b"x", "0/attrs/a": b"a"},
                            bounds=[Prefix("0/attrs"), ChunkBox("0", (0,), (4,))])
    assert set(shift_chunks(beside, "0", (1,)).mapping) == {"0/c/2"}


def test_one_overlay_of_many_overlapping_layers_takes_linear_time():
    layers = [ReferenceStore({f"0/c/{k}": bytes([i % 256]) for k in range(200)}, bounds=[Prefix("0")])
              for i in range(1000)]
    patches = [ReferenceStore({}, bounds=[ChunkBox("0", (k,), (k + 1,))]) for k in range(1001)]
    start = time.perf_counter()
    top = overlay(*layers)
    blanked = overlay(*layers, *patches)
    assert top.mapping["0/c/7"] == bytes([999 % 256])
    assert not blanked.mapping
    assert time.perf_counter() - start < 30


def test_combining_refuses_a_box_that_takes_in_no_chunk_of_its_array():
    described = ReferenceStore({"a/zarr.json": array([4, 4], [2, 2])})
    with pytest.raises(ValueError, match="takes in no chunk of array 'a'"):
        overlay(described, ReferenceStore({}, bounds=[ChunkBox("a", (0, 0), (1, 1), separator=".")]))


def test_mount_keeps_derived_bounds_derived_and_normalize_is_idempotent():
    s = ReferenceStore({"zarr.json": GROUP, "0/zarr.json": array([2], [1]), "0/c/0": b"x"})
    mounted = mount(s, "m")
    assert not mounted.bounds_stated
    assert set(map_keys(mounted, lambda k: "n/" + k).mapping) == {"n/m/zarr.json", "n/m/0/zarr.json", "n/m/0/c/0"}
    mixed = ReferenceStore({"a/b/zarr.json": GROUP, "v2/x/.zarray": b"{}", "v2/x/0": b"0"})
    once = normalize(mixed)
    assert values(normalize(once)) == values(once)
    for stray in ({"a/zarr.json": GROUP, "stray": b"s"}, {"zarr.json": b"junk", "a/.zarray": b"{}", "a/0": b"7"}):
        first = normalize(ReferenceStore(stray))
        assert values(normalize(first)) == values(first)
        assert "zarr.json" not in first.mapping or first.mapping["zarr.json"] != b"junk"
    # a store whose bounds keep a key outside them out is not renamed into them
    with pytest.raises(ValueError, match="outside the store's bounds, is renamed to 'public', inside them"):
        map_keys(ReferenceStore({"secret": b"x"}, bounds=[Key("public")]), lambda _: "public")


def test_a_later_store_may_replace_a_zarr_document_held_as_a_reference():
    held = ReferenceStore({"zarr.json": Range(source=0, length=10)}, [Source(url="https://h.test/zarr.json")],
                          url="https://h.test/")
    header = ReferenceStore({"zarr.json": GROUP}, bounds=[Key("zarr.json")])
    assert overlay(held, header).mapping["zarr.json"] == GROUP


def test_combining_refuses_a_zarr_document_held_as_a_reference():
    s = ReferenceStore({"zarr.json": Range(source=0, length=10)}, [Source(url="https://h.test/zarr.json")],
                       url="https://h.test/")
    with pytest.raises(ValueError, match="held as a reference, so its consolidated metadata"):
        overlay(s)


def test_combining_refuses_a_document_it_cannot_parse_to_drop_its_listing():
    deep = b'{"consolidated_metadata":' + b"[" * 100000 + b"]" * 100000 + b"}"
    with pytest.raises(ValueError, match="not strict JSON"):
        overlay(ReferenceStore({"zarr.json": deep}))


def test_union_agrees_on_values_that_read_the_same_bytes():
    url = [Source(url="https://h.test/d.bin")]
    pairs = [
        (ReferenceStore({"k": b"abc"}), ReferenceStore({"k": Range(data=b"abc")})),
        (ReferenceStore({"k": Range(source=0, offset=2, length=4)}, url, url="https://h.test/"),
         ReferenceStore({"k": Concat((Range(source=0, offset=2, length=1), Range(source=0, offset=3, length=3)))},
                        url, url="https://h.test/")),
        (ReferenceStore({"k": b"abc"}), ReferenceStore({"k": Range(source=0, offset=1, length=3)},
                                                       [Source(data=b"xabcx")])),
    ]
    pairs.append((ReferenceStore({"k": b"abc"}), ReferenceStore(
        {"k": Concat((Range(data=b"abc"), Range(source=0, offset=0, length=0, crc32c=5)))}, [Source(data=b"x")])))
    attrs = b'{"a": 1}'
    pairs.append((ReferenceStore({"g/.zattrs": attrs}), ReferenceStore({"g/.zattrs": Range(data=b'{ "a" : 1 }')})))
    for a, b in pairs:  # each pair unites in either order (the second pair's URL is not read)
        assert set(union(a, b).mapping) == set(union(b, a).mapping) == set(a.mapping)
    assert union(*pairs[2]).get("k") == b"abc"


@pytest.mark.filterwarnings("ignore:Both zarr.json:UserWarning")  # a mixed hierarchy, on purpose
def test_normalize_reaches_every_node_through_groups_of_its_version():
    a3 = array([2], [1])
    store = ReferenceStore({"a/.zgroup": GROUP_V2, "a/b/zarr.json": a3, "a/b/c/0": b"\x07"})
    n = normalize(store)
    root = zarr.open_group(refkv.zarr.store(n), mode="r")
    a = root["a"]
    assert isinstance(a, zarr.Group)
    b = a["b"]
    assert isinstance(b, zarr.Array)
    assert np.asarray(b[...]).tolist() == [7, 0]
    clash = normalize(ReferenceStore({"zarr.json": GROUP, ".zarray": b"{}", "a/.zgroup": GROUP_V2}))
    assert values(normalize(clash)) == values(clash)
    assert values(normalize(n)) == values(n)


def test_normalize_refuses_a_zarr_document_held_as_a_reference():
    s = ReferenceStore({"zarr.json": GROUP, "a/zarr.json": Range(source=0, length=10)},
                       [Source(url="https://h.test/a.json")], url="https://h.test/")
    with pytest.raises(ValueError, match="normalize cannot tell what node"):
        normalize(s)


def test_union_refuses_a_value_that_would_not_read_against_one_that_does():
    for broken in (Range(source=0, offset=1, length=9), Range(source=0, offset=1, length=3, crc32c=1)):
        with pytest.raises(ValueError, match="hold different values"):
            union(ReferenceStore({"k": b"abc"}), ReferenceStore({"k": broken}, [Source(data=b"xabcx")]))


def test_combining_refuses_a_key_source_target_another_store_holds_as_a_reference():
    i = ReferenceStore({"t": b"HELLO", "v": Range(source=0, length=5)}, [Source(key="t")])
    j = ReferenceStore({"t": Range(source=0, length=5)}, [Source(data=b"HELLO")])
    for order in ((i, j), (j, i)):
        with pytest.raises(ValueError, match="'t': stores 0 and 1 hold different values"):
            union(*order)


def test_options_of_none_ask_for_the_default_the_stores_decide(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"AAAA")
    remote = ReferenceStore({"b": Range(source=0, length=4)}, [Source(url=(tmp_path / "a.bin").as_uri())],
                            url="https://h.test/archive/")
    strict = Policy(prefixes=("https://only.test/",))
    for combined in (overlay(remote, url=None), mount(remote, "m", url=None),
                     overlay(remote.with_bounds(None), policy=None)):
        assert combined.url == "https://h.test/archive/"
    assert overlay(ReferenceStore({"k": b"v"}, policy=strict), policy=None).policy == strict
    crafted = "__vz__/sources~" + hashlib.sha256(b"H").hexdigest()[:16]
    hidden = ReferenceStore({crafted: b"H", "v": Range(source=0, length=1)}, [Source(key=crafted)])
    assert overlay(hidden).get("v") == b"H"


def test_union_compares_a_long_concat_in_linear_time():
    parts = tuple(Range(source=0, offset=512 * k, length=512) for k in range(40000))
    data = [Source(data=bytes(512 * 40000))]
    start = time.perf_counter()
    union(ReferenceStore({"k": Concat(parts)}, data), ReferenceStore({"k": Concat(parts)}, data))
    assert time.perf_counter() - start < 10


def test_the_bounds_index_finds_boxes_of_many_shapes_and_ranks():
    rng = random.Random(4)
    for rank, count in ((3, 300), (6, 80)):
        sets = []
        for i in range(count):
            start = tuple(rng.randrange(0, 50) for _ in range(rank))
            size = tuple(rng.choice([1, 3, 9, 30, 100, 1000]) for _ in range(rank))
            sets.append((i, [ChunkBox("0", start, tuple(a + s for a, s in zip(start, size, strict=True)))]))
        cover = _Cover(sets)
        for _ in range(300):
            key = "0/c/" + "/".join(str(rng.randrange(0, 200)) for _ in range(rank))
            want = {i for i, bs in sets if bs[0].contains(key)}
            assert cover.which(key) == want
            assert cover.owner(key) == max(want, default=-1)


def test_a_mosaic_beside_odd_boxes_and_documents_compared_as_held():
    tiles = [(i, [ChunkBox("a", (x * 5, y * 5, z * 5), (x * 5 + 5, y * 5 + 5, z * 5 + 5))])
             for i, (x, y, z) in enumerate(itertools.product(range(8), repeat=3))]
    shapes = list(itertools.product([1, 20, 150, 1200], repeat=3))[:40]
    odd = [(len(tiles) + k, [ChunkBox("a", (1000,) * 3, (1000 + a, 1000 + b, 1000 + c))])
           for k, (a, b, c) in enumerate(shapes)]
    cover = _Cover(tiles + odd)
    assert cover.owner("a/c/7/12/39") == 1 * 64 + 2 * 8 + 7
    assert cover.which("a/c/1000/1000/1000") == {len(tiles) + k for k in range(len(odd))}
    listed = (b'{"zarr_format":3,"node_type":"group","attributes":{},'
              b'"consolidated_metadata":{"kind":"inline","metadata":{}}}')
    reader = ReferenceStore({"zarr.json": listed, "v": Range(source=0, length=5)}, [Source(key="zarr.json")])
    holder = ReferenceStore({"zarr.json": listed, "v": listed[:5]})
    assert union(reader, holder).get("v") == listed[:5]
    stray = ReferenceStore({"a/.zarray": json.dumps({"zarr_format": 2, "shape": [2], "chunks": [1]}).encode(),
                            "a/0": b"x", "a/zarr.json": b"not json"})
    assert set(shift_chunks(stray, "a", (1,)).mapping) == {"a/1"}


def test_union_compares_only_strict_json_as_documents():
    doubled = b'{"zarr_format":3,"node_type":"group","attributes":{"a":1,"a":2}}'
    single = b'{"zarr_format":3,"node_type":"group","attributes":{"a":2}}'
    bom = b"\xef\xbb\xbf" + single
    for other in (doubled, bom):
        with pytest.raises(ValueError, match="hold different values"):
            union(ReferenceStore({"zarr.json": single}), ReferenceStore({"zarr.json": other}))


def test_map_keys_and_shift_chunks_take_repeated_bounds_and_one_axis_v2_boxes():
    twice = union(ReferenceStore({"zarr.json": GROUP}, bounds=[Key("zarr.json")]),
                  ReferenceStore({"zarr.json": GROUP}, bounds=[Key("zarr.json")]))
    assert map_keys(twice, lambda k: "m/" + k).bounds == (Key("m/zarr.json"),)
    zarray = json.dumps({"zarr_format": 2, "shape": [4], "chunks": [1], "dimension_separator": "/"}).encode()
    s = ReferenceStore({"a/.zarray": zarray, "a/1": b"x"}, bounds=[ChunkBox("a", (1,), (2,), "v2")])
    assert set(shift_chunks(s, "a", (2,)).mapping) == {"a/3"}


def test_map_keys_refuses_two_bounds_renamed_to_one():
    with pytest.raises(ValueError, match="two of the store's Key bounds are renamed to one key"):
        map_keys(ReferenceStore({"x": b"1"}, bounds=[Key("x"), Key("y")]), lambda _: "z")


def test_a_v2_box_of_one_axis_matches_either_separator():
    zarray = json.dumps({"zarr_format": 2, "shape": [4], "chunks": [1], "dimension_separator": "/"}).encode()
    s = ReferenceStore({"a/.zarray": zarray, "a/1": b"x", "a/2": b"y"})
    blank = ReferenceStore({}, bounds=[ChunkBox("a", (1,), (2,), "v2")])
    assert set(overlay(s, blank).mapping) == {"a/.zarray", "a/2"}


def test_lenient_listings_deep_keys_rank_zero_boxes_and_huge_indexes():
    # a listing a lenient reader would find, in a document that is not strict JSON, is refused
    listed = json.dumps({"zarr_format": 3, "node_type": "group", "attributes": {},
                         "consolidated_metadata": {"kind": "inline", "metadata": {}}})
    first_listed = listed[:-1] + ', "consolidated_metadata": null}'
    for doc in (listed.encode("utf-16"), first_listed.encode()):
        with pytest.raises(ValueError, match="not strict JSON"):
            overlay(ReferenceStore({"zarr.json": doc}))
    escaped = listed.replace('"consolidated_metadata"', '"consolidated_\\u006detadata"').encode()
    assert "consolidated" not in document(overlay(ReferenceStore({"zarr.json": escaped})), "zarr.json")
    # a deep key costs memory for one path at a time
    deep = ReferenceStore({"a/" * 20000 + "x": b"x", "zarr.json": GROUP})
    assert len(deep.bounds) == 2
    # a rank-0 box matches whatever the array's separator
    scalar = json.dumps({"zarr_format": 3, "node_type": "array", "shape": [], "data_type": "uint8",
                         "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": []}},
                         "chunk_key_encoding": {"name": "default", "configuration": {"separator": "."}},
                         "fill_value": 0, "codecs": [{"name": "bytes"}]}).encode()
    s = ReferenceStore({"a/zarr.json": scalar, "a/c": b"\x07"})
    assert "a/c" not in overlay(s, ReferenceStore({}, bounds=[ChunkBox("a", (), ())])).mapping
    # an index too long to be a chunk's is no chunk key
    junk = ReferenceStore({"a/c/0": b"x", "a/c/" + "1" * 5000: b"junk"})
    assert "a/c/0" not in overlay(junk, ReferenceStore({}, bounds=[ChunkBox("a", (0,), (1,))])).mapping


def test_combining_refuses_to_let_a_store_read_a_file_only_another_policy_allows(tmp_path):
    (tmp_path / "own.bin").write_bytes(b"OWN!")
    (tmp_path / "secret.bin").write_bytes(b"SECRET")
    allowed = Policy(schemes=frozenset({"file"}))
    own = ReferenceStore({"a": Range(source=0, length=4)}, [Source(url=(tmp_path / "own.bin").as_uri())],
                         policy=allowed)
    other = ReferenceStore({"b": Range(source=0, length=6)}, [Source(url=(tmp_path / "secret.bin").as_uri())],
                           url="https://h.test/archive/")
    with pytest.raises(ValueError, match="different reader policies"):
        overlay(own, other)
    assert overlay(own, other, policy=allowed).get("b") == b"SECRET"
