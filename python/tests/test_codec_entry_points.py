"""refkv's codecs as zarr finds them: through the `zarr.codecs` entry points of
python/pyproject.toml, by name, with no `import refkv.zarr` first, alongside the codecs of
zarr-python and imagecodecs-zarr that refkv's archives also name. A codec whose decoder needs
an extra names it when a chunk is decoded, not when zarr loads the codec."""

from __future__ import annotations

import importlib.metadata
import json
import pkgutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import zarr
import zarr.registry
from obstore.store import LocalStore

import refkv
import refkv.zarr
import refkv.zarr.codecs
from refkv._narrow import ndarray
from refkv.reader import ReferenceReader
from refkv.zarr.codecs.n5_default import N5BlockError
from refkv.zarrify import zarrify, zarrify_store

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"

# For every codec an archive of refkv's names, a fixture whose zarrified array (at the path given)
# names it: refkv's own (its entry points), zarr-python's `numcodecs.zlib` and imagecodecs-zarr's
# image codecs and `imagecodecs_delta`. The LZW fixture's final code is one bit too narrow, which imagecodecs-zarr's
# `imagecodecs_lzw` would decode to too many bytes: it reads only through `refkv.tiff_lzw`.
EVERY_CODEC = {
    "refkv.tiff_lzw": ("qptiff/qptiff_fl_lzw_narrow_eoi.qptiff", "0"),
    "refkv.stream": ("mp3/mp3_single_frame.mp3", "samples"),
    "refkv.fits_tiles": ("fits/fits_comp_gzip1.fits", "1"),
    "refkv.skip": ("npy/npz_compressed.npz", "fortran"),
    "n5_default": ("n5/n5_compressions", "zlib_f64"),
    "numcodecs.zlib": ("tiff/tczyx_uint16_deflate.ome.tif", "0"),
    "imagecodecs_jpeg": ("dicom/dicom_jpeg_single_frame_fragments.dcm", "0"),
    "imagecodecs_jpeg2k": ("dicom/dicom_j2k_signed16.dcm", "0"),
    "imagecodecs_ljpeg": ("dicom/dicom_ljpeg_signed16_eot.dcm", "0"),
    "imagecodecs_jpegxr": ("ndpi/ndpi_jpegxr_bgr.ndpi", "0"),
    "imagecodecs_delta": ("tiff/predictor_lzw_uint16.tif", "0"),
}

# The zarr-extensions codec that another package (zarr-n5) also registers, and its implementation
# in refkv, by the name zarr's config gives it: with both installed, the config chooses one.
SHARED = {"n5_default": "refkv.zarr.codecs.n5_default.N5DefaultCodec"}

# Run in a fresh interpreter, so that no earlier import registers a codec: opens each array of a
# plain directory store with zarr alone, and saves what it reads. zarr's warnings are errors, so
# that a name two installed packages register (zarr then picks one arbitrarily, and warns) fails,
# but for SHARED's, which the config names as a reader with zarr-n5 installed would.
READ_WITH_ZARR_ALONE = f"""
import sys, warnings
import numpy as np
import zarr
from zarr.errors import ZarrUserWarning
from zarr.storage import LocalStore
warnings.simplefilter("error", ZarrUserWarning)
zarr.config.set({ {f"codecs.{name}": cls for name, cls in SHARED.items()}!r})
assert not [m for m in sys.modules if m == "refkv" or m.startswith("refkv.")], "zarr imported refkv"
root, out, *paths = sys.argv[1:]
for i, path in enumerate(paths):
    np.save(f"{{out}}/{{i}}.npy", zarr.open_array(LocalStore(root, read_only=True), path=path, mode="r")[...])
"""

# Run in a fresh interpreter: has zarr load refkv's `n5_default` through its entry point, and
# prints the config's choice of implementation afterwards.
LOAD_N5_DEFAULT = """
import warnings
import zarr
import zarr.registry
from zarr.errors import ZarrUserWarning
warnings.simplefilter("ignore", ZarrUserWarning)  # zarr-n5's n5_default, when it is installed
zarr.registry.get_codec_class("n5_default")
import refkv.zarr.codecs.n5_default  # loaded already, by the entry point
print(zarr.config.get("codecs.n5_default", None))
"""


def _codec_names(codecs: list[dict[str, object]]) -> set[str]:
    """The names of `codecs` and of the codecs within them (n5_default's, sharding's)."""
    names: set[str] = set()
    for c in codecs:
        names.add(str(c["name"]))
        inner = c.get("configuration")
        if isinstance(inner, dict) and isinstance(inner.get("codecs"), list):
            names |= _codec_names(inner["codecs"])
    return names


def _zarrified(fixture: str, tmp_path: Path) -> ReferenceReader:
    """The fixture zarrified; a store's archive, whose objects are at an https URL, read from
    the fixtures through a registry."""
    path = FIXTURES / fixture
    if not path.is_dir():
        return zarrify(str(path))
    zarrify_store(str(path), f"https://example.test/{path.name}/").save(str(tmp_path / f"{path.name}.vzip"))
    return refkv.open(str(tmp_path / f"{path.name}.vzip"), registry={"https://example.test": LocalStore(path.parent)})


def _refkv_entry_points() -> dict[str, importlib.metadata.EntryPoint]:
    return {e.name: e for e in importlib.metadata.distribution("refkv").entry_points if e.group == "zarr.codecs"}


