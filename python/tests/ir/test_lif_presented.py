"""The LIF profile's arrays against the stored arrays' descriptions (spec/stored-array.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): what the profile
writes of an image's storage, read off the image's description by the profile's rules
(spec/zarrify/lif.md §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#3-placement>,
§4.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#43-image-arrays> and §7.4
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#74-chunk-references>), is what the
profile wrote. Of a description, and of the XML its declaration holds, the test derives

- which axis each dimension is, by the `DimID` the declaration gives the dimension that `axes`
  says a storage axis is, and so the image's axes, its shape and its fields, a series each;
- whether the channels are interleaved: their axis is then faster than X's in storage order;
- the data type, by the declaration's `DataType` and the description's `item_bits`, and the
  codecs: a `transpose` for interleaved channels, and `bytes`;
- where each row of a plane is: the unit its samples are in, by the grid, and so the row of the
  chunk map that holds it, then the samples' place in the unit, which is in storage order;
- the band, the most rows a chunk holds, by the bytes of a row and, of rows with bytes between
  them, by the size of the references of the farthest band's rows; and so the chunk shape and
  every chunk, each the ranges of its rows, those that touch joined.

One thing is the profile's reading and is taken from its output: in a project, the order of the
images is the order its documents name one another, which no description holds, so each LOF's
image is found by the LOF that a series' chunks name. An image whose description has no grid is
presented from its declaration alone, and nothing of it is derived here."""

import json
import math
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pytest
from test_lif_stored import (
    DOCUMENTS,
    FILES,
    Input,
    M,
    Range,
    T,
    X,
    Y,
    Z,
    declared_images,
    read_input,
    write_lif,
    write_nested_project,
    write_project,
)

from refkv._narrow import JSON, at, mapping
from refkv.ir.stored import Each
from refkv.zarrify.common import Output

# the most bytes of a band of rows, and of the references of a chunk (spec/zarrify/lif.md §3.4
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#34-rows-and-bands>)
BAND = 1 << 24
PAYLOAD = 65519
# the DimID of an emission wavelength: a field dimension
WAVELENGTH = 5


def varint(v: int) -> int:
    """The bytes of a varint."""
    return max(1, math.ceil(v.bit_length() / 7))


def references(ranges: list[Range]) -> int:
    """The size of a chunk's references to `ranges` of one source (spec/zarrify.md §1.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>): a range is its offset
    and its length, each a varint after a tag, and each of several is an entry of a list, after
    the entry's tag and its length."""
    sizes = [2 + varint(offset) + varint(length) for _, offset, length in ranges]
    return sizes[0] if len(sizes) == 1 else sum(1 + varint(n) + n for n in sizes)


