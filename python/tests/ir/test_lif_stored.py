"""The stored arrays of the LIF fixtures (spec/stored-array.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>), compared with
liffile, the reference reader. Of each image of a LIF, LOF or LIFEXT file, and of each LOF a
document of a LAS X project names,

- the description is keyed by the element of the XML that declares the image, with the image's
  index, and that element is its declaration: the XML liffile reads the image's dimensions and
  channels from;
- its shape, its axes' order and its grid are those the byte increments liffile reads give, by
  the rule of the format's paragraph, and its sample size is that of liffile's data type;
- each row's element is the memory block liffile finds, at its offset and of its size, and its
  place is where the increments put the unit's first sample;
- each unit's bytes are the samples the increments place there, and the whole array is the one
  liffile reads, its axes mapped through `shape` and `axes`.

liffile 2026.10.14 departs from what a file declares in six ways, each counted where it is met.
It gives the samples of an image whose channels are tagged blue, then green, as RGB, where the
file stores BGR: the comparison turns them back. It reads only the first `ChildrenOf` of a LIFEXT
file: the image of a second is compared with the bytes the fixture's writer wrote. It follows a
cycle of a project's documents forever, so a project's documents are read one at a time. It
reads an image's samples end to end from the start of its memory block, whatever the increments
say of bytes between them or before them (but for stride-aligned rows of interleaved samples),
and it does not count the `Offset` of a project's `Block` into a LOF's block: of such images,
which no fixture holds, it reads other bytes than the increments give. And it orders two
dimensions of one `BytesInc` the first listed slowest, where the description takes the later
one, as LAS X packs them; only dimensions of one element are met so, whose order moves no byte.

A second test asserts which cases the fixtures reached; the others write the cases no fixture
holds."""

import sys
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from functools import cache
from itertools import pairwise
from pathlib import Path

import liffile
import numpy as np
import pytest
from described import Described, described
from ir_helpers import FIXTURES, ROOT

from refkv.ir.stored import Each, ElementPlace, Grid

sys.path.insert(0, str(ROOT / "fixtures" / "generators" / "lif"))
# pyrefly: ignore[missing-import]  # on sys.path above; the config's search-path lacks fixtures/generators
from write_fixtures import (
    Lif,
    Lof,
    accepted_lifext,
    attrs,
    block_memory,
    collection,
    document,
    experiment,
    uid,
    written,
)

DOCUMENTS = (".xlef", ".xlcf", ".xlif", ".xllf")
FILES = sorted(p for p in (FIXTURES / "lif").iterdir()
               if p.is_file() and "reject" not in p.name and p.suffix in (".lif", ".lof", ".lifext", *DOCUMENTS))
DIMENSIONS = "Image/ImageDescription/Dimensions/DimensionDescription"
CHANNELS = "Image/ImageDescription/Channels/ChannelDescription"
# DimIDs (spec/zarrify/lif.md §2.4
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#24-dimensions-and-channels>): X, Y,
# Z, T and a mosaic's tiles
X, Y, Z, T, M = 1, 2, 3, 4, 10
# what liffile raises of an image it does not read
REFUSALS = (ValueError, AssertionError, ZeroDivisionError)
# the chunk map's limit of a file, in entries, before the 4 for each of the file's bytes
# (spec/stored-array.md §6 <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#6-in-the-ir>)
MAP_LIMIT = 1 << 22

type Range = tuple[str | None, int, int]
"""(object key, offset, length): bytes of a file of the input; the key of a file input is None"""


@dataclass(frozen=True)
class Input:
    """An input's descriptions, with its files: each object's key by its element (a file input
    has no object), and each file's bytes by its key."""

    d: Described
    keys: dict[int, str]
    files: dict[str | None, bytes]

    def where(self, element: int, within: tuple[int, int] | None = None) -> Range:
        """The bytes of an element's extent, or of a range of it."""
        return self.d.at(element, within)

    def unit(self, row: dict) -> Range:
        """A row's unit: its element's bytes, or its range of them."""
        r = row["range"]
        return self.where(row["element"], None if r is None else (r["offset"], r["length"]))

    def held(self, at: Range) -> bytes:
        key, offset, length = at
        return self.files[key][offset:offset + length]


def read_input(path: Path) -> Input:
    """The input at `path`: a file, or a project's named document with the files it names."""
    d = described(path)
    return Input(d, d.keys, d.objects)


