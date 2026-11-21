"""The description of every DICOM fixture (spec/zarrify/dicom.md §7.8
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#78-the-description>), compared with
pydicom, the reference reader: the transfer syntax and its encoding; each attribute the profile
reads of the image, as pydicom reads it (a Pixel Spacing or Spacing Between Slices from the Shared
Functional Groups' Pixel Measures first, a decimal string that is no finite number as null); a
palette's descriptors and where pydicom finds its tables; the top-level Pixel Data's place and VR;
and its structure: native frames' size and count, or the fragments of each encapsulated frame where
`pydicom.encaps` finds them, the Basic Offset Table's presence and an Extended Offset Table's frames.
Each description is valid by its JSON Schema, and holds none of the output's decisions."""

import json
import math
import struct
from pathlib import Path
from typing import Any

import jsonschema
import pydicom
import pytest
from ir_helpers import FIXTURES, ROOT
from pydicom.dataset import Dataset
from pydicom.multival import MultiValue
from pydicom.uid import UID
from test_dicom_stored import NATIVE, PIXEL_DATA, Image, fragments, held

from refkv.build import read

FILES = sorted(p for p in (FIXTURES / "dicom").glob("*.dcm") if "reject" not in p.name)
SCHEMA = json.loads((ROOT / "spec" / "zarrify" / "dicom" / "description.json").read_text())
# members a description never has: the output's decisions (spec/zarrify/dicom.md §7.8
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#78-the-description>)
DECISIONS = {"axes", "shape", "chunk_shape", "data_type", "scale", "units", "window", "whole_slide", "across",
             "frame_axis", "adobe", "root", "declared_by", "nested", "layout"}
# the attributes the description reads as integers (the first value of a US or UL), by keyword
INTEGERS = {
    "samples_per_pixel": "SamplesPerPixel", "planar_configuration": "PlanarConfiguration", "rows": "Rows",
    "columns": "Columns", "bits_allocated": "BitsAllocated", "bits_stored": "BitsStored", "high_bit": "HighBit",
    "pixel_representation": "PixelRepresentation", "total_pixel_matrix_columns": "TotalPixelMatrixColumns",
    "total_pixel_matrix_rows": "TotalPixelMatrixRows", "number_of_optical_paths": "NumberOfOpticalPaths",
    "total_pixel_matrix_focal_planes": "TotalPixelMatrixFocalPlanes",
}
TEXTS = {"sop_class_uid": "SOPClassUID", "dimension_organization_type": "DimensionOrganizationType",
         "photometric_interpretation": "PhotometricInterpretation"}
DECIMALS = {"window_center": "WindowCenter", "window_width": "WindowWidth", "rescale_intercept": "RescaleIntercept",
            "rescale_slope": "RescaleSlope", "frame_time": "FrameTime"}
MEASURES = {"pixel_spacing": "PixelSpacing", "spacing_between_slices": "SpacingBetweenSlices"}


def decimals(value: Any) -> list[float | None] | None:
    """A decimal string's values as the description gives them: a value that is no finite number is
    null; None for no attribute."""
    if value is None:
        return None
    out: list[float | None] = []
    for v in value if isinstance(value, (list, MultiValue)) else [value]:
        try:
            x = float(v)
        except (TypeError, ValueError):
            out.append(None)
            continue
        out.append(x if math.isfinite(x) else None)
    return out


