"""The stored arrays of the TIFF and QPTIFF fixtures (spec/stored-array.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>), compared with
tifffile, the reference reader: every IFD that declares an image is read by tifffile at its offset
and compared with its description (shape, sample size, grid, codec chain and its parameters), each
tile or strip tifffile lists with its chunk map row (its place), and the values each row's bytes
decode to through the row's chain with the values tifffile reads there. A second test asserts
which cases the fixtures reached."""

import json
import logging
import lzma
import math
import struct
import zlib
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import cache
from pathlib import Path

import imagecodecs
import numpy as np
import pytest
import tifffile
from described import Described, described
from ir_helpers import FIXTURES

from refkv.ir.stored import Codec, Each, ElementPlace, Source, StoredArray, Value

# files the TIFF profile rejects, whose names do not say so
REJECTED = {"mm_split_MMStack_Pos0_1.ome.tif", "unsupported_predictor_2_float.tif"}
FILES = sorted(
    p for d in ("tiff", "qptiff") for p in (FIXTURES / d).iterdir()
    if p.is_file() and "reject" not in p.name and p.name not in REJECTED
)
# the compressions whose decoding restores the stored bytes, by their codec (spec/stored-array.md
# §3.2 <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#32-the-list>): those under
# which libtiff applies a predictor, and those under which it applies none
PREDICTED = {5: "tiff_lzw", 8: "zlib", 32946: "zlib", 34925: "xz", 50000: "zstd"}
UNPREDICTED = {1: None, 32773: "packbits"}
JPEG2000 = {33003, 33004, 33005, 34712}
EER = {65000: (8, 2, 2), 65001: (7, 2, 2), 65002: None}
# the compressions whose streams use no predictor, under which libtiff and tifffile pass one over
IMAGE_CODECS = JPEG2000 | {7, 50001, 50002, 52546} | set(EER)
# the tags that declare what an image's elements are, after the header's byte order
DECLARING = (258, 262, 320, 338, 339, 529, 530, 532)


@dataclass(frozen=True)
class Image:
    """What tifffile reads of an IFD's fields, in a file of `data` under the key `key` of the IR's
    objects (None for the parsed file)."""

    page: tifffile.TiffPage
    key: str | None
    data: bytes

    def first(self, tag: int, default: int) -> int:
        t = self.page.tags.get(tag)
        if t is None:
            return default
        return int(t.value[0] if isinstance(t.value, tuple) else t.value)

    def every(self, tag: int, default: tuple[int, ...]) -> tuple[int, ...]:
        t = self.page.tags.get(tag)
        if t is None:
            return default
        return tuple(int(v) for v in t.value) if isinstance(t.value, tuple) else (int(t.value),)

    @property
    def samples(self) -> int:
        return self.first(277, 1)

    @property
    def planar(self) -> bool:
        return self.samples > 1 and self.first(284, 1) == 2

    @property
    def bits(self) -> int | None:
        """BitsPerSample, where every sample has the same."""
        found = set(self.every(258, (1,)))
        return found.pop() if len(found) == 1 else None

    @property
    def shape(self) -> tuple[int, ...]:
        h, w, s, depth = self.first(257, 0), self.first(256, 0), self.samples, self.first(32997, 1)
        plane = (h, w) if depth == 1 else (depth, h, w)
        return plane if s == 1 else (s, *plane) if self.planar else (*plane, s)

    @property
    def tiled(self) -> bool:
        """An image with a tile's size is in tiles, as libtiff and tifffile read it."""
        return 322 in self.page.tags and 323 in self.page.tags

    @property
    def unit(self) -> tuple[int, int]:
        """The rows and columns of a tile, or of a strip."""
        if self.tiled:
            return self.first(323, 0), self.first(322, 0)
        return min(self.first(278, 2**32 - 1), self.first(257, 0)), self.first(256, 0)

    @property
    def table(self) -> tuple[tuple[int, ...], tuple[int, ...]]:
        # tiles are listed in the strips' tables where the image has no tables of its own
        own = self.tiled and 324 in self.page.tags and 325 in self.page.tags
        offsets, counts = (324, 325) if own else (273, 279)
        return self.every(offsets, ()), self.every(counts, ())

    def lerc_outer(self) -> int:
        """The second value of LercParameters, the compression of the LERC blob: 0 without the
        field, and -1 where the field is not two unsigned integers."""
        t = self.page.tags.get(50674)
        if t is None:
            return 0
        return int(t.value[1]) if t.dtype in (1, 3, 4, 13, 16, 18) and t.count >= 2 else -1

    def chain(self) -> tuple[str, ...] | None:
        """The names of the codecs of the image's chain, as this test reads tifffile's fields;
        None for an image whose decoding the IR does not model."""
        bits, compression, predictor = self.bits, self.first(259, 1), self.first(317, 1)
        if bits is None or self.first(266, 1) != 1:
            return None
        restoring = compression in PREDICTED or compression in UNPREDICTED
        if restoring and self.first(262, 0) == 6 and self.every(530, (2, 2)) != (1, 1):
            return None
        if compression in PREDICTED:
            formats = set(self.every(339, (1,)))
            integers = bits in (8, 16, 32, 64) and formats <= {1, 2} and len(formats) == 1
            floats = bits in (32, 64) and formats == {3}
            fits = {1: True, 2: integers, 3: floats}.get(predictor, False)
            return ((() if predictor == 1 else ("tiff_predictor",)) + (PREDICTED[compression],)) if fits else None
        if predictor != 1 and compression not in IMAGE_CODECS:
            return None
        if compression in UNPREDICTED:
            name = UNPREDICTED[compression]
            return () if name is None else (name,)
        one = self.samples == 1
        named = {
            4: ("ccitt_t6",) if bits == 1 and one else None,
            7: ("jpeg",) if bits == 8 else None,
            50002: ("jpegxl",) if bits in (8, 16, 32) else None,
            52546: ("jpegxl",) if bits in (8, 16, 32) else None,
            50001: ("webp",) if bits == 8 else None,
            34887: {0: ("lerc",), 1: ("lerc", "zlib"), 2: ("lerc", "zstd")}.get(self.lerc_outer())
            if bits in (8, 16, 32, 64) else None,
        }
        if compression in JPEG2000:
            return ("jpeg2k",) if bits in (8, 16) else ("source",)
        if compression in EER:
            return ("eer",) if bits == 1 and one else ("source",)
        return named.get(compression) or ("source",)

    def units(self) -> str | int:
        """The number of units the image's tables must list, or why the IR gives it no grid."""
        h, w = self.first(257, 0), self.first(256, 0)
        if h == 0 or w == 0 or self.samples == 0:
            return "an empty image"
        if self.first(32997, 1) != 1 or (self.samples > 1 and self.first(284, 1) not in (1, 2)):
            return "a layout the IR does not model"
        rows, columns = self.unit
        if rows == 0 or columns == 0:
            return "an empty unit"
        offsets, counts = self.table
        count = math.ceil(h / rows) * math.ceil(w / columns) * (self.samples if self.planar else 1)
        if len(offsets) != count or len(counts) != count:
            return "tables of another length"
        if any(n and o + n > len(self.data) for o, n in zip(offsets, counts, strict=True)):
            return "a unit outside the file"
        # a unit of no bytes is one a sparse file leaves out only where its offset is 0 too
        if any(o and not n for o, n in zip(offsets, counts, strict=True)):
            return "a unit of no bytes at an offset"
        return count


