"""The stored arrays of the SAFE fixtures (spec/stored-array.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>), compared with the
reference readers: rasterio (GDAL's JP2OpenJPEG driver), which reads a band file's image, in a
process of its own (rasterio_jp2.py says why); imagecodecs (OpenJPEG), which decodes a
codestream; and a reader of a JP2 file's boxes and its codestream's markers written here, which
gives the places. Of each product, in the directory form and in the zip form,

- the band files are the image files its product metadata lists under `IMG_DATA`, read here from
  the XML, and each has one description, keyed by its codestream: the contents of its `jp2c` box;
- its declarations are the SIZ marker segment, then the contents of the `jp2h` box;
- its shape and sample size are those of the image rasterio reads, a pixel's components last, and
  `axes` says that SIZ gives the width before the height;
- its grid is the tiles SIZ declares, cut at the image's edges;
- its chain is `jpeg2k_tile`, whose `header` is the codestream's bytes before its first SOT;
- its rows are the tile-parts the markers' reader walks, each from its SOT marker, at the
  coordinates its SOT's tile index gives;
- each row's bytes, decoded through the chain (the header, the tile-part and an EOC, decoded by
  imagecodecs, and the tile its SOT names cut out of the image), are the values rasterio reads in
  the tile's window.

rasterio reads a band file in blocks that are GDAL's choice, the codestream's tiles or the whole
image: the grid is SIZ's, and which blocks GDAL takes is counted.

A second test asserts which cases the fixtures reached, and a third compares band files no fixture
holds, written by rasterio. The parser describes no image with no grid: a band file it does not
walk to its end is refused with its product, which two more tests hold."""

import importlib.util
import io
import json
import math
import re
import shutil
import struct
import subprocess
import sys
import zipfile
from collections import Counter
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import imagecodecs
import jsonschema
import numpy as np
import pytest
from ir_helpers import FIXTURES, ROOT

from refkv.ir import Ir
from refkv.ir.kind import Kind
from refkv.ir.store import StoreIrOutput
from refkv.ir.stored import Each, ElementPlace, Place, Source, Stored, StoredArray
from refkv.zarrify import Rejected, _zarrify
from refkv.zarrify.common import Output

pa = pytest.importorskip("pyarrow")
# rasterio runs in a process of its own (rasterio_jp2.py), so it is looked for here, and not
# imported: where it is not installed, this module's tests are skipped, and with them those of
# test_safe_presented.py, which imports this module
if importlib.util.find_spec("rasterio") is None:
    pytest.skip("rasterio is not installed", allow_module_level=True)

INPUTS = sorted(p for p in (FIXTURES / "safe").iterdir() if "reject" not in p.name)
SCHEMA = json.loads((ROOT / "spec" / "stored-array.json").read_text())
URL = "https://data.test/safe/{}"
METADATA = ("MTD_MSIL1C.xml", "MTD_MSIL2A.xml")
RASTERIO = Path(__file__).with_name("rasterio_jp2.py")
SOC, SIZ, SOT, EOC = b"\xff\x4f", b"\xff\x51", b"\xff\x90", b"\xff\xd9"

type Range = tuple[str | None, int, int]
"""(object key, None in the zip form; offset; length): bytes of a file of the input"""


@dataclass(frozen=True)
class Product:
    """A product's IR, its descriptions and its chunk map's rows by array, with the profile's
    output; its files' bytes by key (the zip file's under None), and, in the directory form, each
    object's key by its element."""

    ir: Ir
    stored: Stored
    rows: dict[int, list[dict]]
    out: Output | StoreIrOutput
    keys: dict[int, str]
    files: dict[str | None, bytes]

    def where(self, element: int, within: tuple[int, int] | None = None) -> Range:
        """The bytes of an element's extent, or of a range of it."""
        start, length = self.ir.extent(element)
        if within is not None:
            start, length = start + within[0], within[1]
        if not self.keys:
            return None, start, length
        while self.ir.kind(element) != Kind.OBJECT:
            element = self.ir.parent(element)
        return self.keys[element], start, length

    def placed(self, place: Place) -> Range:
        """The bytes at a place: an element's, or a range of them."""
        assert isinstance(place, ElementPlace)
        return self.where(place.element, place.range)

    def held(self, at: Range) -> bytes:
        key, offset, length = at
        return self.files[key][offset:offset + length]

    def unit(self, row: dict) -> Range:
        """A row's unit: an element, whole."""
        assert row["element"] is not None
        assert (row["range"], row["ranges"], row["member"]) == (None, None, None)
        return self.where(row["element"])


