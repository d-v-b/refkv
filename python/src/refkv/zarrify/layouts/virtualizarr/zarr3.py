"""How the layout `refkv.virtualizarr` presents a Zarr v3 store, as VirtualiZarr 2.8.0's
`ZippedZarrParser` does (`virtualizarr/parsers/zarr/`).

**Zarr v3**, zipped (`ZippedZarrParser`) or listed: the store's hierarchy as refkv's own output
has it, each node with the attributes it keeps in place.

Where `ZippedZarrParser`'s presentation is a defect rather than a contract, the layout keeps
refkv's:

- a zipped Zarr store's own chunk key encoding, where the `ManifestStore` of `ZippedZarrParser`
  presents the default encoding with a `.` separator."""

import json

from refkv.build import Parsed, Written
from refkv.build.routines.zarr import ZarrHierarchy, zarr_hierarchy
from refkv.zarrify.layouts import dump, undeclared


def _zarr3(h: ZarrHierarchy) -> Written:
    """Each node the Zarr v3 convention presents, with the attributes it keeps in place but refkv's
    declarations, and each array's chunks. A document written as refkv's own output writes it (a
    node refkv declares nothing on, whose `dump` is refkv's text) keeps that output's placement:
    read at open."""
    out = Written()
    for path in h.nodes():
        key = f"{path}/zarr.json" if path else "zarr.json"
        written = h.document_bytes(key)
        doc = json.loads(written)
        doc["attributes"] = undeclared(doc.get("attributes", {}))
        out[key] = dump(doc)
        if path and h.source_metadata(path) is None and out[key] == written:
            out.at_open[key] = True
        if h.node_type(path) == "array":
            prefix = f"{path}/" if path else ""
            out.update({f"{prefix}{k}": v for k, v in h.array_entries(path)[1].items()})
    return out


def write(parsed: Parsed, token: bool) -> Written:  # noqa: ARG001 (every profile's writer takes it)
    """A Zarr v3 store as `ZippedZarrParser` presents it; `token` whether a NaN or infinite attribute
    is a bare JSON token (the option `nonfinite`)."""
    return _zarr3(zarr_hierarchy(parsed))

