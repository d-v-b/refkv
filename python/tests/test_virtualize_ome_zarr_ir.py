"""The OME-Zarr profile through the Rust core (vzip.ir.store; spec/virtualize/ome-zarr.md) on
the synthetic stores: the same outcome as the frozen reference (revision 25) for every store,
the same hierarchy outside `vzip_source`, and `vzip_source` the store's IR mirror, which
rebuilds every node document byte for byte (conventions §8.9
<https://github.com/d-v-b/vzip/blob/main/spec/conventions.md#89-stores>). The reference's own
tests are python/tests/reference/test_virtualize_ome_zarr.py; the full comparison, browser
included, is conformance/virtualize/compare.py's.
"""

import json
import sys
from pathlib import Path

import pytest

from vzip.virtualize import Rejected, _virtualize as virtualize  # the profile output, not the store
from vzip.virtualize.common import CONVENTION_KEY, REVISION

sys.path.insert(0, str(Path(__file__).parents[2] / "conformance" / "virtualize" / "reference"))
from vzip_reference import virtualize as reference  # noqa: E402

FIXTURES = Path(__file__).parents[2] / "fixtures" / "ome-zarr"
URL = "https://data.test/ome-zarr/{}/"
STORES = sorted(p for p in FIXTURES.iterdir() if p.is_dir())


def whole_object(parts) -> str:
    """The key of the one listed object a reference's parts are, from its first byte."""
    (kind, key, offset, _), = parts
    assert kind == "object" and offset == 0
    return key


def hierarchy(out) -> dict:
    """The IR path's documents outside vzip_source, and its references (entry key -> object key)."""
    docs = {k: json.loads(v) for k, v in out.entries.items() if k.endswith("zarr.json") and not k.startswith("vzip_source/")}
    return {"docs": docs, "refs": {k: whole_object(v) for k, v in out.refs.items()}}


def reference_hierarchy(out) -> dict:
    docs = {k: json.loads(json.dumps(v)) for k, v in out.docs.items() if not k.startswith("vzip_source/")}
    docs["zarr.json"]["attributes"][CONVENTION_KEY]["revision"] = REVISION
    return {"docs": docs, "refs": {k: out.origins.get(k, k) for k, _ in out.chunks}}


@pytest.mark.parametrize("store", STORES, ids=[p.name for p in STORES])
def test_the_ir_gives_the_reference_hierarchy(store):
    url = URL.format(store.name)
    try:
        expected = reference(str(store), url)
    except Rejected:
        with pytest.raises(Rejected):
            virtualize(str(store), url)
        return
    fmt, out = virtualize(str(store), url)
    assert fmt == expected[0] == "ome-zarr"
    assert hierarchy(out) == reference_hierarchy(expected[1])
    assert out.sources == expected[1].sources
    for k in ("images", "labels", "droppedLabelLevels", "plates", "wells", "fields", "omeXml", "otherObjects", "chunks"):
        assert out.summary[k] == expected[1].summary[k], k


def test_the_mirror_rebuilds_each_node_document():
    import vzip_ir

    for store in STORES:
        if "_reject_" in store.name:
            continue
        _, out = virtualize(str(store), URL.format(store.name))
        rebuilt = vzip_ir.rebuild_store(out.entries.get)
        held = {k: b for k, _, b in rebuilt if b is not None}
        assert held and all(Path(k).name in (".zgroup", ".zarray", ".zattrs") for k in held), store.name
        for k, b in held.items():
            assert (store / k).read_bytes() == b, (store.name, k)
        assert {k for k, _, _ in rebuilt} == {p.relative_to(store).as_posix() for p in store.rglob("*") if p.is_file()}


def test_a_rejection_names_the_profile_the_store_was_decided_to_be(tmp_path):
    # the output's checks are the decided profile's: in a store whose root declares OME-NGFF 0.4,
    # OME-Zarr's own and the Zarr v2 rules it applies (spec/virtualize/ome-zarr.md §2
    # <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/ome-zarr.md#2-hierarchy>); in any
    # other Zarr v2 store, the zarr2 format's
    with pytest.raises(Rejected) as e:
        virtualize(str(FIXTURES / "ome_zarr_reject_axes_count"), URL.format("a"))
    assert e.value.format == "ome-zarr"
    import shutil

    store = tmp_path / "s"
    shutil.copytree(FIXTURES / "ome_zarr_image_2d", store)
    z = json.loads((store / "0" / ".zarray").read_text())
    (store / "0" / ".zarray").write_text(json.dumps({**z, "dtype": "<c8"}))
    with pytest.raises(Rejected, match="dtype") as e:
        virtualize(str(store), URL.format("c"))
    assert e.value.format == "ome-zarr"
    with pytest.raises(Rejected) as e:
        virtualize(str(FIXTURES.parent / "zarr2" / "zarr2_reject_dtype_complex"), "https://data.test/z/")
    assert e.value.format == "zarr2"
