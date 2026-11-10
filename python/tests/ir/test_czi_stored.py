"""The stored arrays of the CZI fixtures (spec/stored-array.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>), compared with
czifile, the reference reader, and imagecodecs, which czifile decodes with. Of every file of an
input (an image across files is a store of them), every subblock czifile lists is found among the
descriptions by its segment's offset, and of each

- the declarations are the subblock's own copy of its entry, where the file holds it;
- the shape and the sample size are those of the entry czifile reads: its stored height and width,
  and its pixel type's samples and their size;
- the array is one unit, whose row is the bytes czifile reads the subblock's data from (the pixels
  alone, of an uncompressed subblock with bytes after them);
- the chain is the one the entry's compression and the stream's own header give, which the test
  reads again by the rules of that section, as it reads whether the data is what the entry says
  at all: where it is not, the array has no grid;
- the unit's bytes, decoded through the chain, are the values czifile reads of the subblock.

czifile 2026.8.16 departs from what a subblock's entry and stream declare in these ways, each
counted where it is met:

- it gives the samples of an uncompressed, LZW or Zstd subblock of three or four in red, green,
  blue order, where the file stores blue, green, red: the test puts them back;
- it gives a fourth sample (alpha) of 255 whatever the file holds, so alpha is not compared;
- it has no decoder for Compression 3 (lossless JPEG), and reads no such subblock: their units
  are compared with imagecodecs' second lossless JPEG decoder (LJ92) instead;
- it reads an uncompressed subblock with bytes after its pixels as an array of all its bytes,
  which is none of its stored size, and so reads none;
- it reads data that is not what the entry says, which has no grid, as another array than the
  entry declares: data or a frame of fewer bytes as an array of fewer rows, an 8-bit stream under
  Gray16 as 8-bit samples, and a Zstd1 frame of 8-bit samples whose header says hi-lo packing as
  the frame's content;
- it reads a subblock by the directory's entry when it comes to it from the directory, and by the
  subblock's own copy when it walks the segments: a subblock whose two differ has no shape.

A second test asserts which cases the fixtures reached, and further tests write the cases they
lack."""

import json
import logging
import re
import struct
import sys
from collections import Counter
from dataclasses import dataclass
from functools import cache
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType

import czifile
import imagecodecs
import numpy as np
import pytest
from described import Described, described
from ir_helpers import FIXTURES, ROOT

from refkv.ir.kind import Kind
from refkv.ir.stored import Each, ElementPlace, ExtentPlace, Place

REJECTS = ROOT / "conformance" / "zarrify" / "rejects" / "czi.json"
REJECTED = {c["input"] for c in json.loads(REJECTS.read_text())["cases"]}
# a part of an image across files is no input: its master's store reads it (`name(1).czi`, of
# `name.czi`). One fixture of such a name is no part of the file its name gives, and is an input
PART = re.compile(r"\((\d+)\)\.czi$")
ALONE = "czi_parts_stranger(1).czi"
FILES = sorted(p for p in (FIXTURES / "czi").iterdir()
               if p.name not in REJECTED and (PART.search(p.name) is None or p.name == ALONE))

UNCOMPRESSED, JPEG, LZW, LJPEG, JPEGXR, ZSTD0, ZSTD1 = range(7)
GRAY8, GRAY16, BGR24, BGR48, BGR96, BGRA32 = 0, 1, 3, 4, 8, 9
# a codec header lies within the first 2^16 bytes of a subblock's data
HEAD = 1 << 16
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
# after the magic of the frame of 24 bytes the tests rewrite: a single segment, its content size
SIZED = b"\x20\x18"
# the JPEG XR pixel formats a pixel type admits: the last byte of a WIC GUID, or a whole GUID
WIC = bytes.fromhex("24C3DD6F034EFE4BB1853D77768DC9")
BGR96_FORMAT = bytes.fromhex("8FD7FEE3DBE8CF4A84C1E97F6136B327")
ADMITS = {GRAY8: [0x08], GRAY16: [0x0B], 2: [0x11], BGR24: [0x0C, 0x0D], BGR48: [0x15], BGRA32: [0x0F]}
# the samples of a pixel czifile gives in another order than the file stores
SWAPPED = [2, 1, 0]

