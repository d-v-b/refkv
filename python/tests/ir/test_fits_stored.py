"""The stored arrays of the FITS fixtures (spec/stored-array.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>), compared with
astropy, the reference reader. Every HDU astropy reads is compared with its descriptions:

- an image: its shape, its sample size, its one unit's place against where astropy finds the
  data, and the unit's bytes against the values astropy reads, unscaled;
- a table: its rows and its heap, each one unit, against the rows' and the heap's bytes as astropy
  holds them;
- a tile-compressed image: its shape, its sample size, its tile grid, its chains and their
  parameters against astropy's reading of the header; each tile's row against the descriptor
  astropy reads of the table, and its unit parameters against the table's columns; and each tile's
  bytes, decoded through its chain, against the values astropy decompresses of that tile. The
  chain is decoded here by each codec's entry of the list: `gzip` by zlib, `fits_rice` by
  imagecodecs, `shuffle` and `fits_quantize` as their entries say.

A second test asserts which cases the fixtures reached. The others craft the cases no fixture
holds: the valid ones in one test, and each fallback in a test of its own."""

import bz2
import gzip
import json
import math
import struct
import warnings
from collections import Counter
from collections.abc import Generator
from contextlib import contextmanager
from fractions import Fraction
from functools import cache
from io import BytesIO
from pathlib import Path
from typing import Any

import imagecodecs
import numpy as np
import pytest
from astropy.io import fits
from astropy.io.fits.hdu.compressed._tiled_compression import _header_to_settings
from described import Described, described
from ir_helpers import FIXTURES

from refkv.ir.stored import Codec, Each, ElementPlace, Grid, Listed, Source, StoredArray, UnitParameter, Value

FILES = sorted(p for p in (FIXTURES / "fits").iterdir() if ".fits" in p.name and "reject" not in p.name)
BLOCK, CARD = 2880, 80
RANDOMS = 10000
# the integer FITS's second dither codes a pixel of exactly 0 as
ZERO_VALUE = -2147483646


def where(d: Described, row: dict) -> tuple[int, int]:
    """A row's unit: its (offset, length) in the bytes its element's extent is a range of."""
    start, length = d.ir.extent(row["element"])
    r = row["range"]
    return (start, length) if r is None else (start + r["offset"], r["length"])


type Astropy = Any
"""an object of astropy's, which is unannotated: a header, a card, an HDU"""


@contextmanager
def quiet() -> Generator[None]:
    """Without astropy's warnings of the odd files (a file cut short, bytes after the last HDU,
    cards it gives defaults for)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        yield


def card_at(d: Described, data: bytes, a: StoredArray, param: object) -> Astropy | None:
    """The card a parameter's place holds, as astropy reads it: 80 bytes of the array's header."""
    if not isinstance(param, Source) or not isinstance(param.place, ElementPlace) or param.place.range is None:
        return None
    offset, length = param.place.range
    if param.place.element != a.element or length != CARD or offset % CARD:
        return None
    start = d.ir.extent(a.element)[0] + offset
    return fits.Card.fromstring(data[start:start + CARD].decode("latin-1"))


@cache
def randoms() -> np.ndarray:
    """The random numbers of FITS's dithers, as `fits_quantize`'s entry gives them."""
    out, s = np.empty(RANDOMS, "f4"), 1
    for k in range(RANDOMS):
        s = 16807 * s % (2**31 - 1)
        out[k] = s / (2**31 - 1)
    return out


def dequantized(ints: np.ndarray, bits: int, method: str, linear: tuple[float, float], seed: int | None,
                zblank: int | None, fused: bool) -> bytes:
    """What `fits_quantize`'s decoding makes of a unit's integers, as its entry says: each
    operation rounded. With `fused`, a dither's multiply and add are rounded once, together, as a
    C compiler may contract them in a build of cfitsio's code: the sum is computed exactly here.
    `linear` is the unit's scale and zero."""
    scale, zero = linear
    values = ints.astype("f8")
    if method == "NO_DITHER":
        values = values * scale + zero
    else:
        assert seed is not None
        table = randoms()
        entry = (seed - 1) % RANDOMS
        at = math.floor(500 * float(table[entry]))
        r = np.empty(len(ints), "f8")
        for k in range(len(ints)):
            r[k] = table[at]
            at += 1
            if at == RANDOMS:
                entry = (entry + 1) % RANDOMS
                at = math.floor(500 * float(table[entry]))
        values = values - r + 0.5
        if fused:
            values = np.array([float(Fraction(float(v)) * Fraction(scale) + Fraction(zero)) for v in values], "f8")
        else:
            values = values * scale + zero
        if method == "SUBTRACTIVE_DITHER_2":
            values[ints == ZERO_VALUE] = 0.0
    if zblank is not None:
        values[ints == zblank] = np.nan
    return values.astype(f">f{bits // 8}").tobytes()


