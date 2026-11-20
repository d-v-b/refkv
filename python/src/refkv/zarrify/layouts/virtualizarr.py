"""The layout `refkv.virtualizarr` (spec/layout.md
<https://github.com/d-v-b/refkv/blob/main/spec/layout.md>): a file presented as VirtualiZarr's own
parsers present it, so that code written against `HDFParser`, `FITSParser` or `ZippedZarrParser`
finds the arrays, dimension names and attributes it expects. Its chunk references, chunk grids and
codecs are those of refkv's own output, and it writes none of refkv's declarations and no
`refkv_source` mirror. It writes its documents with `dump`, so it runs without the `layouts` extra.

    python -m refkv.zarrify data.nc out.vzip --layout refkv.virtualizarr

The reference is VirtualiZarr 2.8.0 (`virtualizarr/parsers/hdf/hdf.py`, `parsers/fits.py`,
`parsers/zarr/`), read profile by profile:

- **HDF5 and netCDF-4** (`HDFParser`): every group and dataset of the base at its path, with the
  base's `dimension_names` (its dimension scales, else the names netCDF-4 gives an axis without
  one), and `[]` for a scalar. Attributes are the source's, as `coding.hdf5_attributes` gives them, without the
  bookkeeping `HDFParser` hides (`CLASS`, `NAME`, `DIMENSION_LIST`, `REFERENCE_LIST`,
  `_Netcdf4Dimid`, `_Netcdf4Coordinates`, `_nc3_strict`, `_NCProperties`) nor any whose value is
  `"DIMENSION_SCALE"`; an array of one element is that element, an array of fixed-length strings a
  list of strings, a null attribute `""`, and a numeric dataset's `_FillValue` is encoded as xarray's
  `FillValueCoder` encodes it (a float as the Base64 of its binary64). A fixed-length string
  dataset, which the base holds as uint8 with a trailing axis of its bytes, is a
  `null_terminated_bytes` array without that axis, its chunks at the keys the shorter grid gives
  them, as `HDFParser` presents it through numpy's `S<n>`; `vlen-utf8` gets its empty
  configuration.
- **FITS** (`FITSParser`): each HDU with data is an array at the root named by `EXTNAME` (else
  `PRIMARY`, `COMPRESSED_IMAGE` or its index, a repeat suffixed `_<index>`), its axes named
  `<name>_row`, `<name>_y`, `<name>_x`, `<name>_z` or `<name>_axis<n>`, its attributes its header's
  cards (the first of a repeated keyword, each record of a record-valued keyword its own
  `<keyword>.<field>` member, without commentary cards, a complex value as Python's text of it).
  An image with `BSCALE` or `BZERO` is float64 through `numcodecs.fixedscaleoffset`, and an
  integer image's `BLANK` its `_FillValue`, scaled. An ASCII table is a struct array read by
  VirtualiZarr's `virtualizarr.fits_ascii_table` codec, which a reader registers by importing
  `virtualizarr`. A primary HDU with no data gives the root its attributes; another is left out.
- **Zipped Zarr v3** (`ZippedZarrParser`): the store's hierarchy as the base has it.

Where VirtualiZarr's presentation is a defect rather than a contract, this layout keeps refkv's:

- the names netCDF-4 gives an axis without a dimension scale (a dimension scale of its group of
  the same size, else `phony_dim_<n>`, numbered across the file by size), where `HDFParser` names
  every such axis `phony_dim_<axis>`, which gives one name two sizes in a group that xarray then
  refuses to open;
- the chunks of variable-length strings, which `HDFParser` reads at parse time and inlines;
- refkv's chunk grid, where refkv splits a large contiguous dataset or HDU into chunks within its
  payload limit or `HDFParser` trims an oversized chunk, and the `numcodecs.zlib` level (refkv
  writes 1; a decoder ignores it, and the IR does not hold the filter's client data);
- a one-element array of fixed-length strings on a dataset with dimension names is its string,
  where `HDFParser` keeps a list: the base writes such an attribute as its element (hdf5.md §6
  <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#6-attributes>), and its source
  metadata does not say which it was;
- the datasets refkv does not present (spec/zarrify/hdf5.md §7.4
  <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#74-what-is-not-presented>), such
  as a fixed-length string dataset whose fill value is not one byte repeated;
- an empty fixed-length string attribute is `""`, not `HDFParser`'s `" "`;
- a FITS card with no value is `null`, not the text `"None"`;
- a FITS tile-compressed image is refkv's decoded image (`FITSParser` points the raw table bytes
  at an uncompressed image), its attributes the image header astropy reconstructs;
- a FITS binary table, which `FITSParser` refuses, is refkv's group for the HDU, under the HDU's
  name, as is any other HDU it cannot present;
- a zipped Zarr store's own chunk key encoding, where `ZippedZarrParser`'s `ManifestStore`
  presents a `.` separator.

`check` finds no problem on its output but `json` at a document that holds a `NaN` or `Infinity`
token (only where a source attribute is not finite and `nonfinite` is `"token"`: zarr-python
writes those tokens, and VirtualiZarr's metadata is written by zarr-python), and `copy` at a
fixed-length string dataset or an ASCII table, whose chunks are the base's at the keys of a grid
without the base's trailing axis of bytes.
"""

