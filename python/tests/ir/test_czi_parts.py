"""A CZI image across numbered files (spec/zarrify/czi.md §7.9
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#79-several-files>): each input's array
read back through zarr-python, its sources and its mirror, from a local directory and over HTTP. The
rejections are in conformance/zarrify/rejects/czi.json."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
import zarr
from ir_helpers import FIXTURES, ROOT

import refkv.zarr
from refkv import _core
from refkv._narrow import at, mapping, ndarray
from refkv.ir.store import StoreIrOutput
from refkv.policy import Policy
from refkv.zarrify import Rejected
from refkv.zarrify import _zarrify as zarrify
from refkv.zarrify.store.beside import VsiFiles

sys.path.insert(0, str(ROOT / "fixtures" / "generators" / "czi"))
sys.path.insert(0, str(ROOT / "conformance" / "zarrify"))
# pyrefly: ignore[missing-import]  # on sys.path above; the config's search-path lacks fixtures/generators
import write_fixtures
from proxy import Proxy

# pyrefly: ignore[missing-import]  # on sys.path above; the config's search-path lacks fixtures/generators
from write_fixtures import part_plane

CZI = FIXTURES / "czi"


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """The fixtures served over HTTP by the harness proxy (ranges, and a 404 for a file that is
    not there): the directory's URL, ending in `/`."""
    return Proxy(CZI, tmp_path_factory.mktemp("cache")).base + "f/"


def planes(n: int) -> np.ndarray:
    return np.stack([part_plane(k) for k in range(n)])


def read(out, tmp_path: Path) -> zarr.Group:
    out.write(str(tmp_path / "a.vzip"))
    return zarr.open_group(refkv.zarr.open(str(tmp_path / "a.vzip")), mode="r", zarr_format=3)


@pytest.mark.parametrize(("name", "parts", "want"), [
    ("czi_parts_cover.czi", ["czi_parts_cover(1).czi"], planes(4)),
    ("czi_parts_own.czi", ["czi_parts_own(1).czi", "czi_parts_own(2).czi"], planes(4)),
    ("czi_parts_walk.czi", ["czi_parts_walk(1).czi"], planes(2)),
    ("czi_parts_spaced.czi", ["czi_parts_spaced (1).czi"], planes(2)),
    ("czi_parts_both.czi", ["czi_parts_both(1).czi"], planes(3)),
    ("czi_parts_primary.czi", ["czi_parts_primary(1).czi"], np.hstack([part_plane(0), part_plane(1)])),
    ("czi_parts_stopped.czi", [], planes(2)),
])
def test_an_image_across_files_is_read_as_a_store(name, parts, want, tmp_path):
    """Its array is the subblocks' wherever they lie; every file is an object the mirror describes
    in place, from its own bytes, and each that holds a subblock a source of the archive."""
    fmt, out = zarrify(str(CZI / name))
    assert fmt == "czi"
    assert isinstance(out, StoreIrOutput)
    assert set(out.sizes) == {name, *parts}
    rebuilt = _core.rebuild_store(out.entries.get)
    assert {k for k, _, _ in rebuilt} == {name, *parts}
    assert all(held is None for _, _, held in rebuilt)
    assert all(size == (CZI / key).stat().st_size for key, size, _ in rebuilt)
    root = read(out, tmp_path)
    assert at(root.attrs, "refkv_zarrified", "czi", "parts") == parts
    assert np.array_equal(ndarray(root.get_array("0/0")[...]), want)


def test_a_complete_file_stays_a_file_input(tmp_path):
    """A file whose directory has every plane its XML says probes nothing, and its mirror is a file
    IR's; so is one whose XML gives no size."""
    for name in ("czi_gray8_single.czi", "czi_parts_stranger(1).czi"):
        fmt, out = zarrify(str(CZI / name))
        assert fmt == "czi"
        assert not isinstance(out, StoreIrOutput)
        assert "parts" not in mapping(at(read(out, tmp_path).attrs, "refkv_zarrified", "czi"))


@pytest.mark.parametrize(("name", "probes"), [
    ("czi_gray8_single.czi", []),
    ("czi_parts_own.czi", ["czi_parts_own(1).czi", "czi_parts_own(2).czi"]),
    ("czi_parts_spaced.czi", ["czi_parts_spaced(1).czi", "czi_parts_spaced (1).czi"]),
    ("czi_parts_stopped.czi", ["czi_parts_stopped(1).czi", "czi_parts_stopped (1).czi"]),
])
def test_a_run_probes_only_what_the_file_calls_for(name, probes, monkeypatch):
    """The second spelling only when the first is absent, nothing past the planes found or the
    first absent, and nothing for a complete file."""
    asked = []
    probe = VsiFiles.probe

    def recorded(self, key):
        asked.append(key)
        return probe(self, key)

    monkeypatch.setattr(VsiFiles, "probe", recorded)
    zarrify(str(CZI / name))
    assert asked == probes


def test_the_probes_stop_when_the_planes_are_found(tmp_path):
    """A third file beside two that hold every plane is not read: it is not an object."""
    sources = {"a.czi": "czi_parts_spaced.czi", "a(1).czi": "czi_parts_spaced (1).czi",
               "a(2).czi": "czi_gray8_single.czi"}
    for name, source in sources.items():
        (tmp_path / name).write_bytes((CZI / source).read_bytes())
    _, out = zarrify(str(tmp_path / "a.czi"))
    assert isinstance(out, StoreIrOutput)
    assert set(out.sizes) == {"a.czi", "a(1).czi"}


def test_over_http_the_parts_are_probed_beside_the_input(server, tmp_path):
    """The archive's sources are the files' URLs, and the root names the file it was given."""
    policy = Policy(allow_private_hosts=True)
    name = "czi_parts_spaced.czi"
    fmt, out = zarrify(server + name, policy=policy)
    assert fmt == "czi"
    assert isinstance(out, StoreIrOutput)
    assert out.url == server
    out.write(str(tmp_path / "a.vzip"))
    g = zarr.open_group(refkv.zarr.open(str(tmp_path / "a.vzip"), policy=policy), mode="r", zarr_format=3)
    assert at(g.attrs, "refkv_zarrified", "source") == {"url": server + name}
    assert np.array_equal(ndarray(g.get_array("0/0")[...]), planes(2))


def test_a_url_of_the_directory_names_the_parts_there():
    """With `--url`, every object's URL is below the URL the named file is served from."""
    name = "czi_parts_walk.czi"
    _, out = zarrify(str(CZI / name), f"https://data.test/x/{name}")
    assert isinstance(out, StoreIrOutput)
    assert sorted(out.table()[0][:2]) == ["https://data.test/x/czi_parts_walk(1).czi", "https://data.test/x/czi_parts_walk.czi"]


def test_a_file_that_names_more_parts_than_a_run_probes_is_rejected(tmp_path):
    """Nothing is probed: the file says at once that it names too many."""
    p = write_fixtures.part(70000, 1)
    p.build()
    data = write_fixtures.master(0, planes=2, more_entries=write_fixtures.named_by_master(p, 70000, 0)).build()
    (tmp_path / "a.czi").write_bytes(data)
    with pytest.raises(Rejected, match=r"budget: the input names more than 65536 files"):
        zarrify(str(tmp_path / "a.czi"))
