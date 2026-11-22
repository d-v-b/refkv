"""The description of every accepted SAFE fixture, of each form (spec/zarrify/safe.md §8.11
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#811-the-description>), compared with
the reference readers: ElementTree, which parses the product and tile metadata; the listing of the
directory or the zip file's central directory (`zipfile`), which gives the objects; and, of each band
file, the reader of a JP2 file's boxes and markers of test_safe_stored.py and rasterio (GDAL's
JP2OpenJPEG driver), which reads its image. The level, the metadata documents' trees, the tile
geocoding, the bands (their image files, keys, basenames, resolutions, band names and codestreams'
sizes, tiles, components and precision) and the objects (their keys, sizes, folder markers and held
XML documents) are what those read. A second test asserts the description holds none of the output's
decisions."""

import json
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from ir_helpers import ROOT
from test_safe_stored import INPUTS, METADATA, band_files, rasterio_does, read_jp2

from refkv.build import read

SCHEMA = json.loads((ROOT / "spec" / "zarrify" / "safe" / "description.json").read_text())
MARKER = "_$folder$"
MAX_HELD_XML = 1 << 16
# members a description never has: the output's decisions (spec/zarrify/safe.md §8.11
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#811-the-description>)
DECISIONS = {"meta", "root", "counts", "data_type", "codecs", "chunk_grid", "spatial:transform", "multiscales"}


def local(name: str) -> str:
    """A name without its namespace: its local name, after a prefix's `:` or ElementTree's `}`."""
    return name.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def same_tree(ours: dict[str, Any], theirs: ET.Element, at: str) -> None:
    """The description's element is ElementTree's: its local name, its attributes but the namespace
    declarations (which ElementTree does not keep), by local name, its text (its own character data,
    ElementTree's text and each child's tail) and its children, in order."""
    assert local(ours["name"]) == local(theirs.tag), at
    attributes = {local(n): v for n, v in ours["attributes"] if n != "xmlns" and not n.startswith("xmlns:")}
    assert attributes == {local(n): v for n, v in theirs.attrib.items()}, at
    assert "".join(ours["text"]) == (theirs.text or "") + "".join(c.tail or "" for c in theirs), at
    assert len(ours["children"]) == len(theirs), at
    for k, (a, b) in enumerate(zip(ours["children"], theirs, strict=True)):
        same_tree(a, b, f"{at}/{k}")


def objects(path: Path) -> dict[str, tuple[int, bytes | None]]:
    """The product's objects, by key: each one's size, and its bytes where it is XML (read here to
    compare the metadata)."""
    out: dict[str, tuple[int, bytes | None]] = {}
    if path.is_dir():
        for p in sorted(path.rglob("*")):
            if p.is_file():
                key = p.relative_to(path).as_posix()
                out[key] = (p.stat().st_size, p.read_bytes() if key.endswith(".xml") else None)
        return out
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            key = info.filename.split("/", 1)[1]
            out[key] = (info.file_size, z.read(info) if key.endswith(".xml") else None)
    return out


def text(el: ET.Element, name: str) -> str:
    (found,) = [c for c in el if local(c.tag) == name]
    return (found.text or "").strip()


def band_name(basename: str, r: int) -> str:
    """spec/zarrify/safe.md §2.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#25-band-files>."""
    b = basename.removesuffix(f"_{r}m")
    return b.rsplit("_", 1)[-1]


