"""The FITS IR: the Rust parser and its schema, the projection (spec/zarrify/fits.md) against
astropy's reading of every synthetic file, the mirror's rebuild, and the inputs the profile
rejects. FITS has no frozen Python reference: astropy is the independent reader."""

import json
import warnings

import numpy as np
import pytest
import zarr
from astropy.io import fits

from ir_helpers import FIXTURES
from ir_helpers import zarrified as shared_zarrified
from refkv.ir import zarrify as via_ir
from refkv.zarrify import Rejected

FILES = sorted(p for p in (FIXTURES / "fits").iterdir() if p.name.endswith((".fits", ".fz")))
ACCEPTED = [p for p in FILES if "reject" not in p.name]
REJECTED = [p for p in FILES if "reject" in p.name]
MAX_SAFE = (1 << 53) - 1
# hand-built fixtures astropy cannot read (an absent tile; no ZTILEn cards): the fixture whose
# image theirs is, read by astropy, with the rows an absent tile leaves at 0
SAME_IMAGE = {"fits_comp_hand_absent_tile": ("fits_comp_hand_one_column", slice(3, 6)), "fits_comp_hand_no_ztile": ("fits_comp_hand_one_column", None)}
COMMENTARY = ("COMMENT", "HISTORY", "")


def zarrified(path, tmp_path):
    """(the output, the hierarchy's root group); the archive rebuilds the file
    (ir_helpers.zarrified)."""
    out, root, _ = shared_zarrified(path, tmp_path, "fits")
    return out, root

def card_value(v):
    """astropy's card value as the convention writes it (spec/zarrify/fits.md §5.3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#53-cards>)."""
    if isinstance(v, fits.card.Undefined):
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v if abs(v) <= MAX_SAFE else str(v)
    if isinstance(v, complex):
        return [v.real, v.imag]
    return v


def same_cards(ours, theirs):
    """Our cards are astropy's: keyword, value and comment, card for card (a commentary card's
    text is its comment here and its value in astropy; a joined long string's comments are
    compared loosely, since astropy joins them its own way)."""
    assert len(ours) == len(theirs), (len(ours), len(theirs))
    for (keyword, value, comment), card in zip(ours, theirs):
        assert keyword == card.keyword, (keyword, card.keyword)
        if card.keyword in COMMENTARY:
            assert value is None and comment == card.value.rstrip(), (comment, card.value)
            continue
        want = card_value(card.value)
        if isinstance(want, float) and isinstance(value, (int, float)):
            assert float(value) == want, (keyword, value, want)
        else:
            assert value == want, (keyword, value, want)
        if not (isinstance(want, str) and len(want) > 60):
            assert (comment or "") == card.comment, (keyword, comment, card.comment)


def same_pixels(ours, theirs, what, table=None):
    """Integers equal, and unquantized floats; quantized floats equal within the rounding of
    `(I - R + 0.5) * scale + zero`, NaNs in the same places: the convention computes it in
    binary64 with each operation rounded (spec/zarrify.md §1.3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#13-arithmetic>), while astropy's
    build may fuse the multiply and the add; the two differ by a few units in the last place of
    the intermediate `I * scale`, which SUBTRACTIVE_DITHER_2 (integers near -2^31, a zero of
    the same size) makes about 1e-6 absolute after the cancellation, and the cast to the
    array's type can then move its last bit (one ulp of the value, relative)."""
    if theirs.dtype.kind != "f" or table is None or "ZSCALE" not in table.dtype.names:
        assert np.array_equal(ours, theirs, equal_nan=theirs.dtype.kind == "f"), what
        return
    bound = 8 * np.finfo(np.float64).eps * (np.max(np.abs(table["ZZERO"])) + 2.0**31 * np.max(np.abs(table["ZSCALE"])))
    assert np.allclose(ours, theirs, rtol=np.finfo(theirs.dtype).eps, atol=bound, equal_nan=True), what


