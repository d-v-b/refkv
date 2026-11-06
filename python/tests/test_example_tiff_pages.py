"""The example layout `refkv.example.tiff-pages` (refkv.zarrify.layouts.examples.tiff_pages): every
main-chain IFD of a TIFF as an image of its own, its SubIFDs as further levels, checked with
`check`, read back with zarr against tifffile and the base, and written alike under another
PYTHONHASHSEED; and each way its options or an IFD are refused."""

import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import tifffile
import zarr

import refkv.zarr
from refkv.zarrify import Rejected, zarrify
from refkv.zarrify.layouts import Layout, LayoutUsageError, base, encode_entries
from refkv.zarrify.layouts.examples.tiff_pages import TiffPages
from refkv.zarrify.layouts.hierarchy import check

TIFF = Path(__file__).resolve().parents[2] / "fixtures" / "tiff"
ALLOWED = frozenset({"copy"})  # the check codes the module's docstring names

# (fixture, options, the IFDs left out): several IFDs, SubIFDs, tiles and strips, compressed and not
CASES = [
    ("tczyx_uint16_deflate.ome.tif", {}, set()),  # 24 pages of Deflate tiles
    ("rgb_contig_zstd.ome.tif", {}, set()),  # interleaved, a SubIFD
    ("rgb_planar_jpeg2000_bigtiff_be.ome.tif", {}, set()),  # planar, big-endian BigTIFF, two SubIFDs
    ("strips_uint16_pyramid_short.ome.tif", {}, set()),  # short uncompressed strips: cut, rectilinear
    ("strips_one_row_tyx_lzw.ome.tif", {}, set()),  # pages of LZW strips, each with a SubIFD
    ("strips_uint16_deflate_short.tif", {}, set()),  # a short Deflate strip: rectilinear
    ("strips_rgb_contig_short.tif", {}, set()),  # interleaved uncompressed, short
    ("strips_rgb_contig_jpeg_short.tif", {}, set()),  # JPEG strips, short
    ("jpeg_aperio_rgb.tif", {}, set()),  # JPEG tables spliced in by the recipe
    ("jpeg_ycbcr.tif", {}, set()),
    ("svs_like_int16.tif", {}, set()),  # tiled pages and a stripped thumbnail
    ("uint12_rgb_contig.tif", {}, set()),  # packed, interleaved
    ("bilevel_strips_short.tif", {}, set()),  # one bit a sample, short
    ("predictor3_planar_deflate_float32.tif", {}, set()),
    ("edge_strips_absent.tif", {}, set()),  # a strip of byte count 0: a missing chunk
    ("edge_pointers.tif", {}, set()),  # a stripped page, a SubIFD with a SubIFD of its own
    ("ome_set_a.ome.tif", {}, set()),  # a file read as a store: each file's pages
    ("edge_shared_tables.tif", {"skip": True}, {"ifds/3"}),  # aliased strips; an IFD without tables
    ("edge_budget.tif", {"skip": True}, {"ifds/1", "ifds/2", "ifds/3"}),  # IFDs that are not images
    ("edge_pointer_list.tif", {"skip": True}, {"ifds/1"}),  # old-style JPEG
    ("eer_v1_frames.tif", {}, set()),  # EER frames, the codec of the chunk shape
]

# tifffile reads these pages' strips as one contiguous run across the absent strip, shifting the
# rows after it; the base reads each strip at its offset, as it says in spec/zarrify/tiff.md §4.4
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#44-arrays>: compared with the base
TIFFFILE_DIFFERS = {"edge_strips_absent.tif"}
# tifffile has no EER decoder: these pages are compared with the base's frames, page n its plane n
BASE_FRAMES = {"eer_v1_frames.tif"}

# what the determinism check runs in a second process: each case's entries, encoded
SCRIPT = """
import json, sys
from refkv.zarrify.layouts import base, encode_entries
from refkv.zarrify.layouts.examples.tiff_pages import TiffPages

plugin = TiffPages()
out = []
for path, options in json.loads(sys.argv[1]):
    entries = plugin.write(base(path), plugin.options(options))
    out.append([list(entries), encode_entries(entries).hex()])
print(json.dumps(out))
"""


