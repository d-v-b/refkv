"""The DICOM IR (revision 26): the Rust parser and its schema, the image projection against
the frozen reference, the mirror's rebuild, runs of identical items and frames, the DICOM
probes of gen_probes.py at the tests' sizes, and lossless JPEG frames, which the profile
reads since the freeze."""


import imagecodecs
import numpy as np
import pydicom
import pytest
from ir_helpers import FIXTURES, REVISED, as_the_reference, zarrified
from pydicom.encaps import generate_frames

from refkv._narrow import at, integer, ndarray, sequence
from refkv.ir import zarrify as via_ir

DICOMS = sorted((FIXTURES / "dicom").glob("*.dcm"))
LOSSLESS = [p for p in DICOMS if p.name.startswith("dicom_ljpeg_")]
PROBES = ["dicom_big_private.dcm", "dicom_chain_bot.dcm", "dicom_chain_nobot.dcm", "dicom_deep_defined.dcm",
          "dicom_eot_many.dcm", "dicom_fragments_many.dcm", "dicom_frames_many.dcm", "dicom_items_distinct.dcm",
          "dicom_items_same.dcm", "dicom_many_elements.dcm"]


def decides_as_today(path, tmp_path):
    """The IR's output is the reference's outside refkv_source, or both reject; its mirror
    rebuilds the file. The IR's output, or None when rejected."""
    out = as_the_reference(path, tmp_path, "dicom", same_message=True)
    if out is not None:
        assert at(out.summary, "planner", "fill", "ranges") == 0
    return out


@pytest.mark.parametrize("path", [p for p in DICOMS if p.name not in REVISED], ids=lambda p: p.name)
def test_projects_as_today_and_rebuilds(path, tmp_path):
    decides_as_today(path, tmp_path)


@pytest.mark.parametrize("path", LOSSLESS, ids=lambda p: p.name)
def test_lossless_jpeg_frames_read_as_libjpeg_turbo_decodes_them(path, tmp_path):
    """§7.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#75-frames>: each
    lossless JPEG frame is a chunk of `imagecodecs_ljpeg`, and the array read through
    zarr-python is pydicom's frames decoded by libjpeg-turbo, a decoder apart from the one
    the codec calls, as the Pixel Representation reads them."""
    out, root, _ = zarrified(path, tmp_path, "dicom")
    assert at(out.summary, "planner", "fill", "ranges") == 0
    array = root.get_array("0")
    assert at(sequence(at(array.metadata.to_dict(), "codecs"))[-1], "name") == "imagecodecs_ljpeg"
    ds = pydicom.dcmread(path)
    n = int(ds.get("NumberOfFrames", 1))
    offsets = (ds.ExtendedOffsetTable, ds.ExtendedOffsetTableLengths) if "ExtendedOffsetTable" in ds else None
    frames = np.stack([imagecodecs.jpeg8_decode(f)
                       for f in generate_frames(ds.PixelData, number_of_frames=n, extended_offsets=offsets)])
    dtype = np.dtype(f"{'u' if ds.PixelRepresentation == 0 else ''}int{ds.BitsAllocated}")
    want = frames.astype(f"uint{ds.BitsAllocated}").view(dtype)
    want = want if n > 1 else want[0]
    want = np.moveaxis(want, -1, 0) if ds.SamplesPerPixel == 3 else want
    got = ndarray(array[...])
    assert got.dtype == dtype
    np.testing.assert_array_equal(got, want)


@pytest.mark.parametrize("name", PROBES)
def test_probes_decide_as_today(probes, name, tmp_path):
    decides_as_today(probes / name, tmp_path)


def test_identical_items_and_frames_fold_into_runs(probes, tmp_path):
    # 2000 identical items, and 5000 one-byte frames: a few elements each, many rows
    for name, rows in (("dicom_items_same.dcm", 6020), ("dicom_frames_many.dcm", 5017)):
        out = decides_as_today(probes / name, tmp_path)
        assert out is not None, name
        assert integer(out.summary["elements"]) < 30, name
        assert at(out.summary, "folded", "rows") == rows, name


def test_a_value_past_the_scan_is_not_read(probes):
    # a 2 MiB private value before Pixel Data: skipped, not read
    _, out, _ = via_ir(str(probes / "dicom_big_private.dcm"), "https://data.test/a.dcm")
    assert integer(at(out.summary, "planner", "bytes")) < 300_000


def test_a_basic_offset_table_predicts_the_fragments_in_one_batch(probes):
    # 20 fragments of 70,000 bytes: one batch with the table, a batch a fragment without
    _, bot, _ = via_ir(str(probes / "dicom_chain_bot.dcm"), "https://data.test/a.dcm")
    _, chain, _ = via_ir(str(probes / "dicom_chain_nobot.dcm"), "https://data.test/a.dcm")
    assert integer(at(bot.summary, "planner", "batches")) < 8
    assert integer(at(chain.summary, "planner", "batches")) >= 20
