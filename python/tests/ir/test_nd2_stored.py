"""The stored arrays of the ND2 fixtures (spec/stored-array.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>), compared with the
nd2 library, the reference reader. Of each file:

- the array of the frames the loops count is keyed by the chunk nd2's chunk map gives for
  `ImageAttributesLV!`, and its declarations are the data nd2 loads of that chunk and of
  `ImageMetadataSeqLV|0!`;
- its shape, sample size, grid and chain are what nd2's decoding of the attributes says: the
  height, the width and the components, a frame a unit, or a row a unit where `uiWidthBytes` is
  more than a row's samples, in a `zlib` chain where the frames are compressed; its first axis is
  nd2's count of the loops' frames;
- each frame nd2's chunk map names has its rows, at the place nd2's frame offsets give, and no
  frame it does not name has one;
- each frame's units, decoded through the chain, are the samples nd2's frame reader reads;
- each frame chunk past the loops' count is an array of one frame, compared likewise.

Where nd2 does not read a case, the case is counted, and the second test asserts the counts:

- nd2 loads the attributes only with the picture metadata's channel count, and of the files
  without `ImageMetadataSeqLV|0!` (or whose attributes lack `uiSequenceCount`, or whose picture
  metadata it does not decode) it loads none: its frame reader is then given the attributes its
  own `load_attributes` makes of its own decoding of the chunk, with one channel, and reads the
  frames itself;
- nd2's frame reader takes a frame chunk's name to be 4072 bytes, as NIS-Elements writes it,
  whatever the chunk's header says, and its frame offsets fail on a chunk named `ImageDataSeq|x!`:
  such a frame is compared with the data nd2's chunk reader loads of its chunk, which follows the
  header, the rows cut from it here;
- nd2 does not read the loops of the generator's time loops, which lack members it requires, and
  counts those of one file otherwise (`COUNTED_OTHERWISE`).

nd2 takes any digits after `ImageDataSeq|` as a frame's number; the convention, and this test,
only a number without leading zeros."""

import importlib.util
import re
import struct
import zlib
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import nd2
import numpy as np
import pytest
from described import Described, described
from ir_helpers import FIXTURES, ROOT
from nd2._parse._parse import load_attributes
from nd2._readers import ModernReader

from refkv.ir.kind import Kind
from refkv.ir.stored import Each, ElementPlace, StoredArray

FILES = sorted(p for p in (FIXTURES / "nd2").glob("*.nd2") if "reject" not in p.name)
# the bytes of a frame chunk's name that nd2's frame reader takes every frame to have
NAME = 4072
# a frame's chunk, by the convention's rule for its number
FRAME = re.compile(rb"ImageDataSeq\|(0|[1-9][0-9]*)!")
# the chunks whose data declare what a sample is, in the declarations' order
DECLARING = (b"ImageAttributesLV!", b"ImageMetadataSeqLV|0!")
# the frames nd2 counts of a file's loops, where it counts another number than the convention:
# a time loop of periods with no `pPeriodValid` list, which the convention reads as every period
# valid (spec/zarrify/nd2.md §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nd2.md#3-metadata>)
# and nd2 as none. The description's count is then compared with the file's `uiSequenceCount`.
COUNTED_OTHERWISE = {"nd2_edge_validity.nd2": 1}


@dataclass(frozen=True)
class Layout:
    """How the attributes, as nd2 decodes them, say a frame is stored."""

    height: int
    width: int
    components: int
    bits: int
    width_bytes: int
    compressed: bool
    sequence: int | None
    """the frames the attributes count (`uiSequenceCount`), which the convention does not read"""

    @property
    def row(self) -> int:
        """The bytes of a row's samples."""
        return self.width * self.components * self.bits // 8

    @property
    def padded(self) -> bool:
        return not self.compressed and self.width_bytes != self.row

    @property
    def shape(self) -> tuple[int, ...]:
        """A frame's axes: the components come last, and only where there are several."""
        return (self.height, self.width, self.components)[:3 if self.components > 1 else 2]

    @property
    def units(self) -> int:
        """The units a frame is cut into: its rows, where they are padded."""
        return self.height if self.padded else 1

    def held(self, declared: int, loaded: int) -> bool:
        """Whether a chunk holds a frame after its timestamp: one whose header gives `declared`
        bytes of data, `loaded` of them in the file. A stream is all of the data, and rows lie
        within what the header gives, their samples in the file."""
        if self.compressed:
            return declared > 8 and loaded == declared
        rows = (self.height - 1) * self.width_bytes + self.row
        return declared >= 8 + self.height * self.width_bytes and loaded >= 8 + rows

    def unit(self, r: int, stream: int) -> tuple[int, int]:
        """(offset from the frame's first byte, length) of unit `r`; `stream`: the bytes of a
        compressed frame's stream."""
        if self.compressed:
            return 0, stream
        return (r * self.width_bytes, self.row) if self.padded else (0, self.height * self.row)

    def samples(self, pixels: bytes) -> bytes:
        """A frame's samples of the bytes after its chunk's timestamp: the stream inflated, or the
        rows without their padding."""
        if self.compressed:
            return zlib.decompress(pixels)
        return b"".join(pixels[r * self.width_bytes:r * self.width_bytes + self.row] for r in range(self.height))


