"""The stored arrays of the VSI fixtures (spec/stored-array.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>), compared with
their reference readers. Bio-Formats, the VSI profile's independent reader, reads decoded planes
and gives no tile's place in its file, so the comparison has two halves.

**The tables and the decoders**, which run everywhere:

- a tile file's arrays, one a level, against the file's headers and its tile directory, read
  here as spec/zarrify/vsi.md §3
  <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#3-the-tile-files> lays them out
  (not by the parser), and the stack's size and dimensions, read here from the `.vsi` file's tree
  (§2.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#21-containers-and-fields>,
  §2.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#22-stacks>): the key and
  the indexes, the declaration, the shape and the axes' order, the sample size, the grid, the
  chain, the fill value, and a row for each record of the directory, at the record's place and
  coordinates;
- each tile, decoded through its chain by imagecodecs: an array of the unit's shape and sample
  size, and, of level 0, the pixels the fixtures' generator encoded at the tile's place
  (`fixtures/vsi/expected`). A `jpeg` unit is decoded as imagecodecs reads its markers, which
  converts its colors as the profile's codec does; a `source` unit, which names no decoding, by
  the decoder of the encoding the header's `compression` names;
- the images of the `.vsi` file's IFDs against tifffile, which reads each IFD at its offset: the
  shape, the sample size, the declarations, the grid, the chain, each strip's place, and the
  values each strip decodes to.

**Bio-Formats**, where a JVM runs it (the test skips without one): each tile, decoded as above,
is the pixels Bio-Formats reads at the tile's place in its level, on the plane the tile's
coordinates give. Bio-Formats reads level 0 at the stack's size, as the description has it, and
a later level at a size it computes by halving, at most as large as the level's tiles reach,
which is the description's: its pixels are compared within its size. It reorders the samples of
an uncompressed blue-first file to red first, so those tiles are reversed before they are
compared.

Further tests assert which cases the fixtures reached, and the cases no fixture holds, on slides
the fixtures' generator writes."""

import logging
import math
import re
import struct
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType

import imagecodecs
import numpy as np
import pytest
import tifffile
from described import Described, described
from ir_helpers import FIXTURES, ROOT
from test_vsi import bioformats

from refkv.ir.kind import Kind
from refkv.ir.stored import Codec, Each, ElementPlace, Given, Place, Source, StoredArray, Value

VSI = FIXTURES / "vsi"
FILES = sorted(p for p in VSI.glob("*.vsi") if "reject" not in p.name)

type Range = tuple[str, int, int]
"""(object key, offset, length): bytes of a file of the slide"""


def vsi_generator() -> ModuleType:
    """The VSI fixtures' writer, under a name of its own: each format's generator is `write_fixtures`."""
    spec = spec_from_file_location("vsi_write_fixtures", ROOT / "fixtures" / "generators" / "vsi" / "write_fixtures.py")
    assert spec is not None
    assert spec.loader is not None
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


write_fixtures = vsi_generator()


@dataclass(frozen=True)
class Slide:
    """A slide's descriptions, with its store's files, each one's bytes by its key. A slide is
    always a store, so every place has a key."""

    d: Described
    files: dict[str, bytes]

    def where(self, element: int, within: tuple[int, int] | None = None) -> Range:
        """The bytes of an element's extent, or of a range of it."""
        key, start, length = self.d.at(element, within)
        assert key is not None
        return key, start, length

    def held(self, at: Range) -> bytes:
        key, offset, length = at
        return self.files[key][offset:offset + length]

    def placed(self, place: Place) -> bytes:
        """The bytes at a place: an element's, or a range of them."""
        assert isinstance(place, ElementPlace)
        return self.held(self.where(place.element, place.range))

    def part(self, row: dict) -> Range:
        """A row's unit: its element's bytes, or the range of them the row gives."""
        r = row["range"]
        return self.where(row["element"], None if r is None else (r["offset"], r["length"]))


def read_slide(path: Path) -> Slide:
    """The slide whose `.vsi` file is at `path`, its tile files beside it."""
    d = described(path)
    return Slide(d, {key: data for key, data in d.objects.items() if key is not None})


# ---- the files, read here as spec/zarrify/vsi.md lays them out

# a sample of each pixel type (§3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#3-the-tile-files>),
# little-endian
PIXEL_TYPES = {1: "i1", 2: "u1", 3: "<i2", 4: "<u2", 5: "<i4", 6: "<u4", 7: "<i8", 8: "<u8", 9: "<f4", 10: "<f8"}
RAW, JPEG, JPEG2000, LOSSLESS_JPEG, PNG, BMP = 0, 2, 3, 5, 8, 9
# where the ETS header holds its background color
BACKGROUND = 108


@dataclass(frozen=True)
class Tile:
    """A record of a tile directory: its coordinates, and where its tile is."""

    coords: tuple[int, ...]
    offset: int
    size: int


@dataclass(frozen=True)
class TileFile:
    """A tile file's headers and its directory."""

    ets: tuple[int, int]
    pixel_type: int
    channels: int
    compression: int
    tile_width: int
    tile_height: int
    background: bytes
    component_order: int
    use_pyramid: bool
    dimensions: int
    dimension_sizes: tuple[int, ...]
    tiles: tuple[Tile, ...]

    @property
    def dtype(self) -> np.dtype:
        return np.dtype(PIXEL_TYPES[self.pixel_type])

    @property
    def coordinates(self) -> int:
        return len(self.dimension_sizes)

    def level(self, t: Tile) -> int:
        """A tile's level: its last coordinate, in a pyramid."""
        return t.coords[-1] if self.use_pyramid else 0

    @property
    def outer(self) -> tuple[int, ...]:
        """The coordinates after the row's that are not the level's, the last first."""
        last = self.coordinates - (1 if self.use_pyramid else 0)
        return tuple(reversed(range(2, last)))

    def size(self, c: int) -> int:
        """The size of a coordinate after the row's: the header's, or 1 past those it lists."""
        return self.dimension_sizes[c] if c < self.dimensions else 1


