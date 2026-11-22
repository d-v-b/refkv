"""The description of every LIF, LOF and LIFEXT fixture (spec/zarrify/lif.md §9
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#9-the-description>), compared with liffile,
the reference reader: the file's type, version and `UniqueID`; its XML's root; its memory blocks by ID,
with the offset and size of their data; and of each image, its name, path, `UniqueID`, the attributes of
its element, `Memory`, dimensions and channels as liffile's XML element holds them, the memory block it
names, its sample count, and its `TileScanInfo` attachment as liffile reads it. The description's header
block, LOF parts and tail are checked against the file's bytes. A second test asserts which cases the
fixtures reached, and a third that the description holds none of the output's decisions.

liffile 2026.10.14 departs from what the file declares in four ways, each counted where it is met:
it names a LOF's one memory block by its XML's `MemoryBlockID`, where the block itself has no ID field
(the description's `id` is null); it reads an older writer's LOF XML, whose root is `Data`, as if it were
in an `LMSDataContainerHeader` of `Version` 2 (the description's `root` is `Data` and its `version`
null); it names an image whose element has no `Name` by the file's stem; and it reads only the first
`ChildrenOf` of a LIFEXT file, so the images of a second are compared with the XML alone. A LAS X
project, a store run's input, has no description yet."""

import json
import math
import struct
import xml.etree.ElementTree as ET
from collections import Counter
from functools import cache
from pathlib import Path

import jsonschema
import liffile
import pytest
from ir_helpers import FIXTURES, ROOT

from refkv.build import read

FILES = sorted(p for p in (FIXTURES / "lif").iterdir() if p.is_file() and "reject" not in p.name)
SCHEMA = json.loads((ROOT / "spec" / "zarrify" / "lif" / "description.json").read_text())
# members a description never has: the output's decisions (spec/zarrify/lif.md §9
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#9-the-description>)
DECISIONS = {"data_type", "item", "interleaved", "axes", "shape", "chunk_shape", "offsets", "order", "colors",
             "resolution", "t", "z", "rows", "row", "y_stride", "band", "base", "end", "field_dims", "fields",
             "tiles", "scale", "units", "series", "omero", "held"}
LIFFILE_TYPE = {"lif": liffile.LifFileType.LIF, "lof": liffile.LifFileType.LOF,
                "lifext": liffile.LifFileType.LIFEXT}


def attributes(e: ET.Element | None) -> dict[str, str] | None:
    return None if e is None else dict(e.attrib)


def check_header(d: dict, data: bytes, said: list[str], seen: Counter) -> None:
    """The header block, a LOF's parts, the blocks and the tail against the file's bytes."""
    test, _, magic, chars = struct.unpack_from("<IIBI", data, 0)
    if (test, magic, chars, d["size"]) != (0x70, 0x2A, d["xml_characters"], len(data)):
        said.append("the header block differs")
    o = d["object"]
    if o is not None:
        seen["lofs"] += 1
        seen["older lofs"] += o["older"]
        at = 13 + 2 * chars
        _, v1, _, v2 = struct.unpack_from("<BIBI", data, at)
        if o["versions"] != [v1, v2]:
            said.append(f"LOF versions {o['versions']}, the file's {[v1, v2]}")
        test, _, magic, xml_chars = struct.unpack_from("<IIBI", data, o["xml_at"])
        if (test, magic, xml_chars) != (0x70, 0x2A, o["xml_characters"]):
            said.append("the LOF's XML block differs")
        xml = data[o["xml_at"] + 13:o["xml_at"] + 13 + 2 * xml_chars].decode("utf-16-le")
        if o["older"] != xml.startswith("<Data>"):
            said.append("the LOF's XML is not of the writer the description says")
    for b in d["blocks"]:
        seen["blocks"] += 1
        if b["id"] is None:
            continue
        test, _, magic = struct.unpack_from("<IIB", data, b["offset"])
        wide = d["version"] == 2 or d["file"] == "lifext"
        size, magic2, id_chars = struct.unpack_from("<QBI" if wide else "<IBI", data, b["offset"] + 9)
        id_at = b["offset"] + (22 if wide else 18)
        id_text = data[id_at:id_at + 2 * id_chars].decode("utf-16-le")
        if (test, magic, magic2, size, id_text, id_at + 2 * id_chars) != (0x70, 0x2A, 0x2A, b["size"], b["id"],
                                                                          b["data"]):
            said.append(f"the block at {b['offset']} differs from the file's")
    t = d["tail"]
    if t is not None:
        seen["tails"] += 1
        if t["offset"] + t["length"] != len(data):
            said.append(f"tail {t}")


