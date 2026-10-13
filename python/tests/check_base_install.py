"""Check refkv's installs without the test environment: run with the Python of an
environment where only `pip install .` was run (CI's base-install job), or, with
the argument `zarr`, only `pip install '.[zarr]'`.

The base install: `import refkv` imports neither xarray nor zarr, which are not
installed, and the operations work without them: build a ReferenceStore from a
mapping, read a bytes value and a reference, save it, reopen the archive with
refkv.open and read it again, run the conformance CLI on it, and virtualize a
NIfTI file and a TIFF, both through the Rust core (refkv._ir), which the install
must have built, and a Zarr v2 store. Each optional part names the extra it needs.

With `zarr`: refkv.zarr reads the same archive through zarr, and the image
codecs and the VirtualiZarr conversion name their extras."""

import asyncio
import importlib
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

import refkv
from refkv.pb import Concat, Range, Source
from refkv.uri import file_uri

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
mode = sys.argv[1] if len(sys.argv) > 1 else "base"
assert mode in ("base", "zarr"), f"usage: check_base_install.py [base|zarr], not {mode!r}"
absent = ["xarray", "virtualizarr", "imagecodecs", "pytest"] + (["zarr"] if mode == "base" else [])
for name in absent:
    assert importlib.util.find_spec(name) is None, f"{name} is installed: not a {mode} install"
assert "xarray" not in sys.modules and "zarr" not in sys.modules, "importing refkv imported zarr"
assert not {"refkv.vzip.archive", "refkv.vzip.store"} & set(sys.modules), "importing refkv imported the ZIP code"


def expect_import_error(extra: str, attempt) -> None:
    try:
        attempt()
    except ImportError as e:
        assert f"refkv[{extra}]" in str(e), e
        print(f"ImportError naming refkv[{extra}]: {e}")
    else:
        sys.exit(f"expected an ImportError naming refkv[{extra}]")


with tempfile.TemporaryDirectory() as d:
    os.chdir(d)
    with open("src.bin", "wb") as f:
        f.write(bytes(range(16)))
    metadata = json.dumps({"zarr_format": 3, "node_type": "array", "shape": [4], "data_type": "uint8",
                           "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [4]}},
                           "chunk_key_encoding": {"name": "default"}, "fill_value": 0,
                           "codecs": [{"name": "bytes"}]}).encode()
    mapping = {"x/zarr.json": metadata, "x/c/0": Range(source=0, offset=4, length=4),
               "y": Concat((Range(data=b"<"), Range(source=0, offset=0, length=2)))}
    store = refkv.ReferenceStore(mapping, [Source(url=file_uri("src.bin"))])
    print(store.save("m.vzip"))
    reopened = refkv.open("m.vzip")
    for s in (store, reopened):
        assert s.get("x/zarr.json") == metadata
        assert s.get("x/c/0") == bytes([4, 5, 6, 7])
        assert s.get("x/c/0", refkv.RangeRequest(1, 3)) == bytes([5, 6])
        assert s.get("y") == b"<\x00\x01"
        assert [s.classify(k) for k in ("x/zarr.json", "x/c/0", "nope")] == [
            "bytes", "reference", "missing"]
        assert s.list() == ["x/c/0", "x/zarr.json", "y"] and s.list_dir("") == ["x", "y"]
        assert s.raw("x/c/0") == Range(source=0, offset=4, length=4).encode()
    assert reopened.max_concurrency == 8

    from refkv.vzip import cli

    with open("q.json", "w") as f:
        json.dump([{"op": "get", "key": "x/c/0", "range": {"suffix": 2}},
                   {"op": "classify", "key": "y"}, {"op": "get_raw", "key": "__vz__/sources"},
                   {"op": "list", "prefix": "x/"}], f)
    out = cli.read("m.vzip", "q.json")
    assert out["open"]["ok"] and [r["ok"] for r in out["results"]] == [True] * 4, out
    assert out["results"][0]["value"] == "0607" and out["results"][1]["kind"] == "reference"
    assert out["results"][3]["keys"] == ["x/c/0", "x/zarr.json"]

    from refkv.virtualize import virtualize, virtualize_store

    n = virtualize(str(FIXTURES / "nifti" / "nifti_block_x_split.nii"))
    n.save("n.vzip")
    z = virtualize_store(str(FIXTURES / "zarr2" / "zarr2_hierarchy"), "https://example.test/z/")
    z.save("z.vzip")
    assert (n.info["format"], z.info["format"]) == ("nifti", "zarr2")
    saved = refkv.open("n.vzip")
    for key in ("zarr.json", "0/c/0/0"):  # in memory as saved
        assert n.get(key) is not None and n.get(key) == saved.get(key)
    from refkv import _ir  # noqa: F401  (a dependency: installing refkv built it)

    t = virtualize(str(FIXTURES / "tiff" / "jpeg_gray.tif"))
    t.save("t.vzip")
    assert t.info["format"] == "tiff" and refkv.open("t.vzip").get("zarr.json") == t.get("zarr.json")

    if mode == "zarr":
        import zarr

        import refkv.zarr

        for s in (refkv.zarr.store(store), refkv.zarr.open("m.vzip")):
            assert list(zarr.open_array(s, path="x", mode="r")[:]) == [4, 5, 6, 7]
    os.chdir("/")

if mode == "base":
    assert "zarr" not in sys.modules, "the base operations imported zarr"
    expect_import_error("zarr", lambda: refkv.zarr)
    expect_import_error("zarr", lambda: importlib.import_module("refkv.zarr"))
else:
    import refkv.zarr.codecs

    expect_import_error("images", lambda: asyncio.run(
        refkv.zarr.codecs.JpegCodec()._decode_single(None, None)))
expect_import_error("virtualizarr", lambda: refkv.ReferenceStore.from_virtualizarr(None))
expect_import_error("virtualizarr", lambda: importlib.import_module("refkv.shards"))
expect_import_error("virtualizarr", lambda: importlib.import_module("refkv.convert"))
assert "xarray" not in sys.modules, "refkv imported xarray"
print(f"{mode} install: ok, without xarray" + (" or zarr" if mode == "base" else ""))