def decoded(d: Described, data: bytes, a: StoredArray, chain: tuple[Codec, ...], row: dict, pixels: int,
            fused: bool = False) -> bytes:
    """A unit's bytes taken through its chain, from the last codec to the first, each decoded as
    its entry of the list says (spec/stored-array.md §3.2
    <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#32-the-list>); `fused`, as
    `dequantized` takes it."""
    start, length = where(d, row)
    b = data[start:start + length]
    assert a.item_bits is not None

    def card_value(codec: Codec, name: str, default: object) -> Astropy:
        c = card_at(d, data, a, codec.parameters.get(name))
        return default if c is None else c.value

    for codec in reversed(chain):
        if codec.name == "gzip":
            b = gzip.decompress(b)
        elif codec.name == "shuffle":
            size = codec.parameters["element_size"]
            assert isinstance(size, Value)
            assert isinstance(size.value, int)
            whole = len(b) // size.value * size.value
            b = np.frombuffer(b[:whole], "u1").reshape(size.value, -1).T.tobytes() + b[whole:]
        elif codec.name == "fits_rice":
            width, block = card_value(codec, "byte_pix", 4), card_value(codec, "block_size", 32)
            values = imagecodecs.rcomp_decode(b, shape=(pixels,), dtype=f"i{width}", nblock=block)
            b = values.astype(f">i{width}").tobytes()
        elif codec.name == "fits_quantize":
            # the blank value: the unit's, or the codec's, a card's
            unit = {k.split(".")[-1]: v for k, v in row.items() if k.startswith("p.fits_quantize.")}
            zblank = card_value(codec, "zblank", None) if "zblank" in codec.parameters else unit.get("zblank")
            method = str(card_value(codec, "method", "NO_DITHER"))
            linear = (unit["scale"], unit["zero"])
            b = dequantized(np.frombuffer(b, ">i4"), a.item_bits, method, linear, unit.get("seed"), zblank, fused)
        else:
            raise AssertionError(codec.name)
    return b


def parameters(row: dict) -> dict[str, object]:
    """The unit parameters a row sets."""
    return {k.split(".")[-1]: v for k, v in row.items() if k.startswith("p.") and v is not None}


def check_whole(d: Described, data: bytes, k: int, want: tuple[tuple[int, ...], int], theirs: tuple[int, bytes],
                what: str, seen: Counter) -> list[str]:
    """An array stored as it is, in one unit, against astropy's: its shape `want[0]` of elements
    of `want[1]` bits, its bytes `theirs[1]` at `theirs[0]`."""
    a = d.stored.arrays[k]
    shape, bits = want
    rows = d.rows.get(k, [])
    axes = tuple(reversed(range(len(shape)))) if len(shape) > 1 else None
    said = []
    if (a.shape, a.axes, a.item_bits) != (shape, axes, bits or None):
        said.append(f"shape {a.shape} along {a.axes} of {a.item_bits} bits, astropy {shape} of {bits}")
    if not bits:
        seen[f"{what} with no grid"] += 1
        return [*said, "a grid, a chain or a row of elements of no size"] if a.grid or a.chains or rows else said
    if a.grid != Grid(tuple(Each(n) if n else Listed(()) for n in shape), "short"):
        said.append(f"grid {a.grid} of {shape}")
    if [d.stored.chains[c] for c in a.chains] != [()] or a.unit_parameters:
        said.append(f"chains {a.chains}, not the one empty chain")
    size = math.prod(shape) * bits // 8
    if size != len(theirs[1]):
        said.append(f"{size} bytes, astropy {len(theirs[1])}")
    if not size:
        seen[f"{what} of no bytes"] += 1
        return [*said, "a row of an array of no bytes"] if rows else said
    if len(rows) != 1:
        return [*said, f"{len(rows)} rows of one unit"]
    (row,) = rows
    if row["coords"] != [0] * len(shape) or row["chain"] is not None or row["skips"] is not None or parameters(row):
        said.append(f"a row at {row['coords']} with a chain, skips or parameters")
    if row["ranges"] is not None or row["member"] is not None or where(d, row) != (theirs[0], size):
        said.append(f"the unit at {where(d, row)}, astropy {(theirs[0], size)}")
    start, length = where(d, row)
    if data[start:start + length] != theirs[1]:
        said.append("the unit's bytes are not the values astropy reads")
    seen[what] += 1
    seen[f"{what} in part of an element"] += row["range"] is not None
    return said


def expected_chain(header: Astropy, names: list[str]) -> tuple[list[str], bool]:
    """The codecs of a tile in COMPRESSED_DATA and whether its values are quantized, by astropy's
    reading of the table's header and columns."""
    quantized = "ZSCALE" in names
    width = 4 if quantized else abs(header["ZBITPIX"]) // 8
    kind = header["ZCMPTYPE"]
    if kind in ("RICE_1", "RICE_ONE"):
        # astropy decodes integers of BYTEPIX bytes, and converts them to the image's type
        fits_elements = _header_to_settings(header)["bytepix"] == width and (quantized or header["ZBITPIX"] > 0)
        if not fits_elements:
            return ["source"], False
        rest = ["fits_rice"]
    else:
        rest = {"GZIP_1": ["gzip"], "GZIP_2": ["shuffle", "gzip"], "NOCOMPRESS": []}[kind]
    return (["fits_quantize", *rest] if quantized else rest), quantized


def check_codecs(d: Described, data: bytes, a: StoredArray, header: Astropy, names: list[str]) -> list[str]:
    """The first chain's codecs and their parameters against astropy's reading of the header."""
    want, quantized = expected_chain(header, names)
    chain = d.stored.chains[a.chains[0]]
    if [c.name for c in chain] != want:
        return [f"chain {[c.name for c in chain]}, astropy's reading {want}"]
    said = []
    width = 4 if quantized else abs(header["ZBITPIX"]) // 8
    settings = _header_to_settings(header)
    method = header.get("ZQUANTIZ", "NO_DITHER")
    listed = [u.name for u in a.unit_parameters]
    for codec in chain:
        cards = {name: card_at(d, data, a, p) for name, p in codec.parameters.items()}
        values = {name: None if c is None else c.value for name, c in cards.items()}
        if codec.name == "source":
            got = {name: p.value for name, p in codec.parameters.items() if isinstance(p, Value)}
            if got != {"format": "fits", "id": header["ZCMPTYPE"]} or len(got) != len(codec.parameters):
                said.append(f"source of {codec.parameters}")
        elif codec.name == "fits_rice":
            # each a ZVALn card, or absent for the default
            got = {"blocksize": values.get("block_size", 32), "bytepix": values.get("byte_pix", 4)}
            if got != settings:
                said.append(f"fits_rice of {values}, astropy {settings}")
            if any(c is None or not c.keyword.startswith("ZVAL") for c in cards.values()):
                said.append(f"fits_rice's parameters are not ZVALn cards: {codec.parameters}")
        elif codec.name == "shuffle":
            if codec.parameters != {"element_size": Value(width)}:
                said.append(f"shuffle of {codec.parameters}, elements of {width} bytes")
        elif codec.name == "fits_quantize":
            blank = header.get("ZBLANK", header.get("BLANK"))
            given = {"method": header.get("ZQUANTIZ"), "zblank": None if "ZBLANK" in names else blank}
            if values != {name: v for name, v in given.items() if v is not None}:
                said.append(f"fits_quantize of {values}, astropy {given}")
            if any(c is None for c in cards.values()):
                said.append(f"fits_quantize's parameters are not cards: {codec.parameters}")
        elif codec.parameters:
            said.append(f"{codec.name} with {codec.parameters}")
    columns = ["scale", "zero"] if quantized else []
    columns += ["seed"] * (quantized and method != "NO_DITHER") + ["zblank"] * (quantized and "ZBLANK" in names)
    if a.unit_parameters != tuple(UnitParameter(0, 0, name) for name in columns) or listed != columns:
        said.append(f"unit parameters {a.unit_parameters}, astropy's columns {columns}")
    return said