import base64
import math
import re
import struct
from collections.abc import Mapping
from typing import Any, ClassVar

from refkv.zarrify import Rejected
from refkv.zarrify.coding import hdf5_attributes
from refkv.zarrify.layouts import Base, Entries, EntryValue, chunk_coords, chunk_key, dump

# ---- HDF5 and netCDF-4

# the attributes HDFParser hides (virtualizarr/parsers/hdf/hdf.py, _extract_attrs)
HIDDEN = frozenset(["REFERENCE_LIST", "CLASS", "DIMENSION_LIST", "NAME", "_Netcdf4Dimid", "_Netcdf4Coordinates",
                    "_nc3_strict", "_NCProperties"])
VLEN_UTF8: dict[str, object] = {"name": "vlen-utf8", "configuration": {}}  # as zarr-python writes the codec
NUMERIC = frozenset(["integer", "float", "enum", "bitfield"])  # HDF5 datatype classes FillValueCoder encodes


def _nonfinite(value: object, token: bool) -> object:
    """`value`, each NaN or infinity in it as zarr-python writes it (a float, which `dump` writes as
    a bare token), or as the text `"NaN"`, `"Infinity"`, `"-Infinity"`."""
    if isinstance(value, float) and not math.isfinite(value):
        if token:
            return value
        return "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
    if isinstance(value, list):
        return [_nonfinite(v, token) for v in value]
    if isinstance(value, dict):
        return {k: _nonfinite(v, token) for k, v in value.items()}
    return value


def _one(value: object, kind: object) -> object:
    """An array of one element in all as that element, as HDFParser takes `v.flatten()[0]`, but
    an array of fixed-length strings (datatype class `kind` `string`), which HDFParser keeps a
    list."""
    if kind == "string":
        return value
    inner = value
    while isinstance(inner, list) and len(inner) == 1:
        inner = inner[0]
    return value if isinstance(inner, list) else inner


