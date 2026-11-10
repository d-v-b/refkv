"""The FITS profile's arrays against the stored arrays' descriptions (spec/stored-array.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): what the profile
writes of an HDU's storage, read off its descriptions by the profile's rules (spec/zarrify/fits.md
§9.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#94-chunks>), is what the
profile wrote. An array is matched to its description by its HDU's number, which its path and the
description's key both give. Of the description the test derives:

- of an image, a table's rows and a table's heap, each one unit: the array's shape, the size of
  its data type, an image's dimension names and `bytes` codec, and every chunk, the run of the
  unit's bytes that C order puts in it, padded with zeros;
- in a compressed file, the one chunk, the whole file, and the codecs that keep the unit's bytes
  of what it decodes to;
- of a tile-compressed image: the shape, the chunk shape that the profile's cut of the tile grid
  gives (spec/zarrify/fits.md §3.3
  <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#33-tile-compressed-images>), the
  `refkv.fits_tiles` codec's configuration, and each chunk: its prefix's record of each tile, from
  the tile's row, and the ranges of its tiles' bytes.

What no description holds, and where the test takes it from:

- the chunk shape the profile cuts a unit stored whole into, and whether a table's rows are a
  `struct` or bytes, with the struct's byte order: from the output. The cut is the projection's
  choice, and a row's fields are the profile's reading of the header;
- of a tile-compressed image, what its header gives and its chains have no codec for: the sign of
  ZBITPIX; BLOCKSIZE and BYTEPIX where the chain is not Rice's; ZQUANTIZ where the image is not
  quantized; and ZDITHER0 where no tile is dithered. Each is read from the description's
  declaration, the header;
- of a tile in GZIP_COMPRESSED_DATA, which is never quantized, and of a table row with no tile:
  the scale, zero and blank value of its record, which the codec does not read, are the table's.
  They are not compared;
- of an image whose one chain is `gzip` alone, which of its two columns a tile is in: both are
  gzip streams of the tile's elements, and the record's kind is either's.

A second test asserts which cases the fixtures reached, and a third records a case in which the
profile does not write what the description gives."""

import bz2
import gzip
import json
import math
import struct
from collections import Counter
from functools import cache
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from astropy.io import fits
from described import Described, described
from ir_helpers import FIXTURES
from test_fits_stored import gzip_fallbacks, recarded, tiled_file, written

from refkv._narrow import JSON, at, integer, mapping, sequence, string
from refkv.ir.stored import Each, ElementPlace, Source, StoredArray, Value
from refkv.zarrify.common import Output

FILES = sorted(p for p in (FIXTURES / "fits").iterdir() if ".fits" in p.name and "reject" not in p.name)
CARD = 80
# the most bytes of a chunk of tiles, decoded, and of a chunk of a heap
LIMIT, BLOB = 2**20, 2**24
PREFIX, RECORD = struct.Struct("<II"), struct.Struct("<IIiIdd")
COMPRESSION: dict[tuple[str, ...], str] = {("fits_rice",): "RICE_1", ("gzip",): "GZIP_1", ("shuffle", "gzip"): "GZIP_2",
                                           (): "NOCOMPRESS"}
BYTES: JSON = {"name": "bytes"}
STREAM = {"gunzip": ({"name": "gzip", "configuration": {"level": 1}}, gzip.decompress),
          "bunzip2": ({"name": "numcodecs.bz2", "configuration": {"level": 1}}, bz2.decompress)}

type Piece = tuple[int, int] | bytes
"""a part of a reference: (offset, length) of the file, or bytes the file does not hold there (a
literal, or a range of a data source)"""
type Astropy = Any
"""an object of astropy's, which is unannotated: a header, a card"""


def pieces(out: Output, parts: list) -> list[Piece]:
    """A reference's parts, those that are no range of the file joined where they meet."""
    data = {i: b for b, i in out.data.items()}
    got: list[Piece] = []
    for p in parts:
        if isinstance(p, bytes):
            piece: Piece = p
        elif len(p) == 2 or p[0] == 0:
            piece = (p[-2], p[-1])
        else:
            piece = data[p[0]][p[1]:p[1] + p[2]]
        last = got[-1] if got else None
        if isinstance(piece, bytes) and isinstance(last, bytes):
            got[-1] = last + piece
        else:
            got.append(piece)
    return got


