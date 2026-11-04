"""The LIF IR: the Rust parser and its schema, the image projection (spec/zarrify/lif.md)
against liffile's reading of every synthetic LIF, LOF and LIFEXT file, the mirror's rebuild, and the
bands of a plane past 2^24 bytes. LIF has no frozen Python reference: liffile is the independent reader. The
inputs the profile rejects are conformance/zarrify/rejects/lif.json's (test_rejects.py)."""

import json
import math
import sys

import liffile
import numpy as np
import pytest
from ir_helpers import FIXTURES, ROOT, zarrified

from refkv._narrow import at, integer, mapping, ndarray, number, sequence, string

sys.path.insert(0, str(ROOT / "fixtures" / "generators" / "lif"))
# pyrefly: ignore[missing-import]  # on sys.path above; the config's search-path lacks fixtures/generators
from write_fixtures import Img, Lif, Lof, accepted_lifext  # the fixtures' writer, for a file too large to commit

LIFS = sorted(p for s in ("lif", "lof", "lifext") for p in (FIXTURES / "lif").glob(f"lif_*.{s}"))
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
