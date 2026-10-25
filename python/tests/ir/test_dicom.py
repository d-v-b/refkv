"""The DICOM IR (revision 26): the Rust parser and its schema, the image projection against
the frozen reference, the mirror's rebuild, runs of identical items and frames, and the
DICOM probes of gen_probes.py at the tests' sizes."""


import pytest
from ir_helpers import FIXTURES, as_the_reference

from refkv.ir import zarrify as via_ir

DICOMS = sorted((FIXTURES / "dicom").glob("*.dcm"))
PROBES = ["dicom_big_private.dcm", "dicom_chain_bot.dcm", "dicom_chain_nobot.dcm", "dicom_deep_defined.dcm",
          "dicom_eot_many.dcm", "dicom_fragments_many.dcm", "dicom_frames_many.dcm", "dicom_items_distinct.dcm",
          "dicom_items_same.dcm", "dicom_many_elements.dcm"]


def decides_as_today(path, tmp_path):
    """The IR's output is the reference's outside refkv_source, or both reject; its mirror
    rebuilds the file. The IR's output, or None when rejected."""
    out = as_the_reference(path, tmp_path, "dicom", same_message=True)
    if out is not None:
        assert out.summary["planner"]["fill"]["ranges"] == 0
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