def with_default_tiles(data: bytes, start: int) -> bytes | None:
    """The file with the ZTILEn cards the header at `start` lacks, each of its default (FITS
    Standard 4.0 section 10.1.1: ZNAXIS1 for the first axis and 1 for the others), written over
    the blank cards after END; None where the header has no room. astropy 8 reads no
    tile-compressed image without the cards."""
    end = start
    while data[end:end + CARD] != b"END".ljust(CARD):
        end += CARD
    header = fits.Header.fromstring(data[start:end].decode("latin-1"))
    missing = [n for n in range(1, header["ZNAXIS"] + 1) if f"ZTILE{n}" not in header]
    # the blank cards between END and the end of its block
    room = (start - end) % BLOCK // CARD - 1
    if not missing or len(missing) > room:
        return None
    cards = [f"ZTILE{n:<3}= {header['ZNAXIS1'] if n == 1 else 1:>20}".ljust(CARD) for n in missing]
    added = "".join(cards).encode() + b"END".ljust(CARD)
    return data[:end] + added + data[end + len(added):]


def tile_values(image: Astropy | None, at: tuple[slice, ...]) -> bytes | None:
    """The values astropy decompresses of one tile, unscaled, as big-endian bytes; None where it
    reads none."""
    if image is None:
        return None
    try:
        with quiet():
            values = np.asarray(image.section[at])
    except Exception:  # noqa: BLE001  (whatever astropy raises of a tile it does not read)
        return None
    return values.astype(values.dtype.newbyteorder(">")).tobytes()