def read(path):
    """The values of the base's image array, `0`."""
    return zarr.open_array(refkv.zarr.store(zarrify(str(path))), path="0", mode="r")[...]


def document(entries, key):
    value = entries[key]
    assert isinstance(value, bytes)
    return json.loads(value)


def presented(page: object) -> np.ndarray:
    """A tifffile page's values in the layout's axis order: `(y, x)` or `(c, y, x)`."""
    assert isinstance(page, tifffile.TiffPage)
    values = page.asarray()
    if page.samplesperpixel > 1 and page.planarconfig == 1:  # contiguous: (y, x, s)
        values = np.moveaxis(values, -1, 0)
    return values.astype(page.dtype if page.dtype != bool else np.uint8)


def levels(path, leave_out):
    """Each main-chain page of the TIFF at `path` with its SubIFDs, as tifffile reads them, but
    page `n` for `n` in `leave_out`."""
    with tifffile.TiffFile(path) as tf:
        out = []
        for n, page in enumerate(tf.pages):
            if n in leave_out:
                continue
            found = [presented(page)]
            for offset in page.subifds or ():
                tf.filehandle.seek(offset)
                found.append(presented(tifffile.TiffPage(tf, index=0)))
            out.append((n, found))
        return out


@pytest.mark.parametrize(("name", "options", "skipped"), CASES, ids=[c[0] for c in CASES])
def test_every_page_reads_as_tifffile_reads_it(name, options, skipped):
    """`check` finds only `copy`; zarrify writes the same entries; every page's every level, read
    through zarr (codecs, transposes, rectilinear grids, missing chunks), equals tifffile's; and
    IFD 0's image equals the base's, where the base presents it alone."""
    zarr.config.set({"array.rectilinear_chunks": True})
    path = TIFF / name
    b = base(str(path))
    plugin = TiffPages()
    entries = plugin.write(b, plugin.options(options))
    assert {p.code for p in check(b, entries)} <= ALLOWED
    store = zarrify(str(path), layout=Layout(TiffPages(), options))
    assert sorted(store.mapping) == sorted(entries)
    zs = refkv.zarr.store(store)
    assert set(document(entries, "zarr.json")["attributes"].get("skipped", {})) == skipped
    # a file read as a store: each file's pages under its key
    files = sorted({k.split("/")[0] for k in entries if not k.split("/")[0].isdigit() and k != "zarr.json"})
    for prefix in [f"{f}/" for f in files] or [""]:
        gone = {int(s.rpartition("/")[2]) for s in skipped if s.startswith(f"{prefix}ifds/")}
        assert all(f"{prefix}{n}/zarr.json" not in entries for n in gone)
        found = ([(n, [frame]) for n, frame in enumerate(read(path))] if name in BASE_FRAMES
                 else levels(path.parent / prefix if prefix else path, gone))
        for n, want in found:
            ifd = f"{prefix}ifds/{n}"
            for k, values in enumerate(want):
                got = zarr.open_array(zs, path=f"{prefix}{n}/{k}", mode="r")
                assert got.attrs["ifd"] == (ifd if k == 0 else f"{ifd}/subifds/{k - 1}")
                if name not in TIFFFILE_DIFFERS:
                    np.testing.assert_array_equal(got[...], values)
    mine = f"{name}/0/0" if files else "0/0"
    if b.document("0/zarr.json").get("dimension_names") == document(entries, f"{mine}/zarr.json")["dimension_names"]:
        np.testing.assert_array_equal(zarr.open_array(zs, path=mine, mode="r")[...], read(path))


def test_a_second_process_writes_the_same_entries():
    """Run under another PYTHONHASHSEED, write gives the same entries, in the same order."""
    cases = [(str(TIFF / n), o) for n, o, _ in CASES[:4] + CASES[-3:]]
    plugin = TiffPages()
    here = [[list(e), encode_entries(e).hex()] for e in (plugin.write(base(p), plugin.options(o)) for p, o in cases)]
    env = {**os.environ, "PYTHONHASHSEED": "12345"}
    done = subprocess.run([sys.executable, "-c", SCRIPT, json.dumps(cases)], env=env, capture_output=True,
                          text=True, check=True)
    assert json.loads(done.stdout) == here


