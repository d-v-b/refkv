"""The DICOM IR (revision 26): the Rust parser and its schema, the image projection against
the frozen reference, the mirror's rebuild, runs of identical items and frames, and the
DICOM probes of gen_probes.py at the tests' sizes."""

import io

import pytest

from ir_helpers import FIXTURES, same_hierarchy
from refkv.ir import virtualize as via_ir
from refkv.ir.cmirror import rebuild_from_archive
from refkv.virtualize import Rejected
from refkv_reference import virtualize  # the frozen reference (ir_helpers)

DICOMS = sorted((FIXTURES / "dicom").glob("*.dcm"))
PROBES = ["dicom_big_private.dcm", "dicom_chain_bot.dcm", "dicom_chain_nobot.dcm", "dicom_deep_defined.dcm",
          "dicom_eot_many.dcm", "dicom_fragments_many.dcm", "dicom_frames_many.dcm", "dicom_items_distinct.dcm",
          "dicom_items_same.dcm", "dicom_many_elements.dcm"]


def decides_as_today(path, tmp_path):
    """The IR's output is the reference's outside refkv_source, or both reject; its mirror
    rebuilds the file. The IR's output, or None when rejected."""
    data = path.read_bytes()
    url = f"https://data.test/{path.name}"
    try:
        _, today = virtualize(str(path), url)
    except Rejected as e:
        with pytest.raises(Rejected) as got:
            via_ir(str(path), url)
        assert str(got.value) == str(e)
        return None
    fmt, out, ir = via_ir(str(path), url)
    assert fmt == "dicom"
    ir.check()
    same_hierarchy(today, out)
    assert out.summary["planner"]["fill"]["ranges"] == 0
    out.write(str(tmp_path / "a.vzip"))
    buf = io.BytesIO()
    rebuild_from_archive(str(tmp_path / "a.vzip"), lambda o, n: data[o:o + n], buf.write)
    assert buf.getvalue() == data
    return out


@pytest.mark.parametrize("path", DICOMS, ids=lambda p: p.name)
def test_projects_as_today_and_rebuilds(path, tmp_path):
    decides_as_today(path, tmp_path)


@pytest.mark.parametrize("name", PROBES)
def test_probes_decide_as_today(probes, name, tmp_path):
    decides_as_today(probes / name, tmp_path)


def test_identical_items_and_frames_fold_into_runs(probes, tmp_path):
    # 2000 identical items, and 5000 one-byte frames: a few elements each, many rows
    for name, rows in (("dicom_items_same.dcm", 6020), ("dicom_frames_many.dcm", 5017)):
        out = decides_as_today(probes / name, tmp_path)
        assert out.summary["elements"] < 30, name
        assert out.summary["folded"]["rows"] == rows, name


def test_a_value_past_the_scan_is_not_read(probes):
    # a 2 MiB private value before Pixel Data: skipped, not read
    _, out, _ = via_ir(str(probes / "dicom_big_private.dcm"), "https://data.test/a.dcm")
    assert out.summary["planner"]["bytes"] < 300_000


def test_a_basic_offset_table_predicts_the_fragments_in_one_batch(probes):
    # 20 fragments of 70,000 bytes: one batch with the table, a batch a fragment without
    _, bot, _ = via_ir(str(probes / "dicom_chain_bot.dcm"), "https://data.test/a.dcm")
    _, chain, _ = via_ir(str(probes / "dicom_chain_nobot.dcm"), "https://data.test/a.dcm")
    assert bot.summary["planner"]["batches"] < 8
    assert chain.summary["planner"]["batches"] >= 20
