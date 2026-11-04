"""Files gzipped whole (spec/zarrify/nifti.md §7.4
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#74-gzipped-files>,
spec/zarrify/mrc.md §8.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mrc.md#84-gzipped-files>):
each presents the array its plain file presents, in one chunk of the whole stream. Their rejections
are in conformance/zarrify/rejects/."""

import gzip
from typing import Any

import numpy as np
import pytest
import zarr
from ir_helpers import FIXTURES, zarrified

NIFTI = sorted(p for p in (FIXTURES / "nifti").glob("*.nii.gz") if "reject" not in p.name)
MRC = sorted(p for p in (FIXTURES / "mrc").glob("*.gz") if "reject" not in p.name)
CASES = [*[(p, "nifti", "nifti") for p in NIFTI], *[(p, "mrc", "mrc") for p in MRC]]


def array_of(root: zarr.Group) -> zarr.Array:
    array = root["0"]
    assert isinstance(array, zarr.Array)
    return array


@pytest.mark.parametrize(("path", "fmt", "member"), CASES, ids=lambda c: getattr(c, "name", c))
def test_presents_what_the_plain_file_presents_in_one_chunk(path, fmt, member, tmp_path):
    plain = tmp_path / path.name.removesuffix(".gz")
    plain.write_bytes(gzip.decompress(path.read_bytes()))
    _, want_root, _ = zarrified(plain, tmp_path, fmt)
    _, got_root, _ = zarrified(path, tmp_path, fmt)

    want, got = array_of(want_root), array_of(got_root)
    assert got.shape == want.shape
    assert got.dtype == want.dtype
    assert got.chunks == got.shape
    assert np.array_equal(got[...], want[...], equal_nan=True)

    got_attrs: Any = dict(got_root.attrs)
    want_attrs: Any = dict(want_root.attrs)
    assert got_attrs["ome"] == want_attrs["ome"]
    source = got_attrs["refkv_zarrified"][member]
    gzipped = source.pop("gzip")
    assert source == want_attrs["refkv_zarrified"][member]

    metadata: Any = got.metadata.to_dict()
    codecs = metadata["codecs"]
    assert [c["name"] for c in codecs[-3:]] == ["bytes", "refkv.skip", "gzip"]
    assert codecs[-2]["configuration"]["length"] == gzipped["skip"]
    want_metadata: Any = want.metadata.to_dict()
    assert codecs[:-2] == want_metadata["codecs"]


def test_a_gzipped_files_extensions_are_in_the_source_metadata(tmp_path):
    path = FIXTURES / "nifti" / "nifti_gzip_n1_be_ext.nii.gz"
    _, root, _ = zarrified(path, tmp_path, "nifti")
    attrs: Any = dict(root.attrs)
    gzipped = attrs["refkv_zarrified"]["nifti"]["gzip"]
    assert gzipped["extender"] == [1, 0, 0, 0]
    assert gzipped["extensions"] == [{"esize": 32, "ecode": 6, "data": "a comment in the stream"}]
    assert gzipped["skip"] == 384
