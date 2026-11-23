"""The stored array of the ENVI fixtures (spec/stored-array.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>), compared with
GDAL's ENVI driver (through rasterio, in a process of its own: gdal_envi.py), the reference
reader, which reads the header beside the data file and the data. Of each accepted input:

- the array is keyed by the header file's object, and its declarations are the lines of the
  header that write the `data type`, `byte order` and `interleave` GDAL reads;
- its shape is GDAL's bands, lines and samples in the order the interleave GDAL reads stores them,
  and `axes` places each storage axis in the list `samples`, `lines`, `bands`;
- its sample size is that of GDAL's data type, and its grid one unit of the whole array;
- its chain is `gzip` for a file GDAL reads compressed, after `skip` where the header offset GDAL
  reads is above 0, the skip's length at the place the header writes it;
- its row's bytes, decoded through the chain and read in the byte order the declarations give, in
  storage order, are GDAL's values.

A second test asserts which cases the fixtures reached, and a third compares the parser's
description (spec/zarrify/envi.md §8.7
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#87-the-description>) with GDAL's
reading."""

import gzip
from collections import Counter
from pathlib import Path
from typing import cast

import numpy as np
import pytest
from described import Described, described
from gdal_envi import gdal
from ir_helpers import FIXTURES

from refkv.build import read
from refkv.ir import Kind
from refkv.ir.stored import Each, ElementPlace, Source, StoredArray, Value

FILES = sorted(p for p in (FIXTURES / "envi").iterdir() if p.suffix in (".hdr", ".HDR") and "reject" not in p.name)
# the data's axes, outermost first, by interleave, as indexes into (bands, lines, samples)
ORDER = {"bsq": (0, 1, 2), "bil": (1, 0, 2), "bip": (1, 2, 0)}
# the `data type` field's codes (envi.md §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#3-data-types>)
CODES = {1: "u1", 2: "i2", 3: "i4", 4: "f4", 5: "f8", 6: "c8", 9: "c16", 12: "u2", 13: "u4", 14: "i8", 15: "u8"}
# the fields GDAL's ENVI driver reads into the georeferencing, not its `ENVI` metadata domain
GEOREFERENCING = {"map_info", "projection_info", "coordinate_system_string"}
# what the description leaves to the projection (envi.md §8.7
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#87-the-description>)
DECISIONS = {"own", "array", "codecs", "chunks", "chunk_shape", "dimension_names", "fill_value", "endian"}


def decoded(d: Described, a: StoredArray) -> bytes:
    """The row's bytes taken through its chain, from its last codec to its first."""
    (chain,) = a.chains
    (row,) = d.rows[0]
    b = d.unit(row)
    for codec in reversed(d.stored.chains[chain]):
        if codec.name == "gzip":
            b = gzip.decompress(b)
        elif codec.name == "skip":
            length = codec.parameters["length"]
            b = b[cast("int", length.value) if isinstance(length, Value) else int(d.place(length.place)):]
        else:
            raise AssertionError(f"a codec the test does not decode: {codec.name}")
    return b


