"""The LIF IR: the Rust parser and its schema, the image projection (spec/zarrify/lif.md)
against liffile's reading of every synthetic LIF, LOF and LIFEXT file, the mirror's rebuild, and the
bands of a plane past 2^24 bytes. LIF has no frozen Python reference: liffile is the independent reader. The
inputs the profile rejects are conformance/zarrify/rejects/lif.json's (test_rejects.py)."""

import json
import math
import sys
from pathlib import Path

import liffile
import numpy as np
import pytest
import zarr
from ir_helpers import FIXTURES, ROOT, zarrified

import refkv.zarr
from refkv import _core
from refkv._narrow import at, integer, mapping, ndarray, number, sequence, string
from refkv.ir import NamesSiblingsError
from refkv.ir import zarrify as via_ir
from refkv.ir.store import StoreIrOutput
from refkv.policy import Policy
from refkv.zarrify import Rejected
from refkv.zarrify import _zarrify as zarrify
from refkv.zarrify.store import object_url
from refkv.zarrify.store.beside import VsiFiles, folded, found_key

sys.path.insert(0, str(ROOT / "conformance" / "zarrify"))
from proxy import Proxy

sys.path.insert(0, str(ROOT / "fixtures" / "generators" / "lif"))
# pyrefly: ignore[missing-import]  # on sys.path above; the config's search-path lacks fixtures/generators
from write_fixtures import Img, Lif, Lof, accepted_lifext  # the fixtures' writer, for a file too large to commit

LIFS = sorted(p for s in ("lif", "lof", "lifext") for p in (FIXTURES / "lif").glob(f"lif_*.{s}"))


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """The fixtures served over HTTP by the harness proxy: the directory's URL, ending in `/`."""
    return Proxy(FIXTURES / "lif", tmp_path_factory.mktemp("cache")).base + "f/"
# liffile's dimension codes as axes; any other is a field dimension
AXIS = {"T": "t", "C": "c", "S": "c", "Z": "z", "Y": "y", "X": "x"}


def elements(im, path: str) -> list[dict[str, str]]:
    return [dict(e.attrib) for e in im.xml_element.findall(path)]


def expected_fields(im) -> tuple[list[str], np.ndarray]:
    """liffile's array of an image, its field dimensions first (in the file's order, the first
    fastest), then t, c, z, y, x as the image has them; BGR samples as stored."""
    sizes = dict(im.sizes)  # before asarray, which reads padded rows only once sizes has found them
    data = np.asarray(im.asarray())
    if im._is_bgr:  # liffile gives BGR samples as RGB; the convention keeps the bytes' order
        data = data[..., ::-1]
    dims = list(sizes)
    fields = [d for d in dims if d not in AXIS]
    axes = [d for d in dims if d in AXIS]
    order = sorted(axes, key=lambda d: "tczyx".index(AXIS[d]))
    data = np.transpose(data, [dims.index(d) for d in reversed(fields)] + [dims.index(d) for d in order])
    return [AXIS[d] for d in order], data.reshape(-1, *data.shape[len(fields):])


def micrometers(dim: dict[str, str]) -> float:
    return abs(float(dim["Length"]) / (int(dim["NumberOfElements"]) - 1)) * (1 / 1e-6)