def check_tiled(d: Described, data: bytes, k: int, table: Astropy, image: Astropy | None, seen: Counter) -> list[str]:
    """A tile-compressed image against astropy's reading of its table (`table`) and of its tiles
    (`image`; None where astropy does not read the image)."""
    a = d.stored.arrays[k]
    header = table.header
    rank = header["ZNAXIS"]
    shape = tuple(header[f"ZNAXIS{n}"] for n in range(rank, 0, -1))
    bits = abs(header["ZBITPIX"])
    said = []
    axes = tuple(reversed(range(rank))) if rank > 1 else None
    if (a.shape, a.axes, a.item_bits) != (shape, axes, bits) or (image is not None and image.shape != shape):
        said.append(f"shape {a.shape} along {a.axes} of {a.item_bits} bits, astropy {shape} of {bits}")
    rows = {tuple(r["coords"]): r for r in d.rows.get(k, [])}
    if a.grid is None:
        seen["tile-compressed images with no grid"] += 1
        return [*said, "chains or rows without a grid"] if a.chains or rows or a.unit_parameters else said
    seen["tile-compressed images"] += 1
    # the tiles, with the defaults of a header that leaves ZTILEn out
    tile = tuple(header.get(f"ZTILE{n}", header["ZNAXIS1"] if n == 1 else 1) for n in range(rank, 0, -1))
    if a.grid != Grid(tuple(Each(t) for t in tile), "short") or (image is not None and tuple(image.tile_shape) != tile):
        return [*said, f"grid {a.grid}, tiles of {tile}"]
    names = [n.upper() for n in table.columns.names]
    said += check_codecs(d, data, a, header, names)
    chains = [d.stored.chains[c] for c in a.chains]
    quantized = chains[0][0].name == "fits_quantize" if chains[0] else False
    seen["quantized images"] += quantized
    fallback = (Codec("gzip", {}),)
    # a tile in GZIP_COMPRESSED_DATA is a gzip stream of its elements: a second chain, unless the first is that
    if chains[1:] != [fallback] * ("GZIP_COMPRESSED_DATA" in names and chains[0] != fallback):
        said.append(f"chains {chains[1:]} after the first, of the columns {names}")
    for codec in chains[0]:
        seen[f"images in {codec.name}"] += 1
    # the table as astropy lays it out: each descriptor a length and an offset in the heap
    raw = table.data.view(np.ndarray)
    heap = table.fileinfo()["datLoc"] + table._theap  # where astropy finds the heap
    counts = tuple(math.ceil(s / t) for s, t in zip(shape, tile, strict=True))
    held = 0
    for r in range(len(raw)):
        coords = tuple(int(c) for c in np.unravel_index(r, counts))
        n, o = (int(x) for x in raw["COMPRESSED_DATA"][r])
        gzipped = n == 0 and "GZIP_COMPRESSED_DATA" in names
        if gzipped:
            n, o = (int(x) for x in raw["GZIP_COMPRESSED_DATA"][r])
        row = rows.get(coords)
        if n == 0 or row is None:
            seen["table rows that hold no tile"] += 1
            if n or row is not None:
                said.append(f"tile {coords}: {'no row' if n else 'a row'}, astropy's descriptor {(n, o)}")
            continue
        held += 1
        seen["tiles"] += 1
        seen["tiles in GZIP_COMPRESSED_DATA"] += gzipped
        seen["tiles that are all of their element"] += row["range"] is None
        if row["ranges"] is not None or row["member"] is not None or where(d, row) != (heap + o, n):
            said.append(f"tile {coords} at {where(d, row)}, astropy {(heap + o, n)}")
            continue
        if "/heap/" not in d.ir.path(row["element"]):
            said.append(f"tile {coords} is in {d.ir.path(row['element'])}, no chunk of the heap")
        chain = 1 if gzipped and len(chains) > 1 else 0
        if row["chain"] != (chain or None) or row["skips"] is not None:
            said.append(f"tile {coords} in chain {row['chain']}, not {chain}")
            continue
        columns = {}
        if quantized and not gzipped:
            columns = {"scale": float(raw["ZSCALE"][r]), "zero": float(raw["ZZERO"][r])}
            if header.get("ZQUANTIZ", "NO_DITHER") != "NO_DITHER":
                columns["seed"] = r + header["ZDITHER0"]
            if "ZBLANK" in names:
                columns["zblank"] = int(raw["ZBLANK"][r])
        if parameters(row) != columns:
            said.append(f"tile {coords} with {parameters(row)}, astropy's columns {columns}")
            continue
        if any(c.name == "source" for c in chains[chain]):
            seen["tiles in a source chain"] += 1
            continue
        at = tuple(slice(c * t, min((c + 1) * t, s)) for c, t, s in zip(coords, tile, shape, strict=True))
        pixels = math.prod(s.stop - s.start for s in at)
        mine = decoded(d, data, a, chains[chain], row, pixels)
        if len(mine) != pixels * bits // 8:
            seen["tiles of another width than their elements'"] += 1
            continue
        theirs = tile_values(image, at)
        if theirs is not None and mine != theirs and quantized and chain == 0:
            # astropy's build may fuse a dither's multiply and add: each pixel is then the one or the other
            once = np.frombuffer(decoded(d, data, a, chains[chain], row, pixels, fused=True), f"V{bits // 8}")
            each, read = np.frombuffer(mine, once.dtype), np.frombuffer(theirs, once.dtype)
            mine = np.where(read == once, once, each).tobytes()
        if theirs is None:
            seen["tiles astropy does not read"] += 1
        elif mine != theirs:
            said.append(f"tile {coords} decodes to other values than astropy reads")
        else:
            seen["tiles decoded"] += 1
            floats = np.frombuffer(mine, f">f{bits // 8}" if header["ZBITPIX"] < 0 else "u1")
            seen["tiles decoded to a NaN"] += bool(np.isnan(floats).any())
    if held != len(rows):
        said.append(f"{len(rows)} rows, {held} tiles astropy's table holds")
    return said


def check_hdu(d: Described, data: bytes, arrays: list[int], table: Astropy, image: Astropy | None,
              seen: Counter) -> list[str]:
    """One HDU's descriptions, `arrays` by their indexes, against astropy's HDU: `table` as
    astropy reads it without decompressing, `image` a tile-compressed image decompressed."""
    header = table.header
    info = table.fileinfo()
    if header["NAXIS"] == 0:
        seen["HDUs of no axes"] += 1
        return [f"{len(arrays)} descriptions of an HDU of no axes"] if arrays else []
    tiled = isinstance(table, fits.BinTableHDU) and header.get("ZIMAGE", False) is True
    is_table = isinstance(table, (fits.BinTableHDU, fits.TableHDU))
    if len(arrays) != (3 if tiled else 2 if is_table else 1):
        return [f"{len(arrays)} descriptions of a {type(table).__name__}"]
    said = []
    for index, k in enumerate(arrays):
        a = d.stored.arrays[k]
        # each is keyed by the header, which is its one declaration: the bytes astropy reads the header from
        start, length = d.ir.extent(a.element)
        if a.index != index or a.declarations != (ElementPlace(a.element, None, None),):
            said.append(f"array {index}: index {a.index}, declarations {a.declarations}")
        if (start, length) != (info["hdrLoc"], info["datLoc"] - info["hdrLoc"]):
            said.append(f"array {index}: its header at {(start, length)}, astropy {info['hdrLoc']} to {info['datLoc']}")
        if a.fill != "undefined" or a.pad_from is not None:
            said.append(f"array {index}: a fill value or pad_from")
    if not is_table:
        seen["images"] += 1
        stored = table.data.astype(table.data.dtype.newbyteorder(">")).tobytes()
        want = (table.shape, abs(header["BITPIX"]))
        if table.data.dtype.itemsize * 8 != want[1] or table.data.shape != want[0]:
            said.append(f"astropy reads {table.data.dtype} of {table.data.shape}")
        return said + check_whole(d, data, arrays[0], want, (info["datLoc"], stored), "image units", seen)
    seen["tile-compressed image HDUs" if tiled else "tables"] += 1
    if tiled:
        said += check_tiled(d, data, arrays[0], table, image, seen)
    try:
        table.data  # noqa: B018  (astropy lays the table out when its data is first read)
    except ValueError:
        # two columns of one name, or a column with none: astropy lays out no such table
        seen["tables astropy does not read"] += 1
        return said
    # the rows, a record each, and the heap: the supplement's bytes from THEAP
    rows, heap = arrays[-2:]
    taken = header["NAXIS1"] * header["NAXIS2"]
    theap = table._theap  # where astropy finds the heap
    # astropy's rows are its columns' bytes, one row after another: where those are not NAXIS1,
    # the standard's rows, readers do not agree on a row, and its description has no grid
    row_bits = 8 * header["NAXIS1"] * (table.data.dtype.itemsize == header["NAXIS1"])
    seen["tables whose columns are not their rows' bytes"] += header["NAXIS1"] > 0 and not row_bits
    stored = table.data.tobytes() if row_bits else b""
    said += check_whole(d, data, rows, ((header["NAXIS2"],), row_bits), (info["datLoc"], stored), "rows units", seen)
    size = header["PCOUNT"] - (theap - taken)
    view = bytes(table.data._get_heap_data())  # the heap as astropy holds it
    # astropy's view is PCOUNT bytes from THEAP, which runs past the data where THEAP leaves a gap
    seen["heaps astropy's view runs past"] += len(view) > size
    said += check_whole(d, data, heap, ((size,), 8), (info["datLoc"] + theap, view[:size]), "heap units", seen)
    return said


