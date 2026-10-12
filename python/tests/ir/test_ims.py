"""The IMS IR: the Rust parser (the layout against schema/ims.json, and the walk), the
image projection against the frozen reference (revision 25), the mirror's byte-exact
rebuild, and the hostile probes (python/tests/ir/gen_ims_probes.py)."""

import io

import pytest

from conftest import FIXTURES, same_hierarchy
from vzip.ir import virtualize as via_ir
from vzip.ir.cmirror import rebuild_from_archive
from vzip.virtualize import Rejected
from vzip_reference import virtualize  # the frozen reference (conftest)

IMS = sorted((FIXTURES / "ims").rglob("*.ims"))


def same_as_reference(path, tmp_path) -> bool:
    """The IR's output equals the reference's but vzip_source, and its archive rebuilds the
    file; or both reject it. True when accepted."""
    data = path.read_bytes()
    url = f"https://data.test/{path.name}"
    try:
        _, today = virtualize(str(path), url)
    except Rejected:
        with pytest.raises(Rejected):
            via_ir(str(path), url)
        return False
    fmt, out, ir = via_ir(str(path), url)
    assert fmt == "ims"
    ir.check()
    same_hierarchy(today, out)
    out.write(str(tmp_path / "a.vzip"))
    buf = io.BytesIO()
    rebuild_from_archive(str(tmp_path / "a.vzip"), lambda o, n: data[o:o + n], buf.write)
    assert buf.getvalue() == data
    return True


@pytest.mark.parametrize("path", IMS, ids=lambda p: p.name)
def test_projects_as_the_reference_and_rebuilds(path, tmp_path):
    assert same_as_reference(path, tmp_path) == (not path.name.startswith("ims_reject"))


@pytest.mark.parametrize("name, accepted", [
    ("ims_amp_times", True),  # time points that are one group: its chunks referenced once each
    ("ims_btree_loop_extra", True),  # a loop where the layout does not read: not described
    ("ims_btree_loop_root", False),
    ("ims_continuation_loop_extra", True),
    ("ims_continuation_loop_root", False),
    ("ims_deep_groups", True),
    ("ims_wide_group", True),
])
def test_probes_decide_as_the_reference(ims_probes, tmp_path, name, accepted):
    assert same_as_reference(ims_probes / f"{name}.ims", tmp_path) == accepted


def test_the_walk_stops_at_its_depth_and_reads_a_shared_group_once(ims_probes):
    _, out, _ = via_ir(str(ims_probes / "ims_deep_groups.ims"), "https://data.test/x")
    assert out.summary["elements"] < 2000
    _, out, _ = via_ir(str(ims_probes / "ims_wide_group.ims"), "https://data.test/x")
    assert out.summary["chunks"] == 2 * 4  # the image's chunks, once

