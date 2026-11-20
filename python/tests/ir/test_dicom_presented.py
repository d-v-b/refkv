"""The DICOM profile's arrays against the stored arrays' descriptions (spec/stored-array.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): what the profile
writes of a file's Pixel Data and of its lookup tables, read off their descriptions by the
profile's rules (spec/zarrify/dicom.md §4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#4-output>
and §7.6 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#76-chunk-references>),
is what the profile wrote.

Of the Pixel Data's description the test derives the array's whole document and every chunk:

- the data type, from `item_bits` and the Pixel Representation among the declarations;
- the chunk shape, from the grid: its frames a unit, its rows and its columns, and for the
  samples 1 where each is a plane of native data, which the profile cuts a frame into, else 3;
- the codecs, from the chain and the Transfer Syntax UID among the declarations: `bytes` in that
  syntax's byte order for an empty chain, `packbits` for `lsb_first`, and the profile's codec of
  the transfer syntax for a codec that makes the samples or for `source`, after a `transpose`
  where a unit decodes to a pixel's samples together;
- each chunk, from a row: its unit's bytes; a third of them for each sample that is a plane of
  native data; and of a JPEG Baseline stream of three samples the profile's Adobe marker, by the
  Photometric Interpretation among the declarations, then the unit without its first two bytes.

Each lookup table's chunk is its row's bytes, and the `palette` array's document follows from the
three tables' descriptions.

What no description holds is read with pydicom from the dataset that declares the Pixel Data, as
the profile reads it: Samples per Pixel, Planar Configuration and whether there is a Number of
Frames, which say which axis of the description's shape is which; Frame Increment Pointer, which
names the frame axis; and SOP Class UID and the total pixel matrix's size, by which a whole-slide
image's frames are tiles. The JPEG 2000 frames of half-width chroma have no grid: their chunks
are held against the fragments pydicom finds. Nothing is taken from the output. The Pixel Data of
items are described and not presented."""

import json
import math
import struct
from collections import Counter
from functools import cache
from pathlib import Path

import pydicom
import pytest
from described import Described, described
from test_dicom_stored import (
    BIG,
    FILES,
    HTJ2K,
    LONG,
    LOSSLESS,
    NATIVE,
    ExplicitVRBigEndian,
    Image,
    file_parts,
    filled,
    fragments,
    image,
    paletted,
    saved,
)

from refkv.ir.stored import Each, StoredArray, Value
from refkv.zarrify.common import Output

WHOLE_SLIDE = "1.2.840.10008.5.1.4.1.1.77.1.6"
JPEG, RLE = "1.2.840.10008.1.2.4.50", "1.2.840.10008.1.2.5"
# the profile's codec of each transfer syntax whose frames a codec makes the samples of
IMAGECODECS = {JPEG: "jpeg", "1.2.840.10008.1.2.4.80": "jpegls", "1.2.840.10008.1.2.4.81": "jpegls",
               "1.2.840.10008.1.2.4.90": "jpeg2k", "1.2.840.10008.1.2.4.91": "jpeg2k", RLE: "dicomrle",
               **dict.fromkeys(LOSSLESS, "ljpeg"), **dict.fromkeys(HTJ2K, "htj2k")}
# the codecs of the IR's list a transfer syntax's frames may be in, the first where the list names them
CHAINS = {"jpeg": ("jpeg",), "ljpeg": ("jpeg", "source"), "jpegls": ("jpegls",), "jpeg2k": ("jpeg2k",),
          "dicomrle": ("dicom_rle",), "htj2k": ("source",)}
# SOI and an Adobe APP14 marker, less its last byte, the color transform
ADOBE = bytes.fromhex("ffd8ffee000e41646f626500640000000000")[:17]

type Part = tuple[int, int] | bytes
"""a part of a reference: (offset, length) of the file, or bytes the file does not hold there"""


def document(shape: list[int], data_type: str, chunk: list[int], codecs: list[dict], names: list[str]) -> dict:
    """An array's document, as the profile writes one."""
    return {
        "zarr_format": 3, "node_type": "array", "shape": shape, "data_type": data_type,
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": chunk}},
        "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
        "fill_value": False if data_type == "bool" else 0, "codecs": codecs, "dimension_names": names, "attributes": {},
    }