def _fill_value_coded(value: object, kind: str | None) -> object:
    """A netCDF `_FillValue` as xarray's `FillValueCoder` encodes it for a dataset of datatype
    class `kind`: a float as the Base64 of its binary64, an integer as an integer."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    if kind == "float":
        return base64.standard_b64encode(struct.pack("<d", float(value))).decode()
    if kind in ("integer", "enum", "bitfield"):
        return int(value)
    return value


def _presented(base: Base, path: str, kind: str | None) -> dict[str, Any]:
    """The attributes HDFParser presents for the node at `path`, `kind` its dataset's datatype
    class (None for a group), as Python values (NaN and the infinities as floats)."""
    metadata = base.source_metadata(path) or {}
    described = metadata.get("attributes", {}) if isinstance(metadata, dict) else {}
    out: dict[str, Any] = {}
    for name, given in hdf5_attributes(base, path).items():
        if name in HIDDEN:
            continue
        value = _one(given, (described.get(name) or {}).get("class"))
        if value is None and (described.get(name) or {}).get("class") is not None:
            value = ""  # a null dataspace: h5py's Empty, which HDFParser writes as ""
        if value == "DIMENSION_SCALE":
            continue
        out[name] = _fill_value_coded(value, kind) if name == "_FillValue" and kind in NUMERIC else value
    return out


def _string_length(base: Base, path: str, doc: Mapping[str, Any]) -> int | None:
    """The length of a fixed-length string dataset the base holds as uint8 with a trailing axis of
    its bytes, or None for another array."""
    metadata = base.source_metadata(path)
    datatype = metadata.get("datatype") if isinstance(metadata, dict) else None
    if not isinstance(datatype, dict) or datatype.get("class") != "string" or doc.get("data_type") != "uint8":
        return None
    size, shape = datatype.get("size"), doc.get("shape") or []
    chunks = doc["chunk_grid"]["configuration"]["chunk_shape"]
    if type(size) is not int or not shape or shape[-1] != size or chunks[-1] != size:
        return None
    return size


def _bytes_fill(fill: object, length: int) -> str:
    """A uint8 fill value as a `null_terminated_bytes` one: the Base64 of `length` such bytes,
    trailing NULs dropped, as numpy holds `S<n>` (b"" for zero)."""
    byte = fill if isinstance(fill, int) and 0 <= fill < 256 else 0
    return base64.standard_b64encode(bytes([byte] * length).rstrip(b"\0")).decode()


def _hdf5(base: Base, token: bool) -> Entries:
    out: Entries = {}
    for path in base.nodes():
        prefix = f"{path}/" if path else ""
        if base.node_type(path) == "group":
            attrs = _nonfinite(_presented(base, path, None), token)
            out[f"{prefix}zarr.json"] = dump({"zarr_format": 3, "node_type": "group", "attributes": attrs},
                                             allow_nan=token)
            continue
        doc, chunks = base.array(path)
        metadata = base.source_metadata(path) or {}
        datatype = metadata.get("datatype") if isinstance(metadata, dict) else None
        kind = datatype.get("class") if isinstance(datatype, dict) else None
        doc = dict(doc)
        length = _string_length(base, path, doc)
        if length is not None:
            doc["shape"] = doc["shape"][:-1]
            doc["chunk_grid"] = {"name": "regular", "configuration": {
                "chunk_shape": doc["chunk_grid"]["configuration"]["chunk_shape"][:-1]}}
            doc["data_type"] = {"name": "null_terminated_bytes", "configuration": {"length_bytes": length}}
            doc["fill_value"] = _bytes_fill(doc.get("fill_value"), length)
            if doc.get("dimension_names") is not None:
                doc["dimension_names"] = doc["dimension_names"][:-1]
            kind = "string"
            before = {**doc, "shape": [*doc["shape"], length]}
            chunks = {chunk_key(chunk_coords(k, before)[:-1], doc): v for k, v in chunks.items()}
        if doc.get("dimension_names") is None and doc["shape"] == []:
            doc["dimension_names"] = list[str]()
        doc["codecs"] = [VLEN_UTF8 if c == {"name": "vlen-utf8"} else c for c in doc["codecs"]]
        doc["attributes"] = _nonfinite(_presented(base, path, kind), token)
        out[f"{prefix}zarr.json"] = dump(doc, allow_nan=token)
        out.update({f"{prefix}{k}": v for k, v in chunks.items()})
    return out


# ---- FITS

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
    """A card's value as FITSParser writes it: a complex value (the base's pair) as Python's text
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
INTEGER = re.compile(r"[+-]?[0-9]+")
REAL = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[EeDd][+-]?[0-9]+)?")
COMPLEX = re.compile(r"\(\s*(" + REAL.pattern + r")\s*,\s*(" + REAL.pattern + r")\s*\)")