@dataclass(frozen=True)
class Read:
    """What liffile reads of an image: each dimension's `NumberOfElements` and `BytesInc`, in the
    XML's order; the channels' `BytesInc`, ascending; the attributes of its dimensions and of its
    channels; its data type; its memory block's offset and size, in the block's file; where the
    image starts in the block (the `Offset` of a project's `Block`); and its array, BGR samples as
    stored, with each axis's position in the source's list (the dimensions in the XML's order,
    then the channels), or None where liffile refuses the image or gives other axes."""

    listed: tuple[tuple[int, int], ...]
    offsets: tuple[int, ...]
    attributes: tuple[list[dict[str, str]], list[dict[str, str]]]
    dtype: np.dtype
    block: tuple[int, int]
    start: int
    values: np.ndarray | None
    positions: tuple[int, ...] | None
    bgr: bool


def attributes(of: ET.Element, under: str) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """The attributes of an image's dimensions and of its channels, each list in document order."""
    return ([dict(e.attrib) for e in of.findall(f"{under}{DIMENSIONS}")],
            [dict(e.attrib) for e in of.findall(f"{under}{CHANNELS}")])


def read(im: liffile.LifImage, start: int = 0) -> Read:
    """An image as liffile reads it, the file opened with every dimension kept."""
    dims, channels = attributes(im.xml_element, "./Data/")
    listed = tuple((int(e["NumberOfElements"]), int(e["BytesInc"])) for e in dims)
    block = im.memory_block
    values, positions, bgr = None, None, False
    try:
        sizes = dict(im.sizes)  # before asarray, which reads padded rows only once sizes has found them
        values = np.asarray(im.asarray())
        bgr = bool(im._is_bgr)
    except REFUSALS:
        pass
    else:
        if bgr:  # liffile gives BGR samples as RGB; the file stores them BGR
            values = values[..., ::-1]
        # each of liffile's dimensions is a dimension of the XML: the first not yet taken of its
        # DimID, length and increment (liffile sorts them by increment, and keeps the XML's order
        # of those alike)
        free = list(range(len(dims)))
        at: dict[str, int] = {}
        for dim in im._dimensions:
            read_as = (str(dim.dim_id), dim.number_elements, dim.bytes_inc)
            j = next(j for j in free if (dims[j]["DimID"], *listed[j]) == read_as)
            free.remove(j)
            at[dim.label] = j
        # any other axis is the channels', which liffile may spread over several
        if sum(label not in at for label in sizes) <= 1:
            positions = tuple(at.get(label, len(dims)) for label in sizes)
    return Read(listed, tuple(sorted(int(c["BytesInc"]) for c in channels)), (dims, channels), np.dtype(im.dtype),
                (block.offset, block.size), start, values, positions, bgr)


def laid_out(r: Read, held: bytes) -> tuple[np.ndarray, np.ndarray]:
    """Where the increments put each sample in the file whose bytes are `held`, and the samples
    there: the sample of channel c at the index i of each dimension lies `o_c + Σ i × s` bytes
    into the image's bytes (spec/zarrify/lif.md §3.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#32-channels>). The axes are the
    source's list: the dimensions in the XML's order, then the channels where there are several."""
    at = np.array(r.block[0] + r.start, dtype=np.int64)
    for n, s in r.listed:
        at = at[..., None] + np.arange(n, dtype=np.int64) * s
    at = at[..., None] + np.array(r.offsets, dtype=np.int64) if len(r.offsets) > 1 else at + r.offsets[0]
    raw = np.frombuffer(held, np.uint8)
    samples = np.ascontiguousarray(raw[at[..., None] + np.arange(r.dtype.itemsize)]).view(r.dtype)[..., 0]
    return at, samples


def storage(listed: tuple[tuple[int, int], ...], offsets: tuple[int, ...], item: int) -> tuple[list[int], int | None]:
    """The rule of the format's paragraph: the axes in storage order, the slowest first, each by
    its position in the source's list; and how many of the first have a unit for each of their
    elements, or None where the axes do not nest. An axis is (bytes a step, position, length,
    whether its elements are evenly apart, where its last element is)."""
    axes = [(s, j, n, True, (n - 1) * s) for j, (n, s) in enumerate(listed)]
    if len(offsets) > 1:
        gaps = [b - a for a, b in pairwise(offsets)]
        axes.append((min(gaps), len(listed), len(offsets), len(set(gaps)) == 1, offsets[-1] - offsets[0]))
    axes.sort()
    order = [j for _, j, _, _, _ in reversed(axes)]
    span, packed, inner = item, True, 0
    for i, (step, _, n, even, last) in enumerate(axes):
        if n > 1:
            if step < span:
                return order, None
            packed = packed and even and step == span
            span += last
        if packed:
            inner = i + 1
    return order, len(axes) - inner


