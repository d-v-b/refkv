"""The layout `refkv.virtualizarr` (spec/layout.md
<https://github.com/d-v-b/refkv/blob/main/spec/layout.md>): a file presented as VirtualiZarr's own
parsers present it, so that code written against `HDFParser`, `FITSParser`, `ZarrParser`,
`ZippedZarrParser`, `NetCDF3Parser` or `ENVIParser` finds the arrays, dimension names and
attributes it expects. Its chunk references, chunk grids and codecs are those of refkv's own output,
and it writes none of refkv's declarations and no `refkv_source` mirror. A FITS file's are made from
the parser's description and the IR with the FITS convention's routines (`refkv.build.fits_arrays`
and `fits_cards`), an HDF5 input's from the parser's description with the HDF5 convention's
routine (`refkv.build.hdf5_hierarchy`), and an ENVI raster's from the parser's description with the
ENVI convention's routine (`refkv.build.envi_image`), with no output of refkv's: for those the
layout is that output's peer. Every other format's are still read from refkv's own output
(`refkv.build.project`), until its parser returns a description too.
It writes its documents with `dump`, so it runs without the `layouts` extra.

    python -m refkv.zarrify data.nc out.vzip --layout refkv.virtualizarr

The reference is VirtualiZarr 2.8.0 (`virtualizarr/parsers/hdf/hdf.py`, `parsers/fits.py`,
`parsers/zarr/`, `parsers/netcdf3.py`, `parsers/envi.py`), read profile by profile:

- **HDF5 and netCDF-4** (`HDFParser`): every group and dataset the HDF5 convention presents at its
  path (`refkv.build.hdf5_hierarchy`, made from the parser's description: no output of refkv's),
  with its `dimension_names` (its dimension scales, else the names netCDF-4 gives an axis without
  one), and `[]` for a scalar. Attributes are the source's, as `coding.hdf5_attributes` gives them, without the
  bookkeeping `HDFParser` hides (`CLASS`, `NAME`, `DIMENSION_LIST`, `REFERENCE_LIST`,
  `_Netcdf4Dimid`, `_Netcdf4Coordinates`, `_nc3_strict`, `_NCProperties`) nor any whose value is
  `"DIMENSION_SCALE"`; an array of one element is that element, an array of fixed-length strings a
  list of strings, a null attribute `""`, and a numeric dataset's `_FillValue` is encoded as xarray's
  `FillValueCoder` encodes it (a float as the Base64 of its binary64). A fixed-length string
  dataset, which refkv's own output holds as uint8 with a trailing axis of its bytes, is a
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
- **Zipped Zarr v3** (`ZippedZarrParser`): the store's hierarchy as refkv's own output has it.
- **Zarr v2** (`ZarrParser`): the nodes of refkv's own output, each with its `.zattrs` as
  attributes, and an array's xarray `_ARRAY_DIMENSIONS` its `dimension_names` rather than an
  attribute (where it names each axis with a string or null; another stays an attribute, on which
  `ZarrParser` fails). An array of `|S<n>` or `|V<n>`, which refkv's own output holds as uint8 with
  a trailing axis of its bytes, is a `null_terminated_bytes` or `raw_bytes` array without that
  axis, blosc's `typesize` its length, as `ZarrParser` keeps the source's data type. A root array
  is its own chunks.
- **netCDF3** (`NetCDF3Parser`): the variables and dimension names of refkv's own output, the
  attributes it presents with a float attribute's values as numbers, and a variable's fill value
  its `_FillValue` in its data type, else zarr-python's default, 0.
- **ENVI** (`ENVIParser`): one array, named by the data file's name without its extension, in the
  file's own axis order (the ENVI convention's transpose codec's order) with no transpose, its axes
  `band`, `y` and `x` in that order, its chunks the convention's (`refkv.build.envi_image`, made
  from the parser's description: no output of refkv's) at the keys of that order, and fill value 0.
  Its attributes are the header fields `ENVIParser` carries through (`description`, `wavelength`,
  `map info`, ...), each named with `_` for its spaces, a braced value a list of its
  items as text, and `data ignore value` as its `_FillValue`, encoded as `FillValueCoder` encodes
  it (a complex number as two Base64 binary64s). The root has no attributes.

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
  where `HDFParser` keeps a list: refkv's own output writes such an attribute as its element
  (hdf5.md §6 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#6-attributes>), and its
  source metadata does not say which it was;
- the datasets refkv does not present (spec/zarrify/hdf5.md §7.4
  <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#74-what-is-not-presented>), such
  as a fixed-length string dataset whose fill value is not one byte repeated;
- an empty fixed-length string attribute is `""`, not `HDFParser`'s `" "`;
- a FITS card with no value is `null`, not the text `"None"`;
- a FITS tile-compressed image is refkv's decoded image (`FITSParser` points the raw table bytes
  at an uncompressed image), its attributes the image header astropy reconstructs;
- a FITS binary table, which `FITSParser` refuses, is refkv's group for the HDU, under the HDU's
  name, as is any other HDU it cannot present;
- a zipped Zarr store's or a Zarr v2 directory's own chunk key encoding, where the
  `ManifestStore` of `ZippedZarrParser` or `ZarrParser` presents the default encoding with a `.`
  separator;
- the Zarr v2 arrays refkv does not present (spec/zarrify/zarr2.md §3.4
  <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#34-arrays-that-are-not-presented>),
  such as one of the `delta` filter, and no `transpose` codec on a 1-D Fortran-order array, where
  `ZarrParser` writes one that keeps the order;
- netCDF3 text that is not UTF-8 is refkv's `{"latin1": T}` (spec/conventions.md §6
  <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#6-source-metadata-as-json>), where
  `NetCDF3Parser` writes replacement characters, and fails on such a name;
- the netCDF3 variables refkv does not present (spec/zarrify/netcdf3.md §2
  <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#2-the-hierarchy>), and a
  variable's `_FillValue` it does not present (§5
  <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#5-attributes>), which
  `NetCDF3Parser` fails to encode or encodes as no reader decodes it, the variable's fill value
  then refkv's;
- an ENVI gzip file's header offset is skipped in the inflated stream (refkv's `refkv.skip`
  codec), as GDAL skips it, where `ENVIParser` starts the data at that offset in the compressed
  bytes; the `gzip` level is refkv's 1, not `ENVIParser`'s 5;
- an ENVI `data ignore value` that numpy does not cast to the data type, on which `ENVIParser`
  fails, is the attribute `data_ignore_value`, its text;
- an ENVI header field that is not UTF-8, on which `ENVIParser` fails, is refkv's `{"latin1": T}`,
  braced or not;
- a netCDF3 variable's integer `_FillValue` that its data type does not hold, on which
  `NetCDF3Parser` fails, leaves the variable refkv's fill value.

`check` finds no problem on its output but `json` at a document that holds a `NaN` or `Infinity`
token (only where a source attribute is not finite and `nonfinite` is `"token"`: zarr-python
writes those tokens, and VirtualiZarr's metadata is written by zarr-python), and `copy` at a
fixed-length string dataset, a Zarr v2 array of bytes or an ASCII table, whose chunks are those of
refkv's own output at the keys of a grid without its trailing axis of bytes, and at a netCDF3
variable whose fill value is `NetCDF3Parser`'s rather than that of refkv's own output.
"""

