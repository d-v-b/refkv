"""A TIFF image across files (spec/zarrify/tiff.md §7.4
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#74-several-files>) and a Micro-Manager
stack (spec/zarrify/tiff.md §7.5
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#75-micro-manager-stacks>): each input's
array read back through zarr-python, its sources and its mirror, from a local directory and over
HTTP. The rejections are in conformance/zarrify/rejects/tiff.json."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
import zarr
from ir_helpers import FIXTURES, ROOT

import refkv.zarr
from refkv import _core
from refkv._narrow import at, integer, ndarray, sequence
from refkv.ir.store import StoreIrOutput
from refkv.policy import Policy
from refkv.zarrify import Rejected
from refkv.zarrify import _zarrify as zarrify

sys.path.insert(0, str(ROOT / "fixtures" / "generators" / "tiff"))
sys.path.insert(0, str(ROOT / "conformance" / "zarrify"))
# pyrefly: ignore[missing-import]  # on sys.path above; the config's search-path lacks fixtures/generators
import write_set_fixtures
from proxy import Proxy

# pyrefly: ignore[missing-import]  # on sys.path above; the config's search-path lacks fixtures/generators
from write_set_fixtures import plane

TIFF = FIXTURES / "tiff"
ALL_OF_IT = np.stack([plane(k) for k in range(8)]).reshape(2, 2, 2, 8, 16).transpose(0, 2, 1, 3, 4)  # (t, c, z, y, x)


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """The fixtures served over HTTP by the harness proxy (ranges, and a 404 for a file that is
    not there): the directory's URL, ending in `/`."""
    return Proxy(TIFF, tmp_path_factory.mktemp("cache")).base + "f/"


def stack(n: int) -> np.ndarray:
    return np.stack([plane(k) for k in range(n)])


def read(out, tmp_path: Path, directory: Path = TIFF) -> zarr.Group:
    """The saved `out`, which reads the files of `directory`."""
    out.write(str(tmp_path / "a.vzip"))
    policy = Policy(prefixes=(directory.as_uri() + "/",))
    return zarr.open_group(refkv.zarr.open(str(tmp_path / "a.vzip"), policy=policy), mode="r", zarr_format=3)


@pytest.mark.parametrize(("name", "files", "want"), [
    ("ome_set_a.ome.tif", ["ome_set_a.ome.tif", "ome_set_b.ome.tif"], stack(5)),
    ("ome_set_c.ome.tif", ["ome_set_c.ome.tif", "ome_set_plain.tif"], stack(2)),
    ("mm_split_MMStack_Pos0.ome.tif", ["mm_split_MMStack_Pos0.ome.tif", "mm_split_MMStack_Pos0_1.ome.tif"], ALL_OF_IT),
    ("mm_stopped_MMStack_Pos0.ome.tif", ["mm_stopped_MMStack_Pos0.ome.tif"], None),
])
def test_an_image_across_files_is_read_as_a_store(name, files, want, tmp_path):
    """Its array is the planes' wherever they lie; every file is an object the mirror describes in
    place, from its own bytes, and each that holds a plane a source of the archive."""
    fmt, out = zarrify(str(TIFF / name))
    assert fmt == "tiff"
    assert isinstance(out, StoreIrOutput)
    assert set(out.sizes) == set(files)
    rebuilt = _core.rebuild_store(out.entries.get)
    assert {k for k, _, _ in rebuilt} == set(files)
    assert all(held is None for _, _, held in rebuilt)
    root = read(out, tmp_path)
    assert at(root.attrs, "refkv_zarrified", "tiff", "files") == files[1:]
    got = ndarray(root.get_array("0")[...])
    if want is not None:
        assert np.array_equal(got, want)
    else:  # the planes the stopped acquisition did not reach stay at the fill value
        assert np.array_equal(got[0, 0], plane(0))
        assert np.array_equal(got[1, 0], plane(1))
        assert np.array_equal(got[0, 1], plane(2))
        assert not got[1, 1].any()


def test_a_stack_in_one_file_stays_a_file_input(tmp_path):
    """A stack its own index map covers probes nothing, and its mirror is a file IR's."""
    fmt, out = zarrify(str(TIFF / "mm_single_MMStack_Pos0.ome.tif"))
    assert fmt == "tiff"
    assert not isinstance(out, StoreIrOutput)
    root = read(out, tmp_path)
    assert np.array_equal(ndarray(root.get_array("0")[...]), ALL_OF_IT)
    micromanager = at(root.attrs, "refkv_zarrified", "tiff", "micromanager")
    assert at(micromanager, "UUID") == "390dd25c-dc45-45a8-b421-44772f3de4f4"
    axes = sequence(at(root.attrs, "ome", "multiscales", 0, "axes"))
    assert [at(a, "name") for a in axes] == ["t", "c", "z", "y", "x"]
    channels = sequence(at(root.attrs, "ome", "omero", "channels"))
    assert [at(c, "label") for c in channels] == ["BF", "GFP"]


def test_over_http_the_siblings_are_probed_beside_the_input(server, tmp_path):
    """The archive's sources are the files' URLs, and the root names the file it was given."""
    policy = Policy(allow_private_hosts=True)
    name = "mm_split_MMStack_Pos0.ome.tif"
    fmt, out = zarrify(server + name, policy=policy)
    assert fmt == "tiff"
    assert isinstance(out, StoreIrOutput)
    assert out.url == server
    out.write(str(tmp_path / "a.vzip"))
    g = zarr.open_group(refkv.zarr.open(str(tmp_path / "a.vzip"), policy=policy), mode="r", zarr_format=3)
    assert at(g.attrs, "refkv_zarrified", "source") == {"url": server + name}
    assert np.array_equal(ndarray(g.get_array("0")[...]), ALL_OF_IT)


def test_a_stack_of_many_planes_is_not_an_ifd_a_request(tmp_path, monkeypatch):
    """Its index map names its IFDs, read in one batch rather than one a round down the chain."""
    monkeypatch.setattr(write_set_fixtures, "OUT", tmp_path)
    planes = [plane(k) for k in range(200)]
    summary = {"Channels": 1, "Slices": 1, "Frames": 200, "UUID": "u"}
    index = [(0, 0, t, 0) for t in range(200)]
    name = "many_MMStack_Pos0.ome.tif"
    write_set_fixtures.write(name, planes[:150], summary=summary, index=index[:150])
    write_set_fixtures.write("many_MMStack_Pos0_1.ome.tif", planes[150:], summary=summary, index=index[150:])
    _, out = zarrify(str(tmp_path / name))
    assert isinstance(out, StoreIrOutput)
    assert integer(at(out.summary, "reads", "batches")) < 40
    assert np.array_equal(ndarray(read(out, tmp_path, tmp_path).get_array("0")[...]), np.stack(planes))


def test_a_url_of_the_directory_names_the_siblings_there():
    """With `--url`, every object's URL is below the URL the named file is served from."""
    name = "ome_set_a.ome.tif"
    _, out = zarrify(str(TIFF / name), f"https://data.test/x/{name}")
    assert isinstance(out, StoreIrOutput)
    assert sorted(out.table()[0][:2]) == ["https://data.test/x/ome_set_a.ome.tif", "https://data.test/x/ome_set_b.ome.tif"]


def test_a_sibling_outside_the_store_is_rejected():
    with pytest.raises(Rejected, match=r'file name "../secret.tif" is not a path below'):
        zarrify(str(TIFF / "edge_reject_ome_set_outside.ome.tif"))