def declared_images(text: str) -> list[tuple[list[dict[str, str]], list[dict[str, str]]]]:
    """The images an XML text declares, in document order: each one's dimensions' and channels'
    attributes. An older LOF's XML is its one image's `Data`."""
    root = ET.fromstring(text)  # noqa: S314 (the XML of a fixture, or of a file the test wrote)
    if root.tag == "Data":
        return [attributes(root, "./")]
    return [attributes(e, "./Data/") for e in root.iter("Element") if e.find("./Data/Image") is not None]


def check_key(inp: Input, k: int, key: str | None, name: str, index: int) -> list[str]:
    """The key of description `k`, of image `index` of the file named `name` (the object `key` of
    a project), and its declaration: the element of the file's XML, a LOF's after its data."""
    a = inp.d.stored.arrays[k]
    said = []
    xml = "description/xml" if name.lower().endswith(".lof") else "header/xml"
    if a.path != (xml if key is None else f"{key}/{xml}") or a.index != index or inp.where(a.element)[0] != key:
        said.append(f"keyed by {a.path} with the index {a.index}, not by {xml} of {name} with {index}")
    if a.declarations != (ElementPlace(a.element, None, None),):
        said.append(f"declarations {a.declarations}, not the key's element")
    if a.fill != "undefined" or a.pad_from is not None or a.unit_parameters:
        said.append("a fill value, a padding or unit parameters")
    return said


def check(inp: Input, k: int, key: str | None, name: str, index: int, r: Read, seen: Counter) -> list[str]:
    """What differs between description `k`, of image `index` of the file named `name` (the
    object `key` of a project), and liffile's reading `r` of the image."""
    d = inp.d
    a = d.stored.arrays[k]
    rows = d.rows.get(k, [])
    held = inp.files[key]
    item = r.dtype.itemsize
    said = check_key(inp, k, key, name, index)
    seen["images"] += 1

    # the declaration holds the XML liffile read the image of
    declared = declared_images(inp.held(inp.where(a.element)).decode("utf-16-le"))
    if index >= len(declared) or declared[index] != r.attributes:
        said.append(f"the declaration's image {index} is not the one liffile reads")

    # the shape, the axes' order and the sample size, by the increments
    order, outer = storage(r.listed, r.offsets, item)
    rank = len(order)
    lengths = [*(n for n, _ in r.listed), len(r.offsets)]
    shape = tuple(lengths[j] for j in order)
    in_order = order == list(range(rank))
    if a.shape != shape or a.axes != (None if in_order else tuple(order)):
        said.append(f"shape {a.shape} along {a.axes}; the increments give {shape} along {order}")
    if a.item_bits != 8 * item:
        said.append(f"item_bits {a.item_bits}, liffile's samples {r.dtype}")
    seen[f"images of {a.item_bits} bits a sample"] += 1
    seen["images stored in the order the source lists"] += in_order
    if len(r.offsets) > 1:
        fastest = order[-1] == len(r.listed)
        seen["images of interleaved channels" if fastest else "images of channels in planes"] += 1
    if said:
        return said

    # the grid: none where the axes do not nest, or where the rows alone pass the map's limit
    units = int(np.prod(shape[:outer], dtype=object)) if outer is not None else 0
    past = units * (1 + rank) > MAP_LIMIT + 4 * len(held)
    if outer is None or past:
        seen["images whose rows pass the map's limit" if past else "images whose axes do not nest"] += 1
        if a.grid is not None or a.chains or rows:
            said.append(f"grid {a.grid}, chains {a.chains} and {len(rows)} rows of an image with no grid")
        return said
    grid = Grid(tuple(Each(1 if i < outer else n) for i, n in enumerate(shape)), "short")
    if a.grid != grid:
        return [*said, f"grid {a.grid}, not {grid}"]
    if len(a.chains) != 1 or d.stored.chains[a.chains[0]] != ():
        said.append(f"chains {a.chains}, not one empty chain")
    seen["images of one unit" if units == 1 else "images of several units"] += 1
    seen["units"] += units

    # the rows: a unit for each index of the outer axes, in order, each a run of the samples
    at, samples = laid_out(r, held)
    at, stored = at.transpose(order), samples.transpose(order)
    coords = [[*c, *[0] * (rank - outer)] for c in np.ndindex(*shape[:outer])]
    if [row["coords"] for row in rows] != coords:
        return [*said, f"{len(rows)} rows, not one for each of {units} units in order"]
    for row in rows:
        c = tuple(row["coords"][:outer])
        if row["element"] is None or any(row[x] is not None for x in ("member", "ranges", "chain", "skips")):
            said.append(f"unit {c}: the row {row}")
            continue
        # its element: the memory block liffile finds
        if inp.where(row["element"]) != (key, *r.block):
            said.append(f"unit {c}: an element at {inp.where(row['element'])}, liffile's block at {r.block}")
        # its place: from where the increments put its first sample, as many bytes as its samples
        place = (key, int(at[c].flat[0]), stored[c].size * item)
        whole = place[1:] == r.block
        if inp.unit(row) != place or (row["range"] is None) != whole:
            said.append(f"unit {c} at {inp.unit(row)} (range {row['range']}), the increments give {place}")
            continue
        seen["units that are all of a block" if whole else "units that are a range of a block"] += 1
        if inp.held(place) != stored[c].tobytes():
            said.append(f"unit {c} holds other bytes than the samples the increments place")
        seen["units decoded"] += 1
    seen["blocks with bytes that are no unit's"] += stored.size * item < r.block[1]

    # liffile's array, its axes put in storage order
    seen["images liffile gives as RGB, stored BGR"] += r.bgr
    if r.values is None:
        seen["images liffile refuses"] += 1
    elif r.positions is None or sorted(r.positions) != list(range(rank)):
        seen["images whose channels liffile spreads over several axes"] += 1
    else:
        theirs = r.values.transpose([r.positions.index(j) for j in order])
        if theirs.dtype != r.dtype or theirs.shape != shape or theirs.tobytes() != stored.tobytes():
            seen["images liffile reads other bytes of"] += 1
            return said
        seen["images liffile reads as described"] += 1
        if list(r.positions) != order:
            # two axes of one increment, which liffile orders the other way: of one element
            seen["images whose axes of one element liffile orders otherwise"] += 1
            if [j for j in r.positions if lengths[j] > 1] != [j for j in order if lengths[j] > 1]:
                said.append(f"liffile's axes {r.positions}, the description's {order}")
    return said


