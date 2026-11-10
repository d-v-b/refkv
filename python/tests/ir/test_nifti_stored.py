"""The stored arrays of the NIfTI fixtures (spec/stored-array.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>), compared with
nibabel, the reference reader: a file's one description against the shape, the voxel size and the
voxel offset nibabel reads of its header; its row against the bytes nibabel reads the voxels from;
and those bytes, decoded through the description's chain, against the raw stored values nibabel
reads there, unscaled (scaling is the profile's).

Three kinds of file are compared: a single file, whose key and declaration are its `header` and
whose unit is its `voxels`; a header-and-image pair, whose unit is in the image file; and a file
gzipped whole, whose unit is the whole stream, in a chain that inflates it and passes over the
bytes before the voxels.

nibabel refuses three fixtures the profile accepts: one whose intercept is no number, and two whose
extension chain it does not read. Of those its header class reads the header, and its array reader
the voxels at the header's offset.

A second test asserts which cases the fixtures reached, and a third compares files no fixture
holds."""

import gzip
import struct
from collections import Counter
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import nibabel as nib
import numpy as np
import pytest
from described import Described, described
from ir_helpers import FIXTURES
from nibabel.spatialimages import HeaderDataError
from nibabel.volumeutils import array_from_file

from refkv._narrow import integer
from refkv.ir.stored import Codec, Each, ElementPlace, Grid, Value

FILES = sorted(p for p in (FIXTURES / "nifti").iterdir()
               if "reject" not in p.name and p.name.lower().endswith((".nii", ".nii.gz", ".hdr")))


@dataclass(frozen=True)
class Read:
    """What nibabel reads of a file: its header; the voxels' raw values, in the header's order of
    the dimensions; where the voxels start, in the file that holds them; and whether nibabel read
    the file as an image, or its header class and its array reader did."""

    header: Any  # nibabel's header classes are not annotated
    values: np.ndarray
    offset: int
    data: Path
    image: bool


def read(path: Path) -> Read:
    """The file at `path` as nibabel reads it."""
    try:
        image: Any = nib.load(path)
        values = np.asarray(image.dataobj.get_unscaled())
        return Read(image.header, values, int(image.dataobj.offset), Path(image.dataobj.file_like), image=True)
    except (HeaderDataError, ValueError):
        # an image nibabel refuses, for its scaling or its extensions: its header, and the voxels there
        held = path.read_bytes()
        kind: Any = next(k for k in (nib.Nifti1Header, nib.Nifti2Header) if k.may_contain_header(held[:k.sizeof_hdr]))
        header = kind(held[:kind.sizeof_hdr], check=False)
        offset = int(header.get_data_offset())
        with path.open("rb") as f:
            values = array_from_file(header.get_data_shape(), header.get_data_dtype(), f, offset, order="F")
        return Read(header, np.asarray(values), offset, path, image=False)


def decoded(chain: list[Codec], unit: bytes) -> bytes:
    """A unit's bytes decoded through its chain, from its last codec to its first."""
    for codec in reversed(chain):
        if codec.name == "gzip":
            unit = gzip.decompress(unit)
        elif codec.name == "skip":
            length = codec.parameters["length"]
            assert isinstance(length, Value)
            unit = unit[integer(length.value):]
        else:
            raise AssertionError(f"a codec {codec.name} the test does not decode")
    return unit


