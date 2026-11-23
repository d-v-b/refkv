"""A branch's changed files against its formats' own paths and the files shared on purpose (independence.py)."""

import subprocess

import independence
import pytest
from independence import Change, declared, owner, report, spellings

TITLES = {"dicom": "DICOM", "fits": "FITS", "grib": "GRIB", "mp3": "MP3", "mrc": "MRC", "mrc-dataset": "MRC dataset",
          "nifti": "NIfTI", "npy": "NumPy", "ome-zarr": "OME-Zarr", "safe": "Sentinel-2 SAFE",
          "safetensors": "safetensors", "tiff": "TIFF", "zarr2": "Zarr v2"}
NAMES = sorted(TITLES)
SPELLED = spellings([{"name": n, "title": t, "kind": "file"} for n, t in TITLES.items()])


def test_what_is_not_reported():
    # A path belongs to the format its first component naming a profile names, as whole words.
    assert {path: owner(path, NAMES) for path in [
        "rust/refkv-ir/src/nifti_pair.rs",
        "rust/refkv-ir/src/npy/mod.rs",
        "rust/refkv-ir/src/project/zarr2_nodes.rs",
        "rust/refkv-ir/src/ome_zarr.rs",
        "spec/zarrify/changes/mp3/2026-11-22-a-frames-form-has-no-data-type.md",
        "python/tests/ir/test_mrc_description.py",
        "fixtures/mrc-dataset/x.mrc",
        "fixtures/dicom_series/a.dcm",
        "fixtures/grib/expected/npy_like.0.npy",
        "rust/refkv-ir/src/h5_bitshuffle.rs",
        "docs/build.md",
    ]} == {
        "rust/refkv-ir/src/nifti_pair.rs": "nifti",
        "rust/refkv-ir/src/npy/mod.rs": "npy",
        "rust/refkv-ir/src/project/zarr2_nodes.rs": "zarr2",
        "rust/refkv-ir/src/ome_zarr.rs": "ome-zarr",
        "spec/zarrify/changes/mp3/2026-11-22-a-frames-form-has-no-data-type.md": "mp3",
        "python/tests/ir/test_mrc_description.py": "mrc",
        "fixtures/mrc-dataset/x.mrc": "mrc-dataset",
        "fixtures/dicom_series/a.dcm": "dicom",
        "fixtures/grib/expected/npy_like.0.npy": "grib",
        "rust/refkv-ir/src/h5_bitshuffle.rs": None,
        "docs/build.md": None,
    }
    # The scopes name the branch's formats; a scope of shared machinery, or none, leaves them to the paths.
    assert declared(["fix(npy): a", "docs(npy,mp3): b", "Merge branch 'main'"], NAMES) == {"npy", "mp3"}
    assert declared(["fix(npy): a", "test(ir): b"], NAMES) is None
    assert declared(["chore: merge main"], NAMES) is None
    # An output manifest's change belongs to its changed rows' profiles, or a refused input's directory.
    npy_in_grib = Change("conformance/outputs/manifests/grib.jsonl",
                         rows=(("grib/expected/a.0.npy", "npy"), ("grib/expected/b.0.npy", "npy")))
    refused = Change("conformance/outputs/manifests/dicom_series.jsonl", rows=(("dicom_series/bad.dcm", None),))
    format_branch = [
        Change("rust/refkv-ir/src/npy/mod.rs"),
        Change("spec/zarrify/npy.md"),
        npy_in_grib,
        Change("rust/refkv-ir/src/run.rs", ('    "npy" => Npy,',)),  # shared on purpose, though it names npy
        Change("python/tests/test_ir_has_no_zarr.py"),  # only removes lines: an entry leaves a list
        # a file of no format, whose added lines name no profile (thread-safe, safe_l1c, tiff_set, the titles that
        # hold Zarr, and a name with no capital letter where code names nothing): shared machinery
        Change("python/src/refkv/ir/store.py", ("# thread-safe: a safe_l1c run, or a tiff_set,",
                                                "# read as OME-Zarr or Zarr v2, or Neuroglancer precomputed",
                                                "    const safe = (v: bigint) => v;  // none fits, so: fits.",
                                                "    Ok(fits.then_some(x)) && safe.test(c); `fits(value)` gives")),
    ]
    assert report(format_branch, {"npy"}, NAMES, SPELLED) == ({"npy"}, [])
    assert report([*format_branch, refused], None, NAMES, SPELLED) == ({"npy", "dicom"}, [])
    # A format's own fixture that another profile reads (GRIB's expected .npy files) is its own too.
    grib_branch = [Change("rust/refkv-ir/src/grib/mod.rs"),
                   Change("conformance/outputs/manifests/grib.jsonl",
                          rows=(("grib/grib_new.grib2", "grib"), ("grib/expected/grib_new.0.npy", "npy")))]
    assert report(grib_branch, {"grib"}, NAMES, SPELLED) == ({"grib"}, [])
    assert report(grib_branch, None, NAMES, SPELLED) == ({"grib"}, [])
    assert report(grib_branch[1:], None, NAMES, SPELLED) == ({"grib"}, [])
    # A branch of no format changes what it likes, so long as no line it adds names a format.
    assert report([Change("justfile", ("ci: x",)), Change("docs/build.md", ("A rule.",))], None, NAMES,
                  SPELLED) == (set(), [])
    # The profiles a line names: name, name in capitals, title of one word, as whole words, case and all.
    assert {line: independence.mentions(line, SPELLED) for line in [
        'for name in ["dicom", "npy"]:', "NumPy's and NIfTI's, and an NPY file", "a SAFE product, `mrc-dataset`",
        "Tiff, Dicom, nifti_pair, refkv.grib", "OME-Zarr, Zarr v2, 'ome-zarr', zarr2.md",
        "a safetensors file, an MRC dataset, Sentinel-2 SAFE",
        "use crate::tiff::Ifd; see spec/zarrify/fits.md and zarrify/mp3/", "it fits; a safe tiff, mrc-dataset",
        "pub mod tiff;", "from refkv.zarrify import common, safe", "use crate::{fits, grib};", "import fits, tiff",
        "far from safe; import fits from the model; and zarr2", "  nifti: [...], `fits(value)`, fits.then_some",
    ]} == {
        'for name in ["dicom", "npy"]:': {"dicom", "npy"},
        "NumPy's and NIfTI's, and an NPY file": {"npy", "nifti"},
        "a SAFE product, `mrc-dataset`": {"safe", "mrc-dataset"},
        "Tiff, Dicom, nifti_pair, refkv.grib": {"grib"},
        "OME-Zarr, Zarr v2, 'ome-zarr', zarr2.md": {"ome-zarr", "zarr2"},
        "a safetensors file, an MRC dataset, Sentinel-2 SAFE": {"safetensors", "mrc-dataset", "safe"},  # titles
        "use crate::tiff::Ifd; see spec/zarrify/fits.md and zarrify/mp3/": {"tiff", "fits", "mp3"},
        "it fits; a safe tiff, mrc-dataset": {"mrc-dataset"},  # a name with a digit or `-` is no word
        "pub mod tiff;": {"tiff"},
        "from refkv.zarrify import common, safe": {"safe"},
        "use crate::{fits, grib};": {"fits", "grib"},
        "import fits, tiff": {"fits", "tiff"},
        "far from safe; import fits from the model; and zarr2": {"zarr2"},  # English, but for the name with a digit
        "  nifti: [...], `fits(value)`, fits.then_some": set(),  # what the rule misses, and what it must not count
    }
    assert independence.added_lines("diff --git a/x b/x\n+++ b/x\n@@ -1 +1,2 @@\n-old\n+new\n++plus\n") == (
        "new", "+plus")
    assert independence.manifest_rows(
        '--- a/x\n+++ b/x\n@@ -1 +1 @@\n-{"input": "grib/a.npy", "plain": {"profile": "npy"}}\n'
        '+{"input": "grib/a.npy", "plain": {"profile": "npy"}}\n+{"input": "grib/b.grib", "plain": {"error": "E"}}\n'
    ) == (("grib/a.npy", "npy"), ("grib/b.grib", None))


