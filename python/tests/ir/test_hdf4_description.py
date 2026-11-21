"""The description of every accepted HDF4 fixture (spec/zarrify/hdf4.md §9.6
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#96-the-description>), compared with
the HDF4 library through pyhdf, the reference reader: the library's version; every Vgroup's name,
class and members, and the count of a version 4 Vgroup's attributes; every Vdata's name, class,
interlace, record count, record size and fields; each dataset of the SD interface, by its NDG's
ref, its name, dimensions, number type, compression and attributes (with their values); and the
file's attributes. pyhdf does not bind `SDgetchunkinfo`, so a chunked dataset's chunks are checked
through its stored array (python/tests/ir/test_hdf4_stored.py), and here only against its
compression. A second test asserts the description holds none of the output's decisions."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import jsonschema
import pytest

# pyhdf, where it has a wheel (pyproject.toml)
pytest.importorskip("pyhdf")
import pyhdf.V
import pyhdf.VS  # noqa: F401  (HDF.vstart finds it as an attribute of the package)
from ir_helpers import FIXTURES, ROOT
from pyhdf.error import HDF4Error
from pyhdf.HDF import HDF
from pyhdf.SD import SD

from refkv.build import read

FILES = sorted(p for p in (FIXTURES / "hdf4").iterdir() if p.suffix == ".hdf" and "reject" not in p.name)
SCHEMA = json.loads((ROOT / "spec" / "zarrify" / "hdf4" / "description.json").read_text())
# members a description never has: the output's decisions (spec/zarrify/hdf4.md §9.6
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#96-the-description>)
DECISIONS = {"path", "nodes", "data_type", "codecs", "fill_value", "dimension_names", "unpresented", "store", "own"}


def text(value: Any) -> str:
    """A description's text value as pyhdf reads a name: as UTF-8, else ISO 8859-1."""
    return value["latin1"] if isinstance(value, dict) else value


def as_pyhdf(vdata: dict[str, Any]) -> Any:
    """An attribute's value as pyhdf gives it: text whole, a number alone, a float's NaN and
    infinities as floats."""
    value = vdata["value"]
    if isinstance(value, (str, dict)):
        return text(value)
    out = [float(v) if isinstance(v, str) else v for v in value]
    return out[0] if len(out) == 1 else out


def same(a: Any, b: Any) -> bool:
    """Values equal, NaN equal to NaN."""
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    return a == b


def refs(next_ref) -> list[int]:
    """Every ref an interface's iterator gives (`Vgetid`, `VSgetid`), in its order."""
    out, r = [], -1
    while True:
        try:
            r = next_ref(r)
        except HDF4Error:
            return out
        out.append(r)


def attributes(description: dict[str, Any], refs: list[int]) -> list[tuple[str, Any]]:
    """The attributes of the attribute Vdatas `refs` that the description decodes, by name, in
    order."""
    vdatas = {v["ref"]: v for v in description["vdatas"]}
    return [(text(vdatas[r]["name"]), as_pyhdf(vdatas[r])) for r in refs if vdatas.get(r, {}).get("value") is not None]