import base64
import math
import re
import struct
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from refkv.build import (
    Array,
    EnviImage,
    Hdf5Hierarchy,
    Parsed,
    Projected,
    Value,
    Written,
    envi_image,
    filled,
    fits_arrays,
    fits_cards,
    hdf5_hierarchy,
    project,
)
from refkv.zarrify import Rejected
from refkv.zarrify.coding import hdf5_attributes, hdf5_value
from refkv.zarrify.layouts import MIRROR, chunk_coords, chunk_key, dump

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


def _presented(pr: Hdf5Hierarchy, path: str, kind: str | None) -> dict[str, Any]:
    """The attributes HDFParser presents for the node at `path`, `kind` its dataset's datatype
    class (None for a group), as Python values (NaN and the infinities as floats)."""
    metadata = pr.source_metadata(path) or {}
    described = metadata.get("attributes", {}) if isinstance(metadata, dict) else {}
    out: dict[str, Any] = {}
    for name, given in hdf5_attributes(pr, path).items():
        if name in HIDDEN:
            continue
        value = _one(given, (described.get(name) or {}).get("class"))
        if value is None and (described.get(name) or {}).get("class") is not None:
            value = ""  # a null dataspace: h5py's Empty, which HDFParser writes as ""
        if value == "DIMENSION_SCALE":
            continue
        out[name] = _fill_value_coded(value, kind) if name == "_FillValue" and kind in NUMERIC else value
    return out