@pytest.mark.parametrize("path", LIFS, ids=lambda p: p.name)
def test_matches_liffile_and_rebuilds(path, tmp_path):
    """Each image's fields are series whose arrays are liffile's data, in its type; their source
    metadata is the XML's (spec/zarrify/lif.md §5.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#52-the-images>), their scales
    the dimensions' steps and their windows the channels' resolutions; the file is rebuilt from
    the mirror."""
    out, root, _ = zarrified(path, tmp_path, "lif")
    meta = mapping(at(root.attrs, "refkv_zarrified", "lif"))
    series = [string(s) for s in sequence(at(root.get_group("OME").attrs, "ome", "series"))] if "OME" in root else []
    with liffile.LifFile(path) as f:
        images = list(f.images)
        kind = {".lif": {}, ".lof": {"file": "lof"}, ".lifext": {"file": "lifext"}}[path.suffix]
        assert meta == {"version": f.version, **kind}
        assert out.summary["images"] == len(images)
        s = 0
        for k, im in enumerate(images):
            axes, want = expected_fields(im)
            dims = elements(im, "./Data/Image/ImageDescription/Dimensions/DimensionDescription")
            channels = elements(im, "./Data/Image/ImageDescription/Channels/ChannelDescription")
            for field, values in enumerate(want):
                group = root.get_group(series[s])
                arr = group.get_array("0")
                own = mapping(at(group.attrs, "refkv_zarrified", "lif"))
                assert own["image"] == k
                # liffile gives the element of an older LOF's XML the file's name; it has none
                attributes = {} if path.name == "lif_lof_data.lof" else dict(im.xml_element.attrib)
                assert own["attributes"] == attributes
                assert own["dimensions"] == dims
                assert own["channels"] == channels
                assert own.get("memory") == next(iter(elements(im, "./Memory")), None)
                # liffile's path of a LIFEXT image starts with the MemoryBlockID it belongs to
                assert own.get("parent") == (im.path.split("/")[0] if path.suffix == ".lifext" else None)
                assert ("field" in own) == (len(want) > 1)
                got = ndarray(arr[...])
                assert got.dtype == want.dtype
                assert np.array_equal(got.reshape(values.shape), values, equal_nan=True), (series[s], field)
                stored = sequence(json.loads(out.bytes_entries[f"{series[s]}/0/zarr.json"])["dimension_names"])
                assert [a for a in stored if a != "y" or "y" in axes] == axes
                ome = mapping(at(group.attrs, "ome"))
                transform = at(ome, "multiscales", 0, "datasets", 0, "coordinateTransformations", 0)
                scale = [number(v) for v in sequence(at(transform, "scale"))]
                names = [string(at(a, "name")) for a in sequence(at(ome, "multiscales", 0, "axes"))]
                for d in dims:
                    axis = {"1": "x", "2": "y", "3": "z", "4": "t"}.get(d["DimID"])
                    if axis is None or int(d["NumberOfElements"]) < 2 or float(d["Length"]) == 0:
                        continue
                    step = micrometers(d) if axis != "t" else abs(float(d["Length"]) / (int(d["NumberOfElements"]) - 1))
                    assert math.isclose(scale[names.index(axis)], step, rel_tol=1e-12), (axis, scale)
                windows = [mapping(c).get("window") for c in sequence(at(ome, "omero", "channels"))]
                bits = sorted((int(c["BytesInc"]), int(c["Resolution"])) for c in channels)
                full = [{"min": 0, "max": (1 << b) - 1, "start": 0, "end": (1 << b) - 1} for _, b in bits]
                assert windows == (full if want.dtype.kind == "u" else [None] * len(bits))
                if "tile" in own:
                    tiles = im.xml_element.findall("./Data/Image/Attachment[@Name='TileScanInfo']/Tile")
                    assert own["tile"] == dict(tiles[field].attrib)
                s += 1
        assert s == len(series) == out.summary["series"]


def test_a_lifext_file_presents_the_images_of_every_parent(tmp_path):
    """Each `ChildrenOf` of a LIFEXT file (spec/zarrify/lif.md §2.6
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#26-lifext-files>) gives images, each
    with its parent's `MemoryBlockID`. liffile reads only the first, so the writer's arrays are the
    expected ones."""
    path = FIXTURES / "lif" / "edge_lif_lifext_two_parents.lifext"
    _, root, _ = zarrified(path, tmp_path, "lif")
    lifext = accepted_lifext()["edge_lif_lifext_two_parents"]
    for s, (parent, im) in enumerate((parent, im) for parent, images in lifext.children.items() for im in images):
        group = root.get_group(str(s))
        assert at(group.attrs, "refkv_zarrified", "lif", "parent") == parent
        assert at(group.attrs, "refkv_zarrified", "lif", "path") == [im.name]
        assert np.array_equal(ndarray(group.get_array("0")[...]), im.array())