def _real(text: str) -> object:
    value = float(text.replace("D", "E").replace("d", "e"))
    return value if math.isfinite(value) else {"text": text}


def _value_field(field: str) -> object:
    """A card's value field (bytes 10-79) as spec/zarrify/fits.md §5.3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#53-cards> reads it."""
    text = field.lstrip()
    if text.startswith("'"):
        out: list[str] = []
        i = 1
        while i < len(text):
            if text[i] == "'":
                if text[i + 1:i + 2] == "'":
                    out.append("'")
                    i += 2
                    continue
                break
            out.append(text[i])
            i += 1
        return "".join(out).rstrip()
    text = text.partition("/")[0].strip()
    if not text:
        return None
    if text in ("T", "F"):
        return text == "T"
    if INTEGER.fullmatch(text):
        return int(text)
    if REAL.fullmatch(text):
        return _real(text)
    if (pair := COMPLEX.fullmatch(text)) is not None:
        return [_real(pair.group(1)), _real(pair.group(2))]
    return {"text": text}


def _parsed_cards(records: list[str]) -> list[list[Any]]:
    """A header's cards from its 80-byte records, as the base's `cards` holds them, where the base
    leaves them out (a header past its budget); comments are not read."""
    out: list[list[Any]] = []
    for record in records:
        keyword = record[:8].rstrip()
        if keyword == "END":
            break
        last = out[-1][1] if out else None
        if keyword == "CONTINUE" and record[8:10] == "  " and isinstance(last, str) and last.endswith("&"):
            piece = _value_field(record[10:])
            if isinstance(piece, str):
                out[-1][1] = last[:-1] + piece
                continue
        if keyword == "HIERARCH" and record[8:10] != "= " and "=" in record[8:]:
            name, _, field = record[8:].partition("=")
            out.append([name.strip(), _value_field(field), None])
        elif record[8:10] == "= ":
            out.append([keyword, _value_field(record[10:]), None])
        else:
            out.append([keyword, None, None])
    return out


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


def _node_name(name: str) -> bool:
    """Whether a Zarr node may be named `name`."""
    return bool(name) and "/" not in name and name not in (".", "..") and not name.startswith("__")


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
        # the base's own codec (refkv.fits_tiles) gives native integers; its bytes codec big-endian ones
        astype = stored if bytes_codecs[0].get("name") == "bytes" else "<" + stored[1:] if stored[0] == ">" else stored
        doc = {**doc, "data_type": "float64", "fill_value": 0.0, "codecs": [
            {"name": "numcodecs.fixedscaleoffset",
             "configuration": {"offset": bzero, "scale": 1 / bscale, "astype": astype, "dtype": "<f8"}},
            *bytes_codecs]}
    if blank is not None:
        header = {**header, "_FillValue": _fill_value_coded(blank, "float" if scaled else "integer")}
    return {**doc, "attributes": header}


def _ascii_table(base: Base, path: str, name: str, cards: list[list[Any]],
                 records: set[tuple[str, str]] | None) -> Entries | None:
    """An ASCII table HDU as FITSParser presents it: a struct array of its columns, decoded by
    VirtualiZarr's codec from the table's one chunk; None for a table whose columns this cannot
    read as astropy does (a TFORM without a width)."""
    rows = f"{path}/rows"
    if base.node_type(rows) != "array":
        return None
    doc, chunks = base.array(rows)
    table = {k: v for k, v, _ in reversed(cards)}
    nrows, row_nbytes = int(table.get("NAXIS2", 0)), int(table.get("NAXIS1", 0))
    whole = [nrows, row_nbytes]
    if doc.get("shape") != whole or doc["chunk_grid"]["configuration"]["chunk_shape"] != whole \
            or doc["codecs"][0].get("name") != "bytes":
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
        # the base's bytes-to-bytes codecs after it: a FITS file compressed whole (gzip, bzip2)
        "codecs": [{"name": ASCII_CODEC, "configuration": {"columns": columns, "row_nbytes": row_nbytes}},
                   *doc["codecs"][1:]],
        "attributes": _header(cards, records),
        "dimension_names": _dimension_names(name, 1),
    }
    out: Entries = {f"{name}/zarr.json": dump(out_doc)}
    for key, value in chunks.items():
        out[f"{name}/{chunk_key(chunk_coords(key, doc)[:1], out_doc)}"] = value
    return out