@dataclass(frozen=True)
class Image:
    """An image as its description and its declaration give it: the storage axis of each
    dimension that is X, Y, Z or T (None where the image has no such used dimension), of its
    channels and of each field dimension; the description's shape and each axis's unit length;
    the bytes of a sample; and where a unit is, by its coordinates."""

    x: int
    y: int | None
    z: int | None
    t: int | None
    c: int | None
    fields: tuple[int, ...]
    shape: tuple[int, ...]
    each: tuple[int, ...]
    item: int
    units: Callable[[tuple[int, ...]], Range]

    def length(self, axis: int | None) -> int:
        return 1 if axis is None else self.shape[axis]

    def sample(self, index: dict[int, int]) -> tuple[tuple[int, ...], int]:
        """The unit that holds the sample at `index` (by storage axis; 0 where not given), and
        the sample's position among the unit's samples, which are in storage order."""
        coords = [index.get(i, 0) for i in range(len(self.shape))]
        unit = tuple(c // n for c, n in zip(coords, self.each, strict=True))
        position = 0
        for c, n, length in zip(coords, self.each, self.shape, strict=True):
            position = position * min(n, length) + c % n
        return unit, position

    def run(self, index: dict[int, int], count: int) -> Range | str:
        """The bytes of `count` samples from the one at `index`, which the description must hold
        end to end in one unit: the profile references them as one range."""
        unit, first = self.sample(index)
        end = {**index, self.x: self.shape[self.x] - 1}
        if self.c is not None and self.c > self.x:
            end[self.c] = self.shape[self.c] - 1
        last_unit, last = self.sample(end)
        if last_unit != unit or last - first != count - 1:
            return f"the {count} samples from {index} are not end to end in one unit"
        key, offset, length = self.units(unit)
        if (first + count) * self.item > length:
            return f"the samples from {index} lie past their unit's {length} bytes"
        return key, offset + first * self.item, count * self.item


def image_of(inp: Input, k: int) -> tuple[Image, dict[str, JSON]] | str:
    """The image of description `k`, and its array's document less the chunk shape; or why
    nothing is derived of it."""
    a = inp.d.stored.arrays[k]
    if a.grid is None or a.shape is None or a.item_bits is None or a.index is None:
        return "no grid"
    dims, channels = declared_images(inp.held(inp.where(a.element)).decode("utf-16-le"))[a.index]
    order = a.axes if a.axes is not None else tuple(range(len(a.shape)))
    # the storage axis of each dimension, by its position in the XML, and of the channels
    axis = {j: i for i, j in enumerate(order)}
    ids = [int(e["DimID"]) for e in dims]
    used = [j for j in range(len(dims)) if a.shape[axis[j]] > 1]

    def of(dim_id: int) -> int | None:
        return next((axis[j] for j in used if ids[j] == dim_id), None)

    x = of(X)
    if x is None:
        x = axis[ids.index(X)]
    c = axis.get(len(dims))
    rows = {tuple(r["coords"]): r for r in inp.d.rows.get(k, [])}
    lengths = [u.length for u in a.grid.axes if isinstance(u, Each)]
    assert len(lengths) == len(a.shape), "a grid of units of one length an axis"

    def unit(coords: tuple[int, ...]) -> Range:
        return inp.unit(rows[coords])

    im = Image(x, of(Y), of(Z), of(T), c, tuple(axis[j] for j in used if ids[j] not in (X, Y, Z, T)), a.shape,
               tuple(lengths), a.item_bits // 8, unit)
    # the image's axes, those it has of t, c and z, then y and x
    names = [n for n, i in (("t", im.t), ("c", im.c), ("z", im.z)) if i is not None] + ["y", "x"]
    shape = [im.length(i) for i in (im.t, im.c, im.z) if i is not None] + [im.length(im.y), im.length(im.x)]
    interleaved = im.c is not None and im.c > im.x
    kinds = {e["DataType"] for e in channels}
    assert len(kinds) == 1, "channels of one type"
    codecs: list[JSON] = []
    if interleaved:
        # a chunk holds the channels last
        stored = [n for n in names if n != "c"] + ["c"]
        codecs.append({"name": "transpose", "configuration": {"order": [names.index(n) for n in stored]}})
    codecs.append({"name": "bytes", "configuration": {"endian": "little"}} if im.item > 1 else {"name": "bytes"})
    doc: dict[str, JSON] = {
        "shape": list(shape),
        "data_type": f"{'uint' if kinds == {'0'} else 'float'}{a.item_bits}",
        "codecs": codecs,
        "dimension_names": list(names),
    }
    return im, doc


def planes(im: Image) -> list[tuple[list[int], dict[int, int]]]:
    """An image's planes, in order: each one's chunk coordinates along t, c and z (those the
    image has), and the index of its samples along those axes. Interleaved channels are within a
    chunk, at c = 0."""
    interleaved = im.c is not None and im.c > im.x
    out = []
    for t in range(im.length(im.t)):
        for c in range(1 if interleaved else im.length(im.c)):
            for z in range(im.length(im.z)):
                coords = [v for v, i in ((t, im.t), (c, im.c), (z, im.z)) if i is not None]
                out.append((coords, {i: v for v, i in ((t, im.t), (c, im.c), (z, im.z)) if i is not None}))
    return out


def fields(im: Image) -> list[dict[int, int]]:
    """An image's fields, in order, the first field dimension the fastest: each one's index along
    the field dimensions' axes."""
    counts = [im.shape[i] for i in im.fields]
    out = []
    for f in range(math.prod(counts)):
        index, rest = {}, f
        for i, n in zip(im.fields, counts, strict=True):
            index[i], rest = rest % n, rest // n
        out.append(index)
    return out


def band_of(im: Image, seen: Counter) -> int | str:
    """The rows of a chunk: the largest divisor of the image's rows whose band is at most 2^24
    bytes, and, where rows have bytes between them, whose references at the farthest band (of
    the last time, channel, depth and field) are at most 65519 bytes; at least 1."""
    rows, samples = im.length(im.y), im.shape[im.x] * (im.shape[im.c] if im.c is not None and im.c > im.x else 1)
    row = samples * im.item
    far = {**planes(im)[-1][1], **fields(im)[-1]}

    def place(y: int) -> Range | str:
        return im.run({**far, **({im.y: y} if im.y is not None else {})}, samples)

    padded = False
    if rows > 1:
        first, second = place(0), place(1)
        if isinstance(first, str) or isinstance(second, str):
            return first if isinstance(first, str) else str(second)
        padded = second[1] - first[1] != row
    seen["images of rows with bytes between them"] += padded
    for b in range(max(1, min(rows, BAND // row)), 1, -1):
        if rows % b:
            continue
        if not padded:
            return b
        last = [place(y) for y in range(rows - b, rows)]
        if any(isinstance(r, str) for r in last):
            return next(r for r in last if isinstance(r, str))
        if references([r for r in last if not isinstance(r, str)]) <= PAYLOAD:
            return b
        seen["bands tried whose references are too large"] += 1
    return 1


def chunks_of(im: Image, series: int, field: dict[int, int], b: int, seen: Counter) -> dict[str, list[Range]] | str:
    """The chunks of one field of an image, the series `series`: for each plane and band, the
    ranges of the band's rows, those that touch joined."""
    rows, samples = im.length(im.y), im.shape[im.x] * (im.shape[im.c] if im.c is not None and im.c > im.x else 1)
    out: dict[str, list[Range]] = {}
    for coords, index in planes(im):
        for j in range(rows // b):
            parts: list[Range] = []
            for y in range(j * b, (j + 1) * b):
                r = im.run({**index, **field, **({im.y: y} if im.y is not None else {})}, samples)
                if isinstance(r, str):
                    return r
                if parts and parts[-1][0] == r[0] and parts[-1][1] + parts[-1][2] == r[1]:
                    parts[-1] = (r[0], parts[-1][1], parts[-1][2] + r[2])
                else:
                    parts.append(r)
            out["/".join(map(str, [series, 0, "c", *coords, j, 0]))] = parts
            seen["chunks"] += 1
            seen["chunks of several ranges"] += len(parts) > 1
    return out


def written_parts(inp: Input, parts: list) -> list:
    """A reference's parts as ranges of the input's files, (key, offset, length), the key of a
    file input None; a part that is no such range is left as it is."""
    out = []
    for p in parts:
        if isinstance(inp.d.out, Output):
            out.append((None, p[-2], p[-1]) if isinstance(p, tuple) and (len(p) == 2 or p[0] == 0) else p)
        else:
            out.append(tuple(p[1:]) if isinstance(p, tuple) and p[0] == "object" else p)
    return out


def check(inp: Input, project: bool, seen: Counter) -> list[str]:
    """What differs between the arrays the profile wrote of an input and what the input's
    descriptions give by the profile's rules."""
    d, out = inp.d, inp.d.out
    entries = out.bytes_entries if isinstance(out, Output) else out.entries
    docs = {k: mapping(json.loads(v), k) for k, v in entries.items()
            if k.endswith("/0/zarr.json") and not k.startswith("refkv_source/")}
    refs = {k: written_parts(inp, parts) for k, parts in out.refs.items() if not k.startswith("refkv_source/")}
    said = ["descriptions dropped for room"] if d.stored.undescribed else []
    images = list(range(len(d.stored.arrays)))
    if project:
        # the images in the order the documents name one another: by the LOF whose bytes each
        # series' chunks name, which the profile's output alone gives
        lofs = {inp.where(a.element)[0]: k for k, a in enumerate(d.stored.arrays)}
        named: dict[int, str | None] = {}
        for key in sorted(refs):
            named.setdefault(int(key.split("/")[0]), refs[key][0][0])
        walked = list(dict.fromkeys(lofs[named[s]] for s in sorted(named) if named[s] in lofs))
        images = walked + [k for k in images if k not in walked]
    want_docs: dict[str, dict[str, JSON]] = {}
    want: dict[str, list[Range]] = {}
    series = 0
    for k in images:
        found = image_of(inp, k)
        if isinstance(found, str):
            # nothing is derived of the input: which series are the image's follows from its fields
            seen[f"images with {found}, of which nothing is derived"] += 1
            return said
        im, doc = found
        seen["images"] += 1
        interleaved = im.c is not None and im.c > im.x
        seen["images of interleaved channels"] += interleaved
        seen["images of channels in planes"] += im.c is not None and not interleaved
        seen["images of fields"] += len(im.fields) > 0
        seen["images with no used Y"] += im.y is None
        b = band_of(im, seen)
        if isinstance(b, str):
            return [*said, f"image {k}: {b}"]
        seen["images of several bands a plane"] += b < im.length(im.y)
        # a plane a chunk, of every channel where they are interleaved, cut into bands
        chunk = [1] if im.t is not None else []
        if im.c is not None:
            chunk.append(im.shape[im.c] if interleaved else 1)
        chunk += ([1] if im.z is not None else []) + [b, im.shape[im.x]]
        for field in fields(im):
            want_docs[f"{series}/0/zarr.json"] = {**doc, "chunk_shape": list(chunk)}
            chunks = chunks_of(im, series, field, b, seen)
            if isinstance(chunks, str):
                return [*said, f"image {k}: {chunks}"]
            want.update(chunks)
            series += 1
            seen["arrays"] += 1
    # the documents: each derived member against the profile's
    if set(docs) != set(want_docs):
        said.append(f"arrays at {sorted(docs)}, the descriptions give {sorted(want_docs)}")
    for key, doc in sorted(want_docs.items()):
        got = docs.get(key)
        if got is None:
            continue
        wrote = {"shape": got["shape"], "data_type": got["data_type"], "codecs": got["codecs"],
                 "dimension_names": got["dimension_names"],
                 "chunk_shape": at(got["chunk_grid"], "configuration", "chunk_shape")}
        said += [f"{key}: {name} {wrote[name]}, the description gives {value}" for name, value in doc.items()
                 if wrote[name] != value]
    # the chunks: every one, and each one's parts
    if any("/0/c/" in key for key in entries if not key.startswith("refkv_source/")):
        said.append("chunks the profile copied")
    if set(refs) != set(want):
        alone, absent = sorted(set(refs) - set(want)), sorted(set(want) - set(refs))
        said.append(f"chunks at {alone[:4]} alone, and none at {absent[:4]}")
    said += [f"chunk {key} is {refs[key][:3]}, not {parts[:3]}" for key, parts in sorted(want.items())
             if key in refs and refs[key] != parts]
    return said


@cache
def compared(path: Path) -> tuple[tuple[str, ...], Counter]:
    """What differs between the arrays the profile wrote of the input at `path` and the input's
    descriptions, and what the comparison saw."""
    seen: Counter = Counter()
    project = path.suffix in DOCUMENTS
    seen["projects" if project else "files"] += 1
    return tuple(check(read_input(path), project, seen)), seen


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_the_lif_profile_writes_what_the_descriptions_give(path):
    """Every array the profile writes of the input, and every chunk, is what its image's
    description gives."""
    said, seen = compared(path)
    assert said == ()
    assert seen["images"] == len(read_input(path).d.stored.arrays)


def test_the_fixtures_hold_every_case_the_profile_is_compared_in():
    """The fixtures reach each case of the comparison, so that none of it passes for want of a
    file. The band tried and left is of `lif_rgb_padded_tall.lif`, whose 16384 rows are in four
    chunks: a band of 8192 rows has references past 65519 bytes."""
    seen: Counter = Counter()
    for path in FILES:
        seen += compared(path)[1]
    assert {k: v for k, v in seen.items() if v} == {
        "files": 24,
        "projects": 8,
        "images": 37,
        "images of channels in planes": 7,
        "images of interleaved channels": 6,
        "images of fields": 2,
        "images with no used Y": 1,
        "images of rows with bytes between them": 2,
        "bands tried whose references are too large": 2,
        "images of several bands a plane": 1,
        "arrays": 42,
        "chunks": 102,
        "chunks of several ranges": 5,
    }


def test_arrays_no_fixture_holds_are_what_their_descriptions_give(tmp_path):
    """Cases no fixture holds, each compared as the fixtures are: planes, rows of one channel,
    and channels with bytes between them, whose units are several; channels the XML lists in the
    other order than their bytes; dimensions of one element; a block with bytes after the image;
    interleaved samples of two bytes over depths; fields of two dimensions, the first the
    fastest, over times; a plane of more than 2^24 bytes, in several bands; and a project's image
    at an `Offset` of its LOF's block."""
    one = [(0, 0)]
    cases = {
        "planes_apart": ([(X, 4, 1), (Y, 3, 4), (Z, 2, 20)], one, 32, {}),
        "rows_apart": ([(X, 4, 1), (Y, 3, 6)], one, 18, {}),
        "channels_uneven": ([(X, 4, 1), (Y, 3, 4)], [(5, 0), (17, 0), (40, 0)], 52, {}),
        "channels_listed_last_first": ([(X, 4, 1), (Y, 3, 4)], [(12, 0), (0, 0)], 24, {}),
        "one_element": ([(X, 4, 1), (Z, 1, 0), (Y, 3, 4), (T, 1, 12), (M, 1, 12)], one, 12, {}),
        "bytes_after": ([(X, 4, 1), (Y, 3, 4)], one, 20, {}),
        "interleaved_16": ([(X, 4, 4), (Y, 3, 16), (Z, 2, 48)], [(0, 0), (2, 0)], 96, {"resolution": 12}),
        "fields": ([(X, 2, 8), (Y, 2, 16), (WAVELENGTH, 2, 32), (M, 3, 64), (T, 2, 192)], one, 384,
                   {"data_type": 1, "resolution": 64}),
        "bands": ([(X, 4097, 1), (Y, 4097, 4097)], one, 4097 * 4097, {}),
    }
    paths = [write_lif(tmp_path, name, dims, channels, size, **kind)
             for name, (dims, channels, size, kind) in cases.items()]
    seen: Counter = Counter()
    for path in [*paths, write_project(tmp_path, 12, 5)]:
        said, saw = compared(path)
        assert said == (), path.name
        seen += saw
    assert {k: v for k, v in seen.items() if v} == {
        "files": 9,
        "projects": 1,
        "images": 10,
        "images of channels in planes": 2,
        "images of interleaved channels": 1,
        "images of fields": 1,
        "images of rows with bytes between them": 1,
        "images of several bands a plane": 1,
        "arrays": 15,
        "chunks": 42,
        "chunks of several ranges": 1,
    }


def test_a_project_s_images_are_in_the_order_its_documents_name_one_another(tmp_path):
    """A project whose descriptions are in another order than its images: a LOF is described when
    it is found, a level of documents at a time, and the images are numbered as the documents
    name one another, a document before the ones it names. The image a collection names is the
    first series, though its LOF is found, and described, last."""
    path = write_nested_project(tmp_path)
    said, seen = compared(path)
    assert said == ()
    assert {k: v for k, v in seen.items() if v} == {"projects": 1, "images": 2, "arrays": 2, "chunks": 2}
    inp = read_input(path)
    described_in = [inp.where(a.element)[0] for a in inp.d.stored.arrays]
    assert described_in == ["nested/Flat.lof", "nested/Deep.lof"]
    assert {k: [p[1] for p in parts] for k, parts in inp.d.out.refs.items() if "/0/c/" in k} == {
        "0/0/c/0/0": ["nested/Deep.lof"],
        "1/0/c/0/0": ["nested/Flat.lof"],
    }


def test_an_image_with_no_grid_is_presented_from_its_declaration(tmp_path):
    """An image whose samples share bytes (a dimension of no increment repeats a plane) has a
    description with no grid, of which the test derives nothing; the profile presents it all the
    same, a chunk for each time, every one the same bytes."""
    path = write_lif(tmp_path, "repeat", [(X, 4, 1), (Y, 3, 4), (T, 5, 0)], [(0, 0)], 12)
    said, seen = compared(path)
    assert said == ()
    assert {k: v for k, v in seen.items() if v} == {"files": 1, "images with no grid, of which nothing is derived": 1}
    inp = read_input(path)
    out = inp.d.out
    assert isinstance(out, Output)
    doc = json.loads(out.bytes_entries["0/0/zarr.json"])
    assert (doc["shape"], doc["dimension_names"]) == ([5, 3, 4], ["t", "y", "x"])
    chunks = {k: v for k, v in out.refs.items() if k.startswith("0/0/c/")}
    assert len(chunks) == 5
    assert len({tuple(map(tuple, v)) for v in chunks.values()}) == 1
