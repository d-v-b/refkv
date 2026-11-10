"""The SAFE profile's band arrays against the stored arrays' descriptions (spec/stored-array.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): what the profile
writes of a band file's storage, read off the image's description by the profile's rules
(spec/zarrify/safe.md §4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#4-arrays>
and §8.6 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#86-chunks>), is what the
profile wrote, in the directory form and in the zip form.

An array's description is found by its chunks' bytes: the one whose chunk map's rows hold the
range of the band file its first chunk names. Of that description the test derives

- the array's shape and dimension names: the description's shape, the components first;
- its data type: unsigned, as the SIZ declaration's sign bit says, of the description's `item_bits`;
- its codecs: `imagecodecs_jpeg2k` for the chain's `jpeg2k_tile`, after a `transpose` that puts
  the components the codec makes last back first;
- its chunk shape: the tile size the SIZ declaration gives, `YTsiz` and `XTsiz`, which the
  description's grid holds only cut to the image;
- every chunk, one for each row, at the row's coordinates: a codestream of the row's tile alone,
  the five parts of the profile's `SafeChunk`. Its main header is the bytes of the chain's
  `header` place: SOC and the SIZ segment, written anew as a literal with the tile's own
  geometry, and the rest, a whole data source. Its tile-part is the row's bytes: the SOT segment
  written anew as a literal, for tile 0, and the body, a range of the band file. Its tail is an
  EOC, a literal, after, for a tile the image's edge cuts, the empty tiles that fill the chunk, a
  whole data source: their packets are counted from the layers, the decomposition levels and the
  precinct sizes of the COD and COC segments of the header.

One thing is the profile's reading, and is taken from its output: which band file is which
array, that is the array's path, its resolution group and its band name, which the profile reads
from the tile metadata's geocoding and the file's name, of which no description holds anything.
Every description is some array's.

A second test asserts which cases the fixtures reached, and a third compares the band files of
the stored test's written products, whose cases no fixture holds."""

import json
import math
import struct
from collections import Counter
from functools import cache
from pathlib import Path

import pytest
from test_safe_stored import INPUTS, SIZ, SOC, SOT, Product, Range, lengths, read_product, written_products

from refkv._narrow import JSON, at, integer, mapping, sequence, string
from refkv.ir.store import StoreIrOutput
from refkv.ir.stored import Source
from refkv.zarrify.common import Output

COD, COC, EOC, SOD = b"\xff\x52", b"\xff\x53", b"\xff\xd9", b"\xff\x93"

type Piece = tuple[str, bytes] | Range
"""a part of a chunk: ("literal", bytes); ("shared", bytes), the whole of a data source; or a
range of a file of the input, (key, offset, length), the key None for the zip file"""


def pieces(out: Output | StoreIrOutput, parts: list) -> list[Piece]:
    """A reference's parts, of either form's output."""
    data = dict(enumerate(out.data, 1)) if isinstance(out, StoreIrOutput) else {i: b for b, i in out.data.items()}
    got: list[Piece] = []
    for p in parts:
        if isinstance(p, bytes):
            got.append(("literal", p))
        elif p[0] == "object":
            got.append((p[1], p[2], p[3]))
        elif len(p) == 2 or p[0] == 0:
            got.append((None, p[-2], p[-1]))
        else:
            source, offset, length = p
            assert (offset, length) == (0, len(data[source])), "a data source, whole"
            got.append(("shared", data[source]))
    return got