type Named = tuple[czifile.CziDirectoryEntryDV, bytes]
"""an entry as czifile reads it, and its bytes"""


def czi_generator() -> ModuleType:
    """The CZI fixtures' writer, under a name of its own: each format's generator is `write_fixtures`."""
    spec = spec_from_file_location("czi_fixture_writer", ROOT / "fixtures" / "generators" / "czi" / "write_fixtures.py")
    assert spec is not None
    assert spec.loader is not None
    module = module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses look their module up
    spec.loader.exec_module(module)
    return module


writer = czi_generator()


def subblock_segments(data: bytes) -> list[int]:
    """The offsets of a file's `ZISRAWSUBBLOCK` segments, by the walk of spec/zarrify/czi.md §7.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#72-structures-and-the-walk>:
    from one segment's header to the next, until no header is read."""
    found, at = [], 0
    while at + 32 <= len(data):
        name = data[at:at + 16].rstrip(b"\0")
        allocated, used = struct.unpack_from("<qq", data, at + 16)
        if not re.fullmatch(rb"[A-Z0-9_]+", name) or allocated < 0 or used < 0 or at + 32 + allocated > len(data):
            break
        if name == b"ZISRAWSUBBLOCK":
            found.append(at)
        at += 32 + allocated
    return found


def zstd_frame_of(b: bytes, size: int) -> bool:
    """Whether `b` starts a Zstandard frame that names no dictionary and declares, where it
    declares one, a content size of `size` bytes (spec/zarrify/czi.md §7.4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#74-codec-headers> reads the
    header)."""
    if len(b) < 5 or b[:4] != ZSTD_MAGIC or b[4] & 0x08 or b[4] & 0x03:
        return False
    single = bool(b[4] & 0x20)
    n = (1 if single else 0, 2, 4, 8)[b[4] >> 6]
    at = 5 if single else 6
    if n == 0:
        return True
    if len(b) < at + n:
        return False
    v = int.from_bytes(b[at:at + n], "little")
    return (v + 256 if n == 2 else v) == size


def zstd1_header(b: bytes) -> tuple[int, bool] | None:
    """(length, hi-lo packing) of a Zstd1 subblock's header."""
    if b[:1] == b"\x01":
        return 1, False
    if len(b) >= 3 and b[:2] == b"\x03\x01":
        return 3, bool(b[2] & 1)
    return None


@dataclass(frozen=True)
class Frame:
    """A JPEG stream's frame header."""

    marker: int
    precision: int
    height: int
    width: int
    components: int


def jpeg_frame(b: bytes) -> Frame | None:
    """The frame header of a JPEG stream, where its first bytes hold one before any scan."""
    if b[:2] != b"\xff\xd8":
        return None
    at = 2
    while at + 4 <= len(b):
        if b[at] != 0xFF:
            return None
        while at < len(b) and b[at] == 0xFF:
            at += 1
        if at >= len(b):
            return None
        marker = b[at]
        at += 1
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            continue
        if marker in (0xDA, 0xD9) or at + 2 > len(b):
            return None
        length = struct.unpack_from(">H", b, at)[0]
        if length < 2:
            return None
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if at + 8 > len(b):
                return None
            precision, height, width, components = struct.unpack_from(">BHHB", b, at + 2)
            return Frame(marker, precision, height, width, components)
        at += length
    return None


def jpegxr_format(b: bytes) -> bytes | None:
    """The pixel format of a JPEG XR file's container, where it gives one and a size."""
    if len(b) < 8 or b[:4] != b"II\xbc\x01":
        return None
    ifd = struct.unpack_from("<I", b, 4)[0]
    if ifd + 2 > len(b):
        return None
    found: dict[int, tuple[int, int, bytes]] = {}
    for k in range(struct.unpack_from("<H", b, ifd)[0]):
        at = ifd + 2 + 12 * k
        if at + 12 > len(b):
            return None
        tag, kind, count = struct.unpack_from("<HHI", b, at)
        found.setdefault(tag, (kind, count, b[at + 8:at + 12]))
    if not {0xBC01, 0xBC80, 0xBC81} <= set(found):
        return None
    kind, count, value = found[0xBC01]
    at = struct.unpack("<I", value)[0]
    if (kind, count) != (1, 16) or at + 16 > len(b):
        return None
    for tag in (0xBC80, 0xBC81):
        kind, count, value = found[tag]
        if count != 1 or kind not in (3, 4):
            return None
        if struct.unpack("<H" if kind == 3 else "<I", value[:2 if kind == 3 else 4])[0] < 1:
            return None
    return b[at:at + 16]


