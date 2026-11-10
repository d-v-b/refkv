"""The VSI profile's arrays against the stored arrays' descriptions (spec/stored-array.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): what the profile
writes of an image's storage, read off the image's descriptions by the profile's rules
(spec/zarrify/vsi.md §4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#4-the-images>
and §9.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#94-chunks>), is what the
profile wrote.

Of a pyramid, whose levels are a tile file's descriptions, the test derives each level's array
document and every chunk:

- the data type from the pixel type of the ETS header, which is the descriptions' declaration,
  of the descriptions' sample size;
- level 0's size from its description's shape, and each later level's by Bio-Formats' halving
  rule, in which how far the level's tiles reach is its description's shape;
- the axes from the sizes of the description's axes before the rows, and the samples' axis;
- the chunk shape from the grid; the codecs from the chain, and, for a `jpeg` chain, which
  stands for JPEG and for lossless JPEG, from the header's `compression`;
- the fill value from the description's, or, where the description gives none, from the
  background of the header, its declaration;
- a chunk for each row whose tile lies inside the size the profile gives its level, of the
  row's bytes.

Of an IFD image, from its IFD's description: the shape with the samples first, the data type
from the sample size, the one chunk of the image, the codecs from the chain, and the chunk's
parts: a JPEG strip with the Adobe marker, whose transform byte PhotometricInterpretation (a
declaration) gives, and the body of the chain's `tables` spliced in after its first two bytes;
or the runs of contiguous strips.

Three things are taken from outside the descriptions. Which image each description is: an
image's descriptions are found by its chunks' bytes, in the rows that hold them. Which of a
tile file's axes is the time's, the channels' or the depth's: a description has the records'
coordinates, and what each means is in the `.vsi` file's tree, which the test reads. And the
default fill value of an IFD image, 0, which is the profile's. Which IFD images the profile
leaves out is not derived either: the descriptions the profile writes no array of are counted."""

import json
import math
import struct
from collections import Counter
from functools import cache
from pathlib import Path

import numpy as np
import pytest
from test_vsi_stored import (
    BACKGROUND,
    BMP,
    FILES,
    JPEG,
    JPEG2000,
    LOSSLESS_JPEG,
    PIXEL_TYPES,
    PNG,
    Slide,
    Stack,
    lengths,
    read_slide,
    stacks,
    write_fixtures,
    written,
)

from refkv._narrow import JSON, at, integer, mapping, string
from refkv.ir.store import StoreIrOutput
from refkv.ir.stored import ElementPlace, Given, Source, StoredArray, Value

type Piece = tuple[str, int, int] | bytes
"""a part of a reference: (object key, offset, length) of a file of the slide, or bytes no file
holds there (a data source's)"""
type Chunks = dict[tuple[int, ...], list[Piece]]
"""an array's chunks, by their coordinates"""

# the codec the profile names for each compression of an ETS header
CODECS = {JPEG: "imagecodecs_jpeg", JPEG2000: "imagecodecs_jpeg2k", LOSSLESS_JPEG: "imagecodecs_ljpeg",
          PNG: "imagecodecs_png", BMP: "imagecodecs_bmp"}
# the Adobe marker a JPEG strip of three samples is given, before its transform byte
ADOBE = b"\xff\xee\x00\x0eAdobe\x00\x64\x00\x00\x00\x00"
SOI, EOI = b"\xff\xd8", b"\xff\xd9"


def pieces(out: StoreIrOutput, parts: list) -> list[Piece]:
    """A reference's parts, those that are no range of a file joined where they meet."""
    got: list[Piece] = []
    for p in parts:
        if isinstance(p, bytes):
            piece: Piece = p
        elif p[0] == "object":
            piece = (p[1], p[2], p[3])
        else:
            piece = out.data[p[0] - 1][p[1]:p[1] + p[2]]
        last = got[-1] if got else None
        if isinstance(piece, bytes) and isinstance(last, bytes):
            got[-1] = last + piece
        else:
            got.append(piece)
    return got


