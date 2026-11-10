"""The stored arrays of the DICOM fixtures (spec/stored-array.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>), compared with
pydicom, the reference reader. Of every accepted fixture:

- the top-level Pixel Data's description has the shape, the sample size, the grid, the chain and
  the declarations that pydicom's reading of the attributes gives;
- each of its units is a row at the place where pydicom finds the frame's bytes: a native frame
  at its offset in the value pydicom reads, an encapsulated one at the data of the fragments
  `pydicom.encaps` gives the frame;
- each unit's bytes, decoded through the chain (by NumPy for native samples, by imagecodecs for
  `jpeg`, `jpegls` and `jpeg2k`, and by this file's reading of PS3.5 Annex G for `dicom_rle`),
  are the stored values pydicom reads of the frame, with no rescale and no color conversion;
- each lookup table of a `PALETTE COLOR` image is described as pydicom's palette reading takes
  it, and each Pixel Data in an item of a sequence as pydicom reads the item.

pydicom has no decoder here for lossless JPEG, JPEG-LS and HTJ2K: their units are compared by
their places and bytes, decoded where the list has a codec, and counted. A second test asserts
which cases the fixtures reached; the tests after it write the cases no fixture holds."""

import math
import re
import struct
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import imagecodecs
import numpy as np
import pydicom
import pytest
from described import Described, described
from ir_helpers import FIXTURES
from pydicom.datadict import dictionary_has_tag, dictionary_VR
from pydicom.dataelem import RawDataElement
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.encaps import encapsulate, generate_fragmented_frames, parse_fragments
from pydicom.multival import MultiValue
from pydicom.pixels import pixel_array
from pydicom.sequence import Sequence
from pydicom.uid import (
    UID,
    ExplicitVRBigEndian,
    ExplicitVRLittleEndian,
    ImplicitVRLittleEndian,
    SecondaryCaptureImageStorage,
)

from refkv.ir.stored import Each, ElementPlace, StoredArray, Value

FILES = sorted(p for p in (FIXTURES / "dicom").glob("*.dcm") if "reject" not in p.name)
PIXEL_DATA, FRAMES, TRANSFER_SYNTAX = 0x7FE00010, 0x00280008, 0x00020010
# the attributes that declare what a Pixel Data's elements are, after the transfer syntax
DECLARING = (0x00280004, 0x00280100, 0x00280101, 0x00280102, 0x00280103, 0x00280120, 0x00280121, 0x00281101, 0x00281102,
             0x00281103, 0x00281201, 0x00281202, 0x00281203, 0x00281221, 0x00281222, 0x00281223)
# the VRs whose explicit header is 12 bytes
LONG = {"OB", "OD", "OF", "OL", "OV", "OW", "SQ", "SV", "UC", "UN", "UR", "UT", "UV"}
# the transfer syntaxes of native pixel data, and the codec of each encapsulated one the list names
NATIVE = {"1.2.840.10008.1.2", "1.2.840.10008.1.2.1", "1.2.840.10008.1.2.2"}
LOSSLESS = {"1.2.840.10008.1.2.4.57", "1.2.840.10008.1.2.4.70"}
CODEC = {"1.2.840.10008.1.2.4.50": "jpeg", "1.2.840.10008.1.2.4.80": "jpegls", "1.2.840.10008.1.2.4.81": "jpegls",
         "1.2.840.10008.1.2.4.90": "jpeg2k", "1.2.840.10008.1.2.4.91": "jpeg2k", "1.2.840.10008.1.2.5": "dicom_rle"}
HTJ2K = {"1.2.840.10008.1.2.4.201", "1.2.840.10008.1.2.4.202", "1.2.840.10008.1.2.4.203"}
BIG = "1.2.840.10008.1.2.2"

type Span = tuple[int, int]
"""(offset, length) of the file"""


@dataclass(frozen=True)
class Held:
    """An element as pydicom read it: where it starts and where its value lies in the file, and
    its value's length, None for an undefined one."""

    start: int
    value: int
    length: int | None


def held(ds: Dataset, tag: int, base: int, data: bytes) -> Held | None:
    """Where pydicom read the element `tag` of `ds`, whose elements' offsets count from `base`, in
    the file `data`; None for no element."""
    e = ds.get_item(tag)
    if e is None:
        return None
    if isinstance(e, RawDataElement):
        header = 8 if e.is_implicit_VR or e.VR not in LONG else 12
        return Held(base + e.value_tell - header, base + e.value_tell, None if e.length == 0xFFFFFFFF else e.length)
    # an element whose value pydicom has read already (Pixel Representation, which it reads to tell
    # US from SS): it keeps where the value lay, and the header before it gives the length
    assert e.file_tell is not None
    implicit, little = ds.original_encoding
    value = base + e.file_tell
    wide = implicit or e.VR in LONG
    length = int.from_bytes(data[value - (4 if wide else 2):value], "little" if little else "big")
    return Held(value - (12 if wide and not implicit else 8), value, None if length == 0xFFFFFFFF else length)


