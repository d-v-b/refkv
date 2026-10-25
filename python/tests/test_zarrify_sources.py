"""Inputs named by their profile, and the layouts a hierarchy holds (spec/zarrify.md §1.7
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#17-named-inputs-and-layouts>): the
source and store classes made from the registry, and `layouts=`."""

import sys
from pathlib import Path

import pytest

from refkv._narrow import string
from refkv.policy import Policy
from refkv.zarrify import (
    Dataset,
    Image,
    MrcSource,
    N5Store,
    NdpiSource,
    OmeZarrStore,
    QptiffSource,
    Rejected,
    SafeSource,
    SafeStore,
    TiffSource,
    Zarr2Store,
    zarrify,
)
from refkv.zarrify.profile_classes import CLASSES
from refkv.zarrify.profiles import REGISTRY
from refkv.zarrify.store import WORKERS

sys.path.insert(0, str(Path(__file__).parents[2] / "conformance" / "zarrify"))
from proxy import Proxy

FIXTURES = Path(__file__).parents[2] / "fixtures"
LOOPBACK = Policy(allow_private_hosts=True)  # the proxy serves the stores on 127.0.0.1
TIFF = str(FIXTURES / "tiff/jpeg_gray.tif")
NDPI = str(FIXTURES / "ndpi/ndpi_levels.ndpi")
MRC = str(FIXTURES / "mrc/mrc_chunk_sections.mrc")


@pytest.fixture(scope="module")
def proxy(tmp_path_factory):
    return Proxy(FIXTURES, tmp_path_factory.mktemp("cache"))


def store_url(proxy, name: str) -> str:
    """A fixture store's URL, without its trailing "/"."""
    return proxy.local_store(name).removesuffix("/")


def test_classes_follow_the_registry():
    want = set()
    for entry in REGISTRY:
        parts = "".join(p.capitalize() for p in string(entry["name"]).split("-"))
        kind = entry["kind"]
        want |= {parts + "Source"} if kind in ("file", "both") else set()
        want |= {parts + "Store"} if kind in ("store", "both") else set()
    assert set(CLASSES) == want
    assert all(CLASSES[name].profile for name in want)
    assert {"TiffSource", "NdpiSource", "QptiffSource", "OmeZarrStore", "SafeSource", "SafeStore"} <= want


@pytest.mark.parametrize(("location", "url"), [
    ("https://h/d.zarr", "https://h/d.zarr/"),
    ("https://h/d.zarr/", "https://h/d.zarr/"),
    ("https://h/d.zarr?sig=a", "https://h/d.zarr/?sig=a"),
    ("https://h/d.zarr#f", "https://h/d.zarr/#f"),
    ("/local/d.zarr", "/local/d.zarr"),
])
def test_store_url(location, url):
    assert Zarr2Store(location).store_url == url


# (the input, given the proxy; layouts; the format; whether the dataset layout is written)
CASES = [
    (lambda _p: TiffSource(TIFF), None, "tiff", False),
    (lambda _p: TiffSource(NDPI), None, "ndpi", False),  # the variant the TIFF parser finds
    (lambda _p: NdpiSource(NDPI), None, "ndpi", False),
    (lambda _p: QptiffSource(str(FIXTURES / "qptiff/qptiff_fl_uint8_lzw.qptiff")), None, "qptiff", False),
    (lambda _p: SafeSource(str(FIXTURES / "safe/safe_l2a_pb0207.SAFE.zip")), None, "safe", False),
    (lambda _p: MrcSource(MRC), None, "mrc", True),
    (lambda _p: MrcSource(MRC), [Image(), Dataset()], "mrc", True),
    (lambda _p: MrcSource(MRC), [Image()], "mrc", False),
    (lambda _p: MRC, [Image()], "mrc", False),
    (lambda p: Zarr2Store(store_url(p, "zarr2/zarr2_hierarchy"), policy=LOOPBACK), None, "zarr2", False),
    (lambda p: Zarr2Store(store_url(p, "ome-zarr/ome_zarr_image_2d"), policy=LOOPBACK), None, "ome-zarr", False),
    (lambda p: OmeZarrStore(store_url(p, "ome-zarr/ome_zarr_image_2d"), policy=LOOPBACK), [Image()], "ome-zarr", False),
    (lambda p: N5Store(store_url(p, "n5/n5_hierarchy"), policy=LOOPBACK), None, "n5", False),
    (lambda p: SafeStore(store_url(p, "safe/safe_l2a_pb0207"), policy=LOOPBACK), None, "safe", False),
    (lambda _p: Zarr2Store(str(FIXTURES / "zarr2/zarr2_hierarchy"), url="https://data.test/z/"), None, "zarr2", False),
]