def where(d: Described, row: dict) -> tuple[int, int]:
    """A row's unit: its (offset, length) in the bytes its element's extent is a range of."""
    start, length = d.ir.extent(row["element"])
    r = row["range"]
    return (start, length) if r is None else (start + r["offset"], r["length"])


def card_at(d: Described, data: bytes, a: StoredArray, param: object) -> Astropy | None:
    """The card a parameter's place holds, 80 bytes of the array's header, as astropy reads it."""
    if not isinstance(param, Source) or not isinstance(param.place, ElementPlace) or param.place.range is None:
        return None
    start = d.ir.extent(a.element)[0] + param.place.range[0]
    return fits.Card.fromstring(data[start:start + CARD].decode("latin-1"))


def setting(header: Astropy, name: str, default: int) -> int:
    """A parameter of the compression as the profile reads it: the ZVALn of the last ZNAMEn of
    `name` (spec/zarrify/fits.md §3.3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#33-tile-compressed-images>)."""
    value = default
    for n in range(1, 10):
        if header.get(f"ZNAME{n}") == name:
            value = header.get(f"ZVAL{n}", default)
    return value


def check_cut(d: Described, rows: list[dict], shape: list[int], item: int, doc: dict[str, JSON],
              chunks: dict[tuple[int, ...], list[Piece]], seen: Counter) -> list[str]:
    """The chunks of a unit stored whole, of `shape` elements of `item` bytes, which the profile
    cuts into its chunk shape: each the run of the unit's bytes that C order puts in it, then zeros
    to the chunk's size."""
    chunk = [integer(n, "chunk_shape") for n in sequence(at(doc, "chunk_grid", "configuration", "chunk_shape"), "c")]
    if not rows:
        seen["arrays of no stored data"] += 1
        return [f"chunks {sorted(chunks)} of no stored data"] if chunks else []
    (row,) = rows
    start, length = where(d, row)
    if length != math.prod(shape) * item:
        return [f"a unit of {length} bytes, {shape} elements of {item}"]
    grid = [math.ceil(n / c) for n, c in zip(shape, chunk, strict=True)]
    said = []
    if set(chunks) != set(np.ndindex(*grid)):
        said.append(f"chunks at {sorted(chunks)[:4]} of a grid of {grid}")
    for coords, parts in sorted(chunks.items()):
        first = [x * c for x, c in zip(coords, chunk, strict=True)]
        held = [min(c, n - o) for c, n, o in zip(chunk, shape, first, strict=True)]
        # the chunk's elements are one run of the unit where every axis past the first it cuts is whole
        cut = next((k for k, (h, n) in enumerate(zip(held, shape, strict=True)) if h != n), len(shape))
        if held[cut + 1:] != shape[cut + 1:]:
            said.append(f"chunk {coords} of {chunk} is no one run of the elements of {shape}")
            continue
        run: Piece = (start + int(np.ravel_multi_index(first, shape)) * item, math.prod(held) * item)
        padding = (math.prod(chunk) - math.prod(held)) * item
        if parts != [run, *[bytes(padding)] * (padding > 0)]:
            said.append(f"chunk {coords} is {parts[:3]}, not {run} and {padding} zeros")
        seen["chunks cut from a unit stored whole"] += 1
        seen["chunks padded with zeros"] += padding > 0
    seen["arrays cut into several chunks"] += len(chunks) > 1
    return said