@cache
def compared(path: Path) -> tuple[tuple[str, ...], Counter]:
    """What differs between the descriptions of the file at `path` and astropy's reading of it,
    and what the comparison saw."""
    d = described(path)
    said: list[str] = []
    seen: Counter = Counter()
    if d.stored.undescribed:
        said.append("descriptions dropped for room")
    # a compressed file's elements are in its stream's space: their extents are of the decoded file
    stream = d.ir.at("stream")
    data = d.objects[None]
    if stream is not None:
        seen["compressed files"] += 1
        form = json.loads(d.ir.form(stream) or "{}")
        data = {"gunzip": gzip.decompress, "bunzip2": bz2.decompress}[form["transform"]](data)
    prefix = "" if stream is None else "stream/"
    by_hdu: dict[int, list[int]] = {}
    for k, a in enumerate(d.stored.arrays):
        parts = (a.path or "").removeprefix(prefix).split("/")
        if len(parts) != 3 or parts[0] != "hdus" or parts[2] != "header" or d.ir.space(a.element) != (stream or 0):
            said.append(f"a description keyed by {a.path}")
            continue
        by_hdu.setdefault(int(parts[1]), []).append(k)
    for k, rows in d.rows.items():
        if any(d.ir.space(r["element"]) != (stream or 0) for r in rows if r["element"] is not None):
            said.append(f"{d.stored.arrays[k].path}: a unit in another space than its header")
    unscaled = {"do_not_scale_image_data": True}
    with (quiet(), fits.open(path, disable_image_compression=True, **unscaled) as tables,
          fits.open(path, **unscaled) as images):
        given: list[fits.HDUList] = []
        for i, table in enumerate(tables):
            seen["HDUs"] += 1
            image = None
            if table.header.get("ZIMAGE", False) is True:
                try:
                    image = images[i]
                except Exception:  # noqa: BLE001  (whatever astropy raises of an image it does not read)
                    # no ZTILEn cards: astropy reads the image once it is given their defaults
                    defaulted = with_default_tiles(data, table.fileinfo()["hdrLoc"])
                    if defaulted is not None:
                        seen["images astropy reads once given the default ZTILEn"] += 1
                        given.append(fits.open(BytesIO(defaulted), **unscaled))
                        image = given[-1][i]
                if not isinstance(image, fits.CompImageHDU):
                    image = None
                seen["tile-compressed images astropy does not read"] += image is None
            said += [f"HDU {i}: {s}" for s in check_hdu(d, data, by_hdu.pop(i, []), table, image, seen)]
    said += [f"descriptions of HDU {i}, which astropy does not read" for i in by_hdu]
    return tuple(said), seen


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_fits_arrays_are_described_as_astropy_reads_them(path):
    """Every array of the file is described as astropy reads it, and each unit's bytes decode
    through its chain to the values astropy reads."""
    said, seen = compared(path)
    assert said == ()
    assert seen["HDUs"] >= 1


def test_the_fixtures_hold_every_case_the_comparison_reads():
    """The fixtures reach each case of the comparison, so that none of it passes for want of a
    file. The tables astropy does not read are one of two columns of one name and one of a column
    with no name; the rows with no grid are of a table whose columns are 13 bytes of its rows' 14;
    the heap astropy's view runs past is one after a gap, which it takes to be as long as the gap
    and the heap together. No fixture's Rice is `source`, none has a tile that is all of its
    element, and each of its images has a grid: the tests below craft those."""
    seen: Counter = Counter()
    for path in FILES:
        seen += compared(path)[1]
    assert {k: v for k, v in seen.items() if v} == {
        "HDUs": 226,
        "HDUs of no axes": 52,
        "compressed files": 14,
        "images": 130,
        "image units": 127,
        "image units of no bytes": 3,
        "tables": 12,
        "tables astropy does not read": 2,
        "tables whose columns are not their rows' bytes": 1,
        "rows units": 40,
        "rows units of no bytes": 1,
        "rows units with no grid": 1,
        "heap units": 37,
        "heap units of no bytes": 5,
        "heap units in part of an element": 32,
        "heaps astropy's view runs past": 1,
        "tile-compressed image HDUs": 32,
        "tile-compressed images": 32,
        "images astropy reads once given the default ZTILEn": 1,
        "quantized images": 13,
        "images in fits_quantize": 13,
        "images in fits_rice": 28,
        "images in gzip": 3,
        "images in shuffle": 1,
        "tiles": 1473,
        "tiles in GZIP_COMPRESSED_DATA": 1,
        "table rows that hold no tile": 1,
        "tiles decoded": 1473,
        "tiles decoded to a NaN": 8,
    }


# ---- the cases no fixture holds


