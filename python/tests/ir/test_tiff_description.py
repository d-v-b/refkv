"""The description of every TIFF, QPTIFF and NDPI fixture (spec/zarrify/tiff.md §7.6
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#76-the-description>, spec/zarrify/ndpi.md
§7.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#71-the-description>), compared with
tifffile and imagecodecs, the reference readers: the header; the main chain and each IFD's SubIFDs by
their offsets; every tag of the profile's table each IFD has, its field type, count and the values the
profile reads; the OME-XML, ImageJ, Micro-Manager, Aperio and QPI structures as tifffile (or, for QPI,
ElementTree) reads them; each short last LZW strip's tail, decoded with the strip's kept bytes, as the
strip's rows followed by zero rows; and, of an NDPI, its tile tables and McuStarts with their high
bytes, the headers of its streams, its focal offsets and acquisition tags. A second test asserts which
cases the fixtures reached, and a third that the description holds none of the output's decisions."""

import base64
import json
import logging
import re
import struct
import xml.etree.ElementTree as ET
from collections import Counter
from functools import cache
from pathlib import Path
from typing import Any
from unittest import mock

import imagecodecs
import jsonschema
import numpy as np
import pytest
import tifffile
from ir_helpers import FIXTURES, ROOT

from refkv.build import read

# files the profiles reject, whose names do not say so
REJECTED = {"mm_split_MMStack_Pos0_1.ome.tif", "unsupported_predictor_2_float.tif"}
FILES = sorted(
    p for d in ("tiff", "qptiff", "ndpi") for p in (FIXTURES / d).iterdir()
    if p.is_file() and "reject" not in p.name and p.name not in REJECTED
)
# the tags of TIFF's and QPTIFF's tables (spec/zarrify/tiff.md §2
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#2-the-source>), but SubIFDs, whose IFDs
# `subifds` holds
TABLE = {256, 257, 258, 259, 262, 266, 270, 273, 277, 278, 279, 282, 283, 284, 296, 317, 322, 323, 324, 325, 339,
         347, 530}
QPTIFF_TABLE = TABLE | {286, 287}
# the tags whose first value is all the profile reads
SCALARS = {256, 257, 259, 262, 266, 277, 278, 282, 283, 284, 286, 287, 296, 317, 322, 323}
NDPI_SCALARS = {256, 257, 259, 262, 273, 277, 279, 282, 283, 296, 322, 323, 65420, 65421, 65422, 65423}
RATIONALS = {282, 283, 286, 287}
# NDPI's high tables, OffsetHighBytes, ByteCountHighBytes and McuStartsHighBytes (spec/zarrify/ndpi.md §7
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#7-ndpi-profile>)
HIGH_TABLES = {65324, 65325, 65432}
SCHEMAS = {p: json.loads((ROOT / "spec" / "zarrify" / d / "description.json").read_text())
           for p, d in (("tiff", "tiff"), ("qptiff", "tiff"), ("ndpi", "ndpi"))}
# members a description never has: the output's decisions (spec/zarrify/tiff.md §7.6
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#76-the-description>)
DECISIONS = {"axes", "levels", "planes", "scales", "translation", "units", "sizes", "data_type", "codecs", "omero",
             "join", "shard", "pad", "rectilinear", "plane_c", "own"}


def tag_values(tag: tifffile.TiffTag) -> list[Any]:
    v = tag.value
    if isinstance(v, (tuple, list)):
        return list(v)
    if isinstance(v, np.ndarray):
        return v.ravel().tolist()
    return [v]


def _utf8(path: str, tag: tifffile.TiffTag) -> bool:
    with Path(path).open("rb") as f:
        f.seek(tag.valueoffset)
        b = f.read(tag.count).split(b"\0")[0]
    try:
        b.decode()
    except UnicodeDecodeError:
        return False
    return True