def halved(before: int, reach: int) -> int:
    """A level's width or height from the level before's, as Bio-Formats computes it (§4.1
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#41-the-pyramid>): its half,
    plus 1 where the level before is odd and the half is less than `reach`, how far the level's
    tiles reach, and else at most `reach`."""
    size = before // 2
    return size + 1 if before % 2 == 1 and size < reach else min(size, reach)


def transposed(names: list[str]) -> list[JSON]:
    """The codec that puts the samples last of the axes `names`."""
    order: list[JSON] = [*(i for i, n in enumerate(names) if n != "c"), names.index("c")]
    return [{"name": "transpose", "configuration": {"order": order}}]


def bytes_codec(size: int) -> dict[str, JSON]:
    """The codec of uncompressed samples of `size` bytes: little-endian, where that says anything."""
    return {"name": "bytes", "configuration": {"endian": "little"}} if size > 1 else {"name": "bytes"}


def document(doc: dict[str, JSON]) -> dict[str, JSON]:
    """What an array's document says of its storage."""
    return {"shape": doc["shape"], "chunk_shape": at(doc["chunk_grid"], "configuration", "chunk_shape"),
            "data_type": doc["data_type"], "fill_value": doc["fill_value"], "codecs": doc["codecs"],
            "dimension_names": doc["dimension_names"]}


