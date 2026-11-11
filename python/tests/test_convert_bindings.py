"""Converting a saved store from one binding to the other: `refkv.vzip_to_phkv`,
`refkv.phkv_to_vzip` and `ReferenceStore.from_store`."""

import pytest
from test_phkv import DATA, N, _everything, _mapping

import refkv
from refkv.errors import ArchiveError, PayloadError, ResolutionError
from refkv.pb import Concat, Range, Source
from refkv.policy import Policy

FILES = Policy(schemes=frozenset({"file"}))  # since #266, local sources are read, and saved, only when allowed


def _source_store(tmp_path) -> refkv.ReferenceStore:
    (tmp_path / "blob.bin").write_bytes(DATA)
    return refkv.ReferenceStore(_mapping(), [Source(url=(tmp_path / "blob.bin").as_uri()), Source(data=b"0123456789")],
                                policy=FILES)


def _values(store) -> dict:
    """Every key's value as stored, a reference not followed."""
    return {k: store.value(k) for k in store.list()}


def _convert(binding: str, src, dst, **options):
    """`binding` is the one of `src`."""
    convert = refkv.vzip_to_phkv if binding == "vzip" else refkv.phkv_to_vzip
    return convert(src, dst, policy=FILES, **options)


@pytest.mark.parametrize("binding", ["vzip", "phkv"])
def test_conversion_keeps_every_key_kind_and_value(tmp_path, binding):
    store = _source_store(tmp_path)
    other = "phkv" if binding == "vzip" else "vzip"
    getattr(store, f"save_{binding}")(tmp_path / f"a.{binding}", policy=FILES)
    summary = _convert(binding, tmp_path / f"a.{binding}", tmp_path / f"a.{other}")
    out = refkv.open(tmp_path / f"a.{other}", binding=other, policy=FILES)
    assert _everything(out) == _everything(store)
    assert _values(out) == _values(store)  # the references themselves, not what they resolve to
    assert (summary.keys, summary.references, summary.sources) == (len(store.list()), N + 1, 2)


def test_round_trip_is_the_same_store(tmp_path):
    store = _source_store(tmp_path)
    store.save_vzip(tmp_path / "a.vzip")
    refkv.vzip_to_phkv(tmp_path / "a.vzip", tmp_path / "b.phkv", policy=FILES)
    refkv.phkv_to_vzip(tmp_path / "b.phkv", tmp_path / "c.vzip", policy=FILES)
    a, c = (refkv.ReferenceStore.from_store(refkv.open(tmp_path / f"{n}.vzip", policy=FILES)) for n in "ac")
    assert (dict(c.mapping), c.sources) == (dict(a.mapping), a.sources)  # not `==`: the URLs differ


def test_references_are_not_resolved(tmp_path):
    """The sources are not read: converting a store whose source is gone works."""
    store = refkv.ReferenceStore({"k": Range(source=0, offset=0, length=4)}, [Source(url=(tmp_path / "gone").as_uri())])
    store.save_vzip(tmp_path / "a.vzip")
    refkv.vzip_to_phkv(tmp_path / "a.vzip", tmp_path / "a.phkv", policy=FILES)
    out = refkv.open(tmp_path / "a.phkv", binding="phkv", policy=FILES)
    assert out.classify("k") == "reference"
    assert out.value("k") == Range(source=0, offset=0, length=4)


def test_relative_source_stays_relative_when_it_resolves_the_same(tmp_path):
    (tmp_path / "blob.bin").write_bytes(DATA)
    refkv.ReferenceStore({"k": Range(source=0, length=3)}, [Source(url="blob.bin")],
                         url=(tmp_path / "a.vzip").as_uri()).save_vzip(tmp_path / "a.vzip", policy=FILES)
    refkv.vzip_to_phkv(tmp_path / "a.vzip", tmp_path / "a.phkv", policy=FILES)
    out = refkv.open(tmp_path / "a.phkv", binding="phkv", policy=FILES)
    out.list()  # opens it, which reads the source table
    assert [s.url for s in out.sources] == ["blob.bin"]


