"""`refkv.build.fits_arrays` and `fits_cards`: a FITS HDU's arrays and cards as the FITS
convention writes them, made from the parser's description and the IR, with no output of refkv's.
Each is checked against refkv's own output of every accepted FITS fixture, and for each of its
errors."""

import json
from pathlib import Path

import numpy as np
import pytest

from refkv import _core
from refkv.build import Parsed, filled, fits_arrays, fits_cards, project, read

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
FITS = sorted(p for p in (FIXTURES / "fits").iterdir() if p.is_file() and ".fits" in p.name and "reject" not in p.name)


@pytest.fixture(params=[None, 64], ids=["payload-limit", "copied-edges"])
def copy_threshold(request):
    """The payload past which an edge chunk is copied: refkv's, and one small enough that the
    fixtures' edge chunks are copied."""
    _core._set_copy_threshold(request.param)
    yield request.param
    _core._set_copy_threshold(None)


def edged(tmp_path: Path) -> Path:
    """A FITS image of more than a chunk's bytes whose rows do not divide evenly into chunks, so
    that its last chunk is an edge chunk, padded or copied: no fixture has one."""
    fits = pytest.importorskip("astropy.io.fits")
    path = tmp_path / "edged.fits"
    fits.PrimaryHDU(np.arange(1031 * 1100, dtype=np.uint8).reshape(1031, 1100)).writeto(path)
    return path


def test_an_hdu_s_arrays_and_cards_are_those_refkv_s_own_output_writes(tmp_path, copy_threshold):
    """For every HDU of every accepted fixture: each array's document is refkv's own but for the
    declarations in its attributes, its chunks are refkv's (a copied chunk as the bytes refkv
    copies), and the cards are those refkv's source metadata gives, where it gives them. Images,
    tables' rows and heaps, tile-compressed images, files compressed whole and an edge chunk among
    them."""
    seen = {"image": 0, "rows": 0, "heap": 0, "tiled": 0, "copied": 0, "cards past the budget": 0}
    for path in [*FITS, edged(tmp_path)]:
        with read(str(path)) as parsed:
            built = project(parsed)
            description = parsed.description
            assert description is not None
            for hdu in description["hdus"]:
                index = hdu["index"]
                cards = fits_cards(parsed, index)
                theirs = (built.source_metadata(str(index)) or {}).get("cards")
                if theirs is None:
                    seen["cards past the budget"] += hdu["kind"] != "empty"
                else:
                    assert cards == theirs, f"{path.name} {index}"
                for under, array in fits_arrays(parsed, index).items():
                    node = f"{index}/{under}".rstrip("/")
                    doc, chunks = built.array_entries(node)
                    assert array.document() == {**doc, "attributes": {}}, f"{path.name} {node}"
                    mine = {k[len(node) + 1:]: filled(v) if not isinstance(v, bytes) else v
                            for k, v in array.entries(to=node).items() if not k.endswith("zarr.json")}
                    assert mine == chunks, f"{path.name} {node}"
                    kind = "tiled" if hdu["compression"] else (under or "image")
                    seen[kind] += 1
                    seen["copied"] += any(isinstance(v, bytes) for v in chunks.values())
    assert all(seen[k] for k in ("image", "rows", "heap", "tiled")), seen
    assert copy_threshold is None or seen["copied"], seen


def test_the_routines_ask_for_no_output_of_refkv_s(monkeypatch):
    async def refuse(_self):
        raise AssertionError("refkv's own output was asked for")

    monkeypatch.setattr(Parsed, "_output_async", refuse)
    with read(FIXTURES / "fits" / "fits_comp_rice_int16.fits") as parsed:
        assert set(fits_arrays(parsed, 1)) == {""}
        assert fits_cards(parsed, 1)[0][0] == "XTENSION"


def test_fits_arrays_refuses_an_input_that_is_not_a_fits_file():
    with read(FIXTURES / "npy" / "npy_c_float64.npy") as parsed, pytest.raises(ValueError, match="not a npy input"):
        fits_arrays(parsed, 0)


def test_fits_arrays_refuses_an_hdu_the_file_does_not_have():
    with read(FIXTURES / "fits" / "fits_mef_sci_err_dq.fits") as parsed, pytest.raises(ValueError, match="no HDU 9"):
        fits_arrays(parsed, 9)


def test_fits_cards_refuses_an_input_that_is_not_a_fits_file():
    with read(FIXTURES / "npy" / "npy_c_float64.npy") as parsed, pytest.raises(ValueError, match="not a npy input"):
        fits_cards(parsed, 0)


def test_fits_cards_refuses_an_hdu_the_file_does_not_have():
    with read(FIXTURES / "fits" / "fits_mef_sci_err_dq.fits") as parsed, pytest.raises(ValueError, match="no HDU 9"):
        fits_cards(parsed, 9)


def test_an_hdu_of_no_data_has_no_arrays():
    with read(FIXTURES / "fits" / "fits_mef_sci_err_dq.fits") as parsed:
        description = parsed.description
        assert description is not None
        assert description["hdus"][0]["kind"] == "empty"
        assert fits_arrays(parsed, 0) == {}
        assert json.dumps(fits_cards(parsed, 0))