def tile_file(b: bytes) -> TileFile:
    """A tile file, from its bytes: the SIS header, the ETS header it points at, and the directory."""
    n, ets_offset = struct.unpack_from("<i", b, 12)[0], struct.unpack_from("<q", b, 16)[0]
    tiles_offset, count = struct.unpack_from("<q", b, 32)[0], struct.unpack_from("<i", b, 40)[0]
    pixel_type, channels, _colorspace, compression = struct.unpack_from("<4i", b, ets_offset + 8)
    width, height = struct.unpack_from("<2i", b, ets_offset + 28)
    component_order, use_pyramid = struct.unpack_from("<2i", b, ets_offset + 148)
    dimensions = struct.unpack_from("<i", b, ets_offset + 184)[0]
    sizes = struct.unpack_from("<10i", b, ets_offset + 188)
    tiles = []
    for k in range(count):
        at = tiles_offset + k * (20 + 4 * n)
        assert struct.unpack_from("<i", b, at)[0] == n
        offset, size = struct.unpack_from("<qi", b, at + 4 + 4 * n)
        tiles.append(Tile(struct.unpack_from(f"<{n}i", b, at + 4), offset, size))
    return TileFile((ets_offset, 228), pixel_type, channels, compression, width, height,
                    b[ets_offset + BACKGROUND:ets_offset + BACKGROUND + 40], component_order, use_pyramid != 0,
                    dimensions, (*sizes, *[0] * 6)[:n], tuple(tiles))


@dataclass(frozen=True)
class Field:
    """A field of a container of the `.vsi` file's tree: its tags, its field type, its inline value
    or its data's bytes, and the fields of the container it holds."""

    tag: int
    tag2: int | None
    kind: int
    extended: bool
    value: int | None
    data: bytes
    fields: tuple["Field", ...]

    def first(self, tag: int, kind: int | None = None) -> "Field | None":
        """The first field of `tag` of the container this field holds (an extended one of the
        field type `kind`, where given)."""
        held = (f for f in self.fields if f.tag == tag and (kind is None or (f.extended and f.kind == kind)))
        return next(held, None)


VOLUME, PROPERTY_SET, MDIM_VOLUME = 0, 1, 2


def container(b: bytes, fp: int, end: int) -> tuple[Field, ...]:
    """The fields of the container at `fp`, whose region ends at `end`."""
    data_offset, flags = struct.unpack_from("<qI", b, fp + 8)
    fields = []
    p = fp + data_offset
    for _ in range(flags & 0x0FFFFFFF):
        kind, tag, following, size = struct.unpack_from("<4I", b, p)
        second, extended, inline = kind >> 27 & 1, bool(kind >> 28 & 1), kind >> 30 & 1
        q = p + (20 if second else 16)
        region = fp + following if following else end
        # a field with a second tag counts its data from its container's start
        data_end = q if inline else fp + 20 + size if second and size else q + size
        held: tuple[Field, ...] = ()
        if extended and kind & 0xFFFFFF == VOLUME:
            held = container(b, q, data_end)
        elif extended and kind & 0xFFFFFF in (PROPERTY_SET, MDIM_VOLUME):
            held = container(b, q, region)
        tag2 = struct.unpack_from("<I", b, p + 16)[0] if second else None
        fields.append(Field(tag, tag2, kind & 0xFFFFFF, extended, size if inline else None, b[q:data_end], held))
        p = region
    return tuple(fields)


@dataclass(frozen=True)
class Stack:
    """A stack of the tree: its id, its name, its size in pixels, and the coordinate of each of
    its dimensions that is the time's, the channels' or the depth's."""

    id: int
    name: str
    width: int
    height: int
    coordinate: dict[str, int]


def meaning(sets: list[Field]) -> str | None:
    """A dimension's meaning, of its description's property sets (§2.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#22-stacks>): `t`, `c` or `z`,
    or None for another."""
    codes = [f.value if f.value is not None else struct.unpack("<i", f.data)[0]
             for s in sets for f in s.fields if f.tag == 2023]
    if codes:
        return {1: "z", 2: "t", 4: "c"}.get(codes[0])
    has = {f.tag for s in sets for f in s.fields}
    return "z" if 2012 in has else "t" if has & {2100, 2027} else "c" if has & {2419, 2004} else None


def stacks(b: bytes) -> list[Stack]:
    """The stacks of a `.vsi` file's tree, in its order: each volume of tag 2001 of the collection
    volume, with the width and height of its image boundary."""
    root = Field(0, None, VOLUME, True, None, b"", container(b, 8, len(b)))
    collection = root.first(2000, VOLUME)
    out = []
    for f in collection.fields if collection is not None else ():
        if f.tag != 2001 or not f.extended or f.kind != VOLUME:
            continue
        frame, properties = f.first(2002, VOLUME), f.first(2005, PROPERTY_SET)
        external = frame.first(2018, PROPERTY_SET) if frame is not None else None
        boundary = external.first(2053) if external is not None else None
        _, _, width, height = struct.unpack("<4i", boundary.data) if boundary is not None else (0, 0, 0, 0)
        name = properties.first(2030) if properties is not None else None
        coordinate = {}
        for d in f.fields:
            if d.tag == 2007 and d.extended and d.kind == VOLUME:
                assert d.tag2 is not None
                m = meaning([s for s in d.fields if s.tag == 2008])
                if m is not None:
                    coordinate[m] = 2 + d.tag2
        text = name.data.decode("utf-16-le").split("\0")[0] if name is not None else ""
        assert f.tag2 is not None
        out.append(Stack(f.tag2, text, width, height, coordinate))
    return out


# ---- a tile file's arrays

