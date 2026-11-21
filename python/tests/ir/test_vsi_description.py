"""The description of every VSI fixture the profile reads (spec/zarrify/vsi.md §9.6
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#96-the-description>), compared with the
reference readers.

- tifffile reads the `.vsi` file's TIFF part: the header, the main chain by its offsets, and every tag
  of the profile's set each IFD has, its field type, count and values (JPEGTables as bytes), and each
  EXIF IFD at the offset its IFD's tag 34665 gives.
- Bio-Formats (bioio-bioformats, through its CellSensReader's own fields, read by reflection) reads the
  slide: the tile files it opens, and of each its compression, tile size, coordinate count,
  background color, BGR flag, and its directory's tiles (offsets and coordinates, in order); and of
  each stack with a tile file its name, image size, calibration, origin, magnification, creation
  time, channel names and wavelengths, z increment, and the coordinates of its `z`, `t` and `c`
  dimensions; the series' `Image Type`; and the tree's text fields that it records under the same
  names. Bio-Formats does not open three fixtures (`BIOFORMATS_CANNOT`): theirs are checked by the
  other two.
- The spec, for what neither reader exposes: each tile file's SIS and ETS headers and directory
  records read at their offsets (spec/zarrify/vsi.md §3
  <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#3-the-tile-files>), the stacks' fields
  at their places in the description's own tree (§2.2
  <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#22-stacks>), each channel's LUT color,
  and that every element the description names is the IR's element of those bytes.

A second test asserts which cases the fixtures reached, and a third that the description holds none of
the output's decisions."""

import base64
import json
import os
import re
import struct
from collections import Counter
from functools import cache
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import tifffile
from bioio_bioformats import Reader
from ir_helpers import FIXTURES, ROOT

from refkv.build import read

VSI = FIXTURES / "vsi"
FILES = sorted(p for p in VSI.glob("*.vsi") if "reject" not in p.name)
SCHEMA = json.loads((ROOT / "spec" / "zarrify" / "vsi" / "description.json").read_text())
# the IFD tags the profile reads (spec/zarrify/vsi.md §9.6
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#96-the-description>)
TAGS = {256, 257, 258, 259, 262, 273, 277, 278, 279, 284, 322, 323, 324, 325, 339, 347, 34665}
# the fixtures Bio-Formats does not open: an IFD of width 0 ("0 must be non-null and strictly
# positive"), a tree whose edge cases it reads past the file's end, and an int64 tile file ("Unsupported
# pixel type: long")
BIOFORMATS_CANNOT = {"vsi_left_out_macro_size.vsi", "vsi_metadata_edges.vsi", "vsi_raw_types.vsi"}
# members a description never has: the output's decisions (spec/zarrify/vsi.md §9.6
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#96-the-description>)
DECISIONS = {"images", "not_presented", "declared_by", "axes", "levels", "sizes", "scales", "translation", "units",
             "unit_name", "z_unit_name", "data_type", "codec", "codecs", "fill", "bgr", "omero", "window", "prefix",
             "tiles_outside", "coordinates_of"}
# the tree's tags (spec/zarrify/vsi.md §2.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#22-stacks>)
COLLECTION, STACK, FRAME, PROPERTIES, EXTERNAL, DIMENSION, CHANNEL = 2000, 2001, 2002, 2005, 2018, 2007, 2008


def field(fields: list[dict], tag: int, tag2: int | None = None) -> dict | None:
    return next((f for f in fields if f["tag"] == tag and (tag2 is None or f.get("tag2") == tag2)), None)


def value(fields: list[dict] | None, tag: int) -> Any:
    """The first value field of `tag` (spec/zarrify/vsi.md §6.1
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#61-the-root>)."""
    f = next((f for f in fields or [] if f["tag"] == tag and "value" in f), None)
    return None if f is None else f["value"]


def subfields(fields: list[dict] | None, tag: int) -> list[dict] | None:
    f = next((f for f in fields or [] if f["tag"] == tag and "fields" in f), None)
    return None if f is None else f["fields"]