def check_pyramid(slide: Slide, levels: list[int], stack: Stack, docs: dict[int, dict[str, JSON]],
                  chunks: dict[int, Chunks], seen: Counter) -> list[str]:
    """A pyramid's arrays, by level, and their chunks against the descriptions `levels` of its
    tile file, whose stack in the tree is `stack`."""
    d = slide.d
    arrays = [d.stored.arrays[k] for k in levels]
    first = arrays[0]
    if first.shape is None or first.axes is None or first.grid is None or first.item_bits is None:
        return ["level 0 has no grid"]
    if len(docs) != len(arrays):
        return [f"{len(docs)} arrays, {len(arrays)} descriptions"]
    header = slide.placed(first.declarations[0])
    pixel_type, _, _, compression = struct.unpack_from("<4i", header, 8)
    dtype = np.dtype(PIXEL_TYPES[pixel_type])
    # the samples' axis is the one the header counts apart, after the records' coordinates
    samples = first.shape[-1] if first.axes[-1] == len(first.axes) - 1 else 1
    lead = len(first.shape) - 2 - (samples > 1)
    # an axis before the rows is the records' coordinate `axes` gives it
    axis = {m: i for i in range(lead) for m, c in stack.coordinate.items() if first.axes[i] == c}
    size = {m: first.shape[i] for m, i in axis.items()}
    present = {"t": size.get("t", 1) > 1, "c": size.get("c", 1) > 1 or samples > 1, "z": size.get("z", 1) > 1}
    names = [n for n in ("t", "c", "z") if present[n]] + ["y", "x"]
    seen["pyramids"] += 1
    seen["pyramids of an axis before the rows"] += len(names) > 2 + (samples > 1)
    # the codecs: the samples last, then what the chain and the header's compression name
    chain = d.stored.chains[first.chains[0]]
    if not chain:
        codec = bytes_codec(dtype.itemsize)
    elif chain[0].name == "source":
        identifier = chain[0].parameters["id"]
        assert isinstance(identifier, Value)
        codec = {"name": CODECS[integer(identifier.value)]}
    else:
        codec = {"name": CODECS[compression if chain[0].name == "jpeg" else JPEG2000]}
    seen[f"pyramids of {codec['name']}"] += 1
    codecs = [*(transposed(names) if samples > 1 else []), codec]
    # the fill value: the background's first sample
    fill = first.fill.param if isinstance(first.fill, Given) else None
    seen["fill values from the description's" if fill is not None else "fill values from the declaration"] += 1
    declared_fill = header[BACKGROUND:BACKGROUND + dtype.itemsize]
    sample = slide.placed(fill.place) if isinstance(fill, Source) else declared_fill
    value = np.frombuffer(sample, dtype)[0]
    fill_value: JSON = (float(value) if math.isfinite(value) else 0) if dtype.kind == "f" else int(value)
    tile = lengths(first)[lead:lead + 2]
    height, width = first.shape[lead:lead + 2]
    said = []
    for level, a in enumerate(arrays):
        if level:
            # a level that holds no tile is halved as one of a single tile
            reach = a.shape[lead:lead + 2] if a.shape is not None else tile
            seen["levels of no tile, sized as of one"] += a.shape is None
            height, width = halved(height, reach[0]), halved(width, reach[1])
            seen["levels smaller than their tiles reach"] += (height, width) != reach
        seen["levels"] += 1
        along = {"t": size.get("t", 1), "c": samples if samples > 1 else size.get("c", 1), "z": size.get("z", 1),
                 "y": height, "x": width}
        one = {"t": 1, "c": samples, "z": 1, "y": tile[0], "x": tile[1]}
        want = {"shape": [along[n] for n in names], "chunk_shape": [one[n] for n in names],
                "data_type": dtype.name, "fill_value": fill_value, "codecs": codecs, "dimension_names": names}
        if document(docs[level]) != want:
            said.append(f"level {level} is {document(docs[level])}, the description gives {want}")
        if a.item_bits != 8 * np.dtype(string(docs[level]["data_type"])).itemsize:
            said.append(f"level {level}: item_bits {a.item_bits}, the profile's {docs[level]['data_type']}")
        wanted: Chunks = {}
        for row in d.rows.get(levels[level], []):
            coords = row["coords"]
            line, column = coords[lead:lead + 2]
            # a tile outside the size the profile gives its level is no chunk
            if line >= math.ceil(height / tile[0]) or column >= math.ceil(width / tile[1]):
                seen["tiles outside the level the profile writes"] += 1
                continue
            where = {"t": coords[axis["t"]] if "t" in axis else 0, "z": coords[axis["z"]] if "z" in axis else 0,
                     "c": coords[axis["c"]] if "c" in axis and samples == 1 else 0, "y": line, "x": column}
            chunk = tuple(where[n] for n in names)
            if chunk in wanted:
                said.append(f"level {level}: the tile at {coords} is the chunk {chunk}, as another tile is")
            wanted[chunk] = [slide.part(row)]
            seen["chunks of a tile"] += 1
        got = chunks.get(level, {})
        if set(got) != set(wanted):
            said.append(f"level {level}: chunks at {sorted(set(got) - set(wanted))} alone, and none at "
                        f"{sorted(set(wanted) - set(got))}")
        said += [f"level {level}: chunk {c} is {got[c]}, not {parts}" for c, parts in sorted(wanted.items())
                 if c in got and got[c] != parts]
    return said


def declared(slide: Slide, a: StoredArray, tag: int) -> int | None:
    """The first value of the declaration of an IFD's image that is the field `tag`'s, a SHORT."""
    for place in a.declarations:
        assert isinstance(place, ElementPlace)
        if slide.d.ir.path(place.element).endswith(f"/tags/{tag}/value"):
            return struct.unpack_from("<H", slide.placed(place))[0]
    return None