@cache
def read_product(path: Path) -> Product:
    """The product at `path`, a `.SAFE` directory or a `.SAFE.zip` file, its descriptions'
    document checked against the schema."""
    _, out = _zarrify(str(path), url=URL.format(path.name + "/" * path.is_dir()))
    assert isinstance(out, (Output, StoreIrOutput))
    core = out.ir
    assert core is not None
    jsonschema.validate(json.loads(core.stored_arrays()), SCHEMA)
    ir = Ir(core)
    rows: dict[int, list[dict]] = {}
    for r in pa.record_batch(core.chunk_map()).to_pylist():
        rows.setdefault(r["array"], []).append(r)
    if path.is_dir():
        objects = [i for i in range(len(ir)) if ir.kind(i) == Kind.OBJECT]
        keys = {i: key for i, (key, _, _) in zip(objects, ir.object_bases(), strict=True)}
        files: dict[str | None, bytes] = {key: (path / key).read_bytes() for key in keys.values()}
    else:
        keys = {}
        files = {None: path.read_bytes()}
    return Product(ir, ir.stored_arrays(), rows, out, keys, files)


@dataclass(frozen=True)
class Jp2:
    """What a reader of the boxes and of the codestream's markers finds of a JP2 file, each place
    an (offset, length) of the file."""

    headers: tuple[tuple[int, int], ...]
    """the contents of each `jp2h` box"""
    codestream: tuple[int, int]
    """the contents of the `jp2c` box"""
    siz: tuple[int, int]
    """the SIZ marker segment, from its marker"""
    width: int
    height: int
    tile_width: int
    tile_height: int
    precisions: tuple[int, ...]
    """each component's bits"""
    first_sot: int
    """where the first tile-part starts: the main header ends there"""
    tile_parts: tuple[tuple[int, int, int], ...]
    """each tile-part, in the codestream's order: its offset, its length (Psot) and its tile's
    index (Isot)"""
    long_box: bool
    """whether the `jp2c` box's length is written in 8 bytes (XLBox)"""


def read_jp2(data: bytes) -> Jp2:
    """The boxes of a JP2 file, to its codestream, and the codestream's main header and
    tile-parts, by their markers (ISO/IEC 15444-1 Annexes A and I)."""
    at, headers = 0, []
    while True:
        length, kind = struct.unpack_from(">I4s", data, at)
        head = 8
        if length == 1:
            (length,), head = struct.unpack_from(">Q", data, at + 8), 16
        elif length == 0:
            length = len(data) - at
        if kind == b"jp2c":
            break
        if kind == b"jp2h":
            headers.append((at + head, length - head))
        at += length
    start, end = at + head, at + length
    assert data[start:start + 2] == SOC, "a codestream starts with SOC"
    assert data[start + 2:start + 4] == SIZ, "and SIZ follows"
    (lsiz,) = struct.unpack_from(">H", data, start + 4)
    # Rsiz, then Xsiz, Ysiz, XOsiz, YOsiz, XTsiz, YTsiz, XTOsiz, YTOsiz, then Csiz
    _, width, height, _, _, tile_width, tile_height, _, _, components = struct.unpack_from(">H8IH", data, start + 6)
    triples = [struct.unpack_from(">3B", data, start + 42 + 3 * c) for c in range(components)]
    assert all(not ssiz & 0x80 for ssiz, _, _ in triples), "unsigned components"
    at = start + 4 + lsiz
    while data[at:at + 2] != SOT:
        assert data[at] == 0xFF, "a marker"
        at += 2 + struct.unpack_from(">H", data, at + 2)[0]
    first_sot, parts = at, []
    while data[at:at + 2] == SOT:
        _, tile, psot, part, of = struct.unpack_from(">HHIBB", data, at + 2)
        assert (part, of) == (0, 1), "a tile-part a tile"
        parts.append((at, psot, tile))
        at += psot
    assert data[at:at + 2] == EOC, "EOC follows the last tile-part"
    assert at + 2 == end, "and ends the codestream"
    return Jp2(tuple(headers), (start, end - start), (start + 2, 2 + lsiz), width, height, tile_width, tile_height,
               tuple((ssiz & 0x7F) + 1 for ssiz, _, _ in triples), first_sot, tuple(parts), head == 16)