def mdim(fields: list[dict], tag: int) -> tuple[Any, Any] | None:
    v = subfields(fields, tag)
    return None if v is None else (value(v, 268435458), value(v, 268435456))


def lut_color(fields: list[dict]) -> str | None:
    """A channel's color: the last entry of its display LUT (2004), stored blue first but for a value of
    type 269 (spec/zarrify/vsi.md §4.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#42-channels>)."""
    lut = next((f for f in subfields(fields, 2004) or [] if f["tag"] == 268435458), None)
    if lut is None or not isinstance(lut["value"], list) or len(lut["value"]) < 3 or len(lut["value"]) % 3:
        return None
    last = lut["value"][-3:]
    rgb = last if lut["type"] == 269 else last[::-1]
    return "".join(f"{x:02X}" for x in rgb)


def check_tiff(path: Path, d: dict, ir: Any, said: list[str], seen: Counter) -> None:
    """The `.vsi` file's TIFF part against tifffile."""
    data = path.read_bytes()
    h = d["header"]
    if (h["byte_order"], h["magic"], h["first_ifd"]) != ("II", 42, struct.unpack_from("<I", data, 4)[0]):
        said.append(f"header {h}")
    if ir.extent(h["element"]) != (0, 8):
        said.append("the header's element")
    with tifffile.TiffFile(path) as tif:
        chain = [e for e in d["ifds"] if not e["exif"]]
        if [e["offset"] for e in chain] != [p.offset for p in tif.pages]:
            said.append(f"main chain {[e['offset'] for e in chain]}, tifffile's {[p.offset for p in tif.pages]}")
        for e in chain:
            page = tif.pages[e["index"]]
            seen["ifds"] += 1
            tags = {int(k): v for k, v in e["tags"].items()}
            missing = sorted(t for t in TAGS if t in page.tags and t not in tags)
            said += [f"IFD {e['index']}: tag {t} left out" for t in missing]
            for code, t in tags.items():
                theirs = page.tags.get(code)
                if theirs is None:
                    said.append(f"IFD {e['index']}: tag {code}, which tifffile does not read")
                    continue
                if (t["type"], t["count"]) != (int(theirs.dtype), theirs.count):
                    said.append(f"IFD {e['index']}: tag {code} of type {t['type']}, count {t['count']}")
                seen["tags"] += 1
                if code == 347:
                    seen["JPEG tables"] += 1
                    if base64.b64decode(t["value"]) != bytes(theirs.value):
                        said.append(f"IFD {e['index']}: JPEGTables differ")
                    continue
                # the 4-byte field of an IFD offset, which tifffile reads as the IFD it points at
                raw = struct.unpack_from("<I", data, theirs.offset + 8)[0] if code == 34665 else None
                listed = list(theirs.value) if isinstance(theirs.value, tuple) else [theirs.value]
                want = [raw] if raw is not None else listed
                if t["value"] != want:
                    said.append(f"IFD {e['index']}: tag {code} is {t['value'][:8]}, tifffile's {want[:8]}")
            # each strip's and tile's element is the IR's of its bytes
            for name, offsets, counts in (("strips", 273, 279), ("tiles", 324, 325)):
                if e[name] is None:
                    continue
                pairs = list(zip(tags[offsets]["value"], tags[counts]["value"], strict=True))
                for el, (o, n) in zip(e[name], pairs, strict=True):
                    seen[f"IFD {name}"] += 1
                    if el is not None and ir.extent(el) != (o, n):
                        said.append(f"IFD {e['index']}: {name} element {el} is {ir.extent(el)}, not {(o, n)}")
                    if el is None and n > 0 and o + n <= len(data):
                        said.append(f"IFD {e['index']}: a {name} entry within the file has no element")
            if e["element"] is not None and ir.extent(e["element"])[0] != e["offset"]:
                said.append(f"IFD {e['index']}: its element is not the IFD's")
        for x in (e for e in d["ifds"] if e["exif"]):
            seen["exif ifds"] += 1
            owner = next(e for e in chain if e["index"] == x["index"])
            if owner["tags"]["34665"]["value"] != [x["offset"]]:
                said.append(f"EXIF IFD of IFD {x['index']} at {x['offset']}")


