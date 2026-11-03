"""conformance/zarrify/compare.py: §1.1
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#11-output-and-equivalence> equivalence
with exact integers, and the strict
reading of every archive (spec/bindings/vzip.md, through the independent reader and the writer checks)."""

import io
import json
import re
import sys
import tempfile
import urllib.parse
import zipfile
import zlib
from collections.abc import Callable
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "conformance" / "zarrify"))
import compare  # noqa: E402

from refkv._narrow import string  # noqa: E402
from refkv.pb import Range, Source  # noqa: E402
from refkv.vzip.archive import VZipWriter  # noqa: E402
from refkv.zarrify import _zarrify as zarrify  # the profile output, not the store  # noqa: E402
from refkv.zarrify.common import convention  # noqa: E402

URL = "https://data.test/image.tif"


def output(doc: dict) -> dict:
    return {"sources": [URL], "entries": {"zarr.json": ("json", doc), "0/c/0": ("ranges", [[0, 8, 16]])}}


def test_differences():
    big = 2**53 + 1
    cases = [
        ({"shape": [1]}, {"shape": [1]}, True),
        ({"scale": [1]}, {"scale": [1.0]}, True),  # a computed number written either way (§1.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#13-arithmetic>)
        ({"fill_value": big}, {"fill_value": big - 1}, False),  # integers exactly: binary64 would equal them
        # an integer against a float: equal only if the float is exactly that integer (§1.1
        # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#11-output-and-equivalence>)
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
    """A zarrifier-like archive at `path`: a document and a reference; `build(w)` adds to it."""
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
    with pytest.raises(compare.UnreadableError, match="duplicate member"):
        compare.from_vzip(archive(tmp_path / "a.vzip", doc=b'{"zarr_format":3,"zarr_format":3}'))


def test_from_vzip_rejects_a_changed_reference_body(tmp_path):
    def build(w):
        payload = Range(source=0, offset=0, length=4).encode()
        w._entry("1/c/0", b"not the payload", b"\x76\x7a" + len(payload).to_bytes(2, "little") + payload)

    with pytest.raises(compare.UnreadableError, match="writer requirement: 1/c/0: body"):
        compare.from_vzip(archive(tmp_path / "a.vzip", build))


def test_from_vzip_rejects_an_extra_hidden_entry(tmp_path):
    with pytest.raises(compare.UnreadableError, match=r"writer requirement: entry set differs: extra \['__vz__/x'\]"):
        compare.from_vzip(archive(tmp_path / "a.vzip", lambda w: w.add_hidden("x", b"hidden")))


def test_from_vzip_rejects_a_duplicate_name(tmp_path):
    def build(w):
        w._names.discard("zarr.json")
        w._entry("zarr.json", b'{"zarr_format":3,"node_type":"array"}')

    with pytest.raises(compare.UnreadableError, match=re.escape("duplicate record 'zarr.json'")):
        compare.from_vzip(archive(tmp_path / "a.vzip", build))


def test_from_vzip_rejects_what_the_strict_reader_refuses(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:  # a ZIP file, but not a vzip archive
        z.writestr("zarr.json", "{}")
    (tmp_path / "a.vzip").write_bytes(buf.getvalue())
    with pytest.raises(compare.UnreadableError, match="strict reader: archive error: not a vzip archive"):
        compare.from_vzip(tmp_path / "a.vzip")


def _jpeg_gray(o: int, n: int) -> bytes:
    return (ROOT / "fixtures/tiff/jpeg_gray.tif").read_bytes()[o:o + n]


def _mirror_archive(tmp_path, edit=None) -> Path:
    _, out = zarrify(str(ROOT / "fixtures/tiff/jpeg_gray.tif"), "https://data.test/jpeg_gray.tif")
    if edit:
        edit(out)
    path = tmp_path / "m.vzip"
    out.write(str(path))
    return path


def test_mirror_problem():
    """A TIFF archive's IR mirror (conventions §8
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#8-the-ir-mirror>) that reads and
    checks: no problem; an archive without a mirror: not checked."""
    with tempfile.TemporaryDirectory() as d:
        path = _mirror_archive(Path(d))
        assert compare.mirror_problem(path, compare._zip_view(path), _jpeg_gray) is None
    assert compare.mirror_problem(Path("unused"), output({"attributes": {}})) is None


def test_mirror_problem_finds_a_table_that_does_not_read(tmp_path):
    def corrupt(out):
        out.bytes_entries["refkv_source/ir/rows/c/0/0"] = b"not zlib"

    path = _mirror_archive(tmp_path, corrupt)
    problem = compare.mirror_problem(path, compare._zip_view(path), _jpeg_gray)
    assert string(problem).startswith("Violation: refkv_source/ir/rows/c/0/0")


def test_mirror_problem_finds_a_size_other_than_the_pin(tmp_path):
    def resize(out):
        out.size += 1

    path = _mirror_archive(tmp_path, resize)
    assert "source 0 pins 5320" in string(compare.mirror_problem(path, compare._zip_view(path), _jpeg_gray))


def test_mirror_problem_finds_a_table_that_is_not_canonical(tmp_path):
    """A valid table whose chunks are deflated: the validator flags it (conventions §8.8
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#88-canonical-form>)."""
    def deflate(out):
        key = "refkv_source/ir/rows/zarr.json"
        doc = json.loads(out.bytes_entries[key])
        doc["codecs"].append({"name": "numcodecs.zlib", "configuration": {"level": 6}})
        out.bytes_entries[key] = json.dumps(doc).encode()
        for k in [k for k in out.bytes_entries if k.startswith("refkv_source/ir/rows/c/")]:
            out.bytes_entries[k] = zlib.compress(out.bytes_entries[k], 6)

    path = _mirror_archive(tmp_path, deflate)
    assert compare.mirror_problem(path, compare._zip_view(path), _jpeg_gray) == \
        "not canonical: ir/rows: compressed chunks (the canonical table's chunks are raw)"


def test_at_revision():
    """A frozen reference's root (revision 20 for TIFF, ND2 and CZI, 25 for DICOM, NIfTI, IMS,
    NDPI, SAFE, Zarr v2, N5 and OME-Zarr) reads without its revision, without the members of its source
    metadata the mirror holds instead, and its names (vzip_virtualized, the convention metadata of
    d-v-b/vzip, paths under vzip_source) as the current ones; any other root (another revision,
    another profile's frozen one, no revision, a profile with no twin), or an output without one,
    is unchanged."""
    def root(revision, profile="tiff", own=None):
        prop = {"profile": profile, "version": 0, "revision": revision, "source": {"url": "u"}}
        if revision is None:
            del prop["revision"]
        if own is not None:
            prop[profile] = own
        return {"sources": [], "entries": {"zarr.json": ("json", {"attributes": {"refkv_zarrified": prop}}),
                                           "a": ("bytes", b"x")}}

    def named(revision, profile, legacy, mirrored=()):
        key, node = ("vzip_virtualized", "vzip_source") if legacy else ("refkv_zarrified", "refkv_source")
        cmo = convention(profile)
        if legacy:
            cmo = {**cmo, "name": key, "schema_url": string(cmo["schema_url"]).replace("/refkv/", "/vzip/"),
                   "spec_url": string(cmo["spec_url"]).replace("/refkv/", "/vzip/"),
                   "description": string(cmo["description"]).replace("by refkv", "by vzip")}
        prop = {"profile": profile, "version": 0, **({} if revision is None else {"revision": revision}),
                "source": {"url": "u"},
                profile: {"at": f"{node}/x", "list": [f"{node}/y", "z"], **{m: f"{node}/{m}" for m in mirrored}}}
        return {"sources": [], "entries": {
            "zarr.json": ("json", {"attributes": {"zarr_conventions": [cmo, {"uuid": "other"}], key: prop}}),
            "0/zarr.json": ("json", {"attributes": {key: {profile: {"n": 1}}}}),
            f"{node}/zarr.json": ("json", {"attributes": {key: {}}}),
            "a": ("bytes", b"x")}}

    for profile, frozen in compare.REFERENCE_REVISIONS.items():
        assert compare.at_revision(root(frozen, profile)) == root(None, profile), profile
        # the old names read as the current ones, and the members the mirror holds dropped
        legacy = compare.at_revision(named(frozen, profile, True, compare.MIRRORED.get(string(profile), ())))
        assert compare.without_source(legacy) == compare.without_source(named(None, profile, False))
    kept = {"nifti_version": 1, "header": {}, "scaling": {"slope": 2, "inter": 0}}
    frozen = {**kept, "extender": "AQAAAA==", "extensions": [], "unparsed": "refkv_source/unparsed"}
    assert compare.at_revision(root(25, "nifti", frozen)) == root(None, "nifti", kept)
    for other in (root(28), root(19), root(25), root(20, "zarr2"), root(20, "dicom"),
                  root(20, "ims"), root(20, "nifti", frozen), root(None), root(None, "n5"), root(20, "n5"),
                  {"sources": [], "entries": {}}):
        assert compare.at_revision(other) == other


def test_without_source():
    """The entries under refkv_source go; the others' sources are those they use, in order of
    first use (entries by key), so two outputs whose url sources differ only in what
    refkv_source used, or in order, compare equal."""
    data = {"data": "AAE="}
    a = {"sources": ["u/x", "u/b", "u/a", data], "entries": {
        "r/c/1": ("ranges", [[2, 0, 4], [3, 0, 2]]), "r/c/0": ("ranges", [[1, 0, 4], ["data", "AA=="]]),
        "r/zarr.json": ("json", {}), "refkv_source/objects/x": ("ranges", [[0, 0, 9]])}}
    b = {"sources": ["u/a", "u/b", data], "entries": {
        "r/c/0": ("ranges", [[1, 0, 4], ["data", "AA=="]]), "r/c/1": ("ranges", [[0, 0, 4], [2, 0, 2]]),
        "r/zarr.json": ("json", {}), "refkv_source/ir/rows/c/0/0": ("bytes", "h")}}
    want = {"sources": ["u/b", "u/a", data], "entries": {
        "r/c/0": ("ranges", [[0, 0, 4], ["data", "AA=="]]), "r/c/1": ("ranges", [[1, 0, 4], [2, 0, 2]]),
        "r/zarr.json": ("json", {})}}
    assert compare.without_source(a) == want == compare.without_source(b)


def test_literal_padding():
    """A NIfTI output's ranges of its zero data source read as literal ranges of their bytes,
    and the source then drops out of `without_source`, as the frozen reference writes it; a data
    source of other bytes, and another profile's output, are unchanged."""
    def out(profile, sources, ranges):
        root = ("json", {"attributes": {"refkv_zarrified": {"profile": profile}}})
        return {"sources": sources, "entries": {"zarr.json": root, "0/c/0": ("ranges", ranges)}}

    zeros, other = {"data": "AAAA"}, {"data": "AAE="}
    padded = out("nifti", ["u", zeros], [[0, 8, 16], [1, 0, 2]])
    literal = out("nifti", ["u", zeros], [[0, 8, 16], ["data", "AAA="]])
    assert compare.literal_padding(padded) == literal
    frozen = out("nifti", ["u"], [[0, 8, 16], ["data", "AAA="]])
    assert compare.without_source(literal) == compare.without_source(frozen)
    for kept in (out("nifti", ["u", other], [[0, 8, 16], [1, 0, 2]]),
                 out("mrc", ["u", zeros], [[0, 8, 16], [1, 0, 2]])):
        assert compare.literal_padding(kept) == kept


def test_mirror_problem_finds_a_view_that_is_not_the_tables(tmp_path):
    """A view whose root document lost a member: the validator's view check flags it."""
    def drop(out):
        key = "refkv_source/tree/zarr.json"
        doc = json.loads(out.bytes_entries[key])
        del doc["attributes"]["refkv_zarrified"]["tiff"]["header"]["magic"]
        out.bytes_entries[key] = json.dumps(doc).encode()

    path = _mirror_archive(tmp_path, drop)
    problem = compare.mirror_problem(path, compare._zip_view(path), _jpeg_gray)
    assert problem == 'view tree: member ["header"]["magic"] is missing'


def _store_archive(tmp_path, name="zarr2_hierarchy", edit=None,
                   kind="zarr2") -> tuple[Path, Callable[[str, int, int], bytes]]:
    """A fixture store's archive (a Zarr v2 store's or a SAFE directory's: its mirror a store
    IR's), and a fetch of ranges of its objects by URL."""
    url = f"https://data.test/{name}/"
    _, out = zarrify(str(ROOT / "fixtures" / kind / name), url)
    if edit:
        edit(out)
    path = tmp_path / "s.vzip"
    out.write(str(path))

    def fetch(u: str, o: int, n: int) -> bytes:
        return (ROOT / "fixtures" / kind / name / urllib.parse.unquote(u[len(url):])).read_bytes()[o:o + n]

    return path, fetch


def test_store_mirror_problem(tmp_path):
    """A store's mirror (conventions §8.9
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#89-stores>) that reads, rebuilds
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
        out.entries["refkv_source/ir/rows/c/0/0"] = b"not raw int64"

    path, fetch = _store_archive(tmp_path, edit=corrupt)
    assert string(compare.store_mirror_problem(path, compare._zip_view(path), fetch)).startswith("Violation")


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

    assert string(compare.store_mirror_problem(path, compare._zip_view(path), other)).startswith("view ")


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

    def refuse(_url, _offset, _length):
        raise OSError("refused")

    assert compare.store_mirror_problem(path, compare._zip_view(path), refuse) == ".zattrs: OSError: refused"


def test_unmutated():
    cases = [
        ("lzw_uint8.m3.tif", "lzw_uint8.tif"),
        ("strips_rgb_planar_lzw.ome.m12.tif", "strips_rgb_planar_lzw.ome.tif"),
        ("n5_reject_objects_collision.m7", "n5_reject_objects_collision"),
        ("lzw_uint8.tif", "lzw_uint8.tif"),  # not a mutant
        ("strips_rgb_planar_lzw.ome.tif", "strips_rgb_planar_lzw.ome.tif"),
        ("zarr2_hierarchy", "zarr2_hierarchy"),
    ]
    for name, want in cases:
        assert compare.unmutated(name) == want, name


# An implementation for check(): `fake.py <verdict> <url> <out>` rejects (exit 3), crashes (exit
# 1), or writes a HARNESS.md output whose root declares the profile <verdict>.
FAKE = """
import json, sys
verdict, _url, out = sys.argv[1:]
if verdict == "reject":
    print("rejected: no", file=sys.stderr)
    sys.exit(3)
if verdict == "crash":
    sys.exit(1)
root = {"zarr_format": 3, "node_type": "group", "attributes": {KEY: {"profile": verdict}}}
json.dump({"sources": [], "entries": {"zarr.json": {"json": root}}}, open(out, "w"))
""".replace("KEY", repr(compare.CONVENTION_KEY))


def test_check(tmp_path, monkeypatch):
    fake = tmp_path / "fake.py"
    fake.write_text(FAKE)
    proxy = compare.Proxy(tmp_path, tmp_path / "cache")

    def check(ref: str, others: str, why: str | None = None) -> dict:
        impls = {n: ("json", [sys.executable, str(fake), v])
                 for n, v in (("ref", ref), ("py", others), ("web", others))}
        monkeypatch.setattr(proxy, "unavailable", lambda _url: why)  # proxy.py's Failures, as if recorded
        verdicts = compare.check(("x", proxy.remote("https://x.test/a")), impls, tmp_path, proxy)["verdicts"]
        return {n: v[0] for n, v in verdicts.items()}

    # a profile the frozen reference never had: py and web alone; one it had: against it
    assert check("reject", "hdf5") == {"py": "equivalent", "web": "equivalent"}
    assert check("reject", "tiff") == {"ref": "rejects", "py": "DIVERGES", "web": "DIVERGES"}
    # an input its server did not serve: not compared, unless every implementation read it
    every = ("ref", "py", "web")
    assert check("crash", "crash", "https://x.test/a: TimeoutError") == dict.fromkeys(every, "UNAVAILABLE")
    assert check("reject", "reject", "https://x.test/a: HTTPError 404") == dict.fromkeys(every, "UNAVAILABLE")
    assert check("tiff", "tiff", "https://x.test/a: TimeoutError") == dict.fromkeys(every, "equivalent")
    assert check("crash", "crash") == dict.fromkeys(every, "CRASHED")
