"""The description of every ND2 fixture (spec/zarrify/nd2.md §7.5
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nd2.md#75-the-description>), compared with the
nd2 library, the reference reader:

- `attributes` and `picture` are the members of `SLxImageAttributes` and `SLxPictureMetadata` as nd2
  decodes the chunks' lite variant (`_decode_chunk`, with the members' prefixes kept), each member the
  description has at nd2's value or, where nd2 finds none, at the default spec/zarrify/nd2.md §3
  <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nd2.md#3-metadata> gives; a color modulo
  2^32; `picture` null exactly where nd2 finds no `SLxPictureMetadata`;
- `loops` are the flattening of spec/zarrify/nd2.md §3
  <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nd2.md#3-metadata>, done here over nd2's
  decoding of `SLxExperiment`, and where nd2's own reading of the experiment (`ND2File.experiment`)
  reads the file, its loops' kinds, counts, nesting, periods, steps and stage positions are the
  description's;
- `chunks` are chunks nd2's chunk map names, each the IR's `lv` or `xml` element of the chunk of that
  name, in the map's order.

nd2 does not read the experiment of every file: it fails on time loops that lack members it requires
(the generator's, which lack `dStart`), and reads a time loop of periods with no `pPeriodValid` list as
none valid (the convention: every period valid). It does not decode a chunk with a string of unpaired
surrogates either; of that file's picture metadata only its presence is compared. Those files are
counted (`OTHERWISE`, `UNDECODED`), and the second test
asserts the counts, with which cases the fixtures reach."""

import json
from collections import Counter
from functools import cache
from pathlib import Path
from typing import Any

import jsonschema
import nd2
import pytest
from ir_helpers import FIXTURES, ROOT
from nd2._readers import ModernReader
from nd2.structures import NETimeLoopParams, TimeLoopParams, XYPosLoopParams, ZStackLoopParams

from refkv.build import read

FILES = sorted(p for p in (FIXTURES / "nd2").glob("*.nd2") if "reject" not in p.name)
SCHEMA = json.loads((ROOT / "spec" / "zarrify" / "nd2" / "description.json").read_text())
# the members spec/zarrify/nd2.md §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nd2.md#3-metadata>
# reads, with their defaults (None: absent)
ATTRIBUTES = {"uiWidth": None, "uiHeight": None, "uiWidthBytes": None, "uiComp": None, "uiBpcInMemory": None,
              "uiBpcSignificant": None, "eCompression": 2, "uiTileWidth": 0, "uiTileHeight": 0}
PICTURE = {"bCalibrated": False, "dCalibration": None, "dAspect": 1, "dStgLgCT11": 1, "dStgLgCT12": 0,
           "dStgLgCT21": 0, "dStgLgCT22": 1, "dXPos": None, "dYPos": None}
PLANE = {"sDescription": "", "uiColor": 0xFFFFFF, "uiCompCount": 1}
# the files whose experiment nd2 reads otherwise than the convention, by how
OTHERWISE = {"nd2_edge_validity.nd2": "differs"}
# members a description never has: the output's decisions and what the convention derives
DECISIONS = {"axes", "scales", "scale", "units", "translations", "calibrated", "data_type", "codecs", "channels",
             "omero", "positions", "frames", "row", "compressed", "window", "sizes"}
# the chunk nd2 does not decode (a string of unpaired surrogates), by file
UNDECODED = {"nd2_edge_picture.nd2": b"ImageMetadataSeqLV|0!"}
KINDS = {"TimeLoop": "t", "NETimeLoop": "t", "ZStackLoop": "z", "XYPosLoop": "p"}


@cache
def description(path: Path) -> dict[str, Any]:
    with read(path) as p:
        d = p.description
        assert d is not None
        d["_elements"] = {c["element"]: p.ir.path(c["element"]) for c in d["chunks"]}
        return d


def modern(f: nd2.ND2File) -> ModernReader:
    r = f._rdr
    assert isinstance(r, ModernReader), "the fixtures are no legacy files"
    return r


def decoded(reader: ModernReader, chunk: bytes, member: str) -> dict[str, Any] | None:
    """`member` of nd2's decoding of `chunk`, prefixes kept; None where either is absent."""
    if chunk not in reader.chunkmap:
        return None
    value = reader._decode_chunk(chunk, strip_prefix=False).get(member)
    return value if isinstance(value, dict) else None