def assert_same(ours: list[tuple[str, Any]], full: dict[str, Any], at: object) -> None:
    """The description's attributes are pyhdf's (`attributes(full=1)`), in its order."""
    theirs = [(k, v[0]) for k, v in sorted(full.items(), key=lambda kv: kv[1][1])]
    assert [a[0] for a in ours] == [b[0] for b in theirs], at
    assert all(same(a[1], b[1]) for a, b in zip(ours, theirs, strict=True)), at


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_the_description_is_what_the_hdf4_library_reads(path: Path):
    with read(path) as parsed:
        description: Any = parsed.description
    jsonschema.validate(description, SCHEMA)
    h = HDF(str(path))
    try:
        major, minor, release, info = h.getfileversion()
        version = description["version"]
        assert (version["major"], version["minor"], version["release"], text(version["text"])) == (
            major,
            minor,
            release,
            info,
        )
        # the Vgroups
        vgroups = {g["ref"]: g for g in description["vgroups"]}
        v = h.vgstart()
        listed = refs(v.getid)
        assert set(listed) <= set(vgroups), path.name
        for r in listed:
            g, theirs = vgroups[r], v.attach(r)
            try:
                assert (text(g["name"]), text(g["class"])) == (theirs._name, theirs._class), (path.name, r)
                assert [tuple(m) for m in g["members"]] == list(theirs.tagrefs()), (path.name, r)
                if g["version"] == 4:
                    assert len(g["attributes"]) == len(theirs.attrinfo()), (path.name, r)
            finally:
                theirs.detach()
        v.end()
        # the Vdatas
        vdatas = {d["ref"]: d for d in description["vdatas"]}
        vs = h.vstart()
        listed = refs(vs.next)
        assert set(listed) == set(vdatas), path.name
        for r in listed:
            d, theirs = vdatas[r], vs.attach(r)
            try:
                records, interlace, _, size, name = theirs.inquire()
                assert (text(d["name"]), text(d["class"]), d["interlace"], d["records"], d["record_size"]) == (
                    name,
                    theirs._class,
                    interlace,
                    records,
                    size,
                ), (path.name, r)
                fields = [(text(f["name"]), f["type"], f["order"], f["size"]) for f in d["fields"]]
                assert fields == [(f[0], f[1], f[2], f[5]) for f in theirs.fieldinfo()], (path.name, r)
            finally:
                theirs.detach()
        vs.end()
    finally:
        h.close()
    # the datasets, by their NDGs' refs, and the file's attributes
    datasets = {d["ndg"]: d for d in description["datasets"]}
    sd = SD(str(path))
    try:
        for _, _, _, index in sd.datasets().values():
            s = sd.select(index)
            try:
                name, rank, _, ty, _ = s.info()
                d = datasets[s.ref()]
                at = (path.name, name)
                assert text(d["name"]) == name, at
                assert [(text(x["name"]), x["unlimited"]) for x in d["dimensions"]] == [
                    (s.dim(k).info()[0], k == 0 and s.isrecord()) for k in range(rank)
                ], at
                assert d["coordinate"] in (None, bool(s.iscoordvar())), at
                nt = d["number_type"]
                assert (nt["type"], nt["class"] == 4) == (ty & 0xFF, bool(ty & 0x4000)), at
                # the fixed dimensions' sizes, which the dimension record gives
                assert [n for k, n in enumerate(d["dimension_sizes"]) if not (k == 0 and s.isrecord())] == [
                    s.dim(k).info()[1] for k in range(rank) if not (k == 0 and s.isrecord())
                ], at
                method = s.getcompress()[0]
                data = d["data"]
                if data is None or data["kind"] == "plain":
                    assert method == 0, at
                elif data["kind"] == "compressed":
                    assert data["method"] == method, at
                elif data["kind"] == "chunked":
                    chunks = [c["data"] for c in data["chunks"]]
                    assert (
                        all(c["kind"] == "plain" for c in chunks)
                        if method == 0
                        else all(c["kind"] == "compressed" and c["method"] == method for c in chunks)
                    ), at
                assert_same(attributes(description, d["attributes"]), s.attributes(full=1), at)
            finally:
                s.endaccess()
        # the file's attributes: its first CDF0.0 Vgroup's attribute Vdatas (none without one)
        cdf = next((g for g in description["vgroups"] if g["class"] == "CDF0.0"), {"members": []})
        assert_same(
            attributes(description, [m[1] for m in cdf["members"] if m[0] == 1962]), sd.attributes(full=1), path.name
        )
    finally:
        sd.end()


def test_the_description_holds_no_decision():
    """No member anywhere in a description is one of the output's decisions."""

    def keys(v: Any) -> set[str]:
        if isinstance(v, dict):
            return set(v) | {k for x in v.values() for k in keys(x)}
        if isinstance(v, list):
            return {k for x in v for k in keys(x)}
        return set()

    for path in FILES:
        with read(path) as parsed:
            assert not keys(parsed.description) & DECISIONS, path.name
