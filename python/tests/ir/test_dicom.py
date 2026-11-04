"""The DICOM IR (revision 26): the Rust parser and its schema, the image projection against
the frozen reference, the mirror's rebuild, runs of identical items and frames, the DICOM
probes of gen_probes.py at the tests' sizes, and lossless JPEG frames, which the profile
reads since the freeze."""


from pathlib import Path

import imagecodecs
import numpy as np
import pydicom
import pytest
from ir_helpers import FIXTURES, REVISED, as_the_reference, zarrified
from pydicom.data import get_testdata_file
from pydicom.encaps import generate_frames

from refkv._narrow import at, integer, ndarray, sequence
from refkv.ir import zarrify as via_ir


def pydicom_file(name: str) -> Path:
    """A test file of pydicom's."""
    path = get_testdata_file(name)
    assert isinstance(path, str), name
    return Path(path)


DICOMS = sorted((FIXTURES / "dicom").glob("*.dcm"))
LOSSLESS = [p for p in DICOMS if p.name.startswith("dicom_ljpeg_")]
RLE = [p for p in DICOMS if p.name.startswith("dicom_rle_")]
# pydicom's package's own RLE files, mono and RGB, 8, 16 and 32 bits, one and several frames (the
# others it downloads, as it does the corpus's: conformance/zarrify/corpus_dicom.txt)
RLE_TESTDATA = [pydicom_file(name) for name in (
    "MR_small_RLE.dcm", "SC_rgb_rle.dcm", "SC_rgb_rle_2frame.dcm", "SC_rgb_rle_16bit_2frame.dcm",
    "SC_rgb_rle_32bit_2frame.dcm")]
JPEGLS = [p for p in DICOMS if p.name.startswith("dicom_jpegls_")]
# and its JPEG-LS ones
JPEGLS_TESTDATA = [pydicom_file(name) for name in (
    "MR_small_jpeg_ls_lossless.dcm", "JPEGLSNearLossless_08.dcm", "JPEGLSNearLossless_16.dcm",
    "SC_rgb_jls_lossy_line.dcm", "SC_rgb_jls_lossy_sample.dcm")]
HTJ2K = [p for p in DICOMS if p.name.startswith("dicom_htj2k_")]
PALETTE_TESTDATA = [pydicom_file("examples_palette.dcm")]
YBR_FULL = [p for p in DICOMS if p.name.startswith("dicom_ybr_full_")
            or p.name in ("dicom_rle_ybr_full.dcm", "dicom_jpegls_ybr_full.dcm")]
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


def frames_of(ds: pydicom.Dataset) -> list[bytes]:
    n = int(ds.get("NumberOfFrames", 1))
    offsets = (ds.ExtendedOffsetTable, ds.ExtendedOffsetTableLengths) if "ExtendedOffsetTable" in ds else None
    return list(generate_frames(ds.PixelData, number_of_frames=n, extended_offsets=offsets))


def image_of(frames: np.ndarray, ds: pydicom.Dataset) -> np.ndarray:
    """`frames` ([frame, row, column[, sample]]) as the array's axes: [c,] [frame,] y, x."""
    image = frames if int(ds.get("NumberOfFrames", 1)) > 1 else frames[0]
    return np.moveaxis(image, -1, 0) if ds.SamplesPerPixel == 3 else image


def dtype_of(ds: pydicom.Dataset) -> np.dtype:
    return np.dtype(f"{'u' if ds.PixelRepresentation == 0 else ''}int{ds.BitsAllocated}")


@pytest.mark.parametrize("path", [*RLE, *RLE_TESTDATA], ids=lambda p: p.name)
def test_rle_frames_read_as_pydicom_decodes_them(path, tmp_path):
    """§7.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#75-frames>: each RLE
    frame is a chunk of `imagecodecs_dicomrle`, whose `dtype` is the array's, planar without a
    `transpose`, and the array is pydicom's own RLE decoder's."""
    out, root, _ = zarrified(path, tmp_path, "dicom")
    array = root.get_array("0")
    codecs = sequence(at(array.metadata.to_dict(), "codecs"))
    assert [at(c, "name") for c in codecs] == ["imagecodecs_dicomrle"]
    ds = pydicom.dcmread(path)
    assert at(codecs[-1], "configuration", "dtype") == dtype_of(ds).str
    # the frames' own data, not a conversion of them
    ds.pixel_array_options(raw=True)
    samples = [3] if ds.SamplesPerPixel == 3 else []
    want = ds.pixel_array.reshape(int(ds.get("NumberOfFrames", 1)), ds.Rows, ds.Columns, *samples)
    got = ndarray(array[...])
    assert got.dtype == dtype_of(ds)
    np.testing.assert_array_equal(got, image_of(want.astype(got.dtype), ds))
    assert at(out.summary, "planner", "fill", "ranges") == 0


@pytest.mark.parametrize("path", [*JPEGLS, *JPEGLS_TESTDATA], ids=lambda p: p.name)
def test_jpegls_frames_read_as_imagecodecs_decodes_them(path, tmp_path):
    """§7.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#75-frames>: each
    JPEG-LS frame is a chunk of `imagecodecs_jpegls`, and the array is its frames decoded, as the
    Pixel Representation reads them. Lossless ones also equal what the generator wrote, by the
    round trip test_codec_entry_points runs."""
    _, root, _ = zarrified(path, tmp_path, "dicom")
    array = root.get_array("0")
    assert at(sequence(at(array.metadata.to_dict(), "codecs"))[-1], "name") == "imagecodecs_jpegls"
    ds = pydicom.dcmread(path)
    decoded = np.stack([imagecodecs.jpegls_decode(f) for f in frames_of(ds)])
    want = decoded.astype(f"uint{ds.BitsAllocated}").view(dtype_of(ds))
    got = ndarray(array[...])
    assert got.dtype == dtype_of(ds)
    np.testing.assert_array_equal(got, image_of(want, ds))