def check_spec(path: Path, d: dict, ir: Any, said: list[str], seen: Counter) -> None:
    """The tile files' headers and directories at their offsets, and the stacks' fields in the tree."""
    seen["files"] += 1
    if (d["key"], d["size"], ir.path(d["element"])) != (path.name, path.stat().st_size, path.name):
        said.append(f"the .vsi file {d['key']}")
    base = f"_{path.stem}_"
    for f in d["tile_files"]:
        seen["tile files"] += 1
        stack = d["stacks"][f["stack"]]
        if stack["tile_file"] != d["tile_files"].index(f):
            said.append(f"{f['key']}: its stack does not name it")
        if not re.fullmatch(rf"{re.escape(base)}/stack{stack['id']}/frame_t(_0)?\.ets", f["key"]):
            said.append(f"{f['key']} is not the stack's tile file name")
        data = (path.parent / f["key"]).read_bytes()
        if (f["size"], ir.path(f["element"])) != (len(data), f["key"]):
            said.append(f"{f['key']}: size or element")
        s = f["sis"]
        sis = struct.unpack_from("<4xiiiqi4xqi4xq", data, 0)
        if tuple(s[k] for k in ("header_size", "version", "coordinates", "ets_offset", "ets_size", "tiles_offset",
                                "tiles", "size")) != sis:
            said.append(f"{f['key']}: SIS header {s}, the file's {sis}")
        o = s["ets_offset"]
        e = f["ets"]
        ets = struct.unpack_from("<4x9i", data, o)
        if tuple(e[k] for k in ("version", "pixel_type", "channels", "colorspace", "compression", "quality",
                                "tile_width", "tile_height", "tile_depth")) != ets:
            said.append(f"{f['key']}: ETS header {e}, the file's {ets}")
        order, pyramid = struct.unpack_from("<ii", data, o + 148)
        dims = struct.unpack_from("<i", data, o + 184)[0]
        sizes = list(struct.unpack_from("<10i", data, o + 188))[:max(0, min(dims, 10))]
        if (base64.b64decode(e["background"]), e["component_order"], e["use_pyramid"], e["dimensions"],
                e["dimension_sizes"]) != (data[o + 108:o + 148], order, pyramid != 0, dims, sizes):
            said.append(f"{f['key']}: ETS header's background, order, pyramid or dimensions")
        if ir.extent(f["header"]) != (o, 228) or not ir.path(f["header"]).endswith("/ets"):
            said.append(f"{f['key']}: the ETS header's element")
        n = s["coordinates"]
        rec = 20 + 4 * n
        for k, t in enumerate(f["tiles"]):
            seen["tiles"] += 1
            at = s["tiles_offset"] + k * rec
            coords = list(struct.unpack_from(f"<{n}i", data, at + 4))
            offset, size = struct.unpack_from("<qi", data, at + 4 + 4 * n)
            if t[1:] != [offset, size, *coords]:
                said.append(f"{f['key']}: tile {k} is {t[1:]}, the record's {[offset, size, *coords]}")
            if ir.extent(t[0]) != (offset, size):
                said.append(f"{f['key']}: tile {k}'s element")
        if len(f["tiles"]) != s["tiles"]:
            said.append(f"{f['key']}: {len(f['tiles'])} tiles of {s['tiles']}")
    # the stacks, at their places in the tree
    coll = subfields(d["tree"]["fields"], COLLECTION) or []
    mac = field(coll, 2016)
    if d["macro_ifd"] != (mac["tag2"] if mac and "ifd" in mac else None):
        said.append(f"macro IFD {d['macro_ifd']}")
    seen["macro images"] += d["macro_ifd"] is not None
    stacks = [f for f in coll if f["tag"] == STACK and "fields" in f]
    if [s["id"] for s in d["stacks"]] != [f.get("tag2", 0) for f in stacks]:
        said.append(f"stacks {[s['id'] for s in d['stacks']]}")
    for s, f in zip(d["stacks"], stacks, strict=False):
        seen["stacks"] += 1
        sc = f["fields"]
        fc = subfields(sc, FRAME)
        xp = subfields(fc, EXTERNAL)
        pc = subfields(sc, PROPERTIES)

        def ifd_of(tag: int, fc: list[dict] | None = fc) -> int | None:
            x = next((x for x in fc or [] if x["tag"] == tag and "ifd" in x), None)
            return None if x is None else x.get("tag2")

        want = {
            "name": value(pc, 2030), "type": value(pc, 2074), "has_external_file": value(xp, 20005),
            "image_boundary": value(xp, 2053), "dimension_sizes": value(sc, 2003), "default_ifd": ifd_of(2016),
            "coarse_ifd": ifd_of(2019), "frame_scale": value(pc, 2019), "frame_origin": value(pc, 2018),
            "frame_unit": value(pc, 2020), "display_limits": value(pc, 2003), "creation_time": value(pc, 2015),
        }
        for k, v in want.items():
            if s[k] != v:
                said.append(f"stack {s['id']}: {k} {s[k]!r}, the tree's {v!r}")
        dims = [x for x in sc if x["tag"] == DIMENSION and "fields" in x]
        if [x["index"] for x in s["dimensions"]] != [x.get("tag2", 0) for x in dims]:
            said.append(f"stack {s['id']}: dimensions {s['dimensions']}")
        for x, dim in zip(s["dimensions"], dims, strict=False):
            seen["dimensions"] += 1
            psets = [p["fields"] for p in dim["fields"] if p["tag"] == CHANNEL and "fields" in p]
            code = next((value(p, 2023) for p in psets if isinstance(value(p, 2023), (int, float))), None)
            if x["meaning_code"] != code:
                said.append(f"stack {s['id']}: dimension {x['index']}'s code {x['meaning_code']}")
            if x["meaning"] == "z":
                z = [m for m in (mdim(p, 2013) for p in psets) if m is not None]
                if z:
                    seen["z increments"] += 1
                    if s["z_increment"] != {"value": z[-1][0], "unit": z[-1][1]}:
                        said.append(f"stack {s['id']}: z increment {s['z_increment']}")
            if x["meaning"] == "c":
                if len(s["channels"]) != len(psets):
                    said.append(f"stack {s['id']}: {len(s['channels'])} channels of {len(psets)}")
                for c, p in zip(s["channels"], psets, strict=False):
                    seen["channels"] += 1
                    seen["lut colors"] += c["lut_color"] is not None
                    w = mdim(p, 2417)
                    theirs = {"dimension_name": value(p, 2021), "channel_name": value(p, 2419),
                              "lut_color": lut_color(p), "display_limits": value(p, 2003),
                              "wavelength": None if w is None else w[0]}
                    if c != theirs:
                        said.append(f"stack {s['id']}: channel {c}, the tree's {theirs}")