def modern(f: nd2.ND2File) -> ModernReader:
    r = f._rdr
    assert isinstance(r, ModernReader), "the fixtures are no legacy files"
    return r


def layout(reader: ModernReader) -> Layout:
    """The frames' layout, of nd2's decoding of `ImageAttributesLV!`."""
    raw = reader._decode_chunk(b"ImageAttributesLV!", strip_prefix=False)["SLxImageAttributes"]
    return Layout(raw["uiHeight"], raw["uiWidth"], raw["uiComp"], raw["uiBpcInMemory"], raw["uiWidthBytes"],
                  raw.get("eCompression", 2) == 0, raw.get("uiSequenceCount"))


def given_attributes(reader: ModernReader, seen: Counter) -> None:
    """Has nd2's frame reader read with the attributes nd2 makes of `ImageAttributesLV!`, where it
    loads none itself for want of the picture metadata or of `uiSequenceCount`."""
    try:
        reader.attributes()
        seen["files nd2 loads the attributes of"] += 1
    except (KeyError, UnicodeDecodeError):
        raw = reader._decode_chunk(b"ImageAttributesLV!", strip_prefix=False)["SLxImageAttributes"]
        reader._attributes = load_attributes(cast("Any", {"uiSequenceCount": 0, **raw}), 1)
        seen["files nd2's frame reader is given the attributes of"] += 1


def file_place(d: Described, row: dict) -> tuple[int, int]:
    """(offset, length) in the file of a row's unit: its element's extent, a run's stride further
    for each member before its own, or its range of that."""
    start, length = d.ir.extent(row["element"])
    if row["member"] is not None:
        root = row["element"]
        while (run := d.ir._core.run(root)) is None:
            root = d.ir.parent(root)
        start += row["member"] * run[1]
    if row["range"] is not None:
        start, length = start + row["range"]["offset"], row["range"]["length"]
    return start, length


def decoded(d: Described, a: StoredArray, row: dict) -> bytes:
    """A row's unit, decoded through its array's chain."""
    start, length = file_place(d, row)
    unit = d.objects[None][start:start + length]
    for codec in reversed(d.stored.chains[a.chains[0]]):
        assert codec.name == "zlib", codec.name
        unit = zlib.decompress(unit)
    return unit


def lengths(a: StoredArray) -> tuple[int, ...] | None:
    """The lengths of the array's units, where every axis is cut evenly."""
    if a.grid is None or not all(isinstance(x, Each) for x in a.grid.axes):
        return None
    return tuple(x.length for x in a.grid.axes if isinstance(x, Each))


@dataclass(frozen=True)
class Frame:
    """A frame chunk of nd2's chunk map: its number, its chunk's name, where its header says its
    data start, the data nd2's chunk reader loads of it, and the samples nd2 reads of it."""

    number: int
    name: bytes
    start: int
    data: bytes
    samples: bytes