@pytest.mark.parametrize("path", INPUTS, ids=[p.name for p in INPUTS])
def test_the_description_is_what_the_references_read(path: Path):
    with read(path) as parsed:
        d: Any = parsed.description
    jsonschema.validate(d, SCHEMA)
    listed = objects(path)
    assert d["form"] == ("store" if path.is_dir() else "zip")
    (product,) = [k for k in METADATA if k in listed]
    assert d["level"] == ("L1C" if product == "MTD_MSIL1C.xml" else "L2A")
    # the metadata documents, as ElementTree parses them
    assert d["product_metadata"]["key"] == product
    root = ET.fromstring(listed[product][1] or b"")  # noqa: S314 (a fixture's)
    same_tree(d["product_metadata"]["document"], root, product)
    tile_key = d["tile_metadata"]["key"]
    assert tile_key.startswith("GRANULE/")
    assert tile_key.endswith("/MTD_TL.xml")
    tile = ET.fromstring(listed[tile_key][1] or b"")  # noqa: S314 (a fixture's)
    same_tree(d["tile_metadata"]["document"], tile, tile_key)
    # the tile geocoding
    (geocoding,) = [g for c in tile if local(c.tag) == "Geometric_Info" for g in c if local(g.tag) == "Tile_Geocoding"]
    g = d["geocoding"]
    assert g["code"] == text(geocoding, "HORIZONTAL_CS_CODE")
    sizes = [
        [int(s.get("resolution", "")), int(text(s, "NROWS")), int(text(s, "NCOLS"))]
        for s in geocoding
        if local(s.tag) == "Size"
    ]
    assert g["sizes"] == sizes
    positions = {
        int(p.get("resolution", "")): [float(text(p, n)) for n in ("ULX", "ULY", "XDIM", "YDIM")]
        for p in geocoding
        if local(p.tag) == "Geoposition"
    }
    assert dict(g["positions"]) == {r: positions[r] for r, _, _ in sizes}
    # the band files, as the markers' reader and rasterio read them
    bands = band_files(path)
    assert [b["key"] for b in d["bands"]] == list(bands)
    requests = [
        {"path": str(path if file is None else path / file), "offset": start, "length": len(data), "windows": []}
        for file, start, data in bands.values()
    ]
    images = rasterio_does("read", requests)
    for i, (b, (file, start, data)) in enumerate(zip(d["bands"], bands.values(), strict=True)):
        at = f"{path.name}: {b['key']}"
        jp2 = read_jp2(data)
        image = images[f"image_{i}"]
        assert b["image_file"] == b["key"].removesuffix(".jp2"), at
        assert b["basename"] == b["image_file"].rsplit("/", 1)[-1], at
        assert b["base"] == (0 if file is not None else start), at
        (r,) = [r for r, rows, cols in sizes if (rows, cols) == (jp2.height, jp2.width)]
        assert (b["resolution"], b["name"]) == (r, band_name(b["basename"], r)), at
        cs = b["codestream"]
        assert (cs["height"], cs["width"]) == image.shape[1:] == (jp2.height, jp2.width), at
        assert cs["components"] == image.shape[0] == len(jp2.precisions), at
        assert (cs["tile_h"], cs["tile_w"]) == (jp2.tile_height, jp2.tile_width), at
        assert cs["precision"] == jp2.precisions[0], at
        assert image.dtype.itemsize == (1 if cs["precision"] <= 8 else 2), at
        assert [t[1] for t in cs["tiles"]] == [p[1] for p in jp2.tile_parts], at
    # the objects, as the listing or the central directory gives them
    ours = {o["key"]: o for o in d["objects"]}
    assert set(ours) == set(listed), path.name
    for key, (size, _) in listed.items():
        o = ours[key]
        assert o["size"] == size, key
        assert o["marker"] == (key.endswith(MARKER) and len(key) > len(MARKER)), key
        xml = key == "manifest.safe" or key.endswith((".xml", ".xsd"))
        held = (
            path.is_dir()
            and xml
            and size > 0
            and not o["marker"]
            and key not in {b["key"] for b in d["bands"]}
            and (size <= MAX_HELD_XML or key in (product, tile_key))
        )
        assert o["held"] == held, key


def test_the_description_holds_no_decision():
    """No member anywhere in a description is one of the output's decisions."""

    def keys(v: Any) -> set[str]:
        if isinstance(v, dict):
            return set(v) | {k for x in v.values() for k in keys(x)}
        if isinstance(v, list):
            return {k for x in v for k in keys(x)}
        return set()

    for path in INPUTS:
        with read(path) as parsed:
            d: Any = parsed.description
        # the XML trees' members are their own; the rest holds no decision
        d.pop("product_metadata")
        d.pop("tile_metadata")
        assert not keys(d) & DECISIONS, path.name