def tiles_chain(f: TileFile) -> tuple[str, int | None]:
    """The chain of a tile file's tiles, by its header: the name of its one codec (`raw` for the
    empty chain), and, of `source`, the id it has."""
    size = f.dtype.itemsize
    if f.compression == RAW:
        return "raw", None
    if f.compression in (JPEG, LOSSLESS_JPEG) and size in (1, 2):
        return "jpeg", None
    if f.compression == JPEG2000 and size in (1, 2):
        return "jpeg2k", None
    return "source", f.compression


# the decoder of each encoding a `source` chain names by its id, the header's `compression`
SOURCE_DECODERS: dict[int, Callable[[bytes], np.ndarray]] = {
    JPEG2000: imagecodecs.jpeg2k_decode,
    PNG: imagecodecs.png_decode,
    BMP: imagecodecs.bmp_decode,
}


def decoded(chain: tuple[Codec, ...], unit: bytes, f: TileFile) -> np.ndarray:
    """A tile's bytes taken through its chain: the samples of an empty chain as the file stores
    them, or what imagecodecs decodes of the chain's one codec."""
    if not chain:
        samples = (f.channels,) if f.channels > 1 else ()
        return np.frombuffer(unit, dtype=f.dtype).reshape(f.tile_height, f.tile_width, *samples)
    (codec,) = chain
    if codec.name == "jpeg":
        assert not codec.parameters
        return imagecodecs.jpeg8_decode(unit)
    if codec.name == "jpeg2k":
        return imagecodecs.jpeg2k_decode(unit)
    assert codec.name == "source"
    identifier = codec.parameters["id"]
    assert isinstance(identifier, Value)
    return SOURCE_DECODERS[int(str(identifier.value))](unit)


def lengths(a: StoredArray) -> tuple[int, ...]:
    assert a.grid is not None
    assert all(isinstance(x, Each) for x in a.grid.axes)
    return tuple(x.length for x in a.grid.axes if isinstance(x, Each))


@dataclass(frozen=True)
class Volume:
    """A tile file with what the test reads of it: its key, its headers and directory, its
    stack, its bytes, and the positions of its arrays' descriptions, by level."""

    key: str
    f: TileFile
    stack: Stack
    data: bytes
    arrays: tuple[int, ...]

    def extent(self, level: int) -> tuple[int, int] | None:
        """The rows and columns of a level's array: the stack's at level 0, and as far as a
        later level's tiles reach, where it holds any."""
        if level == 0:
            return self.stack.height, self.stack.width
        held = [t for t in self.f.tiles if self.f.level(t) == level]
        if not held:
            return None
        return (self.f.tile_height * (1 + max(t.coords[1] for t in held)),
                self.f.tile_width * (1 + max(t.coords[0] for t in held)))

    def plane(self, t: Tile) -> tuple[int, int, int]:
        """The time point, the channel and the depth a tile's coordinates give."""
        last = self.f.coordinates - (1 if self.f.use_pyramid else 0)
        at = {m: t.coords[c] for m, c in self.stack.coordinate.items() if c < last}
        return at.get("t", 0), at.get("c", 0), at.get("z", 0)


def volumes(slide: Slide, path: Path) -> list[Volume]:
    """The slide's tile files, in the order of their stacks in the tree."""
    out = []
    for stack in stacks(slide.files[path.name]):
        name = rf"_{re.escape(path.stem)}_/stack{stack.id}/frame_t(_0)?\.ets"
        found = [k for k in slide.files if re.fullmatch(name, k)]
        if not found:
            continue
        (key,) = found
        element = slide.d.ir.at(f"{key}/ets")
        arrays = sorted((a.index if a.index is not None else -1, k)
                        for k, a in enumerate(slide.d.stored.arrays) if a.element == element)
        out.append(Volume(key, tile_file(slide.files[key]), stack, slide.files[key], tuple(k for _, k in arrays)))
    return out


def check_level(slide: Slide, v: Volume, level: int, seen: Counter) -> list[str]:
    """One level's description against the tile file's headers and the stack's size."""
    f, d = v.f, slide.d
    a = d.stored.arrays[v.arrays[level]]
    said = []
    if a.index != level:
        said.append(f"the index {a.index}")
    # the declaration: the ETS header, whole
    header = v.data[f.ets[0]:f.ets[0] + f.ets[1]]
    if slide.where(a.element) != (v.key, *f.ets) or header[:4] != b"ETS\0":
        said.append(f"keyed by {a.path}, which is not the ETS header")
    if a.declarations != (ElementPlace(a.element, None, None),) or slide.placed(a.declarations[0]) != header:
        said.append("the declaration is not the ETS header, the array's key")
    if a.item_bits != 8 * f.dtype.itemsize or a.pad_from is not None or a.unit_parameters:
        said.append(f"item_bits {a.item_bits} of the pixel type {f.pixel_type}, a padding or unit parameters")
    # the fill value: the background's first sample, where the IR can give it
    size, samples = f.dtype.itemsize, f.channels
    same = all(f.background[i * size:(i + 1) * size] == f.background[:size] for i in range(samples))
    given = f.compression == RAW and same
    seen["fill values given" if given else "fill values the IR does not give"] += 1
    if given:
        fill = a.fill.param if isinstance(a.fill, Given) else None
        place = fill.place if isinstance(fill, Source) else None
        # a range of the ETS header, the array's key
        if not isinstance(place, ElementPlace) or (place.element, place.range) != (a.element, (BACKGROUND, size)):
            said.append(f"the fill value {a.fill}, not the background's first sample")
        elif slide.placed(place) != f.background[:size]:
            said.append("the fill value's place does not hold the background's first sample")
    elif a.fill != "undefined":
        said.append(f"the fill value {a.fill} of a background the IR does not give")
    # the shape: the coordinates after the row's, the last first, the rows, the columns, the samples
    extent = v.extent(level)
    if extent is None:
        seen["levels of no tile"] += 1
        if (a.shape, a.axes, a.grid, a.chains) != (None, None, None, ()) or d.rows.get(v.arrays[level]):
            said.append("a shape, a grid, a chain or rows of a level that holds no tile")
        return said
    outer = tuple(f.size(c) for c in f.outer)
    listed = len(outer) + 2
    channels = (samples,) if samples > 1 else ()
    if a.shape != (*outer, *extent, *channels):
        said.append(f"shape {a.shape}, not {(*outer, *extent, *channels)}")
    if a.axes != (*reversed(range(listed)), *([listed] if channels else [])):
        said.append(f"axes {a.axes} of {listed} coordinates and {len(channels)} axes of samples")
    seen["levels of an axis before the rows"] += bool(outer)
    seen["levels of samples"] += bool(channels)
    seen["levels after the first, as large as their tiles reach"] += level > 0
    # the grid: a tile a unit, whole at the edges
    want = (*[1] * len(outer), f.tile_height, f.tile_width, *channels)
    if a.grid is None or lengths(a) != want or a.grid.edge != "full":
        return [*said, f"grid {a.grid}, not tiles of {want}"]
    # the chain: what the header's compression and pixel type give
    name, identifier = tiles_chain(f)
    chain = d.stored.chains[a.chains[0]] if len(a.chains) == 1 else None
    names = None if chain is None else [(c.name, dict(c.parameters)) for c in chain]
    source = {"format": Value("vsi"), "id": Value(identifier)}
    if names != {"raw": [], "source": [("source", source)]}.get(name, [(name, {})]):
        return [*said, f"chain {names} of the compression {f.compression} and the pixel type {f.pixel_type}"]
    seen[f"levels of {name}" + ("" if identifier is None else f" {identifier}")] += 1
    return said


