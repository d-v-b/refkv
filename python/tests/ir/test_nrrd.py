"""The NRRD IR: the Rust parser and its schema, the image projection (spec/zarrify/nrrd.md) against
pynrrd's reading of every accepted input, the mirror's rebuild of every file, the edge chunks
copied past a payload, and the host's side: a detached header's data file probed beside it, from
a local directory and over HTTP, where a missing file is a 404. NRRD has no frozen Python
reference; rust/refkv-ir/tests/nrrd.rs checks the layout's details and each rejection's message."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import nrrd
import numpy as np
import pytest
import zarr
from ir_helpers import FIXTURES, ROOT, core_setting

import refkv.zarr
from refkv import _core
from refkv._narrow import at, mapping, ndarray, sequence, string
from refkv.ir.store import StoreIrOutput
from refkv.policy import Policy
from refkv.zarrify import Rejected
from refkv.zarrify import _zarrify as zarrify

sys.path.insert(0, str(ROOT / "conformance" / "zarrify"))
from proxy import Proxy

NRRD = FIXTURES / "nrrd"
INPUTS = sorted(p for p in NRRD.iterdir() if p.suffix in (".nrrd", ".nhdr") and "reject" not in p.name)
URL = "https://data.test/nrrd/{}"
pytestmark = pytest.mark.filterwarnings("ignore:Duplicate header field")


@pytest.fixture(autouse=True)
def duplicate_keys(monkeypatch):
    """pynrrd reads nrrd_header_everything's repeated key, which NRRD allows (its last value
    counts), within each test alone."""
    monkeypatch.setattr(nrrd.reader, "ALLOW_DUPLICATE_FIELD", True)


def zarrified(path: Path, url: str | None = None, policy: Policy | None = None) -> tuple[StoreIrOutput, zarr.Group]:
    fmt, out = zarrify(str(path), url=url, policy=policy)
    assert fmt == "nrrd"
    assert isinstance(out, StoreIrOutput)
    return out, zarr.open_group(refkv.zarr.store(out.to_store(policy=policy)), mode="r", zarr_format=3)


def expected(path: Path, root: zarr.Group) -> np.ndarray:
    """pynrrd's array, its axes put in the image's order by the root's `axes` (each header axis's
    image axis, fastest first)."""
    data, _ = nrrd.read(str(path), index_order="C")  # the header's last axis first
    stored = [string(a) for a in reversed(sequence(at(root.attrs, "refkv_zarrified", "nrrd", "axes")))]
    names = [string(mapping(a)["name"]) for a in sequence(at(root.attrs, "ome", "multiscales", 0, "axes"))]
    return np.transpose(data, [stored.index(n) for n in names])


@pytest.mark.parametrize("path", INPUTS, ids=lambda p: p.name)
def test_inputs_match_pynrrd_and_rebuild(path, tmp_path):
    """Every accepted input's array, read back through zarr-python, is pynrrd's (values, data
    type, axes in the image's order); every file of the input is rebuilt from the mirror alone
    (the files are described in place: from their own bytes, nothing held)."""
    out, root = zarrified(path)
    got = ndarray(root.get_array("0")[...])
    want = expected(path, root)
    assert got.dtype == want.dtype.newbyteorder("=")
    assert got.shape == want.shape
    assert np.array_equal(got, want, equal_nan=got.dtype.kind == "f")
    rebuilt = _core.rebuild_store(out.entries.get)
    assert {k for k, _, _ in rebuilt} == set(out.sizes)
    for key, size, held in rebuilt:
        assert held is None, key
        assert size == (path.parent / key).stat().st_size, key
    out.write(str(tmp_path / "a.vzip"))


def test_edge_chunks_past_the_payload_are_copied():
    """An edge chunk whose reference (its range and its zero padding) does not fit in a payload
    is copied into the archive, padding included (spec/zarrify/nrrd.md §8.5
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#85-chunks>): with a payload
    limit of 8 bytes, the padded edge chunk of nrrd_chunk_edge.nrrd is copied, by the run's last
    batch, and the array is still pynrrd's."""
    path = NRRD / "nrrd_chunk_edge.nrrd"
    with core_setting(_core._set_copy_threshold, 8):
        out, root = zarrified(path)
    assert "0/c/2/0/0" in out.entries
    assert "0/c/2/0/0" not in out.refs
    assert np.array_equal(ndarray(root.get_array("0")[...]), expected(path, root))


def test_a_url_that_does_not_name_the_header_file_is_rejected():
    with pytest.raises(Rejected, match=re.escape("does not name the header file")):
        zarrify(str(NRRD / "nrrd_detached_raw.nhdr"), url=URL.format("other.nhdr"))


def test_a_url_with_a_query_is_rejected():
    with pytest.raises(Rejected, match=r"NRRD input .* has a query or a fragment"):
        zarrify(str(NRRD / "nrrd_detached_raw.nhdr"), url=URL.format("nrrd_detached_raw.nhdr?x"))


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """The fixtures served over HTTP by the harness proxy (ranges, and a 404 for a file that is
    not there): the directory's URL, ending in `/`."""
    return Proxy(NRRD, tmp_path_factory.mktemp("cache")).base + "f/"


def test_over_http_the_data_file_is_probed_beside_the_header(server, tmp_path):
    """The host opens the data file at the header's directory (a directory below it here): the
    archive's sources are the two files, and the root names the header."""
    policy = Policy(allow_private_hosts=True)
    path = NRRD / "nrrd_detached_gzip.nhdr"
    fmt, out = zarrify(server + path.name, policy=policy)
    assert fmt == "nrrd"
    assert isinstance(out, StoreIrOutput)
    assert set(out.sizes) == {"nrrd_detached_gzip.nhdr", "nrrd_detached_gzip_files/data.raw.gz"}
    assert out.url == server
    assert out.summary["listingRequests"] == 1
    out.write(str(tmp_path / "a.vzip"))
    g = zarr.open_group(refkv.zarr.open(str(tmp_path / "a.vzip"), policy=policy), mode="r", zarr_format=3)
    assert at(g.attrs, "refkv_zarrified", "source") == {"url": server + path.name}
    assert np.array_equal(ndarray(g.get_array("0")[...]), expected(path, g))


def test_over_http_a_missing_data_file_is_rejected(server):
    with pytest.raises(Rejected, match=re.escape('the data file "image.raw" does not exist')):
        zarrify(server + "nrrd_reject_detached_missing.nhdr", policy=Policy(allow_private_hosts=True))
