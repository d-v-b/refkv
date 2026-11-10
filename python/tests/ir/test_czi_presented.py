"""The CZI profile's arrays against the stored arrays' descriptions (spec/stored-array.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): what the profile
writes of each subblock's storage, read off the subblock's description and its declarations by the
profile's rules (spec/zarrify/czi.md §3.1
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#31-pixel-types-and-codecs>, §4.3
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#43-image-arrays>, §4.4
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#44-tiles> and §7.5
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#75-chunk-references>), is what the
profile wrote.

A subblock is one description; an image's level and a tile array are the profile's assembly of
several. So one thing is taken from the output: which subblocks each array holds. A chunk's
subblock is found by the chunk's bytes, the description whose unit holds the range the chunk
names. Of the descriptions an array holds, the test then derives all the rest:

- from each one's declarations, its copy of its entry's dimension entries: its plane (the Start of
  `T`, `C` and `Z`), its series key (those of `S`, `B`, `H`, `I`, `R` and `V`), and its place and
  logical size (the Start and Size of `X` and `Y`); and so the image's axes and its least planes,
  a level's lattice (its origin, its columns and rows, the sizes of its last ones), each chunk's
  key, the array's shape and its chunk grid, and a tile array's place in its source metadata;
- from each one's `shape` and `item_bits`: the samples of the channel axis, the size of the data
  type, and the stored size the lattice is made of;
- from each one's chain: the array's codecs, which every subblock of the array gives alike, and
  the chunk's range, which is the unit, or, of a `czi_zstd1` unit, the frame after its header.

Three things the test reads from a unit's first bytes, as the profile does (spec/zarrify/czi.md
§7.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#74-codec-headers>), since a
description stops at the chain: whether a JPEG stream is lossless, which chooses between
`imagecodecs_jpeg` and `imagecodecs_ljpeg`; a Zstd1 header's length and its hi-lo flag; and the
size of a JPEG or JPEG XR frame, which is a tile array's where it is not the entry's stored size.
Two are not derived: a tile array's `copy`, taken from the output, and the data type's name, of
which the size alone is compared. No fixture has a tile of more than 2^24 bytes, so no array is
in row bands, which the test would refuse.

A second test asserts which cases the fixtures reached, the descriptions the profile writes
nothing of among them."""

import json
import struct
from collections import Counter
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import numpy as np
import pytest
from described import Described, described
from test_czi_stored import FILES, jpeg_frame, jpegxr_format, writer, written, zstd1_header

from refkv._narrow import JSON, at, mapping, string
from refkv.ir.stored import Value
from refkv.zarrify.common import Output

SERIES = "SBHIRV"
# the most bytes of an uncompressed chunk, past which the profile cuts a tile into row bands
MAX_BAND = 1 << 24
ZSTD: JSON = {"name": "zstd", "configuration": {"level": 0, "checksum": False}}
SHUFFLE: JSON = {"name": "numcodecs.shuffle", "configuration": {"elementsize": 2}}

type Range = tuple[str | None, int, int]
"""(object key, None for a file input's one file; offset; length)"""


@dataclass(frozen=True)
class Subblock:
    """A description the profile presents, with what its declarations and its unit's first bytes
    say."""

    k: int
    unit: Range
    # (Start, Size, StoredSize) of each dimension entry, by its letter
    dims: dict[str, tuple[int, int, int]]
    samples: int
    item: int
    names: tuple[str, ...]
    # the SOF marker and the size of a JPEG stream's frame, and the size of a JPEG XR file's
    lossless: bool
    coded: tuple[int, int]
    # a Zstd1 header's length and hi-lo flag
    header: tuple[int, bool]

    def start(self, letter: str) -> int:
        return self.dims[letter][0] if letter in self.dims else 0

    @property
    def plane(self) -> tuple[int, int, int]:
        return self.start("T"), self.start("C"), self.start("Z")

    @property
    def series(self) -> dict[str, int]:
        return {x: self.dims[x][0] for x in SERIES if x in self.dims}

    @property
    def stored(self) -> tuple[int, int]:
        return self.dims["X"][2], self.dims["Y"][2]

    @property
    def logical(self) -> tuple[int, int]:
        return self.dims["X"][1], self.dims["Y"][1]