def check_written(inp: Input, k: int, name: str, wrote: np.ndarray, fastest_first: str, seen: Counter) -> list[str]:
    """What differs between description `k`, of an image liffile does not list, and the array the
    fixture's writer wrote, from its slowest axis to its fastest: one unit, of its bytes. The
    writer packs the axes of `fastest_first` in that order, `c` where the channels are, and lists
    the dimensions in it."""
    d = inp.d
    a = d.stored.arrays[k]
    rows = d.rows.get(k, [])
    said = check_key(inp, k, None, name, k)
    seen["images"] += 1
    seen["images liffile does not list, compared with the bytes written"] += 1
    # each storage axis's position in the source's list: a dimension's, or after them the channels'
    listed = fastest_first.replace("c", "")
    order = tuple(len(listed) if x == "c" else listed.index(x) for x in reversed(fastest_first))
    if a.axes != (None if order == tuple(range(len(order))) else order):
        said.append(f"axes {a.axes}; the writer's order gives {order}")
    if a.shape != wrote.shape or a.item_bits != 8 * wrote.dtype.itemsize:
        said.append(f"shape {a.shape} of {a.item_bits} bits; the writer's {wrote.shape} of {wrote.dtype}")
    if a.grid != Grid(tuple(Each(n) for n in wrote.shape), "short") or len(rows) != 1:
        return [*said, f"grid {a.grid} and {len(rows)} rows, not one unit"]
    if inp.held(inp.unit(rows[0])) != wrote.tobytes():
        said.append("the unit holds other bytes than the writer wrote")
    seen["units"] += 1
    seen["units decoded"] += 1
    return said


def project_reads(path: Path, inp: Input) -> dict[str, Read]:
    """liffile's reading of each LOF that a document of the project at `path` names as an image's
    one `Block`, by the LOF's key. Each XLIF document is read on its own: liffile follows a cycle
    of documents forever."""
    out: dict[str, Read] = {}
    for key in inp.keys.values():
        if not key.endswith(".xlif"):
            continue
        with liffile.LifFile(path.parent / key, squeeze=False) as f:
            for im in f.images:
                lof = im._lof_reference if isinstance(im, liffile.LifImage) else None
                if lof is None:
                    continue
                (frame,) = im.memory_block.frames
                (image,) = lof.images
                assert isinstance(image, liffile.LifImage)
                found = next(k for k in inp.keys.values() if (path.parent / k).samefile(lof.filepath))
                out[found] = read(image, frame.offset)
    return out


