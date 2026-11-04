"""The OME-Zarr profile through the Rust core (refkv.ir.store; spec/zarrify/ome-zarr.md) on
the synthetic stores: the same outcome as the frozen reference (revision 25) for every store,
the same hierarchy outside `refkv_source`, and `refkv_source` the store's IR mirror, which
rebuilds every node document byte for byte (conventions §8.9
<https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#89-stores>). The reference's own
tests are python/tests/reference/test_zarrify_ome_zarr.py; the full comparison, browser
included, is conformance/zarrify/compare.py's.
"""

import json
import shutil
import sys
from pathlib import Path

import pytest

from refkv import _core
from refkv.ir.store import StoreIrOutput
from refkv.zarrify import Rejected
from refkv.zarrify import _zarrify as zarrify  # the profile output, not the store
from refkv.zarrify.common import CONVENTION_KEY

sys.path.insert(0, str(Path(__file__).parents[2] / "conformance" / "zarrify" / "reference"))
from refkv_reference import virtualize as reference
from refkv_reference.legacy import SOURCE_NODE as LEGACY_NODE
from refkv_reference.legacy import StoreOutput, current_names

FIXTURES = Path(__file__).parents[2] / "fixtures" / "ome-zarr"
URL = "https://data.test/ome-zarr/{}/"
STORES = sorted(p for p in FIXTURES.iterdir() if p.is_dir())
# The stores whose verdict the profile changed after the freeze (conformance/zarrify/revised.json).
REVISED_JSON = Path(__file__).parents[2] / "conformance" / "zarrify" / "revised.json"
REVISED = set(json.loads(REVISED_JSON.read_text())["fixtures"])


def whole_object(parts) -> str:
    """The key of the one listed object a reference's parts are, from its first byte."""
    (kind, key, offset, _), = parts
    assert kind == "object"
    assert offset == 0
    return key


def hierarchy(out) -> dict:
    """The IR path's documents outside refkv_source, and its references (entry key -> object key)."""
    docs = {k: json.loads(v) for k, v in out.entries.items()
            if k.endswith("zarr.json") and not k.startswith("refkv_source/")}
    return {"docs": docs, "refs": {k: whole_object(v) for k, v in out.refs.items()}}


def reference_hierarchy(out) -> dict:
    """The frozen reference's, with the current names (its revision's source metadata node
    is vzip_source)."""
    docs = current_names({k: json.loads(json.dumps(v)) for k, v in out.docs.items()
                          if not k.startswith(LEGACY_NODE + "/")})
    del docs["zarr.json"]["attributes"][CONVENTION_KEY]["revision"]
    current = lambda k: "refkv_source" + k[len(LEGACY_NODE):] if k.startswith(LEGACY_NODE + "/") else k  # noqa: E731
    return {"docs": docs, "refs": {current(k): out.origins.get(k, k) for k, _ in out.chunks}}


@pytest.mark.parametrize("store", [p for p in STORES if p.name not in REVISED],
                         ids=[p.name for p in STORES if p.name not in REVISED])
def test_the_ir_gives_the_reference_hierarchy(store):
    url = URL.format(store.name)
    try:
        expected = reference(str(store), url)
    except Rejected:
        with pytest.raises(Rejected):
            zarrify(str(store), url)
        return
    fmt, out = zarrify(str(store), url)
    assert isinstance(out, StoreIrOutput)
    assert isinstance(expected[1], StoreOutput)
    assert fmt == expected[0] == "ome-zarr"
    assert hierarchy(out) == reference_hierarchy(expected[1])
    assert out.sources == expected[1].sources
    for k in ("images", "labels", "droppedLabelLevels", "plates", "wells", "fields", "omeXml", "otherObjects",
              "chunks"):
        assert out.summary[k] == expected[1].summary[k], k


def test_the_mirror_rebuilds_each_node_document():
    for store in STORES:
        if "_reject_" in store.name:
            continue
        _, out = zarrify(str(store), URL.format(store.name))
        assert isinstance(out, StoreIrOutput)
        rebuilt = _core.rebuild_store(out.entries.get)
        held = {k: b for k, _, b in rebuilt if b is not None}
        assert held, store.name
        assert all(Path(k).name in (".zgroup", ".zarray", ".zattrs") for k in held), store.name
        for k, b in held.items():
            assert (store / k).read_bytes() == b, (store.name, k)
        assert {k for k, _, _ in rebuilt} == {p.relative_to(store).as_posix() for p in store.rglob("*") if p.is_file()}


