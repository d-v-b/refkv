"""The NRRD profile's array against the stored array's description (spec/stored-array.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): what the profile
writes of the array's storage, read off the description by the profile's rules
(spec/zarrify/nrrd.md §4.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#41-the-array>
and §8.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#85-chunks>), is what the
profile wrote. Of the description the test derives

- the array's shape and dimension names: the description's shape, its axes put in the image's order;
- its codecs: a `transpose` where the stored order is not the image's, `bytes` in the byte order
  the `endian` declaration gives, and the chain's `skip` and `gzip` as `refkv.skip` and `gzip`;
- its chunk shape and every chunk: for an empty chain, the one unit cut as the profile cuts
  contiguous values, each chunk a range of the unit's bytes, an edge chunk's padding a range of
  the zero data source; for a gzip stream, one chunk, the unit;
- the size of its data type, the description's `item_bits`.

Two things are the profile's reading, and are not derived. Which image axis each header axis is
(`x`, `y`, `z`, `c` or `t`) follows from the `kinds` and `space directions` lines, which no
description lists: it is taken from the root's `nrrd.axes` of the output. And the data type's name
is the profile's reading of the `type` declaration: its size alone is compared."""

import json
from collections import Counter
from functools import cache
from pathlib import Path

import numpy as np
import pytest
from described import Described, described
from test_nrrd_stored import FILES

from refkv._narrow import JSON, at, integer, mapping, sequence, string
from refkv.ir.store import StoreIrOutput
from refkv.ir.stored import Each, Source
from refkv.zarrify.common import grid_chunks

# the most bytes of a chunk of raw data (spec/zarrify/nrrd.md §4.1
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#41-the-array>)
CHUNK_BYTES = 1 << 17
# the image's axes, in the order the profile writes those an input has
IMAGE_AXES = "tczyx"


def descriptor(line: bytes, identifier: str) -> str:
    """The descriptor of the header line of the field `identifier`."""
    name, _, text = line.decode("ascii").partition(":")
    assert name.lower() == identifier, line
    return text.strip()


def check(inp: Described, seen: Counter) -> list[str]:
    """What differs between the array the profile wrote of an input and what the input's
    description gives by the profile's rules."""
    d, out = inp, inp.out
    assert isinstance(out, StoreIrOutput), "a NRRD input is read as a store"
    (a,) = d.stored.arrays
    (row,) = d.rows[0]
    assert a.shape is not None
    assert a.item_bits is not None
    assert a.grid is not None
    root = mapping(json.loads(out.entries["zarr.json"]))
    doc = mapping(json.loads(out.entries["0/zarr.json"]))
    rank, item = len(a.shape), a.item_bits // 8
    said = []
    seen["arrays"] += 1

    # the image's axes: each header axis's, which the profile reads off lines no description
    # lists, by the header axis that each storage axis is
    header_axes = [string(x) for x in sequence(at(root["attributes"], "refkv_zarrified", "nrrd", "axes"))]
    stored = [header_axes[i] for i in (a.axes if a.axes is not None else range(rank))]
    names: list[str] = [x for x in IMAGE_AXES if x in stored]
    if sorted(stored) != sorted(names):
        return [f"the header's axes {header_axes} are not the image's, each once"]
    if doc["dimension_names"] != names or doc["shape"] != [a.shape[stored.index(x)] for x in names]:
        said.append(f"shape {a.shape} along {stored}; the profile's {doc['shape']} along {doc['dimension_names']}")
    if np.dtype(string(doc["data_type"])).itemsize * 8 != a.item_bits:
        said.append(f"item_bits {a.item_bits}; the profile's data type {doc['data_type']}")

    # the codecs: the stored order made the image's, the byte order the endian line gives, and
    # the chain's codecs in its order
    codecs: list[JSON] = []
    if stored != names:
        seen["arrays stored in another order than the image's"] += 1
        codecs.append({"name": "transpose", "configuration": {"order": [names.index(x) for x in stored]}})
    if item == 1:
        seen["arrays of one byte"] += 1
        codecs.append({"name": "bytes"})
    else:
        endian = descriptor(inp.place(a.declarations[1]), "endian").lower()
        seen[f"arrays of {endian}-endian elements"] += 1
        codecs.append({"name": "bytes", "configuration": {"endian": endian}})
    chain = d.stored.chains[a.chains[0]]
    for codec in chain:
        if codec.name == "skip":
            length = codec.parameters["length"]
            assert isinstance(length, Source)
            seen["arrays of a skip in the stream"] += 1
            codecs.append({"name": "refkv.skip", "configuration": {"length": int(inp.place(length.place))}})
        elif codec.name == "gzip":
            codecs.append({"name": "gzip", "configuration": {"level": 1}})
        else:
            return [*said, f"the codec {codec.name}, which the profile writes nothing of"]
    if doc["codecs"] != codecs:
        said.append(f"codecs {codecs} of the description; the profile's {doc['codecs']}")

    # the chunks, of the one unit: its bytes cut, or the stream whole
    if a.grid.axes != tuple(Each(n) for n in a.shape) or row["range"] is not None or row["element"] is None:
        return [*said, f"grid {a.grid} and row {row}, not one unit of the whole array"]
    key, offset, length = inp.at(row["element"])
    seen["arrays of a data file"] += inp.key(a.element) != key
    padding = 0
    want: dict[str, list[tuple]] = {}
    if chain:
        seen["arrays of one chunk, a gzip stream"] += 1
        chunk = list(a.shape)
        want["/".join(["0", "c", *["0"] * rank])] = [("object", key, offset, length)]
    else:
        if length != item * int(np.prod(a.shape, dtype=object)):
            return [*said, f"a unit of {length} bytes of {a.shape} elements of {item} bytes"]
        seen["arrays cut into chunks"] += 1
        chunk, cut = grid_chunks(offset, list(a.shape), item, limit=CHUNK_BYTES)
        for coords, parts in cut.items():
            assert not isinstance(parts, bytes), "a chunk the cut does not copy"
            # one range of the unit's bytes, then an edge chunk's zero bytes
            ((start, count),) = [(p[-2], p[-1]) for p in parts if isinstance(p, tuple)]
            zeros = sum(len(p) for p in parts if isinstance(p, bytes))
            assert offset <= start, "a chunk within the unit"
            assert start + count <= offset + length, "a chunk within the unit"
            refs: list[tuple] = [("object", key, start, count)]
            if zeros:
                # of the output's zero data source
                seen["chunks padded"] += 1
                padding = max(padding, zeros)
                refs.append((1, 0, zeros))
            want["/".join(["0", "c", *[str(coords[stored.index(x)]) for x in names]])] = refs
            seen["chunks cut of raw data"] += 1
        seen["arrays of several chunks"] += len(cut) > 1
    got_chunk = [integer(x) for x in sequence(at(doc["chunk_grid"], "configuration", "chunk_shape"))]
    if got_chunk != [chunk[stored.index(x)] for x in names]:
        said.append(f"chunks of {chunk} along {stored}; the profile's {got_chunk} along {names}")
    got = {k: [tuple(p) for p in parts] for k, parts in out.refs.items() if k.startswith("0/c/")}
    if any(k.startswith("0/c/") for k in out.entries):
        said.append("chunks the profile copied")
    if set(got) != set(want):
        said.append(f"chunks at {sorted(set(got) - set(want))} alone, and none at {sorted(set(want) - set(got))}")
    said += [f"chunk {k} is {got[k]}, not {refs}" for k, refs in sorted(want.items()) if k in got and got[k] != refs]
    # the zero data source: as many zero bytes as the most padding
    if out.table()[1] != [bytes(padding)] * (padding > 0):
        said.append(f"data sources of {[len(b) for b in out.table()[1]]} bytes, for a padding of at most {padding}")
    return said