def get(o: Any, name: str) -> Any:
    """A Java object's field, private or not (Bio-Formats' reader keeps what it read in private fields)."""
    f = o.getClass().getDeclaredField(name)
    f.setAccessible(True)
    return f.get(o)


def jmap(m: Any) -> dict:
    """A Java map's entries, keyed as Java keys them (a Python int does not find an Integer key)."""
    return {k: m.get(k) for k in m.keySet()}


def check_bioformats(path: Path, d: dict, said: list[str], seen: Counter) -> None:
    """The tile files and the stacks against Bio-Formats' CellSensReader."""
    r = Reader(str(path))
    bf = r._bf  # bioio's handle on the Java reader
    with bf.ensure_open():
        j: Any = bf._java_reader
        c = j.getReader()
        used = sorted(Path(str(u)).relative_to(path.parent).as_posix() for u in get(c, "usedFiles"))
        if used != sorted([path.name, *(f["key"] for f in d["tile_files"])]):
            said.append(f"files {used}")
        file_map = {int(k): Path(str(v)).relative_to(path.parent).as_posix()
                    for k, v in jmap(get(c, "fileMap")).items()}
        # each tile file's first core index (Bio-Formats keeps what it read of a file per resolution)
        files = {v: k for k, v in sorted(file_map.items(), reverse=True)}
        background = {int(k): bytes(int(b) & 0xFF for b in v) for k, v in jmap(get(c, "backgroundColor")).items()}
        for f in d["tile_files"]:
            seen["bioformats tile files"] += 1
            i = files[f["key"]]
            e = f["ets"]
            mine = (e["compression"], e["tile_width"], e["tile_height"], f["sis"]["coordinates"],
                    base64.b64decode(e["background"])[:len(background[i])])
            theirs = (int(get(c, "compressionType").get(i)), int(get(c, "tileX").get(i)), int(get(c, "tileY").get(i)),
                      int(get(c, "nDimensions").get(i)), background[i])
            if mine != theirs:
                said.append(f"{f['key']}: {mine}, Bio-Formats' {theirs}")
            offsets = [int(x) for x in get(c, "tileOffsets").get(i)]
            coords = [list(t.coordinate) for t in get(c, "tileMap").get(i)]
            if [t[1] for t in f["tiles"]] != offsets or [t[3:] for t in f["tiles"]] != coords:
                said.append(f"{f['key']}: tiles differ from Bio-Formats'")
            seen["bioformats tiles"] += len(offsets)
        bgr = list(get(c, "bgr"))
        for s in range(j.getSeriesCount()):
            key = file_map.get(int(c.seriesToCoreIndex(s)))
            if key is None:
                continue
            f = next(f for f in d["tile_files"] if f["key"] == key)
            seen["bgr"] += bool(bgr[s])
            if bool(bgr[s]) != (f["ets"]["component_order"] == 1 and f["ets"]["compression"] == 0):
                said.append(f"{f['key']}: Bio-Formats' BGR flag {bgr[s]}")
        stacks = [s for s in d["stacks"] if s["tile_file"] is not None]
        pyramids = list(get(c, "pyramids"))
        if [str(get(p, "name")) for p in pyramids] != [s["name"] for s in stacks]:
            said.append(f"pyramids {[str(get(p, 'name')) for p in pyramids]}")
        for s, p in zip(stacks, pyramids, strict=False):
            seen["pyramids"] += 1

            def num(x: Any) -> float | None:
                return None if x is None else float(x)

            box = s["image_boundary"]
            scale, origin = s["frame_scale"] or [None, None], s["frame_origin"] or [None, None]
            mine = [box[2], box[3], *scale[:2], *origin[:2], s["magnification"], s["creation_time"]]
            theirs = [num(get(p, k)) for k in ("width", "height", "physicalSizeX", "physicalSizeY", "originX",
                                               "originY", "magnification", "acquisitionTime")]
            if mine != theirs:
                said.append(f"stack {s['id']}: {mine}, Bio-Formats' {theirs}")
            names = [str(x) for x in get(p, "channelNames")]
            waves = [float(x) for x in get(p, "channelWavelengths")]
            if names and names != [ch["channel_name"] for ch in s["channels"]]:
                said.append(f"stack {s['id']}: channel names {names}")
            if waves != [ch["wavelength"] for ch in s["channels"] if ch["wavelength"] is not None]:
                said.append(f"stack {s['id']}: wavelengths {waves}")
            seen["bioformats channels"] += len(names)
            z = num(get(p, "zIncrement"))
            if z is not None:
                seen["bioformats z increments"] += 1
                if s["z_increment"] is None or s["z_increment"]["value"] != z:
                    said.append(f"stack {s['id']}: z increment {s['z_increment']}, Bio-Formats' {z}")
            order = get(p, "dimensionOrdering")
            theirs_dims = {str(k).lower(): int(order.get(k)) for k in order.keySet()}
            mine_dims = {x["meaning"]: x["index"] for x in s["dimensions"] if x["meaning"] in ("z", "t", "c")}
            seen["bioformats dimensions"] += len(theirs_dims)
            if mine_dims != theirs_dims:
                said.append(f"stack {s['id']}: dimensions {mine_dims}, Bio-Formats' {theirs_dims}")
        # the series' Image Type, and the tree's text fields Bio-Formats records under the same names
        meta: dict[str, set[str]] = {}
        tables = [j.getGlobalMetadata()]
        for s in range(j.getSeriesCount()):
            j.setSeries(s)
            tables.append(j.getSeriesMetadata())
        for t in tables:
            for k in t.keySet():
                meta.setdefault(re.sub(r" #\d+$", "", str(k)), set()).add(str(t.get(k)))
        for s in d["stacks"]:
            if s["type_name"] is not None and s["tile_file"] is not None:
                seen["image types"] += 1
                if s["type_name"] not in meta.get("Image Type", set()):
                    said.append(f"stack {s['id']}: Image Type {s['type_name']!r}, Bio-Formats' {meta['Image Type']}")

        def texts(fields: list[dict]) -> None:
            for x in fields:
                if "fields" in x:
                    texts(x["fields"])
                # an mdim volume's `Units` (268435456), which Bio-Formats records under one name for
                # every volume, keeping one, is left out
                elif (isinstance(x.get("value"), str) and x.get("name") in meta and x["tag"] != 268435456
                      and len(x["value"]) < 256):
                    seen["tree texts"] += 1
                    if x["value"] not in meta[x["name"]]:
                        said.append(f"tree field {x['name']!r}: {x['value']!r}, Bio-Formats' {meta[x['name']]}")

        texts(d["tree"]["fields"])