def check_ifd_image(slide: Slide, k: int, doc: dict[str, JSON], chunks: Chunks, seen: Counter) -> list[str]:
    """An IFD image's one array and its one chunk against the description `k` of its IFD's image."""
    d = slide.d
    a = d.stored.arrays[k]
    if a.grid is None or a.shape is None or a.item_bits is None:
        return ["a description of no grid"]
    samples = a.shape[2] if len(a.shape) == 3 else 1
    names = ["c", "y", "x"] if samples > 1 else ["y", "x"]
    shape = [samples, *a.shape[:2]] if samples > 1 else list(a.shape)
    chain = d.stored.chains[a.chains[0]]
    rows = d.rows.get(k, [])
    seen["IFD images"] += 1
    if [c.name for c in chain] == ["jpeg"]:
        codec: dict[str, JSON] = {"name": "imagecodecs_jpeg"}
        # the strip, with the Adobe marker for three samples and the tables' body after its SOI
        (row,) = rows
        key, offset, length = slide.part(row)
        photometric = declared(slide, a, 262)
        rgb = (2 if samples == 3 else 1) if photometric is None else photometric
        prefix = ADOBE + bytes([0 if rgb == 2 else 1]) if samples == 3 else b""
        tables = chain[0].parameters.get("tables")
        if isinstance(tables, Source):
            held = slide.placed(tables.place)
            prefix += held[2:-2] if len(held) >= 4 and (held[:2], held[-2:]) == (SOI, EOI) else b""
        framed = bool(prefix) and length > 2
        seen["IFD images of a JPEG strip with a prefix" if framed else "IFD images of a JPEG strip"] += 1
        parts: list[Piece] = [(key, offset, length)]
        if framed:
            parts = [(key, offset, 2), prefix, (key, offset + 2, length - 2)]
    elif not chain:
        codec = bytes_codec(a.item_bits // 8)
        # the strips in order, one range a run of contiguous strips
        parts = []
        for row in rows:
            key, offset, length = slide.part(row)
            last = parts[-1] if parts else None
            if isinstance(last, tuple) and (last[0], last[1] + last[2]) == (key, offset):
                parts[-1] = (key, last[1], last[2] + length)
            else:
                parts.append((key, offset, length))
        seen["IFD images of strips"] += 1
        seen["IFD images of several runs of strips"] += len(parts) > 1
    else:
        return [f"chain {[c.name for c in chain]}, which the profile presents nothing of"]
    # the fill value of an IFD image is the profile's default
    want = {"shape": shape, "chunk_shape": shape, "data_type": f"uint{a.item_bits}", "fill_value": 0,
            "codecs": [*(transposed(names) if samples > 1 else []), codec], "dimension_names": names}
    said = []
    if document(doc) != want:
        said.append(f"the array is {document(doc)}, the description gives {want}")
    if chunks != {(0,) * len(names): parts}:
        said.append(f"chunks {chunks}, not the one of {parts}")
    return said


@cache
def compared(path: Path) -> tuple[tuple[str, ...], Counter]:
    """What differs between the arrays the profile wrote of the slide at `path` and the slide's
    descriptions, and what the comparison saw."""
    slide = read_slide(path)
    d, out = slide.d, slide.d.out
    assert isinstance(out, StoreIrOutput), "a slide is a store's objects"
    said: list[str] = []
    seen: Counter = Counter()
    if d.stored.undescribed:
        said.append("descriptions dropped for room")
    # each row's bytes, with its description
    units = sorted((*slide.part(r), k) for k, rows in d.rows.items() for r in rows)
    by_id = {s.id: s for s in stacks(slide.files[path.name])}
    # the images: each group's arrays, by level, and their chunks
    docs: dict[int, dict[int, dict[str, JSON]]] = {}
    chunks: dict[int, dict[int, Chunks]] = {}
    for key, v in out.entries.items():
        image, _, rest = key.partition("/")
        level, _, name = rest.partition("/")
        if image.isdigit() and level.isdigit() and name == "zarr.json":
            docs.setdefault(int(image), {})[int(level)] = dict(mapping(json.loads(v), key))
    for key, parts in out.refs.items():
        image, level, c, *coords = key.split("/")
        if image.isdigit() and c == "c":
            chunks.setdefault(int(image), {}).setdefault(int(level), {})[tuple(map(int, coords))] = pieces(out, parts)
    presented: set[int] = set()
    for image, levels in sorted(docs.items()):
        seen["images"] += 1
        seen["arrays"] += len(levels)
        # the image's descriptions: those whose rows hold its chunks' bytes
        held = [p for level in chunks.get(image, {}).values() for parts in level.values() for p in parts
                if isinstance(p, tuple)]
        found = {k for key, offset, _ in held[:1] for unit, start, length, k in units
                 if unit == key and start <= offset < start + length}
        if len(found) != 1:
            said.append(f"image {image}: its chunks' bytes are in {len(found)} descriptions' rows")
            continue
        a = d.stored.arrays[found.pop()]
        own = sorted((b.index or 0, k) for k, b in enumerate(d.stored.arrays) if b.element == a.element)
        presented.update(k for _, k in own)
        if a.index is None:
            if sorted(levels) != [0]:
                said.append(f"image {image}: the arrays {sorted(levels)} of one IFD's image")
                continue
            found_said = check_ifd_image(slide, own[0][1], levels[0], chunks.get(image, {}).get(0, {}), seen)
        else:
            # the tile file's stack, which says what its coordinates mean
            stack = by_id[int(Path(slide.where(a.element)[0]).parent.name.removeprefix("stack"))]
            found_said = check_pyramid(slide, [k for _, k in own], stack, levels, chunks.get(image, {}), seen)
        said += [f"image {image}, {a.path}: {s}" for s in found_said]
    seen["descriptions the profile writes no array of"] += len(d.stored.arrays) - len(presented)
    return tuple(said), seen


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_the_vsi_profile_writes_what_the_descriptions_give(path):
    """Every array the profile writes of the slide, and every chunk of it, is what its image's
    descriptions give."""
    said, _ = compared(path)
    assert said == ()


def test_the_fixtures_hold_every_case_the_profile_is_compared_in():
    """The fixtures reach each case of the comparison, so that none of it passes for want of a
    file. The descriptions with no array are the stacks' thumbnails and the IFD images the
    profile leaves out."""
    seen: Counter = Counter()
    for path in FILES:
        seen += compared(path)[1]
    assert {k: v for k, v in seen.items() if v} == {
        "images": 47,
        "arrays": 70,
        "pyramids": 35,
        "pyramids of an axis before the rows": 2,
        "pyramids of bytes": 25,
        "pyramids of imagecodecs_jpeg": 2,
        "pyramids of imagecodecs_ljpeg": 2,
        "pyramids of imagecodecs_jpeg2k": 1,
        "pyramids of imagecodecs_png": 3,
        "pyramids of imagecodecs_bmp": 2,
        "fill values from the description's": 24,
        "fill values from the declaration": 11,
        "levels": 58,
        "levels smaller than their tiles reach": 23,
        "chunks of a tile": 288,
        "IFD images": 12,
        "IFD images of a JPEG strip with a prefix": 11,
        "IFD images of strips": 1,
        "descriptions the profile writes no array of": 48,
    }


def test_slides_no_fixture_holds_are_written_as_their_descriptions_give_them(tmp_path):
    """Images the fixtures do not have, each written here by the fixtures' generator: the profile
    writes of every one what its descriptions give."""
    gradient, image = write_fixtures.gradient, write_fixtures.TiffImage

    def jpeg(**how) -> object:
        return write_fixtures.macro_image(seed=31, w=16, h=8, **how)

    def short_of_the_half(tiles: list) -> list:
        # level 1 of 25 columns is 12, and its one tile reaches 8
        return [t for t in tiles if t[0][-1] == 0 or t[0][0] == 0]

    short, undefined = 3, 7  # TIFF's field types
    cases: dict[str, tuple[dict, dict[str, int]]] = {
        "a level whose tiles stop short of half the level before": (
            {"width": 25, "levels": 2, "edit": short_of_the_half}, {"levels": 2, "chunks of a tile": 9}),
        "a background that is no number, written as 0": (
            {"dtype": "float32", "background": struct.pack("<f", math.nan)}, {"fill values from the description's": 1}),
        "a JPEG strip of YCbCr": (
            {"macro": jpeg(extra=((262, short, [6]),), skip=(262,))}, {"IFD images of a JPEG strip with a prefix": 1}),
        "a JPEG strip of no PhotometricInterpretation": (
            {"macro": jpeg(skip=(262,))}, {"IFD images of a JPEG strip with a prefix": 1}),
        "tables that are no stream of tables": (
            {"macro": jpeg(extra=((347, undefined, b"no tables"),), skip=(347,))},
            {"IFD images of a JPEG strip with a prefix": 1}),
        "a JPEG strip of two bytes": (
            {"macro": jpeg(extra=((279, 4, [2]),), skip=(279,))}, {"IFD images of a JPEG strip": 1}),
        "a JPEG strip of one sample and no tables": (
            {"macro": jpeg(extra=((258, short, [8]), (277, short, [1])), skip=(258, 277, 347))},
            {"IFD images of a JPEG strip": 1}),
        "strips that follow one another": (
            {"macro": image(gradient(31, 8, 16, "uint8"), rows=2)},
            {"IFD images of strips": 1, "IFD images of several runs of strips": 0}),
        "strips apart": (
            {"macro": image(gradient(31, 4, 3, "uint8"), rows=1)},
            {"IFD images of strips": 1, "IFD images of several runs of strips": 1}),
    }
    for name, (how, want) in cases.items():
        folder = tmp_path / str(len(list(tmp_path.iterdir())))
        said, seen = compared(written(folder, **how))
        assert said == (), name
        assert {k: seen[k] for k in want} == want, name
        assert (seen["images"], seen["pyramids"], seen["IFD images"]) == (2, 1, 1), name


def test_a_tile_outside_the_size_the_profile_gives_its_level_is_a_row_and_no_chunk(tmp_path):
    """A later level is as large as its tiles reach, and the profile gives it Bio-Formats' size,
    half the level before's: a tile past that size is a row of the level's description, and no
    chunk of the profile's array."""

    def past_the_half(tiles: list) -> list:
        # level 1 of 24 columns is 12, two tiles of 8: a tile in a fourth column
        return [*tiles, ((3, 0, 1), tiles[0][1])]

    said, seen = compared(written(tmp_path, levels=2, edit=past_the_half))
    assert said == ()
    assert (seen["levels"], seen["chunks of a tile"], seen["tiles outside the level the profile writes"]) == (2, 8, 1)


@pytest.mark.xfail(strict=True, reason="the profile accepts a coordinate of several values that the stack has no "
                                       "dimension for, and writes one chunk for the tiles that differ only in it")
def test_tiles_that_differ_in_a_coordinate_the_stack_has_no_dimension_for_are_each_a_chunk(tmp_path):
    """A tile file whose header gives a coordinate two values, in a stack with no dimension of
    that coordinate: the description has an axis of 2 and a row for each of its 12 tiles. The
    profile checks the sizes of the stack's dimensions alone (spec/zarrify/vsi.md §9.3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#93-tiles-and-levels>), gives
    the array no axis for the coordinate, and so gives two tiles one chunk's key: it writes 6
    chunks, and its summary counts 12."""

    def doubled(tiles: list) -> list:
        return [*tiles, *(((*coords[:2], 1), data[::-1]) for coords, data in tiles)]

    path = written(tmp_path, extra_coords=1, edit=doubled, ets={"dims": [24, 16, 2]})
    (level,) = (a for a in read_slide(path).d.stored.arrays if a.index is not None)
    assert level.shape == (2, 16, 24)
    said, seen = compared(path)
    assert seen["chunks of a tile"] == 12
    assert said == ()


def test_a_level_of_no_tile_is_written_at_the_size_one_tile_would_give_it(tmp_path):
    """The profile writes an array for a level that holds no tile, below one that holds some,
    sized by the halving rule as though it held one tile; its description has no shape, and the
    arrays after it are what the descriptions and that rule give."""
    said, seen = compared(written(tmp_path, levels=3, edit=lambda tiles: [t for t in tiles if t[0][-1] != 1]))
    assert said == ()
    assert (seen["levels"], seen["levels of no tile, sized as of one"], seen["chunks of a tile"]) == (3, 1, 7)
