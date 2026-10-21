"""Writes synthetic FITS files to fixtures/fits/, covering the rules of the FITS profile
(spec/virtualize/fits.md §15
<https://github.com/d-v-b/refkv/blob/main/spec/virtualize/fits.md#15-fits-profile>): images of
every BITPIX, multi-extension files, empty HDUs, value scaling, long and odd headers,
binary and ASCII tables with and without a heap, chunking, trailing bytes, a truncated
final block, and the inputs the profile rejects (`fits_reject_*`).

The well-formed files are written by astropy, as real files are; the malformed ones are
astropy's files with their bytes patched, so that every other card is exactly what a
writer produces. js/test/fits/verify.py compares each accepted file's virtualization
with astropy's reading of it.

Usage: uv run python fixtures/generators/fits/write_fixtures.py
"""

from __future__ import annotations

import bz2
import gzip
from pathlib import Path

import numpy as np
from astropy.io import fits

OUT = Path(__file__).parents[2] / "fits"
BLOCK = 2880
CARD = 80


def ramp(shape, dtype: str, seed: int = 0) -> np.ndarray:
    """Distinct values for every pixel, in the native dtype (astropy writes big-endian)."""
    count = int(np.prod(shape))
    values = (np.arange(count, dtype=np.int64) * 7 + seed) % 251
    if not dtype.startswith("u"):
        values = values - 100
    arr = values.astype(dtype)
    if dtype.startswith("f"):
        arr = (arr / 4).astype(dtype)
    return arr.reshape(shape)


def write(name: str, hdus: list, *, trailer: bytes = b"", truncate: int = 0) -> Path:
    """Writes `hdus` (an HDUList's members) as fixtures/fits/<name>.fits, then any trailer
    bytes, and drops the last `truncate` bytes."""
    path = OUT / f"{name}.fits"
    fits.HDUList(hdus).writeto(path, overwrite=True, checksum=False, output_verify="silentfix")
    data = path.read_bytes() + trailer
    path.write_bytes(data[: len(data) - truncate] if truncate else data)
    return path


def patch_card(data: bytes, hdu: int, keyword: str, card: str | bytes) -> bytes:
    """`data` with the first card of `keyword` in HDU `hdu`'s header replaced by `card`
    (80 bytes, padded). The headers are walked as the profile walks them."""
    card = card.encode("latin-1") if isinstance(card, str) else card
    card = card.ljust(CARD, b" ")
    assert len(card) == CARD
    offset = 0
    for i in range(hdu + 1):
        header_end, data_end = hdu_extent(data, offset)
        if i == hdu:
            pos = offset
            while pos < header_end:
                if data[pos : pos + 8].rstrip() == keyword.encode():
                    return data[:pos] + card + data[pos + CARD :]
                pos += CARD
            raise KeyError(keyword)
        offset = data_end
    raise IndexError(hdu)


