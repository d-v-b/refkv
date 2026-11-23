"""Store inputs (spec/zarrify.md §1.4
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>–§1.6
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#16-json-documents>): the store
machinery, one module per concern, whose names this package gathers.

- `urls`: a store URL's checks, the URL of each object (`object_url`), the listing
  endpoint of an S3-style store, and the repository of a Hub store (`hub_repo`).
- `listing`: the XML subset a listing is read in, the objects of one ListObjectsV2
  response (`parse_listing`), and a Hub store's commit, tree page and next link.
- `documents`: the strict JSON reader of metadata documents (`parse_json`), and its limits.
- `stores`: the store readers (`Store`, `HttpStore`, `HubStore`, `DirStore`), their prefetch of
  documents, `open_store`, and the store profile a store's root keys select
  (`choose_profile`).
- `beside`: the files of a VSI or NRRD input, as a store the Rust core probes (`VsiFiles`);
  not gathered here, since it reads through `refkv.ir.planner`.

The Rust core parses every store format (rust/refkv-ir/src/zarr2.rs, n5.rs, safe/, vsi/), driven
by `refkv.ir.store`, and writes their outputs (`refkv.ir.store.StoreIrOutput`).
"""

from __future__ import annotations

from refkv.zarrify.store.documents import MAX_DEPTH, MAX_DOCUMENT, MAX_SAFE, as_int, is_number, num, parse_json
from refkv.zarrify.store.listing import Element, next_link, parse_hub_revision, parse_hub_tree, parse_listing, parse_xml
from refkv.zarrify.store.stores import (
    PREFETCH_BYTES,
    WORKERS,
    DirStore,
    HttpStore,
    HubStore,
    ListingFailed,
    Opener,
    Read,
    Response,
    Store,
    StoreLimit,
    choose_profile,
    open_store,
)
from refkv.zarrify.store.urls import (
    HUB,
    HubRepo,
    check_store_url,
    hub_endpoint,
    hub_page,
    hub_repo,
    listing_endpoint,
    object_url,
    query_encode,
)

__all__ = [
    "HUB",
    "MAX_DEPTH",
    "MAX_DOCUMENT",
    "MAX_SAFE",
    "PREFETCH_BYTES",
    "WORKERS",
    "DirStore",
    "Element",
    "HttpStore",
    "HubRepo",
    "HubStore",
    "ListingFailed",
    "Opener",
    "Read",
    "Response",
    "Store",
    "StoreLimit",
    "as_int",
    "check_store_url",
    "choose_profile",
    "hub_endpoint",
    "hub_page",
    "hub_repo",
    "is_number",
    "listing_endpoint",
    "next_link",
    "num",
    "object_url",
    "open_store",
    "parse_hub_revision",
    "parse_hub_tree",
    "parse_json",
    "parse_listing",
    "parse_xml",
    "query_encode",
]
