"""The rebuild test of HDF5 (the design of d-v-b/refkv#278, "Rebuilding refkv's own output"): a
projection of an HDF5 input written here, in Python, with the public tools of `refkv.build` alone,
writes what `zarrify` writes for every HDF5 fixture, a file or a store of one with the files it
names: the same source table, keys, documents, references, IR mirror and placement, and it rejects
the inputs `zarrify` rejects. The convention's decisions are the routine `hdf5_hierarchy`'s, made
from the parser's description; what the projection adds is the layout: spec/zarrify/hdf5.md §2
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#2-the-hierarchy>, each node with its
declaration (§2.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#21-declaration-and-attributes>),
and the IR mirror (§7.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#73-the-ir-mirror>).

It uses neither `project` nor a copy of a built-in array: refkv's own output is made to raise
while the projection runs."""

import json
from pathlib import Path
from typing import Any

import pytest

from refkv import ReferenceStore, _core
from refkv.build import Parsed, Written, declaration, filled, hdf5_hierarchy, mirror, read, seal
from refkv.zarrify import Rejected, zarrify
from refkv.zarrify.layouts.hierarchy import group

FIXTURES = sorted(p for p in (Path(__file__).resolve().parents[2] / "fixtures" / "hdf5").iterdir()
                  if p.suffix in (".h5", ".nwb"))


def projected(parsed: Parsed) -> Written:
    """An HDF5 input's output as spec/zarrify/hdf5.md specifies it, from the HDF5 convention's
    hierarchy, the declarations and the IR mirror."""
    hierarchy = hdf5_hierarchy(parsed)
    out = Written()
    for path in hierarchy.nodes():
        prefix = f"{path}/" if path else ""
        attributes = declaration(parsed, path, own=hierarchy.source_metadata(path),
                                 attributes=hierarchy.attributes(path), url=hierarchy.url)
        if hierarchy.node_type(path) == "group":
            out[f"{prefix}zarr.json"] = group(attributes)
            continue
        doc, chunks = hierarchy.array_entries(path)
        out[f"{prefix}zarr.json"] = json.dumps({**doc, "attributes": attributes}).encode()
        # an edge chunk the convention copies is the bytes it holds
        out.update({f"{prefix}{k}": filled(v) for k, v in chunks.items()})
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
def chunking(request):
    """The chunking whose payload past which an edge chunk is copied is refkv's, or one small
    enough that the fixtures' edge chunks are copied."""
    return _core.Chunking(copy_threshold=request.param)


@pytest.mark.parametrize("checksums", [False, True], ids=["plain", "checksums"])
def test_a_projection_written_with_the_public_tools_writes_what_zarrify_writes(monkeypatch, chunking, checksums):
    """Every HDF5 fixture: groups, links and attributes past their budget, every layout and
    filter, chunks a filter skipped, strings, references and records, dimension scales and netCDF-4
    phony dimensions, NWB files, user blocks, files read as stores of the files their external
    links and external data name, and the inputs the profile rejects."""
    def rebuilt(path: Path) -> ReferenceStore:
        async def no_output(_self):
            raise AssertionError("refkv's own output was built")

        with monkeypatch.context() as m, read(str(path), checksums=checksums, chunking=chunking) as parsed:
            m.setattr(Parsed, "_output_async", no_output)
            return seal(projected(parsed), [parsed])

    seen = dict.fromkeys(("output", "rejected", "store"), 0)
    for path in FIXTURES:
        want = outcome(lambda path=path: zarrify(str(path), checksums=checksums, chunking=chunking))
        got = outcome(lambda path=path: rebuilt(path))
        assert got == want, path.name
        seen[got[0]] += 1
        # a store's output names its files, each a source of its own
        seen["store"] += got[0] == "output" and len([s for s in got[1]["sources"] if s[0]]) > 1
    assert all(seen.values()), seen
