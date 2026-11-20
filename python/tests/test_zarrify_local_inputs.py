"""Local inputs as VirtualiZarr passes them: a file, or a Zarr v2 or v3 directory, by its path, by
its `file:` URL, and by its `file:` URL with a `file://` store registered (spec/zarrify.md §1.2
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>, §1.4
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>), with refkv's layout
and with a plugin layout."""

from pathlib import Path

import numpy as np
import pytest
import zarr
from obspec_utils.registry import ObjectStoreRegistry
from obstore.store import LocalStore

import refkv.zarr
from refkv.errors import ResolutionError
from refkv.zarrify import Rejected, zarrify
from refkv.zarrify.layouts import Layout

VALUES = np.arange(24, dtype="<i4").reshape(4, 6)


def _input(tmp_path: Path, kind: str) -> tuple[Path, str]:
    """(the input, the path of its array in the output) of `kind`, in a directory whose name
    needs percent-encoding in a URL."""
    where = tmp_path / "local data"
    where.mkdir()
    if kind == "file":
        np.save(where / "x.npy", VALUES)
        return where / "x.npy", "array"
    path = where / f"{kind}.zarr"
    a = zarr.create_array(str(path), name="a", shape=VALUES.shape, chunks=(2, 3), dtype=VALUES.dtype,
                          zarr_format=2 if kind == "zarr2" else 3)
    a[:] = VALUES
    return path, "a"


@pytest.mark.parametrize("layout", [None, "refkv.example.bare"])
@pytest.mark.parametrize("form", ["path", "file URL", "file URL and registry"])
@pytest.mark.parametrize("kind", ["file", "zarr2", "zarr3"])
def test_a_local_input_zarrifies_in_every_form(tmp_path, kind, form, layout):
    """Every form gives the store the path gives; a directory's chunks are references to its
    files, by their `file:` URLs; the store reads its input's own files and no other local file,
    and zarr reads the input's values from it."""
    path, array = _input(tmp_path, kind)
    url = path.as_uri() + ("/" if kind != "file" else "")
    location = str(path) if form == "path" else url
    registry = ObjectStoreRegistry({"file://": LocalStore()}) if form == "file URL and registry" else None
    plugin = None if layout is None else Layout(layout)

    got = zarrify(location, registry=registry, layout=plugin)
    want = zarrify(str(path), layout=None if layout is None else Layout(layout))

    assert got.mapping == want.mapping
    assert [s.url for s in got.sources] == [s.url for s in want.sources]
    assert got.policy.prefixes == want.policy.prefixes
    if kind == "file":
        assert got.policy.prefixes == (path.parent.as_uri() + "/",)
    else:
        assert all(s.url.startswith(url) for s in got.sources if s.url is not None)
        assert got.policy.prefixes is not None
        assert all(p.startswith(url) for p in got.policy.prefixes)
    (tmp_path / "outside.bin").write_bytes(b"x")
    with pytest.raises(ResolutionError):
        got.policy.check((tmp_path / "outside.bin").as_uri())
    if layout is not None:
        assert got.info["layouts"] == [layout]
    read = zarr.open_array(refkv.zarr.store(got), path=array, mode="r")
    assert np.array_equal(read[...], VALUES)


def test_a_file_url_with_a_host_is_refused(tmp_path):
    np.save(tmp_path / "x.npy", VALUES)
    with pytest.raises(ResolutionError, match="UNC path"):
        zarrify(f"file://server{(tmp_path / 'x.npy').as_posix()}")


def test_a_file_url_with_a_query_names_no_local_path(tmp_path):
    np.save(tmp_path / "x.npy", VALUES)
    with pytest.raises(ValueError, match="query"):
        zarrify((tmp_path / "x.npy").as_uri() + "?v=1")


def test_a_directory_named_by_another_directory_s_file_url_is_rejected(tmp_path):
    path, _ = _input(tmp_path, "zarr3")
    (tmp_path / "other").mkdir()
    with pytest.raises(Rejected, match="is not the URL of the directory"):
        zarrify(str(path), (tmp_path / "other").as_uri() + "/")


def test_a_directory_s_file_url_without_a_trailing_slash_is_rejected(tmp_path):
    path, _ = _input(tmp_path, "zarr3")
    with pytest.raises(Rejected, match="does not end in /"):
        zarrify(str(path), path.as_uri())