def check_tiles(slide: Slide, v: Volume, expected: np.ndarray | None, seen: Counter) -> list[str]:
    """Each record of the directory against the rows of its level's array: at the record's
    place and coordinates, and decoding to a tile of the unit's shape, and to the generator's
    pixels there."""
    f, d = v.f, slide.d
    said = []
    rows = {k: {tuple(r["coords"]): r for r in d.rows.get(k, [])} for k in v.arrays}
    held = dict.fromkeys(v.arrays, 0)
    channels = (0,) if f.channels > 1 else ()
    for index, t in enumerate(f.tiles):
        level = f.level(t)
        k = v.arrays[level]
        a = d.stored.arrays[k]
        if a.grid is None or a.shape is None:
            said.append(f"tile {index} is of level {level}, which has no grid")
            continue
        column, line = t.coords[:2]
        coords = (*[t.coords[c] for c in f.outer], line, column, *channels)
        row = rows[k].get(coords)
        seen["tiles"] += 1
        # a tile past its level's size is no unit of the array
        height, width = v.extent(level) or (0, 0)
        if line >= math.ceil(height / f.tile_height) or column >= math.ceil(width / f.tile_width):
            seen["tiles outside their level"] += 1
            if row is not None:
                said.append(f"tile {index} at {coords}, outside its level, has a row")
            continue
        if row is None:
            said.append(f"tile {index} at {coords} has no row")
            continue
        held[k] += 1
        unset = ("member", "range", "ranges", "chain", "skips")
        if any(row[c] is not None for c in unset) or d.ir.path(row["element"]) != f"{v.key}/tiles/{index}":
            said.append(f"tile {index}: the row {row} is not its element alone")
            continue
        seen["tiles whose element names another's bytes"] += d.ir.kind(row["element"]) == Kind.ALIAS
        if slide.part(row) != (v.key, t.offset, t.size):
            said.append(f"tile {index} at {slide.part(row)}, the directory's {t.offset} of {t.size}")
            continue
        try:
            got = decoded(d.stored.chains[a.chains[0]], slide.held(slide.part(row)), f)
        except Exception as e:  # noqa: BLE001  (whatever a decoder raises: the unit is not its chain's)
            said.append(f"tile {index} does not decode: {e}")
            continue
        if got.shape != lengths(a)[len(f.outer):] or got.dtype.itemsize * 8 != a.item_bits:
            said.append(f"tile {index} decodes to {got.shape} of {got.dtype}, not {lengths(a)} of {a.item_bits} bits")
            continue
        seen["tiles decoded"] += 1
        if expected is not None and level == 0:
            region = expected[v.plane(t)][line * f.tile_height:, column * f.tile_width:][:f.tile_height, :f.tile_width]
            part = got[:region.shape[0], :region.shape[1]]
            seen["tiles compared with the generator's pixels"] += 1
            seen["tiles an edge cuts"] += region.shape != got.shape
            if part.dtype != region.dtype or part.tobytes() != region.tobytes():
                said.append(f"tile {index} at {coords} decodes to other pixels than the generator encoded there")
    said += [f"{len(rows[k])} rows of array {k}, {n} tiles of its grid" for k, n in held.items() if n != len(rows[k])]
    return said


def check_volume(slide: Slide, v: Volume, path: Path, seen: Counter) -> list[str]:
    """A tile file's arrays against its headers, its directory and its stack."""
    count = 1 + max((v.f.level(t) for t in v.f.tiles), default=0)
    seen["tile files"] += 1
    seen["levels"] += count
    if len(v.arrays) != count:
        return [f"{len(v.arrays)} arrays of the ETS header, {count} levels"]
    said = [s for level in range(count) for s in check_level(slide, v, level, seen)]
    at = VSI / "expected" / path.stem / f"{v.stack.id}.npy"
    expected = np.load(at) if path.parent == VSI and at.exists() else None
    return said + check_tiles(slide, v, expected, seen)


# ---- the images of the .vsi file's IFDs

# the tags that declare what an image's elements are, after the header's byte order
DECLARING = (258, 262, 320, 338, 339, 529, 530, 532)
# the compressions whose decoding restores the stored bytes, by their codec
RESTORING: dict[int, tuple[str, ...]] = {1: (), 5: ("tiff_lzw",), 8: ("zlib",), 32946: ("zlib",)}


def first(page: tifffile.TiffPage, tag: int, default: int) -> int:
    t = page.tags.get(tag)
    if t is None:
        return default
    return int(t.value[0] if isinstance(t.value, tuple) else t.value)


