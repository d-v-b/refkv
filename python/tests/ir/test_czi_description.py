"""The description of every CZI fixture (spec/zarrify/czi.md §7.10
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#710-the-description>), compared with
czifile and pylibCZIrw, the reference readers: the file header; each segment the description names,
by czifile's reading of its header, and every segment czifile's walk visits; the subblock
directory's entries with their dimension entries; of each entry's subblock its sizes and header
length, whether its copy of its entry is the entry, and the size of the image its stream holds,
which czifile decodes (or, for a JPEG stream, whose frame header gives it); the metadata segment's
sizes, and the values the profile reads of its XML, by the paths of spec/zarrify/czi.md §2.7
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#27-the-metadata-xml> over
pylibCZIrw's parse of the XML; and the attachment directory's entries. A second test asserts which
cases the fixtures reached, and a third that the description holds none of the output's decisions.

The description departs from the readers in these ways, each counted where it is met:

- a GUID is written as Windows and libCZI write one (its first three groups read little-endian),
  where czifile reads its 16 bytes in order;
- the profile reads a segment of an id of capitals, digits and underscores (`UNKNOWN_ID`), and
  walks past it, where czifile reads ids of capitals and digits alone and stops there;
- a segment's UsedSize is the field's value, where czifile reads a UsedSize of 0 as the
  AllocatedSize;
- the profile reads no value of a metadata XML that is not UTF-8, or whose root is not
  `ImageDocument`, where pylibCZIrw parses it; and an XML pylibCZIrw does not parse (with end tags
  that close no element), which the profile's tag scan reads, is not compared;
- a run of an image across files (a file whose entries name other parts) has no description yet."""

import json
import logging
import re
import struct
import uuid
from collections import Counter
from functools import cache
from pathlib import Path
from typing import Any

import czifile
import imagecodecs
import jsonschema
import numpy as np
import pytest
from ir_helpers import ROOT
from pylibCZIrw import czi as pyczi
from test_czi_stored import FILES, jpeg_frame, writer, written, zstd1_header

from refkv.build import read

