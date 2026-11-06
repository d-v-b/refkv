"""Converting a saved store from one binding to the other: `refkv.vzip_to_phkv`,
`refkv.phkv_to_vzip` and `ReferenceStore.from_store`."""

import pytest
from test_phkv import DATA, N, _everything, _mapping

import refkv
from refkv.errors import ArchiveError
from refkv.pb import Concat, Range, Source
from refkv.policy import Policy

LOCAL = Policy()


def _source_store(tmp_path) -> refkv.ReferenceStore:
    (tmp_path / "blob.bin").write_bytes(DATA)
    return refkv.ReferenceStore(_mapping(), [Source(url=(tmp_path / "blob.bin").as_uri()), Source(data=b"0123456789")])


def test_vzip_to_phkv_keeps_every_value(tmp_path):
    store = _source_store(tmp_path)
    store.save_vzip(tmp_path / "a.vzip")
    summary = refkv.vzip_to_phkv(tmp_path / "a.vzip", tmp_path / "a.phkv")
    out = refkv.open(tmp_path / "a.phkv", binding="phkv")
    assert _everything(out) == _everything(store)
    assert (summary.keys, summary.references, summary.sources) == (len(store.list()), N + 1, 2)


def test_phkv_to_vzip_keeps_every_value(tmp_path):
    store = _source_store(tmp_path)
    store.save_phkv(tmp_path / "a.phkv")
    summary = refkv.phkv_to_vzip(tmp_path / "a.phkv", tmp_path / "a.vzip", page_size=4096)
    assert _everything(refkv.open(tmp_path / "a.vzip")) == _everything(store)
    assert (summary.keys, summary.references, summary.sources) == (len(store.list()), N + 1, 2)


def test_round_trip_is_the_same_store(tmp_path):
    store = _source_store(tmp_path)
    store.save_vzip(tmp_path / "a.vzip")
    refkv.vzip_to_phkv(tmp_path / "a.vzip", tmp_path / "b.phkv")
    refkv.phkv_to_vzip(tmp_path / "b.phkv", tmp_path / "c.vzip")
    a, c = (refkv.ReferenceStore.from_store(refkv.open(tmp_path / f"{n}.vzip")) for n in "ac")
    assert (dict(c.mapping), c.sources) == (dict(a.mapping), a.sources)  # not `==`: the URLs differ


def test_references_are_not_resolved(tmp_path):
    """The sources are not read: converting a store whose source is gone works."""
    store = refkv.ReferenceStore({"k": Range(source=0, offset=0, length=4)}, [Source(url=(tmp_path / "gone").as_uri())])
    store.save_vzip(tmp_path / "a.vzip")
    refkv.vzip_to_phkv(tmp_path / "a.vzip", tmp_path / "a.phkv")
    out = refkv.open(tmp_path / "a.phkv", binding="phkv")
    assert out.classify("k") == "reference"
    assert out.value("k") == Range(source=0, offset=0, length=4)


def test_relative_sources_stay_correct_in_another_directory(tmp_path):
    """A relative source URL is made absolute against the store it was in, so the converted store,
    saved elsewhere, reads the same bytes."""
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()
    (tmp_path / "in" / "blob.bin").write_bytes(DATA)
    refkv.ReferenceStore({"k": Range(source=0, offset=5, length=7)}, [Source(url="blob.bin")],
                         url=(tmp_path / "in" / "a.vzip").as_uri()).save_vzip(tmp_path / "in" / "a.vzip")
    refkv.vzip_to_phkv(tmp_path / "in" / "a.vzip", tmp_path / "out" / "a.phkv")
    assert refkv.open(tmp_path / "out" / "a.phkv", binding="phkv").get("k") == DATA[5:12]


def test_relative_to_writes_relative_sources_again(tmp_path):
    (tmp_path / "blob.bin").write_bytes(DATA)
    refkv.ReferenceStore({"k": Range(source=0, length=3)}, [Source(url="blob.bin")],
                         url=(tmp_path / "a.vzip").as_uri()).save_vzip(tmp_path / "a.vzip")
    refkv.vzip_to_phkv(tmp_path / "a.vzip", tmp_path / "a.phkv", relative_to=tmp_path.as_uri() + "/")
    out = refkv.open(tmp_path / "a.phkv", binding="phkv")
    out.list()  # opens it, which reads the source table
    assert [s.url for s in out.sources] == ["blob.bin"]


def test_a_key_source_naming_a_hidden_key_is_kept(tmp_path):
    """`__vz__/x` is hidden in a vzip archive, and an ordinary key in a phkv store; a `key` source
    that names it still reads."""
    store = refkv.ReferenceStore({"__vz__/x": b"hello world", "k": Range(source=0, offset=6, length=5)},
                                 [Source(key="__vz__/x")])
    store.save_vzip(tmp_path / "a.vzip")
    assert refkv.open(tmp_path / "a.vzip").get("k") == b"world"
    refkv.vzip_to_phkv(tmp_path / "a.vzip", tmp_path / "a.phkv")
    out = refkv.open(tmp_path / "a.phkv", binding="phkv")
    assert out.get("k") == b"world"
    assert out.get("__vz__/x") == b"hello world"  # an ordinary key in phkv
    refkv.phkv_to_vzip(tmp_path / "a.phkv", tmp_path / "b.vzip")
    assert refkv.open(tmp_path / "b.vzip").get("k") == b"world"
    assert refkv.open(tmp_path / "b.vzip").get("__vz__/x") is None  # hidden again


def test_concat_and_compressed_values_survive(tmp_path):
    store = refkv.ReferenceStore(
        {"c": Concat((Range(data=b"<"), Range(source=0, length=3))), "z": b"a" * 5000},
        [Source(data=b"xyz")], compressed=["z"])
    store.save_phkv(tmp_path / "a.phkv")
    refkv.phkv_to_vzip(tmp_path / "a.phkv", tmp_path / "a.vzip")
    assert _everything(refkv.open(tmp_path / "a.vzip")) == _everything(store)


def test_a_file_of_the_wrong_binding_is_an_archive_error(tmp_path):
    refkv.ReferenceStore({"k": b"v"}).save_vzip(tmp_path / "a.vzip")
    with pytest.raises(ArchiveError):
        refkv.phkv_to_vzip(tmp_path / "a.vzip", tmp_path / "b.vzip")
    assert not (tmp_path / "b.vzip").exists()


def test_phkv_options_are_passed_on(tmp_path):
    store = _source_store(tmp_path)
    store.save_vzip(tmp_path / "a.vzip")
    with pytest.raises(TypeError):
        refkv.vzip_to_phkv(tmp_path / "a.vzip", tmp_path / "a.phkv", mirror_refs=True)  # type: ignore[call-arg]
    refkv.vzip_to_phkv(tmp_path / "a.vzip", tmp_path / "a.phkv", inline_max=0, page_size=512)
    assert _everything(refkv.open(tmp_path / "a.phkv", binding="phkv")) == _everything(store)
