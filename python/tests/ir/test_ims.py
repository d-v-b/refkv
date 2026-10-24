"""The IMS IR: the Rust parser (the layout against schema/ims.json, and the walk), the
image projection against the frozen reference (revision 25), the mirror's byte-exact
rebuild, and the hostile probes (python/tests/ir/gen_ims_probes.py)."""

import json

import pytest

from ir_helpers import FIXTURES, as_the_reference
from refkv.ir import virtualize as via_ir
from refkv_reference import virtualize  # the frozen reference (ir_helpers)

IMS = sorted((FIXTURES / "ims").rglob("*.ims"))


def same_as_reference(path, tmp_path) -> bool:
    """The IR's output equals the reference's but refkv_source, and its archive rebuilds the
    file; or both reject it. True when accepted."""
    return as_the_reference(path, tmp_path, "ims") is not None


@pytest.mark.parametrize("path", IMS, ids=lambda p: p.name)
def test_projects_as_the_reference_and_rebuilds(path, tmp_path):
    assert same_as_reference(path, tmp_path) == (not path.name.startswith("ims_reject"))


@pytest.mark.parametrize("name, accepted", [
    ("ims_amp_times", True),  # time points that are one group: its chunks referenced once each
    ("ims_btree_loop_extra", True),  # a loop where the layout does not read: not described
    ("ims_btree_loop_root", False),
    ("ims_btree_cycle_extra", True),
    ("ims_implicit_extra", True),
    ("ims_fill_size_image", True),  # the walk reads it as the layout does
    ("ims_continuation_loop_extra", True),
    ("ims_continuation_loop_root", False),
    ("ims_deep_groups", True),
    ("ims_wide_group", True),
])
def test_probes_decide_as_the_reference(ims_probes, tmp_path, name, accepted):
    assert same_as_reference(ims_probes / f"{name}.ims", tmp_path) == accepted


def objects(ir) -> list[str]:
    """The paths of the IR's objects (spec/virtualize/ims.md §5.2
    <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/ims.md#52-elements>)."""
    return [p for p in (ir.path(i) for i in range(len(ir))) if p.count("/") == 1 and p.startswith("objects/")]


def reference_objects(path) -> int:
    """The number of objects the frozen reference's object table lists (under its revision's
    source metadata node, vzip_source)."""
    _, out = virtualize(str(path), "https://data.test/x")
    return json.loads(out.bytes_entries["vzip_source/objects/parent/zarr.json"])["shape"][0]


def test_the_walk_stops_at_its_depth_and_reads_a_shared_group_once(ims_probes):
    # 80 nested groups under Deep (level 1): levels 0 to 64 are read, so d0 to d62
    path = ims_probes / "ims_deep_groups.ims"
    _, _, ir = via_ir(str(path), "https://data.test/x")
    assert reference_objects(path) - len(objects(ir)) == 80 - 63
    # 2000 hard links to DataSet: one object, as the reference's table has it
    path = ims_probes / "ims_wide_group.ims"
    _, _, ir = via_ir(str(path), "https://data.test/x")
    assert len(objects(ir)) == reference_objects(path)


def test_an_implicit_index_outside_the_image_is_one_range(ims_probes):
    _, _, ir = via_ir(str(ims_probes / "ims_implicit_extra.ims"), "https://data.test/x")
    data = [i for i in range(len(ir)) if ir.path(i).endswith("/data") and ir.kind(i) == 2]
    forms = [json.loads(ir.form(i)) for i in data]
    implicit = [(i, f) for i, f in zip(data, forms) if "grid" in f["geometry"]]
    assert len(implicit) == 1
    i, f = implicit[0]
    assert f["geometry"] == {"chunk": [4, 6], "size": 2, "grid": [16, 1]}
    # 10 chunks of 4 × 6 allocated over a maximum grid of 16 (maxshape 64 × 6)
    assert ir.extent(i)[1] == 10 * 4 * 6 * 2
