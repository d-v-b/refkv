"""The GRIB profile's chunks against the stored arrays' descriptions (spec/stored-array.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): what the profile
writes of each field's storage, read off the field's description by the profile's rules
(spec/zarrify/grib.md §8.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#85-chunks>,
§3.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#31-variables> and §2.6
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#26-what-is-not-presented>), is what
the profile wrote. Of each field's description the test derives

- its chunk's parts: the field's message whole, the extent of the description's key, where the
  message declares no other field; else a message made of the field's sections, each a range of
  the file: section 3 and section 5, the description's first two declarations; section 6, the one
  that holds the description's third declaration, or, where a bitmap's bits are its fourth, the
  section that holds those (an earlier field's, under indicator 254); and section 7, the element
  of the description's row;
- its chunk's shape along the grid: the description's shape in the order of rows, or, for a field
  with a bitmap, whose description is its coded values, the rows and columns or the points that
  its section 3 declaration gives;
- its array's codecs: `refkv.grib` with no configuration, since each chunk is a message of the one
  field;
- whether it is presented, where the reason is its packing, its bitmap or its number of values:
  the template of its section 5 declaration, the indicator of its section 6 declaration with or
  without a bitmap's bits among the declarations, and the numbers of values and of points of its
  sections 5 and 3.

What no description holds is taken from elsewhere, and is not derived:

- which variable a field is in, and at which index along `time`, `step`, a level and `number`:
  the profile's reading of sections 1 and 4. The chunks are matched to the fields by their parts,
  each chunk one field's and each presented field one chunk's, and a chunk's key is compared only
  along the grid's dimensions, where it is 0;
- sections 1, 2 and 4 of a chunk made of sections, which declare nothing of the values: they are
  the last of each number before the field's section 7 among the message's `sections/<j>` elements
  of the IR, and the discipline of the literal section 0 is the message's seventh octet;
- section 7 of a field whose description has no row (a constant field): the section after its
  section 6 among those elements;
- a field the profile does not present for its grid, its product or its time (a reading of
  sections 3, 4 and 1): the root's `unpresented` list of the output says so;
- the array's data type and fill value, `float64` and NaN, which are the profile's choice for
  what its codec makes of every packing.

A second test asserts which cases the fixtures reached, and a third writes those they do not.
Where there is no ecCodes library, which writes two of the third test's cases, the module is
skipped, as `test_grib_stored`, whose helpers it imports, is."""

import json
from collections import Counter
from functools import cache
from pathlib import Path

import pytest
from described import Described, described
from test_grib_stored import (
    FILES,
    ROWS,
    edited,
    fixture,
    logarithms,
    message_of,
    octets,
    parts,
    section,
    uint,
    unread_grid,
    written,
)

from refkv._narrow import at, integer, mapping, sequence, string
from refkv.ir.store import StoreIrOutput
from refkv.ir.stored import ElementPlace, StoredArray

# the data representation templates `refkv.grib` decodes (spec/zarrify/grib.md §2.5
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#25-values>)
DECODED = (0, 2, 3, 4, 40, 41, 42)
# the reasons a field is not presented that its description gives
DERIVED = ("packing", "bitmap", "values")

type Part = bytes | tuple[str, str | None, int, int]
"""A part of a chunk: literal bytes, or a range of an object of the store"""


def section_of(d: Described, element: int) -> int:
    """The message's section an element is, or is in: a `sections/<j>` element."""
    while not d.ir.name(element).startswith("sections/"):
        element = d.ir.parent(element)
    return element


def elements(a: StoredArray) -> list[int]:
    """The elements of an array's declarations, each a whole element."""
    assert all(isinstance(p, ElementPlace) and p.range is None for p in a.declarations)
    return [p.element for p in a.declarations if isinstance(p, ElementPlace)]


def reason_of(d: Described, a: StoredArray) -> str | None:
    """Why the profile does not present a field, as far as its description says (the profile's
    reasons of the field's values, in its order): its packing is not one the codec decodes; its
    bitmap is predefined, or reuses one the message has not given; or its values are not its
    grid's points, with no bitmap."""
    s3, s5, s6 = (d.place(p) for p in a.declarations[:3])
    indicator = s6[5]
    if uint(s5[9:11]) not in DECODED:
        return "packing"
    if 1 <= indicator <= 253 or (indicator == 254 and len(a.declarations) == 3):
        return "bitmap"
    if indicator == 255 and uint(s5[5:9]) != uint(s3[6:10]):
        return "values"
    return None


