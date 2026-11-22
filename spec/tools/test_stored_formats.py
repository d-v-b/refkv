"""Every profile's stored-arrays file against the registry (stored_formats.py)."""

import stored_formats


def test_every_profile_has_a_file(tmp_path):
    for name in ("czi", "npy"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "stored-arrays.md").write_text("# x\n")
    (tmp_path / "flac").mkdir()  # a format's directory with no stored-arrays file
    assert stored_formats.specified(tmp_path) == ["czi", "npy"]
    assert stored_formats.problems(["czi", "npy"], ["czi", "npy"]) == []
    assert stored_formats.check() == []


def test_a_profile_without_a_file():
    assert stored_formats.problems(["czi", "npy", "hdf4"], ["czi", "npy"]) == [
        "hdf4: no spec/zarrify/hdf4/stored-arrays.md"]


def test_a_file_of_no_profile():
    assert stored_formats.problems(["czi"], ["czi", "npy"]) == [
        "npy: spec/zarrify/npy/stored-arrays.md, but no profile of the registry but a layout"]