def local(name: str) -> str:
    return name.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def page_at(tif: tifffile.TiffFile, offset: int, ndpi: bool) -> tifffile.TiffPage:
    tif.filehandle.seek(offset)
    if not ndpi:
        return tifffile.TiffPage(tif, 0)
    # tifffile knows an NDPI page by its Make field, which the fixtures lack
    with mock.patch.object(tifffile.TiffPage, "is_ndpi", new=property(lambda _: True)):
        return tifffile.TiffPage(tif, 0)


def check_tags(e: dict, page: tifffile.TiffPage, data: bytes, table: set[int], scalars: set[int],
               said: list[str], seen: Counter) -> None:
    """The IFD's tags in the description against tifffile's."""
    tags = {int(k): v for k, v in e["tags"].items()}
    # an ImageDescription the profile reads only when ASCII
    missing = [t for t in table
               if t in page.tags and t not in tags and not (t == 270 and int(page.tags[t].dtype) != 2)]
    said += [f"IFD at {page.offset}: tag {t} left out" for t in missing]
    for code, d in tags.items():
        t = page.tags.get(code)
        if t is None:
            said.append(f"IFD at {page.offset}: tag {code}, which tifffile does not read")
            continue
        if (d["type"], d["count"]) != (int(t.dtype), t.count):
            said.append(f"IFD at {page.offset}: tag {code} of type {d['type']} and count {d['count']}, "
                        f"tifffile's {int(t.dtype)} and {t.count}")
        v = d["value"]
        if code == 270:
            seen["image descriptions"] += 1
            # the ASCII value as the file holds it: tifffile's `valueoffset` is the value field's
            # place for an in-line value
            text = data[t.valueoffset:t.valueoffset + t.count].split(b"\0")[0]
            mine = v.encode() if isinstance(v, str) else v["latin1"].encode("latin-1")
            if mine != text:
                said.append(f"IFD at {page.offset}: ImageDescription differs")
            continue
        if code == 347:
            seen["JPEG tables"] += 1
            if base64.b64decode(v) != bytes(t.value):
                said.append(f"IFD at {page.offset}: JPEGTables differ")
            continue
        theirs = tag_values(t)
        if code in RATIONALS or int(t.dtype) in (5, 10):
            pairs = [theirs[i:i + 2] for i in range(0, len(theirs), 2)] if not isinstance(theirs[0], tuple) else theirs
            theirs = [list(p) for p in pairs]
        want: list[Any] = theirs[:1] if code in scalars else theirs
        if code in HIGH_TABLES:
            # the 32-bit values a high table stores: tifffile adds an in-line one's high word
            want = [x & 0xFFFFFFFF for x in want]
        if v != want:
            said.append(f"IFD at {page.offset}: tag {code} is {v[:8]}, tifffile's {want[:8]}")
        seen["tags"] += 1