class Image:
    """What pydicom reads of a dataset that holds Pixel Data, the file's or an item's: its
    attributes, and where their elements and the Pixel Data's value lie in the file."""

    def __init__(self, ds: Dataset, base: int, syntax: str, data: bytes) -> None:
        self.places = {t: held(ds, t, base, data) for t in (PIXEL_DATA, FRAMES, *DECLARING)}
        self.ds, self.syntax, self.data = ds, syntax, data

    def number(self, keyword: str) -> int | None:
        """The first value of an attribute of VR US; None where it is absent, or empty."""
        value = self.ds.get(keyword)
        if isinstance(value, MultiValue):
            value = value[0] if len(value) else None
        return None if value is None or value == "" else int(value)

    @property
    def frames(self) -> int | str | None:
        """Number of Frames: its value, None without the attribute, or why it is not a count."""
        place = self.places[FRAMES]
        if place is None or not place.length:
            return None
        first = self.data[place.value:place.value + place.length].split(b"\\")[0].strip(b" \0")
        if not re.fullmatch(rb"[+-]?[0-9]+", first) or int(first) < 1:
            return "no count"
        return int(first)

    @property
    def native(self) -> bool:
        """Whether the value is of defined length: frames end to end."""
        place = self.places[PIXEL_DATA]
        return place is not None and place.length is not None

    @property
    def samples(self) -> int | None:
        return self.number("SamplesPerPixel")

    @property
    def bits(self) -> int | None:
        return self.number("BitsAllocated") or None

    @property
    def planes(self) -> bool:
        """Whether each sample of a frame is a plane: native data of Planar Configuration 1, and
        RLE data, which decodes to planes."""
        s = self.samples
        return s is not None and s > 1 and (self.number("PlanarConfiguration") == 1 if self.native
                                            else CODEC.get(self.syntax) == "dicom_rle")

    @property
    def within(self) -> tuple[int, ...] | None:
        """A frame's axes, in storage order; None where the attributes do not give them."""
        rows, columns, s = self.number("Rows"), self.number("Columns"), self.samples
        if not rows or not columns or not s:
            return None
        if s > 1 and self.number("PlanarConfiguration") not in (0, 1):
            return None
        return (rows, columns) if s == 1 else (s, rows, columns) if self.planes else (rows, columns, s)

    @property
    def shape(self) -> tuple[int, ...] | None:
        within, frames = self.within, self.frames
        if within is None or isinstance(frames, str):
            return None
        return within if frames is None else (frames, *within)

    @property
    def count(self) -> int:
        frames = self.frames
        return frames if isinstance(frames, int) else 1

    def cut(self) -> tuple[int, int, int]:
        """How pydicom reads native frames: (the frames of a unit, a unit's bytes, the units). A
        frame is as many bytes as its samples take, but 1-bit frames follow each other with no
        padding, and pydicom unpacks them together where a frame's bits do not end on a byte."""
        within, bits = self.within, self.bits
        assert within is not None
        assert bits is not None
        frame = math.prod(within) * bits
        if bits == 1 and frame % 8:
            return self.count, math.ceil(self.count * frame / 8), 1
        return 1, frame // 8, self.count


def sof(stream: bytes) -> tuple[int, int]:
    """A JPEG stream's frame header: its marker's second byte and its precision."""
    at = 2
    while True:
        assert stream[at] == 0xFF, "a marker"
        marker = stream[at + 1]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            return marker, stream[at + 4]
        at += 2 + struct.unpack_from(">H", stream, at + 2)[0]


def fragments(im: Image) -> list[list[Span]]:
    """Where pydicom finds each frame's bytes in encapsulated Pixel Data: the data of the
    fragments `generate_fragmented_frames` gives it, less the empty ones."""
    ds = im.ds
    place = im.places[PIXEL_DATA]
    assert place is not None
    buffer = ds.PixelData
    # the fragments start after the Basic Offset Table's item
    first = 8 + struct.unpack_from("<I", buffer, 4)[0]
    extended = (ds.ExtendedOffsetTable, ds.ExtendedOffsetTableLengths) if "ExtendedOffsetTable" in ds else None
    frames = list(generate_fragmented_frames(buffer, number_of_frames=im.count, extended_offsets=extended))
    if extended:
        offsets = struct.unpack(f"<{im.count}Q", ds.ExtendedOffsetTable)
        spans = [[(place.value + first + o + 8, len(frame[0]))] for o, frame in zip(offsets, frames, strict=True)]
    else:
        _, offsets = parse_fragments(buffer[first:])
        starts = iter(offsets)
        spans = [[(place.value + first + next(starts) + 8, len(f)) for f in frame] for frame in frames]
    for frame, mine in zip(frames, spans, strict=True):
        assert b"".join(frame) == b"".join(im.data[o:o + n] for o, n in mine)
    return [[s for s in frame if s[1]] for frame in spans]


def chain_names(im: Image, streams: list[bytes]) -> tuple[str, ...]:
    """The names of the codecs of the Pixel Data's chain, as this test reads the transfer syntax
    and the frames' streams."""
    if im.native:
        return ("lsb_first",) if im.bits == 1 else ()
    if im.syntax in LOSSLESS:
        # the list's `jpeg` is a stream of 8-bit samples
        return ("jpeg",) if all(sof(s) == (0xC3, 8) for s in streams) else ("source",)
    return ("source",) if im.syntax in HTJ2K else (CODEC[im.syntax],)


def file_parts(d: Described, row: dict) -> list[Span]:
    """Where a row's unit is in the file: its ranges; or its element's extent, a run's stride
    further for each member before its own, or its range of that."""
    if row["element"] is None:
        assert all(r["object"] is None for r in row["ranges"])
        return [(r["offset"], r["length"]) for r in row["ranges"]]
    start, length = d.ir.extent(row["element"])
    if row["member"] is not None:
        root = row["element"]
        while (run := d.ir._core.run(root)) is None:
            root = d.ir.parent(root)
        start += row["member"] * run[1]
    if row["range"] is not None:
        start, length = start + row["range"]["offset"], row["range"]["length"]
    return [(start, length)]


def unit_shape(a: StoredArray) -> tuple[int, ...]:
    """The shape of a unit of the array's grid, which cuts no axis short."""
    assert a.grid is not None
    assert a.shape is not None
    assert all(isinstance(x, Each) for x in a.grid.axes)
    return tuple(x.length for x in a.grid.axes if isinstance(x, Each))


