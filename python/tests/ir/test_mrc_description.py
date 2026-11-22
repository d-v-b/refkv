"""The description of every MRC fixture (spec/zarrify/mrc.md §8.5
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mrc.md#85-the-description>), compared with
mrcfile, the reference reader: the byte order of its header; every field of the header, a float's
bits included, its texts up to their first NUL, its opaque bytes and its labels; the data's offset,
after the header and the extended header mrcfile reads, and its size, that of mrcfile's array (of a
mode mrcfile reads no values of, the size `nx`, `ny` and `nz` give); whether mrcfile inflated the
file, and of a gzipped file the header it read, byte for byte; and the IR elements it names. Each
description is valid by its JSON Schema, and holds none of the output's decisions."""

import base64
import json
import math
import struct
import warnings
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, cast

import jsonschema
import mrcfile
import numpy as np
import pytest
from ir_helpers import FIXTURES, ROOT
from mrcfile.gzipmrcfile import GzipMrcFile

from refkv.build import read

FILES = sorted(p for p in (FIXTURES / "mrc").iterdir() if "reject" not in p.name)
SCHEMA = json.loads((ROOT / "spec" / "zarrify" / "mrc" / "description.json").read_text())
HEADER = 1024
# the bits of a value of each mode (spec/zarrify/mrc.md §3
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mrc.md#3-data-types>)
BITS = {0: 8, 1: 16, 2: 32, 3: 32, 4: 64, 6: 16, 12: 16, 101: 4}
# members a description never has: the output's decisions (spec/zarrify/mrc.md §8.5
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mrc.md#85-the-description>)
DECISIONS = {"data_type", "kind", "axes", "axis_map", "voxel_size", "translation", "chunks", "grid", "own",
             "declared_by", "copy_threshold"}
# the header's fields by their kind (§2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mrc.md#2-the-header>)
TEXTS, OPAQUE = {"exttyp", "map"}, {"extra1", "extra2"}
FLOATS = {"cella", "cellb", "dmin", "dmax", "dmean", "origin", "rms"}


@dataclass(frozen=True)
class Read:
    """mrcfile's reading of a file: its header, the bytes of its extended header, its array's bytes
    (None for a mode it reads no values of), and whether it inflated the file."""

    header: np.recarray
    extended: int
    data: int | None
    gzipped: bool


@cache
def mrcfile_read(path: Path) -> Read:
    """mrcfile's reading, permissive: it then reads the header of a mode it has no data type for,
    and warns of a file longer than its data."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with mrcfile.open(path, permissive=True) as f:
            # mrcfile is unannotated: its header is None only before a read
            header = cast("np.recarray", f.header).copy()
            extended = cast("np.ndarray", f.extended_header).nbytes
            data = None if f.data is None else cast("np.ndarray", f.data).nbytes
            return Read(header, extended, data, isinstance(f, GzipMrcFile))


def f32_bits(v: Any) -> int:
    """The IEEE 754 bits of a float as the source metadata writes it (spec/conventions.md §6
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#6-source-metadata-as-json>)."""
    if isinstance(v, dict):
        return int(v["bits"], 16)
    value = {"NaN": math.nan, "Infinity": math.inf, "-Infinity": -math.inf}.get(v, v)
    return struct.unpack(">I", struct.pack(">f", value))[0]


def theirs_bits(v: Any) -> int:
    return int(np.array([v], dtype=np.float32).view(np.uint32)[0])


def text(v: Any) -> bytes:
    """A text value's bytes (spec/conventions.md §6
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#6-source-metadata-as-json>)."""
    return v["latin1"].encode("latin-1") if isinstance(v, dict) else v.encode()


def nul_ended(b: bytes) -> bytes:
    return b.split(b"\0")[0]


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_the_description_is_mrcfiles_reading(path):
    theirs = mrcfile_read(path)
    h = theirs.header
    with read(path) as parsed:
        d: Any = parsed.description
        jsonschema.validate(d, SCHEMA)
        assert not DECISIONS & set(d), DECISIONS & set(d)
        assert d["byte_order"] == ("big" if h.mode.dtype.byteorder == ">" else "little")
        ours = d["header"]
        names = h.dtype.names
        assert names is not None
        assert list(ours) == list(names)
        for name in names:
            v, t = ours[name], h[name]
            if name in TEXTS:
                assert text(v) == nul_ended(bytes(t)), name
            elif name in OPAQUE:
                assert base64.b64decode(v) == bytes(t), name
            elif name == "label":
                assert [text(x) for x in v] == [nul_ended(bytes(x)) for x in t]
            elif name == "machst":
                assert v == list(np.asarray(t).tolist())
            elif name in FLOATS:
                values = v if isinstance(v, list) else [v]
                held = [t[k] for k in t.dtype.names] if t.dtype.names else [t]
                assert [f32_bits(x) for x in values] == [theirs_bits(x) for x in held], name
            else:
                assert v == int(t), name
        # the data, after the header and the extended header mrcfile reads, as long as its array
        nx, ny, nz, mode = (int(h[k]) for k in ("nx", "ny", "nz", "mode"))
        assert d["data"]["offset"] == HEADER + theirs.extended
        size = theirs.data if theirs.data is not None else math.ceil(nx * BITS[mode] / 8) * ny * nz
        assert d["data"]["bytes"] == size
        # a gzipped file's stream: the header mrcfile read from what it inflates to
        assert ("gzip" in d) == theirs.gzipped
        elements = d["elements"]
        if theirs.gzipped:
            assert bytes.fromhex(d["gzip"]["head"]) == h.tobytes()
            assert d["gzip"]["skip"] == d["data"]["offset"]
            assert set(elements) == {"data"}
        else:
            assert parsed.ir.path(elements["header"]) == "header"
            assert ("extended" in elements) == (theirs.extended > 0)
            if theirs.extended:
                assert parsed.ir.path(elements["extended"]) == "extended"
        assert parsed.ir.path(elements["data"]) == "data"
        # the stored array is keyed by the header, or by a gzipped file's stream
        key = elements["data"] if theirs.gzipped else elements["header"]
        assert [a.element for a in parsed.ir.stored_arrays().arrays] == [key]