def _string_length(pr: Hdf5Hierarchy, path: str, doc: Mapping[str, Any]) -> int | None:
    """The length of a fixed-length string dataset the HDF5 convention holds as uint8 with a
    trailing axis of its bytes, or None for another array."""
    metadata = pr.source_metadata(path)
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


def _without_byte_axis(doc: dict[str, Any], chunks: Mapping[str, bytes | Value], name: str, length: int,
                       fill: object) -> tuple[dict[str, Any], dict[str, bytes | Value]]:
    """An array refkv's own output holds as uint8 with a trailing axis of its items' `length` bytes
    as one of the data type `name` (`null_terminated_bytes` or `raw_bytes`) of that length without
    the axis, of fill value `fill`, its chunks at the keys the shorter grid gives them."""
    doc = {**doc, "shape": doc["shape"][:-1],
           "chunk_grid": {"name": "regular", "configuration": {
               "chunk_shape": doc["chunk_grid"]["configuration"]["chunk_shape"][:-1]}},
           "data_type": {"name": name, "configuration": {"length_bytes": length}}, "fill_value": fill}
    if doc.get("dimension_names") is not None:
        doc["dimension_names"] = doc["dimension_names"][:-1]
    before = {**doc, "shape": [*doc["shape"], length]}
    return doc, {chunk_key(chunk_coords(k, before)[:-1], doc): v for k, v in chunks.items()}


def _hdf5(pr: Hdf5Hierarchy, token: bool) -> Written:
    """An HDF5 input as `HDFParser` presents it, from the HDF5 convention's hierarchy, an edge
    chunk the convention copies written as bytes."""
    out = Written()
    for path in pr.nodes():
        prefix = f"{path}/" if path else ""
        if pr.node_type(path) == "group":
            attrs = _nonfinite(_presented(pr, path, None), token)
            out[f"{prefix}zarr.json"] = dump({"zarr_format": 3, "node_type": "group", "attributes": attrs},
                                             allow_nan=token)
            continue
        doc, chunks = pr.array_entries(path)
        metadata = pr.source_metadata(path) or {}
        datatype = metadata.get("datatype") if isinstance(metadata, dict) else None
        kind = datatype.get("class") if isinstance(datatype, dict) else None
        doc = dict(doc)
        length = _string_length(pr, path, doc)
        if length is not None:
            doc, chunks = _without_byte_axis(doc, chunks, "null_terminated_bytes", length,
                                             _bytes_fill(doc.get("fill_value"), length))
            kind = "string"
        if doc.get("dimension_names") is None and doc["shape"] == []:
            doc["dimension_names"] = list[str]()
        doc["codecs"] = [VLEN_UTF8 if c == {"name": "vlen-utf8"} else c for c in doc["codecs"]]
        doc["attributes"] = _nonfinite(_presented(pr, path, kind), token)
        out[f"{prefix}zarr.json"] = dump(doc, allow_nan=token)
        out.update({f"{prefix}{k}": filled(v) for k, v in chunks.items()})
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


# ---- netCDF3

NC_FLOAT_SIZES = {"NC_FLOAT": 4, "NC_DOUBLE": 8}


def _zarr_float(value: float) -> object:
    """A float as a Zarr v3 fill value: a number, or `"NaN"`, `"Infinity"`, `"-Infinity"`."""
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    return value


def _float32(value: float) -> float:
    """`value` rounded to the nearest binary32, as numpy casts it."""
    try:
        return float(struct.unpack("<f", struct.pack("<f", value))[0])
    except OverflowError:  # beyond binary32's range: an infinity, as numpy casts it
        return math.copysign(math.inf, value)


def _netcdf3_attributes(pr: Projected, path: str, token: bool) -> dict[str, Any]:
    """The netCDF3 attributes of the node at `path` as `NetCDF3Parser` presents them: a float
    attribute's float values (netcdf3.md §5
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#5-attributes> writes them as
    hdf5.md's float values) as numbers, NaN and the infinities as `_nonfinite` writes them."""
    metadata = pr.source_metadata(path) or {}
    types = metadata.get("attributes", {}) if isinstance(metadata, dict) else {}
    out: dict[str, Any] = {}
    for name, given in pr.attributes(path).items():
        size = NC_FLOAT_SIZES.get(str((types.get(name) or {}).get("type")))
        floats = size is not None and not (name == "_FillValue" and path)  # a variable's: FillValueCoder's form
        out[name] = _nonfinite(hdf5_value(given, {"class": "float", "size": size}) if floats else given, token)
    return out


