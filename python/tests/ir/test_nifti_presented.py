"""The NIfTI profile's array against the stored array's description (spec/stored-array.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): what the profile
writes of a file's voxels, read off their description by the profile's rules (spec/zarrify/nifti.md
§4.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#41-the-array> and §7.2
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#72-chunks>), is what the profile
wrote.

Of the description the test derives the array's document and every chunk:

- the axes, from the description's rank and whether a voxel is color samples; the shape and the
  transposition, from its shape in storage order, a color voxel's samples a last axis of their own;
- the chunk shape and each chunk's bytes, by cutting the unit's bytes, which the row gives, as the
  profile cuts contiguous values (conventions §7
  <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays>), an
  edge chunk padded from the zero data source, or copied with its padding where the run's payload
  is too small for the reference;
- of a gzipped file, one chunk of the whole array, the unit, and the codecs its chain gives.

What no description holds is read from the declaration, the header, as a profile reads it: the
byte order (`sizeof_hdr`), and, by the `datatype` and the profile's table of data types
(spec/zarrify/nifti.md §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#3-data-types>),
the data type's name and the samples of a color voxel; the description's `item_bits` is held
against that data type's size. A gzipped file's description has no declaration: its header is read
from the first of the bytes its `skip` codec passes over, where the format's paragraph says it
lies. Nothing is taken from the output. The document's fill value, a default of the profile's, is
not compared."""

import gzip
import json
import math
import shutil
import struct
from collections import Counter
from functools import cache
from pathlib import Path

import numpy as np
import pytest
from described import Described, described
from ir_helpers import FIXTURES, core_setting
from test_nifti_stored import header, voxels

from refkv import _core
from refkv._narrow import JSON, at, integer, mapping
from refkv.ir.stored import Value
from refkv.zarrify.common import Output

FILES = sorted(p for p in (FIXTURES / "nifti").iterdir()
               if "reject" not in p.name and p.name.lower().endswith((".nii", ".nii.gz", ".hdr")))
# the most bytes of a chunk of the image, and the level the profile writes of a gzip codec
LIMIT, GZIP_LEVEL = 1 << 17, 6
# the profile's data types, by `datatype`: the Zarr data type, and the samples of a voxel
DATA_TYPES = {2: ("uint8", 1), 4: ("int16", 1), 8: ("int32", 1), 16: ("float32", 1), 64: ("float64", 1),
              256: ("int8", 1), 512: ("uint16", 1), 768: ("uint32", 1), 1024: ("int64", 1), 1280: ("uint64", 1),
              32: ("complex64", 1), 1792: ("complex128", 1), 128: ("uint8", 3), 2304: ("uint8", 4)}
# the payload the test gives a run so that it copies every padded edge chunk
SMALL_PAYLOAD = 8

type Part = tuple[str, str | int | None, int, int]
"""a part of a reference: ("object", key, offset, length) of an object of the source (the key None
for a file's own bytes), or ("data", source, offset, length) of a data source of the output"""


def parts_of(parts: list) -> list[Part]:
    """A reference's parts as the output gives them, a file's and a store's alike."""
    got: list[Part] = []
    for p in parts:
        assert not isinstance(p, bytes), "the profile writes no literal"
        if p[0] == "object":
            got.append(("object", p[1], p[2], p[3]))
        elif len(p) == 2 or p[0] == 0:
            got.append(("object", None, p[-2], p[-1]))
        else:
            got.append(("data", p[0], p[1], p[2]))
    return got


