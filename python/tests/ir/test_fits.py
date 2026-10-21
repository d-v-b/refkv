"""The FITS IR: the Rust parser and its schema, the projection (spec/virtualize/fits.md) against
astropy's reading of every synthetic file, the mirror's rebuild, and the inputs the profile
rejects. FITS has no frozen Python reference: astropy is the independent reader."""

import io
import json
import warnings

import numpy as np
import pytest
import zarr
from astropy.io import fits

import refkv.zarr
from ir_helpers import FIXTURES
from refkv.ir import virtualize as via_ir
from refkv.ir.cmirror import rebuild_from_archive
from refkv.virtualize import Rejected

FILES = sorted(p for p in (FIXTURES / "fits").iterdir() if p.name.endswith((".fits", ".fits.gz", ".fits.bz2")))
ACCEPTED = [p for p in FILES if "reject" not in p.name]
REJECTED = [p for p in FILES if "reject" in p.name]
MAX_SAFE = (1 << 53) - 1
COMMENTARY = ("COMMENT", "HISTORY", "")


def virtualized(path, tmp_path):
    """(the output, the hierarchy's root group); the archive rebuilds the file."""
    data = path.read_bytes()
    fmt, out, ir = via_ir(str(path))
    assert fmt == "fits"
    ir.check()
    out.write(str(tmp_path / "a.vzip"))
    buf = io.BytesIO()
    rebuild_from_archive(str(tmp_path / "a.vzip"), lambda o, n: data[o:o + n], buf.write)
    assert buf.getvalue() == data
    root = zarr.open_group(refkv.zarr.store(out.to_store()), mode="r", zarr_format=3)
    return out, root


def card_value(v):
    """astropy's card value as the convention writes it (spec/virtualize/fits.md §2.6
    <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/fits.md#26-source-metadata>)."""
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


@pytest.mark.parametrize("path", ACCEPTED, ids=lambda p: p.name)
def test_matches_astropy_and_rebuilds(path, tmp_path):
    """Every HDU is a node of its index: an image's array is astropy's data (stored values),
    a table's rows and heap are the file's bytes, every node's cards are astropy's, the root
    lists the HDUs with their offsets, and the file is rebuilt from the mirror."""
    out, root = virtualized(path, tmp_path)
    raw = path.read_bytes()
    meta = root.attrs["refkv_virtualized"]["fits"]
    with warnings.catch_warnings(), fits.open(path, do_not_scale_image_data=True) as hdul:
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
            own = node.attrs["refkv_virtualized"]["fits"]
            assert own["index"] == i
            if own["cards"] is not None:
                same_cards(own["cards"], hdu.header.cards)
            else:
                assert len(hdu.header) > 700
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
                assert node.attrs["refkv_virtualized"]["fits"]["bzero"] == hdu.header.get("BZERO")
                if want.size:
                    assert np.array_equal(node[...], want, equal_nan=want.dtype.kind == "f")


# fixture: (chunk shape of the array at `path`, the number of its chunks, the parts of one chunk key)
CHUNKS = {
    "fits_chunk_planes": ("0", [10, 128, 256], 2, ("0/c/1/0/0", 1)),
    "fits_chunk_rows": ("0", [550, 1000], 2, ("0/c/1/0", 1)),
    "fits_chunk_edge_literal": ("0", [1, (1 << 19) + 1], 2, ("0/c/0/1", 2)),
    "fits_primary_uint16": ("0", [6, 8], 1, ("0/c/0/0", 1)),
    "fits_bintable_heap": ("1/rows", [3, 12], 1, ("1/rows/c/0/0", 1)),
}


@pytest.mark.parametrize("name", sorted(CHUNKS))
def test_chunks(name, tmp_path):
    """Images and table rows are cut as contiguous values with the 2^20 limit (conventions §7
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays>);
    an edge chunk is one range and a literal padding."""
    path, shape, count, (key, parts) = CHUNKS[name]
    out, root = virtualized(FIXTURES / "fits" / f"{name}.fits", tmp_path)
    assert list(root[path].chunks) == shape
    assert sum(1 for k in out.refs if k.startswith(f"{path}/c/")) == count
    assert len(out.refs[key]) == parts


def test_summary(tmp_path):
    out, _ = virtualized(FIXTURES / "fits" / "fits_mef_sci_err_dq.fits", tmp_path)
    s = out.summary
    assert s["hdus"] == 7 and s["kinds"] == {"empty": 1, "image": 6} and s["tables"] == 0
    assert s["planner"]["bytes"] <= (FIXTURES / "fits" / "fits_mef_sci_err_dq.fits").stat().st_size  # one window holds the file


@pytest.mark.parametrize("path", REJECTED, ids=lambda p: p.name)
def test_rejected(path):
    with pytest.raises(Rejected):
        via_ir(str(path))