def problems(path: Path, seen: Counter) -> list[str]:
    d = described(path)
    (a,) = d.stored.arrays
    said = []
    if d.key(a.element) != path.name or d.ir.kind(a.element) != Kind.OBJECT:
        said.append(f"the key is {d.key(a.element)}, not the header file's object")
    data_key = next(k for k in d.objects if k != path.name)
    want, fields = gdal(path.parent / cast("str", data_key))
    # the declarations: the lines that write the fields GDAL reads the element and the layout from
    lines = [d.place(p).decode() for p in a.declarations]
    names = [ln.split("=")[0].strip().lower() for ln in lines]
    expect = [n for n in ("data type", "byte order", "interleave") if n.replace(" ", "_") in fields]
    if names != expect:
        said.append(f"the declarations are the lines of {names}, not {expect}")
    for ln, n in zip(lines, names, strict=False):
        if ln.split("=", 1)[1].strip() != fields[n.replace(" ", "_")]:
            said.append(f"the line {ln!r} is not GDAL's {n} {fields[n.replace(' ', '_')]!r}")
    order = ORDER[fields["interleave"].lower()]
    stored = want.transpose(order)
    if a.shape != stored.shape:
        said.append(f"the shape is {a.shape}, not {stored.shape}")
    # each storage axis's place in (samples, lines, bands)
    axes = tuple(2 - i for i in order)
    if a.axes != axes:
        said.append(f"axes {a.axes} != {axes}")
    if a.item_bits != 8 * want.dtype.itemsize:
        said.append(f"item_bits {a.item_bits} != {8 * want.dtype.itemsize}")
    if a.grid is None or a.grid.axes != tuple(Each(n) for n in stored.shape):
        said.append(f"the grid is {a.grid}, not one unit")
    gzipped = fields.get("file_compression", "0") == "1"
    offset = int(fields.get("header_offset", "0"))
    chain = [c.name for c in d.stored.chains[a.chains[0]]]
    want_chain = (["skip"] if gzipped and offset else []) + (["gzip"] if gzipped else [])
    if chain != want_chain:
        said.append(f"the chain is {chain}, not {want_chain}")
    if chain[:1] == ["skip"]:
        seen["gzip files with a header offset, skipped in the inflated bytes"] += 1
        length = d.stored.chains[a.chains[0]][0].parameters["length"]
        if not (isinstance(length, Source) and isinstance(length.place, ElementPlace)) or int(
                d.place(length.place)) != offset:
            said.append(f"skip's length is {length}, not the header offset's place")
    big = fields.get("byte_order") == "1"
    seen["big-endian files"] += big
    seen[f"{fields['interleave'].lower()} files"] += 1
    mine = np.frombuffer(decoded(d, a), want.dtype.newbyteorder(">" if big else "<"))
    if mine.size != stored.size or not np.array_equal(mine.reshape(stored.shape), stored):
        said.append("the row's bytes are not GDAL's values")
    return said


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_descriptions_are_gdals_arrays(path):
    assert problems(path, Counter()) == []


def test_the_fixtures_reach_every_case():
    seen: Counter = Counter()
    for path in FILES:
        problems(path, seen)
    assert seen == Counter({"bsq files": 7, "bil files": 7, "bip files": 6, "big-endian files": 7,
                            "gzip files with a header offset, skipped in the inflated bytes": 1})


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_an_envi_rasters_description_is_what_gdal_reads(path):
    """`Parsed.description` of an ENVI raster holds the header as GDAL's ENVI driver reads it, and
    none of the output's decisions: its fields are GDAL's `ENVI` metadata domain, its sizes GDAL's
    bands, lines and samples, its `data_type` code GDAL's data type, and its data's range in the data
    file, read in its byte order, inflated and past its header offset for a gzip file, and laid out
    by its interleave, GDAL's values. Its files are the header and the data file beside it, and its
    elements the header file's object, which keys the stored array, and the data file's `data`."""
    with read(path) as parsed:
        d = parsed.description
        assert d is not None
        ir = parsed.ir
        keys = [a.element for a in ir.stored_arrays().arrays]
    assert not DECISIONS & d.keys()
    data = path.parent / d["data_file"]["key"]
    want, fields = gdal(data)
    # GDAL joins a braced value's lines without their line ends, and reads the georeferencing
    # fields into the dataset's transform and CRS instead of its `ENVI` domain
    mine = {k.replace(" ", "_"): v.replace("\n", "") for k, v in d["fields"].items()}
    assert {k: v for k, v in mine.items() if k not in GEOREFERENCING} == fields
    assert (d["bands"], d["lines"], d["samples"]) == want.shape
    assert d["interleave"] == fields["interleave"].lower()
    assert np.dtype(CODES[d["data_type"]]) == want.dtype.newbyteorder("=")
    for name in ("byte_order", "header_offset", "file_compression"):
        assert d[name] == int(fields.get(name, "0")), name
    assert d["header_file"] == {"key": path.name, "size": path.stat().st_size}
    assert d["data_file"]["size"] == data.stat().st_size
    start, length = d["data_range"]
    b = data.read_bytes()[start:start + length]
    if d["file_compression"]:
        b = gzip.decompress(b)[d["header_offset"]:]
    stored = want.transpose(ORDER[d["interleave"]])
    values = np.frombuffer(b, want.dtype.newbyteorder(">" if d["byte_order"] else "<"))
    assert np.array_equal(values.reshape(stored.shape), stored)
    header, unit = d["elements"]["header"], d["elements"]["data"]
    assert (ir.path(header), keys) == (path.name, [header])
    assert ir.path(unit) == f"{data.name}/data"