def test_relative_source_is_made_absolute_in_another_directory(tmp_path):
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()
    (tmp_path / "in" / "blob.bin").write_bytes(DATA)
    refkv.ReferenceStore({"k": Range(source=0, offset=5, length=7)}, [Source(url="blob.bin")],
                         url=(tmp_path / "in" / "a.vzip").as_uri()).save_vzip(tmp_path / "in" / "a.vzip", policy=FILES)
    refkv.vzip_to_phkv(tmp_path / "in" / "a.vzip", tmp_path / "out" / "a.phkv", policy=FILES)
    assert refkv.open(tmp_path / "out" / "a.phkv", binding="phkv", policy=FILES).get("k") == DATA[5:12]


def test_relative_to_writes_urls_under_it_relative(tmp_path):
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()
    (tmp_path / "in" / "blob.bin").write_bytes(DATA)
    refkv.ReferenceStore({"k": Range(source=0, length=3)}, [Source(url="blob.bin")],
                         url=(tmp_path / "in" / "a.vzip").as_uri()).save_vzip(tmp_path / "in" / "a.vzip", policy=FILES)
    refkv.vzip_to_phkv(tmp_path / "in" / "a.vzip", tmp_path / "out" / "a.phkv",
                       relative_to=(tmp_path / "in").as_uri() + "/")
    out = refkv.open(tmp_path / "out" / "a.phkv", binding="phkv", policy=FILES)
    out.list()
    assert [s.url for s in out.sources] == ["blob.bin"]


def test_a_key_source_naming_a_hidden_key_is_kept(tmp_path):
    """`__vz__/x` is hidden in a vzip archive, and an ordinary key in a phkv store; a `key` source
    that names it still reads."""
    store = refkv.ReferenceStore({"__vz__/x": b"hello world", "k": Range(source=0, offset=6, length=5)},
                                 [Source(key="__vz__/x")])
    store.save_vzip(tmp_path / "a.vzip")
    assert refkv.open(tmp_path / "a.vzip", policy=FILES).get("k") == b"world"
    refkv.vzip_to_phkv(tmp_path / "a.vzip", tmp_path / "a.phkv", policy=FILES)
    out = refkv.open(tmp_path / "a.phkv", binding="phkv", policy=FILES)
    assert out.get("k") == b"world"
    assert out.get("__vz__/x") == b"hello world"  # an ordinary key in phkv
    refkv.phkv_to_vzip(tmp_path / "a.phkv", tmp_path / "b.vzip", policy=FILES)
    assert refkv.open(tmp_path / "b.vzip", policy=FILES).get("k") == b"world"
    assert refkv.open(tmp_path / "b.vzip", policy=FILES).get("__vz__/x") is None  # hidden again


def test_a_vzip_archive_with_a_hybrid_entry_converts(tmp_path):
    """The mirrored payload of a reference entry is not mistaken for its value."""
    store = _source_store(tmp_path)
    store.save_vzip(tmp_path / "a.vzip", mirror_refs=True)
    refkv.vzip_to_phkv(tmp_path / "a.vzip", tmp_path / "a.phkv", policy=FILES)
    assert _values(refkv.open(tmp_path / "a.phkv", binding="phkv", policy=FILES)) == _values(store)


def test_concat_and_compressed_values_survive(tmp_path):
    store = refkv.ReferenceStore(
        {"c": Concat((Range(data=b"<"), Range(source=0, length=3))), "z": b"a" * 5000},
        [Source(data=b"xyz")], compressed=["z"])
    store.save_phkv(tmp_path / "a.phkv")
    refkv.phkv_to_vzip(tmp_path / "a.phkv", tmp_path / "a.vzip", policy=FILES)
    assert _everything(refkv.open(tmp_path / "a.vzip", policy=FILES)) == _everything(store)


def test_phkv_options_are_passed_on(tmp_path):
    store = _source_store(tmp_path)
    store.save_vzip(tmp_path / "a.vzip")
    refkv.vzip_to_phkv(tmp_path / "a.vzip", tmp_path / "a.phkv", inline_max=0, page_size=512, policy=FILES)
    assert _everything(refkv.open(tmp_path / "a.phkv", binding="phkv", policy=FILES)) == _everything(store)