@cache
def compared(path: Path) -> tuple[tuple[str, ...], Counter]:
    """What differs between the array the profile wrote of the input at `path` and the input's
    description, and what the comparison saw."""
    seen: Counter = Counter()
    return tuple(check(described(path), seen)), seen


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_the_nrrd_profile_writes_what_the_description_gives(path):
    """The array the profile writes of the input is what its description gives."""
    said, seen = compared(path)
    assert said == ()
    assert seen["arrays"] == 1


def test_the_fixtures_hold_every_case_the_profile_is_compared_in():
    """The fixtures reach each case of the comparison, so that none of it passes for want of a
    file."""
    seen: Counter = Counter()
    for path in FILES:
        seen += compared(path)[1]
    assert {k: v for k, v in seen.items() if v} == {
        "arrays": 41,
        "arrays stored in another order than the image's": 3,
        "arrays of one byte": 11,
        "arrays of little-endian elements": 20,
        "arrays of big-endian elements": 10,
        "arrays of a skip in the stream": 1,
        "arrays of a data file": 3,
        "arrays of one chunk, a gzip stream": 5,
        "arrays cut into chunks": 36,
        "arrays of several chunks": 2,
        "chunks cut of raw data": 39,
        "chunks padded": 1,
    }


def test_chunks_of_an_array_stored_in_another_order_are_keyed_in_the_images(tmp_path):
    """A case no fixture holds: raw data cut into several chunks whose stored order is not the
    image's (a color axis stored fastest), so that each chunk's coordinates, and the chunk shape,
    are put in the image's order."""
    path = tmp_path / "color_cut.nrrd"
    fields = ("NRRD0004", "type: uint8", "dimension: 3", "sizes: 3 200 301", "encoding: raw",
              "kinds: RGB-color domain domain", "")
    path.write_bytes("".join(f"{line}\n" for line in fields).encode() + bytes(3 * 200 * 301))
    said, seen = compared(path)
    assert said == ()
    assert {k: v for k, v in seen.items() if v} == {
        "arrays": 1,
        "arrays stored in another order than the image's": 1,
        "arrays of one byte": 1,
        "arrays cut into chunks": 1,
        "arrays of several chunks": 1,
        "chunks cut of raw data": 2,
        "chunks padded": 1,
    }
