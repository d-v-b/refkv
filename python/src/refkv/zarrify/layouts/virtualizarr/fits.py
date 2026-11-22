"""How the layout `refkv.virtualizarr` presents a FITS file, as VirtualiZarr 2.8.0's `FITSParser` does
(`virtualizarr/parsers/fits.py`).

**FITS** (`FITSParser`): each HDU with data is an array at the root named by `EXTNAME` (else
`PRIMARY`, `COMPRESSED_IMAGE` or its index, a repeat suffixed `_<index>`), its axes named
`<name>_row`, `<name>_y`, `<name>_x`, `<name>_z` or `<name>_axis<n>`, its attributes its header's
cards (the first of a repeated keyword, each record of a record-valued keyword its own
`<keyword>.<field>` member, without commentary cards, a complex value as Python's text of it).
An image with `BSCALE` or `BZERO` is float64 through `numcodecs.fixedscaleoffset`, and an
integer image's `BLANK` its `_FillValue`, scaled. An ASCII table is a struct array read by
VirtualiZarr's `virtualizarr.fits_ascii_table` codec, which a reader registers by importing
`virtualizarr`. A primary HDU with no data gives the root its attributes; another is left out.

Where `FITSParser`'s presentation is a defect rather than a contract, the layout keeps refkv's:

- refkv's chunk grid, where refkv splits a large HDU into chunks within its payload limit;
- a FITS card with no value is `null`, not the text `"None"`;
- a FITS tile-compressed image is refkv's decoded image (`FITSParser` points the raw table bytes
  at an uncompressed image), its attributes the image header astropy reconstructs;
- a FITS binary table, which `FITSParser` refuses, is refkv's group for the HDU, under the HDU's
  name, as is any other HDU it cannot present.

Given refkv's own output as a base, `check` would report `copy` at an ASCII table, whose chunks are
those of refkv's own output at the keys of a grid without its trailing axis of bytes."""

import base64
import math
import re
from collections.abc import Mapping
from typing import Any

from refkv.build import Array, Parsed, Written, filled
from refkv.build.routines.fits import fits_arrays, fits_cards
from refkv.zarrify import Rejected
from refkv.zarrify.layouts import chunk_key, dump

from ._common import _fill_value_coded, _node_name

COMMENTARY = frozenset(["COMMENT", "HISTORY", ""])  # FITSParser's _COMMENTARY_KEYWORDS
STORED = {8: "|u1", 16: ">i2", 32: ">i4", 64: ">i8", -32: ">f4", -64: ">f8"}  # BITPIX, as numpy's dtype.str
# astropy's reserved keywords of a compressed image's table (astropy/io/fits/hdu/compressed/header.py)
TABLE_INDEXED = frozenset(["TTYPE", "TFORM", "TUNIT", "TNULL", "TSCAL", "TZERO", "TDISP", "TBCOL", "TDIM", "TCTYP",
                           "TCUNI", "TCRPX", "TCRVL", "TCDLT", "TRPOS"])
COMPRESSION = frozenset(["ZSIMPLE", "ZTENSION", "ZBITPIX", "ZNAXIS", "ZEXTEND", "ZBLOCKED", "ZPCOUNT", "ZGCOUNT",
                         "ZHECKSUM", "ZDATASUM", "ZIMAGE", "ZCMPTYPE", "ZMASKCMP", "ZQUANTIZ", "ZDITHER0", "ZBLANK",
                         "ZSCALE", "ZZERO"])
COMPRESSION_INDEXED = frozenset(["ZNAXIS", "ZTILE", "ZNAME", "ZVAL"])
TDEF = re.compile(r"(?P<label>^T[A-Z]*)(?P<num>[1-9][0-9 ]*$)")
ZDEF = re.compile(r"(?P<label>^[Zz][a-zA-Z]*)(?P<num>[1-9][0-9 ]*$)?")
STRUCTURAL = frozenset(["SIMPLE", "XTENSION", "BITPIX", "NAXIS", "EXTEND", "PCOUNT", "GCOUNT", "GROUPS", "TFIELDS",
                        "THEAP", "CHECKSUM", "DATASUM", "BLOCKED"])
ASCII_TYPES = {"A": "S", "I": "i4", "J": "i8", "F": "f8", "E": "f8", "D": "f8"}  # astropy's ASCII2NUMPY
ASCII_FORM = re.compile(r"(?:(?P<format>[AIJ])(?P<width>[0-9]+))|(?:(?P<formatf>[FED])(?P<widthf>[0-9]+)\.[0-9]+)")
ASCII_CODEC = "virtualizarr.fits_ascii_table"  # virtualizarr.codecs.FITS_ASCII_CODEC_NAME
STRUCT_FIELD = {"i2": "int16", "i4": "int32", "i8": "int64", "f8": "float64"}
STRUCT_FILL = {"i2": 0, "i4": 0, "i8": 0, "f8": 0.0}
# zarr-python's default fill value of a struct's string field, which FITSParser's tables get: b"0"
STRING_FILL = base64.standard_b64encode(b"0").decode()