@pytest.mark.parametrize("path", HTJ2K, ids=lambda p: p.name)
def test_htj2k_frames_read_as_imagecodecs_decodes_them(path, tmp_path):
    """§7.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#75-frames>: each HTJ2K
    frame is a chunk of `imagecodecs_htj2k`, interleaved whatever the stream's own default, which
    is planar without a color transform."""
    _, root, _ = zarrified(path, tmp_path, "dicom")
    array = root.get_array("0")
    codec = sequence(at(array.metadata.to_dict(), "codecs"))[-1]
    assert at(codec, "name") == "imagecodecs_htj2k"
    ds = pydicom.dcmread(path)
    decoded = np.stack([imagecodecs.htj2k_decode(f, planar=False) for f in frames_of(ds)])
    got = ndarray(array[...])
    assert got.dtype == dtype_of(ds)
    np.testing.assert_array_equal(got, image_of(decoded.astype(got.dtype), ds))


def test_a_segmented_palette_leaves_its_tables_in_the_mirror(tmp_path):
    """§4.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#41-the-palette>: its
    entries are commands, so the indices are the array and no array `palette` is written."""
    path = FIXTURES / "dicom" / "dicom_palette_segmented.dcm"
    _, root, _ = zarrified(path, tmp_path, "dicom")
    ds = pydicom.dcmread(path)
    ds.pixel_array_options(raw=True)
    np.testing.assert_array_equal(ndarray(root.get_array("0")[...]), ds.pixel_array)
    assert "palette" not in root


PALETTES = [p for p in DICOMS if p.name.startswith("dicom_palette_") and "segmented" not in p.name]


@pytest.mark.parametrize("path", PALETTES + PALETTE_TESTDATA, ids=lambda p: p.name)
def test_palette_images_are_indices_and_their_tables(path, tmp_path):
    """§4.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#41-the-palette>: the
    array `0` is the stored indices, not colors, and the array `palette` each table's entries in
    the file, from which pydicom's `apply_color_lut` follows."""
    _, root, _ = zarrified(path, tmp_path, "dicom")
    ds = pydicom.dcmread(path)
    got = ndarray(root.get_array("0")[...])
    ds.pixel_array_options(raw=True)
    want = ds.pixel_array
    np.testing.assert_array_equal(got, want)
    table = root.get_array("palette")
    assert table.chunks == (1, table.shape[1])
    lut = ndarray(table[...])
    descriptor = ds.RedPaletteColorLookupTableDescriptor
    assert lut.shape == (3, descriptor[0] or 65536)
    assert table.attrs["first_mapped"] == descriptor[1]
    assert table.attrs["bits"] == descriptor[2]
    for values, color in zip(lut, ("Red", "Green", "Blue"), strict=True):
        raw = ds[f"{color}PaletteColorLookupTableData"].value
        order = ">" if not ds.is_little_endian and lut.dtype.itemsize > 1 else "<"
        np.testing.assert_array_equal(values, np.frombuffer(raw, lut.dtype.newbyteorder(order), lut.shape[1]))
    omero = at(root.attrs, "ome", "omero", "channels")
    assert at(sequence(omero)[0], "label") == "index"


@pytest.mark.parametrize("path", YBR_FULL, ids=lambda p: p.name)
def test_native_ybr_full_is_its_channels_as_stored(path, tmp_path):
    """§3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#3-the-image>: Y, Cb and Cr,
    labelled so, with no conversion to RGB."""
    _, root, _ = zarrified(path, tmp_path, "dicom")
    ds = pydicom.dcmread(path)
    if ds.file_meta.TransferSyntaxUID.is_encapsulated and "4.80" in ds.file_meta.TransferSyntaxUID:
        want = np.stack([imagecodecs.jpegls_decode(f) for f in frames_of(ds)])
    else:
        ds.pixel_array_options(raw=True)
        want = ds.pixel_array.reshape(int(ds.get("NumberOfFrames", 1)), ds.Rows, ds.Columns, 3)
    np.testing.assert_array_equal(ndarray(root.get_array("0")[...]), image_of(want, ds))
    channels = sequence(at(root.attrs, "ome", "omero", "channels"))
    assert [at(c, "label") for c in channels] == ["Y", "Cb", "Cr"]


def test_jpeg2k_ybr_422_frames_decode_to_rgb(tmp_path):
    """§7.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#75-frames>: a real
    frame of YBR_FULL_422, whose chroma is half width, which OpenJPEG upsamples and converts: a
    white slide background is near white in R, G and B, where Cb and Cr would be near 128."""
    _, root, _ = zarrified(FIXTURES / "dicom" / "dicom_j2k_ybr_422.dcm", tmp_path, "dicom")
    got = ndarray(root.get_array("0")[...])
    assert got.shape == (3, 2, 240, 240)
    assert (got.mean(axis=(1, 2, 3)) > 235).all()
    channels = sequence(at(root.attrs, "ome", "omero", "channels"))
    assert [at(c, "label") for c in channels] == ["R", "G", "B"]


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