@cache
def compared(path: Path) -> tuple[tuple[str, ...], Counter]:
    """What differs between the descriptions of the input at `path` and liffile's reading of it,
    and what the comparison saw."""
    inp = read_input(path)
    arrays = inp.d.stored.arrays
    seen: Counter = Counter()
    said = ["descriptions dropped for room"] if inp.d.stored.undescribed else []
    if path.suffix in DOCUMENTS:
        # a project: an image for each LOF a document names as an image's, keyed in the LOF
        seen["projects"] += 1
        reads = project_reads(path, inp)
        keyed = [inp.where(a.element)[0] for a in arrays]
        if sorted(map(str, keyed)) != sorted(reads):
            said.append(f"descriptions in {keyed}; liffile reads images of the LOFs {sorted(reads)}")
        for k, key in enumerate(keyed):
            if isinstance(key, str) and key in reads:
                seen["images of a project's LOF"] += 1
                said += [f"{key}: {s}" for s in check(inp, k, key, key, 0, reads[key], seen)]
        return tuple(said), seen
    seen["files"] += 1
    seen["LOFs"] += path.suffix == ".lof"
    seen["LIFEXT files"] += path.suffix == ".lifext"
    with liffile.LifFile(path, squeeze=False) as f:
        reads = [read(im) for im in f.images if isinstance(im, liffile.LifImage)]
    # the images liffile does not list (those of a LIFEXT file's later ChildrenOf), as written
    wrote = accepted_lifext()[path.stem].images() if path.stem in accepted_lifext() else []
    images = max(len(reads), len(wrote))
    if len(arrays) != images:
        said.append(f"{len(arrays)} descriptions of {images} images, of which liffile lists {len(reads)}")
    for k in range(min(len(arrays), images)):
        if k < len(reads):
            found = check(inp, k, None, path.name, k, reads[k], seen)
        else:
            found = check_written(inp, k, path.name, wrote[k].array(), wrote[k].order, seen)
        said += [f"image {k}: {s}" for s in found]
    return tuple(said), seen


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_lif_images_are_described_as_liffile_reads_them(path):
    """Each image of the input is described as its increments lay it out, its units where
    liffile's memory block and the increments put them, and its samples those liffile reads."""
    said, seen = compared(path)
    assert said == ()
    assert seen["units decoded"] == seen["units"]
    unlisted = seen["images liffile does not list, compared with the bytes written"]
    assert seen["images liffile reads as described"] + unlisted == seen["images"]


def test_the_fixtures_hold_every_case_the_comparison_reads():
    """The fixtures reach each case of the comparison, so that none of it passes for want of a
    file. Every image of theirs but one is read by liffile as it is described; the one is of the
    second `ChildrenOf` of `edge_lif_lifext_two_parents.lifext`, which liffile does not list. The
    units that are a range of a block are the padded rows of two images."""
    seen: Counter = Counter()
    for path in FILES:
        seen += compared(path)[1]
    assert {k: v for k, v in seen.items() if v} == {
        "files": 24,
        "LOFs": 4,
        "LIFEXT files": 2,
        "projects": 8,
        "images": 37,
        "images of a project's LOF": 9,
        "images of 8 bits a sample": 23,
        "images of 16 bits a sample": 10,
        "images of 32 bits a sample": 3,
        "images of channels in planes": 7,
        "images of interleaved channels": 6,
        "images of one unit": 34,
        "images of several units": 2,
        "units": 16424,
        "units that are all of a block": 34,
        "units that are a range of a block": 16389,
        "blocks with bytes that are no unit's": 2,
        "units decoded": 16424,
        "images liffile reads as described": 36,
        "images liffile gives as RGB, stored BGR": 4,
        "images liffile does not list, compared with the bytes written": 1,
    }


def image_xml(dims: list[tuple[int, int, int]], channels: list[tuple[int, int]], *, data_type: int = 0,
              resolution: int = 8) -> str:
    """An image's `Data`: its dimensions, each (DimID, NumberOfElements, BytesInc), and its
    channels, each (BytesInc, ChannelTag), of `resolution` bits and the type `data_type`."""
    listed = "".join(
        "<ChannelDescription" + attrs(
            DataType=data_type, ChannelTag=tag, Resolution=resolution, NameOfMeasuredQuantity="", Min="0.000000e+000",
            Max="1.000000e+000", Unit="", LUTName="Gray", IsLUTInverted=0, BytesInc=o, BitInc=0) + "/>"
        for o, tag in channels)
    described_as = "".join(
        "<DimensionDescription" + attrs(
            DimID=i, NumberOfElements=n, Origin="0.000000e+000", Length="0.000000e+000", Unit="", BitInc=0,
            BytesInc=s) + "/>"
        for i, n, s in dims)
    return (f"<Data><Image{attrs(TextDescription='')}><ImageDescription><Channels>{listed}</Channels>"
            f"<Dimensions>{described_as}</Dimensions></ImageDescription><TimeStampList{attrs(NumberOfTimeStamps=0)}/>"
            "</Image></Data>")