def members(path: Path) -> dict[str, tuple[int, int]]:
    """A `.SAFE.zip` file's stored members, by their keys in the product (their names under the
    `.SAFE` directory): where each one's bytes start in the file, and how many they are."""
    data = path.read_bytes()
    out = {}
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            if info.is_dir() or info.compress_type != zipfile.ZIP_STORED:
                continue
            name, extra = struct.unpack_from("<HH", data, info.header_offset + 26)
            out[info.filename.split("/", 1)[1]] = (info.header_offset + 30 + name + extra, info.file_size)
    return out


def band_files(path: Path) -> dict[str, tuple[str | None, int, bytes]]:
    """A product's band files, by key: the image files its product metadata lists under
    `IMG_DATA` (spec/zarrify/safe.md §2.3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#23-image-files>), each as the
    file of the input that holds it (None: the zip file), where it starts there, and its bytes."""
    if path.is_dir():
        (document,) = [(path / name).read_bytes() for name in METADATA if (path / name).is_file()]

        def held(key: str) -> tuple[str | None, int, bytes]:
            return key, 0, (path / key).read_bytes()
    else:
        data, stored = path.read_bytes(), members(path)
        with zipfile.ZipFile(path) as z:
            (document,) = [z.read(name) for name in z.namelist() if name.split("/", 1)[1] in METADATA]

        def held(key: str) -> tuple[str | None, int, bytes]:
            start, size = stored[key]
            return None, start, data[start:start + size]
    listed = [text.decode("ascii") for text in re.findall(rb"<IMAGE_FILE(?:_2A)?>([^<]*)</IMAGE_FILE", document)]
    keys = [f"{text}.jp2" for text in listed if text.split("/")[2] == "IMG_DATA"]
    assert len(set(keys)) == len(keys) >= 1
    return {key: held(key) for key in keys}


def rasterio_does(what: str, requests: list[dict]) -> dict[str, np.ndarray]:
    """What rasterio makes of `requests`, in a process of its own (rasterio_jp2.py): the images it
    reads, or the files it writes."""
    done = subprocess.run([sys.executable, str(RASTERIO), what], input=json.dumps(requests).encode(),
                          capture_output=True, check=True)
    with np.load(io.BytesIO(done.stdout)) as arrays:
        return dict(arrays)


@dataclass(frozen=True)
class Read:
    """A band file as rasterio reads it: the image whole, a pixel's components last, and in each
    tile's window, by the tile's row and column; and the shapes of the blocks GDAL reads it in."""

    image: np.ndarray
    windows: dict[tuple[int, int], np.ndarray]
    blocks: set[tuple[int, int]]


def tiling(jp2: Jp2) -> tuple[tuple[int, int], int, int]:
    """SIZ's tiles, cut to the image: their rows and columns, and how many lie down and across."""
    tile = (min(jp2.tile_height, jp2.height), min(jp2.tile_width, jp2.width))
    return tile, math.ceil(jp2.height / tile[0]), math.ceil(jp2.width / tile[1])


def rasterio_reads(path: Path, bands: dict[str, tuple[str | None, int, bytes]]) -> dict[str, Read]:
    """Each band file of the product at `path`, by key, as rasterio reads it."""
    requests, tiles = [], []
    for file, start, data in bands.values():
        jp2 = read_jp2(data)
        tile, down, across = tiling(jp2)
        tiles.append([(v, u) for v in range(down) for u in range(across)])
        # a tile's window: its column and row, then its width and height, cut at the image's edges
        windows = [[u * tile[1], v * tile[0], min(tile[1], jp2.width - u * tile[1]),
                    min(tile[0], jp2.height - v * tile[0])] for v, u in tiles[-1]]
        at = path if file is None else path / file
        requests.append({"path": str(at), "offset": start, "length": len(data), "windows": windows})
    got = rasterio_does("read", requests)
    about = json.loads(str(got["about"]))
    out = {}
    for i, key in enumerate(bands):
        assert about[i]["driver"] == "JP2OpenJPEG"

        def last(a: np.ndarray) -> np.ndarray:
            return np.moveaxis(a, 0, -1) if len(a) > 1 else a[0]

        out[key] = Read(last(got[f"image_{i}"]), {t: last(got[f"window_{i}_{j}"]) for j, t in enumerate(tiles[i])},
                        {(h, w) for h, w in about[i]["blocks"]})
    return out