def test_an_option_of_the_other_binding_is_a_type_error(tmp_path):
    _source_store(tmp_path).save_vzip(tmp_path / "a.vzip", policy=FILES)
    with pytest.raises(TypeError):
        refkv.vzip_to_phkv(tmp_path / "a.vzip", tmp_path / "a.phkv", mirror_refs=True, policy=FILES)  # type: ignore[call-arg]


def test_a_file_of_the_wrong_binding_is_an_archive_error(tmp_path):
    refkv.ReferenceStore({"k": b"v"}).save_vzip(tmp_path / "a.vzip", policy=FILES)
    with pytest.raises(ArchiveError):
        refkv.phkv_to_vzip(tmp_path / "a.vzip", tmp_path / "b.vzip", policy=FILES)
    assert not (tmp_path / "b.vzip").exists()


def test_a_phkv_key_under_the_reserved_prefix_is_refused(tmp_path):
    refkv.ReferenceStore({"__vz__/notes": b"n", "k": b"v"}).save_phkv(tmp_path / "a.phkv", policy=FILES)
    with pytest.raises(ValueError, match="__vz__/notes"):
        refkv.phkv_to_vzip(tmp_path / "a.phkv", tmp_path / "a.vzip", policy=FILES)
    assert not (tmp_path / "a.vzip").exists()


def test_a_phkv_reference_under_the_reserved_prefix_is_refused(tmp_path):
    refkv.ReferenceStore({"__vz__/r": Range(data=b"abc")}).save_phkv(tmp_path / "a.phkv", policy=FILES)
    with pytest.raises(ValueError, match="__vz__/r"):
        refkv.phkv_to_vzip(tmp_path / "a.phkv", tmp_path / "a.vzip", policy=FILES)


def test_a_reference_payload_too_long_for_vzip_is_refused_and_leaves_the_file(tmp_path):
    big = Concat(tuple(Range(data=b"x" * 1000) for _ in range(70)))  # over vzip's 65,519 bytes
    refkv.ReferenceStore({"k": big}).save_phkv(tmp_path / "a.phkv", policy=FILES)
    (tmp_path / "a.vzip").write_bytes(b"previous")
    with pytest.raises((ValueError, PayloadError)):
        refkv.phkv_to_vzip(tmp_path / "a.phkv", tmp_path / "a.vzip", policy=FILES)
    assert (tmp_path / "a.vzip").read_bytes() == b"previous"


def test_a_failed_conversion_leaves_an_existing_destination(tmp_path):
    refkv.ReferenceStore({"__vz__/n": b"n"}).save_phkv(tmp_path / "a.phkv", policy=FILES)
    (tmp_path / "a.vzip").write_bytes(b"previous")
    with pytest.raises(ValueError, match="__vz__/n"):
        refkv.phkv_to_vzip(tmp_path / "a.phkv", tmp_path / "a.vzip", policy=FILES)
    assert (tmp_path / "a.vzip").read_bytes() == b"previous"


def test_policy_applies_to_the_reader_as_well(tmp_path):
    refkv.ReferenceStore({"k": b"v"}, [Source(url="s3://bucket/x")]).save_vzip(
        tmp_path / "a.vzip", policy=Policy(schemes=frozenset({"s3"})))
    with pytest.raises(ResolutionError):
        refkv.vzip_to_phkv(tmp_path / "a.vzip", tmp_path / "a.phkv", policy=FILES)  # the default policy refuses s3
    refkv.vzip_to_phkv(tmp_path / "a.vzip", tmp_path / "a.phkv", policy=Policy(schemes=frozenset({"s3"})))
    assert (tmp_path / "a.phkv").exists()


def test_from_store_refuses_a_raw_view_of_an_archive(tmp_path):
    refkv.ReferenceStore({"k": Range(data=b"abc")}).save_vzip(tmp_path / "a.vzip", policy=FILES)
    with pytest.raises(ValueError, match="resolve"):
        refkv.ReferenceStore.from_store(refkv.vzip.open(str(tmp_path / "a.vzip"), resolve=False))