def test_a_lof_reads_its_parts_not_its_pixels(tmp_path):
    """A LOF's XML, after the data (spec/zarrify/lif.md §7.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#72-reading>), is read in one more
    read, of the file's end, however large the data is; the mirror's description of the file then
    rebuilds it."""
    big = tmp_path / "big.lof"
    big.write_bytes(Lof(Img("big", "xy", {"x": 2048, "y": 2048})).build())
    out, root, _ = zarrified(big, tmp_path, "lif")
    assert at(out.summary, "planner", "batches") == 2
    assert integer(at(out.summary, "planner", "bytes")) < (1 << 16) + (1 << 14)
    with liffile.LifFile(big) as f:
        assert np.array_equal(ndarray(root.get_array("0/0")[...]), f.images[0].asarray())


LIF = FIXTURES / "lif"
# each accepted project: its named file, its images' paths, and those it does not present
PROJECTS = [
    ("lif_xlef_tree.xlef", [["lif_xlef_tree", "Series1"], ["lif_xlef_tree", "Group A", "Series 2"]], 0),
    ("lif_xlef_lowercase.xlef", [["lif_xlef_lowercase", "Series1"], ["lif_xlef_lowercase", "Group A", "Series 2"]], 0),
    ("lif_xlef_frames.xlef", [["lif_xlef_frames", "Solo"]], 2),
    ("lif_xlef_cycle.xlef", [["lif_xlef_cycle", "Back", "Solo"]], 0),
    ("lif_xlef_chain.xlef", [], 0),
    ("lif_xlcf.xlcf", [["lif_xlcf", "Solo"]], 0),
    ("lif_xlif.xlif", [["Solo"]], 0),
    ("lif_xllf.xllf", [["lif_xllf", "Solo"]], 0),
]


def project_output(name: str) -> tuple[StoreIrOutput, zarr.Group]:
    fmt, out = zarrify(str(LIF / name))
    policy = None
    assert fmt == "lif"
    assert isinstance(out, StoreIrOutput)
    return out, zarr.open_group(refkv.zarr.store(out.to_store(policy=policy)), mode="r", zarr_format=3)


@pytest.mark.parametrize(("name", "paths", "left_out"), PROJECTS, ids=lambda v: v if isinstance(v, str) else "")
def test_a_project_matches_liffile_and_rebuilds(name, paths, left_out):
    """A project's images are, in the order its documents name one another, each at its path in the
    element tree, with the element's attributes from its document and its array from its LOF
    (spec/zarrify/lif.md §8.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#84-the-images>).
    liffile, where it can read the project, gives the same arrays. Every file is an object of the
    mirror at the key it has, whatever case its name is written in: a document held, and a LOF
    described in place, a source of the archive when a chunk references it."""
    out, root = project_output(name)
    meta = mapping(at(root.attrs, "refkv_zarrified", "lif"))
    assert meta["file"] == "set"
    assert meta["version"] == 2
    assert len(sequence(meta.get("not_presented", []))) == left_out
    for r in sequence(meta.get("not_presented", [])):
        assert string(at(r, "reason"))
    series = [string(s) for s in sequence(at(root.get_group("OME").attrs, "ome", "series"))] if "OME" in root else []
    assert len(series) == len(paths)
    groups = [root.get_group(s) for s in series]
    own = [mapping(at(g.attrs, "refkv_zarrified", "lif")) for g in groups]
    assert [o["path"] for o in own] == paths
    assert [o["image"] for o in own] == list(range(len(paths)))
    if name != "lif_xlef_cycle.xlef" and left_out == 0:  # liffile follows a cycle forever, and opens every frame
        with liffile.LifFile(LIF / name) as f:
            images = list(f.images)
            assert len(images) == len(paths)
            for im, group, o in zip(images, groups, own, strict=True):
                # through its LOF: liffile flips BGR samples read through a document, and does not say so
                # pyrefly: ignore[missing-attribute]
                _, want = expected_fields(im._lof_reference.images[0])
                got = ndarray(group.get_array("0")[...])
                assert got.dtype == want.dtype
                assert np.array_equal(got.reshape(want[0].shape), want[0])
                assert o["attributes"] == dict(im.xml_element.attrib)
                assert o["memory"] == elements(im, "./Memory")[0]
                assert o["dimensions"] == elements(im, "./Data/Image/ImageDescription/Dimensions/DimensionDescription")
    rebuilt = _core.rebuild_store(out.entries.get)
    assert {k for k, _, _ in rebuilt} == set(out.sizes)
    assert set(sequence(meta["files"])) == {k for k, _, _ in rebuilt} - {name}
    sources = out.table()[0]
    for key, size, held in rebuilt:
        data = (LIF / key).read_bytes()
        assert size == len(data), key
        if key.endswith(".lof"):
            assert held is None, key
        else:
            assert held == data, key
    for o in own:
        assert at(o, "attributes", "Name") == sequence(o["path"])[-1]
    # every LOF is a source, of an image's chunks or at refkv_source/objects/; no document is
    lofs = {object_url(out.url, k) for k, _, _ in rebuilt if k.endswith(".lof")}
    assert {u for u in sources if u is not None} == lofs