def coding(rest: bytes, components: int) -> tuple[int, list[tuple[int, list[tuple[int, int]]]], int]:
    """The layers; for each component its decomposition levels N and the precinct size exponents
    (PPx, PPy) of its resolutions 0 to N; and the components with a COC segment of their own:
    from the COD and COC segments of a main header's marker segments after SIZ (ISO/IEC 15444-1
    A.6.1 and A.6.2; spec/zarrify/safe.md §8.3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#83-band-files>)."""

    def precincts(style: int, fields: bytes) -> tuple[int, list[tuple[int, int]]]:
        # the levels, the code-block sizes, style and transform, then a byte a resolution
        levels = fields[0]
        if not style & 1:
            return levels, [(15, 15)] * (levels + 1)
        return levels, [(b & 0x0F, b >> 4) for b in fields[5:6 + levels]]

    position, layers, default, own = 0, 0, None, {}
    while position < len(rest):
        marker = rest[position:position + 2]
        (length,) = struct.unpack_from(">H", rest, position + 2)
        segment = rest[position + 4:position + 2 + length]
        if marker == COD:
            (layers,) = struct.unpack_from(">H", segment, 2)
            default = precincts(segment[0], segment[5:])
        elif marker == COC:
            own[segment[0]] = precincts(segment[1], segment[2:])
        position += 2 + length
    assert default is not None
    return layers, [own.get(c, default) for c in range(components)], len(own)


def empty_packets(layers: int, components: list[tuple[int, list[tuple[int, int]]]], x: tuple[int, int],
                  y: tuple[int, int]) -> int:
    """The packets of a tile of the area `x` by `y`, each [start, end): one for each layer,
    component, resolution and precinct (spec/zarrify/safe.md §8.6
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#86-chunks>)."""

    def across(z: tuple[int, int], d: int, e: int) -> int:
        first, last = math.ceil(z[0] / 2**d), math.ceil(z[1] / 2**d)
        return math.ceil(last / 2**e) - first // 2**e if last > first else 0

    return layers * sum(across(x, levels - r, px) * across(y, levels - r, py)
                        for levels, sizes in components for r, (px, py) in enumerate(sizes))


def derived_chunks(d: Product, k: int, seen: Counter) -> tuple[tuple[int, int], dict[tuple[int, ...], list[Piece]]]:
    """The chunk shape's rows and columns, and every chunk by its coordinates, that the
    description `k` gives by the profile's rules."""
    a = d.stored.arrays[k]
    assert a.shape is not None
    (codec,) = d.stored.chains[a.chains[0]]
    header = codec.parameters["header"]
    assert isinstance(header, Source)
    main = d.held(d.placed(header.place))
    siz = d.held(d.placed(a.declarations[0]))
    assert main[:2] == SOC
    assert main[2:2 + len(siz)] == siz, "the header holds the SIZ segment the declaration names"
    assert siz[:2] == SIZ
    rest = main[2 + len(siz):]
    # the image's and the tiles' sizes, as SIZ declares them
    width, height, _, _, tile_width, tile_height = struct.unpack_from(">6I", siz, 6)
    (components,) = struct.unpack_from(">H", siz, 38)
    layers, coded, own_coding = coding(rest, components)
    seen["arrays of a tile larger than the image"] += (tile_height, tile_width) != lengths(a)[:2]
    listed = coded[0][1][0] != (15, 15)
    seen["arrays of precinct sizes the header gives" if listed else "arrays of the largest precincts"] += 1
    seen["arrays of several layers"] += layers > 1
    seen["arrays of a component coded its own way"] += own_coding > 0
    out: dict[tuple[int, ...], list[Piece]] = {}
    for row in d.rows.get(k, []):
        v, u = row["coords"][:2]
        key, offset, length = d.unit(row)
        sot = d.held((key, offset, 12))
        assert sot[:2] == SOT
        x0, y0 = u * tile_width, v * tile_height
        w, h = min(tile_width, width - x0), min(tile_height, height - y0)
        # the image is the chunk's area, and its tiles are of the file's tile's size, from its place
        own = siz[:4] + struct.pack(">H8I", 0, x0 + tile_width, y0 + tile_height, x0, y0, w, h, x0, y0) + siz[38:]
        tail = b""
        columns, rows = math.ceil(tile_width / w), math.ceil(tile_height / h)
        for t in range(1, columns * rows):
            i, j = t % columns, t // columns
            packets = empty_packets(layers, coded, (x0 + i * w, min(x0 + (i + 1) * w, x0 + tile_width)),
                                    (y0 + j * h, min(y0 + (j + 1) * h, y0 + tile_height)))
            tail += SOT + struct.pack(">HHIBB", 10, t, 14 + packets, 0, 1) + SOD + bytes(packets)
        seen["chunks"] += 1
        seen["chunks of empty tiles" if tail else "chunks of a whole tile"] += 1
        out[tuple([0] * (len(a.shape) - 2) + [v, u])] = [
            ("literal", SOC + own),
            ("shared", rest),
            # tile 0 of the chunk's codestream, of the file's tile-part's length
            ("literal", sot[:4] + struct.pack(">H", 0) + sot[6:]),
            (key, offset + 12, length - 12),
            ("shared", tail + EOC) if tail else ("literal", EOC),
        ]
    return (tile_height, tile_width), out