def frames_of(d: Described, reader: ModernReader, lay: Layout, seen: Counter) -> tuple[list[Frame], list[str]]:
    """The frames nd2's chunk map names, in order, each with the samples nd2 reads of it."""
    data = d.objects[None]
    named = ((FRAME.fullmatch(name), name, offset) for name, (offset, _) in reader.chunkmap.items())
    chunks = {int(m[1]): (name, offset) for m, name, offset in named if m}
    try:
        offsets: dict[int, int] | None = reader._frame_offsets
    except ValueError:
        seen["files whose frame names nd2 does not read"] += 1
        offsets = None
    out, said = [], []
    for f, (name, offset) in sorted(chunks.items()):
        if offset + 16 > len(data) or struct.unpack_from("<I", data, offset)[0] != 0x0ABECEDA:
            seen["frame chunks of no chunk header"] += 1
            out.append(Frame(f, name, offset, b"", b""))
            continue
        length, declared = struct.unpack_from("<IQ", data, offset + 4)
        start = offset + 16 + length
        loaded = reader._load_chunk(name)
        # nd2's frame reader maps the file at the frame's offset, which is its chunk's and 4096
        # bytes; of a compressed frame it loads the chunk
        placed = offsets is not None and (lay.compressed or length == NAME)
        if offsets is not None and length == NAME and offsets[f] != start + 8:
            said.append(f"frame {f}: nd2's frame offset {offsets[f]}, its header's {start + 8}")
        if not lay.held(declared, len(loaded)):
            seen["frame chunks that hold no frame"] += 1
            out.append(Frame(f, name, start, loaded, b""))
            continue
        if placed:
            seen["frames nd2's frame reader reads"] += 1
            read = reader.read_frame(f)
            if read.shape[:2] != (lay.height, lay.width) or read.size != lay.height * lay.width * lay.components:
                said.append(f"frame {f}: nd2 reads {read.shape}")
            if read.dtype.itemsize * 8 != lay.bits:
                said.append(f"frame {f}: nd2 reads {read.dtype}, the attributes say {lay.bits} bits")
            samples = read.astype(read.dtype.newbyteorder("<")).tobytes()
        else:
            seen["frames nd2's chunk reader alone reads"] += 1
            samples = lay.samples(loaded[8:])
        out.append(Frame(f, name, start, loaded, samples))
    return out, said


def check_frame(d: Described, k: int, rows: list[dict], frame: Frame, lay: Layout, element: str,
                seen: Counter) -> list[str]:
    """One frame's rows of array `k`, in order: a unit each, at its place after the frame's
    timestamp, of the element named `element`, decoding together to the samples nd2 reads."""
    a = d.stored.arrays[k]
    assert a.shape is not None
    if len(rows) != lay.units:
        return [f"frame {frame.number}: {len(rows)} rows, {lay.units} units"]
    said = []
    first = frame.start + 8
    for r, row in enumerate(rows):
        offset, length = lay.unit(r, len(frame.data) - 8)
        if row["coords"][-len(lay.shape):] != [r, *[0] * (len(lay.shape) - 1)]:
            said.append(f"frame {frame.number}: row {r} at {row['coords']}")
        if file_place(d, row) != (first + offset, length):
            said.append(f"frame {frame.number}: unit {r} at {file_place(d, row)}, nd2 {(first + offset, length)}")
        if d.ir.name(row["element"]) != element or (row["ranges"], row["chain"], row["skips"]) != (None, None, None):
            said.append(f"frame {frame.number}: unit {r} is of {d.ir.path(row['element'])}, or has ranges or a chain")
        # a unit that is all of its element names the element alone
        if (row["range"] is None) != ((first + offset, length) == file_place(d, {**row, "range": None})):
            said.append(f"frame {frame.number}: unit {r} has the range {row['range']}")
        seen["units in a run"] += row["member"] is not None
    if not said and b"".join(decoded(d, a, row) for row in rows) != frame.samples:
        said.append(f"frame {frame.number}: its units decode to other samples than nd2 reads")
    seen["frames compared"] += 1
    seen["frames of rows apart"] += lay.padded
    seen["frames of a zlib stream"] += lay.compressed
    seen["units"] += len(rows)
    return said


def check_description(a: StoredArray, lay: Layout, before: tuple[int, ...], held: bool) -> list[str]:
    """What every array of the file shares: its shape after the axes `before`, its sample size and,
    where it has a grid (`held`), the grid and the chain the attributes give."""
    said = []
    if (a.shape, a.item_bits) != ((*before, *lay.shape), lay.bits):
        said.append(f"shape {a.shape} of {a.item_bits} bits, nd2 {(*before, *lay.shape)} of {lay.bits}")
    if a.fill != "undefined" or a.axes is not None or a.pad_from is not None or a.unit_parameters:
        said.append("a fill value, axes, a padding or unit parameters")
    want = (*[1] * len(before), 1 if lay.padded else lay.height, *lay.shape[1:]) if held else None
    if lengths(a) != want or (a.grid is not None and a.grid.edge != "short"):
        said.append(f"grid {a.grid}, units of {want}")
    return said