def test_a_file_of_another_format():
    assert report([Change("rust/refkv-ir/src/npy/mod.rs"), Change("rust/refkv-ir/src/mp3.rs")],
                  {"npy"}, NAMES, SPELLED)[1] == [
        ("rust/refkv-ir/src/mp3.rs", "a file of mp3, which is not one of this branch's formats (npy)")]


def test_a_manifest_row_of_another_format():
    rows = (("grib/a.grib", "grib"), ("grib/expected/a.0.npy", "npy"))
    assert report([Change("rust/refkv-ir/src/npy/mod.rs"),
                   Change("conformance/outputs/manifests/grib.jsonl", rows=rows)], {"npy"}, NAMES, SPELLED)[1] == [
        ("conformance/outputs/manifests/grib.jsonl", "a file of grib, which is not one of this branch's formats (npy)")]


def test_a_central_file_gains_a_format():
    # whatever the branch's formats: here none
    added = ("A rule.", "The description, where a parser returns one: NumPy's and DICOM's.")
    assert report([Change("docs/build.md", added)], None, NAMES, SPELLED)[1] == [
        ("docs/build.md", independence.CENTRAL.format(formats="dicom, npy", line=added[1]))]


def git_repo(tmp_path, monkeypatch):
    """A repository with a commit on `main` and a branch `b` checked out, and a function to commit to it."""
    run = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a],  # noqa: E731
                                    cwd=tmp_path, check=True, capture_output=True)

    def commit(subject: str, files: dict[str, bytes | None]) -> None:
        for name, data in files.items():
            path = tmp_path / name
            if data is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
        run("add", "-A")
        run("commit", "-qm", subject)

    run("init", "-q", "-b", "main")
    manifest = b'{"input": "grib/a.grib2", "plain": {"profile": "grib"}}\n'
    commit("chore: start", {"docs/list.md": b"a\nb\n", "docs/logo.png": b"\x00\x01", "rust/src/npy.rs": b"x\n",
                            "conformance/outputs/manifests/grib.jsonl": manifest})
    run("checkout", "-q", "-b", "b")
    monkeypatch.setattr(independence, "ROOT", tmp_path)
    return commit