def element_xml(name: str, data: str, size: int) -> str:
    """An image's element: its `Data`, and the `Memory` that names its block, of `size` bytes."""
    return (f"<Element{attrs(Name=name, Visibility=1, CopyOption=1, UniqueID=uid(name))}>{data}"
            f"<Memory{attrs(Size=size, MemoryBlockID='MemBlock_1')}/><Children/></Element>")


def block_bytes(size: int, seed: int) -> bytes:
    """A memory block's data: `size` bytes, no two runs of them alike."""
    return np.random.default_rng(seed).integers(0, 256, size, dtype=np.uint8).tobytes()


def write_lif(directory: Path, name: str, dims: list[tuple[int, int, int]], channels: list[tuple[int, int]], size: int,
              *, data_type: int = 0, resolution: int = 8) -> Path:
    """A LIF file `name` in `directory` of one image, in a memory block of `size` bytes; its path."""
    path = directory / f"{name}.lif"
    data = image_xml(dims, channels, data_type=data_type, resolution=resolution)
    element = element_xml(name, data, size)
    path.write_bytes(Lif(path.name, [(element, "MemBlock_1", block_bytes(size, size))]).build())
    return path


def write_project(directory: Path, size: int, offset: int) -> Path:
    """A project in `directory` of one image of 3 rows of 4 samples, in a LOF whose block has 20
    bytes, which the image's document names by a `Block` of `size` bytes at `offset`; the path
    of its named document."""
    data = image_xml([(X, 4, 1), (Y, 3, 4)], [(0, 0)])
    (directory / "set").mkdir()
    (directory / "set" / "Solo.lof").write_bytes(Lof((element_xml("Solo", data, 20), block_bytes(20, 3))).build())
    memory = block_memory(written("./Solo.lof"), size, offset)
    inner = data.removeprefix("<Data>").removesuffix("</Data>")
    (directory / "set" / "Solo.xlif").write_bytes(document("Solo", inner, memory))
    (directory / "set.xlef").write_bytes(experiment("set", (written("./set/Solo.xlif"), uid("Solo"))))
    return directory / "set.xlef"


def write_nested_project(directory: Path) -> Path:
    """A project in `directory` of two images, each in a LOF of its own: its named document names
    a collection, whose one image, `Deep`, is so the first its documents name, and then the image
    `Flat`, whose LOF is found first, since the documents of a level are read before those they
    name; the path of its named document."""
    images = {"Deep": ([(X, 4, 1), (Y, 3, 4)], 8, 12), "Flat": ([(X, 5, 2), (Y, 2, 10)], 16, 20)}
    (directory / "nested").mkdir()
    for name, (dims, resolution, size) in images.items():
        data = image_xml(dims, [(0, 0)], resolution=resolution)
        lof = Lof((element_xml(name, data, size), block_bytes(size, size)))
        (directory / "nested" / f"{name}.lof").write_bytes(lof.build())
        inner = data.removeprefix("<Data>").removesuffix("</Data>")
        memory = block_memory(written(f"./{name}.lof"), size, 0)
        (directory / "nested" / f"{name}.xlif").write_bytes(document(name, inner, memory))
    (directory / "nested" / "Group.xlcf").write_bytes(collection("Group", (written("./Deep.xlif"), uid("Deep"))))
    children = ((written("./nested/Group.xlcf"), uid("Group")), (written("./nested/Flat.xlif"), uid("Flat")))
    (directory / "nested.xlef").write_bytes(experiment("nested", *children))
    return directory / "nested.xlef"