SCHEMA = json.loads((ROOT / "spec" / "zarrify" / "czi" / "description.json").read_text())
JPEG, LJPEG, JPEGXR, ZSTD1 = 1, 3, 4, 6
# the dimensions whose `Size<letter>` the profile reads (spec/zarrify/czi.md §2.7
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#27-the-metadata-xml>)
SIZES = "CZTSBHIRVM"
DECIMAL = re.compile(r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?")
COLOR = re.compile(r"#(?:[0-9A-Fa-f]{2})?([0-9A-Fa-f]{6})")
# members a description never has: the output's decisions (spec/zarrify/czi.md §7.10
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#710-the-description>)
DECISIONS = {"images", "tiles", "levels", "axes", "scales", "translations", "units", "dtype", "chunks", "band",
             "cells", "declared_by", "omero", "root", "subblocks", "series", "layers", "placed", "form", "name"}


def windows_guid(g: uuid.UUID) -> str:
    """A GUID czifile read, as Windows writes its 16 bytes."""
    return str(uuid.UUID(bytes_le=g.bytes))


def first(v: Any) -> Any:
    """xmltodict's first child of a name: a list of them, or one."""
    return v[0] if isinstance(v, list) else v


def at(tree: Any, *path: str) -> Any:
    """The element at `path`, each step its parent's first child of the name; None where absent."""
    for name in path:
        if not isinstance(tree, dict):
            return None
        tree = first(tree.get(name))
    return tree


def every(tree: Any, *path: str) -> list[Any]:
    """Every child of the last name of `path` of the element at the rest of it."""
    parent = at(tree, *path[:-1])
    v = parent.get(path[-1]) if isinstance(parent, dict) else None
    return [] if v is None else v if isinstance(v, list) else [v]


def text(e: Any) -> str | None:
    if e is None:
        return ""
    if isinstance(e, str):
        return e.strip(" \t\r\n")
    t = e.get("#text") if isinstance(e, dict) else None
    return (t or "").strip(" \t\r\n")


def attribute(e: Any, name: str) -> str | None:
    return e.get(f"@{name}") if isinstance(e, dict) else None


def decimal(t: str | None) -> float | None:
    if t is None or not DECIMAL.fullmatch(t):
        return None
    v = float(t)
    return v if np.isfinite(v) else None


def positive(t: str | None) -> float | None:
    v = decimal(t)
    return v if v is not None and v > 0 else None


def integer(t: str | None) -> int | None:
    if t is None or not t.isascii() or not t.isdigit() or len(t.lstrip("0")) > 16 or int(t) >= 1 << 53:
        return None
    return int(t)


def color(t: str | None) -> str | None:
    m = COLOR.fullmatch(t or "")
    return None if m is None else m[1].upper()


def xml_values(tree: dict) -> dict:
    """The values of spec/zarrify/czi.md §2.7
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#27-the-metadata-xml>, by their
    paths over pylibCZIrw's parse of the XML."""
    root = tree.get("ImageDocument")
    image = ("Metadata", "Information", "Image")
    distances = every(root, "Metadata", "Scaling", "Items", "Distance")

    def scaled(axis: str) -> float | None:
        d = next((d for d in distances if attribute(d, "Id") == axis), None)
        return None if d is None or at(d, "Value") is None else positive(text(at(d, "Value")))

    def channel(c: Any, display: bool) -> dict:
        def field(name: str) -> str | None:
            return None if at(c, name) is None and not (isinstance(c, dict) and name in c) else text(at(c, name))

        return {"name": attribute(c, "Name"), "color": color(field("Color")),
                "bits": None if display else integer(field("ComponentBitCount")),
                "low": decimal(field("Low")) if display else None,
                "high": decimal(field("High")) if display else None}

    scenes: dict[int, str | None] = {}
    for s in every(root, *image, "Dimensions", "S", "Scenes", "Scene"):
        index = integer(attribute(s, "Index"))
        if index is not None and index not in scenes:
            scenes[index] = attribute(s, "Name")
    sizes = {}
    for letter in SIZES:
        e = at(root, *image, f"Size{letter}")
        if e is not None and integer(text(e)) is not None:
            sizes[letter] = integer(text(e))
    increment = at(root, *image, "Dimensions", "T", "Positions", "Interval", "Increment")
    bits = at(root, *image, "ComponentBitCount")
    document = ("Metadata", "Information", "Document")
    return {
        "px": scaled("X"), "py": scaled("Y"), "pz": scaled("Z"),
        "inc": None if increment is None else positive(text(increment)),
        "bits": None if bits is None else integer(text(bits)),
        "info": [channel(c, False) for c in every(root, *image, "Dimensions", "Channels", "Channel")],
        "display": [channel(c, True) for c in every(root, "Metadata", "DisplaySetting", "Channels", "Channel")],
        "scenes": [{"index": i, "name": n} for i, n in scenes.items()],
        "sizes": sizes,
        "document": {"name": None if at(root, *document, "Name") is None and "Name" not in (at(root, *document) or {})
                     else text(at(root, *document, "Name")),
                     "creation_date": None if "CreationDate" not in (at(root, *document) or {})
                     else text(at(root, *document, "CreationDate"))},
    }


EMPTY = {"px": None, "py": None, "pz": None, "inc": None, "bits": None, "info": [], "display": [], "scenes": [],
         "sizes": {}, "document": {"name": None, "creation_date": None}}


def czifile_walk(c: czifile.CziFile, size: int) -> list[int]:
    """The offsets of the segments czifile's walk visits, from offset 0, to the first header it
    cannot read or whose segment passes the file's end."""
    found, offset = [], 0
    while offset + 32 <= size:
        try:
            s = czifile.CziSegment(c, offset)
        except (czifile.CziSegmentNotFoundError, ValueError):
            break
        if s.allocated_size < 0 or offset + 32 + s.allocated_size > size:
            break
        found.append(offset)
        offset += 32 + s.allocated_size
    return found


def check_segments(d: dict, c: czifile.CziFile, data: bytes, said: list[str], seen: Counter) -> None:
    named = {s["offset"]: s for s in d["segments"]}
    for s in d["segments"]:
        seen["segments"] += 1
        seen["segments the walk found"] += not s["referenced"]
        seen["deleted segments"] += s["id"] == "DELETED"
        try:
            theirs = czifile.CziSegment(c, s["offset"])
        except ValueError:
            # an id of characters other than capitals and digits, such as `UNKNOWN_ID`
            seen["segment ids czifile does not read"] += 1
            continue
        used = s["used_size"] or s["allocated_size"]
        if (s["id"], s["allocated_size"], used) != (str(theirs.sid), theirs.allocated_size, theirs.used_size):
            said.append(f"segment at {s['offset']}: {s['id']} {s['allocated_size']} {used}, czifile's "
                        f"{theirs.sid} {theirs.allocated_size} {theirs.used_size}")
        seen["used sizes of 0"] += s["used_size"] == 0
    missing = [o for o in czifile_walk(c, len(data)) if o not in named]
    said += [f"czifile's walk visits a segment at {o}, which the description does not name" for o in missing]


def check_entry(e: dict, theirs: czifile.CziDirectoryEntryDV, c: czifile.CziFile, said: list[str],
                seen: Counter) -> None:
    seen["entries"] += 1
    mine = (e["schema"], e["pixel_type"], e["file_position"], e["file_part"], e["compression"], e["pyramid_type"])
    them = ("DV", int(theirs.pixel_type), theirs.file_position, theirs.file_part, int(theirs.compression),
            int(theirs.pyramid_type))
    if mine != them:
        said.append(f"entry at {theirs.offset}: {mine}, czifile's {them}")
    seen["pyramid types"] += e["pyramid_type"] != 0
    dims = theirs.read_dimension_entries(c)
    if len(e["dimensions"]) != theirs.dimensions_count:
        said.append(f"entry at {theirs.offset}: {len(e['dimensions'])} dimension entries")
    for m in e["dimensions"]:
        seen["dimension entries"] += 1
        t = dims.get(m["dimension"])
        if t is None or (m["start"], m["size"], m["start_coordinate"], m["stored_size"]) != (
                t.start, t.size, t.start_coordinate, t.stored_size):
            said.append(f"entry at {theirs.offset}: dimension {m['dimension']} differs")


def check_stream(s: dict, e: dict, segment: czifile.CziSubBlockSegmentData, data: bytes, said: list[str],
                 seen: Counter) -> None:
    """The image a subblock's stream holds: the size czifile decodes, and the frame header of a JPEG
    stream; a Zstd1 stream's header."""
    stream = s["stream"]
    if stream is None:
        seen["streams that are not their entry's"] += 1
        return
    head = data[segment.data_offset:segment.data_offset + min(segment.data_size, 1 << 16)]
    entry = segment.directory_entry
    if entry.compression == czifile.CziCompressionType.JPEGXR:
        entry.decode = imagecodecs.jpegxr_decode
    try:
        a = np.asarray(segment.data())
    except Exception:  # noqa: BLE001  (whatever a decoder raises: czifile reads no size to compare)
        a = None
    if a is not None:
        seen["streams czifile decodes"] += 1
        size = (a.shape[entry.dims.index("X")], a.shape[entry.dims.index("Y")])
        if (stream["width"], stream["height"]) != size:
            said.append(f"subblock at {e['file_position']}: stream of {stream['width']}x{stream['height']}, "
                        f"czifile's {size[0]}x{size[1]}")
    if e["compression"] in (JPEG, LJPEG):
        frame = jpeg_frame(head)
        seen["JPEG frame headers"] += 1
        if frame is None or (frame.width, frame.height) != (stream["width"], stream["height"]):
            said.append(f"subblock at {e['file_position']}: JPEG frame header differs")
        if e["compression"] == JPEG and (frame is not None and frame.marker == 0xC3) != stream["lossless"]:
            said.append(f"subblock at {e['file_position']}: lossless {stream['lossless']}")
        seen["lossless JPEG streams under JpgFile"] += e["compression"] == JPEG and stream["lossless"]
    if e["compression"] == ZSTD1:
        seen["Zstd1 headers"] += 1
        seen["hi-lo packing"] += stream["hilo"]
        if zstd1_header(head) != (stream["header_size"], stream["hilo"]):
            said.append(f"subblock at {e['file_position']}: Zstd1 header {zstd1_header(head)}")


def check_subblocks(d: dict, c: czifile.CziFile, data: bytes, said: list[str], seen: Counter) -> None:
    elements: dict[int, int] = {}
    for k, e in enumerate(d["entries"]):
        s = e["subblock"]
        if s is None:
            said.append(f"entry {k}: no subblock")
            continue
        pos = e["file_position"]
        segment = czifile.CziSegment(c, pos).data()
        assert isinstance(segment, czifile.CziSubBlockSegmentData)
        seen["subblocks"] += 1
        length = segment.data_offset - (pos + 32) - segment.metadata_size
        mine = (s["metadata_size"], s["attachment_size"], s["data_size"], s["header_length"])
        them = (segment.metadata_size, segment.attachment_size, segment.data_size, length)
        if mine != them:
            said.append(f"subblock at {pos}: {mine}, czifile's {them}")
        # an entry that names the segment an earlier one read compares no copy
        again = pos in elements
        seen["entries of a subblock an earlier one names"] += again
        if elements.setdefault(pos, s["element"]) != s["element"] or (s["agrees"] is None) != again:
            said.append(f"subblock at {pos}: element {s['element']}, agrees {s['agrees']}")
        if again:
            continue
        entry = c.subblock_directory[k]
        copy = segment.directory_entry
        mine = data[entry.offset:entry.offset + entry.storage_size]
        agrees = mine == data[copy.offset:copy.offset + copy.storage_size]
        seen["copies that differ"] += not agrees
        if s["agrees"] != agrees:
            said.append(f"subblock at {pos}: agrees {s['agrees']}")
        if agrees:
            check_stream(s, e, segment, data, said, seen)
        if (s["data"] is None) != (s["stream"] is None or not agrees):
            said.append(f"subblock at {pos}: data {s['data']}")


def check_metadata(d: dict, c: czifile.CziFile, path: Path, data: bytes, said: list[str], seen: Counter) -> None:
    m = d["metadata"]
    position = c.header.metadata_position
    if (m is None) != (position == 0):
        said.append(f"metadata {m}")
    if m is None:
        return
    seen["metadata"] += 1
    theirs = c.metadata_segment
    assert theirs is not None
    if (m["position"], m["xml_size"], m["attachment_size"]) != (position, theirs.xml_size, theirs.attachment_size):
        said.append("metadata segment differs")
    xml = data[position + 288:position + 288 + m["xml_size"]].removeprefix(b"\xef\xbb\xbf")
    try:
        xml.decode()
    except UnicodeDecodeError:
        seen["XML that is not UTF-8"] += 1
        if m["values"] != EMPTY:
            said.append("values of an XML that is not UTF-8")
        return
    try:
        with pyczi.open_czi(str(path)) as r:
            tree = r.metadata
    except Exception:  # noqa: BLE001  (an XML pylibCZIrw does not parse: the profile's scan still reads it)
        seen["XML pylibCZIrw does not parse"] += 1
        return
    if "ImageDocument" not in tree:
        seen["XML of another root"] += 1
        if m["values"] != EMPTY:
            said.append("values of an XML of another root")
        return
    want = xml_values(tree)
    for k, v in m["values"].items():
        if v != want[k]:
            said.append(f"XML value {k}: {v}, pylibCZIrw's {want[k]}")
        seen[f"XML {k}"] += v not in (None, [], {}, {"name": None, "creation_date": None})


def check_attachments(d: dict, c: czifile.CziFile, data: bytes, said: list[str], seen: Counter) -> None:
    a = d["attachments"]
    position = c.header.attachment_directory_position
    if (a is None) != (position == 0):
        said.append(f"attachments {a}")
    if a is None:
        return
    count = struct.unpack_from("<i", data, position + 32)[0]
    if (a["position"], a["entry_count"], len(a["entries"])) != (position, count, count):
        said.append("attachment directory differs")
    for k, e in enumerate(a["entries"]):
        at_ = position + 288 + 128 * k
        try:
            t = czifile.CziAttachmentEntryA1(data[at_:at_ + 128], at_)
        except czifile.CziFileError:
            seen["attachment entries of another schema"] += 1
            if e["schema"] == "A1":
                said.append(f"attachment {k}: czifile reads no A1 entry")
            continue
        seen["attachment entries"] += 1
        mine = (e["schema"], e["file_position"], e["file_part"], e["content_guid"], e["content_file_type"], e["name"])
        them = ("A1", t.file_position, t.file_part, windows_guid(t.content_guid), str(t.content_file_type), t.name)
        if mine != them:
            said.append(f"attachment {k}: {mine}, czifile's {them}")


def check_file(path: Path, d: dict, said: list[str], seen: Counter) -> None:
    data = path.read_bytes()
    seen["files"] += 1
    with czifile.CziFile(path) as c:
        h = c.header
        mine = d["file_header"]
        them = {"major": h.version[0], "minor": h.version[1], "primary_file_guid": windows_guid(h.primary_file_guid),
                "file_guid": windows_guid(h.file_guid), "file_part": h.file_part,
                "directory_position": h.directory_position, "metadata_position": h.metadata_position,
                "update_pending": int(h.update_pending),
                "attachment_directory_position": h.attachment_directory_position}
        if d["size"] != len(data) or mine != them:
            said.append(f"file header {mine}, czifile's {them}")
        check_segments(d, c, data, said, seen)
        directory = c.subblock_directory
        if d["directory"] != {"position": h.directory_position, "entry_count": len(directory)}:
            said.append(f"directory {d['directory']}")
        if len(d["entries"]) != len(directory):
            said.append(f"{len(d['entries'])} entries, czifile's {len(directory)}")
        for e, theirs in zip(d["entries"], directory, strict=False):
            check_entry(e, theirs, c, said, seen)
        check_subblocks(d, c, data, said, seen)
        check_metadata(d, c, path, data, said, seen)
        check_attachments(d, c, data, said, seen)


@cache
def compared(path: Path) -> tuple[tuple[str, ...], Counter]:
    logging.getLogger("czifile").setLevel(logging.CRITICAL)
    said: list[str] = []
    seen: Counter = Counter()
    with read(path) as parsed:
        d = parsed.description
        plain = parsed.plain_file
    if not plain:
        # an image across files: its store run has no description yet
        seen["images across files"] += 1
        if d is not None:
            said.append("a description of an image across files")
        return tuple(said), seen
    assert d is not None
    errors = sorted(e.message[:200] for e in jsonschema.Draft202012Validator(SCHEMA).iter_errors(d))
    said += [f"not of the schema: {e}" for e in errors]
    check_file(path, d, said, seen)
    return tuple(said), seen


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_a_czis_description_is_what_czifile_and_pylibczirw_read(path):
    said, _ = compared(path)
    assert said == ()


def test_the_fixtures_reach_every_case_the_comparison_reads():
    seen: Counter = Counter()
    for path in FILES:
        seen += compared(path)[1]
    wanted = ["files", "images across files", "segments", "segments the walk found", "deleted segments",
              "used sizes of 0", "segment ids czifile does not read", "entries", "dimension entries", "pyramid types",
              "subblocks", "copies that differ", "streams that are not their entry's", "streams czifile decodes",
              "JPEG frame headers", "lossless JPEG streams under JpgFile", "Zstd1 headers", "hi-lo packing",
              "metadata", "XML that is not UTF-8", "XML pylibCZIrw does not parse", "XML px", "XML pz", "XML inc",
              "XML bits", "XML info", "XML display", "XML scenes", "attachment entries",
              "attachment entries of another schema"]
    assert [w for w in wanted if not seen[w]] == []


def test_cases_the_fixtures_lack_are_described_as_the_readers_read_them(tmp_path):
    """A subblock that two entries of one directory name, and an XML with `Size<letter>` and
    `Document` values, which no file input among the fixtures holds."""
    plane = writer.plane(0, 0, 0, 0, 6, 4, C=0)
    once = writer.Czi([plane])
    once.build()
    again = writer.dv_entry(0, 0, once.offsets["subblock0"], once.subblocks[0].dims)
    czi = writer.Czi([plane], writer.xml(sizes={"C": 1, "T": 1}, document=("A document", "2026-11-21T00:00:00")),
                     more_entries=[again])
    said, seen = compared(written(tmp_path, "twice", czi.build()))
    assert said == ()
    assert [w for w in ("entries of a subblock an earlier one names", "XML sizes", "XML document") if not seen[w]] == []


@pytest.mark.parametrize("path", FILES[::5], ids=[p.name for p in FILES[::5]])
def test_a_description_holds_none_of_the_outputs_decisions(path):
    with read(path) as parsed:
        d = parsed.description
    if d is None:
        return
    assert not DECISIONS & d.keys()
    assert not any(DECISIONS & e.keys() for e in d["entries"])