def every(page: tifffile.TiffPage, tag: int, default: tuple[int, ...]) -> tuple[int, ...]:
    t = page.tags.get(tag)
    if t is None:
        return default
    return tuple(int(x) for x in t.value) if isinstance(t.value, tuple) else (int(t.value),)


def tag_bytes(page: tifffile.TiffPage, tag: int, data: bytes) -> bytes | None:
    """The bytes of a field's value, as the file holds them."""
    t = page.tags.get(tag)
    if t is None:
        return None
    size = struct.calcsize(tifffile.TIFF.DATA_FORMATS[t.dtype]) * t.count
    return data[t.valueoffset:t.valueoffset + size]


def ifd_chain(page: tifffile.TiffPage) -> tuple[str, ...] | None:
    """The names of the codecs of an IFD image's chain, as this test reads tifffile's fields (by
    the rules a TIFF's images are described by, for the fields a slide's IFDs have); None for
    samples of unlike sizes, whose decoding the IR does not model."""
    # what no IFD of a slide here has, and this comparison does not read
    assert first(page, 266, 1) == 1, "a fill order"
    assert first(page, 317, 1) == 1, "a predictor"
    assert first(page, 32997, 1) == 1, "a depth"
    assert first(page, 262, 0) != 6, "YCbCr"
    assert 322 not in page.tags, "a tile's size"
    bits = set(every(page, 258, (1,)))
    compression = first(page, 259, 1)
    if len(bits) != 1:
        return None
    if compression in RESTORING:
        return RESTORING[compression]
    return ("jpeg",) if compression == 7 and bits == {8} else ("source",)


def ifd_units(page: tifffile.TiffPage, data: bytes) -> str | int:
    """The number of strips an IFD's tables must list, or why the IR gives its image no grid."""
    height, width, samples = first(page, 257, 0), first(page, 256, 0), first(page, 277, 1)
    if height == 0 or width == 0 or samples == 0:
        return "an empty image"
    rows = min(first(page, 278, 2**32 - 1), height)
    if rows == 0:
        return "an empty unit"
    offsets, counts = every(page, 273, ()), every(page, 279, ())
    planes = samples if samples > 1 and first(page, 284, 1) == 2 else 1
    if len(offsets) != math.ceil(height / rows) * planes or len(counts) != len(offsets):
        return "tables of another length"
    if any(n and o + n > len(data) for o, n in zip(offsets, counts, strict=True)):
        return "a unit outside the file"
    return len(offsets)


def check_ifd(slide: Slide, k: int, page: tifffile.TiffPage, key: str, seen: Counter) -> list[str]:
    """One IFD's image against its description: as a TIFF's, in strips."""
    d, data = slide.d, slide.files[key]
    a = d.stored.arrays[k]
    said = []
    height, width, samples = first(page, 257, 0), first(page, 256, 0), first(page, 277, 1)
    planar = samples > 1 and first(page, 284, 1) == 2
    shape = (height, width) if samples == 1 else (samples, height, width) if planar else (height, width, samples)
    bits = set(every(page, 258, (1,)))
    if a.shape != shape or a.item_bits != (bits.pop() if len(bits) == 1 else None):
        said.append(f"shape {a.shape} of {a.item_bits} bits, tifffile {shape} of {every(page, 258, (1,))}")
    if (a.fill, a.axes, a.index, a.unit_parameters, a.pad_from) != ("undefined", None, None, (), None):
        said.append("a fill value, axes, an index, unit parameters or a padding")
    # the byte order is the first two bytes of the file's header element
    declared = [data[:2]] + [b for t in DECLARING if (b := tag_bytes(page, t, data)) is not None]
    order = a.declarations[0] if a.declarations else None
    if [slide.placed(p) for p in a.declarations] != declared or not isinstance(order, ElementPlace) or (
            d.ir.path(order.element), order.range) != (f"{key}/header", (0, 2)):
        said.append("the declarations are not the byte order and the declaring fields' values")
    chain, units = ifd_chain(page), ifd_units(page, data)
    reason = "samples of unlike sizes" if chain is None else units if isinstance(units, str) else None
    if reason is not None or a.grid is None:
        seen[f"IFD images of no grid: {reason}"] += 1
        if reason is None or a.grid is not None or a.chains or d.rows.get(k):
            said.append(f"a grid, chains or rows for {reason}" if reason else "no grid")
        return said
    rows = min(first(page, 278, 2**32 - 1), height)
    want = (rows, width) if samples == 1 else (1, rows, width) if planar else (rows, width, samples)
    if lengths(a) != want or a.grid.edge != "short":
        return [*said, f"grid {a.grid}, tifffile's strips of {want}"]
    codecs = d.stored.chains[a.chains[0]] if len(a.chains) == 1 else ()
    if tuple(c.name for c in codecs) != chain:
        return [*said, f"chain {[c.name for c in codecs]}, by tifffile's fields {chain}"]
    seen["IFD images of " + (" ".join(chain) if chain else "raw")] += 1
    tables = tag_bytes(page, 347, data)
    for c in codecs:
        places = {n: slide.placed(v.place) for n, v in c.parameters.items() if isinstance(v, Source)}
        values = {n: v.value for n, v in c.parameters.items() if isinstance(v, Value)}
        if c.name == "jpeg":
            seen["IFD images of jpeg with tables"] += tables is not None
            if places != ({} if tables is None else {"tables": tables}) or values:
                said.append("jpeg's tables are not the JPEGTables field's value")
        elif c.name == "source":
            if values != {"format": "vsi", "id": first(page, 259, 1)} or places:
                said.append(f"source's parameters {values} {places}")
        elif c.parameters:
            said.append(f"{c.name} has parameters")
    # an image with no tile's size is in strips, whose tables are the strips': tifffile reads the
    # tiles' tables wherever an IFD has them, and so reads no such image
    offsets, counts = every(page, 273, ()), every(page, 279, ())
    theirs = None
    if (page.dataoffsets, page.databytecounts) != (offsets, counts):
        seen["IFD images tifffile reads by the tables of tiles they are not in"] += 1
    else:
        try:
            theirs = page.asarray()
        except Exception:  # noqa: BLE001  (whatever a decoder raises: tifffile reads no values to compare)
            seen["IFD images tifffile does not decode"] += 1
        # samples it has no data type for (floats of 8 bits) read as an array of nothing
        if theirs is not None and theirs.shape != shape:
            theirs = None
            seen["IFD images tifffile has no data type for"] += 1
    got = {tuple(r["coords"]): r for r in d.rows.get(k, [])}
    if len(got) != units:
        said.append(f"{len(got)} rows, {units} strips")
    for j, (offset, count) in enumerate(zip(offsets, counts, strict=True)):
        assert not planar, "planar samples in a table of as many strips: no slide here has them"
        coords = (j, 0) if samples == 1 else (j, 0, 0)
        row = got.get(coords)
        seen["IFD strips"] += 1
        if row is None or slide.part(row) != (key, offset, count) or any(
                row[c] is not None for c in ("member", "ranges", "chain", "skips")):
            said.append(f"strip {coords} at {None if row is None else slide.part(row)}, tifffile {offset} of {count}")
            continue
        # a row names its element alone, but where the element's recipe gives other bytes than the
        # stored ones: then the range of all its extent names those
        form = d.ir.form(row["element"])
        framed = form is not None and '"shared"' in form
        seen["IFD strips of a range"] += framed
        if (row["range"] is not None) != framed or d.ir.path(row["element"]) != f"{a.path}/strips/{j}":
            said.append(f"strip {coords}: the range {row['range']} of the element {d.ir.path(row['element'])}")
        if theirs is None:
            continue
        stored = slide.held(slide.part(row))
        if chain == ("jpeg",):
            space, out = tifffile.tifffile.jpeg_decode_colorspace(
                page.photometric, page.planarconfig, page.extrasamples, page.is_jfif)
            mine = imagecodecs.jpeg8_decode(stored, tables=tables, colorspace=space, outcolorspace=out)
        elif chain == ():
            here = min(rows, height - j * rows)
            mine = np.frombuffer(stored, dtype=theirs.dtype.newbyteorder("<")).reshape(here, width, *shape[2:])
        else:
            continue
        seen["IFD strips decoded"] += 1
        if mine.tobytes() != theirs[j * rows:(j + 1) * rows].tobytes():
            said.append(f"strip {coords} decodes to other values than tifffile reads")
    return said