def compare(path: Path, seen: Counter) -> list[str]:
    """What differs between the descriptions of the file at `path` and nd2's reading of it."""
    d = described(path)
    said: list[str] = []
    if d.stored.undescribed:
        return ["descriptions dropped for room"]
    seen["files"] += 1
    with nd2.ND2File(path) as f:
        reader = modern(f)
        lay = layout(reader)
        given_attributes(reader, seen)
        frames, said = frames_of(d, reader, lay, seen)
        loaded = [reader._load_chunk(name) for name in DECLARING if name in reader.chunkmap]
        key = reader.chunkmap[b"ImageAttributesLV!"][0]
        try:
            count: int | None = reader._seq_count()
        except KeyError:
            seen["files whose loops nd2 does not read"] += 1
            count = None
    arrays = d.stored.arrays
    image = arrays[0]
    assert image.shape is not None
    # the frames the loops count: nd2's count, or the count it is known to give
    n = image.shape[0]
    if path.name in COUNTED_OTHERWISE:
        seen["files whose loops nd2 counts otherwise"] += 1
        if (count, n) != (COUNTED_OTHERWISE[path.name], lay.sequence):
            said.append(f"{n} frames, of {lay.sequence}; nd2 counts {count} of loops it is known to count otherwise")
    elif count is not None:
        seen["files whose loops nd2 counts"] += 1
        if count != n:
            said.append(f"{n} frames, nd2 counts {count}")
    # the key and the declarations: the attributes' chunk, and the chunks' data
    if d.ir.extent(image.element)[0] != key or image.index is not None or image.path != "chunks/ImageAttributesLV!":
        said.append(f"keyed by {image.path} at {d.ir.extent(image.element)}, index {image.index}")
    for a in arrays:
        if [d.place(p) for p in a.declarations] != [b for b in loaded if b] or not all(
                isinstance(p, ElementPlace) and p.range is None for p in a.declarations):
            said.append(f"{a.path} {a.index}: the declarations are not the data of the chunks nd2 loads")
    codecs = [[(c.name, dict(c.parameters)) for c in chain] for chain in d.stored.chains]
    if codecs != [[("zlib", {})] if lay.compressed else []] or any(a.chains != (0,) for a in arrays if a.grid):
        said.append(f"chains {codecs}")
    seen[f"files of {lay.bits}-bit samples"] += 1
    seen["files of several components"] += lay.components > 1
    # the frames below the loops' count: a row for each unit of each the map names, and no other
    said += check_description(image, lay, (n,), held=True)
    by_frame: dict[int, list[dict]] = {}
    for row in d.rows.get(0, []):
        by_frame.setdefault(row["coords"][0], []).append(row)
    placed = [f for f in frames if f.number < n]
    seen["files with a frame the map does not name"] += len(placed) < n
    if sorted(by_frame) != [f.number for f in placed]:
        said.append(f"rows of the frames {sorted(by_frame)[:8]}, nd2's map names {[f.number for f in placed][:8]}")
    else:
        for frame in placed:
            said += check_frame(d, 0, by_frame[frame.number], frame, lay, "pixels", seen)
    # each frame chunk past them: an array of one frame, where its chunk holds one
    past = [f for f in frames if f.number >= n]
    if [(a.path, a.index) for a in arrays[1:]] != [("frames", i) for i in range(len(past))]:
        said.append(f"{len(arrays) - 1} arrays past the first, {len(past)} frame chunks past the loops' count")
        return said
    for k, frame in enumerate(past, 1):
        seen["frame chunks past the loops' count"] += 1
        held = bool(frame.samples)
        said += [f"frames {k - 1}: {s}" for s in check_description(arrays[k], lay, (), held)]
        if held:
            said += [f"frames {k - 1}: {s}" for s in check_frame(d, k, d.rows.get(k, []), frame, lay, "data", seen)]
        elif d.rows.get(k):
            said.append(f"frames {k - 1}: rows of a chunk that holds no frame")
    return said


@cache
def compared(path: Path) -> tuple[tuple[str, ...], Counter]:
    """What differs between the descriptions of the fixture at `path` and nd2's reading, and what
    the comparison saw."""
    seen: Counter = Counter()
    return tuple(compare(path, seen)), seen


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_nd2_frames_are_described_as_nd2_reads_them(path):
    """Every frame chunk of the file is described, at the place nd2 gives it, decoding to the
    samples nd2 reads."""
    said, seen = compared(path)
    assert said == ()
    assert seen["frames compared"] >= 1


