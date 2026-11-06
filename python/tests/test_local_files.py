"""refkv.reader.LocalFiles, which reads a local archive and its file: sources by OS
path, and the file-host rule of spec §5
<https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#5-source-table> as the reader applies
it: an archive on a UNC path reads itself, and its sources need allow_file_hosts."""

import errno
import functools
import ntpath
import os
import posixpath

import pytest

import refkv
from refkv import reader
from refkv._sync import run
from refkv.errors import ResolutionError
from refkv.pb import Range, Source
from refkv.reader import LocalFiles
from refkv.uri import file_path

# LocalFiles keeps descriptors open only where there is os.pread (not Windows; see its docstring)
caches = pytest.mark.skipif(not hasattr(os, "pread"), reason="LocalFiles opens per read here")


@pytest.fixture(autouse=True)
def no_cached_descriptors():
    """Each test starts and ends with the process-wide cache of descriptors empty."""
    LocalFiles().close()
    yield
    LocalFiles().close()


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
        # an offset range, which the options' type leaves out
        run(LocalFiles().get_async(
            str(tmp_path / "f.bin"), options={"range": {"offset": 1}}))  # pyrefly: ignore[bad-assignment]


def test_local_files_refuses_a_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        run(LocalFiles().get_range_async(str(tmp_path / "nope"), start=0, end=1))


@caches
def test_local_files_reuses_one_descriptor_per_file(tmp_path, monkeypatch):
    (tmp_path / "f.bin").write_bytes(bytes(range(100)))
    opened = []
    real_open = os.open
    monkeypatch.setattr(os, "open", lambda *a, **k: opened.append(a[0]) or real_open(*a, **k))
    files = LocalFiles()
    for start in range(0, 100, 10):
        got = run(files.get_range_async(str(tmp_path / "f.bin"), start=start, end=start + 5))
        assert got == bytes(range(start, start + 5))
    assert opened == [str(tmp_path / "f.bin")]


@caches
def test_local_files_reopens_a_replaced_file(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"old-old-old")
    files = LocalFiles()
    assert run(files.get_range_async(str(p), start=0, end=3)) == b"old"
    q = tmp_path / "g.bin"
    q.write_bytes(b"new-new-new!")  # different size, so a different key
    q.replace(p)
    assert run(files.get_range_async(str(p), start=0, end=3)) == b"new"


@caches
def test_local_files_bounds_open_descriptors_and_never_closes_one_in_use(tmp_path, monkeypatch):
    monkeypatch.setattr(reader, "MAX_OPEN_FILES", 2)
    paths = [tmp_path / f"{i}.bin" for i in range(5)]
    for i, p in enumerate(paths):
        p.write_bytes(bytes([i]) * 16)
    files = LocalFiles()
    entry = reader._fds.acquire(str(paths[0]))  # held, as a reader mid-pread would hold it
    for p in paths[1:]:
        assert run(files.get_range_async(str(p), start=0, end=1)) == p.read_bytes()[:1]
    assert entry.evicted
    assert entry not in reader._fds._entries.values()
    assert os.pread(entry.fd, 1, 0) == b"\x00"  # evicted but still open while held
    reader._fds.release(entry)
    with pytest.raises(OSError, match="Bad file descriptor"):  # released after eviction: closed
        os.fstat(entry.fd)
    assert len(reader._fds._entries) <= 2


@caches
def test_local_files_share_one_budget_of_descriptors(tmp_path, monkeypatch):
    monkeypatch.setattr(reader, "MAX_OPEN_FILES", 2)
    one, two = LocalFiles(), LocalFiles()
    for files, name in zip((one, two, one), "abc", strict=True):
        (tmp_path / name).write_bytes(name.encode())
        assert run(files.get_range_async(str(tmp_path / name), start=0, end=1)) == name.encode()
    assert [key[0] for key in reader._fds._entries] == [str(tmp_path / "b"), str(tmp_path / "c")]


def test_local_files_suffix_reads_through_the_cache(tmp_path):
    (tmp_path / "f.bin").write_bytes(bytes(range(10)))
    got = run(LocalFiles().get_async(str(tmp_path / "f.bin"), options={"range": {"suffix": 3}}))
    assert (run(got.buffer_async()), got.meta["size"]) == (bytes([7, 8, 9]), 10)


@caches
def test_local_files_close_closes_idle_descriptors_and_keeps_held_ones(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"a")
    (tmp_path / "b.bin").write_bytes(b"b")
    files = LocalFiles()
    held = reader._fds.acquire(str(tmp_path / "a.bin"))
    idle = reader._fds.acquire(str(tmp_path / "b.bin"))
    reader._fds.release(idle)
    files.close()
    assert idle.evicted
    assert list(reader._fds._entries.values()) == [held]
    assert not held.evicted
    assert os.pread(held.fd, 1, 0) == b"a"  # still open while held
    reader._fds.release(held)
    LocalFiles().close()  # any LocalFiles closes the shared cache's idle descriptors
    assert held.evicted
    assert not reader._fds._entries


@caches
def test_local_files_closes_idle_descriptors_and_retries_when_out_of_descriptors(tmp_path, monkeypatch):
    for name in "abc":
        (tmp_path / name).write_bytes(name.encode())
    for name in "ab":  # through other stores than the one that runs out
        assert run(LocalFiles().get_range_async(str(tmp_path / name), start=0, end=1)) == name.encode()
    idle = list(reader._fds._entries.values())
    real_open = os.open
    calls = []

    def open_(*a, **k):
        calls.append(len(reader._fds._entries))
        if len(calls) == 1:
            raise OSError(errno.EMFILE, "Too many open files")
        return real_open(*a, **k)

    monkeypatch.setattr(os, "open", open_)
    assert run(LocalFiles().get_range_async(str(tmp_path / "c"), start=0, end=1)) == b"c"
    assert calls == [2, 0]  # the retry came after the idle entries were dropped and closed
    assert len(idle) == 2
    assert all(entry.evicted for entry in idle)
    assert [key[0] for key in reader._fds._entries] == [str(tmp_path / "c")]


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
