"""Check vzip's base install, without its extras: run with the Python of an
environment where only `pip install .` was run (CI's base-install job).

It checks that `import vzip` works without xarray, which vzip does not depend
on, builds a ReferenceStore from a mapping, saves it, reopens the archive with
vzip.open, and checks that each optional part names the extra it needs."""

import asyncio
import importlib
import os
import sys
import tempfile

import zarr

import vzip
from vzip.pb import Range, Source

for name in ["xarray", "virtualizarr", "imagecodecs", "pytest"]:
    assert importlib.util.find_spec(name) is None, f"{name} is installed: not a base install"
assert "xarray" not in sys.modules, "importing vzip imported xarray"

with tempfile.TemporaryDirectory() as d:
    os.chdir(d)
    with open("src.bin", "wb") as f:
        f.write(bytes(range(16)))
    mem = zarr.storage.MemoryStore()
    zarr.create_array(mem, name="x", shape=(4,), chunks=(4,), dtype="u1", compressors=None)
    mapping = {k: v.to_bytes() for k, v in mem._store_dict.items()}
    mapping["x/c/0"] = Range(source=0, offset=4, length=4)
    store = vzip.ReferenceStore(mapping, [Source(url="file://" + os.path.abspath("src.bin"))])
    assert list(zarr.open_array(store, path="x", mode="r")[:]) == [4, 5, 6, 7]
    print(store.save("m.vzip"))
    reopened = vzip.open("m.vzip")
    assert list(zarr.open_array(reopened, path="x", mode="r")[:]) == [4, 5, 6, 7]
    assert reopened.max_concurrency == 8
    os.chdir("/")

for extra, attempt in [
    ("virtualizarr", lambda: vzip.ReferenceStore.from_virtualizarr(None)),
    ("virtualizarr", lambda: importlib.import_module("vzip.shards")),
    ("images", lambda: asyncio.run(vzip.codecs.JpegCodec()._decode_single(None, None))),
]:
    try:
        attempt()
    except ImportError as e:
        assert f"vzip[{extra}]" in str(e), e
        print(f"ImportError naming vzip[{extra}]: {e}")
    else:
        sys.exit(f"expected an ImportError naming vzip[{extra}]")
assert "xarray" not in sys.modules, "vzip imported xarray"
print("base install: ok, without xarray")