def _fits(base: Base) -> Entries:
    hdus = sorted((int(p), p) for p in base.nodes() if p and "/" not in p and p.isdigit())
    names: list[str] = []
    out: Entries = {}
    root: dict[str, Any] = {}
    for index, path in hdus:
        metadata = base.source_metadata(path) or {}
        cards = metadata.get("cards") if isinstance(metadata, dict) else None
        header = base.ir.at(f"hdus/{index}/header")
        lines = base.ir.value(header) if header is not None else None
        raw = [str(r["card"]) for r in lines if isinstance(r, dict)] if isinstance(lines, list) else None
        if cards is None:  # past the base's budget: the header's records, from the IR
            cards = _parsed_cards(raw) if raw is not None else []
        records = _records(raw)
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
        is_image = (table.get("SIMPLE") is True and not table.get("GROUPS")) or table.get("XTENSION") == "IMAGE" \
            or compressed
        if is_image and base.node_type(path) == "array":
            doc, chunks = base.array(path)
            header = _header(_image_header(cards) if compressed else cards, records)
            doc = _scaled(dict(doc), header, table)
            if str(doc["data_type"]).startswith("float") and type(doc.get("fill_value")) is int:
                doc["fill_value"] = float(doc["fill_value"])  # as zarr-python writes a float's zero
            doc["dimension_names"] = _dimension_names(name, len(doc["shape"]))
            out[f"{name}/zarr.json"] = dump(doc)
            out.update({f"{name}/{k}": v for k, v in chunks.items()})
            continue
        ascii_table = _ascii_table(base, path, name, cards, records) if table.get("XTENSION") == "TABLE" else None
        if ascii_table is not None:
            out.update(ascii_table)
            continue
        # an HDU FITSParser cannot present (a binary table): refkv's node for it, under its name
        for key, value in base.entries(f"{path}/", declarations=False, mirror=False, datasets=False).items():
            out[f"{name}/{key[len(path) + 1:]}"] = value
    out["zarr.json"] = dump({"zarr_format": 3, "node_type": "group", "attributes": root})
    return out


# ---- the layout

class VirtualiZarr:
    name = "refkv.virtualizarr"
    formats: ClassVar[Mapping[str, frozenset[int]]] = {
        "hdf5": frozenset({0}), "fits": frozenset({0}), "zarr3": frozenset({0})}

    def options(self, given: Mapping[str, object]) -> dict[str, object]:
        """`{"nonfinite": "token"}` by default: whether a NaN or infinite attribute is a bare JSON
        token, as zarr-python writes it, or the text `"NaN"`, `"Infinity"` or `"-Infinity"`
        (`"text"`), which strict JSON readers parse."""
        unknown = sorted(set(given) - {"nonfinite"})
        if unknown:
            raise ValueError(f"unknown options {unknown}: refkv.virtualizarr takes nonfinite (token or text)")
        nonfinite = given.get("nonfinite", "token")
        if nonfinite not in ("token", "text"):
            raise ValueError(f"nonfinite is token or text, not {nonfinite!r}")
        return {"nonfinite": nonfinite}

    def write(self, base: Base, options: Mapping[str, object]) -> Entries:
        """The base as VirtualiZarr's parser of its format presents it."""
        if base.profile == "hdf5":
            return _hdf5(base, options["nonfinite"] == "token")
        if base.profile == "fits":
            return _fits(base)
        out: dict[str, EntryValue] = base.entries(declarations=False, mirror=False, datasets=False)
        return out