def members(value: Any) -> list[Any]:
    """The members of an object or a list, in order (spec/zarrify/nd2.md §2.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nd2.md#22-lite-variant>)."""
    if isinstance(value, dict):
        return list(value.values())
    if isinstance(value, (list, bytes)):
        return list(value)
    return []


def valid(flags: Any, n: int) -> list[bool]:
    """Which of `n` members a validity list keeps: all, without one."""
    if flags is None:
        return [True] * n
    flags = members(flags)
    return [i < len(flags) and bool(flags[i]) for i in range(n)]


def loop_of(node: dict[str, Any]) -> dict[str, Any] | None:
    """A node's loop as spec/zarrify/nd2.md §3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nd2.md#3-metadata> reads it: kind, count, and
    period, step and flip, or stage; None without
    `uLoopPars`."""
    pars = node.get("uLoopPars")
    if not isinstance(pars, dict):
        return None
    e = node["eType"]
    if e == 1:
        return {"kind": "t", "count": pars.get("uiCount", 0), "period": float(pars.get("dPeriod", 0))}
    if e == 8:
        periods = members(pars.get("pPeriod"))
        kept = [p for p, v in zip(periods, valid(pars.get("pPeriodValid"), len(periods)), strict=True) if v]
        distinct = {float(p.get("dPeriod", 0)) for p in kept}
        return {"kind": "t", "count": sum(p["uiCount"] for p in kept),
                "period": distinct.pop() if len(distinct) == 1 else 0.0}
    if e == 2:
        points = members(pars.get("Points"))
        kept = [q for q, v in zip(points, valid(node.get("pItemValid"), len(points)), strict=True) if v]
        return {"kind": "p", "count": len(kept),
                "stage": [[_float(q.get("dPosX")), _float(q.get("dPosY"))] for q in kept]}
    if e == 4:
        count = pars.get("uiCount", 0)
        step, low, high = (float(pars.get(k, 0)) for k in ("dZStep", "dZLow", "dZHigh"))
        if step == 0 and count > 1:
            step = abs(high - low) / (count - 1)
        return {"kind": "z", "count": count, "step": abs(step), "flip": float(pars.get("dZStep", 0)) < 0}
    if e == 6:
        planes = pars.get("pPlanes")
        count = pars.get("uiCount", planes.get("uiCount", 0) if isinstance(planes, dict) else 0)
        return {"kind": "spectral", "count": count}
    raise AssertionError(f"eType {e}")


def _float(v: Any) -> float | None:
    return None if v is None else float(v)


