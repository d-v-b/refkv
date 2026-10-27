"""Store inputs (spec/zarrify.md §1.4
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>–§1.6
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#16-json-documents>): the store
machinery, one module per concern, whose names this package gathers.

- `urls`: a store URL's checks, the URL of each object (`object_url`), and the listing
  endpoint of an S3-style store.
- `listing`: the XML subset a listing is read in, and the objects of one ListObjectsV2
  response (`parse_listing`).
- `documents`: the strict JSON reader of metadata documents (`parse_json`), and its limits.
- `stores`: the store readers (`Store`, `HttpStore`, `DirStore`), their prefetch of
  documents, `open_store`, and the store profile a store's root keys select
  (`choose_profile`).
- `beside`: the files of a VSI or NRRD input, as a store the Rust core probes (`VsiFiles`);
  not gathered here, since it reads through `refkv.ir.planner`.

The Rust core parses every store format (rust/refkv-ir/src/zarr2.rs, n5.rs, safe/, vsi/), driven
by `refkv.ir.store`. What is left here of the formats is what the frozen reference profiles
(conformance/zarrify/reference) share with the shipped code: `hierarchy`, the nodes and chunk
objects of a Zarr v2 or N5 store, `blosc`, the blosc codec of their compressors, and `output`,
their output (`StoreOutput`).
"""

from __future__ import annotations

from refkv.zarrify.store.blosc import BLOSC_NAMES, MAX_BLOCK, blosc_codec
from refkv.zarrify.store.documents import MAX_DEPTH, MAX_DOCUMENT, MAX_SAFE, as_int, is_number, num, parse_json
from refkv.zarrify.store.hierarchy import (
    PathTrie,
    canonical_index,
    classify,
    depth,
    directories_of,
    find_chunks,
    grid,
    implicit_groups,
    join,
    parent_of,
)
from refkv.zarrify.store.listing import Element, parse_listing, parse_xml
from refkv.zarrify.store.output import (
    EMPTY_KEY,
    GROUP_IMPLICIT,
    OBJECTS,
    SOURCE_GROUP,
    StoreOutput,
    doc_key,
    object_key,
    source_metadata,
)
from refkv.zarrify.store.stores import (
    PREFETCH_BYTES,
    WORKERS,
    DirStore,
    HttpStore,
    ListingFailed,
    Opener,
    Read,
    Response,
    Store,
    StoreLimit,
    choose_profile,
    open_store,
)
from refkv.zarrify.store.urls import check_store_url, listing_endpoint, object_url, query_encode

__all__ = [
    "BLOSC_NAMES",
    "EMPTY_KEY",
    "GROUP_IMPLICIT",
    "MAX_BLOCK",
    "MAX_DEPTH",
    "MAX_DOCUMENT",
    "MAX_SAFE",
    "OBJECTS",
    "PREFETCH_BYTES",
    "SOURCE_GROUP",
    "WORKERS",
    "DirStore",
    "Element",
    "HttpStore",
    "ListingFailed",
    "Opener",
    "PathTrie",
    "Read",
    "Response",
    "Store",
    "StoreLimit",
    "StoreOutput",
    "as_int",
    "blosc_codec",
    "canonical_index",
    "check_store_url",
    "choose_profile",
    "classify",
    "depth",
    "directories_of",
    "doc_key",
    "find_chunks",
    "grid",
    "implicit_groups",
    "is_number",
    "join",
    "listing_endpoint",
    "num",
    "object_key",
    "object_url",
    "open_store",
    "parent_of",
    "parse_json",
    "parse_listing",
    "parse_xml",
    "query_encode",
    "source_metadata",
]
