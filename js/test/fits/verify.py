"""Checks the browser FITS zarrifier's output against astropy.

Each fixtures/fits/*.fits is served over local HTTP and zarrified by
js/conformance/zarrify.ts (the browser code, run under Node). Every HDU's node is read
through the reference reader and zarr-python: an image's array (a tile-compressed one
decoded by the `refkv.fits_tiles` codec) must equal astropy's data, a table's rows and heap the
file's bytes, and every node's cards astropy's reading of the header
(spec/zarrify/fits.md §5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#5-source-metadata>).
The file must be rebuilt byte for byte from the archive's IR mirror (refkv_source, §6
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#6-the-ir-mirror>).
`fits_reject_*` files must be rejected.

Usage: uv run python js/test/fits/verify.py [<dir of .fits files>]
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

import numpy as np
import zarr
from astropy.io import fits

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "conformance" / "vzip"))

import refkv.zarr  # noqa: E402  (a vzip archive as a zarr store, with the refkv.fits_tiles codec)
from refkv._narrow import at, mapping, ndarray, sequence  # noqa: E402

sys.path.insert(0, str(HERE.parent))
from ir_mirror import mirror_problems  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # js/test, for verify_common
from verify_common import LOOPBACK_SOURCES, run, serve  # noqa: E402

MAX_SAFE = (1 << 53) - 1
COMMENTARY = ("COMMENT", "HISTORY", "")
# hand-built tile-compressed fixtures astropy cannot read: the fixture with the same image, and
# the rows an absent tile leaves at 0
SAME_IMAGE = {"fits_comp_hand_absent_tile": ("fits_comp_hand_one_column", slice(3, 6)),
              "fits_comp_hand_no_ztile": ("fits_comp_hand_one_column", None)}


def card_value(v):
    if isinstance(v, fits.card.Undefined):
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v if abs(v) <= MAX_SAFE else str(v)
    if isinstance(v, complex):
        return [v.real, v.imag]
    return v


def card_problems(ours, cards) -> list[str]:
    """Our cards against astropy's (spec/zarrify/fits.md §5.3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#53-cards>)."""
    if ours is None:
        return [] if len(cards) > 700 else ["cards are null under the budget"]
    if len(ours) != len(cards):
        return [f"{len(ours)} cards, astropy reads {len(cards)}"]
    problems = []
    for (keyword, value, comment), card in zip(ours, cards, strict=True):
        if keyword != card.keyword:
            problems.append(f"card {keyword!r} is astropy's {card.keyword!r}")
        elif card.keyword in COMMENTARY:
            if value is not None or comment != card.value.rstrip():
                problems.append(f"commentary card {keyword!r}: {comment!r} != {card.value!r}")
        else:
            want = card_value(card.value)
            same = (float(value) == want if isinstance(want, float) and isinstance(value, (int, float))
                    else value == want)
            if not same:
                problems.append(f"card {keyword}: {value!r} != astropy's {want!r}")
    return problems


def one_hdu(hdul: fits.HDUList, i: int):
    """HDU `i` of `hdul` (its indexing is typed to give a slice's HDUList too)."""
    hdu = hdul[i]
    assert not isinstance(hdu, fits.HDUList), i
    return hdu


def pixel_problems(ours, theirs, table) -> list[str]:
    """Quantized floats within the rounding of the dequantization (python/tests/ir/test_fits.py
    says why), everything else exact."""
    if ours.shape != theirs.shape or ours.dtype != theirs.dtype.newbyteorder("="):
        return [f"array {ours.shape} {ours.dtype} is astropy's {theirs.shape} {theirs.dtype}"]
    if theirs.dtype.kind == "f" and table is not None and "ZSCALE" in table.dtype.names:
        bound = 8 * np.finfo(np.float64).eps * (np.max(np.abs(table["ZZERO"]))
                                                + 2.0**31 * np.max(np.abs(table["ZSCALE"])))
        ok = np.allclose(ours, theirs, rtol=np.finfo(theirs.dtype).eps, atol=bound, equal_nan=True)
    else:
        ok = np.array_equal(ours, theirs, equal_nan=theirs.dtype.kind == "f")
    return [] if ok else ["values differ"]


