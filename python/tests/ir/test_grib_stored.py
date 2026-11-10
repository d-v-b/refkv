"""The stored arrays of the GRIB fixtures (spec/stored-array.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>), compared with
ecCodes, the reference reader. Of every accepted fixture, and of every bitmap and field of its
messages:

- the array is keyed by its message's element, at its index among the message's bitmaps and
  fields, and its declarations are the sections ecCodes reads the field from;
- its shape, its sample size, its grid and its chain are those ecCodes' keys give (the grid's
  `Nj` and `Ni`, the scanning mode, the number of values, the data representation template and
  the bit width), and each parameter is the place of the octets ecCodes reads the key from;
- its row is the octets of its section 7 that hold its values, at the offset ecCodes gives the
  section in its message;
- those octets, decoded through the chain by decoders written here from the list's entries
  (spec/stored-array.md §3.2 <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#32-the-list>),
  are exactly, bit for bit, the values ecCodes reads, in the file's order: NaN where ecCodes
  reports a missing value of complex packing, and, of a field with a bitmap, the values of the
  points the bitmap keeps.

ecCodes reads a message of several fields a field at a time, each as a message of its own that it
makes of the field's sections, so it gives the offsets in the file of a message's first field
alone. The test walks every message's sections itself (their lengths and numbers), holds that walk
against ecCodes' offsets for each first field, and against the bytes of the sections of the
message ecCodes makes for every field.

ecCodes 2.50 departs from the file in three ways, each counted where it is met. It aborts on a PNG
field of 1, 2 or 4 bits, whose values are then those of `fixtures/grib/expected`, which the
fixtures' generator wrote. Under bitmap indicator 254 it takes the bitmap of the field just before
(none, where that field has none), not of the last field that has one: such a field's values are
then its coded values, as the description has them. And it reads a constant field as its
reference value whatever its decimal scale factor is, as the description does.

A second test asserts which cases the fixtures reached; the tests after it write cases no fixture
holds."""

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
from described import Described, described
from ir_helpers import FIXTURES

from refkv.ir.stored import Codec, Each, ElementPlace, Given, Listed, Source, StoredArray, Value

try:
    import eccodes
except (ImportError, RuntimeError):  # no ecCodes library for this platform
    pytest.skip("no ecCodes library", allow_module_level=True)

GRIB = FIXTURES / "grib"
FILES = sorted(p for p in GRIB.glob("*.grib2") if "reject" not in p.name)
MISSING = 1e300  # ecCodes' missing value while reading: no field holds it
# the grid templates whose rows the parser reads
ROWS = (0, 1, 10, 20, 30, 40)
# the codec of the integers of each packing the list names, by data representation template
PACKINGS = {0: "grib_simple", 2: "grib_complex", 3: "grib_complex", 40: "jpeg2k", 41: "grib_png", 42: "grib_ccsds"}
# the octets of section 5 that are each codec's parameters: (first octet, count)
OCTETS = {
    "grib_scale": {"reference": (12, 4), "binary_scale": (16, 2), "decimal_scale": (18, 2)},
    "grib_simple": {"bits": (20, 1)},
    "grib_complex": {"bits": (20, 1), "groups": (22, 26), "differencing": (48, 2)},
    "jpeg2k": {},
    "grib_png": {},
    "grib_ccsds": {"bits": (20, 1), "flags": (22, 1), "block_size": (23, 1), "reference_sample_interval": (24, 2)},
}
# ecCodes' keys of the octets of `grib_complex`'s `groups`, by the octet each starts at, and its length
GROUPS = {23: ("missingValueManagementUsed", 1), 32: ("numberOfGroupsOfDataValues", 4),
          36: ("referenceForGroupWidths", 1), 37: ("numberOfBitsUsedForTheGroupWidths", 1),
          38: ("referenceForGroupLengths", 4), 42: ("lengthIncrementForTheGroupLengths", 1),
          43: ("trueLengthOfLastGroup", 4), 47: ("numberOfBitsForScaledGroupLengths", 1)}

type Span = tuple[int, int]
"""(offset, length): bytes of the file"""


# ---- the file, by a walk of its sections


@dataclass(frozen=True)
class Held:
    """A field as the file holds it: its message, its index among the message's bitmaps and
    fields, and where its sections 3, 5, 6 and 7 are; the index and the bits of the bitmap that
    says which points its values are, if any; and, where its own section 6 holds a bitmap, that
    bitmap's index."""

    message: int
    start: int
    index: int
    first: bool
    s3: Span
    s5: Span
    s6: Span
    s7: Span
    indicator: int
    mask: tuple[int, Span] | None
    own: int | None


def uint(b: bytes) -> int:
    return int.from_bytes(b, "big")


def walked(b: bytes) -> list[Held]:
    """The fields of the file `b`, by FM 92's framing alone: a message is `GRIB`, its length at
    octets 9–16, and sections of a length (4 octets) and a number (1 octet) up to `7777`."""
    out: list[Held] = []
    pos, message = b.find(b"GRIB"), 0
    while pos >= 0:
        end = pos + uint(b[pos + 8:pos + 16])
        at: dict[int, Span] = {}
        index, last, own, first = 0, None, None, True
        p = pos + 16
        while p < end - 4:
            length, number = uint(b[p:p + 4]), b[p + 4]
            at[number] = (p, length)
            if number == 6 and b[p + 5] == 0:
                last = own = (index, (p + 6, length - 6))
                index += 1
            if number == 7:
                indicator = b[at[6][0] + 5]
                mask = last if indicator in (0, 254) else None
                mine = own and own[0]
                out.append(Held(message, pos, index, first, at[3], at[5], at[6], at[7], indicator, mask, mine))
                index, own, first = index + 1, None, False
            p += length
        pos, message = b.find(b"GRIB", end), message + 1
    return out


# ---- ecCodes


@dataclass(frozen=True)
class Read:
    """What ecCodes reads of a field: the message of that field alone that it makes, where that
    message's sections are, its keys, its values in the file's order (or, where it gives none,
    why: it "aborts", or it "refuses" them with an error) and its bitmap's bits (None where it
    reads none)."""

    message: bytes
    sections: dict[int, Span]
    keys: dict[str, int]
    reference: float
    values: np.ndarray | str
    bitmap: np.ndarray | None


KEYS = ("numberOfValues", "numberOfDataPoints", "dataRepresentationTemplateNumber", "bitsPerValue", "binaryScaleFactor",
        "decimalScaleFactor", "bitMapIndicator", "gridDefinitionTemplateNumber", "sourceOfGridDefinition",
        "numberOfOctectsForNumberOfPoints")
OPTIONAL = ("Ni", "Nj", "scanningMode", "precision", "orderOfSpatialDifferencing", "numberOfOctetsExtraDescriptors",
            "ccsdsFlags", "ccsdsBlockSize", "ccsdsRsi", *(k for k, _ in GROUPS.values()))


def sections_read(h: int) -> dict[int, Span]:
    """Where ecCodes has a handle's sections 3, 5, 6 and 7 in its message."""
    return {s: (eccodes.codes_get_long(h, f"offsetSection{s}"), eccodes.codes_get_long(h, f"section{s}Length"))
            for s in (3, 5, 6, 7)}