def lengths(a: StoredArray) -> tuple[int, ...]:
    assert a.grid is not None
    assert all(isinstance(x, Each) for x in a.grid.axes)
    return tuple(x.length for x in a.grid.axes if isinstance(x, Each))


def decoded(header: bytes, unit: bytes, shape: tuple[int, ...]) -> np.ndarray:
    """What the codec `jpeg2k_tile` makes of a unit, as its entry says
    (spec/stored-array.md §3.2 <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#32-the-list>),
    from the unit's bytes, its `header` and the unit's shape alone: the main header, the tile's
    tile-parts and an EOC are a codestream of the whole image with the one tile, which OpenJPEG
    decodes; the tile is where its SOT's index and the header's SIZ put it, of the unit's size."""
    assert unit[:2] == SOT
    (tile,) = struct.unpack_from(">H", unit, 4)
    assert header[:4] == SOC + SIZ
    width, height, _, _, tile_width, tile_height = struct.unpack_from(">6I", header, 8)
    image = imagecodecs.jpeg2k_decode(header + unit + EOC)
    assert image.shape[:2] == (height, width)
    down, across = divmod(tile, math.ceil(width / tile_width))
    got = image[down * tile_height:(down + 1) * tile_height, across * tile_width:(across + 1) * tile_width]
    assert got.shape == shape, "the tile is of the unit's size"
    return got


def check_band(d: Product, k: int, at: tuple[str | None, int, bytes], read: Read, seen: Counter) -> list[str]:
    """One band file, held at `at` (the file of the input, its start there, its bytes) and read
    by rasterio as `read`, against its description `k`."""
    a = d.stored.arrays[k]
    file, start, data = at
    jp2 = read_jp2(data)

    def there(place: tuple[int, int]) -> Range:
        return file, start + place[0], place[1]

    said = []
    if d.where(a.element) != there(jp2.codestream) or a.path is None or not a.path.endswith("/codestream"):
        said.append(f"keyed by {a.path} at {d.where(a.element)}, not by the codestream at {there(jp2.codestream)}")
    if a.index is not None or a.fill != "undefined" or a.unit_parameters or a.pad_from is not None:
        said.append("an index, a fill value, unit parameters or pad_from")
    # the declarations: SIZ, then the JP2 header box's contents
    declared = [there(jp2.siz), *[there(h) for h in jp2.headers]]
    places = a.declarations
    if not all(isinstance(p, ElementPlace) for p in places) or [d.placed(p) for p in places] != declared:
        said.append("the declarations are not the SIZ segment and the jp2h box's contents")
    seen["declarations of a jp2h box"] += len(jp2.headers)
    seen["bands of a codestream box whose length is in 8 bytes"] += jp2.long_box

    # the image, as rasterio reads it: a pixel's components last
    theirs = read.image
    samples = len(jp2.precisions)
    shape = (jp2.height, jp2.width, *([samples] if samples > 1 else []))
    assert theirs.shape == shape, "rasterio reads an image of SIZ's size and components"
    # SIZ gives the width, then the height; the components' count is no extent of its list
    listed = ("width", "height", "components")
    axes = tuple(listed.index(x) for x in ("height", "width", "components")[:len(shape)])
    if (a.shape, a.axes) != (shape, axes):
        said.append(f"shape {a.shape} with axes {a.axes}; rasterio reads {shape}, and SIZ lists {listed}")
    bits = theirs.dtype.itemsize * 8
    assert len(set(jp2.precisions)) == 1
    assert bits == (8 if jp2.precisions[0] <= 8 else 16), "rasterio reads samples of whole bytes"
    if a.item_bits != bits:
        said.append(f"item_bits {a.item_bits}; rasterio reads {theirs.dtype}")
    seen[f"bands of {samples} {'component' if samples == 1 else 'components'}"] += 1
    seen[f"bands of {bits}-bit samples"] += 1
    seen[f"bands of precision {jp2.precisions[0]}"] += 1

    # the grid: SIZ's tiles, cut at the image's edges
    tile, down, across = tiling(jp2)
    if a.grid is None or (lengths(a), a.grid.edge) != ((*tile, *shape[2:]), "short"):
        return [*said, f"grid {a.grid}; SIZ's tiles are {tile}"]
    seen["bands of a tile larger than the image"] += (jp2.tile_height, jp2.tile_width) != tile
    # the blocks GDAL reads the image in are its own choice
    blocks = "their tiles" if read.blocks == {tile} else "another size"
    seen[f"bands GDAL reads in blocks of {blocks}"] += 1
    seen["bands of one tile" if down * across == 1 else "bands of several tiles"] += 1

    # the chain: the tile's codec, with the main header
    codecs = [c for chain in a.chains for c in d.stored.chains[chain]]
    header = codecs[0].parameters.get("header") if codecs else None
    if [(c.name, set(c.parameters)) for c in codecs] != [("jpeg2k_tile", {"header"})] or not isinstance(header, Source):
        return [*said, f"chain {codecs}"]
    main = (jp2.codestream[0], jp2.first_sot - jp2.codestream[0])
    if d.placed(header.place) != there(main):
        said.append(f"the header at {d.placed(header.place)}, not the codestream's bytes before its first SOT, "
                    f"{there(main)}")
    held = d.held(d.placed(header.place))

    # the rows: each tile-part the markers' reader walks, where its SOT's index puts it
    rows = {tuple(r["coords"]): r for r in d.rows.get(k, [])}
    if len(rows) != len(jp2.tile_parts) or len(rows) != down * across:
        said.append(f"{len(rows)} rows; {len(jp2.tile_parts)} tile-parts, of {down * across} tiles")
    for offset, length, index in jp2.tile_parts:
        v, u = divmod(index, across)
        coords = (v, u, *[0] * (len(shape) - 2))
        row = rows.get(coords)
        seen["tiles"] += 1
        if row is None or (row["chain"], row["skips"]) != (None, None) or d.unit(row) != there((offset, length)):
            said.append(f"tile {coords} at {None if row is None else d.unit(row)}, "
                        f"its tile-part {there((offset, length))}")
            continue
        window = read.windows[(v, u)]
        seen["tiles the image's edge cuts"] += window.shape[:2] != tile
        got = decoded(held, d.held(d.unit(row)), window.shape)
        if got.dtype.itemsize * 8 != a.item_bits:
            said.append(f"tile {coords} decodes to {got.dtype}, not samples of {a.item_bits} bits")
            continue
        if got.tobytes() != window.tobytes():
            said.append(f"tile {coords} decodes to other values than rasterio reads in its window")
        seen["tiles decoded"] += 1
    return said


