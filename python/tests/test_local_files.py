"""refkv.reader.LocalFiles, which reads a local archive and its file: sources by OS
path, and the file-host rule of spec §5
<https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#5-source-table> as the reader applies
it: an archive on a UNC path reads itself, and its sources need allow_file_hosts."""

import functools
import ntpath
import posixpath

import pytest

import refkv
from refkv import reader
from refkv._sync import run
from refkv.errors import ResolutionError
from refkv.pb import Range, Source
from refkv.reader import LocalFiles
from refkv.uri import file_path


def test_local_files_reads_ranges_and_suffixes(tmp_path):
    path = str(tmp_path / "f.bin")
    (tmp_path / "f.bin").write_bytes(bytes(range(10)))
    files = LocalFiles()
    ranges = {(0, 10): bytes(range(10)), (2, 5): bytes([2, 3, 4]), (8, 20): bytes([8, 9]),
              (10, 12): b""}
    for (start, end), want in ranges.items():
        assert run(files.get_range_async(path, start=start, end=end)) == want, (start, end)
    for n, want in {3: bytes([7, 8, 9]), 10: bytes(range(10)), 64: bytes(range(10))}.items():
        got = run(files.get_async(path, options={"range": {"suffix": n}}))
        assert (run(got.buffer_async()), got.meta["size"]) == (want, 10), n


def test_local_files_refuses_a_range_past_the_end(tmp_path):
    (tmp_path / "f.bin").write_bytes(b"abc")
    with pytest.raises(ValueError, match="past the end of the 3-byte file"):
        run(LocalFiles().get_range_async(str(tmp_path / "f.bin"), start=4, end=6))


def test_local_files_reads_only_a_suffix_with_get(tmp_path):
    (tmp_path / "f.bin").write_bytes(b"abc")
    with pytest.raises(NotImplementedError, match="reads a suffix"):
        run(LocalFiles().get_async(str(tmp_path / "f.bin"), options={"range": {"offset": 1}}))


def test_local_files_refuses_a_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        run(LocalFiles().get_range_async(str(tmp_path / "nope"), start=0, end=1))


@pytest.fixture
def windows_reads(monkeypatch):
    """The reader maps file: URLs as on Windows, and records the paths it reads."""
    seen = []
    monkeypatch.setattr(reader, "file_path", functools.partial(file_path, pathmod=ntpath))
    monkeypatch.setattr(LocalFiles, "_range",
                        staticmethod(lambda path, start, end: seen.append(path) or b"x" * (end - start)))
    return seen


def _store(policy=None):
    return refkv.ReferenceStore({"k": Range(source=0, offset=0, length=2)},
                                [Source(url="b.bin")], url="file://server/share/a.vzip",
                                policy=policy)


def test_an_archive_on_a_unc_path_reads_itself_and_allowed_sources(windows_reads):
    store = _store(refkv.Policy(allow_file_hosts=True))
    assert run(store._read(store.url, 0, 2, external=False)) == b"xx"
    assert store.get("k") == b"xx"
    assert windows_reads == ["\\\\server\\share\\a.vzip", "\\\\server\\share\\b.bin"]


def test_an_archive_on_a_unc_path_reads_itself_without_allow_file_hosts(windows_reads):
    store = _store()
    assert run(store._read(store.url, 0, 2, external=False)) == b"xx"
    assert windows_reads == ["\\\\server\\share\\a.vzip"]


def test_an_archive_on_a_unc_path_refuses_its_sources_without_allow_file_hosts(windows_reads):
    with pytest.raises(ResolutionError, match="allow_file_hosts"):
        _store().get("k")
    assert windows_reads == []


def test_a_file_host_is_a_resolution_error_on_posix_even_when_allowed(monkeypatch):
    monkeypatch.setattr(reader, "file_path", functools.partial(file_path, pathmod=posixpath))
    store = _store(refkv.Policy(allow_file_hosts=True))
    with pytest.raises(ResolutionError, match="file: URI with authority 'server'"):
        run(store._read("file://server/share/b.bin", 0, 2, external=True))