def hdu_extent(data: bytes, offset: int) -> tuple[int, int]:
    """(end of the header blocks, end of the data blocks) of the HDU at `offset`."""
    cards = {}
    pos = offset
    while True:
        card = data[pos : pos + CARD]
        pos += CARD
        if card[:8] == b"END     ":
            break
        if card[8:10] == b"= ":
            cards[card[:8].decode().strip()] = card[10:].split(b"/")[0].strip().decode()
    header_end = -(-pos // BLOCK) * BLOCK
    naxis = int(cards.get("NAXIS", 0))
    axes = [int(cards[f"NAXIS{k}"]) for k in range(1, naxis + 1)]
    size = abs(int(cards["BITPIX"])) // 8 * int(cards.get("GCOUNT", 1)) * (int(cards.get("PCOUNT", 0)) + int(np.prod(axes))) if naxis else 0
    return header_end, header_end + -(-size // BLOCK) * BLOCK


def image(data: np.ndarray, name: str | None = None, ver: int | None = None, **cards) -> fits.ImageHDU:
    h = fits.ImageHDU(data=data, name=name, ver=ver)
    for k, v in cards.items():
        h.header[k] = v
    return h


def wcs(h, crpix=(1025.0, 745.0), crval=(229.5379888, 42.7632764), cd=6.12245e-05):
    """A TAN world coordinate system on axes 1 and 2."""
    h.header["CTYPE1"] = ("RA---TAN", "Right ascension, gnomonic")
    h.header["CTYPE2"] = ("DEC--TAN", "Declination, gnomonic")
    h.header["CRPIX1"] = crpix[0]
    h.header["CRPIX2"] = crpix[1]
    h.header["CRVAL1"] = crval[0]
    h.header["CRVAL2"] = crval[1]
    h.header["CD1_1"] = cd
    h.header["CD1_2"] = 0.0
    h.header["CD2_1"] = 0.0
    h.header["CD2_2"] = -cd
    h.header["RADESYS"] = "FK5"
    return h


def primary(data=None, **cards) -> fits.PrimaryHDU:
    h = fits.PrimaryHDU(data=data)
    for k, v in cards.items():
        h.header[k] = v
    return h


def accepted() -> None:
    # One primary image per BITPIX, with a WCS, a unit, and the unsigned convention.
    p = wcs(primary(ramp((6, 8), "u2", 1), BUNIT=("COUNTS", "pixel unit"), OBJECT="M42"))
    write("fits_primary_uint16", [p])
    write("fits_primary_int16", [wcs(primary(ramp((5, 7), "i2", 2), BUNIT="DN"))])
    write("fits_primary_uint8", [primary(ramp((4, 9), "u1", 3))])
    write("fits_primary_int32", [primary(ramp((3, 5), "i4", 4), BLANK=(-1, "undefined pixels"))])
    write("fits_primary_int64", [primary(ramp((2, 5), "i8", 5))])
    write("fits_primary_float32", [primary(ramp((7, 6), "f4", 6), BUNIT="MJy/sr")])
    write("fits_primary_float64", [primary(ramp((3, 4), "f8", 7))])
    write("fits_primary_uint32", [primary(ramp((3, 4), "u4", 8))])
    # Value scaling that is not the unsigned convention, and a 1-D and a 4-D array.
    p = primary(ramp((4, 5), "i2", 9))
    p.header["BSCALE"] = 0.045777764213996
    p.header["BZERO"] = 1500.0
    write("fits_primary_scaled", [p])
    write("fits_primary_1d", [primary(ramp((2048,), "f4", 10))])
    cube = primary(ramp((1, 5, 4, 3), "f4", 11))  # (Stokes, freq, dec, ra) as a radio cube
    cube.header["CTYPE1"], cube.header["CTYPE2"], cube.header["CTYPE3"], cube.header["CTYPE4"] = "RA---SIN", "DEC--SIN", "FREQ", "STOKES"
    cube.header["CRVAL3"], cube.header["CDELT3"], cube.header["CRPIX3"], cube.header["CUNIT3"] = 1.4e9, 1e6, 1.0, "Hz"
    write("fits_primary_cube", [cube])

    # A multi-extension file in HST's shape: an empty primary, SCI/ERR/DQ per chip, EXTVER.
    hdus = [primary(TELESCOP="HST", INSTRUME="WFC3", NEXTEND=6)]
    for chip in (1, 2):
        hdus.append(wcs(image(ramp((5, 6), "f4", 20 + chip), "SCI", chip, BUNIT="ELECTRONS"), crpix=(1.0 + chip, 2.0)))
        hdus.append(image(ramp((5, 6), "f4", 30 + chip), "ERR", chip, BUNIT="ELECTRONS"))
        hdus.append(image(ramp((5, 6), "i2", 40 + chip), "DQ", chip, BUNIT="UNITLESS"))
    write("fits_mef_sci_err_dq", hdus)
    # STIS raw: ERR and DQ extensions with NAXIS = 0 and full headers.
    hdus = [primary(TELESCOP="HST", INSTRUME="STIS"), wcs(image(ramp((4, 6), "u2", 50), "SCI", 1))]
    for name in ("ERR", "DQ"):
        h = fits.ImageHDU(data=None, name=name, ver=1)
        h.header["BUNIT"] = "COUNTS"
        wcs(h)
        hdus.append(h)
    write("fits_mef_naxis0", hdus)
    # An extension with NAXIS2 = 0: an empty array, no chunks.
    write("fits_mef_naxisn_zero", [primary(), image(np.zeros((0, 5), dtype=np.int16), "EMPTY")])
    # Four chips named alike, in a WFPC2 file's shape: names repeat, EXTVER tells them apart.
    write("fits_mef_same_name", [primary()] + [image(ramp((3, 4), "i2", 60 + k), "SCI", k) for k in range(1, 5)])
    # Two primaries' worth of images with no EXTNAME at all.
    write("fits_mef_unnamed", [primary(ramp((3, 3), "u1", 70)), fits.ImageHDU(ramp((2, 2), "u1", 71)), fits.ImageHDU(ramp((2, 3), "i2", 72))])

    # Headers: long strings, HIERARCH, commentary cards, blank cards, duplicates, D exponents, complex values.
    p = primary(ramp((2, 3), "i2", 80))
    h = p.header
    h["LONGSTR"] = ("A string value that goes on past the eighty columns of one card, so astropy continues it. " * 3, "a comment")
    h.append(fits.Card("HIERARCH ESO INS OPTI-3 ID", "ESO#427", "a HIERARCH card"), end=True)
    h.append(fits.Card("HIERARCH ESO DET DIT", 0.1), end=True)
    h.add_comment("a comment card")
    h.add_comment("another, with 'quotes' and a / slash")
    h.add_history("made by write_fixtures.py")
    h.add_blank()
    h.add_blank("text on a blank card")
    h["DUPKEY"] = 1
    h.append(fits.Card("DUPKEY", 2, "the same keyword twice"), end=True)
    h.append(fits.Card.fromstring("DEXPON  =        1.5000000000D+03 / a real with a D exponent"), end=True)
    h.append(fits.Card.fromstring("BIGINT  =      123456789012345678901 / an integer past 2^53"), end=True)
    h["CPLX"] = (1.5 + 2.5j, "a complex value")
    h["LOGIC"] = (False, "a logical")
    h["EMPTYSTR"] = ""
    h["QUOTED"] = "it''s"
    h["TRAILSP"] = "trailing   "
    h["NOVALUE"] = None
    write("fits_header_cards", [p])
    # A header past the cards budget of the node's document: about 1200 cards.
    p = primary(ramp((2, 2), "i2", 81))
    for k in range(1200):
        p.header[f"KEY{k:05d}"] = (f"value number {k} of a long header", f"comment {k}")
    write("fits_header_huge", [p])

    # Tables: fixed columns of every TFORM letter, TDIM, TSCAL/TZERO, TNULL; a heap; an ASCII table.
    cols = [
        fits.Column(name="c_l", format="L", array=np.array([True, False, True])),
        fits.Column(name="c_x", format="3X", array=np.array([[1, 0, 1], [0, 1, 0], [1, 1, 1]])),
        fits.Column(name="c_b", format="B", array=np.array([1, 2, 255], dtype=np.uint8)),
        fits.Column(name="c_i", format="I", array=np.array([-1, 2, 3], dtype=np.int16), unit="ADU"),
        fits.Column(name="c_j", format="J", array=np.array([-4, 5, 6], dtype=np.int32), null=-999),
        fits.Column(name="c_k", format="K", array=np.array([-7, 8, 9], dtype=np.int64)),
        fits.Column(name="c_a", format="5A", array=np.array(["ab", "cde", "f"])),
        fits.Column(name="c_e", format="E", array=np.array([1.5, -2.5, 3.5], dtype=np.float32)),
        fits.Column(name="c_d", format="2D", array=np.array([[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]]), dim="(2)"),
        fits.Column(name="c_c", format="C", array=np.array([1 + 2j, 3 + 4j, 5 + 6j], dtype=np.complex64)),
        fits.Column(name="c_m", format="M", array=np.array([1 + 2j, 3 + 4j, 5 + 6j], dtype=np.complex128)),
    ]
    path = write("fits_bintable", [primary(), fits.BinTableHDU.from_columns(cols, name="TAB")])
    # column 4 (c_i) scaled by TSCAL/TZERO, patched in: astropy cannot write a scaled integer column
    data = _append_card(path.read_bytes(), 1, "TSCAL4  =                  0.5 / scaling of c_i")
    path.write_bytes(_append_card(data, 1, "TZERO4  =                 10.0 / offset of c_i"))
    var = fits.Column(name="var", format="PI()", array=np.array([np.array([1, 2, 3], dtype=np.int16), np.array([], dtype=np.int16), np.array([4], dtype=np.int16)], dtype=object))
    fixed = fits.Column(name="xyz", format="2I", array=np.array([[1, 2], [3, 4], [5, 6]], dtype=np.int16))
    write("fits_bintable_heap", [primary(), fits.BinTableHDU.from_columns([var, fixed], name="VLA")])
    q = fits.Column(name="varq", format="QJ()", array=np.array([np.array([1, 2], dtype=np.int32), np.array([3], dtype=np.int32)], dtype=object))
    write("fits_bintable_q", [primary(), fits.BinTableHDU.from_columns([q])])
    # A heap that starts past the rows: THEAP with a gap, patched in after the write.
    path = write("fits_bintable_theap", [primary(), fits.BinTableHDU.from_columns([var, fixed], name="GAP")])
    data = path.read_bytes()
    with fits.open(path) as hdul:
        rows = hdul[1].header["NAXIS1"] * hdul[1].header["NAXIS2"]
        pcount = hdul[1].header["PCOUNT"]
    header_end, data_end = hdu_extent(data, hdu_extent(data, 0)[1])
    heap = data[header_end + rows : header_end + rows + pcount]
    gap = b"\xaa" * 16
    body = data[header_end : header_end + rows] + gap + heap
    body = body.ljust(-(-len(body) // BLOCK) * BLOCK, b"\0")
    data = data[:header_end] + body
    data = patch_card(data, 1, "PCOUNT", f"PCOUNT  = {len(gap) + len(heap):>20} / number of group parameters")
    data = _append_card(data, 1, f"THEAP   = {rows + len(gap):>20} / byte offset to heap")
    path.write_bytes(data)
    # A one-row table with one byte column holding YAML: the JWST ASDF extension's shape.
    yaml = b"#ASDF 1.0.0\n%YAML 1.1\n--- !core/asdf-1.1.0\nmeta: {telescope: JWST}\n...\n"
    asdf = fits.Column(name="ASDF_METADATA", format=f"{len(yaml)}B", array=np.frombuffer(yaml, dtype=np.uint8)[None, :])
    write("fits_bintable_asdf", [primary(), image(ramp((3, 3), "f4", 90), "SCI", 1), fits.BinTableHDU.from_columns([asdf], name="ASDF")])
    ascii_cols = [
        fits.Column(name="a_i", format="I6", array=np.array([1, 22, 333])),
        fits.Column(name="a_f", format="F8.3", array=np.array([1.5, 2.25, -3.125])),
        fits.Column(name="a_s", format="A5", array=np.array(["x", "yy", "zzz"])),
    ]
    write("fits_asciitable", [primary(), fits.TableHDU.from_columns(ascii_cols, name="ASCII")])
    # An empty table (no rows) and a table with no columns.
    write("fits_bintable_empty", [primary(), fits.BinTableHDU.from_columns([fits.Column(name="a", format="J", array=np.array([], dtype=np.int32))])])

    # Chunking (conventions §7 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays>,
    # with the profile's 2^20 limit): 20 planes of 64 KiB, cut into 2 chunks of 10 planes; a plane of 1.1 MB,
    # cut into 2 bands of 550 rows; a row of 2^20 + 1 bytes, cut into 2 chunks, the second padded with a literal byte.
    write("fits_chunk_planes", [primary(ramp((20, 128, 256), "i2", 100))])
    write("fits_chunk_rows", [primary(ramp((1100, 1000), "u1", 101))])
    write("fits_chunk_edge_literal", [primary(ramp((1, 2**20 + 1), "u1", 102))])

    # Bytes after the last HDU (special records), and a final block cut short.
    write("fits_trailing", [primary(ramp((2, 3), "i2", 110))], trailer=b"SPECIAL RECORDS" + bytes(100))
    write("fits_truncated_pad", [primary(ramp((2, 3), "i2", 111))], truncate=BLOCK - 12)
    # Many HDUs: the root's table.
    write("fits_many_hdus", [primary()] + [image(ramp((2, 2), "i2", 200 + k), "IMG", k) for k in range(1, 65)])


def rejected() -> None:
    base = write("fits_reject_base", [primary(ramp((3, 4), "i2", 300))])
    data = base.read_bytes()
    base.unlink()
    mef = write("fits_reject_base", [primary(), image(ramp((3, 4), "i2", 301), "SCI", 1)])
    mefdata = mef.read_bytes()
    mef.unlink()
    table = write("fits_reject_base", [primary(), fits.BinTableHDU.from_columns([fits.Column(name="a", format="J", array=np.array([1, 2], dtype=np.int32))])])
    tabdata = table.read_bytes()
    table.unlink()

    def out(name: str, content: bytes) -> None:
        (OUT / f"fits_reject_{name}.fits").write_bytes(content)

    out("short", data[:100])
    out("simple_false", b"SIMPLE  =                    F" + data[30:])
    out("not_fits", b"NOTFITS " + data[8:])
    out("no_end", patch_card(data[:BLOCK], 0, "END", "COMMENT the END card is missing") + b" " * BLOCK)
    out("header_nul", patch_card(data, 0, "NAXIS2", b"NAXIS2  = \0                  4"))
    out("mandatory_order", data[:CARD] + data[2 * CARD : 3 * CARD] + data[CARD : 2 * CARD] + data[3 * CARD :])  # BITPIX and NAXIS swapped
    out("bitpix", patch_card(data, 0, "BITPIX", "BITPIX  =                   12 / wrong"))
    out("bitpix_text", patch_card(data, 0, "BITPIX", "BITPIX  = 'sixteen'"))
    out("naxis_negative", patch_card(data, 0, "NAXIS", "NAXIS   =                   -1"))
    out("naxis_1000", patch_card(data, 0, "NAXIS", "NAXIS   =                 1000"))
    out("naxisn_negative", patch_card(data, 0, "NAXIS1", "NAXIS1  =                   -4"))
    out("naxisn_missing", patch_card(data, 0, "NAXIS2", "NAXIS3  =                    3"))
    out("pcount_negative", patch_card(mefdata, 1, "PCOUNT", "PCOUNT  =                   -1"))
    out("gcount_zero", patch_card(mefdata, 1, "GCOUNT", "GCOUNT  =                    0"))
    out("xtension_unknown", patch_card(mefdata, 1, "XTENSION", "XTENSION= 'FOREIGN '"))
    out("extension_first", patch_card(data, 0, "SIMPLE", "XTENSION= 'IMAGE   '"))
    # Random groups, by hand (astropy cannot write a GroupsHDU in memory): 2 groups of 2
    # parameters and a 3 x 4 float32 array each.
    groups = header_of([
        "SIMPLE  =                    T", "BITPIX  =                  -32", "NAXIS   =                    3",
        "NAXIS1  =                    0", "NAXIS2  =                    4", "NAXIS3  =                    3",
        "GROUPS  =                    T", "PCOUNT  =                    2", "GCOUNT  =                    2",
        "PTYPE1  = 'UU      '", "PTYPE2  = 'VV      '",
    ])
    out("groups", groups + bytes(BLOCK))
    out("data_outside", data[: len(data) - (BLOCK - 24 + 1)])  # the 24 data bytes, less one
    out("data_overflow", patch_card(patch_card(data, 0, "NAXIS1", "NAXIS1  =     4000000000000000"), 0, "NAXIS2", "NAXIS2  =     4000000000000000"))
    out("table_bitpix", patch_card(tabdata, 1, "BITPIX", "BITPIX  =                   16"))
    out("table_naxis", remove_card(patch_card(tabdata, 1, "NAXIS", "NAXIS   =                    1"), 1, "NAXIS2"))
    out("tfields", patch_card(tabdata, 1, "TFIELDS", "TFIELDS =                 1000"))
    out("tform_missing", patch_card(tabdata, 1, "TFORM1", "TFORMX  = 'J       '"))
    out("theap_small", _with_theap(tabdata, 4))
    out("ztable", _append_card(tabdata, 1, "ZTABLE  =                    T"))
    (OUT / "fits_reject_gzip_file.fits.gz").write_bytes(gzip.compress(data, mtime=0))
    (OUT / "fits_reject_bzip2_file.fits.bz2").write_bytes(bz2.compress(data))


def remove_card(data: bytes, hdu: int, keyword: str) -> bytes:
    """`data` with the first card of `keyword` in HDU `hdu` removed, the header's end padded."""
    offset = 0
    for _ in range(hdu):
        offset = hdu_extent(data, offset)[1]
    header_end, _ = hdu_extent(data, offset)
    pos = offset
    while data[pos : pos + 8].rstrip() != keyword.encode():
        pos += CARD
        assert pos < header_end, keyword
    return data[:pos] + data[pos + CARD : header_end] + b" " * CARD + data[header_end:]


def header_of(cards: list[str]) -> bytes:
    """A header's blocks from card texts (each padded to 80), with END and space padding."""
    text = b"".join(c.encode().ljust(CARD, b" ") for c in cards + ["END"])
    return text.ljust(-(-len(text) // BLOCK) * BLOCK, b" ")


def _with_theap(tabdata: bytes, theap: int) -> bytes:
    """The table with a THEAP below the rows' bytes (and a PCOUNT that would hold a heap)."""
    data = patch_card(tabdata, 1, "PCOUNT", f"PCOUNT  = {8:>20}")
    return _append_card(data, 1, f"THEAP   = {theap:>20}")


def _append_card(data: bytes, hdu: int, card: str) -> bytes:
    """`data` with `card` inserted before HDU `hdu`'s END card; the header grows by a block
    when its last block is full."""
    offset = 0
    for _ in range(hdu):
        offset = hdu_extent(data, offset)[1]
    header_end, _ = hdu_extent(data, offset)
    pos = offset
    while data[pos : pos + 8] != b"END     ":
        pos += CARD
    cards = data[offset:pos] + card.encode().ljust(CARD, b" ") + data[pos : pos + CARD]
    new_end = offset + -(-len(cards) // BLOCK) * BLOCK
    return data[:offset] + cards.ljust(new_end - offset, b" ") + data[header_end:]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for p in list(OUT.glob("*.fits")) + list(OUT.glob("*.fits.*")):
        p.unlink()
    accepted()
    rejected()


if __name__ == "__main__":
    main()