def test_images_no_fixture_holds_are_described_as_their_increments_say(tmp_path):
    """Cases no fixture holds, each compared as the fixtures are. liffile reads the samples of an
    image end to end from the start of its block, so of the images whose increments leave bytes
    between their samples, or before them, it reads other bytes."""
    one = [(0, 0)]
    cases = {
        # planes with bytes between them: a unit a plane
        "planes_apart": ([(X, 4, 1), (Y, 3, 4), (Z, 2, 20)], one, 32, {}),
        # rows of one channel with bytes between them, which liffile does not look for
        "rows_apart": ([(X, 4, 1), (Y, 3, 6)], one, 18, {}),
        # channels unevenly apart, the first not at the block's start: a unit a channel
        "channels_uneven": ([(X, 4, 1), (Y, 3, 4)], [(5, 0), (17, 0), (40, 0)], 52, {}),
        # channels evenly apart, with bytes between them
        "channels_apart": ([(X, 4, 1), (Y, 3, 4)], [(0, 0), (20, 0)], 32, {}),
        # one channel that starts past the block's start
        "channel_offset": ([(X, 4, 1), (Y, 3, 4)], [(7, 0)], 19, {}),
        # channels the XML lists in the other order than their bytes
        "channels_listed_last_first": ([(X, 4, 1), (Y, 3, 4)], [(12, 0), (0, 0)], 24, {}),
        # two dimensions of one element and one increment: the later listed is the slower
        "one_element_alike": ([(X, 4, 1), (Y, 3, 4), (Z, 1, 12), (T, 1, 12)], one, 12, {}),
        # a dimension of one element and no increment, listed between the others: the fastest
        "one_element_no_step": ([(X, 4, 1), (Z, 1, 0), (Y, 3, 4)], one, 12, {}),
        # the slowest dimension listed first: stored in the source's order, so no `axes`
        "slowest_first": ([(Y, 3, 4), (X, 4, 1)], one, 12, {}),
        # a block with bytes after the image: one unit, a range of the block's data
        "bytes_after": ([(X, 4, 1), (Y, 3, 4)], one, 20, {}),
        # interleaved BGR samples in planes
        "bgr_planes": ([(X, 4, 3), (Y, 3, 12), (Z, 2, 36)], [(0, 3), (1, 2), (2, 1)], 72, {}),
        # interleaved samples of two bytes
        "interleaved_16": ([(X, 4, 4), (Y, 3, 16)], [(0, 0), (2, 0)], 48, {"resolution": 12}),
        # samples of eight bytes, floats, over tiles and times
        "float_64": ([(X, 2, 8), (Y, 2, 16), (M, 3, 32), (T, 2, 96)], one, 192, {"data_type": 1, "resolution": 64}),
    }
    seen: Counter = Counter()
    for name, (dims, channels, size, kind) in cases.items():
        said, saw = compared(write_lif(tmp_path, name, dims, channels, size, **kind))
        assert said == (), name
        seen += saw
    assert {k: v for k, v in seen.items() if v} == {
        "files": 13,
        "images": 13,
        "images of 8 bits a sample": 11,
        "images of 16 bits a sample": 1,
        "images of 64 bits a sample": 1,
        "images stored in the order the source lists": 1,
        "images of channels in planes": 3,
        "images of interleaved channels": 2,
        "images of one unit": 9,
        "images of several units": 4,
        "units": 19,
        "units that are all of a block": 7,
        "units that are a range of a block": 12,
        "blocks with bytes that are no unit's": 6,
        "units decoded": 19,
        "images liffile reads as described": 8,
        "images whose axes of one element liffile orders otherwise": 1,
        "images liffile gives as RGB, stored BGR": 1,
        "images liffile reads other bytes of": 5,
    }


def test_a_project_s_image_starts_at_its_block_s_offset(tmp_path):
    """A project's document names its image's bytes by a `Block` of a LOF with an `Offset`, which
    counts into the LOF's memory block: the description's unit starts there. liffile reads the
    LOF's block from its start."""
    size, offset = 12, 5
    path = write_project(tmp_path, size, offset)
    said, seen = compared(path)
    assert said == ()
    inp = read_input(path)
    (row,) = inp.d.rows[0]
    assert inp.unit(row) == ("set/Solo.lof", 62 + offset, size)
    assert {k: v for k, v in seen.items() if v} == {
        "projects": 1,
        "images": 1,
        "images of a project's LOF": 1,
        "images of 8 bits a sample": 1,
        "images of one unit": 1,
        "units": 1,
        "units that are a range of a block": 1,
        "blocks with bytes that are no unit's": 1,
        "units decoded": 1,
        "images liffile reads other bytes of": 1,
    }


def test_a_lof_s_image_that_no_image_s_document_names_is_described(tmp_path):
    """A document that is no image may name a LOF that holds one. The parser reads the LOF, so its
    image is described, as liffile reads it of the LOF; the profile presents no image of it, and
    liffile lists none of the project."""
    data = image_xml([(X, 4, 1), (Y, 3, 4)], [(0, 0)])
    (tmp_path / "set").mkdir()
    lof = tmp_path / "set" / "Solo.lof"
    lof.write_bytes(Lof((element_xml("Solo", data, 12), block_bytes(12, 3))).build())
    memory = block_memory(written("./Solo.lof"), 12, 0)
    (tmp_path / "set" / "List.xlif").write_bytes(document("Solo", "<SimpleListInMemory />", memory))
    named = tmp_path / "set.xlef"
    named.write_bytes(experiment("set", (written("./set/List.xlif"), uid("Solo"))))
    inp = read_input(named)
    assert project_reads(named, inp) == {}
    assert inp.d.out.summary["series"] == 0
    with liffile.LifFile(lof, squeeze=False) as f:
        (im,) = f.images
        assert isinstance(im, liffile.LifImage)
        r = read(im)
    seen: Counter = Counter()
    assert len(inp.d.stored.arrays) == 1
    assert check(inp, 0, "set/Solo.lof", "set/Solo.lof", 0, r, seen) == []
    assert (seen["units decoded"], seen["images liffile reads as described"]) == (1, 1)