def test_the_fixtures_hold_every_case_the_comparison_reads():
    """The fixtures reach each case of the comparison, so that none of it passes for want of a
    file, and the cases nd2 does not read are as many as here. The frames nd2's chunk reader alone
    reads are those of three files whose frame chunks' names are 24 and 32 bytes (2000 frames of
    one sample, and two frames), and the three of the file with a chunk named `ImageDataSeq|x!`.
    No fixture holds a frame chunk past the loops' count that holds no frame: the tests below
    write them."""
    seen: Counter = Counter()
    for path in FILES:
        seen += compared(path)[1]
    assert {k: v for k, v in seen.items() if v} == {
        "files": 33,
        "files nd2 loads the attributes of": 10,
        "files nd2's frame reader is given the attributes of": 23,
        "files whose loops nd2 counts": 24,
        "files whose loops nd2 does not read": 8,
        "files whose loops nd2 counts otherwise": 1,
        "files whose frame names nd2 does not read": 1,
        "files of 8-bit samples": 6,
        "files of 16-bit samples": 26,
        "files of 32-bit samples": 1,
        "files of several components": 4,
        "files with a frame the map does not name": 4,
        "frames compared": 2118,
        "frames nd2's frame reader reads": 113,
        "frames nd2's chunk reader alone reads": 2005,
        "frames of rows apart": 3,
        "frames of a zlib stream": 8,
        "frame chunks past the loops' count": 5,
        "units": 14123,
        "units in a run": 55,
    }


@cache
def writer() -> ModuleType:
    """The fixtures' generator, whose writer the tests of what no fixture holds use."""
    source = ROOT / "fixtures" / "generators" / "nd2" / "write_fixtures.py"
    spec = importlib.util.spec_from_file_location("nd2_fixtures", source)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MAGIC = 0x0ABECEDA
SIGNATURE = b"ND2 CHUNK MAP SIGNATURE 0000001!"
type Records = list[tuple[bytes, int, int]]
"""a chunk map's records, as the generator's writer keeps them: (name, offset, size)"""


def sample(shape: tuple[int, int, int], dtype: type, first: int) -> np.ndarray:
    """A frame of samples counting up from `first`."""
    return (np.arange(int(np.prod(shape))) + first).astype(dtype).reshape(shape)


def written(path: Path, attributes: bytes, frames: list[tuple[int, bytes]], count: int | None = None,
            name: int = NAME, records: Callable[[Records], None] | None = None) -> Path:
    """An ND2 file at `path` of the chunk `attributes`, a time loop of `count` (none: no loops) and
    the frame chunks `frames`, by number, their names `name` bytes long; `records` changes the
    chunk map's records before they are written."""
    w = writer()
    f = w.Nd2()
    f.chunk("ImageAttributesLV!", attributes)
    if count is not None:
        loop = {"uiCount": count, "dPeriod": 0.0, "dStart": 0.0, "dDuration": 0.0}
        f.chunk("ImageMetadataLV!", w.experiment(w.node(1, loop)))
    for number, data in frames:
        f.chunk(f"ImageDataSeq|{number}!", data, name)
    if records is not None:
        records(f.map)
    f.finish(path)
    return path


def offset_of(records: Records, name: bytes) -> int:
    return next(o for n, o, _ in records if n == name)


def row_at_the_end(path: Path) -> Path:
    """A file whose one frame is one padded row that ends the file: its chunk's header, name and
    timestamp lie in the chunk map's data after the map's last record, where nothing is read, and
    its row is the file's last 40 bytes, the padding after it in no byte of the file."""
    w = writer()
    f = w.Nd2()
    f.chunk("ImageAttributesLV!", w.attributes(40, 1, 1, 8, sequence=1, width_bytes=44))
    name, frame = b"ND2 FILEMAP SIGNATURE NAME 0001!", b"ImageDataSeq|0!"
    listed = b"".join(n + struct.pack("<QQ", o, s) for n, o, s in f.map) + frame
    at = len(f.buf)
    chunk = struct.pack("<IIQ", MAGIC, NAME, 8 + 44) + frame.ljust(NAME, b"\0") + struct.pack("<d", 1.5)
    # the frame's chunk starts after the map's records and the record that ends them
    start = at + 16 + len(name) + len(listed) + 16 + len(SIGNATURE) + 16
    body = listed + struct.pack("<QQ", start, 8 + 44) + SIGNATURE + bytes(16) + chunk
    head = struct.pack("<IIQ", MAGIC, len(name), len(body))
    path.write_bytes(bytes(f.buf) + head + name + body + SIGNATURE + struct.pack("<Q", at))
    return path


