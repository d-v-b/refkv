"""The stored arrays of the NDPI fixtures (spec/stored-array.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>), compared with
tifffile and imagecodecs, the reference readers: every IFD that declares an image is read by
tifffile at its offset and compared with its description, each unit tifffile lists with its chunk
map row, and the values each row's bytes decode to with the values the references read there.

Three kinds of image are compared:

- a JPEG strip that McuStarts cuts into restart intervals, which tifffile reads as tiles: the
  description's grid, its header place and each row against tifffile's tile size, its patched JPEG
  header and its tile table;
- a JPEG strip read whole, as a TIFF's;
- JPEG XR tiles or a strip, whose samples are the stream's and not the fields': tifffile reads the
  fields, so the description's shape and sample size are compared with what imagecodecs decodes of
  each stream.

A second test asserts which cases the fixtures reached."""

import logging
import math
import struct
from collections import Counter
from functools import cache
from pathlib import Path
from unittest import mock

import imagecodecs
import numpy as np
import pytest
import tifffile
from described import Described, described
from ir_helpers import FIXTURES

from refkv.ir.stored import Each, ElementPlace, Source, StoredArray, Value

FILES = sorted(p for p in (FIXTURES / "ndpi").iterdir() if p.suffix == ".ndpi" and "reject" not in p.name)
# the tags that declare what an image's elements are, after the header's byte order
DECLARING = (258, 262, 320, 338, 339, 529, 530, 532)
JPEG, JPEGXR = 7, 22610
MAGNIFICATION, MCU_STARTS = 65421, 65426
SOF0, EOI = b"\xff\xc0", b"\xff\xd9"


def ndpi_page(tif: tifffile.TiffFile, offset: int) -> tifffile.TiffPage:
    """The page at `offset` as tifffile reads an NDPI's, its McuStarts as a tile table: tifffile
    knows an NDPI page by its Make field (271), which the JPEG fixtures lack, so the test says it
    is one while the page is read."""
    tif.filehandle.seek(offset)
    with mock.patch.object(tifffile.TiffPage, "is_ndpi", new=property(lambda _: True)):
        return tifffile.TiffPage(tif, 0)


def first(page: tifffile.TiffPage, tag: int, default: int) -> int:
    t = page.tags.get(tag)
    if t is None:
        return default
    return int(t.value[0] if isinstance(t.value, tuple) else t.value)


def tag_bytes(page: tifffile.TiffPage, tag: int, data: bytes) -> bytes | None:
    """The bytes of a field's value, as the file holds them."""
    t = page.tags.get(tag)
    if t is None:
        return None
    size = struct.calcsize(tifffile.TIFF.DATA_FORMATS[t.dtype]) * t.count
    return data[t.valueoffset:t.valueoffset + size]


def frame_sized(header: bytes, rows: int, columns: int) -> bytes:
    """A JPEG stream's header with the height and width of its frame header (its first SOF0
    segment) set to `rows` and `columns`: the header of a stream of one unit."""
    at = 2
    while header[at:at + 2] != SOF0:
        assert header[at] == 0xFF, "a marker"
        at += 2 + struct.unpack_from(">H", header, at + 2)[0]
    # the marker, the segment's length and the precision come before the height
    return header[:at + 5] + struct.pack(">HH", rows, columns) + header[at + 9:]


def jpeg_values(page: tifffile.TiffPage, stream: bytes) -> np.ndarray:
    """What a JPEG stream decodes to, in the color space tifffile reads the page in."""
    colorspace, out = tifffile.tifffile.jpeg_decode_colorspace(
        page.photometric, page.planarconfig, page.extrasamples, page.is_jfif)
    return imagecodecs.jpeg8_decode(stream, colorspace=colorspace, outcolorspace=out)


def read(page: tifffile.TiffPage) -> np.ndarray | None:
    """The image tifffile reads of a page; None where it reads none."""
    try:
        return page.asarray()
    except Exception:  # noqa: BLE001  (whatever a decoder raises: tifffile reads no values to compare)
        return None


def lengths(a: StoredArray) -> tuple[int, ...]:
    assert a.grid is not None
    assert all(isinstance(x, Each) for x in a.grid.axes)
    return tuple(x.length for x in a.grid.axes if isinstance(x, Each))


