"""conformance/virtualize/compare.py: §1.1
<https://github.com/d-v-b/vzip/blob/main/spec/virtualize.md#11-output-and-equivalence> equivalence
with exact integers, and the strict
reading of every archive (spec/archive.md, through the independent reader and the writer checks)."""

import io
import sys
import urllib.parse
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "conformance" / "virtualize"))
import compare  # noqa: E402

from vzip.archive import VZipWriter  # noqa: E402
from vzip.pb import Range, Source  # noqa: E402

URL = "https://data.test/image.tif"


def output(doc: dict) -> dict:
    return {"sources": [URL], "entries": {"zarr.json": ("json", doc), "0/c/0": ("ranges", [[0, 8, 16]])}}


def test_differences():
    big = 2**53 + 1
    cases = [
        ({"shape": [1]}, {"shape": [1]}, True),
        ({"scale": [1]}, {"scale": [1.0]}, True),  # a computed number written either way (§1.3 <https://github.com/d-v-b/vzip/blob/main/spec/virtualize.md#13-arithmetic>)
        ({"fill_value": big}, {"fill_value": big - 1}, False),  # integers exactly: binary64 would equal them
        # an integer against a float: equal only if the float is exactly that integer (§1.1
        # <https://github.com/d-v-b/vzip/blob/main/spec/virtualize.md#11-output-and-equivalence>)
        ({"fill_value": big}, {"fill_value": float(big)}, False),
        ({"fill_value": big - 1}, {"fill_value": float(big)}, True),
        ({"flag": True}, {"flag": 1}, False),
        ({"a": [1, 2]}, {"a": [2, 1]}, False),
    ]
    for a, b, equal in cases:
        assert (compare.differences(output(a), output(b)) == []) == equal, (a, b)
    other = output({"shape": [1]})
    other["entries"]["0/c/0"] = ("ranges", [[0, 8, 17]])
    assert compare.differences(output({"shape": [1]}), other)


def archive(path: Path, build=None, *, doc: bytes = b'{"zarr_format":3,"node_type":"group"}') -> Path:
    """A virtualizer-like archive at `path`: a document and a reference; `build(w)` adds to it."""
    with open(path, "wb") as fh:
        w = VZipWriter(fh, page_size=1 << 16)
        src = w.source(Source(url=URL))
        w.add_bytes("zarr.json", doc)
        w.add_ranges("0/c/0", [Range(source=src, offset=8, length=16)])
        if build is not None:
            build(w)
        w.close()
    return path


def test_from_vzip(tmp_path):
    out = compare.from_vzip(archive(tmp_path / "a.vzip"))
    assert out == {"sources": [URL], "entries": {"zarr.json": ("json", {"zarr_format": 3, "node_type": "group"}),
                                                 "0/c/0": ("ranges", [[0, 8, 16]])}}


def test_from_vzip_rejects_a_duplicate_json_member(tmp_path):
    with pytest.raises(compare.Unreadable, match="duplicate member"):
        compare.from_vzip(archive(tmp_path / "a.vzip", doc=b'{"zarr_format":3,"zarr_format":3}'))


def test_from_vzip_rejects_a_changed_reference_body(tmp_path):
    def build(w):
        payload = Range(source=0, offset=0, length=4).encode()
        w._entry("1/c/0", b"not the payload", b"\x76\x7a" + len(payload).to_bytes(2, "little") + payload)

    with pytest.raises(compare.Unreadable, match="writer requirement: 1/c/0: body"):
        compare.from_vzip(archive(tmp_path / "a.vzip", build))


def test_from_vzip_rejects_an_extra_hidden_entry(tmp_path):
    with pytest.raises(compare.Unreadable, match=r"writer requirement: entry set differs: extra \['__vz__/x'\]"):
        compare.from_vzip(archive(tmp_path / "a.vzip", lambda w: w.add_hidden("x", b"hidden")))