@cache
def compared(path: Path) -> tuple[tuple[str, ...], Counter]:
    """What differs between the descriptions of the product at `path` and the reference readers,
    and what the comparison saw."""
    d = read_product(path)
    said: list[str] = []
    seen: Counter = Counter()
    if d.stored.undescribed:
        said.append("descriptions dropped for room")
    seen["products in the directory form" if path.is_dir() else "products in the zip form"] += 1
    bands = band_files(path)
    reads = rasterio_reads(path, bands)
    # each description's band file: the one that holds its key's bytes
    by_file: dict[str, list[int]] = {}
    for k, a in enumerate(d.stored.arrays):
        file, offset, _ = d.where(a.element)
        found = [key for key, (held, start, data) in bands.items()
                 if held == file and start <= offset < start + len(data)]
        if len(found) == 1:
            by_file.setdefault(found[0], []).append(k)
        else:
            said.append(f"{a.path} is in {len(found)} band files")
    for key, at in bands.items():
        seen["bands"] += 1
        described = by_file.get(key, [])
        if len(described) != 1:
            said.append(f"{key}: {len(described)} descriptions")
            continue
        said += [f"{key}: {s}" for s in check_band(d, described[0], at, reads[key], seen)]
    return tuple(said), seen


@pytest.mark.parametrize("path", INPUTS, ids=[p.name for p in INPUTS])
def test_safe_band_files_are_described_as_the_references_read_them(path):
    """Every band file of the product is described as the markers' reader and rasterio read it,
    and its tiles decode to the values rasterio reads."""
    said, seen = compared(path)
    assert said == ()
    assert seen["bands"] >= 1
    assert seen["tiles decoded"] == seen["tiles"]