def _card_value(value: object) -> object:
    """A card's value as FITSParser writes it: a complex value (refkv's pair) as Python's text
    of the complex number, a value the standard does not define as its text; a value-less card
    stays null."""
    if isinstance(value, list) and len(value) == 2 and all(isinstance(v, (int, float)) for v in value):
        return str(complex(value[0], value[1]))
    if isinstance(value, dict) and set(value) == {"text"}:
        return value["text"]
    return value


# astropy's record-valued keyword cards (astropy/io/fits/card.py, _rvkc_field_specifier_val)
RECORD = re.compile(r"(?P<field>[a-zA-Z_]\w*(\.\d+)?(\.[a-zA-Z_]\w*(\.\d+)?)*): +"
                    r"(?P<value>[+-]?(\.\d+|\d+(\.\d*)?)([DE][+-]?\d+)?)")
RECORD_CARD = re.compile(r" *'(?P<raw>" + RECORD.pattern + r")' *(/.*)?$")
STANDARD = re.compile(r"[A-Z0-9_-]{1,8}")  # a keyword that is no HIERARCH one


def _records(records: list[str] | None) -> set[tuple[str, str]] | None:
    """The (keyword, value) of each record-valued card of a header's 80-byte records, as astropy
    finds them: the closing quote right after the number; None without the records."""
    if records is None:
        return None
    out = set()
    for record in records:
        if STANDARD.fullmatch(keyword := record[:8].rstrip()) and record[8:10] == "= ":
            match = RECORD_CARD.match(record[10:])
            if match is not None:
                out.add((keyword, match.group("raw")))
    return out


def _record(keyword: str, value: object, records: set[tuple[str, str]] | None) -> tuple[str, object] | None:
    """A record-valued keyword card (`DP1 = 'AXIS.1: 1'`) as astropy reads it, `DP1.AXIS.1` and
    its number as a float, or None for another card (one `records` does not name, given them)."""
    match = RECORD.fullmatch(value) if isinstance(value, str) and STANDARD.fullmatch(keyword) else None
    if match is None or (records is not None and (keyword, value) not in records):
        return None
    number = float(match.group("value").replace("D", "E"))
    return (f"{keyword}.{match.group('field')}", number) if math.isfinite(number) else None


def _header(cards: list[list[Any]], records: set[tuple[str, str]] | None = None) -> dict[str, Any]:
    """A header's cards as attributes, as `dict(header)` gives them: a repeated keyword's first
    value, and each record of a record-valued keyword (of `records`, given them) its own member."""
    out: dict[str, Any] = {}
    for keyword, value, _comment in cards:
        record = _record(keyword, value, records)
        key, shown = record if record is not None else (keyword, _card_value(value))
        if keyword not in COMMENTARY and key not in out:
            out[key] = shown
    return out


def _reserved_compressed(keyword: str) -> bool:
    table = TDEF.match(keyword)
    z = ZDEF.match(keyword)
    return keyword in ("TFIELDS", "THEAP", "CHECKSUM", "DATASUM") or keyword in COMPRESSION \
        or bool(table and table.group("label").upper() in TABLE_INDEXED) \
        or bool(z and z.group("label").upper() in COMPRESSION_INDEXED)


def _image_header(cards: list[list[Any]]) -> list[list[Any]]:
    """A tile-compressed image's header as astropy reconstructs the image's
    (`_bintable_header_to_image_header`): the table's and the compression's keywords removed, and
    the image's own from their `Z` forms."""
    table = {k: v for k, v, _ in reversed(cards)}  # the first of a repeated keyword
    naxis = int(table.get("ZNAXIS", 0))
    head: list[list[Any]] = []
    head.append(["SIMPLE", table["ZSIMPLE"], None] if "ZSIMPLE" in table else ["XTENSION", "IMAGE", None])
    head += [["BITPIX", table.get("ZBITPIX"), None], ["NAXIS", naxis, None]]
    head += [[f"NAXIS{i}", table.get(f"ZNAXIS{i}"), None] for i in range(1, naxis + 1)]
    if "ZSIMPLE" not in table:
        head += [["PCOUNT", table.get("ZPCOUNT", 0), None], ["GCOUNT", table.get("ZGCOUNT", 1), None]]
    rest = [c for c in cards if not _reserved_compressed(c[0]) and c[0] not in STRUCTURAL
            and not (c[0].startswith("NAXIS") and c[0][5:].isdigit())]
    # astropy keeps BSCALE and BZERO only when they are not zero, and the default EXTNAME not
    rest = [c for c in rest if not (c[0] == "EXTNAME" and c[1] == "COMPRESSED_IMAGE")
            and not (c[0] in ("BSCALE", "BZERO") and not c[1])]
    for key, image in (("ZEXTEND", "EXTEND"), ("ZBLOCKED", "BLOCKED"), ("ZHECKSUM", "CHECKSUM"),
                       ("ZDATASUM", "DATASUM")):
        if key in table:
            rest.append([image, table[key], None])
    if int(table.get("ZBITPIX", 0)) > 0 and "BLANK" not in table and "ZBLANK" in table:
        rest.append(["BLANK", table["ZBLANK"], None])
    return head + rest