def test_from_vzip_rejects_a_duplicate_name(tmp_path):
    def build(w):
        w._names.discard("zarr.json")
        w._entry("zarr.json", b'{"zarr_format":3,"node_type":"array"}')

    with pytest.raises(compare.Unreadable, match="duplicate record 'zarr.json'"):
        compare.from_vzip(archive(tmp_path / "a.vzip", build))


def test_from_vzip_rejects_what_the_strict_reader_refuses(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:  # a ZIP file, but not a vzip archive
        z.writestr("zarr.json", "{}")
    (tmp_path / "a.vzip").write_bytes(buf.getvalue())
    with pytest.raises(compare.Unreadable, match="strict reader: archive error: not a vzip archive"):
        compare.from_vzip(tmp_path / "a.vzip")


def _jpeg_gray(o: int, n: int) -> bytes:
    return (ROOT / "fixtures/tiff/jpeg_gray.tif").read_bytes()[o:o + n]


def _mirror_archive(tmp_path, edit=None) -> Path:
    from vzip.virtualize import _virtualize as virtualize  # the profile output, not the store

    _, out = virtualize(str(ROOT / "fixtures/tiff/jpeg_gray.tif"), "https://data.test/jpeg_gray.tif")
    if edit:
        edit(out)
    path = tmp_path / "m.vzip"
    out.write(str(path))
    return path


def test_mirror_problem():
    """A TIFF archive's IR mirror (conventions §8
    <https://github.com/d-v-b/vzip/blob/main/spec/conventions.md#8-the-ir-mirror>) that reads and
    checks: no problem; an archive without a mirror: not checked."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        path = _mirror_archive(Path(d))
        assert compare.mirror_problem(path, compare._zip_view(path), _jpeg_gray) is None
    assert compare.mirror_problem(Path("unused"), output({"attributes": {}})) is None


def test_mirror_problem_finds_a_table_that_does_not_read(tmp_path):
    def corrupt(out):
        out.bytes_entries["vzip_source/ir/rows/c/0/0"] = b"not zlib"

    path = _mirror_archive(tmp_path, corrupt)
    assert compare.mirror_problem(path, compare._zip_view(path), _jpeg_gray).startswith("Violation: vzip_source/ir/rows/c/0/0")


def test_mirror_problem_finds_a_size_other_than_the_pin(tmp_path):
    def resize(out):
        out.size += 1

    path = _mirror_archive(tmp_path, resize)
    assert "source 0 pins 5320" in compare.mirror_problem(path, compare._zip_view(path), _jpeg_gray)


def test_mirror_problem_finds_a_table_that_is_not_canonical(tmp_path):
    """A valid table whose chunks are deflated: the validator flags it (conventions §8.8
    <https://github.com/d-v-b/vzip/blob/main/spec/conventions.md#88-canonical-form>)."""
    import json
    import zlib

    def deflate(out):
        key = "vzip_source/ir/rows/zarr.json"
        doc = json.loads(out.bytes_entries[key])
        doc["codecs"].append({"name": "zlib", "configuration": {"level": 6}})
        out.bytes_entries[key] = json.dumps(doc).encode()
        for k in [k for k in out.bytes_entries if k.startswith("vzip_source/ir/rows/c/")]:
            out.bytes_entries[k] = zlib.compress(out.bytes_entries[k], 6)

    path = _mirror_archive(tmp_path, deflate)
    assert compare.mirror_problem(path, compare._zip_view(path), _jpeg_gray) == \
        "not canonical: ir/rows: compressed chunks (the canonical table's chunks are raw)"


def test_at_revision():
    """A frozen reference's root (revision 20 for TIFF, ND2 and CZI, 25 for DICOM, NIfTI, IMS,
    NDPI, SAFE, Zarr v2 and OME-Zarr) reads at the current revision, without the members of its source
    metadata the mirror holds instead; any other root (another revision, another profile's frozen
    one, no revision, a profile with no twin), or an output without one, is unchanged."""
    def root(revision, profile="tiff", own=None):
        prop = {"profile": profile, "version": 0, "revision": revision, "source": {"url": "u"}}
        if revision is None:
            del prop["revision"]
        if own is not None:
            prop[profile] = own
        return {"sources": [], "entries": {"zarr.json": ("json", {"attributes": {"vzip_virtualized": prop}}),
                                           "a": ("bytes", b"x")}}

    for profile, frozen in compare.REFERENCE_REVISIONS.items():
        assert compare.at_revision(root(frozen, profile)) == root(compare.REVISION, profile), profile
    kept = {"nifti_version": 1, "header": {}, "scaling": {"slope": 2, "inter": 0}}
    frozen = {**kept, "extender": "AQAAAA==", "extensions": [], "unparsed": "vzip_source/unparsed"}
    assert compare.at_revision(root(25, "nifti", frozen)) == root(compare.REVISION, "nifti", kept)
    for other in (root(compare.REVISION), root(19), root(25), root(20, "zarr2"), root(20, "dicom"),
                  root(20, "ims"), root(20, "nifti", frozen), root(None), root(None, "n5"), root(20, "n5"),
                  {"sources": [], "entries": {}}):
        assert compare.at_revision(other) == other


def test_without_source():
    """The entries under vzip_source go; the others' sources are those they use, in order of
    first use (entries by key), so two outputs whose url sources differ only in what
    vzip_source used, or in order, compare equal."""
    data = {"data": "AAE="}
    a = {"sources": ["u/x", "u/b", "u/a", data], "entries": {
        "r/c/1": ("ranges", [[2, 0, 4], [3, 0, 2]]), "r/c/0": ("ranges", [[1, 0, 4], ["data", "AA=="]]),
        "r/zarr.json": ("json", {}), "vzip_source/objects/x": ("ranges", [[0, 0, 9]])}}
    b = {"sources": ["u/a", "u/b", data], "entries": {
        "r/c/0": ("ranges", [[1, 0, 4], ["data", "AA=="]]), "r/c/1": ("ranges", [[0, 0, 4], [2, 0, 2]]),
        "r/zarr.json": ("json", {}), "vzip_source/ir/rows/c/0/0": ("bytes", "h")}}
    want = {"sources": ["u/b", "u/a", data], "entries": {
        "r/c/0": ("ranges", [[0, 0, 4], ["data", "AA=="]]), "r/c/1": ("ranges", [[1, 0, 4], [2, 0, 2]]),
        "r/zarr.json": ("json", {})}}
    assert compare.without_source(a) == want == compare.without_source(b)


def test_mirror_problem_finds_a_view_that_is_not_the_tables(tmp_path):
    """A view whose root document lost a member: the validator's view check flags it."""
    import json

    def drop(out):
        key = "vzip_source/tree/zarr.json"
        doc = json.loads(out.bytes_entries[key])
        del doc["attributes"]["vzip_virtualized"]["tiff"]["header"]["magic"]
        out.bytes_entries[key] = json.dumps(doc).encode()

    path = _mirror_archive(tmp_path, drop)
    assert compare.mirror_problem(path, compare._zip_view(path), _jpeg_gray) == 'view tree: member ["header"]["magic"] is missing'


def _store_archive(tmp_path, name="zarr2_hierarchy", edit=None, kind="zarr2") -> tuple[Path, callable]:
    """A fixture store's archive (a Zarr v2 store's or a SAFE directory's: its mirror a store
    IR's), and a fetch of ranges of its objects by URL."""
    from vzip.virtualize import _virtualize as virtualize  # the profile output, not the store

    url = f"https://data.test/{name}/"
    _, out = virtualize(str(ROOT / "fixtures" / kind / name), url)
    if edit:
        edit(out)
    path = tmp_path / "s.vzip"
    out.write(str(path))

    def fetch(u: str, o: int, n: int) -> bytes:
        return (ROOT / "fixtures" / kind / name / urllib.parse.unquote(u[len(url):])).read_bytes()[o:o + n]

    return path, fetch


def test_store_mirror_problem(tmp_path):
    """A store's mirror (conventions §8.9
    <https://github.com/d-v-b/vzip/blob/main/spec/conventions.md#89-stores>) that reads, rebuilds
    the listing and the documents, and is canonical: no problem."""
    for kind, name in (("zarr2", "zarr2_hierarchy"), ("zarr2", "zarr2_objects"), ("zarr2", "zarr2_root_array_empty"),
                       ("safe", "safe_l2a_pb0207")):
        path, fetch = _store_archive(tmp_path, name, kind=kind)
        assert compare.store_mirror_problem(path, compare._zip_view(path), fetch) is None, name


def test_store_mirror_problem_finds_a_document_other_than_the_store_s(tmp_path):
    path, fetch = _store_archive(tmp_path)
    assert compare.store_mirror_problem(path, compare._zip_view(path), lambda u, o, n: fetch(u, o, n)[:-1] + b"!") == \
        ".zattrs: the mirror's bytes are not the object's"


def test_store_mirror_problem_finds_a_table_that_does_not_read(tmp_path):
    def corrupt(out):
        out.entries["vzip_source/ir/rows/c/0/0"] = b"not raw int64"

    path, fetch = _store_archive(tmp_path, edit=corrupt)
    assert compare.store_mirror_problem(path, compare._zip_view(path), fetch).startswith("Violation")


def test_store_mirror_problem_finds_a_size_other_than_the_pin(tmp_path):
    def resize(out):
        (_, key, _, size), = out.refs["a/c/0/0"]
        out.sizes[key] = size + 1

    path, fetch = _store_archive(tmp_path, edit=resize)
    assert compare.store_mirror_problem(path, compare._zip_view(path), fetch) == \
        "a/c/0/0: the mirror lists 16 bytes, its source pins 17"


def test_store_mirror_problem_finds_a_source_of_no_listed_object(tmp_path):
    def stray(out):
        out.refs["a/c/0/0"] = [("object", "nowhere", 0, 16)]

    path, fetch = _store_archive(tmp_path, edit=stray)
    assert compare.store_mirror_problem(path, compare._zip_view(path), fetch) == \
        "the url source https://data.test/zarr2_hierarchy/nowhere names no object of the listing"


def test_store_mirror_problem_finds_a_value_described_in_place_other_than_the_store_s(tmp_path):
    """A SAFE band file's values are read from the store at their virtual offsets: a store
    that serves other bytes there gives another view."""
    path, fetch = _store_archive(tmp_path, "safe_l2a_pb0207", kind="safe")

    def other(u, o, n):
        b = fetch(u, o, n)
        return bytes(x ^ 0xFF for x in b) if u.endswith(".jp2") else b

    assert compare.store_mirror_problem(path, compare._zip_view(path), other).startswith("view ")


def test_store_mirror_problem_finds_an_object_no_source_names(tmp_path):
    def drop(out):
        del out.refs["a/c/0/0"]

    path, fetch = _store_archive(tmp_path, edit=drop)
    assert compare.store_mirror_problem(path, compare._zip_view(path), fetch) == \
        "a/c/0/0: a referenced object of 16 bytes that no source names"


def test_store_mirror_problem_finds_a_root_without_a_store_url(tmp_path):
    path, fetch = _store_archive(tmp_path)
    view = compare._zip_view(path)
    view["entries"]["zarr.json"] = ("json", {"attributes": {}})
    assert compare.store_mirror_problem(path, view, fetch) == "no store URL on the root"


def test_store_mirror_problem_finds_an_object_the_store_does_not_serve(tmp_path):
    path, _ = _store_archive(tmp_path)

    def refuse(u, o, n):
        raise OSError("refused")

    assert compare.store_mirror_problem(path, compare._zip_view(path), refuse) == ".zattrs: OSError: refused"
