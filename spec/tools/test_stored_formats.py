"""§7 of the stored-array spec against the registry (stored_formats.py)."""

import stored_formats

LIST = ("The parsers of [MP3](zarrify/mp3.md) and [Zarr v2](zarrify/zarr2.md) describe no array yet,\n"
        "and have no paragraph here.")


def spec(*paragraphs: str, listed: str = LIST) -> str:
    body = "".join(f"{p}\n\n" for p in paragraphs)
    # a paragraph after §7, which is not one of it
    after = "**FLAC** ([spec/zarrify/flac.md](zarrify/flac.md)).\n"
    return f"# Stored arrays\n\n## 7. The formats\n\n{listed}\n\n{body}## 8. Next\n\n{after}"


CZI = "**CZI** ([spec/zarrify/czi.md](zarrify/czi.md)). Every subblock is a stored array."
NPY = "**NumPy** ([spec/zarrify/npy.md](zarrify/npy.md)). A file's array.\n\n- **Declarations.** One."


def test_every_profile_in_a_paragraph_or_the_list():
    assert stored_formats.problems(["czi", "npy", "mp3", "zarr2"], spec(CZI, NPY)) == []
    assert stored_formats.accounted(spec(CZI, NPY)) == (["czi", "npy"], ["mp3", "zarr2"])
    assert stored_formats.check() == []


def test_a_profile_in_neither():
    assert stored_formats.problems(["czi", "npy", "mp3", "zarr2", "hdf4"], spec(CZI, NPY)) == [
        "hdf4: no paragraph in §7, and not in its list of formats that describe no array"]


def test_a_profile_in_both():
    listed = LIST.replace("[MP3](zarrify/mp3.md)", "[CZI](zarrify/czi.md), [MP3](zarrify/mp3.md)")
    assert stored_formats.problems(["czi", "npy", "mp3", "zarr2"], spec(CZI, NPY, listed=listed)) == [
        "czi: a paragraph in §7, and in its list of formats that describe no array"]


def test_a_profile_with_two_paragraphs():
    assert stored_formats.problems(["czi", "npy", "mp3", "zarr2"], spec(CZI, NPY, CZI)) == ["czi: named 2 times in §7"]


def test_a_format_no_profile_has():
    assert stored_formats.problems(["czi", "mp3", "zarr2"], spec(CZI, NPY)) == [
        "npy: in §7, but no profile of the registry"]