def check_intervals(d: Described, k: int, page: tifffile.TiffPage, offset: int, data: bytes,
                    seen: Counter) -> list[str]:
    """A JPEG strip McuStarts cuts, against the tiles tifffile reads of it: a restart interval a
    unit, its bytes without the restart marker or EOI after them, in a chain whose codec has the
    strip's header. `offset` is the strip's."""
    a = d.stored.arrays[k]
    said = []
    if (a.shape, a.item_bits, a.pad_from) != (page.shape, 8, None):
        said.append(f"shape {a.shape} of {a.item_bits} bits, tifffile {page.shape}")
    down, across = page.tilelength, page.tilewidth
    if a.grid is None or (lengths(a), a.grid.edge) != ((down, across, 3), "full"):
        return [*said, f"grid {a.grid}, tifffile's tiles {down} by {across}"]
    codecs = [c for chain in a.chains for c in d.stored.chains[chain]]
    header = codecs[0].parameters.get("header") if codecs else None
    if [(c.name, set(c.parameters)) for c in codecs] != [("jpeg_restart_interval", {"header"})] or not isinstance(
            header, Source):
        return [*said, f"chain {codecs}"]
    # the header is the strip's bytes before its first interval, and tifffile's is it with a tile's size
    held = d.place(header.place)
    if held != data[offset:page.dataoffsets[0]] or frame_sized(held, down, across) != page.jpegheader:
        said.append("the header is not the strip's bytes before its first interval")
    columns = math.ceil(page.shape[1] / across)
    rows = {tuple(r["coords"]): r for r in d.rows.get(k, [])}
    if len(rows) != len(page.dataoffsets):
        said.append(f"{len(rows)} rows, tifffile lists {len(page.dataoffsets)} tiles")
    theirs = read(page)
    mine = np.zeros((math.ceil(page.shape[0] / down) * down, columns * across, 3), "u1")
    for i, (o, n) in enumerate(zip(page.dataoffsets, page.databytecounts, strict=True)):
        coords = (i // columns, i % columns, 0)
        row = rows.get(coords)
        seen["intervals"] += 1
        # tifffile's tile runs to the next one's start: the last 2 bytes are the marker between them
        after = data[o + n - 2:o + n]
        last = i == len(page.dataoffsets) - 1
        if after != (EOI if last else bytes([0xFF, 0xD0 + i % 8])):
            said.append(f"interval {coords} is followed by {after!r}")
        if row is None or d.parts(row) != [(None, o, n - 2)] or row["element"] != d.rows[k][0]["element"]:
            said.append(f"interval {coords} at {None if row is None else d.parts(row)}, tifffile {o} of {n}")
            continue
        got = jpeg_values(page, frame_sized(held, down, across) + d.unit(row) + EOI)
        mine[coords[0] * down:(coords[0] + 1) * down, coords[1] * across:(coords[1] + 1) * across] = got
    if theirs is None or mine[:page.shape[0], :page.shape[1]].tobytes() != theirs.tobytes():
        said.append("the intervals decode to other values than tifffile reads")
    seen["images cut into intervals"] += 1
    return said


def check_units(d: Described, k: int, page: tifffile.TiffPage, seen: Counter) -> list[str]:
    """An image in tiles or strips, as a TIFF's, against tifffile's table: a row for each unit that
    holds bytes, the unit's bytes."""
    a = d.stored.arrays[k]
    assert a.shape is not None
    height, width = a.shape[:2]
    if page.is_tiled:
        unit, edge = (page.tilelength, page.tilewidth), "full"
    else:
        unit, edge = (min(first(page, 278, 2**32 - 1), height), width), "short"
    if a.grid is None or (lengths(a)[:2], a.grid.edge) != (unit, edge) or lengths(a)[2:] != a.shape[2:]:
        return [f"grid {a.grid}, tifffile's units {unit} {edge}"]
    columns = math.ceil(width / unit[1])
    rows = {tuple(r["coords"]): r for r in d.rows.get(k, [])}
    said, held = [], 0
    for i, (o, n) in enumerate(zip(page.dataoffsets, page.databytecounts, strict=True)):
        coords = (i // columns, i % columns, *[0] * (len(a.shape) - 2))
        row = rows.get(coords)
        if n == 0 or row is None:
            if (n == 0) != (row is None):
                said.append(f"unit {coords}: {'a row' if n == 0 else 'no row'}, tifffile lists {n} bytes")
            continue
        held += 1
        seen["units"] += 1
        if d.parts(row) != [(None, o, n)] or row["range"] is not None:
            said.append(f"unit {coords} at {d.parts(row)}, tifffile {o} of {n}")
    if held != len(rows):
        said.append(f"{len(rows)} rows, {held} units tifffile lists with bytes")
    return said


def unit_shape(a: StoredArray, coords: tuple[int, ...]) -> tuple[int, ...]:
    """The shape of the unit at `coords` of the array's grid: cut at the array's end under `short`."""
    assert a.grid is not None
    assert a.shape is not None
    full = lengths(a)
    if a.grid.edge == "full":
        return full
    return tuple(min(n, size - c * n) for n, c, size in zip(full, coords, a.shape, strict=True))


def check_decoded(d: Described, k: int, page: tifffile.TiffPage, name: str, seen: Counter) -> list[str]:
    """Each unit of an image whose chain is one codec that makes the elements, decoded by
    imagecodecs: an array of the unit's shape and sample size, and, where tifffile reads the image,
    its values there."""
    a = d.stored.arrays[k]
    assert a.shape is not None
    assert a.item_bits is not None
    theirs = read(page)
    # tifffile reads a JPEG XR page by its fields, which say other samples than a 16-bit stream holds
    comparable = theirs is not None and theirs.shape == a.shape and theirs.dtype.itemsize * 8 == a.item_bits
    seen["images tifffile reads"] += comparable
    said = []
    for row in d.rows.get(k, []):
        coords = tuple(row["coords"])
        shape = unit_shape(a, coords)
        try:
            got = imagecodecs.jpegxr_decode(d.unit(row)) if name == "jpegxr" else jpeg_values(page, d.unit(row))
        except Exception:  # noqa: BLE001  (whatever a decoder raises: no reference reads the unit)
            seen["units no decoder reads"] += 1
            continue
        if got.shape != shape:
            # a JPEG strip whose frame is of another size than the image's fields give
            # (spec/zarrify/ndpi.md §4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#4-the-image>)
            seen["units of a frame of another size"] += 1
            if name != "jpeg":
                said.append(f"unit {coords} decodes to {got.shape}, not its unit's {shape}")
            continue
        if got.dtype.itemsize * 8 != a.item_bits:
            said.append(f"unit {coords} decodes to {got.dtype}, not samples of {a.item_bits} bits")
            continue
        seen["units decoded"] += 1
        if comparable:
            assert theirs is not None
            at = tuple(slice(c * n, c * n + n) for c, n in zip(coords[:2], lengths(a)[:2], strict=True))
            region = theirs[at]
            if got[:region.shape[0], :region.shape[1]].tobytes() != region.tobytes():
                said.append(f"unit {coords} decodes to other values than tifffile reads")
    return said


def check_image(d: Described, k: int, tif: tifffile.TiffFile, offset: int, data: bytes, seen: Counter) -> list[str]:
    """One IFD's image against its description."""
    a = d.stored.arrays[k]
    tif.filehandle.seek(offset)
    page = tifffile.TiffPage(tif, 0)
    said = []
    if a.fill != "undefined" or a.axes is not None or a.index is not None or a.unit_parameters:
        said.append("a fill value, axes, an index or unit parameters")
    declared = [data[:2]] + [b for t in DECLARING if (b := tag_bytes(page, t, data)) is not None]
    if [d.place(p) for p in a.declarations] != declared or not all(isinstance(p, ElementPlace) for p in a.declarations):
        said.append("the declarations are not the byte order and the declaring fields' values")
    compression = first(page, 259, 1)
    names = [c.name for chain in a.chains for c in d.stored.chains[chain]]
    # an image of the convention (spec/zarrify/ndpi.md §3
    # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#3-levels>), whose strip's header
    # the parser reads: tifffile cuts its strip by McuStarts
    image = MAGNIFICATION in page.tags and float(page.tags[MAGNIFICATION].value) > 0
    if compression == JPEG and image and MCU_STARTS in page.tags:
        return said + check_intervals(d, k, ndpi_page(tif, offset), page.dataoffsets[0], data, seen)
    if compression == JPEGXR and names == ["jpegxr"]:
        seen["images of JPEG XR"] += 1
        seen["images of 16-bit samples"] += a.item_bits == 16
        seen["images of one sample"] += a.shape is not None and len(a.shape) == 2
    elif compression == JPEGXR:
        # a JPEG XR image that is none of the convention's, whose stream the parser does not read
        seen["images of an unread JPEG XR stream"] += 1
        want = [{"format": "ndpi", "id": JPEGXR}]
        params = [{n: v.value for n, v in c.parameters.items() if isinstance(v, Value)}
                  for chain in a.chains for c in d.stored.chains[chain]]
        if names != ["source"] or params != want or (a.shape, a.item_bits) != (page.shape, 8):
            said.append(f"chain {names} {params} of shape {a.shape}, tifffile {page.shape}")
        return said + check_units(d, k, page, seen)
    elif names != ["jpeg"] or (a.shape, a.item_bits) != (page.shape, 8):
        return [*said, f"chain {names} of shape {a.shape}, tifffile {page.shape} of compression {compression}"]
    else:
        seen["images of a strip read whole"] += 1
        seen["strips with McuStarts read whole"] += MCU_STARTS in page.tags
    return said + check_units(d, k, page, seen) + check_decoded(d, k, page, names[0], seen)


@cache
def compared(path: Path) -> tuple[tuple[str, ...], Counter]:
    """What differs between the descriptions of the file at `path` and the reference readers, and
    what the comparison saw."""
    logging.getLogger("tifffile").setLevel(logging.CRITICAL)
    d = described(path)
    data = d.objects[None]
    said: list[str] = []
    seen: Counter = Counter()
    if d.stored.undescribed:
        said.append("descriptions dropped for room")
    by_ifd = {d.at(a.element)[1]: k for k, a in enumerate(d.stored.arrays)}
    assert len(by_ifd) == len(d.stored.arrays)
    with tifffile.TiffFile(path) as tif:
        assert tif.tiff.is_ndpi, "tifffile reads the file in NDPI's layout"
        # every page tifffile reaches that declares a size is described
        sized = [p.offset for p in tif.pages if isinstance(p, tifffile.TiffPage) and 256 in p.tags and 257 in p.tags]
        said += [f"no description of the IFD at {o}" for o in sized if o not in by_ifd]
        for offset, k in sorted(by_ifd.items()):
            seen["images"] += 1
            said += [f"{d.stored.arrays[k].path}: {s}" for s in check_image(d, k, tif, offset, data, seen)]
    return tuple(said), seen


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_ndpi_images_are_described_as_the_references_read_them(path):
    """Every image of the file is described as tifffile reads it, and its units decode as
    imagecodecs decodes them."""
    said, seen = compared(path)
    assert said == ()
    assert seen["images"] >= 1


def test_the_fixtures_hold_every_case_the_comparison_reads():
    """The fixtures reach each case of the comparison, so that none of it passes for want of a
    file. The units no decoder reads are a macro image's two strips, which together are one JPEG
    stream; the strip with McuStarts read whole is an image of no Magnification, whose strip's
    header the parser does not read."""
    seen: Counter = Counter()
    for path in FILES:
        seen += compared(path)[1]
    assert {k: v for k, v in seen.items() if v} == {
        "images": 50,
        "images cut into intervals": 12,
        "intervals": 5484,
        "images of a strip read whole": 25,
        "strips with McuStarts read whole": 1,
        "images of JPEG XR": 12,
        "images of 16-bit samples": 8,
        "images of one sample": 8,
        "images of an unread JPEG XR stream": 1,
        "units": 61,
        "units decoded": 55,
        "units of a frame of another size": 3,
        "units no decoder reads": 2,
        "images tifffile reads": 25,
    }