def check_tile_scan(mine: dict | None, im: liffile.LifImageABC, said: list[str], seen: Counter) -> None:
    theirs = im.tilescan
    if mine is None or theirs is None:
        if (mine is None) != (theirs is None):
            said.append(f"{im.name}: TileScanInfo {mine}, liffile's {theirs}")
        return
    seen["tile scans"] += 1
    a = mine["attributes"]
    flips = [a.get(k, "0") == "1" for k in ("FlipX", "FlipY", "SwapXY")]
    if flips != [theirs.flip_x, theirs.flip_y, theirs.swap_xy]:
        said.append(f"{im.name}: TileScanInfo flips {flips}")
    if len(mine["tiles"]) != len(theirs.tiles):
        said.append(f"{im.name}: {len(mine['tiles'])} tiles, liffile's {len(theirs.tiles)}")
    for tile, row in zip(mine["tiles"], theirs.tiles, strict=False):
        seen["tiles"] += 1
        want = (int(row["field_x"]), int(row["field_y"]), float(row["pos_x"]), float(row["pos_y"]), float(row["pos_z"]))
        got = (int(tile["FieldX"]), int(tile["FieldY"]), float(tile["PosX"]), float(tile["PosY"]),
               float(tile.get("PosZ", 0)))
        if got != want:
            said.append(f"{im.name}: tile {got}, liffile's {want}")


def check_image(d: dict, mine: dict, im: liffile.LifImageABC, stem: str, said: list[str], seen: Counter) -> None:
    seen["images"] += 1
    e = im.xml_element
    name = mine["name"]
    if name is None:
        # liffile names an element that has none by the file's stem
        seen["images without a name"] += 1
        name = stem
    if name != im.name or mine["attributes"].get("UniqueID") != im.uuid:
        said.append(f"image {mine['name']!r}: liffile's {im.name!r} {im.uuid!r}")
    # liffile's path leaves out a LIF file's own element, and starts with a LIFEXT image's parent
    path = [stem if p is None else p for p in (mine["path"][1:] if d["file"] == "lif" else mine["path"])]
    if mine["parent"] is not None:
        seen["parents"] += 1
        path = [mine["parent"], *path]
    if "/".join(path) != im.path:
        said.append(f"image {name!r}: path {path}, liffile's {im.path!r}")
    description = e.find("Data/Image/ImageDescription")
    dims = [] if description is None else description.findall("Dimensions/DimensionDescription")
    channels = [] if description is None else description.findall("Channels/ChannelDescription")
    # the Name liffile gives an element that has none
    given = {k: v for k, v in e.attrib.items() if mine["name"] is not None or k != "Name"}
    want = {"attributes": given, "memory": attributes(e.find("Memory")),
            "dimensions": [dict(x.attrib) for x in dims], "channels": [dict(x.attrib) for x in channels]}
    for k, v in want.items():
        if mine[k] != v:
            said.append(f"image {name!r}: {k} {mine[k]}, liffile's XML element's {v}")
    seen["dimensions"] += len(dims)
    seen["channels"] += len(channels)
    samples = math.prod(int(x["NumberOfElements"]) for x in mine["dimensions"]) * len(mine["channels"])
    if samples != math.prod(im.shape):
        said.append(f"image {name!r}: {samples} samples, liffile's shape {im.shape}")
    b = d["blocks"][mine["block"]]
    block = im.memory_block
    if b["id"] is None:
        # a LOF's block, which liffile names by its XML's MemoryBlockID
        seen["lof blocks named by their xml"] += 1
    elif b["id"] != block.id:
        said.append(f"image {name!r}: block {b['id']!r}, liffile's {block.id!r}")
    if (b["data"], b["size"]) != (block.offset, block.size):
        said.append(f"image {name!r}: block data at {b['data']} of {b['size']}, liffile's {block.offset} of "
                    f"{block.size}")
    check_tile_scan(mine["tile_scan"], im, said, seen)