def check_stream(d: Described, rows: list[dict], doc: dict[str, JSON], chunks: dict[tuple[int, ...], list[Piece]],
                 seen: Counter) -> list[str]:
    """An array of a compressed file: one chunk of the array's shape, the whole file, which the
    codecs after `bytes` decode and keep the unit's bytes of."""
    stream = d.ir.index("stream")
    shape = [integer(n, "shape") for n in sequence(doc["shape"], "shape")]
    codecs = sequence(doc["codecs"], "codecs")
    start, length = where(d, rows[0]) if rows else (0, 0)
    said = []
    if at(doc, "chunk_grid", "configuration", "chunk_shape") != [max(n, 1) for n in shape]:
        said.append(f"chunks of {at(doc, 'chunk_grid')} of an array of {shape}")
    skip: JSON = {"name": "refkv.skip", "configuration": {"length": start, "keep": length}}
    if list(codecs[1:]) != [skip, STREAM[json.loads(d.ir.form(stream) or "{}")["transform"]][0]]:
        said.append(f"codecs {codecs[1:]}, the unit at {(start, length)} of the decoded file")
    whole: dict[tuple[int, ...], list[Piece]] = {(0,) * len(shape): [d.ir.extent(stream)]} if rows else {}
    if chunks != whole:
        said.append(f"chunks {chunks}, not the file {whole}")
    seen["arrays of a compressed file"] += 1
    seen["arrays of a compressed file with no chunk"] += not rows
    return said


