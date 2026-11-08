"""Inputs read through the application's object stores (`registry=`): a file, a file with the
files beside it, and a store, under a prefix registered for an obspec store."""

from __future__ import annotations

from pathlib import Path

import pytest
from obspec_utils.registry import ObjectStoreRegistry
from obstore.store import LocalStore, MemoryStore

import refkv
from refkv.errors import ResolutionError
from refkv.zarrify import Hdf5Source, NrrdSource, StoreLimit, TiffSource, Zarr2Store, zarrify, zarrify_store

FIXTURES = Path(__file__).parents[2] / "fixtures"
BUCKET = "s3://bucket"
# the inputs, by fixture path: files, files that name files beside them, and a store
PLAIN = ["tiff/bilevel.tif", "hdf5/hdf5_netcdf4.h5"]
FILES = [*PLAIN, "hdf5/hdf5_external.h5", "nrrd/nrrd_detached_raw.nhdr"]
BESIDE = ["hdf5/hdf5_external_a.bin", "hdf5/hdf5_external_b.bin", "hdf5/hdf5_external_target.h5",
          "nrrd/nrrd_detached_raw.raw"]
STORE = "zarr2/zarr2_codecs"
CLASSES = {"tiff": TiffSource, "hdf5": Hdf5Source, "nrrd": NrrdSource}


def _values(store) -> dict:
    return {k: store.get(k) for k in store.list()}


@pytest.fixture(scope="module")
def bucket() -> MemoryStore:
    """The fixtures' inputs at their paths in a store, as an S3 bucket's objects."""
    mem = MemoryStore()
    for rel in FILES + BESIDE:
        mem.put(rel, (FIXTURES / rel).read_bytes())
    for f in (FIXTURES / STORE).rglob("*"):
        if f.is_file():
            mem.put(f.relative_to(FIXTURES).as_posix(), f.read_bytes())
    return mem


@pytest.mark.parametrize("form", ["mapping", "registry", "class"])
@pytest.mark.parametrize("rel", [*FILES, STORE + "/"])
def test_an_input_under_a_registered_prefix_is_read_with_its_store(bucket, rel, form):
    """The input, its files beside it and the store's objects are read with the registered store,
    and the result is what a local run gives: the same keys, metadata and chunk bytes, read
    through the registry the result keeps. (The local run is named by https URLs, which a local
    directory's must be, so its root document names those.) A file read alone is source 0, which
    pins the ETag the store gives."""
    url = f"{BUCKET}/{rel}"
    registry = {BUCKET: bucket} if form == "mapping" else ObjectStoreRegistry({BUCKET: bucket})
    store = rel.endswith("/")
    if form == "class":
        fmt = rel.split("/")[0]
        got = zarrify(Zarr2Store(url, registry=registry) if store else CLASSES[fmt](url, registry=registry))
    else:
        got = zarrify(url, registry=registry)
    local = zarrify(str(FIXTURES / rel), f"https://h/{rel}", registry={"https://h": LocalStore(FIXTURES)})
    want = _values(local)
    want["zarr.json"] = want["zarr.json"].replace(b"https://h/", f"{BUCKET}/".encode())
    assert _values(got) == want
    urls = [s.url for s in got.sources if s.url is not None]
    assert urls
    assert all(u.startswith(f"{BUCKET}/{rel.split('/')[0]}/") for u in urls)
    if rel in PLAIN:
        assert got.sources[0].url == url
        assert got.sources[0].etag == bucket.head(rel)["e_tag"]


def test_a_url_of_a_scheme_with_no_registered_store_is_refused(bucket):
    with pytest.raises(ResolutionError, match=r"no store is registered for s3://other.*registry="):
        zarrify("s3://other/tiff/bilevel.tif", registry={BUCKET: bucket})


def test_a_registered_url_outside_the_policys_prefixes_is_refused(bucket):
    policy = refkv.Policy(prefixes=("https://",))
    with pytest.raises(ResolutionError, match="not under an allowed URL prefix"):
        zarrify(f"{BUCKET}/tiff/bilevel.tif", registry={BUCKET: bucket}, policy=policy)


def test_a_registered_store_listing_more_than_max_objects_is_refused(bucket):
    with pytest.raises(StoreLimit, match="lists more than 2 objects"):
        zarrify_store(f"{BUCKET}/{STORE}/", registry={BUCKET: bucket}, max_objects=2)


def test_a_registry_beside_a_source_instance_is_refused(bucket):
    with pytest.raises(TypeError, match="registry: a source's read options are its own"):
        zarrify(TiffSource(f"{BUCKET}/tiff/bilevel.tif"), registry={BUCKET: bucket})