def test_entry_points_name_every_codec_and_resolve_to_it():
    """Every module of refkv.zarr.codecs has an entry point, under the name its codec registers,
    and zarr resolves that name to the entry point's class."""
    modules = {m.name for m in pkgutil.iter_modules(refkv.zarr.codecs.__path__) if not m.name.startswith("_")}
    points = _refkv_entry_points()
    assert {e.value.partition(":")[0] for e in points.values()} == {f"refkv.zarr.codecs.{m}" for m in modules}
    assert set(points) == {"refkv.tiff_lzw", "refkv.stream", "refkv.fits_tiles", "refkv.skip", "n5_default"}
    for name, e in points.items():
        with zarr.config.set({f"codecs.{name}": SHARED[name]} if name in SHARED else {}):
            assert zarr.registry.get_codec_class(name) is e.load(), name
    # refkv's own codecs are named refkv.<codec>, but n5_default, a zarr-extensions codec
    assert {n for n in points if not n.startswith("refkv.")} == {"n5_default"}


def test_zarr_alone_decodes_every_codec_of_refkvs_archives(tmp_path):
    """A plain directory copy of each archive's array, read by zarr in an interpreter that imports
    refkv only through zarr's loading of its entry points, gives what refkv.zarr reads."""
    root, out = tmp_path / "store", tmp_path / "out"
    out.mkdir()
    paths, expected = [], []
    for i, (name, (fixture, path)) in enumerate(EVERY_CODEC.items()):
        store = _zarrified(fixture, tmp_path)
        doc = json.loads(store.get(f"{path}/zarr.json") or b"{}")
        assert name in _codec_names(doc["codecs"]), (name, fixture)
        for key in store.list(f"{path}/"):
            if (value := store.get(key)) is not None:
                (root / str(i) / key).parent.mkdir(parents=True, exist_ok=True)
                (root / str(i) / key).write_bytes(value)
        paths.append(f"{i}/{path}")
        expected.append(ndarray(zarr.open_array(refkv.zarr.store(store), path=path, mode="r")[...]))
    subprocess.run([sys.executable, "-I", "-c", READ_WITH_ZARR_ALONE, str(root), str(out), *paths],
                   check=True)
    for i, (name, want) in enumerate(zip(EVERY_CODEC, expected, strict=True)):
        np.testing.assert_array_equal(np.load(out / f"{i}.npy"), want, err_msg=name)


def _read_with(n5_default: str, reader: ReferenceReader, path: str) -> np.ndarray:
    """The array at `path` of `reader`, its `n5_default` chunks decoded by the implementation of that
    name."""
    with zarr.config.set({"codecs.n5_default": n5_default}):
        return ndarray(zarr.open_array(refkv.zarr.store(reader), path=path, mode="r")[...])


def test_zarr_n5s_n5_default_reads_what_refkvs_does(tmp_path):
    """Every N5 fixture array refkv's `n5_default` reads, zarr-n5's reads the same, so that a reader
    with both installed may choose either (docs/zarrify/n5/README.md)."""
    pytest.importorskip("zarr_n5")
    arrays, refused = 0, set()
    for store in sorted(p for p in (FIXTURES / "n5").iterdir() if p.is_dir() and not p.name.startswith("n5_reject")):
        reader = _zarrified(f"n5/{store.name}", tmp_path)
        for key in reader.list():
            doc = json.loads(reader.get(key) or b"{}") if key.endswith("zarr.json") else {}
            if doc.get("node_type") != "array" or doc["codecs"][0]["name"] != "n5_default":
                continue
            path = key.removesuffix("zarr.json").rstrip("/")
            try:
                ours = _read_with(SHARED["n5_default"], reader, path)
            except N5BlockError:  # a block refkv's codec refuses
                refused.add(f"{store.name}/{path}")
                continue
            arrays += 1
            np.testing.assert_array_equal(_read_with("zarr_n5.codec.default.N5DefaultCodec", reader, path), ours,
                                          err_msg=f"{store.name}/{path}")
    assert (arrays, refused) == (51, {"n5_edge_varlength_block/v"})  # a varlength-mode block


def test_loading_refkvs_n5_default_leaves_the_choice_to_zarrs_config():
    """refkv names no implementation of `n5_default` in zarr's config, so that having zarr load
    refkv's (as it does for any array that names `n5_default`) does not override zarr-n5's."""
    out = subprocess.run([sys.executable, "-I", "-c", LOAD_N5_DEFAULT], check=True, capture_output=True,
                         text=True).stdout
    assert out.strip() == "None"


def _read_without(module: str, fixture: str, path: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = refkv.zarr.store(_zarrified(fixture, tmp_path))
    monkeypatch.setitem(sys.modules, module, None)  # as if it were not installed
    zarr.open_array(store, path=path, mode="r")[...]


def test_tiff_lzw_without_imagecodecs_names_the_images_extra(monkeypatch, tmp_path):
    with pytest.raises(ImportError, match=r"refkv\[images\]"):
        _read_without("imagecodecs", *EVERY_CODEC["refkv.tiff_lzw"], monkeypatch, tmp_path)


def test_stream_without_pyav_names_the_media_extra(monkeypatch, tmp_path):
    with pytest.raises(ImportError, match=r"refkv\[media\]"):
        _read_without("av", *EVERY_CODEC["refkv.stream"], monkeypatch, tmp_path)