def _int_range(data_type: str) -> tuple[int, int]:
    """The least and greatest values of the integer data type `data_type` (`int16`, `uint8`, ...)."""
    bits = int(data_type.lstrip("uint"))
    return (0, 2**bits - 1) if data_type.startswith("u") else (-(2 ** (bits - 1)), 2 ** (bits - 1) - 1)


def _netcdf3_fill(doc: Mapping[str, Any], attributes: Mapping[str, Any], unpresented: Mapping[str, Any]) -> object:
    """The fill value `NetCDF3Parser` gives a variable: its `_FillValue`, in the variable's data
    type, else zarr-python's default (0); refkv's where netcdf3.md §5
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#5-attributes> does not
    present the `_FillValue` (one `NetCDF3Parser` fails to encode, or one on an `NC_CHAR` variable)
    or the variable's data type does not hold it."""
    data_type = doc.get("data_type")
    if not isinstance(data_type, str) or "_FillValue" in unpresented:
        return doc.get("fill_value")
    fill = attributes.get("_FillValue")
    if data_type.startswith("float"):
        if isinstance(fill, str):  # the Base64 of its binary64 (FillValueCoder's)
            value = float(struct.unpack("<d", base64.standard_b64decode(fill))[0])
            return _zarr_float(_float32(value) if data_type == "float32" else value)
        return 0.0
    if not isinstance(fill, int) or isinstance(fill, bool):
        return 0
    # an integer of another type the variable's does not hold, on which NetCDF3Parser fails: refkv's
    return fill if _int_range(data_type)[0] <= fill <= _int_range(data_type)[1] else doc.get("fill_value")


def _netcdf3(pr: Projected, token: bool) -> Written:
    out = Written()
    for path in pr.nodes():
        prefix = f"{path}/" if path else ""
        attributes = _netcdf3_attributes(pr, path, token)
        if pr.node_type(path) == "group":
            out[f"{prefix}zarr.json"] = dump({"zarr_format": 3, "node_type": "group", "attributes": attributes},
                                             allow_nan=token)
            continue
        doc, chunks = pr.array_entries(path)
        metadata = pr.source_metadata(path) or {}
        unpresented = metadata.get("unpresented_attributes", {}) if isinstance(metadata, dict) else {}
        doc = {**doc, "attributes": attributes, "fill_value": _netcdf3_fill(doc, attributes, unpresented)}
        out[f"{prefix}zarr.json"] = dump(doc, allow_nan=token)
        out.update({f"{prefix}{k}": v for k, v in chunks.items()})
    return out


# ---- ENVI

# the header fields ENVIParser carries through as attributes (virtualizarr/parsers/envi.py,
# PASSTHROUGH_ATTR_KEYS), each named with `_` for its spaces
ENVI_PASSTHROUGH = ("description", "sensor type", "acquisition time", "wavelength", "wavelength units", "fwhm", "bbl",
                    "band names", "class names", "class lookup", "classes", "security tag", "map info",
                    "projection info", "coordinate system string", "x start", "y start", "default bands",
                    "default stretch", "z plot titles", "z plot range", "sun azimuth", "sun elevation")
# refkv's axis names (envi.md §4.1
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#41-the-array>), as ENVIParser names them
ENVI_AXES = {"c": "band", "y": "y", "x": "x"}


def _envi_value(raw: object) -> object:
    """A header field's value as ENVIParser gives it: a braced value a list of its comma-separated
    items, stripped, empty ones left out; any other the text. Text that is not UTF-8, refkv's
    `{"latin1": T}`, on which ENVIParser fails, stays that."""
    if not isinstance(raw, str):
        return raw
    text = raw.strip()
    if not text.startswith("{"):
        return raw
    if text.endswith("}"):
        text = text[1:-1]
    items = (item.strip() for item in text.replace("\n", " ").split(","))
    return [item for item in items if item]