def no_grid(path: Path, shape: tuple[int, ...], axes: tuple[int, ...], case: str) -> Read:
    """Asserts that the one image of the file at `path` is described with `shape` and `axes` and
    no grid, for the reason `case`, though the profile presents it; liffile's reading of it."""
    said, seen = compared(path)
    assert said == ()
    assert seen[case] == 1
    d = described(path)
    (a,) = d.stored.arrays
    assert (a.shape, a.axes, a.item_bits, a.grid, a.chains) == (shape, axes, 8, None, ())
    assert d.rows == {}
    assert d.stored.chains == ()
    assert d.out.summary["series"] == 1
    with liffile.LifFile(path, squeeze=False) as f:
        (im,) = f.images
        assert isinstance(im, liffile.LifImage)
        return read(im)


def test_a_dimension_of_no_increment_has_no_grid(tmp_path):
    """A dimension of several elements and a `BytesInc` of 0 repeats a plane: its samples share
    bytes, so the image is no array of distinct elements. liffile refuses it: it counts a plane
    for each element, more bytes than the block has."""
    path = write_lif(tmp_path, "repeat", [(X, 4, 1), (Y, 3, 4), (T, 5, 0)], [(0, 0)], 12)
    assert no_grid(path, (3, 4, 5), (1, 0, 2), "images whose axes do not nest").values is None


def test_a_dimension_that_overlaps_the_faster_ones_has_no_grid(tmp_path):
    """A dimension that steps by fewer bytes than the faster ones span: its planes overlap.
    liffile refuses it, as it does a repeated plane."""
    path = write_lif(tmp_path, "overlap", [(X, 4, 1), (Y, 3, 4), (Z, 2, 11)], [(0, 0)], 23)
    assert no_grid(path, (2, 3, 4), (2, 1, 0), "images whose axes do not nest").values is None


def test_channels_that_overlap_have_no_grid(tmp_path):
    """Channels whose offsets are closer than a plane is long. liffile refuses them."""
    path = write_lif(tmp_path, "channels", [(X, 4, 1), (Y, 3, 4)], [(0, 0), (6, 0)], 18)
    assert no_grid(path, (2, 3, 4), (2, 1, 0), "images whose axes do not nest").values is None


def test_channels_unevenly_apart_around_a_dimension_have_no_grid(tmp_path):
    """Channels unevenly apart, with a dimension's steps between them: no sample shares a byte, but
    the planes lie in an order that neither the channels within the dimension nor the dimension
    within the channels gives, so the axes do not nest. liffile refuses it: the dimension steps by
    no whole number of planes."""
    path = write_lif(tmp_path, "around", [(X, 4, 1), (Y, 3, 4), (Z, 2, 50)], [(0, 0), (12, 0), (100, 0)], 162)
    assert no_grid(path, (2, 3, 3, 4), (2, 3, 1, 0), "images whose axes do not nest").values is None


def test_an_image_whose_rows_pass_the_maps_limit_has_no_grid(tmp_path):
    """An image cut into more units than the file's chunk map has room for keeps none of its rows
    (spec/stored-array.md §6 <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#6-in-the-ir>):
    2^19 rows of one sample each, a byte apart, of 16 axes, are 17 entries a row, and the limit is
    2^22 entries and 4 for each of the file's bytes, which are 2 a row. liffile reads the rows end
    to end. The same rows of 15 axes, 16 entries a row, are within the limit by the 4 entries for
    each byte of the file that is not the block's: they are units, every one compared."""
    rows = 1 << 19
    dims = [(X, 1, 1), (Y, rows, 2), *[(Z, 1, 2 * rows)] * 14]
    path = write_lif(tmp_path, "rows", dims, [(0, 0)], 2 * rows)
    shape = (*[1] * 14, rows, 1)
    r = no_grid(path, shape, (*range(15, 0, -1), 0), "images whose rows pass the map's limit")
    assert r.values is not None
    assert r.values.shape == shape
    within = write_lif(tmp_path, "rows_within", dims[:-1], [(0, 0)], 2 * rows)
    said, seen = compared(within)
    assert said == ()
    assert (seen["images of several units"], seen["units decoded"]) == (1, rows)