def test_the_fixtures_hold_every_case_the_comparison_reads():
    """The fixtures reach each case of the comparison, so that none of it passes for want of a
    file. Every image of the fixtures is square, in several square tiles, of 8 or 15 bits: the
    written cases of the next test are the others."""
    seen: Counter = Counter()
    for path in INPUTS:
        seen += compared(path)[1]
    assert {k: v for k, v in seen.items() if v} == {
        "products in the directory form": 10,
        "products in the zip form": 4,
        "bands": 203,
        "declarations of a jp2h box": 203,
        "bands of 1 component": 186,
        "bands of 3 components": 17,
        "bands of 8-bit samples": 26,
        "bands of 16-bit samples": 177,
        "bands of precision 8": 26,
        "bands of precision 15": 177,
        "bands of several tiles": 203,
        "bands GDAL reads in blocks of their tiles": 60,
        "bands GDAL reads in blocks of another size": 143,
        "tiles": 2628,
        "tiles the image's edge cuts": 1229,
        "tiles decoded": 2628,
    }


def written(rows: int, columns: int, tile: tuple[int, int], *, samples: int = 1, bits: int = 8, layers: int = 1,
            precincts: str | None = None, seed: int = 0) -> dict:
    """The request of a JP2 file (rasterio_jp2.py) of `rows` by `columns` pixels of `samples`
    components of `bits` bits, in tiles of `tile` rows and columns, which may be more than the
    image's, in `layers` quality layers and precincts of the sizes `precincts` (None: of the
    largest, which a header does not list); three components go through the codestream's
    component transform."""
    return {"rows": rows, "columns": columns, "tile": tile, "samples": samples, "bits": bits, "layers": layers,
            "precincts": precincts, "seed": seed}


def with_component_coding(jp2: bytes) -> bytes:
    """A JP2 file whose main header gives the precinct sizes for each component, in a COC segment
    of its own, and none in its COD segment, whose precinct sizes they were: the same image,
    coded the same way."""
    read = read_jp2(jp2)
    start, components = read.codestream[0], len(read.precisions)
    at = read.siz[0] + read.siz[1]
    assert jp2[at:at + 2] == b"\xff\x52", "COD follows SIZ"
    (length,) = struct.unpack_from(">H", jp2, at + 2)
    # Scod, then the progression order, the layers and the component transform, then the levels,
    # the code-block sizes, style and transform, and a precinct size a resolution
    style, shared, coding = jp2[at + 4], jp2[at + 5:at + 9], jp2[at + 9:at + 2 + length]
    assert style == 1, "a COD segment with precinct sizes"
    cod = b"\xff\x52" + struct.pack(">HB", 12, 0) + shared + coding[:5]
    cocs = b"".join(b"\xff\x53" + struct.pack(">HBB", 4 + len(coding), c, 1) + coding for c in range(components))
    # the codestream's box runs to the end of the file, whatever its length
    return jp2[:start - 8] + struct.pack(">I4s", 0, b"jp2c") + jp2[start:at] + cod + cocs + jp2[at + 2 + length:]


def with_long_boxes(jp2: bytes) -> bytes:
    """A JP2 file with its `jp2h` and `jp2c` boxes' lengths written in 8 bytes, as XLBox."""
    at, out = 0, b""
    while at < len(jp2):
        length, kind = struct.unpack_from(">I4s", jp2, at)
        assert length >= 8 or (length == 0 and kind == b"jp2c"), "a box of a short header"
        length = length or len(jp2) - at
        box = jp2[at:at + length]
        out += struct.pack(">I4sQ", 1, kind, length + 8) + box[8:] if kind in (b"jp2h", b"jp2c") else box
        at += length
    return out


def write_product(to: Path, sizes: dict[int, tuple[int, int]], bands: dict[str, bytes]) -> Path:
    """A product at `to`, a directory: a fixture's, with the rows and columns of its tile
    geocoding's resolutions set to `sizes` and its four band files, by band name, to `bands`."""
    shutil.copytree(FIXTURES / "safe" / "safe_metadata_gaps", to)
    (tile_metadata,) = to.glob("GRANULE/*/MTD_TL.xml")
    text = tile_metadata.read_text()
    for resolution, (rows, columns) in sizes.items():
        was = re.search(rf'<Size resolution="{resolution}">\s*<NROWS>(\d+)</NROWS>\s*<NCOLS>(\d+)</NCOLS>', text)
        assert was is not None
        now = was[0].replace(f"<NROWS>{was[1]}<", f"<NROWS>{rows}<").replace(f"<NCOLS>{was[2]}<", f"<NCOLS>{columns}<")
        text = text.replace(was[0], now)
    tile_metadata.write_text(text)
    files = {p.stem.rsplit("_", 1)[1]: p for p in to.glob("GRANULE/*/IMG_DATA/*.jp2")}
    assert set(files) == set(bands)
    for name, data in bands.items():
        files[name].write_bytes(data)
    return to