def check_ifds(slide: Slide, path: Path, seen: Counter) -> list[str]:
    """Every IFD tifffile reaches in the `.vsi` file that declares a size, against the description
    of its image: the description keyed by the IFD's element."""
    d, key = slide.d, path.name
    elements = {d.ir.extent(i)[0]: i for i in range(len(d.ir))
                if re.fullmatch(rf"{re.escape(key)}/.*/ifds/\d+", d.ir.path(i))}
    described_by = {a.element: k for k, a in enumerate(d.stored.arrays)}
    said, compared_with = [], set()
    with tifffile.TiffFile(path) as tif:
        tif.pages.useframes = False
        for page in tif.pages:
            assert isinstance(page, tifffile.TiffPage)
            if 256 not in page.tags or 257 not in page.tags:
                continue
            seen["IFDs that declare a size"] += 1
            element = elements.get(page.offset)
            if element is None:
                # an IFD in no TIFF region of the tree has no element, and so no description
                seen["IFDs of no element, not described"] += 1
                continue
            k = described_by.get(element)
            if k is None:
                said.append(f"no description of the IFD at {page.offset}")
                continue
            compared_with.add(k)
            said += [f"{d.stored.arrays[k].path}: {s}" for s in check_ifd(slide, k, page, key, seen)]
    return said + [f"{a.path}: a description of no IFD tifffile reaches" for k, a in enumerate(d.stored.arrays)
                   if k not in compared_with and slide.where(a.element)[0] == key]


@cache
def compared(path: Path) -> tuple[tuple[str, ...], Counter]:
    """What differs between the descriptions of the slide whose `.vsi` file is at `path` and what
    the tables, the decoders and tifffile give, and what the comparison saw."""
    logging.getLogger("tifffile").setLevel(logging.CRITICAL)
    slide = read_slide(path)
    seen: Counter = Counter()
    said = ["descriptions dropped for room"] if slide.d.stored.undescribed else []
    found = volumes(slide, path)
    if {v.key for v in found} != set(slide.files) - {path.name}:
        said.append(f"the tile files {sorted(slide.files)} are not the tree's stacks'")
    for v in found:
        said += [f"{v.key}: {s}" for s in check_volume(slide, v, path, seen)]
    said += check_ifds(slide, path, seen)
    # every description is a tile file's level or an IFD's image
    seen["arrays"] += len(slide.d.stored.arrays)
    if len(slide.d.stored.arrays) != sum(len(v.arrays) for v in found) + sum(
            slide.where(a.element)[0] == path.name for a in slide.d.stored.arrays):
        said.append("a description that is neither a tile file's nor an IFD's")
    return tuple(said), seen


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_a_slides_arrays_are_described_as_its_tables_and_tifffile_give_them(path):
    """Every level of every tile file is described as the file's headers and directory give it,
    each tile at its record's place, decoding to a tile of its pixels; and every image of the
    `.vsi` file's IFDs as tifffile reads it."""
    said, seen = compared(path)
    assert said == ()
    assert seen["arrays"] >= 1


