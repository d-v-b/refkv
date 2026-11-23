"""A branch's changed files against its formats' own paths and the files shared on purpose (independence.py)."""

import independence
from independence import Change, declared, owner, report

NAMES = ["dicom", "grib", "mp3", "mrc", "mrc-dataset", "nifti", "npy", "ome-zarr", "tiff", "zarr2"]


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
    npy_in_grib = Change("conformance/outputs/manifests/grib.jsonl", True,
                         (("grib/expected/a.0.npy", "npy"), ("grib/expected/b.0.npy", "npy")))
    refused = Change("conformance/outputs/manifests/dicom_series.jsonl", True, (("dicom_series/bad.dcm", None),))
    format_branch = [
        Change("rust/refkv-ir/src/npy/mod.rs", True),
        Change("spec/zarrify/npy.md", True),
        npy_in_grib,
        Change("rust/refkv-ir/src/run.rs", True),  # shared on purpose
        Change("python/tests/test_ir_has_no_zarr.py", False),  # only removes lines: an entry leaves a list
    ]
    assert report(format_branch, {"npy"}, NAMES) == ({"npy"}, [])
    assert report([*format_branch, refused], None, NAMES) == ({"npy", "dicom"}, [])
    # A branch of no format changes what it likes.
    assert report([Change("justfile", True), Change("docs/build.md", True)], None, NAMES) == (set(), [])
    assert independence.manifest_rows(
        '--- a/x\n+++ b/x\n@@ -1 +1 @@\n-{"input": "grib/a.npy", "plain": {"profile": "npy"}}\n'
        '+{"input": "grib/a.npy", "plain": {"profile": "npy"}}\n+{"input": "grib/b.grib", "plain": {"error": "E"}}\n'
    ) == (("grib/a.npy", "npy"), ("grib/b.grib", None))


def test_a_file_of_another_format():
    assert report([Change("rust/refkv-ir/src/npy/mod.rs", True), Change("rust/refkv-ir/src/mp3.rs", False)],
                  {"npy"}, NAMES)[1] == [
        ("rust/refkv-ir/src/mp3.rs", "a file of mp3, which is not one of this branch's formats (npy)")]


def test_a_manifest_row_of_another_format():
    rows = (("grib/a.grib", "grib"), ("grib/expected/a.0.npy", "npy"))
    assert report([Change("rust/refkv-ir/src/npy/mod.rs", True),
                   Change("conformance/outputs/manifests/grib.jsonl", True, rows)], {"npy"}, NAMES)[1] == [
        ("conformance/outputs/manifests/grib.jsonl", "a file of grib, which is not one of this branch's formats (npy)")]


def test_a_central_file():
    assert report([Change("rust/refkv-ir/src/npy/mod.rs", True), Change("docs/build.md", True)], None, NAMES)[1] == [
        ("docs/build.md", independence.CENTRAL)]