def _hdu_name(cards: list[list[Any]], index: int, compressed: bool) -> str:
    """astropy's `hdu.name`: its EXTNAME, else the default name of its kind."""
    table = {k: v for k, v, _ in reversed(cards)}
    if "EXTNAME" in table and not (compressed and table["EXTNAME"] == "COMPRESSED_IMAGE"):
        return str(table["EXTNAME"])
    if compressed:
        return "COMPRESSED_IMAGE"
    return "PRIMARY" if index == 0 else ""


def _dimension_names(name: str, ndim: int) -> list[str]:
    """FITSParser's names of an HDU's axes."""
    if ndim == 1:
        return [f"{name}_row"]
    if ndim <= 3:
        return [f"{name}_{axis}" for axis in ["z", "y", "x"][-ndim:]]
    return [f"{name}_axis{ndim - i}" for i in range(ndim)]


def _scaled(doc: dict[str, Any], header: Mapping[str, Any], cards: Mapping[str, Any]) -> dict[str, Any]:
    """An image's document with FITSParser's scaling: float64, read through
    `numcodecs.fixedscaleoffset`, where its header has BSCALE or BZERO, and its BLANK its
    `_FillValue`."""
    bitpix = int(cards.get("ZBITPIX", cards.get("BITPIX", 0)))
    stored = STORED.get(bitpix)
    blank = header.get("BLANK") if stored is not None and stored[1] in "iu" else None
    scaled = stored is not None and ("BSCALE" in header or "BZERO" in header)
    if scaled:
        bscale = float(header.get("BSCALE", 1))
        if bscale == 0:
            raise Rejected("an image with BSCALE = 0, whose every stored value is one physical value")
        bzero = float(header.get("BZERO", 0))
        if blank is not None:
            blank = bzero + bscale * blank
        bytes_codecs = doc["codecs"]
        # refkv's own codec (refkv.fits_tiles) gives native integers; its bytes codec big-endian ones
        astype = stored if bytes_codecs[0].get("name") == "bytes" else "<" + stored[1:] if stored[0] == ">" else stored
        doc = {**doc, "data_type": "float64", "fill_value": 0.0, "codecs": [
            {"name": "numcodecs.fixedscaleoffset",
             "configuration": {"offset": bzero, "scale": 1 / bscale, "astype": astype, "dtype": "<f8"}},
            *bytes_codecs]}
    if blank is not None:
        header = {**header, "_FillValue": _fill_value_coded(blank, "float" if scaled else "integer")}
    return {**doc, "attributes": header}