def grid_of(d: Described, a: StoredArray, seen: Counter) -> list[int] | None:
    """A field's shape along its grid's dimensions, as the profile writes it: the rows and the
    points of each, or the points. The description's own shape where it is the grid's (every
    point has a value), else what the section 3 declaration gives; None where the two differ."""
    s3 = d.place(a.declarations[0])
    rows = uint(s3[12:14]) in ROWS and s3[10] == 0
    declared = [uint(s3[34:38]), uint(s3[30:34])] if rows else [uint(s3[6:10])]
    assert a.shape is not None
    if len(a.declarations) == 3:
        # no bitmap: the description is the grid, its columns first where it has no `axes`
        seen["grids the description's shape gives"] += 1
        mine = list(a.shape if a.axes or len(a.shape) == 1 else a.shape[::-1])
        return mine if mine == declared else None
    seen["grids the section 3 declaration gives, of a field with a bitmap"] += 1
    return declared


def chunk_of(d: Described, k: int, fields: int, seen: Counter) -> list[Part]:
    """The parts of the chunk of the field whose description is array `k`, of a message that
    declares `fields` fields."""
    a = d.stored.arrays[k]
    key = d.key(a.element)
    start, length = d.ir.extent(a.element)
    if fields == 1:
        seen["chunks of a whole message"] += 1
        return [("object", key, start, length)]
    declared = elements(a)
    s3, s5 = declared[0], declared[1]
    s6 = section_of(d, declared[3 if len(declared) == 4 else 2])
    seen["chunks with an earlier field's bitmap"] += s6 != section_of(d, declared[2])
    # the message's sections, as the IR holds them, each with its number, the fifth octet
    sections = [c for c in d.ir.children(a.element) if d.ir.name(c).startswith("sections/")]
    number = {c: d.objects[key][d.ir.extent(c)[0] + 4] for c in sections}
    rows = d.rows.get(k, [])
    if rows:
        s7 = rows[0]["element"]
    else:
        # no row names section 7: the section after the field's own section 6
        seen["chunks whose section 7 no row names"] += 1
        s7 = sections[sections.index(section_of(d, declared[2])) + 1]
    assert number[s7] == 7
    # sections 1, 2 and 4, which declare nothing of the values: the last of each before section 7
    last = {number[c]: c for c in sections[:sections.index(s7)]}
    assert (last[3], last[5]) == (s3, s5)
    made = [d.ir.extent(c) for c in (last[1], last.get(2), s3, last[4], s5, s6, s7) if c is not None]
    total = 16 + sum(n for _, n in made) + 4
    head = b"GRIB\0\0" + bytes([d.objects[key][start + 6], 2]) + total.to_bytes(8, "big")
    seen["chunks made of a field's sections"] += 1
    return [head, *(("object", key, o, n) for o, n in made), b"7777"]