def problems_of(path: Path, out: Path) -> list[str]:
    raw = path.read_bytes()
    root = zarr.open_group(refkv.zarr.open(str(out), policy=LOOPBACK_SOURCES), mode="r", zarr_format=3)
    meta = mapping(at(root.attrs, "refkv_zarrified", "fits"))
    hdus = sequence(meta["hdus"])
    problems = []
    same = SAME_IMAGE.get(path.stem)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with fits.open(path, do_not_scale_image_data=True, disable_image_compression=True) as hdul, \
                fits.open(path, do_not_scale_image_data=True) as decoded:
            if len(hdus) != len(hdul):
                return [f"{len(hdus)} HDUs, astropy reads {len(hdul)}"]
            problems += [f"primary {p}" for p in card_problems(meta["cards"], one_hdu(hdul, 0).header.cards)]
            for i, hdu in enumerate(hdul):
                entry, info = mapping(hdus[i], f"hdus[{i}]"), hdu.fileinfo()
                if entry["offset"] != info["hdrLoc"] or entry["data_offset"] != info["datLoc"]:
                    problems.append(f"HDU {i} offsets {entry['offset']}, {entry['data_offset']} "
                                    f"!= {info['hdrLoc']}, {info['datLoc']}")
                node = root[str(i)]
                problems += [f"HDU {i} {p}" for p in card_problems(at(node.attrs, "refkv_zarrified", "fits", "cards"),
                                                                   hdu.header.cards)]
                if hdu.header.get("ZIMAGE"):
                    if same is not None:
                        ref, absent = same
                        with fits.open(ROOT / "fixtures" / "fits" / f"{ref}.fits",
                                       do_not_scale_image_data=True) as other:
                            want = one_hdu(other, 1).data.copy()
                        if absent is not None:
                            want[absent] = 0
                        problems += [f"HDU {i} {p}"
                                     for p in pixel_problems(ndarray(root.get_array(str(i))[...]), want, None)]
                    else:
                        problems += [f"HDU {i} {p}" for p in pixel_problems(ndarray(root.get_array(str(i))[...]),
                                                                            one_hdu(decoded, i).data, hdu.data)]
                elif isinstance(hdu, (fits.BinTableHDU, fits.TableHDU)):
                    n1, n2 = hdu.header["NAXIS1"], hdu.header["NAXIS2"]
                    rows = raw[info["datLoc"]:info["datLoc"] + n1 * n2]
                    if n1 * n2 and ndarray(root.get_array(f"{i}/rows")[...]).tobytes() != rows:
                        problems.append(f"HDU {i} rows differ")
                    theap = hdu.header.get("THEAP", n1 * n2)
                    heap = hdu.header["PCOUNT"] - (theap - n1 * n2)
                    heap_at = info["datLoc"] + theap
                    if heap and ndarray(root.get_array(f"{i}/heap")[...]).tobytes() != raw[heap_at:heap_at + heap]:
                        problems.append(f"HDU {i} heap differs")
                elif hdu.header["NAXIS"] == 0:
                    if entry["kind"] != "empty" or not isinstance(node, zarr.Group):
                        problems.append(f"HDU {i} is not an empty group")
                else:
                    want = hdu.data
                    if want.size:
                        problems += [f"HDU {i} {p}"
                                     for p in pixel_problems(ndarray(root.get_array(str(i))[...]), want, None)]
    return problems + mirror_problems(out, path)


def main(fixtures: Path) -> int:
    paths = sorted(p for p in fixtures.iterdir() if p.name.endswith((".fits", ".fz")))
    return run(paths, lambda path, out, _: problems_of(path, out), url=serve(paths))


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "fixtures" / "fits"))