def rle_planes(b: bytes, planes: int, plane: int, size: int) -> np.ndarray:
    """One frame of DICOM's RLE (PS3.5 Annex G), decoded to `planes` planes of `plane` samples of
    `size` bytes: 16 little-endian 32-bit integers, the number of segments and their offsets, then
    each segment's PackBits runs, one byte of every sample of a plane, a sample's most significant
    byte first."""
    count, *offsets = struct.unpack_from("<16I", b)
    assert count == planes * size
    ends = [*offsets[1:count], len(b)]
    out = np.zeros((planes, plane), dtype=f"<u{size}")
    for k in range(count):
        segment = np.frombuffer(imagecodecs.packbits_decode(b[offsets[k]:ends[k]]), dtype="u1")[:plane]
        out[k // size] |= segment.astype(out.dtype) << (8 * (size - 1 - k % size))
    return out


def decoded(d: Described, a: StoredArray, row: dict, big: bool, within: tuple[int, ...]) -> np.ndarray | None:
    """A row's unit decoded through the array's chain, as unsigned samples of the array's size in
    the unit's shape, a frame's axes being `within`; None for `source`, which names no decoding."""
    assert a.item_bits is not None
    shape = unit_shape(a)
    (chain,) = (d.stored.chains[c] for c in a.chains)
    b = b"".join(d.objects[None][o:o + n] for o, n in file_parts(d, row))
    names = [c.name for c in chain]
    if names == ["lsb_first"]:
        # each byte's least significant bit first; the bits past the unit's are padding
        return np.unpackbits(np.frombuffer(b, dtype="u1"), bitorder="little")[:math.prod(shape)].reshape(shape)
    size = a.item_bits // 8
    if names == []:
        return np.frombuffer(b[:math.prod(shape) * size], dtype=f"{'>' if big else '<'}u{size}").reshape(shape)
    if names == ["dicom_rle"]:
        planes = within[0] if len(within) == 3 else 1
        return rle_planes(b, planes, math.prod(within) // planes, size).reshape(shape)
    if names == ["jpeg"]:
        # the frame's samples as the stream codes them: libjpeg is told the stream's color space
        # is the one it is to give, so it converts nothing
        same = "YCBCR" if len(within) == 3 else None
        made = imagecodecs.jpeg8_decode(b, colorspace=same, outcolorspace=same)
    elif names == ["jpegls"]:
        made = imagecodecs.jpegls_decode(b)
    elif names == ["jpeg2k"]:
        made = imagecodecs.jpeg2k_decode(b)
    else:
        assert names == ["source"], names
        return None
    assert made.size == math.prod(shape), f"a frame of {made.shape}, not its unit's {shape}"
    return made.astype(f"<u{size}").reshape(shape)


def stored_frames(im: Image, a: StoredArray) -> np.ndarray | None:
    """The stored values pydicom reads of the Pixel Data, a frame after another, each in the
    description's layout (pydicom gives a pixel's samples together, whichever way they are
    stored); None where pydicom has no decoder of the transfer syntax."""
    try:
        theirs = pixel_array(im.ds, raw=True)
    except Exception:  # noqa: BLE001  (whatever a missing decoder raises: pydicom reads no values to compare)
        return None
    rows, columns, s = im.number("Rows"), im.number("Columns"), im.samples
    assert rows is not None
    assert columns is not None
    assert s is not None
    theirs = theirs.reshape((im.count, rows, columns) if s == 1 else (im.count, rows, columns, s))
    if im.planes:
        theirs = theirs.transpose(0, 3, 1, 2)
    assert a.item_bits is not None
    # as unsigned samples: a signed sample's bits, unchanged
    if a.item_bits == 1:
        return theirs.astype("u1")
    return np.ascontiguousarray(theirs.astype(theirs.dtype.newbyteorder("<"))).view(f"<u{a.item_bits // 8}")


def check_declarations(d: Described, a: StoredArray, im: Image, said: list[str]) -> None:
    """The declarations: the Transfer Syntax UID's element, then each declaring attribute of the
    dataset that has a value, a data element whole, where pydicom read it."""
    if not a.declarations or not all(isinstance(p, ElementPlace) and p.range is None for p in a.declarations):
        said.append("declarations that are not whole elements")
        return
    syntax, *rest = a.declarations
    assert isinstance(syntax, ElementPlace)
    if d.ir.path(syntax.element) != "meta/00020010" or d.place(syntax)[8:].rstrip(b"\0 ").decode() != im.syntax:
        said.append("the first declaration is not the Transfer Syntax UID")
    want = [(p.start, p.value - p.start + p.length) for t in DECLARING
            if (p := im.places[t]) is not None and p.length]
    got = [file_parts(d, {"element": p.element, "member": p.member, "range": None})[0]
           for p in rest if isinstance(p, ElementPlace)]
    if got != want:
        said.append(f"declarations at {got}, pydicom's elements at {want}")


def check_rows(d: Described, k: int, im: Image, units: list[list[Span]], theirs: np.ndarray | None,
               seen: Counter, said: list[str]) -> None:
    """An array's rows against the units pydicom finds: a row for each, at its place, named as the
    IR names it, decoding to the values pydicom reads there."""
    a = d.stored.arrays[k]
    assert a.shape is not None
    rows = d.rows.get(k, [])
    shape = unit_shape(a)
    declared = len(a.shape) == len(im.within or ()) + 1
    if len(rows) != len(units):
        said.append(f"{len(rows)} rows, pydicom {len(units)} units")
        return
    for u, (row, spans) in enumerate(zip(rows, units, strict=True)):
        if row["coords"] != [*([u] if declared else []), *[0] * len(im.within or ())]:
            said.append(f"unit {u} at {row['coords']}")
        if file_parts(d, row) != spans:
            said.append(f"unit {u} at {file_parts(d, row)}, pydicom {spans}")
            continue
        if row["chain"] is not None or row["skips"] is not None:
            said.append(f"unit {u} of another chain, or skipping a codec")
        # bytes one element holds are named by it; a frame of several fragments is ranges
        if (row["element"] is None) != (len(spans) > 1):
            said.append(f"unit {u}: {len(spans)} spans named by {'ranges' if row['element'] is None else 'an element'}")
        seen["units"] += 1
        seen["units of several ranges"] += row["element"] is None
        seen["units that are a run's member"] += row["member"] is not None
        seen["units that are a range of an element"] += row["range"] is not None
        mine = decoded(d, a, row, im.syntax == BIG, im.within or ())
        if mine is None:
            seen["units of source, not decoded"] += 1
            continue
        if theirs is None:
            seen["units decoded that pydicom has no decoder for"] += 1
            if im.syntax in LOSSLESS:
                # the list's `jpeg` decoded by libjpeg-turbo: against imagecodecs' other lossless
                # JPEG decoder (LJ92)
                other = imagecodecs.ljpeg_decode(b"".join(im.data[o:o + n] for o, n in spans))
                if mine.tobytes() != other.astype(mine.dtype).tobytes():
                    said.append(f"unit {u} decodes to other values than imagecodecs' lossless JPEG decoder's")
                seen["units decoded to a second lossless JPEG decoder's values"] += 1
            continue
        frames = shape[0] if declared else 1
        region = theirs[u * frames:(u + 1) * frames].reshape(shape)
        if mine.tobytes() != region.astype(mine.dtype).tobytes():
            said.append(f"unit {u} decodes to other values than pydicom reads")
        seen["units decoded to pydicom's values"] += 1


def check_described(d: Described, k: int, im: Image, top: bool, seen: Counter) -> list[str]:
    """One Pixel Data against its description: the file's (`top`), or an item's."""
    a = d.stored.arrays[k]
    said: list[str] = []
    if a.shape != im.shape:
        said.append(f"shape {a.shape}, pydicom {im.shape}")
    if a.item_bits != im.bits:
        said.append(f"item_bits {a.item_bits}, pydicom {im.bits}")
    if a.fill != "undefined" or a.axes is not None or a.pad_from is not None or a.unit_parameters:
        said.append("a fill value, axes, a padding or unit parameters")
    check_declarations(d, a, im, said)
    reason = no_grid(im, top)
    if reason is not None or a.grid is None:
        seen[f"no grid: {reason}"] += 1
        if reason is None or a.grid is not None or a.chains or d.rows.get(k):
            said.append(f"a grid, chains or rows for {reason}" if reason else "no grid")
        return said
    assert im.shape is not None
    if im.native:
        together, size, count = im.cut()
        place = im.places[PIXEL_DATA]
        assert place is not None
        units = [[(place.value + u * size, size)] for u in range(count)]
        streams = []
    else:
        together, units = 1, fragments(im)
        streams = [b"".join(im.data[o:o + n] for o, n in spans) for spans in units]
    within = im.within
    assert within is not None
    axes = tuple(Each(n) for n in ((together, *within) if len(im.shape) > len(within) else within))
    if (a.grid.axes, a.grid.edge) != (axes, "short"):
        said.append(f"grid {a.grid}, pydicom's frames {axes}")
        return said
    names = chain_names(im, streams)
    (chain,) = (d.stored.chains[c] for c in a.chains)
    if tuple(c.name for c in chain) != names:
        said.append(f"chain {[c.name for c in chain]}, by the transfer syntax {names}")
        return said
    values = [{n: v.value for n, v in c.parameters.items() if isinstance(v, Value)} for c in chain]
    given: dict[tuple[str, ...], list[dict[str, object]]] = {
        ("lsb_first",): [{"bits": 1}], ("source",): [{"format": "dicom", "id": im.syntax}]}
    want = given.get(names, [{}] * len(chain))
    if values != want or any(len(c.parameters) != len(v) for c, v in zip(chain, values, strict=True)):
        said.append(f"codec parameters {[c.parameters for c in chain]}, not {want}")
    seen[f"chain {' '.join(names) or 'empty'}"] += 1
    seen["a frames' axis"] += len(im.shape) > len(within)
    seen["a frames' axis of one frame"] += len(im.shape) > len(within) and im.count == 1
    seen["samples a plane each"] += im.planes
    seen["samples of a pixel together"] += im.samples != 1 and not im.planes
    seen["frames one unit together"] += together > 1
    seen["lossless JPEG in the list's jpeg"] += im.syntax in LOSSLESS and names == ("jpeg",)
    check_rows(d, k, im, units, stored_frames(im, a) if top or readable(im) else None, seen, said)
    return said


def readable(im: Image) -> bool:
    """Whether pydicom reads an item's Pixel Data, given the transfer syntax its file has: it
    needs the attributes of the Image Pixel module, which a description does not."""
    return all(t in im.ds for t in ("PixelRepresentation", "BitsStored", "PhotometricInterpretation"))


def no_grid(im: Image, top: bool) -> str | None:
    """Why a Pixel Data has no grid, as this test reads pydicom's values of its attributes; None
    where it has one."""
    if top:
        # the parser accepts such frames only with chroma components of half the width
        halved = CODEC.get(im.syntax) == "jpeg2k" and im.ds.PhotometricInterpretation == "YBR_FULL_422"
        return "JPEG 2000 chroma of half the width" if halved else None
    if im.shape is None or im.bits is None:
        return "an item's attributes do not give the frames"
    if not im.native:
        return "an item's fragments"
    photometric = im.ds.get("PhotometricInterpretation")
    if im.bits not in (8, 16, 32) and not (im.bits == 1 and im.samples == 1):
        return "an item's samples of a size the profile does not read"
    if im.samples != 1 and not (im.samples == 3 and photometric in ("RGB", "YBR_FULL")):
        return "an item's samples of colors the profile does not read"
    place = im.places[PIXEL_DATA]
    assert place is not None
    assert place.length is not None
    _, size, count = im.cut()
    if size * count > place.length:
        return "an item's value shorter than its frames"
    if im.syntax == BIG and im.bits < 16:
        return "an item's bytes under Explicit VR Big Endian"
    return None


def check_palette(d: Described, ks: list[int], ds: Dataset, places: list[Held], big: bool, seen: Counter) -> list[str]:
    """The three lookup tables against pydicom's palette reading (`apply_color_lut`): the
    descriptor's first value counts a table's entries, 0 meaning 65536, and an entry is as many
    bytes as the table's value has for each."""
    said: list[str] = []
    for c, (k, place) in enumerate(zip(ks, places, strict=True)):
        a = d.stored.arrays[k]
        descriptor = ds[0x00281101 + c].value
        table = bytes(ds[0x00281201 + c].value)
        entries = int(descriptor[0]) or 65536
        size = len(table) // entries
        if len(table) % entries or size not in (1, 2):
            # pydicom takes no table of another length; the profile reads its first entries
            seen["lookup tables pydicom's palette reading refuses"] += 1
            size = 2 if len(table) >= 2 * entries else 1
        if (a.shape, a.item_bits, a.index) != ((entries,), 8 * size, None):
            said.append(f"table {c}: shape {a.shape} of {a.item_bits} bits, pydicom {entries} entries of {size} bytes")
            continue
        paths = [d.ir.path(p.element) for p in a.declarations if isinstance(p, ElementPlace)]
        if paths != ["meta/00020010", f"dataset/{0x00281101 + c:08X}"] or a.fill != "undefined" or a.axes is not None:
            said.append(f"table {c}: declarations {paths}, a fill value or axes")
        rows = d.rows.get(k, [])
        if big and size == 1 and ds[0x00281201 + c].VR == "OW":
            # 16-bit words, which Explicit VR Big Endian swaps: the entries are not in order
            seen["no grid: a table's bytes in big-endian words"] += 1
            if a.grid is not None or a.chains or rows:
                said.append(f"table {c}: a grid, chains or rows for bytes in big-endian words")
            continue
        chains = [d.stored.chains[i] for i in a.chains]
        if a.grid is None or (a.grid.axes, a.grid.edge, chains) != ((Each(entries),), "short", [()]):
            said.append(f"table {c}: grid {a.grid}, not its entries in one unit as they are")
            continue
        if len(rows) != 1 or rows[0]["coords"] != [0] or file_parts(d, rows[0]) != [(place.value, entries * size)]:
            said.append(f"table {c}: rows {[file_parts(d, r) for r in rows]}, pydicom's value at {place.value}")
            continue
        if rows[0]["range"] is None:
            said.append(f"table {c}: its element whole, which holds its header too")
        order = ">" if big else "<"
        mine = np.frombuffer(d.unit(rows[0]), dtype=f"{order}u{size}")
        if mine.tolist() != np.frombuffer(table, dtype=f"{order}u{size}")[:entries].tolist():
            said.append(f"table {c}: other entries than pydicom's")
        seen["lookup tables"] += 1
        seen["lookup tables of two bytes an entry"] += size == 2
        seen["lookup tables longer than their entries"] += len(table) > entries * size
    return said


def sequences(ds: Dataset, base: int) -> Iterator[tuple[int, int]]:
    """The tag of each sequence of `ds`, with the offset its items' elements' places count from:
    pydicom reads a sequence of defined length from a buffer of its value, when it is asked for,
    and one of undefined length where it lies."""
    for tag in list(ds.keys()):
        e = ds.get_item(tag)
        # an element of implicit VR has the dictionary's
        vr = e.VR or (dictionary_VR(tag) if dictionary_has_tag(tag) else None)
        if vr == "SQ":
            yield int(tag), base + e.value_tell if isinstance(e, RawDataElement) else base


def items(ds: Dataset, base: int) -> Iterator[tuple[Dataset, int]]:
    """Each item of the sequences of `ds`, at any depth, in the order of each sequence, with the
    offset its elements' places count from."""
    for tag, inner in list(sequences(ds, base)):
        for item in ds[tag].value:
            yield item, inner
            yield from items(item, inner)


def nested(ds: Dataset) -> dict[int, list[tuple[Dataset, int]]]:
    """The items that hold Pixel Data, by the tag of the top-level sequence they lie in, in the
    order of the file, each with the offset its elements' places count from."""
    out: dict[int, list[tuple[Dataset, int]]] = {}
    for tag, base in list(sequences(ds, 0)):
        found = []
        for item in ds[tag].value:
            found += [(item, base), *items(item, base)]
        found = [(item, b) for item, b in found if PIXEL_DATA in item]
        if found:
            out[tag] = found
    return out


def as_file(item: Dataset, im: Image) -> Dataset:
    """An item with a File Meta Information that tells pydicom how its Pixel Data is encoded: a
    value of defined length is native, in the file's byte order."""
    out = Dataset()
    out.update(item)
    out.file_meta = FileMetaDataset()
    out.file_meta.TransferSyntaxUID = ExplicitVRBigEndian if im.syntax == BIG else ExplicitVRLittleEndian
    return out


@cache
def compared(path: Path) -> tuple[tuple[str, ...], Counter]:
    """What differs between the descriptions of the file at `path` and pydicom's reading of it,
    and what the comparison saw."""
    d = described(path)
    data = d.objects[None]
    said: list[str] = []
    seen: Counter = Counter()
    if d.stored.undescribed:
        said.append("descriptions dropped for room")
    ds = pydicom.dcmread(path)
    syntax = str(ds.file_meta.TransferSyntaxUID)
    by_key: dict[str, list[int]] = {}
    for k, a in enumerate(d.stored.arrays):
        assert a.path is not None
        by_key.setdefault(a.path, []).append(k)
    # the items before the top-level values: a sequence's value, once read, is no longer where its
    # items' places count from
    inner = {f"dataset/{tag:08X}": [Image(item, base, syntax, data) for item, base in found]
             for tag, found in nested(ds).items()}
    tables = [held(ds, 0x00281201 + c, 0, data) for c in range(3)]
    segmented = any(0x00281221 + c in ds for c in range(3))
    top = Image(ds, 0, syntax, data)
    # the top-level Pixel Data, keyed by its element
    (k,) = by_key.pop("dataset/7FE00010", [None])
    if k is None or d.stored.arrays[k].index is not None:
        return ("no description of the Pixel Data, or an index",), seen
    seen["pixel data"] += 1
    said += [f"pixel data: {s}" for s in check_described(d, k, top, True, seen)]
    # a palette's tables, each keyed by its own element, where none is segmented
    if ds.PhotometricInterpretation == "PALETTE COLOR" and not segmented:
        ks = [by_key.pop(f"dataset/{0x00281201 + c:08X}", [None])[0] for c in range(3)]
        if None in ks or None in tables:
            said.append("no description of a lookup table")
        else:
            said += check_palette(d, [k for k in ks if k is not None], ds, [t for t in tables if t is not None],
                                  syntax == BIG, seen)
    seen["palettes with a segmented table"] += segmented
    # the Pixel Data of items, keyed by the top-level sequence, indexed in the order of the file
    for key, images in inner.items():
        ks = by_key.pop(key, [])
        if [d.stored.arrays[k].index for k in ks] != list(range(len(images))):
            indexes = [d.stored.arrays[k].index for k in ks]
            said.append(f"{key}: indexes {indexes}, pydicom {len(images)} items of Pixel Data")
            continue
        order = sorted(range(len(images)), key=lambda i: (p.start if (p := images[i].places[PIXEL_DATA]) else 0))
        for k, i in zip(ks, order, strict=True):
            seen["pixel data of items"] += 1
            im = images[i]
            item = im.ds
            im.ds = as_file(item, im)
            said += [f"{key} {d.stored.arrays[k].index}: {s}" for s in check_described(d, k, im, False, seen)]
    if by_key:
        said.append(f"descriptions of {sorted(by_key)}, which pydicom reads no array of")
    return tuple(said), seen


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_dicom_arrays_are_described_as_pydicom_reads_them(path):
    """The file's Pixel Data, its lookup tables and the Pixel Data of its items are described as
    pydicom reads them."""
    said, seen = compared(path)
    assert said == ()
    assert seen["pixel data"] == 1


def test_the_fixtures_hold_every_case_the_comparison_reads():
    """The fixtures reach each case of the comparison, so that none of it passes for want of a
    file. Of the 220 units, pydicom reads the values of 198. It has no decoder here for the other
    22: the 11 of HTJ2K and of lossless JPEG of 16 bits, which are `source` and not decoded; and
    the 11 of JPEG-LS and of lossless JPEG of 8 bits, which are decoded to their unit's shape, the
    3 lossless ones also against a second decoder. The two Pixel Data of items are icons with no
    Samples per Pixel, and with no attributes at all."""
    seen: Counter = Counter()
    for path in FILES:
        seen += compared(path)[1]
    assert {k: v for k, v in seen.items() if v} == {
        "pixel data": 77,
        "chain empty": 35,
        "chain lsb_first": 5,
        "chain jpeg": 12,
        "chain jpegls": 5,
        "chain jpeg2k": 4,
        "chain dicom_rle": 8,
        "chain source": 7,
        "lossless JPEG in the list's jpeg": 2,
        "no grid: JPEG 2000 chroma of half the width": 1,
        "a frames' axis": 42,
        "a frames' axis of one frame": 1,
        "samples of a pixel together": 14,
        "samples a plane each": 6,
        "frames one unit together": 1,
        "units": 220,
        "units that are a run's member": 128,
        "units of several ranges": 10,
        "units decoded to pydicom's values": 198,
        "units of source, not decoded": 11,
        "units decoded that pydicom has no decoder for": 11,
        "units decoded to a second lossless JPEG decoder's values": 3,
        "lookup tables": 12,
        "lookup tables of two bytes an entry": 9,
        "palettes with a segmented table": 1,
        "pixel data of items": 2,
        "no grid: an item's attributes do not give the frames": 2,
    }


def image(rows: int = 4, columns: int = 4, *, samples: int = 1, bits: int = 8, frames: int | None = None,
          planar: int = 0, photometric: str | None = None) -> Dataset:
    """A dataset with the attributes of native Pixel Data of those sizes, and none yet."""
    ds = Dataset()
    ds.SamplesPerPixel = samples
    ds.PhotometricInterpretation = photometric or ("MONOCHROME2" if samples == 1 else "RGB")
    if samples > 1:
        ds.PlanarConfiguration = planar
    if frames is not None:
        ds.NumberOfFrames = frames
    ds.Rows, ds.Columns = rows, columns
    ds.BitsAllocated, ds.BitsStored, ds.HighBit, ds.PixelRepresentation = bits, bits, bits - 1, 0
    return ds


def ramp(count: int, start: int = 1) -> bytes:
    """`count` bytes, each unlike its neighbors."""
    return bytes((start + 7 * i) % 251 for i in range(count))


def filled(ds: Dataset, vr: str = "OB", less: int = 0) -> Dataset:
    """`ds` with native Pixel Data of as many bytes as its frames take, `less` frames fewer, made
    even with a byte past them."""
    frame = ds.Rows * ds.Columns * ds.SamplesPerPixel * ds.BitsAllocated
    count = int(ds.get("NumberOfFrames", 1)) - less
    size = math.ceil(count * frame / 8)
    ds.add_new(PIXEL_DATA, vr, ramp(size + size % 2, ds.Rows))
    return ds


def saved(path: Path, ds: Dataset, syntax: str = ExplicitVRLittleEndian) -> Path:
    """`ds` written as a DICOM file of the transfer syntax `syntax`."""
    ds.SOPClassUID = SecondaryCaptureImageStorage
    ds.SOPInstanceUID = "1.2.826.0.1.3680043.8.498.1"
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.file_meta.TransferSyntaxUID = UID(syntax)
    ds.save_as(path, enforce_file_format=True)
    return path


def paletted(descriptor: list[int], length: int, vr: str = "OW") -> Dataset:
    """A small `PALETTE COLOR` image whose three lookup tables have `descriptor`, and `length`
    bytes each."""
    ds = filled(image(photometric="PALETTE COLOR"))
    for c in range(3):
        ds.add_new(0x00281101 + c, "US", descriptor)
        ds.add_new(0x00281201 + c, vr, ramp(length, c))
    return ds


def with_icons(path: Path, *icons: Dataset, syntax: str = ExplicitVRLittleEndian) -> Path:
    """A file of one small native image, with `icons` the items of its Icon Image Sequence."""
    ds = filled(image())
    ds.IconImageSequence = Sequence(list(icons))
    return saved(path, ds, syntax)


def saw(paths: list[Path]) -> dict[str, int]:
    """What the comparison saw of the files at `paths`, none of which differs from pydicom's
    reading."""
    seen: Counter = Counter()
    for path in paths:
        said, counts = compared(path)
        assert said == (), path.name
        seen += counts
    return {k: v for k, v in seen.items() if v}


def test_arrays_no_fixture_holds_are_described_as_pydicom_reads_them(tmp_path):
    """The cases the fixtures lack: native Pixel Data in items (icons) of one sample and of three,
    interleaved and planar, of 1, 8 and 16 bits, with and without a frames' axis, in explicit and
    implicit VR and in big endian; in items of undefined length; in items that are a run, whose
    units are its members'; in an item of an item, keyed by the top-level sequence; beside
    encapsulated frames; a declaring attribute with no value, which is no declaration; lookup
    tables of 65536 entries, which the descriptor counts as 0; and lookup tables longer than their
    entries, of one byte and of two, which pydicom's palette reading refuses and the profile reads
    the first entries of."""
    icons = with_icons(
        tmp_path / "icons.dcm", filled(image(3, 5)), filled(image(2, 3, samples=3, bits=16, frames=2, planar=1), "OW"),
        filled(image(2, 2, samples=3, frames=1)), filled(image(5, 3, bits=1, frames=3)),
        filled(image(4, 4, bits=1, frames=2)), filled(image(2, 2, bits=32), "OW"))
    run = with_icons(tmp_path / "run.dcm", *(filled(image(2, 2, frames=2)) for _ in range(3)))
    implicit = with_icons(tmp_path / "implicit.dcm", filled(image(2, 3, bits=16), "OW"), syntax=ImplicitVRLittleEndian)
    big = with_icons(tmp_path / "big.dcm", filled(image(2, 3, bits=16), "OW"), syntax=ExplicitVRBigEndian)
    # an icon in an item of another sequence's item, and one of the dataset's own
    holder = Dataset()
    holder.IconImageSequence = Sequence([filled(image(2, 3)), filled(image(3, 2))])
    ds = filled(image())
    ds.ReferencedImageSequence = Sequence([holder])
    ds.IconImageSequence = Sequence([filled(image(1, 2))])
    deep = saved(tmp_path / "deep.dcm", ds)
    # items and a sequence of undefined length
    item = filled(image(2, 2))
    item.is_undefined_length_sequence_item = True
    ds = filled(image())
    ds.IconImageSequence = Sequence([item])
    ds["IconImageSequence"].is_undefined_length = True
    loose = saved(tmp_path / "loose.dcm", ds)
    # a native icon beside JPEG frames
    ds = pydicom.dcmread(FIXTURES / "dicom" / "dicom_jpeg_mono_bot.dcm")
    ds.IconImageSequence = Sequence([filled(image(2, 2))])
    beside = tmp_path / "beside.dcm"
    ds.save_as(beside)
    # a High Bit with no value, the file's and an icon's
    icon = filled(image(2, 2))
    icon.add_new(0x00280102, "US", None)
    ds = filled(image())
    ds.add_new(0x00280102, "US", None)
    ds.IconImageSequence = Sequence([icon])
    valueless = saved(tmp_path / "valueless.dcm", ds)
    # tables of 5 entries of one byte in 6 bytes, of 5 of two bytes in 12, and of 65536 entries
    palettes = [saved(tmp_path / f"palette{k}.dcm", paletted(descriptor, length))
                for k, (descriptor, length) in enumerate((([5, 0, 8], 6), ([5, 0, 16], 12), ([0, 0, 16], 1 << 17)))]

    assert saw([icons, run, implicit, big, deep, loose, beside, valueless, *palettes]) == {
        "pixel data": 11,
        "pixel data of items": 17,
        "chain empty": 25,
        "chain lsb_first": 2,
        "chain jpeg": 1,
        "a frames' axis": 8,
        "a frames' axis of one frame": 1,
        "samples a plane each": 1,
        "samples of a pixel together": 1,
        "frames one unit together": 1,
        "units": 35,
        "units that are a range of an element": 22,
        "units that are a run's member": 6,
        "units decoded to pydicom's values": 35,
        "lookup tables": 9,
        "lookup tables of two bytes an entry": 6,
        "lookup tables longer than their entries": 6,
        "lookup tables pydicom's palette reading refuses": 6,
    }
    # a run's members: each array's units are its own member's
    d = described(run)
    assert [[r["member"] for r in d.rows[k]] for k in range(3)] == [[0, 0], [1, 1], [2, 2]]
    assert [(a.path, a.index) for a in described(deep).stored.arrays] == [
        ("dataset/00081140", 0), ("dataset/00081140", 1), ("dataset/00880200", 0), ("dataset/7FE00010", None)]


def test_pydicom_gives_one_frame_no_axis():
    """pydicom drops the frames' axis of a file whose Number of Frames is 1; the description keeps
    the axis the attribute declares."""
    path = FIXTURES / "dicom" / "dicom_gathered_bounds.dcm"
    ds = pydicom.dcmread(path)
    assert ds.NumberOfFrames == 1
    assert pixel_array(ds, raw=True).shape == (2, 2)
    (a,) = (a for a in described(path).stored.arrays if a.path == "dataset/7FE00010")
    assert a.shape == (1, 2, 2)


def test_jpeg2k_frames_of_half_width_chroma_have_no_grid():
    """JPEG 2000 frames of `YBR_FULL_422` have chroma components of half the width: their samples
    are not an array of the image's shape, and decoders do not agree on what to make of them
    (OpenJPEG, through imagecodecs, upsamples and converts to RGB; pydicom's Pillow decoder gives
    other values). The description keeps the shape and the sample size, and has no grid."""
    path = FIXTURES / "dicom" / "dicom_j2k_ybr_422.dcm"
    d = described(path)
    (a,) = d.stored.arrays
    assert (a.shape, a.item_bits, a.grid, a.chains, d.rows) == ((2, 240, 240, 3), 8, None, (), {})
    ds = pydicom.dcmread(path)
    assert ds.PhotometricInterpretation == "YBR_FULL_422"
    frame = b"".join(next(generate_fragmented_frames(ds.PixelData, number_of_frames=2)))
    openjpeg, pillow = imagecodecs.jpeg2k_decode(frame), pixel_array(ds, raw=True)[0]
    assert openjpeg.shape == pillow.shape == (240, 240, 3)
    assert not np.array_equal(openjpeg, pillow)


def test_lossless_jpeg_of_another_precision_is_in_a_source_chain(tmp_path):
    """The list's `jpeg` is a stream of 8-bit samples: lossless JPEG frames of 7 bits in Bits
    Allocated 8 are `source`, by the transfer syntax, as those of 16 bits are."""
    pixels = (np.arange(2 * 6 * 5, dtype="u1") % 128).reshape(2, 6, 5)
    streams = [bytes(imagecodecs.jpeg8_encode(f, lossless=True, predictor=1, bitspersample=7)) for f in pixels]
    assert all(sof(s) == (0xC3, 7) for s in streams)
    ds = image(6, 5, frames=2)
    ds.BitsStored, ds.HighBit = 7, 6
    ds.add_new(PIXEL_DATA, "OB", encapsulate(streams))
    ds[PIXEL_DATA].is_undefined_length = True
    path = saved(tmp_path / "seven.dcm", ds, "1.2.840.10008.1.2.4.70")
    assert saw([path]) == {"pixel data": 1, "chain source": 1, "a frames' axis": 1, "units": 2,
                           "units of source, not decoded": 2}
    (a,) = described(path).stored.arrays
    (chain,) = (described(path).stored.chains[c] for c in a.chains)
    assert [(c.name, c.parameters) for c in chain] == [
        ("source", {"format": Value("dicom"), "id": Value("1.2.840.10008.1.2.4.70")})]
    assert (a.shape, a.item_bits) == ((2, 6, 5), 8)


def test_htj2k_frames_are_in_a_source_chain():
    """The list's `jpeg2k` is a codestream of ISO/IEC 15444-1, which an HTJ2K one is not: its
    frames are `source`, by the transfer syntax, each a unit at its place."""
    path = FIXTURES / "dicom" / "dicom_htj2k_mono16_bot.dcm"
    d = described(path)
    (a,) = d.stored.arrays
    (chain,) = (d.stored.chains[c] for c in a.chains)
    assert [(c.name, c.parameters) for c in chain] == [
        ("source", {"format": Value("dicom"), "id": Value("1.2.840.10008.1.2.4.201")})]
    assert (a.shape, a.item_bits, len(d.rows[0])) == ((2, 12, 10), 16, 2)
    # the frames are HTJ2K streams, which imagecodecs' HTJ2K decoder reads at the unit's shape
    assert [imagecodecs.htj2k_decode(d.unit(r)).shape for r in d.rows[0]] == [(12, 10), (12, 10)]


def test_an_item_whose_attributes_do_not_give_its_frames_has_no_grid(tmp_path):
    """An item's Pixel Data has no shape, and so no grid, without Rows, Columns or Samples per
    Pixel, with one of them 0, with more than one sample and no Planar Configuration, or with a
    Number of Frames that is no count; and no grid without Bits Allocated."""
    def without(keyword: str) -> Dataset:
        ds = filled(image(2, 2, samples=3))
        delattr(ds, keyword)
        return ds

    def given(keyword: str, value: object) -> Dataset:
        ds = filled(image(2, 2))
        setattr(ds, keyword, value)
        return ds

    icons = [without("Rows"), without("Columns"), without("SamplesPerPixel"), without("PlanarConfiguration"),
             without("BitsAllocated"), given("Rows", 0), given("NumberOfFrames", "0"), given("NumberOfFrames", "-2")]
    paths = [with_icons(tmp_path / f"{k}.dcm", icon) for k, icon in enumerate(icons)]
    assert saw(paths) == {"pixel data": 8, "chain empty": 8, "units": 8, "units decoded to pydicom's values": 8,
                          "pixel data of items": 8, "no grid: an item's attributes do not give the frames": 8}
    shapes = [described(p).stored.arrays[0].shape for p in paths]
    assert shapes == [None, None, None, None, (2, 2, 3), None, None, None]
    assert [described(p).stored.arrays[0].item_bits for p in paths] == [8, 8, 8, 8, None, 8, 8, 8]


def test_an_items_fragments_have_no_grid(tmp_path):
    """An item's Pixel Data of undefined length is a fragment sequence the parser does not read as
    frames: it has its shape and sample size, a plane a sample under RLE Lossless, and no grid."""
    icon = image(2, 2, samples=3)
    icon.add_new(PIXEL_DATA, "OB", encapsulate([ramp(10)]))
    icon[PIXEL_DATA].is_undefined_length = True
    plain = with_icons(tmp_path / "plain.dcm", icon)
    ds = pydicom.dcmread(FIXTURES / "dicom" / "dicom_rle_rgb16.dcm")
    ds.IconImageSequence = Sequence([icon])
    rle = tmp_path / "rle.dcm"
    ds.save_as(rle)
    assert saw([plain, rle])["no grid: an item's fragments"] == 2
    assert [described(p).stored.arrays[0].shape for p in (plain, rle)] == [(2, 2, 3), (3, 2, 2)]


def test_an_item_of_a_sample_size_the_profile_does_not_read_has_no_grid(tmp_path):
    """An item's native Pixel Data has a grid for Bits Allocated 8, 16 and 32, and 1 with one
    sample: none for 12 or 64, or for three samples of 1 bit."""
    icons = [filled(image(2, 2, bits=12)), filled(image(2, 2, bits=64)), filled(image(2, 4, samples=3, bits=1))]
    paths = [with_icons(tmp_path / f"{k}.dcm", icon) for k, icon in enumerate(icons)]
    assert saw(paths)["no grid: an item's samples of a size the profile does not read"] == 3
    assert [described(p).stored.arrays[0].item_bits for p in paths] == [12, 64, 1]


def test_an_item_of_colors_the_profile_does_not_read_has_no_grid(tmp_path):
    """An item's native Pixel Data of more than one sample has a grid for three of `RGB` or
    `YBR_FULL`: none for subsampled chroma, whose frames are not Rows by Columns by three samples,
    for four samples, or without a Photometric Interpretation."""
    bare = filled(image(2, 2, samples=3))
    del bare.PhotometricInterpretation
    icons = [filled(image(2, 2, samples=3, photometric="YBR_FULL_422")),
             filled(image(2, 2, samples=4, photometric="ARGB")), bare]
    paths = [with_icons(tmp_path / f"{k}.dcm", icon) for k, icon in enumerate(icons)]
    assert saw(paths)["no grid: an item's samples of colors the profile does not read"] == 3
    assert [described(p).stored.arrays[0].shape for p in paths] == [(2, 2, 3), (2, 2, 4), (2, 2, 3)]


def test_an_item_shorter_than_its_frames_has_no_grid(tmp_path):
    """An item's native Pixel Data whose value does not hold its frames has no grid, of whole
    frames and of 1-bit frames that are one unit together."""
    icons = [filled(image(2, 2, frames=3), less=1), filled(image(5, 3, bits=1, frames=4), less=2)]
    paths = [with_icons(tmp_path / f"{k}.dcm", icon) for k, icon in enumerate(icons)]
    assert saw(paths)["no grid: an item's value shorter than its frames"] == 2


def test_an_items_bytes_under_big_endian_have_no_grid(tmp_path):
    """Under Explicit VR Big Endian a VR of OW swaps the bytes of samples of 8 bits or 1, and the
    parser does not read an item's Pixel Data's VR: such an item has no grid, of either VR."""
    icons = [filled(image(2, 2)), filled(image(2, 2), "OW"), filled(image(4, 4, bits=1))]
    paths = [with_icons(tmp_path / f"{k}.dcm", icon, syntax=ExplicitVRBigEndian) for k, icon in enumerate(icons)]
    assert saw(paths)["no grid: an item's bytes under Explicit VR Big Endian"] == 3


def test_lookup_tables_of_bytes_in_big_endian_words_have_no_grid(tmp_path):
    """Under Explicit VR Big Endian a table of VR OW is 16-bit words, byte-swapped: entries of one
    byte in it have no grid, and those of a table of VR OB have."""
    words, octets = (saved(tmp_path / f"{vr}.dcm", paletted([4, 0, 8], 4, vr), ExplicitVRBigEndian)
                     for vr in ("OW", "OB"))
    assert saw([words])["no grid: a table's bytes in big-endian words"] == 3
    assert saw([octets])["lookup tables"] == 3