@cache
def bioformats_available() -> bool:
    """Whether bioio-bioformats can start its JVM (it needs Java and Maven to fetch Bio-Formats)."""
    if os.environ.get("REFKV_NO_BIOFORMATS"):
        return False
    try:
        Reader(str(VSI / "vsi_no_stacks.vsi"))
    except Exception:  # noqa: BLE001 - no JVM, no Maven, no network: not a failure of the profile
        return False
    return True


@cache
def described(path: Path) -> dict:
    with read(path) as parsed:
        d = parsed.description
        assert d is not None
        return {"description": d, "ir": parsed.ir}


@cache
def compared(path: Path, bioformats: bool) -> tuple[tuple[str, ...], Counter]:
    said: list[str] = []
    seen: Counter = Counter()
    got = described(path)
    d, ir = got["description"], got["ir"]
    said += [f"not of the schema: {e.message[:200]}" for e in jsonschema.Draft202012Validator(SCHEMA).iter_errors(d)]
    if bioformats:
        check_bioformats(path, d, said, seen)
    else:
        check_tiff(path, d, ir, said, seen)
        check_spec(path, d, ir, said, seen)
    return tuple(said), seen


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_a_slides_description_is_what_tifffile_and_the_spec_read(path):
    said, _ = compared(path, False)
    assert said == ()