def same_json(a: object, b: object) -> bool:
    """Whether two documents are the same JSON: `false` is not 0, as Python has it."""
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def declared(d: Described, a: StoredArray) -> tuple[str, dict[int, bytes]]:
    """What a description's declarations hold: the Transfer Syntax UID, the first's value, and
    the value of each data element after it, by its tag, read in that syntax's encoding."""
    first, *rest = (d.place(p) for p in a.declarations)
    syntax = first[8:].rstrip(b"\0 ").decode()
    order = ">" if syntax == BIG else "<"
    values = {}
    for b in rest:
        group, element = struct.unpack_from(order + "HH", b)
        # an explicit VR whose length is 4 bytes, after 2 reserved ones
        wide = syntax != "1.2.840.10008.1.2" and b[4:6].decode() in LONG
        values[group << 16 | element] = b[12 if wide else 8:]
    return syntax, values


def bytes_codec(size: int, syntax: str) -> dict:
    endian = {"configuration": {"endian": "big" if syntax == BIG else "little"}} if size > 1 else {}
    return {"name": "bytes", **endian}


def check_pixels(d: Described, k: int, ds: pydicom.Dataset, out: Output, seen: Counter) -> list[str]:
    """The array `0` and its chunks, as the profile wrote them, against the Pixel Data's description."""
    a = d.stored.arrays[k]
    assert a.shape is not None
    assert a.item_bits is not None
    syntax, values = declared(d, a)
    order = ">" if syntax == BIG else "<"
    # which axis of the shape is which: the profile's reading of the dataset
    samples, frames_axis = int(ds.SamplesPerPixel), "NumberOfFrames" in ds
    planes = samples > 1 and (ds.PlanarConfiguration == 1 if syntax in NATIVE else syntax == RLE)
    shape = list(a.shape)
    n = shape.pop(0) if frames_axis else 1
    if samples > 1 and (shape.pop(0) if planes else shape.pop()) != samples:
        return [f"shape {a.shape} without the {samples} samples of a pixel where the dataset puts them"]
    rows, columns = shape
    whole_slide = ds.SOPClassUID == WHOLE_SLIDE
    stack = not whole_slide and n > 1
    timed = ds.get("FrameIncrementPointer") in (0x00181063, 0x00181065)
    names = [*(["c"] if samples == 3 else []), *(["t" if timed else "z"] if stack else []), "y", "x"]
    height, width = (ds.TotalPixelMatrixRows, ds.TotalPixelMatrixColumns) if whole_slide else (rows, columns)
    signed = struct.unpack_from(order + "H", values[0x00280103])[0] == 1
    data_type = "bool" if a.item_bits == 1 else f"{'' if signed else 'u'}int{a.item_bits}"
    size = math.ceil(a.item_bits / 8)
    # the chain: the profile's codec, after a transpose where a unit holds a pixel's samples together
    chain = [c.name for i in a.chains for c in d.stored.chains[i]]
    native = syntax in NATIVE
    if native:
        packbits = {"name": "packbits", "configuration": {"padding_encoding": "none"}}
        codec = packbits if a.item_bits == 1 else bytes_codec(size, syntax)
        want_chain = ["lsb_first"] if a.item_bits == 1 else []
    else:
        name = IMAGECODECS[syntax]
        configuration = {"dicomrle": {"dtype": f"{'|' if size == 1 else '<'}{'i' if signed else 'u'}{size}"},
                         "htj2k": {"planar": False} if samples == 3 else None}.get(name)
        codec = {"name": f"imagecodecs_{name}", **({"configuration": configuration} if configuration else {})}
        want_chain = chain if chain and chain[0] in CHAINS[name] else [CHAINS[name][0]]
    said = []
    if a.grid is not None and chain != want_chain:
        said.append(f"chain {chain} of the transfer syntax {syntax}")
    source = [c.parameters.get("id") for i in a.chains for c in d.stored.chains[i] if c.name == "source"]
    if source not in ([], [Value(syntax)]):
        said.append(f"source of {source}, not the transfer syntax {syntax}")
    together = 3 if samples == 3 and not (native and planes) else 1
    transpose = [{"name": "transpose", "configuration": {"order": [*range(1, len(names)), 0]}}]
    codecs = [*(transpose if samples == 3 and not planes else []), codec]
    # the frames of a unit, from the grid: one, or all of 1-bit frames that are one unit together
    unit = a.grid.axes[0] if a.grid is not None and frames_axis else Each(1)
    assert isinstance(unit, Each)
    chunk = [*([together] if samples == 3 else []), *([unit.length] if stack else []), rows, columns]
    full = [*([3] if samples == 3 else []), *([n] if stack else []), height, width]
    want_document = document(full, data_type, chunk, codecs, names)
    got_document = json.loads(out.bytes_entries["0/zarr.json"])
    if same_json(got_document, want_document) is False:
        said.append(f"the profile's array {got_document}, by the description {want_document}")
    # the chunks: a unit each, or a plane of one
    if a.grid is None:
        # no grid: the frames' places are not described
        units = fragments(Image(ds, 0, syntax, d.objects[None]))
        seen["arrays of no grid, their chunks held against pydicom's fragments"] += 1
    else:
        units = [file_parts(d, row) for row in d.rows.get(k, [])]
    across = math.ceil(width / columns)
    want: dict[str, list[Part]] = {}
    for f, spans in enumerate(units):
        tile = [f // across, f % across] if whole_slide else [0, 0]
        seen["chunks of a tile"] += whole_slide
        if native and planes:
            ((offset, length),) = spans
            third = length // samples
            for c in range(samples):
                want["/".join(map(str, ["0/c", c, *([f] if stack else []), *tile]))] = [(offset + c * third, third)]
                seen["chunks of a plane of a frame"] += 1
            continue
        parts: list[Part] = list(spans)
        if syntax == JPEG and samples == 3:
            # the stream made explicit about its color transform: 0 for RGB, else 1
            (offset, length), *rest = spans
            transform = 0 if values[0x00280004].rstrip(b" \0") == b"RGB" else 1
            parts = [ADOBE + bytes([transform]), (offset + 2, length - 2), *rest]
            seen["chunks with the profile's JPEG marker"] += 1
        want["/".join(map(str, ["0/c", *([0] if samples == 3 else []), *([f] if stack else []), *tile]))] = parts
        seen["chunks of a unit"] += 1
        seen["chunks of several ranges"] += len(spans) > 1
        seen["chunks of every frame together"] += unit.length > 1
    got = {key: list(parts) for key, parts in out.refs.items() if key.startswith("0/c/")}
    if got != want:
        differing = sorted(key for key in got.keys() | want.keys() if got.get(key) != want.get(key))
        said.append(f"chunks {differing[:3]}: the profile's {[got.get(key) for key in differing[:3]]}, "
                    f"by the description {[want.get(key) for key in differing[:3]]}")
    seen["arrays"] += 1
    seen["arrays with a transpose"] += len(codecs) > 1
    seen["arrays of a whole-slide image"] += whole_slide
    seen[f"arrays of {codec['name']}"] += 1
    return said


def check_palette(d: Described, ks: list[int], out: Output, seen: Counter) -> list[str]:
    """The array `palette` and its chunks, as the profile wrote them, against the three lookup
    tables' descriptions."""
    tables = [d.stored.arrays[k] for k in ks]
    first = tables[0]
    assert first.shape is not None
    assert first.item_bits is not None
    if any((t.shape, t.item_bits) != (first.shape, first.item_bits) for t in tables):
        return ["lookup tables of unlike entries"]
    syntax, _ = declared(d, first)
    (entries,), size = first.shape, first.item_bits // 8
    codecs = [bytes_codec(size, syntax)]
    want_document = document([3, entries], f"uint{first.item_bits}", [1, entries], codecs, ["c", "i"])
    said = []
    got_document = json.loads(out.bytes_entries.get("palette/zarr.json", b"null"))
    if same_json(got_document, want_document) is False:
        said.append(f"the profile's palette {got_document}, by the descriptions {want_document}")
    want = {f"palette/c/{c}/0": [p for row in d.rows.get(k, []) for p in file_parts(d, row)] for c, k in enumerate(ks)}
    got = {key: list(parts) for key, parts in out.refs.items() if key.startswith("palette/c/")}
    if got != want:
        said.append(f"the profile's palette chunks {got}, by the descriptions {want}")
    seen["palettes"] += 1
    seen["palettes of two bytes an entry"] += size == 2
    return said


@cache
def compared(path: Path) -> tuple[tuple[str, ...], Counter]:
    """What differs between the arrays the profile wrote of the file at `path` and the file's
    descriptions, and what the comparison saw."""
    d = described(path)
    out = d.out
    assert isinstance(out, Output), "a DICOM file is one file"
    ds = pydicom.dcmread(path)
    said: list[str] = []
    seen: Counter = Counter()
    by_path: dict[str, list[int]] = {}
    for k, a in enumerate(d.stored.arrays):
        assert a.path is not None
        by_path.setdefault(a.path, []).append(k)
    (k,) = by_path.pop("dataset/7FE00010")
    said += check_pixels(d, k, ds, out, seen)
    tables = [by_path.pop(f"dataset/{0x00281201 + c:08X}", []) for c in range(3)]
    if any(tables):
        said += check_palette(d, [k for ks in tables for k in ks], out, seen)
    elif "palette/zarr.json" in out.bytes_entries:
        said.append("a palette array of no described lookup table")
    paletted = ds.PhotometricInterpretation == "PALETTE COLOR"
    seen["palettes the profile leaves in the mirror"] += paletted and not any(tables)
    seen["described arrays the profile writes none of"] += sum(len(ks) for ks in by_path.values())
    return tuple(said), seen


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_the_dicom_profile_writes_what_the_descriptions_give(path):
    """The arrays the profile writes of the file are what its descriptions give."""
    said, seen = compared(path)
    assert said == ()
    assert seen["arrays"] == 1


def test_the_fixtures_hold_every_case_the_profile_is_compared_in():
    """The fixtures reach each case of the comparison, so that none of it passes for want of a
    file. The arrays the profile writes none of are the Pixel Data of an Icon Image Sequence's
    two items; the palette it leaves in the mirror has a segmented table."""
    seen: Counter = Counter()
    for path in FILES:
        seen += compared(path)[1]
    assert {k: v for k, v in seen.items() if v} == {
        "arrays": 77,
        "arrays of bytes": 35,
        "arrays of packbits": 5,
        "arrays of imagecodecs_jpeg": 10,
        "arrays of imagecodecs_ljpeg": 4,
        "arrays of imagecodecs_jpegls": 5,
        "arrays of imagecodecs_jpeg2k": 5,
        "arrays of imagecodecs_htj2k": 5,
        "arrays of imagecodecs_dicomrle": 8,
        "arrays with a transpose": 15,
        "arrays of a whole-slide image": 2,
        "arrays of no grid, their chunks held against pydicom's fragments": 1,
        "chunks of a unit": 217,
        "chunks of a plane of a frame": 15,
        "chunks of a tile": 10,
        "chunks of several ranges": 10,
        "chunks of every frame together": 1,
        "chunks with the profile's JPEG marker": 13,
        "palettes": 4,
        "palettes of two bytes an entry": 3,
        "palettes the profile leaves in the mirror": 1,
        "described arrays the profile writes none of": 2,
    }


def test_the_profile_writes_what_the_descriptions_give_of_written_files(tmp_path):
    """The cases no fixture holds, written: planes of native samples of 16 bits in big endian,
    whose chunks are a third of a frame each, and a lookup table longer than its entries, whose
    chunk is the entries alone."""
    planes = saved(tmp_path / "planes.dcm", filled(image(2, 3, samples=3, bits=16, frames=2, planar=1), "OW"),
                   ExplicitVRBigEndian)
    palette = saved(tmp_path / "palette.dcm", paletted([5, 0, 8], 6))
    seen: Counter = Counter()
    for path in (planes, palette):
        said, counts = compared(path)
        assert said == (), path.name
        seen += counts
    assert {k: v for k, v in seen.items() if v} == {
        "arrays": 2, "arrays of bytes": 2, "chunks of a unit": 1, "chunks of a plane of a frame": 6, "palettes": 1}
    # 5 of the table's 6 bytes
    chunks = [described(palette).out.refs[f"palette/c/{c}/0"] for c in range(3)]
    assert [[p[-1] for p in parts if isinstance(p, tuple)] for parts in chunks] == [[5], [5], [5]]


@pytest.mark.xfail(strict=True, reason="the profile presents a lookup table of one-byte entries in big-endian 16-bit "
                                       "words in the file's order, which the description gives no grid")
def test_the_profile_presents_no_lookup_table_the_description_gives_no_grid(tmp_path):
    """Under Explicit VR Big Endian, entries of one byte in a table of VR OW are in 16-bit words
    that the syntax swaps, so the description gives the table no grid; the profile writes its
    bytes as they lie, as pydicom's palette reading takes them."""
    path = saved(tmp_path / "words.dcm", paletted([4, 0, 8], 4), ExplicitVRBigEndian)
    d = described(path)
    assert [a.grid for a in d.stored.arrays if a.path != "dataset/7FE00010"] == [None, None, None]
    assert not [key for key in d.out.refs if key.startswith("palette/c/")]