@cache
def compared(path: Path) -> tuple[tuple[str, ...], Counter]:
    said: list[str] = []
    seen: Counter = Counter()
    with read(path) as parsed:
        d = parsed.description
    if d is None:
        seen["sets"] += 1
        return (), seen
    seen["files"] += 1
    seen[d["file"]] += 1
    seen[f"version {d['version']}"] += 1
    errors = sorted(e.message[:200] for e in jsonschema.Draft202012Validator(SCHEMA).iter_errors(d))
    said += [f"not of the schema: {e}" for e in errors]
    check_header(d, path.read_bytes(), said, seen)
    with liffile.LifFile(path) as f:
        version, root = d["version"], d["root"]
        if d["object"] is not None and d["object"]["older"]:
            # liffile reads an older writer's XML in a root of Version 2 it makes, as the parser does
            seen["older lofs read in a root liffile makes"] += 1
            if (version, root) != (None, "Data"):
                said.append(f"an older writer's LOF of root {root} and version {version}")
            version, root = 2, "LMSDataContainerHeader"
        if (LIFFILE_TYPE[d["file"]], version, root) != (f.type, f.version, f.xml_element.tag):
            said.append(f"{d['file']} {d['version']} {d['root']}, liffile's {f.type} {f.version} {f.xml_element.tag}")
        if d["unique_id"] != f.uuid:
            said.append(f"UniqueID {d['unique_id']!r}, liffile's {f.uuid!r}")
        theirs = {b.id: (b.offset, b.size) for b in f.memory_blocks.values()}
        mine = {b["id"]: (b["data"], b["size"]) for b in d["blocks"] if b["id"] is not None}
        if d["file"] != "lof" and mine != theirs:
            said.append(f"blocks {mine}, liffile's {theirs}")
        images = list(f.images)
        if not d["images"]:
            seen["files without images"] += 1
        if len(images) < len(d["images"]):
            # liffile reads only the first ChildrenOf of a LIFEXT file
            seen["images liffile does not read"] += len(d["images"]) - len(images)
        elif len(images) > len(d["images"]):
            said.append(f"{len(d['images'])} images, liffile's {len(images)}")
        for mine_image, im in zip(d["images"], images, strict=False):
            check_image(d, mine_image, im, path.stem, said, seen)
    return tuple(said), seen


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_a_lifs_description_is_what_liffile_reads(path):
    said, _ = compared(path)
    assert said == ()


def test_the_fixtures_reach_every_case_the_comparison_reads():
    seen: Counter = Counter()
    for path in FILES:
        seen += compared(path)[1]
    wanted = ["files", "sets", "lif", "lof", "lifext", "version 1", "version 2", "lofs", "older lofs", "blocks",
              "tails", "images", "images without a name", "parents", "dimensions", "channels", "tile scans", "tiles",
              "lof blocks named by their xml", "images liffile does not read", "files without images",
              "older lofs read in a root liffile makes"]
    assert [w for w in wanted if not seen[w]] == []


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_a_description_holds_none_of_the_outputs_decisions(path):
    with read(path) as parsed:
        d = parsed.description
    assert d is None or not DECISIONS & (d.keys() | {k for im in d["images"] for k in im})