def read_field(h: int) -> Read:
    """One handle's field."""
    keys = {k: eccodes.codes_get_long(h, k) for k in KEYS}
    keys |= {k: eccodes.codes_get_long(h, k) for k in OPTIONAL
             if eccodes.codes_is_defined(h, k) and not eccodes.codes_is_missing(h, k)}
    sections = sections_read(h)
    template, bits = keys["dataRepresentationTemplateNumber"], keys["bitsPerValue"]
    values: np.ndarray | str = "aborts"
    # ecCodes 2.50 aborts on a PNG image of samples that are not whole bytes
    if not (template == 41 and bits in (1, 2, 4)):
        eccodes.codes_set(h, "missingValue", MISSING)
        try:
            values = np.array(eccodes.codes_get_values(h), dtype=np.float64)
        except eccodes.CodesInternalError:
            values = "refuses"
    bitmap = None
    # of a predefined bitmap (indicator 1 to 253) ecCodes has no bits
    if keys["bitMapIndicator"] == 0:
        bitmap = np.array(eccodes.codes_get_array(h, "bitmap"), dtype=np.uint8)
    reference = eccodes.codes_get_double(h, "referenceValue")
    return Read(eccodes.codes_get_message(h), sections, keys, reference, values, bitmap)


def read_fields(path: Path) -> tuple[list[Read], list[tuple[int, dict[int, Span]]]]:
    """ecCodes' reading of the file at `path`: every field, in order, with its support for
    messages of several fields on; and, with it off, each message's offset in the file and the
    offsets in the message of its first field's sections."""
    fields: list[Read] = []
    firsts: list[tuple[int, dict[int, Span]]] = []
    eccodes.codes_grib_multi_support_on()
    try:
        with path.open("rb") as f:
            while (h := eccodes.codes_grib_new_from_file(f)) is not None:
                fields.append(read_field(h))
                eccodes.codes_release(h)
    finally:
        eccodes.codes_grib_multi_support_off()
    with path.open("rb") as f:
        while (h := eccodes.codes_grib_new_from_file(f)) is not None:
            start = int(eccodes.codes_get_long(h, "offset"))
            firsts.append((start, sections_read(h)))
            eccodes.codes_release(h)
    return fields, firsts


# ---- the codecs, from their entries


def signed(b: bytes) -> int:
    """An integer whose first bit is its sign and whose others are its magnitude."""
    v = uint(b)
    top = 1 << (8 * len(b) - 1)
    return -(v & (top - 1)) if v & top else v & (top - 1)


def integers(bits: np.ndarray, at: int, count: int, width: int) -> np.ndarray:
    """`count` unsigned integers of `width` bits each, the most significant bit first, from bit
    `at` of `bits` (one bit an item)."""
    if width == 0:
        return np.zeros(count, dtype=np.uint64)
    if at + count * width > len(bits):
        raise ValueError("the unit ends before its integers do")
    cut = bits[at:at + count * width].reshape(count, width).astype(np.uint64)
    return (cut << np.arange(width - 1, -1, -1, dtype=np.uint64)).sum(axis=1, dtype=np.uint64)


def grib_scale(x: bytes, reference: bytes, binary_scale: bytes, decimal_scale: bytes) -> bytes:
    """`(X × 2^E + R) × 10^−D` in binary64, the powers by repeated multiplication or division;
    NaN for −2^63."""
    ints = np.frombuffer(x, dtype="<i8")
    r = np.float64(np.frombuffer(reference, dtype=">f4")[0])
    e, d = signed(binary_scale), signed(decimal_scale)
    two, ten = np.float64(1.0), np.float64(1.0)
    for _ in range(abs(e)):
        two = two * 2.0 if e > 0 else two / 2.0
    for _ in range(abs(d)):
        ten = ten / 10.0 if d > 0 else ten * 10.0
    out = (ints.astype(np.float64) * two + r) * ten
    out[ints == np.iinfo(np.int64).min] = np.nan
    return out.astype("<f8").tobytes()


def grib_simple(unit: bytes, n: int, bits: bytes) -> bytes:
    """`n` integers of `bits` bits each, one after another."""
    return integers(np.unpackbits(np.frombuffer(unit, dtype=np.uint8)), 0, n, bits[0]).astype("<u8").tobytes()