def tag_bytes(page: tifffile.TiffPage, tag: int, data: bytes) -> bytes | None:
    """The bytes of a field's value, as the file holds them."""
    t = page.tags.get(tag)
    if t is None:
        return None
    size = struct.calcsize(tifffile.TIFF.DATA_FORMATS[t.dtype]) * t.count
    return data[t.valueoffset:t.valueoffset + size]


def unit_shape(a: StoredArray, coords: tuple[int, ...]) -> tuple[int, ...]:
    """The shape of the unit at `coords` of the array's grid: cut at the array's end under `short`."""
    assert a.grid is not None
    assert a.shape is not None
    out = []
    for axis, c, n in zip(a.grid.axes, coords, a.shape, strict=True):
        assert isinstance(axis, Each)
        out.append(min(axis.length, n - c * axis.length) if a.grid.edge == "short" else axis.length)
    return tuple(out)


def unit_bytes(a: StoredArray, shape: tuple[int, ...]) -> int:
    """The bytes of a decoded unit of `shape`, as `item_bits` and `pad_from` pack its elements."""
    assert a.item_bits is not None
    at = len(shape) if a.item_bits % 8 == 0 else 0 if a.pad_from is None else a.pad_from
    return math.prod(shape[:at]) * math.ceil(math.prod(shape[at:]) * a.item_bits / 8)


def unpredicted(d: Described, c: Codec, b: bytes, a: StoredArray, shape: tuple[int, ...]) -> bytes:
    """What `tiff_predictor` decodes `b` to: the rows of a unit of `shape`, summed back along
    each, by what the codec's places hold (TIFF 6.0 §14, and Adobe's technical note 3)."""
    places = {k: d.place(v.place) for k, v in c.parameters.items() if isinstance(v, Source)}
    order = {b"II": "<", b"MM": ">"}[places["byte_order"]]
    predictor = struct.unpack(order + "H", places["predictor"])[0]
    planar = struct.unpack(order + "H", places["planar_configuration"])[0] if "planar_configuration" in places else 1
    assert a.item_bits is not None
    size = a.item_bits // 8
    # the unit's rows and columns, and the samples a pixel interleaves
    rows, columns, samples = (*shape[-2:], 1) if len(shape) == 2 or planar == 2 else shape
    if predictor == 2:
        differences = np.frombuffer(b, dtype=f"{order}u{size}").reshape(rows, columns, samples)
        return np.cumsum(differences, axis=1, dtype=differences.dtype).astype(differences.dtype).tobytes()
    assert predictor == 3
    planes = np.cumsum(np.frombuffer(b, dtype="u1").reshape(rows, size * columns, samples), axis=1, dtype="u1")
    # each row: every sample's most significant byte, then every sample's next
    stored = planes.reshape(rows, size, columns * samples).transpose(0, 2, 1)
    return (stored if order == ">" else stored[:, :, ::-1]).tobytes()