def cards_of(*cards: tuple[str, object]) -> bytes:
    """A header's blocks: a card for each keyword and value, then END."""
    out = b""
    for keyword, value in cards:
        text = f"'{value:<8}'".ljust(20) if isinstance(value, str) else f"{'T' if value is True else value:>20}"
        out += f"{keyword:<8}= {text}".ljust(CARD).encode()
    out += b"END".ljust(CARD)
    return out.ljust(math.ceil(len(out) / BLOCK) * BLOCK)


def blocks(data: bytes) -> bytes:
    return data.ljust(math.ceil(len(data) / BLOCK) * BLOCK, b"\0")


PRIMARY = cards_of(("SIMPLE", True), ("BITPIX", 8), ("NAXIS", 0))
# a table's heap of no bytes, as its description's index, shape, sample size and grid
EMPTY_HEAP = (1, (0,), 8, Grid((Listed(()),), "short"))


def table_cards(row_bytes: int, rows: int, heap: int, columns: list[tuple[str, str]]) -> list[tuple[str, object]]:
    cards: list[tuple[str, object]] = [("XTENSION", "BINTABLE"), ("BITPIX", 8), ("NAXIS", 2), ("NAXIS1", row_bytes),
                                       ("NAXIS2", rows), ("PCOUNT", heap), ("GCOUNT", 1), ("TFIELDS", len(columns))]
    for n, (name, form) in enumerate(columns, 1):
        cards += [(f"TTYPE{n}", name), (f"TFORM{n}", form)]
    return cards


def recarded(data: bytes, edits: dict[str, object | None], add: tuple[tuple[str, object], ...] = ()) -> bytes:
    """A file with cards of its second HDU's header given other values (None: the card blanked),
    and cards added over the blank ones after END."""
    out = bytearray(data)
    at = BLOCK
    while out[at:at + CARD] != b"END".ljust(CARD):
        keyword = out[at:at + 8].decode().strip()
        if keyword in edits:
            value = edits[keyword]
            out[at:at + CARD] = b" " * CARD if value is None else cards_of((keyword, value))[:CARD]
        at += CARD
    added = cards_of(*add)[:CARD * (len(add) + 1)]
    assert out[at + CARD:at + len(added)].strip() == b"", "blank cards after END"
    out[at:at + len(added)] = added
    return bytes(out)


def written(path: Path, *hdus: Astropy) -> bytes:
    """The bytes of a file of an empty primary HDU and `hdus`, as astropy writes it."""
    with quiet():
        fits.HDUList([fits.PrimaryHDU(), *hdus]).writeto(path, overwrite=True)
    return path.read_bytes()


def noise(shape: tuple[int, ...], dtype: str) -> np.ndarray:
    """Noise, which quantization has a level to work from, or integers."""
    rng = np.random.default_rng(3)
    return (rng.normal(0.0, 100.0, shape) if dtype[0] == "f" else rng.integers(0, 200, shape)).astype(dtype)


def crafted(tmp_path: Path, name: str, data: bytes) -> tuple[Described, tuple[str, ...], Counter]:
    path = tmp_path / name
    path.write_bytes(data)
    return described(path), *compared(path)


def test_cases_no_fixture_holds_are_described_as_astropy_reads_them(tmp_path):
    """The valid cases the fixtures lack, each compared with astropy as the fixtures are: an image
    in the first bytes of data that PCOUNT makes longer; Rice with no ZNAMEn cards, its parameters
    the defaults; a quantized image whose blank value is a BLANK card; quantized images in GZIP_1,
    GZIP_2 and NOCOMPRESS, of 32 and 64 bits; and floats and integers of 8 and 64 bits stored
    whole, in GZIP_1 and GZIP_2; an image of one tile, which is all of its element; and tiles in
    GZIP_COMPRESSED_DATA of an image in GZIP_1 and of a quantized one."""
    plain = written(tmp_path / "plain.fits", fits.ImageHDU(np.arange(12, dtype=">i2").reshape(3, 4)))
    d, said, seen = crafted(tmp_path, "pcount.fits", recarded(plain, {"PCOUNT": 4}))
    assert (said, seen["image units"], seen["image units in part of an element"]) == ((), 1, 1)
    assert d.rows[0][0]["range"] == {"offset": 0, "length": 24}

    one = fits.CompImageHDU(noise((6, 8), "i2"), compression_type="RICE_1", tile_shape=(6, 8))
    d, said, seen = crafted(tmp_path, "one_tile.fits", written(tmp_path / "one.fits", one))
    assert (said, seen["tiles decoded"], seen["tiles that are all of their element"]) == ((), 1, 1)
    assert d.rows[0][0]["range"] is None

    # a tile in GZIP_COMPRESSED_DATA: in the first chain where that is gzip alone, else in a second,
    # with none of a quantized image's unit parameters
    integers, floats = gzip_fallbacks()
    d, said, seen = crafted(tmp_path, "fallback_gzip.fits", integers)
    assert (said, seen["tiles decoded"], seen["tiles in GZIP_COMPRESSED_DATA"]) == ((), 2, 1)
    assert [d.stored.chains[c] for c in d.stored.arrays[0].chains] == [(Codec("gzip", {}),)]
    assert d.rows[0][1]["chain"] is None
    d, said, seen = crafted(tmp_path, "fallback_quantized.fits", floats)
    assert (said, seen["tiles decoded"], seen["tiles in GZIP_COMPRESSED_DATA"]) == ((), 2, 1)
    assert (len(d.stored.arrays[0].chains), d.rows[0][1]["chain"], parameters(d.rows[0][1])) == (2, 1, {})
    assert parameters(d.rows[0][0]) == {"scale": 0.5, "zero": 10.0}

    rice = (FIXTURES / "fits" / "fits_comp_rice_int32.fits").read_bytes()
    blanked = recarded(rice, dict.fromkeys(["ZNAME1", "ZVAL1", "ZNAME2", "ZVAL2"]))
    d, said, seen = crafted(tmp_path, "defaults.fits", blanked)
    assert (said, seen["tiles decoded"]) == ((), 25)
    assert d.stored.chains[d.stored.arrays[0].chains[0]] == (Codec("fits_rice", {}),)

    blank = (FIXTURES / "fits" / "fits_comp_rice_float_nodither.fits").read_bytes()
    d, said, seen = crafted(tmp_path, "blank.fits", recarded(blank, {"ZBLANK": None}, (("BLANK", -2147483648),)))
    assert (said, seen["tiles decoded"], seen["tiles decoded to a NaN"]) == ((), 24, 2)
    a = d.stored.arrays[0]
    zblank = card_at(d, d.objects[None], a, d.stored.chains[a.chains[0]][0].parameters["zblank"])
    assert zblank is not None
    assert zblank.keyword == "BLANK"

    reached: Counter = Counter()
    for kind in ("GZIP_1", "GZIP_2", "NOCOMPRESS"):
        for dtype in ("f4", "f8"):
            how = {"compression_type": kind, "tile_shape": (5, 20), "quantize_level": 16.0, "dither_seed": 7}
            quantized = fits.CompImageHDU(noise((12, 20), dtype), **how)
            d, said, seen = crafted(tmp_path, f"quantized_{kind}_{dtype}.fits", written(tmp_path / "q.fits", quantized))
            names = [c.name for c in d.stored.chains[d.stored.arrays[0].chains[0]]]
            assert (said, names[0], seen["tiles decoded"]) == ((), "fits_quantize", 3), (kind, dtype)
            reached[" ".join(names[1:])] += 1
    assert reached == {"gzip": 2, "shuffle gzip": 2, "": 2}
    for kind in ("GZIP_1", "GZIP_2"):
        for dtype in ("f4", "f8", "u1", "i8"):
            how = {"compression_type": kind, "tile_shape": (5, 20), "quantize_level": 0}
            whole = fits.CompImageHDU(noise((12, 20), dtype), **how)
            d, said, seen = crafted(tmp_path, f"whole_{kind}_{dtype}.fits", written(tmp_path / "w.fits", whole))
            chain = d.stored.chains[d.stored.arrays[0].chains[0]]
            shuffle = (Codec("shuffle", {"element_size": Value(np.dtype(dtype).itemsize)}),) if kind == "GZIP_2" else ()
            assert (said, seen["tiles decoded"]) == ((), 3), (kind, dtype)
            assert chain == (*shuffle, Codec("gzip", {}))