def _ascii_table(arrays: Mapping[str, Array], name: str, cards: list[list[Any]],
                 records: set[tuple[str, str]] | None) -> Written | None:
    """An ASCII table HDU as FITSParser presents it: a struct array of its columns, decoded by
    VirtualiZarr's codec from the table's one chunk; None for a table whose columns this cannot
    read as astropy does (a TFORM without a width), or whose rows the FITS convention cuts into
    several chunks."""
    rows = arrays.get("rows")
    if rows is None:
        return None
    table = {k: v for k, v, _ in reversed(cards)}
    nrows, row_nbytes = int(table.get("NAXIS2", 0)), int(table.get("NAXIS1", 0))
    whole = [nrows, row_nbytes]
    if rows.shape != whole or rows.chunk_shape != whole or rows.codecs[0].get("name") != "bytes":
        return None
    columns: list[list[object]] = []
    fields: list[dict[str, object]] = []
    fills: dict[str, object] = {}
    for i in range(1, int(table.get("TFIELDS", 0)) + 1):
        form = ASCII_FORM.fullmatch(str(table.get(f"TFORM{i}", "")).strip())
        if form is None or f"TBCOL{i}" not in table:
            return None
        code = (form.group("format") or form.group("formatf")).upper()
        width = int(form.group("width") or form.group("widthf"))
        recformat = ASCII_TYPES[code]
        if code == "I":
            recformat = "i2" if width <= 4 else "i8" if width > 9 else "i4"
        elif code == "A":
            recformat += str(width)
        column = str(table.get(f"TTYPE{i}", ""))
        columns.append([column, int(table[f"TBCOL{i}"]) - 1, width, recformat])
        if code == "A":
            fields.append({"name": column, "data_type": {"name": "null_terminated_bytes",
                                                          "configuration": {"length_bytes": width}}})
            fills[column] = STRING_FILL
        else:
            fields.append({"name": column, "data_type": STRUCT_FIELD[recformat]})
            fills[column] = STRUCT_FILL[recformat]
    out_doc = {
        "zarr_format": 3, "node_type": "array", "shape": [nrows],
        "data_type": {"name": "struct", "configuration": {"fields": fields}},
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [nrows]}},
        "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
        "fill_value": fills,
        # the bytes-to-bytes codecs of the convention's after it: a FITS file compressed whole (gzip, bzip2)
        "codecs": [{"name": ASCII_CODEC, "configuration": {"columns": columns, "row_nbytes": row_nbytes}},
                   *rows.codecs[1:]],
        "attributes": _header(cards, records),
        "dimension_names": _dimension_names(name, 1),
    }
    out = Written({f"{name}/zarr.json": dump(out_doc)})
    for coords, value in rows.chunks.items():
        out[f"{name}/{chunk_key(list(coords[:1]), out_doc)}"] = filled(value)
    return out


def _fits(parsed: Parsed) -> Written:
    """A FITS file as FITSParser presents it, from the parser's description, the headers' cards
    and the arrays the FITS convention writes of each HDU (`fits_cards`, `fits_arrays`): no output
    of refkv's is made."""
    description: Any = parsed.description
    names: list[str] = []
    out = Written()
    root: dict[str, Any] = {}
    for hdu in description["hdus"]:
        index = int(hdu["index"])
        cards = fits_cards(parsed, index)
        lines = parsed.ir.value(int(hdu["elements"]["header"]))
        records = _records([str(r["card"]) for r in lines if isinstance(r, dict)] if isinstance(lines, list) else None)
        table = {k: v for k, v, _ in reversed(cards)}
        compressed = table.get("XTENSION") == "BINTABLE" and table.get("ZIMAGE") is True
        name = _hdu_name(cards, index, compressed) or str(index)
        if not _node_name(name):
            name = str(index)
        if name in names:
            name = f"{name}_{index}"
        names.append(name)
        naxis = int(table.get("ZNAXIS" if compressed else "NAXIS", 0))
        if naxis == 0:
            if index == 0:
                root = _header(cards, records)
            continue
        arrays = fits_arrays(parsed, index)
        is_image = (table.get("SIMPLE") is True and not table.get("GROUPS")) or table.get("XTENSION") == "IMAGE" \
            or compressed
        if is_image and "" in arrays:
            image = arrays[""]
            header = _header(_image_header(cards) if compressed else cards, records)
            doc = _scaled(image.document(), header, table)
            if str(doc["data_type"]).startswith("float") and type(doc.get("fill_value")) is int:
                doc["fill_value"] = float(doc["fill_value"])  # as zarr-python writes a float's zero
            doc["dimension_names"] = _dimension_names(name, len(doc["shape"]))
            out[f"{name}/zarr.json"] = dump(doc)
            for coords, value in image.chunks.items():
                out[f"{name}/{chunk_key(list(coords), doc)}"] = filled(value)
            continue
        ascii_table = _ascii_table(arrays, name, cards, records) if table.get("XTENSION") == "TABLE" else None
        if ascii_table is not None:
            out |= ascii_table
            continue
        # an HDU FITSParser cannot present (a binary table): its group as the FITS convention writes
        # it, under its name, without refkv's declarations
        out[f"{name}/zarr.json"] = dump({"zarr_format": 3, "node_type": "group", "attributes": {}})
        for path, array in arrays.items():
            for key, value in array.entries(to=f"{name}/{path}").items():
                out[key] = value if isinstance(value, bytes) else filled(value)
    out["zarr.json"] = dump({"zarr_format": 3, "node_type": "group", "attributes": root})
    return out


def write(parsed: Parsed, token: bool) -> Written:  # noqa: ARG001 (every profile's writer takes it)
    """A FITS file as `FITSParser` presents it; `token` whether a NaN or infinite attribute
    is a bare JSON token (the option `nonfinite`)."""
    return _fits(parsed)