def chain_of(pixel_type: int, compression: int, size: int, samples: int, item: int, data: bytes) -> list[str] | None:
    """The codecs of the chain of a subblock's data, of `size` bytes decoded, of `samples` samples
    of `item` bytes a pixel; None where the data is not what the entry says, which has no grid
    (spec/stored-array.md §7
    <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>). The
    conditions are the profile's for a coded size (spec/zarrify/czi.md §3.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#32-conforming-and-unplaced-subblocks>),
    but that a lossless JPEG stream may have three components, and a Zstandard frame may declare
    no content size."""
    head = data[:HEAD]
    if compression == UNCOMPRESSED:
        return [] if len(data) >= size else None
    if compression == ZSTD0:
        return ["zstd"] if zstd_frame_of(head, size) else None
    if compression == ZSTD1:
        header = zstd1_header(head)
        if header is None or len(data) <= header[0] or (header[1] and item != 2):
            return None
        return ["czi_zstd1"] if zstd_frame_of(head[header[0]:], size) else None
    if compression == LZW:
        return ["tiff_lzw"] if len(head) >= 2 and head[0] == 0x80 and not head[1] & 0x80 else None
    if compression == JPEGXR:
        guid = jpegxr_format(head)
        if guid is None:
            return None
        admitted = guid == BGR96_FORMAT if pixel_type == BGR96 else (
            guid[:15] == WIC and guid[15] in ADMITS.get(pixel_type, []))
        return ["jpegxr"] if admitted else None
    frame = jpeg_frame(head)
    if frame is None or frame.height < 1 or frame.width < 1 or frame.components != samples:
        return None
    lossless = frame.marker == 0xC3 and (2 <= frame.precision <= 8 if item == 1 else 9 <= frame.precision <= 16)
    lossy = compression == JPEG and frame.marker in (0xC0, 0xC1, 0xC2) and frame.precision == (8 if item == 1 else 12)
    if not (lossless or lossy):
        return None
    # the list's `jpeg`, of samples of 8 or 16 bits: the precision is at most theirs
    return ["jpeg"]