def tiled_file(columns: list[tuple[str, str]], rows: bytes, heap: bytes, row_bytes: int, *, kind: str = "NOCOMPRESS",
               zbitpix: int = 16, tiles: int = 2) -> bytes:
    """A file of an empty primary HDU and an image of `tiles` rows of 4 pixels, of 16-bit integers
    unless `zbitpix` says otherwise, in tiles of a row each (NOCOMPRESS unless `kind` says
    otherwise), its table's rows and heap as given."""
    image = [("ZIMAGE", True), ("ZCMPTYPE", kind), ("ZBITPIX", zbitpix), ("ZNAXIS", 2), ("ZNAXIS1", 4),
             ("ZTILE1", 4), ("ZNAXIS2", tiles), ("ZTILE2", 1)]
    return PRIMARY + cards_of(*table_cards(row_bytes, tiles, len(heap), columns), *image) + blocks(rows + heap)


def gzip_fallbacks() -> tuple[bytes, bytes]:
    """Two images, each of two tiles, the second in GZIP_COMPRESSED_DATA: one of integers in
    GZIP_1, whose first chain is `gzip` alone; and one of floats quantized with no dither, its
    first tile's integers stored as they are."""
    both = [("COMPRESSED_DATA", "1PB"), ("GZIP_COMPRESSED_DATA", "1PB")]
    first, second = (gzip.compress(np.arange(k, k + 4, dtype=">i2").tobytes(), mtime=0) for k in (0, 4))
    rows = struct.pack(">iiii", len(first), 0, 0, 0) + struct.pack(">iiii", 0, 0, len(second), len(first))
    integers = tiled_file(both, rows, first + second, 16, kind="GZIP_1")
    first, second = np.arange(4, dtype=">i4").tobytes(), gzip.compress(np.arange(4, dtype=">f4").tobytes(), mtime=0)
    rows = struct.pack(">iiiidd", len(first), 0, 0, 0, 0.5, 10.0)
    rows += struct.pack(">iiiidd", 0, 0, len(second), len(first), 0.25, 3.0)
    floats = tiled_file([*both, ("ZSCALE", "1D"), ("ZZERO", "1D")], rows, first + second, 32, zbitpix=-32)
    return integers, floats


def ungridded(tmp_path: Path, name: str, data: bytes) -> Described:
    """The file's descriptions, its tile-compressed image's with no grid and what astropy reads
    of the rest as described."""
    d, said, seen = crafted(tmp_path, name, data)
    a = d.stored.arrays[0]
    assert (said, seen["tile-compressed images with no grid"], seen["tile-compressed images"]) == ((), 1, 0), name
    assert (a.index, a.grid, a.chains, a.unit_parameters, d.rows.get(0)) == (0, None, (), (), None), name
    assert a.shape is not None, name
    assert a.item_bits is not None, name
    assert seen["heap units"] == 1, name
    return d


def lossless(tmp_path: Path) -> bytes:
    """A float image astropy writes in GZIP_1 without quantizing it."""
    image = fits.CompImageHDU(noise((6, 8), "f4"), compression_type="GZIP_1", quantize_level=0)
    return written(tmp_path / "lossless.fits", image)