def check(d: Described, path: Path, r: Read, seen: Counter) -> list[str]:
    """The file's description against what nibabel reads of it."""
    if len(d.stored.arrays) != 1:
        return [f"{len(d.stored.arrays)} descriptions of one image"]
    a = d.stored.arrays[0]
    held = path.read_bytes()
    gzipped, pair = path.name.endswith(".gz"), r.data != path
    dims: tuple[int, ...] = tuple(int(s) for s in r.header.get_data_shape())
    dtype: np.dtype = r.header.get_data_dtype()
    length: int = type(r.header).sizeof_hdr
    n = len(dims)
    said = []
    if a.index is not None or a.fill != "undefined" or a.pad_from is not None or a.unit_parameters:
        said.append("an index, a fill value, a padding or unit parameters")
    # the header lists the dimensions the fastest-varying first, and nibabel gives them in its order
    if a.shape != dims[::-1] or a.axes != (tuple(range(n))[::-1] if n >= 2 else None):
        said.append(f"shape {a.shape} along {a.axes}, nibabel {dims}")
    if a.item_bits != dtype.itemsize * 8:
        said.append(f"item_bits {a.item_bits}, nibabel's voxels {dtype}")
    if a.grid != Grid(tuple(Each(s) for s in dims[::-1]), "short"):
        said.append(f"grid {a.grid}, not one unit of {dims[::-1]}")
    chain = [c for k in a.chains for c in d.stored.chains[k]]
    codecs = [(c.name, dict(c.parameters)) for c in chain]
    if gzipped:
        # keyed by the stream, which holds the header where no element does
        if (a.path, d.at(a.element), a.declarations) != ("voxels", (None, 0, len(held)), ()):
            said.append(f"keyed by {a.path} at {d.at(a.element)}, with {len(a.declarations)} declarations")
        if len(a.chains) != 1 or codecs != [("skip", {"length": Value(r.offset)}), ("gzip", {})]:
            said.append(f"chain {codecs}, nibabel's voxels {r.offset} bytes into the stream")
        parts = [(None, 0, len(held))]
    else:
        key = path.name if pair else None
        if (a.path, d.at(a.element)) != (f"{key}/header" if pair else "header", (key, 0, length)):
            said.append(f"keyed by {a.path} at {d.at(a.element)}, not the header's {length} bytes")
        if a.declarations != (ElementPlace(a.element, None, None),) or d.place(a.declarations[0]) != held[:length]:
            said.append("the declaration is not the header")
        else:
            # what nibabel reads of the file, it reads of the declaration's bytes
            declared = type(r.header)(d.place(a.declarations[0]), check=False)
            read_there = (declared.get_data_shape(), declared.get_data_dtype(), declared.get_data_offset())
            if read_there != (dims, dtype, r.offset):
                said.append(f"nibabel reads {read_there} of the declaration")
        if len(a.chains) != 1 or codecs:
            said.append(f"chain {codecs} of voxels stored as they are")
        parts = [(r.data.name if pair else None, r.offset, r.values.nbytes)]
    rows = d.rows.get(0, [])
    if len(rows) != 1:
        return [*said, f"{len(rows)} rows of one unit"]
    (row,) = rows
    if row["coords"] != [0] * n or row["chain"] is not None or row["skips"] is not None:
        said.append(f"a row at {row['coords']} of chain {row['chain']}, skipping {row['skips']}")
    if d.parts(row) != parts:
        said.append(f"the unit at {d.parts(row)}, nibabel reads {parts}")
        return said
    # the values in storage order, as the file holds them
    theirs = np.ascontiguousarray(r.values.transpose(tuple(range(n))[::-1])).tobytes()
    if decoded(chain, d.unit(row)) != theirs:
        said.append("the unit decodes to other bytes than the values nibabel reads")
    seen["units decoded"] += 1
    seen["single files"] += not gzipped and not pair
    seen["gzipped files"] += gzipped
    seen["pairs"] += pair
    seen["NIfTI-2 headers"] += length == 540
    seen["big-endian files"] += r.header.endianness == ">"
    seen[f"arrays of rank {n}"] += 1
    seen["arrays of color voxels"] += dtype.names is not None
    seen["arrays of complex voxels"] += dtype.kind == "c"
    seen["voxels after more than the header and its extender"] += not pair and r.offset > length + 4
    seen["voxels at an offset of a pair's image file"] += pair and r.offset > 0
    seen["images nibabel refuses, read by its header"] += not r.image
    return said


@cache
def compared(path: Path) -> tuple[tuple[str, ...], Counter]:
    """What differs between the description of the file at `path` and nibabel, and what the
    comparison saw."""
    d = described(path)
    seen: Counter = Counter(files=1)
    said = ["descriptions dropped for room"] if d.stored.undescribed else []
    said += check(d, path, read(path), seen)
    return tuple(said), seen


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_nifti_voxels_are_described_as_nibabel_reads_them(path):
    """The file's voxels are described as nibabel reads them, and their unit decodes to the raw
    values nibabel reads."""
    said, seen = compared(path)
    assert said == ()
    assert seen["units decoded"] == 1


def test_the_fixtures_hold_every_case_the_comparison_reads():
    """The fixtures reach each case of the comparison, so that none of it passes for want of a
    file. No fixture's array has 6 axes; a file of them is among those the next test writes."""
    seen: Counter = Counter()
    for path in FILES:
        seen += compared(path)[1]
    assert {k: v for k, v in seen.items() if v} == {
        "files": 84,
        "units decoded": 84,
        "single files": 70,
        "gzipped files": 5,
        "pairs": 9,
        "NIfTI-2 headers": 18,
        "big-endian files": 17,
        "arrays of rank 1": 2,
        "arrays of rank 2": 6,
        "arrays of rank 3": 58,
        "arrays of rank 4": 11,
        "arrays of rank 5": 6,
        "arrays of rank 7": 1,
        "arrays of color voxels": 8,
        "arrays of complex voxels": 6,
        "voxels after more than the header and its extender": 18,
        "voxels at an offset of a pair's image file": 1,
        "images nibabel refuses, read by its header": 3,
    }