@pytest.mark.parametrize("path", [p for p in FILES if p.name not in BIOFORMATS_CANNOT], ids=lambda p: p.name)
def test_a_slides_description_is_what_bioformats_reads(path):
    if not bioformats_available():
        pytest.skip("Bio-Formats is not available (bioio-bioformats needs a JVM and Maven)")
    said, _ = compared(path, True)
    assert said == ()


def test_the_fixtures_reach_every_case_the_comparison_reads():
    seen: Counter = Counter()
    for path in FILES:
        seen += compared(path, False)[1]
    wanted = ["files", "ifds", "exif ifds", "tags", "JPEG tables", "IFD strips", "tiles", "tile files", "macro images",
              "stacks", "dimensions", "z increments", "channels", "lut colors"]
    if bioformats_available():
        for path in FILES:
            if path.name not in BIOFORMATS_CANNOT:
                seen += compared(path, True)[1]
        wanted += ["bioformats tile files", "bioformats tiles", "bgr", "pyramids", "bioformats channels",
                   "bioformats z increments", "bioformats dimensions", "image types", "tree texts"]
    assert [w for w in wanted if not seen[w]] == []


@pytest.mark.parametrize("path", FILES[::5], ids=lambda p: p.name)
def test_a_description_holds_none_of_the_outputs_decisions(path):
    d = described(path)["description"]

    def keys(v: Any) -> set[str]:
        if isinstance(v, dict):
            return set(v) | {k for x in v.values() for k in keys(x)}
        if isinstance(v, list):
            return {k for x in v for k in keys(x)}
        return set()

    assert not DECISIONS & keys({k: v for k, v in d.items() if k != "tree"})