@pytest.mark.parametrize("path", ACCEPTED, ids=lambda p: p.name)
def test_matches_astropy_and_rebuilds(path, tmp_path):
    """Every HDU is a node of its index: an image's array is astropy's data (stored values),
    a table's rows and heap are the file's bytes, every node's cards are astropy's, the root
    lists the HDUs with their offsets, and the file is rebuilt from the mirror."""
    out, root = zarrified(path, tmp_path)
    raw = path.read_bytes()
    meta = root.attrs["refkv_zarrified"]["fits"]
    same = SAME_IMAGE.get(path.stem)
    with warnings.catch_warnings(), fits.open(path, do_not_scale_image_data=True, disable_image_compression=True) as hdul, \
            fits.open(path, do_not_scale_image_data=True) as decoded:
        warnings.simplefilter("ignore")  # astropy's warnings about the odd fixtures (truncated, trailing bytes)
        assert len(meta["hdus"]) == len(hdul)
        if meta["cards"] is None:
            assert len(hdul[0].header) > 700  # past the cards budget
        else:
            same_cards(meta["cards"], hdul[0].header.cards)
        for i, hdu in enumerate(hdul):
            entry = meta["hdus"][i]
            info = hdu.fileinfo()
            assert entry["index"] == i and entry["offset"] == info["hdrLoc"] and entry["data_offset"] == info["datLoc"]
            assert entry["name"] == (hdu.name if hdu.name and hdu.name != "PRIMARY" else None) or (i == 0 and entry["name"] is None)
            node = root[str(i)]
            own = node.attrs["refkv_zarrified"]["fits"]
            assert own["index"] == i
            if own["cards"] is not None:
                same_cards(own["cards"], hdu.header.cards)
            else:
                assert len(hdu.header) > 700
            if hdu.header.get("ZIMAGE"):
                # a tile-compressed image: the table's cards above, astropy's decoded pixels here
                assert entry["kind"] == "image" and entry["compression"]["type"] == hdu.header["ZCMPTYPE"].replace("RICE_ONE", "RICE_1")
                if same is not None:
                    ref, absent = same
                    with fits.open(FIXTURES / "fits" / f"{ref}.fits", do_not_scale_image_data=True) as other:
                        want = other[1].data.copy()
                    if absent is not None:
                        want[absent] = 0
                else:
                    want = decoded[i].data
                assert node.shape == want.shape and node.dtype == want.dtype.newbyteorder("=")
                same_pixels(node[...], want, path.stem, hdu.data if same is None else None)
                codec = json.loads(out.bytes_entries[f"{i}/zarr.json"])["codecs"][0]
                assert codec["name"] == "refkv.fits_tiles" and codec["configuration"]["compression"] == entry["compression"]["type"]
                continue
            if isinstance(hdu, (fits.BinTableHDU, fits.TableHDU)):
                assert entry["kind"] == "table"
                n1, n2 = hdu.header["NAXIS1"], hdu.header["NAXIS2"]
                assert [c["form"] for c in own["columns"]] == [hdu.header[f"TFORM{k}"] for k in range(1, hdu.header["TFIELDS"] + 1)]
                if n1 * n2:
                    assert node["rows"][...].tobytes() == raw[info["datLoc"]:info["datLoc"] + n1 * n2]
                    assert node["rows"].shape == (n2, n1)
                theap = hdu.header.get("THEAP", n1 * n2)
                heap = hdu.header["PCOUNT"] - (theap - n1 * n2)
                if heap:
                    assert node["heap"][...].tobytes() == raw[info["datLoc"] + theap:info["datLoc"] + theap + heap]
                else:
                    assert "heap" not in node
            elif hdu.header["NAXIS"] == 0:
                assert entry["kind"] == "empty" and isinstance(node, zarr.Group) and list(node) == []
            else:
                assert entry["kind"] == "image"
                want = hdu.data
                assert node.shape == tuple(hdu.header[f"NAXIS{k}"] for k in range(hdu.header["NAXIS"], 0, -1))
                assert node.dtype == want.dtype.newbyteorder("=")
                assert node.attrs["refkv_zarrified"]["fits"]["bzero"] == hdu.header.get("BZERO")
                if want.size:
                    assert np.array_equal(node[...], want, equal_nan=want.dtype.kind == "f")


# fixture: (chunk shape of the array at `path`, the number of its chunks, the parts of one chunk key)
CHUNKS = {
    "fits_chunk_planes": ("0", [10, 128, 256], 2, ("0/c/1/0/0", 1)),
    "fits_chunk_rows": ("0", [550, 1000], 2, ("0/c/1/0", 1)),
    "fits_chunk_edge_literal": ("0", [1, (1 << 19) + 1], 2, ("0/c/0/1", 2)),
    "fits_primary_uint16": ("0", [6, 8], 1, ("0/c/0/0", 1)),
    "fits_bintable_heap": ("1/rows", [3, 12], 1, ("1/rows/c/0/0", 1)),
    # tile-compressed: 1100 row tiles of 2000 bytes, 367 per chunk; 3 x 4 tiles of 100 x 100 in one
    # chunk; a chunk's reference is its prefix (a literal) and one range per run of adjacent tiles
    "fits_comp_chunk_rows": ("1", [367, 1000], 3, ("1/c/1/0", 2)),
    "fits_comp_tiles_square": ("1", [300, 350], 1, ("1/c/0/0", 2)),
    "fits_comp_hand_heap_gaps": ("1", [12, 20], 1, ("1/c/0/0", 5)),
}


@pytest.mark.parametrize("name", sorted(CHUNKS))
def test_chunks(name, tmp_path):
    """Images and table rows are cut as contiguous values with the 2^20 limit (conventions §7
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays>);
    an edge chunk is one range and a literal padding."""
    path, shape, count, (key, parts) = CHUNKS[name]
    out, root = zarrified(FIXTURES / "fits" / f"{name}.fits", tmp_path)
    assert list(root[path].chunks) == shape
    assert sum(1 for k in out.refs if k.startswith(f"{path}/c/")) == count
    assert len(out.refs[key]) == parts


def test_summary(tmp_path):
    out, _ = zarrified(FIXTURES / "fits" / "fits_mef_sci_err_dq.fits", tmp_path)
    s = out.summary
    assert s["hdus"] == 7 and s["kinds"] == {"empty": 1, "image": 6} and s["tables"] == 0
    assert s["planner"]["bytes"] <= (FIXTURES / "fits" / "fits_mef_sci_err_dq.fits").stat().st_size  # one window holds the file


@pytest.mark.parametrize("path", REJECTED, ids=lambda p: p.name)
def test_rejected(path):
    with pytest.raises(Rejected):
        via_ir(str(path))
