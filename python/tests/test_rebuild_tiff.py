"""The rebuild test of TIFF (the design of d-v-b/refkv#278, "Rebuilding refkv's own output"): a
projection of a TIFF, QPTIFF or NDPI input written here, in Python, with the public tools of
`refkv.build` alone, writes what `zarrify` writes for every fixture of the TIFF family, a file or an
image across files: the same source table, keys, documents, references, IR mirror and placement,
and it rejects the inputs `zarrify` rejects. The convention's decisions are the routine
`ome_image`'s, made from the parser's description through refkv's plan; what the projection adds is
the layout: the image at the root (spec/zarrify/tiff.md §4
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#4-the-image>), each node with its
declaration (§1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#1-declaration>), and the
IR mirror (§5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#5-source-metadata>).

It uses neither `project` nor a copy of a built-in array: refkv's own output is made to raise
while the projection runs."""

import json
from pathlib import Path
from typing import Any

import pytest

from refkv import ReferenceStore
from refkv.build import Parsed, Written, declaration, filled, mirror, ome_image, read, seal
from refkv.zarrify import Rejected, zarrify
from refkv.zarrify.layouts.hierarchy import group

ROOT = Path(__file__).resolve().parents[2] / "fixtures"
FIXTURES = sorted(p for d in ("tiff", "qptiff", "ndpi") for p in (ROOT / d).iterdir() if p.is_file())


def projected(parsed: Parsed) -> Written:
    """A TIFF-family input's output as its convention specifies it, from the convention's image,
    the declarations and the IR mirror."""
    image = ome_image(parsed)
    out = Written()
    for path in image.nodes():
        prefix = f"{path}/" if path else ""
        attributes = declaration(parsed, path, own=image.source_metadata(path), attributes=image.attributes(path),
                                 url=image.url)
        if image.node_type(path) == "group":
            out[f"{prefix}zarr.json"] = group(attributes)
            continue
        doc, chunks = image.array_entries(path)
        out[f"{prefix}zarr.json"] = json.dumps({**doc, "attributes": attributes}).encode()
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


@pytest.mark.parametrize("checksums", [False, True], ids=["plain", "checksums"])
def test_a_projection_written_with_the_public_tools_writes_what_zarrify_writes(monkeypatch, checksums):
    """Every TIFF, QPTIFF and NDPI fixture: tiles and strips, joined, padded (with zeros or an LZW
    tail) and sharded strips, rectilinear grids, pyramids, OME-XML, ImageJ and Micro-Manager planes,
    EER frames, JPEG, JPEG XR and NDPI restart intervals, images across files, and the inputs the
    profiles reject, whether the parser or refkv's plan rejects them."""
    def rebuilt(path: Path) -> ReferenceStore:
        async def no_output(_self):
            raise AssertionError("refkv's own output was built")

        with monkeypatch.context() as m, read(str(path), checksums=checksums) as parsed:
            m.setattr(Parsed, "_output_async", no_output)
            return seal(projected(parsed), [parsed])

    seen = dict.fromkeys(("output", "rejected", "planned rejection", "set", "tiff", "qptiff", "ndpi"), 0)
    for path in FIXTURES:
        want = outcome(lambda path=path: zarrify(str(path), checksums=checksums))
        got = outcome(lambda path=path: rebuilt(path))
        assert got == want, path.name
        seen[got[0]] += 1
        if got[0] == "output":
            seen[got[1]["entries"]["zarr.json"]["attributes"]["refkv_zarrified"]["profile"]] += 1
            # an image across files names its files, each a source of its own
            seen["set"] += len([s for s in got[1]["sources"] if s[0]]) > 1
        else:
            # a rejection the parser lets through, which refkv's plan makes
            try:
                read(str(path)).close()
            except Rejected:
                continue
            seen["planned rejection"] += 1
    assert all(seen.values()), seen
