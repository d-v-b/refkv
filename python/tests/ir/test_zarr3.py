"""Zarr v3 stores, listed and zipped, through the Rust IR (rust/refkv-ir/src/zarr3.rs;
spec/zarrify/zarr3.md), against zarr-python, the independent reader: every array the archive
presents reads, through refkv.zarr and zarr-python, as zarr-python reads it from the store
itself, with the same metadata and attributes but the convention's declaration. The store is
read from a copy without its empty objects, which the profile reads as absent chunks and
zarr-python fails on; a zip file is extracted, the last entry of a name winning, as Python's
zipfile reads it.

The hierarchies' structure, the attributes moved, the nodes left out and the mirror are tested
in rust/refkv-ir/tests/zarr3.rs; the rejected fixtures in test_rejects.py; the browser code's
output against this one by conformance/zarrify/compare.py.
"""

import json
import shutil
import sys
import zipfile
from pathlib import Path

import numpy as np
import pytest
import zarr
from zarr.storage import LocalStore

import refkv.zarr
from refkv._narrow import ndarray
from refkv.policy import Policy
from refkv.zarrify import _zarrify as zarrify

sys.path.insert(0, str(Path(__file__).parents[3] / "conformance" / "zarrify"))
from proxy import Proxy

FIXTURES = Path(__file__).parents[3] / "fixtures" / "zarr3"
DECLARATION = ("zarr_conventions", "refkv_zarrified")
# a codec no reader has, which the profile passes through: zarr-python cannot read either side
UNKNOWN_CODEC = "custom"

INPUTS = ["zarr3_hierarchy", "zarr3_codecs", "zarr3_scalar_root", "zarr3_rectilinear", "zarr3_left_out",
          "zarr3_hierarchy.zip", "zarr3_deflated_documents.zip", "zarr3_compressed_chunks.zip"]


def source(name: str, tmp: Path) -> LocalStore:
    path, copy = FIXTURES / name, tmp / "source"
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as z:
            z.extractall(copy)
    else:
        shutil.copytree(path, copy)
    for f in copy.rglob("*"):
        if f.is_file() and f.stat().st_size == 0:
            f.unlink()
    return LocalStore(copy, read_only=True)


@pytest.mark.parametrize("name", INPUTS)
def test_the_arrays_read_as_zarr_python_reads_the_store(name, tmp_path):
    base = Proxy(FIXTURES.parents[1], tmp_path).base
    policy = Policy(allow_private_hosts=True)
    url = f"{base}f/fixtures/zarr3/{name}" + ("" if name.endswith(".zip") else "/")
    fmt, out = zarrify(url, policy=policy)
    assert fmt == "zarr3"
    got, want = refkv.zarr.store(out.to_store(policy=policy)), source(name, tmp_path)
    out.write(str(tmp_path / "a.vzip"))
    with zipfile.ZipFile(tmp_path / "a.vzip") as z:
        paths = [n.removesuffix("zarr.json").rstrip("/") for n in z.namelist() if n.endswith("zarr.json")
                 and not n.startswith("refkv_source/") and json.loads(z.read(n))["node_type"] == "array"]
    paths = [p for p in paths if p != UNKNOWN_CODEC]
    assert paths
    for path in paths:
        a = zarr.open_array(got, path=path, mode="r", zarr_format=3)
        b = zarr.open_array(want, path=path, mode="r", zarr_format=3)
        assert (a.shape, a.dtype, a.metadata.chunk_grid, a.metadata.codecs) == \
               (b.shape, b.dtype, b.metadata.chunk_grid, b.metadata.codecs), path
        assert {k: v for k, v in a.attrs.items() if k not in DECLARATION} == dict(b.attrs), path
        x, y = ndarray(a[...]), ndarray(b[...])
        if x.dtype.kind in "TO":
            x, y = x.astype(object), y.astype(object)
        assert np.array_equal(x, y, equal_nan=x.dtype.kind in "fc"), path


def test_a_local_directory_still_needs_the_url_it_is_served_from():
    """A store's object URLs are the store's (spec/zarrify.md §1.4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>): a directory has
    none of its own, so it is named by the URL it will be served from; a zip file, a file input,
    needs none."""
    with pytest.raises(ValueError, match="a local directory needs url"):
        zarrify(str(FIXTURES / "zarr3_hierarchy"))
    fmt, out = zarrify(str(FIXTURES / "zarr3_hierarchy.zip"))
    assert fmt == "zarr3"
    assert out.url.endswith("zarr3_hierarchy.zip")