def _envi_fill(raw: object, data_type: str) -> object:
    """The `_FillValue` ENVIParser gives `data ignore value` `raw` on an array of `data_type`, as
    xarray's `FillValueCoder` encodes it (a float as the Base64 of its binary64, a complex number as
    two), or None for a value numpy does not cast to it (where ENVIParser fails)."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    try:
        if data_type.startswith("complex"):
            value = complex(text)
            parts = [value.real, value.imag]
            if data_type == "complex64":
                parts = [_float32(p) for p in parts]
            return [base64.standard_b64encode(struct.pack("<d", p)).decode() for p in parts]
        if data_type.startswith("float"):
            value = float(text)
            if data_type == "float32":
                value = _float32(value)
            return base64.standard_b64encode(struct.pack("<d", value)).decode()
        number = int(text)
    except ValueError:
        return None
    low, high = _int_range(data_type)
    return number if low <= number <= high else None


def _envi(image: EnviImage) -> Written:
    """An ENVI raster as ENVIParser presents it, from the ENVI convention's image (`envi_image`): no
    output of refkv's is made."""
    fields: Mapping[str, object] = image.source_metadata().get("fields", {})
    data_file = image.data_key
    stem = data_file.rsplit(".", 1)[0] if "." in data_file.lstrip(".") else data_file
    name = stem if stem and _node_name(stem) else "data"  # ENVIParser's Path(data).stem, else data
    doc = image.document()
    codecs = list(doc["codecs"])
    rank = len(doc["shape"])
    # the file's axis order, outermost first: refkv's transpose codec's order (envi.md §4.1
    # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#41-the-array>), which
    # ENVIParser presents as the array's own
    order = codecs.pop(0)["configuration"]["order"] if codecs[0]["name"] == "transpose" else list(range(rank))
    chunk_shape = doc["chunk_grid"]["configuration"]["chunk_shape"]
    attributes: dict[str, Any] = {key.replace(" ", "_"): _envi_value(fields[key])
                                  for key in ENVI_PASSTHROUGH if key in fields}
    if "data ignore value" in fields:
        fill = _envi_fill(fields["data ignore value"], doc["data_type"])
        if fill is None:  # ENVIParser fails; the text, under a name xarray does not decode
            attributes["data_ignore_value"] = fields["data ignore value"]
        else:
            attributes["_FillValue"] = fill
    data_type = doc["data_type"]
    default: object = 0.0 if data_type.startswith("float") else [0.0, 0.0] if data_type.startswith("complex") else 0
    presented = {
        **doc,
        "shape": [doc["shape"][i] for i in order],
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [chunk_shape[i] for i in order]}},
        "fill_value": default,
        "codecs": codecs,
        "dimension_names": [ENVI_AXES.get(doc["dimension_names"][i], doc["dimension_names"][i]) for i in order],
        "attributes": attributes,
    }
    out = Written({"zarr.json": dump({"zarr_format": 3, "node_type": "group", "attributes": {}}),
                   f"{name}/zarr.json": dump(presented)})
    for coords, value in image.chunks().items():
        out[f"{name}/{chunk_key([coords[i] for i in order], presented)}"] = value
    return out


# ---- Zarr v2

ZARR2_BYTES = re.compile(r"\|(?P<kind>[SV])(?P<length>[1-9][0-9]*)")  # a Zarr v2 dtype of bytes
ZARR2_BYTES_TYPES = {"S": "null_terminated_bytes", "V": "raw_bytes"}


def _zarr2_bytes_fill(fill: object, kind: str, length: int) -> str:
    """The Zarr v3 fill value of a `|S<n>` (`kind` S) or `|V<n>` array of Zarr v2 fill value `fill`
    (the Base64 of its bytes, or null for zero bytes): the Base64 of its `length` bytes, trailing
    NULs dropped for `null_terminated_bytes`, as zarr-python writes them."""
    try:
        raw = base64.standard_b64decode(fill) if isinstance(fill, str) else b""
    except ValueError:
        raw = b""
    raw = raw[:length].ljust(length, b"\0")
    return base64.standard_b64encode(raw.rstrip(b"\0") if kind == "S" else raw).decode()