def header(version: int, order: str, datatype: tuple[int, int], dims: tuple[int, ...], vox: int, magic: bytes) -> bytes:
    """A NIfTI header of `version`, in the byte order `order` (`<` or `>`): its `datatype` and
    `bitpix`, its dimensions, its `vox_offset` and its magic's three characters, with every
    `pixdim` 1 and no scaling."""
    dim = [len(dims), *dims, *[1] * (7 - len(dims))]
    if version == 1:
        h = bytearray(348)
        struct.pack_into(f"{order}i", h, 0, 348)
        struct.pack_into(f"{order}8h", h, 40, *dim)
        struct.pack_into(f"{order}hh", h, 70, *datatype)
        struct.pack_into(f"{order}8f", h, 76, *[1.0] * 8)
        struct.pack_into(f"{order}f", h, 108, float(vox))
        h[344:348] = magic + b"\0"
    else:
        h = bytearray(540)
        struct.pack_into(f"{order}i", h, 0, 540)
        h[4:12] = magic + b"\0\r\n\x1a\n"
        struct.pack_into(f"{order}hh", h, 12, *datatype)
        struct.pack_into(f"{order}8q", h, 16, *dim)
        struct.pack_into(f"{order}8d", h, 104, *[1.0] * 8)
        struct.pack_into(f"{order}q", h, 168, vox)
    return bytes(h)


def voxels(count: int) -> bytes:
    """`count` bytes of voxels, no two runs of them alike."""
    at = np.arange(count, dtype=np.uint64)
    return (7 * at + at // 251).astype(np.uint8).tobytes()


def test_files_no_fixture_holds_are_described_as_nibabel_reads_them(tmp_path):
    """Files the fixtures lack, each in a directory of its own: an array of 6 axes, with bytes
    after its voxels; a gzipped NIfTI-2 file of one axis, its voxels after padding; a gzipped file
    of RGBA32 voxels, big-endian; a pair whose header has 7 dimensions and no extender, its voxels
    between other bytes of the image file; and a NIfTI-2 pair of big-endian COMPLEX128 voxels."""
    float64, uint64, rgba32, int8, complex128 = (64, 64), (1280, 64), (2304, 32), (256, 8), (1792, 128)
    files = {
        "six/six.nii": header(1, ">", float64, (2, 3, 1, 2, 2, 1), 352, b"n+1") + bytes(4) + voxels(192) + b"after",
        "one/one.nii.gz": gzip.compress(header(2, "<", uint64, (5,), 560, b"n+2") + bytes(20) + voxels(40), mtime=0),
        "rgba/rgba.nii.gz": gzip.compress(header(1, ">", rgba32, (3, 2), 352, b"n+1") + bytes(4) + voxels(24), mtime=0),
        "seven/seven.hdr": header(1, ">", int8, (2, 1, 2, 1, 1, 1, 1), 32, b"ni1"),
        "seven/seven.img": bytes(32) + voxels(4) + b"after",
        "wide/wide.hdr": header(2, ">", complex128, (2, 2, 1, 3), 0, b"ni2") + bytes(4),
        "wide/wide.img": voxels(192),
    }
    for name, held in files.items():
        (tmp_path / name).parent.mkdir(exist_ok=True)
        (tmp_path / name).write_bytes(held)
    seen: Counter = Counter()
    for name in files:
        if name.endswith(".img"):
            continue
        said, saw = compared(tmp_path / name)
        assert said == (), name
        seen += saw
    assert {k: v for k, v in seen.items() if v} == {
        "files": 5,
        "units decoded": 5,
        "single files": 1,
        "gzipped files": 2,
        "pairs": 2,
        "NIfTI-2 headers": 2,
        "big-endian files": 4,
        "arrays of rank 1": 1,
        "arrays of rank 2": 1,
        "arrays of rank 4": 1,
        "arrays of rank 6": 1,
        "arrays of rank 7": 1,
        "arrays of color voxels": 1,
        "arrays of complex voxels": 1,
        "voxels after more than the header and its extender": 1,
        "voxels at an offset of a pair's image file": 1,
    }
