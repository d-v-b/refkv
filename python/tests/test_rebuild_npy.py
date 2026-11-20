"""The rebuild test of NumPy (the design of d-v-b/refkv#278, "Rebuilding refkv's own output"): a
projection of a NumPy file written here, in Python, with the public tools of `refkv.build` and
`refkv.zarrify.coding` alone, writes what `zarrify` writes for every NumPy fixture: the same
source table, keys, documents, references, IR mirror, declarations and placement, and it rejects
the inputs `zarrify` rejects. So the tools carry every decision of the convention but its layout,
which is what the projection below spells out: spec/zarrify/npy.md §4
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#4-the-hierarchy>.

It uses neither `project` nor a copy of a built-in array: refkv's own output is made to raise
while the projection runs."""

import json
import struct
from pathlib import Path
from typing import Any

import pytest

from refkv import ReferenceStore, _core
from refkv.build import (
    Array,
    Parsed,
    Ref,
    Written,
    declaration,
    filled,
    grid,
    gzip_member,
    mirror,
    read,
    seal,
)
from refkv.zarrify import Rejected, zarrify
from refkv.zarrify.coding import gzip_codecs, npy_array_type
from refkv.zarrify.layouts.hierarchy import group

FIXTURES = sorted(p for p in (Path(__file__).resolve().parents[2] / "fixtures" / "npy").iterdir()
                  if p.suffix in (".npy", ".npz"))
# the most bytes of a chunk
# (spec/zarrify/npy.md §8.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#84-chunks>)
CHUNK_BYTES = 1 << 20
# the level the convention names for a deflated member's stream
# (spec/zarrify/npy.md §4.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#43-the-arrays>)
GZIP_LEVEL = 6


def data_of(parsed: Parsed, position: dict[int, int], header: int) -> tuple[list[int], bool, Ref | None, int, int]:
    """An array as the IR describes it: its shape in storage order, whether that is its header's
    reversed, and its data, its stored array's one unit: the range of the file that holds it, or,
    for a deflated member, None, its data lying in what the member's stream inflates to; then the
    data's offset there and its length.

    An array whose description has no grid (a `descr` NumPy gives no size, which the profile
    presents all the same) has its data in the `data` element beside its header, and an array of
    no elements has none, at its header's end."""
    k = position[header]
    stored = parsed.ir.stored_arrays().arrays[k]
    assert stored.shape is not None
    units = parsed.ir.units(k)
    beside = parsed.ir.at(f"{parsed.ir.path(header).rpartition('/')[0]}/data".lstrip("/"))
    if units:
        assert units[0].element is not None
        start, length = parsed.ir.extent(units[0].element)
    elif beside is not None:
        start, length = parsed.ir.extent(beside)
    else:
        start, length = sum(parsed.ir.extent(header)), 0
    span = Ref(parsed, 0, start, length) if parsed.ir.space(header) == 0 else None
    return list(stored.shape), stored.axes is not None, span, start, length


def projected(parsed: Parsed) -> Written:
    """A NumPy file's output as spec/zarrify/npy.md specifies it, from the parser's description,
    the IR's stored arrays and the routines."""
    description: Any = parsed.description
    position = {a.element: k for k, a in enumerate(parsed.ir.stored_arrays().arrays)}
    out = Written({"zarr.json": group(declaration(parsed, own=description["own"]))})
    for entry in description["arrays"]:
        declared = npy_array_type(entry["header"])
        rank = len(declared["shape"])
        # fixed-length bytes are uint8 along a trailing axis
        inner = [declared["trailing"]] if declared["trailing"] is not None else []
        stored, reverse, span, start, length = data_of(parsed, position, entry["stored"])

        def ordered(coords: tuple[int, ...], rank: int = rank, reverse: bool = reverse) -> tuple[int, ...]:
            """Storage order's coordinates in the array's: the header's axes reversed, a trailing axis last."""
            return (*coords[:rank][::-1], *coords[rank:]) if reverse else coords

        shape = [*ordered(tuple(stored)), *inner]
        codecs: list[dict[str, Any]] = []
        if reverse:
            codecs.append({"name": "transpose", "configuration": {"order": [*range(rank)][::-1] + [rank] * len(inner)}})
        endian = declared["endian"]
        codecs.append({"name": "bytes", "configuration": {"endian": endian}} if endian else {"name": "bytes"})
        chunks: dict[tuple[int, ...], Any]
        if span is not None:
            # cut as the conventions cut contiguous values (`grid`), in storage order
            cut_shape, cut = grid(span, [*stored, *inner], 1 if inner else declared["item_size"], limit=CHUNK_BYTES)
            chunk_shape = list(ordered(tuple(cut_shape)))
            chunks = {ordered(coords): filled(v) for coords, v in cut.items()}
        else:
            # one chunk, the member's deflate stream as a gzip member, its header skipped:
            # the stream is the derived element whose space the header is in
            stream = parsed.encoded(parsed.ir.space(entry["stored"]))
            member = entry["deflate"]
            chunk_shape = [max(n, 1) for n in shape]
            chunks = {}
            if length:
                chunks[(0,) * len(shape)] = gzip_member(stream, crc32=member["crc"], size=member["size"])
            codecs += gzip_codecs(start, GZIP_LEVEL)
        array = Array(shape, declared["data_type"], chunk_shape, codecs, chunks, declared["fill_value"],
                      [*(f"dim_{k}" for k in range(rank)), *(["byte"] if inner else [])])
        out |= array.entries(to=entry["path"])
    return out | mirror(parsed)