def test_a_rejection_names_the_profile_the_store_was_decided_to_be(tmp_path):
    # the output's checks are the decided profile's: in a store whose root declares OME-NGFF 0.4,
    # OME-Zarr's own and the Zarr v2 rules it applies (spec/zarrify/ome-zarr.md §2
    # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ome-zarr.md#2-hierarchy>); in any
    # other Zarr v2 store, the zarr2 format's
    with pytest.raises(Rejected) as e:
        zarrify(str(FIXTURES / "ome_zarr_reject_axes_count"), URL.format("a"))
    assert e.value.format == "ome-zarr"

    store = tmp_path / "s"
    shutil.copytree(FIXTURES / "ome_zarr_image_2d", store)
    (store / "bad").mkdir()
    (store / "bad" / ".zgroup").write_text(json.dumps({"zarr_format": 3}))
    with pytest.raises(Rejected, match="zarr_format") as e:
        zarrify(str(store), URL.format("c"))
    assert e.value.format == "ome-zarr"
    with pytest.raises(Rejected) as e:
        zarrify(str(FIXTURES.parent / "zarr2" / "zarr2_reject_dtype_object"), "https://data.test/z/")
    assert e.value.format == "zarr2"


def own_metadata(out, path: str = "zarr.json") -> dict:
    return json.loads(out.entries[path])["attributes"][CONVENTION_KEY]["ome-zarr"]


def test_an_array_that_is_no_level_is_left_out_of_an_image():
    """The image is migrated, and the array recorded, its chunk kept (spec/zarrify/ome-zarr.md §2.1
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ome-zarr.md#21-groups-that-are-not-ome>)."""
    _, out = zarrify(str(FIXTURES / "ome_zarr_left_out"), URL.format("l"))
    assert isinstance(out, StoreIrOutput)
    assert json.loads(out.entries["zarr.json"])["attributes"]["ome"]["version"] == "0.5"
    assert "table/zarr.json" not in out.entries
    assert "refkv_source/objects/table/0.0" in {k for k, _ in out.chunks}
    assert [r["path"] for r in own_metadata(out)["not_presented"]] == ["table"]
    assert "not_ome" not in own_metadata(out)
    assert out.summary["notPresented"] == 1


def test_a_group_that_needs_an_array_left_out_is_not_ome():
    """What names such an image is not OME either, up to the collection; what does not stays OME
    (spec/zarrify/ome-zarr.md §2.1
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ome-zarr.md#21-groups-that-are-not-ome>)."""
    _, out = zarrify(str(FIXTURES / "ome_zarr_not_ome"), URL.format("n"))
    assert isinstance(out, StoreIrOutput)
    own = own_metadata(out)
    assert [r["path"] for r in own["not_presented"]] == ["0/1"]
    assert {r["path"]: r["reason"] for r in own["not_ome"]} == {
        "": "its image 0 is not OME",
        "0": "its level 0/1: it is not presented: dtype |O is not supported: an object array has no data type "
             "without the one filter vlen-utf8",
        "OME": "its image 0 is not OME",
        "bytes": "its level bytes/0: it has a trailing axis of bytes",
        "bytes/labels": "its label image bytes/labels/named is not OME",
        "bytes/labels/named": "its source image bytes is not OME",
    }
    attrs = lambda p: json.loads(out.entries[f"{p}/zarr.json"])["attributes"]  # noqa: E731
    # Not OME: its attributes are its source metadata, whole, and its level a plain array.
    assert "ome" not in attrs("0")
    assert "multiscales" in attrs("0")[CONVENTION_KEY]["ome-zarr"]["attributes"]
    assert "dimension_names" not in json.loads(out.entries["0/0/zarr.json"])
    assert "ome" not in attrs("bytes")
    # A level of bytes is presented, as a plain array.
    assert json.loads(out.entries["bytes/0/zarr.json"])["shape"] == [2, 2, 1]
    # Still OME: an image of no collection now, and a label image whose source image is only
    # 0.4's default, which it does not name.
    for p in ("1", "fine", "0/labels/cells"):
        assert attrs(p)["ome"]["version"] == "0.5", p
    assert "dimension_names" in json.loads(out.entries["fine/0/zarr.json"])
    # The labels groups still name images; the OME-XML is an other object, as its collection is gone.
    assert attrs("0/labels")["ome"]["labels"] == ["cells"]
    assert "refkv_source/objects/OME/METADATA.ome.xml" in {k for k, _ in out.chunks}
    assert out.summary["notOme"] == 6


def test_a_plate_with_a_field_left_out_is_not_ome():
    _, out = zarrify(str(FIXTURES / "ome_zarr_not_ome_plate"), URL.format("p"))
    assert isinstance(out, StoreIrOutput)
    assert {r["path"]: r["reason"] for r in own_metadata(out)["not_ome"]} == {
        "": "its well A/1 is not OME",
        "A/1": "its field A/1/0 is not OME",
        "A/1/0": "its level A/1/0/0: it is not presented: dtype |O is not supported: an object array has no data "
                 "type without the one filter vlen-utf8",
    }
    assert json.loads(out.entries["A/2/zarr.json"])["attributes"]["ome"]["version"] == "0.5"