def test_frames_no_fixture_holds_are_described_as_nd2_reads_them(tmp_path):
    """Frames the fixtures do not hold, each compared with nd2 as the fixtures are: a stream and
    padded rows past the loops' count, which are ranges of their chunks' data; one padded row; two
    frames of one chunk, the second's elements aliases of the first's; a run of frames that the
    IR's finish unfolds, since another chunk claims a member's bytes; frame chunks whose names
    are not 4072 bytes, of rows end to end and of padded rows; and one padded row that is all of
    its element, the file's last bytes."""
    w = writer()
    seen: Counter = Counter()
    words = [sample((3, 4, 1), np.uint16, 100 * k) for k in range(3)]
    octets = [sample((3, 5, 1), np.uint8, 20 * k) for k in range(2)]
    plain = w.attributes(4, 3, 1, 16, sequence=3)
    streams = [w.frame_bytes(words[k], 8, True) for k in range(2)]
    compressed = written(tmp_path / "compressed.nd2", w.attributes(4, 3, 1, 16, sequence=2, compression=0),
                         list(enumerate(streams)))
    padded = written(tmp_path / "padded.nd2", w.attributes(5, 3, 1, 8, sequence=2, width_bytes=8),
                     [(k, w.frame_bytes(octets[k], 8, False)) for k in range(2)])
    one_row = written(tmp_path / "one_row.nd2", w.attributes(5, 1, 1, 8, sequence=1, width_bytes=8),
                      [(0, w.frame_bytes(octets[0][:1], 8, False))])
    shared = written(tmp_path / "shared.nd2", plain, [(0, w.frame_bytes(words[0], 8, False))], count=2,
                     records=lambda m: m.append((b"ImageDataSeq|1!", offset_of(m, b"ImageDataSeq|0!"), 32)))
    run = [(k, w.frame_bytes(words[k], 8, False)) for k in range(3)]
    folded = written(tmp_path / "folded.nd2", plain, run, count=3)
    unfolded = written(tmp_path / "unfolded.nd2", plain, run, count=3,
                       records=lambda m: m.append((b"Other!", offset_of(m, b"ImageDataSeq|1!"), 32)))
    short_names = written(tmp_path / "short_names.nd2", plain, run[:2], name=24)
    short_padded = written(tmp_path / "short_padded.nd2", w.attributes(5, 3, 1, 8, sequence=1, width_bytes=8),
                           [(0, w.frame_bytes(octets[1], 8, False))], name=24)
    last = row_at_the_end(tmp_path / "last.nd2")
    for path in (compressed, padded, one_row, shared, folded, unfolded, short_names, short_padded, last):
        assert compare(path, seen) == [], path.name
    # the frame past the count: a stream, and rows, that are ranges of the chunk's data
    assert [r["range"] for r in described(compressed).rows[1]] == [{"offset": 8, "length": len(streams[1]) - 8}]
    assert [r["range"] for r in described(padded).rows[1]] == [{"offset": 8 + 8 * r, "length": 5} for r in range(3)]
    assert [r["range"] for r in described(one_row).rows[0]] == [{"offset": 0, "length": 5}]
    # two frames of one chunk: one row names an alias, and both name the same bytes
    d = described(shared)
    assert sorted(d.ir.kind(r["element"]) for r in d.rows[0]) == [Kind.DATA, Kind.ALIAS]
    assert len({file_place(d, r) for r in d.rows[0]}) == 1
    # frames in a run are its members, and frames of an unfolded run elements of their own
    assert [r["member"] for r in described(folded).rows[0]] == [0, 1, 2]
    assert [r["member"] for r in described(unfolded).rows[0]] == [None, None, None]
    assert len({r["element"] for r in described(unfolded).rows[0]}) == 3
    # the row that is all of its element is the element alone
    assert [(r["range"], r["coords"]) for r in described(last).rows[0]] == [(None, [0, 0, 0])]
    assert {k: v for k, v in seen.items() if v} == {
        "files": 9,
        "files nd2's frame reader is given the attributes of": 9,
        "files whose loops nd2 counts": 9,
        "files of 8-bit samples": 4,
        "files of 16-bit samples": 5,
        "frames compared": 17,
        "frames nd2's frame reader reads": 14,
        "frames nd2's chunk reader alone reads": 3,
        "frames of rows apart": 5,
        "frames of a zlib stream": 2,
        "frame chunks past the loops' count": 3,
        "units": 23,
        "units in a run": 3,
    }