def check(d: Described, seen: Counter) -> list[str]:
    """What differs between the chunks the profile wrote of a file and what the file's
    descriptions give by the profile's rules."""
    out = d.out
    assert isinstance(out, StoreIrOutput), "a GRIB file is read as a store"
    root = mapping(json.loads(out.entries["zarr.json"]))
    said = []
    # the fields the output says it does not present: (message, field) and the reason
    listed = {(integer(u["message"]), integer(u["field"])): string(u["reason"])
              for u in map(mapping, sequence(at(root["attributes"], "refkv_zarrified", "grib", "unpresented")))}
    # the chunks, by their parts
    written_at: dict[tuple, list[str]] = {}
    for key, chunk in sorted(out.refs.items()):
        written_at.setdefault(tuple(p if isinstance(p, bytes) else tuple(p) for p in chunk), []).append(key)
    # a message's fields, in order: the arrays its element keys that are no bitmaps
    by_message: dict[int, list[int]] = {}
    for k, a in enumerate(d.stored.arrays):
        if len(a.declarations) != 2:
            by_message.setdefault(a.element, []).append(k)
    for message, fields in sorted(by_message.items()):
        m = int(d.ir.name(message).split("/")[1])
        for f, k in enumerate(fields):
            here = f"message {m} field {f}"
            a = d.stored.arrays[k]
            seen["fields"] += 1
            reason = reason_of(d, a)
            if (m, f) in listed:
                # for its packing, its bitmap or its values, as the description says; or for its
                # grid, its product or its time, which the profile reads
                theirs = listed.pop((m, f))
                kind = "the description gives" if theirs in DERIVED else "the output's"
                seen[f"fields not presented, for a reason {kind}"] += 1
                if theirs in DERIVED and reason != theirs:
                    said.append(f"{here}: not presented for its {theirs}; the description gives {reason}")
                continue
            if reason is not None:
                said.append(f"{here}: presented, and the description gives the reason {reason}")
                continue
            want = tuple(chunk_of(d, k, len(fields), seen))
            keys = written_at.get(want, [])
            if not keys:
                said.append(f"{here}: no chunk of the parts {want}")
                continue
            key = keys.pop()
            # the chunk's array: its codecs, and its chunk's shape, 1 but along the grid
            array, _, coords = key.partition("/c/")
            doc = mapping(json.loads(out.entries[f"{array}/zarr.json"]))
            grid = grid_of(d, a, seen)
            shape = [integer(x) for x in sequence(at(doc["chunk_grid"], "configuration", "chunk_shape"))]
            if grid is None or shape != [1] * (len(shape) - len(grid)) + grid:
                said.append(f"{here}: chunks of {shape}, and the description's grid {grid}")
            elif coords.split("/")[-len(grid):] != ["0"] * len(grid):
                said.append(f"{here}: the chunk {key}, not the one of its grid")
            if doc["codecs"] != [{"name": "refkv.grib"}]:
                said.append(f"{here}: the codecs {doc['codecs']}")
            seen["chunks"] += 1
    left = sorted(k for keys in written_at.values() for k in keys)
    if left:
        said.append(f"chunks of no field's description: {left}")
    if listed:
        said.append(f"fields not presented that no description is of: {sorted(listed)}")
    return said


@cache
def compared(path: Path) -> tuple[tuple[str, ...], Counter]:
    """What differs between the chunks the profile wrote of the file at `path` and the file's
    descriptions, and what the comparison saw."""
    seen: Counter = Counter()
    return tuple(check(described(path), seen)), seen


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_the_grib_profile_writes_what_the_descriptions_give(path):
    """Every chunk the profile writes of the file is a field's, of the parts its description
    gives, in an array of the grid its description gives."""
    said, seen = compared(path)
    assert said == ()
    assert seen["chunks"] >= 1


def test_the_fixtures_hold_every_case_the_profile_is_compared_in():
    """The fixtures reach each case of the comparison, so that none of it passes for want of a
    file: every field of the fixtures is presented."""
    seen: Counter = Counter()
    for path in FILES:
        seen += compared(path)[1]
    assert {k: v for k, v in seen.items() if v} == {
        "fields": 79,
        "chunks": 79,
        "chunks of a whole message": 66,
        "chunks made of a field's sections": 13,
        "chunks with an earlier field's bitmap": 2,
        "grids the description's shape gives": 70,
        "grids the section 3 declaration gives, of a field with a bitmap": 9,
    }


def test_written_fields_are_presented_as_their_descriptions_give(tmp_path):
    """Cases no fixture holds. A message of two fields with a section 2 (local use), which each
    of its chunks then holds, whose first field is constant, so that no row names its section 7,
    which is then the section after its section 6. And fields the profile does not present: for
    their values (fewer than the grid's points, with no bitmap), for their bitmap (a predefined
    one; indicator 254 with none before) and for their packing (5.61), as the descriptions say;
    and for its grid (a template the parser does not read), which the output alone says."""
    simple = fixture("grib_simple")
    first, *rest = parts(fixture("grib_constant"))
    two = message_of(simple, [first, section(2, b"local"), *rest, *(s for s in parts(simple) if s[4] >= 4)])
    fewer = edited(edited(simple, 5, octets(6, (100).to_bytes(4, "big"))), 7, lambda data: bytes(data[:150]))
    predefined, alone = edited(fewer, 6, octets(6, bytes([5]))), edited(fewer, 6, octets(6, bytes([254])))
    path = written(tmp_path, "presented", two, fewer, predefined, alone, logarithms(), unread_grid())
    said, seen = compared(path)
    assert said == ()
    assert {k: v for k, v in seen.items() if v} == {
        "fields": 8,
        "chunks": 3,
        "chunks of a whole message": 1,
        "chunks made of a field's sections": 2,
        "chunks whose section 7 no row names": 1,
        "grids the description's shape gives": 3,
        "fields not presented, for a reason the description gives": 4,
        "fields not presented, for a reason the output's": 1,
    }