@pytest.mark.parametrize(("make", "layouts", "fmt", "dataset"), CASES, ids=range(len(CASES)))
def test_named_inputs_and_layouts(proxy, make, layouts, fmt, dataset):
    store = zarrify(make(proxy), layouts=layouts)
    assert store.info["format"] == fmt
    keys = store.list()
    assert any(k.startswith("dataset/") for k in keys) == dataset
    if fmt == "mrc":
        assert store.info["layouts"] == (["dataset"] if dataset else [])


def test_layouts_without_the_image_are_rejected():
    with pytest.raises(ValueError, match="must hold Image"):
        zarrify(MrcSource(MRC), layouts=[Dataset()])


# a named input's layouts are checked before it is read: these hosts do not resolve
@pytest.mark.parametrize(("call", "fmt"), [
    (lambda layouts: zarrify(TiffSource("https://unreachable.test/a.tif"), layouts=layouts), "tiff"),
    (lambda layouts: zarrify(Zarr2Store("https://unreachable.test/a.zarr"), layouts=layouts), "zarr2"),
    (lambda layouts: zarrify(OmeZarrStore("https://unreachable.test/a.zarr"), layouts=layouts), "ome-zarr"),
], ids=["source", "store", "ome-zarr store"])
def test_a_dataset_layout_the_format_lacks_is_rejected(call, fmt):
    with pytest.raises(ValueError, match=f"{fmt} has no dataset layout"):
        call([Image(), Dataset()])


def test_a_layout_that_is_not_one_is_rejected():
    with pytest.raises(TypeError, match="a layout is Image"):
        zarrify(MrcSource(MRC), layouts=[Image(), "dataset"])  # pyrefly: ignore[bad-argument-type] (the wrong type)


def test_a_tiff_variant_not_found_is_rejected():
    with pytest.raises(Rejected, match="not NDPI: the TIFF parser read it as tiff"):
        zarrify(NdpiSource(TIFF))


def test_an_ome_zarr_store_without_its_layout_is_rejected(proxy):
    with pytest.raises(Rejected, match=r"not an OME-Zarr 0\.4 store"):
        zarrify(OmeZarrStore(store_url(proxy, "zarr2/zarr2_hierarchy"), policy=LOOPBACK))


def test_a_store_of_another_profile_is_rejected(proxy):
    with pytest.raises(Rejected, match="not an N5 store"):
        zarrify(N5Store(store_url(proxy, "zarr2/zarr2_hierarchy"), policy=LOOPBACK))


def test_a_file_of_another_profile_is_rejected():
    with pytest.raises(Rejected):
        zarrify(MrcSource(TIFF))


# a read option beside a source, even a falsy value or the default
@pytest.mark.parametrize("option", [{"checksums": True}, {"checksums": False}, {"url": ""}, {"connections": 0},
                                    {"byte_cost": 0.0}, {"workers": WORKERS}], ids=str)
@pytest.mark.parametrize("source", [TiffSource(TIFF), Zarr2Store(str(FIXTURES / "zarr2/zarr2_hierarchy"))],
                         ids=["source", "store"])
def test_a_read_option_beside_a_source_is_rejected(source, option):
    with pytest.raises(TypeError, match=f"{next(iter(option))}: a source's read options are its own"):
        zarrify(source, **option)