def test_a_project_is_read_with_the_keys_its_files_have(tmp_path):
    """Names written in another case than the files have find them by listing the directories
    (spec/zarrify/lif.md §8.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#82-the-siblings>);
    an output names the files as they are, in its sources and its mirror."""
    out, root = project_output("lif_xlef_lowercase.xlef")
    keys = set(out.sizes)
    assert "_lif_xlef_lowercase_/Metadata/Series1.xlif" in keys
    assert "_lif_xlef_lowercase_/Series1/Metadata/FrameProperties.xlif" in keys
    assert "_lif_xlef_lowercase_/Group A/Series 2.lof" in keys
    assert all(k == k.replace("metadata", "Metadata") for k in keys)
    assert len(sequence(at(root.attrs, "refkv_zarrified", "lif", "files"))) == len(keys) - 1
    # a directory of a copy of the project, moved: its key is relative to the named file
    other = tmp_path / "a b"
    other.mkdir()
    for k in keys:
        (other / k).parent.mkdir(parents=True, exist_ok=True)
        (other / k).write_bytes((LIF / k).read_bytes())
    _, moved = zarrify(str(other / "lif_xlef_lowercase.xlef"), url="https://data.test/set/lif_xlef_lowercase.xlef")
    assert isinstance(moved, StoreIrOutput)
    assert set(moved.sizes) == keys


def test_folding_and_listing_are_the_core_s():
    """The hosts' twin of refkv_ir::beside: simple case folding, an exact entry first."""
    assert folded("Series010.XLIF") == "series010.xlif"
    assert folded("\u00c9") == folded("\u00e9")
    assert folded("stra\u00dfe") != folded("strasse")
    assert folded("\u0131") != folded("i")
    tree = {"": ["Metadata", "both.lof", "BOTH.lof"], "Metadata": ["Series010.xlif"]}
    assert found_key("metadata/SERIES010.xlif", tree.get) == "Metadata/Series010.xlif"
    assert found_key("both.lof", tree.get) == "both.lof"
    assert found_key("absent.lof", tree.get) is None
    assert found_key("both.lof/x", tree.get) is None


def test_two_entries_that_match_ignoring_case_are_rejected():
    """A file system that ignores case cannot hold such a pair, so a listing stands in for it."""
    tree = {"": ["Dir"], "Dir": ["A.lof", "a.LOF"]}
    with pytest.raises(Rejected, match=r'the file "dir/a\.lof" matches both "Dir/A\.lof" and "Dir/a\.LOF"'):
        found_key("dir/a.lof", tree.get)


