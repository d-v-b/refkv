"""`vzip.virtualize.virtualize` returns a `vzip.ReferenceStore`, which reads in memory
as its saved archive reads, and whose `save` writes the archive the profile output's
own writer writes, byte for byte."""

import subprocess
import sys
from pathlib import Path

import pytest

import vzip
from vzip.pb import Range, Source
from vzip.policy import Policy
from vzip.virtualize import _virtualize, virtualize
from vzip.virtualize.common import reference_store

sys.path.insert(0, str(Path(__file__).parents[2] / "conformance" / "virtualize"))
from proxy import Proxy  # noqa: E402

FIXTURES = Path(__file__).parents[2] / "fixtures"
LOOPBACK = Policy(allow_private_hosts=True)  # the proxy serves the stores on 127.0.0.1


@pytest.fixture(scope="module")
def proxy(tmp_path_factory):
    return Proxy(FIXTURES, tmp_path_factory.mktemp("cache"))


# (fixture, a store, checksums, format): a TIFF through the Rust core, NIfTI, Imaris IMS,
# a Zarr v2 store, and a SAFE product as a store and as a zip file
INPUTS = [
    ("tiff/jpeg_gray.tif", False, False, "tiff"),
    ("tiff/jpeg_gray.tif", False, True, "tiff"),
    ("nifti/nifti_block_x_split.nii", False, False, "nifti"),
    ("ims/ims_latest_uint8.ims", False, False, "ims"),
    ("ims/ims_latest_uint8.ims", False, True, "ims"),
    ("zarr2/zarr2_hierarchy", True, False, "zarr2"),
    ("zarr2/zarr2_hierarchy", True, True, "zarr2"),
    ("safe/safe_l2a_pb0207", True, False, "safe"),
    ("safe/safe_l2a_pb0207.SAFE.zip", False, False, "safe"),
]


@pytest.mark.parametrize("name, is_store, checksums, fmt", INPUTS, ids=lambda v: str(v))
def test_virtualize_returns_the_store_it_saves(proxy, tmp_path, name, is_store, checksums, fmt):
    location, policy = (proxy.local_store(name), LOOPBACK) if is_store else (str(FIXTURES / name), None)
    store = virtualize(location, checksums=checksums, policy=policy)
    assert isinstance(store, vzip.ReferenceStore)
    _, out = _virtualize(location, checksums=checksums, policy=policy)
    assert store.info == {"format": fmt, **out.summary}

    store.save(tmp_path / "new.vzip", policy=policy)
    out.write(str(tmp_path / "old.vzip"))
    assert (tmp_path / "new.vzip").read_bytes() == (tmp_path / "old.vzip").read_bytes()

    saved = vzip.open(str(tmp_path / "new.vzip"), policy=policy)
    keys = store.list()
    assert keys == saved.list() and any(store.classify(k) == "reference" for k in keys)
    for k in keys:
        assert store.classify(k) == saved.classify(k), k
        assert store.get(k) == saved.get(k), k


@pytest.mark.parametrize("entries", [[("a", b"x", False)], []], ids=["entry", "reference"])
def test_reference_store_rejects_a_duplicate_key(entries):
    ref = ("a", [Range(source=0, offset=0, length=1)])
    with pytest.raises(ValueError, match="duplicate key 'a'"):
        reference_store([ref] + ([] if entries else [ref]), entries, [Source(url="https://data.test/a")])


def test_cli_refuses_to_save_sources_its_policy_refuses(tmp_path):
    out = tmp_path / "out.vzip"
    p = subprocess.run([sys.executable, "-m", "vzip.virtualize", str(FIXTURES / "nifti/nifti_block_x_split.nii"),
                        str(out), "--url", "http://127.0.0.1/x.nii"], capture_output=True, text=True)
    assert p.returncode == 1 and "error: not saving" in p.stderr and not out.exists(), p.stderr