def test_the_fixtures_hold_every_case_the_comparison_reads():
    """The fixtures reach each case of the comparison, so that none of it passes for want of a
    file. The `source` chains are PNG (8) and BMP (9). The
    fill values the IR does not give are those of compressed tiles, and of a background whose
    samples differ. The IFD of no element is a macro image's that lies outside the tree. The
    images of no grid are a macro image of no columns, one whose strip lies outside the file, a
    JPEG stream listed as two strips, and planar samples in a table of one strip. tifffile decodes
    no JPEG strip whose IFD says LZW, has no data type for floats of 8 bits, and reads an image
    that has the tiles' tables and no tile's size by those tables, where the description, as
    TIFF's rules have it, reads the strips'."""
    seen: Counter = Counter()
    for path in FILES:
        seen += compared(path)[1]
    assert {k: v for k, v in seen.items() if v} == {
        "arrays": 118,
        "tile files": 35,
        "levels": 58,
        "levels after the first, as large as their tiles reach": 23,
        "levels of an axis before the rows": 6,
        "levels of samples": 14,
        "levels of raw": 41,
        "levels of jpeg": 8,
        "levels of jpeg2k": 2,
        "levels of source 8": 4,
        "levels of source 9": 3,
        "fill values given": 39,
        "fill values the IR does not give": 19,
        "tiles": 288,
        "tiles decoded": 288,
        "tiles compared with the generator's pixels": 231,
        "tiles an edge cuts": 90,
        "IFDs that declare a size": 61,
        "IFDs of no element, not described": 1,
        "IFD images of raw": 39,
        "IFD images of jpeg": 13,
        "IFD images of jpeg with tables": 13,
        "IFD images of tiff_lzw": 3,
        "IFD images of source": 1,
        "IFD images of no grid: an empty image": 1,
        "IFD images of no grid: a unit outside the file": 1,
        "IFD images of no grid: tables of another length": 2,
        "IFD images tifffile does not decode": 3,
        "IFD images tifffile has no data type for": 1,
        "IFD images tifffile reads by the tables of tiles they are not in": 1,
        "IFD strips": 6090,
        "IFD strips of a range": 14,
        "IFD strips decoded": 6084,
    }


# ---- the cases no fixture holds, on slides the fixtures' generator writes


def written(folder: Path, edit: Callable[[list], list] | None = None, ets: dict | None = None,
            macro: object = None, **how) -> Path:
    """A slide the fixtures' generator writes to `folder`: a macro image (a JPEG strip of 16 by 8
    pixels, or the generator's image `macro`), and one stack of 24 by 16 pixels in tiles of 8 by
    8, with `how` for the generator's other arguments, its tiles (each its coordinates and its
    bytes) changed by `edit` before they are written, and its tile file written with `ets` for
    the generator's arguments."""
    stack = write_fixtures.Stack(10002, "s", **{"width": 24, "height": 16, "tile": (8, 8), "seed": 30, **how})
    build = stack.build

    def built() -> np.ndarray | None:
        out = build()
        if edit is not None:
            stack.tiles = edit(stack.tiles)
        return out

    stack.build = built
    image = write_fixtures.macro_image(seed=31, w=16, h=8) if macro is None else macro
    write_fixtures.Slide("written", [stack], macro=image).write(folder, ets_kw={10002: ets or {}}, expected=False)
    return folder / "written.vsi"


def test_slides_no_fixture_holds_are_described_as_their_tables_give_them(tmp_path):
    """Tile files the fixtures do not have, each written here: every one is described as its
    headers and its directory give it, each level with a grid.

    A tile of level 0 past the stack's size is no unit of the array the slide declares: it has
    no row, and the level keeps the stack's size. Two records of one place are two rows, each
    its own element, one of which the finished IR makes a second name for the other's bytes."""

    def past_the_size(tiles: list) -> list:
        # a column and a row past the three columns and two rows of level 0
        return [*tiles, ((5, 0, 0), tiles[0][1]), ((0, 4, 0), tiles[1][1])]

    # the second tile's place: after the headers and the first tile's 64 bytes
    shared = {"tile_offset": (4, 64 + 228 + 64)}
    cases: dict[str, tuple[dict, dict[str, int]]] = {
        "tiles past the stack's size": (
            {"levels": 2, "edit": past_the_size}, {"levels": 2, "tiles": 10, "tiles outside their level": 2}),
        "two records of one place": (
            {"ets": shared}, {"levels": 1, "tiles": 6, "tiles whose element names another's bytes": 1}),
        "a file of no tile": ({"edit": lambda _: []}, {"levels": 1, "tiles": 0, "fill values given": 1}),
        "uncompressed samples of one background": (
            {"samples": 3, "background": b"\x07\x07\x07"}, {"levels of samples": 1, "fill values given": 1}),
        "a background of unlike samples": (
            {"samples": 3, "background": b"\x07\x07\x08"}, {"levels of raw": 1, "fill values the IR does not give": 1}),
    }
    for name, (how, want) in cases.items():
        folder = tmp_path / str(len(list(tmp_path.iterdir())))
        said, seen = compared(written(folder, **how))
        assert said == (), name
        assert {k: seen[k] for k in want} == want, name
        assert (seen["tile files"], seen["levels of no tile"]) == (1, 0), name
        assert seen["tiles decoded"] == seen["tiles"] - seen["tiles outside their level"], name


def test_a_level_that_holds_no_tile_has_no_shape_and_no_grid(tmp_path):
    """A level after the first declares no size, and is as large as its tiles reach: one that
    holds no tile, below one that holds some, has no extent, and so neither a shape nor a grid."""
    path = written(tmp_path, levels=3, edit=lambda tiles: [t for t in tiles if t[0][-1] != 1])
    said, seen = compared(path)
    assert said == ()
    assert (seen["levels"], seen["levels of no tile"], seen["tiles"], seen["tiles decoded"]) == (3, 1, 7, 7)
    slide = read_slide(path)
    (v,) = volumes(slide, path)
    first_level, second, third = (slide.d.stored.arrays[k] for k in v.arrays)
    assert (first_level.shape, third.shape) == ((16, 24), (8, 8))
    assert (second.shape, second.axes, second.grid, second.chains, second.item_bits) == (None, None, None, (), 8)
    assert slide.d.rows.get(v.arrays[1]) is None