def read_by_nd2(path: Path, f: int) -> tuple[int, bytes]:
    """Where nd2's frame offsets place frame `f` of the file at `path`, and the samples its frame
    reader reads there."""
    with nd2.ND2File(path) as file:
        reader = modern(file)
        given_attributes(reader, Counter())
        read = reader.read_frame(f)
        return reader._frame_offsets[f], read.astype(read.dtype.newbyteorder("<")).tobytes()


def test_nd2s_frame_reader_reads_other_bytes_of_a_frame_whose_name_is_not_4072_bytes(tmp_path):
    """Where the readers differ, of a frame's place: a frame's pixels follow its chunk's name, of
    the length its chunk's header gives, and the description says so; nd2's frame reader takes
    the name to be 4072 bytes, and reads other bytes of a chunk with a name of 24."""
    w = writer()
    words = [sample((3, 4, 1), np.uint16, 100 * k) for k in range(2)]
    path = written(tmp_path / "short_names.nd2", w.attributes(4, 3, 1, 16, sequence=1),
                   [(k, w.frame_bytes(words[k], 8, False)) for k in range(2)], name=24)
    d = described(path)
    (row,) = d.rows[0]
    offset, samples = read_by_nd2(path, 0)
    start = file_place(d, row)[0]
    assert d.objects[None][start - 8 - 24:start - 8].rstrip(b"\0") == b"ImageDataSeq|0!"
    assert decoded(d, d.stored.arrays[0], row) == words[0].tobytes()
    assert (offset - start, samples == words[0].tobytes()) == (NAME - 24, False)


def test_nd2_reads_a_frame_named_with_leading_zeros_where_the_description_does_not(tmp_path):
    """Where the readers differ, of a frame's name: the convention's frame 0 is the chunk
    `ImageDataSeq|0!`, which the description's row names, and a chunk `ImageDataSeq|00!` is no
    frame; nd2 reads any digits as the number, and of the two chunks the second."""
    w = writer()
    words = [sample((3, 4, 1), np.uint16, 100 * k) for k in range(2)]
    f = w.Nd2()
    f.chunk("ImageAttributesLV!", w.attributes(4, 3, 1, 16, sequence=1))
    f.chunk("ImageDataSeq|0!", w.frame_bytes(words[0], 8, False))
    f.chunk("ImageDataSeq|00!", w.frame_bytes(words[1], 8, False))
    path = tmp_path / "zeros.nd2"
    f.finish(path)
    d = described(path)
    (row,) = d.rows[0]
    offset, samples = read_by_nd2(path, 0)
    assert len(d.stored.arrays) == 1
    assert decoded(d, d.stored.arrays[0], row) == words[0].tobytes()
    assert file_place(d, row)[0] == offset_of(f.map, b"ImageDataSeq|0!") + 16 + NAME + 8
    assert (offset, samples) == (offset_of(f.map, b"ImageDataSeq|00!") + 16 + NAME + 8, words[1].tobytes())


def no_grid(path: Path, k: int) -> StoredArray:
    """Array `k` of the file at `path`, which has no grid, no chain and no row."""
    d = described(path)
    a = d.stored.arrays[k]
    assert (a.grid, a.chains, d.rows.get(k)) == (None, (), None)
    return a


def test_a_frame_chunk_too_short_for_a_frame_has_no_grid(tmp_path):
    """A frame chunk past the loops' count whose header gives fewer bytes of data than a frame's
    rows take, each with its padding, holds no frame, as one below the count is rejected: one
    that lacks a sample, whose bytes nd2's frame reader would read after the chunk, and one that
    lacks only the last row's padding."""
    w = writer()
    words, octets = sample((3, 4, 1), np.uint16, 0), sample((3, 5, 1), np.uint8, 0)
    files = {
        "short.nd2": (w.attributes(4, 3, 1, 16, sequence=1), w.frame_bytes(words, 8, False), (3, 4), 16),
        "short_padding.nd2": (w.attributes(5, 3, 1, 8, sequence=1, width_bytes=8), w.frame_bytes(octets, 8, False),
                              (3, 5), 8),
    }
    seen: Counter = Counter()
    for name, (attributes, frame, shape, bits) in files.items():
        path = written(tmp_path / name, attributes, [(0, frame), (1, frame[:-1])])
        assert compare(path, seen) == [], name
        a = no_grid(path, 1)
        assert (a.path, a.index, a.shape, a.item_bits) == ("frames", 0, shape, bits)
    assert (seen["frame chunks that hold no frame"], seen["frames compared"]) == (2, 2)