def grib_complex(unit: bytes, n: int, bits: bytes, groups: bytes, differencing: bytes | None) -> bytes:
    """The groups' values, each its group's reference plus its coded value; a missing value
    −2^63; and spatial differences summed back over the values that are not missing."""
    octet = lambda first, count=1: uint(groups[first - 22:first - 22 + count])  # noqa: E731
    management, count = octet(23), octet(32, 4)
    width_reference, width_bits = octet(36), octet(37)
    length_reference, increment, last, length_bits = octet(38, 4), octet(42), octet(43, 4), octet(47)
    order, descriptor = (differencing[0], differencing[1]) if differencing is not None else (0, 0)
    stream = np.unpackbits(np.frombuffer(unit, dtype=np.uint8))
    firsts = [uint(unit[k * descriptor:(k + 1) * descriptor]) for k in range(order)]
    minimum = signed(unit[order * descriptor:(order + 1) * descriptor]) if order else 0
    pos = 8 * descriptor * (order + 1) if order else 0
    references = integers(stream, pos, count, bits[0])
    pos = -(-(pos + count * bits[0]) // 8) * 8
    widths = integers(stream, pos, count, width_bits) + np.uint64(width_reference)
    pos = -(-(pos + count * width_bits) // 8) * 8
    lengths = integers(stream, pos, count, length_bits) * np.uint64(increment) + np.uint64(length_reference)
    lengths[-1] = last
    pos = -(-(pos + count * length_bits) // 8) * 8
    if int(lengths.sum()) != n:
        raise ValueError(f"the groups hold {int(lengths.sum())} values, not the unit's {n}")
    gone = np.iinfo(np.int64).min
    values = np.empty(n, dtype=np.int64)
    done = 0
    for reference, width, length in zip(references.tolist(), widths.tolist(), lengths.tolist(), strict=True):
        coded = integers(stream, pos, length, width)
        pos += length * width
        group = (coded + np.uint64(reference)).astype(np.int64)
        if management:
            # all ones, of the group's width or, for a group of width 0, of the reference's
            ones = np.uint64((1 << width) - 1 if width else (1 << bits[0]) - 1)
            of = coded if width else np.full(length, reference, dtype=np.uint64)
            group[(of == ones) | ((of == ones - np.uint64(1)) if management == 2 else False)] = gone
        values[done:done + length] = group
        done += length
    if order:
        kept = values != gone
        d = values[kept]
        first = np.array(firsts, dtype=np.uint64).astype(np.int64)
        if order == 1 and len(d):
            # the first value as it is; each later one is itself plus the minimum plus the one before
            steps = d + np.int64(minimum)
            steps[0] = first[0]
            values[kept] = np.cumsum(steps)
        elif len(d) == 1:
            values[kept] = first[:1]
        elif len(d):
            # the first two as they are; each later one is itself plus the minimum plus twice the
            # one before less the one before that: its step from the one before is the step
            # before, plus itself and the minimum
            steps = d[1:] + np.int64(minimum)
            steps[0] = first[1] - first[0]
            values[kept] = np.concatenate([first[:1], first[0] + np.cumsum(np.cumsum(steps))])
    return values.astype("<i8").tobytes()


def unfiltered(raw: bytes, row: int, pixel: int) -> bytes:
    """The rows of a PNG image's inflated data, each `row` bytes after its filter's byte, with
    the filters undone; `pixel` is the bytes of a pixel, at least 1."""
    out = bytearray()
    before = bytes(row)
    for at in range(0, len(raw), row + 1):
        kind, line = raw[at], bytearray(raw[at + 1:at + 1 + row])
        for k in range(row):
            left = line[k - pixel] if k >= pixel else 0
            up, corner = before[k], before[k - pixel] if k >= pixel else 0
            if kind == 1:
                predicted = left
            elif kind == 2:
                predicted = up
            elif kind == 3:
                predicted = (left + up) // 2
            elif kind == 4:
                p = left + up - corner
                nearest = min((abs(p - left), 0), (abs(p - up), 1), (abs(p - corner), 2))[1]
                predicted = (left, up, corner)[nearest]
            else:
                predicted = 0
            line[k] = (line[k] + predicted) & 0xFF
        out += line
        before = bytes(line)
    return bytes(out)


def grib_png(unit: bytes, n: int) -> bytes:
    """A PNG file's pixels, each pixel's bits as one integer."""
    if unit[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("the unit is not a PNG file")
    pos, header, data = 8, b"", b""
    while pos < len(unit):
        length, kind = uint(unit[pos:pos + 4]), unit[pos + 4:pos + 8]
        chunk = unit[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            header = chunk
        elif kind == b"IDAT":
            data += chunk
        elif kind == b"PLTE":
            raise ValueError("a PNG file with a palette")
        pos += 12 + length
    width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", header)
    samples = {0: 1, 2: 3, 6: 4}[color]
    if interlace or (samples > 1 and depth != 8) or width * height != n:
        raise ValueError("a PNG file the entry does not take, or of other pixels than the unit's")
    pixel = depth * samples
    row = -(-width * pixel // 8)
    lines = unfiltered(zlib.decompress(data), row, max(pixel // 8, 1))
    rows = np.frombuffer(lines, dtype=np.uint8).reshape(height, row)
    bits = np.unpackbits(rows, axis=1)[:, :width * pixel]
    return integers(np.ascontiguousarray(bits).ravel(), 0, n, pixel).astype("<u8").tobytes()


def grib_ccsds(unit: bytes, n: int, bits: bytes, flags: bytes, block_size: bytes,
               reference_sample_interval: bytes) -> bytes:
    """The first `n` samples libaec decodes of the stream, as the flags say it lays them out."""
    depth, options = bits[0], flags[0]
    if options & 1:
        raise ValueError("signed samples")
    size = (depth + 7) // 8
    if size == 3 and not options & 2:
        size = 4
    # the stream's last block is whole, so it may hold samples after the unit's
    block, interval = block_size[0], uint(reference_sample_interval)
    raw = imagecodecs.aec_decode(unit, bitspersample=depth, flags=options, blocksize=block, rsi=interval,
                                 out=-(-n // block) * block * size)
    if len(raw) < n * size:
        raise ValueError(f"a stream of {len(raw) // size} samples, fewer than the unit's {n}")
    octets = np.frombuffer(raw[:n * size], dtype=np.uint8).reshape(n, size).astype(np.uint64)
    # the most significant octet first with the flag 4, else the least
    shifts = np.arange(size, dtype=np.uint64)[::-1] if options & 4 else np.arange(size, dtype=np.uint64)
    return (octets << (np.uint64(8) * shifts)).sum(axis=1, dtype=np.uint64).astype("<u8").tobytes()


def jpeg2k(unit: bytes, n: int) -> bytes:
    """A JPEG 2000 codestream's samples, in the image's order, as integers of 64 bits."""
    image = imagecodecs.jpeg2k_decode(unit)
    if image.size != n:
        raise ValueError(f"an image of {image.size} samples, not the unit's {n}")
    return image.ravel().astype("<i8").tobytes()


DECODERS: dict[str, Callable[..., bytes]] = {
    "grib_simple": grib_simple, "grib_png": grib_png, "grib_ccsds": grib_ccsds, "jpeg2k": jpeg2k}


def decoded(d: Described, chain: tuple[Codec, ...], unit: bytes, n: int) -> bytes:
    """A unit of `n` elements taken through its chain, from its last codec to its first: the
    elements' bytes."""
    for codec in reversed(chain):
        given = {k: d.place(v.place) for k, v in codec.parameters.items() if isinstance(v, Source)}
        if len(given) != len(codec.parameters):
            raise ValueError(f"{codec.name} with a parameter that is no place")
        if codec.name == "grib_scale":
            unit = grib_scale(unit, **given)
        elif codec.name == "grib_complex":
            unit = grib_complex(unit, n, given["bits"], given["groups"], given.get("differencing"))
        else:
            unit = DECODERS[codec.name](unit, n, **given)
    return unit


# ---- the comparison


def units_of(a: StoredArray) -> tuple[int, ...] | None:
    """The lengths of the one unit of an array's grid, along each axis; None for a grid that is
    not one unit of the whole array under `short`."""
    if a.grid is None or a.shape is None or a.grid.edge != "short" or len(a.grid.axes) != len(a.shape):
        return None
    for axis, length in zip(a.grid.axes, a.shape, strict=True):
        if axis != (Each(length) if length else Listed(())):
            return None
    return a.shape


def spans(d: Described, a: StoredArray) -> list[Span]:
    """Where an array's declarations are in the file."""
    return [d.at(p.element, p.range)[1:] for p in a.declarations if isinstance(p, ElementPlace)]


def expected_shape(held: Held, read: Read) -> tuple[tuple[int, ...], tuple[int, ...] | None]:
    """A field's shape and axes, by ecCodes' keys: the grid's rows where every point has a
    value, else the coded values."""
    k = read.keys
    n, points = k["numberOfValues"], k["numberOfDataPoints"]
    # a grid of rows: of a template the parser reads, with no list of the points of each row
    # (ecCodes' key of section 3's octet 11 is spelled so), `Ni` × `Nj` its points
    rows = (k["gridDefinitionTemplateNumber"] in ROWS and k["sourceOfGridDefinition"] == 0
            and k["numberOfOctectsForNumberOfPoints"] == 0 and k["Ni"] * k["Nj"] == points)
    if not (rows and held.indicator == 255 and n == points):
        return (n,), None
    return ((k["Ni"], k["Nj"]), None) if k["scanningMode"] & 0x20 else ((k["Nj"], k["Ni"]), (1, 0))


def expected_values(read: Read, seen: Counter) -> np.ndarray:
    """A field's coded values, in the file's order, as ecCodes reads them: NaN for a missing
    value, and under a bitmap the values of the points it keeps."""
    assert not isinstance(read.values, str), "ecCodes refuses the values of a field the description decodes"
    values = read.values if read.bitmap is None else read.values[read.bitmap == 1]
    missing = values == MISSING
    seen["fields with missing values of complex packing"] += bool(missing.any())
    return np.where(missing, np.nan, values)


def check_bitmap(d: Described, k: int, name: str, held: Held, read: Read) -> list[str]:
    """A bitmap's array against ecCodes' bitmap of the field it is first read in."""
    a = d.stored.arrays[k]
    said = []
    points = read.keys["numberOfDataPoints"]
    start, length = held.s6
    if spans(d, a) != [held.s3, (start, 6)] or len(a.declarations) != 2:
        said.append(f"declarations at {spans(d, a)}")
    stated = (a.shape, a.axes, a.item_bits, a.pad_from, a.fill, a.unit_parameters)
    if stated != ((points,), None, 1, None, "undefined", ()):
        said.append(f"shape {a.shape}, axes {a.axes}, {a.item_bits} bits, fill {a.fill}")
    rows = d.rows.get(k, [])
    if 8 * (length - 6) < points:
        # the section holds fewer bits than the grid has points
        if a.grid is not None or a.chains or rows:
            said.append(f"grid {a.grid} and {len(rows)} rows, of {8 * (length - 6)} bits for {points} points")
        return said
    if units_of(a) != (points,) or [d.stored.chains[c] for c in a.chains] != [()]:
        said.append(f"grid {a.grid}, chains {[d.stored.chains[c] for c in a.chains]}")
    units = [(r["coords"], d.parts(r)) for r in rows]
    if units != [([0], [(name, start + 6, -(-points // 8))])]:
        said.append(f"rows {units}, of a bitmap at {start + 6} of {length - 6} octets")
        return said
    bits = np.unpackbits(np.frombuffer(d.unit(rows[0]), dtype=np.uint8))[:points]
    if read.bitmap is None or not np.array_equal(bits, read.bitmap):
        said.append("bits that are not ecCodes' bitmap")
    return said


def expected_chain(read: Read) -> tuple[str, list[str]]:
    """How a field's values are stored, by ecCodes' keys: "floats", "constant", "packed" with the
    chain's codecs, "source", or "no grid" where its section 7 does not hold them."""
    k = read.keys
    template, bits, n = k["dataRepresentationTemplateNumber"], k["bitsPerValue"], k["numberOfValues"]
    data = read.sections[7][1] - 5
    if template == 4:
        if k["precision"] not in (1, 2):
            return "source", ["source"]
        return ("floats", []) if 4 * k["precision"] * n <= data else ("no grid", [])
    if template not in PACKINGS:
        return "source", ["source"]
    if template in (2, 3):
        wide = max(bits, k["numberOfBitsUsedForTheGroupWidths"], k["numberOfBitsForScaledGroupLengths"]) > 64
        order, descriptor = k.get("orderOfSpatialDifferencing"), k.get("numberOfOctetsExtraDescriptors", 0)
        taken = template == 2 or (order in (1, 2) and descriptor <= 8)
        if k["missingValueManagementUsed"] > 2 or wide or not taken:
            return "source", ["source"]
        if k["numberOfGroupsOfDataValues"] == 0:
            return "constant", []
    else:
        takes = {0: bits <= 63, 40: True, 41: True, 42: bits <= 32 and not k.get("ccsdsFlags", 0) & 1}
        if not takes[template]:
            return "source", ["source"]
        if bits == 0:
            return "constant", []
    # simple packing's integers, or a stream of at least an octet
    held = -(-n * bits // 8) <= data if template == 0 else data > 0 or n == 0
    return ("packed", ["grib_scale", PACKINGS[template]]) if held else ("no grid", [])


def check_parameters(d: Described, chain: tuple[Codec, ...], held: Held, read: Read) -> list[str]:
    """Each parameter of a packed field's chain is the place of its octets of section 5, which
    hold what ecCodes reads there."""
    said = []
    k = read.keys
    for codec in chain:
        want = dict(OCTETS[codec.name])
        if k["dataRepresentationTemplateNumber"] != 3:
            want.pop("differencing", None)
        got = {name: d.at(p.place.element, p.place.range)[1:] for name, p in codec.parameters.items()
               if isinstance(p, Source) and isinstance(p.place, ElementPlace)}
        places = {name: (held.s5[0] + first - 1, count) for name, (first, count) in want.items()}
        if got != places or len(got) != len(codec.parameters):
            said.append(f"{codec.name}'s parameters at {got}, of a section 5 at {held.s5[0]}")
            continue
        given = {name: d.place(p.place) for name, p in codec.parameters.items() if isinstance(p, Source)}
        if codec.name == "grib_scale":
            r = struct.unpack(">f", given["reference"])[0]
            mine = (r, signed(given["binary_scale"]), signed(given["decimal_scale"]))
            theirs = (read.reference, k["binaryScaleFactor"], k["decimalScaleFactor"])
            if mine != theirs:
                said.append(f"R, E and D {mine}, ecCodes' {theirs}")
        if "bits" in given and uint(given["bits"]) != k["bitsPerValue"]:
            said.append(f"a bit width of {uint(given['bits'])}, ecCodes' {k['bitsPerValue']}")
        if codec.name == "grib_complex":
            mine = {key: uint(given["groups"][first - 22:first - 22 + count]) for first, (key, count) in GROUPS.items()}
            if mine != {key: k[key] for key, _ in GROUPS.values()}:
                said.append(f"groups {mine}")
            order = (k.get("orderOfSpatialDifferencing"), k.get("numberOfOctetsExtraDescriptors"))
            if "differencing" in given and tuple(given["differencing"]) != order:
                said.append(f"differencing {tuple(given['differencing'])}")
        if codec.name == "grib_ccsds":
            mine = (uint(given["flags"]), uint(given["block_size"]), uint(given["reference_sample_interval"]))
            if mine != (k["ccsdsFlags"], k["ccsdsBlockSize"], k["ccsdsRsi"]):
                said.append(f"CCSDS flags, block size and interval {mine}")
    return said


def check_field(d: Described, k: int, path: Path, held: Held, read: Read, seen: Counter) -> list[str]:
    """A field's array against ecCodes' reading of the field."""
    a = d.stored.arrays[k]
    name = path.name
    said = []
    keys = read.keys
    n = keys["numberOfValues"]
    declared = [held.s3, held.s5, (held.s6[0], 6)] + ([held.mask[1]] if held.mask else [])
    if spans(d, a) != declared or len(a.declarations) != len(declared):
        said.append(f"declarations at {spans(d, a)}, the sections at {declared}")
    shape, axes = expected_shape(held, read)
    if (a.shape, a.axes, a.pad_from, a.unit_parameters) != (shape, axes, None, ()):
        said.append(f"shape {a.shape} and axes {a.axes}, ecCodes' {shape} and {axes}")
    laid = "rows" if axes else "columns" if len(shape) == 2 else "coded values, one axis"
    seen[f"fields of {laid}"] += 1
    kind, names = expected_chain(read)
    chains = [d.stored.chains[c] for c in a.chains]
    rows = d.rows.get(k, [])
    data = (held.s7[0] + 5, held.s7[1] - 5)
    template = keys["dataRepresentationTemplateNumber"]
    seen[f"fields of 5.{template}, {kind}"] += 1
    if kind == "no grid":
        # section 7 does not hold the values
        if a.grid is not None or chains or rows or a.item_bits is not None or a.fill != "undefined":
            said.append(f"grid {a.grid}, chains {chains} and {len(rows)} rows, of a section 7 of {held.s7[1]} octets")
        return said
    if units_of(a) != shape or len(chains) != 1 or [c.name for c in chains[0]] != names:
        return [*said, f"grid {a.grid} and chains {chains}, of a field of {kind} {names}"]
    (chain,) = chains
    if kind == "constant":
        # no value is coded: the reference value, where section 5 holds it, is every value
        at = (held.s5[0] + 11, 4)
        fill = a.fill.param if isinstance(a.fill, Given) else None
        place = fill.place if isinstance(fill, Source) else None
        placed = isinstance(place, ElementPlace) and d.at(place.element, place.range)[1:] == at
        if not isinstance(place, ElementPlace) or not placed or a.item_bits != 32 or rows:
            return [*said, f"{a.item_bits} bits, {len(rows)} rows and the fill {a.fill}, of a constant field"]
        value = np.frombuffer(d.place(place), dtype=">f4").astype(np.float64)
        want = expected_values(read, seen)
        if len(want) != n or not np.array_equal(np.broadcast_to(value, (n,)), want):
            said.append(f"a fill value of {value}, and ecCodes reads {want[:3]}")
        seen["constant fields whose decimal scale factor is not 0"] += keys["decimalScaleFactor"] != 0
        return said
    if a.fill != "undefined":
        said.append(f"the fill {a.fill}")
    if kind == "source":
        (codec,) = chain
        parameters = codec.parameters.get("parameters")
        place = parameters.place if isinstance(parameters, Source) else None
        values = {key: v.value for key, v in codec.parameters.items() if isinstance(v, Value)}
        rest = (held.s5[0] + 11, held.s5[1] - 11)
        stated = values == {"format": "grib", "id": template} and len(codec.parameters) == 3 and a.item_bits is None
        if not isinstance(place, ElementPlace) or d.at(place.element, place.range)[1:] != rest or not stated:
            said.append(f"source's parameters {codec.parameters}, of template 5.{template}")
        length = data[1]
    elif kind == "floats":
        size = 4 * keys["precision"]
        if a.item_bits != 8 * size:
            said.append(f"{a.item_bits} bits, of IEEE floats of precision {keys['precision']}")
        length = n * size
    else:
        if a.item_bits != 64:
            said.append(f"{a.item_bits} bits, of packed values")
        said += check_parameters(d, chain, held, read)
        length = -(-n * keys["bitsPerValue"] // 8) if template == 0 else data[1]
    want_rows = [([0] * len(shape), [(name, data[0], length)])] if n and length else []
    if [(r["coords"], d.parts(r)) for r in rows] != want_rows:
        return [*said, f"rows {[(r['coords'], d.parts(r)) for r in rows]}, of a section 7 at {held.s7}: {want_rows}"]
    if said or not rows or kind == "source":
        return said
    # the unit, through its chain, is ecCodes' values, bit for bit
    unit = d.unit(rows[0])
    stored = "<f8" if chain else ">f4" if a.item_bits == 32 else ">f8"
    got = np.frombuffer(decoded(d, chain, unit, n), dtype=stored).astype(np.float64)
    want = expected_values(read, seen)
    same = got.shape == want.shape and np.array_equal(got, want, equal_nan=True)
    if not same or not np.array_equal(np.signbit(got), np.signbit(want)):
        said.append(f"values {got[:4]}, ecCodes' {want[:4]}")
    seen["units decoded"] += 1
    return said


def held_against_eccodes(b: bytes, held: list[Held], fields: list[Read], firsts: list[tuple[int, dict[int, Span]]],
                         seen: Counter) -> list[str]:
    """The walk of the file's sections against ecCodes: each message's first field is where
    ecCodes gives its sections, and every field's sections hold the bytes of the message ecCodes
    makes of it."""
    said = []
    first = [h for h in held if h.first]
    mine = [(h.start, {3: h.s3, 5: h.s5, 6: h.s6, 7: h.s7}) for h in first]
    theirs = [(start, {s: (start + o, n) for s, (o, n) in at.items()}) for start, at in firsts]
    if mine != theirs:
        said.append(f"the messages' first fields at {mine}, ecCodes' at {theirs}")
    seen["fields at the offsets ecCodes gives"] += len(first)
    seen["fields ecCodes gives no offsets of"] += len(held) - len(first)
    for h, r in zip(held, fields, strict=True):
        # under indicator 254, the message ecCodes makes has another section 6 than the field's
        mine = {3: h.s3, 5: h.s5, 7: h.s7} | ({} if h.indicator == 254 else {6: h.s6})
        for s, (o, n) in mine.items():
            at, length = r.sections[s]
            if b[o:o + n] != r.message[at:at + length]:
                said.append(f"message {h.message}: section {s} at {o} is not the one of ecCodes' message")
        if h.indicator == 254:
            # ecCodes takes the bitmap of the field just before, or none
            seen[f"fields of indicator 254 whose bitmap ecCodes {'leaves' if r.bitmap is None else 'applies'}"] += 1
            if r.bitmap is None:
                continue
            start, length = h.mask[1] if h.mask is not None else (0, 0)
            bits = np.unpackbits(np.frombuffer(b[start:start + length], dtype=np.uint8))[:len(r.bitmap)]
            if not np.array_equal(bits, r.bitmap):
                said.append(f"message {h.message}: the bitmap before field {h.index} is not the one ecCodes applies")
    return said


def compare(path: Path, known: dict[int, np.ndarray] | None = None) -> tuple[tuple[str, ...], Counter]:
    """What differs between the descriptions of the file at `path` and ecCodes, and what the
    comparison saw. Of a field ecCodes cannot decode, the values are those `known` gives for its
    number in the file, or those the fixtures' generator wrote for the fixture."""
    d = described(path)
    seen: Counter = Counter()
    if d.stored.undescribed:
        return ("descriptions dropped for room",), seen
    b = path.read_bytes()
    held = walked(b)
    fields, firsts = read_fields(path)
    if len(fields) != len(held):
        return (f"{len(held)} fields, ecCodes {len(fields)}",), seen
    said = held_against_eccodes(b, held, fields, firsts, seen)
    # the arrays, by key: a message's bitmaps and fields in the order of their sections
    keys = {(d.ir.path(a.element), a.index): k for k, a in enumerate(d.stored.arrays)}
    want = {(f"{path.name}/messages/{h.message}", i) for h in held for i in (h.index, h.own) if i is not None}
    if set(keys) != want or len(keys) != len(d.stored.arrays):
        return (*said, f"arrays keyed {sorted(keys)}, the file's bitmaps and fields {sorted(want)}"), seen
    for number, (h, read) in enumerate(zip(held, fields, strict=True)):
        message = f"{path.name}/messages/{h.message}"
        at = f"message {h.message}, array {h.index}"
        seen["fields"] += 1
        r = read
        if isinstance(read.values, str) and read.values == "aborts":
            # values of every point in the order of rows: these fields have neither a bitmap nor
            # another scanning mode than rows first
            seen["fields ecCodes cannot decode"] += 1
            assert read.bitmap is None
            assert read.keys["scanningMode"] == 0
            given = known[number] if known else np.load(GRIB / "expected" / f"{path.stem}.{h.index}.npy").ravel()
            r = replace(read, values=given)
        if h.own is not None:
            seen["bitmaps"] += 1
            mask = check_bitmap(d, keys[message, h.own], path.name, h, r)
            said += [f"message {h.message}, array {h.own}: {s}" for s in mask]
        if h.mask is not None:
            seen["fields under their own bitmap" if h.indicator == 0 else "fields under an earlier bitmap"] += 1
        said += [f"{at}: {s}" for s in check_field(d, keys[message, h.index], path, h, r, seen)]
    return tuple(said), seen


@cache
def compared(path: Path) -> tuple[tuple[str, ...], Counter]:
    return compare(path)


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_grib_fields_are_described_as_eccodes_reads_them(path):
    """Every bitmap and field of the file is described where ecCodes reads it, with the shape,
    the sample size and the parameters ecCodes' keys give, and its unit decodes through its chain
    to exactly the values ecCodes reads."""
    said, seen = compared(path)
    assert said == ()
    assert seen["fields"] >= 1


def test_the_fixtures_hold_every_case_the_comparison_reads():
    """The fixtures reach each case of the comparison, so that none of it passes for want of a
    file. The three fields ecCodes cannot decode are the PNG fields of 1, 2 and 4 bits; the field
    of indicator 254 whose bitmap ecCodes leaves is `grib_bitmap_254`'s third, after a field with
    no bitmap; the constant fields of a decimal scale factor of 2 are `grib_constant` and
    `grib_jpeg2000_constant`, which ecCodes wrote and reads as their reference value."""
    seen: Counter = Counter()
    for path in FILES:
        seen += compared(path)[1]
    assert {k: v for k, v in seen.items() if v} == {
        "fields": 79,
        "fields at the offsets ecCodes gives": 71,
        "fields ecCodes gives no offsets of": 8,
        "fields of rows": 65,
        "fields of columns": 2,
        "fields of coded values, one axis": 12,
        "bitmaps": 7,
        "fields under their own bitmap": 7,
        "fields under an earlier bitmap": 2,
        "fields of indicator 254 whose bitmap ecCodes applies": 1,
        "fields of indicator 254 whose bitmap ecCodes leaves": 1,
        "fields of 5.0, packed": 36,
        "fields of 5.0, constant": 2,
        "fields of 5.2, packed": 3,
        "fields of 5.3, packed": 12,
        "fields of 5.4, floats": 2,
        "fields of 5.40, packed": 10,
        "fields of 5.40, constant": 1,
        "fields of 5.41, packed": 7,
        "fields of 5.42, packed": 6,
        "constant fields whose decimal scale factor is not 0": 2,
        "fields with missing values of complex packing": 6,
        "fields ecCodes cannot decode": 3,
        "units decoded": 76,
    }


# ---- cases no fixture holds, written from the fixtures' messages


def parts(message: bytes) -> list[bytes]:
    """A message's sections 1 to 7, in order, each with its length and number."""
    out, pos = [], 16
    while pos < len(message) - 4:
        length = uint(message[pos:pos + 4])
        out.append(message[pos:pos + length])
        pos += length
    return out


def section(number: int, body: bytes) -> bytes:
    """A section: its length, its number and the octets after them."""
    return struct.pack(">IB", 5 + len(body), number) + body


def message_of(like: bytes, sections: list[bytes]) -> bytes:
    """A message of `sections`, with the discipline of the message `like`."""
    body = b"".join(sections)
    return like[:8] + struct.pack(">Q", 16 + len(body) + 4) + body + b"7777"


def edited(message: bytes, number: int, edit: Callable[[bytearray], bytes]) -> bytes:
    """The one-field message with the octets of its section `number` after the first 5 replaced
    by what `edit` makes of them, the section's and the message's lengths set again."""
    return message_of(message, [section(number, bytes(edit(bytearray(s[5:])))) if s[4] == number else s
                                for s in parts(message)])


def octets(first: int, value: bytes) -> Callable[[bytearray], bytes]:
    """An edit that sets a section's octets from octet `first` to `value`."""
    def edit(body: bytearray) -> bytes:
        body[first - 6:first - 6 + len(value)] = value
        return bytes(body)
    return edit


def fixture(name: str) -> bytes:
    return (GRIB / f"{name}.grib2").read_bytes()


def written(tmp_path: Path, stem: str, *messages: bytes) -> Path:
    """A file of `grib_simple`'s message, which the profile presents, then `messages`."""
    path = tmp_path / f"{stem}.grib2"
    path.write_bytes(fixture("grib_simple") + b"".join(messages))
    return path


def last_field(path: Path) -> tuple[Described, StoredArray, list[dict], Held]:
    """The description of the last field of the file at `path`, its rows, and where the file
    holds it."""
    d = described(path)
    held = walked(path.read_bytes())[-1]
    (k,) = [k for k, a in enumerate(d.stored.arrays)
            if (d.ir.path(a.element), a.index) == (f"{path.name}/messages/{held.message}", held.index)]
    return d, d.stored.arrays[k], d.rows.get(k, []), held


def source_of(d: Described, a: StoredArray) -> dict[str, object]:
    """The parameters of an array's one chain, which is `source` alone: its values, and where its
    place is in the file."""
    ((codec,),) = [d.stored.chains[c] for c in a.chains]
    assert codec.name == "source"
    out: dict[str, object] = {}
    for k, v in codec.parameters.items():
        if isinstance(v, Value):
            out[k] = v.value
        elif isinstance(v.place, ElementPlace):
            out[k] = d.at(v.place.element, v.place.range)[1:]
    return out


def assert_source(path: Path, template: int) -> None:
    """The last field of the file at `path` is one unit of all of its section 7's data, of no
    stated sample size, in a `source` chain of its template's number and octets."""
    d, a, rows, held = last_field(path)
    assert source_of(d, a) == {"format": "grib", "id": template, "parameters": (held.s5[0] + 11, held.s5[1] - 11)}
    assert (a.item_bits, a.fill, units_of(a)) == (None, "undefined", a.shape)
    assert [d.parts(r) for r in rows] == [[(path.name, held.s7[0] + 5, held.s7[1] - 5)]]


def logarithms() -> bytes:
    """`grib_simple`'s field in a packing the list names no codec for (5.61, simple packing of
    logarithms), which ecCodes writes and reads."""
    h = eccodes.codes_new_from_message(fixture("grib_simple"))
    try:
        values = eccodes.codes_get_values(h)
        eccodes.codes_set(h, "packingType", "grid_simple_log_preprocessing")
        eccodes.codes_set_values(h, values)
        return eccodes.codes_get_message(h)
    finally:
        eccodes.codes_release(h)


def unread_grid() -> bytes:
    """A field on a grid of a template the parser does not read (3.140, Lambert azimuthal equal
    area), which ecCodes writes."""
    h = eccodes.codes_grib_new_from_samples("GRIB2")
    try:
        eccodes.codes_set(h, "gridType", "lambert_azimuthal_equal_area")
        eccodes.codes_set(h, "Nx", 16)
        eccodes.codes_set(h, "Ny", 12)
        eccodes.codes_set_values(h, np.linspace(250.0, 300.0, 192))
        return eccodes.codes_get_message(h)
    finally:
        eccodes.codes_release(h)


def regrouped(bits: int) -> bytes:
    """`grib_simple`'s field in complex packing (5.2), written here: its integers of 12 bits in two
    groups of 96, the groups' references 0, each of `bits` bits, and their lengths counted in
    twos (a length increment of 2)."""
    simple = fixture("grib_simple")
    s5, s7 = (next(s for s in parts(simple) if s[4] == number) for number in (5, 7))
    # the values, template 5.2, R, E and D, the references' bits, the type of the values, the
    # splitting method, no missing values and their substitutes; then 2 groups, their widths 0
    # and a coded width of 8 bits, their lengths 0 and twice a scaled length of 8 bits, the last 96
    template = (s5[5:9] + (2).to_bytes(2, "big") + s5[11:19] + bytes([bits, s5[20], 1, 0]) + bytes(8)
                + (2).to_bytes(4, "big") + bytes([0, 8]) + bytes(4) + bytes([2]) + (96).to_bytes(4, "big") + bytes([8]))
    data = bytes(-(-2 * bits // 8)) + bytes([12, 12]) + bytes([48, 0]) + s7[5:]
    return edited(edited(simple, 5, lambda _: template), 7, lambda _: data)


def png_of_rows() -> tuple[bytes, np.ndarray]:
    """A PNG field (5.41) of 12 rows of 15 gray samples of 4 bits, each row ending in 4 bits of
    padding, which ecCodes cannot decode; and its values. It is made of a field in simple packing
    of 4 bits that ecCodes writes and reads: the same integers, a pixel each, so the same values."""
    h = eccodes.codes_new_from_message(fixture("grib_simple"))
    try:
        eccodes.codes_set(h, "Ni", 15)
        eccodes.codes_set(h, "bitsPerValue", 4)
        eccodes.codes_set_values(h, np.linspace(250.0, 300.0, 180))
        simple = eccodes.codes_get_message(h)
        values = np.array(eccodes.codes_get_values(h), dtype=np.float64)
    finally:
        eccodes.codes_release(h)
    s5, s7 = (next(s for s in parts(simple) if s[4] == number) for number in (5, 7))
    assert (uint(s5[5:9]), s5[19], len(s7)) == (180, 4, 95)
    # each row: no filter, then its 15 pixels and 4 bits of padding
    pixels = np.unpackbits(np.frombuffer(s7[5:], dtype=np.uint8)).reshape(12, 60)
    filters, padding = np.zeros((12, 8), dtype=np.uint8), np.ones((12, 4), dtype=np.uint8)
    rows = np.packbits(np.concatenate([filters, pixels, padding], axis=1))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 15, 12, 4, 0, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(rows.tobytes())) + chunk(b"IEND", b""))
    return edited(edited(simple, 5, octets(10, (41).to_bytes(2, "big"))), 7, lambda _: png), values


def test_written_fields_are_described_as_eccodes_reads_them(tmp_path):
    """Cases the fixtures do not hold, in one file of 22 messages, each read by ecCodes as the
    fixtures are: a message after padding, at another offset than 0; a bitmap whose section holds
    octets after its points' bits, whose row is the range of the first; a constant field under a
    bitmap; complex packing of no groups, with and without spatial differencing, which ecCodes
    reads as the reference value; a field of fewer values than points with no bitmap, with a
    predefined bitmap (indicator 5), and with indicator 254 and no bitmap before it, each its
    coded values on one axis; a field of no values, under a bitmap that keeps no point, in simple
    packing and in JPEG 2000, which then holds no codestream; a field on a grid the parser does
    not read, one on a grid that no template defines (section 3's octet 6), and one whose `Ni` ×
    `Nj` is not its points, each one axis of its values; simple packing and IEEE floats whose
    section 7 holds octets after the values', whose rows are the ranges of the values' octets
    alone; a message of two fields under a bitmap that keeps every point, the second by indicator
    254, each still one axis of its coded values; a negative decimal scale factor; complex
    packing whose groups' lengths are counted in twos, with references of 8 bits and of none (a
    bit width of 0, which is no constant field while there are groups); CCSDS samples of 24 bits
    without libaec's flag of 3 octets a sample; and a PNG image whose rows end in padding bits,
    which ecCodes cannot decode: its values are those of the simple packing it was made of."""
    bitmap, simple = fixture("grib_bitmap"), fixture("grib_simple")
    constant = edited(edited(bitmap, 5, octets(20, b"\0")), 7, lambda _: b"")
    no_groups = [edited(edited(fixture(name), 5, octets(32, bytes(4))), 7, lambda _: b"")
                 for name in ("grib_complex", "grib_complex_sd2")]
    fewer = edited(edited(simple, 5, octets(6, (100).to_bytes(4, "big"))), 7, lambda body: bytes(body[:150]))
    none_kept = edited(bitmap, 6, lambda body: bytes(body[:1]) + bytes(len(body) - 1))
    none_kept = edited(edited(none_kept, 5, octets(6, bytes(4))), 7, lambda _: b"")
    longer = edited(bitmap, 6, lambda body: bytes(body) + b"\0\0")
    predefined, alone = edited(fewer, 6, octets(6, bytes([5]))), edited(fewer, 6, octets(6, bytes([254])))
    undefined = edited(simple, 3, octets(6, bytes([1])))
    padded = [edited(fixture(name), 7, lambda body: bytes(body) + b"\0\0\0") for name in ("grib_simple", "grib_ieee32")]
    full = edited(simple, 6, lambda _: b"\0" + b"\xff" * 24)
    again = [section(6, b"\xfe") if s[4] == 6 else s for s in parts(simple) if s[4] >= 4]
    kept = message_of(simple, [*parts(full), *again])
    ragged = edited(simple, 3, octets(31, (8).to_bytes(4, "big")))
    negative = edited(simple, 5, octets(18, b"\x80\x01"))
    no_stream = edited(fixture("grib_jpeg2000_bitmap"), 6, lambda body: bytes(body[:1]) + bytes(len(body) - 1))
    no_stream = edited(edited(no_stream, 5, octets(6, bytes(4))), 7, lambda _: b"")
    four_octets = edited(fixture("grib_ccsds_24bit"), 5, octets(22, bytes([12])))
    png, values = png_of_rows()
    path = written(tmp_path, "written", b"\0\0\0", fixture("grib_ieee64"), longer, constant, *no_groups, fewer,
                   predefined, alone, none_kept, unread_grid(), undefined, *padded, kept, ragged, negative,
                   no_stream, regrouped(8), regrouped(0), four_octets, png)
    said, seen = compare(path, {22: values})
    assert said == ()
    assert {k: v for k, v in seen.items() if v} == {
        "fields": 23,
        "fields at the offsets ecCodes gives": 22,
        "fields ecCodes gives no offsets of": 1,
        "fields of rows": 11,
        "fields of coded values, one axis": 12,
        "bitmaps": 5,
        "fields under their own bitmap": 5,
        "fields under an earlier bitmap": 1,
        "fields of indicator 254 whose bitmap ecCodes applies": 1,
        "fields of indicator 254 whose bitmap ecCodes leaves": 1,
        "fields of 5.0, packed": 13,
        "fields of 5.0, constant": 1,
        "fields of 5.2, packed": 2,
        "fields of 5.2, constant": 1,
        "fields of 5.3, constant": 1,
        "fields of 5.4, floats": 2,
        "fields of 5.40, packed": 1,
        "fields of 5.41, packed": 1,
        "fields of 5.42, packed": 1,
        "fields ecCodes cannot decode": 1,
        "units decoded": 18,
    }
    d = described(path)
    by_message = {int(d.ir.path(a.element).rsplit("/", 1)[1]): k for k, a in enumerate(d.stored.arrays)}
    # the bitmap of 26 octets for 192 points: the range of its first 24
    (row,) = d.rows[by_message[2] - 1]
    assert (row["range"], d.ir.extent(row["element"])[1]) == ({"offset": 0, "length": 24}, 26)
    # the field of no values, and the bitmap that keeps no point
    assert (d.stored.arrays[by_message[9]].shape, d.rows.get(by_message[9])) == ((0,), None)
    assert units_of(d.stored.arrays[by_message[9]]) == (0,)
    # the sections 7 of three octets after their values': the values' octets alone
    for message, length in ((12, 288), (13, 768)):
        (row,) = d.rows[by_message[message]]
        assert (row["range"], d.ir.extent(row["element"])[1]) == ({"offset": 5, "length": length}, 5 + length + 3)


def test_a_packing_the_list_has_no_name_for_is_source(tmp_path):
    """A field of a data representation template the list names no codec for (5.61, simple
    packing of logarithms, which ecCodes writes and reads) is one unit of its section 7's data,
    in a `source` chain of the template's number and octets."""
    path = written(tmp_path, "logarithms", logarithms())
    said, seen = compare(path)
    assert said == ()
    assert seen["fields of 5.61, source"] == 1
    assert_source(path, 61)


def test_ieee_floats_of_another_precision_are_source(tmp_path):
    """IEEE floats of precision 3 (binary128), which ecCodes does not read, are `source`."""
    path = written(tmp_path, "binary128", edited(fixture("grib_ieee32"), 5, octets(12, bytes([3]))))
    said, seen = compare(path)
    assert said == ()
    assert seen["fields of 5.4, source"] == 1
    assert_source(path, 4)


def test_simple_packing_of_64_bits_is_source(tmp_path):
    """Simple packing of a bit width of 64, past the integers `grib_scale` takes, is `source`."""
    path = written(tmp_path, "wide", edited(fixture("grib_simple"), 5, octets(20, bytes([64]))))
    said, seen = compare(path)
    assert said == ()
    assert seen["fields of 5.0, source"] == 1
    assert_source(path, 0)


def test_complex_packing_of_other_options_is_source(tmp_path):
    """Complex packing of missing value management 3, and spatial differencing of order 3, which
    `grib_complex` does not take, are `source`."""
    for stem, name, first, template in (("management", "grib_complex", 23, 2), ("order", "grib_complex_sd2", 48, 3)):
        path = written(tmp_path, stem, edited(fixture(name), 5, octets(first, bytes([3]))))
        said, seen = compare(path)
        assert said == ()
        assert seen[f"fields of 5.{template}, source"] == 1
        assert_source(path, template)


def test_signed_ccsds_samples_are_source(tmp_path):
    """CCSDS packing whose flags say signed samples (libaec's flag 1), which `grib_ccsds` does not
    take, is `source`. ecCodes reads the field as if the flag were not set."""
    path = written(tmp_path, "signed", edited(fixture("grib_ccsds"), 5, octets(22, bytes([15]))))
    said, seen = compare(path)
    assert said == ()
    assert seen["fields of 5.42, source"] == 1
    assert_source(path, 42)


def test_a_section_5_shorter_than_its_template_is_source(tmp_path):
    """A section 5 of simple packing that ends before its bit width (19 octets), which ecCodes
    does not read (it takes the section for one of 21), is `source`: the description is held
    against the test's own walk of the sections."""
    path = written(tmp_path, "short", edited(fixture("grib_simple"), 5, lambda body: bytes(body[:14])))
    assert walked(path.read_bytes())[-1].s5[1] == 19
    assert_source(path, 0)


def test_a_section_7_shorter_than_its_values_has_no_grid(tmp_path):
    """A field whose section 7 holds an octet less than its IEEE floats, or than its simple
    packing's integers, take has no grid, no chain and no row: its shape and its declarations
    alone. ecCodes reads the floats past the section's end, and refuses the integers."""
    for stem, name in (("floats", "grib_ieee32"), ("integers", "grib_simple")):
        path = written(tmp_path, stem, edited(fixture(name), 7, lambda body: bytes(body[:-1])))
        said, seen = compare(path)
        assert said == ()
        assert seen[f"fields of 5.{4 if stem == 'floats' else 0}, no grid"] == 1
        d, a, rows, held = last_field(path)
        assert (a.shape, a.axes, a.grid, a.chains, a.item_bits, rows) == ((12, 16), (1, 0), None, (), None, [])
        assert spans(d, a) == [held.s3, held.s5, (held.s6[0], 6)]


def test_a_stream_of_no_octets_has_no_grid(tmp_path):
    """A JPEG 2000 field with values to code and a section 7 of 5 octets, which holds no
    codestream, has no grid: no unit of no bytes is named."""
    path = written(tmp_path, "empty", edited(fixture("grib_jpeg2000_20bit"), 7, lambda _: b""))
    said, seen = compare(path)
    assert said == ()
    assert seen["fields of 5.40, no grid"] == 1
    _, a, rows, _ = last_field(path)
    assert (a.shape, a.grid, a.chains, rows) == ((12, 16), None, (), [])


def test_a_bitmap_of_fewer_bits_than_points_has_no_grid(tmp_path):
    """A bitmap whose section holds fewer bits than the grid has points (23 octets for 192
    points; and none, a section 6 of 6 octets) has no grid, and its field is still its coded
    values, with the bits the section holds among its declarations where it holds any. ecCodes
    reads the bits it lacks from the octets after the section, so its values are not compared."""
    for stem, keep in (("short", 23), ("none", 0)):
        cut = edited(fixture("grib_bitmap"), 6, lambda body, keep=keep: bytes(body[:1 + keep]))
        path = written(tmp_path, stem, cut)
        d, a, rows, held = last_field(path)
        (b,) = [x for x in d.stored.arrays if x.element == a.element and x.index == 0]
        assert (a.index, b.shape, b.item_bits, b.grid, b.chains) == (1, (192,), 1, None, ())
        assert spans(d, b) == [held.s3, (held.s6[0], 6)]
        assert not d.rows.get(d.stored.arrays.index(b))
        # the field: its 180 coded values, in simple packing
        assert [c.name for c in d.stored.chains[a.chains[0]]] == ["grib_scale", "grib_simple"]
        assert (a.shape, units_of(a)) == ((180,), (180,))
        assert [d.parts(r) for r in rows] == [[(path.name, held.s7[0] + 5, 270)]]
        assert spans(d, a) == [held.s3, held.s5, (held.s6[0], 6)] + ([(held.s6[0] + 6, keep)] if keep else [])
