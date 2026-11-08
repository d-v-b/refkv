"""A local input's siblings stay below its directory, symbolic links followed (spec/zarrify.md §1.9
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#19-sources-beside-the-input>)."""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

import pytest

from refkv.zarrify import Rejected
from refkv.zarrify import _zarrify as zarrify
from refkv.zarrify.store.beside import VsiFiles

NRRD = Path(__file__).parents[2] / "fixtures" / "nrrd"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="making a symbolic link needs a privilege on Windows")


@pytest.fixture
def upload(tmp_path: Path) -> Path:
    """A directory with a named file, its files and links, beside a directory with a secret."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.bin").write_bytes(b"secret")
    upload = tmp_path / "upload"
    (upload / "sub").mkdir(parents=True)
    (upload / "a.bin").write_bytes(b"named")
    (upload / "plain.bin").write_bytes(b"plain")
    (upload / "sub" / "plain.bin").write_bytes(b"plain")
    (upload / "link_inside.bin").symlink_to("plain.bin")
    (upload / "dir_inside").symlink_to("sub")
    (upload / "link_outside.bin").symlink_to(outside / "secret.bin")
    (upload / "dir_outside").symlink_to(outside)
    return upload


@pytest.mark.parametrize("case", [False, True])
@pytest.mark.parametrize("named_through_a_link", [False, True])
@pytest.mark.parametrize("key", ["plain.bin", "sub/plain.bin", "link_inside.bin", "dir_inside/plain.bin"])
def test_a_sibling_below_the_directory_is_found(upload: Path, key: str, named_through_a_link: bool, case: bool):
    directory = upload
    if named_through_a_link:
        directory = upload.parent / "linked"
        directory.symlink_to(upload)
    assert VsiFiles(str(directory / "a.bin"), case=case).probe(key) == len(b"plain")


@pytest.mark.parametrize("case", [False, True])
def test_a_link_to_a_file_outside_is_absent(upload: Path, case: bool):
    files = VsiFiles(str(upload / "a.bin"), case=case)
    assert files.probe("link_outside.bin") is None
    assert "link_outside.bin" not in files.objects


@pytest.mark.parametrize("case", [False, True])
def test_a_key_through_a_link_to_a_directory_outside_is_absent(upload: Path, case: bool):
    files = VsiFiles(str(upload / "a.bin"), case=case)
    assert files.probe("dir_outside/secret.bin") is None
    assert "dir_outside/secret.bin" not in files.objects


def test_a_directory_outside_is_not_listed(upload: Path):
    assert VsiFiles(str(upload / "a.bin"), case=True)._listdir("dir_outside") is None


def test_a_required_file_linked_from_outside_is_rejected(tmp_path: Path):
    """The data file a detached header names is there, by a link to a file outside the directory."""
    outside = tmp_path / "outside"
    shutil.copytree(NRRD / "nrrd_detached_gzip_files", outside)
    upload = tmp_path / "upload"
    upload.mkdir()
    shutil.copy(NRRD / "nrrd_detached_gzip.nhdr", upload)
    (upload / "nrrd_detached_gzip_files").symlink_to(outside)
    message = 'the data file "nrrd_detached_gzip_files/data.raw.gz" does not exist'
    with pytest.raises(Rejected, match=re.escape(message)):
        zarrify(str(upload / "nrrd_detached_gzip.nhdr"))
