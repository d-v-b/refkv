"""The description of every NRRD fixture (spec/zarrify/nrrd.md §8.7
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#87-the-description>), compared with
pynrrd, the reference reader: the magic; each field pynrrd parses, as the profile reads it (the
dimension, the type, the sizes, the endian, the encoding, the skips, the data file, the space
directions, origin and units, the spacings, units, kinds, min and max), a number pynrrd reads as NaN
being null; each key/value pair, which pynrrd keeps escaped and stripped; the header and data files'
keys; the data's range, the stored array's one unit, which test_nrrd_stored.py compares with where
pynrrd reads the data; and the IR elements it names. Each description is valid by its JSON Schema,
and holds none of the output's decisions."""

import json
import math
import posixpath
from pathlib import Path
from typing import Any
from unittest import mock

import jsonschema
import nrrd
import numpy as np
import pytest
from ir_helpers import FIXTURES, ROOT

from refkv.build import Ref, read

FILES = sorted(p for p in (FIXTURES / "nrrd").iterdir() if p.suffix in (".nrrd", ".nhdr") and "reject" not in p.name)
SCHEMA = json.loads((ROOT / "spec" / "zarrify" / "nrrd" / "description.json").read_text())
# members a description never has: the output's decisions (spec/zarrify/nrrd.md §8.7
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#87-the-description>)
DECISIONS = {"data_type", "axes", "own", "orientation", "declared_by", "copy_threshold", "copied", "array"}
# the encodings' names, as the profile reads them (spec/zarrify/nrrd.md §3
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#3-data-types-and-encodings>)
ENCODINGS = {"raw": "raw", "gzip": "gzip", "gz": "gzip"}
pytestmark = pytest.mark.filterwarnings("ignore:Duplicate header field")


def pynrrd_header(path: Path) -> dict[str, Any]:
    """pynrrd's reading of the header, allowing a repeated field as the profile does (its last)."""
    with mock.patch.object(nrrd.reader, "ALLOW_DUPLICATE_FIELD", True), path.open("rb") as f:
        return dict(nrrd.read_header(f))


def numbers(v: Any) -> list[float | None] | None:
    """pynrrd's numbers as the description gives them: a NaN as null."""
    if v is None:
        return None
    return [None if math.isnan(x) else float(x) for x in np.ravel(v)]


def text(v: Any) -> str:
    """A text value's string (spec/conventions.md §6
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#6-source-metadata-as-json>)."""
    return v["latin1"] if isinstance(v, dict) else v


def escaped(v: str) -> str:
    """A key/value pair's value as the header writes it, which pynrrd keeps."""
    return v.replace("\\", "\\\\").replace("\n", "\\n")


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_the_description_is_pynrrds_reading(path):
    theirs = pynrrd_header(path)
    with read(path) as parsed:
        d: Any = parsed.description
        jsonschema.validate(d, SCHEMA)
        assert not DECISIONS & set(d), DECISIONS & set(d)
        assert d["magic"] == path.read_bytes().split(b"\n")[0].rstrip(b"\r").decode()
        assert d["dimension"] == theirs["dimension"]
        assert d["type"] == theirs["type"].strip().lower()
        assert d["sizes"] == np.asarray(theirs["sizes"]).tolist()
        assert d["endian"] == theirs.get("endian")
        assert d["encoding"] == ENCODINGS[theirs["encoding"].strip().lower()]
        assert d["line_skip"] == theirs.get("line skip", 0)
        assert d["byte_skip"] == theirs.get("byte skip", 0)
        directions = theirs.get("space directions")
        if directions is None:
            assert d["directions"] is None
        else:
            # pynrrd gives `none` as a row of NaN
            assert d["directions"] == [None if np.isnan(row).all() else numbers(row) for row in np.asarray(directions)]
        assert d["origin"] == numbers(theirs.get("space origin"))
        assert d["space_units"] == theirs.get("space units")
        assert d["spacings"] == numbers(theirs.get("spacings"))
        assert d["units"] == theirs.get("units")
        # the kinds' names are read without case (spec/zarrify/nrrd.md §8.2
        # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#82-the-fields>)
        kinds = theirs.get("kinds")
        assert d["kinds"] == (None if kinds is None else [k.lower() for k in kinds])
        for k in ("min", "max"):
            assert [d[k]] == (numbers(theirs[k]) if k in theirs else [None]), k
        # the key/value pairs, which pynrrd keeps escaped, their keys and values stripped
        for key, value in d["key_values"].items():
            if key.strip() not in d["fields"]:
                assert theirs[key.strip()] == escaped(text(value)).strip(), key
        # the files, and the data's range: the stored array's one unit
        assert d["header_key"] == path.name
        assert d["header_size"] == path.stat().st_size
        # a data file's key is relative to the header's directory, without `.` segments
        # (spec/zarrify/nrrd.md §8.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#83-the-data-file>)
        assert d["data_key"] == posixpath.normpath(theirs.get("data file", path.name))
        elements = d["elements"]
        assert parsed.ir.path(elements["header_file"]) == path.name
        assert parsed.ir.path(elements["data_file"]) == d["data_key"]
        lines = elements["lines"]
        assert [parsed.ir.path(e) for e in lines] == [f"{path.name}/lines/{i}" for i in range(len(lines))]
        assert parsed.ir.path(elements["data"]) == f"{d['data_key']}/data"
        stored = parsed.ir.stored_arrays()
        assert [a.element for a in stored.arrays] == [elements["header_file"]]
        (ref,) = parsed.reference(elements["data"]).parts
        assert isinstance(ref, Ref)
        assert (ref.offset, ref.length) == (d["data"]["offset"], d["data"]["bytes"])
        assert str(parsed.sources[ref.source].url).endswith(d["data_key"])