def unpacked(unit: bytes) -> bytes:
    """What `czi_zstd1` makes of a unit (spec/stored-array.md §3.2
    <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#32-the-list>): the content of
    the frame after the header, its low bytes and high bytes put together where the header says
    they are apart."""
    header = zstd1_header(unit)
    assert header is not None
    content = bytes(imagecodecs.zstd_decode(unit[header[0]:]))
    if not header[1]:
        return content
    return np.frombuffer(content, "u1").reshape(2, len(content) // 2).T.tobytes()


def decoded(names: list[str], unit: bytes, dtype: np.dtype, shape: tuple[int, ...]) -> np.ndarray:
    """A unit decoded through its chain."""
    if names == ["jpeg"]:
        return np.asarray(imagecodecs.jpeg8_decode(unit))
    if names == ["jpegxr"]:
        return np.asarray(imagecodecs.jpegxr_decode(unit))
    if not names:
        made = unit
    elif names == ["zstd"]:
        made = bytes(imagecodecs.zstd_decode(unit))
    elif names == ["czi_zstd1"]:
        made = unpacked(unit)
    else:
        assert names == ["tiff_lzw"], names
        made = bytes(imagecodecs.lzw_decode(unit))
    # the stream's bytes, up to the unit's
    size = int(np.prod(shape)) * dtype.itemsize
    assert len(made) >= size, f"{len(made)} bytes, of a unit of {size}"
    return np.frombuffer(made[:size], dtype).reshape(shape)


def czifile_reads(segment: czifile.CziSubBlockSegmentData) -> np.ndarray | None:
    """The subblock's values as czifile reads them; None where it reads none."""
    try:
        return np.asarray(segment.data())
    except Exception:  # noqa: BLE001  (whatever a decoder raises: czifile reads no values to compare)
        return None


def agree(copy: bytes, entry: bytes, part: int) -> bool:
    """Whether a subblock's copy of its entry is the entry: in a part, whatever FilePart each
    names (spec/zarrify/czi.md §7.9
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#79-several-files>)."""
    if part == 0 or len(copy) != len(entry):
        return copy == entry
    return copy[:14] == entry[:14] and copy[18:] == entry[18:]


def held(d: Described, a_element: int, name: str, key: str | None, at: int, length: int) -> tuple[Place, int | None]:
    """The place of the `length` bytes at `at` of the file `key` that the child `name` of a
    subblock's struct holds, and that child: the element, or, where the finished IR made it an
    alias, which holds no bytes of its own, the range of the file."""
    element = d.ir.child(a_element, name)
    assert element is not None, name
    assert d.at(element) == (key, at, length), name
    if d.ir.kind(element) == Kind.ALIAS:
        return ExtentPlace(key, at, length), None
    return ElementPlace(element, None, None), element


def check_values(names: list[str], unit: bytes, ours: np.ndarray, theirs: np.ndarray | None,
                 entry: czifile.CziDirectoryEntryDV, shape: tuple[int, ...], item_bits: int | None, trailing: bool,
                 seen: Counter) -> list[str]:
    """A unit's decoded values against czifile's of the subblock, which has bytes after its
    pixels where `trailing`; where czifile has no decoder (lossless JPEG), against a second
    decoder's."""
    samples, compression = entry.pixel_type.samples, int(entry.compression._value_)
    said = []
    if ours.dtype.itemsize * 8 != item_bits:
        said.append(f"decodes to {ours.dtype}, not samples of {item_bits} bits")
    if ours.shape != shape:
        # a stream of another size than its entry's stored size, as some writers leave a
        # pyramid's edge (spec/zarrify/czi.md §3.2
        # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#32-conforming-and-unplaced-subblocks>)
        seen["units of a frame of another size"] += 1
        if names not in (["jpeg"], ["jpegxr"]) or ours.shape[2:] != shape[2:]:
            said.append(f"decodes to {ours.shape}, not its unit's {shape}")
    seen["units decoded"] += 1
    if theirs is None or theirs.size != ours.size:
        # czifile's own: no decoder for lossless JPEG, and bytes after the pixels read as pixels,
        # which are no array of the entry's size
        if compression == LJPEG and theirs is None:
            seen["units czifile has no decoder for"] += 1
            # the list's `jpeg` decoded by libjpeg-turbo: against imagecodecs' other lossless
            # JPEG decoder (LJ92)
            other = np.asarray(imagecodecs.ljpeg_decode(unit))
            if other.astype(ours.dtype).tobytes() != ours.tobytes():
                said.append("decodes to other values than imagecodecs' lossless JPEG decoder's")
            seen["units decoded to a second lossless JPEG decoder's values"] += 1
        elif trailing:
            seen["units czifile reads none of, for the bytes after them"] += 1
        else:
            said.append(f"czifile reads {None if theirs is None else theirs.size} values, the unit holds {ours.size}")
        return said
    theirs = theirs.reshape(ours.shape)
    if samples > 1 and compression in (UNCOMPRESSED, LZW, ZSTD0, ZSTD1):
        seen["units whose samples czifile reorders"] += 1
        if samples == 4:
            seen["units whose alpha czifile sets"] += 1
            if not (theirs[..., 3] == 255).all():
                said.append("czifile gives an alpha other than 255")
        ours, theirs = ours[..., :3], theirs[..., SWAPPED]
    if ours.tobytes() != theirs.tobytes():
        said.append("decodes to other values than czifile reads")
    return said


def check_subblock(d: Described, k: int, key: str | None, c: czifile.CziFile, entry: czifile.CziDirectoryEntryDV,
                   named: bytes, part: int, seen: Counter) -> list[str]:
    """One subblock's description against the subblock as czifile reads it by the entry that
    names it first, whose bytes are `named`."""
    a = d.stored.arrays[k]
    data = d.objects[key]
    at = entry.file_position
    count = struct.unpack_from("<i", data, at + 76)[0]
    copy = data[at + 48:at + 80 + 20 * count]
    said = []
    if (a.fill, a.axes, a.index, a.unit_parameters, a.pad_from) != ("undefined", None, None, (), None):
        said.append("a fill value, axes, an index, unit parameters or padding")
    want = [held(d, a.element, "entry", key, at + 48, 32)[0]]
    if count:
        want.append(held(d, a.element, "dimensions", key, at + 80, 20 * count)[0])
    seen["declarations that are ranges of the file"] += sum(isinstance(p, ExtentPlace) for p in want)
    if list(a.declarations) != want:
        said.append(f"the declarations are {a.declarations}, the subblock's copy of its entry {want}")
    rows = d.rows.get(k, [])
    if not agree(copy, named, part):
        # declared two ways: czifile follows the directory's entry from the directory, and the
        # subblock's own copy where it walks the segments
        seen["subblocks declared two ways"] += 1
        own = czifile.CziSegment(c, at).data()
        assert isinstance(own, czifile.CziSubBlockSegmentData)
        ways = [(e.dims, e.stored_shape, e.start, int(e.pixel_type), int(e.compression._value_), e.mosaic_index)
                for e in (entry, own.directory_entry)]
        seen["subblocks czifile reads two ways"] += ways[0] != ways[1]
        if a.shape is not None or a.item_bits is not None or a.grid is not None or a.chains or rows:
            said.append("a shape, a sample size or a grid, of a subblock whose copy of its entry is not the entry")
        return said
    segment = entry.read_segment_data(c)
    assert isinstance(segment, czifile.CziSubBlockSegmentData)
    sizes = dict(zip(entry.dims, entry.stored_shape, strict=True))
    samples, dtype = entry.pixel_type.samples, entry.pixel_type.dtype
    pixel_type, compression = int(entry.pixel_type), int(entry.compression._value_)
    shape = (sizes["Y"], sizes["X"], *([samples] if samples > 1 else []))
    if (a.shape, a.item_bits) != (shape, dtype.itemsize * 8):
        said.append(f"shape {a.shape} of {a.item_bits} bits; czifile's entry {shape} of {dtype}")
    size = int(np.prod(shape)) * dtype.itemsize
    stored = data[segment.data_offset:segment.data_offset + segment.data_size]
    names = chain_of(pixel_type, compression, size, samples, dtype.itemsize, stored)
    theirs = czifile_reads(segment)
    if names is None:
        # data that is not what the entry says: what czifile makes of it is counted
        seen["subblocks whose data is not their entry's"] += 1
        if theirs is None:
            seen["of them, czifile reads none"] += 1
        elif theirs.dtype != dtype:
            seen["of them, czifile reads other samples"] += 1
        elif theirs.size != size // dtype.itemsize:
            fewer = theirs.size < size // dtype.itemsize
            seen["of them, czifile reads fewer values" if fewer else "of them, czifile reads more values"] += 1
        else:
            seen["of them, czifile reads the entry's array"] += 1
        if a.grid is not None or a.chains or rows:
            said.append("a grid, of a subblock whose data is not its entry's")
        return said
    codecs = [x for chain in a.chains for x in d.stored.chains[chain]]
    if [x.name for x in codecs] != names or len(a.chains) != 1 or any(x.parameters for x in codecs):
        said.append(f"chain {[(x.name, x.parameters) for x in codecs]}, the stream's {names}")
    if a.grid is None or a.grid.edge != "short" or a.grid.axes != tuple(Each(n) for n in shape):
        return [*said, f"grid {a.grid}, of a subblock of {shape}"]
    # the pixels alone of an uncompressed subblock, and else all its data
    length = size if compression == UNCOMPRESSED else segment.data_size
    trailing = compression == UNCOMPRESSED and length < segment.data_size
    seen["uncompressed subblocks with bytes after their pixels"] += trailing
    if len(rows) != 1:
        return [*said, f"{len(rows)} rows"]
    (row,) = rows
    if (d.parts(row) != [(key, segment.data_offset, length)] or row["coords"] != [0] * len(shape)
            or row["range"] is not None or row["chain"] is not None or row["skips"] is not None
            or any(v is not None for n, v in row.items() if n.startswith("p."))):
        said.append(f"its unit at {d.parts(row)}, czifile's data at {segment.data_offset} of {length}")
    # the unit is the `data` element; a Zstd1 subblock's header and frame are two elements, and
    # an alias holds no bytes, so the unit of either is a range of the file
    element = None if compression == ZSTD1 else held(d, a.element, "data", key, segment.data_offset, length)[1]
    seen["units that are ranges of the file"] += element is None
    if row["element"] != element:
        said.append(f"its unit is element {row['element']}, its data's {element}")
    seen[f"units of {names}"] += 1
    unit = d.unit(row)
    ours = decoded(names, unit, dtype, shape)
    return said + check_values(names, unit, ours, theirs, entry, shape, a.item_bits, trailing, seen)


def check_file(d: Described, key: str | None, path: Path, part: int, given: list[Named], seen: Counter) -> list[str]:
    """The subblocks of one file of an input against their descriptions: `given` are the entries
    of its master that name its subblocks, with their bytes."""
    data = d.objects[key]
    said = []
    described_at = {d.at(a.element)[1]: k for k, a in enumerate(d.stored.arrays) if d.key(a.element) == key}
    with czifile.CziFile(path) as c:
        has_directory = c.header.directory_position != 0
        # the entries that name this file's subblocks, the master's first: czifile's of a file
        # with no directory are the copies its walk finds
        own = [(e, data[e.offset:e.offset + e.storage_size]) for e in c.subblock_directory]
        seen["entries of another file's subblocks"] += sum(e.file_part not in (0, part) for e, _ in own)
        first: dict[int, tuple[czifile.CziDirectoryEntryDV, bytes]] = {}
        for e, b in [*given, *((e, b) for e, b in own if e.file_part in (0, part))]:
            seen["entries"] += 1
            seen["entries of a subblock an earlier one names"] += e.file_position in first
            first.setdefault(e.file_position, (e, b))
        for at, (e, b) in first.items():
            k = described_at.get(at)
            if k is None:
                # a part with no directory is not walked where its master's entries are all
                # the image lacks (spec/zarrify/czi.md §7.9
                # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#79-several-files>)
                if has_directory or (e, b) in given:
                    said.append(f"no description of the subblock at {at}")
                seen["subblocks of a part that is not walked"] += 1
                continue
            seen["subblocks"] += 1
            said += [f"{d.stored.arrays[k].path}: {s}" for s in check_subblock(d, k, key, c, e, b, part, seen)]
        # a subblock segment no entry names is not read, and not described
        seen["subblock segments no entry names"] += len(set(subblock_segments(data)) - set(first))
        unnamed = sorted(set(described_at) - set(first))
        said += [f"a description of the segment at {at}, which no entry names" for at in unnamed]
    return said


@cache
def compared(path: Path) -> tuple[tuple[str, ...], Counter]:
    """What differs between the descriptions of the input at `path` and czifile's reading of its
    files, and what the comparison saw."""
    logging.getLogger("czifile").setLevel(logging.CRITICAL)
    d = described(path)
    said: list[str] = []
    seen: Counter = Counter()
    if d.stored.undescribed:
        said.append("descriptions dropped for room")
    keys = list(d.objects)
    master: list[tuple[czifile.CziDirectoryEntryDV, bytes]] = []
    if keys != [None]:
        # a store of the named file and its parts
        named = d.objects[path.name]
        with czifile.CziFile(path) as c:
            master = [(e, named[e.offset:e.offset + e.storage_size]) for e in c.subblock_directory]
    for key in sorted(keys, key=lambda k: (k is not None and k != path.name, k or "")):
        number = None if key is None else PART.search(key)
        part = 0 if number is None or key == path.name else int(number[1])
        seen["files read as parts"] += part > 0
        given = [(e, b) for e, b in master if part and e.file_part == part]
        name = key or path.name
        said += [f"{name}: {s}" for s in check_file(d, key, path.parent / name, part, given, seen)]
    seen["descriptions"] += len(d.stored.arrays)
    return tuple(said), seen


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_czi_subblocks_are_described_as_czifile_reads_them(path):
    """Every subblock of the input is described as czifile reads it, and its unit decodes to the
    values czifile reads."""
    said, _ = compared(path)
    assert said == ()


def test_the_fixtures_hold_every_case_the_comparison_reads():
    """The fixtures reach each case of the comparison, so that none of it passes for want of a
    file. Of the 13 subblocks whose data is not their entry's, czifile reads six: a frame and
    uncompressed data of three rows of four as three rows; an 8-bit JPEG stream and an 8-bit JPEG
    XR file under Gray16 as 8-bit samples, and a 12-bit JPEG stream under Gray8 as 16-bit ones;
    and a Zstd1 frame of 8-bit samples whose header says hi-lo packing as the entry's array. The
    16 `jpeg` units have among them 12-bit and 16-bit streams, and a lossless stream of three
    components, which the profile does not place; czifile has no decoder for the 3 lossless ones,
    which a second decoder reads. The seven parts are every fixture that is a part of another's image."""
    seen: Counter = Counter()
    for path in FILES:
        seen += compared(path)[1]
    parts = [p.name for p in (FIXTURES / "czi").iterdir() if p.name not in REJECTED and p not in FILES]
    assert len(parts) == seen["files read as parts"]
    assert {k: v for k, v in seen.items() if v} == {
        "files read as parts": 7,
        "entries": 201,
        "entries of another file's subblocks": 3,
        "entries of a subblock an earlier one names": 3,
        "subblocks": 198,
        "descriptions": 198,
        "subblock segments no entry names": 1,
        "subblocks declared two ways": 1,
        "subblocks whose data is not their entry's": 13,
        "of them, czifile reads none": 7,
        "of them, czifile reads other samples": 3,
        "of them, czifile reads fewer values": 2,
        "of them, czifile reads the entry's array": 1,
        "units of []": 127,
        "units of ['zstd']": 3,
        "units of ['czi_zstd1']": 4,
        "units of ['tiff_lzw']": 17,
        "units of ['jpeg']": 16,
        "units of ['jpegxr']": 17,
        "units that are ranges of the file": 4,
        "units decoded": 184,
        "units of a frame of another size": 1,
        "uncompressed subblocks with bytes after their pixels": 1,
        "units czifile reads none of, for the bytes after them": 1,
        "units czifile has no decoder for": 3,
        "units decoded to a second lossless JPEG decoder's values": 3,
        "units whose samples czifile reorders": 12,
        "units whose alpha czifile sets": 2,
    }


def written(tmp_path: Path, name: str, data: bytes) -> Path:
    """The file `name` of `tmp_path`, in a directory of its own."""
    (tmp_path / name).mkdir()
    path = tmp_path / name / f"{name}.czi"
    path.write_bytes(data)
    return path


def test_subblocks_the_fixtures_lack_are_described_as_czifile_reads_them(tmp_path):
    """Cases no fixture holds: color under Zstd0 and Zstd1, with and without hi-lo packing; a
    Zstandard frame that declares no content size, under Zstd0 and Zstd1, which the profile does
    not place; a JPEG XR file one row short of its entry's stored size; a subblock that two
    entries of one directory name; and a subblock whose data spans the next subblock's segment,
    whose elements the finished IR makes aliases, so that its declarations and its unit are
    ranges of the file."""
    plane, pixels = writer.plane, writer.pixels
    color = [plane(BGRA32, ZSTD0, 0, 0, 6, 4, C=0), plane(BGRA32, ZSTD1, 0, 0, 6, 4, C=1),
             plane(BGR48, ZSTD1, 0, 0, 6, 4, C=2, hilo=True), plane(BGR24, ZSTD0, 0, 0, 6, 4, C=3)]
    frame = writer.zstd0(pixels(GRAY8, 6, 4))
    assert frame[4:6] == SIZED
    # a window descriptor in place of the content size
    unsized = frame[:4] + b"\x00\x00" + frame[6:]
    frames = [plane(GRAY8, ZSTD0, 0, 0, 6, 4, C=0, coded=unsized),
              plane(GRAY8, ZSTD1, 0, 0, 6, 4, C=1, coded=b"\x01" + unsized)]
    short = plane(GRAY16, JPEGXR, 0, 0, 16, 9, coded=writer.jxr(pixels(GRAY16, 16, 8)))
    once = writer.Czi([plane(GRAY8, UNCOMPRESSED, 0, 0, 6, 4, C=0)])
    once.build()
    again = writer.dv_entry(GRAY8, UNCOMPRESSED, once.offsets["subblock0"], once.subblocks[0].dims)
    twice = writer.Czi([plane(GRAY8, UNCOMPRESSED, 0, 0, 6, 4, C=0)], more_entries=[again])
    # 352 bytes of data: the 32 left of its own segment, then the next subblock's 320
    spans = plane(GRAY8, UNCOMPRESSED, 0, 0, 88, 4, C=0)
    spans.data, spans.sizes = bytes(range(16)), (0, 0, 352)
    cases = {
        "color": (writer.Czi(color), {"units of ['zstd']": 2, "units of ['czi_zstd1']": 2, "units decoded": 4,
                                      "units whose samples czifile reorders": 4, "units whose alpha czifile sets": 2,
                                      "units that are ranges of the file": 2}),
        "frames": (writer.Czi(frames), {"units of ['zstd']": 1, "units of ['czi_zstd1']": 1, "units decoded": 2,
                                        "units that are ranges of the file": 1}),
        "short": (writer.Czi([short]), {"units of ['jpegxr']": 1, "units decoded": 1,
                                        "units of a frame of another size": 1}),
        "twice": (twice, {"units of []": 1, "units decoded": 1, "entries of a subblock an earlier one names": 1}),
        "spans": (writer.Czi([spans, plane(GRAY8, UNCOMPRESSED, 0, 0, 4, 4, C=1)]), {
            "units of []": 2, "units decoded": 2, "units that are ranges of the file": 1,
            "declarations that are ranges of the file": 2}),
    }
    for name, (czi, want) in cases.items():
        said, seen = compared(written(tmp_path, name, czi.build()))
        assert said == (), name
        n = len(czi.subblocks)
        assert {k: v for k, v in seen.items() if v} == {
            "entries": n + (name == "twice"), "subblocks": n, "descriptions": n, **want}, name


def test_a_subblock_declared_two_ways_has_neither_shape_nor_grid(tmp_path):
    """A subblock whose own copy of its entry gives another stored size than the directory's
    entry has its declarations alone: czifile reads it as 4 by 6 from the directory, and as 4 by 3
    where it walks the segments."""
    narrower = writer.plane(GRAY8, UNCOMPRESSED, 0, 0, 6, 4, C=0)
    # the StoredSize of its first dimension entry, X
    narrower.copy = lambda e: e[:48] + struct.pack("<i", 3) + e[52:]
    said, seen = compared(written(tmp_path, "narrower", writer.Czi([narrower]).build()))
    assert said == ()
    assert {k: v for k, v in seen.items() if v} == {
        "entries": 1, "subblocks": 1, "descriptions": 1, "subblocks declared two ways": 1,
        "subblocks czifile reads two ways": 1}


def test_data_that_is_not_its_entrys_has_no_grid(tmp_path):
    """Data the fixtures lack that is not what its entry says, each a subblock with its entry's
    shape and no grid: a Zstandard frame that names a dictionary, and bytes that are no frame; a
    Zstd1 header with no frame after it; a JPEG stream of three components for a pixel type of
    one sample, which czifile reads as three; and bytes that are no JPEG XR file."""
    plane, pixels = writer.plane, writer.pixels
    frame = writer.zstd0(pixels(GRAY8, 6, 4))
    assert frame[4:6] == SIZED
    # the flag of a 1-byte dictionary id
    bad = [plane(GRAY8, ZSTD0, 0, 0, 6, 4, C=0, coded=frame[:4] + b"\x21" + frame[5:]),
           plane(GRAY8, ZSTD0, 0, 0, 6, 4, C=1, coded=bytes(30)),
           plane(GRAY8, ZSTD1, 0, 0, 6, 4, C=2, coded=b"\x01"),
           plane(GRAY8, JPEG, 0, 0, 6, 4, C=3, coded=writer.jpeg(pixels(BGR24, 6, 4))),
           plane(GRAY8, JPEGXR, 0, 0, 6, 4, C=4, coded=bytes(40))]
    said, seen = compared(written(tmp_path, "bad", writer.Czi(bad).build()))
    assert said == ()
    assert {k: v for k, v in seen.items() if v} == {
        "entries": 5, "subblocks": 5, "descriptions": 5, "subblocks whose data is not their entry's": 5,
        "of them, czifile reads none": 4, "of them, czifile reads more values": 1}