def test_rice_of_other_integers_than_the_elements_is_source(tmp_path):
    """RICE_1 decodes to integers of BYTEPIX bytes. Where those are not the image's elements (16-bit
    elements with a BYTEPIX of 4, quantized ones with a BYTEPIX of 2, floats that are not
    quantized), the first chain is `source`, with no unit parameters, and each tile still has its
    row."""
    fixtures = FIXTURES / "fits"
    cases = {
        "int16": (recarded((fixtures / "fits_comp_rice_int16.fits").read_bytes(), {"ZVAL2": 4}), 30),
        "quantized": (recarded((fixtures / "fits_comp_rice_float_dither1.fits").read_bytes(), {"ZVAL2": 2}), 24),
        "floats": (recarded(lossless(tmp_path), {"ZCMPTYPE": "RICE_1"}), 6),
    }
    for name, (data, tiles) in cases.items():
        d, said, seen = crafted(tmp_path, f"source_{name}.fits", data)
        a = d.stored.arrays[0]
        source = Codec("source", {"format": Value("fits"), "id": Value("RICE_1")})
        assert d.stored.chains[a.chains[0]] == (source,), name
        assert (said, seen["tiles"], seen["tiles in a source chain"]) == ((), tiles, tiles), name
        assert (seen["tiles decoded"], a.unit_parameters) == (0, ()), name
        assert a.grid is not None, name
        assert a.item_bits is not None, name


def test_an_integer_image_with_scale_columns_has_no_grid(tmp_path):
    """An integer image whose table has ZSCALE and ZZERO columns: astropy scales its values, and
    the profile reads them as stored."""
    source = FIXTURES / "fits" / "fits_comp_rice_float_nodither.fits"
    ungridded(tmp_path, "int_scaled.fits", recarded(source.read_bytes(), {"ZBITPIX": 32}))

    def pixels(path: Path) -> np.ndarray:
        with quiet(), fits.open(path) as hdus:
            image: Astropy = hdus[1]
            return np.asarray(image.data)

    # astropy's integers are the quantized image's floats, scaled as it scales those
    assert np.array_equal(pixels(tmp_path / "int_scaled.fits")[0, 1:], pixels(source)[0, 1:].astype("i4"))


def test_a_float_image_with_scale_cards_and_no_columns_has_no_grid(tmp_path):
    """A float image whose header has ZSCALE and ZZERO cards and whose table has no such columns:
    astropy reads its values as stored, and cfitsio takes the cards as every tile's."""
    stored = lossless(tmp_path)
    scale, zero = ("ZSCALE", 2.0), ("ZZERO", 1.0)
    for name, cards in {"both": (scale, zero), "scale": (scale,), "zero": (zero,)}.items():
        ungridded(tmp_path, f"cards_{name}.fits", recarded(stored, {}, cards))


def test_a_blank_value_of_pixels_that_are_not_quantized_has_no_grid(tmp_path):
    """An image that is not quantized with a blank value: astropy makes a stored float equal to a
    ZBLANK or a BLANK card NaN, and rewrites a stored integer equal to its row's ZBLANK; the
    profile reads both as stored."""
    stored = lossless(tmp_path)
    for keyword in ("ZBLANK", "BLANK"):
        ungridded(tmp_path, f"float_{keyword}.fits", recarded(stored, {}, ((keyword, 7),)))
    values = np.arange(8, dtype=">i2").tobytes()
    rows = b"".join(struct.pack(">iii", 8, 8 * k, 7) for k in range(2))
    ungridded(tmp_path, "column.fits", tiled_file([("COMPRESSED_DATA", "1PB"), ("ZBLANK", "1J")], rows, values, 12))
    # the same image without the column has its grid
    rows = b"".join(struct.pack(">ii", 8, 8 * k) for k in range(2))
    _, said, seen = crafted(tmp_path, "no_column.fits", tiled_file([("COMPRESSED_DATA", "1PB")], rows, values, 8))
    assert (said, seen["tiles decoded"]) == ((), 2)


def test_a_quantized_image_whose_blank_card_is_no_integer_has_no_grid(tmp_path):
    """A quantized image with no ZBLANK, in a column or a card, and a BLANK card that is no integer."""
    data = (FIXTURES / "fits" / "fits_comp_rice_float_nodither.fits").read_bytes()
    for name, value in {"string": "none", "real": 7.5}.items():
        ungridded(tmp_path, f"blank_{name}.fits", recarded(data, {"ZBLANK": None}, (("BLANK", value),)))


def test_rows_that_are_not_their_columns_fields_have_no_grid(tmp_path):
    """A binary table whose columns' fields are not all ones the parser reads: its rows have
    neither a sample size nor a grid. (A tile-compressed image's table whose columns are not
    NAXIS1 bytes is rejected: conformance/zarrify/rejects/fits.json's `fits_reject_row_width`.)"""
    # a letter the standard has not: astropy reads no such table
    unknown = PRIMARY + cards_of(*table_cards(4, 3, 0, [("a", "4Z")])) + blocks(bytes(12))
    (tmp_path / "unknown.fits").write_bytes(unknown)
    d = described(tmp_path / "unknown.fits")
    assert [(a.index, a.shape, a.item_bits, a.grid) for a in d.stored.arrays] == [(0, (3,), None, None), EMPTY_HEAP]
    assert d.rows == {}


def test_rows_of_no_bytes_have_no_grid(tmp_path):
    """A table whose NAXIS1 is 0: its rows are elements of no bytes, which no grid holds."""
    (tmp_path / "empty.fits").write_bytes(PRIMARY + cards_of(*table_cards(0, 3, 0, [])))
    d = described(tmp_path / "empty.fits")
    assert [(a.index, a.shape, a.item_bits, a.grid) for a in d.stored.arrays] == [(0, (3,), None, None), EMPTY_HEAP]
    assert d.rows == {}
