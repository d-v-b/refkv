"""Store inputs (spec/zarrify.md §1.4
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>–§1.6
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#16-json-documents>): the store
machinery, one module per concern, whose names this package gathers. It sits below
`refkv.zarrify` (whose `refkv.zarrify.store` re-exports it), so that `refkv.ir`'s store runs read
stores without importing `refkv.zarrify`.

- The rules themselves are the Rust core's (rust/refkv-ir/src/host), which `refkv._rules` gives
  by name; `urls`, `listing`, `stores` and `beside` give them Python's types and do the I/O.
- `urls`: a store URL's checks, the URL of each object (`object_url`), the listing
  endpoint of an S3-style store, and the repository of a Hub store (`hub_repo`).
- `listing`: the objects of one ListObjectsV2 response (`parse_listing`), and a Hub store's
  commit, tree page and next link.
- `documents`: the strict JSON reader of metadata documents (`parse_json`), and its limits.
- `stores`: the store readers (`Store`, `HttpStore`, `HubStore`, `DirStore`), their prefetch of
  documents, `open_store`, and the store profile a store's root keys select
  (`choose_profile`).
- `http`: the readers' requests: retries, kept-alive connections and redirects.
- `beside`: the files of a VSI or NRRD input, as a store the Rust core probes (`VsiFiles`);
  not gathered here, since it reads through `refkv.ir.planner`.

The Rust core parses every store format (rust/refkv-ir/src/zarr2.rs, n5.rs, safe/, vsi/), driven
by `refkv.ir.store`, and writes their outputs (`refkv.ir.store.StoreIrOutput`).
"""

from __future__ import annotations

from refkv.host.documents import MAX_DEPTH, MAX_DOCUMENT, MAX_SAFE, as_int, is_number, num, parse_json
from refkv.host.listing import next_link, parse_hub_revision, parse_hub_tree, parse_listing
from refkv.host.stores import (
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
from refkv.host.urls import (
    HUB,
    HubRepo,
    check_store_url,
    hub_endpoint,
    hub_page,
    hub_repo,
    listing_endpoint,
    listing_url,
    object_url,
    object_urls,
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
    "listing_url",
    "next_link",
    "num",
    "object_url",
    "object_urls",
    "open_store",
    "parse_hub_revision",
    "parse_hub_tree",
    "parse_json",
    "parse_listing",
    "query_encode",
]