def flattened(root: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The flattening of an experiment tree, rules 1 to 3 of spec/zarrify/nd2.md §3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nd2.md#3-metadata>."""
    loops: list[dict[str, Any]] = []

    def visit(node: dict[str, Any], depth: int) -> None:
        lp = loop_of(node)
        if lp is None or lp["count"] == 0:
            return
        child = depth
        if lp["kind"] != "spectral":
            child = depth + 1
            item = {**lp, "depth": depth}
            last = loops[-1] if loops else None
            if last is None or last["depth"] < depth:
                loops.append(item)
            elif (last["depth"], last["kind"]) == (depth, item["kind"]) and last["count"] < item["count"]:
                loops[-1] = item
        for c in members(node.get("ppNextLevelEx")):
            visit(c, child)

    if root is not None:
        visit(root, 0)
    return loops


def as_read(value: Any, default: Any) -> Any:
    return default if value is None else value


def nd2_loops(f: nd2.ND2File) -> list[dict[str, Any]]:
    """nd2's own loops, as the description writes them."""
    out = []
    for lp in f.experiment:
        item: dict[str, Any] = {"kind": KINDS[lp.type], "count": lp.count, "depth": lp.nestingLevel}
        p = lp.parameters
        if isinstance(p, TimeLoopParams):
            item["period"] = p.periodMs
        elif isinstance(p, NETimeLoopParams):
            distinct = {q.periodMs for q in p.periods}
            item["period"] = distinct.pop() if len(distinct) == 1 else 0.0
        elif isinstance(p, ZStackLoopParams):
            item |= {"step": abs(p.stepUm), "flip": p.stepUm < 0}
        elif isinstance(p, XYPosLoopParams):
            item["stage"] = [[q.stagePositionUm.x, q.stagePositionUm.y] for q in p.points]
        else:
            raise TypeError(lp.type)
        out.append(item)
    return out


def check(path: Path, seen: Counter) -> None:
    d = description(path)
    jsonschema.validate({k: v for k, v in d.items() if k != "_elements"}, SCHEMA)
    with nd2.ND2File(path) as f:
        r = modern(f)
        attrs = decoded(r, b"ImageAttributesLV!", "SLxImageAttributes")
        assert attrs is not None
        assert list(d["attributes"]) == list(ATTRIBUTES)
        for k, default in ATTRIBUTES.items():
            assert d["attributes"][k] == as_read(attrs.get(k), default), (path.name, k)
        if UNDECODED.get(path.name) == b"ImageMetadataSeqLV|0!":
            with pytest.raises(UnicodeDecodeError):
                decoded(r, b"ImageMetadataSeqLV|0!", "SLxPictureMetadata")
            assert d["picture"] is not None
            seen["nd2 undecoded"] += 1
            picture = None
        else:
            picture = decoded(r, b"ImageMetadataSeqLV|0!", "SLxPictureMetadata")
        if UNDECODED.get(path.name) == b"ImageMetadataSeqLV|0!":
            pass
        elif picture is None:
            assert d["picture"] is None, path.name
            seen["no picture"] += 1
        else:
            seen["picture"] += 1
            for k, default in PICTURE.items():
                assert d["picture"][k] == as_read(picture.get(k), default), (path.name, k)
            planes = picture.get("sPicturePlanes")
            if planes is None:
                assert d["picture"]["sPicturePlanes"] == {}, path.name
                seen["no planes"] += 1
            else:
                got = d["picture"]["sPicturePlanes"]
                count = planes.get("uiCount", 0)
                assert got["uiCount"] == count, path.name
                raw = planes.get("sPlaneNew") or {}
                want = {k: v for k, v in raw.items() if k.startswith("a") and k[1:].isdigit()
                        and (k == "a0" or not k[1:].startswith("0")) and int(k[1:]) < count}
                assert sorted(got["sPlaneNew"]) == sorted(want), path.name
                for k, plane in want.items():
                    for m, default in PLANE.items():
                        v = as_read(plane.get(m), default)
                        assert got["sPlaneNew"][k][m] == (v % 2**32 if m == "uiColor" else v), (path.name, k, m)
                        seen["negative color"] += m == "uiColor" and v < 0
                seen["planes"] += 1
        experiment = decoded(r, b"ImageMetadataLV!", "SLxExperiment")
        assert d["loops"] == flattened(experiment), path.name
        for lp in d["loops"]:
            seen[f"loop {lp['kind']}"] += 1
            seen["flipped"] += lp.get("flip", False)
            seen["stage coordinate absent"] += any(None in q for q in lp.get("stage", []))
        try:
            theirs = nd2_loops(f)
        except KeyError:  # a time loop without a member nd2 requires, such as `dStart`
            seen["nd2 fails"] += 1
        else:
            if OTHERWISE.get(path.name) == "differs":
                assert theirs != d["loops"], path.name
                seen["nd2 differs"] += 1
            else:
                # nd2 fills an absent stage coordinate with 0
                want = [{**lp, "stage": [[x or 0.0, y or 0.0] for x, y in lp["stage"]]} if "stage" in lp else lp
                        for lp in d["loops"]]
                assert theirs == want, path.name
        names = [c["name"] for c in d["chunks"]]
        order = [k.decode("latin-1") for k in r.chunkmap]
        assert [n for n in order if n in names] == names, path.name
        for c in d["chunks"]:
            element = d["_elements"][c["element"]]
            assert element.rsplit("/", 1)[-1] in ("lv", "xml"), (path.name, element)
            seen["xml chunk"] += element.endswith("/xml")
            seen["records"] += c["records"] > 0
        for k in DECISIONS:
            assert k not in d, (path.name, k)


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_the_description_is_what_nd2_reads(path):
    check(path, Counter())


def test_the_fixtures_reach_every_case():
    seen: Counter = Counter()
    for path in FILES:
        check(path, seen)
    assert seen["nd2 fails"] == 8
    assert seen["nd2 differs"] == 1
    assert seen["nd2 undecoded"] == 1
    for case in ("picture", "no picture", "no planes", "planes", "negative color", "loop t", "loop z", "loop p",
                 "flipped", "stage coordinate absent", "xml chunk", "records"):
        assert seen[case] > 0, case