def test_the_branch_s_changes_from_git(tmp_path, monkeypatch, capsys):
    commit = git_repo(tmp_path, monkeypatch)
    commit("fix(npy): a", {"rust/src/npy.rs": b"y\n", "docs/list.md": b"a\n", "docs/logo.png": None,
                           "docs/icon.png": b"\x00\x02", "docs/new.md": b"A rule.\nAs NumPy's does.\n",
                           "conformance/outputs/manifests/grib.jsonl":
                               b'{"input": "grib/expected/a.0.npy", "plain": {"profile": "npy"}}\n'})
    assert sorted(independence.changes("main", "HEAD", ["grib", "npy"]), key=lambda c: c.path) == [
        Change("conformance/outputs/manifests/grib.jsonl",
               rows=(("grib/a.grib2", "grib"), ("grib/expected/a.0.npy", "npy"))),
        Change("docs/icon.png"),  # a binary file adds no lines
        Change("docs/list.md"),  # a line removed
        Change("docs/logo.png"),
        Change("docs/new.md", ("A rule.", "As NumPy's does.")),
        Change("rust/src/npy.rs"),  # a file of a format: its lines are not read
    ]
    independence.main(["--base", "main"])
    assert capsys.readouterr().out.splitlines() == [
        "independence: the branch's formats (named by its commits' scopes): npy",
        "  conformance/outputs/manifests/grib.jsonl: a file of grib, which is not one of this branch's formats (npy)",
        "  docs/new.md: " + independence.CENTRAL.format(formats="npy", line="As NumPy's does."),
        ("independence: 2 file(s) of another format, or shared and gaining a format's name; name each in the PR "
         "with why, or move what it says into the format's own files")]


def test_strict_fails_on_a_report(tmp_path, monkeypatch):
    commit = git_repo(tmp_path, monkeypatch)
    commit("fix(npy): a", {"rust/src/npy.rs": b"y\n", "docs/list.md": b'a\nb\n"npy",\n'})
    independence.main(["--base", "main"])  # a warning
    with pytest.raises(SystemExit) as e:
        independence.main(["--base", "main", "--strict"])
    assert e.value.code == 1
