"""An input's scope, what it may read besides itself (spec/zarrify.md §1.9
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#19-sources-beside-the-input>, Scope): an
input that needs a file outside it is rejected, naming it, before that file is opened."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import h5py
import pytest

from refkv.zarrify import (
    CziSource,
    Hdf5Source,
    LifSource,
    NiftiSource,
    NrrdSource,
    Rejected,
    TiffSource,
    VsiSource,
    zarrify,
)
from refkv.zarrify.store.beside import Scope, VsiFiles

FIXTURES = Path(__file__).parents[2] / "fixtures"
GRIB = FIXTURES / "grib" / "grib_simple.grib2"
SOURCES = {"hdf5": Hdf5Source, "nrrd": NrrdSource, "nifti": NiftiSource, "tiff": TiffSource, "czi": CziSource,
           "lif": LifSource, "vsi": VsiSource}


def no_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails the test when the host is asked to open or list a file beside the input."""

    def refused(*_args: object) -> None:
        raise AssertionError("a sibling was probed")

    monkeypatch.setattr(VsiFiles, "probe", refused)
    monkeypatch.setattr(VsiFiles, "_listdir", refused)


def shared_upload(tmp_path: Path) -> Path:
    """An upload whose HDF5 external storage names another user's file in the shared directory."""
    (tmp_path / "other-user").mkdir()
    (tmp_path / "other-user" / "session.txt").write_bytes(b"s" * 40)
    with h5py.File(tmp_path / "upload.h5", "w") as f:
        f.create_dataset("x", shape=(40,), dtype="u1", external=[("other-user/session.txt", 0, 40)])
    return tmp_path / "upload.h5"


# (the input, the sibling it names first): one of each profile that reads siblings
NAMING = [
    ("hdf5/hdf5_external.h5", "hdf5_external_a.bin"),  # external storage
    ("hdf5/hdf5_external_target.h5", "hdf5_external_absent.h5"),  # an external link, which may dangle
    ("nrrd/nrrd_detached_gzip.nhdr", "nrrd_detached_gzip_files/data.raw.gz"),
    ("nifti/nifti_pair_nibabel.hdr", "nifti_pair_nibabel.img"),
    ("tiff/ome_set_a.ome.tif", "ome_set_b.ome.tif"),
    ("czi/czi_parts_both.czi", "czi_parts_both(1).czi"),
    ("lif/lif_xlef_chain.xlef", "_lif_xlef_chain_/1.xlcf"),
    ("vsi/vsi_bmp.vsi", "_vsi_bmp_/stack10000/frame_t.ets"),
]


@pytest.mark.parametrize(("name", "key"), [*NAMING, ("upload", "other-user/session.txt")],
                         ids=[n for n, _ in NAMING] + ["shared upload"])
@pytest.mark.parametrize("named", [False, True], ids=["sniffed", "named"])
def test_an_input_that_names_a_sibling_out_of_its_scope_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                    name: str, key: str, named: bool):
    no_probes(monkeypatch)
    path = str(shared_upload(tmp_path) if name == "upload" else FIXTURES / name)
    fmt = "hdf5" if name == "upload" else name.split("/", maxsplit=1)[0]
    message = f'out of scope: the input names the file "{key}"'
    with pytest.raises(Rejected, match=re.escape(message)):
        zarrify(SOURCES[fmt](path, scope=path) if named else path, scope=None if named else path)


NRRD = FIXTURES / "nrrd" / "nrrd_detached_gzip.nhdr"


@pytest.mark.parametrize("scope", [
    [NRRD.parent],  # the input's directory, the default
    [NRRD.parent / "nrrd_detached_gzip_files"],  # a directory below it
    [NRRD, NRRD.parent / "nrrd_detached_gzip_files" / "data.raw.gz"],  # the input and its data file
    str(NRRD.parent / "nrrd_detached_gzip_files" / "data.raw.gz"),  # one path
    [NRRD.parent / "nrrd_detached_gzip_files" / ".." / "nrrd_detached_gzip_files"],
], ids=["directory", "directory below", "files", "one path", "spelled with .."])
def test_a_scope_that_holds_the_siblings_reads_them(tmp_path: Path, scope: Scope):
    want, got = tmp_path / "want.vzip", tmp_path / "got.vzip"
    zarrify(str(NRRD), "https://data.test/nrrd_detached_gzip.nhdr").save_vzip(str(want))
    zarrify(str(NRRD), "https://data.test/nrrd_detached_gzip.nhdr", scope=scope).save_vzip(str(got))
    assert got.read_bytes() == want.read_bytes()


@pytest.mark.parametrize("named", ["plain", "grib with an index"])
def test_an_input_that_needs_no_sibling_is_zarrified_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                           named: str):
    """A file that names no sibling, and a GRIB file beside its optional index, give in a scope of
    themselves alone the archive the file alone in its directory gives."""
    source = FIXTURES / "tiff" / "bilevel.tif" if named == "plain" else GRIB
    url = f"https://data.test/{source.name}"
    want, got = tmp_path / "want.vzip", tmp_path / "got.vzip"
    zarrify(str(source), url).save_vzip(str(want))
    beside = tmp_path / "beside"
    beside.mkdir()
    shutil.copy(source, beside)
    (beside / f"{source.name}.idx").write_text("1:0:d=2026102900:X:surface:anl:\n")
    no_probes(monkeypatch)
    zarrify(str(beside / source.name), url, scope=beside / source.name).save_vzip(str(got))
    assert got.read_bytes() == want.read_bytes()


@pytest.mark.parametrize("scope", ["../outside", "/", "https://data.test/a/"])
def test_a_scope_outside_the_input_directory_is_refused(tmp_path: Path, scope: str):
    (tmp_path / "d").mkdir()
    shutil.copy(NRRD, tmp_path / "d" / "a.nhdr")
    with pytest.raises(ValueError, match="is not the input's directory"):
        zarrify(str(tmp_path / "d" / "a.nhdr"), scope=str(tmp_path / "d" / scope) if scope.startswith(".") else scope)


def test_a_scope_outside_a_remote_input_directory_is_refused():
    with pytest.raises(ValueError, match="is not the input's directory"):
        zarrify("https://data.test/d/a.nhdr", scope="https://data.test/e/")


def test_a_store_input_takes_no_scope():
    with pytest.raises(ValueError, match="scope is for a file input"):
        zarrify(str(FIXTURES / "zarr2" / "zarr2_hierarchy"), "https://data.test/s/", scope=[])


def test_the_command_rejects_an_input_that_names_a_sibling_out_of_its_scope(tmp_path: Path):
    upload = str(shared_upload(tmp_path))
    p = subprocess.run([sys.executable, "-m", "refkv.zarrify", upload, str(tmp_path / "out.vzip"), "--scope", upload],
                       capture_output=True, text=True, check=False)
    assert p.returncode == 3, p.stderr
    assert p.stderr == 'rejected: out of scope: the input names the file "other-user/session.txt"\n'
    assert not (tmp_path / "out.vzip").exists()


def test_the_command_refuses_a_scope_outside_the_input_directory(tmp_path: Path):
    upload = str(shared_upload(tmp_path))
    p = subprocess.run([sys.executable, "-m", "refkv.zarrify", upload, str(tmp_path / "out.vzip"), "--scope",
                        str(tmp_path.parent)], capture_output=True, text=True, check=False)
    assert p.returncode == 2, p.stderr
    assert "is not the input's directory" in p.stderr