# ---- the errors: one test each

def two_pages(tmp_path, **second):
    """A TIFF of two pages: a small one the profile presents, and one `second` writes."""
    path = tmp_path / "two.tif"
    with tifffile.TiffWriter(path) as w:
        w.write(np.zeros((16, 16), np.uint8))
        w.write(np.arange(32 * 48, dtype=np.uint8).reshape(32, 48), **second)
    return path


def patch(path, tag, value, index=0, count=False):
    """Sets value `index` of page 1's `tag` (or, with `count`, its entry's count) in place."""
    with tifffile.TiffFile(path) as tf:
        page = tf.pages[1]
        assert isinstance(page, tifffile.TiffPage)
        t = page.tags[tag]
        fmt = "<I" if count else {3: "<H", 4: "<I"}[int(t.dtype)]  # SHORT or LONG
        at = t.offset + 4 if count else t.valueoffset + index * struct.calcsize(fmt)
    with open(path, "r+b") as f:
        f.seek(at)
        f.write(struct.pack(fmt, value))


def rejected(path, match, options=None):
    with pytest.raises(Rejected, match=match):
        zarrify(str(path), layout=Layout(TiffPages(), options))


def test_an_ifd_whose_tables_the_ir_does_not_hold_is_rejected(tmp_path):
    """Page 1's tile tables, of more than 2^14 tiles, are past the parser's eager budget."""
    path = tmp_path / "large.tif"
    with tifffile.TiffWriter(path) as w:
        w.write(np.zeros((16, 16), np.uint8))
        w.write(np.zeros((2064, 2064), np.uint8), tile=(16, 16), compression="zlib")
    rejected(path, r"ifds/1: the IR does not hold its TileOffsets and TileByteCounts")


def test_an_ifd_without_tables_is_rejected():
    rejected(TIFF / "edge_shared_tables.tif", r"ifds/3: it has no StripOffsets and StripByteCounts")


def test_an_ifd_that_is_not_an_image_is_rejected():
    rejected(TIFF / "edge_budget.tif", r"ifds/1: it has no ImageWidth and ImageLength")


def test_a_coding_the_routines_refuse_is_rejected():
    rejected(TIFF / "edge_pointer_list.tif", r"ifds/1: unsupported compression 6")


def test_strips_of_no_size_are_rejected(tmp_path):
    path = two_pages(tmp_path, rowsperstrip=8)
    patch(path, "RowsPerStrip", 0)
    rejected(path, r"ifds/1: its strips have no size")


def test_a_table_of_the_wrong_length_is_rejected(tmp_path):
    path = two_pages(tmp_path, rowsperstrip=8)
    patch(path, "StripByteCounts", 3, count=True)
    rejected(path, r"ifds/1: it has 3 strips, not the 4 its size makes")


def test_a_tile_outside_the_file_is_rejected(tmp_path):
    path = two_pages(tmp_path, tile=(16, 16))
    patch(path, "TileOffsets", 1 << 30, index=2)
    rejected(path, r"ifds/1: tile 2 does not lie within the file")


def test_an_uncompressed_strip_short_of_its_rows_is_rejected(tmp_path):
    path = two_pages(tmp_path, rowsperstrip=8)
    patch(path, "StripByteCounts", 100, index=1)
    rejected(path, r"ifds/1: strip 1 holds 100 bytes, fewer than its 384")


def test_an_unknown_option_is_refused():
    with pytest.raises(LayoutUsageError, match="unknown options"):
        zarrify(str(TIFF / "strips_uint8.tif"), layout=Layout(TiffPages(), {"levels": 2}))


def test_a_skip_that_is_not_a_boolean_is_refused():
    with pytest.raises(LayoutUsageError, match="skip is true or false"):
        zarrify(str(TIFF / "strips_uint8.tif"), layout=Layout(TiffPages(), {"skip": 1}))