def _zarr2(pr: Projected, token: bool) -> Written:
    """Each node of refkv's own output with its `.zattrs` as attributes (refkv's source metadata, zarr2.md §4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#4-source-metadata>), an array's
    `_ARRAY_DIMENSIONS` its `dimension_names`, as `ZarrParser` promotes it."""
    out = Written()
    for path in pr.nodes():
        prefix = f"{path}/" if path else ""
        metadata = pr.source_metadata(path) or {}
        given = metadata.get("attributes") if isinstance(metadata, dict) else None
        attributes: dict[str, Any] = dict(given) if isinstance(given, dict) else {}
        if pr.node_type(path) == "group":
            out[f"{prefix}zarr.json"] = dump({"zarr_format": 3, "node_type": "group",
                                              "attributes": _nonfinite(attributes, token)}, allow_nan=token)
            continue
        doc, chunks = pr.array_entries(path)
        doc = dict(doc)
        if not path:  # a root array: its chunks, not the mirror's keys beside them
            chunks = {k: v for k, v in chunks.items() if not k.startswith(f"{MIRROR}/")}
        found = metadata.get("metadata") if isinstance(metadata, dict) else None
        source: Mapping[str, Any] = found if isinstance(found, dict) else {}
        dtype = ZARR2_BYTES.fullmatch(str(source.get("dtype")))
        shape, grid = doc["shape"], doc["chunk_grid"]["configuration"]["chunk_shape"]
        length = int(dtype["length"]) if dtype else 0
        if dtype and doc["data_type"] == "uint8" and shape and shape[-1] == length and grid[-1] == length:
            # zarr2.md §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#3-arrays> holds
            # |S<n> and |V<n> as uint8 with a trailing axis; ZarrParser keeps the source's data type
            doc, chunks = _without_byte_axis(doc, chunks, ZARR2_BYTES_TYPES[dtype["kind"]], length,
                                             _zarr2_bytes_fill(source.get("fill_value"), dtype["kind"], length))
            doc["codecs"] = [{**c, "configuration": {**c["configuration"], "typesize": length}}
                             if c.get("name") == "blosc" and "configuration" in c else c for c in doc["codecs"]]
            # an array in Fortran order starts with a transpose, which took the byte axis, last, with it
            first = doc["codecs"][0] if doc["codecs"] else {}
            order = first.get("configuration", {}).get("order") if first.get("name") == "transpose" else None
            if isinstance(order, list) and order and order[-1] == len(doc["shape"]):
                doc["codecs"] = [{**first, "configuration": {**first["configuration"], "order": order[:-1]}},
                                 *doc["codecs"][1:]]
        names = attributes.get("_ARRAY_DIMENSIONS")
        # names a Zarr v3 array takes (ZarrParser fails on others, which stay an attribute)
        if isinstance(names, list) and len(names) == len(doc["shape"]) and \
                all(n is None or isinstance(n, str) for n in names):
            del attributes["_ARRAY_DIMENSIONS"]
            doc["dimension_names"] = names
        doc["attributes"] = _nonfinite(attributes, token)
        out[f"{prefix}zarr.json"] = dump(doc, allow_nan=token)
        out.update({f"{prefix}{k}": v for k, v in chunks.items()})
    return out


# ---- the layout

class VirtualiZarr:
    name = "refkv.virtualizarr"
    formats: ClassVar[Mapping[str, frozenset[int]]] = {
        "hdf5": frozenset({0}), "fits": frozenset({0}), "zarr3": frozenset({0}), "netcdf3": frozenset({0}),
        "envi": frozenset({0}), "zarr2": frozenset({0})}

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

    def write(self, inputs: Sequence[Parsed], options: Mapping[str, object]) -> Written:
        """The input as VirtualiZarr's parser of its format presents it: a FITS file's from its
        description, its IR and the FITS convention's routines, an HDF5 input's from the HDF5
        convention's hierarchy (`hdf5_hierarchy`), an ENVI raster's from the ENVI convention's image
        (`envi_image`), any other's built on refkv's own output of it."""
        [parsed] = inputs
        if parsed.profile == "fits":
            return _fits(parsed)
        if parsed.profile == "hdf5":
            return _hdf5(hdf5_hierarchy(parsed), options["nonfinite"] == "token")
        if parsed.profile == "envi":
            return _envi(envi_image(parsed))
        pr = project(parsed)
        if parsed.profile == "netcdf3":
            return _netcdf3(pr, options["nonfinite"] == "token")
        if parsed.profile == "zarr2":
            return _zarr2(pr, options["nonfinite"] == "token")
        return pr.entries(declarations=False, mirror=False, datasets=False)