def test_a_frame_chunk_the_files_end_cuts_has_no_grid(tmp_path):
    """A frame chunk past the loops' count whose rows, or whose stream, the file does not hold to
    their end holds no frame."""
    w = writer()
    words = sample((64, 64, 1), np.uint16, 0)
    seen: Counter = Counter()
    for compress in (False, True):
        whole = w.frame_bytes(words, 128, compress)
        f = w.Nd2()
        f.chunk("ImageAttributesLV!", w.attributes(64, 64, 1, 16, sequence=1, compression=0 if compress else 2))
        f.chunk("ImageDataSeq|0!", whole)
        f.chunk("ImageDataSeq|1!", whole)
        # the second frame's chunk keeps the length its header gives, and loses all but 20 bytes
        # of its data: the chunk map and the file's end follow, in fewer bytes than the frame's
        del f.buf[len(f.buf) - len(whole) + 20:]
        path = tmp_path / f"cut_{compress}.nd2"
        f.finish(path)
        assert compare(path, seen) == []
        a = no_grid(path, 1)
        assert (a.path, a.index, a.shape, a.item_bits) == ("frames", 0, (64, 64), 16)
    assert (seen["frame chunks that hold no frame"], seen["frames compared"]) == (2, 2)


def test_a_frame_chunk_of_no_chunk_header_has_no_grid(tmp_path):
    """A frame the map names past the loops' count where the file ends, or where no chunk
    starts, holds no frame."""
    w = writer()
    words = sample((3, 4, 1), np.uint16, 0)

    def records(m: Records) -> None:
        m.append((b"ImageDataSeq|1!", 1 << 40, 32))
        m.append((b"ImageDataSeq|2!", offset_of(m, b"ImageDataSeq|0!") + 16, 32))

    path = written(tmp_path / "headless.nd2", w.attributes(4, 3, 1, 16, sequence=1),
                   [(0, w.frame_bytes(words, 8, False))], records=records)
    seen: Counter = Counter()
    assert compare(path, seen) == []
    assert (seen["frame chunks of no chunk header"], seen["frames compared"]) == (2, 1)
    assert [(a.path, a.index, a.shape) for a in (no_grid(path, 1), no_grid(path, 2))] == [
        ("frames", 0, (3, 4)), ("frames", 1, (3, 4))]


def test_a_compressed_frame_chunk_of_no_stream_has_no_grid(tmp_path):
    """A frame chunk past the loops' count of compressed frames whose data is its timestamp alone
    holds no frame."""
    w = writer()
    words = sample((3, 4, 1), np.uint16, 0)
    path = written(tmp_path / "streamless.nd2", w.attributes(4, 3, 1, 16, sequence=1, compression=0),
                   [(0, w.frame_bytes(words, 8, True)), (1, w.frame_bytes(words, 8, True)[:8])])
    seen: Counter = Counter()
    assert compare(path, seen) == []
    assert (seen["frame chunks that hold no frame"], seen["frames compared"]) == (1, 1)
    assert no_grid(path, 1).shape == (3, 4)


def test_frames_whose_rows_pass_the_chunk_maps_limit_have_no_grid(tmp_path):
    """Frames of padded rows that share one chunk have more rows than the file's size allows the
    chunk map: their array falls back, with no grid, chain or row
    (spec/stored-array.md §6 <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#6-in-the-ir>),
    and the rows are never made. With fewer frames of the chunk the rows fit."""
    w = writer()
    column = sample((12000, 1, 1), np.uint8, 0)
    paths = {}
    for count in (2, 120):
        def records(m: Records, count: int = count) -> None:
            m.extend((f"ImageDataSeq|{k}!".encode(), offset_of(m, b"ImageDataSeq|0!"), 32) for k in range(1, count))

        attributes = w.attributes(1, 12000, 1, 8, sequence=count, width_bytes=4)
        paths[count] = written(tmp_path / f"shared_{count}.nd2", attributes, [(0, w.frame_bytes(column, 4, False))],
                               count=count, records=records)
    seen: Counter = Counter()
    assert compare(paths[2], seen) == []
    assert seen["units"] == 2 * 12000
    d = described(paths[120])
    a = no_grid(paths[120], 0)
    assert (a.path, a.shape, a.item_bits, len(a.declarations)) == ("chunks/ImageAttributesLV!", (120, 12000, 1), 8, 1)
    assert (d.stored.chains, d.rows, d.stored.undescribed) == ((), {}, False)