RESTORING: dict[str, Callable[[bytes], bytes]] = {
    "zlib": zlib.decompress,
    "zstd": lambda b: bytes(imagecodecs.zstd_decode(b)),
    "xz": lzma.decompress,
    "packbits": lambda b: bytes(imagecodecs.packbits_decode(b)),
    "tiff_lzw": lambda b: bytes(imagecodecs.lzw_decode(b)),
}


def stored_values(a: StoredArray, b: bytes, shape: tuple[int, ...], dtype: np.dtype, little: bool) -> np.ndarray:
    """The elements `b` holds as the description packs them, as values of tifffile's data type, in
    the file's byte order."""
    assert a.item_bits is not None
    if a.item_bits % 8 == 0:
        return np.frombuffer(b, dtype=dtype.newbyteorder("<" if little else ">")).reshape(shape)
    assert a.pad_from is not None
    runs, run = math.prod(shape[:a.pad_from]), math.prod(shape[a.pad_from:])
    bits = np.unpackbits(np.frombuffer(b, dtype="u1").reshape(runs, -1), axis=1)[:, :run * a.item_bits]
    weights = 1 << np.arange(a.item_bits - 1, -1, -1, dtype="u8")
    return (bits.reshape(runs, run, a.item_bits) * weights).sum(axis=2).reshape(shape).astype(dtype)


def made(d: Described, im: Image, c: Codec, b: bytes, shape: tuple[int, ...]) -> np.ndarray | None:
    """What a codec that makes the elements decodes `b` to, by imagecodecs, given only what the
    description holds and, for JPEG, the color space tifffile reads the stream in; None for
    `source`, which names no decoding."""
    page = im.page
    rows, columns = shape[-2:] if len(shape) == 2 or im.planar else shape[:2]
    places = {k: d.place(v.place) for k, v in c.parameters.items() if isinstance(v, Source)}
    if c.name == "jpeg":
        colorspace, out = tifffile.tifffile.jpeg_decode_colorspace(
            page.photometric, page.planarconfig, page.extrasamples, page.is_jfif)
        return imagecodecs.jpeg8_decode(b, tables=places.get("tables"), colorspace=colorspace, outcolorspace=out)
    if c.name == "jpeg2k":
        return imagecodecs.jpeg2k_decode(b)
    if c.name == "jpegxl":
        return imagecodecs.jpegxl_decode(b)
    if c.name == "webp":
        return imagecodecs.webp_decode(b, hasalpha=im.samples == 4)
    if c.name == "lerc":
        return imagecodecs.lerc_decode(b)
    if c.name == "ccitt_t6":
        return imagecodecs.ccittfax4_decode(b, height=rows, width=columns)
    if c.name == "eer":
        # a value, or a field's one SHORT; EER's defaults where the file has no field
        order = page.parent.byteorder
        bits = {"skip_bits": 7, "horizontal_bits": 2, "vertical_bits": 2}
        bits |= {k: v.value for k, v in c.parameters.items() if isinstance(v, Value)}
        bits |= {k: struct.unpack(order + "H", v)[0] for k, v in places.items()}
        skip, horizontal, vertical = (int(str(bits[k])) for k in ("skip_bits", "horizontal_bits", "vertical_bits"))
        return imagecodecs.eer_decode(b, (rows, columns), skip, horizontal, vertical)
    return None


def check_chain(d: Described, im: Image, a: StoredArray, said: list[str], seen: Counter) -> None:
    """The array's one chain against tifffile's fields: its codecs, and each parameter."""
    data = im.data
    (k,) = a.chains
    chain = d.stored.chains[k]
    if tuple(c.name for c in chain) != im.chain():
        said.append(f"chain {[c.name for c in chain]}, by tifffile's fields {im.chain()}")
        return
    page = im.page
    for c in chain:
        seen[f"codec {c.name}"] += 1
        places = {k: d.place(v.place) for k, v in c.parameters.items() if isinstance(v, Source)}
        values = {k: v.value for k, v in c.parameters.items() if isinstance(v, Value)}
        compression = im.first(259, 1)
        if c.name == "tiff_predictor":
            want = {"predictor": tag_bytes(page, 317, data), "byte_order": data[:2]}
            if im.samples > 1 and 284 in page.tags:
                want["planar_configuration"] = tag_bytes(page, 284, data)
            if places != want or values:
                said.append(f"tiff_predictor's parameters {places} {values}, the file's {want}")
        elif c.name == "jpeg":
            tables = tag_bytes(page, 347, data)
            seen["jpeg with tables"] += tables is not None
            if places != ({} if tables is None else {"tables": tables}) or values:
                said.append("jpeg's tables are not the JPEGTables field's value")
        elif c.name == "eer":
            fixed = EER[compression]
            names = ("skip_bits", "horizontal_bits", "vertical_bits")
            if fixed is not None:
                want_values, want_places = dict(zip(names, fixed, strict=True)), {}
            else:
                want_values = {}
                tags = zip(names, (65007, 65008, 65009), strict=True)
                want_places = {n: tag_bytes(page, t, data) for n, t in tags if t in page.tags}
            seen["eer with places"] += bool(want_places)
            if (values, places) != (want_values, want_places):
                said.append(f"eer's parameters {values} {places}, the file's {want_values} {want_places}")
        elif c.name == "source":
            if values != {"format": "tiff", "id": compression} or places:
                said.append(f"source's parameters {values} {places}")
        elif c.parameters:
            said.append(f"{c.name} has parameters")