def test_jpeg_tiles_of_two_byte_samples_are_jpeg(tmp_path):
    """The list's `jpeg` is of samples of 8 or 16 bits: JPEG tiles of a pixel type of two bytes
    (12-bit streams) are `jpeg`, decoded to the pixel type's sample size."""
    said, seen = compared(written(tmp_path, dtype="uint16", compression=JPEG))
    assert said == ()
    assert (seen["levels"], seen["levels of jpeg"], seen["tiles decoded"]) == (1, 1, 6)
    assert seen["fill values the IR does not give"] == 1


def test_jpeg_2000_tiles_of_four_byte_samples_are_source(tmp_path):
    """The list's `jpeg2k` is given samples of one or two bytes: JPEG 2000 tiles of a pixel type
    of four are `source`."""
    said, seen = compared(written(tmp_path, dtype="int32", compression=JPEG2000))
    assert said == ()
    assert (seen["levels"], seen["levels of source 3"], seen["tiles decoded"]) == (1, 1, 6)


# ---- Bio-Formats, where a JVM runs it

def check_scene(slide: Slide, v: Volume, planes: dict[int, np.ndarray], seen: Counter) -> list[str]:
    """A tile file's tiles against the planes Bio-Formats reads of its stack, by level, each
    plane in TCZYX order, with the samples last where it has several."""
    f, d = v.f, slide.d
    said = []
    if len(planes) != len(v.arrays):
        said.append(f"Bio-Formats reads {len(planes)} levels, {len(v.arrays)} are described")
    # Bio-Formats reorders the samples of an uncompressed blue-first file to red first
    reorder = f.compression == RAW and f.component_order == 1 and f.channels > 1
    for level, theirs in sorted(planes.items()):
        a = d.stored.arrays[v.arrays[level]]
        seen["levels Bio-Formats reads"] += 1
        if a.shape is None or a.grid is None:
            seen["levels Bio-Formats reads of no description's shape"] += 1
            continue
        mine = a.shape[len(f.outer):len(f.outer) + 2]
        size = theirs.shape[3:5]
        # level 0 at the stack's size; a later level no larger than its tiles reach
        if size != mine if level == 0 else any(n > m for n, m in zip(size, mine, strict=True)):
            said.append(f"level {level} is {mine}, Bio-Formats reads {size}")
        seen["levels Bio-Formats reads smaller than their tiles reach"] += size != mine
        for row in d.rows.get(v.arrays[level], []):
            index = int(d.ir.path(row["element"]).rsplit("/", 1)[1])
            t = f.tiles[index]
            got = decoded(d.stored.chains[a.chains[0]], slide.held(slide.part(row)), f)
            column, line = t.coords[:2]
            plane = theirs[v.plane(t)]
            region = plane[line * f.tile_height:, column * f.tile_width:][:f.tile_height, :f.tile_width]
            if region.size == 0:
                seen["tiles outside the level Bio-Formats reads"] += 1
                continue
            part = got[:region.shape[0], :region.shape[1]]
            seen["tiles Bio-Formats reads"] += 1
            seen["tiles Bio-Formats reorders"] += reorder
            if not np.array_equal(part[..., ::-1] if reorder else part, region):
                said.append(f"level {level}: tile {index} decodes to other pixels than Bio-Formats reads there")
    return said


def read_by_bioformats(path: Path, reader: type, seen: Counter) -> list[str]:
    """What differs between the tiles of the slide at `path`, decoded through their chains, and
    the pixels Bio-Formats reads of it."""
    slide = read_slide(path)
    try:
        r = reader(str(path))
    except Exception:  # noqa: BLE001  (whatever Bio-Formats raises of a file it does not read)
        seen["slides Bio-Formats refuses"] += 1
        return []
    seen["slides Bio-Formats reads"] += 1
    found = volumes(slide, path)
    scenes = list(r.scenes or ())
    # its scenes are the stacks with a tile file, in the tree's order, and then the macro image
    if [s.lower() for s in scenes[:len(found)]] != [v.stack.name.lower() for v in found]:
        return [f"Bio-Formats' scenes {scenes}, the tile files' stacks {[v.stack.name for v in found]}"]
    said = []
    for scene, v in zip(scenes, found, strict=False):
        r.set_scene(scene)
        r.set_resolution_level(0)  # the level stays set across scenes
        planes = {}
        for level in r.resolution_levels:
            r.set_resolution_level(level)
            planes[level] = r.get_image_data("TCZYXS" if "S" in str(r.dims) else "TCZYX")
        seen["stacks Bio-Formats reads"] += 1
        said += [f"{path.name}: {v.key}: {s}" for s in check_scene(slide, v, planes, seen)]
    return said


def test_tiles_decode_to_the_pixels_bioformats_reads_at_their_places():
    """Every tile of every fixture Bio-Formats reads, decoded through its chain, is the pixels
    Bio-Formats reads at the tile's place, on its plane and in its level; and the counts say
    what Bio-Formats read: 242 of the fixtures' 288 tiles. It refuses three slides (the one of
    every raw pixel type, the one of the metadata's edges, and one whose macro image has no
    columns), and reads every level after the first smaller than the level's tiles reach."""
    reader = bioformats()
    if reader is None:
        pytest.skip("Bio-Formats is not available (bioio-bioformats needs a JVM and Maven)")
    seen: Counter = Counter()
    said = [s for path in FILES for s in read_by_bioformats(path, reader, seen)]
    assert said == []
    assert {k: v for k, v in seen.items() if v} == {
        "slides Bio-Formats reads": 22,
        "slides Bio-Formats refuses": 3,
        "stacks Bio-Formats reads": 25,
        "levels Bio-Formats reads": 47,
        "levels Bio-Formats reads smaller than their tiles reach": 22,
        "tiles Bio-Formats reads": 242,
        "tiles Bio-Formats reorders": 8,
    }
