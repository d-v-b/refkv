"""The example layout `refkv.example.bare` (refkv.zarrify.layouts.examples.bare) over a fixture or
two of every file profile: its output is refkv's own hierarchy without refkv's declarations and
without the IR mirror, `check` finds nothing in it, and zarr reads every array as the base's."""

import ast
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import zarr

import refkv
import refkv.zarr
from refkv.zarrify import zarrify
from refkv.zarrify.common import PROFILES
from refkv.zarrify.layouts import FILE_PROFILES, MIRROR, Layout, LayoutUsageError, base, encode_entries
from refkv.zarrify.layouts.examples.bare import Bare
from refkv.zarrify.layouts.hierarchy import check

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures"
NAME = "refkv.example.bare"
ALLOWED: set[str] = set()  # the check codes the module's docstring names: none
UUIDS = {uuid.lower() for uuid, *_ in PROFILES.values()}

# a fixture or two of each file profile, the module a reader needs to decode its arrays, and
# whether the call asks for checksums
CASES = [
    ("czi/czi_gray16_tczyx.czi", None, False),
    ("czi/czi_clipped_raw.czi", None, False),  # a run-length rectilinear grid
    ("dicom/dicom_bigendian_mono16.dcm", None, False),
    ("fits/fits_bintable.fits", None, False),
    ("fits/fits_gzip_image.fits.gz", None, False),
    ("flac/flac_mono_16.flac", "av", False),
    ("grib/grib_complex.grib2", None, False),
    ("hdf5/hdf5_attributes.h5", None, False),
    ("hdf5/hdf5_compounds.h5", None, False),
    ("ims/ims_2d_no_metadata.ims", None, False),
    ("lif/lif_2d_uint8.lif", None, False),
    ("lif/lif_xlef_tree.xlef", None, False),  # a file read as a store
    ("mp3/mp3_mpeg1_stereo_cbr.mp3", "av", False),
    ("mp4/mov_h264_aac.mov", "av", False),
    ("mrc/mrc_chunk_rows.mrc", None, False),  # with a dataset layout
    ("nd2/nd2_padded_rgb.nd2", None, False),
    ("ndpi/ndpi_levels.ndpi", None, False),
    ("nifti/nifti_dims_2d.nii", None, False),
    ("npy/npy_c_float64.npy", None, False),
    ("nrrd/nrrd_gzip_3d.nrrd", None, False),
    ("nrrd/nrrd_detached_raw.nhdr", None, False),
    ("qptiff/qptiff_fl_uint8_lzw.qptiff", "imagecodecs", False),
    ("safe/safe_l1c.SAFE.zip", "imagecodecs", False),
    ("safetensors/st_dtypes.safetensors", "ml_dtypes", False),
    ("tiff/bilevel_tiled.tif", None, False),
    ("tiff/strips_uint8.tif", None, False),
    ("vsi/vsi_png.vsi", "imagecodecs", False),
    ("zarr3/zarr3_hierarchy.zip", None, False),  # a zipped Zarr v3 store
    ("tiff/strips_uint8.tif", None, True),
    ("mrc/mrc_chunk_rows.mrc", None, True),
]
OPTIONS = [{}, {"dataset": False}]


def _documents(store):
    """The JSON documents of a store, parsed, by key."""
    return {k: json.loads(v) for k, v in store.mapping.items() if k.rpartition("/")[2] == "zarr.json"}


def _arrays(store):
    """Every array zarr finds in a store, by path."""
    group = zarr.open_group(refkv.zarr.store(store), mode="r")
    return {path: a for path, a in group.members(max_depth=None) if isinstance(a, zarr.Array)}


def _same(a, b):
    """Whether two arrays hold the same values, NaNs and structured records included."""
    if a.dtype != b.dtype or a.shape != b.shape:
        return False
    return a.tolist() == b.tolist() if a.dtype.kind in "OT" else a.tobytes() == b.tobytes()


def _digest(path, options):
    """The plugin's output for a fixture, hashed."""
    b = base(path)
    plugin = Bare()
    out = plugin.write(b, plugin.options(options))
    return hashlib.sha256(encode_entries(out)).hexdigest()


def test_the_cases_cover_every_file_profile():
    profiles = {base(str(FIXTURES / f)).profile for f, _, _ in CASES}
    assert profiles == FILE_PROFILES


@pytest.mark.parametrize("options", OPTIONS, ids=["dataset", "no-dataset"])
@pytest.mark.parametrize(("fixture", "needs", "checksums"), CASES,
                         ids=[f + (" checksums" if c else "") for f, _, c in CASES])