def cut(shape: list[int], element: int) -> tuple[int, int, list[str]]:
    """The axis that contiguous values of `shape`, of `element` bytes each, are cut along, the
    indices of a chunk along it (conventions §7
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays>, with
    the profile's chunk limit), and which of the rule's cases chose them. Where no axis fits a
    count of chunks the rule cuts the last axis all the same, which no file here reaches."""
    total = element * math.prod(shape)
    how: list[str] = []
    for j, n in enumerate(shape):
        slab = element * math.prod(shape[j + 1:])
        if slab > LIMIT:
            continue
        k = math.ceil(n / (LIMIT // slab))
        # each count of chunks from k to twice it: the padding of its edge chunk, and its chunk's indices
        lengths = [math.ceil(n / q) for q in range(k, min(2 * k, n) + 1)]
        counts = [((math.ceil(n / c) * c - n) * slab, c) for c in lengths]
        small = [i for i, (padding, _) in enumerate(counts) if padding <= 1024]
        if small:
            return j, counts[small[0]][1], [*how, "cut with a small padding, at more chunks" if small[0] else "cut"]
        # the least padding, the first of equals
        padding, c = min(counts, key=lambda count: count[0])
        in_all = padding * math.prod(shape[:j])
        if padding <= 64512 and in_all <= max(1 << 20, total // 64):
            within = ", within 1/64 of the array" if in_all > 1 << 20 else ""
            return j, c, [*how, f"cut with the least padding{within}"]
        how.append("with an axis passed over for its padding")
    raise AssertionError("no axis fits a count of chunks")


def check(d: Described, copying: bool, seen: Counter) -> list[str]:
    """The array the profile wrote against the one description; `copying`, whether the run's
    payload is `SMALL_PAYLOAD`."""
    out = d.out
    entries = out.bytes_entries if isinstance(out, Output) else out.entries
    arrays = [k for k, v in entries.items() if k.endswith("zarr.json") and not k.startswith("refkv_source/")
              and mapping(json.loads(v), k)["node_type"] == "array"]
    if arrays != ["0/zarr.json"] or len(d.stored.arrays) != 1 or len(d.rows.get(0, [])) != 1:
        return [f"arrays at {arrays}, {len(d.stored.arrays)} descriptions and {len(d.rows.get(0, []))} rows"]
    doc = dict(mapping(json.loads(entries["0/zarr.json"]), "0/zarr.json"))
    a, (row,) = d.stored.arrays[0], d.rows[0]
    if a.shape is None or a.item_bits is None or a.grid is None:
        return ["a description of no grid"]
    chain = d.stored.chains[a.chains[0]]
    names = [c.name for c in chain]
    ((key, start, length),) = d.parts(row)
    skip = 0
    if a.declarations:
        declared = d.place(a.declarations[0])
    else:
        # a gzipped file: the header is the first of the bytes that `skip` passes over
        if names != ["skip", "gzip"]:
            return [f"no declaration, and a chain {names}"]
        passed = chain[0].parameters["length"]
        assert isinstance(passed, Value)
        skip = integer(passed.value)
        declared = gzip.decompress(d.unit(row))[:skip]
    # the byte order in which `sizeof_hdr` is the header's length, and the `datatype` at its offset there
    order = "<" if struct.unpack_from("<i", declared)[0] in (348, 540) else ">"
    (sizeof_hdr,) = struct.unpack_from(f"{order}i", declared)
    (datatype,) = struct.unpack_from(f"{order}h", declared, 70 if sizeof_hdr == 348 else 12)
    data_type, samples = DATA_TYPES[datatype]
    color, n = samples > 1, len(a.shape)
    element = np.dtype(data_type).itemsize

    # the array's axes, and the stored ones: dimension 5 outermost, a color voxel's samples innermost
    axes = [x for x, there in (("t", n >= 4), ("c", n >= 5 or color), ("z", n >= 3), ("y", True), ("x", True)) if there]
    stored = [*(["c"] if n >= 5 else []), *[x for x in axes if x != "c"], *(["c"] if color else [])]
    # their sizes: the header's sixth and seventh dimensions are no axes, and one dimension is a row
    sizes = [*([1] if n == 1 else []), *a.shape[max(n - 5, 0):], *([samples] if color else [])]
    along = dict(zip(stored, sizes, strict=True))
    said = []
    if a.item_bits != 8 * element * samples:
        said.append(f"item_bits {a.item_bits}, and {samples} samples of {data_type}, the header's datatype {datatype}")
    if doc["dimension_names"] != axes or doc["shape"] != [along[x] for x in axes]:
        said.append(f"shape {a.shape} of {samples} samples; the profile's {doc['shape']}, {doc['dimension_names']}")
    if doc["data_type"] != data_type:
        said.append(f"the header's datatype {datatype}, {data_type}; the profile's data type {doc['data_type']}")
    codecs: list[JSON] = []
    if stored != axes:
        codecs.append({"name": "transpose", "configuration": {"order": [axes.index(x) for x in stored]}})
        seen["arrays transposed"] += 1
    endian: JSON = {"endian": "little" if order == "<" else "big"}
    codecs.append({"name": "bytes", "configuration": endian} if element > 1 else {"name": "bytes"})

    # the chunks: each one's coordinates along the stored axes, its bytes in the unit and its padding
    chunks: list[tuple[tuple[int, ...], int, int, int]] = []
    if names == ["skip", "gzip"]:
        # one chunk of the whole array, the stream, which the codecs inflate and pass over the header of
        codecs.append({"name": "refkv.skip", "configuration": {"length": skip}})
        codecs.append({"name": "gzip", "configuration": {"level": GZIP_LEVEL}})
        shape = sizes
        chunks.append(((0,) * len(sizes), 0, length, 0))
        seen["arrays of a gzipped file, in one chunk"] += 1
    elif names:
        return [*said, f"chain {names}, which the profile presents nothing of"]
    else:
        if length != element * math.prod(sizes):
            said.append(f"a unit of {length} bytes, and {sizes} elements of {element}")
        j, c, how = cut(sizes, element)
        seen.update(f"arrays {h}" for h in how if h != "cut")
        slab = element * math.prod(sizes[j + 1:])
        shape = [*[1] * j, c, *sizes[j + 1:]]
        for outer in np.ndindex(*sizes[:j]):
            before = int(np.ravel_multi_index(outer, sizes[:j])) if j else 0
            for q in range(math.ceil(sizes[j] / c)):
                m = min(c, sizes[j] - q * c)
                at_byte = (before * sizes[j] + q * c) * slab
                chunks.append(((*map(int, outer), q, *[0] * (len(sizes) - j - 1)), at_byte, m * slab, (c - m) * slab))
        seen["arrays of a pair"] += key is not None
        seen["arrays cut into several chunks"] += len(chunks) > 1
    if doc["codecs"] != codecs:
        said.append(f"chain {names} of {samples} samples; the profile's codecs {doc['codecs']}, not {codecs}")
    chunk = dict(zip(stored, shape, strict=True))
    if at(doc["chunk_grid"], "configuration", "chunk_shape") != [chunk[x] for x in axes]:
        said.append(f"chunks of {doc['chunk_grid']}, not {shape} along {stored}")

    # every chunk is written: a reference to its bytes, padded from the zero data source, or its bytes
    written = {k for k in (*out.refs, *entries) if k.startswith("0/c/")}
    keys = {"0/c/" + "/".join(str(coords[stored.index(x)]) for x in axes): coords for coords, *_ in chunks}
    if written != set(keys):
        said.append(f"chunks at {sorted(written - set(keys))[:4]} alone, and none at {sorted(set(keys) - written)[:4]}")
    most = 0
    for chunk_key, (_, at_byte, held, padding) in zip(keys, chunks, strict=True):
        held_at: Part = ("object", key, start + at_byte, held)
        want: list[Part] | bytes = [held_at]
        if padding and copying:
            # the reference, with its padding, is past the run's payload: the chunk's bytes, padded
            want = d.objects[key][start + at_byte:start + at_byte + held] + bytes(padding)
            seen["chunks copied"] += 1
        elif padding:
            want = [held_at, ("data", 1, 0, padding)]
            most = max(most, padding)
            seen["chunks padded from the zero data source"] += 1
        got = entries.get(chunk_key) if isinstance(want, bytes) else parts_of(out.refs.get(chunk_key, []))
        if got != want:
            said.append(f"chunk {chunk_key} is {got if isinstance(got, list) else 'bytes'}, not {want[:4]}")
        seen["chunks"] += 1
    # the zero data source holds as many zeros as the most padding of a reference
    data = sorted(out.data) if isinstance(out, Output) else list(out.data)
    if data != ([bytes(most)] if most else []):
        said.append(f"data sources of {[len(b) for b in data]} bytes, and a padding of at most {most}")
    seen["arrays of color samples"] += color
    seen["arrays"] += 1
    return said


@cache
def compared(path: Path, copying: bool = False) -> tuple[tuple[str, ...], Counter]:
    """What differs between the array the profile wrote of the file at `path` and the file's
    description, and what the comparison saw; `copying`, in a run of a payload of `SMALL_PAYLOAD`."""
    with core_setting(_core._set_copy_threshold, SMALL_PAYLOAD if copying else None):
        d = described(path)
    seen: Counter = Counter()
    said = ["descriptions dropped for room"] if d.stored.undescribed else []
    said += check(d, copying, seen)
    return tuple(said), seen


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_the_nifti_profile_writes_what_the_description_gives(path):
    """The array the profile writes of the file is what its voxels' description gives."""
    said, seen = compared(path)
    assert said == ()
    assert seen["arrays"] == 1


def test_the_fixtures_hold_every_case_the_profile_is_compared_in():
    """The fixtures reach each case of the comparison, so that none of it passes for want of a
    file. One fixture has a padded edge chunk, and none a copied one: the next test writes more."""
    seen: Counter = Counter()
    for path in FILES:
        seen += compared(path)[1]
    assert {k: v for k, v in seen.items() if v} == {
        "arrays": 84,
        "arrays of a pair": 9,
        "arrays of a gzipped file, in one chunk": 5,
        "arrays transposed": 15,
        "arrays of color samples": 8,
        "arrays cut into several chunks": 7,
        "chunks": 93,
        "chunks padded from the zero data source": 1,
    }


def test_chunks_no_fixture_holds_are_written_as_the_description_gives(tmp_path):
    """Chunks the fixtures lack.

    Padded edge chunks: the rows of an image, each cut in two; those of a color image, whose
    samples the profile's array has on another axis; and a pair's. Each is compared in a run of the
    profile's payload, where its padding is a range of the zero data source, and in one whose
    payload holds no padded reference, where the chunk is its bytes and its padding, as is the
    padded chunk of the one fixture that has one.

    Cuts by the rule's other cases, in a run of the profile's payload: `least`, 131 rows that no
    count of chunks divides, cut with the least padding, at the first of the counts that share it;
    `count`, 136 rows, cut in 4 where 3 would pad more; `moved`, 25 slices of 5 rows, whose padding
    over every slice is past what the rule allows, so that the cut moves to the next axis; and
    `within`, 66 slices of 67 rows, whose padding over every slice is past 2^20 bytes and within
    1/64 of the array's, a file of 70 MB that holds no bytes after its header."""
    files = {
        "rows.nii": header(1, "<", (64, 64), (20001, 3), 352, b"n+1") + bytes(4) + voxels(8 * 20001 * 3),
        "color.nii": header(2, ">", (128, 24), (50001, 2), 544, b"n+2") + bytes(4) + voxels(3 * 50001 * 2),
        "edge.hdr": header(2, "<", (2, 8), (131073,), 16, b"ni2"),
        "edge.img": voxels(16 + 131073),
    }
    cuts = {
        "least.nii": header(1, "<", (2, 8), (2000, 131), 352, b"n+1") + bytes(4) + voxels(2000 * 131),
        "count.nii": header(1, "<", (2, 8), (2000, 136), 352, b"n+1") + bytes(4) + voxels(2000 * 136),
        "moved.nii": header(1, ">", (512, 16), (21500, 5, 25), 352, b"n+1") + bytes(4) + voxels(2 * 21500 * 5 * 25),
        "within.nii": header(1, "<", (2, 8), (16000, 67, 66), 352, b"n+1") + bytes(4),
    }
    padded = [p for p in FILES if compared(p)[1]["chunks padded from the zero data source"]]
    seen: Counter = Counter()
    for copying in (False, True):
        folder = tmp_path / ("copied" if copying else "referenced")
        folder.mkdir()
        for name, held in (files if copying else files | cuts).items():
            (folder / name).write_bytes(held)
        for path in padded if copying else []:
            shutil.copy(path, folder / path.name)
        if not copying:
            with (folder / "within.nii").open("r+b") as f:
                f.truncate(352 + 16000 * 67 * 66)
        for path in sorted(folder.iterdir()):
            if path.suffix == ".img":
                continue
            said, saw = compared(path, copying)
            assert said == (), path
            seen += saw
    assert {k: v for k, v in seen.items() if v} == {
        "arrays": 11,
        "arrays of a pair": 2,
        "arrays transposed": 2,
        "arrays of color samples": 2,
        "arrays cut into several chunks": 11,
        "arrays cut with a small padding, at more chunks": 1,
        "arrays cut with the least padding": 1,
        "arrays cut with the least padding, within 1/64 of the array": 1,
        "arrays with an axis passed over for its padding": 1,
        "chunks": 1280,
        "chunks padded from the zero data source": 73,
        "chunks copied": 7,
    }