def decoded(d: Described, im: Image, a: StoredArray, row: dict, shape: tuple[int, ...]) -> np.ndarray | bytes | None:
    """A row's unit decoded by the array's chain, last codec first: the elements' bytes, or the
    array a codec that makes them gives; None where the test has no decoder of a codec."""
    chain = d.stored.chains[a.chains[0]]
    b: bytes = d.unit(row)
    for i in reversed(range(len(chain))):
        c = chain[i]
        if c.name in RESTORING:
            b = RESTORING[c.name](b)
        elif c.name == "tiff_predictor":
            b = unpredicted(d, c, b, a, shape)
        else:
            assert i == 0, "a codec that makes the elements is its chain's first"
            return made(d, im, c, b, shape)
    return b


def check_units(d: Described, im: Image, k: int, theirs: np.ndarray | None, said: list[str], seen: Counter) -> None:
    """Each tile or strip tifffile lists against the array's rows: a row for each that holds
    bytes, at its place, decoding to the values tifffile reads there."""
    a = d.stored.arrays[k]
    assert a.grid is not None
    page = im.page
    rows, columns = im.unit
    h, w = im.first(257, 0), im.first(256, 0)
    across = math.ceil(w / columns)
    plane = math.ceil(h / rows) * across
    got = {tuple(r["coords"]): r for r in d.rows.get(k, [])}
    offsets, counts = im.table
    held = 0
    for j, (offset, count) in enumerate(zip(offsets, counts, strict=True)):
        at = (j % plane // across, j % plane % across)
        coords = at if im.samples == 1 else (j // plane, *at) if im.planar else (*at, 0)
        row = got.get(coords)
        if count == 0 or row is None:
            if (count == 0) != (row is None):
                said.append(f"unit {coords}: {'a row' if count == 0 else 'no row'}, tifffile lists {count} bytes")
            continue
        held += 1
        seen["units"] += 1
        if d.parts(row) != [(im.key, offset, count)]:
            said.append(f"unit {coords} at {d.parts(row)}, tifffile {offset} of {count}")
            continue
        # a row names its element alone, but where the element's recipe gives other bytes than the
        # stored ones: then the range of all its extent names those
        form = d.ir.form(row["element"])
        whole = form is not None and json.loads(form)["recipe"] != [["src", 0, None]]
        if (row["range"] is not None) != whole:
            said.append(f"unit {coords}: a range {row['range']} of an element of the form {form}")
            continue
        seen["rows of a range"] += whole
        seen["rows of an element"] += not whole
        shape = unit_shape(a, coords)
        seen["units an edge cuts"] += shape != tuple(x.length for x in a.grid.axes if isinstance(x, Each))
        mine = decoded(d, im, a, row, shape)
        if mine is None:
            seen["units not decoded"] += 1
            continue
        if theirs is None:
            seen["units tifffile does not read"] += 1
            continue
        # the unit's region of the image: tifffile's array is (planes, depth, rows, columns, samples)
        r0, c0 = at[0] * rows, at[1] * columns
        region = theirs[j // plane if im.planar else 0, 0, r0:r0 + rows, c0:c0 + columns, :]
        if isinstance(mine, bytes):
            # a chain may decode to more bytes than the unit's elements take, which are the first
            size = unit_bytes(a, shape)
            if len(mine) < size:
                said.append(f"unit {coords} decodes to {len(mine)} bytes, fewer than its {size}")
                continue
            seen["units longer than their elements"] += len(mine) > size
            mine = mine[:size]
            mine = stored_values(a, mine, shape, np.dtype(page.dtype), d.place(a.declarations[0]) == b"II")
        elif mine.size != math.prod(shape):
            said.append(f"unit {coords}: a frame of {mine.shape}, not its unit's {shape}")
            continue
        mine = mine.reshape(*((*shape[-2:], 1) if im.planar or im.samples == 1 else shape))
        mine = mine[:region.shape[0], :region.shape[1]]
        if mine.shape != region.shape or mine.astype(region.dtype).tobytes() != region.tobytes():
            said.append(f"unit {coords} decodes to other values than tifffile reads")
        seen["units decoded"] += 1
    if held != len(got):
        said.append(f"{len(got)} rows, {held} units tifffile lists with bytes")


def check_image(d: Described, k: int, im: Image, seen: Counter) -> list[str]:
    """One IFD's image against its description."""
    a = d.stored.arrays[k]
    page, data = im.page, im.data
    said: list[str] = []
    if a.shape != im.shape:
        said.append(f"shape {a.shape}, tifffile {im.shape}")
    if a.item_bits != im.bits:
        said.append(f"item_bits {a.item_bits}, tifffile {im.bits}")
    if a.fill != "undefined" or a.axes is not None or a.index is not None or a.unit_parameters:
        said.append("a fill value, axes, an index or unit parameters")
    declared = [data[:2]] + [b for t in DECLARING if (b := tag_bytes(page, t, data)) is not None]
    if [d.place(p) for p in a.declarations] != declared or not all(isinstance(p, ElementPlace) for p in a.declarations):
        said.append("the declarations are not the byte order and the declaring fields' values")
    units = im.units()
    reason = "a decoding the IR does not model" if im.chain() is None else units if isinstance(units, str) else None
    if reason is not None or a.grid is None:
        seen[f"no grid: {reason}"] += 1
        if reason is None or a.grid is not None or a.chains or a.pad_from is not None or d.rows.get(k):
            said.append(f"a grid, chains or rows for {reason}" if reason else "no grid")
        return said
    rows, columns = im.unit
    axes = (Each(rows), Each(columns))
    axes = axes if im.samples == 1 else (Each(1), *axes) if im.planar else (*axes, Each(im.samples))
    edge = "full" if im.tiled else "short"
    if (a.grid.axes, a.grid.edge) != (axes, edge):
        said.append(f"grid {a.grid}, tifffile {axes} {edge}")
        return said
    pad = None if im.bits is None or im.bits % 8 == 0 else 2 if im.planar else 1
    if a.pad_from != pad:
        said.append(f"pad_from {a.pad_from}, not {pad}")
    seen["tiled" if im.tiled else "strips"] += 1
    seen["planar" if im.planar else "interleaved" if im.samples > 1 else "one sample"] += 1
    seen["packed samples"] += pad is not None
    check_chain(d, im, a, said, seen)
    if said:
        return said
    try:
        theirs = page.asarray().reshape(page.shaped)
    except Exception:  # noqa: BLE001  (whatever a decoder raises: tifffile reads no values to compare)
        # tifffile reads EER only in a file it takes for an EER file, and no encoding it has no name for
        theirs = None
        if im.chain() not in (("eer",), ("source",)):
            said.append("tifffile does not read the image")
    check_units(d, im, k, theirs, said, seen)
    return said


def reached(pages: list) -> list[tifffile.TiffPage]:
    """The pages tifffile reaches from `pages`, with those of their SubIFDs."""
    out = []
    for p in pages:
        if isinstance(p, tifffile.TiffPage):
            out.append(p)
            out += reached(list(p.pages or []))
    return out


def later_planes(path: Path, d: Described, later: dict[int, int], seen: Counter) -> list[str]:
    """The planes after the first of an ImageJ stack in one IFD (spec/stored-array.md §7
    <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>), `later` their
    descriptions by plane: each is IFD 0's but for its key and the ImageDescription it also names,
    and its rows' bytes, in order, are the plane tifffile reads."""
    if not later:
        return []
    said = []
    with tifffile.TiffFile(path) as tif:
        page = tif.pages.first
        stack = tif.series[0].asarray().reshape(-1, *page.shaped)
        stack = stack.astype(stack.dtype.newbyteorder(tif.byteorder))  # as the file stores them
        first = next(a for a in d.stored.arrays if d.at(a.element)[1] == page.offset)
        described_at = (page.tags[270].valueoffset, page.tags[270].count)
        if len(tif.pages) != 1 or sorted(later) != list(range(1, len(stack))):
            return [f"planes {sorted(later)} of a stack of {len(stack)} planes in {len(tif.pages)} IFDs"]
        for p, k in sorted(later.items()):
            a = d.stored.arrays[k]
            seen["planes after the first of an ImageJ stack in one IFD"] += 1
            if replace(a, element=first.element, path=first.path, declarations=a.declarations[:-1]) != first:
                said.append(f"{a.path}: not IFD 0's description")
            last = a.declarations[-1]
            if not isinstance(last, ElementPlace) or d.at(last.element)[1:] != described_at:
                said.append(f"{a.path}: its last declaration is not the ImageDescription")
            if b"".join(d.unit(r) for r in d.rows.get(k, [])) != stack[p].tobytes():
                said.append(f"{a.path}: its rows' bytes are not the plane tifffile reads")
    return said


@cache
def compared(path: Path) -> tuple[tuple[str, ...], Counter]:
    """What differs between the descriptions of the file at `path` (and of the files of its set)
    and tifffile, and what the comparison saw."""
    logging.getLogger("tifffile").setLevel(logging.CRITICAL)
    d = described(path)
    said: list[str] = []
    seen: Counter = Counter()
    if d.stored.undescribed:
        said.append(f"descriptions dropped for room: {d.stored.undescribed}")
    by_ifd: dict[tuple[str | None, int], int] = {}
    later: dict[int, int] = {}
    for k, a in enumerate(d.stored.arrays):
        if (a.path or "").startswith("imagej/planes/"):
            later[int((a.path or "").rsplit("/", 1)[1])] = k
        else:
            by_ifd[d.at(a.element)[:2]] = k
    assert len(by_ifd) + len(later) == len(d.stored.arrays)
    said += later_planes(path, d, later, seen)
    for key, data in d.objects.items():
        at = path if key is None else path.parent / key
        with tifffile.TiffFile(at) as tif:
            tif.pages.useframes = False
            # every page tifffile reaches that declares a size is described
            sized = [p.offset for p in reached(list(tif.pages)) if 256 in p.tags and 257 in p.tags]
            said += [f"{at.name}: no description of the IFD at {o}" for o in sized if (key, o) not in by_ifd]
            for (object_key, offset), k in sorted(by_ifd.items(), key=lambda x: x[0][1]):
                if object_key != key:
                    continue
                tif.filehandle.seek(offset)
                im = Image(tifffile.TiffPage(tif, 0), key, data)
                seen["images"] += 1
                said += [f"{at.name}:{d.stored.arrays[k].path}: {s}" for s in check_image(d, k, im, seen)]
    return tuple(said), seen


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_tiff_images_are_described_as_tifffile_reads_them(path):
    """Every image of the file, and of the files of its set, is described as tifffile reads it."""
    said, seen = compared(path)
    assert said == ()
    assert seen["images"] >= 1


def test_the_fixtures_hold_every_case_the_comparison_reads():
    """The fixtures reach each case of the comparison, so that none of it passes for want of a
    file."""
    seen: Counter = Counter()
    for path in FILES:
        seen += compared(path)[1]
    wanted = ["images", "tiled", "strips", "planar", "interleaved", "one sample", "packed samples", "units",
              "units decoded", "units an edge cuts", "rows of a range", "rows of an element",
              "units longer than their elements", "no grid: a unit of no bytes at an offset", "jpeg with tables",
              "eer with places", "planes after the first of an ImageJ stack in one IFD",
              *(f"codec {c}" for c in ("tiff_lzw", "zlib", "xz", "zstd", "packbits", "tiff_predictor", "ccitt_t6",
                                       "jpeg", "jpeg2k", "jpegxl", "webp", "lerc", "eer", "source"))]
    assert [w for w in wanted if not seen[w]] == []
    # the units compared with nothing are EER's, which tifffile does not read in these files, and
    # those of an encoding the IR has no name for
    assert seen["units tifffile does not read"] + seen["units not decoded"] == 7


# ---- one test for each case no fixture holds, on a file written here


def written(tmp_path: Path, second: np.ndarray, tags: dict[int, int] | None = None,
            edit: Callable[[bytearray, tifffile.TiffPage], None] | None = None, pointer: bool = False, **how) -> Path:
    """A file of two images: one the profile presents, and a larger one it passes over, which
    tifffile wrote from `second` (`how`: its arguments), with a SHORT field of one value for each of
    `tags` (written under a private tag, since tifffile writes none of these itself, and renamed
    in the file), and then changed by `edit(file, page)`. With `pointer`, the second image's IFD is
    not the first's next: the first's Exif IFD field (34665) leads to it."""
    path = tmp_path / "written.tif"
    private = {60000 + i: tag for i, tag in enumerate(tags or {})}
    extra = [(code, "H", 1, (tags or {})[tag], False) for code, tag in private.items()]
    with tifffile.TiffWriter(path) as w:
        w.write(np.arange(64 * 64, dtype="u1").reshape(64, 64), tile=(32, 32), metadata=None,
                extratags=[(60099, "I", 1, 0, False)] if pointer else [])
        w.write(second, metadata=None, extratags=extra, **how)
    data = bytearray(path.read_bytes())
    with tifffile.TiffFile(path) as tif:
        first, page = tif.pages[0], tif.pages[1]
        assert isinstance(first, tifffile.TiffPage)
        assert isinstance(page, tifffile.TiffPage)
        for code, tag in private.items():
            struct.pack_into("<H", data, page.tags[code].offset, tag)
        if pointer:
            exif, next_ifd = first.tags[60099], first.offset + 2 + 12 * len(first.tags)
            assert struct.unpack_from("<I", data, next_ifd) == (page.offset,)
            struct.pack_into("<H", data, exif.offset, 34665)
            struct.pack_into("<I", data, exif.valueoffset, page.offset)
            struct.pack_into("<I", data, next_ifd, 0)
        if edit is not None:
            edit(data, page)
    path = tmp_path / "changed.tif"
    path.write_bytes(data)
    return path


def renamed(old: int, new: int, kind: int | None = None) -> Callable[[bytearray, tifffile.TiffPage], None]:
    """An edit that gives the page's field `old` the tag `new`, and the field type `kind`."""

    def edit(data: bytearray, page: tifffile.TiffPage) -> None:
        struct.pack_into("<H", data, page.tags[old].offset, new)
        if kind is not None:
            struct.pack_into("<H", data, page.tags[old].offset + 2, kind)

    return edit


def test_images_no_fixture_holds_are_described_as_tifffile_reads_them(tmp_path):
    """Layouts and codecs the fixtures do not have, each written here as a file's second image:
    every one is described as tifffile reads it, with a grid, and its units decode to tifffile's
    values."""
    rng = np.random.default_rng(0)

    def image(shape: tuple[int, ...], dtype: str) -> np.ndarray:
        return rng.integers(0, 200, shape).astype(dtype)

    def tiles_in_strips_tables(data: bytearray, page: tifffile.TiffPage) -> None:
        renamed(324, 273)(data, page)
        renamed(325, 279)(data, page)

    def sparse(data: bytearray, page: tifffile.TiffPage) -> None:
        # the second of four tiles left out, as a sparse file's writer leaves one
        for tag in (324, 325):
            size = struct.calcsize(tifffile.TIFF.DATA_FORMATS[page.tags[tag].dtype])
            at = page.tags[tag].valueoffset + size
            data[at:at + size] = bytes(size)

    rgb, planar = {"photometric": "rgb"}, {"photometric": "rgb", "planarconfig": "separate"}
    cases: dict[str, tuple[dict, dict[str, int]]] = {
        "planar samples of 1 bit": (
            {"second": rng.integers(0, 2, (3, 70, 90)).astype(bool), "photometric": "minisblack",
             "planarconfig": "separate"},
            {"planar": 1, "packed samples": 1}),
        "a predictor over planar samples": (
            {"second": image((3, 70, 90), "u2"), "compression": "zlib", "predictor": True, **planar},
            {"planar": 1, "codec tiff_predictor": 1}),
        "a predictor over samples of 32 bits": (
            {"second": image((70, 90), "u4"), "compression": "lzw", "predictor": True}, {"codec tiff_predictor": 1}),
        "Deflate as 32946": ({"second": image((70, 90), "u2"), "compression": "adobe_deflate"}, {"codec zlib": 1}),
        "a predictor JPEG 2000 passes over": (
            {"second": image((70, 90), "u1"), "compression": "jpeg2000", "tags": {317: 2}}, {"codec jpeg2k": 1}),
        "interleaved JPEG 2000 of 16 bits": (
            {"second": image((70, 90, 3), "u2"), "compression": "jpeg2000", **rgb},
            {"codec jpeg2k": 1, "interleaved": 1}),
        "a predictor JPEG passes over, and a stream with no tables": (
            {"second": image((70, 90), "u1"), "compression": "jpeg", "tags": {317: 2}},
            {"codec jpeg": 1, "rows of an element": 5}),
        "a predictor WebP passes over": (
            {"second": image((70, 90, 3), "u1"), "compression": "webp", "tags": {317: 2}, **rgb}, {"codec webp": 1}),
        "tiles listed in the strips' tables": (
            {"second": image((70, 90), "u1"), "tile": (64, 64), "edit": tiles_in_strips_tables},
            {"tiled": 2, "units": 8}),
        "an image a pointer tag leads to, with a field of a signed type": (
            {"second": image((70, 90), "i2"), "pointer": True, "edit": renamed(339, 339, 8)},
            {"strips": 1, "units": 5}),
        "a tile a sparse file leaves out": (
            {"second": image((128, 128), "u1"), "tile": (64, 64), "edit": sparse}, {"tiled": 2, "units": 7}),
    }
    for name, (how, want) in cases.items():
        folder = tmp_path / str(len(list(tmp_path.iterdir())))
        folder.mkdir()
        said, seen = compared(written(folder, **how))
        assert said == (), name
        assert {k: seen[k] for k in ("images", *(k for k in seen if k.startswith("no grid")))} == {"images": 2}, name
        assert {k: seen[k] for k in want} == want, name
        assert seen["units decoded"] == seen["units"], name


def no_grid(path: Path, reason: str) -> StoredArray:
    """The description of the second image of the file at `path`, which the comparison finds as
    tifffile reads it, with no grid for `reason`."""
    said, seen = compared(path)
    assert said == ()
    assert (seen["images"], seen[f"no grid: {reason}"]) == (2, 1)
    first, second = described(path).stored.arrays
    assert first.grid is not None
    assert (second.grid, second.chains, second.pad_from) == (None, (), None)
    return second


UNMODELED = "a decoding the IR does not model"


def test_an_image_of_another_fill_order_has_no_grid(tmp_path):
    """FillOrder 2 packs a byte's bits least significant first, which the IR does not model."""
    no_grid(written(tmp_path, np.zeros((70, 90), "u1"), {266: 2}), UNMODELED)


def test_a_predictor_without_a_compression_that_takes_one_has_no_grid(tmp_path):
    """libtiff applies no predictor to uncompressed data and tifffile applies one, so the IR does
    not say what the bytes hold."""
    no_grid(written(tmp_path, np.zeros((70, 90), "u2"), {317: 2}), UNMODELED)


def test_a_predictor_the_compression_does_not_fit_has_no_grid(tmp_path):
    """Horizontal differencing of floating-point samples is one readers disagree on."""
    no_grid(written(tmp_path, np.zeros((70, 90), "f4"), {317: 2}, compression="zlib"), UNMODELED)


def test_an_unknown_predictor_has_no_grid(tmp_path):
    no_grid(written(tmp_path, np.zeros((70, 90), "u2"), {317: 4}, compression="zlib"), UNMODELED)


def test_samples_of_unlike_sizes_have_no_grid_and_no_sample_size(tmp_path):
    """BitsPerSample that differ between samples give no one size of an element."""

    def widen(data: bytearray, page: tifffile.TiffPage) -> None:
        at = page.tags[258].valueoffset
        assert struct.unpack_from("<3H", data, at) == (8, 8, 8)
        struct.pack_into("<H", data, at + 4, 16)

    a = no_grid(written(tmp_path, np.zeros((70, 90, 3), "u1"), edit=widen, photometric="rgb"), UNMODELED)
    assert (a.shape, a.item_bits) == ((70, 90, 3), None)


def test_subsampled_stored_ycbcr_has_no_grid(tmp_path):
    """Uncompressed YCbCr samples with TIFF 6.0's default subsampling are not an array of the
    image's shape."""

    def recolor(data: bytearray, page: tifffile.TiffPage) -> None:
        at = page.tags[262].valueoffset
        assert struct.unpack_from("<H", data, at) == (2,)
        struct.pack_into("<H", data, at, 6)

    no_grid(written(tmp_path, np.zeros((70, 90, 3), "u1"), edit=recolor, photometric="rgb"), UNMODELED)


def test_a_volume_has_its_depth_in_its_shape_and_no_grid(tmp_path):
    """ImageDepth makes the image a volume, whose tiles and strips the IR does not model."""
    a = no_grid(written(tmp_path, np.zeros((70, 90), "u1"), {32997: 3}), "a layout the IR does not model")
    assert a.shape == (3, 70, 90)


def test_a_planar_configuration_that_is_neither_has_no_grid(tmp_path):
    def unknown(data: bytearray, page: tifffile.TiffPage) -> None:
        at = page.tags[284].valueoffset
        assert struct.unpack_from("<H", data, at) == (1,)
        struct.pack_into("<H", data, at, 3)

    path = written(tmp_path, np.zeros((70, 90, 3), "u1"), edit=unknown, photometric="rgb")
    no_grid(path, "a layout the IR does not model")


def test_a_strip_outside_the_file_leaves_its_image_no_grid(tmp_path):
    """A unit past the file's end leaves the image no grid, so that a file cut short does not read
    as an image with units the source does not hold."""

    def move(data: bytearray, page: tifffile.TiffPage) -> None:
        struct.pack_into("<I", data, page.tags[273].valueoffset, len(data))

    no_grid(written(tmp_path, np.zeros((70, 90), "u1"), edit=move), "a unit outside the file")


def test_strips_of_no_rows_leave_their_image_no_grid(tmp_path):
    def empty(data: bytearray, page: tifffile.TiffPage) -> None:
        struct.pack_into("<H", data, page.tags[278].valueoffset, 0)

    no_grid(written(tmp_path, np.zeros((70, 90), "u1"), edit=empty), "an empty unit")


def test_an_image_of_no_columns_has_no_grid(tmp_path):
    def narrow(data: bytearray, page: tifffile.TiffPage) -> None:
        struct.pack_into("<H", data, page.tags[256].valueoffset, 0)

    a = no_grid(written(tmp_path, np.zeros((70, 90), "u1"), edit=narrow), "an empty image")
    assert a.shape == (70, 0)


def test_a_strip_of_no_bytes_at_an_offset_leaves_its_image_no_grid(tmp_path):
    """libtiff sizes a lone strip of no bytes from the image, and refuses one among others, and
    tifffile reads the first as the image and the second as absent: the IR does not say which."""

    def unsized(data: bytearray, page: tifffile.TiffPage) -> None:
        struct.pack_into("<I", data, page.tags[279].valueoffset, 0)

    no_grid(written(tmp_path, np.zeros((70, 90), "u1"), edit=unsized), "a unit of no bytes at an offset")


def a_source_chain(path: Path) -> None:
    """The second image of the file at `path` has a grid, and one chain: `source`."""
    said, seen = compared(path)
    assert said == ()
    assert (seen["images"], seen["codec source"], seen["units not decoded"]) == (2, 1, 1)


def test_lerc_under_a_compression_the_list_has_no_name_for_is_source(tmp_path):
    def other(data: bytearray, page: tifffile.TiffPage) -> None:
        at = page.tags[50674].valueoffset
        assert struct.unpack_from("<2I", data, at) == (4, 0)
        struct.pack_into("<I", data, at + 4, 3)

    a_source_chain(written(tmp_path, np.zeros((70, 90), "f4"), edit=other, compression="lerc"))


def test_lerc_parameters_that_are_not_unsigned_integers_are_source(tmp_path):
    """LercParameters of a signed type are not read (and do not stop the parse)."""
    a_source_chain(written(tmp_path, np.zeros((70, 90), "f4"), edit=renamed(50674, 50674, 9), compression="lerc"))