def test_bare_is_the_base_without_refkv_s_declarations(tmp_path, fixture, needs, options, checksums):
    """`check` finds nothing; the output is the base's entries but the mirror's (and, with
    `dataset: false`, the dataset layout's), each document's attributes as `Base.attributes`
    gives them; zarr reads every array as the base's, in memory and from a saved archive."""
    if needs is not None:
        pytest.importorskip(needs, reason=f"{fixture}'s arrays need {needs} to decode")
    path = str(FIXTURES / fixture)
    b = base(path, checksums=checksums)
    plugin = Bare()
    out = plugin.write(b, plugin.options(options))
    assert [p for p in check(b, out) if p.code not in ALLOWED] == []

    store = zarrify(path, layout=Layout(NAME, options), checksums=checksums)
    whole = zarrify(path, checksums=checksums)
    assert store.info["layouts"] == [NAME]
    docs, theirs = _documents(store), _documents(whole)
    root = theirs["zarr.json"]["attributes"]["refkv_zarrified"]  # read here as the base declares it
    left_out = [f"{MIRROR}/"] + ([] if options.get("dataset", True) else
                                 [f"{p}/" for p in root.get("layouts", {}).values()])
    assert b.datasets == root.get("layouts", {})
    assert b.mirror == MIRROR
    assert sorted(store.mapping) == sorted(k for k in whole.mapping if not k.startswith(tuple(left_out)))
    if fixture.startswith("mrc/") and not options.get("dataset", True):
        assert not any(k.startswith("dataset/") for k in store.mapping)

    for key, doc in docs.items():
        node = key.rpartition("/")[0]
        assert doc.get("attributes", {}) == b.attributes(node), key
        attrs = doc.get("attributes", {})
        assert "refkv_zarrified" not in attrs, key
        assert "refkv_provenance" not in attrs, key
        assert not any(isinstance(c, dict) and str(c.get("uuid", "")).lower() in UUIDS
                       for c in attrs.get("zarr_conventions", [])), key
        assert {k: v for k, v in doc.items() if k != "attributes"} == \
            {k: v for k, v in theirs[key].items() if k != "attributes"}, key
    for key, value in store.mapping.items():
        if key not in docs:
            assert value == whole.mapping[key], key

    expected = _arrays(whole)
    archive = tmp_path / "out.vzip"
    store.save_vzip(str(archive))
    assert sorted(refkv.open(str(archive)).list()) == sorted(store.mapping)
    saved = zarr.open_group(refkv.zarr.open(str(archive)), mode="r")
    for arrays in (_arrays(store), {p: a for p, a in saved.members(max_depth=None) if isinstance(a, zarr.Array)}):
        assert set(arrays) == {p for p in expected if not f"{p}/".startswith(tuple(left_out))}
        for p, a in arrays.items():
            assert _same(np.asarray(a[...]), np.asarray(expected[p][...])), p


# `_digest` in a process without zarr-metadata, which `bare` does not need (the `layouts` extra)
WITHOUT_THE_EXTRA = """
import hashlib, json, sys
sys.modules["zarr_metadata"] = None
from refkv.zarrify.layouts import base, encode_entries
from refkv.zarrify.layouts.examples.bare import Bare
b, plugin = base(sys.argv[1]), Bare()
out = plugin.write(b, plugin.options(json.loads(sys.argv[2])))
print(hashlib.sha256(encode_entries(out)).hexdigest())
"""


def test_bare_is_deterministic():
    """Another process, with another hash seed and without the `layouts` extra, writes the
    same output."""
    path, options = str(FIXTURES / "mrc/mrc_chunk_rows.mrc"), {"dataset": False}
    env = {**os.environ, "PYTHONHASHSEED": "12345"}
    run = subprocess.run([sys.executable, "-c", WITHOUT_THE_EXTRA, path, json.dumps(options)],
                         env=env, capture_output=True, text=True, check=True)
    assert run.stdout.strip() == _digest(path, options)

def _cli(*args):
    return subprocess.run([sys.executable, "-m", "refkv.zarrify", *args], capture_output=True, text=True,
                          check=False)


def test_the_cli_writes_bare(tmp_path):
    out = tmp_path / "out.vzip"
    run = _cli(str(FIXTURES / "mrc/mrc_chunk_rows.mrc"), str(out), "--layout", NAME,
               "--layout-options", '{"dataset": false}')
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["layouts"] == [NAME]
    keys = refkv.open(str(out)).list()
    assert "zarr.json" in keys
    assert not any(k.startswith(("refkv_source/", "dataset/")) for k in keys)


def test_an_unknown_option_is_a_usage_error():
    with pytest.raises(LayoutUsageError, match="unknown options"):
        zarrify(str(FIXTURES / "tiff/bilevel_tiled.tif"), layout=Layout(NAME, {"mirror": False}))


def test_a_dataset_option_not_a_boolean_is_a_usage_error():
    with pytest.raises(LayoutUsageError, match="dataset is true or false"):
        zarrify(str(FIXTURES / "tiff/bilevel_tiled.tif"), layout=Layout(NAME, {"dataset": "no"}))


def test_the_cli_refuses_layout_options_without_a_layout(tmp_path):
    run = _cli(str(FIXTURES / "tiff/bilevel_tiled.tif"), str(tmp_path / "out.vzip"), "--layout-options", "{}")
    assert run.returncode == 2
    assert "--layout-options without --layout" in run.stderr


def test_the_cli_refuses_two_layouts(tmp_path):
    run = _cli(str(FIXTURES / "tiff/bilevel_tiled.tif"), str(tmp_path / "out.vzip"), "--layout", NAME,
               "--layout", NAME)
    assert run.returncode == 2
    assert "given twice" in run.stderr


def test_the_cli_refuses_an_unknown_layout(tmp_path):
    run = _cli(str(FIXTURES / "tiff/bilevel_tiled.tif"), str(tmp_path / "out.vzip"), "--layout",
               "refkv.example.no-such-layout")
    assert run.returncode == 2
    assert "no layout plugin is named" in run.stderr
    assert not (tmp_path / "out.vzip").exists()


def test_bare_imports_only_public_modules():
    """The module imports the standard library and refkv's public API (not `hierarchy`, so that
    it runs without the `layouts` extra), at its top, and no underscore name."""
    tree = ast.parse((ROOT / "python/src/refkv/zarrify/layouts/examples/bare.py").read_text())
    allowed = {"refkv", "refkv.ir", "refkv.zarrify", "refkv.zarrify.layouts", "refkv.zarrify.coding"}
    found = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert all(n in tree.body for n in found)
    modules = [a.name for n in found if isinstance(n, ast.Import) for a in n.names]
    modules += [n.module or "" for n in found if isinstance(n, ast.ImportFrom)]
    assert all(m in allowed or m.split(".")[0] in sys.stdlib_module_names for m in modules), modules
    assert not [a.name for n in found for a in n.names if a.name.startswith("_")]