def jpegxr_size(b: bytes) -> tuple[int, int]:
    """(ImageWidth, ImageHeight) of a JPEG XR file's container."""
    ifd = struct.unpack_from("<I", b, 4)[0]
    found = {}
    for n in range(struct.unpack_from("<H", b, ifd)[0]):
        tag, kind, _, value = struct.unpack_from("<HHII", b, ifd + 2 + 12 * n)
        found.setdefault(tag, value & 0xFFFF if kind == 3 else value)
    return found[0xBC80], found[0xBC81]


def subblock(d: Described, k: int) -> Subblock:
    """The description `k`, which has a grid, as the profile reads it."""
    a = d.stored.arrays[k]
    assert a.shape is not None
    assert a.item_bits is not None
    (row,) = d.rows[k]
    (unit,) = d.parts(row)
    entries = d.place(a.declarations[1])
    dims = {}
    for n in range(len(entries) // 20):
        letter, start, size, _, stored = struct.unpack_from("<4siifi", entries, 20 * n)
        dims[letter.rstrip(b"\0").decode()] = (start, size, stored)
    assert (dims["Y"][2], dims["X"][2]) == a.shape[:2], "the shape is the entry's stored size"
    (codec,) = d.stored.chains[a.chains[0]] or (None,)
    names = () if codec is None else (codec.name,)
    head = d.unit(row)[:1 << 16]
    lossless, coded, header = False, (a.shape[1], a.shape[0]), (0, False)
    if codec is not None and codec.name in ("jpeg", "source"):
        frame = jpeg_frame(head)
        assert frame is not None
        lossless, coded = frame.marker == 0xC3, (frame.width, frame.height)
        if codec.name == "source":
            # the source's own encoding: CZI's JpgFile or lossless JPEG, of another precision than 8
            given = {n: v.value for n, v in codec.parameters.items() if isinstance(v, Value)}
            assert given["format"] == "czi"
            assert given["id"] in (1, 3)
            names = (f"source {given['id']}",)
    elif codec is not None and codec.name == "jpegxr":
        assert jpegxr_format(head) is not None
        coded = jpegxr_size(head)
    elif codec is not None and codec.name == "czi_zstd1":
        found = zstd1_header(head)
        assert found is not None
        header = found
    samples = a.shape[2] if len(a.shape) == 3 else 1
    return Subblock(k, unit, dims, samples, a.item_bits // 8, names, lossless, coded, header)


def codecs_of(s: Subblock, axes: list[str]) -> list[JSON]:
    """The codecs of an array of the subblock's codec form (spec/zarrify/czi.md §3.1
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#31-pixel-types-and-codecs>):
    `transpose` for samples, which a chunk holds last, then the chain's."""
    order: list[JSON] = [*(i for i, x in enumerate(axes) if x != "c"), axes.index("c")] if s.samples > 1 else []
    out: list[JSON] = [{"name": "transpose", "configuration": {"order": order}}] if s.samples > 1 else []
    little: JSON = {"name": "bytes"} if s.item == 1 else {"name": "bytes", "configuration": {"endian": "little"}}
    jpeg: JSON = {"name": "imagecodecs_ljpeg" if s.lossless else "imagecodecs_jpeg"}
    chain: dict[tuple[str, ...], list[JSON]] = {
        (): [little],
        ("zstd",): [little, ZSTD],
        ("czi_zstd1",): [little, *([SHUFFLE] if s.header[1] else []), ZSTD],
        ("tiff_lzw",): [little, {"name": "imagecodecs_lzw"}],
        ("jpegxr",): [{"name": "imagecodecs_jpegxr"}],
        ("jpeg",): [jpeg],
        ("source 1",): [jpeg],
        ("source 3",): [{"name": "imagecodecs_ljpeg"}],
    }
    return [*out, *chain[s.names]]


def chunk_of(s: Subblock) -> Range:
    """The range of a subblock's chunk (spec/zarrify/czi.md §7.5
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#75-chunk-references>): its
    unit, or, of a Zstd1 subblock, the frame after its header."""
    key, offset, length = s.unit
    return key, offset + s.header[0], length - s.header[0]


def source_range(part: object) -> Range:
    """The range of a file that a chunk's one part names: (offset, length), or source 0 and
    them, of a file input's file; ("object", key, offset, length) of a file of a store."""
    assert isinstance(part, tuple), "a range of a source"
    *where, offset, length = part
    assert isinstance(offset, int)
    assert isinstance(length, int)
    if where[:1] == ["object"]:
        assert isinstance(where[1], str)
        return where[1], offset, length
    assert where in ([], [0]), "source 0, the file"
    return None, offset, length


def lengths(full: int, last: int, count: int) -> JSON:
    """An axis's chunk lengths: `count` units of `full`, the last of `last`."""
    return full if last == full else [[full, count - 1], last]


def grid_of(chunks: list[JSON]) -> JSON:
    if all(isinstance(n, int) for n in chunks):
        return {"name": "regular", "configuration": {"chunk_shape": chunks}}
    return {"name": "rectilinear", "configuration": {"kind": "inline", "chunk_shapes": chunks}}


def along(axes: list[str], values: dict[str, JSON]) -> list[JSON]:
    return [values[x] for x in axes]


def check_array(prefix: str, doc: dict[str, JSON], members: list[Subblock], lo: tuple[int, int, int], axes: list[str],
                got: dict[str, Range], seen: Counter) -> list[str]:
    """One array the profile wrote, a level of an image or a tile array, against the subblocks it
    holds: `lo` and `axes` are its image's, or its own."""
    first = members[0]
    said = []
    if doc["dimension_names"] != axes:
        said.append(f"axes {axes}; the profile's {doc['dimension_names']}")
    if any(codecs_of(s, axes) != doc["codecs"] for s in members):
        said.append(f"codecs {codecs_of(first, axes)} of chain {first.names}; the profile's {doc['codecs']}")
    if np.dtype(string(doc["data_type"])).itemsize != first.item:
        said.append(f"samples of {first.item} bytes; the profile's data type {doc['data_type']}")
    if any(s.names == () and s.stored[0] * s.stored[1] * s.samples * s.item > MAX_BAND for s in members):
        return [*said, "a tile in row bands, which the test does not derive"]
    planes = [s.plane for s in members]
    extent = [max(p[n] for p in planes) - lo[n] + 1 for n in range(3)]
    if prefix.startswith("tiles/"):
        # one position: every member at one place, of one size and form
        seen["tile arrays"] += 1
        seen["tile arrays of a frame of another size"] += first.coded != first.stored
        column = row = {s.k: 0 for s in members}
        (wide, high), (last_w, last_h), (m, r) = first.coded, first.coded, (1, 1)
        own = mapping(at(doc["attributes"], "refkv_zarrified", "czi"))
        size_of: JSON = [first.logical[0], first.logical[1]]
        stored: JSON = [first.stored[0], first.stored[1]]
        least: JSON = {"t": lo[0], "c": lo[1], "z": lo[2]}
        series: JSON = dict(first.series.items())
        want_own: dict[str, JSON] = {"x": first.start("X"), "y": first.start("Y"), "size": size_of,
                                     "stored_size": stored, "planes": least, "copy": own["copy"]}
        if first.series:
            want_own["dimensions"] = series
        same = {(s.start("X"), s.start("Y"), s.logical, s.stored, s.coded, s.names, tuple(s.series.items()))
                for s in members}
        if dict(own) != want_own or len(same) != 1:
            said.append(f"a tile array of {want_own}; the profile's {dict(own)}")
    else:
        # a regular level: equal tiles on a lattice, the last column and row smaller
        seen["levels"] += 1
        wide, high = max(s.stored[0] for s in members), max(s.stored[1] for s in members)
        logical = max(s.logical[0] for s in members), max(s.logical[1] for s in members)
        x0, y0 = min(s.start("X") for s in members), min(s.start("Y") for s in members)
        column = {s.k: (s.start("X") - x0) // logical[0] for s in members}
        row = {s.k: (s.start("Y") - y0) // logical[1] for s in members}
        m, r = max(column.values()) + 1, max(row.values()) + 1
        last_w = {s.stored[0] for s in members if column[s.k] == m - 1}.pop()
        last_h = {s.stored[1] for s in members if row[s.k] == r - 1}.pop()
        seen["levels clipped"] += (last_w, last_h) != (wide, high)
        seen["levels of several tiles"] += m * r > 1
        if any(s.coded != s.stored for s in members):
            said.append("a level of a subblock whose frame is not of its stored size")
    size: dict[str, JSON] = {"t": extent[0], "c": extent[1] * first.samples, "z": extent[2],
                             "y": (r - 1) * high + last_h, "x": (m - 1) * wide + last_w}
    cut: dict[str, JSON] = {"t": 1, "c": first.samples, "z": 1,
                            "y": lengths(high, last_h, r), "x": lengths(wide, last_w, m)}
    if doc["shape"] != along(axes, size) or doc["chunk_grid"] != grid_of(along(axes, cut)):
        said.append(f"shape {along(axes, size)} in chunks of {along(axes, cut)}; the profile's {doc['shape']} "
                    f"in {doc['chunk_grid']}")
    want = {}
    for s in members:
        t, c, z = (s.plane[n] - lo[n] for n in range(3))
        coords = along(axes, {"t": t, "c": c, "z": z, "y": row[s.k], "x": column[s.k]})
        want[prefix + "c/" + "/".join(str(n) for n in coords)] = chunk_of(s)
        seen[f"chunks of {list(s.names)}"] += 1
    if want != got:
        differ = sorted(k for k in want.keys() | got.keys() if want.get(k) != got.get(k))
        theirs, ours = [got.get(k) for k in differ[:4]], [want.get(k) for k in differ[:4]]
        said.append(f"chunks {differ[:4]} are {theirs}, not {ours}")
    return said


def axes_of(members: list[Subblock]) -> tuple[list[str], tuple[int, int, int]]:
    """The axes of an array over its subblocks' planes, and the least plane along each
    (spec/zarrify/czi.md §4.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#42-images>)."""
    planes = [s.plane for s in members]
    lo, hi = [min(p[n] for p in planes) for n in range(3)], [max(p[n] for p in planes) for n in range(3)]
    several = [hi[0] > lo[0], hi[1] > lo[1] or members[0].samples > 1, hi[2] > lo[2]]
    return [x for x, has in zip("tcz", several, strict=True) if has] + ["y", "x"], (lo[0], lo[1], lo[2])


@cache
def compared(path: Path) -> tuple[tuple[str, ...], Counter]:
    """What differs between the arrays the profile wrote of the input at `path` and what its
    descriptions give, and what the comparison saw."""
    d = described(path)
    out = d.out
    entries = out.bytes_entries if isinstance(out, Output) else out.entries
    said: list[str] = []
    seen: Counter = Counter()
    if d.stored.undescribed:
        said.append("descriptions dropped for room")
    units = [(d.parts(rows[0])[0], k) for k, rows in d.rows.items()]
    # each array's chunks, and the subblock that holds each chunk's bytes
    docs: dict[str, dict[str, JSON]] = {}
    chunks: dict[str, dict[str, Range]] = {}
    held: dict[str, dict[int, Subblock]] = {}
    for key, v in sorted(entries.items()):
        if key.endswith("zarr.json") and not key.startswith("refkv_source/"):
            doc = dict(mapping(json.loads(v), key))
            if doc["node_type"] == "array":
                docs[key[:-len("zarr.json")]] = doc
    for key, parts in out.refs.items():
        prefix = key.split("c/")[0]
        if prefix not in docs:
            assert key.startswith("refkv_source/"), key
            continue
        (part,) = parts
        chunk = source_range(part)
        chunks.setdefault(prefix, {})[key] = chunk
        found = [k for (file, offset, length), k in units
                 if file == chunk[0] and offset <= chunk[1] and chunk[1] + chunk[2] <= offset + length]
        if len(found) != 1:
            said.append(f"{key}: its bytes are in {len(found)} descriptions' units")
            continue
        held.setdefault(prefix, {}).setdefault(found[0], subblock(d, found[0]))
    presented: set[int] = set()
    for prefix, doc in docs.items():
        seen["arrays"] += 1
        members = list(held.get(prefix, {}).values())
        if not members:
            said.append(f"{prefix}: an array of no chunk")
            continue
        if presented & {s.k for s in members}:
            said.append(f"{prefix}: a subblock of another array too")
        presented |= {s.k for s in members}
        if prefix.startswith("tiles/"):
            axes, lo = axes_of(members)
        else:
            # an image's axes and least planes are over the subblocks of all its levels
            image = prefix.split("/")[0]
            levels = [m for p, m in held.items() if p.split("/")[0] == image and not p.startswith("tiles/")]
            every = [s for m in levels for s in m.values()]
            axes, lo = axes_of(every)
            group = mapping(json.loads(entries[f"{image}/zarr.json"]))
            own = mapping(group["attributes"]).get("refkv_zarrified")
            series = [dict(s.series) for s in every]
            if any(s != series[0] for s in series) or (own is None) != (not series[0]) or (
                    own is not None and at(own, "czi", "dimensions") != series[0]):
                said.append(f"{prefix}: a series of {series[0]}; the profile's {own}")
        said += [f"{prefix}: {s}" for s in check_array(prefix, doc, members, lo, axes, chunks[prefix], seen)]
    for k, a in enumerate(d.stored.arrays):
        if k not in presented:
            seen["descriptions the profile writes nothing of"] += 1
            seen["of them, with a grid"] += a.grid is not None
    seen["descriptions"] += len(d.stored.arrays)
    return tuple(said), seen


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_the_czi_profile_writes_what_the_descriptions_give(path):
    """Every array the profile writes of the input is what its subblocks' descriptions give."""
    said, _ = compared(path)
    assert said == ()


def test_the_fixtures_hold_every_case_the_profile_is_compared_in():
    """The fixtures reach each case of the comparison, so that none of it passes for want of a
    file. The 15 descriptions the profile writes nothing of are the 14 with no grid and one with
    a grid, a lossless JPEG stream of three components, which the profile does not place."""
    seen: Counter = Counter()
    for path in FILES:
        seen += compared(path)[1]
    assert {k: v for k, v in seen.items() if v} == {
        "descriptions": 198,
        "descriptions the profile writes nothing of": 15,
        "of them, with a grid": 1,
        "arrays": 94,
        "levels": 79,
        "levels clipped": 3,
        "levels of several tiles": 8,
        "tile arrays": 15,
        "tile arrays of a frame of another size": 1,
        "chunks of []": 127,
        "chunks of ['zstd']": 3,
        "chunks of ['czi_zstd1']": 4,
        "chunks of ['tiff_lzw']": 17,
        "chunks of ['jpeg']": 10,
        "chunks of ['jpegxr']": 17,
        "chunks of ['source 1']": 3,
        "chunks of ['source 3']": 2,
    }


def test_planes_the_fixtures_lack_are_placed_from_their_images_least(tmp_path):
    """Two images no fixture holds: one whose planes start above 0, so that a chunk's index along
    `t` is its `T` less the least; and one whose second level has the later of two planes alone,
    so that the level's chunk is placed from the image's least plane, not the level's own."""
    plane = writer.plane
    later = writer.Czi([plane(1, 0, 0, 0, 8, 8, T=2), plane(1, 0, 0, 0, 8, 8, T=3)])
    levels = writer.Czi([plane(1, 0, 0, 0, 8, 8, T=0), plane(1, 0, 0, 0, 8, 8, T=1),
                         plane(1, 0, 0, 0, 4, 4, wl=8, hl=8, T=1)])
    said, seen = compared(written(tmp_path, "later", later.build()))
    assert said == ()
    assert {k: v for k, v in seen.items() if v} == {"descriptions": 2, "arrays": 1, "levels": 1, "chunks of []": 2}
    assert sorted(k for k in described(tmp_path / "later" / "later.czi").out.refs if k.startswith("0/")) == [
        "0/0/c/0/0/0", "0/0/c/1/0/0"]
    said, seen = compared(written(tmp_path, "levels", levels.build()))
    assert said == ()
    assert {k: v for k, v in seen.items() if v} == {"descriptions": 3, "arrays": 2, "levels": 2, "chunks of []": 3}
    assert sorted(k for k in described(tmp_path / "levels" / "levels.czi").out.refs if k.startswith("0/")) == [
        "0/0/c/0/0/0", "0/0/c/1/0/0", "0/1/c/1/0/0"]