def test_over_http_a_project_is_read_with_its_names_as_written(server):
    """A web server cannot list: the names written as the files have them are found, and the host
    lists nothing."""
    policy = Policy(allow_private_hosts=True)
    fmt, out = zarrify(server + "lif_xlef_tree.xlef", policy=policy)
    assert fmt == "lif"
    assert out.summary["series"] == 2
    assert out.url == server


def test_over_http_the_host_never_lists(server, monkeypatch):
    def refuse(_path):
        raise AssertionError("a remote input was listed")

    monkeypatch.setattr(Path, "iterdir", refuse)
    s = VsiFiles(server + "lif_xlef_tree.xlef", policy=Policy(allow_private_hosts=True), case=True)
    assert s.case is False
    assert s.probe("_lif_xlef_tree_/Series1.lof") is not None
    assert s.found_key("_lif_xlef_tree_/Series1.lof") == "_lif_xlef_tree_/Series1.lof"


def test_over_http_an_absent_name_says_to_download_the_set(server):
    with pytest.raises(Rejected, match="download the set and zarrify the local copy"):
        zarrify(server + "edge_reject_lif_xlef_absent.xlef", policy=Policy(allow_private_hosts=True))


def test_a_file_run_of_a_document_starts_over_as_a_store_run():
    """A document's bytes are the LIF profile's second signature (spec/zarrify/lif.md §7.1
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#71-detection>): a file run finds
    that it is one, and says so to the host."""
    with pytest.raises(NamesSiblingsError):
        via_ir(str(LIF / "lif_xlcf.xlcf"))


def test_bands_bound_a_chunk(tmp_path):
    """A band is the most rows, a divisor of the height, within 2^24 bytes, and for padded rows a
    reference payload of at most 65519 bytes (spec/zarrify/lif.md §3.4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#34-rows-and-bands>); the run
    reads the structure, not the pixels."""
    big = tmp_path / "big.lif"
    # 4097 rows of 4097 bytes: 16.8 MB, a plane past 2^24 bytes; 4097 = 17 x 241
    big.write_bytes(Lif("big.lif", [Img("big", "xy", {"x": 4097, "y": 4097})]).build())
    cases = [(big, [241, 4097], 17), (FIXTURES / "lif" / "lif_rgb_padded_tall.lif", [3, 4096, 2], 4)]
    for path, chunk_shape, chunks in cases:
        out, root, _ = zarrified(path, tmp_path, "lif")
        doc = json.loads(out.bytes_entries["0/0/zarr.json"])
        assert at(doc, "chunk_grid", "configuration", "chunk_shape") == chunk_shape
        assert out.summary["chunks"] == chunks
        assert integer(at(out.summary, "planner", "bytes")) < 1 << 17
        with liffile.LifFile(path) as f:
            _, want = expected_fields(f.images[0])
        assert np.array_equal(ndarray(root.get_array("0/0")[...]).reshape(want.shape[1:]), want[0])


def test_the_walk_reads_the_blocks_the_xml_predicts(tmp_path):
    """The blocks' headers are read at the offsets the XML's Memory elements predict (spec/zarrify/lif.md
    §7.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#72-reading>): blocks in the
    elements' order past the first read (64 KiB) take one round of requests, and each block the
    prediction misses (here a folder's empty one) one more; no pixel is read."""
    images = [Img(f"im{i}", "xy", {"x": 256, "y": 256}) for i in range(20)]
    cases = [(Lif("a.lif", images), 2), (Lif("b.lif", images, folder=("folder", 10)), 3)]
    for lif, batches in cases:
        path = tmp_path / lif.name
        path.write_bytes(lif.build())
        out, _, _ = zarrified(path, tmp_path, "lif")
        assert at(out.summary, "planner", "batches") == batches, lif.name
        assert integer(at(out.summary, "planner", "bytes")) < (1 << 16) + (1 << 14), lif.name
