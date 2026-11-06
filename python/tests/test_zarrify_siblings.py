"""Zarrifying with siblings refused reads no file but the input (spec/zarrify.md §1.9
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#19-sources-beside-the-input>, Refusing
siblings): an input that needs another file is rejected, naming it, before any is opened."""

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
from refkv.zarrify.store.beside import VsiFiles

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
def test_an_input_that_names_a_sibling_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str,
                                                   key: str, named: bool):
    no_probes(monkeypatch)
    path = str(shared_upload(tmp_path) if name == "upload" else FIXTURES / name)
    fmt = "hdf5" if name == "upload" else name.split("/", maxsplit=1)[0]
    message = f'siblings refused: the input names the file "{key}"'
    with pytest.raises(Rejected, match=re.escape(message)):
        zarrify(SOURCES[fmt](path, siblings=False) if named else path, siblings=None if named else False)


@pytest.mark.parametrize("named", ["plain", "grib with an index"])
def test_an_input_that_needs_no_sibling_is_zarrified_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                           named: str):
    """A file that names no sibling, and a GRIB file beside its optional index, give the archive the
    file alone in its directory gives without the option."""
    source = FIXTURES / "tiff" / "bilevel.tif" if named == "plain" else GRIB
    url = f"https://data.test/{source.name}"
    want, got = tmp_path / "want.vzip", tmp_path / "got.vzip"
    zarrify(str(source), url).save_vzip(str(want))
    beside = tmp_path / "beside"
    beside.mkdir()
    shutil.copy(source, beside)
    (beside / f"{source.name}.idx").write_text("1:0:d=2026102900:X:surface:anl:\n")
    no_probes(monkeypatch)
    zarrify(str(beside / source.name), url, siblings=False).save_vzip(str(got))
    assert got.read_bytes() == want.read_bytes()


def test_the_command_rejects_an_input_that_names_a_sibling(tmp_path: Path):
    p = subprocess.run([sys.executable, "-m", "refkv.zarrify", str(shared_upload(tmp_path)),
                        str(tmp_path / "out.vzip"), "--no-siblings"], capture_output=True, text=True, check=False)
    assert p.returncode == 3, p.stderr
    assert p.stderr == 'rejected: siblings refused: the input names the file "other-user/session.txt"\n'
    assert not (tmp_path / "out.vzip").exists()