def held(store: ReferenceStore) -> dict[str, Any]:
    """What an output is, for comparing two (spec/zarrify.md §1.1
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#11-output-and-equivalence>): its
    source table, its entries (a `zarr.json` by its JSON value), and their placement."""
    entries = {k: json.loads(v) if isinstance(v, bytes) and k.rpartition("/")[2] == "zarr.json" else v
               for k, v in store.mapping.items()}
    return {"sources": [(s.url, s.size, s.etag, s.data) for s in store.sources], "entries": entries,
            "read_at_open": store.read_at_open, "compressed": store.compressed}


def outcome(make: Any) -> tuple[str, Any]:
    try:
        return "output", held(make())
    except Rejected as e:
        return "rejected", str(e)


@pytest.fixture(params=[None, 64], ids=["payload-limit", "copied-edges"])
def copy_threshold(request):
    """The payload past which an edge chunk is copied: refkv's, and one small enough that the
    fixtures' edge chunks are copied, which no fixture is large enough for otherwise."""
    _core._set_copy_threshold(request.param)
    yield request.param
    _core._set_copy_threshold(None)


def sizeless(tmp_path: Path) -> Path:
    """A `.npy` file of the older type code `a5`, which the profile presents as it does `S5` and
    NumPy gives no size, so that its stored array has no grid: the one kind of presented array no
    fixture holds (NumPy 2 does not read it, and the fixtures' tests compare with NumPy)."""
    text = b"{'descr': '|a5', 'fortran_order': False, 'shape': (3,), }"
    header = text + b" " * (-(10 + len(text) + 1) % 64) + b"\n"
    path = tmp_path / "bytes_a.npy"
    path.write_bytes(b"\x93NUMPY\x01\x00" + struct.pack("<H", len(header)) + header + b"alphabravocharl")
    return path


@pytest.mark.parametrize("checksums", [False, True], ids=["plain", "checksums"])
def test_a_projection_written_with_the_public_tools_writes_what_zarrify_writes(monkeypatch, tmp_path, copy_threshold,
                                                                              checksums):
    """Every NumPy fixture: `.npy` files and archives, either order and byte order, fixed-length
    bytes, structures, scalars, arrays of no elements, arrays cut into chunks with a padded or a
    copied edge, deflated members, members not presented, and the inputs the profile rejects; and
    an array whose stored array has no grid."""
    def rebuilt(path: Path) -> ReferenceStore:
        async def no_output(_self):
            raise AssertionError("refkv's own output was built")

        with monkeypatch.context() as m, read(str(path), checksums=checksums) as parsed:
            m.setattr(Parsed, "_output_async", no_output)
            return seal(projected(parsed), [parsed])

    seen: dict[str, int] = dict.fromkeys(("output", "rejected", "copied", "padded", "deflated", "transposed"), 0)
    for path in [*FIXTURES, sizeless(tmp_path)]:
        want = outcome(lambda path=path: zarrify(str(path), checksums=checksums))
        got = outcome(lambda path=path: rebuilt(path))
        assert got == want, path.name
        assert path.parent != tmp_path or "array/c/0/0" in got[1]["entries"], "the array with no grid has its chunk"
        seen[got[0]] += 1
        if got[0] == "output":
            entries = got[1]["entries"]
            documents = [v for k, v in entries.items() if k.count("/") == 1 and k.endswith("/zarr.json")]
            seen["deflated"] += any(c["name"] == "gzip" for d in documents for c in d.get("codecs", []))
            seen["transposed"] += any(c["name"] == "transpose" for d in documents for c in d.get("codecs", []))
            chunks = [v for k, v in entries.items() if "/c/" in k and not k.startswith("refkv_source/")]
            seen["copied"] += any(isinstance(v, bytes) for v in chunks)
            seen["padded"] += any(not isinstance(v, bytes) and len(getattr(v, "parts", ())) == 2 for v in chunks)
    assert all(seen[kind] for kind in ("output", "rejected", "deflated", "transposed")), seen
    assert seen["copied" if copy_threshold else "padded"], seen