def tile_cut(shape: tuple[int, ...], tile: tuple[int, ...], item: int) -> tuple[list[int], int, int]:
    """The profile's cut of a tile grid into chunks of consecutive tiles: the chunk shape, the cut
    axis and the tiles a chunk holds along it."""
    counts = [math.ceil(n / t) for n, t in zip(shape, tile, strict=True)]
    # the bytes of one index along each axis of the tile grid
    spans = [math.prod(tile) * item * math.prod(counts[j + 1:]) for j in range(len(shape))]
    axis = next((j for j, s in enumerate(spans) if s <= LIMIT), len(shape) - 1)
    held = math.ceil(counts[axis] / math.ceil(counts[axis] / max(1, LIMIT // spans[axis])))
    return [*tile[:axis], held * tile[axis], *shape[axis + 1:]], axis, held


def check_tiled(d: Described, data: bytes, k: int, doc: dict[str, JSON], chunks: dict[tuple[int, ...], list[Piece]],
                seen: Counter) -> list[str]:
    """A tile-compressed image's array: its codec's configuration and its chunks, each a prefix of
    its tiles' records and the ranges of their bytes."""
    a = d.stored.arrays[k]
    assert a.shape is not None
    assert a.grid is not None
    assert a.item_bits is not None
    start, length = d.ir.extent(a.element)
    # the declaration: the header, whose cards astropy reads
    header = fits.Header.fromstring(data[start:start + length].decode("latin-1"))
    tile = tuple(x.length for x in a.grid.axes if isinstance(x, Each))
    chains = [d.stored.chains[c] for c in a.chains]
    quantize = chains[0][0] if chains[0] and chains[0][0].name == "fits_quantize" else None
    rest = chains[0][quantize is not None:]
    names = tuple(c.name for c in rest)
    listed = [u.name for u in a.unit_parameters]
    rows = {tuple(r["coords"]): r for r in d.rows.get(k, [])}
    counts = [math.ceil(n / t) for n, t in zip(a.shape, tile, strict=True)]
    said = []
    # the configuration: what the chain and its parameters say, and the header for the rest
    rice = rest[0] if names == ("fits_rice",) else None
    given = {name: card_at(d, data, a, p) for name, p in (rice.parameters.items() if rice else ())}
    block, width = (32 if c is None else c.value for c in (given.get("block_size"), given.get("byte_pix")))
    parameters = {"BLOCKSIZE": setting(header, "BLOCKSIZE", 32), "BYTEPIX": setting(header, "BYTEPIX", 4)}
    if rice and parameters != {"BLOCKSIZE": block, "BYTEPIX": 4 if "byte_pix" not in given else width}:
        said.append(f"fits_rice of {rice.parameters}, the header's {parameters}")
    source = rest[0].parameters.get("id") if names == ("source",) else None
    compression = source.value if isinstance(source, Value) else COMPRESSION.get(names)
    method = card_at(d, data, a, quantize.parameters.get("method")) if quantize else None
    dithers = {r["p.fits_quantize.seed"] - int(np.ravel_multi_index(c, counts)) for c, r in rows.items()
               if "seed" in listed and r["chain"] is None}
    if len(dithers) > 1:
        said.append(f"tiles whose seeds are not their indexes past one ZDITHER0: {sorted(dithers)}")
    dithered = quantize is not None and "seed" in listed
    config: dict[str, Any] = {
        "compression": "RICE_1" if compression == "RICE_ONE" else compression,
        "parameters": parameters,
        "zbitpix": header["ZBITPIX"],
        "shape": list(a.shape),
        "tile_shape": list(tile),
        "quantize": (method.value if method else "NO_DITHER") if quantize else header.get("ZQUANTIZ", "NO_DITHER"),
        "dither_seed": (min(dithers) if dithers else header["ZDITHER0"]) if dithered else 0,
    }
    codecs = [{"name": "refkv.fits_tiles", "configuration": config}]
    if abs(header["ZBITPIX"]) != a.item_bits or doc["codecs"] != codecs:
        said.append(f"codecs {doc['codecs']}, the description's {config}")
    seen[f"tiled arrays in {config['compression']}"] += 1
    seen["tiled arrays quantized"] += quantize is not None
    if 0 in counts:
        seen["tiled arrays of no tiles"] += 1
        return [*said, f"chunks {sorted(chunks)} of no tiles"] if chunks else said
    chunk, axis, held = tile_cut(a.shape, tile, a.item_bits // 8)
    if at(doc, "chunk_grid", "configuration", "chunk_shape") != chunk:
        return [*said, f"chunks of {at(doc, 'chunk_grid')}, the cut of the tile grid gives {chunk}"]
    # each chunk: its tiles, consecutive in storage order; present where one of them has a row
    grid = [*counts[:axis], math.ceil(counts[axis] / held), *[1] * (len(counts) - axis - 1)]
    wanted = set()
    blank = card_at(d, data, a, quantize.parameters.get("zblank")) if quantize else None
    for coords in np.ndindex(*grid):
        along = min(held, counts[axis] - coords[axis] * held)
        origin = (*coords[:axis], coords[axis] * held, *[0] * (len(counts) - axis - 1))
        first = int(np.ravel_multi_index(origin, counts))
        count = along * math.prod(counts[axis + 1:])
        tiles = [rows.get(tuple(int(c) for c in np.unravel_index(r, counts))) for r in range(first, first + count)]
        if not any(tiles):
            seen["chunks of no tile"] += 1
            continue
        wanted.add(coords)
        parts = chunks.get(coords)
        if parts is None or not isinstance(parts[0], bytes):
            said.append(f"chunk {coords}: no prefix")
            continue
        seen["chunks of tiles"] += 1
        prefix = parts[0]
        if len(prefix) != PREFIX.size + count * RECORD.size or PREFIX.unpack_from(prefix) != (count, first):
            said.append(f"chunk {coords}: a prefix of {len(prefix)} bytes, of {count} tiles from {first}")
            continue
        ranges: list[Piece] = []
        for t, row in enumerate(tiles):
            record = RECORD.unpack_from(prefix, PREFIX.size + t * RECORD.size)
            size, kind = record[:2]
            if row is None:
                # a table row with no tile: a record of no stream
                seen["records of no tile"] += 1
                said += [f"chunk {coords}: a record of {size} bytes of a tile with no row"] * (size != 0)
                continue
            place = where(d, row)
            if ranges and not isinstance(ranges[-1], bytes) and sum(ranges[-1]) == place[0]:
                ranges[-1] = (ranges[-1][0], ranges[-1][1] + place[1])
            else:
                ranges.append(place)
            if row["chain"] is not None:
                # a gzip stream of the tile's elements, which the codec reads no scale or blank of
                seen["records of a tile in the second chain"] += 1
                if (size, kind) != (place[1], 1):
                    said.append(f"chunk {coords}: record {t} is {(size, kind)}, not {(place[1], 1)}")
                continue
            unit = {name.split(".")[-1]: v for name, v in row.items() if name.startswith("p.") and v is not None}
            has_blank = "zblank" in unit or blank is not None
            flags = (quantize is not None) | has_blank << 1
            # where the one chain is gzip alone, a tile of either column is in it, and decodes alike
            either = kind == 1 and names == ("gzip",) and len(chains) == 1 and quantize is None
            seen["records of a tile of either column"] += either
            zblank = unit.get("zblank", blank.value if blank else 0)
            want = (place[1], int(either), zblank, flags, unit.get("scale", 0.0), unit.get("zero", 0.0))
            if record != want:
                said.append(f"chunk {coords}: record {t} is {record}, not {want}")
            seen["records of a tile"] += 1
            seen["records of a quantized tile"] += quantize is not None
            seen["records with a blank value"] += has_blank
        # one range where each tile starts where the one before it ends, else a range a tile
        seen["chunks whose tiles are not end to end"] += len(ranges) > 1
        if len(ranges) > 1:
            ranges = [where(d, row) for row in tiles if row is not None]
        if parts[1:] != ranges:
            said.append(f"chunk {coords}: ranges {parts[1:4]}, the tiles' {ranges[:3]}")
    if set(chunks) != wanted:
        said.append(f"chunks at {sorted(set(chunks) ^ wanted)[:4]} alone, or missing")
    return said


def check_array(d: Described, data: bytes, k: int, kind: str, doc: dict[str, JSON],
                chunks: dict[tuple[int, ...], list[Piece]], seen: Counter) -> list[str]:
    """One array the profile wrote, its document and its chunks, against the description `k`:
    `kind`, which of an HDU's arrays it is."""
    a = d.stored.arrays[k]
    tiled = kind == "an image in tiles"
    if (a.unit_parameters or len(a.chains) > 1 or any(d.stored.chains[c] for c in a.chains)) and not tiled:
        return [f"{kind} in the chains {a.chains}"]
    assert a.shape is not None
    shape = [integer(n, "shape") for n in sequence(doc["shape"], "shape")]
    rows = d.rows.get(k, [])
    if a.grid is None or a.item_bits is None:
        # rows that are not their columns' fields, which the profile writes as bytes; a
        # tile-compressed image that readers do not agree on
        seen["arrays of a description with no grid"] += 1
        return [f"shape {a.shape}; the profile's {shape}"] if tuple(shape[:len(a.shape)]) != a.shape else []
    seen["arrays"] += 1
    seen[f"arrays of {kind}"] += 1
    item = a.item_bits // 8
    said = []
    if tiled:
        if tuple(shape) != a.shape or np.dtype(string(doc["data_type"])).itemsize != item:
            said.append(f"shape {a.shape} of {a.item_bits} bits; the profile's {shape} of {doc['data_type']}")
        said += check_tiled(d, data, k, doc, chunks, seen)
    else:
        codec: JSON = {"name": "bytes", "configuration": {"endian": "big"}} if item > 1 else BYTES
        if kind == "rows" and isinstance(doc["data_type"], dict):
            # a struct of the row's fields, which the profile reads of the header: its size is the row's
            seen["rows as a struct"] += 1
            fields = sequence(at(doc["data_type"], "configuration", "fields"), "fields")
            sized, codec = shape == list(a.shape) and struct_size(fields) == item, sequence(doc["codecs"], "codecs")[0]
        elif kind == "rows":
            # bytes, along an axis of their own
            seen["rows as bytes"] += 1
            sized = shape == [*a.shape, item] and doc["data_type"] == "uint8"
            shape, item, codec = [*a.shape, item], 1, BYTES
        else:
            sized = shape == list(a.shape) and np.dtype(string(doc["data_type"])).itemsize == item
        if not sized or sequence(doc["codecs"], "codecs")[0] != codec:
            said.append(f"shape {a.shape} of {a.item_bits} bits; the profile's {shape} of {doc['data_type']}")
        # a heap, which has an array where it has a byte, is cut as bytes: into the fewest chunks of
        # one size of at most 2^24
        cut = [math.ceil(shape[0] / math.ceil(shape[0] / BLOB))] if kind == "a heap" and shape[0] else None
        if cut is not None and at(doc, "chunk_grid", "configuration", "chunk_shape") != cut:
            said.append(f"a heap of {shape} in chunks of {at(doc, 'chunk_grid')}")
        if d.ir.at("stream") is not None:
            said += check_stream(d, rows, doc, chunks, seen)
        elif len(sequence(doc["codecs"], "codecs")) != 1:
            said.append(f"codecs {doc['codecs']} of a unit stored as it is")
        else:
            said += check_cut(d, rows, shape, item, doc, chunks, seen)
    if kind.startswith("an image"):
        # FITS axis n is the storage axis whose place in the header's list is n - 1
        axes = a.axes or tuple(range(len(a.shape)))
        if doc["dimension_names"] != [f"axis{n + 1}" for n in axes]:
            said.append(f"axes {a.axes}; the profile's dimension names {doc['dimension_names']}")
    return said


def struct_size(fields: object) -> int:
    """The bytes of a struct's fields, as the profile writes a table's row."""
    total = 0
    for f in sequence(fields, "fields"):
        t = at(f, "data_type")
        if isinstance(t, str):
            total += np.dtype(t).itemsize
        elif at(t, "name") == "struct":
            total += struct_size(at(t, "configuration", "fields"))
        else:
            total += integer(at(t, "configuration", "length_bytes"), "length_bytes")
    return total


@cache
def compared(path: Path) -> tuple[tuple[str, ...], Counter]:
    """What differs between the arrays the profile wrote of the file at `path` and the file's
    descriptions, and what the comparison saw."""
    d = described(path)
    out = d.out
    assert isinstance(out, Output), "a FITS file is one file"
    said: list[str] = []
    seen: Counter = Counter()
    if d.stored.undescribed:
        said.append("descriptions dropped for room")
    stream = d.ir.at("stream")
    data = d.objects[None]
    if stream is not None:
        data = STREAM[json.loads(d.ir.form(stream) or "{}")["transform"]][1](data)
    # a description by its HDU's number and its index
    keyed = {(int((a.path or "").split("/")[-2]), a.index): k for k, a in enumerate(d.stored.arrays)}
    presented: set[int] = set()
    for key, v in sorted(out.bytes_entries.items()):
        if not key.endswith("zarr.json") or key.startswith("refkv_source/"):
            continue
        doc = dict(mapping(json.loads(v), key))
        if doc["node_type"] != "array":
            continue
        prefix = key[:-len("zarr.json")]
        # HDU i's image is the node i; a table's rows and heap are its children
        hdu, _, name = prefix.rstrip("/").partition("/")
        index, kind = {"": (0, "an image"), "rows": (0, "rows"), "heap": (1, "a heap")}[name]
        # a tile-compressed image's HDU has three descriptions, the image's first
        kind += " in tiles" * (not name and (int(hdu), 2) in keyed)
        k = keyed.get((int(hdu), index))
        if k is None:
            said.append(f"{prefix}: no description {index} of HDU {hdu}")
            continue
        presented.add(k)
        chunks: dict[tuple[int, ...], list[Piece]] = {}
        for chunk_key, parts in out.refs.items():
            if chunk_key.startswith(f"{prefix}c/"):
                chunks[tuple(int(x) for x in chunk_key[len(prefix) + 2:].split("/"))] = pieces(out, parts)
        if any(c.startswith(f"{prefix}c/") for c in out.bytes_entries):
            said.append(f"{prefix}: a chunk the profile copied")
        said += [f"{prefix}: {s}" for s in check_array(d, data, k, kind, doc, chunks, seen)]
    seen["descriptions the profile writes no array of"] += len(d.stored.arrays) - len(presented)
    return tuple(said), seen


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_the_fits_profile_writes_what_the_descriptions_give(path):
    """Every array the profile writes of the file is what its description gives."""
    said, _ = compared(path)
    assert said == ()


def test_the_fixtures_hold_every_case_the_profile_is_compared_in():
    """The fixtures reach each case of the comparison, so that none of it passes for want of a
    file. The descriptions with no array are the rows and the heap of each tile-compressed image's
    table, and the heaps of no bytes; the array of a description with no grid is the rows of a
    table whose columns are 13 bytes of its rows' 14."""
    seen: Counter = Counter()
    for path in FILES:
        seen += compared(path)[1]
    assert {k: v for k, v in seen.items() if v} == {
        "arrays": 178,
        "arrays of an image": 130,
        "arrays of rows": 11,
        "arrays of a heap": 5,
        "arrays of an image in tiles": 32,
        "arrays of a description with no grid": 1,
        "arrays of no stored data": 2,
        "arrays of a compressed file": 36,
        "arrays of a compressed file with no chunk": 2,
        "arrays cut into several chunks": 3,
        "rows as a struct": 9,
        "rows as bytes": 2,
        "chunks cut from a unit stored whole": 111,
        "chunks padded with zeros": 1,
        "tiled arrays in RICE_1": 28,
        "tiled arrays in GZIP_1": 2,
        "tiled arrays in GZIP_2": 1,
        "tiled arrays in NOCOMPRESS": 1,
        "tiled arrays quantized": 13,
        "chunks of tiles": 34,
        "chunks whose tiles are not end to end": 1,
        "records of a tile": 1472,
        "records of a quantized tile": 158,
        "records with a blank value": 78,
        "records of a tile in the second chain": 1,
        "records of no tile": 1,
        "descriptions the profile writes no array of": 71,
    }


def test_cases_no_fixture_holds_are_written_as_their_descriptions_give(tmp_path):
    """The cases the fixtures lack: tiles each over the chunk limit, which the profile cuts along
    the fastest axis; Rice the header names RICE_ONE, in a `source` chain; tiles of which some start
    where the one before ends and one does not, each then a range of its own; and tiles in
    GZIP_COMPRESSED_DATA, of a quantized image and of one whose first chain is gzip alone."""
    def reached(name: str, data: bytes) -> Counter:
        (tmp_path / name).write_bytes(data)
        said, seen = compared(tmp_path / name)
        assert said == (), name
        return seen

    large = fits.CompImageHDU(np.zeros((1100, 2000), "u1"), compression_type="RICE_1", tile_shape=(1100, 1000))
    seen = reached("large.fits", written(tmp_path / "written.fits", large))
    assert (seen["chunks of tiles"], seen["records of a tile"]) == (2, 2)

    rice_one = (FIXTURES / "fits" / "fits_comp_rice_one.fits").read_bytes()
    seen = reached("rice_one.fits", recarded(rice_one, {"ZVAL2": 4}))
    assert (seen["tiled arrays in RICE_1"], seen["records of a tile"]) == (1, 16)
    d = described(tmp_path / "rice_one.fits")
    assert [c.name for c in d.stored.chains[d.stored.arrays[0].chains[0]]] == ["source"]

    # three tiles, the third after a gap of 4 bytes
    rows = b"".join(struct.pack(">ii", 8, at) for at in (0, 8, 20))
    apart = tiled_file([("COMPRESSED_DATA", "1PB")], rows, bytes(28), 8, tiles=3)
    seen = reached("apart.fits", apart)
    assert (seen["chunks whose tiles are not end to end"], seen["records of a tile"]) == (1, 3)

    integers, floats = gzip_fallbacks()
    seen = reached("fallback_gzip.fits", integers)
    assert (seen["records of a tile"], seen["records of a tile of either column"]) == (2, 1)
    seen = reached("fallback_quantized.fits", floats)
    assert (seen["records of a quantized tile"], seen["records of a tile in the second chain"]) == (1, 1)


def test_the_profile_reads_no_blank_value_of_a_blank_card(tmp_path):
    """A finding, recorded: a quantized image whose blank value is its header's BLANK card, with
    no ZBLANK. astropy reads the card (test_fits_stored) and the description gives its place; the
    profile reads ZBLANK alone, so each tile's record says the image has no blank value, and its
    pixels of that integer decode to numbers where astropy reads NaN."""
    source = (FIXTURES / "fits" / "fits_comp_rice_float_nodither.fits").read_bytes()
    path = tmp_path / "blank.fits"
    path.write_bytes(recarded(source, {"ZBLANK": None}, (("BLANK", -2147483648),)))
    said, seen = compared(path)
    assert seen["records of a tile"] == 24
    # every record's blank value and flags: 0 and "scaled"; the description gives the card's value, and "blank" too
    assert len(said) == 24
    assert said[0].endswith("record 0 is (151, 0, 0, 1, 0.01, 100.0), not (151, 0, -2147483648, 3, 0.01, 100.0)")
    assert all(", 0, 0, 1, " in s and ", 0, -2147483648, 3, " in s for s in said)