def written_products(to: Path) -> tuple[Path, Path]:
    """Two products under `to`, of band files of the cases the fixtures lack, written by rasterio:
    an image that is not square, in tiles that are not; three components of 16 bits, through the
    codestream's component transform; one tile, of the image's size and declared larger than the
    image, across, down and both; precisions of 1, 5, 12 and 16 bits; two quality layers;
    precincts of the largest size, of sizes the COD segment gives and of sizes a COC segment gives
    each component; and boxes whose lengths are written in 8 bytes."""
    sizes = {10: (37, 50), 20: (20, 31), 60: (9, 13)}
    files = rasterio_does("write", [
        written(9, 13, (12, 16), bits=16, seed=1),
        written(37, 50, (16, 32), bits=12, precincts="{8,8},{4,8}", seed=2),
        written(37, 50, (32, 16), samples=3, bits=16, precincts="{8,8},{4,8}", seed=3),
        written(20, 31, (20, 31), bits=5, seed=4),
        written(9, 13, (16, 4), bits=8, seed=5),
        written(20, 31, (8, 8), bits=8, seed=6),
        written(37, 50, (8, 64), samples=3, bits=8, seed=7),
        written(20, 31, (16, 16), bits=1, layers=2, seed=8),
    ])
    made = [files[f"file_{i}"].tobytes() for i in range(8)]
    first = write_product(to / "first", sizes, {
        "B01": made[0], "B02": made[1], "B03": with_component_coding(made[2]), "B04": made[3]})
    second = write_product(to / "second", sizes, {
        "B01": made[4], "B02": with_long_boxes(made[5]), "B03": made[6], "B04": made[7]})
    return first, second


def test_band_files_no_fixture_holds_are_described_as_the_references_read_them(tmp_path):
    """The band files of the written products are described as the references read them."""
    seen: Counter = Counter()
    for path in written_products(tmp_path):
        said, saw = compared(path)
        assert said == (), path.name
        seen += saw
    assert {k: v for k, v in seen.items() if v} == {
        "products in the directory form": 2,
        "bands": 8,
        "declarations of a jp2h box": 8,
        "bands of a codestream box whose length is in 8 bytes": 1,
        "bands of 1 component": 6,
        "bands of 3 components": 2,
        "bands of 8-bit samples": 5,
        "bands of 16-bit samples": 3,
        "bands of precision 1": 1,
        "bands of precision 5": 1,
        "bands of precision 8": 3,
        "bands of precision 12": 1,
        "bands of precision 16": 2,
        "bands of a tile larger than the image": 3,
        "bands of one tile": 2,
        "bands of several tiles": 6,
        "bands GDAL reads in blocks of their tiles": 4,
        "bands GDAL reads in blocks of another size": 4,
        "tiles": 41,
        "tiles the image's edge cuts": 20,
        "tiles decoded": 41,
    }


def test_a_band_file_with_a_tlm_marker_is_refused_and_not_described_whole():
    """A codestream whose main header indexes its tile-parts with a TLM marker is one the parser
    does not walk: the product is refused, as before, and its image is not described as one unit
    in a `jpeg2k` chain."""
    with pytest.raises(Rejected, match="a band file's main header has a TLM marker"):
        read_product(FIXTURES / "safe" / "safe_reject_tlm")


def test_a_band_file_of_two_tile_parts_a_tile_is_refused_and_not_described_whole():
    """A codestream with a tile in two tile-parts is one the parser does not walk: the product is
    refused, as before, and no unit is described that the parser did not locate."""
    with pytest.raises(Rejected, match="a band file's tile 0 has more than one tile-part"):
        read_product(FIXTURES / "safe" / "safe_reject_two_tile_parts")