def check_lzw_tails(d: dict, data: bytes, said: list[str], seen: Counter) -> None:
    """Each short last LZW strip's kept bytes and tail decode to the strip's rows and then zero rows,
    to RowsPerStrip rows."""
    for t in d["lzw_tails"]:
        e = d["ifds"][t["ifd"]]
        tags = {int(k): v["value"] for k, v in e["tags"].items()}
        w, h, rows = tags[256][0], tags[257][0], min(tags.get(278, [2 ** 32 - 1])[0], tags[257][0])
        bits, spp = tags.get(258, [1])[0], tags.get(277, [1])[0]
        contig = spp > 1 and tags.get(284, [1])[0] == 1
        row = -(-w * bits * (spp if contig else 1) // 8)
        short = h - rows * (-(-h // rows) - 1)
        strip = data[t["offset"]:t["offset"] + tags[279][t["strip"]]]
        if "problem" in t:
            said.append(f"strip {t['strip']}: {t['problem']}")
            continue
        seen["lzw tails"] += 1
        made = bytes(imagecodecs.lzw_decode(strip[:t["prefix"]] + base64.b64decode(t["tail"])))
        want = bytes(imagecodecs.lzw_decode(strip))[:short * row] + bytes((rows - short) * row)
        if made != want:
            said.append(f"strip {t['strip']} of IFD {t['ifd']}: its tail does not decode to zero rows")


def check_ome(d: dict, tif: tifffile.TiffFile, said: list[str], seen: Counter) -> None:
    text = tif.pages.first.description if tif.pages.first.tags.get(270) else ""
    o = d["ome"]
    if o is None:
        # OME-XML is read only from an ImageDescription that is UTF-8 (spec/zarrify/tiff.md §3
        # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#3-ome-xml>)
        t = tif.pages.first.tags.get(270)
        utf8 = t is not None and _utf8(tif.filehandle.path, t)
        if tif.is_ome and "<OME" in text and utf8:
            said.append("no OME-XML, which tifffile reads")
        return
    seen["ome"] += 1
    try:
        root = ET.fromstring(re.sub(r"^<\?xml[^>]*\?>", "", tif.ome_metadata or text))  # noqa: S314 (a fixture's)
    except ET.ParseError:
        seen["ome tifffile cannot parse"] += 1
        return
    images = [x for x in root.iter() if local(x.tag) == "Image"]
    pixels = next(x for x in root.iter() if local(x.tag) == "Pixels")
    if o["name"] != (images[0].get("Name") if images else None):
        said.append(f"OME image name {o['name']!r}")
    if o["uuid"] != root.get("UUID"):
        said.append(f"OME UUID {o['uuid']!r}")
    if o["pixels"] != dict(pixels.attrib):
        said.append(f"OME Pixels {o['pixels']}, ElementTree's {dict(pixels.attrib)}")
    tiff_data = [x for x in pixels if local(x.tag) == "TiffData"]
    if [t["attributes"] for t in o["tiff_data"]] != [dict(x.attrib) for x in tiff_data]:
        said.append("OME TiffData differ")
    for mine, x in zip(o["tiff_data"], tiff_data, strict=False):
        uuid = next((u for u in x if local(u.tag) == "UUID"), None)
        file = None if uuid is None else uuid.get("FileName", (uuid.text or "").strip())
        if mine["file"] != file:
            said.append(f"OME TiffData file {mine['file']!r}, ElementTree's {file!r}")
            seen["ome files"] += 1
    channels = [dict(x.attrib) for x in pixels if local(x.tag) == "Channel"][:64]
    if o["channels"] != channels:
        said.append("OME Channels differ")
    planes = [x for x in pixels if local(x.tag) == "Plane"]
    at_zero = [p for p in planes if all(p.get(k, "0").strip() == "0" for k in ("TheZ", "TheC", "TheT"))]
    zero = dict(at_zero[0].attrib) if at_zero else None
    if o["plane"] != zero and o["plane"] is not None:
        said.append("OME Plane at (0, 0, 0) differs")


def check_structures(d: dict, tif: tifffile.TiffFile, said: list[str], seen: Counter) -> None:
    check_ome(d, tif, said, seen)
    ij = d["imagej"]
    if ij is not None:
        seen["imagej"] += 1
        meta = tif.imagej_metadata or {}
        theirs = {k: meta.get(k, 1) for k in ("frames", "channels", "slices")} | {"images": meta.get("images")}
        if {k: ij[k] for k in theirs} != theirs:
            said.append(f"ImageJ {ij}, tifffile's {theirs}")
        if ij["plane_bytes"] is not None and len(ij["planes"]) != ij["images"] - 1:
            said.append("an ImageJ stack in one IFD without a struct per later plane")
    mm = d["micromanager"]
    if mm is not None:
        seen["micromanager"] += 1
        with Path(tif.filehandle.path).open("rb") as fh:
            meta = tifffile.read_micromanager_metadata(fh)
        if meta is None or mm["summary"] != meta["Summary"]:
            said.append("Micro-Manager summary differs")
        index = meta["IndexMap"] if meta else None
        if index is not None and mm["index_map"] != np.asarray(index).reshape(-1, 5).tolist():
                said.append("Micro-Manager index map differs")
    ap = d["aperio"]
    if ap is not None:
        seen["aperio"] += 1
        theirs = tifffile.tifffile.svs_description_metadata(tif.pages.first.description)
        if not set(theirs) - {"Header"} <= set(ap) | {"Header"}:
            said.append(f"Aperio fields {sorted(ap)}, tifffile's {sorted(theirs)}")


def check_qpi(e: dict, page: tifffile.TiffPage, said: list[str], seen: Counter) -> None:
    text = page.tags[270].value if 270 in page.tags else ""
    try:
        root = ET.fromstring(re.sub(r"^<\?xml[^>]*\?>", "", text))  # noqa: S314 (a fixture's)
    except ET.ParseError:
        root = None
    q = e["qpi"]
    if root is None or local(root.tag) != "PerkinElmer-QPI-ImageDescription":
        if q is not None:
            said.append(f"IFD at {page.offset}: a QPI description ElementTree does not read")
        return
    seen["qpi"] += 1
    child = lambda n: next(((x.text or "").strip() for x in root if local(x.tag) == n), None)  # noqa: E731
    theirs = {"image_type": child("ImageType"), "name": child("Name"), "color": child("Color"),
              "slide_id": child("SlideID")}
    if q != theirs:
        said.append(f"IFD at {page.offset}: QPI {q}, ElementTree's {theirs}")


def jpeg_size(b: bytes) -> tuple[int, int] | None:
    """A JPEG stream's height and width, from its SOF0."""
    at = b.find(b"\xff\xc0")
    return None if at < 0 else struct.unpack(">HH", b[at + 5:at + 9])


def check_ndpi(e: dict, page: tifffile.TiffPage, data: bytes, said: list[str], seen: Counter) -> None:
    if "tile_tables" not in e:
        return
    seen["ndpi images"] += 1
    t = e["tile_tables"]
    if t is not None and page.is_tiled:
        seen["ndpi tiles"] += 1
        if (t["offsets"], t["byte_counts"]) != (list(page.dataoffsets), list(page.databytecounts)):
            said.append(f"IFD at {page.offset}: tile tables differ from tifffile's")
    s = e["mcu_starts"]
    if s is not None and s["same_length"] and s["high_word"]:
        seen["mcu starts"] += 1
        starts = list(page.tags[65426].value)
        if 65432 in page.tags:
            starts = [a + (b << 32) for a, b in zip(starts, page.tags[65432].value, strict=True)]
        if s["starts"] != starts:
            said.append(f"IFD at {page.offset}: McuStarts differ")
    s0 = tag_values(page.tags[273])[0] if 273 in page.tags else 0
    if e.get("jpeg_header"):
        seen["jpeg headers"] += 1
        h = base64.b64decode(e["jpeg_header"])
        if h != data[s0:s0 + len(h)] or not h.startswith(b"\xff\xd8"):
            said.append(f"IFD at {page.offset}: the JPEG header is not the strip's first bytes")
    if e.get("jpeg_frame"):
        seen["jpeg frames"] += 1
        f = e["jpeg_frame"]
        if jpeg_size(data[s0:s0 + 4096]) != (f["height"], f["width"]):
            said.append(f"IFD at {page.offset}: frame header {f}")
    x = e.get("jpegxr")
    if x and x["header"]:
        seen["jpegxr headers"] += 1
        o, c = x["first"]
        a = imagecodecs.jpegxr_decode(data[o:o + c])
        if a.shape[:2] != (x["header"]["height"], x["header"]["width"]):
            said.append(f"IFD at {page.offset}: JPEG XR header {x['header']}, decoded {a.shape}")
    if e["focal_offset"] is not None:
        seen["focal offsets"] += 1
        if e["focal_offset"] != tag_values(page.tags[65424])[0]:
            said.append(f"IFD at {page.offset}: focal offset {e['focal_offset']}")
    for code, name in ((65434, "Fluorescence"), (65451, "Wavelength"), (65454, "ExposureTime")):
        if name in e["acquisition"]:
            seen["acquisition tags"] += 1
            v, theirs = e["acquisition"][name], tag_values(page.tags[code])[0]
            if v != (theirs.rstrip("\0") if isinstance(theirs, str) else pytest.approx(theirs)):
                said.append(f"IFD at {page.offset}: {name} {v!r}, tifffile's {theirs!r}")


@cache
def compared(path: Path) -> tuple[tuple[str, ...], Counter]:
    logging.getLogger("tifffile").setLevel(logging.CRITICAL)
    said: list[str] = []
    seen: Counter = Counter()
    with read(path) as parsed:
        d = parsed.description
        profile = parsed.profile
    if d is None:
        seen["sets"] += 1
        return (), seen
    data = path.read_bytes()
    seen["files"] += 1
    errors = sorted(e.message[:200] for e in jsonschema.Draft202012Validator(SCHEMAS[profile]).iter_errors(d))
    said += [f"not of the schema: {e}" for e in errors]
    ndpi = profile == "ndpi"
    with tifffile.TiffFile(path) as tif:
        tif.pages.useframes = False
        if (d["byte_order"], d["bigtiff"], d["size"]) != ({"<": "little", ">": "big"}[tif.byteorder], tif.is_bigtiff,
                                                          len(data)):
            said.append("header differs")
        chain = d["ifds"] if ndpi else [d["ifds"][i] for i in d["chain"]]
        offsets = [p.offset for p in tif.pages]
        if [e["offset"] for e in chain] != offsets[:len(chain)]:
            said.append(f"main chain {[e['offset'] for e in chain]}, tifffile's {offsets}")
        table = QPTIFF_TABLE if profile == "qptiff" else TABLE
        for e in d["ifds"]:
            try:
                page = page_at(tif, e["offset"], ndpi)
            except (MemoryError, ValueError, struct.error):
                # a tag whose value lies outside the file, which tifffile reads and the profile
                # does not (`ndpi_unused_values.ndpi`)
                seen["ifds tifffile cannot read"] += 1
                continue
            seen["ifds"] += 1
            if ndpi:
                check_tags(e, page, data, set(), NDPI_SCALARS, said, seen)
                check_ndpi(e, page, data, said, seen)
                continue
            check_tags(e, page, data, table, SCALARS, said, seen)
            subs = [d["ifds"][i]["offset"] for i in e["subifds"]]
            if subs != list(page.subifds or [])[:len(subs)]:
                said.append(f"IFD at {page.offset}: SubIFDs {subs}, tifffile's {page.subifds}")
            seen["subifds"] += len(subs)
            if profile == "qptiff":
                check_qpi(e, page, said, seen)
        if not ndpi:
            check_lzw_tails(d, data, said, seen)
            if profile == "tiff":
                check_structures(d, tif, said, seen)
    return tuple(said), seen


@pytest.mark.parametrize("path", FILES, ids=[f"{p.parent.name}/{p.name}" for p in FILES])
def test_a_tiffs_description_is_what_tifffile_reads(path):
    said, _ = compared(path)
    assert said == ()


def test_the_fixtures_reach_every_case_the_comparison_reads():
    seen: Counter = Counter()
    for path in FILES:
        seen += compared(path)[1]
    wanted = ["files", "sets", "ifds", "tags", "subifds", "image descriptions", "JPEG tables", "lzw tails", "ome",
              "imagej", "micromanager", "aperio", "qpi", "ndpi images", "ndpi tiles", "mcu starts", "jpeg headers",
              "jpeg frames", "jpegxr headers", "focal offsets", "acquisition tags"]
    assert [w for w in wanted if not seen[w]] == []


@pytest.mark.parametrize("path", FILES[::7], ids=[p.name for p in FILES[::7]])
def test_a_description_holds_none_of_the_outputs_decisions(path):
    with read(path) as parsed:
        d = parsed.description
    assert d is None or not DECISIONS & d.keys()