def measured(ds: Dataset, keyword: str) -> Any:
    """An attribute of the Shared Functional Groups' first Pixel Measures item, else the dataset's."""
    try:
        item = ds.SharedFunctionalGroupsSequence[0].PixelMeasuresSequence[0]
    except (AttributeError, IndexError):
        item = None
    if item is not None and keyword in item:
        return item.get(keyword)
    return ds.get(keyword)


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_the_description_is_pydicoms_reading(path: Path):
    ds = pydicom.dcmread(path)
    data = path.read_bytes()
    syntax = str(ds.file_meta.TransferSyntaxUID)
    im = Image(ds, 0, syntax, data)
    with read(path) as parsed:
        d: Any = parsed.description
        jsonschema.validate(d, SCHEMA)
        assert not DECISIONS & set(d), DECISIONS & set(d)
        assert d["transfer_syntax"] == syntax
        assert (d["explicit"], d["little"]) == (not UID(syntax).is_implicit_VR, UID(syntax).is_little_endian)
        assert (d["compression"] == "native") == (syntax in NATIVE)
        a = d["attributes"]
        for ours, keyword in INTEGERS.items():
            assert a[ours] == im.number(keyword), ours
        for ours, keyword in TEXTS.items():
            value = ds.get(keyword)
            assert a[ours] == (None if value is None else str(value)), ours
        for ours, keyword in DECIMALS.items():
            assert a[ours] == decimals(ds.get(keyword)), ours
        for ours, keyword in MEASURES.items():
            assert a[ours] == decimals(measured(ds, keyword)), ours
        assert a["number_of_frames"] == im.frames
        pointer = ds.get("FrameIncrementPointer")
        assert a["frame_increment_pointer"] == (None if pointer is None else f"{int(pointer):08X}")
        # a palette: its descriptors, and where pydicom finds each table's data
        if d["palette"] is not None:
            p = d["palette"]
            entries, first, bits = ds.RedPaletteColorLookupTableDescriptor
            assert (p["entries"], p["first_mapped"], p["bits"]) == (entries or 65536, first, bits)
            tables = [held(ds, 0x00281201 + c, 0, data) for c in range(3)]
            assert p["tables"] == [t.value for t in tables if t is not None]
        else:
            # a segmented palette's tables are commands, which the description does not read
            segmented = any(0x00281221 + c in ds for c in range(3))
            assert ds.get("PhotometricInterpretation") != "PALETTE COLOR" or segmented
        # the top-level Pixel Data, where pydicom read it
        px = d["pixel_data"]
        place = held(ds, PIXEL_DATA, 0, data)
        assert place is not None
        assert (px["offset"], px["length"]) == (place.value, 0xFFFFFFFF if place.length is None else place.length)
        assert px["vr"] == (ds["PixelData"].VR if d["explicit"] else None)
        assert px["encapsulated"] == (place.length is None)
        structure = px["structure"]
        if not px["encapsulated"]:
            # native frames end to end: pydicom's unit of them
            _, size, units = im.cut()
            assert (structure["frame_size"], structure["stored"]) == (size, units)
            assert place.length is not None
            assert px["end"] == place.value + place.length
            # the bytes past the frames, but one zero byte that pads the value to an even length
            used = size * units
            left = place.length - used
            padding = left == 0 or (left == 1 and data[place.value + used] == 0)
            extra = structure["extra"]
            assert (extra is None) == padding
            if extra is not None:
                assert (extra["offset"], extra["bytes"]) == (place.value + used, left)
        else:
            buffer = ds.PixelData
            assert structure["offset_table"] == (struct.unpack_from("<I", buffer, 4)[0] > 0)
            theirs = fragments(im)
            # the sequence's delimiter, (FFFE,E0DD), and Pixel Data's end after it
            delimiter = structure["delimiter"]
            assert data[delimiter:delimiter + 4] == b"\xfe\xff\xdd\xe0"
            assert px["end"] == delimiter + 8
            eot = structure["extended_offset_table"]
            if eot is not None:
                assert [[s] for s in map(tuple, eot["frames"])] == theirs
                # whether each frame's item header, (FFFE,E000) and its length, precedes its data
                item = [data[o - 8:o - 4] == b"\xfe\xff\x00\xe0" and int.from_bytes(data[o - 4:o], "little") == n
                        for o, n in eot["frames"]]
                assert eot["headers"] == all(item)
            else:
                items, bounds = structure["items"], structure["bounds"]
                assert items[0][0] == place.value
                ours = [[(o, n) for _, o, n in items[1 + bounds[f]:1 + bounds[f + 1]] if n]
                        for f in range(len(bounds) - 1)]
                assert ours == theirs
        assert parsed.ir.path(d["elements"]["pixel_data"]) == "dataset/7FE00010"