def check_array(d: Product, k: int, doc: dict[str, JSON], chunks: dict[tuple[int, ...], list[Piece]],
                seen: Counter) -> list[str]:
    """One array's document and its chunks, by their coordinates, against the description `k`."""
    a = d.stored.arrays[k]
    if a.grid is None or a.shape is None or a.item_bits is None:
        return ["a description of no grid"]
    names = [c.name for chain in a.chains for c in d.stored.chains[chain]]
    if names != ["jpeg2k_tile"]:
        return [f"chain {names}, which the profile presents nothing of"]
    samples = list(a.shape[2:])
    seen["arrays of three components" if samples else "arrays of one component"] += 1
    said = []
    shape = [integer(x) for x in sequence(doc["shape"])]
    if shape != [*samples, *a.shape[:2]] or doc["dimension_names"] != [*["c"] * len(samples), "y", "x"]:
        said.append(f"shape {a.shape}; the profile's {shape} along {doc['dimension_names']}")
    # an unsigned sample, of the size the codec makes
    siz = d.held(d.placed(a.declarations[0]))
    assert not siz[40] & 0x80, "SIZ declares unsigned components"
    if doc["data_type"] != f"uint{a.item_bits}":
        said.append(f"item_bits {a.item_bits}; the profile's data type {doc['data_type']}")
    seen[f"arrays of {string(doc['data_type'])}"] += 1
    # the codec makes a pixel's components together, last
    transposed: list[JSON] = [{"name": "transpose", "configuration": {"order": [1, 2, 0]}}] if samples else []
    if doc["codecs"] != [*transposed, {"name": "imagecodecs_jpeg2k"}]:
        said.append(f"chain {names} of shape {a.shape}; the profile's codecs {doc['codecs']}")
    tile, want = derived_chunks(d, k, seen)
    chunk = [integer(x) for x in sequence(at(doc["chunk_grid"], "configuration", "chunk_shape"))]
    if doc["chunk_grid"] != {"name": "regular", "configuration": {"chunk_shape": [*samples, *tile]}}:
        said.append(f"tiles of {tile}, declared; the profile's chunks {chunk}")
    if set(want) != set(chunks):
        said.append(f"chunks at {sorted(set(chunks) - set(want))} alone, and none at {sorted(set(want) - set(chunks))}")
    said += [f"chunk {c} is {chunks[c]}, not {parts}" for c, parts in sorted(want.items())
             if c in chunks and chunks[c] != parts]
    return said


