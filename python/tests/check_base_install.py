"""Check vzip's installs without the test environment: run with the Python of an
environment where only `pip install .` was run (CI's base-install job), or, with
the argument `zarr`, only `pip install '.[zarr]'`.

The base install: `import vzip` imports neither xarray nor zarr, which are not
installed, and the operations work without them: build a ReferenceStore from a
mapping, read a bytes value and a reference, save it, reopen the archive with
vzip.open and read it again, run the conformance CLI on it, and virtualize a
NIfTI file and a TIFF, both through the Rust core (vzip_ir), which the install
must have built, and a Zarr v2 store. Each optional part names the extra it needs.

With `zarr`: vzip.zarr reads the same archive through zarr, and the image
codecs and the VirtualiZarr conversion name their extras."""

import asyncio
import importlib
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

import vzip
from vzip.pb import Concat, Range, Source

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
mode = sys.argv[1] if len(sys.argv) > 1 else "base"
assert mode in ("base", "zarr"), f"usage: check_base_install.py [base|zarr], not {mode!r}"
absent = ["xarray", "virtualizarr", "imagecodecs", "pytest"] + (["zarr"] if mode == "base" else [])
for name in absent:
    assert importlib.util.find_spec(name) is None, f"{name} is installed: not a {mode} install"
assert "xarray" not in sys.modules and "zarr" not in sys.modules, "importing vzip imported zarr"


def expect_import_error(extra: str, attempt) -> None:
    try:
        attempt()
    except ImportError as e:
        assert f"vzip[{extra}]" in str(e), e
        print(f"ImportError naming vzip[{extra}]: {e}")
    else:
        sys.exit(f"expected an ImportError naming vzip[{extra}]")


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
    store = vzip.ReferenceStore(mapping, [Source(url="file://" + os.path.abspath("src.bin"))])
    print(store.save("m.vzip"))
    reopened = vzip.open("m.vzip")
    for s in (store, reopened):
        assert s.get("x/zarr.json") == metadata
        assert s.get("x/c/0") == bytes([4, 5, 6, 7])
        assert s.get("x/c/0", vzip.RangeRequest(1, 3)) == bytes([5, 6])
        assert s.get("y") == b"<\x00\x01"
        assert [s.classify(k) for k in ("x/zarr.json", "x/c/0", "nope")] == [
            "bytes", "reference", "missing"]
        assert s.list() == ["x/c/0", "x/zarr.json", "y"] and s.list_dir("") == ["x", "y"]
        assert s.raw("x/c/0") == Range(source=0, offset=4, length=4).encode()
    assert reopened.max_concurrency == 8

    from vzip import cli

    with open("q.json", "w") as f:
        json.dump([{"op": "get", "key": "x/c/0", "range": {"suffix": 2}},
                   {"op": "classify", "key": "y"}, {"op": "get_raw", "key": "__vz__/sources"},
                   {"op": "list", "prefix": "x/"}], f)
    out = cli.read("m.vzip", "q.json")
    assert out["open"]["ok"] and [r["ok"] for r in out["results"]] == [True] * 4, out
    assert out["results"][0]["value"] == "0607" and out["results"][1]["kind"] == "reference"
    assert out["results"][3]["keys"] == ["x/c/0", "x/zarr.json"]

    from vzip.virtualize import virtualize, virtualize_store

    fmt, o = virtualize(str(FIXTURES / "nifti" / "nifti_block_x_split.nii"))
    o.write("n.vzip")
    fmt2, o2 = virtualize_store(str(FIXTURES / "zarr2" / "zarr2_hierarchy"),
                                "https://example.test/z/")
    o2.write("z.vzip")
    assert (fmt, fmt2) == ("nifti", "zarr2") and vzip.open("n.vzip").get("zarr.json")
    import vzip_ir  # noqa: F401  (a dependency: installing vzip built it)

    fmt3, o3 = virtualize(str(FIXTURES / "tiff" / "jpeg_gray.tif"))
    o3.write("t.vzip")
    assert fmt3 == "tiff" and vzip.open("t.vzip").get("zarr.json")

    if mode == "zarr":
        import zarr

        import vzip.zarr

        for s in (vzip.zarr.store(store), vzip.zarr.open("m.vzip")):
            assert list(zarr.open_array(s, path="x", mode="r")[:]) == [4, 5, 6, 7]
    os.chdir("/")

if mode == "base":
    assert "zarr" not in sys.modules, "the base operations imported zarr"
    expect_import_error("zarr", lambda: vzip.zarr)
    expect_import_error("zarr", lambda: importlib.import_module("vzip.zarr"))
else:
    import vzip.zarr.codecs

    expect_import_error("images", lambda: asyncio.run(
        vzip.zarr.codecs.JpegCodec()._decode_single(None, None)))
expect_import_error("virtualizarr", lambda: vzip.ReferenceStore.from_virtualizarr(None))
expect_import_error("virtualizarr", lambda: importlib.import_module("vzip.shards"))
expect_import_error("virtualizarr", lambda: importlib.import_module("vzip.convert"))
assert "xarray" not in sys.modules, "vzip imported xarray"
print(f"{mode} install: ok, without xarray" + (" or zarr" if mode == "base" else ""))
