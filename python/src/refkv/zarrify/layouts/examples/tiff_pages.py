"""The example layout `refkv.example.tiff-pages`: every main-chain IFD of a TIFF as an image of
its own, its SubIFDs as further levels, built from the IR without `copy` (spec/layout.md
<https://github.com/d-v-b/refkv/blob/main/spec/layout.md>).

The output is plain Zarr v3: a root group, a group `<n>` for main-chain IFD `n` (`ifds/<n>`), and
in it an array `0` of the IFD itself and an array `<j + 1>` of each of its SubIFDs
(`ifds/<n>/subifds/<j>`). Of a file read as a store, each file's pages are under a group named by
the file's key. An array's axes are `(y, x)`, or `(c, y, x)` when a pixel has several samples
(interleaved samples are stored `(y, x, c)` and undone by a `transpose`), its chunks the IFD's
tiles or strips, one each: a tile or strip of byte count 0 is a missing chunk (the fill value
0), and a short last strip makes the `rectilinear` grid (uncompressed strips are cut to exactly
their rows). Each array's attributes name its IFD: `{"ifd": "ifds/3/subifds/0"}`.

It writes no OME-Zarr metadata: a TIFF's pages need not be one image (an SVS file's label and
macro are pages beside its pyramid), nor its SubIFDs a pyramid of known scales, and an axis
type or unit would be a guess; `ome-zarr-06` is the example that declares them.

An IFD it cannot present (no image size; tables the IR does not hold, which the parser leaves
unread for a large IFD outside the image it presents; a tile outside the file; codings that
`refkv.zarrify.coding` refuses; EER in strips whose last is short, which its codec's one chunk
shape does not decode) is `Rejected`, naming it, unless the option
`skip` is true: then it is left out, and the root's attribute `skipped` maps its IR path to why.
Options: `{"skip": false}`.

`check` finds `copy` on its output, and nothing else: where the base stacks an IFD with others
(a plane of an OME-TIFF, an EER frame), this layout's array of it holds only the base's
references, built by hand around them with a shape that is no `copy` of the base's array.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any, ClassVar

from refkv.ir import Kind, Value
from refkv.zarrify import Rejected
from refkv.zarrify.coding import NoCodec, tiff_byte_order, tiff_codecs, tiff_data_type, tiff_tags
from refkv.zarrify.layouts import Base, Entries, EntryValue, cut
from refkv.zarrify.layouts.hierarchy import array, group

PAGE = re.compile(r"ifds/(\d+)")  # a main-chain IFD's name in its parent
SUBIFD = re.compile(r"subifds/(\d+)")
EER = frozenset({65000, 65001, 65002})


class Unfit(Exception):  # noqa: N818 (internal: why an IFD is not presented)
    """An IFD this layout cannot present, and why."""


def _one(tags: Mapping[int, Value | None], tag: int, default: int | None = None) -> int | None:
    """A scalar tag's value (the first of a count-1 tag's list), or `default` when absent."""
    value = tags.get(tag)
    if value is None:
        return default
    first = value[0] if isinstance(value, list) and value else value
    return first if isinstance(first, int) else default


def _level(base: Base, i: int, to: str) -> Entries:
    """The entries of the array of the IFD that is IR element `i`, at the path `to`."""
    ir = base.ir
    if ir.kind(i) == Kind.ALIAS:  # an IFD read twice: its tags are its target's
        i = ir.target(i)
    try:
        tags = tiff_tags(base, i)
    except KeyError as e:
        raise Unfit("it has no tags") from e
    width, length = _one(tags, 256), _one(tags, 257)
    if not width or not length:
        raise Unfit("it has no ImageWidth and ImageLength: it is not an image")
    spp = _one(tags, 277, 1) or 1
    interleaved = spp > 1 and _one(tags, 284, 1) == 1
    tiled = 322 in tags and 324 in tags
    if tiled:
        name, tables, (offsets, counts) = "tiles", "TileOffsets and TileByteCounts", (tags.get(324), tags.get(325))
        rows, cols = _one(tags, 323), _one(tags, 322)
    else:
        name, tables, (offsets, counts) = "strips", "StripOffsets and StripByteCounts", (tags.get(273), tags.get(279))
        rows, cols = min(_one(tags, 278, 2**32 - 1) or 0, length), width
    if not rows or not cols:
        raise Unfit(f"its {name} have no size")
    table = ir.child(i, name)
    if not ({324, 325} if tiled else {273, 279}) <= tags.keys():
        raise Unfit(f"it has no {tables}")
    if not isinstance(offsets, list) or not isinstance(counts, list) or table is None:
        raise Unfit(f"the IR does not hold its {tables}")
    across, down = math.ceil(width / cols), math.ceil(length / rows)
    planes = 1 if spp == 1 or interleaved else spp
    if len(offsets) != len(counts) or len(counts) != across * down * planes:
        raise Unfit(f"it has {len(counts)} {name}, not the {across * down * planes} its size makes")
    last = length - (down - 1) * rows  # the last strip's rows
    compression = _one(tags, 259, 1)
    if compression in EER and not tiled and last != rows:
        raise Unfit("its last EER strip is short, which the codec's one chunk shape does not decode")
    row_length = cols * (spp if interleaved else 1)
    try:
        data_type = tiff_data_type(tags)
        codecs = tiff_codecs(tags, little_endian=tiff_byte_order(base, i), x_axis=1 if spp == 1 or interleaved else 2,
                             row_length=row_length, chunk_shape=[rows, cols])
    except NoCodec as e:
        raise Unfit(str(e)) from e
    row_bytes = math.ceil(row_length * (_one(tags, 258, 1) or 1) / 8)
    members = base.members(table)  # (number, start, length); an alias's are its target's
    held = {k for k, _, _ in members}
    for k, n in enumerate(counts):
        if n and k not in held:
            raise Unfit(f"{name[:-1]} {k} does not lie within the file")
    chunks: dict[tuple[int, ...], EntryValue] = {}
    for member, (k, _, _) in enumerate(members):
        plane, j = divmod(k, across * down)
        row, col = divmod(j, across)
        try:
            value = base.reference(table, member)
        except ValueError as e:  # bytes no range of a source reads
            raise Unfit(str(e)) from e
        if compression == 1:  # exactly its rows: what `bytes` decodes, on either grid
            want = (last if row == down - 1 and not tiled else rows) * row_bytes
            if value.size < want:
                raise Unfit(f"{name[:-1]} {k} holds {value.size} bytes, fewer than its {want}")
            value = cut(value, 0, want)
        chunks[(plane, row, col) if spp > 1 else (row, col)] = value
    # tiles are padded; a short last strip makes a rectilinear grid, as a run and the last
    y: int | list[int | list[int]] = rows if tiled or last == rows else [[rows, down - 1], last]
    attributes = {"ifd": ir.path(i)}
    if spp == 1:
        return array([length, width], data_type, [y, cols], codecs, chunks, ["y", "x"], attributes, to=to)
    if interleaved:
        codecs = [{"name": "transpose", "configuration": {"order": [1, 2, 0]}}, *codecs]
    return array([spp, length, width], data_type, [spp if interleaved else 1, y, cols], codecs, chunks,
                  ["c", "y", "x"], attributes, to=to)


class TiffPages:
    """Every main-chain IFD of a TIFF as an image of its own (the module's docstring)."""

    name = "refkv.example.tiff-pages"
    formats: ClassVar[Mapping[str, frozenset[int]]] = {"tiff": frozenset({0})}

    def options(self, given: Mapping[str, Any]) -> dict[str, Any]:
        """`{"skip": false}`: whether an IFD it cannot present is left out rather than Rejected."""
        unknown = sorted(set(given) - {"skip"})
        if unknown:
            raise ValueError(f"unknown options {unknown}: the one option is skip")
        skip = given.get("skip", False)
        if not isinstance(skip, bool):
            raise ValueError(f"skip is true or false, not {skip!r}")  # noqa: TRY004 (options() refuses with ValueError)
        return {"skip": skip}

    def write(self, base: Base, options: Mapping[str, Any]) -> Entries:
        ir = base.ir
        # the files: the IR's root, or each file (object) of a store the IR describes
        files = [(f, f"{ir.path(f)}/") for f in range(len(ir)) if ir.kind(f) == Kind.OBJECT] or [(0, "")]
        out: dict[str, EntryValue] = {}
        skipped: dict[str, str] = {}
        for root, prefix in files:
            pages = sorted((int(m[1]), c) for c in ir.children(root) if (m := PAGE.fullmatch(ir.name(c))))
            for n, page in pages:
                subifds = sorted((int(m[1]) + 1, c) for c in ir.children(page) if (m := SUBIFD.fullmatch(ir.name(c))))
                for k, i in [(0, page), *subifds]:
                    try:
                        out |= _level(base, i, f"{prefix}{n}/{k}")
                    except Unfit as e:
                        if not options["skip"]:
                            raise Rejected(f"{ir.path(i)}: {e} (the option skip leaves it out)") from e
                        skipped[ir.path(i)] = str(e)
        for path in sorted({k.rsplit("/", 2)[0] for k in out if k.endswith("/zarr.json")}):
            parts = path.split("/")
            for d in range(1, len(parts) + 1):  # the page's group, and a file's above it
                out.setdefault(f"{'/'.join(parts[:d])}/zarr.json", group())
        out["zarr.json"] = group({"skipped": skipped} if skipped else None)
        return dict(sorted(out.items()))
