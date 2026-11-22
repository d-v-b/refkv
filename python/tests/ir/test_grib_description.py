"""The description of every accepted GRIB fixture (spec/zarrify/grib.md §8.7
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#87-the-description>), compared with
ecCodes, the reference reader: each message's offset and length, and each field, in order, with
ecCodes' support for messages of several fields on: its discipline, centre and reference time; its
product template, parameter category and number, surfaces' types and ensemble member; its packing
template and number of values; its bitmap indicator (of a reused bitmap, 254, ecCodes gives 0 or
255); and its grid's template, number of points, `Ni` and `Nj` and scanning mode, where the parser
interprets the grid. A second test asserts the description holds none of the output's decisions."""

import calendar
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from ir_helpers import FIXTURES, ROOT

from refkv.build import read

try:
    import eccodes
except (ImportError, RuntimeError):  # no ecCodes library for this platform
    pytest.skip("no ecCodes library", allow_module_level=True)

FILES = sorted(p for p in (FIXTURES / "grib").glob("*.grib2") if "reject" not in p.name)
SCHEMA = json.loads((ROOT / "spec" / "zarrify" / "grib" / "description.json").read_text())
# members a description never has: the output's decisions (spec/zarrify/grib.md §8.7
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#87-the-description>)
DECISIONS = {"reason", "own", "unpresented", "variables", "name", "data_type", "codecs", "counts"}
# the product templates with an ensemble member's number (spec/zarrify/grib.md §2.3
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#23-dimensions>)
MEMBERS = (1, 11)


def long(h: int, key: str) -> int | None:
    """An ecCodes key's value, None where it is not defined or is missing."""
    if not eccodes.codes_is_defined(h, key) or eccodes.codes_is_missing(h, key):
        return None
    return int(eccodes.codes_get_long(h, key))


def fields(path: Path) -> list[dict[str, Any]]:
    """ecCodes' reading of every field of the file, in order."""
    out = []
    eccodes.codes_grib_multi_support_on()
    try:
        with path.open("rb") as f:
            while (h := eccodes.codes_grib_new_from_file(f)) is not None:
                try:
                    time = calendar.timegm(
                        tuple(long(h, k) or 0 for k in ("year", "month", "day", "hour", "minute", "second"))
                    )
                    out.append(
                        {
                            k: long(h, k)
                            for k in (
                                "discipline",
                                "centre",
                                "productDefinitionTemplateNumber",
                                "parameterCategory",
                                "parameterNumber",
                                "typeOfFirstFixedSurface",
                                "typeOfSecondFixedSurface",
                                "perturbationNumber",
                                "dataRepresentationTemplateNumber",
                                "numberOfValues",
                                "bitMapIndicator",
                                "gridDefinitionTemplateNumber",
                                "numberOfDataPoints",
                                "Ni",
                                "Nj",
                                "scanningMode",
                            )
                        }
                        | {"time": time}
                    )
                finally:
                    eccodes.codes_release(h)
    finally:
        eccodes.codes_grib_multi_support_off()
    return out


def messages(path: Path) -> list[list[int | None]]:
    """Each message's offset and length, as ecCodes reads them."""
    out = []
    with path.open("rb") as f:
        while (h := eccodes.codes_grib_new_from_file(f)) is not None:
            out.append([long(h, "offset"), long(h, "totalLength")])
            eccodes.codes_release(h)
    return out


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_the_description_is_what_eccodes_reads(path: Path):
    with read(path) as parsed:
        d: Any = parsed.description
    jsonschema.validate(d, SCHEMA)
    assert d["file_key"] == path.name
    assert d["messages"] == messages(path)
    theirs = fields(path)
    assert len(d["fields"]) == len(theirs), path.name
    for ours, e in zip(d["fields"], theirs, strict=True):
        at = f"{path.name}: message {ours['message']} field {ours['field']}"
        assert (ours["discipline"], ours["centre"], ours["reference"]) == (e["discipline"], e["centre"], e["time"]), at
        assert (ours["product"], ours["packing"], ours["values"]) == (
            e["productDefinitionTemplateNumber"],
            e["dataRepresentationTemplateNumber"],
            e["numberOfValues"],
        ), at
        # ecCodes gives a field that reuses the message's last bitmap (254) the indicator 0 where it
        # applies that bitmap and 255 where it leaves it (test_grib_stored.py counts both)
        assert e["bitMapIndicator"] in ((0, 255) if ours["bitmap"] == 254 else (ours["bitmap"],)), at
        if ours["unread"] is None:
            assert (ours["category"], ours["number"]) == (e["parameterCategory"], e["parameterNumber"]), at
            assert [s[0] for s in ours["surfaces"]] == [e["typeOfFirstFixedSurface"], e["typeOfSecondFixedSurface"]], at
            if ours["product"] in MEMBERS:
                assert ours["member"] == e["perturbationNumber"], at
        g = d["grids"][ours["grid"]]
        assert (g["template"], g["points"]) == (e["gridDefinitionTemplateNumber"], e["numberOfDataPoints"]), at
        if ours["unread"] is None and g["template"] != 101:
            assert g["scan"] == e["scanningMode"], at
            assert g["ny"] == e["Nj"], at
            # a reduced grid's Ni is missing; the description gives its longest row
            if e["Ni"] is not None:
                assert g["nx"] == e["Ni"], at


def test_the_description_holds_no_decision():
    """No member of a description, nor of its fields, is one of the output's decisions."""
    for path in FILES:
        with read(path) as parsed:
            d: Any = parsed.description
        assert not set(d) & DECISIONS, path.name
        assert not {k for f in d["fields"] for k in f} & DECISIONS, path.name