@cache
def compared(path: Path) -> tuple[tuple[str, ...], Counter]:
    """What differs between the arrays the profile wrote of the product at `path` and the
    product's descriptions, and what the comparison saw."""
    d = read_product(path)
    out = d.out
    said: list[str] = []
    seen: Counter = Counter()
    if d.stored.undescribed:
        said.append("descriptions dropped for room")
    seen["products in the directory form" if isinstance(out, StoreIrOutput) else "products in the zip form"] += 1
    entries = out.entries if isinstance(out, StoreIrOutput) else out.bytes_entries
    # each row's bytes in the input, with its description
    units = [(*d.unit(r), k) for k, rows in d.rows.items() for r in rows]
    presented: list[int] = []
    for key, v in sorted(entries.items()):
        if not key.endswith("zarr.json") or key.startswith("refkv_source/"):
            continue
        doc = dict(mapping(json.loads(v), key))
        if doc["node_type"] != "array":
            continue
        prefix = key[:-len("zarr.json")]
        seen["arrays"] += 1
        chunks = {tuple(int(x) for x in chunk_key[len(prefix) + 2:].split("/")): pieces(out, parts)
                  for chunk_key, parts in out.refs.items() if chunk_key.startswith(f"{prefix}c/")}
        # the band file's range that the array's first chunk names
        first = next(p for _, parts in sorted(chunks.items()) for p in parts if len(p) == 3)
        found = [k for file, offset, length, k in units if file == first[0] and offset <= first[1] < offset + length]
        if len(found) != 1:
            said.append(f"{prefix}: its chunks' bytes are in {len(found)} descriptions' rows")
            continue
        presented.append(found[0])
        path_of = d.stored.arrays[found[0]].path
        said += [f"{prefix}, {path_of}: {s}" for s in check_array(d, found[0], doc, chunks, seen)]
    if sorted(presented) != list(range(len(d.stored.arrays))):
        said.append(f"the arrays are of the descriptions {sorted(presented)}, of {len(d.stored.arrays)}")
    return tuple(said), seen


@pytest.mark.parametrize("path", INPUTS, ids=[p.name for p in INPUTS])
def test_the_safe_profile_writes_what_the_descriptions_give(path):
    """Every array the profile writes of the product is what its band file's description gives,
    and every description is an array's."""
    said, seen = compared(path)
    assert said == ()
    assert seen["arrays"] >= 1


def test_the_fixtures_hold_every_case_the_profile_is_compared_in():
    """The fixtures reach each case of the comparison, so that none of it passes for want of a
    file. No fixture's tiles are larger than its image, nor of several layers, nor of precincts
    of the largest size, nor has a component a COC segment: the written cases of the next test
    are those."""
    seen: Counter = Counter()
    for path in INPUTS:
        seen += compared(path)[1]
    assert {k: v for k, v in seen.items() if v} == {
        "products in the directory form": 10,
        "products in the zip form": 4,
        "arrays": 203,
        "arrays of one component": 186,
        "arrays of three components": 17,
        "arrays of uint8": 26,
        "arrays of uint16": 177,
        "arrays of precinct sizes the header gives": 203,
        "chunks": 2628,
        "chunks of a whole tile": 1399,
        "chunks of empty tiles": 1229,
    }


def test_the_profile_writes_what_the_descriptions_give_of_band_files_no_fixture_holds(tmp_path):
    """The band files of the stored test's written products: images that are not square, in
    tiles that are not; tiles declared larger than the image, whose chunks are of the declared
    size; precincts of the largest size; precinct sizes a COC segment gives each component; and
    two layers."""
    seen: Counter = Counter()
    for path in written_products(tmp_path):
        said, saw = compared(path)
        assert said == (), path.name
        seen += saw
    assert {k: v for k, v in seen.items() if v} == {
        "products in the directory form": 2,
        "arrays": 8,
        "arrays of one component": 6,
        "arrays of three components": 2,
        "arrays of uint8": 5,
        "arrays of uint16": 3,
        "arrays of a tile larger than the image": 3,
        "arrays of the largest precincts": 6,
        "arrays of precinct sizes the header gives": 2,
        "arrays of a component coded its own way": 1,
        "arrays of several layers": 1,
        "chunks": 41,
        "chunks of a whole tile": 13,
        "chunks of empty tiles": 28,
    }
